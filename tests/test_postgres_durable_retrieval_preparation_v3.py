from __future__ import annotations

from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_artifact_service_v3 import _context, _registry
from tests.test_durable_retrieval_preparation_v3 import (
    _durable_request,
    _durable_service,
)
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _Source,
    _candidate,
    _indexes,
    _policy,
    _record,
    _result,
    _retrieval_authorization,
)


ROOT = Path(__file__).resolve().parents[1]
GATE_SESSION_INSTALL = ROOT / "schemas" / "postgres-v3-gate-session.sql"
GATE_EVIDENCE_INSTALL = ROOT / "schemas" / "postgres-v3-gate-evidence.sql"


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for script in (GATE_SESSION_INSTALL, GATE_EVIDENCE_INSTALL):
        installed = cluster.run_script(script)
        assert installed.returncode == 0, installed.stderr


def _candidate_discovery(memory_id: str):
    candidate = _candidate(memory_id)
    return (
        candidate,
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
    )


def test_postgres_durable_retrieval_prepares_and_replays_exactly(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    candidate, discovery = _candidate_discovery("memory_postgres_durable")
    try:
        with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
            sessions = tbm.PostgresGateSessionRepository(
                connection,
                allow_direct_completion=False,
            )
            evidence = tbm.PostgresGateEvidenceV3Repository(connection)
            service = _durable_service(
                authorization,
                sessions,
                evidence,
                discovery,
                _Source((candidate,)),
                session_ids=iter(
                    (
                        "gate_session_postgres_durable_001",
                        "gate_session_postgres_durable_002",
                    )
                ),
            )

            first = service.prepare(_context(registry), _durable_request())
            replay = service.prepare(_context(registry), _durable_request())

            assert first.session.status == "prepared"
            assert replay == first
            assert discovery.calls == 1
            assert evidence.load_snapshot(first.value.snapshot.snapshot_id) == (
                first.value.snapshot
            )
            assert (
                evidence.load_evaluation(
                    first.value.system_gate_evaluation.evaluation_id
                )
                == first.value.system_gate_evaluation
            )
            assert [
                item.status for item in sessions.history(first.session.session_id)
            ] == ["created", "prepared"]
    finally:
        decisions.close()


class _PreparedTransitionFailure:
    def __init__(self, delegate):
        self.delegate = delegate

    def create_or_get(self, **kwargs):
        return self.delegate.create_or_get(**kwargs)

    def get(self, session_id):
        return self.delegate.get(session_id)

    def transition(self, session_id, target_status, **kwargs):
        if target_status == "prepared":
            raise RuntimeError("private prepared transition failure")
        return self.delegate.transition(session_id, target_status, **kwargs)


def test_postgres_durable_retrieval_compensates_after_evidence_is_durable(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    candidate, discovery = _candidate_discovery("memory_postgres_compensation")
    try:
        with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
            sessions = tbm.PostgresGateSessionRepository(
                connection,
                allow_direct_completion=False,
            )
            evidence = tbm.PostgresGateEvidenceV3Repository(connection)
            gate = tbm.AuthenticatedGateSessionService(
                authorization_service=authorization,
                session_writer=_PreparedTransitionFailure(sessions),
                session_id_factory=lambda: "gate_session_postgres_compensation",
                evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
            )
            retrieval = tbm.AuthenticatedRetrievalPreparationService(
                authorization_service=authorization,
                policy_provider=_policy,
                discovery=discovery,
                revision_source=_Source((candidate,)),
                clock=lambda: "2026-07-29T00:00:00Z",
                evaluator_id="system_gate",
                evaluator_version="v1",
            )
            service = tbm.DurableRetrievalPreparationService(
                gate_session_service=gate,
                retrieval_service=retrieval,
                evidence_authority=evidence,
            )

            with pytest.raises(tbm.GatePreparationFailedError) as failed:
                service.prepare(_context(registry), _durable_request())

            assert failed.value.session.status == "canceled"
            assert "private" not in str(failed.value)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM "
                    "trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots"
                )
                assert cursor.fetchone() == (1,)
                cursor.execute(
                    "SELECT count(*) FROM "
                    "trace_backed_memory_v3_gate_evidence."
                    "v3_system_gate_evaluations"
                )
                assert cursor.fetchone() == (1,)
            assert [
                item.status
                for item in sessions.history(failed.value.session.session_id)
            ] == ["created", "canceled"]
    finally:
        decisions.close()


def test_postgres_durable_retrieval_respects_caller_transaction_rollback(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    candidate, discovery = _candidate_discovery("memory_postgres_outer_rollback")
    try:
        with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
            sessions = tbm.PostgresGateSessionRepository(
                connection,
                allow_direct_completion=False,
            )
            evidence = tbm.PostgresGateEvidenceV3Repository(connection)
            service = _durable_service(
                authorization,
                sessions,
                evidence,
                discovery,
                _Source((candidate,)),
                session_ids=iter(("gate_session_postgres_outer_rollback",)),
            )

            with pytest.raises(RuntimeError):
                with connection.transaction():
                    prepared = service.prepare(
                        _context(registry),
                        _durable_request(),
                    )
                    assert prepared.session.status == "prepared"
                    raise RuntimeError("caller rollback")

            with pytest.raises(tbm.PostgresGateSessionNotFoundError):
                sessions.get("gate_session_postgres_outer_rollback")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM "
                    "trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots"
                )
                assert cursor.fetchone() == (0,)
                cursor.execute(
                    "SELECT count(*) FROM "
                    "trace_backed_memory_v3_gate_evidence."
                    "v3_system_gate_evaluations"
                )
                assert cursor.fetchone() == (0,)
    finally:
        decisions.close()
