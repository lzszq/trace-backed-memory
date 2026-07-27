import sqlite3
from pathlib import Path

import pytest

import trace_backed_memory as tbm


def _mapping(
    *,
    ancestry_mode: str = "disabled",
) -> tbm.SnapshotV3MigrationMapping:
    ancestry = (
        tbm.AncestryPolicy(
            mode="disabled",
            bypass_reason="no commit-bearing evidence exists in this fixture",
        )
        if ancestry_mode == "disabled"
        else tbm.AncestryPolicy(mode="required")
    )
    return tbm.SnapshotV3MigrationMapping(
        repositories=(),
        tenants=(),
        trace_bindings=(),
        memory_scopes=(),
        regression_evidence=(),
        global_policy_approvals=(),
        ancestry_policy=ancestry,
    )


def _bundle(
    *,
    ancestry_mode: str = "disabled",
) -> tbm.SnapshotV3MigrationBundle:
    verifier = (
        (lambda _repository_id, _relation: True)
        if ancestry_mode == "required"
        else None
    )
    return tbm.create_snapshot_v3_migration_bundle(
        tbm.TraceBackedMemoryStore(),
        _mapping(ancestry_mode=ancestry_mode),
        commit_relation_verifier=verifier,
    )


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-migration.sql"
        ).decode("utf-8")
    )


def test_sqlite_v3_staging_round_trip_and_exact_replay():
    bundle = _bundle()
    with tbm.SQLiteV3MigrationRepository.connect(
        ":memory:",
        initialize=True,
    ) as repository:
        inserted = repository.stage(bundle)
        replayed = repository.stage(bundle)

        assert inserted == tbm.SQLiteV3MigrationStageResult(
            bundle_id=bundle.bundle_id,
            state="ready",
            inserted=True,
        )
        assert replayed == tbm.SQLiteV3MigrationStageResult(
            bundle_id=bundle.bundle_id,
            state="ready",
            inserted=False,
        )
        assert repository.load(bundle.bundle_id) == bundle
        assert repository.list_bundle_ids() == (bundle.bundle_id,)


def test_sqlite_v3_staging_is_side_by_side_with_runtime_schema(tmp_path):
    database = tmp_path / "combined.sqlite3"
    runtime = tbm.SQLiteMemoryRepository.connect(
        database,
        initialize=True,
    )
    runtime.sync(tbm.TraceBackedMemoryStore())
    runtime.close()

    staging = tbm.SQLiteV3MigrationRepository.connect(
        database,
        initialize=True,
    )
    staging.stage(_bundle())
    staging.close()

    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT schema_version FROM trace_backed_memory_schema"
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT schema_version "
        "FROM trace_backed_memory_v3_migration_schema"
    ).fetchone() == (1,)
    connection.close()

    runtime = tbm.SQLiteMemoryRepository.connect(database)
    assert runtime.load().to_snapshot() == (
        tbm.TraceBackedMemoryStore().to_snapshot()
    )
    runtime.close()


def test_sqlite_v3_staging_preserves_blocked_state():
    bundle = tbm.create_snapshot_v3_migration_bundle(
        tbm.TraceBackedMemoryStore(),
        _mapping(ancestry_mode="required"),
    )
    assert bundle.state == "blocked"

    with tbm.SQLiteV3MigrationRepository.connect(
        ":memory:",
        initialize=True,
    ) as repository:
        result = repository.stage(bundle)

        assert result.state == "blocked"
        assert repository.load(bundle.bundle_id).ready is False


def test_sqlite_v3_ready_required_bundle_needs_same_trusted_verifier():
    bundle = _bundle(ancestry_mode="required")
    repository = tbm.SQLiteV3MigrationRepository.connect(
        ":memory:",
        initialize=True,
    )

    with pytest.raises(tbm.V3MigrationBundleError) as error:
        repository.stage(bundle)
    assert error.value.code == "TBM_V3_BUNDLE_PLAN_REPLAY_MISMATCH"

    def verifier(_repository_id, _relation):
        return True

    assert repository.stage(
        bundle,
        commit_relation_verifier=verifier,
    ).inserted is True
    repository.close()


def test_sqlite_v3_schema_is_separate_and_strict():
    missing = tbm.SQLiteV3MigrationRepository(sqlite3.connect(":memory:"))
    with pytest.raises(tbm.SQLiteV3MigrationSchemaError):
        missing.list_bundle_ids()

    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE trace_backed_memory_v3_migration_schema "
        "SET schema_version = 99"
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    mismatched = tbm.SQLiteV3MigrationRepository(connection)
    with pytest.raises(
        tbm.SQLiteV3MigrationSchemaError,
        match="expected 1, found 99",
    ):
        mismatched.list_bundle_ids()


def test_sqlite_v3_database_triggers_keep_bundles_immutable():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteV3MigrationRepository(connection)
    bundle = _bundle()
    repository.stage(bundle)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE v3_migration_bundles SET state = 'blocked'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("DELETE FROM v3_migration_bundles")

    assert repository.load(bundle.bundle_id) == bundle


def test_sqlite_v3_repository_rejects_weakened_same_version_schema():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute(
        "DROP TRIGGER v3_migration_bundles_immutable_update"
    )
    repository = tbm.SQLiteV3MigrationRepository(connection)

    with pytest.raises(
        tbm.SQLiteV3MigrationSchemaError,
        match="missing or incomplete",
    ):
        repository.list_bundle_ids()


def test_sqlite_v3_load_revalidates_columns_and_payload():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteV3MigrationRepository(connection)
    bundle = _bundle()
    payload = tbm.dumps_snapshot_v3_migration_bundle(bundle)
    connection.execute(
        "INSERT INTO v3_migration_bundles "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bundle.bundle_id,
            bundle.bundle_version,
            bundle.state,
            bundle.source_snapshot_sha256,
            bundle.normalized_source_snapshot_sha256,
            "sha256:" + "0" * 64,
            bundle.plan_sha256,
            payload,
        ),
    )
    connection.commit()

    with pytest.raises(
        tbm.SQLiteV3MigrationPersistenceError,
        match="columns do not match",
    ):
        repository.load(bundle.bundle_id)


def test_sqlite_v3_stage_uses_savepoint_inside_caller_transaction():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute("CREATE TABLE caller_state (value TEXT)")
    repository = tbm.SQLiteV3MigrationRepository(connection)
    bundle = _bundle()

    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES ('pending')")
    assert repository.stage(bundle).inserted is True
    connection.rollback()

    assert connection.execute(
        "SELECT count(*) FROM caller_state"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM v3_migration_bundles"
    ).fetchone() == (0,)
    assert repository.stage(bundle).inserted is True


def test_sqlite_v3_unreleased_savepoint_aborts_outer_transaction():
    class FailingReleaseConnection(sqlite3.Connection):
        release_failures_remaining = 0

        def execute(self, sql, parameters=(), /):
            if (
                self.release_failures_remaining
                and sql.upper().startswith("RELEASE SAVEPOINT")
            ):
                self.release_failures_remaining -= 1
                raise sqlite3.OperationalError(
                    "simulated savepoint release failure"
                )
            return super().execute(sql, parameters)

    connection = sqlite3.connect(
        ":memory:",
        isolation_level=None,
        factory=FailingReleaseConnection,
    )
    _initialize(connection)
    connection.execute("CREATE TABLE caller_state (value TEXT)")
    repository = tbm.SQLiteV3MigrationRepository(connection)
    bundle = _bundle()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES ('must-roll-back')")
    connection.release_failures_remaining = 3

    with pytest.raises(
        tbm.SQLiteV3MigrationPersistenceError,
        match="failed to stage",
    ) as error:
        repository.stage(bundle)

    notes = getattr(error.value.__cause__, "__notes__", ())
    assert any("retry failed" in note for note in notes)
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT count(*) FROM caller_state"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM v3_migration_bundles"
    ).fetchone() == (0,)
    assert repository.stage(bundle).inserted is True


def test_sqlite_v3_closes_connection_when_outer_rollback_cannot_recover():
    class UnrecoverableRollbackConnection(sqlite3.Connection):
        release_failures_remaining = 0
        rollback_failures_remaining = 0

        def execute(self, sql, parameters=(), /):
            if (
                self.release_failures_remaining
                and sql.upper().startswith("RELEASE SAVEPOINT")
            ):
                self.release_failures_remaining -= 1
                raise sqlite3.OperationalError(
                    "simulated savepoint release failure"
                )
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            if self.rollback_failures_remaining:
                self.rollback_failures_remaining -= 1
                raise sqlite3.OperationalError(
                    "simulated outer rollback failure"
                )
            super().rollback()

    connection = sqlite3.connect(
        ":memory:",
        isolation_level=None,
        factory=UnrecoverableRollbackConnection,
    )
    _initialize(connection)
    repository = tbm.SQLiteV3MigrationRepository(connection)
    bundle = _bundle()
    connection.execute("BEGIN")
    connection.release_failures_remaining = 3
    connection.rollback_failures_remaining = 2

    with pytest.raises(
        tbm.SQLiteV3MigrationPersistenceError,
        match="failed to stage",
    ) as error:
        repository.stage(bundle)

    notes = getattr(error.value.__cause__, "__notes__", ())
    assert any("retry failed while rolling back" in note for note in notes)
    assert repository._closed is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.commit()


def test_sqlite_v3_stage_conflict_rolls_back_without_replacing_row():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteV3MigrationRepository(connection)
    bundle = _bundle()
    payload = tbm.dumps_snapshot_v3_migration_bundle(bundle)
    row = (
        bundle.bundle_id,
        bundle.bundle_version,
        "blocked",
        bundle.source_snapshot_sha256,
        bundle.normalized_source_snapshot_sha256,
        bundle.mapping_sha256,
        bundle.plan_sha256,
        payload,
    )
    connection.execute(
        "INSERT INTO v3_migration_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    connection.commit()

    with pytest.raises(tbm.SQLiteV3MigrationConflictError):
        repository.stage(bundle)

    assert connection.execute(
        "SELECT state FROM v3_migration_bundles"
    ).fetchone() == ("blocked",)


def test_sqlite_v3_repository_validates_ids_limits_and_close():
    repository = tbm.SQLiteV3MigrationRepository.connect(
        ":memory:",
        initialize=True,
    )
    with pytest.raises(ValueError, match="bundle_id"):
        repository.load("invalid")
    with pytest.raises(ValueError, match="limit"):
        repository.list_bundle_ids(limit=0)
    with pytest.raises(KeyError):
        repository.load("sha256:" + "0" * 64)

    repository.close()
    repository.close()
    with pytest.raises(tbm.SQLiteV3MigrationError, match="closed"):
        repository.list_bundle_ids()


def test_sqlite_v3_schema_resource_is_canonical_and_versioned():
    source = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "sqlite-v3-migration.sql"
    ).read_bytes()

    assert tbm.SQLITE_V3_MIGRATION_SCHEMA_VERSION == 1
    assert (
        tbm.read_packaged_resource("schemas/sqlite-v3-migration.sql")
        == source
    )
    assert b"trace_backed_memory_v3_migration_schema" in source
    assert b"trace_backed_memory_schema" not in source.replace(
        b"trace_backed_memory_v3_migration_schema",
        b"",
    )


def test_sqlite_v3_public_exports_are_intentional():
    for name in (
        "SQLiteV3MigrationConflictError",
        "SQLiteV3MigrationError",
        "SQLiteV3MigrationPersistenceError",
        "SQLiteV3MigrationRepository",
        "SQLiteV3MigrationSchemaError",
        "SQLiteV3MigrationStageResult",
    ):
        assert name in tbm.__all__
