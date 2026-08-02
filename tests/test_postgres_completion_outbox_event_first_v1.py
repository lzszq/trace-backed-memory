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
        "postgres-v3-completion-outbox.sql",
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


def _executing(repository: tbm.PostgresCompletionOutboxV3Repository) -> None:
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


def _complete(
    repository: tbm.PostgresCompletionOutboxV3Repository,
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


def test_postgres_completion_outbox_appends_effect_requested_after_completion(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        repository.gate_sessions.enable_event_first()
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)
        write = _complete(repository)
        claim = repository.claim_due(
            worker_id="worker_001",
            lease_seconds=300,
            limit=1,
        )[0]
        dead_letter = repository.fail_delivery(
            write.event.event_id,
            expected_version=claim.delivery.version,
            worker_id="worker_001",
            error_code="consumer_rejected",
            retry_delay_seconds=1,
            max_attempts=1,
        )
        delivery_history = repository.list_delivery_history(
            write.event.event_id
        )
        assert delivery_history == (
            write.delivery,
            claim.delivery,
            dead_letter,
        )

        ledger = PostgresEventLedgerV1(connection, _access())
        page = ledger.read_global(limit=100)
        effect_page = ledger.read_stream(
            tbm.effect_event_stream_id(write.event.event_id)
        )
        assert tuple(event.event_type for event in page.events[6:10]) == (
            tbm.EVALUATION_AUTHENTICATED_EVENT,
            tbm.RUN_OUTCOME_RECORDED_EVENT,
            tbm.GATE_SESSION_COMPLETED_EVENT,
            tbm.EFFECT_REQUESTED_EVENT,
        )
        assert tuple(event.global_position for event in page.events) == tuple(
            range(1, 14)
        )
        assert tuple(event.event_type for event in effect_page.events) == (
            tbm.EFFECT_REQUESTED_EVENT,
            tbm.EFFECT_STARTED_EVENT,
            tbm.EFFECT_FAILED_EVENT,
            tbm.EFFECT_DEAD_LETTERED_EVENT,
        )
        assert tuple(event.actor_id for event in effect_page.events[1:]) == (
            "worker_001",
            "worker_001",
            "worker_001",
        )
        state = _reduce(effect_page.events)
        tbm.verify_effect_projection_parity(
            state,
            (tbm.EffectProjectionAuthority(write.event, delivery_history),),
            effect_page.events,
        )
        assert _complete(
            repository,
            trusted=_recovery_trusted(),
        ).event_inserted is False
        assert len(ledger.read_global(limit=100).events) == 13
        ledger.close()


def test_postgres_completion_outbox_projection_failure_rolls_back_effect_event(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        repository.gate_sessions.enable_event_first()
        with repository.gate_sessions.bind_event_context(_trusted()):
            _executing(repository)

        def fail_projection(*_args: object) -> None:
            raise RuntimeError("synthetic outbox projection failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(repository, "_insert_bundle", fail_projection)
            with pytest.raises(RuntimeError, match="synthetic outbox projection"):
                _complete(repository)

        assert repository.gate_sessions.get("gate_session_001").status == (
            "executing"
        )
        assert connection.execute(
            "SELECT count(*) FROM "
            "trace_backed_memory_v3_completion_outbox.events"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_event_ledger.events"
        ).fetchone()[0] == 6

        write = _complete(repository)

        def fail_delivery_projection(*_args: object) -> None:
            raise RuntimeError("synthetic delivery projection failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(
                repository,
                "_append_delivery",
                fail_delivery_projection,
            )
            with pytest.raises(
                RuntimeError,
                match="synthetic delivery projection",
            ):
                repository.claim_due(
                    worker_id="worker_001",
                    lease_seconds=300,
                    limit=1,
                )
        assert repository.get_delivery(write.event.event_id) == write.delivery
        assert connection.execute(
            "SELECT count(*) FROM trace_backed_memory_v3_event_ledger.events"
        ).fetchone()[0] == 10
