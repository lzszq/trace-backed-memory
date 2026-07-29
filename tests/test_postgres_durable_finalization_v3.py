from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_artifact_service_v3 import (
    _context as _service_context,
    _registry,
)
from tests.test_durable_retrieval_preparation_v3 import _durable_request
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
    _provider_result,
    _request as _semantic_request,
    _semantic_service,
)
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _PolicyProvider,
    _Source,
    _candidate,
    _indexes,
    _policy,
    _record,
    _result,
    _retrieval_authorization,
    _service,
)
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for script in (
        "postgres-v3-gate-session.sql",
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
        "postgres-v3-replay.sql",
    ):
        installed = cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr


def _decided_stack(
    connection,
    *,
    session_id: str,
    permissions: tuple[str, ...] = ("memory:retrieve",),
    sessions: tbm.PostgresGateSessionRepository | None = None,
):
    registry = _registry(permissions=permissions)
    context = _service_context(registry)
    authorization, decisions = _retrieval_authorization(registry)
    if sessions is None:
        sessions = tbm.PostgresGateSessionRepository(
            connection,
            allow_direct_completion=False,
        )
    evidence = PostgresGateEvidenceV3Repository(connection)
    semantic = tbm.PostgresSemanticGateArtifactV3Repository(connection)
    replay = tbm.PostgresReplayV3Repository(connection)
    policy = _policy()
    candidate = _candidate("memory_postgres_finalization")
    source = _Source((candidate,))
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
        session_id_factory=lambda: session_id,
        evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
    )
    prepared = tbm.DurableRetrievalPreparationService(
        gate_session_service=gate,
        retrieval_service=retrieval,
        evidence_authority=evidence,
    ).prepare(
        context,
        replace(
            _durable_request(
                idempotency_key=f"{session_id}_retrieval",
            ),
            expires_in_seconds=3_600,
        ),
    )
    semantic_service = _semantic_service(
        semantic,
        evidence,
        iter(("2026-07-30T02:03:00Z", "2026-07-30T02:03:00.125Z")),
    )
    decided = tbm.AuthenticatedSemanticGateSessionService(
        semantic_gate_service=semantic_service,
        session_writer=sessions,
    ).decide(
        _provider_context(),
        _semantic_request(prepared.session),
        lambda _call: _provider_result(
            prepared.value.system_gate_evaluation
        ),
    ).session
    finalizer = tbm.DurableFinalizationService(
        authorization_service=authorization,
        session_writer=sessions,
        evidence_reader=evidence,
        semantic_authority=semantic,
        revision_source=source,
        policy_loader=lambda: policy,
        replay_authority=replay,
        clock=lambda: "2026-07-30T02:05:00Z",
    )
    return (
        decisions,
        context,
        prepared.scope,
        sessions,
        replay,
        decided,
        finalizer,
    )


def test_postgres_durable_finalization_parity(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            scope,
            sessions,
            replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_finalization_001",
        )
        request = tbm.DurableFinalizationRequest(
            decided.session_id,
            decided.version,
        )
        try:
            result = finalizer.finalize(context, scope, request)
            assert result.session.status == "finalized"
            assert sessions.get(decided.session_id) == result.session
            assert replay.load_injection(
                result.injection.artifact.artifact_id
            ) == (result.injection, result.snippet.encode())

            replayed = finalizer.finalize(context, scope, request)
            assert replayed.replayed is True
            assert replayed.session == result.session
            assert replayed.usage_decision == result.usage_decision
            assert replayed.manifest == result.manifest
        finally:
            decisions.close()


def test_postgres_durable_finalization_respects_outer_rollback(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            scope,
            sessions,
            replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_finalization_rollback",
        )
        request = tbm.DurableFinalizationRequest(
            decided.session_id,
            decided.version,
        )
        result = None
        try:
            with pytest.raises(RuntimeError, match="rollback caller"):
                with connection.transaction():
                    result = finalizer.finalize(context, scope, request)
                    raise RuntimeError("rollback caller")

            assert result is not None
            assert sessions.get(decided.session_id) == decided
            with pytest.raises(KeyError):
                replay.load_injection(
                    result.injection.artifact.artifact_id
                )
            with pytest.raises(KeyError):
                replay.load_artifact(
                    tbm.usage_decision_artifact_id(
                        result.usage_decision.usage_decision_id
                    )
                )
        finally:
            decisions.close()
