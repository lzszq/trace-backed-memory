from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory.sqlite_event_ledger_v1 as sqlite_ledger
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
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import (
    SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE,
    SQLiteEventLedgerV1,
    SQLiteEventLedgerV1Error,
    SQLiteEventLedgerV1IntegrityError,
    SQLiteEventLedgerV1PersistenceError,
    SQLiteEventLedgerV1SchemaError,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _access(
    *,
    tenant_id: str = "tenant_001",
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
            tenant_id=tenant_id,
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


def _artifact(character: str = "f") -> EventArtifactRef:
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + character * 64,
        content_sha256=_digest(character),
        media_type="application/json",
        size_bytes=128,
        classification="internal",
        retention_policy_id="retention_engineering_memory",
        encryption_key_id="encryption_key_001",
        availability="available",
    )


def _event(
    access: LedgerAccessContext,
    *,
    stream_id: str,
    stream_version: int,
    global_position: int,
    previous_sha256: str | None,
    event_id: str,
    idempotency_key_sha256: str,
    command_sha256: str,
    classification: str = "internal",
    artifact_refs: tuple[EventArtifactRef, ...] = (),
) -> CanonicalEvent:
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
        stream_version=stream_version,
        global_position=global_position,
        trusted_context=trusted,
        request_id=f"request_{event_id}",
        idempotency_key_sha256=idempotency_key_sha256,
        request_sha256=command_sha256,
        correlation_id="correlation_001",
        causation_id=None,
        occurred_at="2026-08-01T00:00:00Z",
        recorded_at=f"2026-08-01T00:00:{global_position:02d}Z",
        producer="trace_backed_memory",
        producer_version="0.1.0",
        payload_schema="tbm.test.committed.v1",
        previous_stream_event_sha256=previous_sha256,
        classification=classification,  # type: ignore[arg-type]
        retention_policy_id="retention_default",
        artifact_refs=artifact_refs,
        payload={"event_id": event_id},
    )


def _batch(
    access: LedgerAccessContext,
    *,
    stream_id: str = "stream_001",
    expected_version: int = 0,
    first_global_position: int = 1,
    previous_sha256: str | None = None,
    key_character: str = "a",
    command_character: str = "b",
    event_prefix: str = "evt_sqlite",
    count: int = 2,
    classification: str = "internal",
    artifact_refs: tuple[EventArtifactRef, ...] = (),
) -> LedgerAppendRequest:
    key = _digest(key_character)
    command = _digest(command_character)
    events: list[CanonicalEvent] = []
    previous = previous_sha256
    for offset in range(count):
        event = _event(
            access,
            stream_id=stream_id,
            stream_version=expected_version + offset + 1,
            global_position=first_global_position + offset,
            previous_sha256=previous,
            event_id=f"{event_prefix}_{offset + 1}",
            idempotency_key_sha256=key,
            command_sha256=command,
            classification=classification,
            artifact_refs=artifact_refs if offset == 0 else (),
        )
        events.append(event)
        previous = event.event_sha256
    return LedgerAppendRequest(
        access=access,
        stream_id=stream_id,
        expected_stream_version=expected_version,
        events=tuple(events),
        idempotency=LedgerIdempotency(key, command),
    )


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        ":memory:",
        isolation_level=None,
        check_same_thread=False,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource(SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE).decode(
            "utf-8"
        )
    )
    return connection


def _append(
    ledger: SQLiteEventLedgerV1,
    request: LedgerAppendRequest,
):
    return ledger.append(
        request.stream_id,
        request.expected_stream_version,
        request.events,
        request.idempotency,
    )


def test_sqlite_event_ledger_file_owner_requires_wal_and_exact_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    with SQLiteEventLedgerV1.connect(
        database,
        _access(),
        initialize=True,
    ) as ledger:
        assert ledger._connection.execute("PRAGMA journal_mode").fetchone() == (
            "wal",
        )
        assert ledger._connection.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_event_ledger_schema"
        ).fetchone() == (1, "tbm.event-ledger-port.v1")
        assert ledger.verify_integrity() == ()

        with pytest.raises(SQLiteEventLedgerV1PersistenceError):
            SQLiteEventLedgerV1.connect(database, _access(), timeout_seconds=0)


def test_sqlite_event_ledger_validates_configuration_and_closed_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    with pytest.raises(ValueError, match="connection"):
        SQLiteEventLedgerV1(object(), _access())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="access_context"):
        SQLiteEventLedgerV1(connection, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="flags"):
        SQLiteEventLedgerV1(connection, _access(), owns_connection=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="access_context"):
        SQLiteEventLedgerV1.connect(":memory:", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="initialize"):
        SQLiteEventLedgerV1.connect(":memory:", _access(), initialize=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="database"):
        SQLiteEventLedgerV1.connect(object(), _access())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout_seconds"):
        SQLiteEventLedgerV1.connect(":memory:", _access(), timeout_seconds=-1)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            sqlite_ledger.sqlite3,
            "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("connect failed")
            ),
        )
        with pytest.raises(SQLiteEventLedgerV1PersistenceError):
            SQLiteEventLedgerV1.connect(":memory:", _access())

    ledger = SQLiteEventLedgerV1(connection, _access())
    assert ledger.access_context == _access()
    connection.close()
    with pytest.raises(SQLiteEventLedgerV1Error, match="closed"):
        ledger.read_global()
    ledger.close()
    ledger.close()

    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as memory_ledger:
        assert memory_ledger.verify_integrity() == ()


def test_sqlite_event_ledger_defensive_helpers_fail_closed() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())

    class Cursor:
        def __init__(
            self,
            *,
            one: list[object] | None = None,
            all_rows: list[list[object]] | None = None,
        ) -> None:
            self._one = [] if one is None else list(one)
            self._all = [] if all_rows is None else list(all_rows)

        def execute(self, *_args: object) -> None:
            return None

        def fetchone(self) -> object:
            return self._one.pop(0)

        def fetchall(self) -> list[object]:
            return self._all.pop(0)

    try:
        with pytest.raises(EventLedgerInvalidRequestError):
            sqlite_ledger._canonical_json({"not-json"})
        assert sqlite_ledger._is_schema_error(
            sqlite3.OperationalError("no such table: missing")
        )
        assert not sqlite_ledger._is_schema_error(
            sqlite3.OperationalError("database is locked")
        )
        with pytest.raises(SQLiteEventLedgerV1IntegrityError, match="row"):
            ledger._event_from_row(Cursor(), ())  # type: ignore[arg-type]
        with pytest.raises(SQLiteEventLedgerV1IntegrityError, match="global"):
            ledger._select_global_position(  # type: ignore[arg-type]
                Cursor(all_rows=[[]])
            )

        partition = _access().partition
        assert ledger._select_head_event(  # type: ignore[arg-type]
            Cursor(one=[None]),
            "stream_missing",
        ) is None
        with pytest.raises(SQLiteEventLedgerV1IntegrityError, match="partition"):
            ledger._select_head_event(  # type: ignore[arg-type]
                Cursor(one=[("wrong", "tenant", "repo", "env", 0, None, None)]),
                "stream_001",
            )
        assert ledger._select_head_event(  # type: ignore[arg-type]
            Cursor(
                one=[
                    (
                        partition.organization_id,
                        partition.tenant_id,
                        partition.repository_id,
                        partition.environment_id,
                        0,
                        None,
                        None,
                    )
                ]
            ),
            "stream_001",
        ) is None
        with pytest.raises(SQLiteEventLedgerV1IntegrityError, match="shape"):
            ledger._select_head_event(  # type: ignore[arg-type]
                Cursor(
                    one=[
                        (
                            partition.organization_id,
                            partition.tenant_id,
                            partition.repository_id,
                            partition.environment_id,
                            "bad",
                            "evt_bad",
                            None,
                        )
                    ]
                ),
                "stream_001",
            )
        with pytest.raises(SQLiteEventLedgerV1IntegrityError, match="canonical"):
            ledger._select_head_event(  # type: ignore[arg-type]
                Cursor(
                    one=[
                        (
                            partition.organization_id,
                            partition.tenant_id,
                            partition.repository_id,
                            partition.environment_id,
                            1,
                            "evt_missing",
                            _digest("a"),
                        ),
                        None,
                    ]
                ),
                "stream_001",
            )
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_appends_batch_replays_and_preserves_artifact_ref() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access(), artifact_refs=(_artifact(),))
    try:
        receipt = _append(ledger, request)
        replay = _append(ledger, request)

        assert replay == receipt
        assert receipt.previous_stream_version == 0
        assert receipt.current_stream_version == 2
        assert receipt.first_global_position == 1
        assert receipt.last_global_position == 2
        assert ledger.read_stream("stream_001").events == request.events
        assert ledger.read_global().events == request.events
        assert ledger.verify_stream("stream_001").valid is True
        assert ledger.verify_integrity()[0].valid is True

        descriptor = connection.execute(
            "SELECT descriptor FROM v3_event_ledger_artifacts"
        ).fetchone()
        assert descriptor is not None
        assert json.loads(descriptor[0]) == _artifact().to_dict()
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(v3_event_ledger_artifacts)"
            ).fetchall()
        }
        assert "content" not in columns
        assert "content_bytes" not in columns
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_paginates_stream_and_global_reads() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
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
        assert second_stream.next_stream_version is None

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
        assert second_global.next_global_position is None
        assert ledger.read_global(after_position=2).events == ()
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_rejects_idempotency_conflict_and_stale_head_atomically() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    first = _batch(_access())
    try:
        _append(ledger, first)
        conflicting_key = _batch(
            _access(),
            key_character="a",
            command_character="c",
            event_prefix="evt_conflicting_key",
        )
        with pytest.raises(EventLedgerIdempotencyConflictError):
            _append(ledger, conflicting_key)

        stale = _batch(
            _access(),
            expected_version=1,
            first_global_position=3,
            previous_sha256=first.events[-1].event_sha256,
            key_character="d",
            command_character="e",
            event_prefix="evt_stale",
        )
        with pytest.raises(EventLedgerConflictError):
            _append(ledger, stale)

        wrong_parent = _batch(
            _access(),
            expected_version=2,
            first_global_position=3,
            previous_sha256=_digest("f"),
            key_character="f",
            command_character="0",
            event_prefix="evt_wrong_parent",
        )
        with pytest.raises(EventLedgerConflictError):
            _append(ledger, wrong_parent)

        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_idempotency"
        ).fetchone() == (1,)
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_global_order_and_partition_reads() -> None:
    connection = _connection()
    access_a = _access(tenant_id="tenant_a")
    access_b = _access(tenant_id="tenant_b")
    ledger_a = SQLiteEventLedgerV1(connection, access_a)
    ledger_b = SQLiteEventLedgerV1(connection, access_b)
    try:
        request_a = _batch(access_a, count=1, event_prefix="evt_tenant_a")
        request_b = _batch(
            access_b,
            first_global_position=2,
            count=1,
            key_character="c",
            command_character="d",
            event_prefix="evt_tenant_b",
        )
        _append(ledger_a, request_a)
        _append(ledger_b, request_b)

        page_a = ledger_a.read_global()
        page_b = ledger_b.read_global()
        assert page_a.events == request_a.events
        assert page_b.events == request_b.events
        assert page_a.high_watermark_global_position == 2
        assert page_b.high_watermark_global_position == 2
        assert len(ledger_a.verify_integrity()) == 1
        assert len(ledger_b.verify_integrity()) == 1
    finally:
        ledger_a.close()
        ledger_b.close()
        connection.close()


def test_sqlite_event_ledger_classification_filter_fails_closed_for_stream() -> None:
    connection = _connection()
    writer = SQLiteEventLedgerV1(connection, _access())
    reader = SQLiteEventLedgerV1(
        connection,
        _access(allowed=("public", "internal")),
    )
    try:
        request = _batch(
            _access(),
            count=1,
            classification="restricted",
        )
        _append(writer, request)

        assert reader.read_global().events == ()
        with pytest.raises(EventLedgerClassificationDeniedError):
            reader.read_stream("stream_001")
        with pytest.raises(EventLedgerClassificationDeniedError):
            reader.verify_stream("stream_001")
    finally:
        writer.close()
        reader.close()
        connection.close()


def test_sqlite_event_ledger_nested_append_respects_caller_transaction() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        connection.execute("CREATE TABLE caller_work (value TEXT NOT NULL)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_work VALUES ('kept-until-rollback')")
        _append(ledger, request)
        assert connection.in_transaction is True
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (2,)
        connection.rollback()
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM caller_work").fetchone() == (
            0,
        )
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_failed_nested_append_preserves_outer_work() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    first = _batch(_access())
    try:
        _append(ledger, first)
        connection.execute("CREATE TABLE caller_work (value TEXT NOT NULL)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_work VALUES ('retained')")
        stale = _batch(
            _access(),
            expected_version=1,
            first_global_position=3,
            previous_sha256=first.events[-1].event_sha256,
            key_character="c",
            command_character="d",
            event_prefix="evt_nested_stale",
        )

        with pytest.raises(EventLedgerConflictError):
            _append(ledger, stale)

        assert connection.in_transaction is True
        assert connection.execute("SELECT value FROM caller_work").fetchall() == [
            ("retained",)
        ]
        connection.rollback()
        assert ledger.read_global().events == first.events
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_concurrent_exact_replay_is_single_commit() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = tuple(
                executor.map(lambda _index: _append(ledger, request), range(2))
            )

        assert receipts[0] == receipts[1]
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_idempotency"
        ).fetchone() == (1,)
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_schema_drift_and_direct_mutation_fail_closed() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        _append(ledger, request)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE v3_event_ledger_events SET classification = 'public'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM v3_event_ledger_idempotency")

        connection.execute(
            "DROP TRIGGER v3_event_ledger_events_immutable_delete"
        )
        with pytest.raises(SQLiteEventLedgerV1SchemaError):
            ledger.verify_integrity()
    finally:
        ledger.close()
        connection.close()


@pytest.mark.parametrize(
    "requirement",
    ["foreign_keys", "recursive_triggers", "wal", "metadata"],
)
def test_sqlite_event_ledger_rejects_runtime_schema_requirement_drift(
    requirement: str,
) -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    try:
        if requirement == "foreign_keys":
            connection.execute("PRAGMA foreign_keys = OFF")
        elif requirement == "recursive_triggers":
            connection.execute("PRAGMA recursive_triggers = OFF")
        elif requirement == "wal":
            ledger._require_wal = True
        else:
            connection.execute(
                "DROP TRIGGER v3_event_ledger_schema_immutable_delete"
            )
            connection.execute(
                "DELETE FROM trace_backed_memory_v3_event_ledger_schema"
            )
        with pytest.raises(SQLiteEventLedgerV1SchemaError):
            ledger.read_global()
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_maps_missing_and_operational_database_errors() -> None:
    missing_connection = _connection()
    missing = SQLiteEventLedgerV1(missing_connection, _access())
    request = _batch(_access())
    try:
        missing_connection.execute("DROP TABLE v3_event_ledger_events")
        missing._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        with pytest.raises(SQLiteEventLedgerV1SchemaError):
            missing.read_stream("stream_001")
        with pytest.raises(SQLiteEventLedgerV1SchemaError):
            missing.read_global()
        with pytest.raises(SQLiteEventLedgerV1SchemaError):
            missing.verify_integrity()
        with pytest.raises(SQLiteEventLedgerV1SchemaError):
            _append(missing, request)
    finally:
        missing.close()
        missing_connection.close()

    locked_connection = _connection()
    locked = SQLiteEventLedgerV1(locked_connection, _access())

    def raise_locked(*_args: object) -> tuple[CanonicalEvent, ...]:
        raise sqlite3.OperationalError("database is locked")

    try:
        locked._events_from_query = raise_locked  # type: ignore[method-assign]
        with pytest.raises(SQLiteEventLedgerV1PersistenceError):
            locked.read_stream("stream_001")
        with pytest.raises(SQLiteEventLedgerV1PersistenceError):
            locked.read_global()
    finally:
        locked.close()
        locked_connection.close()


def test_sqlite_event_ledger_subscription_is_at_least_once_until_acknowledged() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        _append(ledger, request)
        subscription = ledger.subscribe(limit=1)
        first = subscription.poll()
        assert subscription.poll() == first
        with pytest.raises(EventLedgerConflictError):
            subscription.acknowledge(
                "delivery_wrong",
                expected_next_global_position=first.page.next_global_position,
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
        assert heartbeat.page.events == ()
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
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_backup_restores_exact_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    request = _batch(_access(), artifact_refs=(_artifact(),))
    with SQLiteEventLedgerV1.connect(
        database,
        _access(),
        initialize=True,
    ) as ledger:
        receipt = _append(ledger, request)
        assert ledger.backup(backup) == backup.resolve()
        with pytest.raises(SQLiteEventLedgerV1PersistenceError):
            ledger.backup(backup)
        assert ledger.backup(backup, overwrite=True) == backup.resolve()
        with pytest.raises(ValueError, match="overwrite"):
            ledger.backup(tmp_path / "invalid.sqlite3", overwrite=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="must differ"):
            ledger.backup(database)

    with SQLiteEventLedgerV1.connect(backup, _access()) as restored:
        assert restored.read_global().events == request.events
        assert restored.verify_integrity()[0].valid is True
        assert _append(restored, request) == receipt


def test_sqlite_event_ledger_reports_corrupt_event_and_head_metadata() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        _append(ledger, request)
        connection.execute(
            "DROP TRIGGER v3_event_ledger_events_immutable_update"
        )
        connection.execute(
            "UPDATE v3_event_ledger_events SET canonical_event = '{}' "
            "WHERE event_id = ?",
            (request.events[0].event_id,),
        )
        connection.execute(
            "DROP TRIGGER v3_event_ledger_stream_heads_advance"
        )
        connection.execute(
            "UPDATE v3_event_ledger_stream_heads "
            "SET current_event_id = 'evt_missing'"
        )
        ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        verification = ledger.verify_stream("stream_001")
        assert verification.valid is False
        assert "EVENT_HASH_MISMATCH" in verification.issue_codes
        assert "HASH_CHAIN_MISMATCH" in verification.issue_codes
        assert "HEAD_MISMATCH" in verification.issue_codes
    finally:
        ledger.close()
        connection.close()


@pytest.mark.parametrize(
    ("corruption", "issue"),
    [
        ("stream_version", "STREAM_VERSION_GAP"),
        ("partition", "PARTITION_MISMATCH"),
        ("global_position", "GLOBAL_POSITION_INVALID"),
    ],
)
def test_sqlite_event_ledger_reports_semantic_event_corruption(
    corruption: str,
    issue: str,
) -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        _append(ledger, request)
        replacement_access = (
            _access(tenant_id="tenant_corrupt")
            if corruption == "partition"
            else _access()
        )
        replacement = _event(
            replacement_access,
            stream_id="stream_001",
            stream_version=3 if corruption == "stream_version" else 1,
            global_position=3 if corruption == "global_position" else 1,
            previous_sha256=(
                request.events[1].event_sha256
                if corruption == "stream_version"
                else None
            ),
            event_id=request.events[0].event_id,
            idempotency_key_sha256=request.idempotency.idempotency_key_sha256,
            command_sha256=request.idempotency.command_sha256,
        )
        connection.execute(
            "DROP TRIGGER v3_event_ledger_events_immutable_update"
        )
        connection.execute(
            "UPDATE v3_event_ledger_events SET event_sha256 = ?, "
            "organization_id = ?, tenant_id = ?, repository_id = ?, "
            "environment_id = ?, stream_version = ?, global_position = ?, "
            "previous_stream_event_sha256 = ?, classification = ?, "
            "artifact_ref_count = ?, canonical_event = ? WHERE event_id = ?",
            (
                replacement.event_sha256,
                replacement.organization_id,
                replacement.tenant_id,
                replacement.repository_id,
                replacement.environment_id,
                replacement.stream_version,
                replacement.global_position,
                replacement.previous_stream_event_sha256,
                replacement.classification,
                len(replacement.artifact_refs),
                dumps_canonical_event(replacement),
                replacement.event_id,
            ),
        )
        ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        verification = ledger.verify_stream("stream_001")
        assert verification.valid is False
        assert issue in verification.issue_codes
    finally:
        ledger.close()
        connection.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "event_columns",
        "artifact",
        "idempotency_scalar",
        "idempotency_missing",
        "idempotency_receipt",
    ],
)
def test_sqlite_event_ledger_rejects_corrupt_stored_rows(
    corruption: str,
) -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access(), artifact_refs=(_artifact(),))
    try:
        _append(ledger, request)
        if corruption == "event_columns":
            connection.execute(
                "DROP TRIGGER v3_event_ledger_events_immutable_update"
            )
            connection.execute(
                "UPDATE v3_event_ledger_events SET classification = 'public' "
                "WHERE stream_version = 1"
            )
        elif corruption == "artifact":
            connection.execute(
                "DROP TRIGGER v3_event_ledger_artifacts_immutable_update"
            )
            connection.execute(
                "UPDATE v3_event_ledger_artifacts SET descriptor = '{}'"
            )
        else:
            connection.execute(
                "DROP TRIGGER v3_event_ledger_idempotency_immutable_update"
            )
            if corruption == "idempotency_scalar":
                connection.execute(
                    "UPDATE v3_event_ledger_idempotency "
                    "SET event_sha256s_json = 'null'"
                )
            elif corruption == "idempotency_missing":
                connection.execute(
                    "UPDATE v3_event_ledger_idempotency "
                    "SET event_sha256s_json = ?",
                    (json.dumps([_digest("9")], separators=(",", ":")),),
                )
            else:
                connection.execute(
                    "UPDATE v3_event_ledger_idempotency "
                    "SET current_stream_version = 3"
                )
        ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        with pytest.raises(SQLiteEventLedgerV1IntegrityError):
            if corruption.startswith("idempotency"):
                _append(ledger, request)
            else:
                ledger.read_global()
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_integrity_rejects_global_gap() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        _append(ledger, request)
        replacement = _event(
            _access(),
            stream_id="stream_001",
            stream_version=1,
            global_position=3,
            previous_sha256=None,
            event_id=request.events[0].event_id,
            idempotency_key_sha256=request.idempotency.idempotency_key_sha256,
            command_sha256=request.idempotency.command_sha256,
        )
        connection.execute(
            "DROP TRIGGER v3_event_ledger_events_immutable_update"
        )
        connection.execute(
            "UPDATE v3_event_ledger_events SET event_sha256 = ?, "
            "global_position = ?, canonical_event = ? WHERE event_id = ?",
            (
                replacement.event_sha256,
                replacement.global_position,
                dumps_canonical_event(replacement),
                replacement.event_id,
            ),
        )
        ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        with pytest.raises(
            SQLiteEventLedgerV1IntegrityError,
            match="global event positions",
        ):
            ledger.verify_integrity()
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_integrity_rejects_noncanonical_checkpoint() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        _append(ledger, request)
        connection.execute(
            "INSERT INTO v3_event_ledger_checkpoints ("
            "projection_name, projection_version, partition_sha256, "
            "global_position, state_sha256, descriptor"
            ") VALUES ('projection_test', 1, ?, 2, ?, ?)",
            (
                _access().partition.partition_sha256,
                _digest("d"),
                '{"b":1,"a":2}',
            ),
        )
        with pytest.raises(
            SQLiteEventLedgerV1IntegrityError,
            match="noncanonical",
        ):
            ledger.verify_integrity()
    finally:
        ledger.close()
        connection.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "global_head",
        "stream_head",
        "checkpoint",
        "partition_hash",
        "stream_version",
        "hash_chain",
        "missing_global_head",
        "checkpoint_json",
        "checkpoint_identity",
    ],
)
def test_sqlite_event_ledger_integrity_rejects_metadata_corruption(
    corruption: str,
) -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    request = _batch(_access())
    try:
        _append(ledger, request)
        if corruption == "global_head":
            connection.execute(
                "DROP TRIGGER v3_event_ledger_global_head_advance"
            )
            connection.execute(
                "UPDATE v3_event_ledger_global_head "
                "SET current_global_position = 99"
            )
            expected = "global ledger head"
            ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        elif corruption == "stream_head":
            connection.execute(
                "DROP TRIGGER v3_event_ledger_stream_heads_no_delete"
            )
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM v3_event_ledger_stream_heads")
            expected = "stream head count"
            ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        elif corruption == "checkpoint":
            connection.execute(
                "INSERT INTO v3_event_ledger_checkpoints ("
                "projection_name, projection_version, partition_sha256, "
                "global_position, state_sha256, descriptor"
                ") VALUES ('projection_invalid', 1, ?, 99, ?, '{}')",
                (_access().partition.partition_sha256, _digest("c")),
            )
            expected = "invalid shape"
        elif corruption == "partition_hash":
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "DROP TRIGGER v3_event_ledger_events_immutable_update"
            )
            connection.execute(
                "UPDATE v3_event_ledger_events SET partition_sha256 = ? "
                "WHERE stream_version = 1",
                (_digest("9"),),
            )
            expected = "partition hash"
            ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        elif corruption == "stream_version":
            replacement = _event(
                _access(),
                stream_id="stream_001",
                stream_version=3,
                global_position=1,
                previous_sha256=request.events[1].event_sha256,
                event_id=request.events[0].event_id,
                idempotency_key_sha256=request.idempotency.idempotency_key_sha256,
                command_sha256=request.idempotency.command_sha256,
            )
            connection.execute(
                "DROP TRIGGER v3_event_ledger_events_immutable_update"
            )
            connection.execute(
                "UPDATE v3_event_ledger_events SET event_sha256 = ?, "
                "stream_version = ?, previous_stream_event_sha256 = ?, "
                "canonical_event = ? WHERE event_id = ?",
                (
                    replacement.event_sha256,
                    replacement.stream_version,
                    replacement.previous_stream_event_sha256,
                    dumps_canonical_event(replacement),
                    replacement.event_id,
                ),
            )
            expected = "stream versions"
            ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        elif corruption == "hash_chain":
            replacement = _event(
                _access(),
                stream_id="stream_001",
                stream_version=2,
                global_position=2,
                previous_sha256=_digest("9"),
                event_id=request.events[1].event_id,
                idempotency_key_sha256=request.idempotency.idempotency_key_sha256,
                command_sha256=request.idempotency.command_sha256,
            )
            connection.execute(
                "DROP TRIGGER v3_event_ledger_events_immutable_update"
            )
            connection.execute(
                "UPDATE v3_event_ledger_events SET event_sha256 = ?, "
                "previous_stream_event_sha256 = ?, canonical_event = ? "
                "WHERE event_id = ?",
                (
                    replacement.event_sha256,
                    replacement.previous_stream_event_sha256,
                    dumps_canonical_event(replacement),
                    replacement.event_id,
                ),
            )
            expected = "hash chain"
            ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        elif corruption == "missing_global_head":
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "DROP TRIGGER v3_event_ledger_global_head_no_delete"
            )
            connection.execute("DELETE FROM v3_event_ledger_global_head")
            expected = "global ledger head is missing"
            ledger._require_schema = lambda _cursor: None  # type: ignore[method-assign]
        elif corruption == "checkpoint_json":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "INSERT INTO v3_event_ledger_checkpoints ("
                "projection_name, projection_version, partition_sha256, "
                "global_position, state_sha256, descriptor"
                ") VALUES ('projection_bad_json', 1, ?, 2, ?, '{]')",
                (_access().partition.partition_sha256, _digest("b")),
            )
            expected = "descriptor is invalid"
        else:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "INSERT INTO v3_event_ledger_checkpoints ("
                "projection_name, projection_version, partition_sha256, "
                "global_position, state_sha256, descriptor"
                ") VALUES ('', 0, 'bad', 2, ?, '{}')",
                (_digest("b"),),
            )
            expected = "invalid shape"
        with pytest.raises(SQLiteEventLedgerV1IntegrityError, match=expected):
            ledger.verify_integrity()
    finally:
        ledger.close()
        connection.close()


def test_sqlite_event_ledger_maps_backup_and_close_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    ledger = SQLiteEventLedgerV1.connect(
        database,
        _access(),
        initialize=True,
    )

    def fail_verification() -> tuple[object, ...]:
        raise OSError("verification failed")

    monkeypatch.setattr(ledger, "verify_integrity", fail_verification)
    with pytest.raises(SQLiteEventLedgerV1PersistenceError):
        ledger.backup(tmp_path / "failed.sqlite3")
    ledger.close()

    connection = _connection()
    close_ledger = SQLiteEventLedgerV1(connection, _access())

    class BrokenLock:
        def __exit__(self, *_args: object) -> None:
            raise OSError("lock release failed")

    close_ledger._file_lock = BrokenLock()  # type: ignore[assignment]
    with pytest.raises(SQLiteEventLedgerV1PersistenceError, match="close"):
        close_ledger.close()
    close_ledger.close()
    connection.close()


def test_sqlite_event_ledger_closes_after_repeated_rollback_failure() -> None:
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    original_connection = ledger._connection

    class BrokenRollback:
        in_transaction = True

        def rollback(self) -> None:
            raise OSError("rollback failed")

        def close(self) -> None:
            raise OSError("close failed")

    primary = RuntimeError("primary failure")
    ledger._connection = BrokenRollback()  # type: ignore[assignment]
    ledger._rollback_connection_or_close(primary, context="test transaction")
    assert ledger._closed is True
    assert len(primary.__notes__) == 3
    ledger._connection = original_connection
    ledger._closed = False
    ledger.close()
    connection.close()


def test_sqlite_event_ledger_rejects_malformed_or_oversized_direct_rows() -> None:
    connection = _connection()
    try:
        partition = _access().partition
        connection.execute(
            "INSERT INTO v3_event_ledger_stream_heads ("
            "partition_sha256, stream_id, organization_id, tenant_id, "
            "repository_id, environment_id, current_stream_version, "
            "current_event_id, current_event_sha256"
            ") VALUES (?, 'stream_direct', ?, ?, ?, ?, 0, NULL, NULL)",
            (
                partition.partition_sha256,
                partition.organization_id,
                partition.tenant_id,
                partition.repository_id,
                partition.environment_id,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO v3_event_ledger_events ("
                "event_id, event_sha256, partition_sha256, organization_id, "
                "tenant_id, repository_id, environment_id, stream_id, "
                "stream_version, global_position, previous_stream_event_sha256, "
                "classification, artifact_ref_count, canonical_event"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'stream_direct', 1, 1, NULL, "
                "'internal', 0, ?)",
                (
                    "evt_direct_oversized",
                    _digest("f"),
                    partition.partition_sha256,
                    partition.organization_id,
                    partition.tenant_id,
                    partition.repository_id,
                    partition.environment_id,
                    json.dumps({"value": "x" * 1_048_576}),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO v3_event_ledger_events ("
                "event_id, event_sha256, partition_sha256, organization_id, "
                "tenant_id, repository_id, environment_id, stream_id, "
                "stream_version, global_position, previous_stream_event_sha256, "
                "classification, artifact_ref_count, canonical_event"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'stream_direct', 1, 1, NULL, "
                "'internal', 0, ?)",
                (
                    "evt_direct_malformed",
                    _digest("e"),
                    partition.partition_sha256,
                    partition.organization_id,
                    partition.tenant_id,
                    partition.repository_id,
                    partition.environment_id,
                    "{]",
                ),
            )
    finally:
        connection.close()
