from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

import trace_backed_memory as tbm
from tests.test_artifact_service_v3 import _context, _registry
from tests.test_gate_service_v3 import _Clock
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _PolicyProvider,
    _Source,
    _candidate,
    _indexes,
    _policy,
    _record,
    _request,
    _result,
    _retrieval_authorization,
    _service,
)


def _durable_request(
    *,
    idempotency_key: str = "durable_retrieval_001",
) -> tbm.DurableRetrievalPreparationRequest:
    request = _request()
    return tbm.DurableRetrievalPreparationRequest(
        request_id=request.request_id,
        trace_id=request.trace_id,
        run_id=request.run_id,
        context=request.context,
        retrieval_mode=request.retrieval_mode,
        retriever_id=request.retriever_id,
        retriever_version=request.retriever_version,
        top_k=request.top_k,
        idempotency_key=idempotency_key,
        expires_in_seconds=300,
        lease_seconds=60,
        query=request.query,
        semantic_query=request.semantic_query,
    )


def _sqlite_authorities():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-gate-session.sql").decode("utf-8")
    )
    connection.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-gate-evidence.sql").decode(
            "utf-8"
        )
    )
    return (
        connection,
        tbm.SQLiteGateSessionRepository(
            connection,
            clock=_Clock(),
            allow_direct_completion=False,
        ),
        tbm.SQLiteGateEvidenceV3Repository(connection),
    )


def _durable_service(
    authorization,
    sessions,
    evidence,
    discovery,
    source,
    *,
    session_ids=None,
):
    ids = session_ids or iter(("gate_session_durable_001",))
    retrieval = _service(
        authorization,
        _PolicyProvider(_policy()),
        discovery,
        source,
    )
    gate = tbm.AuthenticatedGateSessionService(
        authorization_service=authorization,
        session_writer=sessions,
        session_id_factory=lambda: next(ids),
        evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
    )
    return tbm.DurableRetrievalPreparationService(
        gate_session_service=gate,
        retrieval_service=retrieval,
        evidence_authority=evidence,
    )


class _AdvanceSessionOnSnapshotLoad:
    def __init__(self, authority, sessions, prepared) -> None:
        self._authority = authority
        self._sessions = sessions
        self._prepared = prepared
        self._advanced = False

    def store_bundle(self, *args, **kwargs):
        return self._authority.store_bundle(*args, **kwargs)

    def load_snapshot(self, snapshot_id):
        snapshot = self._authority.load_snapshot(snapshot_id)
        if not self._advanced:
            self._advanced = True
            self._sessions.transition(
                self._prepared.session_id,
                "awaiting_decision",
                expected_version=self._prepared.version,
            )
        return snapshot

    def load_evaluation(self, evaluation_id):
        return self._authority.load_evaluation(evaluation_id)


def test_durable_retrieval_preparation_attaches_exact_evidence_with_one_authorization():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    try:
        result = _durable_service(
            authorization,
            sessions,
            evidence,
            discovery,
            _Source((candidate,)),
        ).prepare(_context(registry), _durable_request())

        assert result.session.status == "prepared"
        assert result.session.version == 2
        assert result.value.snapshot.session_id == result.session.session_id
        assert result.value.snapshot.authorization_event_id == (
            result.scope.authorization_event_id
        )
        assert result.session.retrieval_snapshot_id == result.value.snapshot.snapshot_id
        assert result.session.system_gate_evaluation_id == (
            result.value.system_gate_evaluation.evaluation_id
        )
        assert evidence.load_snapshot(result.value.snapshot.snapshot_id) == (
            result.value.snapshot
        )
        assert (
            evidence.load_evaluation(result.value.system_gate_evaluation.evaluation_id)
            == result.value.system_gate_evaluation
        )
        assert [
            item.status for item in sessions.history(result.session.session_id)
        ] == [
            "created",
            "prepared",
        ]
        assert discovery.calls == 1
        assert (
            len(decisions.list_decisions(registry.authorization_policy.policy_sha256))
            == 1
        )
    finally:
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()


def test_durable_retrieval_exact_replay_does_not_repeat_discovery_or_evidence():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable_replay")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    service = _durable_service(
        authorization,
        sessions,
        evidence,
        discovery,
        _Source((candidate,)),
        session_ids=iter(("gate_session_replay_001", "gate_session_replay_002")),
    )
    try:
        first = service.prepare(_context(registry), _durable_request())
        replay = service.prepare(_context(registry), _durable_request())

        assert replay == first
        assert discovery.calls == 1
        assert (
            evidence.load_snapshot(first.value.snapshot.snapshot_id)
            == first.value.snapshot
        )
        assert [item.status for item in sessions.history(first.session.session_id)] == [
            "created",
            "prepared",
        ]
    finally:
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()


def test_durable_retrieval_exact_replay_rejects_concurrent_head_change():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable_replay_race")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    source = _Source((candidate,))
    request = _durable_request()
    context = _context(registry)
    try:
        first = _durable_service(
            authorization,
            sessions,
            evidence,
            discovery,
            source,
        ).prepare(context, request)
        racing = _AdvanceSessionOnSnapshotLoad(
            evidence,
            sessions,
            first.session,
        )

        with pytest.raises(tbm.GateSessionReplayError) as captured:
            _durable_service(
                authorization,
                sessions,
                racing,
                discovery,
                source,
            ).prepare(context, request)

        assert captured.value.session.status == "awaiting_decision"
        assert discovery.calls == 1
    finally:
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()


def test_durable_retrieval_resumes_created_session_after_interruption():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable_created_resume")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    request = _durable_request()
    context = _context(registry)
    gate_request = request.gate_request()

    def create_only(scope):
        return sessions.create_or_get(
            session_id="gate_session_interrupted_created",
            tenant_id=scope.tenant_id,
            repository_id=scope.repository_id,
            principal_id=scope.principal_id,
            agent_client_id=scope.agent_client_id,
            trace_id=gate_request.trace_id,
            run_id=gate_request.run_id,
            request_fingerprint=gate_request.request_fingerprint,
            idempotency_key=gate_request.idempotency_key,
            expires_in_seconds=gate_request.expires_in_seconds,
        )

    try:
        interrupted = authorization.authorize_retrieval(
            context,
            create_only,
        ).value.session
        assert interrupted.status == "created"

        resumed = _durable_service(
            authorization,
            sessions,
            evidence,
            discovery,
            _Source((candidate,)),
            session_ids=iter(("gate_session_unused_after_interruption",)),
        ).prepare(context, request)

        assert resumed.session.status == "prepared"
        assert resumed.session.session_id == interrupted.session_id
        assert [
            item.status for item in sessions.history(interrupted.session_id)
        ] == ["created", "prepared"]
        assert resumed.value.snapshot.authorization_event_id == (
            resumed.scope.authorization_event_id
        )
        assert discovery.calls == 1
        assert len(
            decisions.list_decisions(
                registry.authorization_policy.policy_sha256
            )
        ) == 2
    finally:
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()


def test_durable_retrieval_request_fingerprint_is_stable_and_complete():
    request = _durable_request()
    assert request.request_fingerprint == _durable_request().request_fingerprint
    assert request.request_fingerprint.startswith("sha256:")
    assert request.retrieval_request("gate_session_exact").session_id == (
        "gate_session_exact"
    )
    assert request.gate_request().request_fingerprint == request.request_fingerprint
    assert (
        replace(request, top_k=request.top_k + 1).request_fingerprint
        != request.request_fingerprint
    )
    assert (
        replace(request, lease_seconds=request.lease_seconds + 1).request_fingerprint
        != request.request_fingerprint
    )
    assert b"repair the cache" not in repr(request).encode("utf-8")


def test_authorized_scope_hook_rejects_forged_scope_before_discovery():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    candidate = _candidate("memory_forged_scope")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    request = _request()
    context = _context(registry)
    forged_scope = tbm.AuthorizedRetrievalScope(
        authorization_event_id="authz_sha256_" + ("0" * 64),
        organization_id="organization_001",
        principal_id=context.principal.principal_id,
        agent_client_id=context.agent_client.agent_client_id,
        tenant_id=context.tenant_id,
        repository_id=request.context.repository_id,
        environment_id=context.environment_id,
    )
    service = _service(
        authorization,
        _PolicyProvider(_policy()),
        discovery,
        _Source((candidate,)),
    )
    try:
        with pytest.raises(tbm.RetrievalPreparationV3Error) as invalid:
            service.prepare_for_authorized_scope(
                context,
                forged_scope,
                request,
            )

        assert invalid.value.code == "TBM_SERVICE_AUTHORIZATION_SCOPE_INVALID"
        assert discovery.calls == 0
        assert (
            decisions.list_decisions(registry.authorization_policy.policy_sha256) == ()
        )
    finally:
        decisions.close()


def test_authorized_scope_reverification_rejects_cross_principal_reuse():
    original = _registry(permissions=("memory:retrieve",))
    principal_a = original.authorization_policy.principals[0]
    client_a = original.authorization_policy.agent_clients[0]
    binding_a = original.authorization_policy.role_bindings[0]
    principal_b = replace(
        principal_a,
        principal_id="principal_002",
        subject_hash="sha256:" + ("b" * 64),
    )
    client_b = replace(client_a, agent_client_id="client_002")
    binding_b = replace(
        binding_a,
        binding_id="binding_002",
        principal_id=principal_b.principal_id,
        agent_client_id=client_b.agent_client_id,
    )
    policy = replace(
        original.authorization_policy,
        principals=(principal_a, principal_b),
        agent_clients=(client_a, client_b),
        role_bindings=(binding_a, binding_b),
    )
    registry = replace(original, authorization_policy=policy)
    context_a = _context(registry)
    context_b = replace(
        context_a,
        principal=principal_b,
        agent_client=client_b,
    )
    authorization, decisions = _retrieval_authorization(registry)
    try:
        authorized_a = authorization.authorize_retrieval(
            context_a,
            lambda scope: scope,
        )

        with pytest.raises(tbm.AuthenticatedServiceV3Error) as invalid:
            authorization.verify_authorized_scope(
                context_b,
                authorized_a.scope,
                permission="memory:retrieve",
            )

        assert invalid.value.code == "TBM_SERVICE_ENTITY_CONTEXT_REJECTED"
        assert len(decisions.list_decisions(policy.policy_sha256)) == 1
    finally:
        decisions.close()


@pytest.mark.parametrize(
    "changes",
    (
        {"contract_version": "unsupported"},
        {"request_id": ""},
        {"idempotency_key": ""},
        {"expires_in_seconds": 0},
        {"lease_seconds": 0},
        {"query": None},
    ),
)
def test_durable_retrieval_request_rejects_invalid_inputs(changes):
    with pytest.raises((tbm.DurableRetrievalPreparationV3Error, ValueError)):
        replace(_durable_request(), **changes)


class _FailingEvidenceAuthority:
    def store_bundle(self, _snapshot, _evaluation):
        raise RuntimeError("private store failure")

    def load_snapshot(self, _snapshot_id):
        raise AssertionError("not reached")

    def load_evaluation(self, _evaluation_id):
        raise AssertionError("not reached")


def test_durable_retrieval_store_failure_cancels_created_session():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, _evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable_store_failure")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    authority = _FailingEvidenceAuthority()
    gate = tbm.AuthenticatedGateSessionService(
        authorization_service=authorization,
        session_writer=sessions,
        session_id_factory=lambda: "gate_session_store_failure",
        evidence_verifier=lambda *_args: None,
    )
    service = tbm.DurableRetrievalPreparationService(
        gate_session_service=gate,
        retrieval_service=_service(
            authorization,
            _PolicyProvider(_policy()),
            discovery,
            _Source((candidate,)),
        ),
        evidence_authority=authority,
    )
    try:
        with pytest.raises(tbm.GatePreparationFailedError) as failed:
            service.prepare(_context(registry), _durable_request())

        assert failed.value.session.status == "canceled"
        assert failed.value.session.terminal_reason == "prepare_failed"
        assert "private" not in str(failed.value)
        assert [
            item.status for item in sessions.history(failed.value.session.session_id)
        ] == [
            "created",
            "canceled",
        ]
    finally:
        decisions.close()
        sessions.close()
        connection.close()


class _InvalidReceiptAuthority:
    def __init__(self, delegate):
        self.delegate = delegate

    def store_bundle(self, snapshot, evaluation):
        self.delegate.store_bundle(snapshot, evaluation)
        return object()

    def load_snapshot(self, snapshot_id):
        return self.delegate.load_snapshot(snapshot_id)

    def load_evaluation(self, evaluation_id):
        return self.delegate.load_evaluation(evaluation_id)


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


def test_durable_retrieval_invalid_store_receipt_cancels_but_retains_evidence():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable_receipt")
    discovery_result = _result(
        records=(_record(candidate),),
        index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
    )
    authority = _InvalidReceiptAuthority(evidence)
    service = _durable_service(
        authorization,
        sessions,
        authority,
        _Discovery(discovery_result),
        _Source((candidate,)),
    )
    try:
        with pytest.raises(tbm.GatePreparationFailedError) as failed:
            service.prepare(_context(registry), _durable_request())

        assert failed.value.session.status == "canceled"
        stored_session = failed.value.session.session_id
        snapshot_rows = connection.execute(
            "SELECT snapshot_id FROM v3_retrieval_snapshots WHERE session_id = ?",
            (stored_session,),
        ).fetchall()
        evaluation_rows = connection.execute(
            "SELECT evaluation_id FROM v3_system_gate_evaluations WHERE session_id = ?",
            (stored_session,),
        ).fetchall()
        assert len(snapshot_rows) == 1
        assert len(evaluation_rows) == 1
    finally:
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()


def test_sqlite_durable_retrieval_compensates_after_evidence_is_durable():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_sqlite_compensation")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    retrieval = _service(
        authorization,
        _PolicyProvider(_policy()),
        discovery,
        _Source((candidate,)),
    )
    gate = tbm.AuthenticatedGateSessionService(
        authorization_service=authorization,
        session_writer=_PreparedTransitionFailure(sessions),
        session_id_factory=lambda: "gate_session_sqlite_compensation",
        evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
    )
    service = tbm.DurableRetrievalPreparationService(
        gate_session_service=gate,
        retrieval_service=retrieval,
        evidence_authority=evidence,
    )
    try:
        with pytest.raises(tbm.GatePreparationFailedError) as failed:
            service.prepare(_context(registry), _durable_request())

        assert failed.value.session.status == "canceled"
        assert "private" not in str(failed.value)
        assert connection.execute(
            "SELECT count(*) FROM v3_retrieval_snapshots"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM v3_system_gate_evaluations"
        ).fetchone() == (1,)
        assert [
            item.status for item in sessions.history(failed.value.session.session_id)
        ] == [
            "created",
            "canceled",
        ]
    finally:
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()


def test_sqlite_durable_retrieval_respects_caller_transaction_rollback():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable_outer_rollback")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    service = _durable_service(
        authorization,
        sessions,
        evidence,
        discovery,
        _Source((candidate,)),
        session_ids=iter(("gate_session_sqlite_outer_rollback",)),
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        prepared = service.prepare(_context(registry), _durable_request())
        assert prepared.session.status == "prepared"
        connection.rollback()

        with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
            sessions.get("gate_session_sqlite_outer_rollback")
        assert connection.execute(
            "SELECT count(*) FROM v3_retrieval_snapshots"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM v3_system_gate_evaluations"
        ).fetchone() == (0,)
    finally:
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()


def test_durable_retrieval_service_rejects_misconfiguration_and_invalid_calls():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    other_authorization, other_decisions = _retrieval_authorization(registry)
    connection, sessions, evidence = _sqlite_authorities()
    candidate = _candidate("memory_durable_configuration")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    retrieval = _service(
        authorization,
        _PolicyProvider(_policy()),
        discovery,
        _Source((candidate,)),
    )
    gate = tbm.AuthenticatedGateSessionService(
        authorization_service=authorization,
        session_writer=sessions,
        session_id_factory=lambda: "gate_session_configuration",
        evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
    )
    try:
        for changes in (
            {"gate_session_service": object()},
            {"retrieval_service": object()},
            {"evidence_authority": object()},
        ):
            values = {
                "gate_session_service": gate,
                "retrieval_service": retrieval,
                "evidence_authority": evidence,
                **changes,
            }
            with pytest.raises(TypeError):
                tbm.DurableRetrievalPreparationService(**values)

        other_retrieval = _service(
            other_authorization,
            _PolicyProvider(_policy()),
            discovery,
            _Source((candidate,)),
        )
        with pytest.raises(TypeError):
            tbm.DurableRetrievalPreparationService(
                gate_session_service=gate,
                retrieval_service=other_retrieval,
                evidence_authority=evidence,
            )

        service = tbm.DurableRetrievalPreparationService(
            gate_session_service=gate,
            retrieval_service=retrieval,
            evidence_authority=evidence,
        )
        with pytest.raises(tbm.DurableRetrievalPreparationV3Error):
            service.prepare(object(), _durable_request())
        with pytest.raises(tbm.DurableRetrievalPreparationV3Error):
            service.prepare(_context(registry), object())
    finally:
        other_decisions.close()
        decisions.close()
        evidence.close()
        sessions.close()
        connection.close()
