from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_audit_v3 as postgres_audit_v3
from trace_backed_memory.audit_v3 import (
    AuditEvent,
    AuditReference,
    RecoveryAction,
    build_audit_event,
    build_recovery_action,
)
from trace_backed_memory.postgres_audit_v3 import (
    PostgresAuditV3AppendResult,
    PostgresAuditV3ConflictError,
    PostgresAuditV3PersistenceError,
    PostgresAuditV3Repository,
    PostgresAuditV3SchemaError,
)
from tests.postgres_support import PostgresCluster, assert_sql_succeeds


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-audit.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-audit-rollback.sql"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
NOW = "2026-07-28T00:00:00Z"


def test_postgres_audit_v3_public_exports_are_intentional():
    assert tbm.PostgresAuditV3Repository is PostgresAuditV3Repository
    assert tbm.POSTGRES_AUDIT_V3_SCHEMA_VERSION == 1
    assert "PostgresAuditV3Repository" in tbm.__all__


def test_postgres_audit_v3_validates_public_inputs_and_lifecycle():
    with pytest.raises(ValueError, match="connection"):
        PostgresAuditV3Repository(None)

    connection = SimpleNamespace(closed=False, close=lambda: None)
    repository = PostgresAuditV3Repository(connection, owns_connection=True)
    for event_id in (None, "event_001"):
        with pytest.raises(ValueError, match="canonical audit event ID"):
            repository.load_event(event_id)  # type: ignore[arg-type]
    for recovery_id in (None, "recovery_001"):
        with pytest.raises(ValueError, match="canonical recovery action ID"):
            repository.load_recovery(recovery_id)  # type: ignore[arg-type]
    for stream_id in ("", " stream", "stream\nid", "x" * 129):
        with pytest.raises(ValueError, match="bounded identifier"):
            repository.stream_head(stream_id)
    for after_sequence in (-1, True, tbm.AUDIT_MAX_SEQUENCE + 1):
        with pytest.raises(ValueError, match="after_sequence"):
            repository.list_events("stream_001", after_sequence=after_sequence)
    for limit in (0, True, tbm.POSTGRES_AUDIT_V3_MAX_PAGE_SIZE + 1):
        with pytest.raises(ValueError, match="limit"):
            repository.list_events("stream_001", limit=limit)
    with pytest.raises(ValueError, match="exactly AuditEvent"):
        repository.append_event(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact audit records"):
        repository.append_recovery(object(), object())  # type: ignore[arg-type]

    assert repository.__enter__() is repository
    repository.close()
    repository.close()
    with pytest.raises(PostgresAuditV3PersistenceError, match="closed"):
        repository.__enter__()


def test_postgres_audit_v3_revalidates_rows_and_maps_database_errors():
    event = _event()
    event_fields = (
        "event_id",
        "stream_id",
        "sequence",
        "previous_event_id",
        "tenant_id",
        "repository_id",
        "session_id",
        "trace_id",
        "run_id",
        "actor_type",
        "actor_id",
        "event_type",
        "recovery_action_id",
        "reason_code",
        "payload_sha256",
        "occurred_at",
        "descriptor",
    )
    event_row = dict(
        zip(
            event_fields,
            PostgresAuditV3Repository._event_values(event),
            strict=True,
        )
    )
    assert PostgresAuditV3Repository._stored_event(event_row) == event
    for field, value, message in (
        ("descriptor", 1, "invalid shape"),
        ("descriptor", "{", "failed validation"),
        ("reason_code", "CHANGED", "do not match"),
    ):
        corrupt = {**event_row, field: value}
        with pytest.raises(PostgresAuditV3PersistenceError, match=message):
            PostgresAuditV3Repository._stored_event(corrupt)
    with pytest.raises(PostgresAuditV3PersistenceError, match="invalid shape"):
        PostgresAuditV3Repository._stored_event({"event_id": event.event_id})

    recovery = _recovery()
    recovery_event = _recovery_event(recovery)
    recovery_fields = (
        "recovery_action_id",
        "event_id",
        "session_id",
        "trace_id",
        "run_id",
        "result",
        "executor_id",
        "request_sha256",
        "finished_at",
        "descriptor",
    )
    recovery_row = dict(
        zip(
            recovery_fields,
            PostgresAuditV3Repository._recovery_values(
                recovery,
                recovery_event.event_id,
            ),
            strict=True,
        )
    )
    assert PostgresAuditV3Repository._stored_recovery(recovery_row) == (
        recovery,
        recovery_event.event_id,
    )
    for field, value, message in (
        ("event_id", 1, "invalid shape"),
        ("descriptor", "{", "failed validation"),
        ("executor_id", "changed", "do not match"),
    ):
        corrupt = {**recovery_row, field: value}
        with pytest.raises(PostgresAuditV3PersistenceError, match=message):
            PostgresAuditV3Repository._stored_recovery(corrupt)

    for sqlstate, error_type in (
        ("42P01", PostgresAuditV3SchemaError),
        ("23505", PostgresAuditV3ConflictError),
        ("P0001", PostgresAuditV3ConflictError),
        ("08006", PostgresAuditV3PersistenceError),
        (None, PostgresAuditV3PersistenceError),
    ):
        error = RuntimeError("database failure")
        error.sqlstate = sqlstate  # type: ignore[attr-defined]
        with pytest.raises(error_type):
            PostgresAuditV3Repository._raise_database_error(error, "failed")


def test_postgres_audit_v3_rejects_incomplete_canonical_schema_resource(
    monkeypatch: pytest.MonkeyPatch,
):
    postgres_audit_v3._expected_function_bodies.cache_clear()
    monkeypatch.setattr(
        postgres_audit_v3,
        "read_packaged_resource",
        lambda _name: b"CREATE FUNCTION incomplete() RETURNS trigger AS $$BEGIN END$$;",
    )
    with pytest.raises(PostgresAuditV3SchemaError, match="incomplete"):
        postgres_audit_v3._expected_function_bodies()
    postgres_audit_v3._expected_function_bodies.cache_clear()

    def unreadable(_name: str) -> bytes:
        raise tbm.PackagedResourceError(
            "read",
            name="schemas/postgres-v3-audit.sql",
        )

    monkeypatch.setattr(postgres_audit_v3, "read_packaged_resource", unreadable)
    with pytest.raises(PostgresAuditV3SchemaError, match="could not read"):
        postgres_audit_v3._expected_function_bodies()
    postgres_audit_v3._expected_function_bodies.cache_clear()


def test_postgres_audit_v3_maps_connection_and_cursor_shape_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    class DriverError(Exception):
        pass

    class Driver:
        Error = DriverError

        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> object:
            raise DriverError("unavailable")

    monkeypatch.setattr(
        postgres_audit_v3,
        "_load_psycopg",
        lambda: (Driver, object(), object()),
    )
    with pytest.raises(PostgresAuditV3PersistenceError, match="connect"):
        PostgresAuditV3Repository.connect("postgresql://invalid")

    class Cursor:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def execute(self, *_args: object) -> None:
            pass

        def fetchall(self) -> list[object]:
            return self.rows

    repository = PostgresAuditV3Repository(object())
    with pytest.raises(PostgresAuditV3SchemaError):
        repository._names(Cursor([{"name": 1}]), "SELECT")
    with pytest.raises(PostgresAuditV3SchemaError, match="metadata"):
        repository._lock_schema(Cursor([]))
    with pytest.raises(PostgresAuditV3PersistenceError, match="event row"):
        repository._select_event(Cursor([object()]), "audit_event_sha256_" + "a" * 64)
    with pytest.raises(PostgresAuditV3PersistenceError, match="stream head"):
        repository._select_head(Cursor([object()]), "stream_001", for_update=True)


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    installed = cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr


def _repository(cluster: PostgresCluster) -> PostgresAuditV3Repository:
    return PostgresAuditV3Repository.connect(**cluster.connection_kwargs())


def _event(
    *,
    parent: AuditEvent | None = None,
    stream_id: str = "audit_stream_001",
    repository_id: str = "repository_001",
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
        tenant_id="tenant_001",
        repository_id=repository_id,
        session_id="gate_session_001",
        trace_id="trace_001",
        run_id="run_001",
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
        started_at="2026-07-28T00:10:00Z",
        finished_at="2026-07-28T00:10:01Z",
    )


def _recovery_event(
    recovery: RecoveryAction,
    *,
    parent: AuditEvent | None = None,
    occurred_at: str = "2026-07-28T00:10:01Z",
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


def test_postgres_audit_v3_installs_isolated_append_only_schema(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            """
        SELECT schema_version || ':' || contract_version
        FROM trace_backed_memory_v3_audit.schema_metadata
        WHERE singleton
        """,
        )
        == "1:tbm.audit-event.v3"
    )


def test_postgres_audit_v3_event_round_trip_chain_and_pagination(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    with _repository(postgres_cluster) as repository:
        first = _event()
        second = _event(
            parent=first,
            event_type="authorization_evaluated",
            reason_code="AUTHORIZED",
            occurred_at="2026-07-28T00:00:01Z",
        )
        assert repository.append_event(first) is True
        assert repository.append_event(first) is False
        assert repository.append_event(second) is True
        assert repository.load_event(first.event_id) == first
        assert repository.list_events(first.stream_id, limit=1) == (first,)
        assert repository.list_events(
            first.stream_id,
            after_sequence=1,
        ) == (second,)
        head = repository.stream_head(first.stream_id)
        assert head is not None
        assert head.current_sequence == 2
        assert head.current_event_id == second.event_id


def test_postgres_audit_v3_persists_every_non_recovery_event_and_actor_type(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    event_types = (
        "session_created",
        "session_transitioned",
        "authorization_evaluated",
        "retrieval_recorded",
        "system_gate_evaluated",
        "semantic_gate_attempted",
        "decision_finalized",
        "injection_created",
        "execution_completed",
        "outcome_attributed",
        "session_canceled",
        "session_expired",
        "session_abandoned",
    )
    actor_types = ("principal", "service", "worker")
    with _repository(postgres_cluster) as repository:
        for index, event_type in enumerate(event_types):
            event = _event(
                stream_id=f"audit_stream_type_{index}",
                actor_type=actor_types[index % len(actor_types)],
                actor_id=f"actor_{index}",
                event_type=event_type,
                reason_code=f"EVENT_{index}",
            )
            assert repository.append_event(event) is True
            assert repository.load_event(event.event_id) == event


def test_postgres_audit_v3_rejects_forks_and_identity_changes(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    with _repository(postgres_cluster) as repository:
        first = _event()
        repository.append_event(first)
        stale = _event(
            parent=first,
            occurred_at="2026-07-28T00:00:01Z",
        )
        repository.append_event(stale)
        fork = _event(
            parent=first,
            event_type="retrieval_recorded",
            reason_code="RETRIEVED",
            occurred_at="2026-07-28T00:00:02Z",
        )
        with pytest.raises(
            PostgresAuditV3ConflictError,
            match="extend",
        ):
            repository.append_event(fork)
        wrong_identity = _event(
            parent=stale,
            repository_id="repository_other",
            occurred_at="2026-07-28T00:00:03Z",
        )
        with pytest.raises(
            PostgresAuditV3ConflictError,
            match="identity",
        ):
            repository.append_event(wrong_identity)


def test_postgres_audit_v3_recovery_atomic_idempotent_and_linked(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    recovery = _recovery()
    event = _recovery_event(recovery)
    with _repository(postgres_cluster) as repository:
        assert repository.append_recovery(recovery, event) == (
            PostgresAuditV3AppendResult(
                event_id=event.event_id,
                event_inserted=True,
                recovery_action_id=recovery.recovery_action_id,
                recovery_inserted=True,
            )
        )
        replayed = repository.append_recovery(recovery, event)
        assert replayed.event_inserted is False
        assert replayed.recovery_inserted is False
        assert repository.load_recovery(recovery.recovery_action_id) == (
            recovery,
            event,
        )
        with pytest.raises(KeyError):
            repository.load_recovery("recovery_action_sha256_" + "f" * 64)
        with pytest.raises(ValueError, match="append_recovery"):
            repository.append_event(event)
        bad_event = _event(
            actor_type="worker",
            actor_id="wrong_worker",
            event_type="recovery_succeeded",
            reason_code="RECOVERY_COMPLETED",
            payload_sha256=DIGEST_B,
            references=(
                AuditReference(
                    "recovery_action",
                    _recovery(request_sha256=DIGEST_B).recovery_action_id,
                ),
            ),
            occurred_at="2026-07-28T00:10:01Z",
        )
        with pytest.raises(PostgresAuditV3ConflictError, match="linkage"):
            repository.append_recovery(
                _recovery(request_sha256=DIGEST_B),
                bad_event,
            )


def test_postgres_audit_v3_request_collision_rolls_back_event(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    first_recovery = _recovery()
    first_event = _recovery_event(first_recovery)
    with _repository(postgres_cluster) as repository:
        repository.append_recovery(first_recovery, first_event)
        second_recovery = _recovery(action="investigate")
        second_event = _recovery_event(
            second_recovery,
            parent=first_event,
            occurred_at="2026-07-28T00:10:02Z",
        )
        with pytest.raises(PostgresAuditV3ConflictError):
            repository.append_recovery(second_recovery, second_event)
        head = repository.stream_head(first_event.stream_id)
        assert head is not None
        assert head.current_event_id == first_event.event_id
        with pytest.raises(KeyError):
            repository.load_event(second_event.event_id)


def test_postgres_audit_v3_uses_caller_savepoint(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    psycopg = pytest.importorskip("psycopg")
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        autocommit=False,
    )
    repository = PostgresAuditV3Repository(connection)
    event = _event()
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE caller_work (value integer)")
            cursor.execute("INSERT INTO caller_work VALUES (1)")
        assert repository.append_event(event) is True
        with pytest.raises(PostgresAuditV3ConflictError):
            repository.append_event(
                _event(
                    parent=event,
                    repository_id="repository_other",
                    occurred_at="2026-07-28T00:00:01Z",
                )
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM caller_work")
            assert cursor.fetchone() == (1,)
    connection.close()


def test_postgres_audit_v3_concurrent_identical_append_is_idempotent(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    event = _event()

    def append() -> bool:
        with _repository(postgres_cluster) as repository:
            return repository.append_event(event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _value: append(), range(2)))
    assert sorted(results) == [False, True]


def test_postgres_audit_v3_schema_drift_and_missing_schema_fail_closed(
    postgres_cluster: PostgresCluster,
):
    postgres_cluster.load_schema()
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuditV3SchemaError):
            repository.stream_head("stream_001")
    _install_result = postgres_cluster.run_script(INSTALL)
    assert _install_result.returncode == 0, _install_result.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
        "DISABLE TRIGGER audit_events_append",
    )
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuditV3SchemaError):
            repository.append_event(_event())


@pytest.mark.parametrize(
    "mutation",
    (
        "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
        "DROP CONSTRAINT audit_events_parent_fkey",
        "DROP INDEX trace_backed_memory_v3_audit.audit_events_type",
        "ALTER FUNCTION "
        "trace_backed_memory_v3_audit.validate_event_insert() "
        "RESET search_path",
        "CREATE OR REPLACE FUNCTION "
        "trace_backed_memory_v3_audit.validate_event_insert() "
        "RETURNS trigger LANGUAGE plpgsql "
        "SET search_path = pg_catalog AS $$ BEGIN RETURN NEW; END $$",
        "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
        "ADD COLUMN unexpected text",
        "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
        "DROP CONSTRAINT audit_events_event_type_check; "
        "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
        "ADD CONSTRAINT audit_events_event_type_check CHECK (true)",
        "DROP INDEX trace_backed_memory_v3_audit.audit_events_type; "
        "CREATE INDEX audit_events_type ON "
        "trace_backed_memory_v3_audit.audit_events (reason_code)",
        "ALTER FUNCTION "
        "trace_backed_memory_v3_audit.validate_event_insert() "
        "SECURITY DEFINER",
        "DROP TRIGGER audit_events_append ON "
        "trace_backed_memory_v3_audit.audit_events; "
        "CREATE TRIGGER audit_events_append BEFORE INSERT ON "
        "trace_backed_memory_v3_audit.audit_events FOR EACH ROW "
        "WHEN (false) EXECUTE FUNCTION "
        "trace_backed_memory_v3_audit.validate_event_insert()",
        "DROP TRIGGER audit_events_append ON "
        "trace_backed_memory_v3_audit.audit_events; "
        "CREATE TRIGGER audit_events_append BEFORE INSERT ON "
        "trace_backed_memory_v3_audit.audit_events FOR EACH ROW "
        "EXECUTE FUNCTION "
        "trace_backed_memory_v3_audit.reject_immutable_change()",
    ),
)
def test_postgres_audit_v3_catalog_drift_fails_closed(
    postgres_cluster: PostgresCluster,
    mutation: str,
):
    _install(postgres_cluster)
    assert_sql_succeeds(postgres_cluster, mutation)
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuditV3SchemaError):
            repository.stream_head("stream_001")


def test_postgres_audit_v3_direct_mutation_and_orphans_fail(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    with _repository(postgres_cluster) as repository:
        event = _event()
        repository.append_event(event)
    for statement in (
        "UPDATE trace_backed_memory_v3_audit.audit_events SET reason_code = 'CHANGED'",
        "DELETE FROM trace_backed_memory_v3_audit.audit_events",
        "TRUNCATE trace_backed_memory_v3_audit.audit_events, "
        "trace_backed_memory_v3_audit.recovery_actions, "
        "trace_backed_memory_v3_audit.audit_stream_heads",
    ):
        result = postgres_cluster.run(statement)
        assert result.returncode != 0
        assert "immutable" in result.stderr


def test_postgres_audit_v3_database_rejects_incomplete_recovery_pair(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    psycopg = pytest.importorskip("psycopg")
    recovery = _recovery()
    event = _recovery_event(recovery)
    connection = psycopg.connect(**postgres_cluster.connection_kwargs())
    repository = PostgresAuditV3Repository(connection)
    with pytest.raises(psycopg.IntegrityError):
        with connection.transaction():
            with repository._cursor() as cursor:
                repository._lock_schema(cursor)
                repository._put_event(cursor, event)
    with _repository(postgres_cluster) as reader:
        assert reader.stream_head(event.stream_id) is None

    with pytest.raises(psycopg.IntegrityError):
        with connection.transaction():
            with repository._cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO
                        trace_backed_memory_v3_audit.recovery_actions (
                            recovery_action_id, event_id, session_id, trace_id,
                            run_id, result, executor_id, request_sha256,
                            finished_at, descriptor
                        )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    repository._recovery_values(
                        recovery,
                        "audit_event_sha256_" + "f" * 64,
                    ),
                )
    connection.close()


def test_postgres_audit_v3_database_rejects_semantic_pair_mismatch(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    psycopg = pytest.importorskip("psycopg")
    recovery = _recovery()
    event = _recovery_event(recovery)
    connection = psycopg.connect(**postgres_cluster.connection_kwargs())
    repository = PostgresAuditV3Repository(connection)
    values = list(repository._recovery_values(recovery, event.event_id))
    values[5] = "failed"
    with pytest.raises(
        psycopg.errors.RaiseException,
        match="linkage differs",
    ):
        with connection.transaction():
            with repository._cursor() as cursor:
                repository._lock_schema(cursor)
                repository._put_event(cursor, event)
                cursor.execute(
                    """
                    INSERT INTO
                        trace_backed_memory_v3_audit.recovery_actions (
                            recovery_action_id, event_id, session_id, trace_id,
                            run_id, result, executor_id, request_sha256,
                            finished_at, descriptor
                        )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    tuple(values),
                )
    connection.close()


def test_postgres_audit_v3_rollback_is_exact_and_fail_closed(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            """
        SELECT count(*)
        FROM pg_catalog.pg_namespace
        WHERE nspname = 'trace_backed_memory_v3_audit'
        """,
        )
        == "0"
    )

    installed = postgres_cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE TABLE trace_backed_memory_v3_audit.unexpected (value integer)",
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "relation catalog mismatch" in rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            """
        SELECT count(*)
        FROM trace_backed_memory_v3_audit.schema_metadata
        """,
        )
        == "1"
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
            "DROP CONSTRAINT audit_events_parent_fkey",
            "constraint catalog mismatch",
        ),
        (
            "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
            "ADD COLUMN unexpected text",
            "column catalog mismatch",
        ),
        (
            "CREATE INDEX unexpected_audit_index ON "
            "trace_backed_memory_v3_audit.audit_events (reason_code)",
            "relation catalog mismatch",
        ),
        (
            "ALTER FUNCTION "
            "trace_backed_memory_v3_audit.validate_event_insert() "
            "RESET search_path",
            "function catalog mismatch",
        ),
        (
            "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
            "DISABLE TRIGGER audit_events_append",
            "trigger catalog mismatch",
        ),
        (
            "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
            "DROP CONSTRAINT audit_events_event_type_check; "
            "ALTER TABLE trace_backed_memory_v3_audit.audit_events "
            "ADD CONSTRAINT audit_events_event_type_check CHECK (true)",
            "catalog fingerprint mismatch",
        ),
        (
            "DROP INDEX trace_backed_memory_v3_audit.audit_events_type; "
            "CREATE INDEX audit_events_type ON "
            "trace_backed_memory_v3_audit.audit_events (reason_code)",
            "catalog fingerprint mismatch",
        ),
        (
            "ALTER FUNCTION "
            "trace_backed_memory_v3_audit.validate_event_insert() "
            "SECURITY DEFINER",
            "catalog fingerprint mismatch",
        ),
        (
            "DROP TRIGGER audit_events_append ON "
            "trace_backed_memory_v3_audit.audit_events; "
            "CREATE TRIGGER audit_events_append BEFORE INSERT ON "
            "trace_backed_memory_v3_audit.audit_events FOR EACH ROW "
            "WHEN (false) EXECUTE FUNCTION "
            "trace_backed_memory_v3_audit.validate_event_insert()",
            "catalog fingerprint mismatch",
        ),
    ),
)
def test_postgres_audit_v3_rollback_rejects_catalog_drift(
    postgres_cluster: PostgresCluster,
    mutation: str,
    message: str,
):
    _install(postgres_cluster)
    assert_sql_succeeds(postgres_cluster, mutation)
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert message in rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT count(*) FROM trace_backed_memory_v3_audit.schema_metadata",
        )
        == "1"
    )


def test_postgres_audit_v3_rollback_preserves_external_dependencies(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        """
        CREATE TABLE public.audit_event_consumer (
            event_id text COLLATE "C" PRIMARY KEY
                REFERENCES trace_backed_memory_v3_audit.audit_events (event_id)
        )
        """,
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "depend" in rejected.stderr.lower()
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT count(*) FROM trace_backed_memory_v3_audit.schema_metadata",
        )
        == "1"
    )
