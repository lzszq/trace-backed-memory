from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

import trace_backed_memory as tbm
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


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(seconds=1)
        return value.isoformat().replace("+00:00", "Z")


@dataclass
class _Stack:
    connection: sqlite3.Connection
    decisions: tbm.SQLiteAuthorizationV3Repository
    authorization: tbm.AuthenticatedRetrievalService
    context: tbm.AuthenticatedServiceContext
    scope: tbm.AuthorizedRetrievalScope
    sessions: tbm.SQLiteGateSessionRepository
    evidence: tbm.SQLiteGateEvidenceV3Repository
    semantic: tbm.SQLiteSemanticGateArtifactV3Repository
    replay: tbm.SQLiteReplayV3Repository
    source: _Source
    policy: tbm.RetrievalPolicyBundle
    decided: tbm.GateSession

    def finalizer(
        self,
        *,
        session_writer: object | None = None,
        policy_loader=None,
    ) -> tbm.DurableFinalizationService:
        return tbm.DurableFinalizationService(
            authorization_service=self.authorization,
            session_writer=session_writer or self.sessions,  # type: ignore[arg-type]
            evidence_reader=self.evidence,
            semantic_authority=self.semantic,
            revision_source=self.source,
            policy_loader=policy_loader or (lambda: self.policy),
            replay_authority=self.replay,
            clock=_Clock(),
        )

    def close(self) -> None:
        self.replay.close()
        self.semantic.close()
        self.evidence.close()
        self.sessions.close()
        self.decisions.close()
        self.connection.close()


class _FailingFinalizedTransition:
    def __init__(self, sessions: tbm.SQLiteGateSessionRepository) -> None:
        self._sessions = sessions

    def get(self, session_id: str) -> tbm.GateSession:
        return self._sessions.get(session_id)

    def renew_lease(self, *args, **kwargs) -> tbm.GateSession:
        return self._sessions.renew_lease(*args, **kwargs)

    def transition(self, *args, **kwargs) -> tbm.GateSession:
        raise RuntimeError("private finalization write failure")


def _stack() -> _Stack:
    registry = _registry(permissions=("memory:retrieve",))
    context = _service_context(registry)
    authorization, decisions = _retrieval_authorization(registry)
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-session.sql",
        "schemas/sqlite-v3-gate-evidence.sql",
        "schemas/sqlite-v3-semantic-gate.sql",
        "schemas/sqlite-v3-semantic-gate-artifacts.sql",
        "schemas/sqlite-v3-replay.sql",
    ):
        connection.executescript(tbm.read_packaged_resource(resource).decode("utf-8"))
    sessions = tbm.SQLiteGateSessionRepository(
        connection,
        clock=_Clock(),
        allow_direct_completion=False,
    )
    evidence = tbm.SQLiteGateEvidenceV3Repository(connection)
    semantic = tbm.SQLiteSemanticGateArtifactV3Repository(connection)
    replay = tbm.SQLiteReplayV3Repository(connection)
    policy = _policy()
    candidate = _candidate("memory_finalization")
    source = _Source((candidate,))
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
    retrieval = _service(
        authorization,
        _PolicyProvider(policy),
        discovery,
        source,
    )
    gate = tbm.AuthenticatedGateSessionService(
        authorization_service=authorization,
        session_writer=sessions,
        session_id_factory=lambda: "gate_session_finalization_001",
        evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
    )
    prepared_result = tbm.DurableRetrievalPreparationService(
        gate_session_service=gate,
        retrieval_service=retrieval,
        evidence_authority=evidence,
    ).prepare(
        context,
        replace(_durable_request(), expires_in_seconds=3_600),
    )
    semantic_service = _semantic_service(
        semantic,
        evidence,
        iter(("2026-07-30T01:03:00Z", "2026-07-30T01:03:00.125Z")),
    )
    decided = (
        tbm.AuthenticatedSemanticGateSessionService(
            semantic_gate_service=semantic_service,
            session_writer=sessions,
        )
        .decide(
            _provider_context(),
            _semantic_request(prepared_result.session),
            lambda _call: _provider_result(
                prepared_result.value.system_gate_evaluation
            ),
        )
        .session
    )
    return _Stack(
        connection,
        decisions,
        authorization,
        context,
        prepared_result.scope,
        sessions,
        evidence,
        semantic,
        replay,
        source,
        policy,
        decided,
    )


def _request(stack: _Stack) -> tbm.DurableFinalizationRequest:
    return tbm.DurableFinalizationRequest(
        session_id=stack.decided.session_id,
        expected_session_version=stack.decided.version,
    )


def test_durable_finalization_retains_complete_bundle_and_exactly_replays() -> None:
    stack = _stack()
    try:
        result = stack.finalizer().finalize(
            stack.context,
            stack.scope,
            _request(stack),
        )

        assert result.replayed is False
        assert result.session.status == "finalized"
        assert result.session.final_memory_revision_ids == (
            stack.source.candidates["memory_finalization"].revision.revision_id,
        )
        envelope = json.loads(result.snippet)
        assert envelope["contract_version"] == "tbm.injection-render.v3"
        assert envelope["memory_items"][0]["memory_id"] == "memory_finalization"
        assert result.usage_decision.injection_artifact_id == (
            result.injection.artifact.artifact_id
        )
        assert result.manifest.completeness == "complete"
        for _, digest in result.manifest.components:
            assert digest is not None
            assert (
                stack.replay.load_artifact(
                    tbm.artifact_id_from_sha256(digest)
                ).artifact.content_sha256
                == digest
            )

        replayed = stack.finalizer().finalize(
            stack.context,
            stack.scope,
            _request(stack),
        )
        assert replayed == tbm.DurableFinalizationResult(
            session=result.session,
            usage_decision=result.usage_decision,
            injection=result.injection,
            manifest=result.manifest,
            snippet=result.snippet,
            replayed=True,
        )
    finally:
        stack.close()


def test_durable_finalization_rejects_rotated_authorization_even_when_valid() -> None:
    stack = _stack()
    try:
        rotated = stack.authorization.authorize_retrieval(
            stack.context,
            lambda scope: scope,
        ).scope
        assert rotated.authorization_event_id != stack.scope.authorization_event_id

        with pytest.raises(
            tbm.DurableFinalizationV3Error,
            match="authorization changed",
        ) as captured:
            stack.finalizer().finalize(
                stack.context,
                rotated,
                _request(stack),
            )
        assert captured.value.code == ("TBM_DURABLE_FINALIZATION_AUTHORIZATION_STALE")
    finally:
        stack.close()


def test_exact_terminal_replay_rejects_rotated_authorization_event() -> None:
    stack = _stack()
    try:
        finalized = stack.finalizer().finalize(
            stack.context,
            stack.scope,
            _request(stack),
        )
        assert finalized.session.status == "finalized"
        rotated = stack.authorization.authorize_retrieval(
            stack.context,
            lambda scope: scope,
        ).scope
        assert rotated.authorization_event_id != stack.scope.authorization_event_id

        with pytest.raises(
            tbm.DurableFinalizationV3Error,
            match="authorization changed",
        ) as captured:
            stack.finalizer().finalize(
                stack.context,
                rotated,
                _request(stack),
            )
        assert captured.value.code == ("TBM_DURABLE_FINALIZATION_AUTHORIZATION_STALE")
    finally:
        stack.close()


def test_durable_finalization_fails_closed_on_stale_head_before_replay_write() -> None:
    stack = _stack()
    stack.source.stale = True
    try:
        with pytest.raises(tbm.DurableFinalizationV3Error) as captured:
            stack.finalizer().finalize(
                stack.context,
                stack.scope,
                _request(stack),
            )
        assert captured.value.code == "TBM_DURABLE_FINALIZATION_STALE"
        assert stack.sessions.get(stack.decided.session_id).status == "decided"
        assert stack.connection.execute(
            "SELECT COUNT(*) FROM v3_replay_artifacts"
        ).fetchone() == (0,)
    finally:
        stack.close()


def test_durable_finalization_retains_bundle_for_explicit_recovery() -> None:
    stack = _stack()
    writer = _FailingFinalizedTransition(stack.sessions)
    try:
        with pytest.raises(tbm.DurableFinalizationRecoveryRequiredError) as captured:
            stack.finalizer(session_writer=writer).finalize(
                stack.context,
                stack.scope,
                _request(stack),
            )
        error = captured.value
        assert error.usage_decision is not None
        assert error.injection is not None
        assert stack.sessions.get(stack.decided.session_id).status == "decided"
        assert stack.replay.load_artifact(
            tbm.usage_decision_artifact_id(error.usage_decision.usage_decision_id)
        ) == tbm.create_usage_decision_artifact(error.usage_decision)
        assert (
            stack.replay.load_injection(error.injection.artifact.artifact_id)[0]
            == error.injection
        )
    finally:
        stack.close()


def test_durable_finalization_respects_caller_transaction_rollback() -> None:
    stack = _stack()
    try:
        stack.connection.execute("BEGIN IMMEDIATE")
        result = stack.finalizer().finalize(
            stack.context,
            stack.scope,
            _request(stack),
        )
        stack.connection.rollback()

        assert stack.sessions.get(stack.decided.session_id) == stack.decided
        with pytest.raises(KeyError):
            stack.replay.load_injection(result.injection.artifact.artifact_id)
        with pytest.raises(KeyError):
            stack.replay.load_artifact(
                tbm.usage_decision_artifact_id(result.usage_decision.usage_decision_id)
            )
    finally:
        stack.close()
