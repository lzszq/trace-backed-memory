from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_artifact_service_v3 import _registry
from tests.test_postgres_durable_finalization_v3 import (
    _decided_stack,
    _install,
)


ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
EVALUATOR = tbm.TrustedOutcomeEvaluator(
    evaluator_id="postgres_outcome_evaluator",
    evaluator_version="v1",
    authenticator_id="mtls",
    credential_id="postgres_evaluator_credential",
)
EVALUATOR_CONTEXT = tbm.AuthenticatedOutcomeEvaluatorContext(
    evaluator_id=EVALUATOR.evaluator_id,
    authenticator_id=EVALUATOR.authenticator_id,
    credential_id="postgres_evaluator_credential",
)


def _authenticate_evaluator(
    context: tbm.AuthenticatedOutcomeEvaluatorContext,
) -> tbm.TrustedOutcomeEvaluator:
    if context != EVALUATOR_CONTEXT:
        raise ValueError("untrusted evaluator transport")
    return EVALUATOR


def _install_execution(cluster: PostgresCluster) -> None:
    _install(cluster)
    for script in (
        "postgres-v3-outcome.sql",
        "postgres-v3-completion-outbox.sql",
    ):
        installed = cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _completion(session: tbm.GateSession) -> tbm.GateCompletionRequest:
    return tbm.GateCompletionRequest(
        session_id=session.session_id,
        expected_version=session.version,
        result="pass",
        evaluator_id=EVALUATOR.evaluator_id,
        evaluator_version=EVALUATOR.evaluator_version,
        evidence_artifact_sha256s=(DIGEST_B,),
        output_sha256=DIGEST_A,
        latency_ms=250,
        cost_usd=0.25,
    )


def test_postgres_durable_execution_parity(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install_execution(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            retrieval_scope,
            sessions,
            _replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_execution_001",
            permissions=(
                "memory:retrieve",
                "gate_session:transition",
            ),
        )
        registry = _registry(
            permissions=(
                "memory:retrieve",
                "gate_session:transition",
            )
        )
        request_number = iter(range(1, 100))
        authorization = tbm.AuthenticatedRetrievalService(
            registry_provider=lambda: registry,
            decision_writer=decisions,
            clock=_now,
            request_id_factory=lambda: (
                f"postgres_execution_{next(request_number):03d}"
            ),
        )
        transition_scope = authorization.authorize_permission(
            context,
            permission="gate_session:transition",
            operation=lambda scope: scope,
        ).scope
        outbox = tbm.PostgresCompletionOutboxV3Repository(connection)
        service = tbm.DurableExecutionService(
            authorization_service=authorization,
            session_writer=sessions,
            finalization_reader=finalizer,
            completion_authority=outbox,
            evaluator_authenticator=_authenticate_evaluator,
            clock=_now,
        )
        try:
            finalized = finalizer.finalize(
                context,
                retrieval_scope,
                tbm.DurableFinalizationRequest(
                    decided.session_id,
                    decided.version,
                ),
            )
            started = service.start(
                context,
                retrieval_scope,
                transition_scope,
                tbm.DurableExecutionStartRequest(
                    finalized.session.session_id,
                    finalized.session.version,
                ),
            )
            assert started.session.status == "executing"
            assert started.snippet == finalized.snippet
            assert started.execution_required is True

            completed = service.complete(
                context,
                transition_scope,
                EVALUATOR_CONTEXT,
                _completion(started.session),
            )
            assert completed.session.status == "completed"
            assert completed.outcome.usage_decision_id == (
                finalized.usage_decision.usage_decision_id
            )
            assert outbox.get_event(completed.event.event_id) == completed.event
            assert (
                outbox.get_delivery(completed.event.event_id)
                == completed.delivery
            )
        finally:
            decisions.close()


def test_postgres_durable_execution_start_respects_outer_rollback(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install_execution(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            retrieval_scope,
            sessions,
            _replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_execution_rollback",
            permissions=(
                "memory:retrieve",
                "gate_session:transition",
            ),
        )
        registry = _registry(
            permissions=(
                "memory:retrieve",
                "gate_session:transition",
            )
        )
        authorization = tbm.AuthenticatedRetrievalService(
            registry_provider=lambda: registry,
            decision_writer=decisions,
            clock=_now,
            request_id_factory=lambda: "postgres_execution_rollback_auth",
        )
        transition_scope = authorization.authorize_permission(
            context,
            permission="gate_session:transition",
            operation=lambda scope: scope,
        ).scope
        outbox = tbm.PostgresCompletionOutboxV3Repository(connection)
        service = tbm.DurableExecutionService(
            authorization_service=authorization,
            session_writer=sessions,
            finalization_reader=finalizer,
            completion_authority=outbox,
            evaluator_authenticator=_authenticate_evaluator,
            clock=_now,
        )
        try:
            finalized = finalizer.finalize(
                context,
                retrieval_scope,
                tbm.DurableFinalizationRequest(
                    decided.session_id,
                    decided.version,
                ),
            )
            with pytest.raises(RuntimeError, match="rollback caller"):
                with connection.transaction():
                    started = service.start(
                        context,
                        retrieval_scope,
                        transition_scope,
                        tbm.DurableExecutionStartRequest(
                            finalized.session.session_id,
                            finalized.session.version,
                        ),
                    )
                    assert started.session.status == "executing"
                    raise RuntimeError("rollback caller")
            assert sessions.get(finalized.session.session_id) == finalized.session
        finally:
            decisions.close()
