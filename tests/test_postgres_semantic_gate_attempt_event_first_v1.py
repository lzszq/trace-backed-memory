from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_semantic_gate_attempt_event_v1 import _attempts, _trusted
from tests.test_sqlite_semantic_gate_artifact_v3 import _evidence
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3Repository,
)
from trace_backed_memory.postgres_semantic_gate_artifact_v3 import (
    PostgresSemanticGateArtifactV3PersistenceError,
    PostgresSemanticGateArtifactV3Repository,
)
from trace_backed_memory.reducer import ReducerEvent, execute_reducer_step
from trace_backed_memory.semantic_gate_attempt_reducer_v1 import (
    verify_semantic_gate_attempt_projection_parity,
)


ROOT = Path(__file__).resolve().parents[1]


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for name in (
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-event-ledger.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
    ):
        installed = cluster.run_script(ROOT / "schemas" / name)
        assert installed.returncode == 0, installed.stderr


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


def _seed_evidence(
    connection: object,
) -> tuple[tbm.EventTrustedContext, PostgresGateEvidenceV3Repository]:
    snapshot, evaluation = _evidence()
    trusted = _trusted(snapshot.authorization_event_id)
    repository = PostgresGateEvidenceV3Repository(connection)
    repository.enable_event_first()
    with repository.bind_event_context(trusted):
        repository.store_bundle(snapshot, evaluation)
    return trusted, repository


def test_postgres_semantic_attempt_event_first_rebuilds_exact_projection(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    failed, succeeded = _attempts()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        trusted, evidence = _seed_evidence(connection)
        repository = PostgresSemanticGateArtifactV3Repository(connection)
        repository.enable_event_first()
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
            replay = repository.store_attempt_with_artifacts(
                failed.attempt,
                failed.prompt,
                failed.response,
            )

        ledger = PostgresEventLedgerV1(connection, _access(trusted))
        page = ledger.read_global(0, 10)
        reducer = tbm.build_semantic_gate_attempt_reducer()
        state = reducer.initial_state()
        for event in page.events:
            if event.event_type not in reducer.descriptor.input_event_types:
                continue
            state = execute_reducer_step(
                reducer,
                state,
                ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
            ).state

        assert first.attempt.inserted is True
        assert second.attempt.inserted is True
        assert replay.attempt.inserted is False
        assert tuple(event.event_type for event in page.events[2:]) == (
            tbm.SEMANTIC_GATE_ATTEMPT_FAILED_EVENT,
            tbm.SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
        )
        verify_semantic_gate_attempt_projection_parity(
            state,
            (failed, succeeded),
            (page.events[1], *page.events[2:]),
        )
        assert repository.load_attempt_with_artifacts(
            failed.attempt.attempt_id
        ) == failed
        assert repository.load_attempt_with_artifacts(
            succeeded.attempt.attempt_id
        ) == succeeded
        ledger.close()
        repository.close()
        evidence.close()


def test_postgres_semantic_attempt_event_rolls_back_projection_failure(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    failed, _succeeded = _attempts()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        trusted, evidence = _seed_evidence(connection)
        repository = PostgresSemanticGateArtifactV3Repository(connection)
        repository.enable_event_first()

        def fail_projection(*_args: object) -> bool:
            raise RuntimeError("synthetic semantic artifact failure")

        monkeypatch.setattr(
            PostgresSemanticGateArtifactV3Repository,
            "_put_artifact",
            fail_projection,
        )
        with repository.bind_event_context(trusted):
            with pytest.raises(
                PostgresSemanticGateArtifactV3PersistenceError
            ):
                repository.store_attempt_with_artifacts(
                    failed.attempt,
                    failed.prompt,
                    failed.response,
                )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_semantic_gate_artifacts."
                "semantic_gate_artifacts"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_event_ledger.events"
            )
            assert cursor.fetchone()[0] == 2
            cursor.execute(
                "SELECT current_global_position FROM "
                "trace_backed_memory_v3_event_ledger.global_head"
            )
            assert cursor.fetchone()[0] == 2
        repository.close()
        evidence.close()


def test_postgres_semantic_attempt_event_requires_retained_system_event(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    failed, _succeeded = _attempts()
    snapshot, evaluation = _evidence()
    trusted = _trusted(snapshot.authorization_event_id)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        evidence = PostgresGateEvidenceV3Repository(connection)
        evidence.store_bundle(snapshot, evaluation)
        repository = PostgresSemanticGateArtifactV3Repository(connection)
        repository.enable_event_first()
        with repository.bind_event_context(trusted):
            with pytest.raises(tbm.PostgresSemanticGateArtifactV3ConflictError):
                repository.store_attempt_with_artifacts(
                    failed.attempt,
                    failed.prompt,
                    failed.response,
                )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_event_ledger.events"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts"
            )
            assert cursor.fetchone()[0] == 0
        repository.close()
        evidence.close()


def test_postgres_semantic_attempt_event_rejects_mismatched_trusted_scope(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    failed, _succeeded = _attempts()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        trusted, evidence = _seed_evidence(connection)
        repository = PostgresSemanticGateArtifactV3Repository(connection)
        repository.enable_event_first()
        with repository.bind_event_context(
            replace(trusted, tenant_id="tenant_other")
        ):
            with pytest.raises(tbm.PostgresSemanticGateArtifactV3ConflictError):
                repository.store_attempt_with_artifacts(
                    failed.attempt,
                    failed.prompt,
                    failed.response,
                )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_event_ledger.events"
            )
            assert cursor.fetchone()[0] == 2
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts"
            )
            assert cursor.fetchone()[0] == 0
        repository.close()
        evidence.close()


def test_postgres_semantic_attempt_event_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    failed, _succeeded = _attempts()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        trusted, evidence = _seed_evidence(connection)
        repository = PostgresSemanticGateArtifactV3Repository(connection)
        repository.enable_event_first()
        with pytest.raises(RuntimeError, match="caller rollback"):
            with connection.transaction():
                with repository.bind_event_context(trusted):
                    repository.store_attempt_with_artifacts(
                        failed.attempt,
                        failed.prompt,
                        failed.response,
                    )
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM "
                        "trace_backed_memory_v3_event_ledger.events"
                    )
                    assert cursor.fetchone()[0] == 3
                raise RuntimeError("caller rollback")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_event_ledger.events"
            )
            assert cursor.fetchone()[0] == 2
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts"
            )
            assert cursor.fetchone()[0] == 0
        repository.close()
        evidence.close()


def test_postgres_semantic_attempt_event_concurrent_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    failed, _succeeded = _attempts()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        trusted, evidence = _seed_evidence(connection)
        evidence.close()

    def store() -> bool:
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            repository = PostgresSemanticGateArtifactV3Repository(connection)
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

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_event_ledger.events"
            )
            assert cursor.fetchone()[0] == 3
            cursor.execute(
                "SELECT current_global_position FROM "
                "trace_backed_memory_v3_event_ledger.global_head"
            )
            assert cursor.fetchone()[0] == 3
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts"
            )
            assert cursor.fetchone()[0] == 1
