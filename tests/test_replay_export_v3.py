from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import pytest
from referencing import Registry, Resource

import trace_backed_memory as tbm
from trace_backed_memory.replay_v3 import REPLAY_COMPONENT_NAMES


NOW = "2026-07-30T00:00:00Z"
ROOT = Path(__file__).resolve().parents[1]


def _records(
    *,
    classification: tbm.DataClassification = "internal",
    created_at: str = NOW,
) -> tuple[
    tbm.DecisionReplayManifest,
    tbm.InjectionArtifact,
    dict[str, tbm.StoredReplayArtifact],
]:
    snippet = "Use exact reviewed memory."
    injection = tbm.create_injection_artifact(
        snippet,
        session_id="session_export_001",
        decision_id="decision_export_001",
        usage_decision_id="usage_export_001",
        memory_revision_ids=("revision_001",),
        renderer_id="renderer_001",
        renderer_version="1.0.0",
        policy_bundle_sha256="sha256:" + "a" * 64,
        rendered_at=created_at,
        classification=classification,
    )
    artifacts: dict[str, tbm.StoredReplayArtifact] = {}
    component_hashes: dict[str, str] = {}
    for name in REPLAY_COMPONENT_NAMES:
        if name == "injection_artifact":
            stored = tbm.StoredReplayArtifact(
                injection.artifact,
                snippet.encode(),
            )
        else:
            content = f"{name} replay bytes".encode()
            descriptor = tbm.create_content_addressed_artifact(
                content,
                media_type="application/octet-stream",
                classification=classification,
                created_at=created_at,
            )
            stored = tbm.StoredReplayArtifact(descriptor, content)
        artifacts[name] = stored
        component_hashes[name] = stored.artifact.content_sha256
    manifest = tbm.build_decision_replay_manifest(
        session_id=injection.session_id,
        decision_id=injection.decision_id,
        usage_decision_id=injection.usage_decision_id,
        component_hashes=component_hashes,
        injection_artifact_id=injection.artifact.artifact_id,
        completeness="complete",
        created_at=created_at,
    )
    return manifest, injection, artifacts


class _Reader:
    def __init__(
        self,
        manifest: tbm.DecisionReplayManifest,
        injection: tbm.InjectionArtifact,
        artifacts: dict[str, tbm.StoredReplayArtifact],
    ) -> None:
        self.manifest = manifest
        self.injection = injection
        self.artifacts = artifacts
        self.artifact_loads = 0
        self.descriptor_loads = 0
        self.injection_loads = 0

    def load_manifest(
        self,
        manifest_sha256: str,
    ) -> tbm.DecisionReplayManifest:
        if manifest_sha256 != self.manifest.manifest_sha256:
            raise KeyError(manifest_sha256)
        return self.manifest

    def load_injection(
        self,
        artifact_id: str,
    ) -> tuple[tbm.InjectionArtifact, bytes]:
        if artifact_id != self.injection.artifact.artifact_id:
            raise KeyError(artifact_id)
        self.injection_loads += 1
        return (
            self.injection,
            self.artifacts["injection_artifact"].content,
        )

    def load_artifact(
        self,
        artifact_id: str,
    ) -> tbm.StoredReplayArtifact:
        for stored in self.artifacts.values():
            if stored.artifact.artifact_id == artifact_id:
                self.artifact_loads += 1
                return stored
        raise KeyError(artifact_id)

    def load_artifact_descriptor(
        self,
        artifact_id: str,
    ) -> tbm.ContentAddressedArtifact:
        for stored in self.artifacts.values():
            if stored.artifact.artifact_id == artifact_id:
                self.descriptor_loads += 1
                return stored.artifact
        raise KeyError(artifact_id)


class _MismatchedManifestReader(_Reader):
    def load_manifest(
        self,
        manifest_sha256: str,
    ) -> tbm.DecisionReplayManifest:
        del manifest_sha256
        return self.manifest


def test_replay_export_round_trip_is_canonical_and_content_addressed():
    manifest, injection, artifacts = _records()
    export = tbm.build_replay_bundle_export(
        manifest=manifest,
        injection=injection,
        artifacts=artifacts,
    )

    assert export.contract_version == "tbm.replay-export.v3"
    assert export.content_encoding == "base64"
    assert tuple(item.component_name for item in export.artifacts) == (
        REPLAY_COMPONENT_NAMES
    )
    assert tbm.verify_replay_bundle_export(export) is True

    encoded = tbm.dumps_replay_bundle_export(export)
    parsed = tbm.loads_replay_bundle_export(encoded)
    assert parsed == export
    assert tbm.dumps_replay_bundle_export(parsed) == encoded
    assert json.loads(encoded)["export_sha256"] == export.export_sha256


def test_replay_export_round_trip_canonicalizes_offset_timestamps():
    manifest, injection, artifacts = _records(
        created_at="2026-07-30T08:00:00+08:00"
    )
    export = tbm.build_replay_bundle_export(
        manifest=manifest,
        injection=injection,
        artifacts=artifacts,
    )

    assert export.manifest.created_at == NOW
    assert export.injection is not None
    assert export.injection.rendered_at == NOW
    assert tbm.loads_replay_bundle_export(
        tbm.dumps_replay_bundle_export(export)
    ) == export


def test_reader_export_requires_explicit_classifications_and_bounds():
    manifest, injection, artifacts = _records()
    reader = _Reader(manifest, injection, artifacts)

    exported = tbm.export_replay_bundle(
        reader,
        manifest.manifest_sha256,
        allowed_classifications=frozenset({"internal"}),
    )
    assert tbm.verify_replay_bundle_export(exported)
    total_bytes = sum(
        stored.artifact.size_bytes for stored in artifacts.values()
    )
    exact_limit_export = tbm.export_replay_bundle(
        _Reader(manifest, injection, artifacts),
        manifest.manifest_sha256,
        allowed_classifications=frozenset({"internal"}),
        max_content_bytes=total_bytes,
    )
    assert exact_limit_export == exported

    with pytest.raises(tbm.ReplayExportError) as forbidden:
        tbm.export_replay_bundle(
            reader,
            manifest.manifest_sha256,
            allowed_classifications=frozenset({"public"}),
        )
    assert forbidden.value.code == "TBM_REPLAY_EXPORT_FORBIDDEN"

    bounded_reader = _Reader(manifest, injection, artifacts)
    with pytest.raises(tbm.ReplayExportError) as too_large:
        tbm.export_replay_bundle(
            bounded_reader,
            manifest.manifest_sha256,
            allowed_classifications=frozenset({"internal"}),
            max_content_bytes=1,
        )
    assert too_large.value.code == "TBM_REPLAY_EXPORT_TOO_LARGE"
    assert bounded_reader.artifact_loads == 0

    with pytest.raises(tbm.ReplayExportError) as missing:
        tbm.export_replay_bundle(
            reader,
            "sha256:" + "f" * 64,
            allowed_classifications=frozenset({"internal"}),
        )
    assert missing.value.code == "TBM_REPLAY_EXPORT_NOT_FOUND"
    assert "sha256:" not in str(missing.value)

    different_manifest = tbm.build_decision_replay_manifest(
        session_id=manifest.session_id,
        decision_id=manifest.decision_id,
        usage_decision_id=manifest.usage_decision_id,
        component_hashes=dict(manifest.components),
        injection_artifact_id=manifest.injection_artifact_id,
        completeness="complete",
        created_at="2026-07-30T00:00:01Z",
    )
    mismatched_reader = _MismatchedManifestReader(
        different_manifest,
        injection,
        artifacts,
    )
    with pytest.raises(tbm.ReplayExportError) as mismatched:
        tbm.export_replay_bundle(
            mismatched_reader,
            manifest.manifest_sha256,
            allowed_classifications=frozenset({"internal"}),
        )
    assert mismatched.value.code == "TBM_REPLAY_EXPORT_HASH_MISMATCH"


def test_reader_preflights_every_classification_before_loading_any_bytes():
    manifest, injection, artifacts = _records()
    late_name = REPLAY_COMPONENT_NAMES[-1]
    late_artifact = artifacts[late_name]
    artifacts[late_name] = tbm.StoredReplayArtifact(
        replace(
            late_artifact.artifact,
            classification="restricted",
            encryption_key_id="key_restricted",
        ),
        late_artifact.content,
    )
    reader = _Reader(manifest, injection, artifacts)

    with pytest.raises(tbm.ReplayExportError) as forbidden:
        tbm.export_replay_bundle(
            reader,
            manifest.manifest_sha256,
            allowed_classifications=frozenset({"internal"}),
        )

    assert forbidden.value.code == "TBM_REPLAY_EXPORT_FORBIDDEN"
    assert reader.descriptor_loads == len(REPLAY_COMPONENT_NAMES)
    assert reader.artifact_loads == 0
    assert reader.injection_loads == 0


def test_reader_preflights_total_size_before_loading_any_bytes():
    manifest, injection, artifacts = _records()
    total_bytes = sum(
        stored.artifact.size_bytes for stored in artifacts.values()
    )
    reader = _Reader(manifest, injection, artifacts)

    with pytest.raises(tbm.ReplayExportError) as too_large:
        tbm.export_replay_bundle(
            reader,
            manifest.manifest_sha256,
            allowed_classifications=frozenset({"internal"}),
            max_content_bytes=total_bytes - 1,
        )

    assert too_large.value.code == "TBM_REPLAY_EXPORT_TOO_LARGE"
    assert reader.descriptor_loads == len(REPLAY_COMPONENT_NAMES)
    assert reader.artifact_loads == 0
    assert reader.injection_loads == 0


def test_replay_export_rejects_tampering_and_unbounded_input():
    manifest, injection, artifacts = _records()
    export = tbm.build_replay_bundle_export(
        manifest=manifest,
        injection=injection,
        artifacts=artifacts,
    )
    payload = export.to_dict()

    tampered = json.loads(json.dumps(payload))
    tampered["artifacts"][0]["content_base64"] = "dGFtcGVyZWQ="
    with pytest.raises(tbm.ReplayExportError) as content_error:
        tbm.parse_replay_bundle_export(tampered)
    assert content_error.value.code == "TBM_REPLAY_EXPORT_HASH_MISMATCH"

    with pytest.raises(tbm.ReplayExportError) as export_hash_error:
        replace(export, export_sha256="sha256:" + "0" * 64)
    assert export_hash_error.value.code == "TBM_REPLAY_EXPORT_HASH_MISMATCH"


def test_replay_export_strict_json_and_build_validation():
    manifest, injection, artifacts = _records()
    export = tbm.build_replay_bundle_export(
        manifest=manifest,
        injection=injection,
        artifacts=artifacts,
    )
    encoded = tbm.dumps_replay_bundle_export(export)
    duplicate = encoded.replace(
        '"artifacts":',
        '"artifacts":[],"artifacts":',
        1,
    )
    with pytest.raises(tbm.ReplayExportError) as duplicate_error:
        tbm.loads_replay_bundle_export(duplicate)
    assert duplicate_error.value.code == "TBM_REPLAY_EXPORT_INVALID_JSON"

    payload = export.to_dict()
    payload["unknown"] = True
    with pytest.raises(tbm.ReplayExportError, match="unknown field"):
        tbm.parse_replay_bundle_export(payload)

    invalid_base64 = export.to_dict()
    invalid_base64["artifacts"][0]["content_base64"] = "not base64!"
    with pytest.raises(tbm.ReplayExportError, match="canonical base64"):
        tbm.parse_replay_bundle_export(invalid_base64)

    with pytest.raises(tbm.ReplayExportError, match="non-empty frozenset"):
        tbm.export_replay_bundle(
            _Reader(manifest, injection, artifacts),
            manifest.manifest_sha256,
            allowed_classifications=frozenset(),
        )

    invalid_manifest = export.to_dict()
    invalid_manifest["manifest"]["session_id"] = ""
    with pytest.raises(tbm.ReplayExportError) as manifest_error:
        tbm.parse_replay_bundle_export(invalid_manifest)
    assert manifest_error.value.code == "TBM_REPLAY_EXPORT_INVALID"

    invalid_descriptor = export.to_dict()
    invalid_descriptor["artifacts"][0]["artifact"][
        "classification"
    ] = "unknown"
    with pytest.raises(tbm.ReplayExportError) as descriptor_error:
        tbm.parse_replay_bundle_export(invalid_descriptor)
    assert descriptor_error.value.code == "TBM_REPLAY_EXPORT_INVALID"

    oversized = b" " * (tbm.REPLAY_EXPORT_JSON_MAX_BYTES + 1)
    with pytest.raises(tbm.ReplayExportError) as oversized_error:
        tbm.loads_replay_bundle_export(oversized)
    assert oversized_error.value.code == "TBM_REPLAY_EXPORT_INVALID_JSON"


def test_legacy_partial_export_can_omit_injection():
    _manifest, _injection, complete_artifacts = _records()
    component_hashes = {
        name: stored.artifact.content_sha256
        for name, stored in complete_artifacts.items()
    }
    component_hashes["semantic_gate_response"] = None
    component_hashes["injection_artifact"] = None
    artifacts = {
        name: stored
        for name, stored in complete_artifacts.items()
        if component_hashes[name] is not None
    }
    manifest = tbm.build_decision_replay_manifest(
        session_id="session_legacy",
        decision_id="decision_legacy",
        usage_decision_id="usage_legacy",
        component_hashes=component_hashes,
        injection_artifact_id=None,
        completeness="legacy_partial",
        created_at=NOW,
    )

    export = tbm.build_replay_bundle_export(
        manifest=manifest,
        injection=None,
        artifacts=artifacts,
    )
    assert export.injection is None
    assert len(export.artifacts) == 6
    assert tbm.loads_replay_bundle_export(
        tbm.dumps_replay_bundle_export(export)
    ) == export


def test_sqlite_replay_repository_exports_through_reader_protocol():
    manifest, injection, artifacts = _records()
    with tbm.SQLiteReplayV3Repository.connect(
        initialize=True
    ) as repository:
        for name, stored in artifacts.items():
            if name != "injection_artifact":
                assert repository.store_artifact(
                    stored.artifact,
                    stored.content,
                )
        repository.store_bundle(
            injection,
            artifacts["injection_artifact"].content,
            manifest,
        )

        export = tbm.export_replay_bundle(
            repository,
            manifest.manifest_sha256,
            allowed_classifications=frozenset({"internal"}),
        )

    assert export.manifest == manifest
    assert tbm.verify_replay_bundle_export(export)


def test_replay_export_package_root_exports_are_intentional():
    expected = {
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
    }
    assert expected <= set(tbm.__all__)
    assert all(hasattr(tbm, name) for name in expected)


def test_replay_export_schema_example_and_domain_parser_agree():
    schema_names = (
        "decision_replay_manifest_v3.schema.json",
        "injection_artifact_v3.schema.json",
        "replay_bundle_export_v3.schema.json",
    )
    schemas = {
        name: json.loads(
            (ROOT / "schemas" / name).read_text(encoding="utf-8")
        )
        for name in schema_names
    }
    registry = Registry().with_resources(
        (
            schema["$id"],
            Resource.from_contents(schema),
        )
        for schema in schemas.values()
    )
    export_schema = schemas["replay_bundle_export_v3.schema.json"]
    example = json.loads(
        (
            ROOT / "examples" / "replay_bundle_export_v3.example.json"
        ).read_text(encoding="utf-8")
    )

    Draft202012Validator.check_schema(export_schema)
    Draft202012Validator(
        export_schema,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(example)
    parsed = tbm.parse_replay_bundle_export(example)
    assert parsed.export_sha256 == example["export_sha256"]
