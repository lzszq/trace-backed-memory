from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.contracts_v3 import CommitRelationEvidence
from trace_backed_memory.event_v1 import (
    EventArtifactRef,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.evidence_v3 import build_structured_regression_evidence
from trace_backed_memory.fix_evidence_v3 import build_fix_evidence
from trace_backed_memory.git_graph_reducer_v1 import (
    GitGraphV1Error,
    build_git_graph_reducer,
    pr_anchor_commit_ancestry_evidence,
    reduce_git_graph_events,
)
from trace_backed_memory.git_observation_v1 import (
    GitAncestryObservation,
    GitAncestryRelation,
    GitCheckoutObservation,
    GitCommitObservation,
    GitDiffObservation,
    GitObjectAvailability,
    GitObjectAvailabilityObservation,
    GitObservationDraft,
    GitObservationProvenance,
    GitRefObservation,
    GitShallowStateObservation,
    build_git_observation_batch,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.models import PRCaseProvenance


CURRENT = "a" * 40
TREE = "b" * 40
PARENT = "c" * 40
SOURCE = "d" * 40
FIX = "e" * 40
VERIFICATION = "f" * 40
OTHER = "1" * 40
OBSERVED_AT = "2026-08-02T01:00:00Z"
RECORDED_AT = "2026-08-02T01:00:01Z"


def _access(
    *,
    repository_id: str = "repository_001",
    classifications: tuple[str, ...] = (
        "public",
        "internal",
        "confidential",
        "restricted",
    ),
) -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id=repository_id,
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="service_git_graph",
        authorization_decision_id="authorization_git_graph",
        classification_filter=LedgerClassificationFilter(classifications),
    )


def _artifact(data: bytes = b"diff --git a/a.txt b/a.txt\n") -> EventArtifactRef:
    digest = hashlib.sha256(data).hexdigest()
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + digest,
        content_sha256="sha256:" + digest,
        media_type="application/vnd.git.diff",
        size_bytes=len(data),
        classification="confidential",
        retention_policy_id="retention_git_diff",
        encryption_key_id="git_diff_key_001",
        availability="available",
    )


def _provenance() -> GitObservationProvenance:
    return GitObservationProvenance(
        runner_id="tbm_git_capture",
        runner_version="f3-v1",
        algorithm_id="git_observation",
        algorithm_version="v1",
        git_version="git version 2.50.1.windows.1",
    )


def _drafts(
    *,
    shallow: str = "full",
    current_availability: str = "present",
    source_availability: str = "present",
    ancestry_status: str = "ancestor",
    ref_oid: str = CURRENT,
    object_format: str = "sha1",
) -> tuple[GitObservationDraft, ...]:
    artifact = _artifact()
    objects = tuple(
        GitObjectAvailability(object_oid=oid, status=status)  # type: ignore[arg-type]
        for oid, status in sorted(
            {
                CURRENT: current_availability,
                PARENT: "present",
                SOURCE: source_availability,
                FIX: "present",
                VERIFICATION: "present",
            }.items()
        )
    )
    observations = (
        GitCheckoutObservation(
            root_sha256="sha256:" + "2" * 64,
            repository_name="repo",
            object_format="sha1",
            head_oid=CURRENT,
            dirty=True,
        ),
        GitRefObservation(
            object_format=object_format,  # type: ignore[arg-type]
            target_oid=ref_oid,
            ref_name="refs/heads/main",
            detached=False,
        ),
        GitCommitObservation(
            object_format="sha1",
            commit_oid=CURRENT,
            tree_oid=TREE,
            parent_oids=(PARENT,),
        ),
        GitDiffObservation(
            object_format="sha1",
            base_oid=CURRENT,
            target="index_and_worktree",
            content_sha256=artifact.content_sha256,
            size_bytes=artifact.size_bytes,
            artifact_id=artifact.artifact_id,
        ),
        GitObjectAvailabilityObservation(
            object_format="sha1",
            objects=objects,
        ),
        GitAncestryObservation(
            object_format="sha1",
            current_oid=CURRENT,
            relations=(
                GitAncestryRelation(
                    anchor_oid=SOURCE,
                    status=ancestry_status,  # type: ignore[arg-type]
                ),
            ),
        ),
        GitShallowStateObservation(state=shallow),  # type: ignore[arg-type]
    )
    return tuple(
        GitObservationDraft(
            checkout_id="checkout_001",
            sequence=index,
            observed_at=OBSERVED_AT,
            provenance=_provenance(),
            observation=observation,
            artifact_refs=(artifact,) if type(observation) is GitDiffObservation else (),
        )
        for index, observation in enumerate(observations, start=1)
    )


def _events(
    *,
    access: LedgerAccessContext | None = None,
    **draft_options: str,
):
    events, _ = build_git_observation_batch(
        _drafts(**draft_options),
        access=_access() if access is None else access,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    return events


def _clone_event(
    event,
    *,
    request_sha256: str,
):
    return build_canonical_event(
        event_id=event.event_id,
        event_type=event.event_type,
        event_version=event.event_version,
        event_kind=event.event_kind,
        origin=event.origin,
        source=event.source,
        stream_id=event.stream_id,
        stream_type=event.stream_type,
        stream_version=event.stream_version,
        global_position=event.global_position,
        trusted_context=EventTrustedContext(
            organization_id=event.organization_id,
            tenant_id=event.tenant_id,
            repository_id=event.repository_id,
            environment_id=event.environment_id,
            principal_id=event.principal_id,
            agent_client_id=event.agent_client_id,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            authorization_decision_id=event.authorization_decision_id,
        ),
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=request_sha256,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        artifact_refs=event.artifact_refs,
        payload=event.payload,
    )


def _evidence():
    source_to_fix = CommitRelationEvidence(
        from_commit_sha=SOURCE,
        to_commit_sha=FIX,
        relation="ancestor",
        verified_by="git_relation_verifier",
        verified_at="2026-08-02T00:50:00Z",
    )
    fix_to_verification = CommitRelationEvidence(
        from_commit_sha=FIX,
        to_commit_sha=VERIFICATION,
        relation="ancestor",
        verified_by="git_relation_verifier",
        verified_at="2026-08-02T00:51:00Z",
    )
    fix = build_fix_evidence(
        case_id="case_001",
        source_trace_id="trace_source_001",
        source_commit_sha=SOURCE,
        fix_commit_sha=FIX,
        source_to_fix=source_to_fix,
        artifact_hashes=(),
        submitter_id="fix_submitter",
        submitted_at="2026-08-02T00:55:00Z",
        reviewer_id="fix_reviewer",
        reviewed_at="2026-08-02T00:56:00Z",
        attestation_sha256="sha256:" + "3" * 64,
    )
    regression = build_structured_regression_evidence(
        case_id="case_001",
        source_trace_id="trace_source_001",
        verification_trace_id="trace_verification_001",
        verification_run_id="run_verification_001",
        evaluator_id="evaluator_001",
        evaluator_version="v1",
        evaluation_suite="suite_001",
        evaluation_case_id="evaluation_case_001",
        expected_outcome="pass",
        observed_outcome="pass",
        result="pass",
        environment={"python": "3.11"},
        source_commit_sha=SOURCE,
        fix_commit_sha=FIX,
        verification_commit_sha=VERIFICATION,
        source_to_fix=source_to_fix,
        fix_to_verification=fix_to_verification,
        artifact_hashes=(),
        submitter_id="regression_submitter",
        submitted_at="2026-08-02T00:58:00Z",
        verifier_id="regression_verifier",
        verified_at="2026-08-02T00:59:00Z",
        attestation_sha256="sha256:" + "4" * 64,
    )
    return fix, regression


def _pr_case(
    *, case_id: str = "case_001", commit_sha: str = SOURCE
) -> PRCaseProvenance:
    return PRCaseProvenance(
        case_id=case_id,
        source_trace_id="trace_source_001",
        commit_sha=commit_sha,
        fix_commit_sha=FIX,
        trace_uri="trace://source/001",
        failure_type="regression",
        matched_change_endpoint="old",
    )


def test_git_graph_replay_projects_graph_confidence_evidence_and_pr_anchors():
    fix, regression = _evidence()
    events = _events()
    projection = reduce_git_graph_events(
        events,
        access=_access(),
        fix_evidence=(fix,),
        regression_evidence=(regression,),
        pr_case_provenance=(_pr_case(),),
    )

    assert projection is not None
    assert projection.repository.repository_id == "repository_001"
    assert projection.repository.observed_repository_name == "repo"
    assert projection.head_oid == CURRENT
    assert projection.shallow_state == "full"
    assert tuple(item.commit_oid for item in projection.commits) == tuple(
        sorted({CURRENT, PARENT, SOURCE, FIX, VERIFICATION})
    )
    observed_current = next(item for item in projection.commits if item.commit_oid == CURRENT)
    assert observed_current.observed
    assert observed_current.parent_oids == (PARENT,)
    assert projection.parent_relations[0].confidence == "locally_observed"
    assert projection.parent_relations[0].parent_oid == PARENT
    ancestry = projection.ancestry_relations[0]
    assert ancestry.reported_status == "ancestor"
    assert ancestry.status == "ancestor"
    assert ancestry.confidence == "locally_observed"
    assert projection.missing_objects == ()
    assert tuple(item.relation_kind for item in projection.evidence_relations) == (
        "fix_to_verification",
        "source_to_fix",
        "source_to_fix",
    )
    assert all(
        item.confidence == "independently_verified"
        for item in projection.evidence_relations
    )
    assert projection.pr_anchors[0].anchor_oid == SOURCE
    assert projection.pr_anchors[0].case_ids == ("case_001",)
    assert projection.pr_anchors[0].status == "ancestor"
    assert projection.last_observation.stream_version == 7
    assert projection.last_observation.runner_version == "f3-v1"
    assert projection.last_validated_at == OBSERVED_AT
    assert projection.to_dict()["projection_sha256"] == projection.projection_sha256
    assert pr_anchor_commit_ancestry_evidence(projection).commit_relations == (
        (SOURCE, True),
    )

    replayed = reduce_git_graph_events(
        events,
        access=_access(),
        fix_evidence=(fix,),
        regression_evidence=(regression,),
        pr_case_provenance=(_pr_case(),),
    )
    assert replayed == projection
    assert replayed is not None
    assert replayed.projection_sha256 == projection.projection_sha256
    assert build_git_graph_reducer().descriptor.deterministic


@pytest.mark.parametrize(
    (
        "current_availability",
        "source_availability",
        "shallow",
        "reported",
    ),
    [
        ("missing", "present", "full", "ancestor"),
        ("present", "missing", "full", "ancestor"),
        ("present", "unknown", "full", "not_ancestor"),
        ("present", "present", "shallow", "not_ancestor"),
        ("present", "present", "unknown", "ancestor"),
    ],
)
def test_missing_unknown_and_shallow_never_become_false_ancestry(
    current_availability: str,
    source_availability: str,
    shallow: str,
    reported: str,
):
    projection = reduce_git_graph_events(
        _events(
            source_availability=source_availability,
            current_availability=current_availability,
            shallow=shallow,
            ancestry_status=reported,
        ),
        access=_access(),
        pr_case_provenance=(_pr_case(),),
    )

    assert projection is not None
    relation = projection.ancestry_relations[0]
    assert relation.reported_status == reported
    assert relation.status == "unknown"
    assert relation.confidence == "indeterminate"
    assert relation.last_validated_at is None
    assert projection.pr_anchors[0].status == "unknown"
    with pytest.raises(GitGraphV1Error) as error:
        pr_anchor_commit_ancestry_evidence(projection)
    assert error.value.code == "TBM_GIT_GRAPH_PR_ANCHOR_UNVERIFIED"
    expected_missing = {
        oid: status
        for oid, status in (
            (CURRENT, current_availability),
            (SOURCE, source_availability),
        )
        if status != "present"
    }
    assert {item.object_oid: item.status for item in projection.missing_objects} == (
        expected_missing
    )


def test_missing_relation_keeps_pr_anchor_unknown_and_sorted_unique():
    drafts = tuple(
        draft
        for draft in _drafts()
        if type(draft.observation) is not GitAncestryObservation
    )
    drafts = tuple(replace(draft, sequence=index) for index, draft in enumerate(drafts, 1))
    events, _ = build_git_observation_batch(
        drafts,
        access=_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    projection = reduce_git_graph_events(
        events,
        access=_access(),
        pr_case_provenance=(
            _pr_case(case_id="case_002"),
            _pr_case(case_id="case_001"),
        ),
    )

    assert projection is not None
    assert len(projection.pr_anchors) == 1
    assert projection.pr_anchors[0].case_ids == ("case_001", "case_002")
    assert projection.pr_anchors[0].anchor_oid == SOURCE
    assert projection.pr_anchors[0].status == "unknown"


def test_replay_rejects_scope_classification_sequence_and_capture_conflicts():
    foreign_events = _events(access=_access(repository_id="repository_foreign"))
    with pytest.raises(GitGraphV1Error) as scope:
        reduce_git_graph_events(foreign_events, access=_access())
    assert scope.value.code == "TBM_GIT_GRAPH_SCOPE_MISMATCH"

    with pytest.raises(GitGraphV1Error) as classification:
        reduce_git_graph_events(
            _events(),
            access=_access(classifications=("public", "internal")),
        )
    assert classification.value.code == "TBM_GIT_GRAPH_CLASSIFICATION_DENIED"

    with pytest.raises(GitGraphV1Error) as sequence:
        reduce_git_graph_events(_events()[1:], access=_access())
    assert sequence.value.code == "TBM_GIT_GRAPH_SEQUENCE_INVALID"

    with pytest.raises(GitGraphV1Error) as head:
        reduce_git_graph_events(_events(ref_oid=OTHER), access=_access())
    assert head.value.code == "TBM_GIT_GRAPH_HEAD_CONFLICT"

    with pytest.raises(GitGraphV1Error) as object_format:
        reduce_git_graph_events(
            _events(ref_oid="1" * 64, object_format="sha256"),
            access=_access(),
        )
    assert object_format.value.code == "TBM_GIT_GRAPH_OBJECT_FORMAT_CONFLICT"


def test_duplicate_points_and_conflicting_ancestry_poison_replay():
    first = _events()
    duplicate = (
        GitObservationDraft(
            checkout_id="checkout_001",
            sequence=1,
            observed_at=OBSERVED_AT,
            provenance=_provenance(),
            observation=GitShallowStateObservation(state="full"),
        ),
        GitObservationDraft(
            checkout_id="checkout_001",
            sequence=2,
            observed_at=OBSERVED_AT,
            provenance=_provenance(),
            observation=GitShallowStateObservation(state="full"),
        ),
    )
    duplicate_events, _ = build_git_observation_batch(
        duplicate,
        access=_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    with pytest.raises(GitGraphV1Error) as duplicate_error:
        reduce_git_graph_events(duplicate_events, access=_access())
    assert duplicate_error.value.code == "TBM_GIT_GRAPH_CAPTURE_CONFLICT"

    second_draft = GitObservationDraft(
        checkout_id="checkout_001",
        sequence=8,
        observed_at="2026-08-02T02:00:00Z",
        provenance=_provenance(),
        observation=GitAncestryObservation(
            object_format="sha1",
            current_oid=CURRENT,
            relations=(
                GitAncestryRelation(anchor_oid=SOURCE, status="not_ancestor"),
            ),
        ),
    )
    second, _ = build_git_observation_batch(
        (second_draft,),
        access=_access(),
        expected_stream_version=7,
        next_global_position=8,
        previous_event=first[-1],
        recorded_at="2026-08-02T02:00:01Z",
    )
    with pytest.raises(GitGraphV1Error) as conflict:
        reduce_git_graph_events(first + second, access=_access())
    assert conflict.value.code == "TBM_GIT_GRAPH_ANCESTRY_CONFLICT"


def test_observed_commit_cycle_is_rejected():
    first = _events()
    cycle_draft = GitObservationDraft(
        checkout_id="checkout_001",
        sequence=8,
        observed_at="2026-08-02T02:00:00Z",
        provenance=_provenance(),
        observation=GitCommitObservation(
            object_format="sha1",
            commit_oid=PARENT,
            tree_oid=OTHER,
            parent_oids=(CURRENT,),
        ),
    )
    second, _ = build_git_observation_batch(
        (cycle_draft,),
        access=_access(),
        expected_stream_version=7,
        next_global_position=8,
        previous_event=first[-1],
        recorded_at="2026-08-02T02:00:01Z",
    )

    with pytest.raises(GitGraphV1Error) as cycle:
        reduce_git_graph_events(first + second, access=_access())
    assert cycle.value.code == "TBM_GIT_GRAPH_CYCLE"


def test_unknown_cannot_hide_conflicting_known_ancestry():
    first = _events()
    unknown_draft = GitObservationDraft(
        checkout_id="checkout_001",
        sequence=8,
        observed_at="2026-08-02T02:00:00Z",
        provenance=_provenance(),
        observation=GitAncestryObservation(
            object_format="sha1",
            current_oid=CURRENT,
            relations=(GitAncestryRelation(anchor_oid=SOURCE, status="unknown"),),
        ),
    )
    unknown_events, _ = build_git_observation_batch(
        (unknown_draft,),
        access=_access(),
        expected_stream_version=7,
        next_global_position=8,
        previous_event=first[-1],
        recorded_at="2026-08-02T02:00:01Z",
    )
    contradiction_drafts = (
        GitObservationDraft(
            checkout_id="checkout_001",
            sequence=9,
            observed_at="2026-08-02T03:00:00Z",
            provenance=_provenance(),
            observation=GitObjectAvailabilityObservation(
                object_format="sha1",
                objects=(
                    GitObjectAvailability(object_oid=CURRENT, status="present"),
                    GitObjectAvailability(object_oid=SOURCE, status="present"),
                ),
            ),
        ),
        GitObservationDraft(
            checkout_id="checkout_001",
            sequence=10,
            observed_at="2026-08-02T03:00:00Z",
            provenance=_provenance(),
            observation=GitAncestryObservation(
                object_format="sha1",
                current_oid=CURRENT,
                relations=(
                    GitAncestryRelation(anchor_oid=SOURCE, status="not_ancestor"),
                ),
            ),
        ),
        GitObservationDraft(
            checkout_id="checkout_001",
            sequence=11,
            observed_at="2026-08-02T03:00:00Z",
            provenance=_provenance(),
            observation=GitShallowStateObservation(state="full"),
        ),
    )
    contradiction, _ = build_git_observation_batch(
        contradiction_drafts,
        access=_access(),
        expected_stream_version=8,
        next_global_position=9,
        previous_event=unknown_events[-1],
        recorded_at="2026-08-02T03:00:01Z",
    )

    with pytest.raises(GitGraphV1Error) as conflict:
        reduce_git_graph_events(first + unknown_events + contradiction, access=_access())
    assert conflict.value.code == "TBM_GIT_GRAPH_ANCESTRY_CONFLICT"


def test_capture_request_segments_cannot_be_reopened():
    first = _events()
    middle_draft = GitObservationDraft(
        checkout_id="checkout_001",
        sequence=8,
        observed_at="2026-08-02T02:00:00Z",
        provenance=_provenance(),
        observation=GitShallowStateObservation(state="full"),
    )
    middle, _ = build_git_observation_batch(
        (middle_draft,),
        access=_access(),
        expected_stream_version=7,
        next_global_position=8,
        previous_event=first[-1],
        recorded_at="2026-08-02T02:00:01Z",
    )
    tail_draft = replace(
        middle_draft,
        sequence=9,
        observed_at="2026-08-02T03:00:00Z",
    )
    tail, _ = build_git_observation_batch(
        (tail_draft,),
        access=_access(),
        expected_stream_version=8,
        next_global_position=9,
        previous_event=middle[-1],
        recorded_at="2026-08-02T03:00:01Z",
    )
    reopened = _clone_event(tail[0], request_sha256=first[0].request_sha256)

    with pytest.raises(GitGraphV1Error) as conflict:
        reduce_git_graph_events(first + middle + (reopened,), access=_access())
    assert conflict.value.code == "TBM_GIT_GRAPH_CAPTURE_COMMAND_INVALID"


def test_replay_algorithm_is_not_caller_injectable_and_pr_contract_is_strict():
    parameters = inspect.signature(reduce_git_graph_events).parameters
    assert "reducer" not in parameters
    assert "event_registry" not in parameters

    projection = reduce_git_graph_events(
        _events(),
        access=_access(),
        pr_case_provenance=(_pr_case(),),
    )
    assert projection is not None
    with pytest.raises(GitGraphV1Error) as provenance:
        replace(projection.pr_anchors[0], last_observation=None)
    assert provenance.value.code == "TBM_GIT_GRAPH_PROJECTION_INVALID"

    mismatched_anchor = replace(
        projection.pr_anchors[0],
        current_oid=OTHER,
    )
    with pytest.raises(GitGraphV1Error) as current:
        replace(projection, pr_anchors=(mismatched_anchor,))
    assert current.value.code == "TBM_GIT_GRAPH_PROJECTION_INVALID"

    unknown_projection = reduce_git_graph_events(
        _events(source_availability="missing"),
        access=_access(),
        pr_case_provenance=(_pr_case(),),
    )
    assert unknown_projection is not None
    fabricated_anchor = replace(
        unknown_projection.pr_anchors[0],
        status="ancestor",
        confidence="locally_observed",
        last_observation=unknown_projection.last_observation,
    )
    with pytest.raises(GitGraphV1Error) as ancestry_binding:
        replace(unknown_projection, pr_anchors=(fabricated_anchor,))
    assert ancestry_binding.value.code == "TBM_GIT_GRAPH_PROJECTION_INVALID"


def test_parent_confidence_degrades_without_explicit_object_availability():
    drafts = _drafts()
    availability_index = next(
        index
        for index, draft in enumerate(drafts)
        if type(draft.observation) is GitObjectAvailabilityObservation
    )
    availability = drafts[availability_index].observation
    assert type(availability) is GitObjectAvailabilityObservation
    without_parent = replace(
        drafts[availability_index],
        observation=replace(
            availability,
            objects=tuple(
                item for item in availability.objects if item.object_oid != PARENT
            ),
        ),
    )
    drafts = drafts[:availability_index] + (without_parent,) + drafts[availability_index + 1 :]
    events, _ = build_git_observation_batch(
        drafts,
        access=_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    projection = reduce_git_graph_events(events, access=_access())

    assert projection is not None
    assert projection.parent_relations[0].confidence == "degraded"
    assert projection.parent_relations[0].last_validated_at is None


def test_evidence_and_pr_anchor_inputs_fail_closed_on_mismatched_links():
    _, regression = _evidence()
    with pytest.raises(GitGraphV1Error) as missing_fix:
        reduce_git_graph_events(
            _events(),
            access=_access(),
            regression_evidence=(regression,),
        )
    assert missing_fix.value.code == "TBM_GIT_GRAPH_EVIDENCE_MISMATCH"

    with pytest.raises(GitGraphV1Error) as duplicate_case:
        reduce_git_graph_events(
            _events(),
            access=_access(),
            pr_case_provenance=(_pr_case(), _pr_case()),
        )
    assert duplicate_case.value.code == "TBM_GIT_GRAPH_PR_ANCHOR_INVALID"

    with pytest.raises(GitGraphV1Error) as short_oid:
        reduce_git_graph_events(
            _events(),
            access=_access(),
            pr_case_provenance=(_pr_case(commit_sha="abc123"),),
        )
    assert short_oid.value.code == "TBM_GIT_GRAPH_OID_INVALID"


def test_root_exports_are_intentional():
    assert tbm.GitGraphProjection.__name__ == "GitGraphProjection"
    assert tbm.GIT_GRAPH_PROTOCOL_VERSION == "tbm.git-graph.v1"
    assert tbm.reduce_git_graph_events is reduce_git_graph_events
