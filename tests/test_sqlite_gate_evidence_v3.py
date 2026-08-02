from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.gate_evidence_v3 import (
    DurablePreparedGateEvidenceVerifier,
    GateEvidenceV3VerificationError,
)
from trace_backed_memory.gate_service_v3 import PreparedGateEvidence
from trace_backed_memory.service_v3 import AuthorizedRetrievalScope
from trace_backed_memory.sqlite_gate_evidence_v3 import (
    SQLiteGateEvidenceV3Error,
    SQLiteGateEvidenceV3ConflictError,
    SQLiteGateEvidenceV3NotFoundError,
    SQLiteGateEvidenceV3PersistenceError,
    SQLiteGateEvidenceV3Repository,
    SQLiteGateEvidenceV3SchemaError,
)
from trace_backed_memory.sqlite_authorization_v3 import (
    SQLiteAuthorizationV3Repository,
)
from trace_backed_memory.sqlite_gate_session_v3 import (
    SQLiteGateSessionRepository,
)


ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 27, 7, 59, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(seconds=1)
        return value.isoformat().replace("+00:00", "Z")


def _records() -> tuple[tbm.RetrievalSnapshot, tbm.SystemGateEvaluation]:
    snapshot = tbm.loads_retrieval_snapshot(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
    )
    evaluation = tbm.loads_system_gate_evaluation(
        (
            ROOT / "examples" / "system_gate_evaluation_v3.example.json"
        ).read_bytes()
    )
    return snapshot, evaluation


def test_sqlite_gate_evidence_stores_exact_bundle_and_replays_idempotently():
    snapshot, evaluation = _records()
    with SQLiteGateEvidenceV3Repository.connect(
        initialize=True
    ) as repository:
        first = repository.store_bundle(snapshot, evaluation)
        second = repository.store_bundle(snapshot, evaluation)

        assert first.snapshot_inserted is True
        assert first.evaluation_inserted is True
        assert second.snapshot_inserted is False
        assert second.evaluation_inserted is False
        assert repository.load_snapshot(snapshot.snapshot_id) == snapshot
        assert repository.load_evaluation(evaluation.evaluation_id) == evaluation


def test_sqlite_gate_evidence_rejects_second_evaluation_for_snapshot():
    snapshot, evaluation = _records()
    conflicting = tbm.build_system_gate_evaluation(
        session_id=evaluation.session_id,
        retrieval_snapshot_id=evaluation.retrieval_snapshot_id,
        authorization_event_id=evaluation.authorization_event_id,
        policy_bundle_sha256=evaluation.policy_bundle_sha256,
        evaluator_id=evaluation.evaluator_id,
        evaluator_version="3.1",
        decisions=evaluation.decisions,
        evaluated_at=evaluation.evaluated_at,
    )
    with SQLiteGateEvidenceV3Repository.connect(
        initialize=True
    ) as repository:
        repository.store_bundle(snapshot, evaluation)
        with pytest.raises(SQLiteGateEvidenceV3ConflictError):
            repository.store_bundle(snapshot, conflicting)
        assert repository.load_evaluation(evaluation.evaluation_id) == evaluation
        with pytest.raises(SQLiteGateEvidenceV3NotFoundError):
            repository.load_evaluation(conflicting.evaluation_id)


def test_sqlite_gate_evidence_detects_schema_drift_before_write():
    snapshot, evaluation = _records()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-gate-evidence.sql").read_text(
            encoding="utf-8"
        )
    )
    connection.execute("DROP INDEX v3_retrieval_snapshots_session")
    repository = SQLiteGateEvidenceV3Repository(connection)

    with pytest.raises(SQLiteGateEvidenceV3SchemaError):
        repository.store_bundle(snapshot, evaluation)


def test_sqlite_gate_evidence_requires_recursive_triggers_on_injected_connection():
    snapshot, evaluation = _records()
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-gate-evidence.sql").read_text(
            encoding="utf-8"
        )
    )
    connection.execute("PRAGMA recursive_triggers = OFF")
    repository = SQLiteGateEvidenceV3Repository(connection)

    with pytest.raises(
        SQLiteGateEvidenceV3SchemaError,
        match="recursive triggers",
    ):
        repository.store_bundle(snapshot, evaluation)


def test_sqlite_gate_evidence_nested_savepoint_is_caller_owned():
    snapshot, evaluation = _records()
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-gate-evidence.sql").read_text(
            encoding="utf-8"
        )
    )
    repository = SQLiteGateEvidenceV3Repository(connection)
    connection.execute("BEGIN")

    repository.store_bundle(snapshot, evaluation)
    assert connection.in_transaction is True
    connection.rollback()
    with pytest.raises(SQLiteGateEvidenceV3NotFoundError):
        repository.load_snapshot(snapshot.snapshot_id)


def test_sqlite_gate_evidence_replace_cannot_bypass_immutability():
    snapshot, evaluation = _records()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-gate-evidence.sql").read_text(
            encoding="utf-8"
        )
    )
    repository = SQLiteGateEvidenceV3Repository(connection)
    repository.store_bundle(snapshot, evaluation)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO v3_retrieval_snapshots ("
            "snapshot_id, session_id, authorization_event_id, descriptor"
            ") VALUES (?, ?, ?, ?)",
            (
                snapshot.snapshot_id,
                snapshot.session_id,
                snapshot.authorization_event_id,
                tbm.dumps_retrieval_snapshot(snapshot),
            ),
        )
    assert repository.load_snapshot(snapshot.snapshot_id) == snapshot


def test_sqlite_gate_evidence_direct_insert_requires_parent_scope_match():
    snapshot, evaluation = _records()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-gate-evidence.sql").read_text(
            encoding="utf-8"
        )
    )
    connection.execute(
        "INSERT INTO v3_retrieval_snapshots ("
        "snapshot_id, session_id, authorization_event_id, descriptor"
        ") VALUES (?, ?, ?, ?)",
        (
            snapshot.snapshot_id,
            snapshot.session_id,
            snapshot.authorization_event_id,
            tbm.dumps_retrieval_snapshot(snapshot),
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="parent scope"):
        connection.execute(
            "INSERT INTO v3_system_gate_evaluations ("
            "evaluation_id, session_id, retrieval_snapshot_id, "
            "authorization_event_id, descriptor"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                evaluation.evaluation_id,
                "different_session",
                evaluation.retrieval_snapshot_id,
                evaluation.authorization_event_id,
                tbm.dumps_system_gate_evaluation(evaluation),
            ),
        )
    connection.close()


def test_sqlite_gate_evidence_rejects_invalid_inputs_and_missing_records():
    snapshot, evaluation = _records()
    with pytest.raises(ValueError, match="sqlite3.Connection"):
        SQLiteGateEvidenceV3Repository(object())  # type: ignore[arg-type]
    with pytest.raises(
        SQLiteGateEvidenceV3PersistenceError,
        match="connect",
    ):
        SQLiteGateEvidenceV3Repository.connect(
            ROOT / "missing-parent" / "evidence.sqlite3",
        )
    with SQLiteGateEvidenceV3Repository.connect(
        initialize=True
    ) as repository:
        with pytest.raises(ValueError, match="exact v3 records"):
            repository.store_bundle(object(), evaluation)  # type: ignore[arg-type]
        with pytest.raises(SQLiteGateEvidenceV3NotFoundError):
            repository.load_snapshot(snapshot.snapshot_id)
        with pytest.raises(SQLiteGateEvidenceV3NotFoundError):
            repository.load_evaluation(evaluation.evaluation_id)

    with pytest.raises(SQLiteGateEvidenceV3Error, match="closed"):
        repository.load_snapshot(snapshot.snapshot_id)
    repository.close()


@pytest.mark.parametrize(
    ("loader", "row"),
    (
        (
            SQLiteGateEvidenceV3Repository._snapshot_from_row,
            ("wrong",),
        ),
        (
            SQLiteGateEvidenceV3Repository._snapshot_from_row,
            ("id", "session", "authz", "{}"),
        ),
        (
            SQLiteGateEvidenceV3Repository._evaluation_from_row,
            ("wrong",),
        ),
        (
            SQLiteGateEvidenceV3Repository._evaluation_from_row,
            ("id", "session", "snapshot", "authz", "{}"),
        ),
    ),
)
def test_sqlite_gate_evidence_rejects_corrupt_rows(loader, row):
    with pytest.raises(SQLiteGateEvidenceV3PersistenceError):
        loader(row)


def test_durable_verifier_requires_exact_authorized_session_linkage():
    snapshot, evaluation = _records()
    session = tbm.create_gate_session(
        session_id=snapshot.session_id,
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id=snapshot.trace_id,
        run_id=snapshot.run_id,
        request_fingerprint="sha256:" + "1" * 64,
        idempotency_key="idempotency_001",
        created_at="2026-07-27T07:59:00Z",
        expires_at="2026-07-27T09:00:00Z",
    )
    scope = AuthorizedRetrievalScope(
        authorization_event_id=snapshot.authorization_event_id,
        organization_id="organization_001",
        principal_id=session.principal_id,
        agent_client_id=session.agent_client_id,
        tenant_id=session.tenant_id,
        repository_id=session.repository_id,
        environment_id="environment_001",
    )
    evidence = PreparedGateEvidence(
        retrieval_snapshot_id=snapshot.snapshot_id,
        system_gate_evaluation_id=evaluation.evaluation_id,
        value="prepared",
    )
    with SQLiteGateEvidenceV3Repository.connect(
        initialize=True
    ) as repository:
        repository.store_bundle(snapshot, evaluation)
        verifier = DurablePreparedGateEvidenceVerifier(repository)
        verifier(scope, session, evidence)

        with pytest.raises(
            GateEvidenceV3VerificationError,
            match="authorized scope",
        ):
            verifier(
                scope,
                replace(session, repository_id="repository_other"),
                evidence,
            )


def test_durable_verifier_sanitizes_unavailable_and_invalid_authorities():
    snapshot, evaluation = _records()
    session = tbm.create_gate_session(
        session_id=snapshot.session_id,
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id=snapshot.trace_id,
        run_id=snapshot.run_id,
        request_fingerprint="sha256:" + "1" * 64,
        idempotency_key="idempotency_001",
        created_at="2026-07-27T07:59:00Z",
        expires_at="2026-07-27T09:00:00Z",
    )
    scope = AuthorizedRetrievalScope(
        authorization_event_id=snapshot.authorization_event_id,
        organization_id="organization_001",
        principal_id=session.principal_id,
        agent_client_id=session.agent_client_id,
        tenant_id=session.tenant_id,
        repository_id=session.repository_id,
        environment_id="environment_001",
    )
    evidence = PreparedGateEvidence(
        snapshot.snapshot_id,
        evaluation.evaluation_id,
        "prepared",
    )

    class _Unavailable:
        def load_snapshot(self, _snapshot_id: str):
            raise RuntimeError("secret backend error")

    with pytest.raises(
        GateEvidenceV3VerificationError,
        match="unavailable",
    ) as unavailable:
        DurablePreparedGateEvidenceVerifier(_Unavailable())(
            scope,
            session,
            evidence,
        )
    assert "secret" not in str(unavailable.value)

    class _WrongRecord:
        def load_snapshot(self, _snapshot_id: str):
            return object()

        def load_evaluation(self, _evaluation_id: str):
            return evaluation

    with pytest.raises(
        GateEvidenceV3VerificationError,
        match="different records",
    ):
        DurablePreparedGateEvidenceVerifier(_WrongRecord())(
            scope,
            session,
            evidence,
        )
    with pytest.raises(GateEvidenceV3VerificationError, match="input"):
        DurablePreparedGateEvidenceVerifier(_WrongRecord())(
            scope,
            session,
            object(),  # type: ignore[arg-type]
        )

    class _MustNotRead:
        def load_snapshot(self, _snapshot_id: str):
            raise AssertionError("reader must not receive invalid identifiers")

        def load_evaluation(self, _evaluation_id: str):
            raise AssertionError("reader must not receive invalid identifiers")

    with pytest.raises(
        GateEvidenceV3VerificationError,
        match="identifiers",
    ):
        DurablePreparedGateEvidenceVerifier(_MustNotRead())(
            scope,
            session,
            PreparedGateEvidence(
                "x" * 1_000_000,
                evaluation.evaluation_id,
                "prepared",
            ),
        )


def test_authenticated_gate_service_prepares_from_durable_exact_records():
    registry = tbm.loads_entity_registry(
        (ROOT / "examples" / "entity_registry_v3.example.json").read_bytes()
    )
    policy = registry.authorization_policy
    context = tbm.AuthenticatedServiceContext(
        principal=policy.principals[0],
        agent_client=policy.agent_clients[0],
        tenant_id="tenant_001",
        repository_reference="owner/repository",
        environment_id="environment_001",
    )
    template, _template_evaluation = _records()
    with (
        SQLiteAuthorizationV3Repository.connect(
            initialize=True
        ) as authorization,
        SQLiteGateSessionRepository.connect(
            initialize=True,
            clock=_Clock(),
        ) as sessions,
        SQLiteGateEvidenceV3Repository.connect(
            initialize=True
        ) as evidence_repository,
    ):
        authorization_service = tbm.AuthenticatedRetrievalService(
            registry_provider=lambda: registry,
            decision_writer=authorization,
            clock=lambda: "2026-07-27T07:58:00Z",
            request_id_factory=lambda: "authorization_request_001",
        )
        service = tbm.AuthenticatedGateSessionService(
            authorization_service=authorization_service,
            session_writer=sessions,
            session_id_factory=lambda: "session_001",
            evidence_verifier=DurablePreparedGateEvidenceVerifier(
                evidence_repository
            ),
        )

        def prepare(
            scope: AuthorizedRetrievalScope,
            session: tbm.GateSession,
        ) -> PreparedGateEvidence[str]:
            snapshot = tbm.build_retrieval_snapshot(
                session_id=session.session_id,
                request_id=template.request_id,
                trace_id=session.trace_id,
                run_id=session.run_id,
                authorization_event_id=scope.authorization_event_id,
                context_sha256=template.context_sha256,
                query_sha256=template.query_sha256,
                retrieval_mode=template.retrieval_mode,
                retriever_id=template.retriever_id,
                retriever_version=template.retriever_version,
                index_versions=template.index_versions,
                hits=template.hits,
                total_candidates=template.total_candidates,
                top_k=template.top_k,
                truncated=template.truncated,
                truncation_reasons=template.truncation_reasons,
                created_at="2026-07-27T08:00:00Z",
            )
            evaluation = tbm.build_system_gate_evaluation(
                session_id=session.session_id,
                retrieval_snapshot_id=snapshot.snapshot_id,
                authorization_event_id=scope.authorization_event_id,
                policy_bundle_sha256="sha256:" + "4" * 64,
                evaluator_id="system_gate",
                evaluator_version="3.0",
                decisions=_template_evaluation.decisions,
                evaluated_at="2026-07-27T08:01:00Z",
            )
            evidence_repository.store_bundle(snapshot, evaluation)
            return PreparedGateEvidence(
                retrieval_snapshot_id=snapshot.snapshot_id,
                system_gate_evaluation_id=evaluation.evaluation_id,
                value="prepared",
            )

        result = service.prepare(
            context,
            tbm.GatePreparationRequest(
                trace_id=template.trace_id,
                run_id=template.run_id,
                request_fingerprint="sha256:" + "5" * 64,
                idempotency_key="idempotency_001",
                expires_in_seconds=300,
                lease_seconds=60,
            ),
            prepare,
        )

        assert result.session.status == "prepared"
        assert result.value == "prepared"
        assert evidence_repository.load_snapshot(
            result.session.retrieval_snapshot_id
        ).authorization_event_id == result.authorization.authorization_event_id


def test_sqlite_gate_evidence_public_exports_are_intentional():
    assert (
        tbm.SQLiteGateEvidenceV3Repository
        is SQLiteGateEvidenceV3Repository
    )
    assert (
        tbm.DurablePreparedGateEvidenceVerifier
        is DurablePreparedGateEvidenceVerifier
    )
    assert "SQLiteGateEvidenceV3Repository" in tbm.__all__
