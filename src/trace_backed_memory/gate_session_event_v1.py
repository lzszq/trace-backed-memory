from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Literal, NoReturn, Protocol, cast

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError
from .event_registry_v1 import (
    EventPayloadRegistration,
    EventTypeRegistry,
)
from .event_v1 import (
    CanonicalEvent,
    EventSource,
    build_canonical_event,
    verify_event_parent,
)
from .entity_registry_v3 import EntityRegistrySnapshot
from .gate_session_v3 import (
    GATE_SESSION_CONTRACT_VERSION,
    GATE_SESSION_MAX_MEMORY_REVISIONS,
    GATE_SESSION_MAX_SEMANTIC_ATTEMPTS,
    GateSession,
    dumps_gate_session,
    parse_gate_session,
    renew_gate_session_lease,
    transition_gate_session,
)
from .ledger_port_v1 import (
    EventLedgerConflictError,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from .policy import (
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    execute_reducer_step,
    initial_reducer_state,
)


GATE_SESSION_EVENT_PROTOCOL_VERSION = "tbm.gate-session-event.v1"
GATE_SESSION_EVENT_REDUCER_ID = "gate-session-current"
GATE_SESSION_EVENT_PROJECTION = "gate_session_current_v1"
GATE_SESSION_EVENT_MAX_APPEND_RETRIES = 8
GATE_SESSION_EVENT_STREAM_TYPE = "gate_session"
GATE_SESSION_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "gate_session_event_payload_registry_v1.schema.json"
)

GATE_SESSION_BASELINE_IMPORTED = "tbm.gate_session.baseline_imported"
GATE_SESSION_CREATED = "tbm.gate_session.created"
GATE_SESSION_PREPARED = "tbm.gate_session.prepared"
GATE_SESSION_AWAITING_DECISION = "tbm.gate_session.awaiting_decision"
SEMANTIC_GATE_DECIDED = "tbm.gate_session.semantic_gate_decided"
USAGE_DECISION_FINALIZED = "tbm.gate_session.usage_decision_finalized"
EXECUTION_STARTED = "tbm.gate_session.execution_started"
GATE_SESSION_COMPLETED = "tbm.gate_session.completed"
GATE_SESSION_CANCELED = "tbm.gate_session.canceled"
GATE_SESSION_EXPIRED = "tbm.gate_session.expired"
EXECUTION_ABANDONED = "tbm.gate_session.execution_abandoned"
GATE_SESSION_LEASE_RENEWED = "tbm.gate_session.lease_renewed"

GateSessionEventTransition = Literal[
    "baseline_imported",
    "created",
    "prepared",
    "awaiting_decision",
    "semantic_gate_decided",
    "usage_decision_finalized",
    "execution_started",
    "completed",
    "canceled",
    "expired",
    "execution_abandoned",
    "lease_renewed",
]

_EVENT_TRANSITIONS: dict[str, GateSessionEventTransition] = {
    GATE_SESSION_BASELINE_IMPORTED: "baseline_imported",
    GATE_SESSION_CREATED: "created",
    GATE_SESSION_PREPARED: "prepared",
    GATE_SESSION_AWAITING_DECISION: "awaiting_decision",
    SEMANTIC_GATE_DECIDED: "semantic_gate_decided",
    USAGE_DECISION_FINALIZED: "usage_decision_finalized",
    EXECUTION_STARTED: "execution_started",
    GATE_SESSION_COMPLETED: "completed",
    GATE_SESSION_CANCELED: "canceled",
    GATE_SESSION_EXPIRED: "expired",
    EXECUTION_ABANDONED: "execution_abandoned",
    GATE_SESSION_LEASE_RENEWED: "lease_renewed",
}
GATE_SESSION_EVENT_TYPES = tuple(sorted(_EVENT_TRANSITIONS))

_STATUS_EVENT_TYPES = {
    "prepared": GATE_SESSION_PREPARED,
    "awaiting_decision": GATE_SESSION_AWAITING_DECISION,
    "decided": SEMANTIC_GATE_DECIDED,
    "finalized": USAGE_DECISION_FINALIZED,
    "executing": EXECUTION_STARTED,
    "completed": GATE_SESSION_COMPLETED,
    "canceled": GATE_SESSION_CANCELED,
    "expired": GATE_SESSION_EXPIRED,
    "abandoned": EXECUTION_ABANDONED,
}
_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in GATE_SESSION_EVENT_TYPES
}


class GateSessionEventV1Error(V3ContractError):
    """Stable failure for event-first GateSession projection handling."""


class GateSessionRevisionEventSink(Protocol):
    def append_and_reduce(
        self,
        current: GateSession | None,
        next_session: GateSession,
    ) -> GateSession: ...


class GateSessionTransitionCompanionSink(Protocol):
    def append_for_transition(
        self,
        current: GateSession | None,
        next_session: GateSession,
    ) -> None: ...


@dataclass(frozen=True)
class RegistryGateSessionLedgerAccessResolver:
    """Resolve one exact event partition from a trusted entity registry."""

    registry_provider: Callable[[], EntityRegistrySnapshot]
    actor_id: str = "service_durable_event_adapter"
    authorization_decision_id: str = "authorization_durable_event_append"

    def __post_init__(self) -> None:
        if not callable(self.registry_provider):
            raise TypeError("registry_provider must be callable")
        for value in (self.actor_id, self.authorization_decision_id):
            if type(value) is not str or not value or len(value) > 128:
                raise ValueError("event adapter identity must be bounded")

    def __call__(self, session: GateSession) -> LedgerAccessContext:
        if type(session) is not GateSession:
            _fail(
                "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
                "session must be exactly GateSession",
            )
        try:
            registry = self.registry_provider()
        except Exception as error:
            raise GateSessionEventV1Error(
                "TBM_GATE_SESSION_EVENT_REGISTRY_UNAVAILABLE",
                "trusted entity registry is unavailable",
            ) from error
        if type(registry) is not EntityRegistrySnapshot:
            _fail(
                "TBM_GATE_SESSION_EVENT_REGISTRY_INVALID",
                "registry provider returned an invalid snapshot",
            )
        tenants = tuple(
            item
            for item in registry.tenants
            if item.tenant_id == session.tenant_id and item.status == "active"
        )
        if len(tenants) != 1:
            _fail(
                "TBM_GATE_SESSION_EVENT_PARTITION_UNRESOLVED",
                "GateSession tenant does not resolve exactly once",
            )
        tenant = tenants[0]
        organizations = tuple(
            item
            for item in registry.organizations
            if item.organization_id == tenant.organization_id
            and item.status == "active"
        )
        environments = tuple(
            item
            for item in registry.environments
            if item.tenant_id == session.tenant_id
            and item.repository_id == session.repository_id
            and item.status == "active"
        )
        if len(organizations) != 1 or len(environments) != 1:
            _fail(
                "TBM_GATE_SESSION_EVENT_PARTITION_UNRESOLVED",
                "GateSession event partition is ambiguous or unavailable",
            )
        return LedgerAccessContext(
            partition=LedgerTenantPartition(
                organization_id=organizations[0].organization_id,
                tenant_id=session.tenant_id,
                repository_id=session.repository_id,
                environment_id=environments[0].environment_id,
            ),
            principal_id=session.principal_id,
            agent_client_id=session.agent_client_id,
            actor_type="service",
            actor_id=self.actor_id,
            authorization_decision_id=self.authorization_decision_id,
            classification_filter=LedgerClassificationFilter(("internal",)),
        )


@dataclass(frozen=True)
class GateSessionEventDraft:
    event_type: str
    transition: GateSessionEventTransition
    session: GateSession
    previous_session_sha256: str | None
    imported: bool = False

    def __post_init__(self) -> None:
        if self.event_type not in _EVENT_TRANSITIONS:
            _fail("TBM_GATE_SESSION_EVENT_TYPE_INVALID", "event type is invalid")
        if _EVENT_TRANSITIONS[self.event_type] != self.transition:
            _fail(
                "TBM_GATE_SESSION_EVENT_TYPE_INVALID",
                "event type and transition do not match",
            )
        if type(self.session) is not GateSession:
            _fail(
                "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
                "session must be exactly GateSession",
            )
        if self.previous_session_sha256 is not None:
            _digest(self.previous_session_sha256, "previous_session_sha256")
        if type(self.imported) is not bool:
            _fail(
                "TBM_GATE_SESSION_EVENT_DRAFT_INVALID",
                "imported must be a boolean",
            )
        if self.imported != (self.event_type == GATE_SESSION_BASELINE_IMPORTED):
            _fail(
                "TBM_GATE_SESSION_EVENT_DRAFT_INVALID",
                "only baseline events may be imported",
            )

    @property
    def session_sha256(self) -> str:
        return gate_session_projection_sha256(self.session)

    def payload(self) -> dict[str, object]:
        return {
            "transition": self.transition,
            "previous_session_sha256": self.previous_session_sha256,
            "session_sha256": self.session_sha256,
            "session": self.session.to_dict(),
        }

    def command_value(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "transition": self.transition,
            "previous_session_sha256": self.previous_session_sha256,
            "session_sha256": self.session_sha256,
            "imported": self.imported,
        }


class GateSessionEventLedgerProjector:
    """Append one revision event and rebuild before the SQL projection write."""

    def __init__(
        self,
        *,
        ledger_factory: Callable[[LedgerAccessContext], EventLedgerPort],
        access_resolver: Callable[[GateSession], LedgerAccessContext],
        transition_companion_sink: GateSessionTransitionCompanionSink
        | None = None,
    ) -> None:
        for callback in (ledger_factory, access_resolver):
            if not callable(callback):
                raise TypeError("GateSession event projector callbacks are invalid")
        if transition_companion_sink is not None and not callable(
            getattr(
                transition_companion_sink,
                "append_for_transition",
                None,
            )
        ):
            raise TypeError("GateSession transition companion sink is invalid")
        self._ledger_factory = ledger_factory
        self._access_resolver = access_resolver
        self._transition_companion_sink = transition_companion_sink
        self._event_registry = build_gate_session_event_registry()
        self._reducer = build_gate_session_reducer()

    @property
    def event_registry(self) -> EventTypeRegistry:
        return self._event_registry

    @property
    def reducer(self) -> FunctionalReducer:
        return self._reducer

    def read_events(
        self,
        session: GateSession,
    ) -> tuple[CanonicalEvent, ...]:
        """Read the complete bounded GateSession stream under trusted access."""

        if type(session) is not GateSession:
            _fail(
                "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
                "session must be exactly GateSession",
            )
        access = self._access_resolver(session)
        _verify_access(access, session)
        ledger = self._ledger_factory(access)
        _verify_ledger(ledger)
        try:
            return _read_stream(ledger, gate_session_stream_id(session.session_id))
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

    def rebuild_current(self, session: GateSession) -> GateSession | None:
        """Rebuild one current projection without consulting authority rows."""

        return reduce_gate_session_events(
            self.read_events(session),
            event_registry=self._event_registry,
            reducer=self._reducer,
        )

    def append_and_reduce(
        self,
        current: GateSession | None,
        next_session: GateSession,
    ) -> GateSession:
        if current is not None and type(current) is not GateSession:
            _fail(
                "TBM_GATE_SESSION_EVENT_CURRENT_INVALID",
                "current session must be exactly GateSession or null",
            )
        if type(next_session) is not GateSession:
            _fail(
                "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
                "next session must be exactly GateSession",
            )
        if self._transition_companion_sink is not None:
            self._transition_companion_sink.append_for_transition(
                current,
                next_session,
            )
        access = self._access_resolver(next_session)
        _verify_access(access, next_session)
        ledger = self._ledger_factory(access)
        _verify_ledger(ledger)
        try:
            stream_id = gate_session_stream_id(next_session.session_id)
            retained = _read_stream(ledger, stream_id)
            retained_current = reduce_gate_session_events(
                retained,
                event_registry=self._event_registry,
                reducer=self._reducer,
            )
            drafts = self._drafts_for_append(
                current,
                next_session,
                retained,
                retained_current,
            )
            recorded_at = _canonical_recorded_at(
                next_session.updated_at,
                next_session.updated_at,
                retained[-1].recorded_at if retained else None,
            )
            expected_version = retained[-1].stream_version if retained else 0
            previous_event = retained[-1] if retained else None
            for attempt in range(GATE_SESSION_EVENT_MAX_APPEND_RETRIES):
                high_watermark = ledger.read_global(
                    after_position=0,
                    limit=1,
                ).high_watermark_global_position
                events, idempotency = build_gate_session_event_batch(
                    drafts,
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
                    break
                except EventLedgerConflictError as error:
                    if (
                        error.code != "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"
                        or attempt + 1 >= GATE_SESSION_EVENT_MAX_APPEND_RETRIES
                    ):
                        raise
            else:  # pragma: no cover - bounded loop always breaks or raises
                raise AssertionError("event append retry loop did not terminate")

            rebuilt = reduce_gate_session_events(
                _read_stream(ledger, stream_id),
                event_registry=self._event_registry,
                reducer=self._reducer,
            )
            if rebuilt != next_session:
                _fail(
                    "TBM_GATE_SESSION_EVENT_PROJECTION_MISMATCH",
                    "event reducer did not reproduce the next GateSession",
                )
            return rebuilt
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _drafts_for_append(
        current: GateSession | None,
        next_session: GateSession,
        retained: tuple[CanonicalEvent, ...],
        retained_current: GateSession | None,
    ) -> tuple[GateSessionEventDraft, ...]:
        if retained:
            if current is None or retained_current != current:
                _fail(
                    "TBM_GATE_SESSION_EVENT_PROJECTION_DRIFT",
                    "retained event projection differs from the SQL projection",
                )
            return (build_gate_session_transition_draft(current, next_session),)
        if retained_current is not None:
            _fail(
                "TBM_GATE_SESSION_EVENT_PROJECTION_DRIFT",
                "an empty stream produced a non-empty projection",
            )
        if current is None:
            return (build_gate_session_transition_draft(None, next_session),)
        return (
            build_gate_session_baseline_draft(current),
            build_gate_session_transition_draft(current, next_session),
        )


def gate_session_projection_sha256(session: GateSession) -> str:
    if type(session) is not GateSession:
        _fail(
            "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
            "session must be exactly GateSession",
        )
    return _domain_sha256(
        b"tbm.gate-session-projection.v1\x00",
        json.loads(dumps_gate_session(session)),
    )


def gate_session_stream_id(session_id: str) -> str:
    if type(session_id) is not str or not session_id:
        _fail(
            "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
            "session_id must be a non-empty string",
        )
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return "gate_session_sha256_" + digest


def build_gate_session_baseline_draft(
    session: GateSession,
) -> GateSessionEventDraft:
    if type(session) is not GateSession:
        _fail(
            "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
            "session must be exactly GateSession",
        )
    return GateSessionEventDraft(
        event_type=GATE_SESSION_BASELINE_IMPORTED,
        transition="baseline_imported",
        session=session,
        previous_session_sha256=None,
        imported=True,
    )


def build_gate_session_transition_draft(
    current: GateSession | None,
    next_session: GateSession,
) -> GateSessionEventDraft:
    if type(next_session) is not GateSession:
        _fail(
            "TBM_GATE_SESSION_EVENT_SESSION_INVALID",
            "next session must be exactly GateSession",
        )
    if current is None:
        if next_session.status != "created" or next_session.version != 1:
            _fail(
                "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                "a native stream must begin with a created revision",
            )
        return GateSessionEventDraft(
            event_type=GATE_SESSION_CREATED,
            transition="created",
            session=next_session,
            previous_session_sha256=None,
        )
    if type(current) is not GateSession:
        _fail(
            "TBM_GATE_SESSION_EVENT_CURRENT_INVALID",
            "current session must be exactly GateSession or null",
        )
    if current.session_id != next_session.session_id:
        _fail(
            "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
            "GateSession event cannot change session_id",
        )
    if next_session.version != current.version + 1:
        _fail(
            "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
            "GateSession event revision must advance by one",
        )
    if next_session.status == current.status:
        event_type = GATE_SESSION_LEASE_RENEWED
    else:
        event_type = _STATUS_EVENT_TYPES.get(next_session.status)
        if event_type is None:
            _fail(
                "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                "GateSession target status has no event mapping",
            )
    return GateSessionEventDraft(
        event_type=event_type,
        transition=_EVENT_TRANSITIONS[event_type],
        session=next_session,
        previous_session_sha256=gate_session_projection_sha256(current),
    )


def build_gate_session_event_batch(
    drafts: tuple[GateSessionEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= 2
        or any(type(item) is not GateSessionEventDraft for item in drafts)
    ):
        _fail(
            "TBM_GATE_SESSION_EVENT_BATCH_INVALID",
            "event drafts must be a bounded non-empty tuple",
        )
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_GATE_SESSION_EVENT_ACCESS_INVALID",
            "ledger access must be exactly LedgerAccessContext",
        )
    if type(expected_stream_version) is not int or expected_stream_version < 0:
        _fail(
            "TBM_GATE_SESSION_EVENT_BATCH_INVALID",
            "expected stream version is invalid",
        )
    if type(next_global_position) is not int or next_global_position < 1:
        _fail(
            "TBM_GATE_SESSION_EVENT_BATCH_INVALID",
            "next global position is invalid",
        )
    session_id = drafts[0].session.session_id
    if any(item.session.session_id != session_id for item in drafts):
        _fail(
            "TBM_GATE_SESSION_EVENT_BATCH_INVALID",
            "event drafts must belong to one GateSession",
        )
    stream_id = gate_session_stream_id(session_id)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_GATE_SESSION_EVENT_BATCH_INVALID",
                "a nonzero stream version requires its parent event",
            )
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail(
            "TBM_GATE_SESSION_EVENT_BATCH_INVALID",
            "previous event does not match the expected stream head",
        )
    command_value = {
        "protocol_version": GATE_SESSION_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "drafts": [item.command_value() for item in drafts],
    }
    command_sha256 = _domain_sha256(
        b"tbm.gate-session-event-command.v1\x00",
        command_value,
    )
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.gate-session-event-idempotency.v1\x00",
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
        source = (
            EventSource(
                source_system="gate_session_projection_v3",
                source_record_id=session_id,
                evidence_quality="legacy_partial",
                observed_at=canonical_rfc3339(draft.session.updated_at),
            )
            if draft.imported
            else None
        )
        event = build_canonical_event(
            event_id="evt_gs_" + event_digest,
            event_type=draft.event_type,
            event_version=1,
            event_kind="observation" if draft.imported else "domain",
            origin="imported" if draft.imported else "native",
            source=source,
            stream_id=stream_id,
            stream_type=GATE_SESSION_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_gs_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_gs_" + correlation_digest[:32],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=draft.session.updated_at,
            recorded_at=recorded_at,
            producer="tbm_durable_gate_runtime",
            producer_version="f2-v1",
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification="internal",
            retention_policy_id="retention_gate_session_events",
            artifact_refs=(),
            payload=draft.payload(),
        )
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_gate_session_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    session_schema = _gate_session_schema()
    nullable_digest = {
        "oneOf": [
            {"type": "null"},
            {
                "type": "string",
                "pattern": r"^sha256:[0-9a-f]{64}$",
            },
        ]
    }
    for event_type in GATE_SESSION_EVENT_TYPES:
        transition = _EVENT_TRANSITIONS[event_type]
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind=(
                    "observation"
                    if event_type == GATE_SESSION_BASELINE_IMPORTED
                    else "domain"
                ),
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "transition",
                        "previous_session_sha256",
                        "session_sha256",
                        "session",
                    ],
                    "properties": {
                        "transition": {"const": transition},
                        "previous_session_sha256": nullable_digest,
                        "session_sha256": {
                            "type": "string",
                            "pattern": r"^sha256:[0-9a-f]{64}$",
                        },
                        "session": session_schema,
                    },
                },
            )
        )
    return registry.seal()


def gate_session_event_payload_dispatch_schema() -> dict[str, object]:
    """Return the frozen dispatch schema for every GateSession event payload."""

    schema = build_gate_session_event_registry().dispatch_schema()
    schema["$id"] = GATE_SESSION_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory GateSession event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed GateSession tbm.event-registry.v1 catalog. "
        "The GateSession event runtime remains the authoritative validator."
    )
    return schema


def dumps_gate_session_event_payload_dispatch_schema() -> str:
    """Serialize the GateSession dispatch schema as deterministic JSON."""

    return json.dumps(
        gate_session_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_gate_session_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=GATE_SESSION_EVENT_REDUCER_ID,
        reducer_version=1,
        input_event_types=GATE_SESSION_EVENT_TYPES,
        output_projection=GATE_SESSION_EVENT_PROJECTION,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "gate-session-current",
                "algorithm_version": 1,
                "event_transitions": _EVENT_TRANSITIONS,
                "projection_fields": [
                    "session",
                    "last_event_sha256",
                    "last_global_position",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={
            event_type: 1 for event_type in GATE_SESSION_EVENT_TYPES
        },
    )

    def initial() -> Mapping[str, object]:
        return {
            "session": None,
            "last_event_sha256": None,
            "last_global_position": 0,
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        typed = reducer_event.typed_event
        if typed is None:
            _fail(
                "TBM_GATE_SESSION_EVENT_TYPED_INPUT_REQUIRED",
                "GateSession reducer requires typed input",
            )
        payload = _thaw_json(typed.payload)
        if type(payload) is not dict:
            _fail(
                "TBM_GATE_SESSION_EVENT_PAYLOAD_INVALID",
                "GateSession event payload must be an object",
            )
        raw_session = payload.get("session")
        if type(raw_session) is not dict:
            _fail(
                "TBM_GATE_SESSION_EVENT_PAYLOAD_INVALID",
                "GateSession event payload is missing its session",
            )
        next_session = parse_gate_session(raw_session)
        if gate_session_projection_sha256(next_session) != payload.get(
            "session_sha256"
        ):
            _fail(
                "TBM_GATE_SESSION_EVENT_PAYLOAD_INVALID",
                "GateSession event session digest does not match",
            )
        if reducer_event.source_event.stream_id != gate_session_stream_id(
            next_session.session_id
        ):
            _fail(
                "TBM_GATE_SESSION_EVENT_STREAM_INVALID",
                "GateSession event stream identity does not match",
            )
        raw_current = _thaw_json(state.get("session"))
        if raw_current is None:
            current = None
        elif type(raw_current) is dict:
            current = parse_gate_session(raw_current)
        else:
            _fail(
                "TBM_GATE_SESSION_EVENT_STATE_INVALID",
                "GateSession reducer state is invalid",
            )
        event_type = reducer_event.source_event.event_type
        previous_digest = payload.get("previous_session_sha256")
        if event_type == GATE_SESSION_BASELINE_IMPORTED:
            if current is not None or previous_digest is not None:
                _fail(
                    "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                    "baseline import must initialize an empty projection",
                )
        elif event_type == GATE_SESSION_CREATED:
            if (
                current is not None
                or previous_digest is not None
                or next_session.status != "created"
                or next_session.version != 1
            ):
                _fail(
                    "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                    "created event must initialize revision one",
                )
        else:
            if current is None:
                _fail(
                    "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                    "transition event requires a current GateSession",
                )
            if previous_digest != gate_session_projection_sha256(current):
                _fail(
                    "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                    "transition event previous digest does not match",
                )
            expected = _expected_next_session(current, next_session, event_type)
            if expected != next_session:
                _fail(
                    "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                    "transition event does not reproduce the next revision",
                )
        return {
            "session": next_session.to_dict(),
            "last_event_sha256": reducer_event.source_event.event_sha256,
            "last_global_position": reducer_event.source_event.global_position,
        }

    return FunctionalReducer(descriptor, initial, transition)


def reduce_gate_session_events(
    events: tuple[CanonicalEvent, ...],
    *,
    event_registry: EventTypeRegistry | None = None,
    reducer: FunctionalReducer | None = None,
) -> GateSession | None:
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _fail(
            "TBM_GATE_SESSION_EVENT_SEQUENCE_INVALID",
            "events must be a tuple of CanonicalEvent values",
        )
    if not events:
        return None
    registry = (
        build_gate_session_event_registry()
        if event_registry is None
        else event_registry
    )
    selected_reducer = build_gate_session_reducer() if reducer is None else reducer
    if type(registry) is not EventTypeRegistry or not registry.sealed:
        _fail(
            "TBM_GATE_SESSION_EVENT_REGISTRY_INVALID",
            "event registry must be a sealed EventTypeRegistry",
        )
    if type(selected_reducer) is not FunctionalReducer:
        _fail(
            "TBM_GATE_SESSION_EVENT_REDUCER_INVALID",
            "reducer must be exactly FunctionalReducer",
        )
    step = initial_reducer_state(selected_reducer)
    parent: CanonicalEvent | None = None
    for event in events:
        if parent is None:
            if (
                event.stream_version != 1
                or event.previous_stream_event_sha256 is not None
            ):
                _fail(
                    "TBM_GATE_SESSION_EVENT_SEQUENCE_INVALID",
                    "GateSession event stream must begin at version one",
                )
        else:
            verify_event_parent(event, parent)
        typed = registry.consume(event, target_version=1)
        step = execute_reducer_step(
            selected_reducer,
            step.state,
            ReducerEvent(event, typed),
        )
        parent = event
    raw_session = _thaw_json(step.state.get("session"))
    if type(raw_session) is not dict:
        _fail(
            "TBM_GATE_SESSION_EVENT_STATE_INVALID",
            "GateSession reducer did not produce a current session",
        )
    return parse_gate_session(raw_session)


def _expected_next_session(
    current: GateSession,
    next_session: GateSession,
    event_type: str,
) -> GateSession:
    if event_type == GATE_SESSION_LEASE_RENEWED:
        if (
            next_session.status != current.status
            or next_session.lease_expires_at is None
        ):
            _fail(
                "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
                "lease-renewed event has an invalid target",
            )
        return renew_gate_session_lease(
            current,
            expected_version=current.version,
            updated_at=next_session.updated_at,
            lease_expires_at=next_session.lease_expires_at,
        )
    expected_type = _STATUS_EVENT_TYPES.get(next_session.status)
    if expected_type != event_type:
        _fail(
            "TBM_GATE_SESSION_EVENT_TRANSITION_INVALID",
            "event type does not match the target status",
        )
    kwargs: dict[str, object] = {}
    if next_session.status == "prepared":
        kwargs.update(
            lease_expires_at=next_session.lease_expires_at,
            retrieval_snapshot_id=next_session.retrieval_snapshot_id,
            system_gate_evaluation_id=next_session.system_gate_evaluation_id,
        )
    elif next_session.status == "decided":
        kwargs.update(
            semantic_gate_attempt_ids=next_session.semantic_gate_attempt_ids,
            decision_id=next_session.decision_id,
        )
    elif next_session.status == "finalized":
        kwargs.update(
            final_memory_revision_ids=next_session.final_memory_revision_ids,
            injection_artifact_id=next_session.injection_artifact_id,
            usage_decision_id=next_session.usage_decision_id,
        )
    elif next_session.status == "completed":
        kwargs["run_outcome_id"] = next_session.run_outcome_id
    elif next_session.status in {"canceled", "expired", "abandoned"}:
        kwargs["terminal_reason"] = next_session.terminal_reason
    return transition_gate_session(
        current,
        cast(str, next_session.status),
        expected_version=current.version,
        updated_at=next_session.updated_at,
        **kwargs,  # type: ignore[arg-type]
    )


def _gate_session_schema() -> dict[str, object]:
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": MEMORY_ID_MAX_CHARS,
    }
    nullable_identifier = {
        "oneOf": [{"type": "null"}, identifier],
    }
    timestamp = {"type": "string", "minLength": 1, "maxLength": 64}
    nullable_timestamp = {"oneOf": [{"type": "null"}, timestamp]}
    identifier_array = {
        "type": "array",
        "items": identifier,
        "minItems": 0,
        "uniqueItems": True,
    }
    fields = [
        "contract_version",
        "session_id",
        "tenant_id",
        "repository_id",
        "principal_id",
        "agent_client_id",
        "trace_id",
        "run_id",
        "request_fingerprint",
        "idempotency_key",
        "status",
        "version",
        "created_at",
        "updated_at",
        "expires_at",
        "lease_expires_at",
        "retrieval_snapshot_id",
        "system_gate_evaluation_id",
        "semantic_gate_attempt_ids",
        "decision_id",
        "final_memory_revision_ids",
        "injection_artifact_id",
        "usage_decision_id",
        "run_outcome_id",
        "terminal_reason",
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": fields,
        "properties": {
            "contract_version": {"const": GATE_SESSION_CONTRACT_VERSION},
            "session_id": identifier,
            "tenant_id": identifier,
            "repository_id": identifier,
            "principal_id": identifier,
            "agent_client_id": identifier,
            "trace_id": identifier,
            "run_id": identifier,
            "request_fingerprint": {
                "type": "string",
                "pattern": r"^sha256:[0-9a-f]{64}$",
            },
            "idempotency_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": METADATA_VALUE_MAX_CHARS,
            },
            "status": {
                "enum": [
                    "created",
                    "prepared",
                    "awaiting_decision",
                    "decided",
                    "finalized",
                    "executing",
                    "completed",
                    "canceled",
                    "expired",
                    "abandoned",
                ]
            },
            "version": {"type": "integer", "minimum": 1},
            "created_at": timestamp,
            "updated_at": timestamp,
            "expires_at": timestamp,
            "lease_expires_at": nullable_timestamp,
            "retrieval_snapshot_id": nullable_identifier,
            "system_gate_evaluation_id": nullable_identifier,
            "semantic_gate_attempt_ids": {
                **identifier_array,
                "maxItems": GATE_SESSION_MAX_SEMANTIC_ATTEMPTS,
            },
            "decision_id": nullable_identifier,
            "final_memory_revision_ids": {
                **identifier_array,
                "maxItems": GATE_SESSION_MAX_MEMORY_REVISIONS,
            },
            "injection_artifact_id": nullable_identifier,
            "usage_decision_id": nullable_identifier,
            "run_outcome_id": nullable_identifier,
            "terminal_reason": {
                "oneOf": [
                    {"type": "null"},
                    {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MEMORY_DECISION_REASON_MAX_CHARS,
                    },
                ]
            },
        },
    }


def _read_stream(
    ledger: EventLedgerPort,
    stream_id: str,
) -> tuple[CanonicalEvent, ...]:
    events: list[CanonicalEvent] = []
    from_version = 1
    while True:
        page = ledger.read_stream(stream_id, from_version=from_version, limit=100)
        events.extend(page.events)
        if not page.has_more:
            break
        if page.next_stream_version is None:
            _fail(
                "TBM_GATE_SESSION_EVENT_SEQUENCE_INVALID",
                "stream page omitted its continuation version",
            )
        from_version = page.next_stream_version
    return tuple(events)


def _verify_access(access: object, session: GateSession) -> None:
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_GATE_SESSION_EVENT_ACCESS_INVALID",
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
            "TBM_GATE_SESSION_EVENT_ACCESS_INVALID",
            "ledger access does not match the GateSession scope",
        )


def _verify_ledger(ledger: object) -> None:
    if not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global")
    ):
        _fail(
            "TBM_GATE_SESSION_EVENT_LEDGER_INVALID",
            "ledger factory returned an invalid event ledger",
        )


def _canonical_recorded_at(
    candidate: str,
    occurred_at: str,
    parent_recorded_at: str | None,
) -> str:
    try:
        selected = max(
            item
            for item in (
                parse_rfc3339(candidate),
                parse_rfc3339(occurred_at),
                (
                    parse_rfc3339(parent_recorded_at)
                    if parent_recorded_at is not None
                    else parse_rfc3339(occurred_at)
                ),
            )
        )
    except (TypeError, ValueError) as error:
        raise GateSessionEventV1Error(
            "TBM_GATE_SESSION_EVENT_TIMESTAMP_INVALID",
            "event recorded timestamp is invalid",
        ) from error
    return canonical_rfc3339(selected.isoformat())


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [
            _thaw_json(item) for item in cast(list[object] | tuple[object, ...], value)
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
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise GateSessionEventV1Error(
            "TBM_GATE_SESSION_EVENT_NON_CANONICAL",
            "GateSession event value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _digest(value: object, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        _fail(
            "TBM_GATE_SESSION_EVENT_DIGEST_INVALID",
            f"{name} must be a canonical SHA-256 digest",
        )


def _fail(code: str, message: str) -> NoReturn:
    raise GateSessionEventV1Error(code, message)


__all__ = [
    "EXECUTION_ABANDONED",
    "EXECUTION_STARTED",
    "GATE_SESSION_AWAITING_DECISION",
    "GATE_SESSION_BASELINE_IMPORTED",
    "GATE_SESSION_CANCELED",
    "GATE_SESSION_COMPLETED",
    "GATE_SESSION_CREATED",
    "GATE_SESSION_EVENT_MAX_APPEND_RETRIES",
    "GATE_SESSION_EVENT_PAYLOAD_SCHEMA_ID",
    "GATE_SESSION_EVENT_PROJECTION",
    "GATE_SESSION_EVENT_PROTOCOL_VERSION",
    "GATE_SESSION_EVENT_REDUCER_ID",
    "GATE_SESSION_EVENT_STREAM_TYPE",
    "GATE_SESSION_EVENT_TYPES",
    "GATE_SESSION_EXPIRED",
    "GATE_SESSION_LEASE_RENEWED",
    "GATE_SESSION_PREPARED",
    "SEMANTIC_GATE_DECIDED",
    "USAGE_DECISION_FINALIZED",
    "GateSessionEventDraft",
    "GateSessionEventLedgerProjector",
    "GateSessionEventTransition",
    "GateSessionEventV1Error",
    "GateSessionRevisionEventSink",
    "GateSessionTransitionCompanionSink",
    "RegistryGateSessionLedgerAccessResolver",
    "build_gate_session_baseline_draft",
    "build_gate_session_event_batch",
    "build_gate_session_event_registry",
    "build_gate_session_reducer",
    "build_gate_session_transition_draft",
    "dumps_gate_session_event_payload_dispatch_schema",
    "gate_session_event_payload_dispatch_schema",
    "gate_session_projection_sha256",
    "gate_session_stream_id",
    "reduce_gate_session_events",
]
