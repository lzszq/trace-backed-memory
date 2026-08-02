from __future__ import annotations

from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import EventTrustedContext
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.reducer import ReducerEvent, execute_reducer_step


ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = "sha256:" + "a" * 64


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for name in (
        "postgres-v3-gate-session.sql",
        "postgres-v3-event-ledger.sql",
    ):
        installed = cluster.run_script(ROOT / "schemas" / name)
        assert installed.returncode == 0, installed.stderr


def _trusted() -> EventTrustedContext:
    return EventTrustedContext(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repo_001",
        environment_id="environment_local",
        principal_id="principal_001",
        agent_client_id="agent_001",
        actor_type="agent_client",
        actor_id="agent_001",
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


def _create(repository: tbm.PostgresGateSessionRepository):
    return repository.create_or_get(
        session_id="session_event_first_001",
        tenant_id="tenant_001",
        repository_id="repo_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=FINGERPRINT,
        idempotency_key="idempotency_001",
        expires_in_seconds=3600,
    )


def _prepare(repository: tbm.PostgresGateSessionRepository):
    return repository.transition(
        "session_event_first_001",
        "prepared",
        expected_version=1,
        lease_seconds=600,
        retrieval_snapshot_id="retrieval_001",
        system_gate_evaluation_id="system_gate_001",
    )


def test_postgres_event_first_gate_session_rebuilds_exact_projection(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        repository.enable_event_first()
        with repository.bind_event_context(_trusted()):
            created = _create(repository).session
            prepared = _prepare(repository)

        ledger = PostgresEventLedgerV1(connection, _access())
        page = ledger.read_stream(created.session_id)
        assert tuple(event.event_type for event in page.events) == (
            tbm.GATE_SESSION_CREATED_EVENT,
            tbm.GATE_SESSION_PREPARED_EVENT,
        )
        reducer = tbm.build_gate_session_reducer()
        state = reducer.initial_state()
        for event in page.events:
            state = execute_reducer_step(
                reducer,
                state,
                ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
            ).state
        tbm.verify_gate_session_projection_parity(state, (prepared,))


def test_postgres_event_first_rolls_back_event_after_projection_failure(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        repository.enable_event_first()
        with repository.bind_event_context(_trusted()):
            created = _create(repository).session

        def fail_projection(*_args: object) -> None:
            raise RuntimeError("synthetic projection failure")

        monkeypatch.setattr(
            tbm.PostgresGateSessionRepository,
            "_insert_revision",
            staticmethod(fail_projection),
        )
        with repository.bind_event_context(_trusted()):
            with pytest.raises(RuntimeError, match="synthetic projection"):
                _prepare(repository)

        assert repository.get(created.session_id) == created
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_global_position "
                "FROM trace_backed_memory_v3_event_ledger.global_head"
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                "SELECT count(*) "
                "FROM trace_backed_memory_v3_event_ledger.events"
            )
            assert cursor.fetchone()[0] == 1
