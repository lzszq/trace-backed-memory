from __future__ import annotations

from collections.abc import Iterable
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
from threading import Barrier, Thread

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_gate_session_v3 as gate_sqlite


NOW = "2026-07-27T00:00:00Z"
FINGERPRINT = "sha256:" + "a" * 64


class Clock:
    def __init__(self, values: Iterable[str]) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


def _create(
    repository: tbm.SQLiteGateSessionRepository,
    *,
    session_id: str = "session_001",
    idempotency_key: str = "idempotency_001",
    fingerprint: str = FINGERPRINT,
    trace_id: str = "trace_001",
) -> tbm.SQLiteGateSessionCreateResult:
    return repository.create_or_get(
        session_id=session_id,
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id=trace_id,
        run_id="run_001",
        request_fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        expires_in_seconds=600,
    )


def _prepared(
    repository: tbm.SQLiteGateSessionRepository,
) -> tbm.GateSession:
    _create(repository)
    return repository.transition(
        "session_001",
        "prepared",
        expected_version=1,
        lease_seconds=120,
        retrieval_snapshot_id="retrieval_001",
        system_gate_evaluation_id="system_gate_001",
    )


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-gate-session.sql"
        ).decode("utf-8")
    )


def test_sqlite_gate_session_create_replay_get_and_history():
    clock = Clock(
        [
            NOW,
            "2026-07-27T00:00:01Z",
            "2026-07-27T00:00:02Z",
        ]
    )
    with tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=clock,
    ) as repository:
        created = _create(repository)
        replayed = _create(repository, session_id="ignored_on_replay")

        assert created.inserted is True
        assert replayed.inserted is False
        assert replayed.session == created.session
        assert repository.get("session_001") == created.session
        assert repository.history("session_001") == (created.session,)
        assert created.session.created_at == NOW
        assert created.session.expires_at == "2026-07-27T00:10:00Z"
        with pytest.raises(FrozenInstanceError):
            created.inserted = False  # type: ignore[misc]


def test_sqlite_gate_session_default_clock_has_transition_precision():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
    )
    created = _create(repository).session
    prepared = repository.transition(
        created.session_id,
        "prepared",
        expected_version=1,
        lease_seconds=120,
        retrieval_snapshot_id="retrieval",
        system_gate_evaluation_id="system",
    )
    assert prepared.updated_at > created.updated_at
    repository.close()


def test_sqlite_gate_session_rejects_backwards_trusted_clock():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock([NOW, "2026-07-26T23:59:59Z"]),
    )
    _create(repository)
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="moved backwards",
    ) as error:
        repository.transition(
            "session_001",
            "canceled",
            expected_version=1,
            terminal_reason="cancel",
        )
    assert error.value.code == "TBM_SQLITE_GATE_SESSION_CLOCK"
    repository.close()


def test_sqlite_gate_session_idempotency_scope_and_conflicts():
    clock = Clock(
        [
            NOW,
            "2026-07-27T00:00:01Z",
            "2026-07-27T00:00:02Z",
            "2026-07-27T00:00:03Z",
        ]
    )
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=clock,
    )
    _create(repository)

    with pytest.raises(
        tbm.SQLiteGateSessionConflictError,
        match="another request",
    ) as fingerprint_conflict:
        _create(
            repository,
            session_id="session_002",
            fingerprint="sha256:" + "b" * 64,
        )
    assert fingerprint_conflict.value.code == (
        "TBM_SQLITE_GATE_SESSION_IDEMPOTENCY_CONFLICT"
    )

    with pytest.raises(
        tbm.SQLiteGateSessionConflictError,
        match="another request",
    ):
        _create(
            repository,
            session_id="session_002",
            trace_id="trace_other",
        )

    created_other_key = _create(
        repository,
        session_id="session_002",
        idempotency_key="idempotency_002",
    )
    assert created_other_key.inserted is True
    repository.close()


def test_sqlite_gate_session_id_collision_is_distinct_from_key_conflict():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock([NOW, "2026-07-27T00:00:01Z"]),
    )
    _create(repository)

    with pytest.raises(tbm.SQLiteGateSessionConflictError) as conflict:
        _create(
            repository,
            idempotency_key="different_key",
        )
    assert conflict.value.code == "TBM_SQLITE_GATE_SESSION_ID_CONFLICT"
    repository.close()


def test_sqlite_gate_session_full_lifecycle_is_append_only():
    clock = Clock(
        [
            NOW,
            "2026-07-27T00:00:01Z",
            "2026-07-27T00:00:02Z",
            "2026-07-27T00:00:03Z",
            "2026-07-27T00:00:04Z",
            "2026-07-27T00:00:05Z",
            "2026-07-27T00:00:06Z",
        ]
    )
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=clock,
    )
    prepared = _prepared(repository)
    awaiting = repository.transition(
        prepared.session_id,
        "awaiting_decision",
        expected_version=2,
    )
    decided = repository.transition(
        awaiting.session_id,
        "decided",
        expected_version=3,
        semantic_gate_attempt_ids=("attempt_001",),
        decision_id="decision_001",
    )
    finalized = repository.transition(
        decided.session_id,
        "finalized",
        expected_version=4,
        final_memory_revision_ids=("memory_revision_001",),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_001",
    )
    executing = repository.transition(
        finalized.session_id,
        "executing",
        expected_version=5,
    )
    completed = repository.transition(
        executing.session_id,
        "completed",
        expected_version=6,
        run_outcome_id="outcome_001",
    )

    history = repository.history(completed.session_id)
    assert tuple(item.version for item in history) == tuple(range(1, 8))
    assert tuple(item.status for item in history) == (
        "created",
        "prepared",
        "awaiting_decision",
        "decided",
        "finalized",
        "executing",
        "completed",
    )
    assert repository.get(completed.session_id) == completed
    assert completed.lease_expires_at is None
    repository.close()


def test_sqlite_gate_session_stale_transition_rolls_back():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock([NOW, "2026-07-27T00:00:01Z"]),
    )
    _create(repository)

    with pytest.raises(tbm.GateSessionContractError) as stale:
        repository.transition(
            "session_001",
            "prepared",
            expected_version=2,
            lease_seconds=120,
            retrieval_snapshot_id="retrieval_001",
            system_gate_evaluation_id="system_gate_001",
        )
    assert stale.value.code == "TBM_GATE_SESSION_STALE_VERSION"
    assert tuple(item.version for item in repository.history("session_001")) == (
        1,
    )
    repository.close()


def test_sqlite_gate_session_lease_renewal_uses_trusted_clock():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock(
            [
                NOW,
                "2026-07-27T00:00:01Z",
                "2026-07-27T00:01:00Z",
            ]
        ),
    )
    prepared = _prepared(repository)
    renewed = repository.renew_lease(
        prepared.session_id,
        expected_version=2,
        lease_seconds=180,
    )

    assert renewed.version == 3
    assert renewed.updated_at == "2026-07-27T00:01:00Z"
    assert renewed.lease_expires_at == "2026-07-27T00:04:00Z"
    assert len(repository.history(prepared.session_id)) == 3
    repository.close()


def test_sqlite_gate_session_rejects_lease_renewal_at_deadline():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock(
            [
                NOW,
                "2026-07-27T00:00:01Z",
                "2026-07-27T00:02:01Z",
            ]
        ),
    )
    prepared = _prepared(repository)

    with pytest.raises(
        tbm.GateSessionContractError,
        match="before the current lease expires",
    ):
        repository.renew_lease(
            prepared.session_id,
            expected_version=2,
            lease_seconds=180,
        )
    assert repository.get(prepared.session_id) == prepared
    repository.close()


def test_sqlite_gate_session_rejects_transition_after_lease():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock(
            [
                NOW,
                "2026-07-27T00:00:01Z",
                "2026-07-27T00:02:02Z",
            ]
        ),
    )
    prepared = _prepared(repository)

    with pytest.raises(tbm.SQLiteGateSessionConflictError) as expired:
        repository.transition(
            prepared.session_id,
            "canceled",
            expected_version=2,
            terminal_reason="too late",
        )
    assert expired.value.code == "TBM_SQLITE_GATE_SESSION_LEASE_EXPIRED"
    assert repository.get(prepared.session_id) == prepared
    repository.close()


def test_sqlite_gate_session_only_expiry_transition_runs_after_expiry():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock(
            [
                NOW,
                "2026-07-27T00:00:01Z",
                "2026-07-27T00:10:01Z",
                "2026-07-27T00:10:02Z",
            ]
        ),
    )
    prepared = _prepared(repository)
    with pytest.raises(tbm.SQLiteGateSessionConflictError) as expired:
        repository.transition(
            prepared.session_id,
            "canceled",
            expected_version=2,
            terminal_reason="late cancel",
        )
    assert expired.value.code == "TBM_SQLITE_GATE_SESSION_EXPIRED"

    terminal = repository.transition(
        prepared.session_id,
        "expired",
        expected_version=2,
        terminal_reason="session expired",
    )
    assert terminal.status == "expired"
    repository.close()


def test_sqlite_gate_session_lists_only_current_due_sessions():
    clock = Clock(
        [
            NOW,
            "2026-07-27T00:00:01Z",
            "2026-07-27T00:03:00Z",
            "2026-07-27T00:03:00Z",
        ]
    )
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=clock,
    )
    prepared = _prepared(repository)

    assert repository.list_due() == (prepared,)
    assert repository.list_due(limit=1) == (prepared,)
    with pytest.raises(ValueError, match="1 through 10000"):
        repository.list_due(limit=0)
    repository.close()


def test_sqlite_gate_session_created_and_terminal_are_not_due():
    clock = Clock(
        [
            NOW,
            "2026-07-27T00:00:01Z",
            "2026-07-27T00:20:00Z",
        ]
    )
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=clock,
    )
    _create(repository)
    repository.transition(
        "session_001",
        "canceled",
        expected_version=1,
        terminal_reason="caller canceled",
    )
    assert repository.list_due() == ()
    repository.close()


def test_sqlite_gate_session_not_found_and_close_are_stable():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
    )
    with pytest.raises(tbm.SQLiteGateSessionNotFoundError) as missing:
        repository.get("missing")
    assert missing.value.code == "TBM_SQLITE_GATE_SESSION_NOT_FOUND"
    with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
        repository.history("missing")

    repository.close()
    repository.close()
    with pytest.raises(tbm.SQLiteGateSessionPersistenceError) as closed:
        repository.get("missing")
    assert closed.value.code == "TBM_SQLITE_GATE_SESSION_CLOSED"


def test_sqlite_gate_session_missing_transition_and_renew_are_stable():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
    )
    with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
        repository.transition(
            "missing",
            "prepared",
            expected_version=1,
            lease_seconds=10,
            retrieval_snapshot_id="retrieval",
            system_gate_evaluation_id="system",
        )
    with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
        repository.renew_lease(
            "missing",
            expected_version=1,
            lease_seconds=10,
        )
    repository.close()


def test_sqlite_gate_session_get_and_history_require_string_ids():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
    )
    with pytest.raises(ValueError, match="session_id must be"):
        repository.get(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="session_id must be"):
        repository.history(1)  # type: ignore[arg-type]
    repository.close()


def test_sqlite_gate_session_schema_is_side_by_side_with_runtime(tmp_path):
    database = tmp_path / "combined.sqlite3"
    runtime = tbm.SQLiteMemoryRepository.connect(
        database,
        initialize=True,
    )
    runtime.sync(tbm.TraceBackedMemoryStore())
    runtime.close()

    gates = tbm.SQLiteGateSessionRepository.connect(
        database,
        initialize=True,
        clock=Clock([NOW]),
    )
    _create(gates)
    gates.close()

    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT schema_version FROM trace_backed_memory_schema"
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT schema_version "
        "FROM trace_backed_memory_v3_gate_session_schema"
    ).fetchone() == (1,)
    connection.close()

    runtime = tbm.SQLiteMemoryRepository.connect(database)
    assert runtime.load().to_snapshot() == (
        tbm.TraceBackedMemoryStore().to_snapshot()
    )
    runtime.close()


def test_sqlite_gate_session_schema_missing_version_and_drift_fail_closed():
    missing = tbm.SQLiteGateSessionRepository(sqlite3.connect(":memory:"))
    with pytest.raises(tbm.SQLiteGateSessionSchemaError):
        missing.get("session")

    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE trace_backed_memory_v3_gate_session_schema "
        "SET schema_version = 99"
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    mismatched = tbm.SQLiteGateSessionRepository(connection)
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="metadata does not match",
    ):
        mismatched.get("session")

    drifted_connection = sqlite3.connect(":memory:")
    _initialize(drifted_connection)
    drifted_connection.execute(
        "DROP TRIGGER gate_session_revisions_immutable_update"
    )
    drifted = tbm.SQLiteGateSessionRepository(drifted_connection)
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="missing or incomplete",
    ):
        drifted.get("session")

    no_metadata = sqlite3.connect(":memory:")
    _initialize(no_metadata)
    no_metadata.execute("PRAGMA foreign_keys = OFF")
    no_metadata.execute(
        "DELETE FROM trace_backed_memory_v3_gate_session_schema"
    )
    no_metadata.commit()
    missing_metadata = tbm.SQLiteGateSessionRepository(no_metadata)
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="metadata must contain one row",
    ):
        missing_metadata.get("session")
    missing._connection.close()
    connection.close()
    drifted_connection.close()
    no_metadata.close()


def test_sqlite_gate_session_schema_error_is_mapped_for_due_query():
    repository = tbm.SQLiteGateSessionRepository(sqlite3.connect(":memory:"))
    with pytest.raises(tbm.SQLiteGateSessionSchemaError) as error:
        repository.list_due()
    assert error.value.code == "TBM_SQLITE_GATE_SESSION_SCHEMA"
    repository._connection.close()


def test_sqlite_gate_session_schema_definition_mismatch_is_detected():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute("DROP INDEX gate_session_revisions_due")
    connection.execute(
        "CREATE INDEX gate_session_revisions_due "
        "ON gate_session_revisions (session_id)"
    )
    repository = tbm.SQLiteGateSessionRepository(connection)
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="definitions do not match",
    ):
        repository.get("session")
    connection.close()


def test_sqlite_gate_session_extra_trigger_is_schema_drift():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute(
        "CREATE TRIGGER extra_gate_trigger "
        "BEFORE INSERT ON gate_session_heads BEGIN SELECT 1; END"
    )
    repository = tbm.SQLiteGateSessionRepository(connection)
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="unexpected object",
    ):
        repository.get("session")
    connection.close()


def test_sqlite_gate_session_extra_metadata_index_is_schema_drift():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute(
        "CREATE INDEX extra_gate_metadata_index "
        "ON trace_backed_memory_v3_gate_session_schema (contract_version)"
    )
    repository = tbm.SQLiteGateSessionRepository(connection)
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="unexpected object",
    ):
        repository.get("session")
    connection.close()


def test_sqlite_gate_session_database_triggers_protect_history_and_identity():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteGateSessionRepository(
        connection,
        clock=Clock([NOW]),
    )
    _create(repository)

    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        connection.execute(
            "UPDATE gate_session_heads SET tenant_id = 'other'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="revision is immutable"):
        connection.execute(
            "UPDATE gate_session_revisions SET status = 'canceled'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="revision is immutable"):
        connection.execute("DELETE FROM gate_session_revisions")
    with pytest.raises(sqlite3.IntegrityError, match="head is immutable"):
        connection.execute("DELETE FROM gate_session_heads")
    with pytest.raises(sqlite3.IntegrityError, match="head is immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO gate_session_heads "
            "SELECT session_id, 'other', repository_id, principal_id, "
            "agent_client_id, trace_id, run_id, request_fingerprint, "
            "idempotency_key, current_version FROM gate_session_heads"
        )
    assert repository.get("session_001").tenant_id == "tenant_001"


def test_sqlite_gate_session_insert_trigger_rejects_illegal_revision():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteGateSessionRepository(
        connection,
        clock=Clock([NOW]),
    )
    created = _create(repository).session
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        connection.execute(
            "INSERT INTO gate_session_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                created.session_id,
                2,
                "created",
                "2026-07-27T00:00:01Z",
                created.expires_at,
                None,
                tbm.dumps_gate_session(created),
            ),
        )
    connection.close()


def test_sqlite_gate_session_head_identity_is_revalidated():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    session = tbm.create_gate_session(
        session_id="session",
        tenant_id="payload_tenant",
        repository_id="repository",
        principal_id="principal",
        agent_client_id="agent",
        trace_id="trace",
        run_id="run",
        request_fingerprint=FINGERPRINT,
        idempotency_key="key",
        created_at=NOW,
        expires_at="2026-07-27T00:10:00Z",
    )
    connection.execute(
        "INSERT INTO gate_session_heads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session.session_id,
            "head_tenant",
            session.repository_id,
            session.principal_id,
            session.agent_client_id,
            session.trace_id,
            session.run_id,
            session.request_fingerprint,
            session.idempotency_key,
            1,
        ),
    )
    connection.execute(
        "INSERT INTO gate_session_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session.session_id,
            session.version,
            session.status,
            session.updated_at,
            session.expires_at,
            session.lease_expires_at,
            tbm.dumps_gate_session(session),
        ),
    )
    connection.commit()
    repository = tbm.SQLiteGateSessionRepository(connection)
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="head identity",
    ):
        repository.get(session.session_id)
    connection.close()


def test_sqlite_gate_session_load_revalidates_payload_and_columns():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteGateSessionRepository(
        connection,
        clock=Clock([NOW]),
    )
    created = _create(repository).session
    connection.execute(
        "INSERT INTO gate_session_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            created.session_id,
            2,
            "canceled",
            "2026-07-27T00:00:01Z",
            created.expires_at,
            None,
            tbm.dumps_gate_session(created),
        ),
    )
    connection.execute(
        "UPDATE gate_session_heads SET current_version = 2 "
        "WHERE session_id = ?",
        (created.session_id,),
    )
    connection.commit()

    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="columns do not match",
    ):
        repository.get(created.session_id)
    connection.close()


def test_sqlite_gate_session_load_rejects_invalid_payload():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteGateSessionRepository(
        connection,
        clock=Clock([NOW]),
    )
    created = _create(repository).session
    connection.execute(
        "INSERT INTO gate_session_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            created.session_id,
            2,
            "canceled",
            "2026-07-27T00:00:01Z",
            created.expires_at,
            None,
            "{}",
        ),
    )
    connection.execute(
        "UPDATE gate_session_heads SET current_version = 2 "
        "WHERE session_id = ?",
        (created.session_id,),
    )
    connection.commit()

    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="failed contract validation",
    ):
        repository.get(created.session_id)
    connection.close()


def test_sqlite_gate_session_internal_row_shape_and_schema_sql_fail_closed():
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="invalid shape",
    ):
        tbm.SQLiteGateSessionRepository._session_from_row(("short",))
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="invalid definition",
    ):
        gate_sqlite._normalized_schema_sql(None)


def test_sqlite_gate_session_head_cas_rejects_stale_cursor_update():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteGateSessionRepository(
        connection,
        clock=Clock([NOW]),
    )
    current = _create(repository).session
    next_session = tbm.transition_gate_session(
        current,
        "canceled",
        expected_version=1,
        updated_at="2026-07-27T00:00:01Z",
        terminal_reason="canceled",
    )
    cursor = connection.cursor()
    with pytest.raises(tbm.GateSessionContractError) as stale:
        repository._append_revision(
            cursor,
            current,
            next_session,
            expected_version=0,
        )
    assert stale.value.code == "TBM_GATE_SESSION_STALE_VERSION"
    connection.rollback()
    connection.close()


def test_sqlite_gate_session_uses_savepoint_in_caller_transaction():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute("CREATE TABLE caller_state (value TEXT)")
    connection.execute("INSERT INTO caller_state VALUES ('before')")
    repository = tbm.SQLiteGateSessionRepository(
        connection,
        clock=Clock([NOW, "2026-07-27T00:00:01Z"]),
    )

    _create(repository)
    with pytest.raises(tbm.GateSessionContractError):
        repository.transition(
            "session_001",
            "completed",
            expected_version=1,
        )

    assert connection.in_transaction is True
    assert connection.execute(
        "SELECT value FROM caller_state"
    ).fetchall() == [("before",)]
    assert repository.get("session_001").version == 1
    connection.rollback()
    with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
        repository.get("session_001")
    connection.close()


def test_sqlite_gate_session_outer_rollback_removes_successful_write():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    connection.execute("CREATE TABLE caller_state (value TEXT)")
    connection.execute("INSERT INTO caller_state VALUES ('before')")
    repository = tbm.SQLiteGateSessionRepository(
        connection,
        clock=Clock([NOW]),
    )
    _create(repository)
    assert connection.in_transaction is True
    connection.rollback()

    with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
        repository.get("session_001")
    connection.close()


def test_sqlite_gate_session_concurrent_cas_has_one_winner(tmp_path):
    database = tmp_path / "gate.sqlite3"
    seed = tbm.SQLiteGateSessionRepository.connect(
        database,
        initialize=True,
        clock=Clock([NOW]),
    )
    _create(seed)
    seed.close()

    barrier = Barrier(2)
    successes: list[tbm.GateSession] = []
    failures: list[BaseException] = []

    def transition() -> None:
        repository = tbm.SQLiteGateSessionRepository.connect(
            database,
            timeout=5,
            clock=Clock(["2026-07-27T00:00:01Z"]),
        )
        barrier.wait()
        try:
            successes.append(
                repository.transition(
                    "session_001",
                    "prepared",
                    expected_version=1,
                    lease_seconds=120,
                    retrieval_snapshot_id="retrieval_001",
                    system_gate_evaluation_id="system_gate_001",
                )
            )
        except BaseException as error:
            failures.append(error)
        finally:
            repository.close()

    first = Thread(target=transition)
    second = Thread(target=transition)
    first.start()
    second.start()
    first.join()
    second.join()

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], tbm.GateSessionContractError)
    assert failures[0].code == "TBM_GATE_SESSION_STALE_VERSION"


def test_sqlite_gate_session_external_writer_lock_is_persistence_error(
    tmp_path,
):
    database = tmp_path / "locked.sqlite3"
    repository = tbm.SQLiteGateSessionRepository.connect(
        database,
        initialize=True,
        timeout=0,
        clock=Clock([NOW]),
    )
    blocker = sqlite3.connect(database, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")

    with pytest.raises(tbm.SQLiteGateSessionPersistenceError) as locked:
        _create(repository)
    assert locked.value.code == "TBM_SQLITE_GATE_SESSION_PERSISTENCE"

    blocker.rollback()
    blocker.close()
    repository.close()


def test_sqlite_gate_session_payload_limit_fails_before_commit(monkeypatch):
    monkeypatch.setattr(gate_sqlite, "GATE_SESSION_MAX_BYTES", 10)
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock([NOW]),
    )
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="storage limit",
    ):
        _create(repository)
    with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
        repository.get("session_001")
    repository.close()


@pytest.mark.parametrize(
    "lease_seconds",
    [True, 0, tbm.GATE_SESSION_MAX_LEASE_SECONDS + 1],
)
def test_sqlite_gate_session_rejects_invalid_lease_duration(lease_seconds):
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock([NOW, "2026-07-27T00:00:01Z"]),
    )
    _create(repository)
    with pytest.raises(ValueError, match="seconds must be"):
        repository.transition(
            "session_001",
            "prepared",
            expected_version=1,
            lease_seconds=lease_seconds,
            retrieval_snapshot_id="retrieval",
            system_gate_evaluation_id="system",
        )
    repository.close()


@pytest.mark.parametrize(
    "expires_in_seconds",
    [True, 0, tbm.GATE_SESSION_MAX_TTL_SECONDS + 1],
)
def test_sqlite_gate_session_rejects_invalid_ttl(expires_in_seconds):
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=Clock([NOW]),
    )
    with pytest.raises(ValueError, match="seconds must be"):
        repository.create_or_get(
            session_id="session",
            tenant_id="tenant",
            repository_id="repository",
            principal_id="principal",
            agent_client_id="agent",
            trace_id="trace",
            run_id="run",
            request_fingerprint=FINGERPRINT,
            idempotency_key="key",
            expires_in_seconds=expires_in_seconds,
        )
    repository.close()


def test_sqlite_gate_session_rejects_invalid_clock_and_constructor():
    with pytest.raises(ValueError, match="connection"):
        tbm.SQLiteGateSessionRepository(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="clock"):
        invalid_clock_connection = sqlite3.connect(":memory:")
        try:
            tbm.SQLiteGateSessionRepository(  # type: ignore[arg-type]
                invalid_clock_connection,
                clock=None,
            )
        finally:
            invalid_clock_connection.close()
    with pytest.raises(ValueError, match="initialize"):
        tbm.SQLiteGateSessionRepository.connect(  # type: ignore[arg-type]
            ":memory:",
            initialize=1,
        )
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="failed to connect",
    ):
        tbm.SQLiteGateSessionRepository.connect(object())  # type: ignore[arg-type]

    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
        clock=lambda: "not-a-time",
    )
    with pytest.raises(tbm.SQLiteGateSessionPersistenceError) as invalid:
        _create(repository)
    assert invalid.value.code == "TBM_SQLITE_GATE_SESSION_CLOCK"
    repository.close()


def test_sqlite_gate_session_connect_wraps_resource_failure(monkeypatch):
    def fail_resource(_name):
        raise OSError("resource unavailable")

    monkeypatch.setattr(gate_sqlite, "read_packaged_resource", fail_resource)
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="failed to connect",
    ):
        tbm.SQLiteGateSessionRepository.connect(
            ":memory:",
            initialize=True,
        )


def test_sqlite_gate_session_closed_borrowed_connection_is_detected():
    connection = sqlite3.connect(":memory:")
    repository = tbm.SQLiteGateSessionRepository(connection)
    connection.close()
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="closed",
    ):
        repository.get("session")


def test_sqlite_gate_session_constructor_enforces_foreign_keys():
    connection = sqlite3.connect(":memory:")
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (0,)
    assert connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
    repository = tbm.SQLiteGateSessionRepository(connection)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA recursive_triggers").fetchone() == (1,)
    repository.close()
    connection.close()

    disabled = sqlite3.connect(":memory:")
    disabled.execute("CREATE TABLE outer_state (value TEXT)")
    disabled.execute("INSERT INTO outer_state VALUES ('open transaction')")
    with pytest.raises(
        tbm.SQLiteGateSessionPersistenceError,
        match="requires foreign keys",
    ) as error:
        tbm.SQLiteGateSessionRepository(disabled)
    assert error.value.code == "TBM_SQLITE_GATE_SESSION_FOREIGN_KEYS"
    disabled.rollback()
    disabled.close()


def test_sqlite_gate_session_detects_foreign_keys_disabled_after_init():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteGateSessionRepository(connection)
    connection.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="foreign keys are disabled",
    ):
        repository.get("session")
    connection.close()


def test_sqlite_gate_session_detects_recursive_triggers_disabled_after_init():
    connection = sqlite3.connect(":memory:")
    _initialize(connection)
    repository = tbm.SQLiteGateSessionRepository(connection)
    connection.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(
        tbm.SQLiteGateSessionSchemaError,
        match="recursive triggers are disabled",
    ):
        repository.get("session")
    connection.close()


def test_sqlite_gate_session_context_manager_closes_owned_connection():
    repository = tbm.SQLiteGateSessionRepository.connect(
        ":memory:",
        initialize=True,
    )
    with repository as entered:
        assert entered is repository
    with pytest.raises(tbm.SQLiteGateSessionPersistenceError):
        repository.get("session")


def test_sqlite_gate_session_public_exports_and_resource():
    assert tbm.SQLITE_GATE_SESSION_SCHEMA_VERSION == 1
    assert (
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-gate-session.sql"
        )
        == Path("schemas/sqlite-v3-gate-session.sql").read_bytes()
    )
    for name in (
        "SQLiteGateSessionRepository",
        "SQLiteGateSessionCreateResult",
        "SQLiteGateSessionConflictError",
        "SQLiteGateSessionNotFoundError",
        "SQLiteGateSessionPersistenceError",
        "SQLiteGateSessionSchemaError",
    ):
        assert name in tbm.__all__
