from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.contracts_v3 import (
    AuthorizationScope,
    CommitRelationEvidence,
)
from trace_backed_memory.evidence_v3 import (
    StructuredRegressionEvidence,
    build_structured_regression_evidence,
)
from trace_backed_memory.fix_evidence_v3 import FixEvidence, build_fix_evidence
from trace_backed_memory.memory_revision_v3 import (
    MEMORY_REVISION_JSON_MAX_BYTES,
    MemoryRevision,
    MemoryRevisionContractError,
    build_memory_revision,
    dumps_memory_revision,
    loads_memory_revision,
    parse_memory_revision,
    verify_memory_revision_evidence,
    verify_memory_revision_evidence_bundle,
)
from trace_backed_memory.replay_v3 import create_content_addressed_artifact


NOW = "2026-07-27T00:06:00Z"
ROOT = Path(__file__).resolve().parents[1]


def _evidence(
    *,
    result: str = "pass",
    evaluation_case_id: str = "case_fixed",
    case_id: str = "case_001",
) -> StructuredRegressionEvidence:
    return build_structured_regression_evidence(
        case_id=case_id,
        source_trace_id="trace_source",
        verification_trace_id="trace_verification",
        verification_run_id="run_verification",
        evaluator_id="eval_001",
        evaluator_version="1.0",
        evaluation_suite="regression",
        evaluation_case_id=evaluation_case_id,
        expected_outcome="The source failure does not recur.",
        observed_outcome="All assertions passed.",
        result=result,  # type: ignore[arg-type]
        environment={"python": "3.11"},
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
        submitter_id="evidence_submitter",
        submitted_at="2026-07-27T00:03:00Z",
        verifier_id="evidence_verifier",
        verified_at="2026-07-27T00:04:00Z",
        attestation_sha256="sha256:" + "b" * 64,
    )


def _fix_evidence(*, source_trace_id: str = "trace_source") -> FixEvidence:
    return build_fix_evidence(
        case_id="case_001",
        source_trace_id=source_trace_id,
        source_commit_sha="abc123",
        fix_commit_sha="def456",
        source_to_fix=CommitRelationEvidence(
            "abc123",
            "def456",
            "ancestor",
            "git_verifier",
            "2026-07-27T00:01:00Z",
        ),
        artifact_hashes=("sha256:" + "d" * 64,),
        submitter_id="fix_submitter",
        submitted_at="2026-07-27T00:02:00Z",
        reviewer_id="fix_reviewer",
        reviewed_at="2026-07-27T00:03:00Z",
        attestation_sha256="sha256:" + "e" * 64,
    )


def _revision(
    *,
    evidence: StructuredRegressionEvidence | None = None,
    fix_evidence: FixEvidence | None = None,
    proposed_by: str = "revision_proposer",
) -> MemoryRevision:
    evidence = evidence or _evidence()
    fix_evidence = fix_evidence or _fix_evidence()
    content = b'{"memory_text":"Prefer the verified workflow."}'
    artifact = create_content_addressed_artifact(
        content,
        media_type="application/vnd.trace-backed-memory.revision+json",
        classification="internal",
        created_at="2026-07-27T00:05:00Z",
    )
    return build_memory_revision(
        memory_id="memory_001",
        memory_kind="lesson",
        revision_number=1,
        previous_revision_id=None,
        memory_type="procedural",
        content_artifact=artifact,
        scope=AuthorizationScope(
            kind="repository",
            tenant_id="tenant_001",
            repository_id="repository_001",
            attributes=(("branch", "main"),),
        ),
        confidence=1,
        sensitive=False,
        eval_leaking=False,
        source_case_id="case_001",
        source_case_revision_id="case_revision_001",
        fix_evidence_id=fix_evidence.evidence_id,
        regression_evidence_ids=(evidence.evidence_id,),
        proposed_by=proposed_by,
        proposed_via_client_id="agent_client_001",
        proposed_at=NOW,
        proposal_attestation_sha256="sha256:" + "c" * 64,
    )


def _revision_kwargs(revision: MemoryRevision) -> dict[str, object]:
    return {
        key: value
        for key, value in revision.__dict__.items()
        if key not in {"revision_id", "contract_version"}
    }


def _project_policy_revision() -> MemoryRevision:
    lesson = _revision()
    return build_memory_revision(
        **{
            **_revision_kwargs(lesson),
            "memory_kind": "project_policy",
            "memory_type": "policy",
            "source_case_id": None,
            "source_case_revision_id": None,
            "fix_evidence_id": None,
            "regression_evidence_ids": (),
        }
    )


def test_memory_revision_round_trips_canonical_json():
    revision = _revision()
    document = dumps_memory_revision(revision)

    assert loads_memory_revision(document) == revision
    assert loads_memory_revision(document.encode()) == revision
    assert document == json.dumps(
        revision.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert revision.confidence == 1.0


def test_memory_revision_id_detects_immutable_content_change():
    revision = _revision()

    with pytest.raises(
        MemoryRevisionContractError,
        match="canonical revision content",
    ) as captured:
        replace(revision, confidence=0.5)

    assert captured.value.code == "TBM_MEMORY_REVISION_HASH_MISMATCH"


def test_revision_number_requires_exact_parent_shape():
    revision = _revision()

    with pytest.raises(MemoryRevisionContractError, match="first revision"):
        replace(revision, previous_revision_id=revision.revision_id)
    with pytest.raises(MemoryRevisionContractError, match="previous_revision"):
        replace(revision, revision_number=2, previous_revision_id="not-a-revision")


def test_project_policy_forbids_case_and_regression_references():
    revision = _revision()

    with pytest.raises(
        MemoryRevisionContractError,
        match="project_policy revision forbids",
    ):
        replace(
            revision,
            memory_kind="project_policy",
            memory_type="policy",
        )


def test_sensitive_flag_and_artifact_classification_must_match():
    revision = _revision()

    with pytest.raises(
        MemoryRevisionContractError,
        match="sensitive revision",
    ):
        replace(revision, sensitive=True)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "tbm.memory-revision.v4"}, "contract_version"),
        ({"revision_id": "not-a-revision"}, "revision_id"),
        ({"memory_id": " "}, "memory_id"),
        ({"memory_kind": "note"}, "memory_kind"),
        ({"revision_number": 0}, "revision_number"),
        ({"memory_type": "unknown"}, "memory_type"),
        (
            {"memory_kind": "project_policy", "memory_type": "procedural"},
            "policy memory_type",
        ),
        ({"content_artifact": None}, "content_artifact"),
        ({"scope": None}, "scope"),
        ({"confidence": "high"}, "confidence"),
        ({"confidence": float("nan")}, "confidence"),
        ({"sensitive": "yes"}, "booleans"),
        ({"source_case_id": None}, "source_case_id"),
        ({"regression_evidence_ids": ()}, "regression evidence"),
        ({"proposed_by": " "}, "proposed_by"),
        ({"proposed_via_client_id": " "}, "proposed_via_client_id"),
        ({"proposal_attestation_sha256": "bad"}, "attestation"),
    ],
)
def test_revision_record_rejects_invalid_fields_before_hash_check(
    changes: dict[str, object],
    message: str,
):
    with pytest.raises(MemoryRevisionContractError, match=message):
        replace(_revision(), **changes)


def test_revision_rejects_self_parent_and_future_artifact():
    revision = _revision()

    with pytest.raises(MemoryRevisionContractError, match="itself"):
        replace(
            revision,
            revision_number=2,
            previous_revision_id=revision.revision_id,
        )

    future_artifact = replace(
        revision.content_artifact,
        created_at="2026-07-27T00:07:00Z",
    )
    with pytest.raises(MemoryRevisionContractError, match="after proposal"):
        replace(revision, content_artifact=future_artifact)


def test_sensitive_classification_requires_sensitive_flag():
    revision = _revision()
    protected = create_content_addressed_artifact(
        b"protected memory",
        media_type="application/vnd.trace-backed-memory.revision+json",
        classification="confidential",
        created_at="2026-07-27T00:05:00Z",
        encryption_key_id="key_001",
    )

    with pytest.raises(
        MemoryRevisionContractError,
        match="sensitive classification",
    ):
        replace(revision, content_artifact=protected)


def test_project_policy_revision_is_proposal_only_and_has_no_case_evidence():
    revision = _project_policy_revision()

    verify_memory_revision_evidence(revision, {})
    assert loads_memory_revision(dumps_memory_revision(revision)) == revision

    with pytest.raises(MemoryRevisionContractError, match="policy memory_type"):
        replace(revision, memory_type="procedural")
    with pytest.raises(MemoryRevisionContractError, match="case/fix"):
        replace(revision, source_case_id="case_001")


def test_structured_evidence_verification_accepts_independent_passing_record():
    evidence = _evidence()
    revision = _revision(evidence=evidence)

    verify_memory_revision_evidence(
        revision,
        {evidence.evidence_id: evidence},
    )


def test_evidence_bundle_binds_fix_and_regression_to_revision():
    fix_evidence = _fix_evidence()
    regression = _evidence()
    revision = _revision(evidence=regression, fix_evidence=fix_evidence)

    verify_memory_revision_evidence_bundle(
        revision,
        {fix_evidence.evidence_id: fix_evidence},
        {regression.evidence_id: regression},
    )

    with pytest.raises(MemoryRevisionContractError, match="missing fix"):
        verify_memory_revision_evidence_bundle(
            revision,
            {},
            {regression.evidence_id: regression},
        )
    with pytest.raises(MemoryRevisionContractError, match="independent"):
        conflicted = _revision(
            evidence=regression,
            fix_evidence=fix_evidence,
            proposed_by=fix_evidence.reviewer_id,
        )
        verify_memory_revision_evidence_bundle(
            conflicted,
            {fix_evidence.evidence_id: fix_evidence},
            {regression.evidence_id: regression},
        )
    with pytest.raises(MemoryRevisionContractError, match="independent"):
        relation_conflicted = _revision(
            evidence=regression,
            fix_evidence=fix_evidence,
            proposed_by=fix_evidence.source_to_fix.verified_by,
        )
        verify_memory_revision_evidence_bundle(
            relation_conflicted,
            {fix_evidence.evidence_id: fix_evidence},
            {regression.evidence_id: regression},
        )
    mismatched = _fix_evidence(source_trace_id="other_trace")
    mismatched_revision = _revision(
        evidence=regression,
        fix_evidence=mismatched,
    )
    with pytest.raises(MemoryRevisionContractError, match="source traces"):
        verify_memory_revision_evidence_bundle(
            mismatched_revision,
            {mismatched.evidence_id: mismatched},
            {regression.evidence_id: regression},
        )


def test_structured_evidence_verification_fails_closed():
    evidence = _evidence()
    revision = _revision(evidence=evidence)

    with pytest.raises(MemoryRevisionContractError, match="missing"):
        verify_memory_revision_evidence(revision, {})
    with pytest.raises(MemoryRevisionContractError, match="independent"):
        verify_memory_revision_evidence(
            _revision(evidence=evidence, proposed_by=evidence.verifier_id),
            {evidence.evidence_id: evidence},
        )
    with pytest.raises(MemoryRevisionContractError, match="passing"):
        failed = _evidence(result="fail")
        verify_memory_revision_evidence(
            _revision(evidence=failed),
            {failed.evidence_id: failed},
        )
    other = _evidence(evaluation_case_id="other")
    with pytest.raises(MemoryRevisionContractError, match="identity"):
        verify_memory_revision_evidence(
            revision,
            {evidence.evidence_id: other},
        )
    different_case = _evidence(case_id="case_002")
    with pytest.raises(MemoryRevisionContractError, match="source_case_id"):
        verify_memory_revision_evidence(
            _revision(evidence=different_case),
            {different_case.evidence_id: different_case},
        )


def test_evidence_verifier_and_serializer_reject_wrong_record_types():
    with pytest.raises(MemoryRevisionContractError, match="revision"):
        verify_memory_revision_evidence(None, {})  # type: ignore[arg-type]
    with pytest.raises(MemoryRevisionContractError, match="revision"):
        dumps_memory_revision(None)  # type: ignore[arg-type]


def test_builder_wraps_invalid_evidence_types():
    revision = _revision()

    with pytest.raises(MemoryRevisionContractError, match="contain strings"):
        build_memory_revision(
            memory_id=revision.memory_id,
            memory_kind=revision.memory_kind,
            revision_number=revision.revision_number,
            previous_revision_id=revision.previous_revision_id,
            memory_type=revision.memory_type,
            content_artifact=revision.content_artifact,
            scope=revision.scope,
            confidence=revision.confidence,
            sensitive=revision.sensitive,
            eval_leaking=revision.eval_leaking,
            source_case_id=revision.source_case_id,
            source_case_revision_id=revision.source_case_revision_id,
            fix_evidence_id=revision.fix_evidence_id,
            regression_evidence_ids=(1,),  # type: ignore[arg-type]
            proposed_by=revision.proposed_by,
            proposed_via_client_id=revision.proposed_via_client_id,
            proposed_at=revision.proposed_at,
            proposal_attestation_sha256=revision.proposal_attestation_sha256,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("content_artifact", None, "content_artifact"),
        ("scope", None, "scope"),
        ("regression_evidence_ids", [], "tuple"),
        ("confidence", True, "confidence"),
    ],
)
def test_builder_rejects_invalid_container_and_scalar_types(
    field: str,
    value: object,
    message: str,
):
    kwargs = _revision_kwargs(_revision())
    kwargs[field] = value

    with pytest.raises(MemoryRevisionContractError, match=message):
        build_memory_revision(**kwargs)  # type: ignore[arg-type]


def test_builder_canonicalizes_nested_scope_and_artifact_timestamps():
    revision = _revision()
    artifact = replace(
        revision.content_artifact,
        created_at="2026-07-27T01:05:00+01:00",
    )
    scope = replace(
        revision.scope,
        attributes=(("tool", "pytest"), ("branch", "main")),
    )
    rebuilt = build_memory_revision(
        memory_id=revision.memory_id,
        memory_kind=revision.memory_kind,
        revision_number=revision.revision_number,
        previous_revision_id=revision.previous_revision_id,
        memory_type=revision.memory_type,
        content_artifact=artifact,
        scope=scope,
        confidence=revision.confidence,
        sensitive=revision.sensitive,
        eval_leaking=revision.eval_leaking,
        source_case_id=revision.source_case_id,
        source_case_revision_id=revision.source_case_revision_id,
        fix_evidence_id=revision.fix_evidence_id,
        regression_evidence_ids=revision.regression_evidence_ids,
        proposed_by=revision.proposed_by,
        proposed_via_client_id=revision.proposed_via_client_id,
        proposed_at=revision.proposed_at,
        proposal_attestation_sha256=revision.proposal_attestation_sha256,
    )

    assert rebuilt.content_artifact.created_at == "2026-07-27T00:05:00Z"
    assert rebuilt.scope.attributes == (
        ("branch", "main"),
        ("tool", "pytest"),
    )
    assert loads_memory_revision(dumps_memory_revision(rebuilt)) == rebuilt


def test_parser_normalizes_schema_valid_number_and_evidence_order():
    first = _evidence()
    second = _evidence(evaluation_case_id="case_fixed_variant")
    payload = _revision(evidence=first).to_dict()
    payload["confidence"] = 1
    payload["regression_evidence_ids"] = [
        second.evidence_id,
        first.evidence_id,
    ]
    unsigned = dict(payload)
    del unsigned["revision_id"]
    unsigned["confidence"] = 1.0
    unsigned["regression_evidence_ids"] = sorted(
        unsigned["regression_evidence_ids"]  # type: ignore[arg-type]
    )
    from trace_backed_memory.memory_revision_v3 import memory_revision_id

    payload["revision_id"] = memory_revision_id(unsigned)

    parsed = parse_memory_revision(payload)

    assert parsed.confidence == 1.0
    assert parsed.regression_evidence_ids == tuple(
        sorted((first.evidence_id, second.evidence_id))
    )


@pytest.mark.parametrize(
    "document",
    [
        '{"revision_id":"first","revision_id":"second"}',
        '{"confidence":NaN}',
        '{"nested":' * 33 + "null" + "}" * 33,
    ],
)
def test_revision_json_rejects_duplicate_nonfinite_and_deep_input(
    document: str,
):
    with pytest.raises(MemoryRevisionContractError) as captured:
        loads_memory_revision(document)

    assert captured.value.code == "TBM_MEMORY_REVISION_INVALID_JSON"


def test_revision_json_rejects_invalid_utf8_and_oversized_input():
    for document in (
        b"\xff",
        "x" * (MEMORY_REVISION_JSON_MAX_BYTES + 1),
    ):
        with pytest.raises(MemoryRevisionContractError) as captured:
            loads_memory_revision(document)
        assert captured.value.code == "TBM_MEMORY_REVISION_INVALID_JSON"


def test_revision_json_rejects_wrong_source_and_top_level_shape():
    with pytest.raises(MemoryRevisionContractError) as captured:
        loads_memory_revision(1)  # type: ignore[arg-type]
    assert captured.value.code == "TBM_MEMORY_REVISION_INVALID_JSON"

    with pytest.raises(MemoryRevisionContractError, match="object"):
        loads_memory_revision("[]")


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda payload: payload.pop("memory_id"), "fields"),
        (lambda payload: payload.update({"extra": True}), "fields"),
        (lambda payload: payload.update({1: "value"}), "keys"),
        (
            lambda payload: payload.update(
                {"regression_evidence_ids": "not-an-array"}
            ),
            "array of strings",
        ),
        (
            lambda payload: payload.update(
                {"regression_evidence_ids": ["bad"]}
            ),
            "invalid evidence ID",
        ),
    ],
)
def test_parser_rejects_malformed_top_level_contract(
    mutator,
    message: str,
):
    payload = _revision().to_dict()
    mutator(payload)

    with pytest.raises(MemoryRevisionContractError, match=message):
        parse_memory_revision(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("content_artifact", [], "content_artifact"),
        ("scope", [], "scope"),
    ],
)
def test_parser_rejects_non_object_nested_records(
    field: str,
    value: object,
    message: str,
):
    payload = _revision().to_dict()
    payload[field] = value

    with pytest.raises(MemoryRevisionContractError, match=message):
        parse_memory_revision(payload)


def test_parser_rejects_non_string_scope_attributes():
    payload = _revision().to_dict()
    scope = dict(payload["scope"])  # type: ignore[arg-type]
    scope["attributes"] = {"branch": 1}
    payload["scope"] = scope

    with pytest.raises(MemoryRevisionContractError, match="string mapping"):
        parse_memory_revision(payload)


def test_parser_rejects_duplicate_evidence_references():
    payload = _revision().to_dict()
    evidence_id = payload["regression_evidence_ids"][0]  # type: ignore[index]
    payload["regression_evidence_ids"] = [evidence_id, evidence_id]

    with pytest.raises(MemoryRevisionContractError, match="duplicates"):
        parse_memory_revision(payload)


def test_schema_example_and_public_exports_match_runtime_contract():
    example = json.loads(
        (ROOT / "examples" / "memory_revision_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT / "schemas" / "memory_revision_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert parse_memory_revision(example).to_dict() == example
    assert set(schema["required"]) == set(example)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["contentArtifact"]["allOf"]
    assert set(
        schema["$defs"]["authorizationScope"]["properties"]["attributes"][
            "propertyNames"
        ]["enum"]
    ) == {
        "branch",
        "prompt_version",
        "prompt_family",
        "tool",
        "tool_schema_version",
        "model",
        "model_family",
        "eval_suite",
        "task_type",
        "failure_type",
    }
    assert tbm.MemoryRevision is MemoryRevision
    assert "MemoryRevision" in tbm.__all__


def test_parser_rejects_unencrypted_sensitive_artifact_and_unknown_scope_key():
    payload = _revision().to_dict()
    artifact = dict(payload["content_artifact"])  # type: ignore[arg-type]
    artifact["classification"] = "confidential"
    payload["content_artifact"] = artifact
    payload["sensitive"] = True

    with pytest.raises(MemoryRevisionContractError, match="content_artifact"):
        parse_memory_revision(payload)

    payload = _revision().to_dict()
    scope = dict(payload["scope"])  # type: ignore[arg-type]
    scope["attributes"] = {"unknown": "value"}
    payload["scope"] = scope
    with pytest.raises(MemoryRevisionContractError, match="scope"):
        parse_memory_revision(payload)


def test_parser_canonicalizes_artifact_offset_and_rejects_schema_invalid_offset():
    payload = _revision().to_dict()
    artifact = dict(payload["content_artifact"])  # type: ignore[arg-type]
    artifact["created_at"] = "2026-07-27T01:05:00+01:00"
    payload["content_artifact"] = artifact

    parsed = parse_memory_revision(payload)

    assert parsed.content_artifact.created_at == "2026-07-27T00:05:00Z"
    assert loads_memory_revision(dumps_memory_revision(parsed)) == parsed

    artifact["created_at"] = "2026-07-27T23:05:00+23:00"
    with pytest.raises(MemoryRevisionContractError, match="RFC 3339"):
        parse_memory_revision(payload)


def test_huge_confidence_fails_with_stable_contract_error():
    payload = _revision().to_dict()
    payload["confidence"] = 10**10_000

    with pytest.raises(MemoryRevisionContractError, match="confidence"):
        parse_memory_revision(payload)
