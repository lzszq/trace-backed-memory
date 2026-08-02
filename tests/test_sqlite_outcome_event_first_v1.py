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
from trace_backed_memory.reducer import ReducerEvent
from trace_backed_memory.sqlite_bundle_v3 import install_sqlite_v3_bundle
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.sqlite_outcome_v3 import (
    SQLiteOutcomeV3ConflictError,
    SQLiteOutcomeV3NotFoundError,
    SQLiteOutcomeV3PersistenceError,
    SQLiteOutcomeV3Repository,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 2, tzinfo=timezone.utc)

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


def _executing(repository: SQLiteOutcomeV3Repository) -> tbm.GateSession:
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


def _repository(
    connection: sqlite3.Connection,
) -> SQLiteOutcomeV3Repository:
    repository = SQLiteOutcomeV3Repository(connection, clock=_Clock())
    repository.gate_sessions.enable_event_first()
    return repository


def _complete(
    repository: SQLiteOutcomeV3Repository,
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


def test_sqlite_outcome_completion_is_event_first_and_rebuildable() -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)
        completion = _complete(repository)

        ledger = SQLiteEventLedgerV1(connection, _access())
        try:
            page = ledger.read_global(limit=100)
            outcome_page = ledger.read_stream(
                tbm.run_outcome_event_stream_id(
                    completion.outcome.run_outcome_id
                )
            )
        finally:
            ledger.close()
        assert tuple(event.event_type for event in page.events[-3:]) == (
            tbm.EVALUATION_AUTHENTICATED_EVENT,
            tbm.RUN_OUTCOME_RECORDED_EVENT,
            tbm.GATE_SESSION_COMPLETED_EVENT,
        )
        assert tuple(event.global_position for event in page.events) == tuple(
            range(1, 10)
        )
        assert outcome_page.events == page.events[6:8]

        outcome_state = _reduce(
            tbm.build_outcome_current_reducer(),
            outcome_page.events,
        )
        tbm.verify_outcome_projection_parity(
            outcome_state,
            (
                tbm.OutcomeProjectionAuthority(
                    completion.outcome,
                    completion.session,
                    _evaluator(),
                ),
            ),
            outcome_page.events,
        )
        gate_events = tuple(
            event
            for event in page.events
            if event.event_type in tbm.GATE_SESSION_EVENT_TYPES
        )
        gate_state = _reduce(tbm.build_gate_session_reducer(), gate_events)
        tbm.verify_gate_session_projection_parity(
            gate_state,
            (completion.session,),
        )

        replay = _complete(repository)
        assert replay.inserted is False
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (9,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_outcome_event_first_requires_trusted_evaluator() -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)
        with repository.gate_sessions.bind_event_context(_trusted()):
            with pytest.raises(SQLiteOutcomeV3ConflictError) as raised:
                repository.complete_session(_request())
        assert raised.value.code == (
            "TBM_SQLITE_OUTCOME_EVALUATOR_CONTEXT_REQUIRED"
        )
        assert repository.get_session("gate_session_001").status == "executing"
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_run_outcomes"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (6,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_outcome_projection_failure_rolls_back_event_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)

        def fail_projection(*_args: object) -> None:
            raise sqlite3.OperationalError("synthetic outcome projection failure")

        monkeypatch.setattr(
            SQLiteOutcomeV3Repository,
            "_insert_outcome",
            staticmethod(fail_projection),
        )
        with pytest.raises(SQLiteOutcomeV3PersistenceError):
            _complete(repository)

        assert repository.get_session("gate_session_001").status == "executing"
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_run_outcomes"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (6,)
        assert connection.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head"
        ).fetchone() == (6,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_outcome_event_first_respects_caller_rollback() -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)
        connection.execute("BEGIN IMMEDIATE")
        completion = _complete(repository)
        assert completion.inserted is True
        assert connection.in_transaction is True
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (9,)
        connection.rollback()

        assert repository.get_session("gate_session_001").status == "executing"
        with pytest.raises(SQLiteOutcomeV3NotFoundError):
            repository.get_outcome(completion.outcome.run_outcome_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (6,)
    finally:
        repository.close()
        connection.close()
