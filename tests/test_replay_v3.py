from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import re

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.replay_v3 as replay_module


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-27T00:00:00Z"
HASHES = {
    name: "sha256:" + f"{index:x}" * 64
    for index, name in enumerate(
        tbm.REPLAY_COMPONENT_NAMES,
        start=1,
    )
}


def _injection(
    snippet: str = "Use the exact verified repository-scoped memory.",
) -> tbm.InjectionArtifact:
    return tbm.create_injection_artifact(
        snippet,
        session_id="gate_session_001",
        decision_id="decision_001",
        usage_decision_id="usage_decision_001",
        memory_revision_ids=(
            "memory_revision_001",
            "memory_revision_002",
        ),
        renderer_id="renderer_001",
        renderer_version="1.0.0",
        policy_bundle_sha256="sha256:" + "a" * 64,
        rendered_at=NOW,
    )


def _complete_manifest() -> tbm.DecisionReplayManifest:
    injection = _injection()
    components = dict(HASHES)
    components["injection_artifact"] = (
        injection.artifact.content_sha256
    )
    return tbm.build_decision_replay_manifest(
        session_id=injection.session_id,
        decision_id=injection.decision_id,
        usage_decision_id=injection.usage_decision_id,
        component_hashes=components,
        injection_artifact_id=injection.artifact.artifact_id,
        completeness="complete",
        created_at=NOW,
    )


def test_content_addressed_artifact_derives_identity_and_verifies_bytes():
    content = b"exact artifact bytes"
    artifact = tbm.create_content_addressed_artifact(
        content,
        media_type="application/octet-stream",
        classification="internal",
        created_at=NOW,
    )

    assert artifact.artifact_id == tbm.artifact_id_from_sha256(
        artifact.content_sha256
    )
    assert artifact.size_bytes == len(content)
    assert tbm.verify_artifact_content(artifact, content) is True
    assert (
        tbm.verify_artifact_content(artifact, b"other artifact bytes")
        is False
    )
    assert tbm.verify_artifact_content(artifact, b"short") is False
    with pytest.raises(FrozenInstanceError):
        artifact.size_bytes = 0  # type: ignore[misc]


def test_artifact_classification_requires_encryption_for_sensitive_data():
    confidential = tbm.create_content_addressed_artifact(
        b"encrypted elsewhere",
        media_type="application/octet-stream",
        classification="confidential",
        created_at=NOW,
        encryption_key_id="kms_key_001",
        redaction_policy_id="redaction_policy_001",
    )

    assert confidential.encryption_key_id == "kms_key_001"
    with pytest.raises(
        tbm.ReplayContractError,
        match="require encryption_key_id",
    ):
        tbm.create_content_addressed_artifact(
            b"sensitive",
            media_type="application/octet-stream",
            classification="restricted",
            created_at=NOW,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"artifact_id": "artifact_sha256_" + "0" * 64}, "derived"),
        ({"content_sha256": "not-a-digest"}, "SHA-256"),
        ({"size_bytes": True}, "size_bytes"),
        ({"size_bytes": -1}, "size_bytes"),
        ({"media_type": " "}, "media_type"),
        ({"classification": "secret"}, "supported data class"),
        ({"created_at": "not-a-time"}, "created_at"),
        ({"encryption_key_id": " "}, "encryption_key_id"),
    ],
)
def test_content_addressed_artifact_validation_is_strict(changes, message):
    artifact = tbm.create_content_addressed_artifact(
        b"content",
        media_type="application/octet-stream",
        classification="internal",
        created_at=NOW,
    )
    with pytest.raises(tbm.ReplayContractError, match=message):
        replace(artifact, **changes)


def test_content_artifact_creation_and_verification_reject_wrong_types(
    monkeypatch,
):
    with pytest.raises(tbm.ReplayContractError, match="content must be bytes"):
        tbm.create_content_addressed_artifact(  # type: ignore[arg-type]
            "text",
            media_type="text/plain",
            classification="public",
            created_at=NOW,
        )
    monkeypatch.setattr(replay_module, "ARTIFACT_MAX_BYTES", 3)
    with pytest.raises(tbm.ReplayContractError, match="maximum size"):
        tbm.create_content_addressed_artifact(
            b"four",
            media_type="application/octet-stream",
            classification="internal",
            created_at=NOW,
        )
    artifact = tbm.create_content_addressed_artifact(
        b"ok",
        media_type="application/octet-stream",
        classification="internal",
        created_at=NOW,
    )
    with pytest.raises(tbm.ReplayContractError, match="artifact must be"):
        tbm.verify_artifact_content(  # type: ignore[arg-type]
            object(),
            b"ok",
        )
    with pytest.raises(tbm.ReplayContractError, match="content must be bytes"):
        tbm.verify_artifact_content(
            artifact,
            "ok",  # type: ignore[arg-type]
        )


def test_injection_artifact_hashes_exact_utf8_snippet():
    snippet = "规则：只使用最终允许的记忆。"
    injection = _injection(snippet)

    assert injection.artifact.media_type == (
        tbm.INJECTION_ARTIFACT_MEDIA_TYPE
    )
    assert injection.artifact.size_bytes == len(snippet.encode("utf-8"))
    assert tbm.verify_injection_artifact(injection, snippet) is True
    assert tbm.verify_injection_artifact(injection, snippet + "!") is False
    assert injection.to_dict()["memory_revision_ids"] == [
        "memory_revision_001",
        "memory_revision_002",
    ]


def test_injection_artifact_supports_encrypted_restricted_metadata():
    injection = tbm.create_injection_artifact(
        "restricted snippet",
        session_id="session",
        decision_id="decision",
        usage_decision_id="usage",
        memory_revision_ids=(),
        renderer_id="renderer",
        renderer_version="1",
        policy_bundle_sha256="sha256:" + "a" * 64,
        rendered_at=NOW,
        classification="restricted",
        encryption_key_id="kms_key",
        redaction_policy_id="redaction_policy",
    )

    assert injection.artifact.classification == "restricted"
    assert injection.artifact.encryption_key_id == "kms_key"


def test_injection_artifact_rejects_malformed_and_oversized_content(
    monkeypatch,
):
    with pytest.raises(tbm.ReplayContractError, match="snippet must be"):
        tbm.create_injection_artifact(  # type: ignore[arg-type]
            b"bytes",
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            memory_revision_ids=(),
            renderer_id="renderer",
            renderer_version="1",
            policy_bundle_sha256="sha256:" + "a" * 64,
            rendered_at=NOW,
        )
    with pytest.raises(tbm.ReplayContractError, match="valid Unicode"):
        _injection("\ud800")
    monkeypatch.setattr(
        replay_module,
        "INJECTION_ARTIFACT_MAX_BYTES",
        3,
    )
    with pytest.raises(tbm.ReplayContractError, match="maximum size"):
        _injection("four")

    injection = _injection("ok")
    with pytest.raises(tbm.ReplayContractError, match="injection must be"):
        tbm.verify_injection_artifact(  # type: ignore[arg-type]
            object(),
            "ok",
        )
    with pytest.raises(tbm.ReplayContractError, match="snippet must be"):
        tbm.verify_injection_artifact(
            injection,
            b"ok",  # type: ignore[arg-type]
        )
    assert tbm.verify_injection_artifact(injection, "\ud800") is False


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "wrong"}, "contract_version"),
        ({"artifact_kind": "trace"}, "artifact_kind"),
        ({"artifact": object()}, "artifact must be"),
        ({"session_id": " "}, "session_id"),
        ({"memory_revision_ids": ("same", "same")}, "duplicates"),
        ({"renderer_version": " "}, "renderer_version"),
        ({"policy_bundle_sha256": "bad"}, "SHA-256"),
        ({"rendered_at": "2026-07-27T00:00:01Z"}, "must equal"),
    ],
)
def test_injection_artifact_record_validation_is_strict(changes, message):
    with pytest.raises(tbm.ReplayContractError, match=message):
        replace(_injection(), **changes)


def test_injection_artifact_rejects_wrong_descriptor_media_and_size(
    monkeypatch,
):
    injection = _injection("content")
    with pytest.raises(tbm.ReplayContractError, match="media_type"):
        replace(
            injection,
            artifact=replace(
                injection.artifact,
                media_type="application/json",
            ),
        )
    monkeypatch.setattr(
        replay_module,
        "INJECTION_ARTIFACT_MAX_BYTES",
        1,
    )
    with pytest.raises(tbm.ReplayContractError, match="maximum size"):
        replace(injection)


def test_complete_replay_manifest_binds_every_component_and_own_hash():
    manifest = _complete_manifest()

    assert manifest.completeness == "complete"
    assert manifest.missing_components == ()
    assert tuple(dict(manifest.components)) == tbm.REPLAY_COMPONENT_NAMES
    assert manifest.manifest_sha256.startswith("sha256:")
    assert tbm.loads_decision_replay_manifest(
        tbm.dumps_decision_replay_manifest(manifest)
    ) == manifest
    assert manifest.to_dict()["components"] == dict(manifest.components)

    with pytest.raises(
        tbm.ReplayContractError,
        match="manifest_sha256",
    ) as mismatch:
        replace(manifest, decision_id="different_decision")
    assert mismatch.value.code == "TBM_REPLAY_HASH_MISMATCH"


def test_legacy_partial_manifest_derives_exact_missing_components():
    components = dict(HASHES)
    components["semantic_gate_response"] = None
    components["injection_artifact"] = None
    manifest = tbm.build_decision_replay_manifest(
        session_id="session",
        decision_id="decision",
        usage_decision_id="usage",
        component_hashes=components,
        injection_artifact_id=None,
        completeness="legacy_partial",
        created_at=NOW,
    )

    assert manifest.missing_components == (
        "semantic_gate_response",
        "injection_artifact",
    )
    assert manifest.injection_artifact_id is None


def test_replay_manifest_rejects_false_completeness_and_injection_links():
    components = dict(HASHES)
    components["semantic_gate_response"] = None
    with pytest.raises(
        tbm.ReplayContractError,
        match="complete replay manifest cannot omit",
    ):
        tbm.build_decision_replay_manifest(
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            component_hashes=components,
            injection_artifact_id=tbm.artifact_id_from_sha256(
                components["injection_artifact"]
            ),
            completeness="complete",
            created_at=NOW,
        )
    with pytest.raises(
        tbm.ReplayContractError,
        match="legacy_partial replay manifest must omit",
    ):
        tbm.build_decision_replay_manifest(
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            component_hashes=dict(HASHES),
            injection_artifact_id=tbm.artifact_id_from_sha256(
                HASHES["injection_artifact"]
            ),
            completeness="legacy_partial",
            created_at=NOW,
        )

    complete = _complete_manifest()
    with pytest.raises(
        tbm.ReplayContractError,
        match="recorded together",
    ):
        replace(complete, injection_artifact_id=None)
    with pytest.raises(
        tbm.ReplayContractError,
        match="must match",
    ):
        replace(
            complete,
            injection_artifact_id="artifact_sha256_" + "0" * 64,
        )


def test_replay_manifest_component_mapping_is_fixed_and_strict():
    complete = dict(HASHES)
    missing = dict(complete)
    del missing["renderer"]
    with pytest.raises(tbm.ReplayContractError, match="missing field"):
        tbm.build_decision_replay_manifest(
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            component_hashes=missing,
            injection_artifact_id=None,
            completeness="legacy_partial",
            created_at=NOW,
        )
    unknown = dict(complete)
    unknown["other"] = HASHES["renderer"]
    with pytest.raises(tbm.ReplayContractError, match="unknown field"):
        tbm.build_decision_replay_manifest(
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            component_hashes=unknown,
            injection_artifact_id=None,
            completeness="legacy_partial",
            created_at=NOW,
        )
    malformed = dict(complete)
    malformed["renderer"] = "bad"
    with pytest.raises(tbm.ReplayContractError, match="SHA-256"):
        tbm.build_decision_replay_manifest(
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            component_hashes=malformed,
            injection_artifact_id=None,
            completeness="legacy_partial",
            created_at=NOW,
        )
    with pytest.raises(tbm.ReplayContractError, match="JSON object"):
        tbm.build_decision_replay_manifest(
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            component_hashes=[],  # type: ignore[arg-type]
            injection_artifact_id=None,
            completeness="legacy_partial",
            created_at=NOW,
        )
    mixed_keys = dict(complete)
    mixed_keys[1] = HASHES["renderer"]  # type: ignore[index]
    with pytest.raises(tbm.ReplayContractError, match="keys must be strings"):
        tbm.build_decision_replay_manifest(
            session_id="session",
            decision_id="decision",
            usage_decision_id="usage",
            component_hashes=mixed_keys,  # type: ignore[arg-type]
            injection_artifact_id=None,
            completeness="legacy_partial",
            created_at=NOW,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "wrong"}, "contract_version"),
        ({"manifest_kind": "other"}, "manifest_kind"),
        ({"manifest_sha256": "bad"}, "SHA-256"),
        ({"session_id": " "}, "session_id"),
        ({"components": []}, "components must be a tuple"),
        (
            {"components": (("renderer", HASHES["renderer"]),)},
            "canonical component order",
        ),
        ({"completeness": "unknown"}, "completeness"),
        ({"missing_components": []}, "must be a tuple"),
        (
            {"missing_components": ("unknown",)},
            "replay component names",
        ),
        (
            {"missing_components": ("renderer", "renderer")},
            "must not contain duplicates",
        ),
        ({"created_at": "not-a-time"}, "created_at"),
    ],
)
def test_replay_manifest_record_validation_is_strict(changes, message):
    with pytest.raises(tbm.ReplayContractError, match=message):
        replace(_complete_manifest(), **changes)


def test_replay_json_round_trip_is_canonical_and_strict():
    injection = _injection("exact")
    serialized = tbm.dumps_injection_artifact(injection)
    assert tbm.loads_injection_artifact(serialized) == injection
    assert tbm.loads_injection_artifact(
        serialized.encode("utf-8")
    ) == injection
    assert serialized == json.dumps(
        json.loads(serialized),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    offset = replace(
        injection,
        rendered_at="2026-07-27T08:00:00+08:00",
        artifact=replace(
            injection.artifact,
            created_at="2026-07-27T08:00:00+08:00",
        ),
    )
    assert offset.to_dict()["rendered_at"] == NOW


def test_replay_json_rejects_duplicates_bounds_and_wrong_documents(
    monkeypatch,
):
    with pytest.raises(
        tbm.ReplayContractError,
        match="duplicate object key",
    ):
        tbm.loads_injection_artifact('{"x":1,"x":2}')
    with pytest.raises(tbm.ReplayContractError, match="non-finite"):
        tbm.loads_decision_replay_manifest('{"value":NaN}')
    with pytest.raises(tbm.ReplayContractError, match="invalid UTF-8"):
        tbm.loads_injection_artifact(b"\xff")
    with pytest.raises(tbm.ReplayContractError, match="maximum depth"):
        tbm.loads_injection_artifact(
            '{"value":' + "[" * 34 + "0" + "]" * 34 + "}"
        )
    monkeypatch.setattr(replay_module, "REPLAY_JSON_MAX_BYTES", 20)
    with pytest.raises(tbm.ReplayContractError, match="maximum size"):
        tbm.loads_injection_artifact('{"value":"' + "x" * 20 + '"}')
    with pytest.raises(tbm.ReplayContractError, match="str or bytes"):
        tbm.loads_injection_artifact(7)  # type: ignore[arg-type]
    with pytest.raises(tbm.ReplayContractError, match="one object"):
        tbm.loads_injection_artifact("[]")


def test_replay_parsers_reject_unknown_missing_and_wrong_types():
    injection = _injection()
    payload = injection.to_dict()
    payload["unknown"] = True
    with pytest.raises(tbm.ReplayContractError, match="unknown field"):
        tbm.parse_injection_artifact(payload)

    with pytest.raises(tbm.ReplayContractError, match="keys must be strings"):
        tbm.parse_injection_artifact(  # type: ignore[arg-type]
            {"x": 1, 1: 2}
        )

    payload = injection.to_dict()
    del payload["renderer_id"]
    with pytest.raises(tbm.ReplayContractError, match="missing field"):
        tbm.parse_injection_artifact(payload)

    payload = injection.to_dict()
    payload["artifact"] = []
    with pytest.raises(tbm.ReplayContractError, match="JSON object"):
        tbm.parse_injection_artifact(payload)

    payload = injection.to_dict()
    payload["artifact"]["size_bytes"] = False
    with pytest.raises(tbm.ReplayContractError, match="integer"):
        tbm.parse_injection_artifact(payload)

    payload = injection.to_dict()
    payload["memory_revision_ids"] = "memory"
    with pytest.raises(tbm.ReplayContractError, match="array of strings"):
        tbm.parse_injection_artifact(payload)

    manifest_payload = _complete_manifest().to_dict()
    manifest_payload["injection_artifact_id"] = 7
    with pytest.raises(tbm.ReplayContractError, match="string or null"):
        tbm.parse_decision_replay_manifest(manifest_payload)


def test_replay_dump_and_verify_interfaces_reject_wrong_types():
    with pytest.raises(tbm.ReplayContractError, match="injection must be"):
        tbm.dumps_injection_artifact(object())  # type: ignore[arg-type]
    with pytest.raises(tbm.ReplayContractError, match="manifest must be"):
        tbm.dumps_decision_replay_manifest(object())  # type: ignore[arg-type]
    with pytest.raises(tbm.ReplayContractError, match="content_sha256"):
        tbm.artifact_id_from_sha256("bad")


def test_replay_schemas_examples_and_public_exports():
    injection_schema = json.loads(
        (ROOT / "schemas" / "injection_artifact_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_schema = json.loads(
        (ROOT / "schemas" / "decision_replay_manifest_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )
    injection_example = json.loads(
        (ROOT / "examples" / "injection_artifact_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_example = json.loads(
        (
            ROOT
            / "examples"
            / "decision_replay_manifest_v3.example.json"
        ).read_text(encoding="utf-8")
    )

    assert injection_schema["additionalProperties"] is False
    assert manifest_schema["additionalProperties"] is False
    assert (
        injection_schema["properties"]["contract_version"]["const"]
        == tbm.REPLAY_CONTRACT_VERSION
    )
    assert (
        manifest_schema["properties"]["components"]["required"]
        == list(tbm.REPLAY_COMPONENT_NAMES)
    )
    assert any(
        rule.get("if", {})
        .get("properties", {})
        .get("completeness", {})
        .get("const")
        == "legacy_partial"
        and rule["then"]["properties"]["missing_components"]["minItems"] == 1
        for rule in manifest_schema["allOf"]
    )
    component_membership_rules = [
        rule
        for rule in manifest_schema["allOf"]
        if "components"
        in rule.get("if", {}).get("properties", {})
        and "missing_components"
        in rule.get("then", {}).get("properties", {})
    ]
    assert len(component_membership_rules) == len(
        tbm.REPLAY_COMPONENT_NAMES
    )
    timestamp_pattern = injection_schema["$defs"]["timestamp"]["pattern"]
    assert re.fullmatch(
        timestamp_pattern.removeprefix("^").removesuffix("$"),
        "2026-07-27T00:00:00+99:99",
    ) is None
    assert tbm.parse_injection_artifact(
        injection_example
    ).to_dict() == injection_example
    assert tbm.parse_decision_replay_manifest(
        manifest_example
    ).to_dict() == manifest_example

    for name in (
        "ContentAddressedArtifact",
        "DecisionReplayManifest",
        "InjectionArtifact",
        "ReplayContractError",
        "StoredReplayArtifact",
        "artifact_id_from_sha256",
        "build_decision_replay_manifest",
        "create_content_addressed_artifact",
        "create_injection_artifact",
        "verify_artifact_content",
        "verify_injection_artifact",
    ):
        assert name in tbm.__all__
