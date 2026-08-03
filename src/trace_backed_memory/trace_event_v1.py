from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, cast

from ._timestamps import RFC3339_PATTERN, canonical_rfc3339
from .contracts_v3 import V3ContractError
from .event_v1 import (
    EVENT_MAX_ARTIFACT_REFS,
    EVENT_MAX_VERSION,
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    EventSource,
    EventTrustedContext,
    EventV1ContractError,
    build_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerAtomicAppendPort,
    LedgerAppendCommit,
    LedgerAccessContext,
    LedgerIdempotency,
)


TRACE_EVENT_CONTRACT_VERSION = "tbm.trace-event.v1"
TRACE_EVENT_VERSION = 1
TRACE_EVENT_RECORDED = "tbm.trace.event_recorded"
TRACE_EVENT_TYPES = (TRACE_EVENT_RECORDED,)
TRACE_EVENT_STREAM_TYPE = "trace_event"
TRACE_EVENT_PRODUCER = "trace_backed_memory"
TRACE_EVENT_PRODUCER_VERSION = "0.1.0"
TRACE_EVENT_MAX_SEQUENCE = 1_000_000_000

TracePermissionResult = Literal[
    "not_applicable",
    "allowed",
    "denied",
    "pending",
    "unknown",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$")
_PERMISSION_RESULTS = frozenset(
    {"not_applicable", "allowed", "denied", "pending", "unknown"}
)


class TraceEventV1Error(V3ContractError):
    """Stable failure for ordered, artifact-linked Trace events."""


@dataclass(frozen=True)
class TraceEventRecordRef:
    trace_id: str
    run_id: str
    sequence: int
    trace_event_type: str
    occurred_at: str
    authorization_event_id: str
    source: EventSource
    artifact_refs: tuple[EventArtifactRef, ...]
    classification: EventClassification
    retention_policy_id: str
    tool_correlation_id: str | None = None
    permission_result: TracePermissionResult = "not_applicable"
    parent_trace_id: str | None = None
    subagent_id: str | None = None
    causation_event_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "trace_id",
            "run_id",
            "trace_event_type",
            "authorization_event_id",
            "retention_policy_id",
        ):
            _identifier(getattr(self, name), name)
        for name in (
            "tool_correlation_id",
            "parent_trace_id",
            "subagent_id",
        ):
            _optional_identifier(getattr(self, name), name)
        if self.parent_trace_id == self.trace_id:
            _invalid("parent_trace_id cannot equal trace_id")
        if type(self.causation_event_id) is not str and (
            self.causation_event_id is not None
        ):
            _invalid("causation_event_id is invalid")
        if self.causation_event_id is not None and (
            _EVENT_ID_RE.fullmatch(self.causation_event_id) is None
        ):
            _invalid("causation_event_id is invalid")
        if (
            type(self.sequence) is not int
            or not 1 <= self.sequence <= TRACE_EVENT_MAX_SEQUENCE
        ):
            _invalid("sequence must be a bounded positive integer")
        if type(self.source) is not EventSource:
            _invalid("source must be exactly EventSource")
        canonical_occurred_at = _canonical_timestamp(
            self.occurred_at,
            "occurred_at",
        )
        canonical_observed_at = _canonical_timestamp(
            self.source.observed_at,
            "source.observed_at",
        )
        if self.occurred_at != canonical_occurred_at:
            _invalid("occurred_at must use canonical UTC RFC 3339")
        if self.source.observed_at != canonical_observed_at:
            _invalid("source observed_at must use canonical UTC RFC 3339")
        if (
            type(self.artifact_refs) is not tuple
            or len(self.artifact_refs) > EVENT_MAX_ARTIFACT_REFS
            or any(type(item) is not EventArtifactRef for item in self.artifact_refs)
        ):
            _invalid("artifact_refs must be a bounded tuple of EventArtifactRef")
        artifact_refs = tuple(
            sorted(self.artifact_refs, key=lambda item: item.artifact_id)
        )
        if len({item.artifact_id for item in artifact_refs}) != len(artifact_refs):
            _invalid("artifact_refs must be unique")
        object.__setattr__(self, "artifact_refs", artifact_refs)
        if type(self.classification) is not str or self.classification not in {
            "public",
            "internal",
            "confidential",
            "restricted",
        }:
            _invalid("classification is invalid")
        if (
            type(self.permission_result) is not str
            or self.permission_result not in _PERMISSION_RESULTS
        ):
            _invalid("permission_result is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": TRACE_EVENT_CONTRACT_VERSION,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "trace_event_type": self.trace_event_type,
            "occurred_at": self.occurred_at,
            "authorization_event_id": self.authorization_event_id,
            "artifact_ids": [item.artifact_id for item in self.artifact_refs],
            "tool_correlation_id": self.tool_correlation_id,
            "permission_result": self.permission_result,
            "parent_trace_id": self.parent_trace_id,
            "subagent_id": self.subagent_id,
            "causation_event_id": self.causation_event_id,
        }

    def to_projection_dict(self) -> dict[str, object]:
        return {
            **self.to_dict(),
            "source": self.source.to_dict(),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
        }


def trace_event_payload_schema(event_type: str) -> dict[str, object]:
    if event_type not in TRACE_EVENT_TYPES:
        _invalid("event_type is not a Trace event")
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    }
    optional_identifier = {
        "oneOf": [identifier, {"type": "null"}],
    }
    optional_event_id = {
        "oneOf": [
            {
                "type": "string",
                "minLength": 5,
                "maxLength": 128,
                "pattern": r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$",
            },
            {"type": "null"},
        ],
    }
    timestamp = {
        "type": "string",
        "minLength": 20,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }
    properties: dict[str, object] = {
        "contract_version": {"const": TRACE_EVENT_CONTRACT_VERSION},
        "trace_id": identifier,
        "run_id": identifier,
        "sequence": {
            "type": "integer",
            "minimum": 1,
            "maximum": TRACE_EVENT_MAX_SEQUENCE,
        },
        "trace_event_type": identifier,
        "occurred_at": timestamp,
        "authorization_event_id": identifier,
        "artifact_ids": {
            "type": "array",
            "maxItems": EVENT_MAX_ARTIFACT_REFS,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "pattern": r"^artifact_sha256_[0-9a-f]{64}$",
            },
        },
        "tool_correlation_id": optional_identifier,
        "permission_result": {
            "enum": sorted(_PERMISSION_RESULTS),
        },
        "parent_trace_id": optional_identifier,
        "subagent_id": optional_identifier,
        "causation_event_id": optional_event_id,
        "batch_first_sequence": {
            "type": "integer",
            "minimum": 1,
            "maximum": TRACE_EVENT_MAX_SEQUENCE,
        },
        "batch_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": EVENT_LEDGER_MAX_APPEND_BATCH,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def trace_event_id(
    trace_id: str,
    sequence: int,
    trusted_context: EventTrustedContext,
) -> str:
    _identifier(trace_id, "trace_id")
    if type(sequence) is not int or not 1 <= sequence <= TRACE_EVENT_MAX_SEQUENCE:
        _invalid("sequence must be a bounded positive integer")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    identity_sha256 = _event_identity_sha256(
        trace_id,
        sequence,
        trusted_context,
    )
    return "evt_trace_" + identity_sha256.removeprefix("sha256:")


def build_trace_event(
    reference: TraceEventRecordRef,
    *,
    parent_event: CanonicalEvent | None,
    global_position: int,
    trusted_context: EventTrustedContext,
    recorded_at: str,
) -> CanonicalEvent:
    return build_trace_event_batch(
        (reference,),
        parent_event=parent_event,
        first_global_position=global_position,
        trusted_context=trusted_context,
        recorded_at=recorded_at,
    )[0]


def build_trace_event_batch(
    references: tuple[TraceEventRecordRef, ...],
    *,
    parent_event: CanonicalEvent | None,
    first_global_position: int,
    trusted_context: EventTrustedContext,
    recorded_at: str,
) -> tuple[CanonicalEvent, ...]:
    if (
        type(references) is not tuple
        or not 1 <= len(references) <= EVENT_LEDGER_MAX_APPEND_BATCH
        or any(type(item) is not TraceEventRecordRef for item in references)
    ):
        _invalid("references must be a bounded non-empty tuple of TraceEventRecordRef")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if (
        type(first_global_position) is not int
        or not 1 <= first_global_position <= EVENT_MAX_VERSION
        or first_global_position + len(references) - 1 > EVENT_MAX_VERSION
    ):
        _invalid("first_global_position does not fit the Trace event batch")
    canonical_recorded_at = _canonical_timestamp(recorded_at, "recorded_at")
    if recorded_at != canonical_recorded_at:
        _invalid("recorded_at must use canonical UTC RFC 3339")
    first_reference = references[0]
    if any(
        reference.trace_id != first_reference.trace_id
        or reference.run_id != first_reference.run_id
        or reference.sequence != first_reference.sequence + offset
        or reference.authorization_event_id != trusted_context.authorization_decision_id
        for offset, reference in enumerate(references)
    ):
        _invalid("Trace event batch identity or sequence is invalid")
    batch_identity_sha256 = _batch_identity_sha256(
        first_reference.trace_id,
        first_reference.sequence,
        len(references),
        trusted_context,
    )
    command_sha256 = _batch_command_sha256(
        references,
        recorded_at=canonical_recorded_at,
        trusted_context=trusted_context,
    )
    events: list[CanonicalEvent] = []
    current_parent = parent_event
    for offset, reference in enumerate(references):
        event = _build_trace_event(
            reference,
            parent_event=current_parent,
            global_position=first_global_position + offset,
            trusted_context=trusted_context,
            recorded_at=canonical_recorded_at,
            batch_first_sequence=first_reference.sequence,
            batch_size=len(references),
            batch_identity_sha256=batch_identity_sha256,
            command_sha256=command_sha256,
        )
        events.append(event)
        current_parent = event
    result = tuple(events)
    verify_trace_event_batch(result, parent_event=parent_event)
    return result


def parse_trace_event(event: CanonicalEvent) -> TraceEventRecordRef:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if (
        event.event_type != TRACE_EVENT_RECORDED
        or event.event_version != TRACE_EVENT_VERSION
        or event.event_kind != "observation"
        or event.origin != "native"
        or event.source is None
        or event.stream_type != TRACE_EVENT_STREAM_TYPE
        or event.payload_schema != f"{TRACE_EVENT_RECORDED}.v1"
        or event.occurred_at is None
    ):
        _invalid("Trace event envelope is invalid")
    payload = _plain_mapping(event.payload)
    expected_fields = {
        "contract_version",
        "trace_id",
        "run_id",
        "sequence",
        "trace_event_type",
        "occurred_at",
        "authorization_event_id",
        "artifact_ids",
        "tool_correlation_id",
        "permission_result",
        "parent_trace_id",
        "subagent_id",
        "causation_event_id",
        "batch_first_sequence",
        "batch_size",
    }
    if set(payload) != expected_fields:
        _invalid("Trace event payload fields are invalid")
    if (
        payload["contract_version"] != TRACE_EVENT_CONTRACT_VERSION
        or payload["trace_id"] != event.stream_id
        or payload["sequence"] != event.stream_version
        or payload["occurred_at"] != event.occurred_at
        or payload["authorization_event_id"] != event.authorization_decision_id
        or payload["causation_event_id"] != event.causation_id
        or payload["artifact_ids"] != [item.artifact_id for item in event.artifact_refs]
    ):
        _invalid("Trace event linkage is invalid")
    reference = TraceEventRecordRef(
        trace_id=cast(str, payload["trace_id"]),
        run_id=cast(str, payload["run_id"]),
        sequence=cast(int, payload["sequence"]),
        trace_event_type=cast(str, payload["trace_event_type"]),
        occurred_at=cast(str, payload["occurred_at"]),
        authorization_event_id=cast(str, payload["authorization_event_id"]),
        source=event.source,
        artifact_refs=event.artifact_refs,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        tool_correlation_id=cast(str | None, payload["tool_correlation_id"]),
        permission_result=cast(
            TracePermissionResult,
            payload["permission_result"],
        ),
        parent_trace_id=cast(str | None, payload["parent_trace_id"]),
        subagent_id=cast(str | None, payload["subagent_id"]),
        causation_event_id=cast(str | None, payload["causation_event_id"]),
    )
    _verify_deterministic_envelope(event, reference)
    _first_sequence, batch_size = _batch_descriptor(
        payload,
        reference.sequence,
    )
    if batch_size == 1 and event.request_sha256 != _batch_command_sha256(
        (reference,),
        recorded_at=event.recorded_at,
        trusted_context=_trusted_context_from_event(event),
    ):
        _invalid("Trace event singleton command digest is invalid")
    return reference


def verify_trace_event_parent(
    event: CanonicalEvent,
    parent_event: CanonicalEvent | None,
) -> None:
    reference = parse_trace_event(event)
    if reference.sequence == 1:
        if parent_event is not None:
            _invalid("the first Trace event cannot name a parent")
        try:
            verify_event_parent(event, None)
        except EventV1ContractError as error:
            raise TraceEventV1Error(
                "TBM_TRACE_EVENT_INVALID",
                "Trace event parent envelope is invalid",
            ) from error
        return
    if type(parent_event) is not CanonicalEvent:
        _invalid("non-first Trace event requires its parent")
    parent_reference = parse_trace_event(parent_event)
    if (
        reference.trace_id != parent_reference.trace_id
        or reference.run_id != parent_reference.run_id
        or reference.sequence != parent_reference.sequence + 1
    ):
        _invalid("Trace event parent identity is invalid")
    try:
        verify_event_parent(event, parent_event)
    except EventV1ContractError as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_INVALID",
            "Trace event parent envelope is invalid",
        ) from error


def verify_trace_event_batch(
    events: tuple[CanonicalEvent, ...],
    *,
    parent_event: CanonicalEvent | None,
) -> None:
    if (
        type(events) is not tuple
        or not 1 <= len(events) <= EVENT_LEDGER_MAX_APPEND_BATCH
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _invalid("events must be a bounded non-empty tuple of CanonicalEvent")
    references = tuple(parse_trace_event(event) for event in events)
    first_sequence = references[0].sequence
    descriptors = tuple(
        _batch_descriptor(_plain_mapping(event.payload), reference.sequence)
        for event, reference in zip(events, references, strict=True)
    )
    expected_descriptor = (first_sequence, len(events))
    if any(descriptor != expected_descriptor for descriptor in descriptors):
        _invalid("Trace event batch descriptor is inconsistent")
    first_event = events[0]
    trusted_context = _trusted_context_from_event(first_event)
    expected_identity = _batch_identity_sha256(
        references[0].trace_id,
        first_sequence,
        len(events),
        trusted_context,
    )
    expected_command = _batch_command_sha256(
        references,
        recorded_at=first_event.recorded_at,
        trusted_context=trusted_context,
    )
    if any(
        reference.trace_id != references[0].trace_id
        or reference.run_id != references[0].run_id
        or reference.sequence != first_sequence + offset
        or event.global_position != first_event.global_position + offset
        or event.recorded_at != first_event.recorded_at
        or event.idempotency_key_sha256 != expected_identity
        or event.request_sha256 != expected_command
        or event.request_id
        != "trace_event_request_" + expected_identity.removeprefix("sha256:")[:40]
        or _trusted_context_from_event(event) != trusted_context
        for offset, (event, reference) in enumerate(
            zip(events, references, strict=True)
        )
    ):
        _invalid("Trace event batch command is invalid")
    current_parent = parent_event
    for event in events:
        verify_trace_event_parent(event, current_parent)
        current_parent = event


def append_trace_event_batch(
    ledger: EventLedgerAtomicAppendPort,
    events: tuple[CanonicalEvent, ...],
    *,
    parent_event: CanonicalEvent | None,
) -> LedgerAppendCommit:
    try:
        access_context = ledger.access_context
        append_once = ledger.append_once
    except Exception as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_INVALID",
            "ledger must support trusted atomic append",
        ) from error
    if type(access_context) is not LedgerAccessContext or not callable(append_once):
        _invalid("ledger must support trusted atomic append")
    verify_trace_event_batch(events, parent_event=parent_event)
    if access_context.event_trusted_context() != _trusted_context_from_event(events[0]):
        _invalid("ledger trusted context does not match the Trace event batch")
    expected_version = 0 if parent_event is None else parent_event.stream_version
    return append_once(
        events[0].stream_id,
        expected_version,
        events,
        LedgerIdempotency(
            events[0].idempotency_key_sha256,
            events[0].request_sha256,
        ),
    )


def _build_trace_event(
    reference: TraceEventRecordRef,
    *,
    parent_event: CanonicalEvent | None,
    global_position: int,
    trusted_context: EventTrustedContext,
    recorded_at: str,
    batch_first_sequence: int,
    batch_size: int,
    batch_identity_sha256: str,
    command_sha256: str,
) -> CanonicalEvent:
    if parent_event is None:
        if reference.sequence != 1:
            _invalid("the first Trace event sequence must be one")
        previous_sha256 = None
    else:
        parent_reference = parse_trace_event(parent_event)
        if (
            reference.trace_id != parent_reference.trace_id
            or reference.run_id != parent_reference.run_id
            or reference.sequence != parent_reference.sequence + 1
        ):
            _invalid("Trace event does not continue its parent stream")
        previous_sha256 = parent_event.event_sha256
    payload = {
        **reference.to_dict(),
        "batch_first_sequence": batch_first_sequence,
        "batch_size": batch_size,
    }
    try:
        return build_canonical_event(
            event_id=trace_event_id(
                reference.trace_id,
                reference.sequence,
                trusted_context,
            ),
            event_type=TRACE_EVENT_RECORDED,
            event_version=TRACE_EVENT_VERSION,
            event_kind="observation",
            origin="native",
            source=reference.source,
            stream_id=reference.trace_id,
            stream_type=TRACE_EVENT_STREAM_TYPE,
            stream_version=reference.sequence,
            global_position=global_position,
            trusted_context=trusted_context,
            request_id=(
                "trace_event_request_"
                + batch_identity_sha256.removeprefix("sha256:")[:40]
            ),
            idempotency_key_sha256=batch_identity_sha256,
            request_sha256=command_sha256,
            correlation_id=_correlation_id(reference.trace_id, reference.run_id),
            causation_id=reference.causation_event_id,
            occurred_at=reference.occurred_at,
            recorded_at=recorded_at,
            producer=TRACE_EVENT_PRODUCER,
            producer_version=TRACE_EVENT_PRODUCER_VERSION,
            payload_schema=f"{TRACE_EVENT_RECORDED}.v1",
            previous_stream_event_sha256=previous_sha256,
            classification=reference.classification,
            retention_policy_id=reference.retention_policy_id,
            artifact_refs=reference.artifact_refs,
            payload=payload,
        )
    except EventV1ContractError as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_INVALID",
            "Trace event envelope is invalid",
        ) from error


def _verify_deterministic_envelope(
    event: CanonicalEvent,
    reference: TraceEventRecordRef,
) -> None:
    payload = _plain_mapping(event.payload)
    batch_first_sequence, batch_size = _batch_descriptor(
        payload,
        reference.sequence,
    )
    batch_identity_sha256 = _batch_identity_sha256(
        reference.trace_id,
        batch_first_sequence,
        batch_size,
        _trusted_context_from_event(event),
    )
    if (
        event.event_id
        != trace_event_id(
            reference.trace_id,
            reference.sequence,
            _trusted_context_from_event(event),
        )
        or event.idempotency_key_sha256 != batch_identity_sha256
        or event.request_id
        != "trace_event_request_" + batch_identity_sha256.removeprefix("sha256:")[:40]
        or event.correlation_id != _correlation_id(reference.trace_id, reference.run_id)
        or event.producer != TRACE_EVENT_PRODUCER
        or event.producer_version != TRACE_EVENT_PRODUCER_VERSION
    ):
        _invalid("Trace event deterministic envelope is invalid")


def _event_identity_sha256(
    trace_id: str,
    sequence: int,
    trusted_context: EventTrustedContext,
) -> str:
    return _domain_sha256(
        b"tbm.trace-event-identity.v1\x00",
        {
            **_partition_dict(trusted_context),
            "trace_id": trace_id,
            "sequence": sequence,
        },
    )


def _batch_identity_sha256(
    trace_id: str,
    first_sequence: int,
    batch_size: int,
    trusted_context: EventTrustedContext,
) -> str:
    return _domain_sha256(
        b"tbm.trace-event-batch-identity.v1\x00",
        {
            **_partition_dict(trusted_context),
            "trace_id": trace_id,
            "first_sequence": first_sequence,
            "batch_size": batch_size,
        },
    )


def _batch_command_sha256(
    references: tuple[TraceEventRecordRef, ...],
    *,
    recorded_at: str,
    trusted_context: EventTrustedContext,
) -> str:
    return _domain_sha256(
        b"tbm.trace-event-batch-command.v1\x00",
        {
            "records": [reference.to_projection_dict() for reference in references],
            "recorded_at": recorded_at,
            "trusted_context": {
                "organization_id": trusted_context.organization_id,
                "tenant_id": trusted_context.tenant_id,
                "repository_id": trusted_context.repository_id,
                "environment_id": trusted_context.environment_id,
                "principal_id": trusted_context.principal_id,
                "agent_client_id": trusted_context.agent_client_id,
                "actor_type": trusted_context.actor_type,
                "actor_id": trusted_context.actor_id,
                "authorization_decision_id": (
                    trusted_context.authorization_decision_id
                ),
            },
        },
    )


def _batch_descriptor(
    payload: Mapping[str, object],
    sequence: int,
) -> tuple[int, int]:
    first_sequence = payload.get("batch_first_sequence")
    batch_size = payload.get("batch_size")
    if (
        type(first_sequence) is not int
        or type(batch_size) is not int
        or not 1 <= first_sequence <= TRACE_EVENT_MAX_SEQUENCE
        or not 1 <= batch_size <= EVENT_LEDGER_MAX_APPEND_BATCH
        or not first_sequence <= sequence < first_sequence + batch_size
        or first_sequence + batch_size - 1 > TRACE_EVENT_MAX_SEQUENCE
    ):
        _invalid("Trace event batch descriptor is invalid")
    return first_sequence, batch_size


def _trusted_context_from_event(event: CanonicalEvent) -> EventTrustedContext:
    return EventTrustedContext(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
        principal_id=event.principal_id,
        agent_client_id=event.agent_client_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        authorization_decision_id=event.authorization_decision_id,
    )


def _partition_dict(
    trusted_context: EventTrustedContext,
) -> dict[str, object]:
    return {
        "organization_id": trusted_context.organization_id,
        "tenant_id": trusted_context.tenant_id,
        "repository_id": trusted_context.repository_id,
        "environment_id": trusted_context.environment_id,
    }


def _correlation_id(trace_id: str, run_id: str) -> str:
    digest = _domain_sha256(
        b"tbm.trace-event-correlation.v1\x00",
        {"trace_id": trace_id, "run_id": run_id},
    )
    return "trace_event_correlation_" + digest.removeprefix("sha256:")[:40]


def _plain_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid("Trace event payload is invalid")
    return {str(key): _plain_json(item) for key, item in value.items()}


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    return value


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _canonical_timestamp(value: object, name: str) -> str:
    try:
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise TraceEventV1Error(
            "TBM_TRACE_EVENT_INVALID",
            f"{name} is invalid",
        ) from error


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _invalid(message: str) -> NoReturn:
    raise TraceEventV1Error(
        "TBM_TRACE_EVENT_INVALID",
        message,
    )


__all__ = [
    "TRACE_EVENT_CONTRACT_VERSION",
    "TRACE_EVENT_MAX_SEQUENCE",
    "TRACE_EVENT_RECORDED",
    "TRACE_EVENT_STREAM_TYPE",
    "TRACE_EVENT_TYPES",
    "TRACE_EVENT_VERSION",
    "TraceEventRecordRef",
    "TraceEventV1Error",
    "TracePermissionResult",
    "append_trace_event_batch",
    "build_trace_event",
    "build_trace_event_batch",
    "parse_trace_event",
    "trace_event_id",
    "trace_event_payload_schema",
    "verify_trace_event_parent",
    "verify_trace_event_batch",
]
