from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from trace_backed_memory import usage_decision_v3


ROOT = Path(__file__).resolve().parents[1]
REVISION_ALLOWED = "memory_revision_sha256_" + "1" * 64
REVISION_BLOCKED = "memory_revision_sha256_" + "2" * 64


def _usage(
    *,
    replay_components: tuple[tuple[tbm.ReplayComponentName, str], ...] | None = None,
    injection_artifact_id: str | None = None,
) -> tbm.UsageDecision:
    injection = tbm.create_content_addressed_artifact(
        b"bounded snippet",
        media_type=tbm.INJECTION_ARTIFACT_MEDIA_TYPE,
        classification="internal",
        created_at="2026-07-30T01:00:00Z",
    )
    components = replay_components or tuple(
        (
            name,
            injection.content_sha256
            if name == "injection_artifact"
            else "sha256:" + f"{index + 3:x}" * 64,
        )
        for index, name in enumerate(tbm.REPLAY_COMPONENT_NAMES)
    )
    return tbm.build_usage_decision(
        session_id="gate_session_usage_001",
        decision_id="decision_usage_001",
        trace_id="trace_usage_001",
        run_id="run_usage_001",
        authorization_event_id="authz_sha256_" + "3" * 64,
        retrieval_snapshot_id="retrieval_snapshot_sha256_" + "4" * 64,
        system_gate_evaluation_id="system_gate_sha256_" + "5" * 64,
        semantic_gate_attempt_id="semantic_attempt_sha256_" + "6" * 64,
        candidate_memory_revision_ids=(
            REVISION_ALLOWED,
            REVISION_BLOCKED,
        ),
        system_allowed_memory_revision_ids=(REVISION_ALLOWED,),
        semantic_allowed_memory_revision_ids=(REVISION_ALLOWED,),
        final_memory_revision_ids=(REVISION_ALLOWED,),
        blocked_memory_revision_ids=(REVISION_BLOCKED,),
        system_blocked=((REVISION_BLOCKED, "memory_type_blocked", "system_policy_v1"),),
        reason="Only the reviewed procedural memory remains applicable.",
        risk="low",
        recommended_injection="summary",
        renderer_id="tbm.structured-memory-json",
        renderer_version="v1",
        policy_bundle_sha256="sha256:" + "7" * 64,
        injection_artifact_id=injection_artifact_id or injection.artifact_id,
        replay_components=components,
        created_at="2026-07-30T01:00:00+00:00",
    )


def test_usage_decision_round_trips_and_derives_exact_artifact() -> None:
    usage = _usage()

    assert tbm.loads_usage_decision(tbm.dumps_usage_decision(usage)) == usage
    stored = tbm.create_usage_decision_artifact(usage)
    assert tbm.loads_usage_decision_artifact(stored.content) == usage
    assert stored.artifact.artifact_id == tbm.usage_decision_artifact_id(
        usage.usage_decision_id
    )
    assert stored.artifact.content_sha256 == (
        "sha256:" + usage.usage_decision_id.removeprefix("usage_decision_sha256_")
    )


def test_usage_decision_rejects_hash_and_monotonicity_changes() -> None:
    usage = _usage()

    with pytest.raises(
        tbm.UsageDecisionV3Error,
        match="usage_decision_id does not match",
    ):
        replace(usage, reason="mutated")

    with pytest.raises(
        tbm.UsageDecisionV3Error,
        match="subset",
    ):
        replace(
            usage,
            semantic_allowed_memory_revision_ids=(REVISION_BLOCKED,),
        )


def test_usage_decision_rejects_duplicate_json_and_malformed_components() -> None:
    payload = tbm.dumps_usage_decision(_usage())
    duplicate = payload.replace(
        '"session_id":',
        '"session_id":"duplicate","session_id":',
        1,
    )
    with pytest.raises(tbm.UsageDecisionV3Error):
        tbm.loads_usage_decision(duplicate)

    usage = _usage()
    with pytest.raises(tbm.UsageDecisionV3Error):
        replace(
            usage,
            replay_components=(("retrieval_snapshot",),),  # type: ignore[arg-type]
        )


def test_usage_decision_schema_example_and_public_exports() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "usage_decision_v3.schema.json").read_text(encoding="utf-8")
    )
    example_bytes = (ROOT / "examples" / "usage_decision_v3.example.json").read_bytes()
    example = tbm.loads_usage_decision(example_bytes)

    assert schema["$id"].endswith("/usage_decision_v3.schema.json")
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(example.to_dict())
    assert json.loads(example_bytes) == example.to_dict()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(example.to_dict())

    long_reason = "x" * tbm.MEMORY_DECISION_REASON_MAX_CHARS
    unsigned = example.to_dict()
    unsigned.pop("usage_decision_id")
    unsigned["reason"] = long_reason
    bounded = replace(
        example,
        usage_decision_id=tbm.usage_decision_id(unsigned),
        reason=long_reason,
    )
    validator.validate(bounded.to_dict())

    for invalid_id in (" session", "session ", "session\nid"):
        invalid_payload = example.to_dict()
        invalid_payload["session_id"] = invalid_id
        assert tuple(validator.iter_errors(invalid_payload))
        with pytest.raises(tbm.UsageDecisionV3Error):
            tbm.loads_usage_decision(json.dumps(invalid_payload).encode("utf-8"))
    assert (
        tbm.read_packaged_resource("schemas/usage_decision_v3.schema.json")
        == (ROOT / "schemas" / "usage_decision_v3.schema.json").read_bytes()
    )
    assert (
        tbm.read_packaged_resource("examples/usage_decision_v3.example.json")
        == example_bytes
    )
    for name in usage_decision_v3.__all__:
        assert name in tbm.__all__
