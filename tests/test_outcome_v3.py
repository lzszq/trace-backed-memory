from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.gate_session_v3 import (
    create_gate_session,
    transition_gate_session,
)
from trace_backed_memory.outcome_v3 import (
    OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
    OUTCOME_JSON_MAX_BYTES,
    OUTCOME_MAX_LATENCY_MS,
    RUN_OUTCOME_CONTRACT_VERSION,
    OutcomeAttribution,
    OutcomeContractError,
    RunOutcome,
    build_outcome_attribution,
    build_run_outcome,
    dumps_outcome_attribution,
    dumps_run_outcome,
    loads_outcome_attribution,
    loads_run_outcome,
    outcome_attribution_id,
    parse_outcome_attribution,
    parse_run_outcome,
    run_outcome_id,
    verify_outcome_attribution,
    verify_run_outcome,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64
REVISION_B = "memory_revision_sha256_" + "2" * 64
ROOT = Path(__file__).resolve().parents[1]


def _outcome(**overrides):
    values = {
        "session_id": "gate_session_001",
        "trace_id": "trace_001",
        "run_id": "run_001",
        "usage_decision_id": "usage_decision_001",
        "result": "pass",
        "evaluator_id": "evaluation_service",
        "evaluator_version": "1.2.0",
        "output_sha256": DIGEST_A,
        "evidence_artifact_sha256s": (DIGEST_B,),
        "measured_at": "2026-07-27T00:06:00Z",
    }
    values.update(overrides)
    return build_run_outcome(**values)


def _attribution(**overrides):
    values = {
        "run_outcome_id_value": _outcome().run_outcome_id,
        "usage_decision_id": "usage_decision_001",
        "memory_revision_ids": (REVISION_A,),
        "claim_strength": "association",
        "effect": "unknown",
        "method": "runtime_observation",
        "evaluator_id": "outcome_observer",
        "evaluator_version": "1.0.0",
        "evidence_artifact_sha256s": (DIGEST_A,),
        "confidence": 0.5,
        "reason": "The memory revision was present in the completed run.",
        "recorded_at": "2026-07-27T00:07:00Z",
    }
    values.update(overrides)
    return build_outcome_attribution(**values)


def _completed_session(run_outcome_id_value: str):
    created = create_gate_session(
        session_id="gate_session_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=DIGEST_A,
        idempotency_key="request-001",
        created_at="2026-07-27T00:00:00Z",
        expires_at="2026-07-27T01:00:00Z",
    )
    prepared = transition_gate_session(
        created,
        "prepared",
        expected_version=1,
        updated_at="2026-07-27T00:01:00Z",
        lease_expires_at="2026-07-27T00:20:00Z",
        retrieval_snapshot_id="retrieval_001",
        system_gate_evaluation_id="system_gate_001",
    )
    awaiting = transition_gate_session(
        prepared,
        "awaiting_decision",
        expected_version=2,
        updated_at="2026-07-27T00:02:00Z",
    )
    decided = transition_gate_session(
        awaiting,
        "decided",
        expected_version=3,
        updated_at="2026-07-27T00:03:00Z",
        semantic_gate_attempt_ids=("semantic_attempt_001",),
        decision_id="decision_001",
    )
    finalized = transition_gate_session(
        decided,
        "finalized",
        expected_version=4,
        updated_at="2026-07-27T00:04:00Z",
        final_memory_revision_ids=(REVISION_A,),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_decision_001",
    )
    executing = transition_gate_session(
        finalized,
        "executing",
        expected_version=5,
        updated_at="2026-07-27T00:05:00Z",
    )
    return transition_gate_session(
        executing,
        "completed",
        expected_version=6,
        updated_at="2026-07-27T00:06:00Z",
        run_outcome_id=run_outcome_id_value,
    )


def test_run_outcome_is_content_addressed_and_canonical():
    first = _outcome(evidence_artifact_sha256s=(DIGEST_B, DIGEST_A))
    second = _outcome(evidence_artifact_sha256s=(DIGEST_A, DIGEST_B))
    assert first == second
    assert first.contract_version == RUN_OUTCOME_CONTRACT_VERSION
    assert first.run_outcome_id == run_outcome_id(
        first.to_dict(include_id=False)
    )
    assert json.loads(dumps_run_outcome(first)) == first.to_dict()
    with pytest.raises(FrozenInstanceError):
        first.result = "fail"  # type: ignore[misc]


def test_run_outcome_round_trips_strict_json():
    outcome = _outcome(
        result="error",
        error_code="TOOL_FAILED",
        latency_ms=12,
        cost_usd=0.25,
    )
    assert loads_run_outcome(dumps_run_outcome(outcome)) == outcome
    assert parse_run_outcome(outcome.to_dict()) == outcome


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"result": "unknown"}, "result"),
        ({"output_sha256": None}, "at least one"),
        ({"evidence_artifact_sha256s": ()}, "item count"),
        ({"evidence_artifact_sha256s": (DIGEST_A, DIGEST_A)}, "sorted"),
        ({"latency_ms": -1}, "latency"),
        ({"latency_ms": OUTCOME_MAX_LATENCY_MS + 1}, "latency"),
        ({"cost_usd": float("inf")}, "cost"),
        ({"error_code": "bad"}, "only permitted"),
        ({"measured_at": "2026-07-27T00:06:00+00:00"}, "canonical"),
    ],
)
def test_run_outcome_rejects_invalid_shapes(changes, message):
    outcome = _outcome()
    with pytest.raises(OutcomeContractError, match=message):
        replace(outcome, **changes)


def test_run_outcome_requires_error_code_for_error():
    with pytest.raises(OutcomeContractError, match="error_code"):
        _outcome(result="error")


def test_numeric_inputs_are_normalized_for_content_hash_round_trip():
    outcome = _outcome(cost_usd=1)
    attribution = _attribution(confidence=1)
    assert outcome.cost_usd == 1.0
    assert attribution.confidence == 1.0
    assert loads_run_outcome(dumps_run_outcome(outcome)) == outcome
    assert (
        loads_outcome_attribution(dumps_outcome_attribution(attribution))
        == attribution
    )


def test_huge_numeric_inputs_fail_with_stable_contract_error():
    huge = 10**4_000
    with pytest.raises(OutcomeContractError) as outcome_error:
        _outcome(cost_usd=huge)
    assert outcome_error.value.code == "TBM_OUTCOME_INVALID"
    with pytest.raises(OutcomeContractError) as attribution_error:
        _attribution(confidence=huge)
    assert attribution_error.value.code == "TBM_OUTCOME_INVALID"

    outcome_document = dumps_run_outcome(_outcome()).replace(
        '"cost_usd":null', f'"cost_usd":{huge}'
    )
    with pytest.raises(OutcomeContractError) as parsed_outcome_error:
        loads_run_outcome(outcome_document)
    assert parsed_outcome_error.value.code == "TBM_OUTCOME_INVALID"

    attribution_document = dumps_outcome_attribution(_attribution()).replace(
        '"confidence":0.5', f'"confidence":{huge}'
    )
    with pytest.raises(OutcomeContractError) as parsed_attribution_error:
        loads_outcome_attribution(attribution_document)
    assert parsed_attribution_error.value.code == "TBM_OUTCOME_INVALID"


def test_run_outcome_hash_detects_tampering():
    with pytest.raises(OutcomeContractError, match="does not match"):
        replace(_outcome(), evaluator_version="2.0.0")


def test_verify_run_outcome_binds_completed_session():
    outcome = _outcome()
    verify_run_outcome(outcome, _completed_session(outcome.run_outcome_id))
    with pytest.raises(OutcomeContractError, match="run_outcome_id"):
        verify_run_outcome(
            outcome,
            _completed_session("run_outcome_sha256_" + "f" * 64),
        )


def test_association_and_causal_attributions_are_distinct():
    association = _attribution()
    causal = _attribution(
        claim_strength="causal",
        effect="helped",
        method="controlled_experiment",
        verifier_id="independent_reviewer",
        confidence=0.9,
    )
    assert association.contract_version == OUTCOME_ATTRIBUTION_CONTRACT_VERSION
    assert causal.claim_strength == "causal"
    assert association.attribution_id != causal.attribution_id
    assert causal.attribution_id == outcome_attribution_id(
        causal.to_dict(include_id=False)
    )


def test_attribution_builder_canonicalizes_revision_and_artifact_order():
    attribution = _attribution(
        memory_revision_ids=(REVISION_B, REVISION_A),
        evidence_artifact_sha256s=(DIGEST_B, DIGEST_A),
    )
    assert attribution.memory_revision_ids == (REVISION_A, REVISION_B)
    assert attribution.evidence_artifact_sha256s == (DIGEST_A, DIGEST_B)
    assert loads_outcome_attribution(
        dumps_outcome_attribution(attribution)
    ) == attribution
    assert parse_outcome_attribution(attribution.to_dict()) == attribution


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"claim_strength": "causal"}, "non-observational"),
        ({"method": "manual_review"}, "runtime_observation"),
        ({"verifier_id": "reviewer"}, "cannot name"),
        ({"memory_revision_ids": ()}, "item count"),
        ({"confidence": float("nan")}, "confidence"),
        ({"reason": ""}, "reason"),
    ],
)
def test_attribution_rejects_invalid_shapes(changes, message):
    with pytest.raises(OutcomeContractError, match=message):
        replace(_attribution(), **changes)


def test_causal_attribution_requires_independent_verifier():
    with pytest.raises(OutcomeContractError, match="differ"):
        _attribution(
            claim_strength="causal",
            effect="harmed",
            method="manual_review",
            evaluator_id="reviewer",
            verifier_id="reviewer",
        )
    with pytest.raises(OutcomeContractError, match="unknown"):
        _attribution(
            claim_strength="causal",
            method="external_evaluation",
            verifier_id="reviewer",
        )


def test_verify_attribution_binds_outcome_and_time():
    outcome = _outcome()
    attribution = _attribution(run_outcome_id_value=outcome.run_outcome_id)
    session = _completed_session(outcome.run_outcome_id)
    verify_outcome_attribution(attribution, outcome, session)
    with pytest.raises(OutcomeContractError, match="precedes"):
        verify_outcome_attribution(
            _attribution(
                run_outcome_id_value=outcome.run_outcome_id,
                recorded_at="2026-07-27T00:05:59Z",
            ),
            outcome,
            session,
        )
    with pytest.raises(OutcomeContractError, match="not finalized"):
        verify_outcome_attribution(
            _attribution(
                run_outcome_id_value=outcome.run_outcome_id,
                memory_revision_ids=(REVISION_B,),
            ),
            outcome,
            session,
        )


def test_identifier_rejects_del_control_character():
    with pytest.raises(OutcomeContractError, match="identifier"):
        replace(_outcome(), evaluator_id="bad\x7fidentifier")


def test_outcome_json_rejects_duplicates_nonfinite_and_invalid_utf8():
    duplicate = dumps_run_outcome(_outcome()).replace(
        '"result":"pass"', '"result":"pass","result":"fail"'
    )
    with pytest.raises(OutcomeContractError) as error:
        loads_run_outcome(duplicate)
    assert error.value.code == "TBM_OUTCOME_INVALID_JSON"
    with pytest.raises(OutcomeContractError):
        loads_outcome_attribution(
            dumps_outcome_attribution(_attribution()).replace(
                '"confidence":0.5', '"confidence":NaN'
            )
        )
    with pytest.raises(OutcomeContractError):
        loads_run_outcome(b"\xff")


def test_outcome_json_is_bounded():
    with pytest.raises(OutcomeContractError) as error:
        loads_run_outcome(" " * (OUTCOME_JSON_MAX_BYTES + 1))
    assert error.value.code == "TBM_OUTCOME_INVALID_JSON"


def test_outcome_parser_requires_exact_fields():
    value = _outcome().to_dict()
    value["extra"] = True
    with pytest.raises(OutcomeContractError, match="fields"):
        parse_run_outcome(value)
    attribution = _attribution().to_dict()
    del attribution["reason"]
    with pytest.raises(OutcomeContractError, match="fields"):
        parse_outcome_attribution(attribution)


def test_attribution_is_immutable():
    attribution = _attribution()
    assert isinstance(attribution, OutcomeAttribution)
    with pytest.raises(FrozenInstanceError):
        attribution.effect = "helped"  # type: ignore[misc]


def test_outcome_schema_examples_and_public_exports_match_runtime():
    outcome_value = json.loads(
        (ROOT / "examples" / "run_outcome_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    attribution_value = json.loads(
        (
            ROOT / "examples" / "outcome_attribution_v3.example.json"
        ).read_text(encoding="utf-8")
    )
    outcome_schema = json.loads(
        (ROOT / "schemas" / "run_outcome_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )
    attribution_schema = json.loads(
        (
            ROOT / "schemas" / "outcome_attribution_v3.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert parse_run_outcome(outcome_value).to_dict() == outcome_value
    assert (
        parse_outcome_attribution(attribution_value).to_dict()
        == attribution_value
    )
    assert set(outcome_schema["required"]) == set(outcome_value)
    assert set(attribution_schema["required"]) == set(attribution_value)
    assert tbm.RunOutcome is RunOutcome
    assert tbm.OutcomeAttribution is OutcomeAttribution
    for name in (
        "RunOutcome",
        "OutcomeAttribution",
        "build_run_outcome",
        "build_outcome_attribution",
        "verify_run_outcome",
        "verify_outcome_attribution",
    ):
        assert name in tbm.__all__
