from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.semantic_gate_artifact_v3 as artifact_module


NOW = "2026-07-28T00:00:00Z"
ROOT = Path(__file__).resolve().parents[1]
PROMPT = b"Review the bounded candidate set."
RESPONSE = b'{"decision":"allow","reason":"applicable"}'


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _attempt(*, succeeded: bool = True) -> tbm.SemanticGateAttempt:
    return tbm.build_semantic_gate_attempt(
        session_id="gate_session_001",
        retrieval_snapshot_id="retrieval_snapshot_sha256_" + "1" * 64,
        system_gate_evaluation_id="system_gate_sha256_" + "2" * 64,
        sequence=1,
        previous_attempt_id=None,
        provider_id="provider_001",
        model_id="gate_model",
        model_version="2026-07-01",
        endpoint_id="deployment_001",
        prompt_template_id="semantic_gate_prompt",
        prompt_template_version="3.0",
        prompt_artifact_sha256=_digest(PROMPT),
        response_artifact_sha256=_digest(RESPONSE) if succeeded else None,
        generation_config_sha256="sha256:" + "3" * 64,
        provider_request_id="provider_request_001",
        status="succeeded" if succeeded else "failed",
        decision_id="decision_001" if succeeded else None,
        final_allowed_revision_ids=(
            ("memory_revision_sha256_" + "4" * 64,) if succeeded else ()
        ),
        final_blocked_revision_ids=(),
        reason="Applicable to this run." if succeeded else None,
        risk="low" if succeeded else None,
        recommended_injection="summary" if succeeded else None,
        error_code=None if succeeded else "provider_timeout",
        input_tokens=100,
        output_tokens=25 if succeeded else None,
        latency_ms=500,
        started_at="2026-07-28T00:00:01Z",
        finished_at="2026-07-28T00:00:02Z",
    )


def _binding(
    role: tbm.SemanticGateArtifactRole = "prompt",
) -> tbm.SemanticGateArtifactBinding:
    content = PROMPT if role == "prompt" else RESPONSE
    return tbm.create_semantic_gate_artifact_binding(
        _attempt(),
        content,
        artifact_role=role,
        media_type=(
            "text/plain; charset=utf-8"
            if role == "prompt"
            else "application/json"
        ),
        classification="internal",
        created_at=NOW,
    )


def test_semantic_gate_artifact_binding_round_trip_and_exact_bytes() -> None:
    attempt = _attempt()
    prompt = _binding("prompt")
    response = _binding("response")

    assert tbm.loads_semantic_gate_artifact_binding(
        tbm.dumps_semantic_gate_artifact_binding(prompt)
    ) == prompt
    assert json.loads(
        tbm.dumps_semantic_gate_artifact_binding(response)
    ) == response.to_dict()
    assert tbm.verify_semantic_gate_artifact_binding(
        prompt,
        attempt,
        PROMPT,
    )
    assert tbm.verify_semantic_gate_artifact_binding(
        response,
        attempt,
        RESPONSE,
    )
    assert not tbm.verify_semantic_gate_artifact_binding(
        response,
        attempt,
        b'{"decision":"block"}',
    )
    with pytest.raises(FrozenInstanceError):
        prompt.attempt_id = "changed"  # type: ignore[misc]


def test_binding_creation_rejects_digest_and_attempt_mismatch() -> None:
    attempt = _attempt()
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="digest does not match",
    ):
        tbm.create_semantic_gate_artifact_binding(
            attempt,
            b"different prompt",
            artifact_role="prompt",
            media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
            classification="internal",
            created_at=NOW,
        )

    other_values = {
        key: value
        for key, value in attempt.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    other_values["session_id"] = "gate_session_002"
    other = tbm.build_semantic_gate_attempt(
        **other_values,
    )
    assert not tbm.verify_semantic_gate_artifact_binding(
        _binding(),
        other,
        PROMPT,
    )


def test_failed_attempt_requires_prompt_and_forbids_response() -> None:
    failed = _attempt(succeeded=False)
    prompt = tbm.create_semantic_gate_artifact_binding(
        failed,
        PROMPT,
        artifact_role="prompt",
        media_type="text/plain; charset=utf-8",
        classification="internal",
        created_at=NOW,
    )

    assert tbm.verify_semantic_gate_artifact_binding(
        prompt,
        failed,
        PROMPT,
    )
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="forbids response",
    ):
        tbm.create_semantic_gate_artifact_binding(
            failed,
            RESPONSE,
            artifact_role="response",
            media_type="application/json",
            classification="internal",
            created_at=NOW,
        )


def test_sensitive_artifacts_require_encryption_metadata() -> None:
    attempt = _attempt()
    with pytest.raises(
        tbm.ReplayContractError,
        match="require encryption_key_id",
    ):
        tbm.create_semantic_gate_artifact_binding(
            attempt,
            PROMPT,
            artifact_role="prompt",
            media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
            classification="restricted",
            created_at=NOW,
        )

    binding = tbm.create_semantic_gate_artifact_binding(
        attempt,
        PROMPT,
        artifact_role="prompt",
        media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
        classification="restricted",
        created_at=NOW,
        encryption_key_id="kms_key_001",
        redaction_policy_id="redaction_policy_001",
    )
    assert binding.artifact.encryption_key_id == "kms_key_001"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "tbm.semantic-gate-artifact.v4"}, "supported"),
        ({"artifact_kind": "injection"}, "artifact_kind"),
        ({"artifact_role": "tool"}, "artifact_role"),
        ({"attempt_id": "attempt_001"}, "attempt_id"),
        ({"artifact": object()}, "artifact must be"),
    ],
)
def test_binding_shape_is_strict(changes: dict[str, object], message: str) -> None:
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match=message,
    ):
        replace(_binding(), **changes)


def test_binding_rejects_empty_and_role_size_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_artifact = tbm.create_content_addressed_artifact(
        b"",
        media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
        classification="internal",
        created_at=NOW,
    )
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="must not be empty",
    ):
        tbm.SemanticGateArtifactBinding(
            artifact_role="prompt",
            attempt_id=_attempt().attempt_id,
            artifact=empty_artifact,
        )

    monkeypatch.setattr(
        artifact_module,
        "SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES",
        len(PROMPT) - 1,
    )
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="maximum size",
    ):
        _binding()


def test_prompt_requires_utf8_and_character_limit() -> None:
    attempt = _attempt()
    for invalid in (b"\xff", b"x" * 32_001):
        with pytest.raises(
            tbm.SemanticGateArtifactContractError,
            match="invalid or exceeds",
        ):
            tbm.create_semantic_gate_artifact_binding(
                attempt,
                invalid,
                artifact_role="prompt",
                media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
                classification="internal",
                created_at=NOW,
            )

    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="media_type",
    ):
        replace(
            _binding(),
            artifact=replace(_binding().artifact, media_type="text/plain"),
        )


def test_stored_artifact_validates_exact_content() -> None:
    binding = _binding()
    stored = tbm.StoredSemanticGateArtifact(binding, PROMPT)
    assert stored.content == PROMPT

    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="does not match",
    ):
        tbm.StoredSemanticGateArtifact(binding, b"tampered")
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="content must be bytes",
    ):
        tbm.StoredSemanticGateArtifact(
            binding,
            "prompt",  # type: ignore[arg-type]
        )


def test_json_parser_rejects_unknown_duplicate_and_unbounded_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _binding().to_dict()
    payload["unknown"] = True
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="unknown field",
    ):
        tbm.parse_semantic_gate_artifact_binding(payload)

    relinked = _binding().to_dict()
    relinked_artifact = relinked["artifact"]
    assert isinstance(relinked_artifact, dict)
    relinked_artifact["artifact_id"] = "artifact_sha256_" + "0" * 64
    with pytest.raises(tbm.ReplayContractError, match="derived"):
        tbm.parse_semantic_gate_artifact_binding(relinked)

    duplicate = (
        tbm.dumps_semantic_gate_artifact_binding(_binding())[:-1]
        + ',"attempt_id":"semantic_attempt_sha256_'
        + "0" * 64
        + '"}'
    )
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="duplicate",
    ):
        tbm.loads_semantic_gate_artifact_binding(duplicate)

    monkeypatch.setattr(
        artifact_module,
        "SEMANTIC_GATE_ARTIFACT_JSON_MAX_BYTES",
        16,
    )
    with pytest.raises(
        tbm.SemanticGateArtifactContractError,
        match="too large",
    ):
        tbm.loads_semantic_gate_artifact_binding(
            tbm.dumps_semantic_gate_artifact_binding(_binding())
        )


def test_public_exports_are_intentional() -> None:
    expected = {
        "SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION",
        "SEMANTIC_GATE_ARTIFACT_JSON_MAX_BYTES",
        "SEMANTIC_GATE_ARTIFACT_JSON_MAX_DEPTH",
        "SEMANTIC_GATE_ARTIFACT_JSON_MAX_NODES",
        "SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES",
        "SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE",
        "SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES",
        "SemanticGateArtifactBinding",
        "SemanticGateArtifactContractError",
        "SemanticGateArtifactRole",
        "StoredSemanticGateArtifact",
        "create_semantic_gate_artifact_binding",
        "dumps_semantic_gate_artifact_binding",
        "loads_semantic_gate_artifact_binding",
        "parse_semantic_gate_artifact_binding",
        "verify_semantic_gate_artifact_binding",
    }
    assert expected <= set(tbm.__all__)
    assert all(hasattr(tbm, name) for name in expected)


def test_canonical_schema_example_and_packaged_copies_are_aligned() -> None:
    schema_path = ROOT / "schemas" / "semantic_gate_artifact_v3.schema.json"
    example_path = (
        ROOT / "examples" / "semantic_gate_artifact_v3.example.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    example_text = example_path.read_text(encoding="utf-8")
    example = json.loads(example_text)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["contract_version"]["const"] == (
        tbm.SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION
    )
    assert tbm.parse_semantic_gate_artifact_binding(example).to_dict() == (
        example
    )
    assert tbm.read_packaged_resource(
        "schemas/semantic_gate_artifact_v3.schema.json"
    ) == schema_path.read_bytes()
    assert tbm.read_packaged_resource(
        "examples/semantic_gate_artifact_v3.example.json"
    ) == example_path.read_bytes()
