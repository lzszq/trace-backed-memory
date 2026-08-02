from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
from tests.test_semantic_gate_attempt_event_v1 import _attempts, _trusted
from tests.test_sqlite_semantic_gate_artifact_v3 import _evidence
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.projection import ProjectionRuntime
from trace_backed_memory.reducer_registry import build_default_reducer_registry
from trace_backed_memory.semantic_gate_attempt_reducer_v1 import (
    verify_semantic_gate_attempt_projection_parity,
)
from trace_backed_memory.sqlite_bundle_v3 import install_sqlite_v3_bundle
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.sqlite_gate_evidence_v3 import (
    SQLiteGateEvidenceV3Repository,
)
from trace_backed_memory.sqlite_semantic_gate_artifact_v3 import (
    SQLiteSemanticGateArtifactV3ConflictError,
    SQLiteSemanticGateArtifactV3PersistenceError,
    SQLiteSemanticGateArtifactV3Repository,
)


def _access(trusted: tbm.EventTrustedContext) -> LedgerAccessContext:
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


def _seed_evidence(
    connection: sqlite3.Connection,
) -> tuple[tbm.EventTrustedContext, SQLiteGateEvidenceV3Repository]:
    snapshot, evaluation = _evidence()
    trusted = _trusted(snapshot.authorization_event_id)
    repository = SQLiteGateEvidenceV3Repository(connection)
    repository.enable_event_first()
    with repository.bind_event_context(trusted):
        repository.store_bundle(snapshot, evaluation)
    return trusted, repository


def test_sqlite_semantic_attempt_event_first_rebuilds_exact_projection() -> None:
    failed, succeeded = _attempts()
    connection = _connection()
    trusted, evidence = _seed_evidence(connection)
    repository = SQLiteSemanticGateArtifactV3Repository(connection)
    repository.enable_event_first()
    ledger = SQLiteEventLedgerV1(connection, _access(trusted))
    try:
        with repository.bind_event_context(
            replace(
                trusted,
                authorization_decision_id="authorization_transition_001",
            )
        ):
            first = repository.store_attempt_with_artifacts(
                failed.attempt,
                failed.prompt,
                failed.response,
            )
        with repository.bind_event_context(
            replace(
                trusted,
                authorization_decision_id="authorization_transition_002",
            )
        ):
            second = repository.store_attempt_with_artifacts(
                succeeded.attempt,
                succeeded.prompt,
                succeeded.response,
            )
        with repository.bind_event_context(
            replace(
                trusted,
                authorization_decision_id="authorization_transition_003",
            )
        ):
            second_replay = repository.store_attempt_with_artifacts(
                succeeded.attempt,
                succeeded.prompt,
                succeeded.response,
            )
        with repository.bind_event_context(
            replace(
                trusted,
                authorization_decision_id="authorization_transition_004",
            )
        ):
            old_replay = repository.store_attempt_with_artifacts(
                failed.attempt,
                failed.prompt,
                failed.response,
            )

        page = ledger.read_global(0, 10)
        runtime = ProjectionRuntime(
            ledger,
            build_default_reducer_registry(),
            ledger,
            event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
        )
        rebuilt = runtime.rebuild(
            tbm.SEMANTIC_GATE_ATTEMPT_REDUCER_ID,
            1,
            partition_sha256=_access(trusted).partition.partition_sha256,
            owner="projection_operator",
            rebuild_generation=1,
            page_size=1,
            checkpoint_interval=1,
            created_at="2026-08-02T01:00:00.000000Z",
        )

        assert first.attempt.inserted is True
        assert second.attempt.inserted is True
        assert second_replay.attempt.inserted is False
        assert old_replay.attempt.inserted is False
        assert tuple(event.event_type for event in page.events) == (
            tbm.RETRIEVAL_PREPARED_EVENT,
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
            tbm.SEMANTIC_GATE_ATTEMPT_FAILED_EVENT,
            tbm.SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
        )
        semantic_events = page.events[2:]
        assert tuple(event.stream_version for event in semantic_events) == (1, 2)
        assert semantic_events[1].causation_id == semantic_events[0].event_id
        assert semantic_events[1].previous_stream_event_sha256 == (
            semantic_events[0].event_sha256
        )
        assert rebuilt.status == "completed"
        assert rebuilt.processed_events == 4
        verify_semantic_gate_attempt_projection_parity(
            rebuilt.checkpoint.state,
            (failed, succeeded),
            (page.events[1], *semantic_events),
        )
        assert repository.load_attempt_with_artifacts(
            failed.attempt.attempt_id
        ) == failed
        assert repository.load_attempt_with_artifacts(
            succeeded.attempt.attempt_id
        ) == succeeded
    finally:
        ledger.close()
        repository.close()
        evidence.close()
        connection.close()


def test_sqlite_semantic_attempt_event_first_requires_context() -> None:
    failed, _succeeded = _attempts()
    connection = _connection()
    _trusted_context, evidence = _seed_evidence(connection)
    repository = SQLiteSemanticGateArtifactV3Repository(connection)
    repository.enable_event_first()
    try:
        with pytest.raises(SQLiteSemanticGateArtifactV3ConflictError):
            repository.store_attempt_with_artifacts(
                failed.attempt,
                failed.prompt,
                failed.response,
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_semantic_gate_attempts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (2,)
    finally:
        repository.close()
        evidence.close()
        connection.close()


def test_sqlite_semantic_attempt_event_first_requires_retained_system_event() -> None:
    failed, _succeeded = _attempts()
    snapshot, evaluation = _evidence()
    trusted = _trusted(snapshot.authorization_event_id)
    connection = _connection()
    evidence = SQLiteGateEvidenceV3Repository(connection)
    evidence.store_bundle(snapshot, evaluation)
    repository = SQLiteSemanticGateArtifactV3Repository(connection)
    repository.enable_event_first()
    try:
        with repository.bind_event_context(trusted):
            with pytest.raises(SQLiteSemanticGateArtifactV3ConflictError):
                repository.store_attempt_with_artifacts(
                    failed.attempt,
                    failed.prompt,
                    failed.response,
                )
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_semantic_gate_attempts"
        ).fetchone() == (0,)
    finally:
        repository.close()
        evidence.close()
        connection.close()


def test_sqlite_semantic_attempt_event_rejects_mismatched_trusted_scope() -> None:
    failed, _succeeded = _attempts()
    connection = _connection()
    trusted, evidence = _seed_evidence(connection)
    repository = SQLiteSemanticGateArtifactV3Repository(connection)
    repository.enable_event_first()
    other_scope = replace(trusted, tenant_id="tenant_other")
    try:
        with repository.bind_event_context(other_scope):
            with pytest.raises(SQLiteSemanticGateArtifactV3ConflictError):
                repository.store_attempt_with_artifacts(
                    failed.attempt,
                    failed.prompt,
                    failed.response,
                )
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_semantic_gate_attempts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (2,)
    finally:
        repository.close()
        evidence.close()
        connection.close()


def test_sqlite_semantic_attempt_event_preserves_caller_transaction() -> None:
    failed, _succeeded = _attempts()
    connection = _connection()
    trusted, evidence = _seed_evidence(connection)
    repository = SQLiteSemanticGateArtifactV3Repository(connection)
    repository.enable_event_first()
    try:
        connection.execute("CREATE TABLE caller_work (value TEXT NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO caller_work VALUES ('before')")
        with repository.bind_event_context(trusted):
            repository.store_attempt_with_artifacts(
                failed.attempt,
                failed.prompt,
                failed.response,
            )
        assert connection.in_transaction
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (3,)
        connection.rollback()
        assert connection.execute("SELECT * FROM caller_work").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_semantic_gate_attempts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head"
        ).fetchone() == (2,)
    finally:
        repository.close()
        evidence.close()
        connection.close()


def test_sqlite_semantic_attempt_event_rolls_back_projection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed, _succeeded = _attempts()
    connection = _connection()
    trusted, evidence = _seed_evidence(connection)
    repository = SQLiteSemanticGateArtifactV3Repository(connection)
    repository.enable_event_first()

    def fail_projection(*_args: object) -> bool:
        raise sqlite3.OperationalError("synthetic semantic artifact failure")

    monkeypatch.setattr(
        SQLiteSemanticGateArtifactV3Repository,
        "_put_artifact",
        fail_projection,
    )
    try:
        with repository.bind_event_context(trusted):
            with pytest.raises(SQLiteSemanticGateArtifactV3PersistenceError):
                repository.store_attempt_with_artifacts(
                    failed.attempt,
                    failed.prompt,
                    failed.response,
                )
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_semantic_gate_attempts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_semantic_gate_artifacts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head"
        ).fetchone() == (2,)
    finally:
        repository.close()
        evidence.close()
        connection.close()


def test_sqlite_semantic_attempt_event_concurrent_exact_replay(
    tmp_path: Path,
) -> None:
    failed, _succeeded = _attempts()
    database = tmp_path / "semantic-attempt-event-first.sqlite3"
    seed = sqlite3.connect(database, isolation_level=None)
    seed.execute("PRAGMA foreign_keys = ON")
    seed.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(seed)
    trusted, evidence = _seed_evidence(seed)
    evidence.close()
    seed.close()

    def store() -> bool:
        connection = sqlite3.connect(
            database,
            isolation_level=None,
            timeout=5,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        repository = SQLiteSemanticGateArtifactV3Repository(
            connection,
            owns_connection=True,
        )
        repository.enable_event_first()
        with repository, repository.bind_event_context(trusted):
            return repository.store_attempt_with_artifacts(
                failed.attempt,
                failed.prompt,
                failed.response,
            ).attempt.inserted

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: store(), range(2)))
    assert sorted(results) == [False, True]

    check = sqlite3.connect(database, isolation_level=None)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (3,)
        assert check.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head"
        ).fetchone() == (3,)
        assert check.execute(
            "SELECT COUNT(*) FROM v3_semantic_gate_attempts"
        ).fetchone() == (1,)
    finally:
        check.close()
