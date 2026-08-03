from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn

from .contracts_v3 import V3ContractError
from .effect_event_v1 import (
    EFFECT_COMPENSATED_EVENT,
    EFFECT_COMPENSATION_REQUESTED_EVENT,
    EFFECT_EVENT_TYPES,
    EFFECT_PROVIDER_TRANSITION_EVENT,
    EFFECT_REQUESTED_EVENT,
    EffectCompensatedRef,
    EffectCompensationRequestedRef,
    EffectEventV1Error,
    ProviderEffectTransitionRef,
    build_effect_compensated_event,
    build_effect_compensation_requested_event,
    build_provider_effect_transition_event,
    effect_event_stream_id,
    parse_effect_compensated_event,
    parse_effect_compensation_requested_event,
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
    "dead_letter",
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

    @property
    def descriptor_sha256(self) -> str:
        encoded = json.dumps(
            {
                "contract_version": "tbm.provider-effect-registration.v1",
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "model_version": self.model_version,
                "endpoint_id": self.endpoint_id,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(
            b"tbm.provider-effect-registration.v1\x00" + encoded
        ).hexdigest()


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


@dataclass(frozen=True)
class ProviderEffectCompensationAppendResult:
    reference: EffectCompensationRequestedRef | EffectCompensatedRef
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

    def request_compensation(
        self,
        reference: EffectCompensationRequestedRef,
        *,
        occurred_at: str,
    ) -> ProviderEffectCompensationAppendResult:
        if type(reference) is not EffectCompensationRequestedRef:
            _invalid(
                "reference must be exactly EffectCompensationRequestedRef"
            )
        compensation_id = reference.compensation_effect.effect_id
        last_conflict: EventLedgerConflictError | None = None
        for _ in range(self._max_conflict_retries):
            original_events, _state, high_watermark = self._load_effect(
                reference.original_effect_id
            )
            retained_compensation, scan_high_watermark = (
                self._read_compensation_request(
                    reference.original_effect_id
                )
            )
            target_events, target_high_watermark = self._read_effect_stream(
                compensation_id
            )
            high_watermark = max(
                high_watermark,
                scan_high_watermark,
                target_high_watermark,
            )
            if (
                retained_compensation is not None
                and retained_compensation != reference
            ):
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_CONFLICT",
                    "original effect is bound to another compensation",
                )
            if target_events:
                try:
                    retained = parse_effect_compensation_requested_event(
                        target_events[0]
                    )
                except Exception as error:
                    raise ProviderEffectLedgerV1Error(
                        "TBM_PROVIDER_EFFECT_CORRUPT",
                        "retained compensation request is invalid",
                    ) from error
                if retained != reference:
                    raise ProviderEffectLedgerV1Error(
                        "TBM_PROVIDER_EFFECT_CONFLICT",
                        "compensation identity is bound to another request",
                    )
                if retained_compensation is None:
                    raise ProviderEffectLedgerV1Error(
                        "TBM_PROVIDER_EFFECT_CORRUPT",
                        "retained compensation is absent from the global ledger",
                    )
                commit = self._ledger.append_once(
                    target_events[0].stream_id,
                    0,
                    (target_events[0],),
                    LedgerIdempotency(
                        target_events[0].idempotency_key_sha256,
                        target_events[0].request_sha256,
                    ),
                )
                retained_events, state, _ = self._load_effect(compensation_id)
                return ProviderEffectCompensationAppendResult(
                    reference,
                    commit.receipt,
                    _recovery_from_state(
                        state,
                        retained_events,
                        compensation_id,
                    ),
                    commit.inserted,
                )
            original_stream_events = tuple(
                event
                for event in original_events
                if event.stream_id
                == effect_event_stream_id(reference.original_effect_id)
            )
            if not original_stream_events:
                _invalid("compensation original stream is missing")
            try:
                event = build_effect_compensation_requested_event(
                    reference,
                    original_requested_event=original_stream_events[0],
                    original_terminal_event=original_stream_events[-1],
                    global_position=high_watermark + 1,
                    occurred_at=occurred_at,
                    trusted_context=self._access.event_trusted_context(),
                )
            except EffectEventV1Error as error:
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED",
                    "provider effect compensation is not allowed",
                ) from error
            try:
                commit = self._ledger.append_once(
                    event.stream_id,
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
            retained_events, state, _ = self._load_effect(compensation_id)
            return ProviderEffectCompensationAppendResult(
                reference,
                commit.receipt,
                _recovery_from_state(
                    state,
                    retained_events,
                    compensation_id,
                ),
                commit.inserted,
            )
        if last_conflict is not None:
            raise last_conflict
        raise AssertionError("provider compensation retry loop did not run")

    def complete_compensation(
        self,
        effect_id: str,
        *,
        occurred_at: str,
    ) -> ProviderEffectCompensationAppendResult:
        last_conflict: EventLedgerConflictError | None = None
        for _ in range(self._max_conflict_retries):
            events, state, high_watermark = self._load_effect(effect_id)
            target_events = tuple(
                event
                for event in events
                if event.stream_id == effect_event_stream_id(effect_id)
            )
            if not target_events:
                _invalid("compensation stream is missing")
            request_event = target_events[0]
            try:
                parse_effect_compensation_requested_event(request_event)
            except Exception as error:
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED",
                    "effect is not a compensation request",
                ) from error
            for retained_event in target_events[1:]:
                if retained_event.event_type != EFFECT_COMPENSATED_EVENT:
                    continue
                retained = parse_effect_compensated_event(retained_event)
                commit = self._ledger.append_once(
                    retained_event.stream_id,
                    retained_event.stream_version - 1,
                    (retained_event,),
                    LedgerIdempotency(
                        retained_event.idempotency_key_sha256,
                        retained_event.request_sha256,
                    ),
                )
                return ProviderEffectCompensationAppendResult(
                    retained,
                    commit.receipt,
                    _recovery_from_state(state, events, effect_id),
                    commit.inserted,
                )
            if projected_provider_effect_status(state, effect_id) != "succeeded":
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED",
                    "compensation requires an exact provider receipt",
                )
            receipt_event = target_events[-1]
            try:
                event = build_effect_compensated_event(
                    request_event,
                    receipt_event=receipt_event,
                    global_position=high_watermark + 1,
                    occurred_at=occurred_at,
                    trusted_context=self._access.event_trusted_context(),
                )
                next_state = _reduce_effect_events((*events, event))
            except (EffectEventV1Error, ReducerExecutionError) as error:
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED",
                    "provider compensation completion is not allowed",
                ) from error
            try:
                commit = self._ledger.append_once(
                    event.stream_id,
                    receipt_event.stream_version,
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
            return ProviderEffectCompensationAppendResult(
                parse_effect_compensated_event(event),
                commit.receipt,
                _recovery_from_state(
                    next_state,
                    (*events, event),
                    effect_id,
                ),
                commit.inserted,
            )
        if last_conflict is not None:
            raise last_conflict
        raise AssertionError("provider compensation completion loop did not run")

    def _load_effect(
        self,
        effect_id: str,
    ) -> tuple[tuple[CanonicalEvent, ...], Mapping[str, object], int]:
        retained, high_watermark = self._read_effect_stream(effect_id)
        if not retained:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_NOT_FOUND",
                "provider effect request is not retained",
            )
        reduction_events = retained
        if retained[0].event_type == EFFECT_REQUESTED_EVENT:
            try:
                request_effect_id = parse_effect_requested_event(
                    retained[0]
                ).effect.effect_id
            except Exception as error:
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_CORRUPT",
                    "provider effect request is invalid",
                ) from error
        elif retained[0].event_type == EFFECT_COMPENSATION_REQUESTED_EVENT:
            try:
                compensation = parse_effect_compensation_requested_event(
                    retained[0]
                )
                request_effect_id = compensation.compensation_effect.effect_id
                original_events, original_high_watermark = (
                    self._read_effect_stream(compensation.original_effect_id)
                )
            except Exception as error:
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_CORRUPT",
                    "provider compensation request is invalid",
                ) from error
            if (
                not original_events
                or original_events[0].event_type != EFFECT_REQUESTED_EVENT
            ):
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_CORRUPT",
                    "provider compensation original is unavailable",
                )
            high_watermark = max(high_watermark, original_high_watermark)
            reduction_events = tuple(
                sorted(
                    (*original_events, *retained),
                    key=lambda event: event.global_position,
                )
            )
        else:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_NOT_FOUND",
                "provider effect request is not retained",
            )
        if request_effect_id != effect_id or any(
            event.event_type not in EFFECT_EVENT_TYPES
            for event in reduction_events
        ):
            _invalid("provider effect stream contains unrelated events")
        _verify_effect_access(
            retained[0],
            self._access,
            self._authorized_origin_decision_id,
        )
        _verify_provider_registration(retained, self._provider)
        try:
            state = _reduce_effect_events(reduction_events)
        except ReducerExecutionError as error:
            raise ProviderEffectLedgerV1Error(
                "TBM_PROVIDER_EFFECT_CORRUPT",
                "provider effect stream cannot be rebuilt",
            ) from error
        return reduction_events, state, high_watermark

    def _read_effect_stream(
        self,
        effect_id: str,
    ) -> tuple[tuple[CanonicalEvent, ...], int]:
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
        return tuple(events), high_watermark

    def _read_compensation_request(
        self,
        original_effect_id: str,
    ) -> tuple[EffectCompensationRequestedRef | None, int]:
        retained: EffectCompensationRequestedRef | None = None
        after_position = 0
        scanned = 0
        high_watermark = 0
        while True:
            page = self._ledger.read_global(
                after_position,
                EVENT_LEDGER_MAX_READ_PAGE,
            )
            high_watermark = page.high_watermark_global_position
            scanned += len(page.events)
            if scanned > PROVIDER_EFFECT_LEDGER_MAX_EVENTS:
                _invalid("provider compensation scan exceeds the recovery bound")
            for event in page.events:
                if event.event_type != EFFECT_COMPENSATION_REQUESTED_EVENT:
                    continue
                try:
                    reference = parse_effect_compensation_requested_event(event)
                except Exception as error:
                    raise ProviderEffectLedgerV1Error(
                        "TBM_PROVIDER_EFFECT_CORRUPT",
                        "retained compensation request is invalid",
                    ) from error
                if reference.original_effect_id != original_effect_id:
                    continue
                if retained is not None and retained != reference:
                    raise ProviderEffectLedgerV1Error(
                        "TBM_PROVIDER_EFFECT_CONFLICT",
                        "original effect has multiple compensations",
                    )
                retained = reference
            if not page.has_more:
                return retained, high_watermark
            next_position = page.next_global_position
            if (
                type(next_position) is not int
                or next_position <= after_position
            ):
                _invalid("provider compensation scan did not advance")
            after_position = next_position


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


def _verify_provider_registration(
    events: tuple[CanonicalEvent, ...],
    provider: TrustedProviderEffectRegistration,
) -> None:
    expected = (
        provider.provider_id,
        provider.model_id,
        provider.model_version,
        provider.endpoint_id,
    )
    try:
        for event in events:
            if event.event_type != EFFECT_PROVIDER_TRANSITION_EVENT:
                continue
            transition = parse_provider_effect_transition_event(event)
            if (
                transition.provider_id,
                transition.model_id,
                transition.model_version,
                transition.endpoint_id,
            ) != expected:
                raise ProviderEffectLedgerV1Error(
                    "TBM_PROVIDER_EFFECT_PROVIDER_MISMATCH",
                    "retained provider transition does not match the trusted registration",
                )
    except ProviderEffectLedgerV1Error:
        raise
    except Exception as error:
        raise ProviderEffectLedgerV1Error(
            "TBM_PROVIDER_EFFECT_CORRUPT",
            "retained provider transition is invalid",
        ) from error


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
    elif status == "dead_lettered":
        action = "dead_letter"
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
    "ProviderEffectCompensationAppendResult",
    "ProviderEffectLedgerService",
    "ProviderEffectLedgerV1Error",
    "ProviderEffectRecovery",
    "ProviderEffectRecoveryAction",
    "TrustedProviderEffectRegistration",
]
