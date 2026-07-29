from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import base64
import binascii
import hmac
import json
import re
from typing import Literal, Protocol, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from .contracts_v3 import V3ContractError, canonical_sha256
from .policy import MEMORY_DECISION_REASON_MAX_CHARS
from .replay_v3 import (
    REPLAY_COMPONENT_NAMES,
    ContentAddressedArtifact,
    DataClassification,
    DecisionReplayManifest,
    InjectionArtifact,
    ReplayComponentName,
    ReplayContractError,
    StoredReplayArtifact,
    artifact_id_from_sha256,
    parse_decision_replay_manifest,
    parse_injection_artifact,
    verify_artifact_content,
)


REPLAY_EXPORT_CONTRACT_VERSION = "tbm.replay-export.v3"
REPLAY_EXPORT_CONTENT_ENCODING = "base64"
REPLAY_EXPORT_MAX_CONTENT_BYTES = 8 * 1024 * 1024
REPLAY_EXPORT_JSON_MAX_BYTES = 16 * 1024 * 1024
REPLAY_EXPORT_JSON_MAX_DEPTH = 40
REPLAY_EXPORT_JSON_MAX_NODES = 20_000

_CLASSIFICATIONS = frozenset(
    {
        "public",
        "internal",
        "confidential",
        "restricted",
    }
)
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
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
_EXPORT_ARTIFACT_FIELDS = frozenset(
    {
        "component_name",
        "artifact",
        "content_base64",
    }
)
_EXPORT_FIELDS = frozenset(
    {
        "contract_version",
        "export_kind",
        "export_sha256",
        "content_encoding",
        "manifest",
        "injection",
        "artifacts",
    }
)


class ReplayExportError(V3ContractError):
    """Stable failure for malformed or disallowed replay exports."""


class ReplayExportReader(Protocol):
    """Small immutable-read surface required to construct an export."""

    def load_manifest(
        self,
        manifest_sha256: str,
    ) -> DecisionReplayManifest: ...

    def load_injection(
        self,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]: ...

    def load_artifact_descriptor(
        self,
        artifact_id: str,
    ) -> ContentAddressedArtifact: ...

    def load_artifact(
        self,
        artifact_id: str,
    ) -> StoredReplayArtifact: ...


@dataclass(frozen=True)
class ReplayExportArtifact:
    """One named replay component paired with its exact exported bytes."""

    component_name: ReplayComponentName
    artifact: ContentAddressedArtifact
    content: bytes

    def __post_init__(self) -> None:
        if (
            type(self.component_name) is not str
            or self.component_name not in REPLAY_COMPONENT_NAMES
        ):
            _invalid("component_name must be a replay component name")
        if type(self.artifact) is not ContentAddressedArtifact:
            _invalid("artifact must be exactly ContentAddressedArtifact")
        if type(self.content) is not bytes:
            _invalid("content must be bytes")
        if not verify_artifact_content(self.artifact, self.content):
            raise ReplayExportError(
                "TBM_REPLAY_EXPORT_HASH_MISMATCH",
                "exported artifact bytes do not match their descriptor",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "component_name": self.component_name,
            "artifact": self.artifact.to_dict(),
            "content_base64": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(frozen=True)
class ReplayBundleExport:
    """Portable, content-addressed export of one decision replay bundle."""

    export_sha256: str
    manifest: DecisionReplayManifest
    injection: InjectionArtifact | None
    artifacts: tuple[ReplayExportArtifact, ...]
    export_kind: Literal["decision_replay_bundle"] = "decision_replay_bundle"
    content_encoding: Literal["base64"] = REPLAY_EXPORT_CONTENT_ENCODING
    contract_version: str = REPLAY_EXPORT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPLAY_EXPORT_CONTRACT_VERSION:
            _invalid(
                "contract_version must be "
                f"{REPLAY_EXPORT_CONTRACT_VERSION}"
            )
        if self.export_kind != "decision_replay_bundle":
            _invalid("export_kind must be decision_replay_bundle")
        if self.content_encoding != REPLAY_EXPORT_CONTENT_ENCODING:
            _invalid(
                "content_encoding must be "
                f"{REPLAY_EXPORT_CONTENT_ENCODING}"
            )
        _digest(self.export_sha256, "export_sha256")
        if type(self.manifest) is not DecisionReplayManifest:
            _invalid("manifest must be exactly DecisionReplayManifest")
        if self.injection is not None and type(self.injection) is not InjectionArtifact:
            _invalid("injection must be exactly InjectionArtifact or null")
        if (
            type(self.artifacts) is not tuple
            or len(self.artifacts) > len(REPLAY_COMPONENT_NAMES)
            or any(type(item) is not ReplayExportArtifact for item in self.artifacts)
        ):
            _invalid(
                "artifacts must be a bounded tuple of ReplayExportArtifact"
            )

        component_map = dict(self.manifest.components)
        expected_names = tuple(
            name
            for name in REPLAY_COMPONENT_NAMES
            if component_map[name] is not None
        )
        actual_names = tuple(item.component_name for item in self.artifacts)
        if actual_names != expected_names:
            _invalid(
                "artifacts must exactly match present manifest components "
                "in canonical order"
            )

        total_content_bytes = 0
        by_name: dict[ReplayComponentName, ReplayExportArtifact] = {}
        for item in self.artifacts:
            expected_sha256 = component_map[item.component_name]
            if item.artifact.content_sha256 != expected_sha256:
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_HASH_MISMATCH",
                    "artifact digest differs from replay manifest",
                )
            total_content_bytes += len(item.content)
            if total_content_bytes > REPLAY_EXPORT_MAX_CONTENT_BYTES:
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_TOO_LARGE",
                    "replay export content exceeds the configured maximum",
                )
            by_name[item.component_name] = item

        injection_id = self.manifest.injection_artifact_id
        if injection_id is None:
            if self.injection is not None:
                _invalid(
                    "injection must be null when the manifest omits it"
                )
        else:
            if self.injection is None:
                _invalid(
                    "injection must be present when the manifest records it"
                )
            injection = cast(InjectionArtifact, self.injection)
            injection_item = by_name["injection_artifact"]
            if (
                injection.artifact != injection_item.artifact
                or injection.artifact.artifact_id != injection_id
                or injection.session_id != self.manifest.session_id
                or injection.decision_id != self.manifest.decision_id
                or injection.usage_decision_id
                != self.manifest.usage_decision_id
            ):
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_LINKAGE_MISMATCH",
                    "injection linkage differs from replay manifest",
                )

        unsigned = self._unsigned_dict()
        encoded = _canonical_json(unsigned).encode("utf-8")
        if len(encoded) > REPLAY_EXPORT_JSON_MAX_BYTES:
            raise ReplayExportError(
                "TBM_REPLAY_EXPORT_TOO_LARGE",
                "replay export JSON exceeds the configured maximum",
            )
        expected_export_sha256 = canonical_sha256(unsigned)
        if not hmac.compare_digest(
            self.export_sha256,
            expected_export_sha256,
        ):
            raise ReplayExportError(
                "TBM_REPLAY_EXPORT_HASH_MISMATCH",
                "export_sha256 does not match canonical export content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "export_kind": self.export_kind,
            "content_encoding": self.content_encoding,
            "manifest": self.manifest.to_dict(),
            "injection": (
                None if self.injection is None else self.injection.to_dict()
            ),
            "artifacts": [item.to_dict() for item in self.artifacts],
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._unsigned_dict()
        payload["export_sha256"] = self.export_sha256
        return payload


def build_replay_bundle_export(
    *,
    manifest: DecisionReplayManifest,
    injection: InjectionArtifact | None,
    artifacts: dict[str, StoredReplayArtifact],
) -> ReplayBundleExport:
    """Build one canonical export from already-authorized replay records."""

    if type(manifest) is not DecisionReplayManifest:
        _invalid("manifest must be exactly DecisionReplayManifest")
    if injection is not None and type(injection) is not InjectionArtifact:
        _invalid("injection must be exactly InjectionArtifact or null")
    if type(artifacts) is not dict:
        _invalid("artifacts must be a JSON-like object")
    if any(type(name) is not str for name in artifacts):
        _invalid("artifacts object keys must be strings")

    component_map = dict(manifest.components)
    expected_names = tuple(
        name
        for name in REPLAY_COMPONENT_NAMES
        if component_map[name] is not None
    )
    unknown = sorted(set(artifacts) - set(expected_names))
    missing = sorted(set(expected_names) - set(artifacts))
    if unknown:
        _invalid(f"artifacts has unknown component: {unknown[0]}")
    if missing:
        _invalid(f"artifacts is missing component: {missing[0]}")

    export_artifacts: list[ReplayExportArtifact] = []
    for name in expected_names:
        stored = artifacts[name]
        if type(stored) is not StoredReplayArtifact:
            _invalid(
                f"artifacts.{name} must be exactly StoredReplayArtifact"
            )
        export_artifacts.append(
            ReplayExportArtifact(
                component_name=name,
                artifact=stored.artifact,
                content=stored.content,
            )
        )

    unsigned = _unsigned_export_dict(
        manifest,
        injection,
        tuple(export_artifacts),
    )
    return ReplayBundleExport(
        export_sha256=canonical_sha256(unsigned),
        manifest=manifest,
        injection=injection,
        artifacts=tuple(export_artifacts),
    )


def export_replay_bundle(
    reader: ReplayExportReader,
    manifest_sha256: str,
    *,
    allowed_classifications: frozenset[DataClassification],
    max_content_bytes: int = REPLAY_EXPORT_MAX_CONTENT_BYTES,
) -> ReplayBundleExport:
    """Load and export one replay after the caller authorizes its read.

    The explicit classification allowlist is a second fail-closed boundary,
    not a substitute for authorizing the manifest and every referenced
    artifact before this function is called.
    """

    _digest(manifest_sha256, "manifest_sha256")
    _classification_allowlist(allowed_classifications)
    if (
        type(max_content_bytes) is not int
        or max_content_bytes < 1
        or max_content_bytes > REPLAY_EXPORT_MAX_CONTENT_BYTES
    ):
        _invalid(
            "max_content_bytes must be an integer from 1 to "
            f"{REPLAY_EXPORT_MAX_CONTENT_BYTES}"
        )

    try:
        manifest = reader.load_manifest(manifest_sha256)
        if type(manifest) is not DecisionReplayManifest:
            _invalid(
                "reader.load_manifest must return DecisionReplayManifest"
            )
        if not hmac.compare_digest(
            manifest.manifest_sha256,
            manifest_sha256,
        ):
            raise ReplayExportError(
                "TBM_REPLAY_EXPORT_HASH_MISMATCH",
                "loaded manifest differs from the requested digest",
            )
        descriptors_by_name: dict[str, ContentAddressedArtifact] = {}
        total_content_bytes = 0
        for name, content_sha256 in manifest.components:
            if content_sha256 is None:
                continue
            artifact_id = artifact_id_from_sha256(content_sha256)
            descriptor = reader.load_artifact_descriptor(artifact_id)
            if type(descriptor) is not ContentAddressedArtifact:
                _invalid(
                    "reader.load_artifact_descriptor must return "
                    "ContentAddressedArtifact"
                )
            if descriptor.content_sha256 != content_sha256:
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_HASH_MISMATCH",
                    "artifact descriptor differs from replay manifest",
                )
            if descriptor.classification not in allowed_classifications:
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_FORBIDDEN",
                    "replay export classification is not allowed",
                )
            if total_content_bytes + descriptor.size_bytes > max_content_bytes:
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_TOO_LARGE",
                    "replay export content exceeds the caller limit",
                )
            total_content_bytes += descriptor.size_bytes
            descriptors_by_name[name] = descriptor

        stored_by_name: dict[str, StoredReplayArtifact] = {}
        for name, descriptor in descriptors_by_name.items():
            artifact_id = descriptor.artifact_id
            stored = reader.load_artifact(artifact_id)
            if type(stored) is not StoredReplayArtifact:
                _invalid(
                    "reader.load_artifact must return StoredReplayArtifact"
                )
            if stored.artifact != descriptor:
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_LINKAGE_MISMATCH",
                    "loaded artifact differs from its preflight descriptor",
                )
            if len(stored.content) != descriptor.size_bytes:
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_HASH_MISMATCH",
                    "loaded artifact bytes differ from their preflight size",
                )
            stored_by_name[name] = stored

        injection: InjectionArtifact | None = None
        if manifest.injection_artifact_id is not None:
            injection, injection_content = reader.load_injection(
                manifest.injection_artifact_id
            )
            if type(injection) is not InjectionArtifact:
                _invalid(
                    "reader.load_injection must return InjectionArtifact"
                )
            stored_injection = stored_by_name["injection_artifact"]
            if (
                injection.artifact != stored_injection.artifact
                or injection_content != stored_injection.content
            ):
                raise ReplayExportError(
                    "TBM_REPLAY_EXPORT_LINKAGE_MISMATCH",
                    "loaded injection differs from its replay artifact",
                )
    except KeyError as error:
        raise ReplayExportError(
            "TBM_REPLAY_EXPORT_NOT_FOUND",
            "replay export source is incomplete",
        ) from error

    return build_replay_bundle_export(
        manifest=manifest,
        injection=injection,
        artifacts=stored_by_name,
    )


def verify_replay_bundle_export(export: ReplayBundleExport) -> bool:
    """Verify all hashes, bytes, ordering, and linkage in one export."""

    if type(export) is not ReplayBundleExport:
        _invalid("export must be exactly ReplayBundleExport")
    try:
        ReplayBundleExport(
            export_sha256=export.export_sha256,
            manifest=export.manifest,
            injection=export.injection,
            artifacts=export.artifacts,
            export_kind=export.export_kind,
            content_encoding=export.content_encoding,
            contract_version=export.contract_version,
        )
    except ReplayExportError:
        return False
    return True


def parse_replay_bundle_export(
    payload: Mapping[str, object],
) -> ReplayBundleExport:
    """Parse one strict external replay export object."""

    data = _strict_object(payload, _EXPORT_FIELDS, "replay export")
    manifest_payload = _strict_object(
        data["manifest"],
        frozenset(
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
        ),
        "replay manifest",
    )
    try:
        manifest = parse_decision_replay_manifest(manifest_payload)
    except ReplayContractError as error:
        raise ReplayExportError(
            "TBM_REPLAY_EXPORT_INVALID",
            str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
        ) from error

    injection_payload = data["injection"]
    injection: InjectionArtifact | None
    if injection_payload is None:
        injection = None
    else:
        try:
            injection = parse_injection_artifact(
                _strict_object(
                    injection_payload,
                    frozenset(
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
                    ),
                    "injection artifact",
                )
            )
        except ReplayContractError as error:
            raise ReplayExportError(
                "TBM_REPLAY_EXPORT_INVALID",
                str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
            ) from error

    artifacts_payload = data["artifacts"]
    if (
        type(artifacts_payload) is not list
        or len(artifacts_payload) > len(REPLAY_COMPONENT_NAMES)
    ):
        _invalid(
            "artifacts must be an array bounded by replay component count"
        )
    artifacts: list[ReplayExportArtifact] = []
    for payload_item in artifacts_payload:
        item = _strict_object(
            payload_item,
            _EXPORT_ARTIFACT_FIELDS,
            "replay export artifact",
        )
        descriptor = _parse_artifact(item["artifact"])
        component_name = _string(item, "component_name")
        content = _decode_base64(_string(item, "content_base64"))
        artifacts.append(
            ReplayExportArtifact(
                component_name=cast(ReplayComponentName, component_name),
                artifact=descriptor,
                content=content,
            )
        )

    return ReplayBundleExport(
        contract_version=_string(data, "contract_version"),
        export_kind=cast(
            Literal["decision_replay_bundle"],
            _string(data, "export_kind"),
        ),
        export_sha256=_string(data, "export_sha256"),
        content_encoding=cast(
            Literal["base64"],
            _string(data, "content_encoding"),
        ),
        manifest=manifest,
        injection=injection,
        artifacts=tuple(artifacts),
    )


def loads_replay_bundle_export(
    source: str | bytes,
) -> ReplayBundleExport:
    """Decode bounded duplicate-rejecting JSON into one replay export."""

    return parse_replay_bundle_export(_loads_export_json(source))


def dumps_replay_bundle_export(export: ReplayBundleExport) -> str:
    """Serialize one replay export as canonical JSON."""

    if type(export) is not ReplayBundleExport:
        _invalid("export must be exactly ReplayBundleExport")
    encoded = _canonical_json(export.to_dict())
    if len(encoded.encode("utf-8")) > REPLAY_EXPORT_JSON_MAX_BYTES:
        raise ReplayExportError(
            "TBM_REPLAY_EXPORT_TOO_LARGE",
            "replay export JSON exceeds the configured maximum",
        )
    return encoded


def _unsigned_export_dict(
    manifest: DecisionReplayManifest,
    injection: InjectionArtifact | None,
    artifacts: tuple[ReplayExportArtifact, ...],
) -> dict[str, object]:
    return {
        "contract_version": REPLAY_EXPORT_CONTRACT_VERSION,
        "export_kind": "decision_replay_bundle",
        "content_encoding": REPLAY_EXPORT_CONTENT_ENCODING,
        "manifest": manifest.to_dict(),
        "injection": None if injection is None else injection.to_dict(),
        "artifacts": [item.to_dict() for item in artifacts],
    }


def _parse_artifact(payload: object) -> ContentAddressedArtifact:
    data = _strict_object(
        payload,
        _ARTIFACT_FIELDS,
        "content-addressed artifact",
    )
    try:
        return ContentAddressedArtifact(
            artifact_id=_string(data, "artifact_id"),
            content_sha256=_string(data, "content_sha256"),
            size_bytes=_integer(data, "size_bytes"),
            media_type=_string(data, "media_type"),
            classification=cast(
                DataClassification,
                _string(data, "classification"),
            ),
            created_at=_string(data, "created_at"),
            encryption_key_id=_optional_string(data, "encryption_key_id"),
            redaction_policy_id=_optional_string(
                data,
                "redaction_policy_id",
            ),
        )
    except ReplayContractError as error:
        raise ReplayExportError(
            "TBM_REPLAY_EXPORT_INVALID",
            str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
        ) from error


def _classification_allowlist(
    value: object,
) -> None:
    if (
        type(value) is not frozenset
        or not value
        or any(
            type(item) is not str or item not in _CLASSIFICATIONS
            for item in value
        )
    ):
        _invalid(
            "allowed_classifications must be a non-empty frozenset of "
            "supported classifications"
        )


def _decode_base64(value: str) -> bytes:
    try:
        ascii_bytes = value.encode("ascii")
        content = base64.b64decode(ascii_bytes, validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ReplayExportError(
            "TBM_REPLAY_EXPORT_INVALID",
            "content_base64 must be canonical base64",
        ) from error
    if base64.b64encode(content) != ascii_bytes:
        _invalid("content_base64 must be canonical base64")
    return content


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


def _loads_export_json(source: str | bytes) -> dict[str, object]:
    if type(source) is bytes:
        try:
            source_text = decode_bounded_utf8(
                source,
                max_bytes=REPLAY_EXPORT_JSON_MAX_BYTES,
                description="replay export JSON",
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise ReplayExportError(
                "TBM_REPLAY_EXPORT_INVALID_JSON",
                str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
            ) from error
    else:
        source_text = source
    try:
        if type(source_text) is not str:
            raise ValueError("replay export source must be str or bytes")
        if len(source_text.encode("utf-8")) > REPLAY_EXPORT_JSON_MAX_BYTES:
            raise ValueError(
                "replay export JSON exceeds maximum size of "
                f"{REPLAY_EXPORT_JSON_MAX_BYTES} bytes"
            )
        payload = parse_bounded_json(
            source_text,
            description="replay export",
            max_nodes=REPLAY_EXPORT_JSON_MAX_NODES,
            max_depth=REPLAY_EXPORT_JSON_MAX_DEPTH,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise ReplayExportError(
            "TBM_REPLAY_EXPORT_INVALID_JSON",
            str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
        ) from error
    if type(payload) is not dict:
        _invalid("replay export JSON must contain one object")
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


def _digest(value: object, field_name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{field_name} must be an algorithm-tagged SHA-256 digest")


def _invalid(message: str) -> None:
    raise ReplayExportError("TBM_REPLAY_EXPORT_INVALID", message)


__all__ = [
    "REPLAY_EXPORT_CONTENT_ENCODING",
    "REPLAY_EXPORT_CONTRACT_VERSION",
    "REPLAY_EXPORT_JSON_MAX_BYTES",
    "REPLAY_EXPORT_JSON_MAX_DEPTH",
    "REPLAY_EXPORT_JSON_MAX_NODES",
    "REPLAY_EXPORT_MAX_CONTENT_BYTES",
    "ReplayBundleExport",
    "ReplayExportArtifact",
    "ReplayExportError",
    "ReplayExportReader",
    "build_replay_bundle_export",
    "dumps_replay_bundle_export",
    "export_replay_bundle",
    "loads_replay_bundle_export",
    "parse_replay_bundle_export",
    "verify_replay_bundle_export",
]
