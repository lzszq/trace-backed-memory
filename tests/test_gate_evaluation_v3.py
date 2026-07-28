from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.gate_evaluation_v3 import (
    GateEvaluationContractError,
    SemanticGateAttempt,
    SystemGateDecision,
    SystemGateEvaluation,
    build_semantic_gate_attempt,
    build_system_gate_evaluation,
    dumps_semantic_gate_attempt,
    dumps_system_gate_evaluation,
    loads_semantic_gate_attempt,
    loads_system_gate_evaluation,
    parse_semantic_gate_attempt,
    parse_system_gate_evaluation,
    verify_semantic_gate_attempt,
    verify_semantic_gate_attempt_chain,
    verify_semantic_gate_attempt_parent,
    verify_system_gate_evaluation,
)
from tests.test_retrieval_v3 import _snapshot

ROOT = Path(__file__).resolve().parents[1]


def _system(
    *,
    outcome: str = "allowed",
) -> SystemGateEvaluation:
    snapshot = _snapshot()
    hit = snapshot.hits[0]
    return build_system_gate_evaluation(
        session_id=snapshot.session_id,
        retrieval_snapshot_id=snapshot.snapshot_id,
        authorization_event_id=snapshot.authorization_event_id,
        policy_bundle_sha256="sha256:" + "4" * 64,
        evaluator_id="system_gate",
        evaluator_version="3.0",
        decisions=(
            SystemGateDecision(
                hit.memory_revision_id,
                hit.candidate_sha256,
                outcome,  # type: ignore[arg-type]
                "eligible" if outcome == "allowed" else "scope_block",
                "rule_scope_v3",
            ),
        ),
        evaluated_at="2026-07-27T08:01:00Z",
    )


def _attempt(
    *,
    evaluation: SystemGateEvaluation | None = None,
    status: str = "succeeded",
    allowed: tuple[str, ...] | None = None,
    blocked: tuple[str, ...] | None = None,
) -> SemanticGateAttempt:
    evaluation = evaluation or _system()
    revision_id = evaluation.decisions[0].memory_revision_id
    succeeded = status == "succeeded"
    return build_semantic_gate_attempt(
        session_id=evaluation.session_id,
        retrieval_snapshot_id=evaluation.retrieval_snapshot_id,
        system_gate_evaluation_id=evaluation.evaluation_id,
        sequence=1,
        previous_attempt_id=None,
        provider_id="provider_001",
        model_id="gate_model",
        model_version="2026-07-01",
        endpoint_id="deployment_001",
        prompt_template_id="semantic_gate_prompt",
        prompt_template_version="3.0",
        prompt_artifact_sha256="sha256:" + "5" * 64,
        response_artifact_sha256=("sha256:" + "6" * 64) if succeeded else None,
        generation_config_sha256="sha256:" + "7" * 64,
        provider_request_id="provider_request_001",
        status=status,
        decision_id="decision_001" if succeeded else None,
        final_allowed_revision_ids=(
            (revision_id,) if allowed is None else allowed
        )
        if succeeded
        else (),
        final_blocked_revision_ids=(blocked or ()) if succeeded else (),
        reason="Applicable to this run." if succeeded else None,
        risk="low" if succeeded else None,
        recommended_injection="summary" if succeeded else None,
        error_code=None if succeeded else "provider_timeout",
        input_tokens=100,
        output_tokens=25 if succeeded else None,
        latency_ms=500,
        started_at="2026-07-27T08:02:00Z",
        finished_at="2026-07-27T08:02:01Z",
    )


def _system_rebuild(
    evaluation: SystemGateEvaluation,
    **changes: object,
) -> SystemGateEvaluation:
    values = {
        key: value
        for key, value in evaluation.__dict__.items()
        if key not in {"evaluation_id", "contract_version"}
    }
    values.update(changes)
    return build_system_gate_evaluation(**values)  # type: ignore[arg-type]


def _attempt_rebuild(
    attempt: SemanticGateAttempt,
    **changes: object,
) -> SemanticGateAttempt:
    values = {
        key: value
        for key, value in attempt.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(changes)
    return build_semantic_gate_attempt(**values)


def test_system_and_semantic_records_round_trip():
    system = _system()
    attempt = _attempt(evaluation=system)

    assert loads_system_gate_evaluation(
        dumps_system_gate_evaluation(system)
    ) == system
    assert loads_semantic_gate_attempt(
        dumps_semantic_gate_attempt(attempt)
    ) == attempt
    assert json.loads(dumps_system_gate_evaluation(system)) == system.to_dict()
    assert json.loads(dumps_semantic_gate_attempt(attempt)) == attempt.to_dict()


def test_cross_record_verification_accepts_monotonic_gate_result():
    snapshot = _snapshot()
    evaluation = _system()
    attempt = _attempt(evaluation=evaluation)

    verify_system_gate_evaluation(evaluation, snapshot)
    verify_semantic_gate_attempt(attempt, evaluation, snapshot)


def test_semantic_gate_cannot_reopen_system_block():
    evaluation = _system(outcome="blocked")
    revision_id = evaluation.decisions[0].memory_revision_id
    attempt = _attempt(
        evaluation=evaluation,
        allowed=(revision_id,),
        blocked=(),
    )

    with pytest.raises(GateEvaluationContractError, match="cannot reopen"):
        verify_semantic_gate_attempt(attempt, evaluation, _snapshot())


def test_semantic_gate_must_cover_and_preserve_every_candidate():
    evaluation = _system()
    attempt = _attempt(evaluation=evaluation, allowed=(), blocked=())

    with pytest.raises(GateEvaluationContractError, match="cover every"):
        verify_semantic_gate_attempt(attempt, evaluation, _snapshot())

    blocked_evaluation = _system(outcome="blocked")
    blocked_attempt = _attempt(
        evaluation=blocked_evaluation,
        allowed=(),
        blocked=(),
    )
    with pytest.raises(GateEvaluationContractError, match="cover every"):
        verify_semantic_gate_attempt(
            blocked_attempt,
            blocked_evaluation,
            _snapshot(),
        )


def test_failed_attempt_records_provenance_without_decision_output():
    attempt = _attempt(status="failed")

    verify_semantic_gate_attempt(attempt, _system(), _snapshot())
    assert attempt.error_code == "provider_timeout"
    assert attempt.response_artifact_sha256 is None

    with pytest.raises(GateEvaluationContractError, match="forbids decision"):
        replace(attempt, decision_id="decision_001")


def test_success_and_failure_shapes_are_strict():
    attempt = _attempt()
    with pytest.raises(GateEvaluationContractError, match="requires response"):
        replace(attempt, response_artifact_sha256=None)
    with pytest.raises(GateEvaluationContractError, match="forbids error"):
        replace(attempt, error_code="unexpected")
    with pytest.raises(GateEvaluationContractError, match="endpoint_id"):
        replace(attempt, endpoint_id=None)
    with pytest.raises(GateEvaluationContractError, match="provider_request_id"):
        replace(attempt, provider_request_id=None)

    failed = _attempt(status="failed")
    with pytest.raises(GateEvaluationContractError, match="error_code"):
        replace(failed, error_code=None)
    with pytest.raises(GateEvaluationContractError, match="final revision"):
        replace(
            failed,
            final_blocked_revision_ids=(
                _system().decisions[0].memory_revision_id,
            ),
        )


def test_attempt_parent_time_and_metrics_are_bounded():
    attempt = _attempt()
    with pytest.raises(GateEvaluationContractError, match="first"):
        replace(attempt, previous_attempt_id=attempt.attempt_id)
    with pytest.raises(GateEvaluationContractError, match="parent"):
        replace(attempt, sequence=2, previous_attempt_id=None)
    with pytest.raises(GateEvaluationContractError, match="precede"):
        replace(attempt, finished_at="2026-07-27T08:01:59Z")
    with pytest.raises(GateEvaluationContractError, match="latency_ms"):
        replace(attempt, latency_ms=-1)


def test_system_evaluation_exactly_covers_snapshot():
    snapshot = _snapshot()
    evaluation = _system()

    with pytest.raises(GateEvaluationContractError, match="exactly cover"):
        verify_system_gate_evaluation(
            _system_rebuild(evaluation, decisions=()),
            snapshot,
        )
    with pytest.raises(GateEvaluationContractError, match="session"):
        verify_system_gate_evaluation(
            _system_rebuild(evaluation, session_id="other_session"),
            snapshot,
        )
    with pytest.raises(GateEvaluationContractError, match="precede"):
        verify_system_gate_evaluation(
            _system_rebuild(
                evaluation,
                evaluated_at="2026-07-27T07:59:00Z",
            ),
            snapshot,
        )


def test_semantic_cross_record_references_and_time_are_exact():
    evaluation = _system()
    attempt = _attempt(evaluation=evaluation)

    for changes, message in (
        ({"session_id": "other_session"}, "session"),
        (
            {
                "retrieval_snapshot_id": (
                    "retrieval_snapshot_sha256_" + "a" * 64
                )
            },
            "retrieval snapshot",
        ),
        (
            {"system_gate_evaluation_id": "system_gate_sha256_" + "b" * 64},
            "reference",
        ),
        ({"started_at": "2026-07-27T08:00:30Z"}, "precede"),
    ):
        with pytest.raises(GateEvaluationContractError, match=message):
            verify_semantic_gate_attempt(
                _attempt_rebuild(attempt, **changes),
                evaluation,
                _snapshot(),
            )


def test_semantic_verifier_also_validates_system_against_snapshot():
    snapshot = _snapshot()
    evaluation = _system_rebuild(_system(), decisions=())
    attempt = _attempt_rebuild(
        _attempt(),
        system_gate_evaluation_id=evaluation.evaluation_id,
        final_allowed_revision_ids=(),
    )

    with pytest.raises(GateEvaluationContractError, match="exactly cover"):
        verify_semantic_gate_attempt(attempt, evaluation, snapshot)


def test_hashes_detect_immutable_changes():
    with pytest.raises(GateEvaluationContractError, match="canonical System"):
        replace(_system(), evaluator_version="4.0")
    with pytest.raises(GateEvaluationContractError, match="canonical Semantic"):
        replace(_attempt(), latency_ms=501)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "tbm.system-gate-evaluation.v4"}, "contract_version"),
        ({"evaluation_id": "bad"}, "evaluation_id"),
        ({"authorization_event_id": "bad"}, "authorization_event_id"),
        ({"policy_bundle_sha256": "bad"}, "policy_bundle_sha256"),
        ({"evaluator_id": " "}, "evaluator_id"),
    ],
)
def test_system_record_rejects_invalid_fields(
    changes: dict[str, object],
    message: str,
):
    with pytest.raises(GateEvaluationContractError, match=message):
        replace(_system(), **changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "tbm.semantic-gate-attempt.v4"}, "contract_version"),
        ({"attempt_id": "bad"}, "attempt_id"),
        ({"system_gate_evaluation_id": "bad"}, "system_gate_evaluation_id"),
        ({"status": "pending"}, "status"),
        ({"input_tokens": -1}, "input_tokens"),
        ({"risk": "critical"}, "risk"),
        ({"recommended_injection": "raw"}, "recommended_injection"),
        ({"final_blocked_revision_ids": (_system().decisions[0].memory_revision_id,)}, "disjoint"),
    ],
)
def test_semantic_record_rejects_invalid_fields(
    changes: dict[str, object],
    message: str,
):
    with pytest.raises(GateEvaluationContractError, match=message):
        replace(_attempt(), **changes)


def test_retry_parent_chain_is_exact_and_monotonic():
    parent = _attempt()
    child = _attempt_rebuild(
        parent,
        sequence=2,
        previous_attempt_id=parent.attempt_id,
        started_at="2026-07-27T08:03:00Z",
        finished_at="2026-07-27T08:03:01Z",
    )

    verify_semantic_gate_attempt_parent(parent, None)
    verify_semantic_gate_attempt_parent(child, parent)
    verify_semantic_gate_attempt_chain(
        (parent, child),
        _system(),
        _snapshot(),
    )
    assert tbm.verify_semantic_gate_attempt_chain is (
        verify_semantic_gate_attempt_chain
    )

    with pytest.raises(GateEvaluationContractError, match="parent"):
        verify_semantic_gate_attempt_chain(
            (child,),
            _system(),
            _snapshot(),
        )
    with pytest.raises(GateEvaluationContractError, match="bounded tuple"):
        verify_semantic_gate_attempt_chain(  # type: ignore[arg-type]
            [parent],
            _system(),
            _snapshot(),
        )

    with pytest.raises(GateEvaluationContractError, match="parent identity"):
        verify_semantic_gate_attempt_parent(
            _attempt_rebuild(
                child,
                previous_attempt_id="semantic_attempt_sha256_" + "f" * 64,
            ),
            parent,
        )
    with pytest.raises(GateEvaluationContractError, match="sequence"):
        verify_semantic_gate_attempt_parent(
            _attempt_rebuild(child, sequence=3),
            parent,
        )
    with pytest.raises(GateEvaluationContractError, match="different gate"):
        verify_semantic_gate_attempt_parent(
            _attempt_rebuild(child, session_id="other_session"),
            parent,
        )
    with pytest.raises(GateEvaluationContractError, match="precede"):
        verify_semantic_gate_attempt_parent(
            _attempt_rebuild(
                child,
                started_at="2026-07-27T08:01:59Z",
                finished_at="2026-07-27T08:02:00Z",
            ),
            parent,
        )


def test_sequence_and_direct_parser_arrays_are_bounded():
    with pytest.raises(GateEvaluationContractError, match="sequence"):
        replace(_attempt(), sequence=2_147_483_648)

    system = _system().to_dict()
    system["decisions"] = [system["decisions"][0]] * 101  # type: ignore[index]
    with pytest.raises(GateEvaluationContractError, match="bounded array"):
        parse_system_gate_evaluation(system)

    semantic = _attempt().to_dict()
    semantic["final_allowed_revision_ids"] = [
        "memory_revision_sha256_" + "a" * 64
    ] * 101
    with pytest.raises(GateEvaluationContractError, match="bounded array"):
        parse_semantic_gate_attempt(semantic)


def test_parsers_reject_malformed_nested_and_status_records():
    system = _system().to_dict()
    system["decisions"] = {}
    with pytest.raises(GateEvaluationContractError, match="array"):
        parse_system_gate_evaluation(system)

    attempt = _attempt().to_dict()
    attempt["final_allowed_revision_ids"] = "bad"
    with pytest.raises(GateEvaluationContractError, match="array"):
        parse_semantic_gate_attempt(attempt)

    attempt = _attempt().to_dict()
    attempt["extra"] = True
    with pytest.raises(GateEvaluationContractError, match="fields"):
        parse_semantic_gate_attempt(attempt)
    with pytest.raises(GateEvaluationContractError, match="fields"):
        parse_system_gate_evaluation(
            {str(index): None for index in range(10_000)}
        )


@pytest.mark.parametrize(
    "loader",
    [loads_system_gate_evaluation, loads_semantic_gate_attempt],
)
def test_json_ingestion_rejects_duplicate_nonfinite_deep_and_wrong_shape(loader):
    for document in (
        '{"id":"first","id":"second"}',
        '{"score":NaN}',
        '{"nested":' * 33 + "null" + "}" * 33,
        "[]",
        b"\xff",
    ):
        with pytest.raises(GateEvaluationContractError):
            loader(document)
    with pytest.raises(GateEvaluationContractError):
        loader("x" * (tbm.GATE_EVALUATION_JSON_MAX_BYTES + 1))


def test_wrong_record_types_fail_closed():
    with pytest.raises(GateEvaluationContractError):
        dumps_system_gate_evaluation(None)  # type: ignore[arg-type]
    with pytest.raises(GateEvaluationContractError):
        dumps_semantic_gate_attempt(None)  # type: ignore[arg-type]
    with pytest.raises(GateEvaluationContractError):
        verify_system_gate_evaluation(None, _snapshot())  # type: ignore[arg-type]
    with pytest.raises(GateEvaluationContractError):
        verify_semantic_gate_attempt(  # type: ignore[arg-type]
            None,
            _system(),
            _snapshot(),
        )


def test_schema_examples_and_public_exports_match_runtime():
    system = json.loads(
        (ROOT / "examples" / "system_gate_evaluation_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    semantic = json.loads(
        (ROOT / "examples" / "semantic_gate_attempt_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    system_schema = json.loads(
        (
            ROOT / "schemas" / "system_gate_evaluation_v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    semantic_schema = json.loads(
        (ROOT / "schemas" / "semantic_gate_attempt_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert parse_system_gate_evaluation(system).to_dict() == system
    assert parse_semantic_gate_attempt(semantic).to_dict() == semantic
    assert set(system_schema["required"]) == set(system)
    assert set(semantic_schema["required"]) == set(semantic)
    assert tbm.SystemGateEvaluation is SystemGateEvaluation
    assert tbm.SemanticGateAttempt is SemanticGateAttempt
