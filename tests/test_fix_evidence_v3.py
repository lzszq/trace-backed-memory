from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.contracts_v3 import CommitRelationEvidence
from trace_backed_memory.fix_evidence_v3 import (
    FIX_EVIDENCE_JSON_MAX_BYTES,
    FixEvidence,
    FixEvidenceV3ContractError,
    build_fix_evidence,
    dumps_fix_evidence,
    loads_fix_evidence,
    parse_fix_evidence,
)

ROOT = Path(__file__).resolve().parents[1]


def _evidence(
    *,
    submitter_id: str = "fix_submitter",
    reviewer_id: str = "fix_reviewer",
) -> FixEvidence:
    return build_fix_evidence(
        case_id="case_001",
        source_trace_id="trace_source",
        source_commit_sha="abc123",
        fix_commit_sha="def456",
        source_to_fix=CommitRelationEvidence(
            "abc123",
            "def456",
            "ancestor",
            "git_verifier",
            "2026-07-27T00:01:00Z",
        ),
        artifact_hashes=("sha256:" + "a" * 64,),
        submitter_id=submitter_id,
        submitted_at="2026-07-27T00:02:00Z",
        reviewer_id=reviewer_id,
        reviewed_at="2026-07-27T00:03:00Z",
        attestation_sha256="sha256:" + "b" * 64,
    )


def test_fix_evidence_round_trips_canonical_json():
    evidence = _evidence()
    document = dumps_fix_evidence(evidence)

    assert loads_fix_evidence(document) == evidence
    assert loads_fix_evidence(document.encode()) == evidence
    assert document == json.dumps(
        evidence.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_fix_evidence_hash_detects_immutable_change():
    with pytest.raises(
        FixEvidenceV3ContractError,
        match="canonical fix evidence",
    ) as captured:
        replace(_evidence(), reviewer_id="other_reviewer")

    assert captured.value.code == "TBM_FIX_EVIDENCE_HASH_MISMATCH"


def test_fix_evidence_requires_independent_review_and_exact_relation():
    with pytest.raises(FixEvidenceV3ContractError, match="must differ"):
        _evidence(submitter_id="same_actor", reviewer_id="same_actor")

    with pytest.raises(FixEvidenceV3ContractError, match="bind source and fix"):
        replace(
            _evidence(),
            source_to_fix=CommitRelationEvidence(
                "abc123",
                "other_fix",
                "ancestor",
                "git_verifier",
                "2026-07-27T00:01:00Z",
            ),
        )


def test_fix_evidence_rejects_invalid_time_order():
    with pytest.raises(FixEvidenceV3ContractError, match="before submission"):
        replace(
            _evidence(),
            source_to_fix=CommitRelationEvidence(
                "abc123",
                "def456",
                "ancestor",
                "git_verifier",
                "2026-07-27T00:03:00Z",
            ),
        )
    with pytest.raises(FixEvidenceV3ContractError, match="must not precede"):
        replace(_evidence(), reviewed_at="2026-07-27T00:01:30Z")


def test_fix_evidence_builder_wraps_invalid_timestamps():
    with pytest.raises(FixEvidenceV3ContractError) as captured:
        build_fix_evidence(
            case_id="case_001",
            source_trace_id="trace_source",
            source_commit_sha="abc123",
            fix_commit_sha="def456",
            source_to_fix=_evidence().source_to_fix,
            artifact_hashes=(),
            submitter_id="fix_submitter",
            submitted_at="invalid",
            reviewer_id="fix_reviewer",
            reviewed_at="2026-07-27T00:03:00Z",
            attestation_sha256="sha256:" + "b" * 64,
        )

    assert captured.value.code == "TBM_FIX_EVIDENCE_INVALID"


def test_fix_evidence_parser_rejects_oversized_collections_and_objects():
    payload = _evidence().to_dict()
    payload["artifact_hashes"] = ["sha256:" + "a" * 64] * 1_001
    with pytest.raises(FixEvidenceV3ContractError, match="bounded array"):
        parse_fix_evidence(payload)

    with pytest.raises(FixEvidenceV3ContractError, match="fields"):
        parse_fix_evidence({str(index): None for index in range(10_000)})


@pytest.mark.parametrize(
    "document",
    [
        b"\xff",
        '{"evidence_id":"first","evidence_id":"second"}',
        '{"value":NaN}',
        1,
    ],
)
def test_fix_evidence_json_loader_is_bounded_and_strict(document: object):
    with pytest.raises(FixEvidenceV3ContractError) as captured:
        loads_fix_evidence(document)  # type: ignore[arg-type]

    assert captured.value.code == "TBM_FIX_EVIDENCE_INVALID_JSON"


def test_fix_evidence_json_loader_rejects_character_and_byte_overflow():
    for document in (
        '"' + ("x" * FIX_EVIDENCE_JSON_MAX_BYTES) + '"',
        '"'
        + ("🙂" * ((FIX_EVIDENCE_JSON_MAX_BYTES // 4) + 1))
        + '"',
    ):
        with pytest.raises(FixEvidenceV3ContractError) as captured:
            loads_fix_evidence(document)
        assert captured.value.code == "TBM_FIX_EVIDENCE_INVALID_JSON"


def test_fix_evidence_parser_rejects_tampered_identity():
    payload = _evidence().to_dict()
    payload["evidence_id"] = "fix_evidence_sha256_" + "0" * 64

    with pytest.raises(FixEvidenceV3ContractError) as captured:
        parse_fix_evidence(payload)

    assert captured.value.code == "TBM_FIX_EVIDENCE_HASH_MISMATCH"


def test_fix_evidence_schema_example_and_public_exports_match_runtime():
    example = json.loads(
        (ROOT / "examples" / "fix_evidence_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT / "schemas" / "fix_evidence_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )

    parsed = parse_fix_evidence(example)
    assert parsed.to_dict() == example
    assert set(schema["required"]) == set(example)
    assert schema["additionalProperties"] is False
    assert tbm.FixEvidence is FixEvidence
    assert "verify_memory_revision_evidence_bundle" in tbm.__all__
