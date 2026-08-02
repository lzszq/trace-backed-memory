from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
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
from trace_backed_memory.projection import ProjectionRuntime
from trace_backed_memory.reducer_registry import build_default_reducer_registry
from trace_backed_memory.sqlite_bundle_v3 import install_sqlite_v3_bundle
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.sqlite_gate_evidence_v3 import (
    SQLiteGateEvidenceV3ConflictError,
    SQLiteGateEvidenceV3PersistenceError,
    SQLiteGateEvidenceV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]


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


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(connection)
    return connection


def test_sqlite_gate_evidence_event_first_rebuilds_exact_projection() -> None:
    snapshot, evaluation = _records()
    trusted = _trusted(snapshot.authorization_event_id)
    connection = _connection()
    repository = SQLiteGateEvidenceV3Repository(connection)
    repository.enable_event_first()
    ledger = SQLiteEventLedgerV1(connection, _access(trusted))
    try:
        with repository.bind_event_context(trusted):
            first = repository.store_bundle(snapshot, evaluation)
            replay = repository.store_bundle(snapshot, evaluation)

        page = ledger.read_global(0, 10)
        runtime = ProjectionRuntime(
            ledger,
            build_default_reducer_registry(),
            ledger,
            event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
        )
        rebuilt = runtime.rebuild(
            tbm.GATE_EVIDENCE_REDUCER_ID,
            1,
            partition_sha256=trusted_context_partition_sha256(trusted),
            owner="projection_operator",
            rebuild_generation=1,
            page_size=1,
            checkpoint_interval=1,
            created_at="2026-08-01T02:00:00.000000Z",
        )

        assert first.snapshot_inserted is True
        assert first.evaluation_inserted is True
        assert replay.snapshot_inserted is False
        assert replay.evaluation_inserted is False
        assert tuple(event.event_type for event in page.events) == (
            tbm.RETRIEVAL_PREPARED_EVENT,
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
        )
        assert tuple(event.stream_version for event in page.events) == (1, 1)
        assert page.events[1].causation_id == page.events[0].event_id
        assert rebuilt.status == "completed"
        assert rebuilt.processed_events == 2
        verify_gate_evidence_projection_parity(
            rebuilt.checkpoint.state,
            (snapshot,),
            (evaluation,),
        )
        assert repository.load_snapshot(snapshot.snapshot_id) == snapshot
        assert repository.load_evaluation(evaluation.evaluation_id) == evaluation
    finally:
        ledger.close()
        repository.close()
        connection.close()


def test_sqlite_gate_evidence_event_first_requires_context_without_rows() -> None:
    snapshot, evaluation = _records()
    connection = _connection()
    repository = SQLiteGateEvidenceV3Repository(connection)
    repository.enable_event_first()
    try:
        with pytest.raises(SQLiteGateEvidenceV3ConflictError):
            repository.store_bundle(snapshot, evaluation)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_retrieval_snapshots"
        ).fetchone() == (0,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_gate_evidence_event_rolls_back_on_projection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, evaluation = _records()
    trusted = _trusted(snapshot.authorization_event_id)
    connection = _connection()
    repository = SQLiteGateEvidenceV3Repository(connection)
    repository.enable_event_first()

    def fail_projection(*_args: object) -> bool:
        raise sqlite3.OperationalError("synthetic evidence projection failure")

    monkeypatch.setattr(
        SQLiteGateEvidenceV3Repository,
        "_put_snapshot",
        staticmethod(fail_projection),
    )
    try:
        with repository.bind_event_context(trusted):
            with pytest.raises(SQLiteGateEvidenceV3PersistenceError):
                repository.store_bundle(snapshot, evaluation)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_retrieval_snapshots"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head"
        ).fetchone() == (0,)
    finally:
        repository.close()
        connection.close()


def trusted_context_partition_sha256(trusted: EventTrustedContext) -> str:
    return LedgerTenantPartition(
        trusted.organization_id,
        trusted.tenant_id,
        trusted.repository_id,
        trusted.environment_id,
    ).partition_sha256
