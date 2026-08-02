from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import NoReturn, Protocol, cast

from ._timestamps import (
    aware_datetime_to_rfc3339,
    canonical_rfc3339,
    parse_rfc3339,
)
from .completion_outbox_v3 import (
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    build_completion_outbox_event,
    build_initial_completion_outbox_delivery,
    dumps_completion_outbox_delivery,
    dumps_completion_outbox_event,
    loads_completion_outbox_delivery,
    loads_completion_outbox_event,
    verify_completion_outbox_delivery_transition,
    verify_completion_outbox_event,
)
from .contracts_v3 import canonical_sha256
from .event_registry_v1 import EventPayloadRegistration, EventTypeRegistry
from .event_v1 import CanonicalEvent, build_canonical_event, verify_event_parent
from .gate_session_v3 import GateSession
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerConflictError,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerIdempotency,
)
from .outcome_v3 import (
    OutcomeAttribution,
    RunOutcome,
    dumps_outcome_attribution,
    dumps_run_outcome,
    loads_outcome_attribution,
    loads_run_outcome,
    verify_outcome_attribution,
    verify_run_outcome,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerV1Error,
    execute_reducer_step,
    initial_reducer_state,
)


OUTCOME_EFFECT_EVENT_PROTOCOL_VERSION = "tbm.outcome-effect-event.v1"
OUTCOME_EFFECT_EVENT_STREAM_TYPE = "outcome_effect"
OUTCOME_EFFECT_EVENT_MAX_BATCH = 256
OUTCOME_EFFECT_EVENT_MAX_APPEND_RETRIES = 8
OUTCOME_EFFECT_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "outcome_effect_event_payload_registry_v1.schema.json"
)

RUN_OUTCOME_RECORDED = "tbm.execution.run_outcome_recorded"
OUTCOME_ATTRIBUTION_RECORDED = "tbm.execution.outcome_attribution_recorded"
EFFECT_REQUESTED = "tbm.effect.requested"
EFFECT_STARTED = "tbm.effect.started"
EFFECT_RETRY_SCHEDULED = "tbm.effect.retry_scheduled"
EFFECT_SUCCEEDED = "tbm.effect.succeeded"
EFFECT_DEAD_LETTERED = "tbm.effect.dead_lettered"
EFFECT_COMPENSATION_REQUESTED = "tbm.effect.compensation_requested"
EFFECT_COMPENSATED = "tbm.effect.compensated"

OUTCOME_EFFECT_EVENT_TYPES = tuple(
    sorted(
        (
            RUN_OUTCOME_RECORDED,
            OUTCOME_ATTRIBUTION_RECORDED,
            EFFECT_REQUESTED,
            EFFECT_STARTED,
            EFFECT_RETRY_SCHEDULED,
            EFFECT_SUCCEEDED,
            EFFECT_DEAD_LETTERED,
            EFFECT_COMPENSATION_REQUESTED,
            EFFECT_COMPENSATED,
        )
    )
)
EFFECT_EVENT_TYPES = tuple(
    event_type
    for event_type in OUTCOME_EFFECT_EVENT_TYPES
    if event_type.startswith("tbm.effect.")
)

RUN_OUTCOME_PROJECTION = "run_outcome_current_v1"
OUTCOME_ATTRIBUTION_PROJECTION = "outcome_attribution_v1"
EFFECT_QUEUE_PROJECTION = "effect_queue_v1"
EFFECT_DELIVERY_HISTORY_PROJECTION = "effect_delivery_history_v1"
EFFECT_DEAD_LETTER_PROJECTION = "effect_dead_letter_v1"
EFFECT_COMPENSATION_PROJECTION = "effect_compensation_v1"

RUN_OUTCOME_REDUCER_ID = "run-outcome-current"
OUTCOME_ATTRIBUTION_REDUCER_ID = "outcome-attribution"
EFFECT_QUEUE_REDUCER_ID = "effect-queue"
EFFECT_DELIVERY_HISTORY_REDUCER_ID = "effect-delivery-history"
EFFECT_DEAD_LETTER_REDUCER_ID = "effect-dead-letter"
EFFECT_COMPENSATION_REDUCER_ID = "effect-compensation"

_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in OUTCOME_EFFECT_EVENT_TYPES
}
_EFFECT_STATUS_BY_EVENT = {
    EFFECT_REQUESTED: "ready",
    EFFECT_STARTED: "leased",
    EFFECT_RETRY_SCHEDULED: "retry",
    EFFECT_SUCCEEDED: "succeeded",
    EFFECT_DEAD_LETTERED: "dead_letter",
    EFFECT_COMPENSATION_REQUESTED: "compensation_requested",
    EFFECT_COMPENSATED: "compensated",
}
_DELIVERY_EVENT_BY_STATUS = {
    "pending": EFFECT_REQUESTED,
    "leased": EFFECT_STARTED,
    "retry_wait": EFFECT_RETRY_SCHEDULED,
    "delivered": EFFECT_SUCCEEDED,
    "dead_letter": EFFECT_DEAD_LETTERED,
}


class OutcomeEffectEventV1Error(ReducerV1Error):
    """Stable failure for Outcome and Effect event projections."""


@dataclass(frozen=True)
class OutcomeEffectEventDraft:
    event_type: str
    session_id: str
    record_id: str
    occurred_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.event_type not in OUTCOME_EFFECT_EVENT_TYPES:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_TYPE_INVALID",
                "Outcome/Effect event type is invalid",
            )
        _identifier(self.session_id, "session_id")
        _identifier(self.record_id, "record_id")
        try:
            canonical_rfc3339(self.occurred_at)
        except (TypeError, ValueError) as error:
            raise OutcomeEffectEventV1Error(
                "TBM_OUTCOME_EFFECT_EVENT_TIMESTAMP_INVALID",
                "Outcome/Effect event timestamp is invalid",
            ) from error
        if not isinstance(self.payload, Mapping):
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_DRAFT_INVALID",
                "Outcome/Effect event payload must be an object",
            )
        payload = _thaw_json(self.payload)
        if type(payload) is not dict:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_DRAFT_INVALID",
                "Outcome/Effect event payload must be an object",
            )
        if payload.get("session_id") != self.session_id:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_DRAFT_INVALID",
                "Outcome/Effect event payload session does not match",
            )
        _validate_draft_payload(self.event_type, payload)

    def command_value(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "record_id": self.record_id,
            "occurred_at": canonical_rfc3339(self.occurred_at),
            "payload": _thaw_json(self.payload),
        }


@dataclass(frozen=True)
class OutcomeEffectReducedViews:
    run_outcome: Mapping[str, object] | None
    outcome_attributions: tuple[Mapping[str, object], ...]
    effect_queue: Mapping[str, Mapping[str, object]]
    delivery_history: tuple[Mapping[str, object], ...]
    dead_letters: tuple[Mapping[str, object], ...]
    compensations: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class OutcomeEffectViews:
    run_outcome: RunOutcome | None
    outcome_attributions: tuple[OutcomeAttribution, ...]
    effect_queue: Mapping[str, Mapping[str, object]]
    delivery_history: Mapping[str, tuple[CompletionOutboxDelivery, ...]]
    dead_letters: tuple[CompletionOutboxDelivery, ...]
    compensations: tuple[Mapping[str, object], ...]


class OutcomeCompletionEventSink(Protocol):
    def append_completion(
        self,
        outcome: RunOutcome,
        session: GateSession,
    ) -> OutcomeEffectViews: ...


class EffectDeliveryEventSink(Protocol):
    def append_delivery(
        self,
        event: CompletionOutboxEvent,
        previous: CompletionOutboxDelivery,
        current: CompletionOutboxDelivery,
    ) -> OutcomeEffectViews: ...


class OutcomeEffectSessionReader(Protocol):
    def get(self, session_id: str) -> GateSession: ...


class OutcomeEffectOutcomeReader(Protocol):
    def get_outcome(self, run_outcome_id: str) -> RunOutcome: ...


class OutcomeEffectDeliveryHistoryReader(Protocol):
    def list_delivery_history(
        self,
        event_id: str,
    ) -> tuple[CompletionOutboxDelivery, ...]: ...


class OutcomeEffectEventLedgerProjector:
    """Append Outcome/Effect events and rebuild critical views before SQL writes."""

    def __init__(
        self,
        *,
        ledger_factory: Callable[[LedgerAccessContext], EventLedgerPort],
        access_resolver: Callable[[GateSession], LedgerAccessContext],
        session_reader: OutcomeEffectSessionReader,
        outcome_reader: OutcomeEffectOutcomeReader,
        delivery_reader: OutcomeEffectDeliveryHistoryReader,
    ) -> None:
        for callback in (ledger_factory, access_resolver):
            if not callable(callback):
                raise TypeError("Outcome/Effect event callbacks are invalid")
        for authority, methods in (
            (session_reader, ("get",)),
            (outcome_reader, ("get_outcome",)),
            (delivery_reader, ("list_delivery_history",)),
        ):
            if not all(callable(getattr(authority, name, None)) for name in methods):
                raise TypeError("Outcome/Effect event authority is invalid")
        self._ledger_factory = ledger_factory
        self._access_resolver = access_resolver
        self._session_reader = session_reader
        self._outcome_reader = outcome_reader
        self._delivery_reader = delivery_reader
        self._event_registry = build_outcome_effect_event_registry()

    @property
    def event_registry(self) -> EventTypeRegistry:
        return self._event_registry

    def read_events(self, session: GateSession) -> tuple[CanonicalEvent, ...]:
        if type(session) is not GateSession:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_SESSION_INVALID",
                "session must be exactly GateSession",
            )
        access = self._access_resolver(session)
        _verify_projector_access(access, session)
        ledger = self._ledger_factory(access)
        _verify_projector_ledger(ledger)
        try:
            return _read_outcome_effect_stream(
                ledger,
                outcome_effect_stream_id(session.session_id),
            )
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

    def rebuild_current(self, session: GateSession) -> OutcomeEffectViews:
        return hydrate_outcome_effect_views(
            reduce_outcome_effect_events(
                self.read_events(session),
                event_registry=self._event_registry,
            ),
            session=session,
        )

    def append_completion(
        self,
        outcome: RunOutcome,
        session: GateSession,
    ) -> OutcomeEffectViews:
        if type(outcome) is not RunOutcome or type(session) is not GateSession:
            raise TypeError("outcome and session must be exact contract values")
        verify_run_outcome(outcome, session)
        event = build_completion_outbox_event(outcome, session)
        initial = build_initial_completion_outbox_delivery(event)
        drafts = (
            build_run_outcome_draft(outcome, session),
            *build_completion_outbox_effect_drafts(
                event,
                (initial,),
                outcome,
                session,
            ),
        )
        return self._synchronize(
            session,
            drafts=drafts,
            outcome=outcome,
            event=event,
            deliveries=(initial,),
        )

    def append_delivery(
        self,
        event: CompletionOutboxEvent,
        previous: CompletionOutboxDelivery,
        current: CompletionOutboxDelivery,
    ) -> OutcomeEffectViews:
        if (
            type(event) is not CompletionOutboxEvent
            or type(previous) is not CompletionOutboxDelivery
            or type(current) is not CompletionOutboxDelivery
        ):
            raise TypeError("delivery event values must be exact contract values")
        verify_completion_outbox_delivery_transition(previous, current)
        if event.event_id != current.event_id:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_DELIVERY_INVALID",
                "completion outbox event and delivery do not match",
            )
        session = self._session_reader.get(event.session_id)
        outcome = self._outcome_reader.get_outcome(event.run_outcome_id)
        if type(session) is not GateSession or type(outcome) is not RunOutcome:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_AUTHORITY_INVALID",
                "Outcome/Effect authority returned an invalid record",
            )
        verify_completion_outbox_event(event, outcome, session)
        retained_history = self._delivery_reader.list_delivery_history(
            event.event_id
        )
        if (
            type(retained_history) is not tuple
            or not retained_history
            or any(
                type(delivery) is not CompletionOutboxDelivery
                for delivery in retained_history
            )
        ):
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_DELIVERY_INVALID",
                "completion outbox authority returned invalid delivery history",
            )
        if retained_history[-1] == current:
            deliveries = retained_history
        elif retained_history[-1] == previous:
            deliveries = (*retained_history, current)
        else:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_DELIVERY_INVALID",
                "delivery transition does not extend retained history",
            )
        drafts = (
            build_run_outcome_draft(outcome, session),
            *build_completion_outbox_effect_drafts(
                event,
                deliveries,
                outcome,
                session,
            ),
        )
        return self._synchronize(
            session,
            drafts=drafts,
            outcome=outcome,
            event=event,
            deliveries=deliveries,
        )

    def _synchronize(
        self,
        session: GateSession,
        *,
        drafts: tuple[OutcomeEffectEventDraft, ...],
        outcome: RunOutcome,
        event: CompletionOutboxEvent,
        deliveries: tuple[CompletionOutboxDelivery, ...],
    ) -> OutcomeEffectViews:
        access = self._access_resolver(session)
        _verify_projector_access(access, session)
        ledger = self._ledger_factory(access)
        _verify_projector_ledger(ledger)
        try:
            stream_id = outcome_effect_stream_id(session.session_id)
            retained = _read_outcome_effect_stream(ledger, stream_id)
            missing = _missing_outcome_effect_drafts(retained, drafts)
            if missing:
                retained = self._append_drafts(
                    ledger,
                    access=access,
                    retained=retained,
                    drafts=missing,
                    stream_id=stream_id,
                )
            rebuilt = _read_outcome_effect_stream(ledger, stream_id)
            views = hydrate_outcome_effect_views(
                reduce_outcome_effect_events(
                    rebuilt,
                    event_registry=self._event_registry,
                ),
                session=session,
            )
            _verify_projected_outcome_effect_views(
                retained=rebuilt,
                drafts=drafts,
                views=views,
                outcome=outcome,
                event=event,
                deliveries=deliveries,
            )
            return views
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _append_drafts(
        ledger: EventLedgerPort,
        *,
        access: LedgerAccessContext,
        retained: tuple[CanonicalEvent, ...],
        drafts: tuple[OutcomeEffectEventDraft, ...],
        stream_id: str,
    ) -> tuple[CanonicalEvent, ...]:
        appended = retained
        for offset in range(0, len(drafts), EVENT_LEDGER_MAX_APPEND_BATCH):
            chunk = drafts[offset : offset + EVENT_LEDGER_MAX_APPEND_BATCH]
            expected_version = appended[-1].stream_version if appended else 0
            previous_event = appended[-1] if appended else None
            recorded_at = _outcome_effect_batch_recorded_at(
                chunk,
                previous_event,
            )
            for attempt in range(OUTCOME_EFFECT_EVENT_MAX_APPEND_RETRIES):
                high_watermark = ledger.read_global(
                    after_position=0,
                    limit=1,
                ).high_watermark_global_position
                events, idempotency = build_outcome_effect_event_batch(
                    chunk,
                    access=access,
                    expected_stream_version=expected_version,
                    next_global_position=high_watermark + 1,
                    previous_event=previous_event,
                    recorded_at=recorded_at,
                )
                try:
                    ledger.append(
                        stream_id,
                        expected_version,
                        events,
                        idempotency,
                    )
                    appended = (*appended, *events)
                    break
                except EventLedgerConflictError as error:
                    if (
                        error.code
                        != "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"
                        or attempt + 1
                        >= OUTCOME_EFFECT_EVENT_MAX_APPEND_RETRIES
                    ):
                        raise
            else:  # pragma: no cover - bounded loop always breaks or raises
                raise AssertionError(
                    "Outcome/Effect event append loop did not terminate"
                )
        return appended


def outcome_effect_stream_id(session_id: str) -> str:
    _identifier(session_id, "session_id")
    digest = hashlib.sha256(
        ("tbm.outcome-effect-stream.v1\x00" + session_id).encode("utf-8")
    ).hexdigest()
    return "stream_oe_" + digest


def build_run_outcome_draft(
    outcome: RunOutcome,
    session: GateSession,
) -> OutcomeEffectEventDraft:
    if type(outcome) is not RunOutcome or type(session) is not GateSession:
        raise TypeError("outcome and session must be exact contract values")
    verify_run_outcome(outcome, session)
    record_json = dumps_run_outcome(outcome)
    payload = {
        "session_id": session.session_id,
        "run_outcome_id": outcome.run_outcome_id,
        "record_sha256": canonical_sha256(outcome.to_dict()),
        "record_json": record_json,
    }
    return OutcomeEffectEventDraft(
        RUN_OUTCOME_RECORDED,
        session.session_id,
        outcome.run_outcome_id,
        outcome.measured_at,
        payload,
    )


def build_outcome_attribution_draft(
    attribution: OutcomeAttribution,
    outcome: RunOutcome,
    session: GateSession,
) -> OutcomeEffectEventDraft:
    if (
        type(attribution) is not OutcomeAttribution
        or type(outcome) is not RunOutcome
        or type(session) is not GateSession
    ):
        raise TypeError("attribution, outcome, and session must be exact values")
    verify_outcome_attribution(attribution, outcome, session)
    payload = {
        "session_id": session.session_id,
        "run_outcome_id": outcome.run_outcome_id,
        "attribution_id": attribution.attribution_id,
        "claim_strength": attribution.claim_strength,
        "recorded_at": attribution.recorded_at,
        "record_sha256": canonical_sha256(attribution.to_dict()),
        "record_json": dumps_outcome_attribution(attribution),
    }
    return OutcomeEffectEventDraft(
        OUTCOME_ATTRIBUTION_RECORDED,
        session.session_id,
        attribution.attribution_id,
        attribution.recorded_at,
        payload,
    )


def build_completion_outbox_effect_drafts(
    event: CompletionOutboxEvent,
    deliveries: tuple[CompletionOutboxDelivery, ...],
    outcome: RunOutcome,
    session: GateSession,
) -> tuple[OutcomeEffectEventDraft, ...]:
    if (
        type(event) is not CompletionOutboxEvent
        or type(deliveries) is not tuple
        or not deliveries
        or any(type(item) is not CompletionOutboxDelivery for item in deliveries)
        or type(outcome) is not RunOutcome
        or type(session) is not GateSession
    ):
        raise TypeError("completion outbox source values are invalid")
    verify_completion_outbox_event(event, outcome, session)
    if deliveries[0] != build_initial_completion_outbox_delivery(event):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_DELIVERY_INVALID",
            "completion outbox history lacks its exact initial revision",
        )
    for previous, current in zip(deliveries, deliveries[1:], strict=False):
        verify_completion_outbox_delivery_transition(previous, current)
    return tuple(
        _effect_delivery_draft(event, delivery, session.session_id)
        for delivery in deliveries
    )


def build_effect_requested_draft(
    *,
    session_id: str,
    effect_id: str,
    effect_type: str,
    occurred_at: str,
    compensation_supported: bool,
) -> OutcomeEffectEventDraft:
    """Build a native effect request not backed by the legacy completion outbox."""

    return _generic_effect_draft(
        event_type=EFFECT_REQUESTED,
        session_id=session_id,
        effect_id=effect_id,
        effect_type=effect_type,
        occurred_at=occurred_at,
        compensation_supported=compensation_supported,
        compensates_effect_id=None,
    )


def build_effect_transition_draft(
    *,
    event_type: str,
    session_id: str,
    effect_id: str,
    effect_type: str,
    occurred_at: str,
    compensation_supported: bool,
    compensates_effect_id: str | None = None,
) -> OutcomeEffectEventDraft:
    """Build a native lifecycle transition without legacy delivery evidence."""

    if event_type not in {
        EFFECT_STARTED,
        EFFECT_RETRY_SCHEDULED,
        EFFECT_SUCCEEDED,
        EFFECT_DEAD_LETTERED,
    }:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_TYPE_INVALID",
            "generic transition requires an active Effect event type",
        )
    return _generic_effect_draft(
        event_type=event_type,
        session_id=session_id,
        effect_id=effect_id,
        effect_type=effect_type,
        occurred_at=occurred_at,
        compensation_supported=compensation_supported,
        compensates_effect_id=compensates_effect_id,
    )


def build_effect_compensation_draft(
    *,
    event_type: str,
    session_id: str,
    effect_id: str,
    effect_type: str,
    compensates_effect_id: str,
    occurred_at: str,
) -> OutcomeEffectEventDraft:
    """Build a new effect that requests or records explicit compensation."""

    if event_type not in {
        EFFECT_COMPENSATION_REQUESTED,
        EFFECT_COMPENSATED,
    }:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_TYPE_INVALID",
            "compensation draft requires a compensation event type",
        )
    return _generic_effect_draft(
        event_type=event_type,
        session_id=session_id,
        effect_id=effect_id,
        effect_type=effect_type,
        occurred_at=occurred_at,
        compensation_supported=False,
        compensates_effect_id=compensates_effect_id,
    )


def build_outcome_effect_event_batch(
    drafts: tuple[OutcomeEffectEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= OUTCOME_EFFECT_EVENT_MAX_BATCH
        or any(type(item) is not OutcomeEffectEventDraft for item in drafts)
    ):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_BATCH_INVALID",
            "event drafts must be a bounded non-empty tuple",
        )
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_ACCESS_INVALID",
            "ledger access must be exactly LedgerAccessContext",
        )
    if type(expected_stream_version) is not int or expected_stream_version < 0:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_BATCH_INVALID",
            "expected stream version is invalid",
        )
    if type(next_global_position) is not int or next_global_position < 1:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_BATCH_INVALID",
            "next global position is invalid",
        )
    session_id = drafts[0].session_id
    if any(draft.session_id != session_id for draft in drafts):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_BATCH_INVALID",
            "event drafts must belong to one GateSession",
        )
    stream_id = outcome_effect_stream_id(session_id)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_BATCH_INVALID",
                "nonzero stream version requires its parent event",
            )
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_BATCH_INVALID",
            "previous event does not match the expected stream head",
        )
    command_value = {
        "protocol_version": OUTCOME_EFFECT_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "drafts": [draft.command_value() for draft in drafts],
    }
    command_sha256 = _domain_sha256(
        b"tbm.outcome-effect-event-command.v1\x00",
        command_value,
    )
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.outcome-effect-event-idempotency.v1\x00",
        command_value,
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    correlation_digest = hashlib.sha256(
        (access.partition.partition_sha256 + "\x00" + session_id).encode("utf-8")
    ).hexdigest()
    parent = previous_event
    events: list[CanonicalEvent] = []
    for offset, draft in enumerate(drafts):
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        event = build_canonical_event(
            event_id="evt_oe_" + event_digest,
            event_type=draft.event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=OUTCOME_EFFECT_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_oe_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_oe_" + correlation_digest[:32],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=draft.occurred_at,
            recorded_at=recorded_at,
            producer="tbm_outcome_effect_adapter",
            producer_version="f2-v1",
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification="internal",
            retention_policy_id="retention_outcome_effect_events",
            artifact_refs=(),
            payload=draft.payload,
        )
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_outcome_effect_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    schemas = _payload_json_schemas()
    for event_type in OUTCOME_EFFECT_EVENT_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="domain",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=schemas[event_type],
            )
        )
    return registry.seal()


def outcome_effect_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_outcome_effect_event_registry().dispatch_schema()
    schema["$id"] = OUTCOME_EFFECT_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory Outcome/Effect event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed Outcome/Effect tbm.event-registry.v1 catalog. "
        "The runtime registry remains the authoritative validator."
    )
    return schema


def dumps_outcome_effect_event_payload_dispatch_schema() -> str:
    return json.dumps(
        outcome_effect_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_run_outcome_reducer() -> FunctionalReducer:
    descriptor = _descriptor(
        RUN_OUTCOME_REDUCER_ID,
        RUN_OUTCOME_PROJECTION,
        (RUN_OUTCOME_RECORDED,),
        "singleton-run-outcome",
    )

    def initial() -> Mapping[str, object]:
        return {"current": None}

    def transition(
        state: Mapping[str, object], event: ReducerEvent
    ) -> Mapping[str, object]:
        if state.get("current") is not None:
            _transition_invalid("RunOutcome projection cannot be replaced")
        return {"current": _typed_payload(event, RUN_OUTCOME_RECORDED)}

    return FunctionalReducer(descriptor, initial, transition)


def build_outcome_attribution_reducer() -> FunctionalReducer:
    descriptor = _descriptor(
        OUTCOME_ATTRIBUTION_REDUCER_ID,
        OUTCOME_ATTRIBUTION_PROJECTION,
        (OUTCOME_ATTRIBUTION_RECORDED,),
        "immutable-attribution-set",
    )

    def initial() -> Mapping[str, object]:
        return {"items": []}

    def transition(
        state: Mapping[str, object], event: ReducerEvent
    ) -> Mapping[str, object]:
        payload = _typed_payload(event, OUTCOME_ATTRIBUTION_RECORDED)
        items = _state_list(state, "items")
        attribution_id = payload["attribution_id"]
        if any(item.get("attribution_id") == attribution_id for item in items):
            _transition_invalid("OutcomeAttribution projection contains a duplicate")
        items.append(payload)
        items.sort(
            key=lambda item: (
                cast(str, item["recorded_at"]),
                cast(str, item["attribution_id"]),
            )
        )
        return {"items": items}

    return FunctionalReducer(descriptor, initial, transition)


def build_effect_queue_reducer() -> FunctionalReducer:
    descriptor = _descriptor(
        EFFECT_QUEUE_REDUCER_ID,
        EFFECT_QUEUE_PROJECTION,
        EFFECT_EVENT_TYPES,
        "effect-state-machine",
    )

    def initial() -> Mapping[str, object]:
        return {"effects": {}}

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        event_type = reducer_event.source_event.event_type
        payload = _typed_payload(reducer_event, event_type)
        effects = _state_mapping(state, "effects")
        effect_id = cast(str, payload["effect_id"])
        current = effects.get(effect_id)
        if event_type == EFFECT_REQUESTED:
            if current is not None:
                _transition_invalid("EffectRequested cannot replace an effect")
            effects[effect_id] = _queue_item(payload, reducer_event.source_event)
            return {"effects": effects}
        if event_type == EFFECT_COMPENSATION_REQUESTED:
            if current is not None:
                _transition_invalid("compensation effect already exists")
            original_id = cast(str, payload["compensates_effect_id"])
            original = effects.get(original_id)
            if (
                original is None
                or original.get("compensation_supported") is not True
                or original.get("queue_status") != "succeeded"
                or original.get("compensation_status") is not None
            ):
                _transition_invalid(
                    "compensation requires one uncompensated successful effect"
                )
            original = dict(original)
            original["compensation_status"] = "requested"
            original["compensation_effect_id"] = effect_id
            effects[original_id] = original
            effects[effect_id] = _queue_item(payload, reducer_event.source_event)
            return {"effects": effects}
        if event_type == EFFECT_COMPENSATED:
            original_id = cast(str, payload["compensates_effect_id"])
            original = effects.get(original_id)
            compensation = effects.get(effect_id)
            if (
                original is None
                or compensation is None
                or original.get("compensation_status") != "requested"
                or original.get("compensation_effect_id") != effect_id
                or compensation.get("compensates_effect_id") != original_id
                or compensation.get("queue_status") not in {"ready", "leased"}
            ):
                _transition_invalid("compensation completion lacks its request")
            updated_original = dict(original)
            updated_original["compensation_status"] = "compensated"
            effects[original_id] = updated_original
            effects[effect_id] = _queue_item(
                payload,
                reducer_event.source_event,
                previous=compensation,
            )
            return {"effects": effects}
        if current is None:
            _transition_invalid("effect transition lacks EffectRequested")
        _verify_queue_transition(current, payload)
        effects[effect_id] = _queue_item(
            payload,
            reducer_event.source_event,
            previous=current,
        )
        return {"effects": effects}

    return FunctionalReducer(descriptor, initial, transition)


def build_effect_delivery_history_reducer() -> FunctionalReducer:
    descriptor = _descriptor(
        EFFECT_DELIVERY_HISTORY_REDUCER_ID,
        EFFECT_DELIVERY_HISTORY_PROJECTION,
        EFFECT_EVENT_TYPES,
        "append-only-effect-history",
    )

    def initial() -> Mapping[str, object]:
        return {"items": []}

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        event_type = reducer_event.source_event.event_type
        payload = _typed_payload(reducer_event, event_type)
        items = _state_list(state, "items")
        if any(
            item.get("source_event_sha256")
            == reducer_event.source_event.event_sha256
            for item in items
        ):
            _transition_invalid("delivery history contains a duplicate event")
        items.append(
            {
                **payload,
                "source_event_id": reducer_event.source_event.event_id,
                "source_event_sha256": reducer_event.source_event.event_sha256,
                "stream_version": reducer_event.source_event.stream_version,
            }
        )
        return {"items": items}

    return FunctionalReducer(descriptor, initial, transition)


def build_effect_dead_letter_reducer() -> FunctionalReducer:
    descriptor = _descriptor(
        EFFECT_DEAD_LETTER_REDUCER_ID,
        EFFECT_DEAD_LETTER_PROJECTION,
        (EFFECT_DEAD_LETTERED,),
        "append-only-dead-letter",
    )

    def initial() -> Mapping[str, object]:
        return {"items": []}

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        payload = _typed_payload(reducer_event, EFFECT_DEAD_LETTERED)
        items = _state_list(state, "items")
        if any(item.get("effect_id") == payload["effect_id"] for item in items):
            _transition_invalid("effect can enter dead-letter only once")
        items.append(payload)
        return {"items": items}

    return FunctionalReducer(descriptor, initial, transition)


def build_effect_compensation_reducer() -> FunctionalReducer:
    inputs = tuple(sorted((EFFECT_COMPENSATION_REQUESTED, EFFECT_COMPENSATED)))
    descriptor = _descriptor(
        EFFECT_COMPENSATION_REDUCER_ID,
        EFFECT_COMPENSATION_PROJECTION,
        inputs,
        "explicit-compensation-chain",
    )

    def initial() -> Mapping[str, object]:
        return {"items": []}

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        event_type = reducer_event.source_event.event_type
        payload = _typed_payload(reducer_event, event_type)
        items = _state_list(state, "items")
        effect_id = payload["effect_id"]
        matches = [item for item in items if item.get("effect_id") == effect_id]
        if event_type == EFFECT_COMPENSATION_REQUESTED:
            if matches:
                _transition_invalid("compensation request is duplicated")
        elif (
            len(matches) != 1
            or matches[0].get("event_type") != EFFECT_COMPENSATION_REQUESTED
            or matches[0].get("compensates_effect_id")
            != payload["compensates_effect_id"]
        ):
            _transition_invalid("compensation completion lacks its exact request")
        items.append({**payload, "event_type": event_type})
        return {"items": items}

    return FunctionalReducer(descriptor, initial, transition)


def reduce_outcome_effect_events(
    events: tuple[CanonicalEvent, ...],
    *,
    event_registry: EventTypeRegistry | None = None,
) -> OutcomeEffectReducedViews:
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_SEQUENCE_INVALID",
            "events must be a tuple of CanonicalEvent values",
        )
    registry = (
        build_outcome_effect_event_registry()
        if event_registry is None
        else event_registry
    )
    if type(registry) is not EventTypeRegistry or not registry.sealed:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_REGISTRY_INVALID",
            "event registry must be a sealed EventTypeRegistry",
        )
    reducers = (
        build_run_outcome_reducer(),
        build_outcome_attribution_reducer(),
        build_effect_queue_reducer(),
        build_effect_delivery_history_reducer(),
        build_effect_dead_letter_reducer(),
        build_effect_compensation_reducer(),
    )
    states = [initial_reducer_state(reducer) for reducer in reducers]
    parent: CanonicalEvent | None = None
    stream_id: str | None = None
    for event in events:
        if parent is None:
            if (
                event.stream_version != 1
                or event.previous_stream_event_sha256 is not None
            ):
                _sequence_invalid("Outcome/Effect stream must begin at version one")
            stream_id = event.stream_id
        else:
            verify_event_parent(event, parent)
        if (
            event.stream_type != OUTCOME_EFFECT_EVENT_STREAM_TYPE
            or event.stream_id != stream_id
            or event.event_type not in OUTCOME_EFFECT_EVENT_TYPES
        ):
            _sequence_invalid("Outcome/Effect event stream identity is invalid")
        typed = registry.consume(event, target_version=1)
        for index, reducer in enumerate(reducers):
            states[index] = execute_reducer_step(
                reducer,
                states[index].state,
                ReducerEvent(event, typed),
            )
        parent = event
    run_outcome = _current_state(states[0].state)
    attributions = _items_state(states[1].state)
    if attributions and run_outcome is None:
        _sequence_invalid("OutcomeAttribution projection requires RunOutcome")
    if run_outcome is not None and any(
        item.get("run_outcome_id") != run_outcome.get("run_outcome_id")
        for item in attributions
    ):
        _sequence_invalid("OutcomeAttribution references another RunOutcome")
    queue = _effects_state(states[2].state)
    history = _items_state(states[3].state)
    dead_letters = _items_state(states[4].state)
    compensations = _items_state(states[5].state)
    return OutcomeEffectReducedViews(
        run_outcome=run_outcome,
        outcome_attributions=attributions,
        effect_queue=queue,
        delivery_history=history,
        dead_letters=dead_letters,
        compensations=compensations,
    )


def hydrate_outcome_effect_views(
    reduced: OutcomeEffectReducedViews,
    *,
    session: GateSession | None = None,
) -> OutcomeEffectViews:
    if type(reduced) is not OutcomeEffectReducedViews:
        raise TypeError("reduced must be exactly OutcomeEffectReducedViews")
    if session is not None and type(session) is not GateSession:
        raise TypeError("session must be exactly GateSession or null")
    outcome = None
    if reduced.run_outcome is not None:
        outcome = _load_outcome_payload(reduced.run_outcome)
        if session is not None:
            verify_run_outcome(outcome, session)
    attributions: list[OutcomeAttribution] = []
    for payload in reduced.outcome_attributions:
        attribution = _load_attribution_payload(payload)
        if outcome is None or attribution.run_outcome_id != outcome.run_outcome_id:
            _projection_invalid("OutcomeAttribution projection linkage is invalid")
        if session is not None:
            verify_outcome_attribution(attribution, outcome, session)
        attributions.append(attribution)
    delivery_history: dict[str, list[CompletionOutboxDelivery]] = {}
    outbox_events: dict[str, CompletionOutboxEvent] = {}
    for payload in reduced.delivery_history:
        outbox_json = payload.get("outbox_event_json")
        if outbox_json is not None:
            if type(outbox_json) is not str:
                _projection_invalid("outbox event JSON projection is invalid")
            outbox_event = loads_completion_outbox_event(outbox_json)
            if (
                outbox_event.event_id != payload.get("effect_id")
                or outbox_event.session_id != payload.get("session_id")
            ):
                _projection_invalid("outbox event projection linkage is invalid")
            if outcome is not None and session is not None:
                verify_completion_outbox_event(outbox_event, outcome, session)
            outbox_events[outbox_event.event_id] = outbox_event
        delivery_json = payload.get("delivery_json")
        if delivery_json is None:
            continue
        if type(delivery_json) is not str:
            _projection_invalid("delivery JSON projection is invalid")
        delivery = loads_completion_outbox_delivery(delivery_json)
        if delivery.event_id != payload.get("effect_id"):
            _projection_invalid("delivery projection belongs to another effect")
        history = delivery_history.setdefault(delivery.event_id, [])
        if history:
            verify_completion_outbox_delivery_transition(history[-1], delivery)
        else:
            event = outbox_events.get(delivery.event_id)
            if event is None or delivery != build_initial_completion_outbox_delivery(
                event
            ):
                _projection_invalid("delivery projection lacks its initial event")
        history.append(delivery)
    dead_letters: list[CompletionOutboxDelivery] = []
    for payload in reduced.dead_letters:
        raw = payload.get("delivery_json")
        if type(raw) is not str:
            _projection_invalid("dead-letter projection lacks delivery evidence")
        delivery = loads_completion_outbox_delivery(raw)
        if delivery.status != "dead_letter":
            _projection_invalid("dead-letter projection is not terminal")
        dead_letters.append(delivery)
    for effect_id, item in reduced.effect_queue.items():
        history = delivery_history.get(effect_id)
        if history is not None and item.get("delivery_revision_id") != (
            history[-1].delivery_revision_id
        ):
            _projection_invalid("EffectQueue head differs from delivery history")
    return OutcomeEffectViews(
        run_outcome=outcome,
        outcome_attributions=tuple(attributions),
        effect_queue=reduced.effect_queue,
        delivery_history={
            effect_id: tuple(items)
            for effect_id, items in sorted(delivery_history.items())
        },
        dead_letters=tuple(dead_letters),
        compensations=reduced.compensations,
    )


def _missing_outcome_effect_drafts(
    retained: tuple[CanonicalEvent, ...],
    desired: tuple[OutcomeEffectEventDraft, ...],
) -> tuple[OutcomeEffectEventDraft, ...]:
    desired_identities = {_draft_identity(draft) for draft in desired}
    retained_by_identity: dict[tuple[str, str], tuple[int, CanonicalEvent]] = {}
    for position, event in enumerate(retained):
        identity = _retained_outcome_effect_identity(event)
        if identity is None or identity not in desired_identities:
            continue
        if identity in retained_by_identity:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_PROJECTION_DRIFT",
                "retained Outcome/Effect stream contains a duplicate record",
            )
        retained_by_identity[identity] = (position, event)

    first_missing: int | None = None
    previous_position = -1
    for index, draft in enumerate(desired):
        retained_item = retained_by_identity.get(_draft_identity(draft))
        if retained_item is None:
            if first_missing is None:
                first_missing = index
            continue
        if first_missing is not None:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_PROJECTION_DRIFT",
                "retained Outcome/Effect stream omits an earlier record",
            )
        position, event = retained_item
        if position <= previous_position or not _event_matches_draft(event, draft):
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_PROJECTION_DRIFT",
                "retained Outcome/Effect event differs from its source record",
            )
        previous_position = position
    if first_missing is None:
        return ()
    return desired[first_missing:]


def _verify_projected_outcome_effect_views(
    *,
    retained: tuple[CanonicalEvent, ...],
    drafts: tuple[OutcomeEffectEventDraft, ...],
    views: OutcomeEffectViews,
    outcome: RunOutcome,
    event: CompletionOutboxEvent,
    deliveries: tuple[CompletionOutboxDelivery, ...],
) -> None:
    if _missing_outcome_effect_drafts(retained, drafts):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_PROJECTION_MISMATCH",
            "Outcome/Effect stream did not retain every source record",
        )
    history = views.delivery_history.get(event.event_id)
    queue_item = views.effect_queue.get(event.event_id)
    if (
        views.run_outcome != outcome
        or history != deliveries
        or queue_item is None
        or queue_item.get("delivery_revision_id")
        != deliveries[-1].delivery_revision_id
        or queue_item.get("last_stream_version") != (
            next(
                retained_event.stream_version
                for retained_event in reversed(retained)
                if _retained_outcome_effect_identity(retained_event)
                == _draft_identity(drafts[-1])
            )
        )
    ):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_PROJECTION_MISMATCH",
            "Outcome/Effect reducers did not reproduce source authority state",
        )


def _draft_identity(draft: OutcomeEffectEventDraft) -> tuple[str, str]:
    return draft.event_type, draft.record_id


def _retained_outcome_effect_identity(
    event: CanonicalEvent,
) -> tuple[str, str] | None:
    payload = _thaw_json(event.payload)
    if type(payload) is not dict:
        return None
    if event.event_type == RUN_OUTCOME_RECORDED:
        record_id = payload.get("run_outcome_id")
    elif event.event_type == OUTCOME_ATTRIBUTION_RECORDED:
        record_id = payload.get("attribution_id")
    elif event.event_type in EFFECT_EVENT_TYPES:
        record_id = payload.get("delivery_revision_id") or payload.get(
            "effect_id"
        )
    else:
        return None
    if type(record_id) is not str:
        return None
    return event.event_type, record_id


def _event_matches_draft(
    event: CanonicalEvent,
    draft: OutcomeEffectEventDraft,
) -> bool:
    return (
        event.event_type == draft.event_type
        and event.event_version == 1
        and event.payload_schema == _PAYLOAD_SCHEMAS[draft.event_type]
        and event.occurred_at == canonical_rfc3339(draft.occurred_at)
        and _thaw_json(event.payload) == _thaw_json(draft.payload)
    )


def _read_outcome_effect_stream(
    ledger: EventLedgerPort,
    stream_id: str,
) -> tuple[CanonicalEvent, ...]:
    events: list[CanonicalEvent] = []
    from_version = 1
    while True:
        page = ledger.read_stream(
            stream_id,
            from_version=from_version,
            limit=EVENT_LEDGER_MAX_APPEND_BATCH,
        )
        events.extend(page.events)
        if not page.has_more:
            return tuple(events)
        if page.next_stream_version is None:
            _fail(
                "TBM_OUTCOME_EFFECT_EVENT_SEQUENCE_INVALID",
                "Outcome/Effect stream page omitted its continuation version",
            )
        from_version = page.next_stream_version


def _verify_projector_access(access: object, session: GateSession) -> None:
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_ACCESS_INVALID",
            "access resolver must return exactly LedgerAccessContext",
        )
    if (
        access.partition.tenant_id != session.tenant_id
        or access.partition.repository_id != session.repository_id
        or access.principal_id != session.principal_id
        or access.agent_client_id != session.agent_client_id
        or not access.classification_filter.allows("internal")
    ):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_ACCESS_INVALID",
            "ledger access does not match the GateSession scope",
        )


def _verify_projector_ledger(ledger: object) -> None:
    if not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global")
    ):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_LEDGER_INVALID",
            "ledger factory returned an invalid event ledger",
        )


def _outcome_effect_batch_recorded_at(
    drafts: tuple[OutcomeEffectEventDraft, ...],
    previous_event: CanonicalEvent | None,
) -> str:
    timestamps = [parse_rfc3339(draft.occurred_at) for draft in drafts]
    if previous_event is not None:
        timestamps.append(parse_rfc3339(previous_event.recorded_at))
    return aware_datetime_to_rfc3339(max(timestamps))


def _effect_delivery_draft(
    event: CompletionOutboxEvent,
    delivery: CompletionOutboxDelivery,
    session_id: str,
) -> OutcomeEffectEventDraft:
    event_type = _DELIVERY_EVENT_BY_STATUS[delivery.status]
    payload_without_digest: dict[str, object] = {
        "session_id": session_id,
        "effect_id": event.event_id,
        "effect_type": "completion_outbox.execution_completed",
        "lifecycle_status": _EFFECT_STATUS_BY_EVENT[event_type],
        "delivery_version": delivery.version,
        "delivery_revision_id": delivery.delivery_revision_id,
        "attempt_count": delivery.attempt_count,
        "available_at": delivery.available_at,
        "worker_id": delivery.worker_id,
        "lease_expires_at": delivery.lease_expires_at,
        "receipt_sha256": delivery.response_sha256,
        "error_code": delivery.last_error_code,
        "compensation_supported": False,
        "compensates_effect_id": None,
        "outbox_event_json": (
            dumps_completion_outbox_event(event)
            if delivery.status == "pending"
            else None
        ),
        "delivery_json": dumps_completion_outbox_delivery(delivery),
    }
    payload = {
        **payload_without_digest,
        "record_sha256": canonical_sha256(payload_without_digest),
    }
    return OutcomeEffectEventDraft(
        event_type,
        session_id,
        delivery.delivery_revision_id,
        delivery.updated_at,
        payload,
    )


def _generic_effect_draft(
    *,
    event_type: str,
    session_id: str,
    effect_id: str,
    effect_type: str,
    occurred_at: str,
    compensation_supported: bool,
    compensates_effect_id: str | None,
) -> OutcomeEffectEventDraft:
    payload_without_digest: dict[str, object] = {
        "session_id": session_id,
        "effect_id": effect_id,
        "effect_type": effect_type,
        "lifecycle_status": _EFFECT_STATUS_BY_EVENT[event_type],
        "delivery_version": None,
        "delivery_revision_id": None,
        "attempt_count": 0,
        "available_at": None,
        "worker_id": None,
        "lease_expires_at": None,
        "receipt_sha256": None,
        "error_code": None,
        "compensation_supported": compensation_supported,
        "compensates_effect_id": compensates_effect_id,
        "outbox_event_json": None,
        "delivery_json": None,
    }
    payload = {
        **payload_without_digest,
        "record_sha256": canonical_sha256(payload_without_digest),
    }
    return OutcomeEffectEventDraft(
        event_type,
        session_id,
        effect_id,
        occurred_at,
        payload,
    )


def _validate_draft_payload(
    event_type: str,
    payload: dict[str, object],
) -> None:
    if event_type == RUN_OUTCOME_RECORDED:
        outcome = _load_outcome_payload(payload)
        if outcome.session_id != payload.get("session_id"):
            _draft_invalid("RunOutcome payload belongs to another session")
        return
    if event_type == OUTCOME_ATTRIBUTION_RECORDED:
        attribution = _load_attribution_payload(payload)
        if (
            attribution.run_outcome_id != payload.get("run_outcome_id")
            or attribution.claim_strength != payload.get("claim_strength")
            or attribution.recorded_at != payload.get("recorded_at")
        ):
            _draft_invalid("OutcomeAttribution payload descriptor differs")
        return
    expected_status = _EFFECT_STATUS_BY_EVENT[event_type]
    if payload.get("lifecycle_status") != expected_status:
        _draft_invalid("effect lifecycle status does not match event type")
    required = set(_effect_payload_properties())
    if set(payload) != required:
        _draft_invalid("effect payload fields do not match the contract")
    for name in ("session_id", "effect_id"):
        _identifier(payload.get(name), name)
    _bounded_text(payload.get("effect_type"), "effect_type", 256)
    if type(payload.get("compensation_supported")) is not bool:
        _draft_invalid("compensation_supported must be boolean")
    compensates = payload.get("compensates_effect_id")
    if event_type in {EFFECT_COMPENSATION_REQUESTED, EFFECT_COMPENSATED}:
        _identifier(compensates, "compensates_effect_id")
        if compensates == payload.get("effect_id"):
            _draft_invalid("compensation must be a distinct new effect")
    elif event_type == EFFECT_REQUESTED and compensates is not None:
        _draft_invalid("ordinary EffectRequested cannot name a compensated effect")
    digest = payload.get("record_sha256")
    unsigned = dict(payload)
    unsigned.pop("record_sha256", None)
    if digest != canonical_sha256(unsigned):
        _draft_invalid("effect record digest does not match payload")
    delivery_json = payload.get("delivery_json")
    outbox_json = payload.get("outbox_event_json")
    if delivery_json is None:
        if outbox_json is not None:
            _draft_invalid("outbox event without delivery is invalid")
        if payload.get("delivery_version") is not None or payload.get(
            "delivery_revision_id"
        ) is not None:
            _draft_invalid("generic effect cannot claim a delivery revision")
        return
    if type(delivery_json) is not str:
        _draft_invalid("delivery_json must be a string or null")
    delivery = loads_completion_outbox_delivery(delivery_json)
    if (
        delivery.event_id != payload.get("effect_id")
        or delivery.version != payload.get("delivery_version")
        or delivery.delivery_revision_id != payload.get("delivery_revision_id")
        or delivery.attempt_count != payload.get("attempt_count")
        or delivery.available_at != payload.get("available_at")
        or delivery.worker_id != payload.get("worker_id")
        or delivery.lease_expires_at != payload.get("lease_expires_at")
        or delivery.response_sha256 != payload.get("receipt_sha256")
        or delivery.last_error_code != payload.get("error_code")
        or _DELIVERY_EVENT_BY_STATUS[delivery.status] != event_type
    ):
        _draft_invalid("effect payload differs from delivery evidence")
    if event_type == EFFECT_REQUESTED:
        if type(outbox_json) is not str:
            _draft_invalid("legacy EffectRequested requires its outbox event")
        outbox_event = loads_completion_outbox_event(outbox_json)
        if (
            outbox_event.event_id != delivery.event_id
            or outbox_event.session_id != payload.get("session_id")
            or delivery != build_initial_completion_outbox_delivery(outbox_event)
        ):
            _draft_invalid("initial effect delivery does not match outbox event")
    elif outbox_json is not None:
        _draft_invalid("only EffectRequested may retain the outbox event")


def _load_outcome_payload(payload: Mapping[str, object]) -> RunOutcome:
    raw = payload.get("record_json")
    if type(raw) is not str:
        _projection_invalid("RunOutcome projection lacks canonical JSON")
    outcome = loads_run_outcome(raw)
    if (
        outcome.run_outcome_id != payload.get("run_outcome_id")
        or outcome.session_id != payload.get("session_id")
        or canonical_sha256(outcome.to_dict()) != payload.get("record_sha256")
    ):
        _projection_invalid("RunOutcome projection descriptor differs")
    return outcome


def _load_attribution_payload(
    payload: Mapping[str, object],
) -> OutcomeAttribution:
    raw = payload.get("record_json")
    if type(raw) is not str:
        _projection_invalid("OutcomeAttribution projection lacks canonical JSON")
    attribution = loads_outcome_attribution(raw)
    if (
        attribution.attribution_id != payload.get("attribution_id")
        or canonical_sha256(attribution.to_dict()) != payload.get("record_sha256")
    ):
        _projection_invalid("OutcomeAttribution projection descriptor differs")
    return attribution


def _queue_item(
    payload: Mapping[str, object],
    event: CanonicalEvent,
    *,
    previous: Mapping[str, object] | None = None,
) -> dict[str, object]:
    status = cast(str, payload["lifecycle_status"])
    queue_status = {
        "compensation_requested": "ready",
        "compensated": "compensated",
    }.get(status, status)
    return {
        "session_id": payload["session_id"],
        "effect_id": payload["effect_id"],
        "effect_type": payload["effect_type"],
        "queue_status": queue_status,
        "delivery_version": payload["delivery_version"],
        "delivery_revision_id": payload["delivery_revision_id"],
        "attempt_count": payload["attempt_count"],
        "available_at": payload["available_at"],
        "worker_id": payload["worker_id"],
        "lease_expires_at": payload["lease_expires_at"],
        "receipt_sha256": payload["receipt_sha256"],
        "error_code": payload["error_code"],
        "compensation_supported": payload["compensation_supported"],
        "compensates_effect_id": payload["compensates_effect_id"],
        "compensation_status": (
            None if previous is None else previous.get("compensation_status")
        ),
        "compensation_effect_id": (
            None if previous is None else previous.get("compensation_effect_id")
        ),
        "last_event_id": event.event_id,
        "last_event_sha256": event.event_sha256,
        "last_stream_version": event.stream_version,
    }


def _verify_queue_transition(
    current: Mapping[str, object], payload: Mapping[str, object]
) -> None:
    for name in (
        "session_id",
        "effect_id",
        "effect_type",
        "compensation_supported",
        "compensates_effect_id",
    ):
        if current.get(name) != payload.get(name):
            _transition_invalid("effect transition changes immutable identity")
    before = current.get("queue_status")
    after = payload.get("lifecycle_status")
    allowed = {
        "ready": {"leased"},
        "retry": {"leased"},
        "leased": {"leased", "retry", "succeeded", "dead_letter"},
        "succeeded": set(),
        "dead_letter": set(),
        "compensated": set(),
    }
    if after not in allowed.get(cast(str, before), set()):
        _transition_invalid("effect queue transition is invalid")
    previous_version = current.get("delivery_version")
    next_version = payload.get("delivery_version")
    if previous_version is not None or next_version is not None:
        if (
            type(previous_version) is not int
            or type(next_version) is not int
            or next_version != previous_version + 1
        ):
            _transition_invalid("effect delivery version is not contiguous")


def _descriptor(
    reducer_id: str,
    projection: str,
    input_event_types: tuple[str, ...],
    algorithm: str,
) -> ReducerDescriptor:
    inputs = tuple(sorted(input_event_types))
    return ReducerDescriptor(
        reducer_id=reducer_id,
        reducer_version=1,
        input_event_types=inputs,
        output_projection=projection,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": algorithm,
                "algorithm_version": 1,
                "event_types": list(inputs),
                "projection": projection,
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={event_type: 1 for event_type in inputs},
    )


def _typed_payload(
    reducer_event: ReducerEvent, expected_event_type: str
) -> dict[str, object]:
    typed = reducer_event.typed_event
    if (
        typed is None
        or reducer_event.source_event.event_type != expected_event_type
    ):
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_TYPED_INPUT_REQUIRED",
            "Outcome/Effect reducer requires its exact typed event",
        )
    payload = _thaw_json(typed.payload)
    if type(payload) is not dict:
        _fail(
            "TBM_OUTCOME_EFFECT_EVENT_PAYLOAD_INVALID",
            "Outcome/Effect event payload must be an object",
        )
    if reducer_event.source_event.stream_id != outcome_effect_stream_id(
        cast(str, payload.get("session_id"))
    ):
        _sequence_invalid("Outcome/Effect event stream does not match session")
    return cast(dict[str, object], payload)


def _payload_json_schemas() -> dict[str, Mapping[str, object]]:
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    json_text = {"type": "string", "minLength": 2, "maxLength": 262144}
    nullable_identifier = {"oneOf": [{"type": "null"}, identifier]}
    nullable_text = {"oneOf": [{"type": "null"}, text]}
    nullable_digest = {"oneOf": [{"type": "null"}, digest]}
    nullable_json = {"oneOf": [{"type": "null"}, json_text]}
    nullable_integer = {
        "oneOf": [
            {"type": "null"},
            {"type": "integer", "minimum": 1, "maximum": 2147483647},
        ]
    }

    def schema(properties: Mapping[str, object]) -> Mapping[str, object]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": dict(properties),
        }

    run_outcome = schema(
        {
            "session_id": identifier,
            "run_outcome_id": identifier,
            "record_sha256": digest,
            "record_json": json_text,
        }
    )
    attribution = schema(
        {
            "session_id": identifier,
            "run_outcome_id": identifier,
            "attribution_id": identifier,
            "claim_strength": {"enum": ["association", "causal"]},
            "recorded_at": text,
            "record_sha256": digest,
            "record_json": json_text,
        }
    )
    effect = schema(
        {
            "session_id": identifier,
            "effect_id": identifier,
            "effect_type": text,
            "lifecycle_status": {
                "enum": [
                    "ready",
                    "leased",
                    "retry",
                    "succeeded",
                    "dead_letter",
                    "compensation_requested",
                    "compensated",
                ]
            },
            "delivery_version": nullable_integer,
            "delivery_revision_id": nullable_identifier,
            "attempt_count": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000,
            },
            "available_at": nullable_text,
            "worker_id": nullable_identifier,
            "lease_expires_at": nullable_text,
            "receipt_sha256": nullable_digest,
            "error_code": nullable_text,
            "compensation_supported": {"type": "boolean"},
            "compensates_effect_id": nullable_identifier,
            "outbox_event_json": nullable_json,
            "delivery_json": nullable_json,
            "record_sha256": digest,
        }
    )
    result = {
        RUN_OUTCOME_RECORDED: run_outcome,
        OUTCOME_ATTRIBUTION_RECORDED: attribution,
    }
    result.update({event_type: effect for event_type in EFFECT_EVENT_TYPES})
    return result


def _effect_payload_properties() -> dict[str, object]:
    return cast(
        dict[str, object],
        _payload_json_schemas()[EFFECT_REQUESTED]["properties"],
    )


def _current_state(
    state: Mapping[str, object]
) -> Mapping[str, object] | None:
    current = _thaw_json(state.get("current"))
    if current is None:
        return None
    if type(current) is not dict:
        _state_invalid("singleton projection state is invalid")
    return cast(dict[str, object], current)


def _items_state(
    state: Mapping[str, object]
) -> tuple[Mapping[str, object], ...]:
    return tuple(_state_list(state, "items"))


def _effects_state(
    state: Mapping[str, object]
) -> Mapping[str, Mapping[str, object]]:
    effects = _state_mapping(state, "effects")
    return {
        key: cast(Mapping[str, object], value)
        for key, value in sorted(effects.items())
    }


def _state_list(
    state: Mapping[str, object], name: str
) -> list[dict[str, object]]:
    value = _thaw_json(state.get(name))
    if type(value) is not list or any(type(item) is not dict for item in value):
        _state_invalid(f"{name} projection state is invalid")
    return cast(list[dict[str, object]], value)


def _state_mapping(
    state: Mapping[str, object], name: str
) -> dict[str, dict[str, object]]:
    value = _thaw_json(state.get(name))
    if type(value) is not dict or any(
        type(key) is not str or type(item) is not dict
        for key, item in value.items()
    ):
        _state_invalid(f"{name} projection state is invalid")
    return cast(dict[str, dict[str, object]], value)


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _draft_invalid(f"{name} must be a bounded identifier")


def _bounded_text(value: object, name: str, maximum: int) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _draft_invalid(f"{name} must be bounded text")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [
            _thaw_json(item)
            for item in cast(list[object] | tuple[object, ...], value)
        ]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise OutcomeEffectEventV1Error(
            "TBM_OUTCOME_EFFECT_EVENT_CANONICALIZATION_FAILED",
            "Outcome/Effect event value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _draft_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_EFFECT_EVENT_DRAFT_INVALID", message)


def _sequence_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_EFFECT_EVENT_SEQUENCE_INVALID", message)


def _transition_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_EFFECT_EVENT_TRANSITION_INVALID", message)


def _projection_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_EFFECT_EVENT_PROJECTION_INVALID", message)


def _state_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_EFFECT_EVENT_STATE_INVALID", message)


def _fail(code: str, message: str) -> NoReturn:
    raise OutcomeEffectEventV1Error(code, message)


__all__ = [
    "EFFECT_COMPENSATED",
    "EFFECT_COMPENSATION_PROJECTION",
    "EFFECT_COMPENSATION_REDUCER_ID",
    "EFFECT_COMPENSATION_REQUESTED",
    "EFFECT_DEAD_LETTERED",
    "EFFECT_DEAD_LETTER_PROJECTION",
    "EFFECT_DEAD_LETTER_REDUCER_ID",
    "EFFECT_DELIVERY_HISTORY_PROJECTION",
    "EFFECT_DELIVERY_HISTORY_REDUCER_ID",
    "EFFECT_EVENT_TYPES",
    "EFFECT_QUEUE_PROJECTION",
    "EFFECT_QUEUE_REDUCER_ID",
    "EFFECT_REQUESTED",
    "EFFECT_RETRY_SCHEDULED",
    "EFFECT_STARTED",
    "EFFECT_SUCCEEDED",
    "OUTCOME_ATTRIBUTION_PROJECTION",
    "OUTCOME_ATTRIBUTION_RECORDED",
    "OUTCOME_ATTRIBUTION_REDUCER_ID",
    "OUTCOME_EFFECT_EVENT_MAX_BATCH",
    "OUTCOME_EFFECT_EVENT_MAX_APPEND_RETRIES",
    "OUTCOME_EFFECT_EVENT_PAYLOAD_SCHEMA_ID",
    "OUTCOME_EFFECT_EVENT_PROTOCOL_VERSION",
    "OUTCOME_EFFECT_EVENT_STREAM_TYPE",
    "OUTCOME_EFFECT_EVENT_TYPES",
    "RUN_OUTCOME_PROJECTION",
    "RUN_OUTCOME_RECORDED",
    "RUN_OUTCOME_REDUCER_ID",
    "EffectDeliveryEventSink",
    "OutcomeCompletionEventSink",
    "OutcomeEffectEventDraft",
    "OutcomeEffectEventLedgerProjector",
    "OutcomeEffectEventV1Error",
    "OutcomeEffectDeliveryHistoryReader",
    "OutcomeEffectOutcomeReader",
    "OutcomeEffectReducedViews",
    "OutcomeEffectSessionReader",
    "OutcomeEffectViews",
    "build_completion_outbox_effect_drafts",
    "build_effect_compensation_draft",
    "build_effect_compensation_reducer",
    "build_effect_dead_letter_reducer",
    "build_effect_delivery_history_reducer",
    "build_effect_queue_reducer",
    "build_effect_requested_draft",
    "build_effect_transition_draft",
    "build_outcome_attribution_draft",
    "build_outcome_attribution_reducer",
    "build_outcome_effect_event_batch",
    "build_outcome_effect_event_registry",
    "build_run_outcome_draft",
    "build_run_outcome_reducer",
    "dumps_outcome_effect_event_payload_dispatch_schema",
    "hydrate_outcome_effect_views",
    "outcome_effect_event_payload_dispatch_schema",
    "outcome_effect_stream_id",
    "reduce_outcome_effect_events",
]
