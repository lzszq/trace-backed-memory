from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import cast, get_type_hints

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.capture import (
    CommitAncestryCaptureError,
    capture_commit_ancestry,
    capture_trace_metadata,
)
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventSource,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.git_observation_v1 import (
    GIT_CHECKOUT_OBSERVED,
    GIT_COMMIT_OBSERVED,
    GIT_COMMIT_RELATION_OBSERVED,
    GIT_DIFF_CAPTURED,
    GIT_OBJECT_AVAILABILITY_OBSERVED,
    GIT_REF_OBSERVED,
    GIT_SHALLOW_STATE_OBSERVED,
    GIT_WORKTREE_STATUS_OBSERVED,
    GitObservationDetails,
    GitObservationEventRecorder,
    GitObservationRecorder,
    GitObservationRecordRef,
    GitObservationV1Error,
    append_git_observation_batch,
    build_git_observation,
    build_git_observation_batch,
    git_observation_event_id,
    git_observation_stream_id,
    parse_git_observation,
    verify_git_observation_batch,
    verify_git_observation_parent,
)
from trace_backed_memory.ledger_port_v1 import (
    EventLedgerAtomicAppendPort,
    LedgerAccessContext,
    LedgerAppendCommit,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from trace_backed_memory.models import CommitAncestryEvidence
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from tests.postgres_support import PostgresCluster


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_INSTALL = ROOT / "schemas" / "postgres-v3-event-ledger.sql"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40


class _PostCommitResponseLossLedger:
    def __init__(
        self,
        delegate: SQLiteEventLedgerV1,
        *,
        fail_on_call: int,
    ) -> None:
        self.delegate = delegate
        self.fail_on_call = fail_on_call
        self.call_count = 0

    @property
    def access_context(self) -> LedgerAccessContext:
        return self.delegate.access_context

    def append_once(
        self,
        stream_id: str,
        expected_version: int,
        events: tuple[CanonicalEvent, ...],
        idempotency: LedgerIdempotency,
    ) -> LedgerAppendCommit:
        self.call_count += 1
        commit = self.delegate.append_once(
            stream_id,
            expected_version,
            events,
            idempotency,
        )
        if self.call_count == self.fail_on_call:
            raise RuntimeError("simulated response loss after commit")
        return commit


def _access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_local",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id="authorization_decision_001",
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _source() -> EventSource:
    return EventSource(
        source_system="git_cli",
        source_record_id="git_capture_001",
        evidence_quality="exact",
        observed_at="2026-08-03T01:00:01Z",
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


def _details() -> tuple[GitObservationDetails, ...]:
    artifact_id = _artifact().artifact_id
    return (
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


def _record(
    sequence: int,
    details: GitObservationDetails,
) -> GitObservationRecordRef:
    artifact_refs = () if details.diff_artifact_id is None else (_artifact(),)
    return GitObservationRecordRef(
        trace_id="trace_git_001",
        run_id="run_git_001",
        repository_id="repository_001",
        checkout_alias="checkout_local_001",
        sequence=sequence,
        occurred_at="2026-08-03T01:00:01Z",
        authorization_event_id="authorization_decision_001",
        source=_source(),
        details=details,
        artifact_refs=artifact_refs,
        classification="internal",
        retention_policy_id="retention_engineering_memory",
        git_version="2.43.0",
        runner_name="git_cli_runner",
        runner_version="1.0.0",
        algorithm_name="git_observation_capture",
        algorithm_version="1.0.0",
    )


def _batch() -> tuple[CanonicalEvent, ...]:
    records = tuple(
        _record(sequence, details)
        for sequence, details in enumerate(_details(), start=1)
    )
    return build_git_observation_batch(
        records,
        parent_event=None,
        first_global_position=1,
        trusted_context=_access().event_trusted_context(),
        recorded_at="2026-08-03T01:00:10Z",
    )


def _continued_batch(parent_event: CanonicalEvent) -> tuple[CanonicalEvent, ...]:
    details = (
        GitObservationDetails(
            "object_availability",
            object_sha=COMMIT_A,
            object_type="commit",
            availability="available",
        ),
        GitObservationDetails(
            "ancestry",
            ancestor_sha=COMMIT_B,
            descendant_sha=COMMIT_C,
            relation="not_ancestor",
        ),
    )
    records = tuple(
        _record(sequence, item) for sequence, item in enumerate(details, start=9)
    )
    return build_git_observation_batch(
        records,
        parent_event=parent_event,
        first_global_position=9,
        trusted_context=_access().event_trusted_context(),
        recorded_at="2026-08-03T01:00:20Z",
    )


def _clone_event(
    event: CanonicalEvent,
    *,
    payload: dict[str, object] | None = None,
    request_sha256: str | None = None,
) -> CanonicalEvent:
    trusted_context = EventTrustedContext(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
        principal_id=event.principal_id,
        agent_client_id=event.agent_client_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        authorization_decision_id=event.authorization_decision_id,
    )
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
        trusted_context=trusted_context,
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=(
            event.request_sha256 if request_sha256 is None else request_sha256
        ),
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
        payload=dict(event.payload) if payload is None else payload,
    )


def test_git_observation_batch_round_trips_all_required_evidence() -> None:
    events = _batch()

    assert tuple(event.event_type for event in events) == (
        GIT_CHECKOUT_OBSERVED,
        GIT_COMMIT_OBSERVED,
        GIT_REF_OBSERVED,
        GIT_WORKTREE_STATUS_OBSERVED,
        GIT_DIFF_CAPTURED,
        GIT_COMMIT_RELATION_OBSERVED,
        GIT_OBJECT_AVAILABILITY_OBSERVED,
        GIT_SHALLOW_STATE_OBSERVED,
    )
    assert tuple(event.stream_version for event in events) == tuple(range(1, 9))
    assert len({event.idempotency_key_sha256 for event in events}) == 1
    assert len({event.request_sha256 for event in events}) == 1
    parsed = tuple(parse_git_observation(event) for event in events)
    assert tuple(item.details for item in parsed) == _details()
    assert all(item.git_version == "2.43.0" for item in parsed)
    assert all(item.runner_version == "1.0.0" for item in parsed)
    assert all(item.algorithm_version == "1.0.0" for item in parsed)
    verify_git_observation_batch(events, parent_event=None)
    typed = tuple(DEFAULT_EVENT_TYPE_REGISTRY.consume(event) for event in events)
    assert all(item.source_event.event_kind == "observation" for item in typed)


def test_git_observation_diff_bytes_remain_out_of_ledger_payload() -> None:
    events = _batch()
    for event in events:
        payload_json = json.dumps(event.to_dict()["payload"], sort_keys=True)
        assert _artifact().content_sha256 not in payload_json
        assert "diff --git" not in payload_json
    diff = parse_git_observation(events[4])
    assert diff.artifact_refs == (_artifact(),)
    assert diff.details.diff_artifact_id == _artifact().artifact_id


def test_git_observation_batches_continue_and_replay_exactly_in_sqlite() -> None:
    first_batch = _batch()
    second_batch = _continued_batch(first_batch[-1])
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        first = append_git_observation_batch(
            ledger,
            first_batch,
            parent_event=None,
        )
        first_replay = append_git_observation_batch(
            ledger,
            first_batch,
            parent_event=None,
        )
        second = append_git_observation_batch(
            ledger,
            second_batch,
            parent_event=first_batch[-1],
        )
        second_replay = append_git_observation_batch(
            ledger,
            second_batch,
            parent_event=first_batch[-1],
        )

        assert first.inserted is True
        assert first_replay.replayed is True
        assert first_replay.receipt == first.receipt
        assert second.inserted is True
        assert second_replay.replayed is True
        assert second_replay.receipt == second.receipt
        retained = ledger.read_stream(first_batch[0].stream_id, limit=100).events
        assert retained == first_batch + second_batch
        verify_git_observation_batch(first_batch, parent_event=None)
        verify_git_observation_batch(second_batch, parent_event=first_batch[-1])
        assert ledger.verify_stream(first_batch[0].stream_id).valid


def test_git_observation_batch_matches_postgres_event_ledger(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    installed = postgres_cluster.run_script(POSTGRES_INSTALL)
    assert installed.returncode == 0, installed.stderr
    first_batch = _batch()
    second_batch = _continued_batch(first_batch[-1])
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as sqlite_ledger:
        sqlite_commits = (
            append_git_observation_batch(
                sqlite_ledger,
                first_batch,
                parent_event=None,
            ),
            append_git_observation_batch(
                sqlite_ledger,
                second_batch,
                parent_event=first_batch[-1],
            ),
        )
        sqlite_stream = sqlite_ledger.read_stream(
            first_batch[0].stream_id,
            limit=100,
        )
        sqlite_global = sqlite_ledger.read_global(limit=100)
    with PostgresEventLedgerV1.connect(
        _access(),
        **postgres_cluster.connection_kwargs(),
    ) as postgres_ledger:
        postgres_commits = (
            append_git_observation_batch(
                postgres_ledger,
                first_batch,
                parent_event=None,
            ),
            append_git_observation_batch(
                postgres_ledger,
                second_batch,
                parent_event=first_batch[-1],
            ),
        )
        assert postgres_commits == sqlite_commits
        assert (
            postgres_ledger.read_stream(first_batch[0].stream_id, limit=100)
            == sqlite_stream
        )
        assert postgres_ledger.read_global(limit=100) == sqlite_global


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "observation_kind": "checkout",
            "commit_sha": COMMIT_A,
            "ref_name": None,
            "detached": False,
        },
        {
            "observation_kind": "ref",
            "commit_sha": COMMIT_A,
            "ref_name": "main",
            "detached": True,
        },
        {"observation_kind": "diff", "commit_sha": COMMIT_A},
        {
            "observation_kind": "object_availability",
            "object_sha": COMMIT_A,
            "object_type": "commit",
            "availability": "available",
            "unavailable_reason": "missing",
        },
        {
            "observation_kind": "object_availability",
            "object_sha": COMMIT_A,
            "object_type": "commit",
            "availability": "unknown",
        },
        {
            "observation_kind": "shallow_state",
            "shallow": False,
            "boundary_commit_shas": (COMMIT_A,),
        },
        {
            "observation_kind": "ancestry",
            "ancestor_sha": COMMIT_A,
            "descendant_sha": COMMIT_A,
            "relation": "not_ancestor",
        },
        {"observation_kind": "commit", "commit_sha": "abc123"},
    ],
)
def test_git_observation_details_reject_inconsistent_or_ambiguous_evidence(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(GitObservationV1Error):
        GitObservationDetails(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "ref_name",
    [
        "/absolute/secret",
        "https://user:pass@example.test/repository",
        "stdout dump: secret",
        "refs//heads/main",
        "refs/heads/.hidden",
        "refs/heads/main.lock",
        "refs/heads/main..backup",
        "refs/heads/main@{1}",
    ],
)
def test_git_observation_rejects_ref_names_that_can_carry_sensitive_text(
    ref_name: str,
) -> None:
    with pytest.raises(GitObservationV1Error):
        GitObservationDetails(
            "ref",
            commit_sha=COMMIT_A,
            ref_name=ref_name,
            detached=False,
        )


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("git_version", "https://example.test/version"),
        ("runner_version", "/workspace/runner"),
        ("algorithm_version", "stdout dump: secret"),
    ],
)
def test_git_observation_rejects_version_fields_that_can_carry_sensitive_text(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(GitObservationV1Error):
        replace(_record(1, GitObservationDetails("commit", commit_sha=COMMIT_A)), **{field_name: value})


def test_git_observation_record_requires_exact_diff_artifact_link() -> None:
    details = GitObservationDetails(
        "diff",
        commit_sha=COMMIT_B,
        diff_artifact_id=_artifact().artifact_id,
    )
    with pytest.raises(GitObservationV1Error):
        replace(_record(1, details), artifact_refs=())
    with pytest.raises(GitObservationV1Error):
        replace(
            _record(1, GitObservationDetails("commit", commit_sha=COMMIT_B)),
            artifact_refs=(_artifact(),),
        )


def test_git_observation_verifier_rejects_tampered_command_and_batch() -> None:
    events = _batch()
    tampered_command = (
        _clone_event(events[0], request_sha256="sha256:" + "f" * 64),
        *events[1:],
    )
    with pytest.raises(GitObservationV1Error):
        verify_git_observation_batch(tampered_command, parent_event=None)

    payload = dict(events[0].payload)
    payload["batch_size"] = 7
    with pytest.raises(GitObservationV1Error):
        parse_git_observation(_clone_event(events[0], payload=payload))

    singleton = build_git_observation(
        _record(1, GitObservationDetails("commit", commit_sha=COMMIT_A)),
        parent_event=None,
        global_position=1,
        trusted_context=_access().event_trusted_context(),
        recorded_at="2026-08-03T01:00:10Z",
    )
    with pytest.raises(GitObservationV1Error):
        parse_git_observation(
            _clone_event(singleton, request_sha256="sha256:" + "e" * 64)
        )


def test_git_observation_identity_is_partition_scoped() -> None:
    trusted = _access().event_trusted_context()
    other_partition = replace(trusted, tenant_id="tenant_002")
    stream = git_observation_stream_id(
        "repository_001",
        "trace_git_001",
        "run_git_001",
        "checkout_local_001",
        trusted,
    )
    other_stream = git_observation_stream_id(
        "repository_001",
        "trace_git_001",
        "run_git_001",
        "checkout_local_001",
        other_partition,
    )

    assert stream != other_stream
    assert git_observation_event_id(stream, 1, trusted) != git_observation_event_id(
        other_stream,
        1,
        other_partition,
    )


def test_typed_git_append_rejects_truncated_batch_and_wrong_context_before_write() -> (
    None
):
    events = _batch()
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(GitObservationV1Error):
            append_git_observation_batch(
                ledger,
                events[:-1],
                parent_event=None,
            )
        assert ledger.read_global().events == ()

    wrong_access = replace(
        _access(),
        partition=replace(_access().partition, tenant_id="tenant_002"),
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        wrong_access,
        initialize=True,
    ) as ledger:
        with pytest.raises(GitObservationV1Error):
            append_git_observation_batch(
                ledger,
                events,
                parent_event=None,
            )
        assert ledger.read_global().events == ()


def test_capture_functions_preserve_results_while_recorder_appends_git_events() -> None:
    outputs = {
        ("git", "rev-parse", "HEAD"): COMMIT_B,
        ("git", "rev-parse", "--show-toplevel"): "/workspace/project",
        ("git", "branch", "--show-current"): "main",
        ("git", "status", "--porcelain"): " M src/app.py",
    }

    def metadata_runner(args: list[str], cwd: str | None) -> str:
        assert cwd == "/workspace/project"
        return outputs[tuple(args)]

    def ancestry_runner(args: list[str], cwd: str | None) -> int:
        assert cwd == "/workspace/project"
        return 0 if args[-2] == COMMIT_A else 1

    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = GitObservationEventRecorder(
            ledger,
            trace_id="trace_git_001",
            run_id="run_git_001",
            checkout_alias="checkout_local_001",
            source=_source(),
            trusted_context=_access().event_trusted_context(),
            occurred_at="2026-08-03T01:00:01Z",
            recorded_at="2026-08-03T01:00:10Z",
            git_version="2.43.0",
            runner_name="git_cli_runner",
            runner_version="1.0.0",
            algorithm_name="git_observation_capture",
            algorithm_version="1.0.0",
            classification="internal",
            retention_policy_id="retention_engineering_memory",
            first_sequence=1,
            first_global_position=1,
            parent_event=None,
            remote_sha256="sha256:" + "e" * 64,
            shallow=True,
            boundary_commit_shas=(COMMIT_A,),
            diff_artifact_ref=_artifact(),
            diff_base_commit_sha=COMMIT_A,
        )
        metadata = capture_trace_metadata(
            "/workspace/project",
            runner=metadata_runner,
            observation_recorder=recorder,
        )
        ancestry = capture_commit_ancestry(
            COMMIT_B,
            (COMMIT_C, COMMIT_A),
            "/workspace/project",
            runner=ancestry_runner,
            observation_recorder=recorder,
        )

        assert metadata.commit_sha == COMMIT_B
        assert metadata.repo == "project"
        assert metadata.branch == "main"
        assert metadata.dirty is True
        assert ancestry.current_commit_sha == COMMIT_B
        assert ancestry.commit_relations == ((COMMIT_A, True), (COMMIT_C, False))
        retained = ledger.read_global(limit=100).events
        parsed = tuple(parse_git_observation(event) for event in retained)
        assert tuple(item.sequence for item in parsed) == tuple(
            range(1, len(parsed) + 1)
        )
        assert {item.observation_kind for item in parsed} == {
            "checkout",
            "commit",
            "ref",
            "worktree_status",
            "diff",
            "shallow_state",
            "object_availability",
            "ancestry",
        }
        assert all(item.runner_version == "1.0.0" for item in parsed)
        assert all(item.algorithm_version == "1.0.0" for item in parsed)
        assert recorder.next_sequence == len(parsed) + 1
        assert recorder.next_global_position == len(parsed) + 1
        assert len(recorder.commits) == 2


def test_recorder_resumes_exact_pending_batch_after_post_commit_response_loss() -> (
    None
):
    anchors = tuple(f"{value:040x}" for value in range(1, 52))
    evidence = CommitAncestryEvidence(
        current_commit_sha=COMMIT_B,
        commit_relations=tuple((anchor, False) for anchor in anchors),
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        response_loss = _PostCommitResponseLossLedger(ledger, fail_on_call=2)
        recorder = GitObservationEventRecorder(
            cast(EventLedgerAtomicAppendPort, response_loss),
            trace_id="trace_git_001",
            run_id="run_git_001",
            checkout_alias="checkout_local_001",
            source=_source(),
            trusted_context=_access().event_trusted_context(),
            occurred_at="2026-08-03T01:00:01Z",
            recorded_at="2026-08-03T01:00:10Z",
            git_version="2.43.0",
            runner_name="git_cli_runner",
            runner_version="1.0.0",
            algorithm_name="git_observation_capture",
            algorithm_version="1.0.0",
            classification="internal",
            retention_policy_id="retention_engineering_memory",
            first_sequence=1,
            first_global_position=1,
            parent_event=None,
        )

        with pytest.raises(RuntimeError, match="response loss"):
            recorder.record_ancestry(evidence)

        assert len(ledger.read_global(limit=200).events) == 103
        assert recorder.next_sequence == 101
        assert recorder.pending_batch_count == 1
        assert len(recorder.commits) == 1

        recorder.resume_pending()

        retained = ledger.read_global(limit=200).events
        assert len(retained) == 103
        assert tuple(event.stream_version for event in retained) == tuple(
            range(1, 104)
        )
        assert recorder.next_sequence == 104
        assert recorder.next_global_position == 104
        assert recorder.pending_batch_count == 0
        assert len(recorder.commits) == 2
        assert recorder.commits[-1].replayed is True


def test_capture_recorder_annotations_are_runtime_resolvable() -> None:
    metadata_hints = get_type_hints(capture_trace_metadata)
    ancestry_hints = get_type_hints(capture_commit_ancestry)

    assert metadata_hints["observation_recorder"] == GitObservationRecorder | None
    assert ancestry_hints["observation_recorder"] == GitObservationRecorder | None


def test_failed_ancestry_capture_records_unknown_objects_without_returning_false() -> (
    None
):
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = GitObservationEventRecorder(
            ledger,
            trace_id="trace_git_001",
            run_id="run_git_001",
            checkout_alias="checkout_local_001",
            source=_source(),
            trusted_context=_access().event_trusted_context(),
            occurred_at="2026-08-03T01:00:01Z",
            recorded_at="2026-08-03T01:00:10Z",
            git_version="2.43.0",
            runner_name="git_cli_runner",
            runner_version="1.0.0",
            algorithm_name="git_observation_capture",
            algorithm_version="1.0.0",
            classification="internal",
            retention_policy_id="retention_engineering_memory",
            first_sequence=1,
            first_global_position=1,
            parent_event=None,
        )

        def failing_runner(args: list[str], cwd: str | None) -> int:
            del args, cwd
            raise RuntimeError("missing object with sensitive path")

        with pytest.raises(CommitAncestryCaptureError) as captured:
            capture_commit_ancestry(
                COMMIT_B,
                (COMMIT_A,),
                runner=failing_runner,
                observation_recorder=recorder,
            )

        assert "sensitive path" in str(captured.value)
        retained = ledger.read_global(limit=100).events
        parsed = tuple(parse_git_observation(event) for event in retained)
        assert len(parsed) == 2
        assert all(item.observation_kind == "object_availability" for item in parsed)
        assert all(item.details.availability == "unknown" for item in parsed)
        assert all(
            item.details.unavailable_reason == "capture_failed" for item in parsed
        )


def test_git_observation_parent_and_public_exports_are_intentional() -> None:
    events = _batch()
    verify_git_observation_parent(events[0], None)
    verify_git_observation_parent(events[1], events[0])
    with pytest.raises(GitObservationV1Error):
        verify_git_observation_parent(events[1], None)

    assert tbm.GitObservationRecordRef is GitObservationRecordRef
    assert tbm.GIT_CHECKOUT_OBSERVED == "tbm.git.checkout_observed"
    assert {
        "GitObservationEventRecorder",
        "GitObservationRecordRef",
        "append_git_observation_batch",
        "build_git_observation_batch",
        "verify_git_observation_batch",
    } <= set(tbm.__all__)
