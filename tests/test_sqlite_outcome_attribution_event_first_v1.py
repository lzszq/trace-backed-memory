from __future__ import annotations

import sqlite3

import pytest

import trace_backed_memory as tbm
from tests.test_sqlite_outcome_attribution_v3 import _attribution, _complete
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import EventTrustedContext
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.reducer import ReducerEvent
from trace_backed_memory.sqlite_bundle_v3 import install_sqlite_v3_bundle
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.sqlite_outcome_attribution_v3 import (
    SQLiteOutcomeAttributionV3PersistenceError,
    SQLiteOutcomeAttributionV3Repository,
)


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


def _repository(
    connection: sqlite3.Connection,
) -> SQLiteOutcomeAttributionV3Repository:
    repository = SQLiteOutcomeAttributionV3Repository(connection)
    repository.outcomes.gate_sessions.enable_event_first()
    return repository


def _completed(repository: SQLiteOutcomeAttributionV3Repository):
    with repository.outcomes.gate_sessions.bind_event_context(
        _trusted()
    ), repository.outcomes.bind_evaluator_event_context(_evaluator()):
        return _complete(repository)


def _reduce(reducer, events):
    state = reducer.initial_state()
    for event in events:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def test_sqlite_attribution_events_rebuild_association_and_causality() -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
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

        ledger = SQLiteEventLedgerV1(connection, _access())
        try:
            page = ledger.read_global(limit=100)
        finally:
            ledger.close()
        assert tuple(event.global_position for event in page.events) == tuple(
            range(1, 13)
        )
        assert tuple(event.event_type for event in page.events[9:]) == (
            tbm.OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
            tbm.OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
            tbm.OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
        )
        reducer_events = tuple(
            event
            for event in page.events
            if event.event_type
            in {
                tbm.RUN_OUTCOME_RECORDED_EVENT,
                tbm.OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
                tbm.OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
            }
        )
        state = _reduce(
            tbm.build_outcome_attribution_reducer(),
            reducer_events,
        )
        tbm.verify_outcome_attribution_projection_parity(
            state,
            (completed.outcome,),
            (association, causal),
            reducer_events,
        )
    finally:
        repository.close()
        connection.close()


def test_sqlite_attribution_projection_failure_rolls_back_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
        completed = _completed(repository)
        association = _attribution(completed.outcome)

        def fail_projection(*_args: object) -> None:
            raise sqlite3.OperationalError("synthetic attribution failure")

        monkeypatch.setattr(
            repository,
            "_attribution_row",
            fail_projection,
        )
        with repository.outcomes.gate_sessions.bind_event_context(_trusted()):
            with pytest.raises(SQLiteOutcomeAttributionV3PersistenceError):
                repository.put_attribution(association)

        assert connection.execute(
            "SELECT COUNT(*) FROM v3_outcome_attributions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (9,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_attribution_events_respect_outer_rollback() -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
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
        connection.execute("BEGIN IMMEDIATE")
        with repository.outcomes.gate_sessions.bind_event_context(_trusted()):
            assert repository.put_attribution(causal).inserted is True
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (11,)
        connection.rollback()

        assert connection.execute(
            "SELECT COUNT(*) FROM v3_outcome_attributions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (9,)
    finally:
        repository.close()
        connection.close()
