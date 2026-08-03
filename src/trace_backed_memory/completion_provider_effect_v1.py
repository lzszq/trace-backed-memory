from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Literal, NoReturn

from ._timestamps import parse_rfc3339
from .completion_outbox_v3 import CompletionOutboxDelivery, CompletionOutboxEvent
from .completion_outbox_worker_v3 import (
    CompletionOutboxConsumerError,
    CompletionOutboxConsumerReceipt,
)
from .effect_event_v1 import (
    EFFECT_DEAD_LETTERED_EVENT,
    EFFECT_FAILED_EVENT,
    EFFECT_PROVIDER_TRANSITION_EVENT,
    EFFECT_RETRY_SCHEDULED_EVENT,
    EFFECT_STARTED_EVENT,
    EFFECT_SUCCEEDED_EVENT,
    ProviderEffectTransitionRef,
    effect_event_stream_id,
    parse_effect_delivery_event,
    parse_effect_requested_event,
    parse_provider_effect_transition_event,
    provider_effect_attempt_id,
    provider_effect_invocation_id,
    provider_effect_reconciliation_id,
    provider_effect_receipt_id,
)
from .event_v1 import CanonicalEvent
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerAtomicAppendPort,
    LedgerAccessContext,
)
from .provider_effect_ledger_v1 import (
    PROVIDER_EFFECT_LEDGER_MAX_EVENTS,
    ProviderEffectAppendResult,
    ProviderEffectLedgerService,
    ProviderEffectRecovery,
    TrustedProviderEffectRegistration,
)


COMPLETION_PROVIDER_EFFECT_SERVICE_VERSION = (
    "tbm.completion-provider-effect.v1"
)
COMPLETION_PROVIDER_EFFECT_MAX_ATTEMPTS = 1000

CompletionProviderReconciliationOutcome = Literal[
    "confirmed",
    "not_found",
    "still_unknown",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DELIVERY_EVENT_TYPES = frozenset(
    {
        EFFECT_STARTED_EVENT,
        EFFECT_SUCCEEDED_EVENT,
        EFFECT_FAILED_EVENT,
        EFFECT_RETRY_SCHEDULED_EVENT,
        EFFECT_DEAD_LETTERED_EVENT,
    }
)
_PROVIDER_ERROR_CODES = frozenset(
    {
        "provider_authentication_failed",
        "provider_content_rejected",
        "provider_error",
        "provider_rate_limited",
        "provider_response_invalid",
        "provider_timeout",
        "provider_unavailable",
    }
)


class CompletionProviderCallError(RuntimeError):
    """Sanitized provider failure with an optional retained request ID."""

    def __init__(
        self,
        error_code: str,
        *,
        provider_request_id: str | None = None,
    ) -> None:
        if error_code not in _PROVIDER_ERROR_CODES:
            raise ValueError("provider error code is not supported")
        _optional_identifier(provider_request_id, "provider_request_id")
        self.error_code = error_code
        self.provider_request_id = provider_request_id
        super().__init__("completion provider call failed")


class CompletionProviderEffectRecoveryRequiredError(
    CompletionOutboxConsumerError
):
    """Consumer-safe signal that retained provider state needs recovery."""

    def __init__(self, effect_id: str, error_code: str) -> None:
        _identifier(effect_id, "effect_id")
        self.effect_id = effect_id
        super().__init__(error_code)


@dataclass(frozen=True)
class CompletionProviderCall:
    """Bounded completion notification passed to a trusted provider adapter."""

    provider_id: str
    model_id: str
    model_version: str
    endpoint_id: str
    event: CompletionOutboxEvent
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "model_id",
            "model_version",
            "endpoint_id",
        ):
            _identifier(getattr(self, name), name)
        if type(self.event) is not CompletionOutboxEvent:
            raise ValueError("event must be exactly CompletionOutboxEvent")
        _optional_identifier(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True)
class CompletionProviderResult:
    """Artifact-safe provider result retained only as IDs and a digest."""

    provider_request_id: str
    response_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.provider_request_id, "provider_request_id")
        _digest(self.response_sha256, "response_sha256")


@dataclass(frozen=True)
class CompletionProviderReconciliationCall:
    """Read-only query for one retained unknown completion-provider attempt."""

    effect_id: str
    attempt_id: str
    attempt_sequence: int
    provider_invocation_id: str
    provider_request_id: str | None
    provider_receipt_id: str | None
    request_sha256: str
    provider_call: CompletionProviderCall

    def __post_init__(self) -> None:
        for name in (
            "effect_id",
            "attempt_id",
            "provider_invocation_id",
        ):
            _identifier(getattr(self, name), name)
        if (
            type(self.attempt_sequence) is not int
            or not 1
            <= self.attempt_sequence
            <= COMPLETION_PROVIDER_EFFECT_MAX_ATTEMPTS
        ):
            raise ValueError("attempt_sequence is outside the supported range")
        _optional_identifier(
            self.provider_request_id,
            "provider_request_id",
        )
        _optional_identifier(
            self.provider_receipt_id,
            "provider_receipt_id",
        )
        _digest(self.request_sha256, "request_sha256")
        if type(self.provider_call) is not CompletionProviderCall:
            raise ValueError(
                "provider_call must be exactly CompletionProviderCall"
            )
        event = self.provider_call.event
        if (
            self.effect_id != event.event_id
            or self.request_sha256 != event.outcome_descriptor_sha256
            or self.provider_call.idempotency_key != self.effect_id
            or self.attempt_id
            != provider_effect_attempt_id(
                self.effect_id,
                self.attempt_sequence,
            )
            or self.provider_invocation_id
            != provider_effect_invocation_id(
                effect_id=self.effect_id,
                attempt_id=self.attempt_id,
                provider_id=self.provider_call.provider_id,
                model_id=self.provider_call.model_id,
                model_version=self.provider_call.model_version,
                endpoint_id=self.provider_call.endpoint_id,
                request_sha256=self.request_sha256,
            )
        ):
            raise ValueError("provider reconciliation linkage is invalid")


@dataclass(frozen=True)
class CompletionProviderReconciliationResult:
    """Trusted reconciliation result for an unknown provider attempt."""

    outcome: CompletionProviderReconciliationOutcome
    provider_result: CompletionProviderResult | None = None

    def __post_init__(self) -> None:
        if type(self.outcome) is not str or self.outcome not in {
            "confirmed",
            "not_found",
            "still_unknown",
        }:
            raise ValueError("reconciliation outcome is invalid")
        if self.outcome == "confirmed":
            if type(self.provider_result) is not CompletionProviderResult:
                raise ValueError(
                    "confirmed reconciliation requires a provider result"
                )
        elif self.provider_result is not None:
            raise ValueError(
                "non-confirmed reconciliation cannot carry a provider result"
            )


class CompletionProviderEffectConsumer:
    """Bridge one leased completion delivery through provider receipt evidence."""

    def __init__(
        self,
        *,
        ledger: EventLedgerAtomicAppendPort,
        provider: TrustedProviderEffectRegistration,
        call_provider: Callable[
            [CompletionProviderCall],
            CompletionProviderResult,
        ],
        clock: Callable[[], str],
        reconcile_provider: (
            Callable[
                [CompletionProviderReconciliationCall],
                CompletionProviderReconciliationResult,
            ]
            | None
        ) = None,
    ) -> None:
        try:
            access = ledger.access_context
        except Exception as error:
            raise ValueError("ledger must expose an access context") from error
        if type(access) is not LedgerAccessContext:
            raise ValueError("ledger access context is invalid")
        if access.actor_type != "worker":
            raise ValueError(
                "completion provider ledger requires the delivery worker"
            )
        if type(provider) is not TrustedProviderEffectRegistration:
            raise TypeError(
                "provider must be exactly TrustedProviderEffectRegistration"
            )
        if not callable(call_provider):
            raise TypeError("call_provider must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if reconcile_provider is not None and not callable(reconcile_provider):
            raise TypeError("reconcile_provider must be callable")
        self._ledger = ledger
        self._access = access
        self._provider = provider
        self._call_provider = call_provider
        self._clock = clock
        self._reconcile_provider = reconcile_provider

    def __call__(
        self,
        event: CompletionOutboxEvent,
    ) -> CompletionOutboxConsumerReceipt:
        return self.deliver(event)

    def deliver(
        self,
        event: CompletionOutboxEvent,
    ) -> CompletionOutboxConsumerReceipt:
        if type(event) is not CompletionOutboxEvent:
            raise TypeError("event must be exactly CompletionOutboxEvent")
        events, authorization_decision_id, delivery = self._load_effect(event)
        now = self._now(event.event_id)
        self._verify_active_lease(event, delivery, now)
        service = ProviderEffectLedgerService(
            self._ledger,
            self._provider,
            authorized_origin_decision_id=authorization_decision_id,
        )
        try:
            recovery = service.recover(event.event_id)
        except Exception:
            _recovery_required(event.event_id)
        call = self._call(event)
        if recovery.provider_status == "succeeded":
            return self._retained_receipt(recovery)
        if recovery.provider_status == "not_started":
            return self._invoke_attempt(
                service,
                event=event,
                call=call,
                delivery=delivery,
                attempt_sequence=1,
            )
        if recovery.provider_status in {"not_found", "retry_wait"}:
            return self._continue_retry(
                service,
                event=event,
                call=call,
                delivery=delivery,
                recovery=recovery,
            )
        if recovery.provider_status == "unknown":
            return self._reconcile_unknown(
                service,
                event=event,
                call=call,
                events=events,
                delivery=delivery,
                recovery=recovery,
            )
        if recovery.provider_status in {"in_flight", "submitted"}:
            return self._recover_fenced_attempt(
                service,
                event=event,
                call=call,
                events=events,
                delivery=delivery,
                recovery=recovery,
            )
        if recovery.provider_status == "dead_lettered":
            _recovery_required(
                event.event_id,
                "TBM_COMPLETION_PROVIDER_DEAD_LETTERED",
            )
        _recovery_required(event.event_id)

    def _invoke_attempt(
        self,
        service: ProviderEffectLedgerService,
        *,
        event: CompletionOutboxEvent,
        call: CompletionProviderCall,
        delivery: CompletionOutboxDelivery,
        attempt_sequence: int,
    ) -> CompletionOutboxConsumerReceipt:
        effect_id = event.event_id
        request_sha256 = event.outcome_descriptor_sha256
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
        started_result = self._append(service, started, event, delivery)
        if not started_result.inserted:
            _recovery_required(effect_id)
        try:
            result = self._call_provider(call)
        except CompletionProviderCallError as error:
            self._record_unknown(
                service,
                effect_id=effect_id,
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                provider_request_id=error.provider_request_id,
                error_code=error.error_code,
                event=event,
                delivery=delivery,
            )
            _recovery_required(effect_id)
        except CompletionProviderEffectRecoveryRequiredError:
            raise
        except Exception:
            self._record_unknown(
                service,
                effect_id=effect_id,
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                provider_request_id=None,
                error_code="provider_error",
                event=event,
                delivery=delivery,
            )
            _recovery_required(effect_id)
        if type(result) is not CompletionProviderResult:
            self._record_unknown(
                service,
                effect_id=effect_id,
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                provider_request_id=None,
                error_code="provider_response_invalid",
                event=event,
                delivery=delivery,
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
        self._append(service, submitted, event, delivery)
        receipt_id = provider_effect_receipt_id(
            provider_invocation_id=invocation_id,
            provider_request_id=result.provider_request_id,
            response_sha256=result.response_sha256,
        )
        receipt = self._transition(
            effect_id=effect_id,
            attempt_id=attempt_id,
            attempt_sequence=attempt_sequence,
            invocation_id=invocation_id,
            request_sha256=request_sha256,
            stage="receipt_recorded",
            provider_request_id=result.provider_request_id,
            response_sha256=result.response_sha256,
            provider_receipt_id=receipt_id,
        )
        self._append(service, receipt, event, delivery)
        return CompletionOutboxConsumerReceipt(result.response_sha256)

    def _continue_retry(
        self,
        service: ProviderEffectLedgerService,
        *,
        event: CompletionOutboxEvent,
        call: CompletionProviderCall,
        delivery: CompletionOutboxDelivery,
        recovery: ProviderEffectRecovery,
    ) -> CompletionOutboxConsumerReceipt:
        if (
            recovery.attempt_id is None
            or recovery.attempt_sequence is None
            or recovery.provider_invocation_id is None
        ):
            _recovery_required(event.event_id)
        if (
            recovery.attempt_sequence
            >= COMPLETION_PROVIDER_EFFECT_MAX_ATTEMPTS
        ):
            dead_lettered = self._transition(
                effect_id=event.event_id,
                attempt_id=recovery.attempt_id,
                attempt_sequence=recovery.attempt_sequence,
                invocation_id=recovery.provider_invocation_id,
                request_sha256=event.outcome_descriptor_sha256,
                stage="dead_lettered",
                error_code="attempts_exhausted",
            )
            self._append(service, dead_lettered, event, delivery)
            _recovery_required(
                event.event_id,
                "TBM_COMPLETION_PROVIDER_DEAD_LETTERED",
            )
        if recovery.provider_status == "not_found":
            retry_at = self._now(event.event_id).isoformat().replace(
                "+00:00",
                "Z",
            )
            scheduled = self._transition(
                effect_id=event.event_id,
                attempt_id=recovery.attempt_id,
                attempt_sequence=recovery.attempt_sequence,
                invocation_id=recovery.provider_invocation_id,
                request_sha256=event.outcome_descriptor_sha256,
                stage="retry_scheduled",
                provider_request_id=recovery.provider_request_id,
                retry_at=retry_at,
            )
            recovery = self._append(
                service,
                scheduled,
                event,
                delivery,
            ).recovery
        if (
            recovery.provider_status != "retry_wait"
            or recovery.retry_at is None
            or recovery.attempt_sequence is None
        ):
            _recovery_required(event.event_id)
        if self._now(event.event_id) < parse_rfc3339(recovery.retry_at):
            _recovery_required(event.event_id)
        return self._invoke_attempt(
            service,
            event=event,
            call=call,
            delivery=delivery,
            attempt_sequence=recovery.attempt_sequence + 1,
        )

    def _reconcile_unknown(
        self,
        service: ProviderEffectLedgerService,
        *,
        event: CompletionOutboxEvent,
        call: CompletionProviderCall,
        events: tuple[CanonicalEvent, ...],
        delivery: CompletionOutboxDelivery,
        recovery: ProviderEffectRecovery,
    ) -> CompletionOutboxConsumerReceipt:
        callback = self._reconcile_provider
        if callback is None:
            _recovery_required(event.event_id)
        reconciliation_call = self._reconciliation_call(recovery, call)
        try:
            result = callback(reconciliation_call)
        except Exception:
            _recovery_required(event.event_id)
        if type(result) is not CompletionProviderReconciliationResult:
            _recovery_required(event.event_id)
        provider_result = result.provider_result
        if provider_result is not None:
            self._verify_confirmed(recovery, provider_result)
        sequence = 1 + sum(
            1
            for retained in events
            if _is_reconciliation_for_attempt(
                retained,
                reconciliation_call.attempt_id,
            )
        )
        provider_request_id = recovery.provider_request_id
        response_sha256 = None
        receipt_id = None
        if provider_result is not None:
            provider_request_id = provider_result.provider_request_id
            response_sha256 = provider_result.response_sha256
            receipt_id = provider_effect_receipt_id(
                provider_invocation_id=(
                    reconciliation_call.provider_invocation_id
                ),
                provider_request_id=provider_request_id,
                response_sha256=response_sha256,
            )
        reconciliation_id = provider_effect_reconciliation_id(
            provider_invocation_id=(
                reconciliation_call.provider_invocation_id
            ),
            reconciliation_sequence=sequence,
            reconciliation_result=result.outcome,
            provider_request_id=provider_request_id,
            response_sha256=response_sha256,
            provider_receipt_id=receipt_id,
        )
        reference = self._transition(
            effect_id=event.event_id,
            attempt_id=reconciliation_call.attempt_id,
            attempt_sequence=reconciliation_call.attempt_sequence,
            invocation_id=reconciliation_call.provider_invocation_id,
            request_sha256=event.outcome_descriptor_sha256,
            stage="reconciled",
            provider_request_id=provider_request_id,
            response_sha256=response_sha256,
            provider_receipt_id=receipt_id,
            reconciliation_sequence=sequence,
            reconciliation_id=reconciliation_id,
            reconciliation_result=result.outcome,
        )
        self._append(service, reference, event, delivery)
        if provider_result is not None:
            return CompletionOutboxConsumerReceipt(
                provider_result.response_sha256
            )
        if result.outcome == "not_found":
            _recovery_required(
                event.event_id,
                "TBM_COMPLETION_PROVIDER_NOT_FOUND",
            )
        _recovery_required(event.event_id)

    def _recover_fenced_attempt(
        self,
        service: ProviderEffectLedgerService,
        *,
        event: CompletionOutboxEvent,
        call: CompletionProviderCall,
        events: tuple[CanonicalEvent, ...],
        delivery: CompletionOutboxDelivery,
        recovery: ProviderEffectRecovery,
    ) -> CompletionOutboxConsumerReceipt:
        if (
            recovery.attempt_id is None
            or recovery.attempt_sequence is None
            or recovery.provider_invocation_id is None
            or _attempt_owner_delivery_revision_id(
                events,
                recovery.attempt_id,
            )
            in {None, delivery.delivery_revision_id}
        ):
            _recovery_required(event.event_id)
        unknown = self._transition(
            effect_id=event.event_id,
            attempt_id=recovery.attempt_id,
            attempt_sequence=recovery.attempt_sequence,
            invocation_id=recovery.provider_invocation_id,
            request_sha256=event.outcome_descriptor_sha256,
            stage="result_unknown",
            provider_request_id=recovery.provider_request_id,
            error_code="owner_fenced",
        )
        appended = self._append(service, unknown, event, delivery)
        if appended.recovery.provider_status != "unknown":
            _recovery_required(event.event_id)
        retained, _authorization_decision_id, current_delivery = (
            self._load_effect(event)
        )
        if current_delivery != delivery:
            _recovery_required(event.event_id)
        return self._reconcile_unknown(
            service,
            event=event,
            call=call,
            events=retained,
            delivery=delivery,
            recovery=appended.recovery,
        )

    def _record_unknown(
        self,
        service: ProviderEffectLedgerService,
        *,
        effect_id: str,
        attempt_id: str,
        attempt_sequence: int,
        invocation_id: str,
        request_sha256: str,
        provider_request_id: str | None,
        error_code: str,
        event: CompletionOutboxEvent,
        delivery: CompletionOutboxDelivery,
    ) -> None:
        try:
            if provider_request_id is not None:
                submitted = self._transition(
                    effect_id=effect_id,
                    attempt_id=attempt_id,
                    attempt_sequence=attempt_sequence,
                    invocation_id=invocation_id,
                    request_sha256=request_sha256,
                    stage="request_submitted",
                    provider_request_id=provider_request_id,
                )
                self._append(service, submitted, event, delivery)
            unknown = self._transition(
                effect_id=effect_id,
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                stage="result_unknown",
                provider_request_id=provider_request_id,
                error_code=error_code,
            )
            self._append(service, unknown, event, delivery)
        except CompletionProviderEffectRecoveryRequiredError:
            pass

    def _load_effect(
        self,
        event: CompletionOutboxEvent,
    ) -> tuple[tuple[CanonicalEvent, ...], str, CompletionOutboxDelivery]:
        stream_id = effect_event_stream_id(event.event_id)
        retained: list[CanonicalEvent] = []
        from_version = 1
        while True:
            try:
                page = self._ledger.read_stream(
                    stream_id,
                    from_version=from_version,
                    limit=EVENT_LEDGER_MAX_READ_PAGE,
                )
            except Exception:
                _recovery_required(event.event_id)
            retained.extend(page.events)
            if len(retained) > PROVIDER_EFFECT_LEDGER_MAX_EVENTS:
                _recovery_required(event.event_id)
            if not page.has_more:
                break
            if page.next_stream_version is None:
                _recovery_required(event.event_id)
            from_version = page.next_stream_version
        if not retained:
            _recovery_required(event.event_id)
        try:
            requested = parse_effect_requested_event(retained[0])
        except Exception:
            _recovery_required(event.event_id)
        if (
            requested.outbox_event != event
            or requested.effect.effect_id != event.event_id
            or requested.effect.effect_type != "completion_notification"
            or requested.effect.idempotency_key != event.event_id
            or requested.effect.input_artifact_sha256
            != event.outcome_descriptor_sha256
            or requested.effect.compensation_supported
            or retained[0].tenant_id != event.tenant_id
            or retained[0].repository_id != event.repository_id
        ):
            _recovery_required(event.event_id)
        latest_delivery: CompletionOutboxDelivery | None = None
        try:
            for retained_event in retained[1:]:
                if retained_event.event_type in _DELIVERY_EVENT_TYPES:
                    latest_delivery = parse_effect_delivery_event(
                        retained_event
                    ).delivery
        except Exception:
            _recovery_required(event.event_id)
        if latest_delivery is None:
            _recovery_required(event.event_id)
        return (
            tuple(retained),
            requested.effect.authorization_event_id,
            latest_delivery,
        )

    def _verify_active_lease(
        self,
        event: CompletionOutboxEvent,
        delivery: CompletionOutboxDelivery,
        now: datetime,
    ) -> None:
        if (
            delivery.event_id != event.event_id
            or delivery.status != "leased"
            or delivery.worker_id is None
            or delivery.lease_expires_at is None
            or self._access.actor_id != delivery.worker_id
            or now >= parse_rfc3339(delivery.lease_expires_at)
        ):
            _recovery_required(event.event_id)

    def _call(self, event: CompletionOutboxEvent) -> CompletionProviderCall:
        return CompletionProviderCall(
            provider_id=self._provider.provider_id,
            model_id=self._provider.model_id,
            model_version=self._provider.model_version,
            endpoint_id=self._provider.endpoint_id,
            event=event,
            idempotency_key=event.event_id,
        )

    def _reconciliation_call(
        self,
        recovery: ProviderEffectRecovery,
        call: CompletionProviderCall,
    ) -> CompletionProviderReconciliationCall:
        if (
            recovery.attempt_id is None
            or recovery.attempt_sequence is None
            or recovery.provider_invocation_id is None
        ):
            _recovery_required(recovery.effect_id)
        return CompletionProviderReconciliationCall(
            effect_id=recovery.effect_id,
            attempt_id=recovery.attempt_id,
            attempt_sequence=recovery.attempt_sequence,
            provider_invocation_id=recovery.provider_invocation_id,
            provider_request_id=recovery.provider_request_id,
            provider_receipt_id=recovery.provider_receipt_id,
            request_sha256=call.event.outcome_descriptor_sha256,
            provider_call=replace(
                call,
                idempotency_key=recovery.effect_id,
            ),
        )

    @staticmethod
    def _verify_confirmed(
        recovery: ProviderEffectRecovery,
        result: CompletionProviderResult,
    ) -> None:
        invocation_id = recovery.provider_invocation_id
        if invocation_id is None:
            _recovery_required(recovery.effect_id)
        receipt_id = provider_effect_receipt_id(
            provider_invocation_id=invocation_id,
            provider_request_id=result.provider_request_id,
            response_sha256=result.response_sha256,
        )
        if (
            recovery.provider_request_id is not None
            and recovery.provider_request_id != result.provider_request_id
        ) or (
            recovery.response_sha256 is not None
            and recovery.response_sha256 != result.response_sha256
        ) or (
            recovery.provider_receipt_id is not None
            and recovery.provider_receipt_id != receipt_id
        ):
            _recovery_required(recovery.effect_id)

    @staticmethod
    def _retained_receipt(
        recovery: ProviderEffectRecovery,
    ) -> CompletionOutboxConsumerReceipt:
        if (
            recovery.provider_request_id is None
            or recovery.response_sha256 is None
            or recovery.provider_receipt_id is None
            or recovery.provider_invocation_id is None
            or recovery.provider_receipt_id
            != provider_effect_receipt_id(
                provider_invocation_id=recovery.provider_invocation_id,
                provider_request_id=recovery.provider_request_id,
                response_sha256=recovery.response_sha256,
            )
        ):
            _recovery_required(recovery.effect_id)
        return CompletionOutboxConsumerReceipt(recovery.response_sha256)

    def _append(
        self,
        service: ProviderEffectLedgerService,
        reference: ProviderEffectTransitionRef,
        event: CompletionOutboxEvent,
        delivery: CompletionOutboxDelivery,
    ) -> ProviderEffectAppendResult:
        effect_id = event.event_id
        try:
            events, _authorization_decision_id, current_delivery = (
                self._load_effect(event)
            )
            now = self._now(effect_id)
            if current_delivery != delivery:
                _recovery_required(effect_id)
            self._verify_active_lease(event, current_delivery, now)
            head = events[-1]
            return service.append_transition(
                reference,
                occurred_at=now.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                expected_head_event_id=head.event_id,
                expected_head_event_sha256=head.event_sha256,
            )
        except CompletionProviderEffectRecoveryRequiredError:
            raise
        except Exception:
            _recovery_required(effect_id)

    def _now(self, effect_id: str) -> datetime:
        try:
            return parse_rfc3339(self._clock())
        except Exception:
            _recovery_required(effect_id)

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
        reconciliation_result: CompletionProviderReconciliationOutcome | None = None,
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


def _attempt_owner_delivery_revision_id(
    events: tuple[CanonicalEvent, ...],
    attempt_id: str,
) -> str | None:
    latest_delivery_revision_id: str | None = None
    try:
        for event in events:
            if event.event_type in _DELIVERY_EVENT_TYPES:
                latest_delivery_revision_id = parse_effect_delivery_event(
                    event
                ).delivery.delivery_revision_id
                continue
            if event.event_type != EFFECT_PROVIDER_TRANSITION_EVENT:
                continue
            transition = parse_provider_effect_transition_event(event)
            if (
                transition.stage == "attempt_started"
                and transition.attempt_id == attempt_id
            ):
                return latest_delivery_revision_id
    except Exception:
        return None
    return None


def _is_reconciliation_for_attempt(
    event: CanonicalEvent,
    attempt_id: str,
) -> bool:
    try:
        reference = parse_provider_effect_transition_event(event)
    except Exception:
        return False
    return (
        reference.stage == "reconciled"
        and reference.attempt_id == attempt_id
    )


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded identifier")


def _optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a sha256 digest")


def _recovery_required(
    effect_id: str,
    error_code: str = "TBM_COMPLETION_PROVIDER_RECOVERY_REQUIRED",
) -> NoReturn:
    raise CompletionProviderEffectRecoveryRequiredError(
        effect_id,
        error_code,
    )


__all__ = [
    "COMPLETION_PROVIDER_EFFECT_MAX_ATTEMPTS",
    "COMPLETION_PROVIDER_EFFECT_SERVICE_VERSION",
    "CompletionProviderCall",
    "CompletionProviderCallError",
    "CompletionProviderEffectConsumer",
    "CompletionProviderEffectRecoveryRequiredError",
    "CompletionProviderReconciliationCall",
    "CompletionProviderReconciliationOutcome",
    "CompletionProviderReconciliationResult",
    "CompletionProviderResult",
]
