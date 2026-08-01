from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Literal, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError


EVENT_PROTOCOL_VERSION = "tbm.event.v1"
EVENT_JSON_MAX_BYTES = 1024 * 1024
EVENT_JSON_MAX_DEPTH = 32
EVENT_JSON_MAX_NODES = 10_000
EVENT_PAYLOAD_MAX_BYTES = 512 * 1024
EVENT_PAYLOAD_MAX_DEPTH = 24
EVENT_PAYLOAD_MAX_NODES = 8_192
EVENT_MAX_ARTIFACT_REFS = 128
EVENT_MAX_VERSION = 9_223_372_036_854_775_807
EVENT_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024

EventActorType = Literal["principal", "agent_client", "service", "worker"]
EventKind = Literal["domain", "observation"]
EventOrigin = Literal["native", "imported"]
EventClassification = Literal[
    "public", "internal", "confidential", "restricted"
]
EventEvidenceQuality = Literal[
    "exact", "verified", "observed", "legacy_partial", "unknown"
]
EventArtifactAvailability = Literal[
    "available", "pending", "unavailable", "erased"
]

_ACTOR_TYPES = frozenset({"principal", "agent_client", "service", "worker"})
_EVENT_KINDS = frozenset({"domain", "observation"})
_EVENT_ORIGINS = frozenset({"native", "imported"})
_CLASSIFICATIONS = frozenset(
    {"public", "internal", "confidential", "restricted"}
)
_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_EVIDENCE_QUALITIES = frozenset(
    {"exact", "verified", "observed", "legacy_partial", "unknown"}
)
_ARTIFACT_AVAILABILITY = frozenset(
    {"available", "pending", "unavailable", "erased"}
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_EVENT_TYPE_RE = re.compile(
    r"^tbm\.[a-z0-9][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+$"
)
_PAYLOAD_SCHEMA_RE = re.compile(
    r"^tbm\.[a-z0-9][a-z0-9_.-]*\.v[1-9][0-9]*$"
)
_IDENTIFIER_MAX_CHARS = 128
_CODE_MAX_CHARS = 256
_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "authorizationheader",
        "bearertoken",
        "clientsecret",
        "credential",
        "credentials",
        "passphrase",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "token",
        "xapikey",
    }
)
_ARTIFACT_REF_FIELDS = frozenset(
    {
        "artifact_id",
        "content_sha256",
        "media_type",
        "size_bytes",
        "classification",
        "retention_policy_id",
        "encryption_key_id",
        "availability",
    }
)
_SOURCE_FIELDS = frozenset(
    {"source_system", "source_record_id", "evidence_quality", "observed_at"}
)
_EVENT_FIELDS = frozenset(
    {
        "protocol_version",
        "event_id",
        "event_sha256",
        "event_type",
        "event_version",
        "event_kind",
        "origin",
        "source",
        "stream_id",
        "stream_type",
        "stream_version",
        "global_position",
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "principal_id",
        "agent_client_id",
        "actor_type",
        "actor_id",
        "authorization_decision_id",
        "request_id",
        "idempotency_key_sha256",
        "request_sha256",
        "correlation_id",
        "causation_id",
        "occurred_at",
        "recorded_at",
        "producer",
        "producer_version",
        "payload_schema",
        "payload_sha256",
        "previous_stream_event_sha256",
        "classification",
        "retention_policy_id",
        "artifact_refs",
        "payload",
    }
)


class EventV1ContractError(V3ContractError):
    """Stable failure for the canonical event version-1 contract."""


@dataclass(frozen=True)
class EventTrustedContext:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    principal_id: str
    agent_client_id: str
    actor_type: EventActorType
    actor_id: str
    authorization_decision_id: str

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "principal_id",
            "agent_client_id",
            "actor_id",
            "authorization_decision_id",
        ):
            _identifier(getattr(self, name), name)
        _enum(self.actor_type, _ACTOR_TYPES, "actor_type")


@dataclass(frozen=True)
class EventSource:
    source_system: str
    source_record_id: str
    evidence_quality: EventEvidenceQuality
    observed_at: str

    def __post_init__(self) -> None:
        _identifier(self.source_system, "source_system")
        _identifier(self.source_record_id, "source_record_id")
        _enum(
            self.evidence_quality,
            _EVIDENCE_QUALITIES,
            "evidence_quality",
        )
        _timestamp(self.observed_at, "observed_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "evidence_quality": self.evidence_quality,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class EventArtifactRef:
    artifact_id: str
    content_sha256: str
    media_type: str
    size_bytes: int
    classification: EventClassification
    retention_policy_id: str
    encryption_key_id: str | None
    availability: EventArtifactAvailability

    def __post_init__(self) -> None:
        _digest(self.content_sha256, "content_sha256")
        expected_id = "artifact_sha256_" + self.content_sha256.removeprefix(
            "sha256:"
        )
        if (
            type(self.artifact_id) is not str
            or _ARTIFACT_ID_RE.fullmatch(self.artifact_id) is None
            or self.artifact_id != expected_id
        ):
            _invalid("artifact_id must be derived from content_sha256")
        _code(self.media_type, "media_type")
        if (
            type(self.size_bytes) is not int
            or not 0 <= self.size_bytes <= EVENT_ARTIFACT_MAX_BYTES
        ):
            _invalid("size_bytes must be a bounded non-negative integer")
        _enum(self.classification, _CLASSIFICATIONS, "classification")
        _identifier(self.retention_policy_id, "retention_policy_id")
        _optional_identifier(self.encryption_key_id, "encryption_key_id")
        if (
            self.classification in {"confidential", "restricted"}
            and self.encryption_key_id is None
        ):
            _invalid("protected artifact references require encryption_key_id")
        _enum(self.availability, _ARTIFACT_AVAILABILITY, "availability")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
            "encryption_key_id": self.encryption_key_id,
            "availability": self.availability,
        }


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    event_sha256: str
    event_type: str
    event_version: int
    event_kind: EventKind
    origin: EventOrigin
    source: EventSource | None
    stream_id: str
    stream_type: str
    stream_version: int
    global_position: int
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    principal_id: str
    agent_client_id: str
    actor_type: EventActorType
    actor_id: str
    authorization_decision_id: str
    request_id: str
    idempotency_key_sha256: str
    request_sha256: str
    correlation_id: str
    causation_id: str | None
    occurred_at: str | None
    recorded_at: str
    producer: str
    producer_version: str
    payload_schema: str
    payload_sha256: str
    previous_stream_event_sha256: str | None
    classification: EventClassification
    retention_policy_id: str
    artifact_refs: tuple[EventArtifactRef, ...]
    payload: Mapping[str, object]
    protocol_version: str = EVENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != EVENT_PROTOCOL_VERSION:
            _invalid(f"protocol_version must be {EVENT_PROTOCOL_VERSION}")
        _event_id(self.event_id, "event_id")
        _digest(self.event_sha256, "event_sha256")
        if type(self.event_type) is not str or not _EVENT_TYPE_RE.fullmatch(
            self.event_type
        ):
            _invalid("event_type must be a canonical extensible event type")
        _positive_version(self.event_version, "event_version")
        _enum(self.event_kind, _EVENT_KINDS, "event_kind")
        _enum(self.origin, _EVENT_ORIGINS, "origin")
        if self.source is not None and type(self.source) is not EventSource:
            _invalid("source must be exactly EventSource or null")
        if self.origin == "imported" and self.source is None:
            _invalid("imported events require source evidence")
        if self.event_kind == "observation" and self.source is None:
            _invalid("observation events require source evidence")
        if (
            self.origin == "native"
            and self.event_kind == "domain"
            and self.source is not None
        ):
            _invalid("native domain events cannot claim import source evidence")
        for name in (
            "stream_id",
            "stream_type",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "principal_id",
            "agent_client_id",
            "actor_id",
            "authorization_decision_id",
            "request_id",
            "correlation_id",
            "retention_policy_id",
        ):
            _identifier(getattr(self, name), name)
        _positive_version(self.stream_version, "stream_version")
        _positive_version(self.global_position, "global_position")
        _enum(self.actor_type, _ACTOR_TYPES, "actor_type")
        _digest(self.idempotency_key_sha256, "idempotency_key_sha256")
        _digest(self.request_sha256, "request_sha256")
        if self.causation_id is not None:
            _event_id(self.causation_id, "causation_id")
            if self.causation_id == self.event_id:
                _invalid("causation_id cannot reference the event itself")
        if self.occurred_at is None:
            if self.source is None:
                _invalid("missing occurred_at requires source observation evidence")
        else:
            _timestamp(self.occurred_at, "occurred_at")
        recorded = _timestamp(self.recorded_at, "recorded_at")
        if self.occurred_at is not None:
            occurred = parse_rfc3339(self.occurred_at)
            if occurred > recorded:
                _invalid("occurred_at cannot follow recorded_at")
        if self.source is not None:
            if parse_rfc3339(self.source.observed_at) > recorded:
                _invalid("source observed_at cannot follow recorded_at")
        _identifier(self.producer, "producer")
        _code(self.producer_version, "producer_version")
        if type(self.payload_schema) is not str or not _PAYLOAD_SCHEMA_RE.fullmatch(
            self.payload_schema
        ):
            _invalid("payload_schema must be a versioned tbm schema name")
        _digest(self.payload_sha256, "payload_sha256")
        if self.stream_version == 1:
            if self.previous_stream_event_sha256 is not None:
                _invalid("first stream event cannot name a previous event hash")
        else:
            _digest(
                self.previous_stream_event_sha256,
                "previous_stream_event_sha256",
            )
        _enum(self.classification, _CLASSIFICATIONS, "classification")
        _artifact_ref_tuple(self.artifact_refs)
        for reference in self.artifact_refs:
            if (
                _CLASSIFICATION_RANK[reference.classification]
                > _CLASSIFICATION_RANK[self.classification]
            ):
                _invalid(
                    "event classification cannot be lower than an artifact reference"
                )
        payload = _bounded_payload_copy(self.payload)
        object.__setattr__(self, "payload", _freeze_json(payload))
        if self.payload_sha256 != event_payload_sha256(payload):
            _invalid("payload_sha256 does not match canonical payload")
        expected_event_sha256 = canonical_event_sha256(
            self.to_dict(include_event_sha256=False)
        )
        if self.event_sha256 != expected_event_sha256:
            _invalid("event_sha256 does not match canonical event envelope")

    def to_dict(self, *, include_event_sha256: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_version": self.event_version,
            "event_kind": self.event_kind,
            "origin": self.origin,
            "source": None if self.source is None else self.source.to_dict(),
            "stream_id": self.stream_id,
            "stream_type": self.stream_type,
            "stream_version": self.stream_version,
            "global_position": self.global_position,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "principal_id": self.principal_id,
            "agent_client_id": self.agent_client_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "authorization_decision_id": self.authorization_decision_id,
            "request_id": self.request_id,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "request_sha256": self.request_sha256,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "producer": self.producer,
            "producer_version": self.producer_version,
            "payload_schema": self.payload_schema,
            "payload_sha256": self.payload_sha256,
            "previous_stream_event_sha256": self.previous_stream_event_sha256,
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "payload": _thaw_json(self.payload),
        }
        if include_event_sha256:
            value["event_sha256"] = self.event_sha256
        return value


def event_payload_sha256(payload: Mapping[str, object]) -> str:
    canonical = _bounded_payload_copy(payload)
    return _sha256(_canonical_json_bytes(canonical))


def canonical_event_sha256(unsigned_event: Mapping[str, object]) -> str:
    obj = dict(unsigned_event)
    if "event_sha256" in obj:
        _invalid("unsigned event input cannot contain event_sha256")
    encoded = _canonical_json_bytes(obj)
    return _sha256(b"tbm.event.v1\x00" + encoded)


def build_canonical_event(
    *,
    event_id: str,
    event_type: str,
    event_version: int,
    event_kind: EventKind,
    origin: EventOrigin,
    source: EventSource | None,
    stream_id: str,
    stream_type: str,
    stream_version: int,
    global_position: int,
    trusted_context: EventTrustedContext,
    request_id: str,
    idempotency_key_sha256: str,
    request_sha256: str,
    correlation_id: str,
    causation_id: str | None,
    occurred_at: str | None,
    recorded_at: str,
    producer: str,
    producer_version: str,
    payload_schema: str,
    previous_stream_event_sha256: str | None,
    classification: EventClassification,
    retention_policy_id: str,
    artifact_refs: tuple[EventArtifactRef, ...],
    payload: Mapping[str, object],
) -> CanonicalEvent:
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if source is not None and type(source) is not EventSource:
        _invalid("source must be exactly EventSource or null")
    if (
        type(artifact_refs) is not tuple
        or len(artifact_refs) > EVENT_MAX_ARTIFACT_REFS
        or any(type(item) is not EventArtifactRef for item in artifact_refs)
    ):
        _invalid("artifact_refs must be a bounded tuple of EventArtifactRef")
    artifact_ids = tuple(item.artifact_id for item in artifact_refs)
    if len(artifact_ids) != len(set(artifact_ids)):
        _invalid("artifact_refs must be unique")
    canonical_payload = _bounded_payload_copy(payload)
    canonical_refs = tuple(
        sorted(artifact_refs, key=lambda item: item.artifact_id)
    )
    canonical_occurred = (
        None
        if occurred_at is None
        else _canonical_timestamp(occurred_at, "occurred_at")
    )
    canonical_recorded = _canonical_timestamp(recorded_at, "recorded_at")
    payload_sha256 = event_payload_sha256(canonical_payload)
    unsigned: dict[str, object] = {
        "protocol_version": EVENT_PROTOCOL_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "event_version": event_version,
        "event_kind": event_kind,
        "origin": origin,
        "source": None if source is None else source.to_dict(),
        "stream_id": stream_id,
        "stream_type": stream_type,
        "stream_version": stream_version,
        "global_position": global_position,
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
        "request_id": request_id,
        "idempotency_key_sha256": idempotency_key_sha256,
        "request_sha256": request_sha256,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "occurred_at": canonical_occurred,
        "recorded_at": canonical_recorded,
        "producer": producer,
        "producer_version": producer_version,
        "payload_schema": payload_schema,
        "payload_sha256": payload_sha256,
        "previous_stream_event_sha256": previous_stream_event_sha256,
        "classification": classification,
        "retention_policy_id": retention_policy_id,
        "artifact_refs": [item.to_dict() for item in canonical_refs],
        "payload": canonical_payload,
    }
    return CanonicalEvent(
        event_id=event_id,
        event_sha256=canonical_event_sha256(unsigned),
        event_type=event_type,
        event_version=event_version,
        event_kind=event_kind,
        origin=origin,
        source=source,
        stream_id=stream_id,
        stream_type=stream_type,
        stream_version=stream_version,
        global_position=global_position,
        organization_id=trusted_context.organization_id,
        tenant_id=trusted_context.tenant_id,
        repository_id=trusted_context.repository_id,
        environment_id=trusted_context.environment_id,
        principal_id=trusted_context.principal_id,
        agent_client_id=trusted_context.agent_client_id,
        actor_type=trusted_context.actor_type,
        actor_id=trusted_context.actor_id,
        authorization_decision_id=trusted_context.authorization_decision_id,
        request_id=request_id,
        idempotency_key_sha256=idempotency_key_sha256,
        request_sha256=request_sha256,
        correlation_id=correlation_id,
        causation_id=causation_id,
        occurred_at=canonical_occurred,
        recorded_at=canonical_recorded,
        producer=producer,
        producer_version=producer_version,
        payload_schema=payload_schema,
        payload_sha256=payload_sha256,
        previous_stream_event_sha256=previous_stream_event_sha256,
        classification=classification,
        retention_policy_id=retention_policy_id,
        artifact_refs=canonical_refs,
        payload=canonical_payload,
    )


def verify_event_parent(
    event: CanonicalEvent, parent: CanonicalEvent | None
) -> None:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if event.stream_version == 1:
        if parent is not None:
            _invalid("first stream event cannot have a parent")
        return
    if type(parent) is not CanonicalEvent:
        _invalid("non-first stream event requires its parent")
    if event.previous_stream_event_sha256 != parent.event_sha256:
        _invalid("previous_stream_event_sha256 does not match parent")
    if event.stream_version != parent.stream_version + 1:
        _invalid("stream_version must advance by one")
    if event.global_position <= parent.global_position:
        _invalid("global_position must advance after parent")
    for name in (
        "stream_id",
        "stream_type",
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
    ):
        if getattr(event, name) != getattr(parent, name):
            _invalid(f"parent {name} does not match")
    if parse_rfc3339(event.recorded_at) < parse_rfc3339(parent.recorded_at):
        _invalid("recorded_at precedes parent")


def verify_event_trusted_context(
    event: CanonicalEvent, trusted_context: EventTrustedContext
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
        "authorization_decision_id",
    ):
        if getattr(event, name) != getattr(trusted_context, name):
            _invalid(f"trusted context {name} does not match event")


def dumps_canonical_event(event: CanonicalEvent) -> str:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    return _canonical_json_bytes(event.to_dict()).decode("utf-8")


def loads_canonical_event(data: str | bytes | bytearray) -> CanonicalEvent:
    return parse_canonical_event(_loads_object(data))


def parse_canonical_event(value: Mapping[str, object]) -> CanonicalEvent:
    obj = _strict_object(value, _EVENT_FIELDS, "CanonicalEvent")
    raw_source = obj["source"]
    source = None if raw_source is None else _parse_source(raw_source)
    raw_refs = obj["artifact_refs"]
    if type(raw_refs) is not list:
        _invalid("artifact_refs must be an array")
    if len(raw_refs) > EVENT_MAX_ARTIFACT_REFS:
        _invalid("artifact_refs exceeds the item limit")
    artifact_refs = tuple(_parse_artifact_ref(item) for item in raw_refs)
    raw_payload = obj["payload"]
    if not isinstance(raw_payload, Mapping):
        _invalid("payload must be a JSON object")
    return CanonicalEvent(
        event_id=_as_str(obj["event_id"], "event_id"),
        event_sha256=_as_str(obj["event_sha256"], "event_sha256"),
        event_type=_as_str(obj["event_type"], "event_type"),
        event_version=_as_int(obj["event_version"], "event_version"),
        event_kind=cast(EventKind, obj["event_kind"]),
        origin=cast(EventOrigin, obj["origin"]),
        source=source,
        stream_id=_as_str(obj["stream_id"], "stream_id"),
        stream_type=_as_str(obj["stream_type"], "stream_type"),
        stream_version=_as_int(obj["stream_version"], "stream_version"),
        global_position=_as_int(obj["global_position"], "global_position"),
        organization_id=_as_str(obj["organization_id"], "organization_id"),
        tenant_id=_as_str(obj["tenant_id"], "tenant_id"),
        repository_id=_as_str(obj["repository_id"], "repository_id"),
        environment_id=_as_str(obj["environment_id"], "environment_id"),
        principal_id=_as_str(obj["principal_id"], "principal_id"),
        agent_client_id=_as_str(
            obj["agent_client_id"], "agent_client_id"
        ),
        actor_type=cast(EventActorType, obj["actor_type"]),
        actor_id=_as_str(obj["actor_id"], "actor_id"),
        authorization_decision_id=_as_str(
            obj["authorization_decision_id"],
            "authorization_decision_id",
        ),
        request_id=_as_str(obj["request_id"], "request_id"),
        idempotency_key_sha256=_as_str(
            obj["idempotency_key_sha256"], "idempotency_key_sha256"
        ),
        request_sha256=_as_str(obj["request_sha256"], "request_sha256"),
        correlation_id=_as_str(obj["correlation_id"], "correlation_id"),
        causation_id=_as_optional_str(obj["causation_id"], "causation_id"),
        occurred_at=_as_optional_str(obj["occurred_at"], "occurred_at"),
        recorded_at=_as_str(obj["recorded_at"], "recorded_at"),
        producer=_as_str(obj["producer"], "producer"),
        producer_version=_as_str(
            obj["producer_version"], "producer_version"
        ),
        payload_schema=_as_str(obj["payload_schema"], "payload_schema"),
        payload_sha256=_as_str(obj["payload_sha256"], "payload_sha256"),
        previous_stream_event_sha256=_as_optional_str(
            obj["previous_stream_event_sha256"],
            "previous_stream_event_sha256",
        ),
        classification=cast(EventClassification, obj["classification"]),
        retention_policy_id=_as_str(
            obj["retention_policy_id"], "retention_policy_id"
        ),
        artifact_refs=artifact_refs,
        payload=cast(Mapping[str, object], raw_payload),
        protocol_version=_as_str(
            obj["protocol_version"], "protocol_version"
        ),
    )


def _parse_source(value: object) -> EventSource:
    if not isinstance(value, Mapping):
        _invalid("source must be an object or null")
    obj = _strict_object(
        cast(Mapping[str, object], value), _SOURCE_FIELDS, "EventSource"
    )
    return EventSource(
        source_system=_as_str(obj["source_system"], "source_system"),
        source_record_id=_as_str(
            obj["source_record_id"], "source_record_id"
        ),
        evidence_quality=cast(
            EventEvidenceQuality, obj["evidence_quality"]
        ),
        observed_at=_as_str(obj["observed_at"], "observed_at"),
    )


def _parse_artifact_ref(value: object) -> EventArtifactRef:
    if not isinstance(value, Mapping):
        _invalid("EventArtifactRef must be an object")
    obj = _strict_object(
        cast(Mapping[str, object], value),
        _ARTIFACT_REF_FIELDS,
        "EventArtifactRef",
    )
    return EventArtifactRef(
        artifact_id=_as_str(obj["artifact_id"], "artifact_id"),
        content_sha256=_as_str(obj["content_sha256"], "content_sha256"),
        media_type=_as_str(obj["media_type"], "media_type"),
        size_bytes=_as_int(obj["size_bytes"], "size_bytes"),
        classification=cast(
            EventClassification, obj["classification"]
        ),
        retention_policy_id=_as_str(
            obj["retention_policy_id"], "retention_policy_id"
        ),
        encryption_key_id=_as_optional_str(
            obj["encryption_key_id"], "encryption_key_id"
        ),
        availability=cast(
            EventArtifactAvailability, obj["availability"]
        ),
    )


def _loads_object(data: str | bytes | bytearray) -> Mapping[str, object]:
    try:
        if isinstance(data, (bytes, bytearray)):
            text = decode_bounded_utf8(
                bytes(data),
                max_bytes=EVENT_JSON_MAX_BYTES,
                description="canonical event JSON",
            )
        elif type(data) is str:
            text = data
            if len(text.encode("utf-8")) > EVENT_JSON_MAX_BYTES:
                raise ValueError("canonical event JSON exceeds byte limit")
        else:
            raise TypeError(
                "canonical event JSON must be str, bytes, or bytearray"
            )
        value = parse_bounded_json(
            text,
            description="canonical event",
            max_depth=EVENT_JSON_MAX_DEPTH,
            max_nodes=EVENT_JSON_MAX_NODES,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise EventV1ContractError(
            "TBM_EVENT_INVALID_JSON", str(error)
        ) from error
    if not isinstance(value, Mapping):
        _invalid("canonical event JSON must be an object")
    return cast(Mapping[str, object], value)


def _strict_object(
    value: Mapping[str, object], fields: frozenset[str], label: str
) -> dict[str, object]:
    obj = dict(value)
    if any(type(key) is not str for key in obj):
        _invalid(f"{label} keys must be strings")
    if set(obj) != fields:
        _invalid(f"{label} fields do not match contract")
    return obj


def _bounded_payload_copy(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        _invalid("payload must be a JSON object")
    copied = _copy_json(
        payload,
        max_nodes=EVENT_PAYLOAD_MAX_NODES,
        max_depth=EVENT_PAYLOAD_MAX_DEPTH,
    )
    if type(copied) is not dict:
        _invalid("payload must be a JSON object")
    encoded = _canonical_json_bytes(copied)
    if len(encoded) > EVENT_PAYLOAD_MAX_BYTES:
        _invalid("payload exceeds canonical byte limit")
    _reject_secret_metadata(copied)
    return cast(dict[str, object], copied)


def _copy_json(value: object, *, max_nodes: int, max_depth: int) -> object:
    active: set[int] = set()
    node_count = 0

    def copy(item: object, depth: int) -> object:
        nonlocal node_count
        if depth > max_depth:
            _invalid("payload exceeds maximum depth")
        node_count += 1
        if node_count > max_nodes:
            _invalid("payload exceeds maximum node count")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                _invalid("payload contains a cycle")
            if len(item) > max_nodes - node_count:
                _invalid("payload exceeds maximum node count")
            active.add(identity)
            try:
                result: dict[str, object] = {}
                for key, child in item.items():
                    if (
                        type(key) is not str
                        or not key
                        or len(key) > _CODE_MAX_CHARS
                    ):
                        _invalid("payload keys must be bounded non-empty strings")
                    _utf8(key, "payload key")
                    result[key] = copy(child, depth + 1)
                return result
            finally:
                active.remove(identity)
        if type(item) in {list, tuple}:
            identity = id(item)
            if identity in active:
                _invalid("payload contains a cycle")
            if len(item) > max_nodes - node_count:
                _invalid("payload exceeds maximum node count")
            active.add(identity)
            try:
                return [copy(child, depth + 1) for child in item]
            finally:
                active.remove(identity)
        if item is None or type(item) in {bool, int}:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                _invalid("payload contains a non-finite number")
            return item
        if type(item) is str:
            _utf8(item, "payload string")
            return item
        _invalid("payload contains a non-JSON value")

    return copy(value, 0)


def _reject_secret_metadata(payload: Mapping[str, object]) -> None:
    pending: list[object] = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
                if normalized in _FORBIDDEN_SECRET_KEYS:
                    _invalid("payload contains forbidden secret metadata key")
                pending.append(child)
        elif type(value) in {list, tuple}:
            pending.extend(value)


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _artifact_ref_tuple(values: tuple[EventArtifactRef, ...]) -> None:
    if type(values) is not tuple or len(values) > EVENT_MAX_ARTIFACT_REFS:
        _invalid("artifact_refs must be a bounded tuple")
    if any(type(item) is not EventArtifactRef for item in values):
        _invalid("artifact_refs must contain EventArtifactRef values")
    order = tuple(item.artifact_id for item in values)
    if order != tuple(sorted(order)) or len(order) != len(set(order)):
        _invalid("artifact_refs must be sorted and unique")


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
        raise EventV1ContractError(
            "TBM_EVENT_NON_CANONICAL_JSON",
            "value cannot be encoded as finite canonical JSON",
        ) from error


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _IDENTIFIER_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _invalid(f"{name} must be a bounded identifier")
    _utf8(value, name)


def _optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _event_id(value: object, name: str) -> None:
    if type(value) is not str or _EVENT_ID_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a canonical event identifier")


def _code(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _CODE_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _invalid(f"{name} must be a bounded code")
    _utf8(value, name)


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a canonical sha256 digest")


def _positive_version(value: object, name: str) -> None:
    if (
        type(value) is not int
        or not 1 <= value <= EVENT_MAX_VERSION
    ):
        _invalid(f"{name} must be a bounded positive integer")


def _timestamp(value: object, name: str):
    if type(value) is not str:
        _invalid(f"{name} must be a canonical RFC3339 timestamp")
    try:
        parsed = parse_rfc3339(value)
    except (TypeError, ValueError) as error:
        _invalid(f"{name} must be a canonical RFC3339 timestamp: {error}")
    if canonical_rfc3339(value) != value:
        _invalid(f"{name} must be canonical RFC3339")
    return parsed


def _canonical_timestamp(value: object, name: str) -> str:
    if type(value) is not str:
        _invalid(f"{name} must be an RFC3339 timestamp")
    try:
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        _invalid(f"{name} must be an RFC3339 timestamp: {error}")


def _enum(value: object, allowed: frozenset[str], name: str) -> None:
    if type(value) is not str or value not in allowed:
        _invalid(f"{name} is not supported")


def _utf8(value: str, name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EventV1ContractError(
            "TBM_EVENT_INVALID", f"{name} must be valid UTF-8"
        ) from error


def _as_str(value: object, name: str) -> str:
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return value


def _as_optional_str(value: object, name: str) -> str | None:
    if value is not None and type(value) is not str:
        _invalid(f"{name} must be a string or null")
    return cast(str | None, value)


def _as_int(value: object, name: str) -> int:
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    return value


def _invalid(message: str) -> NoReturn:
    raise EventV1ContractError("TBM_EVENT_INVALID", message)


__all__ = [
    "EVENT_ARTIFACT_MAX_BYTES",
    "EVENT_JSON_MAX_BYTES",
    "EVENT_JSON_MAX_DEPTH",
    "EVENT_JSON_MAX_NODES",
    "EVENT_MAX_ARTIFACT_REFS",
    "EVENT_MAX_VERSION",
    "EVENT_PAYLOAD_MAX_BYTES",
    "EVENT_PAYLOAD_MAX_DEPTH",
    "EVENT_PAYLOAD_MAX_NODES",
    "EVENT_PROTOCOL_VERSION",
    "CanonicalEvent",
    "EventActorType",
    "EventArtifactAvailability",
    "EventArtifactRef",
    "EventClassification",
    "EventEvidenceQuality",
    "EventKind",
    "EventOrigin",
    "EventSource",
    "EventTrustedContext",
    "EventV1ContractError",
    "build_canonical_event",
    "canonical_event_sha256",
    "dumps_canonical_event",
    "event_payload_sha256",
    "loads_canonical_event",
    "parse_canonical_event",
    "verify_event_parent",
    "verify_event_trusted_context",
]
