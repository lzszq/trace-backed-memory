from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_event_ledger_v1 as postgres_ledger
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventTrustedContext,
    build_canonical_event,
    dumps_canonical_event,
)
from trace_backed_memory.ledger_port_v1 import (
    EventLedgerClassificationDeniedError,
    EventLedgerConflictError,
    EventLedgerIdempotencyConflictError,
    EventLedgerInvalidRequestError,
    LedgerAccessContext,
    LedgerAppendRequest,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import (
    PostgresEventLedgerV1,
    PostgresEventLedgerV1Error,
    PostgresEventLedgerV1IntegrityError,
    PostgresEventLedgerV1PersistenceError,
    PostgresEventLedgerV1SchemaError,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from tests.postgres_support import PostgresCluster, assert_sql_succeeds


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-event-ledger.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-event-ledger-rollback.sql"


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _access(
    *,
    allowed: tuple[str, ...] = (
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
            repository_id="repository_001",
            environment_id="environment_local",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="principal",
        actor_id="principal_001",
        authorization_decision_id="authorization_decision_001",
        classification_filter=LedgerClassificationFilter(allowed),  # type: ignore[arg-type]
    )


def _artifact() -> EventArtifactRef:
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + "f" * 64,
        content_sha256=_digest("f"),
        media_type="application/json",
        size_bytes=128,
        classification="internal",
        retention_policy_id="retention_default",
        encryption_key_id="encryption_key_001",
        availability="available",
    )


def _event(
    *,
    version: int,
    position: int,
    parent: str | None,
    event_id: str,
    key: str,
    command: str,
    artifacts: tuple[EventArtifactRef, ...] = (),
    stream_id: str = "stream_001",
    classification: str = "internal",
) -> CanonicalEvent:
    access = _access()
    trusted = access.event_trusted_context()
    assert type(trusted) is EventTrustedContext
    return build_canonical_event(
        event_id=event_id,
        event_type="tbm.test.committed",
        event_version=1,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=stream_id,
        stream_type="test_stream",
        stream_version=version,
        global_position=position,
        trusted_context=trusted,
        request_id=f"request_{event_id}",
        idempotency_key_sha256=key,
        request_sha256=command,
        correlation_id="correlation_001",
        causation_id=None,
        occurred_at="2026-08-01T00:00:00Z",
        recorded_at=f"2026-08-01T00:00:{position:02d}Z",
        producer="trace_backed_memory",
        producer_version="0.1.0",
        payload_schema="tbm.test.committed.v1",
        previous_stream_event_sha256=parent,
        classification=classification,  # type: ignore[arg-type]
        retention_policy_id="retention_default",
        artifact_refs=artifacts,
        payload={"event_id": event_id},
    )


def _request() -> LedgerAppendRequest:
    key = _digest("a")
    command = _digest("b")
    first = _event(
        version=1,
        position=1,
        parent=None,
        event_id="evt_postgres_first",
        key=key,
        command=command,
        artifacts=(_artifact(),),
    )
    second = _event(
        version=2,
        position=2,
        parent=first.event_sha256,
        event_id="evt_postgres_second",
        key=key,
        command=command,
    )
    return LedgerAppendRequest(
        access=_access(),
        stream_id="stream_001",
        expected_stream_version=0,
        events=(first, second),
        idempotency=LedgerIdempotency(key, command),
    )


def _append(ledger: object, request: LedgerAppendRequest):
    return ledger.append(
        request.stream_id,
        request.expected_stream_version,
        request.events,
        request.idempotency,
    )


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    installed = cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr


def _repository(
    cluster: PostgresCluster,
    access: LedgerAccessContext | None = None,
) -> PostgresEventLedgerV1:
    return PostgresEventLedgerV1.connect(
        _access() if access is None else access,
        **cluster.connection_kwargs(),
    )


def test_postgres_event_ledger_public_exports_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tbm.PostgresEventLedgerV1 is PostgresEventLedgerV1
    assert tbm.POSTGRES_EVENT_LEDGER_V1_SCHEMA_VERSION == 1
    assert "PostgresEventLedgerV1" in tbm.__all__
    with pytest.raises(ValueError, match="connection"):
        PostgresEventLedgerV1(None, _access())
    with pytest.raises(ValueError, match="access_context"):
        PostgresEventLedgerV1(object(), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="owns_connection"):
        PostgresEventLedgerV1(object(), _access(), owns_connection=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="access_context"):
        PostgresEventLedgerV1.connect(object())  # type: ignore[arg-type]

    repository = PostgresEventLedgerV1(object(), _access())
    repository.close()
    with pytest.raises(PostgresEventLedgerV1Error, match="closed"):
        repository.read_global()

    class BrokenClose:
        closed = False

        def close(self) -> None:
            raise RuntimeError("close failed")

    broken = PostgresEventLedgerV1(
        BrokenClose(),
        _access(),
        owns_connection=True,
    )
    with pytest.raises(PostgresEventLedgerV1PersistenceError, match="close"):
        broken.close()

    class DatabaseFailure(RuntimeError):
        def __init__(self, sqlstate: str | None) -> None:
            super().__init__("database failure")
            self.sqlstate = sqlstate

    with pytest.raises(PostgresEventLedgerV1SchemaError):
        PostgresEventLedgerV1._raise_database_error(
            DatabaseFailure("42P01"),
            "schema failed",
        )
    with pytest.raises(EventLedgerConflictError):
        PostgresEventLedgerV1._raise_database_error(
            DatabaseFailure("23505"),
            "write conflicted",
        )
    with pytest.raises(EventLedgerConflictError):
        PostgresEventLedgerV1._raise_database_error(
            DatabaseFailure("P0001"),
            "trigger conflicted",
        )
    with pytest.raises(PostgresEventLedgerV1PersistenceError):
        PostgresEventLedgerV1._raise_database_error(
            DatabaseFailure(None),
            "database failed",
        )

    class FakePsycopg:
        class Error(Exception):
            pass

        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> object:
            raise FakePsycopg.Error("connect failed")

    monkeypatch.setattr(
        postgres_ledger,
        "_load_psycopg",
        lambda: (FakePsycopg, object(), object()),
    )
    with pytest.raises(PostgresEventLedgerV1PersistenceError):
        PostgresEventLedgerV1.connect(_access())


def test_postgres_event_ledger_defensive_helpers_fail_closed() -> None:
    ledger = PostgresEventLedgerV1(object(), _access())

    class Cursor:
        def __init__(self, rows: list[list[object]]) -> None:
            self._rows = list(rows)

        def execute(self, *_args: object) -> None:
            return None

        def fetchall(self) -> list[object]:
            return self._rows.pop(0)

    with pytest.raises(EventLedgerInvalidRequestError):
        postgres_ledger._canonical_json({"not-json"})
    with pytest.raises(PostgresEventLedgerV1SchemaError, match="catalog"):
        ledger._names(Cursor([[{"name": 1}]]), "SELECT")
    with pytest.raises(PostgresEventLedgerV1SchemaError, match="columns"):
        ledger._verify_columns(Cursor([[{"table_name": 1}]]))
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="row"):
        ledger._stored_event(Cursor([]), {})

    fields = (
        "event_id",
        "event_sha256",
        "partition_sha256",
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "stream_id",
        "stream_version",
        "global_position",
        "previous_stream_event_sha256",
        "classification",
        "artifact_ref_count",
        "canonical_event",
    )
    event = _request().events[0]
    row = dict(
        zip(
            fields,
            ledger._event_values(event, _access().partition.partition_sha256),
            strict=True,
        )
    )
    invalid_descriptor = dict(row)
    invalid_descriptor["canonical_event"] = b"not-text"
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="descriptor"):
        ledger._stored_event(Cursor([]), invalid_descriptor)
    mismatched = dict(row)
    mismatched["classification"] = "public"
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="columns"):
        ledger._stored_event(Cursor([]), mismatched)
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="multiple"):
        ledger._select_event_by_sha256(Cursor([[row, row]]), event.event_sha256)
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="global"):
        ledger._select_global_position(Cursor([[]]), for_update=False)

    partition = _access().partition
    assert ledger._select_head_event(
        Cursor([[]]),
        "stream_001",
        for_update=False,
    ) is None
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="shape"):
        ledger._select_head_event(
            Cursor([[{}, {}]]),
            "stream_001",
            for_update=False,
        )
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="partition"):
        ledger._select_head_event(
            Cursor(
                [[{"organization_id": "wrong", "current_stream_version": 0}]]
            ),
            "stream_001",
            for_update=False,
        )
    empty_head = {
        "organization_id": partition.organization_id,
        "tenant_id": partition.tenant_id,
        "repository_id": partition.repository_id,
        "environment_id": partition.environment_id,
        "current_stream_version": 0,
        "current_event_id": None,
        "current_event_sha256": None,
    }
    assert ledger._select_head_event(
        Cursor([[empty_head]]),
        "stream_001",
        for_update=False,
    ) is None
    malformed_head = dict(empty_head)
    malformed_head.update(
        current_stream_version="bad",
        current_event_id="evt_bad",
    )
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="malformed"):
        ledger._select_head_event(
            Cursor([[malformed_head]]),
            "stream_001",
            for_update=False,
        )
    missing_event_head = dict(empty_head)
    missing_event_head.update(
        current_stream_version=1,
        current_event_id="evt_missing",
        current_event_sha256=_digest("a"),
    )
    with pytest.raises(PostgresEventLedgerV1IntegrityError, match="event tail"):
        ledger._select_head_event(
            Cursor([[missing_event_head], []]),
            "stream_001",
            for_update=False,
        )


def test_postgres_event_ledger_canonical_functions_are_complete() -> None:
    postgres_ledger._expected_function_bodies.cache_clear()
    assert set(postgres_ledger._expected_function_bodies()) == {
        "reject_immutable_change",
        "validate_artifact_insert",
        "validate_event_insert",
        "validate_global_head_insert",
        "validate_global_head_update",
        "validate_stream_head_insert",
        "validate_stream_head_update",
    }


def test_postgres_event_ledger_append_replay_read_and_artifact_descriptor(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        receipt = _append(ledger, request)
        assert _append(ledger, request) == receipt
        assert ledger.read_stream("stream_001").events == request.events
        assert ledger.read_global().events == request.events
        assert ledger.verify_stream("stream_001").valid is True
        assert ledger.verify_integrity()[0].valid is True
        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                cursor.execute(
                    "SELECT descriptor FROM "
                    "trace_backed_memory_v3_event_ledger.artifacts"
                )
                assert cursor.fetchone()["descriptor"] == (
                    postgres_ledger._canonical_json(_artifact().to_dict())
                )


def test_postgres_event_ledger_paginates_and_runs_subscription_state_machine(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        _append(ledger, request)
        first_stream = ledger.read_stream("stream_001", limit=1)
        assert first_stream.events == (request.events[0],)
        assert first_stream.has_more is True
        assert first_stream.next_stream_version == 2
        second_stream = ledger.read_stream(
            "stream_001",
            from_version=first_stream.next_stream_version,
            limit=1,
        )
        assert second_stream.events == (request.events[1],)
        assert second_stream.has_more is False

        first_global = ledger.read_global(limit=1)
        assert first_global.events == (request.events[0],)
        assert first_global.has_more is True
        assert first_global.next_global_position == 1
        second_global = ledger.read_global(
            after_position=first_global.next_global_position,
            limit=1,
        )
        assert second_global.events == (request.events[1],)
        assert second_global.has_more is False

        subscription = ledger.subscribe(limit=1)
        with pytest.raises(EventLedgerConflictError):
            subscription.acknowledge(
                "delivery_missing",
                expected_next_global_position=None,
            )
        first = subscription.poll()
        assert subscription.poll() == first
        with pytest.raises(EventLedgerConflictError):
            subscription.acknowledge(
                first.delivery_id,
                expected_next_global_position=None,
            )
        subscription.acknowledge(
            first.delivery_id,
            expected_next_global_position=first.page.next_global_position,
        )
        second = subscription.poll()
        assert second.page.events == (request.events[1],)
        subscription.acknowledge(
            second.delivery_id,
            expected_next_global_position=None,
        )
        heartbeat = subscription.poll()
        assert heartbeat.heartbeat is True
        subscription.acknowledge(
            heartbeat.delivery_id,
            expected_next_global_position=None,
        )
        subscription.close()
        subscription.close()
        with pytest.raises(EventLedgerInvalidRequestError):
            subscription.poll()
        with pytest.raises(EventLedgerInvalidRequestError):
            subscription.acknowledge(
                heartbeat.delivery_id,
                expected_next_global_position=None,
            )


def test_postgres_event_ledger_rejects_idempotency_conflict_and_hidden_stream(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as writer:
        _append(writer, request)
        conflict_command = _digest("c")
        conflict_first = _event(
            version=1,
            position=1,
            parent=None,
            event_id="evt_postgres_conflict_first",
            key=request.idempotency.idempotency_key_sha256,
            command=conflict_command,
        )
        conflict_second = _event(
            version=2,
            position=2,
            parent=conflict_first.event_sha256,
            event_id="evt_postgres_conflict_second",
            key=request.idempotency.idempotency_key_sha256,
            command=conflict_command,
        )
        conflicting = LedgerAppendRequest(
            access=_access(),
            stream_id="stream_001",
            expected_stream_version=0,
            events=(conflict_first, conflict_second),
            idempotency=LedgerIdempotency(
                request.idempotency.idempotency_key_sha256,
                conflict_command,
            ),
        )
        with pytest.raises(EventLedgerIdempotencyConflictError):
            _append(writer, conflicting)

        restricted_key = _digest("d")
        restricted_command = _digest("e")
        restricted = _event(
            version=1,
            position=3,
            parent=None,
            event_id="evt_postgres_restricted",
            key=restricted_key,
            command=restricted_command,
            stream_id="stream_restricted",
            classification="restricted",
        )
        writer.append(
            "stream_restricted",
            0,
            (restricted,),
            LedgerIdempotency(restricted_key, restricted_command),
        )

    with _repository(
        postgres_cluster,
        _access(allowed=("public", "internal")),
    ) as reader:
        assert reader.read_global().events == request.events
        with pytest.raises(EventLedgerClassificationDeniedError):
            reader.read_stream("stream_restricted")
        with pytest.raises(EventLedgerClassificationDeniedError):
            reader.verify_stream("stream_restricted")


def test_postgres_event_ledger_matches_sqlite_receipt_contract(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource("schemas/sqlite-v3-event-ledger.sql").decode()
    )
    sqlite_ledger = SQLiteEventLedgerV1(connection, _access())
    try:
        sqlite_receipt = _append(sqlite_ledger, request)
        with _repository(postgres_cluster) as postgres:
            postgres_receipt = _append(postgres, request)
            assert postgres_receipt == sqlite_receipt
            assert postgres.read_global() == sqlite_ledger.read_global()
    finally:
        sqlite_ledger.close()
        connection.close()


def test_postgres_event_ledger_uses_caller_savepoint(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg = pytest.importorskip("psycopg")
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        autocommit=False,
    )
    ledger = PostgresEventLedgerV1(connection, _access())
    request = _request()
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE caller_work (value integer)")
            cursor.execute("INSERT INTO caller_work VALUES (1)")
        _append(ledger, request)
        stale_key = _digest("c")
        stale_command = _digest("d")
        stale_event = _event(
            version=2,
            position=3,
            parent=request.events[-1].event_sha256,
            event_id="evt_postgres_stale",
            key=stale_key,
            command=stale_command,
        )
        with pytest.raises(EventLedgerConflictError):
            ledger.append(
                request.stream_id,
                1,
                (stale_event,),
                LedgerIdempotency(stale_key, stale_command),
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM caller_work")
            assert cursor.fetchone() == (1,)
    connection.close()


def test_postgres_event_ledger_concurrent_exact_replay_is_single_commit(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()

    def append() -> object:
        with _repository(postgres_cluster) as ledger:
            return _append(ledger, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(lambda _value: append(), range(2)))
    assert receipts[0] == receipts[1]
    with _repository(postgres_cluster) as ledger:
        assert ledger.read_global().events == request.events


def test_postgres_event_ledger_schema_drift_and_rollback_fail_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    with _repository(postgres_cluster) as missing:
        with pytest.raises(PostgresEventLedgerV1SchemaError):
            missing.read_global()
    installed = postgres_cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "ALTER TABLE trace_backed_memory_v3_event_ledger.events "
        "DISABLE TRIGGER event_ledger_events_validate_insert",
    )
    with _repository(postgres_cluster) as drifted:
        with pytest.raises(PostgresEventLedgerV1SchemaError):
            drifted.read_global()
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "trigger catalog mismatch" in rejected.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "CREATE TABLE trace_backed_memory_v3_event_ledger.extra_table "
        "(value integer)",
        "CREATE INDEX event_ledger_extra_index ON "
        "trace_backed_memory_v3_event_ledger.events (event_id, stream_id)",
        "ALTER TABLE trace_backed_memory_v3_event_ledger.events "
        "ADD COLUMN extra_column text COLLATE \"C\"",
        "GRANT USAGE ON SCHEMA trace_backed_memory_v3_event_ledger TO PUBLIC",
        "ALTER FUNCTION "
        "trace_backed_memory_v3_event_ledger.reject_immutable_change() "
        "SET search_path = public",
        "ALTER TABLE trace_backed_memory_v3_event_ledger.events "
        "ENABLE ROW LEVEL SECURITY; CREATE POLICY event_ledger_extra_policy "
        "ON trace_backed_memory_v3_event_ledger.events USING (true)",
    ],
)
def test_postgres_event_ledger_rejects_catalog_drift_variants(
    postgres_cluster: PostgresCluster,
    mutation: str,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(postgres_cluster, mutation)
    with _repository(postgres_cluster) as drifted:
        with pytest.raises(PostgresEventLedgerV1SchemaError):
            drifted.read_global()


@pytest.mark.parametrize(
    "corruption",
    [
        "event",
        "artifact",
        "idempotency_scalar",
        "idempotency_invalid",
        "idempotency_missing",
        "idempotency_receipt",
    ],
)
def test_postgres_event_ledger_rejects_corrupt_stored_rows(
    postgres_cluster: PostgresCluster,
    corruption: str,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        _append(ledger, request)
        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                if corruption == "event":
                    cursor.execute(
                        "ALTER TABLE "
                        "trace_backed_memory_v3_event_ledger.events "
                        "DISABLE TRIGGER event_ledger_events_immutable"
                    )
                    cursor.execute(
                        "UPDATE trace_backed_memory_v3_event_ledger.events "
                        "SET canonical_event = '{}' WHERE stream_version = 1"
                    )
                elif corruption == "artifact":
                    cursor.execute(
                        "ALTER TABLE "
                        "trace_backed_memory_v3_event_ledger.artifacts "
                        "DISABLE TRIGGER event_ledger_artifacts_immutable"
                    )
                    cursor.execute(
                        "UPDATE trace_backed_memory_v3_event_ledger.artifacts "
                        "SET descriptor = '{}'"
                    )
                else:
                    cursor.execute(
                        "ALTER TABLE "
                        "trace_backed_memory_v3_event_ledger.idempotency "
                        "DISABLE TRIGGER event_ledger_idempotency_immutable"
                    )
                    if corruption == "idempotency_scalar":
                        cursor.execute(
                            "UPDATE "
                            "trace_backed_memory_v3_event_ledger.idempotency "
                            "SET event_sha256s_json = 'null'"
                        )
                    elif corruption == "idempotency_invalid":
                        cursor.execute(
                            "UPDATE "
                            "trace_backed_memory_v3_event_ledger.idempotency "
                            "SET event_sha256s_json = 'xxxx'"
                        )
                    elif corruption == "idempotency_missing":
                        cursor.execute(
                            "UPDATE "
                            "trace_backed_memory_v3_event_ledger.idempotency "
                            "SET event_sha256s_json = %s",
                            (postgres_ledger._canonical_json([_digest("9")]),),
                        )
                    else:
                        cursor.execute(
                            "UPDATE "
                            "trace_backed_memory_v3_event_ledger.idempotency "
                            "SET current_stream_version = 3"
                        )
        ledger._lock_schema = (  # type: ignore[method-assign]
            lambda _cursor, *, write: None
        )
        with pytest.raises(PostgresEventLedgerV1IntegrityError):
            if corruption.startswith("idempotency"):
                _append(ledger, request)
            else:
                ledger.read_global()


@pytest.mark.parametrize("corruption", ["global_head", "global_gap"])
def test_postgres_event_ledger_rejects_global_metadata_corruption(
    postgres_cluster: PostgresCluster,
    corruption: str,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        _append(ledger, request)
        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                if corruption == "global_head":
                    cursor.execute(
                        "ALTER TABLE "
                        "trace_backed_memory_v3_event_ledger.global_head "
                        "DISABLE TRIGGER event_ledger_global_head_advance"
                    )
                    cursor.execute(
                        "UPDATE "
                        "trace_backed_memory_v3_event_ledger.global_head "
                        "SET current_global_position = 99"
                    )
                else:
                    replacement = _event(
                        version=1,
                        position=3,
                        parent=None,
                        event_id=request.events[0].event_id,
                        key=request.idempotency.idempotency_key_sha256,
                        command=request.idempotency.command_sha256,
                        artifacts=(_artifact(),),
                    )
                    cursor.execute(
                        "ALTER TABLE "
                        "trace_backed_memory_v3_event_ledger.events "
                        "DISABLE TRIGGER event_ledger_events_immutable"
                    )
                    cursor.execute(
                        "UPDATE trace_backed_memory_v3_event_ledger.events "
                        "SET event_sha256 = %s, global_position = %s, "
                        "canonical_event = %s WHERE event_id = %s",
                        (
                            replacement.event_sha256,
                            replacement.global_position,
                            dumps_canonical_event(replacement),
                            replacement.event_id,
                        ),
                    )
        ledger._lock_schema = (  # type: ignore[method-assign]
            lambda _cursor, *, write: None
        )
        with pytest.raises(PostgresEventLedgerV1IntegrityError):
            ledger.verify_integrity()


def test_postgres_event_ledger_detects_corrupt_heads(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        _append(ledger, request)
        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE "
                    "trace_backed_memory_v3_event_ledger.stream_heads "
                    "DISABLE TRIGGER event_ledger_stream_heads_advance"
                )
                cursor.execute(
                    "UPDATE trace_backed_memory_v3_event_ledger.stream_heads "
                    "SET current_event_id = 'evt_missing'"
                )
        ledger._lock_schema = (  # type: ignore[method-assign]
            lambda _cursor, *, write: None
        )
        with pytest.raises(
            PostgresEventLedgerV1IntegrityError,
            match="event tail",
        ):
            ledger.verify_integrity()


@pytest.mark.parametrize(
    ("corruption", "issue"),
    [
        ("stream_version", "STREAM_VERSION_GAP"),
        ("global_position", "GLOBAL_POSITION_INVALID"),
        ("hash_chain", "HASH_CHAIN_MISMATCH"),
    ],
)
def test_postgres_event_ledger_reports_semantic_event_corruption(
    postgres_cluster: PostgresCluster,
    corruption: str,
    issue: str,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        _append(ledger, request)
        target = request.events[1] if corruption == "hash_chain" else request.events[0]
        replacement = _event(
            version=3 if corruption == "stream_version" else target.stream_version,
            position=3 if corruption == "global_position" else target.global_position,
            parent=(
                request.events[1].event_sha256
                if corruption == "stream_version"
                else _digest("9")
                if corruption == "hash_chain"
                else target.previous_stream_event_sha256
            ),
            event_id=target.event_id,
            key=request.idempotency.idempotency_key_sha256,
            command=request.idempotency.command_sha256,
            artifacts=target.artifact_refs,
        )
        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE "
                    "trace_backed_memory_v3_event_ledger.events "
                    "DISABLE TRIGGER event_ledger_events_immutable"
                )
                cursor.execute(
                    "UPDATE trace_backed_memory_v3_event_ledger.events "
                    "SET event_sha256 = %s, stream_version = %s, "
                    "global_position = %s, previous_stream_event_sha256 = %s, "
                    "canonical_event = %s WHERE event_id = %s",
                    (
                        replacement.event_sha256,
                        replacement.stream_version,
                        replacement.global_position,
                        replacement.previous_stream_event_sha256,
                        dumps_canonical_event(replacement),
                        replacement.event_id,
                    ),
                )
                if corruption == "hash_chain":
                    cursor.execute(
                        "ALTER TABLE "
                        "trace_backed_memory_v3_event_ledger.stream_heads "
                        "DISABLE TRIGGER event_ledger_stream_heads_advance"
                    )
                    cursor.execute(
                        "UPDATE "
                        "trace_backed_memory_v3_event_ledger.stream_heads "
                        "SET current_event_sha256 = %s WHERE stream_id = %s",
                        (replacement.event_sha256, replacement.stream_id),
                    )
        ledger._lock_schema = (  # type: ignore[method-assign]
            lambda _cursor, *, write: None
        )
        verification = ledger.verify_stream("stream_001")
        assert verification.valid is False
        assert issue in verification.issue_codes


def test_postgres_event_ledger_verifies_projection_checkpoint_descriptors(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        _append(ledger, request)
        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                cursor.execute(
                    "INSERT INTO "
                    "trace_backed_memory_v3_event_ledger.checkpoints ("
                    "projection_name, projection_version, partition_sha256, "
                    "global_position, state_sha256, descriptor"
                    ") VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        "projection_valid",
                        1,
                        _access().partition.partition_sha256,
                        2,
                        _digest("d"),
                        '{"ok":true}',
                    ),
                )
        assert ledger.verify_integrity()[0].valid is True

        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                cursor.execute(
                    "INSERT INTO "
                    "trace_backed_memory_v3_event_ledger.checkpoints ("
                    "projection_name, projection_version, partition_sha256, "
                    "global_position, state_sha256, descriptor"
                    ") VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        "projection_malformed",
                        1,
                        _access().partition.partition_sha256,
                        2,
                        _digest("e"),
                        "{]",
                    ),
                )
        with pytest.raises(
            PostgresEventLedgerV1IntegrityError,
            match="descriptor is invalid",
        ):
            ledger.verify_integrity()


def test_postgres_event_ledger_rejects_invalid_checkpoint_identity(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    request = _request()
    with _repository(postgres_cluster) as ledger:
        _append(ledger, request)
        with ledger._connection.transaction():
            with ledger._cursor() as cursor:
                cursor.execute(
                    "INSERT INTO "
                    "trace_backed_memory_v3_event_ledger.checkpoints ("
                    "projection_name, projection_version, partition_sha256, "
                    "global_position, state_sha256, descriptor"
                    ") VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        "",
                        1,
                        _access().partition.partition_sha256,
                        2,
                        _digest("d"),
                        "{}",
                    ),
                )
        with pytest.raises(
            PostgresEventLedgerV1IntegrityError,
            match="invalid shape",
        ):
            ledger.verify_integrity()


def test_postgres_event_ledger_rollback_removes_exact_empty_schema(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT count(*) FROM pg_catalog.pg_namespace "
            "WHERE nspname = 'trace_backed_memory_v3_event_ledger'",
        )
        == "0"
    )


def test_postgres_event_ledger_rollback_preserves_external_dependencies(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE TABLE public.event_ledger_dependency ("
        "event_id text REFERENCES "
        "trace_backed_memory_v3_event_ledger.events(event_id))",
    )

    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "depend" in rejected.stderr.lower()
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT count(*) FROM pg_catalog.pg_namespace "
            "WHERE nspname = 'trace_backed_memory_v3_event_ledger'",
        )
        == "1"
    )
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT count(*) FROM pg_catalog.pg_constraint "
            "WHERE conrelid = 'public.event_ledger_dependency'::regclass "
            "AND contype = 'f'",
        )
        == "1"
    )
