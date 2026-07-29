from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm


ROOT = Path(__file__).resolve().parents[1]


def _rules() -> tuple[tbm.ModeMemoryRule, ...]:
    return (
        tbm.ModeMemoryRule("planning", ("semantic", "policy")),
        tbm.ModeMemoryRule("repair", ("procedural", "semantic", "policy")),
        tbm.ModeMemoryRule("debug", ("procedural", "episodic", "policy")),
        tbm.ModeMemoryRule("eval", ("procedural", "semantic")),
        tbm.ModeMemoryRule("production", ("procedural", "policy")),
    )


def _policy(
    *,
    ancestry_mode: tbm.AncestryMode = "required",
    ancestry_bypass_reason: str | None = None,
    minimum_fused_score: float = 0.25,
) -> tbm.RetrievalPolicyBundle:
    return tbm.build_retrieval_policy(
        policy_version="retrieval_policy_001",
        allowed_classifications=("internal", "confidential"),
        mode_memory_rules=_rules(),
        ancestry_mode=ancestry_mode,
        ancestry_bypass_reason=ancestry_bypass_reason,
        stage_weights=(
            ("metadata", 0.1),
            ("lexical", 0.2),
            ("semantic", 0.4),
            ("evidence_graph", 0.3),
        ),
        minimum_fused_score=minimum_fused_score,
        payload_budget_bytes=8_192,
    )


def test_retrieval_policy_round_trips_content_addressed_canonical_json():
    policy = _policy()
    document = tbm.dumps_retrieval_policy(policy)

    assert tbm.loads_retrieval_policy(document) == policy
    assert tbm.loads_retrieval_policy(document.encode()) == policy
    assert document == json.dumps(
        policy.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert policy.policy_id == tbm.retrieval_policy_id(
        {
            key: value
            for key, value in policy.to_dict().items()
            if key != "policy_id"
        }
    )
    assert policy.policy_sha256 == (
        "sha256:"
        + policy.policy_id.removeprefix("retrieval_policy_sha256_")
    )
    assert policy.allowed_types("repair") == frozenset(
        {"procedural", "semantic", "policy"}
    )
    assert tbm.RETRIEVAL_TASK_MODES == (
        "planning",
        "repair",
        "debug",
        "eval",
        "production",
    )
    assert tbm.RETRIEVAL_RANKING_STAGES == (
        "metadata",
        "lexical",
        "semantic",
        "evidence_graph",
    )


def test_retrieval_policy_canonical_example_matches_schema_and_parser():
    document = (
        ROOT / "examples" / "retrieval_policy_v3.example.json"
    ).read_text(encoding="utf-8")
    schema = json.loads(
        (
            ROOT / "schemas" / "retrieval_policy_v3.schema.json"
        ).read_text(encoding="utf-8")
    )

    Draft202012Validator(schema).validate(json.loads(document))
    policy = tbm.loads_retrieval_policy(document)
    assert policy.policy_id == (
        "retrieval_policy_sha256_"
        "8ec2355c7067f31cbd9533b31c301367c14b5a5dfb08f6584b6d02b7f69f0acd"
    )
    assert policy.payload_budget_bytes == 65_536


def test_retrieval_policy_builder_normalizes_order_and_numeric_values():
    policy = tbm.build_retrieval_policy(
        policy_version="retrieval_policy_001",
        allowed_classifications=("confidential", "internal"),
        mode_memory_rules=tuple(reversed(_rules())),
        ancestry_mode="disabled",
        ancestry_bypass_reason="Repository has no trustworthy Git graph.",
        stage_weights=(
            ("semantic", 1),
            ("metadata", 1),
            ("evidence_graph", 1),
            ("lexical", 1),
        ),
        minimum_fused_score=0,
        payload_budget_bytes=1,
    )

    assert policy.allowed_classifications == ("internal", "confidential")
    assert tuple(rule.task_mode for rule in policy.mode_memory_rules) == (
        "planning",
        "repair",
        "debug",
        "eval",
        "production",
    )
    assert policy.stage_weights == (
        ("metadata", 1.0),
        ("lexical", 1.0),
        ("semantic", 1.0),
        ("evidence_graph", 1.0),
    )
    assert policy.minimum_fused_score == 0.0
    assert tbm.loads_retrieval_policy(
        tbm.dumps_retrieval_policy(policy)
    ) == policy


def test_retrieval_policy_rejects_hash_tampering_and_incomplete_mode_rules():
    policy = _policy()
    with pytest.raises(tbm.RetrievalPolicyV3ContractError) as caught:
        replace(policy, policy_version="tampered")
    assert caught.value.code == "TBM_RETRIEVAL_POLICY_HASH_MISMATCH"

    with pytest.raises(tbm.RetrievalPolicyV3ContractError) as caught:
        tbm.build_retrieval_policy(
            policy_version="retrieval_policy_001",
            allowed_classifications=("internal",),
            mode_memory_rules=_rules()[:-1],
            ancestry_mode="required",
            ancestry_bypass_reason=None,
            stage_weights=policy.stage_weights,
            minimum_fused_score=0.25,
            payload_budget_bytes=8_192,
        )
    assert caught.value.code == "TBM_RETRIEVAL_POLICY_INVALID"


@pytest.mark.parametrize(
    "change",
    (
        {"ancestry_bypass_reason": "not allowed"},
        {"block_eval_leaking": False},
        {
            "stage_weights": (
                ("metadata", 0.0),
                ("lexical", 0.2),
                ("semantic", 0.4),
                ("evidence_graph", 0.3),
            )
        },
        {"minimum_fused_score": float("nan")},
        {"payload_budget_bytes": 0},
    ),
)
def test_retrieval_policy_rejects_unsafe_or_nonfinite_values(change):
    policy = _policy()
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        replace(policy, **change)


@pytest.mark.parametrize(
    "document",
    (
        '{"policy_id":"first","policy_id":"second"}',
        '{"value":NaN}',
        "[1,2,3]",
    ),
)
def test_retrieval_policy_rejects_non_strict_json(document: str):
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.loads_retrieval_policy(document)


def test_retrieval_policy_rejects_oversized_json_and_invalid_utf8():
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.loads_retrieval_policy(
            " " * (tbm.RETRIEVAL_POLICY_JSON_MAX_BYTES + 1)
        )
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.loads_retrieval_policy(b"\xff")


@pytest.mark.parametrize(
    ("task_mode", "memory_types"),
    (
        ("unknown", ("semantic",)),
        ("planning", ()),
        ("planning", ("unknown",)),
        ("planning", ("semantic", "semantic")),
        ("planning", ("policy", "semantic")),
    ),
)
def test_mode_memory_rule_rejects_invalid_or_noncanonical_values(
    task_mode: str,
    memory_types: tuple[str, ...],
) -> None:
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.ModeMemoryRule(task_mode, memory_types)  # type: ignore[arg-type]


def test_retrieval_policy_unknown_accessors_are_closed() -> None:
    policy = _policy()
    assert policy.allowed_types("unknown") == frozenset()  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="missing a stage"):
        policy.weight("unknown")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    (
        {"contract_version": "wrong"},
        {"policy_id": "wrong"},
        {"policy_version": " "},
        {"policy_version": "\ud800"},
        {"ancestry_mode": "unknown"},
        {"ancestry_mode": "disabled", "ancestry_bypass_reason": None},
        {
            "allowed_classifications": (
                "confidential",
                "internal",
            )
        },
        {"mode_memory_rules": tuple(reversed(_rules()))},
        {"stage_weights": (("metadata", 0.1),)},
    ),
)
def test_retrieval_policy_record_rejects_invalid_contract_fields(
    change: dict[str, object],
) -> None:
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        replace(_policy(), **change)


@pytest.mark.parametrize(
    "changes",
    (
        {"allowed_classifications": ["internal"]},
        {"mode_memory_rules": list(_rules())},
        {"stage_weights": list(_policy().stage_weights)},
        {"allowed_classifications": ("unknown",)},
        {"mode_memory_rules": (object(),)},
        {"stage_weights": (("unknown", 0.5),) * 4},
        {
            "stage_weights": (
                ("metadata", 0.1),
                ("metadata", 0.2),
                ("semantic", 0.4),
                ("evidence_graph", 0.3),
            )
        },
        {"minimum_fused_score": "0.5"},
        {"minimum_fused_score": 2.0},
    ),
)
def test_retrieval_policy_builder_rejects_invalid_components(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "policy_version": "retrieval_policy_001",
        "allowed_classifications": ("internal", "confidential"),
        "mode_memory_rules": _rules(),
        "ancestry_mode": "required",
        "ancestry_bypass_reason": None,
        "stage_weights": _policy().stage_weights,
        "minimum_fused_score": 0.25,
        "payload_budget_bytes": 8_192,
    }
    values.update(changes)
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.build_retrieval_policy(**values)  # type: ignore[arg-type]


def test_retrieval_policy_serializers_reject_wrong_types() -> None:
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.dumps_retrieval_policy(object())  # type: ignore[arg-type]
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.loads_retrieval_policy(1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("stage_weights", {"metadata": 1}),
        ("ancestry_bypass_reason", 1),
        ("block_eval_leaking", "true"),
        ("allowed_classifications", []),
        ("allowed_classifications", [1]),
        ("minimum_fused_score", "0.5"),
        ("minimum_fused_score", float("inf")),
        ("payload_budget_bytes", "1"),
    ),
)
def test_retrieval_policy_parser_rejects_invalid_field_shapes(
    field: str,
    value: object,
) -> None:
    payload = _policy().to_dict()
    payload[field] = value
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.parse_retrieval_policy(payload)


def test_retrieval_policy_parser_rejects_field_set_and_nonobject() -> None:
    payload = _policy().to_dict()
    payload["unexpected"] = True
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.parse_retrieval_policy(payload)
    with pytest.raises(tbm.RetrievalPolicyV3ContractError):
        tbm.parse_retrieval_policy([])
