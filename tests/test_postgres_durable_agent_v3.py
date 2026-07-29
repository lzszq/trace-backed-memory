from __future__ import annotations

from dataclasses import replace

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_durable_semantic_gate_v3 import _semantic_service
from tests.test_durable_retrieval_preparation_v3 import _durable_request
from tests.test_postgres_durable_execution_v3 import (
    EVALUATOR_CONTEXT,
    _authenticate_evaluator,
    _completion,
    _install_execution,
    _now,
)
from tests.test_postgres_durable_finalization_v3 import _decided_stack
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _PolicyProvider,
    _indexes,
    _record,
    _result,
    _service,
)


def test_postgres_authenticated_durable_agent_lifecycle_parity(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install_execution(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        outbox = tbm.PostgresCompletionOutboxV3Repository(connection)
        (
            decisions,
            context,
            _retrieval_scope,
            sessions,
            _replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_durable_agent_001",
            permissions=(
                "memory:retrieve",
                "gate_session:transition",
                "artifact:read",
            ),
            sessions=outbox.gate_sessions,
        )
        authorization = finalizer._authorization_service  # noqa: SLF001
        evidence = finalizer._evidence_reader  # noqa: SLF001
        semantic_authority = finalizer._semantic_authority  # noqa: SLF001
        source = finalizer._revision_source  # noqa: SLF001
        policy = finalizer._policy_loader()  # noqa: SLF001
        candidate = next(iter(source.candidates.values()))
        retrieval = _service(
            authorization,
            _PolicyProvider(policy),
            _Discovery(
                _result(
                    records=(_record(candidate),),
                    index_versions=_indexes(
                        "metadata",
                        "lexical",
                        "semantic",
                        "git_graph",
                    ),
                )
            ),
            source,
        )
        gate = tbm.AuthenticatedGateSessionService(
            authorization_service=authorization,
            session_writer=sessions,
            session_id_factory=lambda: "unused_postgres_agent_session",
            evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(
                evidence
            ),
        )
        preparation = tbm.DurableRetrievalPreparationService(
            gate_session_service=gate,
            retrieval_service=retrieval,
            evidence_authority=evidence,
        )
        semantic = tbm.AuthenticatedSemanticGateSessionService(
            semantic_gate_service=_semantic_service(
                semantic_authority,
                evidence,
                iter(
                    (
                        "2026-07-30T02:03:00Z",
                        "2026-07-30T02:03:00.125Z",
                    )
                ),
            ),
            session_writer=sessions,
        )
        execution = tbm.DurableExecutionService(
            authorization_service=authorization,
            session_writer=sessions,
            finalization_reader=finalizer,
            completion_authority=outbox,
            evaluator_authenticator=_authenticate_evaluator,
            clock=_now,
        )
        agent = tbm.AuthenticatedDurableAgentMemory(
            authorization_service=authorization,
            preparation_service=preparation,
            semantic_service=semantic,
            finalization_service=finalizer,
            execution_service=execution,
        )
        try:
            finalized = agent.finalize(
                context,
                tbm.DurableFinalizationRequest(
                    decided.session_id,
                    decided.version,
                ),
            )
            started = agent.start(
                context,
                tbm.DurableExecutionStartRequest(
                    finalized.session.session_id,
                    finalized.session.version,
                ),
            )
            resumed = agent.resume(
                context,
                tbm.DurableExecutionResumeRequest(
                    started.session.session_id,
                    started.session.version,
                    lease_seconds=2_700,
                ),
            )
            completed = agent.complete(
                context,
                EVALUATOR_CONTEXT,
                _completion(resumed.session),
            )
            exported = agent.export_replay_bundle(
                context,
                tbm.DurableReplayExportRequest(
                    completed.session.session_id,
                    completed.session.version,
                    ("internal",),
                ),
            )
            prepared_for_cancel = agent.prepare(
                context,
                replace(
                    _durable_request(
                        idempotency_key="postgres_agent_cancel_retrieval",
                    ),
                    expires_in_seconds=3_600,
                ),
            )
            cancel_request = tbm.DurableAgentCancelRequest(
                prepared_for_cancel.session.session_id,
                prepared_for_cancel.session.version,
                "caller canceled PostgreSQL session",
            )
            canceled = agent.cancel(context, cancel_request)
            replayed_cancel = agent.cancel(context, cancel_request)

            assert finalized.session.status == "finalized"
            assert started.session.status == "executing"
            assert resumed.session.status == "executing"
            assert resumed.replayed is True
            assert completed.session.status == "completed"
            assert exported.session == completed.session
            assert exported.bundle.manifest == finalized.manifest
            assert tbm.verify_replay_bundle_export(exported.bundle)
            assert canceled.session.status == "canceled"
            assert replayed_cancel.session == canceled.session
            assert replayed_cancel.replayed is True
            assert agent.get_session(
                context,
                completed.session.session_id,
            ) == completed.session
        finally:
            decisions.close()
