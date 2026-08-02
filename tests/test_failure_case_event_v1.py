from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
import trace_backed_memory.failure_case_event_v1 as failure_case_events
from trace_backed_memory.contracts_v3 import CommitRelationEvidence
from trace_backed_memory.event_v1 import EventArtifactRef, EventSource
from trace_backed_memory.evidence_v3 import build_structured_regression_evidence
from trace_backed_memory.failure_case_event_v1 import (
    FAILURE_CASE_EVENT_TYPES,
    FAILURE_CASE_EXTRACTOR_PROPOSED,
    FAILURE_CASE_FIX_EVIDENCE_RECORDED,
    FAILURE_CASE_LEGACY_IMPORTED,
    FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED,
    FAILURE_CASE_REVIEWED,
    FailureCaseEventV1Error,
    build_failure_case_event_batch,
    build_failure_case_event_registry,
    build_failure_case_extractor_proposal,
    build_failure_case_fix_evidence_draft,
    build_failure_case_proposal_draft,
    build_failure_case_regression_evidence_draft,
    build_failure_case_review_draft,
    build_legacy_failure_case_import_draft,
    dumps_failure_case_event_payload_dispatch_schema,
    reduce_failure_case_events,
)
from trace_backed_memory.fix_evidence_v3 import build_fix_evidence
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.trace_event_v1 import (
    TRACE_SESSION_ENDED,
    TRACE_SESSION_STARTED,
    TraceEventDraft,
    TraceEventLineage,
    build_trace_event_batch,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
ROOT = Path(__file__).resolve().parents[1]


def _access(*, tenant_id: str = "tenant_001") -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id=tenant_id,
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="failure_case_adapter",
        authorization_decision_id="authorization_failure_case_append",
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _artifact(suffix: str, *, media_type: str) -> EventArtifactRef:
    digest = "sha256:" + suffix * 64
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + suffix * 64,
        content_sha256=digest,
        media_type=media_type,
        size_bytes=128,
        classification="confidential",
        retention_policy_id="retention_failure_evidence",
        encryption_key_id="failure_evidence_key_001",
        availability="available",
    )


def _trace_events():
    source_artifact = _artifact("a", media_type="application/trace+json")
    lineage = TraceEventLineage(
        role="root",
        subagent_id=None,
        parent_trace_id=None,
        parent_event_id=None,
    )
    drafts = (
        TraceEventDraft(
            event_type=TRACE_SESSION_STARTED,
            trace_id="trace_failure_001",
            run_id="run_failure_001",
            sequence=1,
            occurred_at="2026-08-02T00:00:01Z",
            artifact_refs=(),
            tool=None,
            permission_result=None,
            lineage=lineage,
            related_subagent_id=None,
        ),
        TraceEventDraft(
            event_type=TRACE_SESSION_ENDED,
            trace_id="trace_failure_001",
            run_id="run_failure_001",
            sequence=2,
            occurred_at="2026-08-02T00:00:02Z",
            artifact_refs=(source_artifact,),
            tool=None,
            permission_result=None,
            lineage=lineage,
            related_subagent_id=None,
            classification="confidential",
        ),
    )
    events, _ = build_trace_event_batch(
        drafts,
        access=_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-08-02T00:00:02Z",
    )
    return events


def _proposal():
    artifact = _artifact("b", media_type="application/failure-proposal+json")
    proposal = build_failure_case_extractor_proposal(
        _trace_events(),
        case_id="case_failure_001",
        failure_type="tool_error",
        proposal_artifacts=(artifact,),
        extractor_id="extractor_failure_001",
        extractor_version="v1",
        extractor_configuration_sha256=DIGEST_C,
        proposed_at="2026-08-02T00:00:03Z",
    )
    return proposal, artifact


def _fix_evidence():
    return build_fix_evidence(
        case_id="case_failure_001",
        source_trace_id="trace_failure_001",
        source_commit_sha="source_commit_001",
        fix_commit_sha="fix_commit_001",
        source_to_fix=CommitRelationEvidence(
            "source_commit_001",
            "fix_commit_001",
            "ancestor",
            "git_verifier_001",
            "2026-08-02T00:00:03Z",
        ),
        artifact_hashes=(DIGEST_A,),
        submitter_id="fix_submitter_001",
        submitted_at="2026-08-02T00:00:04Z",
        reviewer_id="fix_reviewer_001",
        reviewed_at="2026-08-02T00:00:05Z",
        attestation_sha256=DIGEST_B,
    )


def _regression_evidence(
    *,
    result: str = "pass",
    suffix: str = "pass",
    verified_at: str = "2026-08-02T00:00:08Z",
    source_trace_id: str = "trace_failure_001",
    verifier_id: str = "regression_verifier_001",
):
    return build_structured_regression_evidence(
        case_id="case_failure_001",
        source_trace_id=source_trace_id,
        verification_trace_id="trace_verification_" + suffix,
        verification_run_id="run_verification_" + suffix,
        evaluator_id="evaluator_regression_001",
        evaluator_version="v1",
        evaluation_suite="failure_regressions",
        evaluation_case_id="no_repeat_failure_" + suffix,
        expected_outcome="workflow completes",
        observed_outcome="workflow result " + suffix,
        result=result,  # type: ignore[arg-type]
        environment={"os": "linux", "python": "3.11"},
        source_commit_sha="source_commit_001",
        fix_commit_sha="fix_commit_001",
        verification_commit_sha="verification_commit_" + suffix,
        source_to_fix=CommitRelationEvidence(
            "source_commit_001",
            "fix_commit_001",
            "ancestor",
            "git_verifier_001",
            "2026-08-02T00:00:03Z",
        ),
        fix_to_verification=CommitRelationEvidence(
            "fix_commit_001",
            "verification_commit_" + suffix,
            "ancestor",
            "git_verifier_001",
            "2026-08-02T00:00:05Z",
        ),
        artifact_hashes=(DIGEST_C,),
        submitter_id="regression_submitter_001",
        submitted_at="2026-08-02T00:00:06Z",
        verifier_id=verifier_id,
        verified_at=verified_at,
        attestation_sha256=DIGEST_B,
    )


def _native_drafts(*, decision: str = "accepted"):
    proposal, artifact = _proposal()
    return (
        build_failure_case_proposal_draft(
            proposal,
            proposal_artifacts=(artifact,),
        ),
        build_failure_case_review_draft(
            proposal,
            reviewer_id="human_reviewer_001",
            decision=decision,  # type: ignore[arg-type]
            reason_code="confirmed_failure" if decision == "accepted" else "noise",
            reviewed_at="2026-08-02T00:00:04Z",
            attestation_sha256=DIGEST_A,
        ),
    )


def _events(drafts):
    events, _ = build_failure_case_event_batch(
        tuple(drafts),
        access=_access(),
        expected_stream_version=0,
        next_global_position=10,
        previous_event=None,
        recorded_at="2026-08-02T00:00:09Z",
    )
    return events


def test_extractor_proposal_is_event_linked_but_remains_candidate():
    proposal, artifact = _proposal()
    projection = reduce_failure_case_events(
        _events(
            (
                build_failure_case_proposal_draft(
                    proposal,
                    proposal_artifacts=(artifact,),
                ),
            )
        )
    )

    assert projection is not None
    assert projection.status == "candidate"
    assert projection.evidence_quality == "none"
    assert projection.eligible_for_new_memory is False
    assert projection.source_event_sha256s == tuple(
        event.event_sha256 for event in _trace_events()
    )
    assert projection.artifact_ids == (artifact.artifact_id,)


def test_structured_review_fix_and_passing_regression_verify_case():
    drafts = (
        *_native_drafts(),
        build_failure_case_fix_evidence_draft(_fix_evidence()),
        build_failure_case_regression_evidence_draft(_regression_evidence()),
    )
    events = _events(drafts)
    projection = reduce_failure_case_events(events)

    assert projection is not None
    assert projection.status == "verified"
    assert projection.evidence_quality == "structured_verified"
    assert projection.eligible_for_new_memory is True
    assert projection.fix_evidence_id == _fix_evidence().evidence_id
    assert projection.regression_evidence_id == _regression_evidence().evidence_id
    assert projection.projection_sha256 == reduce_failure_case_events(
        events
    ).projection_sha256
    assert tuple(event.event_type for event in events) == (
        FAILURE_CASE_EXTRACTOR_PROPOSED,
        FAILURE_CASE_REVIEWED,
        FAILURE_CASE_FIX_EVIDENCE_RECORDED,
        FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED,
    )


def test_failed_regression_remains_unverified_until_later_pass():
    failed = _regression_evidence(
        result="fail",
        suffix="fail",
        verified_at="2026-08-02T00:00:07Z",
    )
    passing = _regression_evidence()
    before = reduce_failure_case_events(
        _events(
            (
                *_native_drafts(),
                build_failure_case_fix_evidence_draft(_fix_evidence()),
                build_failure_case_regression_evidence_draft(failed),
            )
        )
    )
    after = reduce_failure_case_events(
        _events(
            (
                *_native_drafts(),
                build_failure_case_fix_evidence_draft(_fix_evidence()),
                build_failure_case_regression_evidence_draft(failed),
                build_failure_case_regression_evidence_draft(passing),
            )
        )
    )

    assert before is not None and before.status == "reviewed"
    assert before.eligible_for_new_memory is False
    assert after is not None and after.status == "verified"
    assert after.eligible_for_new_memory is True


def test_rejected_or_out_of_order_case_never_accepts_structured_evidence():
    rejected = reduce_failure_case_events(
        _events(_native_drafts(decision="rejected"))
    )
    assert rejected is not None and rejected.status == "rejected"
    assert rejected.eligible_for_new_memory is False

    proposal, artifact = _proposal()
    with pytest.raises(FailureCaseEventV1Error) as caught:
        reduce_failure_case_events(
            _events(
                (
                    build_failure_case_proposal_draft(
                        proposal,
                        proposal_artifacts=(artifact,),
                    ),
                    build_failure_case_fix_evidence_draft(_fix_evidence()),
                )
            )
        )
    assert caught.value.code == "TBM_FAILURE_CASE_TRANSITION_INVALID"


def test_legacy_boolean_is_downgraded_and_cannot_seed_new_memory():
    source = EventSource(
        source_system="snapshot_v2",
        source_record_id="case_legacy_001",
        evidence_quality="legacy_partial",
        observed_at="2026-08-02T00:00:01Z",
    )
    event = _events(
        (
            build_legacy_failure_case_import_draft(
                case_id="case_legacy_001",
                source_trace_id="trace_legacy_001",
                failure_type="other",
                regression_passed=True,
                imported_at="2026-08-02T00:00:02Z",
                source=source,
            ),
        )
    )[0]
    projection = reduce_failure_case_events((event,))

    assert event.event_type == FAILURE_CASE_LEGACY_IMPORTED
    assert event.origin == "imported"
    assert projection is not None
    assert projection.status == "legacy_imported"
    assert projection.evidence_quality == "legacy_unstructured"
    assert projection.eligible_for_new_memory is False


def test_regression_must_match_fix_trace_and_commits_and_independent_verifier():
    wrong_trace = _regression_evidence(
        source_trace_id="trace_other_001",
    )
    with pytest.raises(FailureCaseEventV1Error) as trace_error:
        reduce_failure_case_events(
            _events(
                (
                    *_native_drafts(),
                    build_failure_case_fix_evidence_draft(_fix_evidence()),
                    build_failure_case_regression_evidence_draft(wrong_trace),
                )
            )
        )
    assert trace_error.value.code == "TBM_FAILURE_CASE_TRANSITION_INVALID"

    forged = _regression_evidence(verifier_id="human_reviewer_001")
    with pytest.raises(FailureCaseEventV1Error) as verifier_error:
        reduce_failure_case_events(
            _events(
                (
                    *_native_drafts(),
                    build_failure_case_fix_evidence_draft(_fix_evidence()),
                    build_failure_case_regression_evidence_draft(forged),
                )
            )
        )
    assert verifier_error.value.code == (
        "TBM_FAILURE_CASE_EVIDENCE_INDEPENDENCE_REQUIRED"
    )


def test_registry_schema_is_strict_deterministic_and_complete():
    registry = build_failure_case_event_registry()
    schema_text = dumps_failure_case_event_payload_dispatch_schema()
    schema = json.loads(schema_text)
    Draft202012Validator.check_schema(schema)

    assert registry.sealed
    assert tuple(
        row["event_type"] for row in registry.catalog()["event_types"]
    ) == FAILURE_CASE_EVENT_TYPES
    assert schema_text == dumps_failure_case_event_payload_dispatch_schema()
    assert (
        ROOT
        / "schemas"
        / "failure_case_event_payload_registry_v1.schema.json"
    ).read_text(encoding="utf-8") == schema_text
    assert read_packaged_resource(
        "schemas/failure_case_event_payload_registry_v1.schema.json"
    ).decode() == schema_text
    assert tbm.FailureCaseProjection.__module__.endswith("failure_case_event_v1")
    assert tbm.reduce_failure_case_events is reduce_failure_case_events


def test_proposal_artifact_descriptor_and_candidate_status_are_exact():
    proposal, artifact = _proposal()
    with pytest.raises(FailureCaseEventV1Error) as artifact_error:
        build_failure_case_proposal_draft(
            proposal,
            proposal_artifacts=(
                _artifact("c", media_type="application/failure-proposal+json"),
            ),
        )
    assert artifact_error.value.code == "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID"

    with pytest.raises(FailureCaseEventV1Error) as candidate_error:
        replace(proposal, candidate_status="verified")  # type: ignore[arg-type]
    assert candidate_error.value.code == "TBM_FAILURE_CASE_EXTRACTOR_CANNOT_VERIFY"


def test_validated_drafts_are_private_immutable_and_partition_bound():
    assert not hasattr(tbm, "FailureCaseEventDraft")
    fix_draft = build_failure_case_fix_evidence_draft(_fix_evidence())
    with pytest.raises(FailureCaseEventV1Error) as producer_error:
        failure_case_events._FailureCaseEventDraft(
            event_type=fix_draft.event_type,
            case_id=fix_draft.case_id,
            occurred_at=fix_draft.occurred_at,
            payload=fix_draft.payload,
        )
    assert producer_error.value.code == "TBM_FAILURE_CASE_DRAFT_PRODUCER_REQUIRED"
    with pytest.raises(TypeError):
        cast(Any, fix_draft.payload)["evidence_sha256"] = DIGEST_C

    proposal, artifact = _proposal()
    with pytest.raises(FailureCaseEventV1Error) as partition_error:
        build_failure_case_event_batch(
            (
                build_failure_case_proposal_draft(
                    proposal,
                    proposal_artifacts=(artifact,),
                ),
            ),
            access=_access(tenant_id="tenant_other"),
            expected_stream_version=0,
            next_global_position=1,
            previous_event=None,
            recorded_at="2026-08-02T00:00:09Z",
        )
    assert partition_error.value.code == (
        "TBM_FAILURE_CASE_SOURCE_PARTITION_MISMATCH"
    )
