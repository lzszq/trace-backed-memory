from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, cast

from ._timestamps import RFC3339_PATTERN, parse_rfc3339
from .contracts_v3 import V3ContractError
from .event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventTrustedContext,
    build_canonical_event,
)
from .gate_evaluation_v3 import SemanticGateAttempt, dumps_semantic_gate_attempt
from .gate_evidence_event_v1 import (
    SYSTEM_GATE_EVALUATED_EVENT,
    gate_evidence_event_id,
)
from .replay_v3 import ContentAddressedArtifact
from .semantic_gate_artifact_v3 import (
    SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
    StoredSemanticGateArtifact,
    StoredSemanticGateAttemptArtifacts,
)


SEMANTIC_GATE_ATTEMPT_EVENT_CONTRACT_VERSION = (
    "tbm.semantic-gate-attempt-event.v1"
)
SEMANTIC_GATE_ATTEMPT_EVENT_VERSION = 1
SEMANTIC_GATE_ATTEMPT_EVENT_STREAM_TYPE = "semantic_gate_attempt_chain"
SEMANTIC_GATE_ATTEMPT_EVENT_PRODUCER = "trace_backed_memory"
SEMANTIC_GATE_ATTEMPT_EVENT_PRODUCER_VERSION = "0.1.0"
SEMANTIC_GATE_ATTEMPT_EVENT_RETENTION_POLICY_ID = (
    "retention_engineering_memory"
)

SEMANTIC_GATE_ATTEMPT_FAILED_EVENT = "tbm.semantic_gate.attempt_failed"
SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT = "tbm.semantic_gate.attempt_succeeded"
SEMANTIC_GATE_ATTEMPT_EVENT_TYPES = tuple(
    sorted(
        (
            SEMANTIC_GATE_ATTEMPT_FAILED_EVENT,
            SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
        )
    )
)

SemanticGateAttemptEventStatus = Literal["failed", "succeeded"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTEMPT_ID_RE = re.compile(r"^semantic_attempt_sha256_[0-9a-f]{64}$")
_SYSTEM_GATE_ID_RE = re.compile(r"^system_gate_sha256_[0-9a-f]{64}$")
_STREAM_ID_RE = re.compile(r"^semantic_gate_stream_sha256_[0-9a-f]{64}$")
_SNAPSHOT_ID_RE = re.compile(r"^retrieval_snapshot_sha256_[0-9a-f]{64}$")
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")


class SemanticGateAttemptEventV1Error(V3ContractError):
    """Stable failure for compact, artifact-linked Semantic Gate events."""


@dataclass(frozen=True)
class SemanticGateAttemptEventRef:
    attempt_id: str
    stream_id: str
    session_id: str
    retrieval_snapshot_id: str
    system_gate_evaluation_id: str
    sequence: int
    previous_attempt_id: str | None
    provider_id: str
    model_id: str
    model_version: str
    endpoint_id: str | None
    prompt_template_id: str
    prompt_template_version: str
    prompt_artifact_sha256: str
    response_artifact_sha256: str | None
    generation_config_sha256: str
    provider_request_id: str | None
    status: SemanticGateAttemptEventStatus
    decision_id: str | None
    final_allowed_revision_ids: tuple[str, ...]
    final_blocked_revision_ids: tuple[str, ...]
    risk: str | None
    recommended_injection: str | None
    error_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    started_at: str
    finished_at: str
    attempt_artifact_ref: EventArtifactRef
    prompt_artifact_ref: EventArtifactRef
    response_artifact_ref: EventArtifactRef | None
    causation_event_id: str

    def __post_init__(self) -> None:
        if (
            type(self.attempt_id) is not str
            or _ATTEMPT_ID_RE.fullmatch(self.attempt_id) is None
        ):
            _invalid("attempt_id is invalid")
        expected_stream_id = semantic_gate_attempt_stream_id(
            self.system_gate_evaluation_id
        )
        if self.stream_id != expected_stream_id:
            _invalid("stream_id does not match the System Gate evaluation")
        for name in (
            "session_id",
            "provider_id",
            "model_id",
            "model_version",
            "prompt_template_id",
            "prompt_template_version",
        ):
            _identifier(getattr(self, name), name)
        if (
            type(self.retrieval_snapshot_id) is not str
            or _SNAPSHOT_ID_RE.fullmatch(self.retrieval_snapshot_id) is None
        ):
            _invalid("retrieval_snapshot_id is invalid")
        for name in ("endpoint_id", "provider_request_id", "decision_id"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)
        if type(self.sequence) is not int or not 1 <= self.sequence <= 4096:
            _invalid("sequence is invalid")
        if self.sequence == 1:
            if self.previous_attempt_id is not None:
                _invalid("first attempt cannot name a previous attempt")
            expected_causation = gate_evidence_event_id(
                SYSTEM_GATE_EVALUATED_EVENT,
                self.system_gate_evaluation_id,
            )
        else:
            if (
                type(self.previous_attempt_id) is not str
                or _ATTEMPT_ID_RE.fullmatch(self.previous_attempt_id) is None
            ):
                _invalid("retry attempt requires a valid previous_attempt_id")
            expected_causation = semantic_gate_attempt_event_id(
                cast(str, self.previous_attempt_id)
            )
        if self.causation_event_id != expected_causation:
            _invalid("causation_event_id does not match the attempt parent")
        for name in (
            "prompt_artifact_sha256",
            "generation_config_sha256",
        ):
            _digest(getattr(self, name), name)
        if self.response_artifact_sha256 is not None:
            _digest(
                self.response_artifact_sha256,
                "response_artifact_sha256",
            )
        if self.status not in {"failed", "succeeded"}:
            _invalid("status is invalid")
        _revision_ids(
            self.final_allowed_revision_ids,
            "final_allowed_revision_ids",
        )
        _revision_ids(
            self.final_blocked_revision_ids,
            "final_blocked_revision_ids",
        )
        if set(self.final_allowed_revision_ids).intersection(
            self.final_blocked_revision_ids
        ):
            _invalid("final revision sets must be disjoint")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not int or not 0 <= value <= 1_000_000_000
            ):
                _invalid(f"{name} is invalid")
        if (
            type(self.latency_ms) is not int
            or not 0 <= self.latency_ms <= 1_000_000_000
        ):
            _invalid("latency_ms is invalid")
        try:
            if parse_rfc3339(self.finished_at) < parse_rfc3339(self.started_at):
                _invalid("finished_at precedes started_at")
        except ValueError as error:
            raise SemanticGateAttemptEventV1Error(
                "TBM_SEMANTIC_GATE_ATTEMPT_EVENT_INVALID",
                "attempt timestamps are invalid",
            ) from error
        if type(self.attempt_artifact_ref) is not EventArtifactRef:
            _invalid("attempt_artifact_ref must be exactly EventArtifactRef")
        if type(self.prompt_artifact_ref) is not EventArtifactRef:
            _invalid("prompt_artifact_ref must be exactly EventArtifactRef")
        if (
            self.response_artifact_ref is not None
            and type(self.response_artifact_ref) is not EventArtifactRef
        ):
            _invalid("response_artifact_ref is invalid")
        if (
            self.attempt_artifact_ref.media_type != "application/json"
            or self.attempt_artifact_ref.classification != "internal"
            or self.attempt_artifact_ref.encryption_key_id is not None
            or self.prompt_artifact_ref.media_type
            != SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE
            or self.prompt_artifact_ref.content_sha256
            != self.prompt_artifact_sha256
        ):
            _invalid("attempt or prompt Artifact reference is invalid")
        for artifact_ref in self.artifact_refs:
            if (
                artifact_ref.retention_policy_id
                != SEMANTIC_GATE_ATTEMPT_EVENT_RETENTION_POLICY_ID
                or artifact_ref.availability != "available"
                or artifact_ref.size_bytes <= 0
            ):
                _invalid("Artifact reference retention or availability is invalid")
        if self.status == "succeeded":
            if (
                self.response_artifact_sha256 is None
                or self.response_artifact_ref is None
                or self.response_artifact_ref.content_sha256
                != self.response_artifact_sha256
                or self.decision_id is None
                or self.risk not in {"low", "medium", "high", "unknown"}
                or self.recommended_injection not in {"none", "summary", "full"}
                or self.error_code is not None
            ):
                _invalid("succeeded attempt event fields are invalid")
        elif (
            self.response_artifact_sha256 is not None
            or self.response_artifact_ref is not None
            or self.decision_id is not None
            or self.final_allowed_revision_ids
            or self.final_blocked_revision_ids
            or self.risk is not None
            or self.recommended_injection is not None
            or self.error_code is None
        ):
            _invalid("failed attempt event fields are invalid")
        if self.error_code is not None:
            _identifier(self.error_code, "error_code")

    @property
    def artifact_refs(self) -> tuple[EventArtifactRef, ...]:
        values = (
            self.attempt_artifact_ref,
            self.prompt_artifact_ref,
            *((self.response_artifact_ref,) if self.response_artifact_ref else ()),
        )
        return _sorted_unique_artifact_refs(values)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": SEMANTIC_GATE_ATTEMPT_EVENT_CONTRACT_VERSION,
            "attempt_id": self.attempt_id,
            "stream_id": self.stream_id,
            "session_id": self.session_id,
            "retrieval_snapshot_id": self.retrieval_snapshot_id,
            "system_gate_evaluation_id": self.system_gate_evaluation_id,
            "sequence": self.sequence,
            "previous_attempt_id": self.previous_attempt_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "endpoint_id": self.endpoint_id,
            "prompt_template_id": self.prompt_template_id,
            "prompt_template_version": self.prompt_template_version,
            "prompt_artifact_sha256": self.prompt_artifact_sha256,
            "response_artifact_sha256": self.response_artifact_sha256,
            "generation_config_sha256": self.generation_config_sha256,
            "provider_request_id": self.provider_request_id,
            "status": self.status,
            "decision_id": self.decision_id,
            "final_allowed_revision_ids": list(
                self.final_allowed_revision_ids
            ),
            "final_blocked_revision_ids": list(
                self.final_blocked_revision_ids
            ),
            "risk": self.risk,
            "recommended_injection": self.recommended_injection,
            "error_code": self.error_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempt_artifact_id": self.attempt_artifact_ref.artifact_id,
            "attempt_content_sha256": self.attempt_artifact_ref.content_sha256,
            "prompt_artifact_id": self.prompt_artifact_ref.artifact_id,
            "response_artifact_id": (
                self.response_artifact_ref.artifact_id
                if self.response_artifact_ref is not None
                else None
            ),
            "causation_event_id": self.causation_event_id,
        }

    def to_projection_dict(self) -> dict[str, object]:
        return {
            **self.to_dict(),
            "attempt_artifact_ref": self.attempt_artifact_ref.to_dict(),
            "prompt_artifact_ref": self.prompt_artifact_ref.to_dict(),
            "response_artifact_ref": (
                self.response_artifact_ref.to_dict()
                if self.response_artifact_ref is not None
                else None
            ),
        }


def semantic_gate_attempt_event_payload_schema(
    event_type: str,
) -> dict[str, object]:
    if event_type not in SEMANTIC_GATE_ATTEMPT_EVENT_TYPES:
        _invalid("event_type is not a Semantic Gate attempt event")
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    }
    attempt_id = {
        "type": "string",
        "pattern": r"^semantic_attempt_sha256_[0-9a-f]{64}$",
    }
    optional_attempt_id = {
        "oneOf": [attempt_id, {"type": "null"}]
    }
    stream_id = {
        "type": "string",
        "pattern": r"^semantic_gate_stream_sha256_[0-9a-f]{64}$",
    }
    snapshot_id = {
        "type": "string",
        "pattern": r"^retrieval_snapshot_sha256_[0-9a-f]{64}$",
    }
    system_gate_id = {
        "type": "string",
        "pattern": r"^system_gate_sha256_[0-9a-f]{64}$",
    }
    revision_id = {
        "type": "string",
        "pattern": r"^memory_revision_sha256_[0-9a-f]{64}$",
    }
    artifact_id = {
        "type": "string",
        "pattern": r"^artifact_sha256_[0-9a-f]{64}$",
    }
    optional_identifier = {"oneOf": [identifier, {"type": "null"}]}
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    timestamp = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }
    revision_ids = {
        "type": "array",
        "maxItems": (
            4096
            if event_type == SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT
            else 0
        ),
        "uniqueItems": True,
        "items": revision_id,
    }
    optional_integer = {
        "oneOf": [
            {"type": "integer", "minimum": 0, "maximum": 1_000_000_000},
            {"type": "null"},
        ]
    }
    succeeded = event_type == SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT
    properties: dict[str, object] = {
        "contract_version": {
            "const": SEMANTIC_GATE_ATTEMPT_EVENT_CONTRACT_VERSION
        },
        "attempt_id": attempt_id,
        "stream_id": stream_id,
        "session_id": identifier,
        "retrieval_snapshot_id": snapshot_id,
        "system_gate_evaluation_id": system_gate_id,
        "sequence": {"type": "integer", "minimum": 1, "maximum": 4096},
        "previous_attempt_id": optional_attempt_id,
        "provider_id": identifier,
        "model_id": identifier,
        "model_version": identifier,
        "endpoint_id": optional_identifier,
        "prompt_template_id": identifier,
        "prompt_template_version": identifier,
        "prompt_artifact_sha256": digest,
        "response_artifact_sha256": digest if succeeded else {"const": None},
        "generation_config_sha256": digest,
        "provider_request_id": optional_identifier,
        "status": {"const": "succeeded" if succeeded else "failed"},
        "decision_id": identifier if succeeded else {"const": None},
        "final_allowed_revision_ids": revision_ids,
        "final_blocked_revision_ids": revision_ids,
        "risk": (
            {"enum": ["low", "medium", "high", "unknown"]}
            if succeeded
            else {"const": None}
        ),
        "recommended_injection": (
            {"enum": ["none", "summary", "full"]}
            if succeeded
            else {"const": None}
        ),
        "error_code": {"const": None} if succeeded else identifier,
        "input_tokens": optional_integer,
        "output_tokens": optional_integer,
        "latency_ms": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1_000_000_000,
        },
        "started_at": timestamp,
        "finished_at": timestamp,
        "attempt_artifact_id": artifact_id,
        "attempt_content_sha256": digest,
        "prompt_artifact_id": artifact_id,
        "response_artifact_id": artifact_id if succeeded else {"const": None},
        "causation_event_id": identifier,
    }
    def branch(
        sequence: Mapping[str, object],
        previous_attempt: Mapping[str, object],
    ) -> dict[str, object]:
        branch_properties = {
            **properties,
            "sequence": dict(sequence),
            "previous_attempt_id": dict(previous_attempt),
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(branch_properties),
            "properties": branch_properties,
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
        "oneOf": [
            branch({"const": 1}, {"const": None}),
            branch(
                {"type": "integer", "minimum": 2, "maximum": 4096},
                attempt_id,
            ),
        ]
    }


def semantic_gate_attempt_event_ref(
    attempt: SemanticGateAttempt,
    prompt: StoredSemanticGateArtifact,
    response: StoredSemanticGateArtifact | None,
) -> SemanticGateAttemptEventRef:
    try:
        StoredSemanticGateAttemptArtifacts(attempt, prompt, response)
    except ValueError as error:
        raise SemanticGateAttemptEventV1Error(
            "TBM_SEMANTIC_GATE_ATTEMPT_EVENT_INVALID",
            "attempt Artifact bundle is invalid",
        ) from error
    attempt_ref = _descriptor_artifact_ref(
        dumps_semantic_gate_attempt(attempt).encode("utf-8")
    )
    prompt_ref = _content_artifact_ref(prompt.binding.artifact)
    response_ref = (
        _content_artifact_ref(response.binding.artifact)
        if response is not None
        else None
    )
    causation_event_id = (
        gate_evidence_event_id(
            SYSTEM_GATE_EVALUATED_EVENT,
            attempt.system_gate_evaluation_id,
        )
        if attempt.sequence == 1
        else semantic_gate_attempt_event_id(
            cast(str, attempt.previous_attempt_id)
        )
    )
    return SemanticGateAttemptEventRef(
        attempt_id=attempt.attempt_id,
        stream_id=semantic_gate_attempt_stream_id(
            attempt.system_gate_evaluation_id
        ),
        session_id=attempt.session_id,
        retrieval_snapshot_id=attempt.retrieval_snapshot_id,
        system_gate_evaluation_id=attempt.system_gate_evaluation_id,
        sequence=attempt.sequence,
        previous_attempt_id=attempt.previous_attempt_id,
        provider_id=attempt.provider_id,
        model_id=attempt.model_id,
        model_version=attempt.model_version,
        endpoint_id=attempt.endpoint_id,
        prompt_template_id=attempt.prompt_template_id,
        prompt_template_version=attempt.prompt_template_version,
        prompt_artifact_sha256=attempt.prompt_artifact_sha256,
        response_artifact_sha256=attempt.response_artifact_sha256,
        generation_config_sha256=attempt.generation_config_sha256,
        provider_request_id=attempt.provider_request_id,
        status=attempt.status,
        decision_id=attempt.decision_id,
        final_allowed_revision_ids=attempt.final_allowed_revision_ids,
        final_blocked_revision_ids=attempt.final_blocked_revision_ids,
        risk=attempt.risk,
        recommended_injection=attempt.recommended_injection,
        error_code=attempt.error_code,
        input_tokens=attempt.input_tokens,
        output_tokens=attempt.output_tokens,
        latency_ms=attempt.latency_ms,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        attempt_artifact_ref=attempt_ref,
        prompt_artifact_ref=prompt_ref,
        response_artifact_ref=response_ref,
        causation_event_id=causation_event_id,
    )


def build_semantic_gate_attempt_event(
    attempt: SemanticGateAttempt,
    prompt: StoredSemanticGateArtifact,
    response: StoredSemanticGateArtifact | None,
    *,
    system_gate_event: CanonicalEvent | None,
    previous_event: CanonicalEvent | None,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    record_ref = semantic_gate_attempt_event_ref(attempt, prompt, response)
    previous_sha256: str | None
    causation_parent: CanonicalEvent
    if attempt.sequence == 1:
        if previous_event is not None:
            _invalid("first attempt cannot receive a previous stream event")
        verify_semantic_gate_system_parent(
            attempt,
            system_gate_event,
            trusted_context,
        )
        causation_parent = cast(CanonicalEvent, system_gate_event)
        previous_sha256 = None
    else:
        if system_gate_event is not None:
            _invalid("retry attempt cannot receive a System Gate parent event")
        if type(previous_event) is not CanonicalEvent:
            _invalid("retry attempt requires the previous stream event")
        verify_semantic_gate_event_scope(previous_event, trusted_context)
        previous_ref = parse_semantic_gate_attempt_event(previous_event)
        if (
            previous_ref.attempt_id != attempt.previous_attempt_id
            or previous_ref.stream_id != record_ref.stream_id
            or previous_ref.sequence + 1 != attempt.sequence
        ):
            _invalid("previous_event does not match the retry attempt")
        causation_parent = previous_event
        previous_sha256 = previous_event.event_sha256
    event_type = (
        SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT
        if attempt.status == "succeeded"
        else SEMANTIC_GATE_ATTEMPT_FAILED_EVENT
    )
    payload = record_ref.to_dict()
    identity_sha256 = _event_identity_sha256(attempt.attempt_id)
    event = build_canonical_event(
        event_id=semantic_gate_attempt_event_id(attempt.attempt_id),
        event_type=event_type,
        event_version=SEMANTIC_GATE_ATTEMPT_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=record_ref.stream_id,
        stream_type=SEMANTIC_GATE_ATTEMPT_EVENT_STREAM_TYPE,
        stream_version=attempt.sequence,
        global_position=global_position,
        trusted_context=trusted_context,
        request_id=(
            "semantic_attempt_request_"
            + identity_sha256.removeprefix("sha256:")[:40]
        ),
        idempotency_key_sha256=identity_sha256,
        request_sha256=_event_command_sha256(
            payload,
            trusted_context.authorization_decision_id,
        ),
        correlation_id=_correlation_id(attempt.session_id),
        causation_id=record_ref.causation_event_id,
        occurred_at=attempt.finished_at,
        recorded_at=attempt.finished_at,
        producer=SEMANTIC_GATE_ATTEMPT_EVENT_PRODUCER,
        producer_version=SEMANTIC_GATE_ATTEMPT_EVENT_PRODUCER_VERSION,
        payload_schema=f"{event_type}.v1",
        previous_stream_event_sha256=previous_sha256,
        classification="internal",
        retention_policy_id=SEMANTIC_GATE_ATTEMPT_EVENT_RETENTION_POLICY_ID,
        artifact_refs=record_ref.artifact_refs,
        payload=payload,
    )
    _verify_causation_order(event, causation_parent)
    parse_semantic_gate_attempt_event(event)
    return event


def parse_semantic_gate_attempt_event(
    event: CanonicalEvent,
) -> SemanticGateAttemptEventRef:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if event.event_type not in SEMANTIC_GATE_ATTEMPT_EVENT_TYPES:
        _invalid("event is not a Semantic Gate attempt event")
    if (
        event.event_version != SEMANTIC_GATE_ATTEMPT_EVENT_VERSION
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != SEMANTIC_GATE_ATTEMPT_EVENT_STREAM_TYPE
        or event.payload_schema != f"{event.event_type}.v1"
        or event.classification != "internal"
        or event.retention_policy_id
        != SEMANTIC_GATE_ATTEMPT_EVENT_RETENTION_POLICY_ID
        or not 1 <= len(event.artifact_refs) <= 3
    ):
        _invalid("Semantic Gate attempt event envelope is invalid")
    if tuple(event.artifact_refs) != _sorted_unique_artifact_refs(
        event.artifact_refs
    ):
        _invalid("Semantic Gate attempt Artifact references are not canonical")
    payload = _plain_mapping(event.payload)
    expected_fields = set(
        semantic_gate_attempt_event_payload_schema(event.event_type)[
            "properties"
        ]
    )
    if set(payload) != expected_fields:
        _invalid("Semantic Gate attempt event payload fields are invalid")
    if (
        payload["contract_version"]
        != SEMANTIC_GATE_ATTEMPT_EVENT_CONTRACT_VERSION
    ):
        _invalid("Semantic Gate attempt event contract_version is invalid")
    refs = {ref.artifact_id: ref for ref in event.artifact_refs}
    attempt_ref = refs.get(cast(str, payload["attempt_artifact_id"]))
    prompt_ref = refs.get(cast(str, payload["prompt_artifact_id"]))
    response_id = cast(str | None, payload["response_artifact_id"])
    response_ref = refs.get(response_id) if response_id is not None else None
    if attempt_ref is None or prompt_ref is None:
        _invalid("attempt or prompt Artifact reference is absent")
    record_ref = SemanticGateAttemptEventRef(
        attempt_id=cast(str, payload["attempt_id"]),
        stream_id=cast(str, payload["stream_id"]),
        session_id=cast(str, payload["session_id"]),
        retrieval_snapshot_id=cast(str, payload["retrieval_snapshot_id"]),
        system_gate_evaluation_id=cast(
            str,
            payload["system_gate_evaluation_id"],
        ),
        sequence=cast(int, payload["sequence"]),
        previous_attempt_id=cast(str | None, payload["previous_attempt_id"]),
        provider_id=cast(str, payload["provider_id"]),
        model_id=cast(str, payload["model_id"]),
        model_version=cast(str, payload["model_version"]),
        endpoint_id=cast(str | None, payload["endpoint_id"]),
        prompt_template_id=cast(str, payload["prompt_template_id"]),
        prompt_template_version=cast(
            str,
            payload["prompt_template_version"],
        ),
        prompt_artifact_sha256=cast(str, payload["prompt_artifact_sha256"]),
        response_artifact_sha256=cast(
            str | None,
            payload["response_artifact_sha256"],
        ),
        generation_config_sha256=cast(
            str,
            payload["generation_config_sha256"],
        ),
        provider_request_id=cast(
            str | None,
            payload["provider_request_id"],
        ),
        status=cast(SemanticGateAttemptEventStatus, payload["status"]),
        decision_id=cast(str | None, payload["decision_id"]),
        final_allowed_revision_ids=_string_tuple(
            payload["final_allowed_revision_ids"],
            "final_allowed_revision_ids",
        ),
        final_blocked_revision_ids=_string_tuple(
            payload["final_blocked_revision_ids"],
            "final_blocked_revision_ids",
        ),
        risk=cast(str | None, payload["risk"]),
        recommended_injection=cast(
            str | None,
            payload["recommended_injection"],
        ),
        error_code=cast(str | None, payload["error_code"]),
        input_tokens=cast(int | None, payload["input_tokens"]),
        output_tokens=cast(int | None, payload["output_tokens"]),
        latency_ms=cast(int, payload["latency_ms"]),
        started_at=cast(str, payload["started_at"]),
        finished_at=cast(str, payload["finished_at"]),
        attempt_artifact_ref=attempt_ref,
        prompt_artifact_ref=prompt_ref,
        response_artifact_ref=response_ref,
        causation_event_id=cast(str, payload["causation_event_id"]),
    )
    expected_type = (
        SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT
        if record_ref.status == "succeeded"
        else SEMANTIC_GATE_ATTEMPT_FAILED_EVENT
    )
    expected_ref_ids = {
        record_ref.attempt_artifact_ref.artifact_id,
        record_ref.prompt_artifact_ref.artifact_id,
        *(
            (record_ref.response_artifact_ref.artifact_id,)
            if record_ref.response_artifact_ref is not None
            else ()
        ),
    }
    if (
        event.event_type != expected_type
        or event.stream_id != record_ref.stream_id
        or event.stream_version != record_ref.sequence
        or event.causation_id != record_ref.causation_event_id
        or event.occurred_at != record_ref.finished_at
        or event.recorded_at != record_ref.finished_at
        or payload["attempt_content_sha256"]
        != record_ref.attempt_artifact_ref.content_sha256
        or set(refs) != expected_ref_ids
    ):
        _invalid("Semantic Gate attempt event linkage is invalid")
    if record_ref.sequence == 1:
        if event.previous_stream_event_sha256 is not None:
            _invalid("first attempt event cannot have a previous hash")
    elif event.previous_stream_event_sha256 is None:
        _invalid("retry attempt event requires a previous hash")
    _verify_deterministic_envelope(event, record_ref)
    return record_ref


def semantic_gate_attempt_stream_id(system_gate_evaluation_id: str) -> str:
    if (
        type(system_gate_evaluation_id) is not str
        or _SYSTEM_GATE_ID_RE.fullmatch(system_gate_evaluation_id) is None
    ):
        _invalid("system_gate_evaluation_id is invalid")
    digest = _domain_sha256(
        b"tbm.semantic-gate-attempt-stream.v1\x00",
        {"system_gate_evaluation_id": system_gate_evaluation_id},
    )
    stream_id = "semantic_gate_stream_sha256_" + digest.removeprefix("sha256:")
    if _STREAM_ID_RE.fullmatch(stream_id) is None:
        _invalid("semantic attempt stream ID is invalid")
    return stream_id


def semantic_gate_attempt_event_id(attempt_id: str) -> str:
    if type(attempt_id) is not str or _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        _invalid("attempt_id is invalid")
    digest = _event_identity_sha256(attempt_id)
    return "evt_semantic_attempt_" + digest.removeprefix("sha256:")


def verify_semantic_gate_system_parent(
    attempt: SemanticGateAttempt,
    system_gate_event: CanonicalEvent | None,
    trusted_context: EventTrustedContext,
) -> None:
    if type(attempt) is not SemanticGateAttempt:
        _invalid("attempt must be exactly SemanticGateAttempt")
    if type(system_gate_event) is not CanonicalEvent:
        _invalid("first attempt requires the retained System Gate event")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    from .gate_evidence_event_v1 import parse_gate_evidence_event

    try:
        system_ref = parse_gate_evidence_event(system_gate_event)
    except V3ContractError as error:
        raise SemanticGateAttemptEventV1Error(
            "TBM_SEMANTIC_GATE_ATTEMPT_EVENT_INVALID",
            "System Gate parent event failed validation",
        ) from error
    if (
        system_gate_event.event_type != SYSTEM_GATE_EVALUATED_EVENT
        or system_ref.record_id != attempt.system_gate_evaluation_id
        or system_ref.session_id != attempt.session_id
        or system_ref.retrieval_snapshot_id != attempt.retrieval_snapshot_id
        or system_gate_event.event_id
        != gate_evidence_event_id(
            SYSTEM_GATE_EVALUATED_EVENT,
            attempt.system_gate_evaluation_id,
        )
    ):
        _invalid("System Gate parent does not match the Semantic Gate attempt")
    verify_semantic_gate_event_scope(system_gate_event, trusted_context)


def verify_semantic_gate_event_scope(
    event: CanonicalEvent,
    trusted_context: EventTrustedContext,
) -> None:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    for name in (
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "principal_id",
        "agent_client_id",
        "actor_type",
        "actor_id",
    ):
        if getattr(event, name) != getattr(trusted_context, name):
            _invalid("event is outside the trusted event scope")


def _verify_causation_order(
    event: CanonicalEvent,
    parent: CanonicalEvent,
) -> None:
    if event.global_position <= parent.global_position:
        _invalid("global_position must advance after the causation parent")
    if parse_rfc3339(event.recorded_at) < parse_rfc3339(parent.recorded_at):
        _invalid("recorded_at precedes the causation parent")


def _verify_deterministic_envelope(
    event: CanonicalEvent,
    record_ref: SemanticGateAttemptEventRef,
) -> None:
    identity_sha256 = _event_identity_sha256(record_ref.attempt_id)
    if (
        event.event_id != semantic_gate_attempt_event_id(record_ref.attempt_id)
        or event.idempotency_key_sha256 != identity_sha256
        or event.request_id
        != "semantic_attempt_request_"
        + identity_sha256.removeprefix("sha256:")[:40]
        or event.request_sha256
        != _event_command_sha256(
            record_ref.to_dict(),
            event.authorization_decision_id,
        )
        or event.correlation_id != _correlation_id(record_ref.session_id)
        or event.producer != SEMANTIC_GATE_ATTEMPT_EVENT_PRODUCER
        or event.producer_version
        != SEMANTIC_GATE_ATTEMPT_EVENT_PRODUCER_VERSION
    ):
        _invalid("Semantic Gate attempt deterministic envelope is invalid")


def _descriptor_artifact_ref(content: bytes) -> EventArtifactRef:
    content_sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + content_sha256.removeprefix("sha256:"),
        content_sha256=content_sha256,
        media_type="application/json",
        size_bytes=len(content),
        classification="internal",
        retention_policy_id=SEMANTIC_GATE_ATTEMPT_EVENT_RETENTION_POLICY_ID,
        encryption_key_id=None,
        availability="available",
    )


def _content_artifact_ref(
    artifact: ContentAddressedArtifact,
) -> EventArtifactRef:
    if type(artifact) is not ContentAddressedArtifact:
        _invalid("artifact must be exactly ContentAddressedArtifact")
    return EventArtifactRef(
        artifact_id=artifact.artifact_id,
        content_sha256=artifact.content_sha256,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        classification=artifact.classification,
        retention_policy_id=SEMANTIC_GATE_ATTEMPT_EVENT_RETENTION_POLICY_ID,
        encryption_key_id=artifact.encryption_key_id,
        availability="available",
    )


def _sorted_unique_artifact_refs(
    values: tuple[EventArtifactRef, ...],
) -> tuple[EventArtifactRef, ...]:
    refs: dict[str, EventArtifactRef] = {}
    for value in values:
        retained = refs.get(value.artifact_id)
        if retained is not None and retained != value:
            _invalid("one Artifact ID has conflicting descriptors")
        refs[value.artifact_id] = value
    return tuple(refs[key] for key in sorted(refs))


def _event_identity_sha256(attempt_id: str) -> str:
    return _domain_sha256(
        b"tbm.semantic-gate-attempt-event-identity.v1\x00",
        {"attempt_id": attempt_id},
    )


def _event_command_sha256(
    payload: Mapping[str, object],
    authorization_decision_id: str,
) -> str:
    return _domain_sha256(
        b"tbm.semantic-gate-attempt-event-command.v1\x00",
        {
            "payload": payload,
            "authorization_decision_id": authorization_decision_id,
        },
    )


def _correlation_id(session_id: str) -> str:
    digest = _domain_sha256(
        b"tbm.semantic-gate-attempt-event-correlation.v1\x00",
        {"session_id": session_id},
    )
    return "semantic_gate_correlation_" + digest.removeprefix("sha256:")[:40]


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _revision_ids(value: object, name: str) -> None:
    if (
        type(value) is not tuple
        or len(value) > 4096
        or len(set(value)) != len(value)
        or any(
            type(item) is not str or _REVISION_ID_RE.fullmatch(item) is None
            for item in value
        )
    ):
        _invalid(f"{name} is invalid")


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid(f"{name} is invalid")
    return tuple(cast(list[str], value))


def _plain_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid("Semantic Gate attempt event payload is invalid")
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
    raise SemanticGateAttemptEventV1Error(
        "TBM_SEMANTIC_GATE_ATTEMPT_EVENT_INVALID",
        message,
    )


__all__ = [
    "SEMANTIC_GATE_ATTEMPT_EVENT_CONTRACT_VERSION",
    "SEMANTIC_GATE_ATTEMPT_EVENT_RETENTION_POLICY_ID",
    "SEMANTIC_GATE_ATTEMPT_EVENT_STREAM_TYPE",
    "SEMANTIC_GATE_ATTEMPT_EVENT_TYPES",
    "SEMANTIC_GATE_ATTEMPT_EVENT_VERSION",
    "SEMANTIC_GATE_ATTEMPT_FAILED_EVENT",
    "SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT",
    "SemanticGateAttemptEventRef",
    "SemanticGateAttemptEventV1Error",
    "build_semantic_gate_attempt_event",
    "parse_semantic_gate_attempt_event",
    "semantic_gate_attempt_event_id",
    "semantic_gate_attempt_event_payload_schema",
    "semantic_gate_attempt_event_ref",
    "semantic_gate_attempt_stream_id",
    "verify_semantic_gate_event_scope",
    "verify_semantic_gate_system_parent",
]
