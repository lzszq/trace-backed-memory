from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import json
import re
from typing import Literal, NoReturn

from ._timestamps import aware_datetime_to_rfc3339, parse_rfc3339
from .effect_event_v1 import (
    EffectContract,
    EffectRequestedRef,
    ProviderEffectTransitionRef,
    build_effect_requested_event,
    effect_event_stream_id,
    provider_effect_attempt_id,
    provider_effect_invocation_id,
    provider_effect_reconciliation_id,
    provider_effect_receipt_id,
    parse_effect_requested_event,
    parse_provider_effect_transition_event,
)
from .event_v1 import CanonicalEvent
from .gate_session_event_v1 import parse_gate_session_event
from .gate_session_v3 import GateSession
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerConflictError,
    EventLedgerAtomicAppendPort,
    LedgerAccessContext,
    LedgerIdempotency,
)
from .provider_effect_ledger_v1 import (
    PROVIDER_EFFECT_LEDGER_MAX_CONFLICT_RETRIES,
    ProviderEffectAppendResult,
    ProviderEffectRecovery,
    ProviderEffectLedgerService,
    TrustedProviderEffectRegistration,
)
from .semantic_gate_service_v3 import (
    SemanticProviderCall,
    SemanticProviderCallError,
    SemanticProviderEffectRecoveryRequiredError,
    SemanticProviderResult,
)
from .semantic_gate_attempt_event_v1 import (
    parse_semantic_gate_attempt_event,
    semantic_gate_attempt_stream_id,
)


SEMANTIC_PROVIDER_EFFECT_SERVICE_VERSION = "tbm.semantic-provider-effect.v1"
SEMANTIC_PROVIDER_EFFECT_MAX_SESSION_EVENTS = 10_000
SEMANTIC_PROVIDER_EFFECT_MAX_EVENTS = 10_000

SemanticProviderReconciliationOutcome = Literal[
    "confirmed",
    "not_found",
    "still_unknown",
]
SemanticProviderAbandonmentReason = Literal[
    "owner_abandoned",
    "process_terminated",
    "worker_fenced",
]
SemanticProviderOwnerActorType = Literal["service", "worker"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SemanticProviderReconciliationCall:
    """Trusted provider query for one retained semantic effect attempt."""

    effect_id: str
    attempt_id: str
    attempt_sequence: int
    provider_invocation_id: str
    provider_status: str
    provider_request_id: str | None
    provider_receipt_id: str | None
    request_sha256: str
    provider_call: SemanticProviderCall

    def __post_init__(self) -> None:
        for value in (
            self.effect_id,
            self.attempt_id,
            self.provider_invocation_id,
        ):
            if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
                raise ValueError("provider reconciliation identity is invalid")
        if (
            type(self.attempt_sequence) is not int
            or not 1 <= self.attempt_sequence <= 1000
            or type(self.provider_status) is not str
            or self.provider_status
            not in {"in_flight", "submitted", "unknown", "succeeded"}
            or type(self.request_sha256) is not str
            or _DIGEST_RE.fullmatch(self.request_sha256) is None
            or type(self.provider_call) is not SemanticProviderCall
        ):
            raise ValueError("provider reconciliation state is invalid")
        for value in (self.provider_request_id, self.provider_receipt_id):
            if value is not None and (
                type(value) is not str
                or _IDENTIFIER_RE.fullmatch(value) is None
            ):
                raise ValueError("provider reconciliation reference is invalid")


@dataclass(frozen=True)
class SemanticProviderReconciliationResult:
    """Provider-specific result for one read-only reconciliation query."""

    outcome: SemanticProviderReconciliationOutcome
    provider_result: SemanticProviderResult | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {
            "confirmed",
            "not_found",
            "still_unknown",
        }:
            raise ValueError("provider reconciliation outcome is invalid")
        if self.outcome == "confirmed":
            if type(self.provider_result) is not SemanticProviderResult:
                raise ValueError(
                    "confirmed reconciliation requires a provider result"
                )
        elif self.provider_result is not None:
            raise ValueError(
                "non-confirmed reconciliation cannot carry a provider result"
            )


@dataclass(frozen=True)
class SemanticProviderEffectRetryPolicy:
    """Trusted bounded retry policy applied only after confirmed not-found."""

    max_attempts: int
    retry_delays_seconds: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 1000
            or type(self.retry_delays_seconds) is not tuple
            or len(self.retry_delays_seconds) != self.max_attempts - 1
            or any(
                type(delay) is not int or not 0 <= delay <= 86_400
                for delay in self.retry_delays_seconds
            )
        ):
            raise ValueError("provider effect retry policy is invalid")

    def retry_delay_seconds(self, attempt_sequence: int) -> int:
        if not 1 <= attempt_sequence < self.max_attempts:
            raise ValueError("provider effect attempt is not retryable")
        return self.retry_delays_seconds[attempt_sequence - 1]

    @property
    def descriptor_sha256(self) -> str:
        encoded = json.dumps(
            {
                "contract_version": "tbm.semantic-provider-retry-policy.v1",
                "max_attempts": self.max_attempts,
                "retry_delays_seconds": list(self.retry_delays_seconds),
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(
            b"tbm.semantic-provider-retry-policy.v1\x00" + encoded
        ).hexdigest()


@dataclass(frozen=True)
class SemanticProviderEffectAbandonmentRequest:
    """Trusted attestation that one exact provider attempt owner is fenced."""

    session_id: str
    expected_previous_attempt_id: str | None
    effect_id: str
    attempt_id: str
    provider_invocation_id: str
    expected_head_event_id: str
    owner_actor_type: SemanticProviderOwnerActorType
    owner_actor_id: str
    fence_token_sha256: str
    reason_code: SemanticProviderAbandonmentReason

    def __post_init__(self) -> None:
        for value in (
            self.session_id,
            self.effect_id,
            self.attempt_id,
            self.provider_invocation_id,
            self.expected_head_event_id,
            self.owner_actor_type,
            self.owner_actor_id,
            self.reason_code,
        ):
            if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
                raise ValueError("provider effect abandonment identity is invalid")
        if self.expected_previous_attempt_id is not None and (
            type(self.expected_previous_attempt_id) is not str
            or _IDENTIFIER_RE.fullmatch(self.expected_previous_attempt_id) is None
        ):
            raise ValueError("provider effect abandonment parent is invalid")
        if self.reason_code not in {
            "owner_abandoned",
            "process_terminated",
            "worker_fenced",
        }:
            raise ValueError("provider effect abandonment reason is invalid")
        if self.owner_actor_type not in {"service", "worker"}:
            raise ValueError("provider effect owner actor type is invalid")
        if _DIGEST_RE.fullmatch(self.fence_token_sha256) is None:
            raise ValueError("provider effect fence token digest is invalid")


class SemanticProviderEffectService:
    """Record one trusted semantic-provider call before returning its result."""

    def __init__(
        self,
        *,
        request_ledger: EventLedgerAtomicAppendPort,
        provider_ledger: EventLedgerAtomicAppendPort,
        provider: TrustedProviderEffectRegistration,
        clock: Callable[[], str],
        reconcile_provider: (
            Callable[
                [SemanticProviderReconciliationCall],
                SemanticProviderReconciliationResult,
            ]
            | None
        ) = None,
        retry_policy: SemanticProviderEffectRetryPolicy | None = None,
        verify_owner_fence: (
            Callable[[SemanticProviderEffectAbandonmentRequest], bool] | None
        ) = None,
        owns_ledgers: bool = False,
    ) -> None:
        request_access = _access_context(request_ledger, "request_ledger")
        provider_access = _access_context(provider_ledger, "provider_ledger")
        request_authority = _authority_identity(
            request_ledger,
            "request_ledger",
        )
        provider_authority = _authority_identity(
            provider_ledger,
            "provider_ledger",
        )
        if (
            request_access.actor_type != "agent_client"
            or request_access.actor_id != request_access.agent_client_id
            or provider_access.actor_type not in {"service", "worker"}
            or not _same_authorized_scope(request_access, provider_access)
            or request_authority is not provider_authority
        ):
            raise ValueError("provider effect ledgers have incompatible access")
        if type(provider) is not TrustedProviderEffectRegistration:
            raise TypeError(
                "provider must be exactly TrustedProviderEffectRegistration"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        if reconcile_provider is not None and not callable(reconcile_provider):
            raise TypeError("reconcile_provider must be callable")
        if retry_policy is not None and (
            type(retry_policy) is not SemanticProviderEffectRetryPolicy
        ):
            raise TypeError(
                "retry_policy must be SemanticProviderEffectRetryPolicy"
            )
        if verify_owner_fence is not None and not callable(verify_owner_fence):
            raise TypeError("verify_owner_fence must be callable")
        if type(owns_ledgers) is not bool:
            raise TypeError("owns_ledgers must be a boolean")
        if owns_ledgers and not all(
            callable(getattr(ledger, "close", None))
            for ledger in (request_ledger, provider_ledger)
        ):
            raise TypeError("owned provider effect ledgers must be closeable")
        self._request_ledger = request_ledger
        self._provider_ledger = provider_ledger
        self._provider_service = ProviderEffectLedgerService(
            provider_ledger,
            provider,
        )
        self._provider = provider
        self._provider_access = provider_access
        self._request_access = request_access
        self._clock = clock
        self._reconcile_provider = reconcile_provider
        self._retry_policy = retry_policy
        self._verify_owner_fence = verify_owner_fence
        self._closed = False
        self._pending_close_ledgers = (
            (self._provider_ledger, self._request_ledger)
            if owns_ledgers
            else ()
        )

    def close(self) -> None:
        if self._closed and not self._pending_close_ledgers:
            return
        self._closed = True
        close_error: BaseException | None = None
        pending: list[EventLedgerAtomicAppendPort] = []
        for ledger in self._pending_close_ledgers:
            try:
                ledger.close()
            except BaseException as error:
                pending.append(ledger)
                if close_error is None:
                    close_error = error
                else:
                    close_error.add_note(
                        f"also failed to close provider effect ledger: {error}"
                    )
        self._pending_close_ledgers = tuple(pending)
        if close_error is not None:
            raise close_error

    def invoke(
        self,
        *,
        session_id: str,
        expected_previous_attempt_id: str | None,
        call: SemanticProviderCall,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
    ) -> SemanticProviderResult:
        if (
            self._closed
            or type(call) is not SemanticProviderCall
            or not callable(call_provider)
        ):
            _recovery_required("effect_invalid")
        if (
            call.provider_id != self._provider.provider_id
            or call.model_id != self._provider.model_id
            or call.model_version != self._provider.model_version
            or call.endpoint_id != self._provider.endpoint_id
        ):
            _recovery_required("effect_provider_mismatch")
        parent_event, session, awaiting_event_ids = (
            self._load_awaiting_parent(session_id)
        )
        evaluation_id = session.system_gate_evaluation_id
        if evaluation_id is None:
            _recovery_required("effect_linkage_invalid")
        retained_parent_id = self._load_semantic_attempt_parent(
            session_id=session.session_id,
            system_gate_evaluation_id=evaluation_id,
        )
        if expected_previous_attempt_id != retained_parent_id:
            _recovery_required("effect_parent_mismatch")
        request_sha256 = _content_sha256(call.prompt)
        effect_id = semantic_provider_effect_id(
            session_id=session.session_id,
            system_gate_evaluation_id=evaluation_id,
            expected_previous_attempt_id=expected_previous_attempt_id,
        )
        contract = EffectContract(
            effect_id=effect_id,
            effect_type="semantic_provider_call",
            idempotency_key=_effect_idempotency_key(
                effect_id,
                self._provider,
                self._retry_policy,
            ),
            requested_by_event_id=parent_event.event_id,
            input_artifact_sha256=request_sha256,
            authorization_event_id=(
                self._request_access.authorization_decision_id
            ),
            compensation_supported=False,
        )
        events = self._load_effect_events(effect_id)
        if events:
            self._verify_existing_request(
                events,
                contract=contract,
                awaiting_event_ids=awaiting_event_ids,
            )
            return self._recover_existing(
                events=events,
                effect_id=effect_id,
                request_sha256=request_sha256,
                call=call,
                call_provider=call_provider,
            )
        if not self._append_request(contract, parent_event):
            _recovery_required(effect_id)
        return self._invoke_attempt(
            effect_id=effect_id,
            attempt_sequence=1,
            request_sha256=request_sha256,
            call=call,
            call_provider=call_provider,
        )

    def record_owner_abandonment(
        self,
        request: SemanticProviderEffectAbandonmentRequest,
        call: SemanticProviderCall,
    ) -> ProviderEffectRecovery:
        if (
            self._closed
            or type(request) is not SemanticProviderEffectAbandonmentRequest
            or type(call) is not SemanticProviderCall
        ):
            _recovery_required("effect_abandonment_invalid")
        if (
            call.provider_id != self._provider.provider_id
            or call.model_id != self._provider.model_id
            or call.model_version != self._provider.model_version
            or call.endpoint_id != self._provider.endpoint_id
        ):
            _recovery_required("effect_provider_mismatch")
        parent_event, session, awaiting_event_ids = self._load_awaiting_parent(
            request.session_id,
            require_current_authorization=False,
        )
        evaluation_id = session.system_gate_evaluation_id
        if evaluation_id is None:
            _recovery_required("effect_linkage_invalid")
        retained_parent_id = self._load_semantic_attempt_parent(
            session_id=session.session_id,
            system_gate_evaluation_id=evaluation_id,
        )
        if request.expected_previous_attempt_id != retained_parent_id:
            _recovery_required("effect_parent_mismatch")
        request_sha256 = _content_sha256(call.prompt)
        effect_id = semantic_provider_effect_id(
            session_id=session.session_id,
            system_gate_evaluation_id=evaluation_id,
            expected_previous_attempt_id=request.expected_previous_attempt_id,
        )
        if request.effect_id != effect_id:
            _recovery_required("effect_abandonment_mismatch")
        contract = EffectContract(
            effect_id=effect_id,
            effect_type="semantic_provider_call",
            idempotency_key=_effect_idempotency_key(
                effect_id,
                self._provider,
                self._retry_policy,
            ),
            requested_by_event_id=parent_event.event_id,
            input_artifact_sha256=request_sha256,
            authorization_event_id=self._request_access.authorization_decision_id,
            compensation_supported=False,
        )
        events = self._load_effect_events(effect_id)
        if not events:
            _recovery_required(effect_id)
        self._verify_existing_request(
            events,
            contract=contract,
            awaiting_event_ids=awaiting_event_ids,
        )
        try:
            requested = parse_effect_requested_event(events[0]).effect
            provider_service = ProviderEffectLedgerService(
                self._provider_ledger,
                self._provider,
                authorized_origin_decision_id=requested.authorization_event_id,
            )
            recovery = provider_service.recover(effect_id)
        except Exception:
            _recovery_required(effect_id)
        if (
            recovery.provider_status not in {"in_flight", "submitted"}
            or recovery.attempt_id != request.attempt_id
            or recovery.provider_invocation_id != request.provider_invocation_id
            or recovery.head_event_id != request.expected_head_event_id
            or recovery.attempt_sequence is None
        ):
            _recovery_required(effect_id)
        self._verify_abandonment_fence(events, request)
        reference = self._transition(
            effect_id=effect_id,
            attempt_id=request.attempt_id,
            attempt_sequence=recovery.attempt_sequence,
            invocation_id=request.provider_invocation_id,
            request_sha256=request_sha256,
            stage="result_unknown",
            provider_request_id=recovery.provider_request_id,
            error_code=request.reason_code,
        )
        return self._append_transition(
            reference,
            effect_id,
            provider_service=provider_service,
        ).recovery

    def _invoke_attempt(
        self,
        *,
        effect_id: str,
        attempt_sequence: int,
        request_sha256: str,
        call: SemanticProviderCall,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
        provider_service: ProviderEffectLedgerService | None = None,
    ) -> SemanticProviderResult:
        attempt_id = provider_effect_attempt_id(effect_id, attempt_sequence)
        invocation_id = provider_effect_invocation_id(
            effect_id=effect_id,
            attempt_id=attempt_id,
            provider_id=self._provider.provider_id,
            model_id=self._provider.model_id,
            model_version=self._provider.model_version,
            endpoint_id=self._provider.endpoint_id,
            request_sha256=request_sha256,
        )
        started = self._transition(
            effect_id=effect_id,
            attempt_id=attempt_id,
            attempt_sequence=attempt_sequence,
            invocation_id=invocation_id,
            request_sha256=request_sha256,
            stage="attempt_started",
        )
        started_result = self._append_transition(
            started,
            effect_id,
            provider_service=provider_service,
        )
        if not started_result.inserted:
            _recovery_required(effect_id)

        if call.idempotency_key not in {None, effect_id}:
            _recovery_required(effect_id)
        provider_call = replace(call, idempotency_key=effect_id)
        try:
            result = call_provider(provider_call)
        except SemanticProviderCallError as error:
            self._record_unknown(
                effect_id=effect_id,
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                provider_request_id=error.provider_request_id,
                error_code=error.error_code,
                provider_service=provider_service,
            )
            _recovery_required(effect_id)
        except SemanticProviderEffectRecoveryRequiredError:
            raise
        except Exception:
            self._record_unknown(
                effect_id=effect_id,
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                provider_request_id=None,
                error_code="provider_error",
                provider_service=provider_service,
            )
            _recovery_required(effect_id)
        if type(result) is not SemanticProviderResult:
            self._record_unknown(
                effect_id=effect_id,
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                provider_request_id=None,
                error_code="provider_response_invalid",
                provider_service=provider_service,
            )
            _recovery_required(effect_id)

        submitted = self._transition(
            effect_id=effect_id,
            attempt_id=attempt_id,
            attempt_sequence=attempt_sequence,
            invocation_id=invocation_id,
            request_sha256=request_sha256,
            stage="request_submitted",
            provider_request_id=result.provider_request_id,
        )
        self._append_transition(
            submitted,
            effect_id,
            provider_service=provider_service,
        )
        response_sha256 = semantic_provider_result_sha256(result)
        receipt_id = provider_effect_receipt_id(
            provider_invocation_id=invocation_id,
            provider_request_id=result.provider_request_id,
            response_sha256=response_sha256,
        )
        receipt = self._transition(
            effect_id=effect_id,
            attempt_id=attempt_id,
            attempt_sequence=attempt_sequence,
            invocation_id=invocation_id,
            request_sha256=request_sha256,
            stage="receipt_recorded",
            provider_request_id=result.provider_request_id,
            response_sha256=response_sha256,
            provider_receipt_id=receipt_id,
        )
        self._append_transition(
            receipt,
            effect_id,
            provider_service=provider_service,
        )
        return result

    def _load_effect_events(
        self,
        effect_id: str,
    ) -> tuple[CanonicalEvent, ...]:
        events: list[CanonicalEvent] = []
        from_version = 1
        while True:
            try:
                page = self._request_ledger.read_stream(
                    effect_event_stream_id(effect_id),
                    from_version=from_version,
                    limit=EVENT_LEDGER_MAX_READ_PAGE,
                )
            except Exception:
                _recovery_required(effect_id)
            events.extend(page.events)
            if len(events) > SEMANTIC_PROVIDER_EFFECT_MAX_EVENTS:
                _recovery_required(effect_id)
            if not page.has_more:
                return tuple(events)
            if page.next_stream_version is None:
                _recovery_required(effect_id)
            from_version = page.next_stream_version

    def _verify_existing_request(
        self,
        events: tuple[CanonicalEvent, ...],
        *,
        contract: EffectContract,
        awaiting_event_ids: frozenset[str],
    ) -> None:
        first = events[0]
        try:
            retained = parse_effect_requested_event(first).effect
        except Exception:
            _recovery_required(contract.effect_id)
        if (
            retained.effect_id != contract.effect_id
            or retained.effect_type != contract.effect_type
            or retained.idempotency_key != contract.idempotency_key
            or retained.input_artifact_sha256
            != contract.input_artifact_sha256
            or retained.compensation_supported
            != contract.compensation_supported
            or retained.requested_by_event_id not in awaiting_event_ids
            or not _same_event_scope(first, self._request_access)
        ):
            _recovery_required(contract.effect_id)
        try:
            for event in events[1:]:
                transition = parse_provider_effect_transition_event(event)
                if (
                    transition.provider_id != self._provider.provider_id
                    or transition.model_id != self._provider.model_id
                    or transition.model_version
                    != self._provider.model_version
                    or transition.endpoint_id != self._provider.endpoint_id
                    or transition.request_sha256
                    != contract.input_artifact_sha256
                ):
                    _recovery_required(contract.effect_id)
        except SemanticProviderEffectRecoveryRequiredError:
            raise
        except Exception:
            _recovery_required(contract.effect_id)

    def _recover_existing(
        self,
        *,
        events: tuple[CanonicalEvent, ...],
        effect_id: str,
        request_sha256: str,
        call: SemanticProviderCall,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
    ) -> SemanticProviderResult:
        try:
            requested = parse_effect_requested_event(events[0]).effect
            provider_service = ProviderEffectLedgerService(
                self._provider_ledger,
                self._provider,
                authorized_origin_decision_id=(
                    requested.authorization_event_id
                ),
            )
            recovery = provider_service.recover(effect_id)
        except Exception:
            _recovery_required(effect_id)
        if recovery.provider_status == "not_started":
            return self._invoke_attempt(
                effect_id=effect_id,
                attempt_sequence=1,
                request_sha256=request_sha256,
                call=call,
                call_provider=call_provider,
                provider_service=provider_service,
            )
        if recovery.provider_status in {"not_found", "retry_wait"}:
            return self._continue_bounded_retry(
                events=events,
                recovery=recovery,
                request_sha256=request_sha256,
                call=call,
                call_provider=call_provider,
                provider_service=provider_service,
            )
        if recovery.provider_status in {"in_flight", "submitted"}:
            _recovery_required(effect_id)
        if recovery.provider_status not in {"unknown", "succeeded"}:
            _recovery_required(effect_id)
        callback = self._reconcile_provider
        if callback is None:
            _recovery_required(effect_id)
        reconciliation_call = self._reconciliation_call(
            recovery,
            request_sha256=request_sha256,
            call=call,
        )
        try:
            reconciled = callback(reconciliation_call)
        except Exception:
            _recovery_required(effect_id)
        if type(reconciled) is not SemanticProviderReconciliationResult:
            _recovery_required(effect_id)
        if reconciled.outcome == "confirmed":
            result = reconciled.provider_result
            if type(result) is not SemanticProviderResult:
                _recovery_required(effect_id)
            self._verify_confirmed_reconciliation(recovery, result)
            if recovery.provider_status == "succeeded":
                return result
            self._append_reconciliation(
                events=events,
                recovery=recovery,
                request_sha256=request_sha256,
                outcome="confirmed",
                result=result,
                provider_service=provider_service,
            )
            return result
        if recovery.provider_status == "succeeded":
            _recovery_required(effect_id)
        self._append_reconciliation(
            events=events,
            recovery=recovery,
            request_sha256=request_sha256,
            outcome=reconciled.outcome,
            result=None,
            provider_service=provider_service,
        )
        if reconciled.outcome == "not_found":
            retained_events = self._load_effect_events(effect_id)
            try:
                retained_recovery = provider_service.recover(effect_id)
            except Exception:
                _recovery_required(effect_id)
            return self._continue_bounded_retry(
                events=retained_events,
                recovery=retained_recovery,
                request_sha256=request_sha256,
                call=call,
                call_provider=call_provider,
                provider_service=provider_service,
            )
        _recovery_required(effect_id)

    def _continue_bounded_retry(
        self,
        *,
        events: tuple[CanonicalEvent, ...],
        recovery: ProviderEffectRecovery,
        request_sha256: str,
        call: SemanticProviderCall,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
        provider_service: ProviderEffectLedgerService,
    ) -> SemanticProviderResult:
        policy = self._retry_policy
        if policy is None or recovery.attempt_sequence is None:
            _recovery_required(recovery.effect_id)
        expected_retry_at: str | None = None
        if recovery.provider_status == "not_found":
            if recovery.attempt_sequence >= policy.max_attempts:
                dead_lettered = self._transition(
                    effect_id=recovery.effect_id,
                    attempt_id=_required_identifier(
                        recovery.attempt_id,
                        recovery.effect_id,
                    ),
                    attempt_sequence=recovery.attempt_sequence,
                    invocation_id=_required_identifier(
                        recovery.provider_invocation_id,
                        recovery.effect_id,
                    ),
                    request_sha256=request_sha256,
                    stage="dead_lettered",
                    error_code="attempts_exhausted",
                )
                self._append_transition(
                    dead_lettered,
                    recovery.effect_id,
                    provider_service=provider_service,
                )
                _recovery_required(recovery.effect_id)
            scheduled_retry_at = _shift_timestamp(
                events[-1].occurred_at,
                policy.retry_delay_seconds(recovery.attempt_sequence),
                recovery.effect_id,
            )
            expected_retry_at = scheduled_retry_at
            scheduled = self._transition(
                effect_id=recovery.effect_id,
                attempt_id=_required_identifier(
                    recovery.attempt_id,
                    recovery.effect_id,
                ),
                attempt_sequence=recovery.attempt_sequence,
                invocation_id=_required_identifier(
                    recovery.provider_invocation_id,
                    recovery.effect_id,
                ),
                request_sha256=request_sha256,
                stage="retry_scheduled",
                retry_at=scheduled_retry_at,
            )
            recovery = self._append_transition(
                scheduled,
                recovery.effect_id,
                provider_service=provider_service,
            ).recovery
        elif recovery.provider_status == "retry_wait":
            expected_retry_at = _retained_retry_at(
                events,
                recovery,
                policy,
            )
        if (
            recovery.provider_status != "retry_wait"
            or recovery.retry_at is None
            or recovery.attempt_sequence is None
            or recovery.retry_at != expected_retry_at
        ):
            _recovery_required(recovery.effect_id)
        try:
            now = parse_rfc3339(self._clock())
            retained_retry_at = parse_rfc3339(recovery.retry_at)
        except Exception:
            _recovery_required(recovery.effect_id)
        if now < retained_retry_at:
            _recovery_required(recovery.effect_id)
        return self._invoke_attempt(
            effect_id=recovery.effect_id,
            attempt_sequence=recovery.attempt_sequence + 1,
            request_sha256=request_sha256,
            call=call,
            call_provider=call_provider,
            provider_service=provider_service,
        )

    def _load_semantic_attempt_parent(
        self,
        *,
        session_id: str,
        system_gate_evaluation_id: str,
    ) -> str | None:
        try:
            stream_id = semantic_gate_attempt_stream_id(
                system_gate_evaluation_id
            )
        except Exception:
            _recovery_required("effect_parent_invalid")
        from_version = 1
        latest_attempt_id: str | None = None
        expected_sequence = 1
        total_events = 0
        while True:
            try:
                page = self._request_ledger.read_stream(
                    stream_id,
                    from_version=from_version,
                    limit=EVENT_LEDGER_MAX_READ_PAGE,
                )
            except Exception:
                _recovery_required("effect_parent_unavailable")
            total_events += len(page.events)
            if total_events > SEMANTIC_PROVIDER_EFFECT_MAX_SESSION_EVENTS:
                _recovery_required("effect_parent_too_large")
            try:
                for event in page.events:
                    reference = parse_semantic_gate_attempt_event(event)
                    if (
                        reference.session_id != session_id
                        or reference.system_gate_evaluation_id
                        != system_gate_evaluation_id
                        or reference.sequence != expected_sequence
                        or reference.previous_attempt_id != latest_attempt_id
                        or not _same_event_scope(event, self._request_access)
                    ):
                        _recovery_required("effect_parent_invalid")
                    latest_attempt_id = reference.attempt_id
                    expected_sequence += 1
            except SemanticProviderEffectRecoveryRequiredError:
                raise
            except Exception:
                _recovery_required("effect_parent_invalid")
            if not page.has_more:
                return latest_attempt_id
            if page.next_stream_version is None:
                _recovery_required("effect_parent_invalid")
            from_version = page.next_stream_version

    def _reconciliation_call(
        self,
        recovery: ProviderEffectRecovery,
        *,
        request_sha256: str,
        call: SemanticProviderCall,
    ) -> SemanticProviderReconciliationCall:
        if (
            recovery.attempt_id is None
            or recovery.attempt_sequence is None
            or recovery.provider_invocation_id is None
        ):
            _recovery_required(recovery.effect_id)
        try:
            return SemanticProviderReconciliationCall(
                effect_id=recovery.effect_id,
                attempt_id=recovery.attempt_id,
                attempt_sequence=recovery.attempt_sequence,
                provider_invocation_id=recovery.provider_invocation_id,
                provider_status=recovery.provider_status,
                provider_request_id=recovery.provider_request_id,
                provider_receipt_id=recovery.provider_receipt_id,
                request_sha256=request_sha256,
                provider_call=replace(
                    call,
                    idempotency_key=recovery.effect_id,
                ),
            )
        except Exception:
            _recovery_required(recovery.effect_id)

    def _verify_abandonment_fence(
        self,
        events: tuple[CanonicalEvent, ...],
        request: SemanticProviderEffectAbandonmentRequest,
    ) -> None:
        verifier = self._verify_owner_fence
        if verifier is None:
            _recovery_required(request.effect_id)
        owner_event: CanonicalEvent | None = None
        try:
            for event in events[1:]:
                transition = parse_provider_effect_transition_event(event)
                if (
                    transition.stage == "attempt_started"
                    and transition.attempt_id == request.attempt_id
                    and transition.provider_invocation_id
                    == request.provider_invocation_id
                ):
                    owner_event = event
                    break
        except Exception:
            _recovery_required(request.effect_id)
        if (
            owner_event is None
            or owner_event.actor_type != request.owner_actor_type
            or owner_event.actor_id != request.owner_actor_id
            or owner_event.actor_type != self._provider_access.actor_type
            or owner_event.actor_id != self._provider_access.actor_id
        ):
            _recovery_required(request.effect_id)
        try:
            verified = verifier(request)
        except Exception:
            _recovery_required(request.effect_id)
        if verified is not True:
            _recovery_required(request.effect_id)

    def _verify_confirmed_reconciliation(
        self,
        recovery: ProviderEffectRecovery,
        result: SemanticProviderResult,
    ) -> None:
        invocation_id = recovery.provider_invocation_id
        if invocation_id is None:
            _recovery_required(recovery.effect_id)
        response_sha256 = semantic_provider_result_sha256(result)
        receipt_id = provider_effect_receipt_id(
            provider_invocation_id=invocation_id,
            provider_request_id=result.provider_request_id,
            response_sha256=response_sha256,
        )
        if (
            recovery.provider_request_id is not None
            and recovery.provider_request_id != result.provider_request_id
        ) or (
            recovery.provider_receipt_id is not None
            and recovery.provider_receipt_id != receipt_id
        ):
            _recovery_required(recovery.effect_id)

    def _append_reconciliation(
        self,
        *,
        events: tuple[CanonicalEvent, ...],
        recovery: ProviderEffectRecovery,
        request_sha256: str,
        outcome: SemanticProviderReconciliationOutcome,
        result: SemanticProviderResult | None,
        provider_service: ProviderEffectLedgerService,
    ) -> None:
        if (
            recovery.attempt_id is None
            or recovery.attempt_sequence is None
            or recovery.provider_invocation_id is None
        ):
            _recovery_required(recovery.effect_id)
        sequence = 1 + sum(
            1
            for event in events[1:]
            if _is_reconciliation_for_attempt(event, recovery.attempt_id)
        )
        provider_request_id = recovery.provider_request_id
        response_sha256 = None
        receipt_id = None
        if result is not None:
            provider_request_id = result.provider_request_id
            response_sha256 = semantic_provider_result_sha256(result)
            receipt_id = provider_effect_receipt_id(
                provider_invocation_id=recovery.provider_invocation_id,
                provider_request_id=provider_request_id,
                response_sha256=response_sha256,
            )
        reconciliation_id = provider_effect_reconciliation_id(
            provider_invocation_id=recovery.provider_invocation_id,
            reconciliation_sequence=sequence,
            reconciliation_result=outcome,
            provider_request_id=provider_request_id,
            response_sha256=response_sha256,
            provider_receipt_id=receipt_id,
        )
        reference = self._transition(
            effect_id=recovery.effect_id,
            attempt_id=recovery.attempt_id,
            attempt_sequence=recovery.attempt_sequence,
            invocation_id=recovery.provider_invocation_id,
            request_sha256=request_sha256,
            stage="reconciled",
            provider_request_id=provider_request_id,
            response_sha256=response_sha256,
            provider_receipt_id=receipt_id,
            reconciliation_sequence=sequence,
            reconciliation_id=reconciliation_id,
            reconciliation_result=outcome,
        )
        self._append_transition(
            reference,
            recovery.effect_id,
            provider_service=provider_service,
        )

    def _append_request(
        self,
        contract: EffectContract,
        parent_event: CanonicalEvent,
    ) -> bool:
        stream_id = effect_event_stream_id(contract.effect_id)
        last_conflict: EventLedgerConflictError | None = None
        for _ in range(PROVIDER_EFFECT_LEDGER_MAX_CONFLICT_RETRIES):
            try:
                page = self._request_ledger.read_stream(stream_id, limit=1)
            except Exception:
                _recovery_required(contract.effect_id)
            if page.events:
                _recovery_required(contract.effect_id)
            event = build_effect_requested_event(
                EffectRequestedRef(contract),
                requested_by_event=parent_event,
                global_position=page.high_watermark_global_position + 1,
                trusted_context=self._request_access.event_trusted_context(),
            )
            try:
                commit = self._request_ledger.append_once(
                    stream_id,
                    0,
                    (event,),
                    LedgerIdempotency(
                        event.idempotency_key_sha256,
                        event.request_sha256,
                    ),
                )
            except EventLedgerConflictError as error:
                if error.code not in {
                    "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT",
                    "TBM_EVENT_LEDGER_HEAD_MISMATCH",
                    "TBM_EVENT_LEDGER_STALE_STREAM_VERSION",
                }:
                    raise
                last_conflict = error
                continue
            except Exception:
                _recovery_required(contract.effect_id)
            return commit.inserted
        if last_conflict is not None:
            _recovery_required(contract.effect_id)
        _recovery_required(contract.effect_id)

    def _append_transition(
        self,
        reference: ProviderEffectTransitionRef,
        effect_id: str,
        *,
        provider_service: ProviderEffectLedgerService | None = None,
    ) -> ProviderEffectAppendResult:
        try:
            selected_service = provider_service or self._provider_service
            return selected_service.append_transition(
                reference,
                occurred_at=self._clock(),
            )
        except Exception:
            _recovery_required(effect_id)

    def _record_unknown(
        self,
        *,
        effect_id: str,
        attempt_id: str,
        attempt_sequence: int,
        invocation_id: str,
        request_sha256: str,
        provider_request_id: str | None,
        error_code: str,
        provider_service: ProviderEffectLedgerService | None = None,
        record_submission: bool = True,
    ) -> None:
        try:
            if record_submission and provider_request_id is not None:
                self._append_transition(
                    self._transition(
                        effect_id=effect_id,
                        attempt_id=attempt_id,
                        attempt_sequence=attempt_sequence,
                        invocation_id=invocation_id,
                        request_sha256=request_sha256,
                        stage="request_submitted",
                        provider_request_id=provider_request_id,
                    ),
                    effect_id,
                    provider_service=provider_service,
                )
            self._append_transition(
                self._transition(
                    effect_id=effect_id,
                    attempt_id=attempt_id,
                    attempt_sequence=attempt_sequence,
                    invocation_id=invocation_id,
                    request_sha256=request_sha256,
                    stage="result_unknown",
                    provider_request_id=provider_request_id,
                    error_code=error_code,
                ),
                effect_id,
                provider_service=provider_service,
            )
        except SemanticProviderEffectRecoveryRequiredError:
            pass

    def _load_awaiting_parent(
        self,
        session_id: str,
        *,
        require_current_authorization: bool = True,
    ) -> tuple[CanonicalEvent, GateSession, frozenset[str]]:
        if type(require_current_authorization) is not bool:
            _recovery_required("effect_session_invalid")
        events: list[CanonicalEvent] = []
        from_version = 1
        while True:
            try:
                page = self._request_ledger.read_stream(
                    session_id,
                    from_version=from_version,
                    limit=EVENT_LEDGER_MAX_READ_PAGE,
                )
            except Exception:
                _recovery_required("effect_session_unavailable")
            events.extend(page.events)
            if len(events) > SEMANTIC_PROVIDER_EFFECT_MAX_SESSION_EVENTS:
                _recovery_required("effect_session_too_large")
            if not page.has_more:
                break
            if page.next_stream_version is None:
                _recovery_required("effect_session_invalid")
            from_version = page.next_stream_version
        previous_session: GateSession | None = None
        parent_event: CanonicalEvent | None = None
        awaiting_event_ids: set[str] = set()
        try:
            for event in events:
                previous_session = parse_gate_session_event(
                    event,
                    previous_session=previous_session,
                    parent_event=parent_event,
                )
                parent_event = event
                if previous_session.status == "awaiting_decision":
                    awaiting_event_ids.add(event.event_id)
        except Exception:
            _recovery_required("effect_session_invalid")
        if (
            parent_event is None
            or previous_session is None
            or previous_session.session_id != session_id
            or previous_session.status != "awaiting_decision"
            or (
                require_current_authorization
                and parent_event.authorization_decision_id
                != self._request_access.authorization_decision_id
            )
        ):
            _recovery_required("effect_session_not_awaiting")
        return parent_event, previous_session, frozenset(awaiting_event_ids)

    def _transition(
        self,
        *,
        effect_id: str,
        attempt_id: str,
        attempt_sequence: int,
        invocation_id: str,
        request_sha256: str,
        stage: str,
        provider_request_id: str | None = None,
        response_sha256: str | None = None,
        provider_receipt_id: str | None = None,
        error_code: str | None = None,
        reconciliation_sequence: int | None = None,
        reconciliation_id: str | None = None,
        reconciliation_result: SemanticProviderReconciliationOutcome | None = None,
        retry_at: str | None = None,
    ) -> ProviderEffectTransitionRef:
        return ProviderEffectTransitionRef(
            effect_id=effect_id,
            attempt_id=attempt_id,
            attempt_sequence=attempt_sequence,
            provider_invocation_id=invocation_id,
            stage=stage,
            provider_id=self._provider.provider_id,
            model_id=self._provider.model_id,
            model_version=self._provider.model_version,
            endpoint_id=self._provider.endpoint_id,
            request_sha256=request_sha256,
            provider_request_id=provider_request_id,
            response_sha256=response_sha256,
            provider_receipt_id=provider_receipt_id,
            error_code=error_code,
            reconciliation_sequence=reconciliation_sequence,
            reconciliation_id=reconciliation_id,
            reconciliation_result=reconciliation_result,
            retry_at=retry_at,
        )


def semantic_provider_effect_id(
    *,
    session_id: str,
    system_gate_evaluation_id: str,
    expected_previous_attempt_id: str | None,
) -> str:
    payload = {
        "contract_version": SEMANTIC_PROVIDER_EFFECT_SERVICE_VERSION,
        "session_id": session_id,
        "system_gate_evaluation_id": system_gate_evaluation_id,
        "expected_previous_attempt_id": expected_previous_attempt_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "effect_semantic_sha256_" + hashlib.sha256(encoded).hexdigest()


def _effect_idempotency_key(
    effect_id: str,
    provider: TrustedProviderEffectRegistration,
    retry_policy: SemanticProviderEffectRetryPolicy | None,
) -> str:
    key = f"{effect_id}:{provider.descriptor_sha256}"
    if retry_policy is not None:
        key = f"{key}:{retry_policy.descriptor_sha256}"
    return key


def _retained_retry_at(
    events: tuple[CanonicalEvent, ...],
    recovery: ProviderEffectRecovery,
    policy: SemanticProviderEffectRetryPolicy,
) -> str:
    try:
        scheduled_event = events[-1]
        reconciled_event = events[-2]
        scheduled = parse_provider_effect_transition_event(scheduled_event)
        reconciled = parse_provider_effect_transition_event(reconciled_event)
        if (
            scheduled.stage != "retry_scheduled"
            or scheduled.effect_id != recovery.effect_id
            or scheduled.attempt_id != recovery.attempt_id
            or scheduled.attempt_sequence != recovery.attempt_sequence
            or scheduled.retry_at != recovery.retry_at
            or reconciled.stage != "reconciled"
            or reconciled.reconciliation_result != "not_found"
            or reconciled.effect_id != recovery.effect_id
            or reconciled.attempt_id != recovery.attempt_id
            or reconciled.attempt_sequence != recovery.attempt_sequence
        ):
            _recovery_required(recovery.effect_id)
        return _shift_timestamp(
            reconciled_event.occurred_at,
            policy.retry_delay_seconds(recovery.attempt_sequence),
            recovery.effect_id,
        )
    except SemanticProviderEffectRecoveryRequiredError:
        raise
    except Exception:
        _recovery_required(recovery.effect_id)


def semantic_provider_result_sha256(
    result: SemanticProviderResult,
) -> str:
    if type(result) is not SemanticProviderResult:
        raise TypeError("result must be exactly SemanticProviderResult")
    payload = {
        "contract_version": SEMANTIC_PROVIDER_EFFECT_SERVICE_VERSION,
        "response_sha256": _content_sha256(result.response),
        "provider_request_id": result.provider_request_id,
        "decision_id": result.decision_id,
        "final_allowed_revision_ids": list(
            result.final_allowed_revision_ids
        ),
        "final_blocked_revision_ids": list(
            result.final_blocked_revision_ids
        ),
        "reason": result.reason,
        "risk": result.risk,
        "recommended_injection": result.recommended_injection,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(
        b"tbm.semantic-provider-result.v1\x00" + encoded
    ).hexdigest()


def _access_context(
    ledger: EventLedgerAtomicAppendPort,
    name: str,
) -> LedgerAccessContext:
    try:
        access = ledger.access_context
    except Exception as error:
        raise ValueError(f"{name} must expose an access context") from error
    if type(access) is not LedgerAccessContext:
        raise ValueError(f"{name} access context is invalid")
    return access


def _authority_identity(
    ledger: EventLedgerAtomicAppendPort,
    name: str,
) -> object:
    try:
        identity = ledger.authority_identity
    except Exception as error:
        raise ValueError(f"{name} must expose an authority identity") from error
    if identity is None:
        raise ValueError(f"{name} authority identity is invalid")
    return identity


def _same_authorized_scope(
    left: LedgerAccessContext,
    right: LedgerAccessContext,
) -> bool:
    return (
        left.partition == right.partition
        and left.principal_id == right.principal_id
        and left.agent_client_id == right.agent_client_id
        and left.authorization_decision_id == right.authorization_decision_id
    )


def _same_event_scope(
    event: CanonicalEvent,
    access: LedgerAccessContext,
) -> bool:
    return (
        event.organization_id == access.partition.organization_id
        and event.tenant_id == access.partition.tenant_id
        and event.repository_id == access.partition.repository_id
        and event.environment_id == access.partition.environment_id
        and event.principal_id == access.principal_id
        and event.agent_client_id == access.agent_client_id
    )


def _is_reconciliation_for_attempt(
    event: CanonicalEvent,
    attempt_id: str,
) -> bool:
    try:
        reference = parse_provider_effect_transition_event(event)
    except Exception:
        _recovery_required("effect_reconciliation_invalid")
    return reference.stage == "reconciled" and reference.attempt_id == attempt_id


def _content_sha256(content: bytes) -> str:
    if type(content) is not bytes or not content:
        _recovery_required("effect_content_invalid")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _required_identifier(value: str | None, effect_id: str) -> str:
    if value is None:
        _recovery_required(effect_id)
    return value


def _shift_timestamp(value: str, seconds: int, effect_id: str) -> str:
    try:
        return aware_datetime_to_rfc3339(
            parse_rfc3339(value) + timedelta(seconds=seconds)
        )
    except Exception:
        _recovery_required(effect_id)


def _recovery_required(effect_id: str) -> NoReturn:
    raise SemanticProviderEffectRecoveryRequiredError(effect_id)


__all__ = [
    "SEMANTIC_PROVIDER_EFFECT_MAX_EVENTS",
    "SEMANTIC_PROVIDER_EFFECT_MAX_SESSION_EVENTS",
    "SEMANTIC_PROVIDER_EFFECT_SERVICE_VERSION",
    "SemanticProviderAbandonmentReason",
    "SemanticProviderEffectAbandonmentRequest",
    "SemanticProviderEffectRetryPolicy",
    "SemanticProviderEffectService",
    "SemanticProviderOwnerActorType",
    "SemanticProviderReconciliationCall",
    "SemanticProviderReconciliationOutcome",
    "SemanticProviderReconciliationResult",
    "semantic_provider_effect_id",
    "semantic_provider_result_sha256",
]
