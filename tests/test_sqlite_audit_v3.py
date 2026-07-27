from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
from trace_backed_memory import sqlite_audit_v3
from trace_backed_memory.audit_v3 import (
    AuditEvent,
    AuditReference,
    RecoveryAction,
    build_audit_event,
    build_recovery_action,
    dumps_audit_event,
    dumps_recovery_action,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
NOW = "2026-07-27T00:00:00Z"


def _event(
    *,
    parent: AuditEvent | None = None,
    stream_id: str = "audit_stream_001",
    tenant_id: str = "tenant_001",
    repository_id: str = "repository_001",
    session_id: str = "gate_session_001",
    trace_id: str = "trace_001",
    run_id: str = "run_001",
    actor_type: str = "service",
    actor_id: str = "tbmd",
    event_type: str = "session_created",
    reason_code: str = "SESSION_CREATED",
    payload_sha256: str = DIGEST_A,
    references: tuple[AuditReference, ...] = (),
    occurred_at: str = NOW,
) -> AuditEvent:
    return build_audit_event(
        stream_id=stream_id,
        sequence=1 if parent is None else parent.sequence + 1,
        previous_event_id=None if parent is None else parent.event_id,
        tenant_id=tenant_id,
        repository_id=repository_id,
        session_id=session_id,
        trace_id=trace_id,
        run_id=run_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        reason_code=reason_code,
        payload_sha256=payload_sha256,
        references=references,
        occurred_at=occurred_at,
    )


def _recovery(
    *,
    action: str = "recover",
    request_sha256: str = DIGEST_A,
) -> RecoveryAction:
    return build_recovery_action(
        target_kind="memory_run",
        action=action,
        result="succeeded",
        session_id="gate_session_001",
        trace_id="trace_001",
        run_id="run_001",
        usage_decision_id="usage_decision_001",
        expected_session_version=None,
        expected_memory_run_status="trace_only",
        memory_caused_failure=None,
        request_sha256=request_sha256,
        requested_by_principal_id="principal_001",
        executor_id="recovery_worker",
        error_code=None,
        started_at="2026-07-27T00:10:00Z",
        finished_at="2026-07-27T00:10:01Z",
    )


def _recovery_event(
    recovery: RecoveryAction,
    *,
    parent: AuditEvent | None = None,
    occurred_at: str = "2026-07-27T00:10:01Z",
) -> AuditEvent:
    return _event(
        parent=parent,
        actor_type="worker",
        actor_id=recovery.executor_id,
        event_type="recovery_succeeded",
        reason_code="RECOVERY_COMPLETED",
        payload_sha256=recovery.request_sha256,
        references=(
            AuditReference(
                "recovery_action",
                recovery.recovery_action_id,
            ),
        ),
        occurred_at=occurred_at,
    )


def _insert_direct_event(
    repository: tbm.SQLiteAuditV3Repository,
    event: AuditEvent,
) -> None:
    repository._connection.execute(
        "INSERT INTO v3_audit_stream_heads ("
        "stream_id, tenant_id, repository_id, session_id, trace_id, run_id, "
        "current_sequence, current_event_id"
        ") VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
        (
            event.stream_id,
            event.tenant_id,
            event.repository_id,
            event.session_id,
            event.trace_id,
            event.run_id,
        ),
    )
    repository._connection.execute(
        "INSERT INTO v3_audit_events ("
        "event_id, stream_id, sequence, previous_event_id, tenant_id, "
        "repository_id, session_id, trace_id, run_id, actor_type, actor_id, "
        "event_type, recovery_action_id, reason_code, payload_sha256, "
        "occurred_at, descriptor"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        repository._event_row(event),
    )
    repository._connection.execute(
        "UPDATE v3_audit_stream_heads "
        "SET current_sequence = ?, current_event_id = ? "
        "WHERE stream_id = ?",
        (event.sequence, event.event_id, event.stream_id),
    )


def test_sqlite_audit_v3_event_round_trip_chain_and_pagination():
    with tbm.SQLiteAuditV3Repository.connect(
        initialize=True
    ) as repository:
        first = _event()
        second = _event(
            parent=first,
            event_type="authorization_evaluated",
            reason_code="AUTHORIZED",
            occurred_at="2026-07-27T00:00:01Z",
        )
        assert repository.append_event(first) is True
        assert repository.append_event(first) is False
        assert repository.append_event(second) is True
        assert repository.load_event(first.event_id) == first
        assert repository.list_events(first.stream_id) == (first, second)
        assert repository.list_events(
            first.stream_id,
            after_sequence=1,
            limit=1,
        ) == (second,)
        assert repository.list_events("unknown") == ()
        assert repository.stream_head(first.stream_id) == tbm.AuditStreamHead(
            stream_id=first.stream_id,
            tenant_id=first.tenant_id,
            repository_id=first.repository_id,
            session_id=first.session_id,
            trace_id=first.trace_id,
            run_id=first.run_id,
            current_sequence=2,
            current_event_id=second.event_id,
        )
        assert repository.stream_head("unknown") is None
        with pytest.raises(KeyError):
            repository.load_event("audit_event_sha256_" + "f" * 64)
    assert tbm.SQLITE_AUDIT_V3_SCHEMA_VERSION == 1
    assert tbm.SQLITE_AUDIT_V3_MAX_PAGE_SIZE == 1000
    for name in (
        "AuditStreamHead",
        "SQLiteAuditV3AppendResult",
        "SQLiteAuditV3ConflictError",
        "SQLiteAuditV3Error",
        "SQLiteAuditV3PersistenceError",
        "SQLiteAuditV3Repository",
        "SQLiteAuditV3SchemaError",
    ):
        assert name in tbm.__all__


def test_sqlite_audit_v3_rejects_forks_gaps_and_identity_changes():
    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    first = _event()
    repository.append_event(first)
    stale = _event(
        parent=first,
        event_type="authorization_evaluated",
        reason_code="AUTHORIZED",
        occurred_at="2026-07-27T00:00:01Z",
    )
    repository.append_event(stale)
    fork = _event(
        parent=first,
        event_type="retrieval_recorded",
        reason_code="RETRIEVED",
        occurred_at="2026-07-27T00:00:02Z",
    )
    with pytest.raises(
        tbm.SQLiteAuditV3ConflictError,
        match="current stream",
    ):
        repository.append_event(fork)
    wrong_identity = _event(
        parent=stale,
        repository_id="repository_other",
        event_type="retrieval_recorded",
        reason_code="RETRIEVED",
        occurred_at="2026-07-27T00:00:02Z",
    )
    with pytest.raises(
        tbm.SQLiteAuditV3ConflictError,
        match="identity differs",
    ):
        repository.append_event(wrong_identity)
    non_first = _event(
        parent=stale,
        stream_id="audit_stream_other",
        occurred_at="2026-07-27T00:00:02Z",
    )
    with pytest.raises(
        tbm.SQLiteAuditV3ConflictError,
        match="sequence one",
    ):
        repository.append_event(non_first)
    repository.close()


def test_sqlite_audit_v3_recovery_is_atomic_idempotent_and_linked():
    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    recovery = _recovery()
    event = _recovery_event(recovery)
    result = repository.append_recovery(recovery, event)
    assert result == tbm.SQLiteAuditV3AppendResult(
        event_id=event.event_id,
        event_inserted=True,
        recovery_action_id=recovery.recovery_action_id,
        recovery_inserted=True,
    )
    replayed = repository.append_recovery(recovery, event)
    assert replayed.event_inserted is False
    assert replayed.recovery_inserted is False
    assert repository.load_recovery(recovery.recovery_action_id) == (
        recovery,
        event,
    )
    duplicate_event = _recovery_event(
        recovery,
        parent=event,
        occurred_at="2026-07-27T00:10:02Z",
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
        repository._connection.execute(
            "INSERT INTO v3_audit_events ("
            "event_id, stream_id, sequence, previous_event_id, tenant_id, "
            "repository_id, session_id, trace_id, run_id, actor_type, "
            "actor_id, event_type, recovery_action_id, reason_code, "
            "payload_sha256, occurred_at, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            repository._event_row(duplicate_event),
        )
    repository._connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        repository._connection.execute(
            "INSERT OR REPLACE INTO v3_recovery_actions ("
            "recovery_action_id, event_id, session_id, request_sha256, "
            "descriptor) VALUES (?, ?, ?, ?, ?)",
            repository._recovery_row(recovery, event.event_id),
        )
    repository._connection.rollback()
    with pytest.raises(KeyError):
        repository.load_recovery(
            "recovery_action_sha256_" + "f" * 64
        )

    second_recovery = _recovery(request_sha256=DIGEST_B)
    bad_actor = _event(
        actor_type="worker",
        actor_id="other_worker",
        event_type="recovery_succeeded",
        reason_code="RECOVERY_COMPLETED",
        payload_sha256=second_recovery.request_sha256,
        references=(
            AuditReference(
                "recovery_action",
                second_recovery.recovery_action_id,
            ),
        ),
        occurred_at="2026-07-27T00:10:01Z",
    )
    with pytest.raises(
        tbm.SQLiteAuditV3ConflictError,
        match="linkage differs",
    ):
        repository.append_recovery(
            second_recovery,
            bad_actor,
        )
    with pytest.raises(ValueError, match="append_recovery"):
        repository.append_event(event)
    repository.close()


def test_sqlite_audit_v3_schema_requires_exact_recovery_event_pair():
    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    ordinary_event = _event()
    repository.append_event(ordinary_event)
    recovery = _recovery()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint"):
        repository._connection.execute(
            "INSERT INTO v3_recovery_actions ("
            "recovery_action_id, event_id, session_id, request_sha256, "
            "descriptor) VALUES (?, ?, ?, ?, ?)",
            repository._recovery_row(recovery, ordinary_event.event_id),
        )
        repository._connection.commit()
    repository._connection.rollback()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint"):
        repository._connection.execute(
            "INSERT INTO v3_recovery_actions ("
            "recovery_action_id, event_id, session_id, request_sha256, "
            "descriptor) VALUES (?, ?, ?, ?, ?)",
            repository._recovery_row(
                recovery,
                "audit_event_sha256_" + "f" * 64,
            ),
        )
        repository._connection.commit()
    repository._connection.rollback()

    repository.close()
    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    recovery_event = _recovery_event(recovery)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint"):
        _insert_direct_event(repository, recovery_event)
        repository._connection.commit()
    repository._connection.rollback()

    mismatched_recovery = _recovery(
        action="investigate",
        request_sha256=DIGEST_B,
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint"):
        _insert_direct_event(repository, recovery_event)
        repository._connection.execute(
            "INSERT INTO v3_recovery_actions ("
            "recovery_action_id, event_id, session_id, request_sha256, "
            "descriptor) VALUES (?, ?, ?, ?, ?)",
            repository._recovery_row(
                mismatched_recovery,
                recovery_event.event_id,
            ),
        )
        repository._connection.commit()
    repository._connection.rollback()
    repository.close()


def test_sqlite_audit_v3_request_hash_collision_rolls_back_event():
    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    first_recovery = _recovery()
    first_event = _recovery_event(first_recovery)
    repository.append_recovery(first_recovery, first_event)
    second_recovery = _recovery(action="investigate")
    second_event = _recovery_event(
        second_recovery,
        parent=first_event,
        occurred_at="2026-07-27T00:10:02Z",
    )
    with pytest.raises(tbm.SQLiteAuditV3ConflictError):
        repository.append_recovery(second_recovery, second_event)
    assert repository.stream_head(first_event.stream_id).current_event_id == (
        first_event.event_id
    )
    with pytest.raises(KeyError):
        repository.load_event(second_event.event_id)
    repository.close()


def test_sqlite_audit_v3_uses_caller_savepoint_and_outer_rollback():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-audit.sql").decode()
    )
    repository = tbm.SQLiteAuditV3Repository(connection)
    connection.execute("BEGIN")
    connection.execute("CREATE TABLE caller_work (value INTEGER)")
    connection.execute("INSERT INTO caller_work VALUES (1)")
    event = _event()
    assert repository.append_event(event) is True
    with pytest.raises(tbm.SQLiteAuditV3ConflictError):
        repository.append_event(
            _event(
                parent=event,
                repository_id="repository_other",
                occurred_at="2026-07-27T00:00:01Z",
            )
        )
    assert connection.execute(
        "SELECT value FROM caller_work"
    ).fetchone() == (1,)
    connection.rollback()
    with pytest.raises(KeyError):
        repository.load_event(event.event_id)
    connection.close()


def test_sqlite_audit_v3_schema_blocks_mutation_and_direct_forks():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-audit.sql").decode()
    )
    repository = tbm.SQLiteAuditV3Repository(connection)
    event = _event()
    repository.append_event(event)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE v3_audit_events SET reason_code = 'CHANGED'"
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="not the next stream event|cannot be deleted",
    ):
        connection.execute(
            "INSERT OR REPLACE INTO v3_audit_events ("
            "event_id, stream_id, sequence, previous_event_id, tenant_id, "
            "repository_id, session_id, trace_id, run_id, actor_type, "
            "actor_id, event_type, recovery_action_id, reason_code, "
            "payload_sha256, occurred_at, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            repository._event_row(event),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        connection.execute("DELETE FROM v3_audit_events")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        connection.execute("DELETE FROM v3_audit_stream_heads")
    head_row = connection.execute(
        "SELECT stream_id, tenant_id, repository_id, session_id, trace_id, "
        "run_id, current_sequence, current_event_id "
        "FROM v3_audit_stream_heads"
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        connection.execute(
            "INSERT OR REPLACE INTO v3_audit_stream_heads ("
            "stream_id, tenant_id, repository_id, session_id, trace_id, "
            "run_id, current_sequence, current_event_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*head_row[:6], 0, None),
        )
    child = _event(
        parent=event,
        occurred_at="2026-07-27T00:00:01Z",
    )
    direct_row = list(repository._event_row(child))
    direct_row[2] = child.sequence + 1
    del direct_row[12]
    with pytest.raises(sqlite3.IntegrityError, match="next stream event"):
        connection.execute(
            "INSERT INTO v3_audit_events ("
            "event_id, stream_id, sequence, previous_event_id, tenant_id, "
            "repository_id, session_id, trace_id, run_id, actor_type, "
            "actor_id, event_type, reason_code, payload_sha256, occurred_at, "
            "descriptor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            tuple(direct_row),
        )
    connection.rollback()
    connection.execute(
        "DROP INDEX v3_audit_events_type"
    )
    with pytest.raises(tbm.SQLiteAuditV3SchemaError):
        repository.load_event(event.event_id)
    connection.execute(
        "CREATE INDEX v3_audit_events_type "
        "ON v3_audit_events(reason_code, occurred_at)"
    )
    with pytest.raises(
        tbm.SQLiteAuditV3SchemaError,
        match="definitions do not match",
    ):
        repository.load_event(event.event_id)
    connection.close()


def test_sqlite_audit_v3_concurrent_identical_append_is_idempotent(
    tmp_path: Path,
):
    database = tmp_path / "audit.sqlite3"
    tbm.SQLiteAuditV3Repository.connect(
        database,
        initialize=True,
    ).close()
    event = _event()

    def append_once() -> bool:
        with tbm.SQLiteAuditV3Repository.connect(
            database,
            timeout=5,
        ) as repository:
            return repository.append_event(event)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: append_once(), range(2)))
    assert sorted(results) == [False, True]


def test_sqlite_audit_v3_validation_missing_schema_and_closed_state():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = tbm.SQLiteAuditV3Repository(connection)
    with pytest.raises(tbm.SQLiteAuditV3SchemaError, match="missing"):
        repository.append_event(_event())
    connection.close()

    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    with pytest.raises(ValueError, match="event"):
        repository.append_event(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact audit records"):
        repository.append_recovery(
            object(),  # type: ignore[arg-type]
            _event(),
        )
    with pytest.raises(ValueError, match="event_id"):
        repository.load_event("bad")
    with pytest.raises(ValueError, match="recovery_action_id"):
        repository.load_recovery("bad")
    with pytest.raises(ValueError, match="stream_id"):
        repository.stream_head("")
    with pytest.raises(ValueError, match="after_sequence"):
        repository.list_events("stream", after_sequence=-1)
    with pytest.raises(ValueError, match="limit"):
        repository.list_events("stream", limit=0)
    repository.close()
    repository.close()
    with pytest.raises(tbm.SQLiteAuditV3Error, match="closed"):
        repository.stream_head("stream")


def test_sqlite_audit_v3_rejects_disabled_recursive_triggers_and_orphan_heads():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-audit.sql").decode()
    )
    connection.execute("PRAGMA recursive_triggers = OFF")
    repository = tbm.SQLiteAuditV3Repository(connection)
    with pytest.raises(
        tbm.SQLiteAuditV3SchemaError,
        match="recursive triggers",
    ):
        repository.append_event(_event())
    connection.execute("PRAGMA recursive_triggers = ON")
    with pytest.raises(sqlite3.IntegrityError, match="begin empty"):
        connection.execute(
            "INSERT INTO v3_audit_stream_heads ("
            "stream_id, tenant_id, repository_id, session_id, trace_id, "
            "run_id, current_sequence, current_event_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "orphan_stream",
                "tenant_001",
                "repository_001",
                "gate_session_001",
                "trace_001",
                "run_001",
                1,
                "audit_event_sha256_" + "f" * 64,
            ),
        )
    connection.execute(
        "INSERT INTO v3_audit_stream_heads ("
        "stream_id, tenant_id, repository_id, session_id, trace_id, run_id, "
        "current_sequence, current_event_id"
        ") VALUES ('empty_stream', 'tenant_001', 'repository_001', "
        "'gate_session_001', 'trace_001', 'run_001', 0, NULL)"
    )
    connection.commit()
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="invalid shape",
    ):
        repository.stream_head("empty_stream")
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="identity",
    ):
        tbm.SQLiteAuditV3Repository._head_from_row(
            (
                "stream_001",
                " malformed ",
                "repository_001",
                "gate_session_001",
                "trace_001",
                "run_001",
                1,
                "audit_event_sha256_" + "f" * 64,
            )
        )
    connection.close()


def test_sqlite_audit_v3_stored_rows_are_revalidated_exactly():
    event = _event()
    event_row = tbm.SQLiteAuditV3Repository._event_row(event)
    assert tbm.SQLiteAuditV3Repository._stored_event(event_row) == event
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="columns",
    ):
        tbm.SQLiteAuditV3Repository._stored_event(
            (*event_row[:12], "DIFFERENT", *event_row[13:])
        )
    with pytest.raises(tbm.SQLiteAuditV3PersistenceError, match="descriptor"):
        tbm.SQLiteAuditV3Repository._stored_event(
            (*event_row[:16], "{}")
        )
    recovery = _recovery()
    recovery_event = _recovery_event(recovery)
    recovery_row = (
        recovery.recovery_action_id,
        recovery_event.event_id,
        recovery.session_id,
        recovery.request_sha256,
        dumps_recovery_action(recovery),
    )
    assert tbm.SQLiteAuditV3Repository._stored_recovery(recovery_row) == (
        recovery,
        recovery_event.event_id,
    )
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="columns",
    ):
        tbm.SQLiteAuditV3Repository._stored_recovery(
            (*recovery_row[:2], "different", *recovery_row[3:])
        )
    assert dumps_audit_event(event) == event_row[-1]


def test_sqlite_audit_v3_defensive_helpers_and_schema_fail_closed():
    assert sqlite_audit_v3._is_schema_error(
        sqlite3.OperationalError("no such table: missing")
    )
    assert not sqlite_audit_v3._is_schema_error(
        sqlite3.OperationalError("database is locked")
    )
    with pytest.raises(tbm.SQLiteAuditV3SchemaError, match="definition"):
        sqlite_audit_v3._normalized_schema_sql(None)
    with sqlite3.connect(":memory:") as empty:
        with pytest.raises(tbm.SQLiteAuditV3SchemaError, match="missing"):
            sqlite_audit_v3._read_schema_definitions(empty.cursor())
    with pytest.raises(ValueError, match="sqlite3.Connection"):
        tbm.SQLiteAuditV3Repository(object())  # type: ignore[arg-type]
    with pytest.raises(tbm.SQLiteAuditV3PersistenceError, match="connect"):
        tbm.SQLiteAuditV3Repository.connect(
            initialize=True,
            unsupported_argument=True,
        )
    with pytest.raises(ValueError, match="boolean"):
        tbm.SQLiteAuditV3Repository.connect(
            initialize=1,  # type: ignore[arg-type]
        )

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-audit.sql").decode()
    )
    repository = tbm.SQLiteAuditV3Repository(connection)
    repository.close()
    assert connection.execute("SELECT 1").fetchone() == (1,)
    connection.close()
    with pytest.raises(tbm.SQLiteAuditV3Error, match="closed"):
        tbm.SQLiteAuditV3Repository(connection).stream_head("stream")

    foreign_keys_off = sqlite3.connect(":memory:")
    foreign_keys_off.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-audit.sql").decode()
    )
    foreign_keys_off.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(tbm.SQLiteAuditV3SchemaError, match="foreign keys"):
        tbm.SQLiteAuditV3Repository(foreign_keys_off).stream_head("stream")
    foreign_keys_off.close()

    missing_metadata = sqlite3.connect(":memory:")
    missing_metadata.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-audit.sql").decode()
    )
    missing_metadata.execute(
        "DELETE FROM trace_backed_memory_v3_audit_schema"
    )
    missing_metadata.commit()
    with pytest.raises(tbm.SQLiteAuditV3SchemaError, match="metadata"):
        tbm.SQLiteAuditV3Repository(missing_metadata).stream_head("stream")
    missing_metadata.close()


def test_sqlite_audit_v3_canonical_schema_load_failure_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    sqlite_audit_v3._canonical_schema_definitions.cache_clear()
    monkeypatch.setattr(
        sqlite_audit_v3,
        "read_packaged_resource",
        lambda _path: b"\xff",
    )
    try:
        with pytest.raises(
            tbm.SQLiteAuditV3SchemaError,
            match="canonical",
        ):
            sqlite_audit_v3._canonical_schema_definitions()
    finally:
        sqlite_audit_v3._canonical_schema_definitions.cache_clear()


def test_sqlite_audit_v3_public_operations_map_database_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    recovery = _recovery()
    recovery_event = _recovery_event(recovery)

    def fail_schema(_cursor: sqlite3.Cursor) -> None:
        raise sqlite3.OperationalError("synthetic storage failure")

    monkeypatch.setattr(repository, "_require_schema", fail_schema)
    operations = (
        lambda: repository.append_event(_event()),
        lambda: repository.append_recovery(recovery, recovery_event),
        lambda: repository.load_event(recovery_event.event_id),
        lambda: repository.load_recovery(recovery.recovery_action_id),
        lambda: repository.stream_head(recovery_event.stream_id),
        lambda: repository.list_events(recovery_event.stream_id),
    )
    for operation in operations:
        with pytest.raises(
            tbm.SQLiteAuditV3PersistenceError,
            match="failed to",
        ):
            operation()
    repository.close()


def test_sqlite_audit_v3_revalidates_head_and_recovery_linkage(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = tbm.SQLiteAuditV3Repository.connect(initialize=True)
    recovery = _recovery()
    event = _recovery_event(recovery)
    repository.append_recovery(recovery, event)
    monkeypatch.setattr(
        repository,
        "_select_event_by_id",
        lambda _cursor, _event_id: None,
    )
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="head does not match",
    ):
        repository.stream_head(event.stream_id)
    monkeypatch.undo()

    def fail_linkage(
        _recovery_record: RecoveryAction,
        _event_record: AuditEvent,
    ) -> None:
        raise tbm.SQLiteAuditV3ConflictError("synthetic linkage conflict")

    monkeypatch.setattr(repository, "_validate_recovery_event", fail_linkage)
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="linkage failed validation",
    ):
        repository.load_recovery(recovery.recovery_action_id)
    repository.close()


def test_sqlite_audit_v3_defensive_row_shapes_and_error_mapping():
    class MalformedSchemaCursor:
        def execute(
            self,
            _sql: str,
            _parameters: object = None,
        ) -> None:
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return [
                (None, "name", "table", "CREATE TABLE x (value INTEGER)")
            ] * len(sqlite_audit_v3._SCHEMA_OBJECT_NAMES)

    with pytest.raises(tbm.SQLiteAuditV3SchemaError, match="invalid shape"):
        sqlite_audit_v3._read_schema_definitions(
            MalformedSchemaCursor()  # type: ignore[arg-type]
        )
    with pytest.raises(tbm.SQLiteAuditV3PersistenceError, match="event row"):
        tbm.SQLiteAuditV3Repository._stored_event(())
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="recovery action row",
    ):
        tbm.SQLiteAuditV3Repository._stored_recovery(())
    recovery = _recovery()
    event = _recovery_event(recovery)
    recovery_row = (
        recovery.recovery_action_id,
        event.event_id,
        recovery.session_id,
        recovery.request_sha256,
        "{}",
    )
    with pytest.raises(
        tbm.SQLiteAuditV3PersistenceError,
        match="descriptor",
    ):
        tbm.SQLiteAuditV3Repository._stored_recovery(recovery_row)
    for error, expected in (
        (
            sqlite3.OperationalError("no such table: missing"),
            tbm.SQLiteAuditV3SchemaError,
        ),
        (
            sqlite3.IntegrityError("constraint failed"),
            tbm.SQLiteAuditV3ConflictError,
        ),
        (
            sqlite3.OperationalError("database is locked"),
            tbm.SQLiteAuditV3PersistenceError,
        ),
    ):
        with pytest.raises(expected):
            tbm.SQLiteAuditV3Repository._raise_database_error(
                error,
                "synthetic failure",
            )
    with pytest.raises(ValueError, match="event_id"):
        sqlite_audit_v3._validate_event_id(None)
    with pytest.raises(ValueError, match="recovery_action_id"):
        sqlite_audit_v3._validate_recovery_action_id(None)
    with pytest.raises(ValueError, match="bounded identifier"):
        sqlite_audit_v3._validate_identifier(" x ", "value")
