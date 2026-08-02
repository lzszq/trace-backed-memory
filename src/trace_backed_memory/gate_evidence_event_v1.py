from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, cast

from ._timestamps import RFC3339_PATTERN
from .contracts_v3 import V3ContractError
from .event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventTrustedContext,
    build_canonical_event,
)
from .gate_evaluation_v3 import (
    SystemGateEvaluation,
    dumps_system_gate_evaluation,
)
from .retrieval_v3 import RetrievalSnapshot, dumps_retrieval_snapshot


GATE_EVIDENCE_EVENT_CONTRACT_VERSION = "tbm.gate-evidence-event.v1"
GATE_EVIDENCE_EVENT_VERSION = 1
GATE_EVIDENCE_EVENT_STREAM_TYPE = "gate_evidence"
GATE_EVIDENCE_EVENT_PRODUCER = "trace_backed_memory"
GATE_EVIDENCE_EVENT_PRODUCER_VERSION = "0.1.0"
GATE_EVIDENCE_EVENT_RETENTION_POLICY_ID = "retention_engineering_memory"

RETRIEVAL_PREPARED_EVENT = "tbm.retrieval.prepared"
SYSTEM_GATE_EVALUATED_EVENT = "tbm.system_gate.evaluated"
GATE_EVIDENCE_EVENT_TYPES = tuple(
    sorted((RETRIEVAL_PREPARED_EVENT, SYSTEM_GATE_EVALUATED_EVENT))
)

GateEvidenceKind = Literal["retrieval_snapshot", "system_gate_evaluation"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class GateEvidenceEventV1Error(V3ContractError):
    """Stable failure for compact, artifact-linked Gate evidence events."""


@dataclass(frozen=True)
class GateEvidenceRecordRef:
    evidence_kind: GateEvidenceKind
    record_id: str
    session_id: str
    authorization_event_id: str
    retrieval_snapshot_id: str | None
    artifact_ref: EventArtifactRef
    occurred_at: str
    causation_event_id: str | None

    def __post_init__(self) -> None:
        if self.evidence_kind == "retrieval_snapshot":
            if (
                self.retrieval_snapshot_id is not None
                or self.causation_event_id is not None
            ):
                _invalid("retrieval evidence cannot name a parent or causation")
        elif self.evidence_kind == "system_gate_evaluation":
            if (
                self.retrieval_snapshot_id is None
                or self.causation_event_id is None
            ):
                _invalid("System Gate evidence must name retrieval causation")
        else:
            _invalid("evidence_kind is invalid")
        for name in ("record_id", "session_id", "authorization_event_id"):
            value = getattr(self, name)
            if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
                _invalid(f"{name} is invalid")
        for name in ("retrieval_snapshot_id", "causation_event_id"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not str
                or _IDENTIFIER_RE.fullmatch(value) is None
            ):
                _invalid(f"{name} is invalid")
        if type(self.artifact_ref) is not EventArtifactRef:
            _invalid("artifact_ref must be exactly EventArtifactRef")
        if type(self.occurred_at) is not str or not self.occurred_at:
            _invalid("occurred_at is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": GATE_EVIDENCE_EVENT_CONTRACT_VERSION,
            "evidence_kind": self.evidence_kind,
            "record_id": self.record_id,
            "session_id": self.session_id,
            "authorization_event_id": self.authorization_event_id,
            "retrieval_snapshot_id": self.retrieval_snapshot_id,
            "artifact_id": self.artifact_ref.artifact_id,
            "content_sha256": self.artifact_ref.content_sha256,
            "occurred_at": self.occurred_at,
            "causation_event_id": self.causation_event_id,
        }

    def to_projection_dict(self) -> dict[str, object]:
        return {
            **self.to_dict(),
            "artifact_ref": self.artifact_ref.to_dict(),
        }


def gate_evidence_event_payload_schema(event_type: str) -> dict[str, object]:
    if event_type not in GATE_EVIDENCE_EVENT_TYPES:
        _invalid("event_type is not a Gate evidence event")
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^\S(?:[\s\S]*\S)?$",
    }
    digest = {"type": "string", "pattern": r"sha256:[0-9a-f]{64}"}
    timestamp = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }
    properties: dict[str, object] = {
        "contract_version": {"const": GATE_EVIDENCE_EVENT_CONTRACT_VERSION},
        "evidence_kind": {
            "const": (
                "retrieval_snapshot"
                if event_type == RETRIEVAL_PREPARED_EVENT
                else "system_gate_evaluation"
            )
        },
        "record_id": identifier,
        "session_id": identifier,
        "authorization_event_id": identifier,
        "retrieval_snapshot_id": (
            {"const": None}
            if event_type == RETRIEVAL_PREPARED_EVENT
            else identifier
        ),
        "artifact_id": identifier,
        "content_sha256": digest,
        "occurred_at": timestamp,
        "causation_event_id": (
            {"const": None}
            if event_type == RETRIEVAL_PREPARED_EVENT
            else identifier
        ),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def retrieval_snapshot_record_ref(
    snapshot: RetrievalSnapshot,
) -> GateEvidenceRecordRef:
    if type(snapshot) is not RetrievalSnapshot:
        _invalid("snapshot must be exactly RetrievalSnapshot")
    artifact_ref = _artifact_ref(dumps_retrieval_snapshot(snapshot).encode("utf-8"))
    return GateEvidenceRecordRef(
        evidence_kind="retrieval_snapshot",
        record_id=snapshot.snapshot_id,
        session_id=snapshot.session_id,
        authorization_event_id=snapshot.authorization_event_id,
        retrieval_snapshot_id=None,
        artifact_ref=artifact_ref,
        occurred_at=snapshot.created_at,
        causation_event_id=None,
    )


def system_gate_evaluation_record_ref(
    evaluation: SystemGateEvaluation,
    *,
    causation_event_id: str,
) -> GateEvidenceRecordRef:
    if type(evaluation) is not SystemGateEvaluation:
        _invalid("evaluation must be exactly SystemGateEvaluation")
    if type(causation_event_id) is not str or not causation_event_id:
        _invalid("causation_event_id is required")
    artifact_ref = _artifact_ref(
        dumps_system_gate_evaluation(evaluation).encode("utf-8")
    )
    return GateEvidenceRecordRef(
        evidence_kind="system_gate_evaluation",
        record_id=evaluation.evaluation_id,
        session_id=evaluation.session_id,
        authorization_event_id=evaluation.authorization_event_id,
        retrieval_snapshot_id=evaluation.retrieval_snapshot_id,
        artifact_ref=artifact_ref,
        occurred_at=evaluation.evaluated_at,
        causation_event_id=causation_event_id,
    )


def build_retrieval_prepared_event(
    snapshot: RetrievalSnapshot,
    *,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    record_ref = retrieval_snapshot_record_ref(snapshot)
    return _build_event(
        RETRIEVAL_PREPARED_EVENT,
        record_ref,
        global_position=global_position,
        trusted_context=trusted_context,
    )


def build_system_gate_evaluated_event(
    evaluation: SystemGateEvaluation,
    *,
    retrieval_event: CanonicalEvent,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    retrieval_ref = parse_gate_evidence_event(retrieval_event)
    if (
        retrieval_event.event_type != RETRIEVAL_PREPARED_EVENT
        or retrieval_ref.record_id != evaluation.retrieval_snapshot_id
        or retrieval_ref.session_id != evaluation.session_id
        or retrieval_ref.authorization_event_id != evaluation.authorization_event_id
    ):
        _invalid("retrieval_event does not match the System Gate evaluation")
    record_ref = system_gate_evaluation_record_ref(
        evaluation,
        causation_event_id=retrieval_event.event_id,
    )
    return _build_event(
        SYSTEM_GATE_EVALUATED_EVENT,
        record_ref,
        global_position=global_position,
        trusted_context=trusted_context,
    )


def parse_gate_evidence_event(event: CanonicalEvent) -> GateEvidenceRecordRef:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if event.event_type not in GATE_EVIDENCE_EVENT_TYPES:
        _invalid("event is not a Gate evidence event")
    if (
        event.event_version != GATE_EVIDENCE_EVENT_VERSION
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != GATE_EVIDENCE_EVENT_STREAM_TYPE
        or event.stream_version != 1
        or event.previous_stream_event_sha256 is not None
        or event.payload_schema != f"{event.event_type}.v1"
        or event.classification != "internal"
        or event.retention_policy_id
        != GATE_EVIDENCE_EVENT_RETENTION_POLICY_ID
        or len(event.artifact_refs) != 1
    ):
        _invalid("Gate evidence event envelope is invalid")
    payload = _plain_mapping(event.payload)
    expected_fields = {
        "contract_version",
        "evidence_kind",
        "record_id",
        "session_id",
        "authorization_event_id",
        "retrieval_snapshot_id",
        "artifact_id",
        "content_sha256",
        "occurred_at",
        "causation_event_id",
    }
    if set(payload) != expected_fields:
        _invalid("Gate evidence event payload fields are invalid")
    expected_kind = (
        "retrieval_snapshot"
        if event.event_type == RETRIEVAL_PREPARED_EVENT
        else "system_gate_evaluation"
    )
    if (
        payload["contract_version"] != GATE_EVIDENCE_EVENT_CONTRACT_VERSION
        or payload["evidence_kind"] != expected_kind
        or payload["record_id"] != event.stream_id
        or payload["authorization_event_id"] != event.authorization_decision_id
        or payload["occurred_at"] != event.occurred_at
        or payload["causation_event_id"] != event.causation_id
    ):
        _invalid("Gate evidence event linkage is invalid")
    artifact_ref = event.artifact_refs[0]
    if (
        payload["artifact_id"] != artifact_ref.artifact_id
        or payload["content_sha256"] != artifact_ref.content_sha256
        or artifact_ref.media_type != "application/json"
        or artifact_ref.size_bytes <= 0
        or artifact_ref.classification != "internal"
        or artifact_ref.retention_policy_id
        != GATE_EVIDENCE_EVENT_RETENTION_POLICY_ID
        or artifact_ref.encryption_key_id is not None
        or artifact_ref.availability != "available"
    ):
        _invalid("Gate evidence event Artifact reference is invalid")
    record_ref = GateEvidenceRecordRef(
        evidence_kind=cast(GateEvidenceKind, payload["evidence_kind"]),
        record_id=cast(str, payload["record_id"]),
        session_id=cast(str, payload["session_id"]),
        authorization_event_id=cast(str, payload["authorization_event_id"]),
        retrieval_snapshot_id=cast(str | None, payload["retrieval_snapshot_id"]),
        artifact_ref=artifact_ref,
        occurred_at=cast(str, payload["occurred_at"]),
        causation_event_id=cast(str | None, payload["causation_event_id"]),
    )
    if (
        event.event_type == RETRIEVAL_PREPARED_EVENT
        and event.causation_id is not None
    ):
        _invalid("retrieval evidence event cannot have causation")
    if (
        event.event_type == SYSTEM_GATE_EVALUATED_EVENT
        and event.causation_id is None
    ):
        _invalid("System Gate evidence event requires causation")
    _verify_deterministic_envelope(event, record_ref)
    return record_ref


def _build_event(
    event_type: str,
    record_ref: GateEvidenceRecordRef,
    *,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if record_ref.authorization_event_id != trusted_context.authorization_decision_id:
        _invalid("trusted event authorization does not match Gate evidence")
    payload = record_ref.to_dict()
    identity_sha256 = _event_identity_sha256(event_type, record_ref.record_id)
    request_sha256 = _event_command_sha256(
        event_type,
        payload,
        trusted_context.authorization_decision_id,
    )
    event = build_canonical_event(
        event_id=gate_evidence_event_id(event_type, record_ref.record_id),
        event_type=event_type,
        event_version=GATE_EVIDENCE_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=record_ref.record_id,
        stream_type=GATE_EVIDENCE_EVENT_STREAM_TYPE,
        stream_version=1,
        global_position=global_position,
        trusted_context=trusted_context,
        request_id=(
            "evidence_request_" + identity_sha256.removeprefix("sha256:")[:48]
        ),
        idempotency_key_sha256=identity_sha256,
        request_sha256=request_sha256,
        correlation_id=_correlation_id(record_ref.session_id),
        causation_id=record_ref.causation_event_id,
        occurred_at=record_ref.occurred_at,
        recorded_at=record_ref.occurred_at,
        producer=GATE_EVIDENCE_EVENT_PRODUCER,
        producer_version=GATE_EVIDENCE_EVENT_PRODUCER_VERSION,
        payload_schema=f"{event_type}.v1",
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id=GATE_EVIDENCE_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(record_ref.artifact_ref,),
        payload=payload,
    )
    parse_gate_evidence_event(event)
    return event


def _verify_deterministic_envelope(
    event: CanonicalEvent,
    record_ref: GateEvidenceRecordRef,
) -> None:
    identity_sha256 = _event_identity_sha256(event.event_type, record_ref.record_id)
    if (
        event.event_id
        != gate_evidence_event_id(event.event_type, record_ref.record_id)
        or event.idempotency_key_sha256 != identity_sha256
        or event.request_id
        != "evidence_request_" + identity_sha256.removeprefix("sha256:")[:48]
        or event.request_sha256
        != _event_command_sha256(
            event.event_type,
            record_ref.to_dict(),
            record_ref.authorization_event_id,
        )
        or event.correlation_id != _correlation_id(record_ref.session_id)
        or event.recorded_at != record_ref.occurred_at
        or event.producer != GATE_EVIDENCE_EVENT_PRODUCER
        or event.producer_version != GATE_EVIDENCE_EVENT_PRODUCER_VERSION
    ):
        _invalid("Gate evidence event deterministic envelope is invalid")


def _artifact_ref(content: bytes) -> EventArtifactRef:
    content_sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + content_sha256.removeprefix("sha256:"),
        content_sha256=content_sha256,
        media_type="application/json",
        size_bytes=len(content),
        classification="internal",
        retention_policy_id=GATE_EVIDENCE_EVENT_RETENTION_POLICY_ID,
        encryption_key_id=None,
        availability="available",
    )


def _event_identity_sha256(event_type: str, record_id: str) -> str:
    return _domain_sha256(
        b"tbm.gate-evidence-event-identity.v1\x00",
        {"event_type": event_type, "record_id": record_id},
    )


def gate_evidence_event_id(event_type: str, record_id: str) -> str:
    if event_type not in GATE_EVIDENCE_EVENT_TYPES:
        _invalid("event_type is not a Gate evidence event")
    if type(record_id) is not str or _IDENTIFIER_RE.fullmatch(record_id) is None:
        _invalid("record_id is invalid")
    identity_sha256 = _event_identity_sha256(event_type, record_id)
    return "evt_evidence_" + identity_sha256.removeprefix("sha256:")


def _event_command_sha256(
    event_type: str,
    payload: Mapping[str, object],
    authorization_event_id: str,
) -> str:
    return _domain_sha256(
        b"tbm.gate-evidence-event-command.v1\x00",
        {
            "event_type": event_type,
            "payload": payload,
            "authorization_event_id": authorization_event_id,
        },
    )


def _correlation_id(session_id: str) -> str:
    digest = _domain_sha256(
        b"tbm.gate-evidence-event-correlation.v1\x00",
        {"session_id": session_id},
    )
    return "gate_evidence_correlation_" + digest.removeprefix("sha256:")[:40]


def _plain_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid("Gate evidence event payload is invalid")
    return {str(key): _plain_json(item) for key, item in value.items()}


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    return value


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
    raise GateEvidenceEventV1Error(
        "TBM_GATE_EVIDENCE_EVENT_INVALID",
        message,
    )


__all__ = [
    "GATE_EVIDENCE_EVENT_CONTRACT_VERSION",
    "GATE_EVIDENCE_EVENT_RETENTION_POLICY_ID",
    "GATE_EVIDENCE_EVENT_STREAM_TYPE",
    "GATE_EVIDENCE_EVENT_TYPES",
    "GATE_EVIDENCE_EVENT_VERSION",
    "GateEvidenceEventV1Error",
    "GateEvidenceRecordRef",
    "RETRIEVAL_PREPARED_EVENT",
    "SYSTEM_GATE_EVALUATED_EVENT",
    "build_retrieval_prepared_event",
    "build_system_gate_evaluated_event",
    "gate_evidence_event_payload_schema",
    "gate_evidence_event_id",
    "parse_gate_evidence_event",
    "retrieval_snapshot_record_ref",
    "system_gate_evaluation_record_ref",
]
