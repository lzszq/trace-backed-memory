from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.contracts_v3 import CommitRelationEvidence
from trace_backed_memory.evidence_v3 import (
    EVIDENCE_JSON_MAX_BYTES,
    EvidenceV3ContractError,
    StructuredRegressionEvidence,
    build_structured_regression_evidence,
    dumps_structured_regression_evidence,
    loads_structured_regression_evidence,
    parse_structured_regression_evidence,
)


ROOT = Path(__file__).resolve().parents[1]


def _evidence() -> StructuredRegressionEvidence:
    return build_structured_regression_evidence(
        case_id="case_001",
        source_trace_id="trace_source_001",
        verification_trace_id="trace_verify_001",
        verification_run_id="run_verify_001",
        evaluator_id="evaluator_regression",
        evaluator_version="1.2.0",
        evaluation_suite="memory_regressions",
        evaluation_case_id="case_no_repeat_failure",
        expected_outcome="The fixed workflow completes.",
        observed_outcome="The fixed workflow completed.",
        result="pass",
        environment={"os": "linux", "python": "3.11"},
        source_commit_sha="abc123",
        fix_commit_sha="def456",
        verification_commit_sha="fedcba",
        source_to_fix=CommitRelationEvidence(
            "abc123",
            "def456",
            "ancestor",
            "git_verifier",
            "2026-07-27T00:01:00Z",
        ),
        fix_to_verification=CommitRelationEvidence(
            "def456",
            "fedcba",
            "ancestor",
            "git_verifier",
            "2026-07-27T00:02:00Z",
        ),
        artifact_hashes=("sha256:" + "a" * 64,),
        submitter_id="engineer_001",
        submitted_at="2026-07-27T00:03:00Z",
        verifier_id="reviewer_001",
        verified_at="2026-07-27T00:04:00Z",
        attestation_sha256="sha256:" + "b" * 64,
    )


def _rebuild(
    evidence: StructuredRegressionEvidence,
    **overrides: Any,
) -> StructuredRegressionEvidence:
    values = {
        "case_id": evidence.case_id,
        "source_trace_id": evidence.source_trace_id,
        "verification_trace_id": evidence.verification_trace_id,
        "verification_run_id": evidence.verification_run_id,
        "evaluator_id": evidence.evaluator_id,
        "evaluator_version": evidence.evaluator_version,
        "evaluation_suite": evidence.evaluation_suite,
        "evaluation_case_id": evidence.evaluation_case_id,
        "expected_outcome": evidence.expected_outcome,
        "observed_outcome": evidence.observed_outcome,
        "result": evidence.result,
        "environment": dict(evidence.environment),
        "source_commit_sha": evidence.source_commit_sha,
        "fix_commit_sha": evidence.fix_commit_sha,
        "verification_commit_sha": evidence.verification_commit_sha,
        "source_to_fix": evidence.source_to_fix,
        "fix_to_verification": evidence.fix_to_verification,
        "artifact_hashes": evidence.artifact_hashes,
        "submitter_id": evidence.submitter_id,
        "submitted_at": evidence.submitted_at,
        "verifier_id": evidence.verifier_id,
        "verified_at": evidence.verified_at,
        "attestation_sha256": evidence.attestation_sha256,
    }
    values.update(overrides)
    return build_structured_regression_evidence(**values)  # type: ignore[arg-type]


def test_structured_evidence_round_trips_canonical_str_and_bytes():
    evidence = _evidence()
    document = dumps_structured_regression_evidence(evidence)

    assert loads_structured_regression_evidence(document) == evidence
    assert loads_structured_regression_evidence(document.encode()) == evidence
    assert document == json.dumps(
        evidence.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_builder_canonicalizes_offset_timestamps_for_exact_round_trip():
    evidence = _evidence()
    rebuilt = _rebuild(
        evidence,
        source_to_fix=replace(
            evidence.source_to_fix,
            verified_at="2026-07-27T01:01:00+01:00",
        ),
        fix_to_verification=replace(
            evidence.fix_to_verification,
            verified_at="2026-07-27T01:02:00+01:00",
        ),
        submitted_at="2026-07-27T01:03:00+01:00",
        verified_at="2026-07-27T01:04:00+01:00",
    )

    assert rebuilt.submitted_at == "2026-07-27T00:03:00Z"
    assert rebuilt.source_to_fix.verified_at == "2026-07-27T00:01:00Z"
    assert loads_structured_regression_evidence(
        dumps_structured_regression_evidence(rebuilt)
    ) == rebuilt


def test_evidence_id_is_content_derived_and_tampering_fails_closed():
    evidence = _evidence()

    with pytest.raises(
        EvidenceV3ContractError,
        match="canonical evidence content",
    ) as captured:
        replace(evidence, observed_outcome="A different result.")

    assert captured.value.code == "TBM_EVIDENCE_HASH_MISMATCH"


def test_evidence_requires_independent_submitter_and_verifier():
    evidence = _evidence()

    with pytest.raises(EvidenceV3ContractError, match="must differ"):
        replace(evidence, submitter_id=evidence.verifier_id)


def test_evidence_rejects_wrong_commit_relationships():
    evidence = _evidence()

    with pytest.raises(EvidenceV3ContractError, match="source and fix"):
        replace(
            evidence,
            source_to_fix=replace(
                evidence.source_to_fix,
                to_commit_sha="wrong",
            ),
        )


def test_evidence_rejects_commit_relationships_verified_after_submission():
    evidence = _evidence()

    with pytest.raises(EvidenceV3ContractError, match="before submission"):
        replace(
            evidence,
            fix_to_verification=replace(
                evidence.fix_to_verification,
                verified_at="2026-07-27T00:03:01Z",
            ),
        )


def test_builder_wraps_invalid_environment_and_artifact_types():
    evidence = _evidence()

    with pytest.raises(EvidenceV3ContractError, match="environment"):
        _rebuild(evidence, environment=None)
    with pytest.raises(EvidenceV3ContractError, match="artifact_hashes"):
        _rebuild(evidence, artifact_hashes=None)
    with pytest.raises(EvidenceV3ContractError, match="source_to_fix"):
        _rebuild(evidence, source_to_fix=None)
    with pytest.raises(EvidenceV3ContractError, match="RFC 3339"):
        _rebuild(evidence, submitted_at="not-a-timestamp")


def test_parser_wraps_nested_relation_contract_errors():
    payload = _evidence().to_dict()
    relation = dict(payload["source_to_fix"])  # type: ignore[arg-type]
    relation["relation"] = None
    payload["source_to_fix"] = relation

    with pytest.raises(EvidenceV3ContractError) as captured:
        parse_structured_regression_evidence(payload)

    assert captured.value.code == "TBM_EVIDENCE_INVALID"


def test_parser_normalizes_schema_valid_unsorted_artifact_hashes():
    payload = _evidence().to_dict()
    payload["artifact_hashes"] = [
        "sha256:" + "b" * 64,
        "sha256:" + "a" * 64,
    ]
    payload_without_id = dict(payload)
    del payload_without_id["evidence_id"]
    payload_without_id["artifact_hashes"] = sorted(
        payload_without_id["artifact_hashes"]  # type: ignore[arg-type]
    )
    payload["evidence_id"] = tbm.regression_evidence_id(payload_without_id)

    parsed = parse_structured_regression_evidence(payload)

    assert parsed.artifact_hashes == (
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )


def test_runtime_rejects_timestamp_offsets_outside_schema_range():
    evidence = _evidence()

    with pytest.raises(EvidenceV3ContractError, match="RFC 3339"):
        replace(evidence, submitted_at="2026-07-27T00:03:00+23:00")


@pytest.mark.parametrize(
    "document",
    [
        '{"evidence_id":"first","evidence_id":"second"}',
        '{"result":NaN}',
        '{"nested":' * 33 + "null" + "}" * 33,
    ],
)
def test_evidence_json_rejects_duplicate_nonfinite_and_deep_input(
    document: str,
):
    with pytest.raises(EvidenceV3ContractError) as captured:
        loads_structured_regression_evidence(document)

    assert captured.value.code == "TBM_EVIDENCE_INVALID_JSON"


def test_evidence_json_rejects_invalid_utf8_and_oversized_input():
    for document in (b"\xff", "x" * (EVIDENCE_JSON_MAX_BYTES + 1)):
        with pytest.raises(EvidenceV3ContractError) as captured:
            loads_structured_regression_evidence(document)
        assert captured.value.code == "TBM_EVIDENCE_INVALID_JSON"


def test_schema_example_and_public_exports_match_runtime_contract():
    example = json.loads(
        (
            ROOT / "examples" / "structured_regression_evidence_v3.example.json"
        ).read_text(encoding="utf-8")
    )
    schema = json.loads(
        (
            ROOT
            / "schemas"
            / "structured_regression_evidence_v3.schema.json"
        ).read_text(encoding="utf-8")
    )

    parsed = parse_structured_regression_evidence(example)
    assert parsed.to_dict() == example
    assert set(schema["required"]) == set(example)
    assert schema["additionalProperties"] is False
    assert tbm.StructuredRegressionEvidence is StructuredRegressionEvidence
    assert "StructuredRegressionEvidence" in tbm.__all__
