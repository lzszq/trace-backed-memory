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
from trace_backed_memory.reducer import ReducerEvent


ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for name in (
        "postgres-v3-gate-session.sql",
        "postgres-v3-event-ledger.sql",
        "postgres-v3-outcome.sql",
    ):
        installed = cluster.run_script(ROOT / "schemas" / name)
        assert installed.returncode == 0, installed.stderr


def _trusted() -> EventTrustedContext:
    return EventTrustedContext(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
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


def _evaluator() -> tbm.OutcomeEvaluatorEventContext:
    return tbm.OutcomeEvaluatorEventContext(
        evaluator_id="evaluation_service",
        evaluator_version="1.2.0",
        authenticator_id="mtls",
        credential_id="credential_evaluation_service",
    )


def _request() -> tbm.GateCompletionRequest:
    return tbm.GateCompletionRequest(
        session_id="gate_session_001",
        expected_version=6,
        result="pass",
        evaluator_id="evaluation_service",
        evaluator_version="1.2.0",
        output_sha256=DIGEST_A,
        evidence_artifact_sha256s=(DIGEST_B,),
        latency_ms=250,
        cost_usd=0.25,
    )


def _executing(repository: tbm.PostgresOutcomeV3Repository) -> tbm.GateSession:
    sessions = repository.gate_sessions
    created = sessions.create_or_get(
        session_id="gate_session_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=DIGEST_A,
        idempotency_key="request-001",
        expires_in_seconds=3600,
    ).session
    prepared = sessions.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=1200,
        retrieval_snapshot_id="retrieval_001",
        system_gate_evaluation_id="system_gate_001",
    )
    awaiting = sessions.transition(
        prepared.session_id,
        "awaiting_decision",
        expected_version=prepared.version,
    )
    decided = sessions.transition(
        awaiting.session_id,
        "decided",
        expected_version=awaiting.version,
        semantic_gate_attempt_ids=("semantic_attempt_001",),
        decision_id="decision_001",
    )
    finalized = sessions.transition(
        decided.session_id,
        "finalized",
        expected_version=decided.version,
        final_memory_revision_ids=(REVISION_A,),
        injection_artifact_id="injection_001",
        usage_decision_id="usage_001",
    )
    return sessions.transition(
        finalized.session_id,
        "executing",
        expected_version=finalized.version,
    )


def _complete(
    repository: tbm.PostgresOutcomeV3Repository,
) -> tbm.GateCompletionResult:
    with repository.gate_sessions.bind_event_context(
        _trusted()
    ), repository.bind_evaluator_event_context(_evaluator()):
        return repository.complete_session(_request())


def _reduce(reducer, events):
    state = reducer.initial_state()
    for event in events:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def test_postgres_outcome_completion_is_event_first_and_rebuildable(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        repository.gate_sessions.enable_event_first()
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)
        completion = _complete(repository)

        ledger = PostgresEventLedgerV1(connection, _access())
        page = ledger.read_global(limit=100)
        outcome_page = ledger.read_stream(
            tbm.run_outcome_event_stream_id(completion.outcome.run_outcome_id)
        )
        assert tuple(event.event_type for event in page.events[-3:]) == (
            tbm.EVALUATION_AUTHENTICATED_EVENT,
            tbm.RUN_OUTCOME_RECORDED_EVENT,
            tbm.GATE_SESSION_COMPLETED_EVENT,
        )
        assert tuple(event.global_position for event in page.events) == tuple(
            range(1, 10)
        )
        assert outcome_page.events == page.events[6:8]
        state = _reduce(tbm.build_outcome_current_reducer(), outcome_page.events)
        tbm.verify_outcome_projection_parity(
            state,
            (
                tbm.OutcomeProjectionAuthority(
                    completion.outcome,
                    completion.session,
                    _evaluator(),
                ),
            ),
            outcome_page.events,
        )

        assert _complete(repository).inserted is False
        assert len(ledger.read_global(limit=100).events) == 9
        ledger.close()


def test_postgres_outcome_event_first_requires_evaluator_and_rolls_back(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        repository.gate_sessions.enable_event_first()
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)

        with repository.gate_sessions.bind_event_context(_trusted()):
            with pytest.raises(tbm.PostgresOutcomeV3ConflictError) as raised:
                repository.complete_session(_request())
        assert raised.value.code == (
            "TBM_POSTGRES_OUTCOME_EVALUATOR_CONTEXT_REQUIRED"
        )

        def fail_projection(*_args: object) -> None:
            raise RuntimeError("synthetic outcome projection failure")

        monkeypatch.setattr(repository, "_insert_outcome", fail_projection)
        with pytest.raises(RuntimeError, match="synthetic outcome projection"):
            _complete(repository)

        assert repository.get_session("gate_session_001").status == "executing"
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_outcome.run_outcomes"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_event_ledger.events"
        ).fetchone()[0] == 6


def test_postgres_outcome_event_first_respects_outer_rollback(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        repository.gate_sessions.enable_event_first()
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)

        class RollbackOuter(Exception):
            pass

        with pytest.raises(RollbackOuter):
            with connection.transaction():
                completion = _complete(repository)
                assert completion.inserted is True
                raise RollbackOuter

        assert repository.get_session("gate_session_001").status == "executing"
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_outcome.run_outcomes"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_event_ledger.events"
        ).fetchone()[0] == 6
