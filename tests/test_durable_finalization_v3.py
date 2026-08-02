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


def _stack(
    *,
    permissions: tuple[str, ...] = ("memory:retrieve",),
    connection: sqlite3.Connection | None = None,
    sessions: tbm.SQLiteGateSessionRepository | None = None,
    event_first: bool = False,
) -> _Stack:
    registry = _registry(permissions=permissions)
    context = _service_context(registry)
    authorization, decisions = _retrieval_authorization(registry)
    if connection is None:
        connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    resources = [
        "schemas/sqlite-v3-gate-session.sql",
        "schemas/sqlite-v3-gate-evidence.sql",
        "schemas/sqlite-v3-semantic-gate.sql",
        "schemas/sqlite-v3-semantic-gate-artifacts.sql",
        "schemas/sqlite-v3-replay.sql",
    ]
    if event_first:
        resources.insert(0, "schemas/sqlite-v3-event-ledger.sql")
    for resource in resources:
        connection.executescript(tbm.read_packaged_resource(resource).decode("utf-8"))
    if sessions is None:
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
    stack = _Stack(
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
    if event_first:
        _backfill_gate_session_events(stack)
        stack.sessions.enable_event_first()
        stack.replay.enable_event_first()
    return stack


def _event_context(stack: _Stack) -> tbm.EventTrustedContext:
    scope = stack.scope
    return tbm.EventTrustedContext(
        organization_id=scope.organization_id,
        tenant_id=scope.tenant_id,
        repository_id=scope.repository_id,
        environment_id=scope.environment_id,
        principal_id=scope.principal_id,
        agent_client_id=scope.agent_client_id,
        actor_type="agent_client",
        actor_id=scope.agent_client_id,
        authorization_decision_id=scope.authorization_event_id,
    )


def _event_access(stack: _Stack) -> tbm.LedgerAccessContext:
    trusted = _event_context(stack)
    return tbm.LedgerAccessContext(
        partition=tbm.LedgerTenantPartition(
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
        classification_filter=tbm.LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _backfill_gate_session_events(stack: _Stack) -> None:
    ledger = tbm.SQLiteEventLedgerV1(stack.connection, _event_access(stack))
    previous_session = None
    previous_event = None
    try:
        for session in stack.sessions.history(stack.decided.session_id):
            event = tbm.build_gate_session_event(
                session,
                previous_session=previous_session,
                parent_event=previous_event,
                global_position=session.version,
                trusted_context=_event_context(stack),
            )
            ledger.append(
                session.session_id,
                session.version - 1,
                (event,),
                tbm.LedgerIdempotency(
                    event.idempotency_key_sha256,
                    event.request_sha256,
                ),
            )
            previous_session = session
            previous_event = event
    finally:
        ledger.close()


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


def test_event_first_finalization_appends_events_before_replay_projection() -> None:
    stack = _stack(event_first=True)
    trusted = _event_context(stack)
    try:
        with stack.sessions.bind_event_context(
            trusted
        ), stack.replay.bind_event_context(trusted):
            result = stack.finalizer().finalize(
                stack.context,
                stack.scope,
                _request(stack),
            )

        ledger = tbm.SQLiteEventLedgerV1(
            stack.connection,
            _event_access(stack),
        )
        try:
            events = ledger.read_global().events
        finally:
            ledger.close()
        assert tuple(event.event_type for event in events[-3:]) == (
            tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
            tbm.USAGE_DECISION_FINALIZED_EVENT,
            tbm.INJECTION_RENDERED_EVENT,
        )
        rendered = tbm.parse_injection_rendered_event(events[-1])
        assert rendered.usage_decision == result.usage_decision
        assert rendered.injection == result.injection
        assert stack.replay.load_manifest(
            result.manifest.manifest_sha256
        ) == result.manifest
    finally:
        stack.close()


def test_event_first_finalization_rolls_back_events_and_session_on_projection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _stack(event_first=True)
    trusted = _event_context(stack)

    def fail_projection(*_args: object) -> bool:
        raise RuntimeError("synthetic replay projection failure")

    monkeypatch.setattr(stack.replay, "_put_artifact", fail_projection)
    try:
        with stack.sessions.bind_event_context(
            trusted
        ), stack.replay.bind_event_context(trusted):
            with pytest.raises(tbm.DurableFinalizationV3Error) as captured:
                stack.finalizer().finalize(
                    stack.context,
                    stack.scope,
                    _request(stack),
                )
        assert captured.value.code == (
            "TBM_DURABLE_FINALIZATION_EVENT_FIRST_FAILED"
        )
        current = stack.sessions.get(stack.decided.session_id)
        assert current.status == "decided"
        assert current.version == stack.decided.version + 1
        ledger = tbm.SQLiteEventLedgerV1(
            stack.connection,
            _event_access(stack),
        )
        try:
            event_types = tuple(
                event.event_type for event in ledger.read_global().events
            )
        finally:
            ledger.close()
        assert event_types[-1] == tbm.GATE_SESSION_LEASE_RENEWED_EVENT
        assert tbm.USAGE_DECISION_FINALIZED_EVENT not in event_types
        assert tbm.INJECTION_RENDERED_EVENT not in event_types
        assert stack.connection.execute(
            "SELECT COUNT(*) FROM v3_replay_artifacts"
        ).fetchone() == (0,)
    finally:
        stack.close()
