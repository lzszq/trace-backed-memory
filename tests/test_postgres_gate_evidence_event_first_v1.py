from __future__ import annotations

from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import EventTrustedContext
from trace_backed_memory.gate_evidence_reducer_v1 import (
    verify_gate_evidence_projection_parity,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3PersistenceError,
    PostgresGateEvidenceV3Repository,
)
from trace_backed_memory.reducer import ReducerEvent, execute_reducer_step


ROOT = Path(__file__).resolve().parents[1]


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for name in (
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-event-ledger.sql",
    ):
        installed = cluster.run_script(ROOT / "schemas" / name)
        assert installed.returncode == 0, installed.stderr


def _records() -> tuple[tbm.RetrievalSnapshot, tbm.SystemGateEvaluation]:
    snapshot = tbm.loads_retrieval_snapshot(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
    )
    evaluation = tbm.loads_system_gate_evaluation(
        (
            ROOT / "examples" / "system_gate_evaluation_v3.example.json"
        ).read_bytes()
    )
    return snapshot, evaluation


def _trusted(authorization_event_id: str) -> EventTrustedContext:
    return EventTrustedContext(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_local",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id=authorization_event_id,
    )


def _access(trusted: EventTrustedContext) -> LedgerAccessContext:
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


def test_postgres_gate_evidence_event_first_rebuilds_exact_projection(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    trusted = _trusted(snapshot.authorization_event_id)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresGateEvidenceV3Repository(connection)
        repository.enable_event_first()
        with repository.bind_event_context(trusted):
            first = repository.store_bundle(snapshot, evaluation)
            replay = repository.store_bundle(snapshot, evaluation)

        ledger = PostgresEventLedgerV1(connection, _access(trusted))
        page = ledger.read_global(0, 10)
        reducer = tbm.build_gate_evidence_reducer()
        state = reducer.initial_state()
        for event in page.events:
            state = execute_reducer_step(
                reducer,
                state,
                ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
            ).state

        assert first.snapshot_inserted is True
        assert first.evaluation_inserted is True
        assert replay.snapshot_inserted is False
        assert replay.evaluation_inserted is False
        assert tuple(event.event_type for event in page.events) == (
            tbm.RETRIEVAL_PREPARED_EVENT,
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
        )
        verify_gate_evidence_projection_parity(
            state,
            (snapshot,),
            (evaluation,),
        )


def test_postgres_gate_evidence_event_rolls_back_on_projection_failure(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    trusted = _trusted(snapshot.authorization_event_id)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresGateEvidenceV3Repository(connection)
        repository.enable_event_first()

        def fail_projection(*_args: object) -> bool:
            raise RuntimeError("synthetic evidence projection failure")

        monkeypatch.setattr(
            PostgresGateEvidenceV3Repository,
            "_put_snapshot",
            staticmethod(fail_projection),
        )
        with repository.bind_event_context(trusted):
            with pytest.raises(PostgresGateEvidenceV3PersistenceError):
                repository.store_bundle(snapshot, evaluation)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) "
                "FROM trace_backed_memory_v3_event_ledger.events"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) "
                "FROM trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT current_global_position "
                "FROM trace_backed_memory_v3_event_ledger.global_head"
            )
            assert cursor.fetchone()[0] == 0
