from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import EventTrustedContext
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.projection import ProjectionRuntime
from trace_backed_memory.reducer import ReducerEvent, execute_reducer_step
from trace_backed_memory.reducer_registry import build_default_reducer_registry
from trace_backed_memory.sqlite_bundle_v3 import install_sqlite_v3_bundle
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.sqlite_gate_session_v3 import (
    SQLiteGateSessionConflictError,
    SQLiteGateSessionPersistenceError,
    SQLiteGateSessionRepository,
)


HASH = "sha256:" + "a" * 64


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._value
        self._value += timedelta(minutes=1)
        return value.isoformat().replace("+00:00", "Z")


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(connection)
    return connection


def _trusted() -> EventTrustedContext:
    return EventTrustedContext(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_local",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id="authorization_decision_001",
    )


def _access() -> LedgerAccessContext:
    trusted = _trusted()
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            trusted.organization_id,
            trusted.tenant_id,
            trusted.repository_id,
            trusted.environment_id,
        ),
        principal_id=trusted.principal_id,
        agent_client_id=trusted.agent_client_id,
        actor_type=trusted.actor_type,
        actor_id=trusted.actor_id,
        authorization_decision_id=trusted.authorization_decision_id,
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _create(repository: SQLiteGateSessionRepository):
    return repository.create_or_get(
        session_id="gate_session_event_first_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=HASH,
        idempotency_key="request-001",
        expires_in_seconds=3600,
    )


def _prepare(
    repository: SQLiteGateSessionRepository,
    *,
    expected_version: int = 1,
) -> tbm.GateSession:
    return repository.transition(
        "gate_session_event_first_001",
        "prepared",
        expected_version=expected_version,
        lease_seconds=600,
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id="system_gate_001",
    )


def test_sqlite_event_first_gate_session_rebuilds_exact_projection() -> None:
    connection = _connection()
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    repository.enable_event_first()
    try:
        with repository.bind_event_context(_trusted()):
            created = _create(repository).session
            prepared = _prepare(repository)

        ledger = SQLiteEventLedgerV1(connection, _access())
        try:
            page = ledger.read_stream(created.session_id)
        finally:
            ledger.close()
        assert tuple(event.event_type for event in page.events) == (
            tbm.GATE_SESSION_CREATED_EVENT,
            tbm.GATE_SESSION_PREPARED_EVENT,
        )
        assert tuple(event.stream_version for event in page.events) == (1, 2)
        assert repository.get(created.session_id) == prepared

        reducer = tbm.build_gate_session_reducer()
        state = reducer.initial_state()
        for event in page.events:
            state = execute_reducer_step(
                reducer,
                state,
                ReducerEvent(
                    event,
                    DEFAULT_EVENT_TYPE_REGISTRY.consume(event),
                ),
            ).state
        tbm.verify_gate_session_projection_parity(state, (prepared,))
    finally:
        repository.close()
        connection.close()


def test_sqlite_gate_session_projection_rebuild_resume_activate_and_rollback() -> None:
    connection = _connection()
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    repository.enable_event_first()
    ledger = SQLiteEventLedgerV1(connection, _access())
    runtime = ProjectionRuntime(
        ledger,
        build_default_reducer_registry(),
        ledger,
        event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
    )
    partition_sha256 = ledger.access_context.partition.partition_sha256
    try:
        with repository.bind_event_context(_trusted()):
            created = _create(repository).session
        initial = runtime.rebuild(
            tbm.GATE_SESSION_REDUCER_ID,
            1,
            partition_sha256=partition_sha256,
            owner="projection_operator",
            rebuild_generation=1,
            page_size=1,
            checkpoint_interval=1,
            created_at="2026-08-01T01:00:00.000000Z",
        )
        initial_activation = runtime.activate(
            initial.checkpoint.build_id,
            owner="projection_operator",
            approved=True,
            expected_head_version=0,
            expected_current_build_id=None,
            created_at="2026-08-01T01:00:01.000000Z",
        )

        with repository.bind_event_context(_trusted()):
            prepared = _prepare(repository)
        resumed = runtime.rebuild(
            tbm.GATE_SESSION_REDUCER_ID,
            1,
            partition_sha256=partition_sha256,
            owner="projection_operator",
            rebuild_generation=2,
            page_size=1,
            checkpoint_interval=1,
            resume=True,
            created_at="2026-08-01T01:00:02.000000Z",
        )
        comparison = runtime.compare(
            initial.checkpoint.build_id,
            resumed.checkpoint.build_id,
        )
        resumed_activation = runtime.activate(
            resumed.checkpoint.build_id,
            owner="projection_operator",
            approved=True,
            expected_head_version=1,
            expected_current_build_id=initial.checkpoint.build_id,
            comparison=comparison,
            created_at="2026-08-01T01:00:03.000000Z",
        )
        rollback = runtime.rollback(
            tbm.GATE_SESSION_PROJECTION_NAME,
            partition_sha256,
            owner="projection_operator",
            expected_head_version=2,
            expected_current_build_id=resumed.checkpoint.build_id,
            created_at="2026-08-01T01:00:04.000000Z",
        )

        assert initial.status == "completed"
        assert initial.processed_events == 1
        assert tbm.projected_gate_session(
            initial.checkpoint.state,
            created.session_id,
        ) == created
        assert resumed.status == "completed"
        assert resumed.resumed_from_build_id == initial.checkpoint.build_id
        assert resumed.processed_events == 1
        assert resumed.checkpoint.global_position == 2
        assert ledger.load_checkpoint(resumed.checkpoint.build_id) == resumed.checkpoint
        tbm.verify_gate_session_projection_parity(
            resumed.checkpoint.state,
            (prepared,),
        )
        assert comparison.equivalent is False
        assert comparison.active_build_id == initial.checkpoint.build_id
        assert comparison.shadow_build_id == resumed.checkpoint.build_id
        assert initial_activation.head_version == 1
        assert resumed_activation.head_version == 2
        assert rollback.head_version == 3
        assert rollback.operation == "rollback"
        assert rollback.target_build_id == initial.checkpoint.build_id
        assert ledger.current_activation(
            tbm.GATE_SESSION_PROJECTION_NAME,
            partition_sha256,
        ) == rollback
        assert ledger.verify_integrity()[0].valid is True
    finally:
        ledger.close()
        repository.close()
        connection.close()


def test_sqlite_event_first_requires_trusted_context_without_partial_rows() -> None:
    connection = _connection()
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    repository.enable_event_first()
    try:
        with pytest.raises(SQLiteGateSessionConflictError) as raised:
            _create(repository)
        assert raised.value.code == (
            "TBM_SQLITE_GATE_SESSION_EVENT_CONTEXT_REQUIRED"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM gate_session_revisions"
        ).fetchone() == (0,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_event_first_rolls_back_event_when_projection_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    repository.enable_event_first()
    try:
        with repository.bind_event_context(_trusted()):
            created = _create(repository).session

        def fail_projection(*_args: object) -> None:
            raise sqlite3.OperationalError("synthetic projection failure")

        monkeypatch.setattr(
            SQLiteGateSessionRepository,
            "_insert_revision",
            staticmethod(fail_projection),
        )
        with repository.bind_event_context(_trusted()):
            with pytest.raises(SQLiteGateSessionPersistenceError):
                _prepare(repository)

        assert repository.get(created.session_id) == created
        assert connection.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM gate_session_revisions"
        ).fetchone() == (1,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_event_first_respects_caller_transaction_rollback() -> None:
    connection = _connection()
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    repository.enable_event_first()
    try:
        with repository.bind_event_context(_trusted()):
            created = _create(repository).session
        connection.execute("BEGIN IMMEDIATE")
        with repository.bind_event_context(_trusted()):
            prepared = _prepare(repository)
        assert repository.get(created.session_id) == prepared
        assert connection.in_transaction is True
        connection.rollback()

        assert repository.get(created.session_id) == created
        assert connection.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head"
        ).fetchone() == (1,)
    finally:
        repository.close()
        connection.close()
