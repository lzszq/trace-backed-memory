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
from trace_backed_memory.sqlite_completion_outbox_v3 import (
    SQLiteCompletionOutboxV3PersistenceError,
    SQLiteCompletionOutboxV3Repository,
)
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1


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


def _recovery_trusted() -> EventTrustedContext:
    trusted = _trusted()
    return EventTrustedContext(
        organization_id=trusted.organization_id,
        tenant_id=trusted.tenant_id,
        repository_id=trusted.repository_id,
        environment_id=trusted.environment_id,
        principal_id=trusted.principal_id,
        agent_client_id=trusted.agent_client_id,
        actor_type="worker",
        actor_id="worker_local_gate_recovery",
        authorization_decision_id="local_owner_recovery_authority",
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


def _executing(repository: SQLiteCompletionOutboxV3Repository) -> None:
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
    sessions.transition(
        finalized.session_id,
        "executing",
        expected_version=finalized.version,
    )


def _repository(
    connection: sqlite3.Connection,
) -> SQLiteCompletionOutboxV3Repository:
    repository = SQLiteCompletionOutboxV3Repository(
        connection,
        clock=_Clock(),
    )
    repository.gate_sessions.enable_event_first()
    return repository


def _complete(
    repository: SQLiteCompletionOutboxV3Repository,
    *,
    trusted: EventTrustedContext | None = None,
):
    with repository.gate_sessions.bind_event_context(
        _trusted() if trusted is None else trusted
    ), repository.bind_evaluator_event_context(_evaluator()):
        return repository.complete_session(_request())


def _reduce(events):
    reducer = tbm.build_effect_queue_reducer()
    state = reducer.initial_state()
    for event in events:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def test_sqlite_completion_outbox_appends_effect_requested_after_completion():
    connection = _connection()
    repository = _repository(connection)
    try:
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)
        write = _complete(repository)
        first_claim = repository.claim_due(
            worker_id="worker_001",
            lease_seconds=300,
            limit=1,
        )[0]
        retry = repository.fail_delivery(
            write.event.event_id,
            expected_version=first_claim.delivery.version,
            worker_id="worker_001",
            error_code="consumer_unavailable",
            retry_delay_seconds=1,
            max_attempts=2,
        )
        second_claim = repository.claim_due(
            worker_id="worker_002",
            lease_seconds=300,
            limit=1,
        )[0]
        dead_letter = repository.fail_delivery(
            write.event.event_id,
            expected_version=second_claim.delivery.version,
            worker_id="worker_002",
            error_code="consumer_rejected",
            retry_delay_seconds=1,
            max_attempts=2,
        )
        delivery_history = repository.list_delivery_history(
            write.event.event_id
        )
        assert delivery_history == (
            write.delivery,
            first_claim.delivery,
            retry,
            second_claim.delivery,
            dead_letter,
        )

        ledger = SQLiteEventLedgerV1(connection, _access())
        try:
            page = ledger.read_global(limit=100)
            effect_page = ledger.read_stream(
                tbm.effect_event_stream_id(write.event.event_id)
            )
        finally:
            ledger.close()
        assert tuple(event.event_type for event in page.events[6:10]) == (
            tbm.EVALUATION_AUTHENTICATED_EVENT,
            tbm.RUN_OUTCOME_RECORDED_EVENT,
            tbm.GATE_SESSION_COMPLETED_EVENT,
            tbm.EFFECT_REQUESTED_EVENT,
        )
        assert tuple(event.global_position for event in page.events) == tuple(
            range(1, 17)
        )
        assert tuple(event.event_type for event in effect_page.events) == (
            tbm.EFFECT_REQUESTED_EVENT,
            tbm.EFFECT_STARTED_EVENT,
            tbm.EFFECT_FAILED_EVENT,
            tbm.EFFECT_RETRY_SCHEDULED_EVENT,
            tbm.EFFECT_STARTED_EVENT,
            tbm.EFFECT_FAILED_EVENT,
            tbm.EFFECT_DEAD_LETTERED_EVENT,
        )
        assert effect_page.events == (
            page.events[9],
            *page.events[10:],
        )
        assert tuple(event.actor_id for event in effect_page.events[1:]) == (
            "worker_001",
            "worker_001",
            "worker_001",
            "worker_002",
            "worker_002",
            "worker_002",
        )
        reference = tbm.parse_effect_requested_event(effect_page.events[0])
        assert reference.outbox_event == write.event
        assert reference.initial_delivery == write.delivery

        state = _reduce(effect_page.events)
        tbm.verify_effect_projection_parity(
            state,
            (tbm.EffectProjectionAuthority(write.event, delivery_history),),
            effect_page.events,
        )
        assert tbm.projected_effect_status(
            state,
            write.event.event_id,
        ) == "dead_letter"
        replay = _complete(repository, trusted=_recovery_trusted())
        assert replay.event_inserted is False
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (16,)
    finally:
        repository.close()
        connection.close()


def test_sqlite_completion_outbox_projection_failure_rolls_back_effect_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)

        def fail_projection(*_args: object) -> None:
            raise sqlite3.OperationalError("synthetic outbox projection failure")

        monkeypatch.setattr(
            SQLiteCompletionOutboxV3Repository,
            "_insert_bundle",
            classmethod(fail_projection),
        )
        with pytest.raises(SQLiteCompletionOutboxV3PersistenceError):
            _complete(repository)

        assert repository.gate_sessions.get("gate_session_001").status == (
            "executing"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_completion_outbox_events"
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


def test_sqlite_delivery_projection_failure_rolls_back_transition_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    repository = _repository(connection)
    try:
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)
        write = _complete(repository)

        def fail_delivery_projection(*_args: object) -> None:
            raise sqlite3.OperationalError("synthetic delivery projection failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(
                SQLiteCompletionOutboxV3Repository,
                "_append_delivery",
                classmethod(fail_delivery_projection),
            )
            with pytest.raises(SQLiteCompletionOutboxV3PersistenceError):
                repository.claim_due(
                    worker_id="worker_001",
                    lease_seconds=300,
                    limit=1,
                )
        assert repository.get_delivery(write.event.event_id) == write.delivery
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (10,)

        claim = repository.claim_due(
            worker_id="worker_001",
            lease_seconds=300,
            limit=1,
        )[0]
        with monkeypatch.context() as scoped:
            scoped.setattr(
                SQLiteCompletionOutboxV3Repository,
                "_append_delivery",
                classmethod(fail_delivery_projection),
            )
            with pytest.raises(SQLiteCompletionOutboxV3PersistenceError):
                repository.fail_delivery(
                    write.event.event_id,
                    expected_version=claim.delivery.version,
                    worker_id="worker_001",
                    error_code="consumer_unavailable",
                    retry_delay_seconds=1,
                    max_attempts=2,
                )
        assert repository.get_delivery(write.event.event_id) == claim.delivery
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_event_ledger_events"
        ).fetchone() == (11,)
    finally:
        repository.close()
        connection.close()
