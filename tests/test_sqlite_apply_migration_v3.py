from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.cli as cli
import trace_backed_memory.sqlite_apply_migration_v3 as migration_apply
from trace_backed_memory.sqlite import SQLiteMemoryRepository
from trace_backed_memory.sqlite_apply_migration_v3 import (
    SQLiteV3ApplyMigrationError,
    apply_sqlite_v3_migration,
    require_durable_sqlite_migration_profile,
    rollback_sqlite_v3_migration,
    verify_sqlite_v3_migration,
)
from trace_backed_memory.sqlite_bundle_v3 import install_sqlite_v3_bundle


NOW = "2026-07-30T00:00:00Z"
NOW_VALUE = datetime(2026, 7, 30, tzinfo=timezone.utc)
HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64


def _fixture(
    *,
    include_usage: bool = True,
    include_dirty_trace: bool = True,
) -> tuple[tbm.TraceBackedMemoryStore, tbm.SnapshotV3MigrationMapping]:
    store = tbm.TraceBackedMemoryStore()
    source = store.record_trace(
        tbm.Trace(
            trace_id="trace_failure",
            run_id="run_failure",
            commit_sha="commit_failure",
            repo="repo",
            tenant="tenant",
            eval_result="fail",
        )
    )
    regression = store.record_trace(
        tbm.Trace(
            trace_id="trace_regression",
            run_id="run_regression",
            commit_sha="commit_fix",
            repo="repo",
            tenant="tenant",
            eval_suite="suite",
            eval_result="pass",
        )
    )
    case = store.add_failure_case(
        tbm.verify_failure_case(
            tbm.review_failure_case(
                tbm.draft_failure_case(
                    source,
                    case_id="case",
                    failure_type="tool_contract",
                    symptom="query was empty",
                ),
                reviewed_by="reviewer",
                root_cause="the caller omitted a required query",
                reviewed_at=NOW,
            ),
            fix="require a non-empty query",
            fix_commit_sha="commit_fix",
            regression_passed=True,
        )
    )
    store.add_lesson(
        tbm.lesson_from_failure_case(
            case,
            lesson_id="lesson",
            lesson_text="Always provide a non-empty query.",
            memory_type="procedural",
            scope={
                "repo": "repo",
                "tenant": "tenant",
                "tool": "search_docs",
            },
        )
    )
    store.add_project_policy(
        tbm.ProjectPolicy(
            policy_id="policy",
            policy_text="Use repository tools only for the current task.",
            scope={"tool": "search_docs"},
        )
    )
    bindings = [
        tbm.TraceIdentityBinding(
            trace_id=source.trace_id,
            repository_id="repository",
            tenant_id="tenant_id",
        ),
        tbm.TraceIdentityBinding(
            trace_id=regression.trace_id,
            repository_id="repository",
            tenant_id="tenant_id",
        ),
    ]
    if include_dirty_trace:
        dirty = store.record_trace(
            tbm.Trace(
                trace_id="trace_dirty",
                run_id="run_dirty",
                commit_sha="commit_fix",
                repo="repo",
                tenant="tenant",
                dirty=True,
            )
        )
        bindings.append(
            tbm.TraceIdentityBinding(
                trace_id=dirty.trace_id,
                repository_id="repository",
                tenant_id="tenant_id",
            )
        )
    if include_usage:
        current = store.record_trace(
            tbm.Trace(
                trace_id="trace_current",
                run_id="run_current",
                commit_sha="commit_fix",
                repo="repo",
                tenant="tenant",
                tool_calls=[{"name": "search_docs"}],
            )
        )
        request = store.prepare_memory(
            tbm.MemoryContext(
                mode="repair",
                repo="repo",
                tenant="tenant",
                commit_sha="commit_fix",
                tool="search_docs",
            ),
            task="repair search",
            trace_id=current.trace_id,
        )
        store.finalize_memory(
            request,
            {
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "memory is not required",
                "risk": "none",
                "recommended_injection": "none",
            },
            trace_id=current.trace_id,
        )
        bindings.append(
            tbm.TraceIdentityBinding(
                trace_id=current.trace_id,
                repository_id="repository",
                tenant_id="tenant_id",
            )
        )
    mapping = tbm.SnapshotV3MigrationMapping(
        repositories=(
            tbm.CanonicalRepository(
                repository_id="repository",
                provider="github",
                provider_repository_id="123",
                canonical_locator_hash=HASH_1,
                display_name="repo",
                legacy_aliases=("repo",),
            ),
        ),
        tenants=(
            tbm.TenantIdentity(
                tenant_id="tenant_id",
                display_name="tenant",
                legacy_aliases=("tenant",),
            ),
        ),
        trace_bindings=tuple(bindings),
        memory_scopes=(
            tbm.MemoryScopeBinding(
                memory_kind="lesson",
                memory_id="lesson",
                scope=tbm.AuthorizationScope(
                    kind="repository",
                    tenant_id="tenant_id",
                    repository_id="repository",
                    attributes=(("tool", "search_docs"),),
                ),
            ),
            tbm.MemoryScopeBinding(
                memory_kind="project_policy",
                memory_id="policy",
                scope=tbm.AuthorizationScope(
                    kind="global",
                    attributes=(("tool", "search_docs"),),
                ),
            ),
        ),
        regression_evidence=(
            tbm.RegressionEvidence(
                evidence_id="evidence",
                case_id="case",
                regression_trace_id="trace_regression",
                regression_run_id="run_regression",
                evaluator_id="evaluator",
                evaluator_version="1",
                eval_suite="suite",
                eval_case_id="case",
                evaluated_commit_sha="commit_fix",
                result="pass",
                observed_at=NOW,
                source_to_fix=tbm.CommitRelationEvidence(
                    from_commit_sha="commit_failure",
                    to_commit_sha="commit_fix",
                    relation="ancestor",
                    verified_by="ci",
                    verified_at=NOW,
                ),
                fix_to_regression=tbm.CommitRelationEvidence(
                    from_commit_sha="commit_fix",
                    to_commit_sha="commit_fix",
                    relation="ancestor",
                    verified_by="ci",
                    verified_at=NOW,
                ),
                artifact_hashes=(HASH_2,),
            ),
        ),
        global_policy_approvals=(
            tbm.GlobalPolicyApproval(
                policy_id="policy",
                principal_id="policy_admin",
                authorization_event_id="authorization_event",
                approved_at=NOW,
                reason="approved through the privileged workflow",
            ),
        ),
        ancestry_policy=tbm.AncestryPolicy(
            mode="disabled",
            bypass_reason="offline migration test",
        ),
    )
    return store, mapping


def _bundle(
    store: tbm.TraceBackedMemoryStore,
    mapping: tbm.SnapshotV3MigrationMapping,
) -> tbm.SnapshotV3MigrationBundle:
    bundle = tbm.create_snapshot_v3_migration_bundle(store, mapping)
    assert bundle.ready is True
    return bundle


def _write_sqlite(
    path: Path,
    store: tbm.TraceBackedMemoryStore,
) -> None:
    with SQLiteMemoryRepository.connect(path, initialize=True) as repository:
        repository.sync(store)


def test_snapshot_apply_verify_and_rollback_preserve_legacy_evidence(
    tmp_path: Path,
) -> None:
    store, mapping = _fixture()
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.json.tbm-v2.bak"
    compatibility = tmp_path / "compat.sqlite3"
    store.save_json(source)
    source_bytes = source.read_bytes()

    applied = apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )

    assert source.read_bytes() == source_bytes
    assert backup.read_bytes() == source_bytes
    assert applied.profile == "durable-v3"
    assert applied.record_count == 8
    assert {
        (
            item.record_kind,
            item.record_id,
            item.evidence_status,
            item.target_status,
        )
        for item in applied.dispositions
    } >= {
        (
            "trace",
            "trace_dirty",
            "legacy_dirty_trace",
            "retained_legacy",
        ),
        (
            "lesson",
            "lesson",
            "mapped_regression_preflight",
            "unpublished_v3",
        ),
        (
            "project_policy",
            "policy",
            "legacy_unverified",
            "unpublished_v3",
        ),
        (
            "usage_log",
            next(iter(store.usage_logs)).decision_id,
            "legacy_partial_replay",
            "legacy_partial",
        ),
    }
    assert verify_sqlite_v3_migration(target) == applied

    rolled_back = rollback_sqlite_v3_migration(
        target,
        compatibility_database=compatibility,
        clock=lambda: NOW_VALUE,
    )
    repeated = rollback_sqlite_v3_migration(
        target,
        compatibility_database=compatibility,
        clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    assert rolled_back.profile == "compat-v2"
    assert repeated == rolled_back
    assert rolled_back.compatibility_path == str(compatibility.resolve())
    with SQLiteMemoryRepository.connect(compatibility) as repository:
        assert repository.load().to_snapshot() == store.to_snapshot()
    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
            require_durable_sqlite_migration_profile(connection)
        assert raised.value.code == (
            "TBM_SQLITE_V3_MIGRATION_PROFILE_ROLLED_BACK"
        )
        assert connection.execute(
            "SELECT count(*) FROM v3_migration_record_dispositions"
        ).fetchone() == (applied.record_count,)
    finally:
        connection.close()


def test_sqlite_source_apply_is_restart_safe_and_keeps_source_v1(
    tmp_path: Path,
) -> None:
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.sqlite3.tbm-v2.bak"
    _write_sqlite(source, store)

    first = apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="sqlite",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )
    second = apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="sqlite",
        target_database=target,
        backup=backup,
        clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    assert second == first
    source_connection = sqlite3.connect(source)
    try:
        assert source_connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE name = 'trace_backed_memory_v3_bundle_schema'"
        ).fetchone() == (0,)
    finally:
        source_connection.close()
    with SQLiteMemoryRepository.connect(backup) as repository:
        assert repository.load().to_snapshot() == store.to_snapshot()


def test_apply_rejects_blocked_bundle_before_backup_or_target(
    tmp_path: Path,
) -> None:
    store, mapping = _fixture(include_usage=False)
    blocked_mapping = tbm.SnapshotV3MigrationMapping(
        repositories=mapping.repositories,
        tenants=mapping.tenants,
        trace_bindings=(),
        memory_scopes=mapping.memory_scopes,
        regression_evidence=mapping.regression_evidence,
        global_policy_approvals=mapping.global_policy_approvals,
        ancestry_policy=mapping.ancestry_policy,
    )
    bundle = tbm.create_snapshot_v3_migration_bundle(
        store,
        blocked_mapping,
    )
    assert bundle.ready is False
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    store.save_json(source)

    with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
        apply_sqlite_v3_migration(
            bundle,
            source=source,
            source_kind="snapshot",
            target_database=target,
            backup=backup,
        )

    assert raised.value.code == "TBM_SQLITE_V3_MIGRATION_BUNDLE_BLOCKED"
    assert not target.exists()
    assert not backup.exists()


@pytest.mark.parametrize(
    "mutate_snapshot",
    [
        lambda snapshot: snapshot["failure_cases"][0].update(
            {"reviewed_by": None}
        ),
        lambda snapshot: snapshot["usage_logs"].append(
            {
                **snapshot["usage_logs"][0],
                "eval_result": "pass",
            }
        ),
    ],
    ids=("unreviewed-verified", "conflicting-decision-outcome"),
)
def test_apply_rejects_invalid_legacy_evidence_before_writing(
    tmp_path: Path,
    mutate_snapshot: object,
) -> None:
    store, mapping = _fixture()
    bundle = _bundle(store, mapping)
    snapshot = store.to_snapshot()
    assert callable(mutate_snapshot)
    mutate_snapshot(snapshot)
    source = tmp_path / "invalid-source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    source.write_text(
        json.dumps(snapshot, allow_nan=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
        apply_sqlite_v3_migration(
            bundle,
            source=source,
            source_kind="snapshot",
            target_database=target,
            backup=backup,
        )

    assert raised.value.code == "TBM_SQLITE_V3_MIGRATION_SOURCE_INVALID"
    assert not target.exists()
    assert not backup.exists()


def test_apply_preserves_missing_legacy_tenant_without_widening(
    tmp_path: Path,
) -> None:
    store = tbm.TraceBackedMemoryStore()
    trace = store.record_trace(
        tbm.Trace(
            trace_id="trace_missing_tenant",
            run_id="run_missing_tenant",
            commit_sha="commit",
            repo="repo",
            tenant=None,
        )
    )
    mapping = tbm.SnapshotV3MigrationMapping(
        repositories=(
            tbm.CanonicalRepository(
                repository_id="repository",
                provider="github",
                provider_repository_id="123",
                canonical_locator_hash=HASH_1,
                display_name="repo",
                legacy_aliases=("repo",),
            ),
        ),
        tenants=(
            tbm.TenantIdentity(
                tenant_id="tenant_id",
                display_name="explicit tenant",
            ),
        ),
        trace_bindings=(
            tbm.TraceIdentityBinding(
                trace_id=trace.trace_id,
                repository_id="repository",
                tenant_id="tenant_id",
            ),
        ),
        memory_scopes=(),
        regression_evidence=(),
        global_policy_approvals=(),
        ancestry_policy=tbm.AncestryPolicy(
            mode="disabled",
            bypass_reason="offline migration test",
        ),
    )
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    store.save_json(source)

    result = apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )

    with SQLiteMemoryRepository.connect(target) as repository:
        migrated = repository.load()
    assert migrated.traces[trace.trace_id].tenant is None
    assert result.dispositions == (
        migration_apply.LegacyRecordDisposition(
            record_kind="trace",
            record_id=trace.trace_id,
            source_status="clean",
            evidence_status="legacy_trace",
            target_status="retained_legacy",
            reason_code="TBM_V3_LEGACY_TRACE_RETAINED",
        ),
    )


@pytest.mark.parametrize("eval_result", [None, "unknown", "pass", "fail"])
def test_apply_preserves_legacy_usage_outcome_without_promotion(
    tmp_path: Path,
    eval_result: str | None,
) -> None:
    original, mapping = _fixture(include_dirty_trace=False)
    snapshot = original.to_snapshot()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0]["eval_result"] = eval_result
    store = tbm.TraceBackedMemoryStore.from_snapshot(snapshot)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    store.save_json(source)

    result = apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )

    usage_disposition = next(
        item
        for item in result.dispositions
        if item.record_kind == "usage_log"
    )
    assert usage_disposition.source_status == eval_result
    assert usage_disposition.evidence_status == "legacy_partial_replay"
    assert usage_disposition.target_status == "legacy_partial"


def test_runtime_profile_guard_rejects_forged_blocked_application(
    tmp_path: Path,
) -> None:
    store, mapping = _fixture(include_usage=False)
    blocked_mapping = tbm.SnapshotV3MigrationMapping(
        repositories=mapping.repositories,
        tenants=mapping.tenants,
        trace_bindings=(),
        memory_scopes=mapping.memory_scopes,
        regression_evidence=mapping.regression_evidence,
        global_policy_approvals=mapping.global_policy_approvals,
        ancestry_policy=mapping.ancestry_policy,
    )
    blocked = tbm.create_snapshot_v3_migration_bundle(
        store,
        blocked_mapping,
    )
    target = tmp_path / "forged.sqlite3"
    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        install_sqlite_v3_bundle(connection)
        tbm.SQLiteV3MigrationRepository(connection).stage(blocked)
        connection.execute(
            "INSERT INTO v3_migration_applications ("
            "application_id, bundle_id, source_kind, backup_path, "
            "backup_sha256, normalized_source_snapshot_sha256, "
            "disposition_sha256, record_count, applied_at"
            ") VALUES (?, ?, 'snapshot', ?, ?, ?, ?, 0, ?)",
            (
                "sha256:" + "a" * 64,
                blocked.bundle_id,
                os.fspath(tmp_path / "missing.bak"),
                "sha256:" + "b" * 64,
                blocked.normalized_source_snapshot_sha256,
                "sha256:" + "c" * 64,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO v3_migration_profile_events ("
            "application_id, sequence, profile, compatibility_path, "
            "compatibility_sha256, occurred_at, reason_code"
            ") VALUES (?, 1, 'durable-v3', NULL, NULL, ?, ?)",
            (
                "sha256:" + "a" * 64,
                NOW,
                "FORGED",
            ),
        )
        connection.commit()

        with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
            require_durable_sqlite_migration_profile(connection)
        assert raised.value.code == "TBM_SQLITE_V3_MIGRATION_BUNDLE_BLOCKED"
    finally:
        connection.close()


def test_legacy_fixed_temporary_symlink_is_never_followed(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    victim = tmp_path / "victim.sqlite3"
    legacy_temporary = target.with_name(f".{target.name}.apply.tmp")
    store.save_json(source)
    try:
        legacy_temporary.symlink_to(victim)
    except OSError:
        pytest.skip("creating a test symlink is not permitted")

    result = apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )

    assert result.profile == "durable-v3"
    assert target.exists()
    assert legacy_temporary.is_symlink()
    assert not victim.exists()


def test_replaced_private_temporary_is_rejected_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    victim = tmp_path / "victim.sqlite3"
    store.save_json(source)
    original_create = migration_apply._create_temporary

    def replace_with_symlink(
        destination: Path,
        purpose: str,
    ) -> migration_apply._TemporaryFile:
        temporary = original_create(destination, purpose)
        temporary.path.unlink()
        try:
            temporary.path.symlink_to(victim)
        except OSError:
            pytest.skip("creating a test symlink is not permitted")
        return temporary

    monkeypatch.setattr(
        migration_apply,
        "_create_temporary",
        replace_with_symlink,
    )

    with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
        apply_sqlite_v3_migration(
            bundle,
            source=source,
            source_kind="snapshot",
            target_database=target,
            backup=backup,
        )

    assert raised.value.code == "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID"
    assert not victim.exists()
    assert not target.exists()
    assert not backup.exists()


def test_publish_rechecks_private_temporary_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published.sqlite3"
    victim = tmp_path / "victim.sqlite3"
    victim.write_bytes(b"SECRET")
    temporary = migration_apply._create_temporary(destination, "apply")
    temporary.path.write_bytes(b"EXPECTED")
    original_sync = migration_apply._fsync_temporary

    def replace_after_sync(
        current: migration_apply._TemporaryFile,
    ) -> None:
        original_sync(current)
        current.path.unlink()
        os.link(victim, current.path)

    monkeypatch.setattr(
        migration_apply,
        "_fsync_temporary",
        replace_after_sync,
    )
    try:
        with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
            migration_apply._publish_temporary(temporary, destination)
        assert raised.value.code == "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID"
        assert not destination.exists()
        assert victim.read_bytes() == b"SECRET"
    finally:
        if migration_apply._path_lexists(temporary.path):
            temporary.path.unlink()
        temporary.directory.rmdir()


def test_profile_event_reason_is_part_of_strict_verification(
    tmp_path: Path,
) -> None:
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    store.save_json(source)
    apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )
    connection = sqlite3.connect(target)
    try:
        trigger_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'v3_migration_profile_events_immutable_update'"
        ).fetchone()
        assert trigger_row is not None
        trigger_sql = trigger_row[0]
        assert isinstance(trigger_sql, str)
        connection.execute(
            "DROP TRIGGER v3_migration_profile_events_immutable_update"
        )
        connection.execute(
            "UPDATE v3_migration_profile_events "
            "SET reason_code = 'FORGED'"
        )
        connection.execute(trigger_sql)
        connection.commit()

        with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
            require_durable_sqlite_migration_profile(connection)
        assert raised.value.code == "TBM_SQLITE_V3_MIGRATION_STATE_INVALID"
    finally:
        connection.close()


def test_verify_detects_backup_tampering_and_dispositions_are_immutable(
    tmp_path: Path,
) -> None:
    store, mapping = _fixture(include_usage=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    store.save_json(source)
    apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )

    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA recursive_triggers = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE v3_migration_record_dispositions "
                "SET target_status = 'retained_legacy' "
                "WHERE record_kind = 'lesson'"
            )
    finally:
        connection.close()
    backup.write_text("{}", encoding="utf-8")
    with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
        verify_sqlite_v3_migration(target)
    assert raised.value.code in {
        "TBM_SQLITE_V3_MIGRATION_BACKUP_MISMATCH",
        "TBM_SQLITE_V3_MIGRATION_SOURCE_INVALID",
    }


def test_migration_cli_apply_verify_and_rollback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    bundle_path = tmp_path / "bundle.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    compatibility = tmp_path / "compat.sqlite3"
    store.save_json(source)
    bundle_path.write_text(
        tbm.dumps_snapshot_v3_migration_bundle(bundle),
        encoding="utf-8",
    )

    assert cli.main(
        [
            "migration",
            "apply-v3",
            str(bundle_path),
            str(source),
            "--source-kind",
            "snapshot",
            "--target",
            "sqlite",
            "--database",
            str(target),
            "--backup",
            str(backup),
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert applied["profile"] == "durable-v3"
    assert applied["verified"] is True

    assert cli.main(["migration", "verify-v3", str(target)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified"
    assert verified["application_id"] == applied["application_id"]

    assert cli.main(
        [
            "migration",
            "rollback-v3",
            str(target),
            "--compat-database",
            str(compatibility),
        ]
    ) == 0
    rolled_back = json.loads(capsys.readouterr().out)
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["profile"] == "compat-v2"
    assert rolled_back["compatibility_path"] == str(
        compatibility.resolve()
    )


def test_empty_and_obsolete_sources_remain_explicit_and_unpublished(
    tmp_path: Path,
) -> None:
    empty = tbm.TraceBackedMemoryStore()
    empty_mapping = tbm.SnapshotV3MigrationMapping(
        repositories=(),
        tenants=(),
        trace_bindings=(),
        memory_scopes=(),
        regression_evidence=(),
        global_policy_approvals=(),
        ancestry_policy=tbm.AncestryPolicy(
            mode="disabled",
            bypass_reason="empty offline migration",
        ),
    )
    empty_bundle = _bundle(empty, empty_mapping)
    empty_source = tmp_path / "empty.json"
    empty_target = tmp_path / "empty.sqlite3"
    empty_backup = tmp_path / "empty.bak"
    empty.save_json(empty_source)

    empty_result = apply_sqlite_v3_migration(
        empty_bundle,
        source=empty_source,
        source_kind="snapshot",
        target_database=empty_target,
        backup=empty_backup,
        clock=lambda: NOW_VALUE,
    )

    assert empty_result.record_count == 0
    assert empty_result.dispositions == ()

    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    store.obsolete_lesson("lesson")
    store.obsolete_project_policy("policy")
    obsolete_bundle = _bundle(store, mapping)
    obsolete_source = tmp_path / "obsolete.json"
    obsolete_target = tmp_path / "obsolete.sqlite3"
    obsolete_backup = tmp_path / "obsolete.bak"
    store.save_json(obsolete_source)

    obsolete_result = apply_sqlite_v3_migration(
        obsolete_bundle,
        source=obsolete_source,
        source_kind="snapshot",
        target_database=obsolete_target,
        backup=obsolete_backup,
        clock=lambda: NOW_VALUE,
    )

    statuses = {
        (item.record_kind, item.record_id): (
            item.source_status,
            item.target_status,
        )
        for item in obsolete_result.dispositions
    }
    assert statuses[("lesson", "lesson")] == (
        "obsolete",
        "unpublished_v3",
    )
    assert statuses[("project_policy", "policy")] == (
        "obsolete",
        "unpublished_v3",
    )


def test_apply_recovers_from_failure_after_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    store.save_json(source)
    original_install = migration_apply.install_sqlite_v3_bundle

    with monkeypatch.context() as patch:
        patch.setattr(
            migration_apply,
            "install_sqlite_v3_bundle",
            lambda _connection: (_ for _ in ()).throw(
                RuntimeError("injected install failure")
            ),
        )
        with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
            apply_sqlite_v3_migration(
                bundle,
                source=source,
                source_kind="snapshot",
                target_database=target,
                backup=backup,
                clock=lambda: NOW_VALUE,
            )
        assert raised.value.code == "TBM_SQLITE_V3_MIGRATION_APPLY_FAILED"

    assert migration_apply.install_sqlite_v3_bundle is original_install
    assert backup.exists()
    assert not target.exists()
    recovered = apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )
    assert recovered.profile == "durable-v3"


def test_process_crash_recovery_across_backup_publish_and_rollback(
    tmp_path: Path,
) -> None:
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    bundle_path = tmp_path / "bundle.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    compatibility = tmp_path / "compat.sqlite3"
    store.save_json(source)
    bundle_path.write_text(
        tbm.dumps_snapshot_v3_migration_bundle(bundle),
        encoding="utf-8",
    )
    common = (
        "import os\n"
        "import trace_backed_memory as tbm\n"
        "import trace_backed_memory.sqlite_apply_migration_v3 as m\n"
        f"bundle = tbm.load_snapshot_v3_migration_bundle({str(bundle_path)!r})\n"
    )
    apply_call = (
        "m.apply_sqlite_v3_migration("
        f"bundle, source={str(source)!r}, source_kind='snapshot', "
        f"target_database={str(target)!r}, backup={str(backup)!r})\n"
    )

    crashed_after_backup = subprocess.run(
        [
            sys.executable,
            "-c",
            common
            + "m._build_target = lambda **_kwargs: os._exit(71)\n"
            + apply_call,
        ],
        cwd=Path(__file__).parents[1],
        check=False,
    )
    assert crashed_after_backup.returncode == 71
    assert backup.exists()
    assert not target.exists()

    crashed_after_publish = subprocess.run(
        [
            sys.executable,
            "-c",
            common
            + "m.verify_sqlite_v3_migration = "
            + "lambda *_args, **_kwargs: os._exit(72)\n"
            + apply_call,
        ],
        cwd=Path(__file__).parents[1],
        check=False,
    )
    assert crashed_after_publish.returncode == 72
    assert target.exists()
    assert apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
    ).profile == "durable-v3"

    rollback_call = (
        "m.rollback_sqlite_v3_migration("
        f"{str(target)!r}, compatibility_database={str(compatibility)!r})\n"
    )
    crashed_before_rollback_event = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os\n"
            "import trace_backed_memory.sqlite_apply_migration_v3 as m\n"
            "m._append_rollback_event = "
            "lambda *_args, **_kwargs: os._exit(73)\n"
            + rollback_call,
        ],
        cwd=Path(__file__).parents[1],
        check=False,
    )
    assert crashed_before_rollback_event.returncode == 73
    assert compatibility.exists()
    assert verify_sqlite_v3_migration(target).profile == "durable-v3"
    assert rollback_sqlite_v3_migration(
        target,
        compatibility_database=compatibility,
    ).profile == "compat-v2"


def test_rollback_recovers_output_published_before_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    compatibility = tmp_path / "compat.sqlite3"
    store.save_json(source)
    apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )

    with monkeypatch.context() as patch:
        patch.setattr(
            migration_apply,
            "_append_rollback_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                SQLiteV3ApplyMigrationError(
                    "TBM_SQLITE_V3_MIGRATION_ROLLBACK_STATE_FAILED",
                    "injected rollback event failure",
                )
            ),
        )
        with pytest.raises(SQLiteV3ApplyMigrationError) as raised:
            rollback_sqlite_v3_migration(
                target,
                compatibility_database=compatibility,
                clock=lambda: NOW_VALUE,
            )
        assert raised.value.code == (
            "TBM_SQLITE_V3_MIGRATION_ROLLBACK_STATE_FAILED"
        )

    assert compatibility.exists()
    assert verify_sqlite_v3_migration(target).profile == "durable-v3"
    recovered = rollback_sqlite_v3_migration(
        target,
        compatibility_database=compatibility,
        clock=lambda: NOW_VALUE,
    )
    assert recovered.profile == "compat-v2"


def test_rollback_holds_sqlite_writer_lock_through_compatibility_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, mapping = _fixture(include_usage=False, include_dirty_trace=False)
    bundle = _bundle(store, mapping)
    source = tmp_path / "source.json"
    target = tmp_path / "durable.sqlite3"
    backup = tmp_path / "source.bak"
    compatibility = tmp_path / "compat.sqlite3"
    store.save_json(source)
    apply_sqlite_v3_migration(
        bundle,
        source=source,
        source_kind="snapshot",
        target_database=target,
        backup=backup,
        clock=lambda: NOW_VALUE,
    )
    original_create = migration_apply._create_compatibility_database

    def create_while_contended(
        current_store: tbm.TraceBackedMemoryStore,
        destination: Path,
    ) -> None:
        contender = sqlite3.connect(target, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()
        original_create(current_store, destination)

    monkeypatch.setattr(
        migration_apply,
        "_create_compatibility_database",
        create_while_contended,
    )

    result = rollback_sqlite_v3_migration(
        target,
        compatibility_database=compatibility,
        clock=lambda: NOW_VALUE,
    )

    assert result.profile == "compat-v2"
