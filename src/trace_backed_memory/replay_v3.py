from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Literal, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError, canonical_sha256
from .gate_session_v3 import GATE_SESSION_MAX_MEMORY_REVISIONS
from .policy import (
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)


REPLAY_CONTRACT_VERSION = "tbm.replay.v3"
REPLAY_JSON_MAX_BYTES = 1024 * 1024
REPLAY_JSON_MAX_DEPTH = 32
REPLAY_JSON_MAX_NODES = 10_000
ARTIFACT_MAX_BYTES = 64 * 1024 * 1024
INJECTION_ARTIFACT_MAX_BYTES = 1024 * 1024
INJECTION_ARTIFACT_MEDIA_TYPE = "text/plain; charset=utf-8"

DataClassification = Literal[
    "public",
    "internal",
    "confidential",
    "restricted",
]
ReplayCompleteness = Literal["complete", "legacy_partial"]
ReplayComponentName = Literal[
    "retrieval_snapshot",
    "system_gate_evaluation",
    "semantic_gate_prompt",
    "semantic_gate_response",
    "ancestry_evidence",
    "policy_bundle",
    "renderer",
    "injection_artifact",
]

REPLAY_COMPONENT_NAMES: tuple[ReplayComponentName, ...] = (
    "retrieval_snapshot",
    "system_gate_evaluation",
    "semantic_gate_prompt",
    "semantic_gate_response",
    "ancestry_evidence",
    "policy_bundle",
    "renderer",
    "injection_artifact",
)

_CLASSIFICATIONS = {
    "public",
    "internal",
    "confidential",
    "restricted",
}
_COMPLETENESS_VALUES = {"complete", "legacy_partial"}
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ARTIFACT_ID_RE = re.compile(r"artifact_sha256_[0-9a-f]{64}")
_INJECTION_FIELDS = frozenset(
    {
        "contract_version",
        "artifact_kind",
        "artifact",
        "session_id",
        "decision_id",
        "usage_decision_id",
        "memory_revision_ids",
        "renderer_id",
        "renderer_version",
        "policy_bundle_sha256",
        "rendered_at",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id",
        "content_sha256",
        "size_bytes",
        "media_type",
        "classification",
        "created_at",
        "encryption_key_id",
        "redaction_policy_id",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "contract_version",
        "manifest_kind",
        "manifest_sha256",
        "session_id",
        "decision_id",
        "usage_decision_id",
        "components",
        "injection_artifact_id",
        "completeness",
        "missing_components",
        "created_at",
    }
)


class ReplayContractError(V3ContractError):
    """Stable failure for malformed content-addressed replay contracts."""


@dataclass(frozen=True)
class ContentAddressedArtifact:
    """Storage-neutral metadata for bytes identified by their SHA-256."""

    artifact_id: str
    content_sha256: str
    size_bytes: int
    media_type: str
    classification: DataClassification
    created_at: str
    encryption_key_id: str | None = None
    redaction_policy_id: str | None = None

    def __post_init__(self) -> None:
        _digest(self.content_sha256, "content_sha256")
        expected_id = artifact_id_from_sha256(self.content_sha256)
        if self.artifact_id != expected_id:
            _invalid("artifact_id must be derived from content_sha256")
        if (
            type(self.size_bytes) is not int
            or self.size_bytes < 0
            or self.size_bytes > ARTIFACT_MAX_BYTES
        ):
            _invalid(
                f"size_bytes must be an integer from 0 to "
                f"{ARTIFACT_MAX_BYTES}"
            )
        _required_metadata(self.media_type, "media_type")
        if (
            type(self.classification) is not str
            or self.classification not in _CLASSIFICATIONS
        ):
            _invalid("classification must be a supported data class")
        _timestamp(self.created_at, "created_at")
        _optional_identifier(self.encryption_key_id, "encryption_key_id")
        _optional_identifier(
            self.redaction_policy_id,
            "redaction_policy_id",
        )
        if (
            self.classification in {"confidential", "restricted"}
            and self.encryption_key_id is None
        ):
            _invalid(
                "confidential and restricted artifacts require "
                "encryption_key_id"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "classification": self.classification,
            "created_at": canonical_rfc3339(self.created_at),
            "encryption_key_id": self.encryption_key_id,
            "redaction_policy_id": self.redaction_policy_id,
        }


@dataclass(frozen=True)
class StoredReplayArtifact:
    """Validated replay descriptor paired with its exact stored bytes."""

    artifact: ContentAddressedArtifact
    content: bytes


@dataclass(frozen=True)
class InjectionArtifact:
    """Exact rendered injection metadata linked to one finalized decision."""

    artifact: ContentAddressedArtifact
    session_id: str
    decision_id: str
    usage_decision_id: str
    memory_revision_ids: tuple[str, ...]
    renderer_id: str
    renderer_version: str
    policy_bundle_sha256: str
    rendered_at: str
    artifact_kind: Literal["injection"] = "injection"
    contract_version: str = REPLAY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPLAY_CONTRACT_VERSION:
            _invalid(
                "contract_version must be "
                f"{REPLAY_CONTRACT_VERSION}"
            )
        if self.artifact_kind != "injection":
            _invalid("artifact_kind must be injection")
        if type(self.artifact) is not ContentAddressedArtifact:
            _invalid("artifact must be exactly ContentAddressedArtifact")
        if self.artifact.media_type != INJECTION_ARTIFACT_MEDIA_TYPE:
            _invalid(
                "injection artifact media_type must be "
                f"{INJECTION_ARTIFACT_MEDIA_TYPE}"
            )
        if self.artifact.size_bytes > INJECTION_ARTIFACT_MAX_BYTES:
            _invalid(
                "injection artifact exceeds maximum size of "
                f"{INJECTION_ARTIFACT_MAX_BYTES} bytes"
            )
        for field_name in (
            "session_id",
            "decision_id",
            "usage_decision_id",
            "renderer_id",
        ):
            _required_identifier(getattr(self, field_name), field_name)
        _identifier_tuple(
            self.memory_revision_ids,
            "memory_revision_ids",
            max_items=GATE_SESSION_MAX_MEMORY_REVISIONS,
        )
        _required_metadata(self.renderer_version, "renderer_version")
        _digest(self.policy_bundle_sha256, "policy_bundle_sha256")
        rendered = _timestamp(self.rendered_at, "rendered_at")
        created = _timestamp(self.artifact.created_at, "artifact.created_at")
        if rendered != created:
            _invalid("rendered_at must equal artifact created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "artifact_kind": self.artifact_kind,
            "artifact": self.artifact.to_dict(),
            "session_id": self.session_id,
            "decision_id": self.decision_id,
            "usage_decision_id": self.usage_decision_id,
            "memory_revision_ids": list(self.memory_revision_ids),
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "policy_bundle_sha256": self.policy_bundle_sha256,
            "rendered_at": canonical_rfc3339(self.rendered_at),
        }


@dataclass(frozen=True)
class DecisionReplayManifest:
    """Content-bound inputs required to replay one finalized decision."""

    manifest_sha256: str
    session_id: str
    decision_id: str
    usage_decision_id: str
    components: tuple[tuple[ReplayComponentName, str | None], ...]
    injection_artifact_id: str | None
    completeness: ReplayCompleteness
    missing_components: tuple[ReplayComponentName, ...]
    created_at: str
    manifest_kind: Literal["decision_replay"] = "decision_replay"
    contract_version: str = REPLAY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPLAY_CONTRACT_VERSION:
            _invalid(
                "contract_version must be "
                f"{REPLAY_CONTRACT_VERSION}"
            )
        if self.manifest_kind != "decision_replay":
            _invalid("manifest_kind must be decision_replay")
        _digest(self.manifest_sha256, "manifest_sha256")
        for field_name in ("session_id", "decision_id", "usage_decision_id"):
            _required_identifier(getattr(self, field_name), field_name)
        _validate_components(self.components)
        _optional_artifact_id(
            self.injection_artifact_id,
            "injection_artifact_id",
        )
        if (
            type(self.completeness) is not str
            or self.completeness not in _COMPLETENESS_VALUES
        ):
            _invalid("completeness must be complete or legacy_partial")
        if type(self.missing_components) is not tuple:
            _invalid("missing_components must be a tuple")
        if any(
            type(item) is not str or item not in REPLAY_COMPONENT_NAMES
            for item in self.missing_components
        ):
            _invalid(
                "missing_components entries must be replay component names"
            )
        if len(set(self.missing_components)) != len(self.missing_components):
            _invalid("missing_components must not contain duplicates")
        component_map = dict(self.components)
        expected_missing = tuple(
            name
            for name in REPLAY_COMPONENT_NAMES
            if component_map[name] is None
        )
        if self.missing_components != expected_missing:
            _invalid(
                "missing_components must exactly identify null components"
            )
        if self.completeness == "complete" and expected_missing:
            _invalid("complete replay manifest cannot omit components")
        if self.completeness == "legacy_partial" and not expected_missing:
            _invalid("legacy_partial replay manifest must omit a component")
        injection_sha256 = component_map["injection_artifact"]
        if (self.injection_artifact_id is None) != (
            injection_sha256 is None
        ):
            _invalid(
                "injection_artifact_id and injection_artifact component "
                "must be recorded together"
            )
        if (
            injection_sha256 is not None
            and self.injection_artifact_id
            != artifact_id_from_sha256(injection_sha256)
        ):
            _invalid(
                "injection_artifact_id must match injection_artifact "
                "component"
            )
        _timestamp(self.created_at, "created_at")
        expected_manifest_sha256 = canonical_sha256(
            self._unsigned_dict()
        )
        if self.manifest_sha256 != expected_manifest_sha256:
            raise ReplayContractError(
                "TBM_REPLAY_HASH_MISMATCH",
                "manifest_sha256 does not match canonical manifest content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "manifest_kind": self.manifest_kind,
            "session_id": self.session_id,
            "decision_id": self.decision_id,
            "usage_decision_id": self.usage_decision_id,
            "components": dict(self.components),
            "injection_artifact_id": self.injection_artifact_id,
            "completeness": self.completeness,
            "missing_components": list(self.missing_components),
            "created_at": canonical_rfc3339(self.created_at),
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._unsigned_dict()
        payload["manifest_sha256"] = self.manifest_sha256
        return payload


def artifact_id_from_sha256(content_sha256: str) -> str:
    """Derive the only valid artifact ID for one tagged SHA-256 digest."""

    _digest(content_sha256, "content_sha256")
    return "artifact_sha256_" + content_sha256.removeprefix("sha256:")


def create_content_addressed_artifact(
    content: bytes,
    *,
    media_type: str,
    classification: DataClassification,
    created_at: str,
    encryption_key_id: str | None = None,
    redaction_policy_id: str | None = None,
) -> ContentAddressedArtifact:
    """Describe bounded bytes without storing or logging their content."""

    if type(content) is not bytes:
        _invalid("content must be bytes")
    if len(content) > ARTIFACT_MAX_BYTES:
        _invalid(
            f"content exceeds maximum size of {ARTIFACT_MAX_BYTES} bytes"
        )
    content_sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    return ContentAddressedArtifact(
        artifact_id=artifact_id_from_sha256(content_sha256),
        content_sha256=content_sha256,
        size_bytes=len(content),
        media_type=media_type,
        classification=classification,
        created_at=created_at,
        encryption_key_id=encryption_key_id,
        redaction_policy_id=redaction_policy_id,
    )


def verify_artifact_content(
    artifact: ContentAddressedArtifact,
    content: bytes,
) -> bool:
    """Verify exact bytes against size, digest, and content-derived ID."""

    if type(artifact) is not ContentAddressedArtifact:
        _invalid("artifact must be exactly ContentAddressedArtifact")
    if type(content) is not bytes:
        _invalid("content must be bytes")
    if len(content) != artifact.size_bytes:
        return False
    actual_sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    return hmac.compare_digest(
        actual_sha256,
        artifact.content_sha256,
    ) and hmac.compare_digest(
        artifact_id_from_sha256(actual_sha256),
        artifact.artifact_id,
    )


def create_injection_artifact(
    snippet: str,
    *,
    session_id: str,
    decision_id: str,
    usage_decision_id: str,
    memory_revision_ids: tuple[str, ...],
    renderer_id: str,
    renderer_version: str,
    policy_bundle_sha256: str,
    rendered_at: str,
    classification: DataClassification = "internal",
    encryption_key_id: str | None = None,
    redaction_policy_id: str | None = None,
) -> InjectionArtifact:
    """Create exact content metadata for the only injectable snippet."""

    if type(snippet) is not str:
        _invalid("snippet must be a string")
    try:
        content = snippet.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ReplayContractError(
            "TBM_REPLAY_INVALID",
            "snippet must contain valid Unicode",
        ) from error
    if len(content) > INJECTION_ARTIFACT_MAX_BYTES:
        _invalid(
            "injection snippet exceeds maximum size of "
            f"{INJECTION_ARTIFACT_MAX_BYTES} bytes"
        )
    artifact = create_content_addressed_artifact(
        content,
        media_type=INJECTION_ARTIFACT_MEDIA_TYPE,
        classification=classification,
        created_at=rendered_at,
        encryption_key_id=encryption_key_id,
        redaction_policy_id=redaction_policy_id,
    )
    return InjectionArtifact(
        artifact=artifact,
        session_id=session_id,
        decision_id=decision_id,
        usage_decision_id=usage_decision_id,
        memory_revision_ids=memory_revision_ids,
        renderer_id=renderer_id,
        renderer_version=renderer_version,
        policy_bundle_sha256=policy_bundle_sha256,
        rendered_at=rendered_at,
    )


def verify_injection_artifact(
    injection: InjectionArtifact,
    snippet: str,
) -> bool:
    """Verify a snippet against one finalized injection artifact."""

    if type(injection) is not InjectionArtifact:
        _invalid("injection must be exactly InjectionArtifact")
    if type(snippet) is not str:
        _invalid("snippet must be a string")
    try:
        content = snippet.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return verify_artifact_content(injection.artifact, content)


def build_decision_replay_manifest(
    *,
    session_id: str,
    decision_id: str,
    usage_decision_id: str,
    component_hashes: Mapping[str, str | None],
    injection_artifact_id: str | None,
    completeness: ReplayCompleteness,
    created_at: str,
) -> DecisionReplayManifest:
    """Build one canonical replay manifest from the fixed component set."""

    components = _components_from_mapping(component_hashes)
    _timestamp(created_at, "created_at")
    missing = tuple(
        name for name, value in components if value is None
    )
    unsigned = {
        "contract_version": REPLAY_CONTRACT_VERSION,
        "manifest_kind": "decision_replay",
        "session_id": session_id,
        "decision_id": decision_id,
        "usage_decision_id": usage_decision_id,
        "components": dict(components),
        "injection_artifact_id": injection_artifact_id,
        "completeness": completeness,
        "missing_components": list(missing),
        "created_at": canonical_rfc3339(created_at),
    }
    return DecisionReplayManifest(
        manifest_sha256=canonical_sha256(unsigned),
        session_id=session_id,
        decision_id=decision_id,
        usage_decision_id=usage_decision_id,
        components=components,
        injection_artifact_id=injection_artifact_id,
        completeness=completeness,
        missing_components=missing,
        created_at=created_at,
    )


def parse_injection_artifact(
    payload: Mapping[str, object],
) -> InjectionArtifact:
    """Parse one strict external injection artifact object."""

    data = _strict_object(payload, _INJECTION_FIELDS, "injection artifact")
    artifact_payload = data["artifact"]
    artifact_data = _strict_object(
        artifact_payload,
        _ARTIFACT_FIELDS,
        "content-addressed artifact",
    )
    artifact = ContentAddressedArtifact(
        artifact_id=_string(artifact_data, "artifact_id"),
        content_sha256=_string(artifact_data, "content_sha256"),
        size_bytes=_integer(artifact_data, "size_bytes"),
        media_type=_string(artifact_data, "media_type"),
        classification=cast(
            DataClassification,
            _string(artifact_data, "classification"),
        ),
        created_at=_string(artifact_data, "created_at"),
        encryption_key_id=_optional_string(
            artifact_data,
            "encryption_key_id",
        ),
        redaction_policy_id=_optional_string(
            artifact_data,
            "redaction_policy_id",
        ),
    )
    return InjectionArtifact(
        contract_version=_string(data, "contract_version"),
        artifact_kind=cast(
            Literal["injection"],
            _string(data, "artifact_kind"),
        ),
        artifact=artifact,
        session_id=_string(data, "session_id"),
        decision_id=_string(data, "decision_id"),
        usage_decision_id=_string(data, "usage_decision_id"),
        memory_revision_ids=_string_list(data, "memory_revision_ids"),
        renderer_id=_string(data, "renderer_id"),
        renderer_version=_string(data, "renderer_version"),
        policy_bundle_sha256=_string(data, "policy_bundle_sha256"),
        rendered_at=_string(data, "rendered_at"),
    )


def parse_decision_replay_manifest(
    payload: Mapping[str, object],
) -> DecisionReplayManifest:
    """Parse one strict external decision replay manifest."""

    data = _strict_object(payload, _MANIFEST_FIELDS, "replay manifest")
    components_payload = data["components"]
    if type(components_payload) is not dict:
        _invalid("components must be a JSON object")
    components = _components_from_mapping(
        cast(dict[str, str | None], components_payload)
    )
    return DecisionReplayManifest(
        contract_version=_string(data, "contract_version"),
        manifest_kind=cast(
            Literal["decision_replay"],
            _string(data, "manifest_kind"),
        ),
        manifest_sha256=_string(data, "manifest_sha256"),
        session_id=_string(data, "session_id"),
        decision_id=_string(data, "decision_id"),
        usage_decision_id=_string(data, "usage_decision_id"),
        components=components,
        injection_artifact_id=_optional_string(
            data,
            "injection_artifact_id",
        ),
        completeness=cast(
            ReplayCompleteness,
            _string(data, "completeness"),
        ),
        missing_components=cast(
            tuple[ReplayComponentName, ...],
            _string_list(data, "missing_components"),
        ),
        created_at=_string(data, "created_at"),
    )


def loads_injection_artifact(source: str | bytes) -> InjectionArtifact:
    """Decode bounded strict JSON into one injection artifact."""

    return parse_injection_artifact(
        _loads_replay_json(source, "injection artifact")
    )


def loads_decision_replay_manifest(
    source: str | bytes,
) -> DecisionReplayManifest:
    """Decode bounded strict JSON into one decision replay manifest."""

    return parse_decision_replay_manifest(
        _loads_replay_json(source, "decision replay manifest")
    )


def dumps_injection_artifact(injection: InjectionArtifact) -> str:
    """Serialize one injection artifact as canonical JSON."""

    if type(injection) is not InjectionArtifact:
        _invalid("injection must be exactly InjectionArtifact")
    return _canonical_json(injection.to_dict())


def dumps_decision_replay_manifest(
    manifest: DecisionReplayManifest,
) -> str:
    """Serialize one decision replay manifest as canonical JSON."""

    if type(manifest) is not DecisionReplayManifest:
        _invalid("manifest must be exactly DecisionReplayManifest")
    return _canonical_json(manifest.to_dict())


def _components_from_mapping(
    component_hashes: Mapping[str, str | None],
) -> tuple[tuple[ReplayComponentName, str | None], ...]:
    if type(component_hashes) is not dict:
        _invalid("component_hashes must be a JSON object")
    if any(type(key) is not str for key in component_hashes):
        _invalid("component_hashes object keys must be strings")
    keys = frozenset(component_hashes)
    expected = frozenset(REPLAY_COMPONENT_NAMES)
    unknown = sorted(keys - expected)
    missing = sorted(expected - keys)
    if unknown:
        _invalid(f"component_hashes has unknown field: {unknown[0]}")
    if missing:
        _invalid(f"component_hashes is missing field: {missing[0]}")
    components: list[tuple[ReplayComponentName, str | None]] = []
    for name in REPLAY_COMPONENT_NAMES:
        value = component_hashes[name]
        if value is not None:
            _digest(value, f"component_hashes.{name}")
        components.append((name, value))
    return tuple(components)


def _validate_components(
    components: object,
) -> None:
    if type(components) is not tuple:
        _invalid("components must be a tuple")
    names: list[object] = []
    for item in components:
        if type(item) is not tuple or len(item) != 2:
            _invalid("components entries must be name/digest tuples")
        name, value = item
        names.append(name)
        if type(name) is not str or name not in REPLAY_COMPONENT_NAMES:
            _invalid("components contains an unsupported component name")
        if value is not None:
            _digest(value, f"components.{name}")
    if tuple(names) != REPLAY_COMPONENT_NAMES:
        _invalid("components must use the canonical component order")


def _strict_object(
    payload: object,
    expected_fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(payload) is not dict:
        _invalid(f"{label} must be a JSON object")
    data = cast(dict[str, object], payload)
    if any(type(key) is not str for key in data):
        _invalid(f"{label} object keys must be strings")
    fields = frozenset(data)
    unknown = sorted(fields - expected_fields)
    missing = sorted(expected_fields - fields)
    if unknown:
        _invalid(f"{label} has unknown field: {unknown[0]}")
    if missing:
        _invalid(f"{label} is missing field: {missing[0]}")
    return data


def _loads_replay_json(
    source: str | bytes,
    label: str,
) -> dict[str, object]:
    if type(source) is bytes:
        try:
            source_text = decode_bounded_utf8(
                source,
                max_bytes=REPLAY_JSON_MAX_BYTES,
                description=f"{label} JSON",
            )
        except UnicodeDecodeError as error:
            raise ReplayContractError(
                "TBM_REPLAY_INVALID_JSON",
                f"{label} JSON contains invalid UTF-8",
            ) from error
    else:
        source_text = source
    try:
        if type(source_text) is not str:
            raise ValueError(f"{label} source must be str or bytes")
        if len(source_text.encode("utf-8")) > REPLAY_JSON_MAX_BYTES:
            raise ValueError(
                f"{label} JSON exceeds maximum size of "
                f"{REPLAY_JSON_MAX_BYTES} bytes"
            )
        payload = parse_bounded_json(
            source_text,
            description=label,
            max_nodes=REPLAY_JSON_MAX_NODES,
            max_depth=REPLAY_JSON_MAX_DEPTH,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise ReplayContractError(
            "TBM_REPLAY_INVALID_JSON",
            str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
        ) from error
    if type(payload) is not dict:
        _invalid(f"{label} JSON must contain one object")
    return cast(dict[str, object], payload)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _string(payload: Mapping[str, object], field_name: str) -> str:
    value = payload[field_name]
    if type(value) is not str:
        _invalid(f"{field_name} must be a string")
    return cast(str, value)


def _optional_string(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = payload[field_name]
    if value is not None and type(value) is not str:
        _invalid(f"{field_name} must be a string or null")
    return cast(str | None, value)


def _integer(payload: Mapping[str, object], field_name: str) -> int:
    value = payload[field_name]
    if type(value) is not int:
        _invalid(f"{field_name} must be an integer")
    return cast(int, value)


def _string_list(
    payload: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    value = payload[field_name]
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid(f"{field_name} must be an array of strings")
    return tuple(cast(list[str], value))


def _required_identifier(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_ID_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{MEMORY_ID_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _optional_identifier(value: object, field_name: str) -> None:
    if value is not None:
        _required_identifier(value, field_name)


def _optional_artifact_id(value: object, field_name: str) -> None:
    if value is not None and (
        type(value) is not str or _ARTIFACT_ID_RE.fullmatch(value) is None
    ):
        _invalid(f"{field_name} must be a content-derived artifact ID")


def _required_metadata(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > METADATA_VALUE_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{METADATA_VALUE_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _identifier_tuple(
    value: object,
    field_name: str,
    *,
    max_items: int,
) -> None:
    if type(value) is not tuple or len(value) > max_items:
        _invalid(
            f"{field_name} must be a tuple with at most "
            f"{max_items} entries"
        )
    for item in value:
        _required_identifier(item, field_name)
    if len(set(value)) != len(value):
        _invalid(f"{field_name} must not contain duplicates")


def _digest(value: object, field_name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{field_name} must be an algorithm-tagged SHA-256 digest")


def _timestamp(value: object, field_name: str):
    try:
        return parse_rfc3339(value)
    except ValueError as error:
        raise ReplayContractError(
            "TBM_REPLAY_INVALID",
            f"{field_name} must be a timezone-aware RFC 3339 date-time",
        ) from error


def _unicode(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ReplayContractError(
            "TBM_REPLAY_INVALID",
            f"{field_name} must contain valid Unicode",
        ) from error


def _invalid(message: str) -> None:
    raise ReplayContractError("TBM_REPLAY_INVALID", message)


__all__ = [
    "ARTIFACT_MAX_BYTES",
    "INJECTION_ARTIFACT_MAX_BYTES",
    "INJECTION_ARTIFACT_MEDIA_TYPE",
    "REPLAY_COMPONENT_NAMES",
    "REPLAY_CONTRACT_VERSION",
    "REPLAY_JSON_MAX_BYTES",
    "REPLAY_JSON_MAX_DEPTH",
    "REPLAY_JSON_MAX_NODES",
    "ContentAddressedArtifact",
    "DataClassification",
    "DecisionReplayManifest",
    "InjectionArtifact",
    "ReplayCompleteness",
    "ReplayComponentName",
    "ReplayContractError",
    "StoredReplayArtifact",
    "artifact_id_from_sha256",
    "build_decision_replay_manifest",
    "create_content_addressed_artifact",
    "create_injection_artifact",
    "dumps_decision_replay_manifest",
    "dumps_injection_artifact",
    "loads_decision_replay_manifest",
    "loads_injection_artifact",
    "parse_decision_replay_manifest",
    "parse_injection_artifact",
    "verify_artifact_content",
    "verify_injection_artifact",
]
