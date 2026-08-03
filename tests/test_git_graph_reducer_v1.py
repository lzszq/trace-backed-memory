from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import cast

import pytest

import trace_backed_memory as tbm
from trace_backed_memory import daemon_entry
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventSource,
    EventTrustedContext,
)
from trace_backed_memory.git_observation_v1 import (
    GitObservationDetails,
    GitObservationRecordRef,
    build_git_observation_batch,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from trace_backed_memory.reducer import (
    ReducerEvent,
    ReducerExecutionError,
    canonical_projection_state,
    execute_reducer_step,
    projection_state_sha256,
)
from trace_backed_memory.reducer_registry import DEFAULT_REDUCER_REGISTRY
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40
COMMIT_D = "d" * 40


def _context(*, tenant_id: str = "tenant_001") -> EventTrustedContext:
    return EventTrustedContext(
        organization_id="organization_001",
        tenant_id=tenant_id,
        repository_id="repository_001",
        environment_id="environment_local",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id="authorization_decision_001",
    )


def _ledger_access() -> LedgerAccessContext:
    context = _context()
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id=context.organization_id,
            tenant_id=context.tenant_id,
            repository_id=context.repository_id,
            environment_id=context.environment_id,
        ),
        principal_id=context.principal_id,
        agent_client_id=context.agent_client_id,
        actor_type=context.actor_type,
        actor_id=context.actor_id,
        authorization_decision_id=context.authorization_decision_id,
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _source(
    source_record_id: str,
    *,
    observed_at: str = "2026-08-03T01:00:01Z",
    evidence_quality: str = "exact",
) -> EventSource:
    return EventSource(
        source_system="git_cli",
        source_record_id=source_record_id,
        evidence_quality=evidence_quality,
        observed_at=observed_at,
    )


def _artifact() -> EventArtifactRef:
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + "d" * 64,
        content_sha256="sha256:" + "d" * 64,
        media_type="text/x-diff",
        size_bytes=256,
        classification="internal",
        retention_policy_id="retention_engineering_memory",
        encryption_key_id=None,
        availability="available",
    )


def _records(
    details: tuple[GitObservationDetails, ...],
    *,
    trace_id: str = "trace_git_001",
    run_id: str = "run_git_001",
    checkout_alias: str = "checkout_local_001",
    source: EventSource | None = None,
    first_sequence: int = 1,
) -> tuple[GitObservationRecordRef, ...]:
    event_source = source or _source(f"source_{run_id}")
    records = []
    for offset, item in enumerate(details):
        artifact_refs = () if item.diff_artifact_id is None else (_artifact(),)
        records.append(
            GitObservationRecordRef(
                trace_id=trace_id,
                run_id=run_id,
                repository_id="repository_001",
                checkout_alias=checkout_alias,
                sequence=first_sequence + offset,
                occurred_at="2026-08-03T01:00:01Z",
                authorization_event_id="authorization_decision_001",
                source=event_source,
                details=item,
                artifact_refs=artifact_refs,
                classification="internal",
                retention_policy_id="retention_engineering_memory",
                git_version="2.43.0",
                runner_name="git_cli_runner",
                runner_version="1.0.0",
                algorithm_name="git_observation_capture",
                algorithm_version="1.0.0",
            )
        )
    return tuple(records)


def _events(
    details: tuple[GitObservationDetails, ...],
    *,
    first_global_position: int = 1,
    parent_event: CanonicalEvent | None = None,
    trace_id: str = "trace_git_001",
    run_id: str = "run_git_001",
    checkout_alias: str = "checkout_local_001",
    source: EventSource | None = None,
    context: EventTrustedContext | None = None,
    first_sequence: int = 1,
) -> tuple[CanonicalEvent, ...]:
    return build_git_observation_batch(
        _records(
            details,
            trace_id=trace_id,
            run_id=run_id,
            checkout_alias=checkout_alias,
            source=source,
            first_sequence=first_sequence,
        ),
        parent_event=parent_event,
        first_global_position=first_global_position,
        trusted_context=context or _context(),
        recorded_at="2026-08-03T01:00:10Z",
    )


def _reduce(
    events: tuple[CanonicalEvent, ...],
    *,
    state: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    reducer = tbm.build_git_graph_reducer()
    current = reducer.initial_state() if state is None else state
    for event in events:
        current = execute_reducer_step(
            reducer,
            current,
            ReducerEvent(
                event,
                DEFAULT_EVENT_TYPE_REGISTRY.consume(event, target_version=1),
            ),
        ).state
    return current


def _complete_observation_batch() -> tuple[CanonicalEvent, ...]:
    artifact_id = _artifact().artifact_id
    return _events(
        (
            GitObservationDetails(
                "checkout",
                commit_sha=COMMIT_B,
                ref_name="main",
                detached=False,
                remote_sha256="sha256:" + "e" * 64,
            ),
            GitObservationDetails("commit", commit_sha=COMMIT_B),
            GitObservationDetails(
                "ref",
                commit_sha=COMMIT_B,
                ref_name="main",
                detached=False,
            ),
            GitObservationDetails(
                "worktree_status",
                commit_sha=COMMIT_B,
                ref_name="main",
                detached=False,
                dirty=True,
                diff_artifact_id=artifact_id,
            ),
            GitObservationDetails(
                "diff",
                commit_sha=COMMIT_B,
                base_commit_sha=COMMIT_A,
                diff_artifact_id=artifact_id,
            ),
            GitObservationDetails(
                "ancestry",
                ancestor_sha=COMMIT_A,
                descendant_sha=COMMIT_B,
                relation="ancestor",
            ),
            GitObservationDetails(
                "object_availability",
                object_sha=COMMIT_C,
                object_type="commit",
                availability="unavailable",
                unavailable_reason="missing",
            ),
            GitObservationDetails(
                "shallow_state",
                shallow=True,
                boundary_commit_shas=(COMMIT_A,),
            ),
        )
    )


def test_git_graph_reducer_builds_conservative_projection() -> None:
    events = _complete_observation_batch()
    state = _reduce(events)
    graph = tbm.projected_git_graph(state, "repository_001")

    descriptor = tbm.build_git_graph_reducer().descriptor
    assert descriptor.reducer_id == tbm.GIT_GRAPH_REDUCER_ID
    assert descriptor.input_event_types == tuple(sorted(tbm.GIT_OBSERVATION_TYPES))
    assert DEFAULT_REDUCER_REGISTRY.resolve(
        tbm.GIT_GRAPH_REDUCER_ID,
        1,
    ).descriptor == descriptor
    assert graph["repository_id"] == "repository_001"
    assert set(graph["commits"]) == {COMMIT_A, COMMIT_B, COMMIT_C}
    relation = graph["relations"][f"{COMMIT_A}:{COMMIT_B}"]
    assert relation["effective_relation"] == "ancestor"
    assert relation["relation_confidence"] == "exact"
    assert relation["conflicted"] is False
    assert graph["missing_objects"][f"commit:{COMMIT_C}"]["availability"] == (
        "unavailable"
    )
    checkout = graph["checkouts"]["checkout_local_001"]
    assert checkout["commit_sha"] == COMMIT_B
    assert checkout["dirty"] is True
    assert checkout["diff_artifact_id"] == _artifact().artifact_id
    assert checkout["shallow"] is True
    assert checkout["boundary_commit_shas"] == [COMMIT_A]
    assert checkout["refs"]["main"]["current_commit_sha"] == COMMIT_B
    assert graph["direct_parent_edges"] == []
    assert graph["source_fix_verification_relationships"] == []
    assert graph["pr_anchors"] == []
    assert graph["last_observation"]["global_position"] == 8
    assert {
        "GIT_GRAPH_REDUCER_ID",
        "build_git_graph_reducer",
        "projected_git_graph",
    } <= set(tbm.__all__)


def test_git_graph_relation_conflict_stays_unknown_and_uses_global_order() -> None:
    ancestor = _events(
        (
            GitObservationDetails(
                "ancestry",
                ancestor_sha=COMMIT_A,
                descendant_sha=COMMIT_B,
                relation="ancestor",
            ),
        ),
        source=_source(
            "source_later_clock",
            observed_at="2026-08-03T01:00:05Z",
            evidence_quality="verified",
        ),
    )
    not_ancestor = _events(
        (
            GitObservationDetails(
                "ancestry",
                ancestor_sha=COMMIT_A,
                descendant_sha=COMMIT_B,
                relation="not_ancestor",
            ),
        ),
        first_global_position=2,
        trace_id="trace_git_002",
        run_id="run_git_002",
        checkout_alias="checkout_local_002",
        source=_source(
            "source_earlier_clock",
            observed_at="2026-08-03T00:00:00Z",
            evidence_quality="exact",
        ),
    )

    state = _reduce(ancestor + not_ancestor)
    graph = tbm.projected_git_graph(state, "repository_001")
    relation = graph["relations"][f"{COMMIT_A}:{COMMIT_B}"]

    assert relation["assertion_counts"] == {
        "ancestor": 1,
        "not_ancestor": 1,
        "unknown": 0,
    }
    assert relation["effective_relation"] == "unknown"
    assert relation["relation_confidence"] == "unknown"
    assert relation["conflicted"] is True
    assert relation["last_observation"]["global_position"] == 2
    assert relation["last_observation"]["observed_at"] == "2026-08-03T00:00:00Z"
    assert graph["direct_parent_edges"] == []


@pytest.mark.parametrize("later_relation", ["ancestor", "not_ancestor"])
def test_git_graph_unknown_relation_is_monotonic(later_relation: str) -> None:
    events = _events(
        (
            GitObservationDetails(
                "ancestry",
                ancestor_sha=COMMIT_A,
                descendant_sha=COMMIT_B,
                relation="unknown",
            ),
            GitObservationDetails(
                "ancestry",
                ancestor_sha=COMMIT_A,
                descendant_sha=COMMIT_B,
                relation=later_relation,  # type: ignore[arg-type]
            ),
        )
    )

    graph = tbm.projected_git_graph(_reduce(events), "repository_001")
    relation = graph["relations"][f"{COMMIT_A}:{COMMIT_B}"]

    assert relation["effective_relation"] == "unknown"
    assert relation["relation_confidence"] == "unknown"
    assert relation["assertion_counts"]["unknown"] == 1
    assert relation["assertion_counts"][later_relation] == 1


def test_git_graph_ref_movement_is_not_claimed_as_force_push() -> None:
    events = _events(
        (
            GitObservationDetails(
                "ref",
                commit_sha=COMMIT_B,
                ref_name="main",
                detached=False,
            ),
            GitObservationDetails(
                "ref",
                commit_sha=COMMIT_C,
                ref_name="main",
                detached=False,
            ),
        )
    )

    graph = tbm.projected_git_graph(_reduce(events), "repository_001")
    ref = graph["checkouts"]["checkout_local_001"]["refs"]["main"]

    assert ref["current_commit_sha"] == COMMIT_C
    assert ref["observed_commit_shas"] == [COMMIT_B, COMMIT_C]
    assert ref["moved"] is True
    assert ref["movement_classification"] == "unknown"
    assert "force" not in str(ref).lower()


def test_git_graph_missing_object_tracks_latest_availability_without_false_relation() -> (
    None
):
    events = _events(
        (
            GitObservationDetails(
                "object_availability",
                object_sha=COMMIT_A,
                object_type="commit",
                availability="available",
            ),
            GitObservationDetails(
                "object_availability",
                object_sha=COMMIT_A,
                object_type="commit",
                availability="unknown",
                unavailable_reason="capture_failed",
            ),
            GitObservationDetails(
                "object_availability",
                object_sha=COMMIT_A,
                object_type="commit",
                availability="available",
            ),
        )
    )
    unknown_state = _reduce(events[:2])
    unknown_graph = tbm.projected_git_graph(unknown_state, "repository_001")
    missing = unknown_graph["missing_objects"][f"commit:{COMMIT_A}"]
    assert missing["availability"] == "unknown"
    assert missing["unavailable_reason"] == "capture_failed"
    assert unknown_graph["relations"] == {}

    recovered_state = _reduce(events[2:], state=unknown_state)
    recovered_graph = tbm.projected_git_graph(recovered_state, "repository_001")
    assert recovered_graph["missing_objects"] == {}
    object_state = recovered_graph["objects"][f"commit:{COMMIT_A}"]
    assert object_state["availability"] == "available"
    assert object_state["assertion_counts"] == {
        "available": 2,
        "unavailable": 0,
        "unknown": 1,
    }


def test_git_graph_reducer_rejects_missing_parent_global_replay_and_scope_mix() -> (
    None
):
    events = _events(
        (
            GitObservationDetails("commit", commit_sha=COMMIT_A),
            GitObservationDetails("commit", commit_sha=COMMIT_B),
        )
    )
    with pytest.raises(ReducerExecutionError):
        _reduce(events[1:])

    first_state = _reduce(events[:1])
    with pytest.raises(ReducerExecutionError):
        _reduce(events[:1], state=first_state)

    other_scope = _events(
        (GitObservationDetails("commit", commit_sha=COMMIT_C),),
        first_global_position=2,
        trace_id="trace_git_other",
        run_id="run_git_other",
        checkout_alias="checkout_other",
        context=_context(tenant_id="tenant_002"),
    )
    with pytest.raises(ReducerExecutionError):
        _reduce(other_scope, state=first_state)


def test_git_graph_reducer_rejects_corrupt_retained_state_fail_closed() -> None:
    events = _complete_observation_batch()
    retained = _reduce(events[:-1])
    corrupt_states: list[dict[str, object]] = []

    corrupt_scope = canonical_projection_state(retained)
    cast(dict[str, object], corrupt_scope["scope"])["extra"] = True
    corrupt_states.append(corrupt_scope)

    missing_scope = canonical_projection_state(retained)
    missing_scope["scope"] = None
    corrupt_states.append(missing_scope)

    corrupt_head = canonical_projection_state(retained)
    cast(dict[str, object], corrupt_head["heads"])["junk"] = {"x": 1}
    corrupt_states.append(corrupt_head)

    corrupt_repository_shape = canonical_projection_state(retained)
    cast(dict[str, object], corrupt_repository_shape["repository"])[
        "relations"
    ] = []
    corrupt_states.append(corrupt_repository_shape)

    corrupt_commit = canonical_projection_state(retained)
    repository = cast(dict[str, object], corrupt_commit["repository"])
    commits = cast(dict[str, object], repository["commits"])
    commits[COMMIT_A] = {"oops": 1}
    corrupt_states.append(corrupt_commit)

    corrupt_root = canonical_projection_state(retained)
    corrupt_root["extra"] = True
    corrupt_states.append(corrupt_root)

    for corrupt_state in corrupt_states:
        with pytest.raises(ReducerExecutionError) as raised:
            _reduce(events[-1:], state=corrupt_state)
        assert raised.value.code == "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID"


def test_projected_git_graph_rejects_corrupt_state() -> None:
    state = canonical_projection_state(_reduce(_complete_observation_batch()))
    cast(dict[str, object], state["scope"])["extra"] = True

    with pytest.raises(ReducerExecutionError) as raised:
        tbm.projected_git_graph(state, "repository_001")
    assert raised.value.code == "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID"


def test_projected_git_graph_rejects_cross_field_corruption() -> None:
    retained = _reduce(_complete_observation_batch())
    corrupt_states: list[dict[str, object]] = []

    mismatched_latest_head = canonical_projection_state(retained)
    heads = cast(dict[str, object], mismatched_latest_head["heads"])
    head = cast(dict[str, object], next(iter(heads.values())))
    head["event_id"] = "evt_tampered_head"
    corrupt_states.append(mismatched_latest_head)

    missing_relation_commit = canonical_projection_state(retained)
    repository = cast(dict[str, object], missing_relation_commit["repository"])
    cast(dict[str, object], repository["commits"]).pop(COMMIT_A)
    corrupt_states.append(missing_relation_commit)

    missing_object_commit = canonical_projection_state(retained)
    repository = cast(dict[str, object], missing_object_commit["repository"])
    cast(dict[str, object], repository["commits"]).pop(COMMIT_C)
    corrupt_states.append(missing_object_commit)

    orphan_checkout = canonical_projection_state(retained)
    repository = cast(dict[str, object], orphan_checkout["repository"])
    checkout = cast(
        dict[str, object],
        cast(dict[str, object], repository["checkouts"])["checkout_local_001"],
    )
    checkout["commit_sha"] = COMMIT_D
    corrupt_states.append(orphan_checkout)

    orphan_ref = canonical_projection_state(retained)
    repository = cast(dict[str, object], orphan_ref["repository"])
    checkout = cast(
        dict[str, object],
        cast(dict[str, object], repository["checkouts"])["checkout_local_001"],
    )
    retained_ref = cast(dict[str, object], cast(dict[str, object], checkout["refs"])["main"])
    retained_ref["current_commit_sha"] = COMMIT_D
    retained_ref["observed_commit_shas"] = [COMMIT_B, COMMIT_D]
    retained_ref["moved"] = True
    retained_ref["movement_classification"] = "unknown"
    corrupt_states.append(orphan_ref)

    orphan_boundary = canonical_projection_state(retained)
    repository = cast(dict[str, object], orphan_boundary["repository"])
    checkout = cast(
        dict[str, object],
        cast(dict[str, object], repository["checkouts"])["checkout_local_001"],
    )
    checkout["boundary_commit_shas"] = [COMMIT_D]
    corrupt_states.append(orphan_boundary)

    oversized_sequence = canonical_projection_state(retained)
    heads = cast(dict[str, object], oversized_sequence["heads"])
    cast(dict[str, object], next(iter(heads.values())))["stream_version"] = 1_000_000_001
    corrupt_states.append(oversized_sequence)

    for corrupt_state in corrupt_states:
        with pytest.raises(ReducerExecutionError) as raised:
            tbm.projected_git_graph(corrupt_state, "repository_001")
        assert raised.value.code == "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID"


@pytest.mark.parametrize("malformed", [2**63, -(2**63) - 1])
def test_projected_git_graph_bounds_direct_input(malformed: int) -> None:
    state = canonical_projection_state(_reduce(_complete_observation_batch()))
    state["last_global_position"] = malformed

    with pytest.raises(ReducerExecutionError) as raised:
        tbm.projected_git_graph(state, "repository_001")
    assert raised.value.code == "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID"


def test_projected_git_graph_rejects_deep_input_with_stable_error() -> None:
    state: dict[str, object] = {}
    cursor = state
    for _ in range(40):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    with pytest.raises(ReducerExecutionError) as raised:
        tbm.projected_git_graph(state, "repository_001")
    assert raised.value.code == "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID"


def test_git_graph_projection_digest_is_repeatable() -> None:
    events = _complete_observation_batch()
    first = _reduce(events)
    second = _reduce(events)

    assert first == second
    assert projection_state_sha256(
        tbm.GIT_GRAPH_PROJECTION_NAME,
        tbm.GIT_GRAPH_PROJECTION_SCHEMA_VERSION,
        first,
    ) == projection_state_sha256(
        tbm.GIT_GRAPH_PROJECTION_NAME,
        tbm.GIT_GRAPH_PROJECTION_SCHEMA_VERSION,
        second,
    )


def test_default_operator_cli_rebuilds_git_graph_projection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "event-ledger.sqlite3"
    events = _complete_observation_batch()
    first = events[0]
    with SQLiteEventLedgerV1.connect(
        database,
        _ledger_access(),
        initialize=True,
    ) as ledger:
        ledger.append(
            first.stream_id,
            0,
            events,
            LedgerIdempotency(
                first.idempotency_key_sha256,
                first.request_sha256,
            ),
        )

    exit_code = daemon_entry.main(
        [
            "projection",
            "rebuild",
            tbm.GIT_GRAPH_REDUCER_ID,
            "--database",
            str(database),
            "--generation",
            "1",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["operation"] == "projection.rebuild"
    assert output["status"] == "completed"
    assert output["processed_events"] == len(events)
    assert output["checkpoint"]["reducer_id"] == tbm.GIT_GRAPH_REDUCER_ID
    assert output["checkpoint"]["projection_name"] == tbm.GIT_GRAPH_PROJECTION_NAME
    assert output["checkpoint"]["global_position"] == len(events)
