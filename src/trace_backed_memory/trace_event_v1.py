from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Literal, NoReturn, cast

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .authorization_v3 import AuthorizationDecision
from .contracts_v3 import V3ContractError
from .event_registry_v1 import (
    EventPayloadRegistration,
    EventRegistryV1Error,
    EventTypeRegistry,
)
from .event_v1 import (
    EVENT_MAX_ARTIFACT_REFS,
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    build_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerIdempotency,
    verify_ledger_append_receipt,
)


TRACE_EVENT_PROTOCOL_VERSION = "tbm.trace-event.v1"
TRACE_EVENT_STREAM_TYPE = "trace_event"
TRACE_EVENT_MAX_BATCH = EVENT_LEDGER_MAX_APPEND_BATCH
TRACE_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "trace_event_payload_registry_v1.schema.json"
)

TRACE_SESSION_STARTED = "tbm.trace.session_started"
TRACE_USER_PROMPT_SUBMITTED = "tbm.trace.user_prompt_submitted"
TRACE_TOOL_STARTED = "tbm.trace.tool_started"
TRACE_PERMISSION_RECORDED = "tbm.trace.permission_recorded"
TRACE_TOOL_COMPLETED = "tbm.trace.tool_completed"
TRACE_SUBAGENT_STARTED = "tbm.trace.subagent_started"
TRACE_SUBAGENT_STOPPED = "tbm.trace.subagent_stopped"
TRACE_PRE_COMPACT = "tbm.trace.pre_compact"
TRACE_STOPPED = "tbm.trace.stopped"
TRACE_SESSION_ENDED = "tbm.trace.session_ended"
TRACE_DIFF_OBSERVED = "tbm.trace.diff_observed"
TRACE_FINAL_RESPONSE_RECORDED = "tbm.trace.final_response_recorded"

TRACE_EVENT_TYPES = tuple(
    sorted(
        (
            TRACE_SESSION_STARTED,
            TRACE_USER_PROMPT_SUBMITTED,
            TRACE_TOOL_STARTED,
            TRACE_PERMISSION_RECORDED,
            TRACE_TOOL_COMPLETED,
            TRACE_SUBAGENT_STARTED,
            TRACE_SUBAGENT_STOPPED,
            TRACE_PRE_COMPACT,
            TRACE_STOPPED,
            TRACE_SESSION_ENDED,
            TRACE_DIFF_OBSERVED,
            TRACE_FINAL_RESPONSE_RECORDED,
        )
    )
)

TraceToolPhase = Literal["request", "permission", "result"]
TracePermissionStatus = Literal["allowed", "denied", "unknown"]
TraceLineageRole = Literal["root", "subagent"]

_TOOL_EVENT_PHASES: dict[str, TraceToolPhase] = {
    TRACE_TOOL_STARTED: "request",
    TRACE_PERMISSION_RECORDED: "permission",
    TRACE_TOOL_COMPLETED: "result",
}
_SUBAGENT_EVENT_TYPES = frozenset(
    {TRACE_SUBAGENT_STARTED, TRACE_SUBAGENT_STOPPED}
)
_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in TRACE_EVENT_TYPES
}
_CLASSIFICATIONS = frozenset(
    {"public", "internal", "confidential", "restricted"}
)
_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_IDENTIFIER_MAX_CHARS = 128
_CODE_MAX_CHARS = 256
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ARTIFACT_ID_PATTERN = r"^artifact_sha256_[0-9a-f]{64}$"
_EVENT_ID_PATTERN = r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$"
_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)


class TraceEventV1Error(V3ContractError):
    """Stable failure raised by the ordered TraceEvent protocol."""


@dataclass(frozen=True)
class TraceToolCorrelation:
    tool_call_id: str
    tool_name: str
    phase: TraceToolPhase
    invocation_sha256: str
    parent_tool_call_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.tool_call_id, "tool_call_id")
        _code(self.tool_name, "tool_name")
        if self.phase not in {"request", "permission", "result"}:
            _fail(
                "TBM_TRACE_EVENT_TOOL_CORRELATION_INVALID",
                "tool phase is invalid",
            )
        _digest(self.invocation_sha256, "invocation_sha256")
        _optional_identifier(self.parent_tool_call_id, "parent_tool_call_id")
        if self.parent_tool_call_id == self.tool_call_id:
            _fail(
                "TBM_TRACE_EVENT_TOOL_CORRELATION_INVALID",
                "tool correlation cannot name itself as parent",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "phase": self.phase,
            "invocation_sha256": self.invocation_sha256,
            "parent_tool_call_id": self.parent_tool_call_id,
        }


@dataclass(frozen=True)
class TracePermissionResult:
    decision_id: str
    permission: str
    status: TracePermissionStatus
    reason_code: str
    decided_at: str
    request_sha256: str
    policy_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.decision_id, "permission decision_id")
        _code(self.permission, "permission")
        if self.status not in {"allowed", "denied", "unknown"}:
            _fail(
                "TBM_TRACE_EVENT_PERMISSION_RESULT_INVALID",
                "permission status is invalid",
            )
        _code(self.reason_code, "permission reason_code")
        if (self.status == "allowed") != (self.reason_code == "allowed"):
            _fail(
                "TBM_TRACE_EVENT_PERMISSION_RESULT_INVALID",
                "allowed permission status and reason must agree",
            )
        _canonical_timestamp(self.decided_at, "permission decided_at")
        _digest(self.request_sha256, "permission request_sha256")
        _digest(self.policy_sha256, "permission policy_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "permission": self.permission,
            "status": self.status,
            "reason_code": self.reason_code,
            "decided_at": self.decided_at,
            "request_sha256": self.request_sha256,
            "policy_sha256": self.policy_sha256,
        }


@dataclass(frozen=True)
class TraceEventLineage:
    role: TraceLineageRole
    subagent_id: str | None
    parent_trace_id: str | None
    parent_event_id: str | None

    def __post_init__(self) -> None:
        if self.role == "root":
            if any(
                value is not None
                for value in (
                    self.subagent_id,
                    self.parent_trace_id,
                    self.parent_event_id,
                )
            ):
                _fail(
                    "TBM_TRACE_EVENT_LINEAGE_INVALID",
                    "root lineage cannot claim a parent or subagent identity",
                )
            return
        if self.role != "subagent":
            _fail(
                "TBM_TRACE_EVENT_LINEAGE_INVALID",
                "lineage role is invalid",
            )
        _identifier(self.subagent_id, "subagent_id")
        _identifier(self.parent_trace_id, "parent_trace_id")
        _event_id(self.parent_event_id, "parent_event_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "subagent_id": self.subagent_id,
            "parent_trace_id": self.parent_trace_id,
            "parent_event_id": self.parent_event_id,
        }


@dataclass(frozen=True)
class TraceEventDraft:
    event_type: str
    trace_id: str
    run_id: str
    sequence: int
    occurred_at: str
    artifact_refs: tuple[EventArtifactRef, ...]
    tool: TraceToolCorrelation | None
    permission_result: TracePermissionResult | None
    lineage: TraceEventLineage
    related_subagent_id: str | None
    classification: EventClassification = "internal"
    retention_policy_id: str = "retention_trace_events"

    def __post_init__(self) -> None:
        if self.event_type not in TRACE_EVENT_TYPES:
            _fail(
                "TBM_TRACE_EVENT_TYPE_INVALID",
                "TraceEvent type is not registered",
            )
        _identifier(self.trace_id, "trace_id")
        _identifier(self.run_id, "run_id")
        _sequence(self.sequence)
        _canonical_timestamp(self.occurred_at, "occurred_at")
        _artifact_ref_tuple(self.artifact_refs)
        if self.tool is not None and type(self.tool) is not TraceToolCorrelation:
            _fail(
                "TBM_TRACE_EVENT_TOOL_CORRELATION_INVALID",
                "tool correlation must be exactly TraceToolCorrelation or null",
            )
        if (
            self.permission_result is not None
            and type(self.permission_result) is not TracePermissionResult
        ):
            _fail(
                "TBM_TRACE_EVENT_PERMISSION_RESULT_INVALID",
                "permission result must be exactly TracePermissionResult or null",
            )
        if type(self.lineage) is not TraceEventLineage:
            _fail(
                "TBM_TRACE_EVENT_LINEAGE_INVALID",
                "lineage must be exactly TraceEventLineage",
            )
        if self.lineage.parent_trace_id == self.trace_id:
            _fail(
                "TBM_TRACE_EVENT_LINEAGE_INVALID",
                "subagent trace cannot be its own parent trace",
            )
        _optional_identifier(self.related_subagent_id, "related_subagent_id")
        if self.classification not in _CLASSIFICATIONS:
            _fail(
                "TBM_TRACE_EVENT_CLASSIFICATION_INVALID",
                "TraceEvent classification is invalid",
            )
        _identifier(self.retention_policy_id, "retention_policy_id")
        for reference in self.artifact_refs:
            if (
                _CLASSIFICATION_RANK[reference.classification]
                > _CLASSIFICATION_RANK[self.classification]
            ):
                _fail(
                    "TBM_TRACE_EVENT_CLASSIFICATION_INVALID",
                    "TraceEvent classification cannot be lower than an artifact",
                )
        expected_phase = _TOOL_EVENT_PHASES.get(self.event_type)
        if expected_phase is None:
            if self.tool is not None or self.permission_result is not None:
                _fail(
                    "TBM_TRACE_EVENT_TOOL_CORRELATION_INVALID",
                    "non-tool TraceEvent cannot carry tool or permission state",
                )
        else:
            if self.tool is None or self.tool.phase != expected_phase:
                _fail(
                    "TBM_TRACE_EVENT_TOOL_CORRELATION_INVALID",
                    "TraceEvent tool phase does not match its event type",
                )
            if (
                self.event_type == TRACE_PERMISSION_RECORDED
            ) != (self.permission_result is not None):
                _fail(
                    "TBM_TRACE_EVENT_PERMISSION_RESULT_INVALID",
                    "only permission-recorded events carry a permission result",
                )
            if (
                self.permission_result is not None
                and parse_rfc3339(self.permission_result.decided_at)
                > parse_rfc3339(self.occurred_at)
            ):
                _fail(
                    "TBM_TRACE_EVENT_PERMISSION_RESULT_INVALID",
                    "permission decision cannot follow the TraceEvent occurrence",
                )
        if (self.event_type in _SUBAGENT_EVENT_TYPES) != (
            self.related_subagent_id is not None
        ):
            _fail(
                "TBM_TRACE_EVENT_LINEAGE_INVALID",
                "only subagent start/stop events name a related subagent",
            )

    def payload(self) -> dict[str, object]:
        return {
            "protocol_version": TRACE_EVENT_PROTOCOL_VERSION,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "artifact_ids": [item.artifact_id for item in self.artifact_refs],
            "tool": None if self.tool is None else self.tool.to_dict(),
            "permission_result": (
                None
                if self.permission_result is None
                else self.permission_result.to_dict()
            ),
            "lineage": self.lineage.to_dict(),
            "related_subagent_id": self.related_subagent_id,
        }

    def command_value(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "payload": self.payload(),
        }


def trace_permission_result_from_authorization_decision(
    decision: AuthorizationDecision,
) -> TracePermissionResult:
    if type(decision) is not AuthorizationDecision:
        _fail(
            "TBM_TRACE_EVENT_PERMISSION_RESULT_INVALID",
            "authorization decision must be exactly AuthorizationDecision",
        )
    return TracePermissionResult(
        decision_id=decision.authorization_event_id,
        permission=decision.permission,
        status="allowed" if decision.allowed else "denied",
        reason_code=decision.reason,
        decided_at=canonical_rfc3339(decision.decided_at),
        request_sha256=decision.request_sha256,
        policy_sha256=decision.policy_sha256,
    )


def trace_event_stream_id(trace_id: str) -> str:
    _identifier(trace_id, "trace_id")
    digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()
    return "trace_event_" + digest


def build_trace_event_batch(
    drafts: tuple[TraceEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= TRACE_EVENT_MAX_BATCH
        or any(type(item) is not TraceEventDraft for item in drafts)
    ):
        _fail(
            "TBM_TRACE_EVENT_BATCH_INVALID",
            "TraceEvent drafts must be a bounded non-empty tuple",
        )
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_TRACE_EVENT_ACCESS_INVALID",
            "ledger access must be exactly LedgerAccessContext",
        )
    if (
        type(expected_stream_version) is not int
        or not 0 <= expected_stream_version <= _MAX_SEQUENCE
    ):
        _fail(
            "TBM_TRACE_EVENT_BATCH_INVALID",
            "expected stream version is invalid",
        )
    if (
        type(next_global_position) is not int
        or not 1 <= next_global_position <= _MAX_SEQUENCE
    ):
        _fail(
            "TBM_TRACE_EVENT_BATCH_INVALID",
            "next global position is invalid",
        )
    canonical_recorded_at = _canonical_timestamp(recorded_at, "recorded_at")
    trace_id = drafts[0].trace_id
    run_id = drafts[0].run_id
    lineage = drafts[0].lineage
    if any(
        item.trace_id != trace_id
        or item.run_id != run_id
        or item.lineage != lineage
        for item in drafts
    ):
        _fail(
            "TBM_TRACE_EVENT_BATCH_INVALID",
            "TraceEvent batch must belong to one trace, run, and lineage",
        )
    for offset, draft in enumerate(drafts, start=1):
        if draft.sequence != expected_stream_version + offset:
            _fail(
                "TBM_TRACE_EVENT_SEQUENCE_INVALID",
                "TraceEvent sequence must be contiguous from the expected head",
            )
    stream_id = trace_event_stream_id(trace_id)
    parent = previous_event
    previous_occurred_at = None
    if parent is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_TRACE_EVENT_BATCH_INVALID",
                "nonzero TraceEvent stream version requires its parent event",
            )
    else:
        if (
            type(parent) is not CanonicalEvent
            or parent.stream_id != stream_id
            or parent.stream_version != expected_stream_version
        ):
            _fail(
                "TBM_TRACE_EVENT_BATCH_INVALID",
                "previous event does not match the TraceEvent stream head",
            )
        verify_trace_event(parent)
        parent_payload = cast(dict[str, object], _thaw_json(parent.payload))
        if (
            parent_payload["trace_id"] != trace_id
            or parent_payload["run_id"] != run_id
            or parent_payload["lineage"] != lineage.to_dict()
        ):
            _fail(
                "TBM_TRACE_EVENT_BATCH_INVALID",
                "previous TraceEvent belongs to another trace, run, or lineage",
            )
        previous_occurred_at = cast(str, parent_payload["occurred_at"])
    for draft in drafts:
        if (
            previous_occurred_at is not None
            and parse_rfc3339(draft.occurred_at)
            < parse_rfc3339(previous_occurred_at)
        ):
            _fail(
                "TBM_TRACE_EVENT_TIMESTAMP_INVALID",
                "TraceEvent occurrence timestamps cannot move backwards",
            )
        previous_occurred_at = draft.occurred_at
    command_value = {
        "protocol_version": TRACE_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "recorded_at": canonical_recorded_at,
        "drafts": [item.command_value() for item in drafts],
    }
    command_sha256 = _domain_sha256(
        b"tbm.trace-event-command.v1\x00", command_value
    )
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.trace-event-idempotency.v1\x00", command_value
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    correlation_digest = hashlib.sha256(
        (
            access.partition.partition_sha256
            + "\x00"
            + trace_id
            + "\x00"
            + run_id
        ).encode("utf-8")
    ).hexdigest()
    parent = previous_event
    events: list[CanonicalEvent] = []
    for offset, draft in enumerate(drafts):
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        event = build_canonical_event(
            event_id="evt_trace_" + event_digest,
            event_type=draft.event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=TRACE_EVENT_STREAM_TYPE,
            stream_version=draft.sequence,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_trace_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_trace_" + correlation_digest[:32],
            causation_id=(
                parent.event_id
                if parent is not None
                else draft.lineage.parent_event_id
            ),
            occurred_at=draft.occurred_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_trace_event_adapter",
            producer_version="f3-v1",
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification=draft.classification,
            retention_policy_id=draft.retention_policy_id,
            artifact_refs=draft.artifact_refs,
            payload=draft.payload(),
        )
        verify_trace_event(event)
        if parent is not None:
            verify_event_parent(event, parent)
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_trace_event_append_request(
    drafts: tuple[TraceEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> LedgerAppendRequest:
    events, idempotency = build_trace_event_batch(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )
    return LedgerAppendRequest(
        access=access,
        stream_id=events[0].stream_id,
        expected_stream_version=expected_stream_version,
        events=events,
        idempotency=idempotency,
    )


def append_trace_event_batch(
    ledger: EventLedgerPort,
    drafts: tuple[TraceEventDraft, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> LedgerAppendReceipt:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not callable(
        getattr(ledger, "append", None)
    ):
        _fail(
            "TBM_TRACE_EVENT_LEDGER_INVALID",
            "TraceEvent append requires an access-bound EventLedgerPort",
        )
    request = build_trace_event_append_request(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )
    receipt = ledger.append(
        request.stream_id,
        request.expected_stream_version,
        request.events,
        request.idempotency,
    )
    verify_ledger_append_receipt(request, receipt)
    return receipt


def verify_trace_event(event: CanonicalEvent) -> None:
    if type(event) is not CanonicalEvent:
        _fail(
            "TBM_TRACE_EVENT_INVALID",
            "TraceEvent must be exactly CanonicalEvent",
        )
    if (
        event.event_type not in TRACE_EVENT_TYPES
        or event.event_version != 1
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != TRACE_EVENT_STREAM_TYPE
        or event.occurred_at is None
    ):
        _fail(
            "TBM_TRACE_EVENT_INVALID",
            "canonical event is not a native TraceEvent v1",
        )
    try:
        payload = build_trace_event_registry().consume(event).payload
    except EventRegistryV1Error as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_PAYLOAD_INVALID",
            "TraceEvent payload does not match its sealed type",
        ) from error
    trace_id = cast(str, payload["trace_id"])
    if event.stream_id != trace_event_stream_id(trace_id):
        _fail(
            "TBM_TRACE_EVENT_INVALID",
            "TraceEvent stream does not match its trace identity",
        )
    if payload["sequence"] != event.stream_version:
        _fail(
            "TBM_TRACE_EVENT_SEQUENCE_INVALID",
            "TraceEvent payload sequence does not match stream version",
        )
    if payload["occurred_at"] != event.occurred_at:
        _fail(
            "TBM_TRACE_EVENT_TIMESTAMP_INVALID",
            "TraceEvent payload timestamp does not match the event envelope",
        )
    artifact_ids = tuple(cast(list[str], payload["artifact_ids"]))
    if artifact_ids != tuple(item.artifact_id for item in event.artifact_refs):
        _fail(
            "TBM_TRACE_EVENT_ARTIFACT_REFS_INVALID",
            "TraceEvent payload artifact IDs do not match event descriptors",
        )
    _validate_payload_semantics(event.event_type, cast(dict[str, object], payload))


def verify_trace_event_lineage(
    event: CanonicalEvent,
    parent_event: CanonicalEvent,
) -> None:
    verify_trace_event(event)
    verify_trace_event(parent_event)
    payload = cast(dict[str, object], _thaw_json(event.payload))
    parent_payload = cast(dict[str, object], _thaw_json(parent_event.payload))
    lineage = cast(dict[str, object], payload["lineage"])
    if (
        lineage["role"] != "subagent"
        or lineage["parent_trace_id"] != parent_payload["trace_id"]
        or lineage["parent_event_id"] != parent_event.event_id
        or event.causation_id != parent_event.event_id
        or payload["trace_id"] == parent_payload["trace_id"]
        or parent_event.event_type != TRACE_SUBAGENT_STARTED
        or parent_payload["related_subagent_id"] != lineage["subagent_id"]
        or event.organization_id != parent_event.organization_id
        or event.tenant_id != parent_event.tenant_id
        or event.repository_id != parent_event.repository_id
        or event.environment_id != parent_event.environment_id
        or parse_rfc3339(cast(str, payload["occurred_at"]))
        < parse_rfc3339(cast(str, parent_payload["occurred_at"]))
    ):
        _fail(
            "TBM_TRACE_EVENT_LINEAGE_INVALID",
            "subagent TraceEvent does not match its exact parent event and scope",
        )


def build_trace_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    for event_type in TRACE_EVENT_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="domain",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=_payload_json_schema(event_type),
            )
        )
    return registry.seal()


def trace_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_trace_event_registry().dispatch_schema()
    schema["$id"] = TRACE_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory ordered TraceEvent payloads v1"
    schema["$comment"] = (
        "Generated from the sealed TraceEvent tbm.event-registry.v1 catalog. "
        "The runtime registry and TraceEvent semantic verifier remain authoritative."
    )
    return schema


def dumps_trace_event_payload_dispatch_schema() -> str:
    return json.dumps(
        trace_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _payload_json_schema(event_type: str) -> dict[str, object]:
    tool_phase = _TOOL_EVENT_PHASES.get(event_type)
    permission_schema: dict[str, object]
    if event_type == TRACE_PERMISSION_RECORDED:
        permission_schema = _permission_schema()
    else:
        permission_schema = {"type": "null"}
    related_subagent_schema: dict[str, object]
    if event_type in _SUBAGENT_EVENT_TYPES:
        related_subagent_schema = _identifier_schema()
    else:
        related_subagent_schema = {"type": "null"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "protocol_version",
            "trace_id",
            "run_id",
            "sequence",
            "occurred_at",
            "artifact_ids",
            "tool",
            "permission_result",
            "lineage",
            "related_subagent_id",
        ],
        "properties": {
            "protocol_version": {"const": TRACE_EVENT_PROTOCOL_VERSION},
            "trace_id": _identifier_schema(),
            "run_id": _identifier_schema(),
            "sequence": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_SEQUENCE,
            },
            "occurred_at": {
                "type": "string",
                "pattern": _TIMESTAMP_PATTERN,
            },
            "artifact_ids": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": _ARTIFACT_ID_PATTERN,
                },
                "minItems": 0,
                "maxItems": EVENT_MAX_ARTIFACT_REFS,
                "uniqueItems": True,
            },
            "tool": (
                {"type": "null"}
                if tool_phase is None
                else _tool_schema(tool_phase)
            ),
            "permission_result": permission_schema,
            "lineage": _lineage_schema(),
            "related_subagent_id": related_subagent_schema,
        },
    }


def _tool_schema(phase: TraceToolPhase) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "tool_call_id",
            "tool_name",
            "phase",
            "invocation_sha256",
            "parent_tool_call_id",
        ],
        "properties": {
            "tool_call_id": _identifier_schema(),
            "tool_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": _CODE_MAX_CHARS,
            },
            "phase": {"const": phase},
            "invocation_sha256": {
                "type": "string",
                "pattern": _DIGEST_PATTERN,
            },
            "parent_tool_call_id": {
                "oneOf": [
                    {"type": "null"},
                    _identifier_schema(),
                ]
            },
        },
    }


def _permission_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "decision_id",
            "permission",
            "status",
            "reason_code",
            "decided_at",
            "request_sha256",
            "policy_sha256",
        ],
        "properties": {
            "decision_id": _identifier_schema(),
            "permission": {
                "type": "string",
                "minLength": 1,
                "maxLength": _CODE_MAX_CHARS,
            },
            "status": {"enum": ["allowed", "denied", "unknown"]},
            "reason_code": {
                "type": "string",
                "minLength": 1,
                "maxLength": _CODE_MAX_CHARS,
            },
            "decided_at": {
                "type": "string",
                "pattern": _TIMESTAMP_PATTERN,
            },
            "request_sha256": {
                "type": "string",
                "pattern": _DIGEST_PATTERN,
            },
            "policy_sha256": {
                "type": "string",
                "pattern": _DIGEST_PATTERN,
            },
        },
    }


def _lineage_schema() -> dict[str, object]:
    common_required = [
        "role",
        "subagent_id",
        "parent_trace_id",
        "parent_event_id",
    ]
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": common_required,
                "properties": {
                    "role": {"const": "root"},
                    "subagent_id": {"type": "null"},
                    "parent_trace_id": {"type": "null"},
                    "parent_event_id": {"type": "null"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": common_required,
                "properties": {
                    "role": {"const": "subagent"},
                    "subagent_id": _identifier_schema(),
                    "parent_trace_id": _identifier_schema(),
                    "parent_event_id": {
                        "type": "string",
                        "pattern": _EVENT_ID_PATTERN,
                    },
                },
            },
        ]
    }


def _identifier_schema() -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": _IDENTIFIER_MAX_CHARS,
    }


def _validate_payload_semantics(
    event_type: str,
    payload: dict[str, object],
) -> None:
    lineage_value = cast(dict[str, object], payload["lineage"])
    lineage = TraceEventLineage(
        role=cast(TraceLineageRole, lineage_value["role"]),
        subagent_id=cast(str | None, lineage_value["subagent_id"]),
        parent_trace_id=cast(str | None, lineage_value["parent_trace_id"]),
        parent_event_id=cast(str | None, lineage_value["parent_event_id"]),
    )
    tool_value = cast(dict[str, object] | None, payload["tool"])
    tool = (
        None
        if tool_value is None
        else TraceToolCorrelation(
            tool_call_id=cast(str, tool_value["tool_call_id"]),
            tool_name=cast(str, tool_value["tool_name"]),
            phase=cast(TraceToolPhase, tool_value["phase"]),
            invocation_sha256=cast(str, tool_value["invocation_sha256"]),
            parent_tool_call_id=cast(
                str | None, tool_value["parent_tool_call_id"]
            ),
        )
    )
    permission_value = cast(
        dict[str, object] | None, payload["permission_result"]
    )
    permission = (
        None
        if permission_value is None
        else TracePermissionResult(
            decision_id=cast(str, permission_value["decision_id"]),
            permission=cast(str, permission_value["permission"]),
            status=cast(TracePermissionStatus, permission_value["status"]),
            reason_code=cast(str, permission_value["reason_code"]),
            decided_at=cast(str, permission_value["decided_at"]),
            request_sha256=cast(str, permission_value["request_sha256"]),
            policy_sha256=cast(str, permission_value["policy_sha256"]),
        )
    )
    TraceEventDraft(
        event_type=event_type,
        trace_id=cast(str, payload["trace_id"]),
        run_id=cast(str, payload["run_id"]),
        sequence=cast(int, payload["sequence"]),
        occurred_at=cast(str, payload["occurred_at"]),
        artifact_refs=(),
        tool=tool,
        permission_result=permission,
        lineage=lineage,
        related_subagent_id=cast(str | None, payload["related_subagent_id"]),
    )


def _artifact_ref_tuple(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > EVENT_MAX_ARTIFACT_REFS
        or any(type(item) is not EventArtifactRef for item in value)
    ):
        _fail(
            "TBM_TRACE_EVENT_ARTIFACT_REFS_INVALID",
            "artifact_refs must be a bounded tuple of EventArtifactRef",
        )
    identifiers = tuple(item.artifact_id for item in value)
    if identifiers != tuple(sorted(identifiers)) or len(identifiers) != len(
        set(identifiers)
    ):
        _fail(
            "TBM_TRACE_EVENT_ARTIFACT_REFS_INVALID",
            "artifact_refs must be sorted and unique",
        )


def _canonical_timestamp(value: object, name: str) -> str:
    try:
        canonical = canonical_rfc3339(cast(str, value))
    except (TypeError, ValueError) as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_TIMESTAMP_INVALID",
            f"{name} must be a canonical RFC3339 timestamp",
        ) from error
    if type(value) is not str or value != canonical:
        _fail(
            "TBM_TRACE_EVENT_TIMESTAMP_INVALID",
            f"{name} must use canonical UTC RFC3339 form",
        )
    return canonical


def _sequence(value: object) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SEQUENCE:
        _fail(
            "TBM_TRACE_EVENT_SEQUENCE_INVALID",
            "TraceEvent sequence must be a bounded positive integer",
        )


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _IDENTIFIER_MAX_CHARS
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(
            "TBM_TRACE_EVENT_IDENTIFIER_INVALID",
            f"{name} must be a bounded identifier",
        )
    _utf8(value, name)


def _optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _event_id(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value.startswith("evt_")
        or len(value) <= 4
        or len(value) > 128
        or any(
            not (
                character.isascii()
                and (character.isalnum() or character in "._:-")
            )
            for character in value[4:]
        )
    ):
        _fail(
            "TBM_TRACE_EVENT_LINEAGE_INVALID",
            f"{name} must be a canonical event identifier",
        )


def _code(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _CODE_MAX_CHARS
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(
            "TBM_TRACE_EVENT_CODE_INVALID",
            f"{name} must be a bounded code",
        )
    _utf8(value, name)


def _digest(value: object, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        _fail(
            "TBM_TRACE_EVENT_DIGEST_INVALID",
            f"{name} must be a canonical SHA-256 digest",
        )


def _utf8(value: str, name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_TEXT_INVALID",
            f"{name} must be valid UTF-8 text",
        ) from error


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
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_CANONICALIZATION_FAILED",
            "TraceEvent value is not finite canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _fail(code: str, message: str) -> NoReturn:
    raise TraceEventV1Error(code, message)


__all__ = [
    "TRACE_DIFF_OBSERVED",
    "TRACE_EVENT_MAX_BATCH",
    "TRACE_EVENT_PAYLOAD_SCHEMA_ID",
    "TRACE_EVENT_PROTOCOL_VERSION",
    "TRACE_EVENT_STREAM_TYPE",
    "TRACE_EVENT_TYPES",
    "TRACE_FINAL_RESPONSE_RECORDED",
    "TRACE_PERMISSION_RECORDED",
    "TRACE_PRE_COMPACT",
    "TRACE_SESSION_ENDED",
    "TRACE_SESSION_STARTED",
    "TRACE_STOPPED",
    "TRACE_SUBAGENT_STARTED",
    "TRACE_SUBAGENT_STOPPED",
    "TRACE_TOOL_COMPLETED",
    "TRACE_TOOL_STARTED",
    "TRACE_USER_PROMPT_SUBMITTED",
    "TraceEventDraft",
    "TraceEventLineage",
    "TraceEventV1Error",
    "TraceLineageRole",
    "TracePermissionResult",
    "TracePermissionStatus",
    "TraceToolCorrelation",
    "TraceToolPhase",
    "append_trace_event_batch",
    "build_trace_event_append_request",
    "build_trace_event_batch",
    "build_trace_event_registry",
    "dumps_trace_event_payload_dispatch_schema",
    "trace_event_payload_dispatch_schema",
    "trace_event_stream_id",
    "trace_permission_result_from_authorization_decision",
    "verify_trace_event",
    "verify_trace_event_lineage",
]
