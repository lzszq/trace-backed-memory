from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import subprocess

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from trace_backed_memory.capture import (
    GitObservationCommandResult,
    capture_and_append_git_observations,
    capture_commit_ancestry_detailed,
    capture_trace_metadata,
    capture_trace_metadata_detailed,
)
from trace_backed_memory.event_v1 import EventArtifactRef, build_canonical_event
from trace_backed_memory.git_observation_v1 import (
    GIT_ANCESTRY_OBSERVED,
    GIT_CHECKOUT_OBSERVED,
    GIT_COMMIT_OBSERVED,
    GIT_DIFF_OBSERVED,
    GIT_OBJECT_AVAILABILITY_OBSERVED,
    GIT_OBSERVATION_TYPES,
    GIT_REF_OBSERVED,
    GIT_SHALLOW_STATE_OBSERVED,
    GitAncestryObservation,
    GitAncestryRelation,
    GitCheckoutObservation,
    GitCommitObservation,
    GitDiffObservation,
    GitObjectAvailability,
    GitObjectAvailabilityObservation,
    GitObservationDraft,
    GitObservationProvenance,
    GitObservationV1Error,
    GitRefObservation,
    GitShallowStateObservation,
    append_git_observation_batch,
    build_git_observation_batch,
    build_git_observation_registry,
    dumps_git_observation_payload_dispatch_schema,
    git_observation_stream_id,
    verify_git_observation_event,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import (
    SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE,
    SQLiteEventLedgerV1,
)


ROOT = Path(__file__).resolve().parents[1]
CAPTURE_MODULE = importlib.import_module("trace_backed_memory.capture")
CURRENT = "a" * 40
TREE = "b" * 40
PARENT = "c" * 40
ANCHOR = "d" * 40
OBSERVED_AT = "2026-08-02T01:00:00Z"
RECORDED_AT = "2026-08-02T01:00:01Z"


def _access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="service_git_observation",
        authorization_decision_id="authorization_git_observation",
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        ":memory:", isolation_level=None, check_same_thread=False
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource(SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE).decode(
            "utf-8"
        )
    )
    return connection


def _artifact(data: bytes) -> EventArtifactRef:
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


def _all_drafts() -> tuple[GitObservationDraft, ...]:
    diff = b"diff --git a/a.txt b/a.txt\n"
    artifact = _artifact(diff)
    observations = (
        GitCheckoutObservation(
            root_sha256="sha256:" + "1" * 64,
            repository_name="repo",
            object_format="sha1",
            head_oid=CURRENT,
            dirty=True,
        ),
        GitRefObservation(
            object_format="sha1",
            target_oid=CURRENT,
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
            objects=(
                GitObjectAvailability(object_oid=CURRENT, status="present"),
                GitObjectAvailability(object_oid=ANCHOR, status="present"),
            ),
        ),
        GitAncestryObservation(
            object_format="sha1",
            current_oid=CURRENT,
            relations=(
                GitAncestryRelation(anchor_oid=ANCHOR, status="ancestor"),
            ),
        ),
        GitShallowStateObservation(state="full"),
    )
    return tuple(
        GitObservationDraft(
            checkout_id="checkout_001",
            sequence=sequence,
            observed_at=OBSERVED_AT,
            provenance=_provenance(),
            observation=observation,
            artifact_refs=(artifact,) if type(observation) is GitDiffObservation else (),
        )
        for sequence, observation in enumerate(observations, start=1)
    )


def _metadata_runner(calls: list[tuple[str, ...]]):
    def run(args: list[str], cwd: str | None) -> str:
        calls.append(tuple(args))
        values = {
            ("git", "rev-parse", "HEAD"): CURRENT,
            ("git", "rev-parse", "--show-toplevel"): "C:/work/repo",
            ("git", "branch", "--show-current"): "main",
            ("git", "status", "--porcelain"): " M a.txt\n",
        }
        return values[tuple(args)]

    return run


def _observation_runner(
    calls: list[tuple[tuple[str, ...], int]],
    *,
    missing: set[str] | None = None,
):
    missing = missing or set()

    def run(
        args: list[str], cwd: str | None, stdout_max_bytes: int
    ) -> GitObservationCommandResult:
        calls.append((tuple(args), stdout_max_bytes))
        key = tuple(args)
        if key == ("git", "--version"):
            return GitObservationCommandResult(0, b"git version 2.50.1.windows.1\n")
        if key == ("git", "rev-parse", "--show-object-format"):
            return GitObservationCommandResult(0, b"sha1\n")
        if key[:2] == ("git", "show"):
            return GitObservationCommandResult(
                0, f"{CURRENT}\x00{TREE}\x00{PARENT}\n".encode()
            )
        if key == ("git", "symbolic-ref", "--quiet", "HEAD"):
            return GitObservationCommandResult(0, b"refs/heads/main\n")
        if key == ("git", "rev-parse", "--is-shallow-repository"):
            return GitObservationCommandResult(0, b"false\n")
        if key[:2] == ("git", "diff"):
            return GitObservationCommandResult(
                0, b"diff --git a/a.txt b/a.txt\n"
            )
        if key[:5] == (
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
        ):
            object_oid = key[5].removesuffix("^{commit}")
            return GitObservationCommandResult(
                1 if object_oid in missing else 0, b""
            )
        raise AssertionError(f"unexpected Git observation command: {args}")

    return run


def test_registry_schema_and_seven_typed_observations_are_exact():
    registry = build_git_observation_registry()
    assert registry.sealed
    assert tuple(
        row["event_type"] for row in registry.catalog()["event_types"]
    ) == GIT_OBSERVATION_TYPES
    schema_text = dumps_git_observation_payload_dispatch_schema()
    schema = json.loads(schema_text)
    Draft202012Validator.check_schema(schema)
    assert schema_text == dumps_git_observation_payload_dispatch_schema()
    assert (
        ROOT / "schemas" / "git_observation_payload_registry_v1.schema.json"
    ).read_text(encoding="utf-8") == schema_text
    assert json.loads(
        (
            ROOT
            / "examples"
            / "git_observation_type_registry_v1.example.json"
        ).read_text(encoding="utf-8")
    ) == registry.catalog()
    assert tbm.GitObservationDraft is GitObservationDraft
    assert tbm.GIT_OBSERVATION_TYPES == GIT_OBSERVATION_TYPES

    drafts = _all_drafts()
    events, _ = build_git_observation_batch(
        drafts,
        access=_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    assert tuple(event.event_type for event in events) == (
        GIT_CHECKOUT_OBSERVED,
        GIT_REF_OBSERVED,
        GIT_COMMIT_OBSERVED,
        GIT_DIFF_OBSERVED,
        GIT_OBJECT_AVAILABILITY_OBSERVED,
        GIT_ANCESTRY_OBSERVED,
        GIT_SHALLOW_STATE_OBSERVED,
    )
    assert all(event.event_kind == "observation" for event in events)
    assert all(event.source is not None for event in events)
    assert all(event.source.observed_at == OBSERVED_AT for event in events if event.source)
    assert all(event.payload["provenance"]["runner_version"] == "f3-v1" for event in events)  # type: ignore[index]
    assert all(event.payload["provenance"]["algorithm_version"] == "v1" for event in events)  # type: ignore[index]
    assert events[3].artifact_refs == (_artifact(b"diff --git a/a.txt b/a.txt\n"),)
    for event in events:
        verify_git_observation_event(event)


def test_git_observation_batch_is_atomic_and_exactly_idempotent():
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    drafts = _all_drafts()

    connection.execute("BEGIN IMMEDIATE")
    rolled_back = append_git_observation_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    assert rolled_back.current_stream_version == 7
    connection.rollback()
    assert ledger.read_stream(git_observation_stream_id("checkout_001")).events == ()

    committed = append_git_observation_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    replayed = append_git_observation_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    assert replayed == committed
    assert len(ledger.read_stream(committed.stream_id).events) == 7

    _, shifted = build_git_observation_batch(
        drafts,
        access=_access(),
        expected_stream_version=0,
        next_global_position=100,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )
    assert shifted.idempotency_key_sha256 != committed.idempotency_key_sha256
    assert shifted.command_sha256 != committed.command_sha256


def test_legacy_metadata_capture_keeps_return_type_and_four_command_order():
    calls: list[tuple[str, ...]] = []
    metadata = capture_trace_metadata("C:/work/repo", runner=_metadata_runner(calls))
    assert type(metadata) is tbm.TraceMetadata
    assert calls == [
        ("git", "rev-parse", "HEAD"),
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "branch", "--show-current"),
        ("git", "status", "--porcelain"),
    ]


def test_detailed_metadata_captures_checkout_ref_commit_diff_and_shallow():
    metadata_calls: list[tuple[str, ...]] = []
    observation_calls: list[tuple[tuple[str, ...], int]] = []
    captured_diff: list[bytes] = []

    def write_diff(data: bytes) -> EventArtifactRef:
        captured_diff.append(data)
        return _artifact(data)

    result = capture_trace_metadata_detailed(
        "C:/work/repo",
        runner=_metadata_runner(metadata_calls),
        observation_runner=_observation_runner(observation_calls),
        diff_artifact_writer=write_diff,
        observed_at=OBSERVED_AT,
    )
    assert type(result.metadata) is tbm.TraceMetadata
    assert metadata_calls == [
        ("git", "rev-parse", "HEAD"),
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "branch", "--show-current"),
        ("git", "status", "--porcelain"),
    ]
    assert tuple(draft.event_type for draft in result.observations) == (
        GIT_CHECKOUT_OBSERVED,
        GIT_REF_OBSERVED,
        GIT_COMMIT_OBSERVED,
        GIT_DIFF_OBSERVED,
        GIT_SHALLOW_STATE_OBSERVED,
    )
    assert captured_diff == [b"diff --git a/a.txt b/a.txt\n"]
    diff_command = next(call for call, _ in observation_calls if call[:2] == ("git", "diff"))
    assert "--no-ext-diff" in diff_command
    assert "--no-textconv" in diff_command
    assert result.provenance.runner_version == "f3-v1"
    assert result.provenance.algorithm_version == "v1"


def test_detailed_default_metadata_commands_disable_lazy_fetch(monkeypatch):
    calls: list[tuple[str, ...]] = []
    values = {
        ("git", "rev-parse", "HEAD"): CURRENT,
        ("git", "rev-parse", "--show-toplevel"): "C:/work/repo",
        ("git", "branch", "--show-current"): "main",
        ("git", "status", "--porcelain"): "dirty",
    }

    def bounded(
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdout_max_bytes: int = 64 * 1024,
        fail_on_stdout_overflow: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None and env["GIT_NO_LAZY_FETCH"] == "1"
        calls.append(tuple(args))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=values[tuple(args)],
            stderr="",
        )

    monkeypatch.setattr(CAPTURE_MODULE, "_run_bounded_process", bounded)
    capture_trace_metadata_detailed(
        "C:/work/repo",
        observation_runner=_observation_runner([]),
        diff_artifact_writer=_artifact,
        observed_at=OBSERVED_AT,
    )
    assert calls == [
        ("git", "rev-parse", "HEAD"),
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "branch", "--show-current"),
        ("git", "status", "--porcelain"),
    ]


def test_diff_descriptor_must_match_exact_protected_bytes():
    with pytest.raises(GitObservationV1Error, match="artifact_id|exactly bind"):
        capture_trace_metadata_detailed(
            "C:/work/repo",
            runner=_metadata_runner([]),
            observation_runner=_observation_runner([]),
            diff_artifact_writer=lambda _: _artifact(b"different bytes"),
            observed_at=OBSERVED_AT,
        )


def test_restricted_diff_artifact_promotes_event_classification():
    result = capture_trace_metadata_detailed(
        "C:/work/repo",
        runner=_metadata_runner([]),
        observation_runner=_observation_runner([]),
        diff_artifact_writer=lambda data: replace(
            _artifact(data),
            classification="restricted",
            encryption_key_id="restricted_git_diff_key",
        ),
        observed_at=OBSERVED_AT,
    )
    diff_draft = result.observations[3]
    assert diff_draft.event_type == GIT_DIFF_OBSERVED
    assert diff_draft.classification == "restricted"
    assert diff_draft.artifact_refs[0].classification == "restricted"


def test_missing_object_is_unknown_and_never_false_ancestry():
    observation_calls: list[tuple[tuple[str, ...], int]] = []
    ancestry_calls: list[tuple[str, ...]] = []

    def ancestry_runner(args: list[str], cwd: str | None) -> int:
        ancestry_calls.append(tuple(args))
        return 1

    result = capture_commit_ancestry_detailed(
        CURRENT,
        [ANCHOR],
        "C:/work/repo",
        runner=ancestry_runner,
        observation_runner=_observation_runner(
            observation_calls, missing={ANCHOR}
        ),
        checkout_id="checkout_001",
        object_format="sha1",
        provenance=_provenance(),
        observed_at=OBSERVED_AT,
        starting_sequence=6,
    )
    assert result.evidence is None
    assert ancestry_calls == []
    availability = result.observations[0].observation
    ancestry = result.observations[1].observation
    assert type(availability) is GitObjectAvailabilityObservation
    assert type(ancestry) is GitAncestryObservation
    assert tuple((item.object_oid, item.status) for item in availability.objects) == (
        (CURRENT, "present"),
        (ANCHOR, "missing"),
    )
    assert ancestry.relations == (
        GitAncestryRelation(anchor_oid=ANCHOR, status="unknown"),
    )


def test_new_runtime_captures_and_appends_all_seven_points_together():
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    result = capture_and_append_git_observations(
        ledger,
        [ANCHOR],
        "C:/work/repo",
        metadata_runner=_metadata_runner([]),
        ancestry_runner=lambda args, cwd: 0,
        observation_runner=_observation_runner([]),
        diff_artifact_writer=_artifact,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        observed_at=OBSERVED_AT,
        recorded_at=RECORDED_AT,
    )
    assert type(result.metadata) is tbm.TraceMetadata
    assert type(result.commit_ancestry) is tbm.CommitAncestryEvidence
    assert result.commit_ancestry.commit_relations == ((ANCHOR, True),)
    assert result.receipt.current_stream_version == 7
    assert tuple(event.event_type for event in result.receipt.events) == (
        GIT_CHECKOUT_OBSERVED,
        GIT_REF_OBSERVED,
        GIT_COMMIT_OBSERVED,
        GIT_DIFF_OBSERVED,
        GIT_SHALLOW_STATE_OBSERVED,
        GIT_OBJECT_AVAILABILITY_OBSERVED,
        GIT_ANCESTRY_OBSERVED,
    )
    assert ledger.read_stream(result.receipt.stream_id).events == result.receipt.events


def test_git_contract_rejects_noncanonical_relations_and_unprotected_diff():
    with pytest.raises(GitObservationV1Error, match="sorted and unique"):
        GitAncestryObservation(
            object_format="sha1",
            current_oid=CURRENT,
            relations=(
                GitAncestryRelation(anchor_oid=ANCHOR, status="ancestor"),
                GitAncestryRelation(anchor_oid=ANCHOR, status="unknown"),
            ),
        )

    draft = _all_drafts()[3]
    weak_artifact = replace(
        draft.artifact_refs[0],
        classification="internal",
        encryption_key_id=None,
    )
    with pytest.raises(GitObservationV1Error, match="protected available"):
        replace(draft, artifact_refs=(weak_artifact,))


@pytest.mark.parametrize(
    "forged_artifact",
    (
        replace(
            _artifact(b"diff --git a/a.txt b/a.txt\n"),
            media_type="text/plain",
            classification="public",
            encryption_key_id=None,
        ),
        replace(
            _artifact(b"diff --git a/a.txt b/a.txt\n"),
            size_bytes=99,
        ),
    ),
)
def test_event_verifier_rejects_forged_diff_descriptor(
    forged_artifact: EventArtifactRef,
):
    valid = build_git_observation_batch(
        _all_drafts(),
        access=_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at=RECORDED_AT,
    )[0][3]
    forged = build_canonical_event(
        event_id=valid.event_id,
        event_type=valid.event_type,
        event_version=valid.event_version,
        event_kind=valid.event_kind,
        origin=valid.origin,
        source=valid.source,
        stream_id=valid.stream_id,
        stream_type=valid.stream_type,
        stream_version=valid.stream_version,
        global_position=valid.global_position,
        trusted_context=_access().event_trusted_context(),
        request_id=valid.request_id,
        idempotency_key_sha256=valid.idempotency_key_sha256,
        request_sha256=valid.request_sha256,
        correlation_id=valid.correlation_id,
        causation_id=valid.causation_id,
        occurred_at=valid.occurred_at,
        recorded_at=valid.recorded_at,
        producer=valid.producer,
        producer_version=valid.producer_version,
        payload_schema=valid.payload_schema,
        previous_stream_event_sha256=valid.previous_stream_event_sha256,
        classification=valid.classification,
        retention_policy_id=valid.retention_policy_id,
        artifact_refs=(forged_artifact,),
        payload=valid.payload,
    )
    with pytest.raises(GitObservationV1Error, match="exactly bind protected"):
        verify_git_observation_event(forged)


def test_detailed_capture_against_real_git_repository(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tbm@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "TBM Test"],
        check=True,
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial"],
        check=True,
    )
    tracked.write_text("after\n", encoding="utf-8")

    metadata = capture_trace_metadata_detailed(
        str(repo),
        diff_artifact_writer=_artifact,
        observed_at=OBSERVED_AT,
    )
    assert metadata.metadata.dirty is True
    assert type(metadata.observations[3].observation) is GitDiffObservation
    assert metadata.observations[3].observation.size_bytes > 0

    ancestry = capture_commit_ancestry_detailed(
        metadata.metadata.commit_sha,
        [metadata.metadata.commit_sha],
        str(repo),
        checkout_id=metadata.checkout_id,
        object_format=metadata.object_format,
        provenance=metadata.provenance,
        observed_at=OBSERVED_AT,
        starting_sequence=6,
    )
    assert ancestry.evidence is not None
    assert ancestry.evidence.commit_relations == (
        (metadata.metadata.commit_sha, True),
    )
