from __future__ import annotations

from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_postgres_outcome_attribution_v3 import _attribution, _complete
from trace_backed_memory.event_v1 import EventTrustedContext
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1


ROOT = Path(__file__).resolve().parents[1]


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for name in (
        "postgres-v3-gate-session.sql",
        "postgres-v3-event-ledger.sql",
        "postgres-v3-outcome.sql",
        "postgres-v3-outcome-attribution.sql",
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
        evaluator_id="run_evaluator",
        evaluator_version="1.0",
        authenticator_id="mtls",
        credential_id="credential_run_evaluator",
    )


def _completed(repository: tbm.PostgresOutcomeAttributionV3Repository):
    with repository.outcomes.gate_sessions.bind_event_context(
        _trusted()
    ), repository.outcomes.bind_evaluator_event_context(_evaluator()):
        return _complete(repository)


def test_postgres_attribution_event_sequences_and_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        repository.outcomes.gate_sessions.enable_event_first()
        completed = _completed(repository)
        association = _attribution(completed.outcome)
        causal = _attribution(
            completed.outcome,
            claim_strength="causal",
            effect="helped",
            method="controlled_experiment",
            evaluator_id="experiment_evaluator",
            verifier_id="independent_verifier",
            confidence=0.9,
            reason="Controlled comparison with independent verification.",
        )
        with repository.outcomes.gate_sessions.bind_event_context(_trusted()):
            assert repository.put_attribution(association).inserted is True
            assert repository.put_attribution(causal).inserted is True
            assert repository.put_attribution(association).inserted is False
            assert repository.put_attribution(causal).inserted is False

        ledger = PostgresEventLedgerV1(connection, _access())
        page = ledger.read_global(limit=100)
        assert tuple(event.global_position for event in page.events) == tuple(
            range(1, 13)
        )
        assert tuple(event.event_type for event in page.events[9:]) == (
            tbm.OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
            tbm.OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
            tbm.OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
        )
        ledger.close()


def test_postgres_attribution_projection_failure_rolls_back_events(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        repository.outcomes.gate_sessions.enable_event_first()
        completed = _completed(repository)
        association = _attribution(completed.outcome)

        def fail_projection(*_args: object) -> bool:
            raise RuntimeError("synthetic attribution projection failure")

        monkeypatch.setattr(repository, "_insert", fail_projection)
        with repository.outcomes.gate_sessions.bind_event_context(_trusted()):
            with pytest.raises(RuntimeError, match="synthetic attribution"):
                repository.put_attribution(association)

        assert connection.execute(
            "SELECT count(*) FROM "
            "trace_backed_memory_v3_outcome_attribution.outcome_attributions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_event_ledger.events"
        ).fetchone()[0] == 9


def test_postgres_attribution_events_respect_outer_rollback(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        repository.outcomes.gate_sessions.enable_event_first()
        completed = _completed(repository)
        causal = _attribution(
            completed.outcome,
            claim_strength="causal",
            effect="helped",
            method="controlled_experiment",
            evaluator_id="experiment_evaluator",
            verifier_id="independent_verifier",
            confidence=0.9,
            reason="Controlled comparison with independent verification.",
        )

        class RollbackOuter(Exception):
            pass

        with pytest.raises(RollbackOuter):
            with connection.transaction():
                with repository.outcomes.gate_sessions.bind_event_context(
                    _trusted()
                ):
                    assert repository.put_attribution(causal).inserted is True
                raise RollbackOuter

        assert connection.execute(
            "SELECT count(*) FROM "
            "trace_backed_memory_v3_outcome_attribution.outcome_attributions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_event_ledger.events"
        ).fetchone()[0] == 9
