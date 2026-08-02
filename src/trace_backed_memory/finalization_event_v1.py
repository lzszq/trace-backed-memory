from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import NoReturn, cast

from ._timestamps import RFC3339_PATTERN
from .contracts_v3 import V3ContractError
from .event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    EventTrustedContext,
    build_canonical_event,
    verify_event_trusted_context,
)
from .gate_session_event_v1 import (
    GATE_SESSION_EVENT_RETENTION_POLICY_ID,
    USAGE_DECISION_FINALIZED_EVENT,
    parse_gate_session_event,
)
from .gate_session_v3 import GateSession
from .policy import (
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)
from .replay_v3 import (
    ARTIFACT_MAX_BYTES,
    INJECTION_ARTIFACT_MAX_BYTES,
    INJECTION_ARTIFACT_MEDIA_TYPE,
    REPLAY_COMPONENT_NAMES,
    ContentAddressedArtifact,
    DecisionReplayManifest,
    InjectionArtifact,
    StoredReplayArtifact,
    artifact_id_from_sha256,
    build_decision_replay_manifest,
    parse_injection_artifact,
    verify_artifact_content,
    verify_injection_artifact,
)
from .usage_decision_v3 import (
    USAGE_DECISION_ARTIFACT_MEDIA_TYPE,
    UsageDecision,
    create_usage_decision_artifact,
    loads_usage_decision_artifact,
    parse_usage_decision,
    usage_decision_artifact_id,
)


FINALIZATION_EVENT_CONTRACT_VERSION = "tbm.finalization-event.v1"
FINALIZATION_EVENT_VERSION = 1
FINALIZATION_EVENT_STREAM_TYPE = "finalization"
FINALIZATION_EVENT_PRODUCER = "trace_backed_memory"
FINALIZATION_EVENT_PRODUCER_VERSION = "0.1.0"
FINALIZATION_EVENT_RETENTION_POLICY_ID = GATE_SESSION_EVENT_RETENTION_POLICY_ID

INJECTION_RENDERED_EVENT = "tbm.injection.rendered"
FINALIZATION_EVENT_TYPES = (INJECTION_RENDERED_EVENT,)
FINALIZATION_ARTIFACT_ROLES: tuple[str, ...] = (
    "usage_decision",
    *REPLAY_COMPONENT_NAMES,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_USAGE_ID_RE = re.compile(r"^usage_decision_sha256_[0-9a-f]{64}$")
_SNAPSHOT_ID_RE = re.compile(r"^retrieval_snapshot_sha256_[0-9a-f]{64}$")
_SYSTEM_GATE_ID_RE = re.compile(r"^system_gate_sha256_[0-9a-f]{64}$")
_SEMANTIC_ATTEMPT_ID_RE = re.compile(
    r"^semantic_attempt_sha256_[0-9a-f]{64}$"
)
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_AUTHORIZATION_ID_RE = re.compile(r"^authz_sha256_[0-9a-f]{64}$")
_STREAM_ID_RE = re.compile(r"^finalization_stream_sha256_[0-9a-f]{64}$")
_CLASSIFICATION_RANK: dict[EventClassification, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


class FinalizationEventV1Error(V3ContractError):
    """Stable failure for exact final decision and injection evidence."""


@dataclass(frozen=True)
class FinalizationEventRef:
    usage_decision: UsageDecision
    injection: InjectionArtifact
    replay_manifest_sha256: str
    artifact_roles: tuple[tuple[str, str], ...]
    artifact_refs: tuple[EventArtifactRef, ...]
    causation_event_id: str

    def __post_init__(self) -> None:
        if type(self.usage_decision) is not UsageDecision:
            _invalid("usage_decision must be exactly UsageDecision")
        if type(self.injection) is not InjectionArtifact:
            _invalid("injection must be exactly InjectionArtifact")
        _digest(self.replay_manifest_sha256, "replay_manifest_sha256")
        _identifier(self.causation_event_id, "causation_event_id")
        expected_manifest = _manifest_from_usage(self.usage_decision)
        if self.replay_manifest_sha256 != expected_manifest.manifest_sha256:
            _invalid("replay manifest digest does not match the UsageDecision")
        if not _usage_injection_match(self.usage_decision, self.injection):
            _invalid("UsageDecision and injection metadata do not match")
        expected_roles = _expected_artifact_roles(self.usage_decision)
        if self.artifact_roles != expected_roles:
            _invalid("artifact_roles do not match the complete replay bundle")
        if (
            type(self.artifact_refs) is not tuple
            or any(type(item) is not EventArtifactRef for item in self.artifact_refs)
            or self.artifact_refs
            != tuple(sorted(self.artifact_refs, key=lambda item: item.artifact_id))
            or len({item.artifact_id for item in self.artifact_refs})
            != len(self.artifact_refs)
        ):
            _invalid("artifact_refs must be sorted unique EventArtifactRef values")
        expected_ids = {artifact_id for _, artifact_id in expected_roles}
        refs = {item.artifact_id: item for item in self.artifact_refs}
        if set(refs) != expected_ids:
            _invalid("artifact_refs do not cover the complete replay bundle")
        _verify_usage_artifact_ref(self.usage_decision, refs)
        _verify_injection_artifact_ref(self.injection, refs)
        component_map = dict(self.usage_decision.replay_components)
        for component_name in REPLAY_COMPONENT_NAMES:
            artifact_id = dict(expected_roles)[component_name]
            if refs[artifact_id].content_sha256 != component_map[component_name]:
                _invalid("replay component Artifact reference digest is invalid")
        for artifact_ref in self.artifact_refs:
            if (
                artifact_ref.retention_policy_id
                != FINALIZATION_EVENT_RETENTION_POLICY_ID
                or artifact_ref.availability != "available"
                or artifact_ref.size_bytes <= 0
            ):
                _invalid("Artifact reference retention or availability is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": FINALIZATION_EVENT_CONTRACT_VERSION,
            "usage_decision": self.usage_decision.to_dict(),
            "injection": self.injection.to_dict(),
            "replay_manifest_sha256": self.replay_manifest_sha256,
            "artifact_roles": dict(self.artifact_roles),
            "causation_event_id": self.causation_event_id,
        }

    def to_projection_dict(self) -> dict[str, object]:
        return {
            **self.to_dict(),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
        }


def build_injection_rendered_event(
    usage_decision: UsageDecision,
    supporting_artifacts: tuple[StoredReplayArtifact, ...],
    injection: InjectionArtifact,
    injection_content: bytes,
    manifest: DecisionReplayManifest,
    *,
    decided_session: GateSession,
    finalized_session: GateSession,
    decided_event: CanonicalEvent,
    finalized_event: CanonicalEvent,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    try:
        retained_session = parse_gate_session_event(
            finalized_event,
            previous_session=decided_session,
            parent_event=decided_event,
        )
    except Exception as error:
        raise FinalizationEventV1Error(
            "TBM_FINALIZATION_EVENT_INVALID",
            "UsageDecisionFinalized parent event is invalid",
        ) from error
    if retained_session != finalized_session:
        _invalid("finalized_session does not match its canonical event")
    _verify_finalized_linkage(finalized_session, usage_decision, injection)
    try:
        verify_event_trusted_context(finalized_event, trusted_context)
    except Exception as error:
        raise FinalizationEventV1Error(
            "TBM_FINALIZATION_EVENT_INVALID",
            "finalization event is outside the trusted event scope",
        ) from error
    if finalized_event.event_type != USAGE_DECISION_FINALIZED_EVENT:
        _invalid("causation event is not UsageDecisionFinalized")
    if global_position <= finalized_event.global_position:
        _invalid("global_position must advance after UsageDecisionFinalized")
    record_ref = finalization_event_ref(
        usage_decision,
        supporting_artifacts,
        injection,
        injection_content,
        manifest,
        causation_event_id=finalized_event.event_id,
    )
    payload = record_ref.to_dict()
    identity_sha256 = _event_identity_sha256(manifest.manifest_sha256)
    event = build_canonical_event(
        event_id="evt_finalization_" + identity_sha256.removeprefix("sha256:"),
        event_type=INJECTION_RENDERED_EVENT,
        event_version=FINALIZATION_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=finalization_event_stream_id(manifest.manifest_sha256),
        stream_type=FINALIZATION_EVENT_STREAM_TYPE,
        stream_version=1,
        global_position=global_position,
        trusted_context=trusted_context,
        request_id=(
            "finalization_request_"
            + identity_sha256.removeprefix("sha256:")[:44]
        ),
        idempotency_key_sha256=identity_sha256,
        request_sha256=_event_command_sha256(
            payload,
            trusted_context.authorization_decision_id,
        ),
        correlation_id=_correlation_id(usage_decision.session_id),
        causation_id=finalized_event.event_id,
        occurred_at=injection.rendered_at,
        recorded_at=finalized_event.recorded_at,
        producer=FINALIZATION_EVENT_PRODUCER,
        producer_version=FINALIZATION_EVENT_PRODUCER_VERSION,
        payload_schema=f"{INJECTION_RENDERED_EVENT}.v1",
        previous_stream_event_sha256=None,
        classification=_event_classification(record_ref.artifact_refs),
        retention_policy_id=FINALIZATION_EVENT_RETENTION_POLICY_ID,
        artifact_refs=record_ref.artifact_refs,
        payload=payload,
    )
    parsed = parse_injection_rendered_event(event)
    if parsed != record_ref:
        raise AssertionError("finalization event did not round-trip")
    return event


def finalization_event_ref(
    usage_decision: UsageDecision,
    supporting_artifacts: tuple[StoredReplayArtifact, ...],
    injection: InjectionArtifact,
    injection_content: bytes,
    manifest: DecisionReplayManifest,
    *,
    causation_event_id: str,
) -> FinalizationEventRef:
    artifact_refs = _verify_exact_bundle(
        usage_decision,
        supporting_artifacts,
        injection,
        injection_content,
        manifest,
    )
    return FinalizationEventRef(
        usage_decision=usage_decision,
        injection=injection,
        replay_manifest_sha256=manifest.manifest_sha256,
        artifact_roles=_expected_artifact_roles(usage_decision),
        artifact_refs=artifact_refs,
        causation_event_id=causation_event_id,
    )


def parse_injection_rendered_event(event: CanonicalEvent) -> FinalizationEventRef:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if (
        event.event_type != INJECTION_RENDERED_EVENT
        or event.event_version != FINALIZATION_EVENT_VERSION
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != FINALIZATION_EVENT_STREAM_TYPE
        or event.stream_version != 1
        or event.previous_stream_event_sha256 is not None
        or event.payload_schema != f"{INJECTION_RENDERED_EVENT}.v1"
        or event.retention_policy_id != FINALIZATION_EVENT_RETENTION_POLICY_ID
    ):
        _invalid("InjectionRendered event envelope is invalid")
    payload = _plain_mapping(event.payload, "InjectionRendered payload")
    expected_fields = {
        "contract_version",
        "usage_decision",
        "injection",
        "replay_manifest_sha256",
        "artifact_roles",
        "causation_event_id",
    }
    if set(payload) != expected_fields:
        _invalid("InjectionRendered payload fields are invalid")
    if payload["contract_version"] != FINALIZATION_EVENT_CONTRACT_VERSION:
        _invalid("InjectionRendered payload contract version is invalid")
    usage_payload = _plain_mapping(
        payload["usage_decision"],
        "InjectionRendered UsageDecision",
    )
    injection_payload = _plain_mapping(
        payload["injection"],
        "InjectionRendered injection",
    )
    try:
        usage_decision = parse_usage_decision(usage_payload)
        injection = parse_injection_artifact(injection_payload)
    except Exception as error:
        raise FinalizationEventV1Error(
            "TBM_FINALIZATION_EVENT_INVALID",
            "InjectionRendered domain payload is invalid",
        ) from error
    artifact_roles_payload = _plain_mapping(
        payload["artifact_roles"],
        "InjectionRendered artifact_roles",
    )
    if set(artifact_roles_payload) != set(FINALIZATION_ARTIFACT_ROLES) or any(
        type(value) is not str for value in artifact_roles_payload.values()
    ):
        _invalid("InjectionRendered artifact_roles are invalid")
    record_ref = FinalizationEventRef(
        usage_decision=usage_decision,
        injection=injection,
        replay_manifest_sha256=_string(payload, "replay_manifest_sha256"),
        artifact_roles=tuple(
            (role, cast(str, artifact_roles_payload[role]))
            for role in FINALIZATION_ARTIFACT_ROLES
        ),
        artifact_refs=event.artifact_refs,
        causation_event_id=_string(payload, "causation_event_id"),
    )
    if (
        event.stream_id
        != finalization_event_stream_id(record_ref.replay_manifest_sha256)
        or event.causation_id != record_ref.causation_event_id
        or event.occurred_at != record_ref.injection.rendered_at
        or event.classification != _event_classification(event.artifact_refs)
    ):
        _invalid("InjectionRendered event linkage is invalid")
    _verify_deterministic_envelope(event, record_ref)
    return record_ref


def finalization_event_stream_id(replay_manifest_sha256: str) -> str:
    _digest(replay_manifest_sha256, "replay_manifest_sha256")
    return (
        "finalization_stream_sha256_"
        + replay_manifest_sha256.removeprefix("sha256:")
    )


def injection_rendered_event_id(replay_manifest_sha256: str) -> str:
    identity_sha256 = _event_identity_sha256(replay_manifest_sha256)
    return "evt_finalization_" + identity_sha256.removeprefix("sha256:")


def finalization_event_payload_schema(event_type: str) -> dict[str, object]:
    if event_type != INJECTION_RENDERED_EVENT:
        _invalid("event_type is not a finalization event")
    properties: dict[str, object] = {
        "contract_version": {"const": FINALIZATION_EVENT_CONTRACT_VERSION},
        "usage_decision": _usage_decision_schema(),
        "injection": _injection_schema(),
        "replay_manifest_sha256": _digest_schema(),
        "artifact_roles": _artifact_roles_schema(),
        "causation_event_id": _identifier_schema(),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _verify_exact_bundle(
    usage_decision: UsageDecision,
    supporting_artifacts: tuple[StoredReplayArtifact, ...],
    injection: InjectionArtifact,
    injection_content: bytes,
    manifest: DecisionReplayManifest,
) -> tuple[EventArtifactRef, ...]:
    if (
        type(supporting_artifacts) is not tuple
        or any(type(item) is not StoredReplayArtifact for item in supporting_artifacts)
    ):
        _invalid("supporting_artifacts must be StoredReplayArtifact values")
    if type(injection_content) is not bytes:
        _invalid("injection_content must be bytes")
    expected_manifest = _manifest_from_usage(usage_decision)
    if manifest != expected_manifest:
        _invalid("manifest does not match the exact UsageDecision")
    if not _usage_injection_match(usage_decision, injection):
        _invalid("injection does not match the exact UsageDecision")
    try:
        snippet = injection_content.decode("utf-8")
    except UnicodeError:
        _invalid("injection_content is not valid UTF-8")
    if not verify_injection_artifact(injection, snippet):
        _invalid("injection_content does not match the injection descriptor")
    artifacts: dict[str, ContentAddressedArtifact] = {
        injection.artifact.artifact_id: injection.artifact
    }
    stored_by_id: dict[str, StoredReplayArtifact] = {}
    for stored in supporting_artifacts:
        if not verify_artifact_content(stored.artifact, stored.content):
            _invalid("supporting Artifact bytes do not match their descriptor")
        retained = stored_by_id.get(stored.artifact.artifact_id)
        if retained is not None and retained != stored:
            _invalid("one supporting Artifact ID has conflicting content")
        stored_by_id[stored.artifact.artifact_id] = stored
        artifacts[stored.artifact.artifact_id] = stored.artifact
    usage_artifact_id = usage_decision_artifact_id(
        usage_decision.usage_decision_id
    )
    retained_usage = stored_by_id.get(usage_artifact_id)
    if retained_usage is None:
        _invalid("complete bundle is missing the UsageDecision Artifact")
    try:
        if loads_usage_decision_artifact(retained_usage.content) != usage_decision:
            _invalid("UsageDecision Artifact bytes do not match the decision")
    except Exception as error:
        raise FinalizationEventV1Error(
            "TBM_FINALIZATION_EVENT_INVALID",
            "UsageDecision Artifact bytes are invalid",
        ) from error
    expected_ids = {
        artifact_id for _, artifact_id in _expected_artifact_roles(usage_decision)
    }
    if set(artifacts) != expected_ids:
        _invalid("complete bundle Artifact set does not match replay roles")
    return tuple(
        sorted(
            (_event_artifact_ref(artifact) for artifact in artifacts.values()),
            key=lambda item: item.artifact_id,
        )
    )


def _verify_finalized_linkage(
    session: GateSession,
    usage_decision: UsageDecision,
    injection: InjectionArtifact,
) -> None:
    if (
        session.status != "finalized"
        or session.session_id != usage_decision.session_id
        or session.decision_id != usage_decision.decision_id
        or session.trace_id != usage_decision.trace_id
        or session.run_id != usage_decision.run_id
        or session.retrieval_snapshot_id != usage_decision.retrieval_snapshot_id
        or session.system_gate_evaluation_id
        != usage_decision.system_gate_evaluation_id
        or not session.semantic_gate_attempt_ids
        or session.semantic_gate_attempt_ids[-1]
        != usage_decision.semantic_gate_attempt_id
        or session.final_memory_revision_ids
        != usage_decision.final_memory_revision_ids
        or session.usage_decision_id != usage_decision.usage_decision_id
        or session.injection_artifact_id != injection.artifact.artifact_id
    ):
        _invalid("finalized GateSession does not match finalization evidence")


def _usage_injection_match(
    usage_decision: UsageDecision,
    injection: InjectionArtifact,
) -> bool:
    return (
        injection.session_id == usage_decision.session_id
        and injection.decision_id == usage_decision.decision_id
        and injection.usage_decision_id == usage_decision.usage_decision_id
        and injection.memory_revision_ids
        == usage_decision.final_memory_revision_ids
        and injection.renderer_id == usage_decision.renderer_id
        and injection.renderer_version == usage_decision.renderer_version
        and injection.policy_bundle_sha256
        == usage_decision.policy_bundle_sha256
        and injection.artifact.artifact_id
        == usage_decision.injection_artifact_id
        and injection.rendered_at == usage_decision.created_at
    )


def _manifest_from_usage(usage_decision: UsageDecision) -> DecisionReplayManifest:
    return build_decision_replay_manifest(
        session_id=usage_decision.session_id,
        decision_id=usage_decision.decision_id,
        usage_decision_id=usage_decision.usage_decision_id,
        component_hashes=dict(usage_decision.replay_components),
        injection_artifact_id=usage_decision.injection_artifact_id,
        completeness="complete",
        created_at=usage_decision.created_at,
    )


def _expected_artifact_roles(
    usage_decision: UsageDecision,
) -> tuple[tuple[str, str], ...]:
    components = dict(usage_decision.replay_components)
    return (
        (
            "usage_decision",
            usage_decision_artifact_id(usage_decision.usage_decision_id),
        ),
        *(
            (component_name, artifact_id_from_sha256(components[component_name]))
            for component_name in REPLAY_COMPONENT_NAMES
        ),
    )


def _verify_usage_artifact_ref(
    usage_decision: UsageDecision,
    refs: Mapping[str, EventArtifactRef],
) -> None:
    expected = create_usage_decision_artifact(usage_decision).artifact
    artifact_ref = refs[expected.artifact_id]
    if (
        artifact_ref.content_sha256 != expected.content_sha256
        or artifact_ref.media_type != USAGE_DECISION_ARTIFACT_MEDIA_TYPE
        or artifact_ref.size_bytes != expected.size_bytes
        or artifact_ref.classification != expected.classification
        or artifact_ref.encryption_key_id != expected.encryption_key_id
    ):
        _invalid("UsageDecision Artifact reference is invalid")


def _verify_injection_artifact_ref(
    injection: InjectionArtifact,
    refs: Mapping[str, EventArtifactRef],
) -> None:
    expected = injection.artifact
    artifact_ref = refs[expected.artifact_id]
    if (
        artifact_ref.content_sha256 != expected.content_sha256
        or artifact_ref.media_type != expected.media_type
        or artifact_ref.size_bytes != expected.size_bytes
        or artifact_ref.classification != expected.classification
        or artifact_ref.encryption_key_id != expected.encryption_key_id
    ):
        _invalid("injection Artifact reference is invalid")


def _event_artifact_ref(artifact: ContentAddressedArtifact) -> EventArtifactRef:
    if type(artifact) is not ContentAddressedArtifact:
        _invalid("artifact must be exactly ContentAddressedArtifact")
    return EventArtifactRef(
        artifact_id=artifact.artifact_id,
        content_sha256=artifact.content_sha256,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        classification=cast(EventClassification, artifact.classification),
        retention_policy_id=FINALIZATION_EVENT_RETENTION_POLICY_ID,
        encryption_key_id=artifact.encryption_key_id,
        availability="available",
    )


def _event_classification(
    artifact_refs: tuple[EventArtifactRef, ...],
) -> EventClassification:
    if not artifact_refs:
        _invalid("finalization event requires Artifact references")
    return max(
        (item.classification for item in artifact_refs),
        key=lambda value: _CLASSIFICATION_RANK[value],
    )


def _verify_deterministic_envelope(
    event: CanonicalEvent,
    record_ref: FinalizationEventRef,
) -> None:
    identity_sha256 = _event_identity_sha256(
        record_ref.replay_manifest_sha256
    )
    if (
        event.event_id != injection_rendered_event_id(
            record_ref.replay_manifest_sha256
        )
        or event.idempotency_key_sha256 != identity_sha256
        or event.request_id
        != "finalization_request_"
        + identity_sha256.removeprefix("sha256:")[:44]
        or event.request_sha256
        != _event_command_sha256(
            record_ref.to_dict(),
            event.authorization_decision_id,
        )
        or event.correlation_id
        != _correlation_id(record_ref.usage_decision.session_id)
        or event.producer != FINALIZATION_EVENT_PRODUCER
        or event.producer_version != FINALIZATION_EVENT_PRODUCER_VERSION
        or event.recorded_at is None
    ):
        _invalid("InjectionRendered deterministic envelope is invalid")


def _event_identity_sha256(replay_manifest_sha256: str) -> str:
    return _domain_sha256(
        b"tbm.finalization-event-identity.v1\x00",
        {"replay_manifest_sha256": replay_manifest_sha256},
    )


def _event_command_sha256(
    payload: Mapping[str, object],
    authorization_decision_id: str,
) -> str:
    return _domain_sha256(
        b"tbm.finalization-event-command.v1\x00",
        {
            "payload": payload,
            "authorization_decision_id": authorization_decision_id,
        },
    )


def _correlation_id(session_id: str) -> str:
    digest = _domain_sha256(
        b"tbm.finalization-event-correlation.v1\x00",
        {"session_id": session_id},
    )
    return "finalization_correlation_" + digest.removeprefix("sha256:")[:40]


def _usage_decision_schema() -> dict[str, object]:
    revision_ids = _revision_ids_schema()
    system_block_properties: dict[str, object] = {
        "memory_revision_id": _revision_id_schema(),
        "reason_code": _identifier_schema(),
        "rule_id": _identifier_schema(),
    }
    replay_components = {
        "type": "object",
        "additionalProperties": False,
        "required": list(REPLAY_COMPONENT_NAMES),
        "properties": {
            name: _digest_schema() for name in REPLAY_COMPONENT_NAMES
        },
    }
    properties: dict[str, object] = {
        "contract_version": {"const": "tbm.usage-decision.v3"},
        "usage_decision_id": {
            "type": "string",
            "pattern": _USAGE_ID_RE.pattern,
        },
        "session_id": _identifier_schema(),
        "decision_id": _identifier_schema(),
        "trace_id": _identifier_schema(),
        "run_id": _identifier_schema(),
        "authorization_event_id": {
            "type": "string",
            "pattern": _AUTHORIZATION_ID_RE.pattern,
        },
        "retrieval_snapshot_id": {
            "type": "string",
            "pattern": _SNAPSHOT_ID_RE.pattern,
        },
        "system_gate_evaluation_id": {
            "type": "string",
            "pattern": _SYSTEM_GATE_ID_RE.pattern,
        },
        "semantic_gate_attempt_id": {
            "type": "string",
            "pattern": _SEMANTIC_ATTEMPT_ID_RE.pattern,
        },
        "candidate_memory_revision_ids": revision_ids,
        "system_allowed_memory_revision_ids": revision_ids,
        "semantic_allowed_memory_revision_ids": revision_ids,
        "final_memory_revision_ids": revision_ids,
        "blocked_memory_revision_ids": revision_ids,
        "system_blocked": {
            "type": "array",
            "maxItems": 4096,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(system_block_properties),
                "properties": system_block_properties,
            },
        },
        "reason": {
            "type": "string",
            "maxLength": MEMORY_DECISION_REASON_MAX_CHARS,
        },
        "risk": {"enum": ["low", "medium", "high"]},
        "recommended_injection": {"enum": ["none", "summary", "full"]},
        "renderer_id": _identifier_schema(),
        "renderer_version": {
            "type": "string",
            "minLength": 1,
            "maxLength": METADATA_VALUE_MAX_CHARS,
        },
        "policy_bundle_sha256": _digest_schema(),
        "injection_artifact_id": _artifact_id_schema(),
        "replay_components": replay_components,
        "created_at": _timestamp_schema(),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _injection_schema() -> dict[str, object]:
    artifact_properties: dict[str, object] = {
        "artifact_id": _artifact_id_schema(),
        "content_sha256": _digest_schema(),
        "size_bytes": {
            "type": "integer",
            "minimum": 0,
            "maximum": ARTIFACT_MAX_BYTES,
        },
        "media_type": {"const": INJECTION_ARTIFACT_MEDIA_TYPE},
        "classification": {
            "enum": ["public", "internal", "confidential", "restricted"]
        },
        "created_at": _timestamp_schema(),
        "encryption_key_id": _optional_identifier_schema(),
        "redaction_policy_id": _optional_identifier_schema(),
    }
    properties: dict[str, object] = {
        "contract_version": {"const": "tbm.replay.v3"},
        "artifact_kind": {"const": "injection"},
        "artifact": {
            "type": "object",
            "additionalProperties": False,
            "required": list(artifact_properties),
            "properties": artifact_properties,
        },
        "session_id": _identifier_schema(),
        "decision_id": _identifier_schema(),
        "usage_decision_id": {
            "type": "string",
            "pattern": _USAGE_ID_RE.pattern,
        },
        "memory_revision_ids": _revision_ids_schema(),
        "renderer_id": _identifier_schema(),
        "renderer_version": {
            "type": "string",
            "minLength": 1,
            "maxLength": METADATA_VALUE_MAX_CHARS,
        },
        "policy_bundle_sha256": _digest_schema(),
        "rendered_at": _timestamp_schema(),
    }
    cast(dict[str, object], artifact_properties["size_bytes"])["maximum"] = (
        INJECTION_ARTIFACT_MAX_BYTES
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _artifact_roles_schema() -> dict[str, object]:
    properties = {
        role: _artifact_id_schema() for role in FINALIZATION_ARTIFACT_ROLES
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _identifier_schema() -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MEMORY_ID_MAX_CHARS,
        "pattern": r"^(?!\s)(?!.*\s$)[^\x00-\x1f\x7f]+$",
    }


def _optional_identifier_schema() -> dict[str, object]:
    return {"oneOf": [_identifier_schema(), {"type": "null"}]}


def _artifact_id_schema() -> dict[str, object]:
    return {"type": "string", "pattern": _ARTIFACT_ID_RE.pattern}


def _digest_schema() -> dict[str, object]:
    return {"type": "string", "pattern": _DIGEST_RE.pattern}


def _revision_id_schema() -> dict[str, object]:
    return {"type": "string", "pattern": _REVISION_ID_RE.pattern}


def _revision_ids_schema() -> dict[str, object]:
    return {
        "type": "array",
        "maxItems": 4096,
        "uniqueItems": True,
        "items": _revision_id_schema(),
    }


def _timestamp_schema() -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }


def _plain_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{name} must be an object")
    return {str(key): _plain_json(item) for key, item in value.items()}


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    return value


def _string(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return cast(str, value)


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(
        domain + _canonical_json_bytes(value)
    ).hexdigest()


def _invalid(message: str) -> NoReturn:
    raise FinalizationEventV1Error(
        "TBM_FINALIZATION_EVENT_INVALID",
        message,
    )


__all__ = [
    "FINALIZATION_ARTIFACT_ROLES",
    "FINALIZATION_EVENT_CONTRACT_VERSION",
    "FINALIZATION_EVENT_RETENTION_POLICY_ID",
    "FINALIZATION_EVENT_STREAM_TYPE",
    "FINALIZATION_EVENT_TYPES",
    "FINALIZATION_EVENT_VERSION",
    "INJECTION_RENDERED_EVENT",
    "FinalizationEventRef",
    "FinalizationEventV1Error",
    "build_injection_rendered_event",
    "finalization_event_ref",
    "finalization_event_payload_schema",
    "finalization_event_stream_id",
    "injection_rendered_event_id",
    "parse_injection_rendered_event",
]
