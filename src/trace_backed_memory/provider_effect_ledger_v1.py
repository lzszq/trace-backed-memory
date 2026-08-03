from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Literal, NoReturn

from .contracts_v3 import V3ContractError
from .effect_event_v1 import (
    EFFECT_EVENT_TYPES,
    EFFECT_REQUESTED_EVENT,
    EffectEventV1Error,
    ProviderEffectTransitionRef,
    build_provider_effect_transition_event,
    effect_event_stream_id,
    parse_effect_requested_event,
    parse_provider_effect_transition_event,
    provider_effect_transition_event_id,
)
from .effect_reducer_v1 import (
    build_effect_queue_reducer,
    projected_provider_effect_status,
    projected_provider_effect_transitions,
)
from .event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from .event_v1 import CanonicalEvent
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerConflictError,
    EventLedgerAtomicAppendPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerIdempotency,
)
from .reducer import ReducerEvent, ReducerExecutionError


PROVIDER_EFFECT_LEDGER_SERVICE_VERSION = "tbm.provider-effect-ledger.v1"
PROVIDER_EFFECT_LEDGER_MAX_EVENTS = 10_000
PROVIDER_EFFECT_LEDGER_MAX_CONFLICT_RETRIES = 8

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

ProviderEffectRecoveryAction = Literal[
    "start_attempt",
    "reconcile",
    "schedule_retry",
    "complete",
]


class ProviderEffectLedgerV1Error(V3ContractError):
    """Stable failure for provider-effect ledger orchestration."""


@dataclass(frozen=True)
class TrustedProviderEffectRegistration:
    provider_id: str
    model_id: str
    model_version: str
    endpoint_id: str

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "model_id",
            "model_version",
            "endpoint_id",
        ):
            value = getattr(self, name)
            if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a bounded identifier")


@dataclass(frozen=True)
class ProviderEffectRecovery:
    effect_id: str
    provider_status: str
    next_action: ProviderEffectRecoveryAction
    attempt_id: str | None
    attempt_sequence: int | None
    provider_invocation_id: str | None
    provider_request_id: str | None
    provider_receipt_id: str | None
    retry_at: str | None
    transition_ids: tuple[str, ...]
    head_event_id: str
    head_event_sha256: str


@dataclass(frozen=True)
class ProviderEffectAppendResult:
    reference: ProviderEffectTransitionRef
    receipt: LedgerAppendReceipt
    recovery: ProviderEffectRecovery
    inserted: bool = False


class ProviderEffectLedgerService:
    """Append and recover provider-effect evidence through one ledger port."""

    def __init__(
        self,
        ledger: EventLedgerAtomicAppendPort,
        provider: TrustedProviderEffectRegistration,
        *,
        max_conflict_retries: int = PROVIDER_EFFECT_LEDGER_MAX_CONFLICT_RETRIES,
        authorized_origin_decision_id: str | None = None,
    ) -> None:
        try:
            access = ledger.access_context
        except Exception as error:
            raise ValueError("ledger must expose an access context") from error
        if type(access) is not LedgerAccessContext:
            raise ValueError("ledger access context is invalid")
        if type(provider) is not TrustedProviderEffectRegistration:
            raise ValueError(
                "provider must be exactly TrustedProviderEffectRegistration"
            )
        if access.actor_type not in {"service", "worker"}:
            raise ValueError("provider effect ledger requires a service or worker")
        if (
            type(max_conflict_retries) is not int
            or not 1 <= max_conflict_retries <= 32
        ):
            raise ValueError("max_conflict_retries must be between 1 and 32")
        if authorized_origin_decision_id is not None and (
            type(authorized_origin_decision_id) is not str
            or _IDENTIFIER_RE.fullmatch(authorized_origin_decision_id) is None
        ):
            raise ValueError(
                "authorized_origin_decision_id must be a bounded identifier"
            )
        self._ledger = ledger
        self._access = access
        self._provider = provider
        self._max_conflict_retries = max_conflict_retries
        self._authorized_origin_decision_id = (
            access.authorization_decision_id
            if authorized_origin_decision_id is None
            else authorized_origin_decision_id
        )

    def recover(self, effect_id: str) -> ProviderEffectRecovery:
        try:
            events, state, _ = self._load_effect(effect_id)
        except EffectEventV1Error as error:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_INVALID",
                "effect identity is invalid",
            ) from error
        return _recovery_from_state(state, events, effect_id)

    def append_transition(
        self,
        reference: ProviderEffectTransitionRef,
        *,
        occurred_at: str,
    ) -> ProviderEffectAppendResult:
        if type(reference) is not ProviderEffectTransitionRef:
            _invalid("reference must be exactly ProviderEffectTransitionRef")
        if (
            reference.provider_id != self._provider.provider_id
            or reference.model_id != self._provider.model_id
            or reference.model_version != self._provider.model_version
            or reference.endpoint_id != self._provider.endpoint_id
        ):
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_PROVIDER_MISMATCH",
                "provider transition does not match the trusted registration",
            )
        last_conflict: EventLedgerConflictError | None = None
        for _ in range(self._max_conflict_retries):
            events, state, high_watermark = self._load_effect(
                reference.effect_id
            )
            retained = _retained_transition(events, reference)
            if retained is not None:
                if (
                    retained.authorization_decision_id
                    != self._access.authorization_decision_id
                ):
                    raise ProviderEffectLedgerV1Error(
                        "TBM_PROVIDER_EFFECT_REPLAY_AUTHORIZATION_MISMATCH",
                        "retained transition requires its original authorization",
                    )
                commit = self._ledger.append_once(
                    retained.stream_id,
                    retained.stream_version - 1,
                    (retained,),
                    LedgerIdempotency(
                        retained.idempotency_key_sha256,
                        retained.request_sha256,
                    ),
                )
                return ProviderEffectAppendResult(
                    reference=reference,
                    receipt=commit.receipt,
                    recovery=_recovery_from_state(
                        state,
                        events,
                        reference.effect_id,
                    ),
                    inserted=commit.inserted,
                )
            parent = events[-1]
            try:
                event = build_provider_effect_transition_event(
                    reference,
                    parent_event=parent,
                    global_position=high_watermark + 1,
                    occurred_at=occurred_at,
                    trusted_context=self._access.event_trusted_context(),
                )
                next_state = _reduce_effect_events((*events, event))
            except (EffectEventV1Error, ReducerExecutionError) as error:
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED",
                    "provider effect transition is not allowed",
                ) from error
            try:
                commit = self._ledger.append_once(
                    event.stream_id,
                    parent.stream_version,
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
            return ProviderEffectAppendResult(
                reference=reference,
                receipt=commit.receipt,
                recovery=_recovery_from_state(
                    next_state,
                    (*events, event),
                    reference.effect_id,
                ),
                inserted=commit.inserted,
            )
        if last_conflict is not None:
            raise last_conflict
        raise AssertionError("provider effect append retry loop did not run")

    def _load_effect(
        self,
        effect_id: str,
    ) -> tuple[tuple[CanonicalEvent, ...], Mapping[str, object], int]:
        try:
            stream_id = effect_event_stream_id(effect_id)
        except EffectEventV1Error as error:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_INVALID",
                "effect identity is invalid",
            ) from error
        events: list[CanonicalEvent] = []
        from_version = 1
        high_watermark = 0
        while True:
            page = self._ledger.read_stream(
                stream_id,
                from_version=from_version,
                limit=EVENT_LEDGER_MAX_READ_PAGE,
            )
            high_watermark = page.high_watermark_global_position
            events.extend(page.events)
            if len(events) > PROVIDER_EFFECT_LEDGER_MAX_EVENTS:
                _invalid("provider effect stream exceeds the recovery bound")
            if not page.has_more:
                break
            if page.next_stream_version is None:
                _invalid("provider effect stream page did not advance")
            from_version = page.next_stream_version
        retained = tuple(events)
        if not retained or retained[0].event_type != EFFECT_REQUESTED_EVENT:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_NOT_FOUND",
                "provider effect request is not retained",
            )
        try:
            request = parse_effect_requested_event(retained[0])
        except Exception as error:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_CORRUPT",
                "provider effect request is invalid",
            ) from error
        if request.effect.effect_id != effect_id or any(
            event.event_type not in EFFECT_EVENT_TYPES for event in retained
        ):
            _invalid("provider effect stream contains unrelated events")
        _verify_effect_access(
            retained[0],
            self._access,
            self._authorized_origin_decision_id,
        )
        try:
            state = _reduce_effect_events(retained)
        except ReducerExecutionError as error:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_CORRUPT",
                "provider effect stream cannot be rebuilt",
            ) from error
        return retained, state, high_watermark


def _retained_transition(
    events: tuple[CanonicalEvent, ...],
    reference: ProviderEffectTransitionRef,
) -> CanonicalEvent | None:
    retained: CanonicalEvent | None = None
    for event in events:
        if event.event_id != provider_effect_transition_event_id(reference):
            continue
        try:
            parsed = parse_provider_effect_transition_event(event)
        except Exception as error:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_CORRUPT",
                "retained provider transition is invalid",
            ) from error
        if parsed != reference:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_CONFLICT",
                "provider transition identity is bound to another descriptor",
            )
        retained = event
    return retained


def _reduce_effect_events(events: tuple[CanonicalEvent, ...]):
    reducer = build_effect_queue_reducer()
    state = reducer.initial_state()
    for event in events:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def _verify_effect_access(
    requested_event: CanonicalEvent,
    access: LedgerAccessContext,
    authorized_origin_decision_id: str,
) -> None:
    expected = {
        "organization_id": access.partition.organization_id,
        "tenant_id": access.partition.tenant_id,
        "repository_id": access.partition.repository_id,
        "environment_id": access.partition.environment_id,
        "principal_id": access.principal_id,
        "agent_client_id": access.agent_client_id,
        "authorization_decision_id": authorized_origin_decision_id,
    }
    if any(
        getattr(requested_event, name) != value
        for name, value in expected.items()
    ):
        raise ProviderEffectLedgerV1Error(
            "TBM_PROVIDER_EFFECT_SCOPE_DENIED",
            "provider effect is outside the authenticated access scope",
        )


def _recovery_from_state(
    state: Mapping[str, object],
    events: tuple[CanonicalEvent, ...],
    effect_id: str,
) -> ProviderEffectRecovery:
    if not isinstance(state, Mapping):
        _invalid("provider effect projection state is invalid")
    transitions = projected_provider_effect_transitions(state, effect_id)
    status = projected_provider_effect_status(state, effect_id)
    latest = None if not transitions else transitions[-1]
    provider_request_id = next(
        (
            item.provider_request_id
            for item in reversed(transitions)
            if latest is not None
            and item.attempt_id == latest.attempt_id
            and item.provider_request_id is not None
        ),
        None,
    )
    provider_receipt_id = next(
        (
            item.provider_receipt_id
            for item in reversed(transitions)
            if latest is not None
            and item.attempt_id == latest.attempt_id
            and item.provider_receipt_id is not None
        ),
        None,
    )
    retry_at = next(
        (
            item.retry_at
            for item in reversed(transitions)
            if latest is not None
            and item.attempt_id == latest.attempt_id
            and item.retry_at is not None
        ),
        None,
    )
    action: ProviderEffectRecoveryAction
    if status in {"not_started", "retry_wait"}:
        action = "start_attempt"
    elif status in {"in_flight", "submitted", "unknown"}:
        action = "reconcile"
    elif status == "not_found":
        action = "schedule_retry"
    else:
        action = "complete"
    head = events[-1]
    return ProviderEffectRecovery(
        effect_id=effect_id,
        provider_status=status,
        next_action=action,
        attempt_id=None if latest is None else latest.attempt_id,
        attempt_sequence=None if latest is None else latest.attempt_sequence,
        provider_invocation_id=(
            None if latest is None else latest.provider_invocation_id
        ),
        provider_request_id=provider_request_id,
        provider_receipt_id=provider_receipt_id,
        retry_at=retry_at,
        transition_ids=tuple(item.transition_id for item in transitions),
        head_event_id=head.event_id,
        head_event_sha256=head.event_sha256,
    )


def _invalid(message: str) -> NoReturn:
    raise ProviderEffectLedgerV1Error(
        "TBM_PROVIDER_EFFECT_INVALID",
        message,
    )


__all__ = [
    "PROVIDER_EFFECT_LEDGER_MAX_CONFLICT_RETRIES",
    "PROVIDER_EFFECT_LEDGER_MAX_EVENTS",
    "PROVIDER_EFFECT_LEDGER_SERVICE_VERSION",
    "ProviderEffectAppendResult",
    "ProviderEffectLedgerService",
    "ProviderEffectLedgerV1Error",
    "ProviderEffectRecovery",
    "ProviderEffectRecoveryAction",
    "TrustedProviderEffectRegistration",
]
