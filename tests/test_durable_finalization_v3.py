from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.policy import (
    FULL_CASE_INJECTION_TEXT_MAX_CHARS,
    INJECTION_TEXT_MAX_CHARS,
)
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
    def __init__(self, start: datetime | None = None) -> None:
        self._next = start or datetime(
            2026,
            7,
            30,
            1,
            0,
            tzinfo=timezone.utc,
        )

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
        clock=None,
    ) -> tbm.DurableFinalizationService:
        return tbm.DurableFinalizationService(
            authorization_service=self.authorization,
            session_writer=session_writer or self.sessions,  # type: ignore[arg-type]
            evidence_reader=self.evidence,
            semantic_authority=self.semantic,
            revision_source=self.source,
            policy_loader=policy_loader or (lambda: self.policy),
            replay_authority=self.replay,
            clock=clock or _Clock(datetime(2026, 7, 30, 1, 5, tzinfo=timezone.utc)),
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

    def history(self, session_id: str) -> tuple[tbm.GateSession, ...]:
        return self._sessions.history(session_id)

    def transition(self, *args, **kwargs) -> tbm.GateSession:
        raise RuntimeError("private finalization write failure")


class _ConcurrentDecisionLeaseClaim:
    def __init__(self, sessions: tbm.SQLiteGateSessionRepository) -> None:
        self._sessions = sessions
        self._claimed = False

    def get(self, session_id: str) -> tbm.GateSession:
        return self._sessions.get(session_id)

    def renew_lease(self, *args, **kwargs) -> tbm.GateSession:
        if not self._claimed:
            self._claimed = True
            self._sessions.renew_lease(*args, **kwargs)
        return self._sessions.renew_lease(*args, **kwargs)

    def history(self, session_id: str) -> tuple[tbm.GateSession, ...]:
        return self._sessions.history(session_id)

    def transition(self, *args, **kwargs) -> tbm.GateSession:
        return self._sessions.transition(*args, **kwargs)


def _stack(
    *,
    permissions: tuple[str, ...] = ("memory:retrieve",),
    connection: sqlite3.Connection | None = None,
    sessions: tbm.SQLiteGateSessionRepository | None = None,
    event_first: bool = False,
    recommended_injection: str = "summary",
    memory_type: tbm.MemoryType = "procedural",
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
    candidate = _candidate(
        "memory_finalization",
        memory_type=memory_type,
    )
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
            lambda _call: replace(
                _provider_result(prepared_result.value.system_gate_evaluation),
                recommended_injection=recommended_injection,
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


def _render_candidate(index: int, text: str) -> object:
    content = text.encode("utf-8")
    artifact = tbm.create_content_addressed_artifact(
        content,
        media_type="text/plain",
        classification="internal",
        created_at="2026-07-30T01:04:00Z",
    )
    revision = SimpleNamespace(
        content_artifact=artifact,
        memory_id=f"memory_render_{index:02d}",
        revision_id=f"memory_revision_render_{index:02d}",
        memory_type="procedural",
    )
    return SimpleNamespace(revision=revision, content=content)


def _render_attempt(
    stack: _Stack,
    recommended_injection: str,
) -> tbm.SemanticGateAttempt:
    attempt = stack.semantic.load_attempt_chain(
        stack.decided.system_gate_evaluation_id
    )[-1]
    values = {
        key: value
        for key, value in attempt.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values["recommended_injection"] = recommended_injection
    return tbm.build_semantic_gate_attempt(
        **values,
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


def test_durable_finalization_none_mode_retains_empty_injection() -> None:
    stack = _stack(recommended_injection="none")
    try:
        result = stack.finalizer().finalize(
            stack.context,
            stack.scope,
            _request(stack),
        )

        assert result.snippet == ""
        assert result.injection.memory_revision_ids == ()
        assert result.usage_decision.final_memory_revision_ids == ()
        assert stack.replay.load_injection(result.injection.artifact.artifact_id) == (
            result.injection,
            b"",
        )
    finally:
        stack.close()


@pytest.mark.parametrize(
    ("mode", "limit"),
    [
        ("summary", INJECTION_TEXT_MAX_CHARS),
        ("full", FULL_CASE_INJECTION_TEXT_MAX_CHARS),
    ],
)
def test_durable_renderer_caps_each_memory_for_requested_mode(
    mode: str,
    limit: int,
) -> None:
    stack = _stack()
    try:
        rendered = stack.finalizer()._render(
            (_render_candidate(1, "雪" * (limit + 100)),),
            _render_attempt(stack, mode),
        )

        envelope = json.loads(rendered.snippet)
        content = envelope["memory_items"][0]["content"]
        assert len(content) == limit
        assert content.endswith("...[truncated]")
        assert len(rendered.candidates) == 1
    finally:
        stack.close()


def test_durable_renderer_enforces_memory_count_and_total_prefix_budget() -> None:
    stack = _stack()
    try:
        compact_candidates = tuple(
            _render_candidate(index, f"memory {index}")
            for index in range(tbm.INJECTION_MAX_MEMORIES + 5)
        )
        compact = stack.finalizer()._render(
            compact_candidates,
            _render_attempt(stack, "summary"),
        )
        compact_items = json.loads(compact.snippet)["memory_items"]
        assert len(compact.candidates) == tbm.INJECTION_MAX_MEMORIES
        assert len(compact_items) == tbm.INJECTION_MAX_MEMORIES

        large_candidates = tuple(
            _render_candidate(index, "x" * FULL_CASE_INJECTION_TEXT_MAX_CHARS)
            for index in range(tbm.INJECTION_MAX_MEMORIES)
        )
        bounded = stack.finalizer()._render(
            large_candidates,
            _render_attempt(stack, "full"),
        )
        bounded_items = json.loads(bounded.snippet)["memory_items"]
        assert 0 < len(bounded.candidates) < tbm.INJECTION_MAX_MEMORIES
        assert len(bounded.snippet) <= tbm.INJECTION_SNIPPET_MAX_CHARS
        assert [item["memory_id"] for item in bounded_items] == [
            candidate.revision.memory_id for candidate in bounded.candidates
        ]
        next_entry = dict(bounded_items[-1])
        next_entry["memory_id"] = large_candidates[
            len(bounded.candidates)
        ].revision.memory_id
        next_entry["memory_revision_id"] = large_candidates[
            len(bounded.candidates)
        ].revision.revision_id
        trial = dict(json.loads(bounded.snippet))
        trial["memory_items"] = [*bounded_items, next_entry]
        assert (
            len(
                json.dumps(
                    trial,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            > tbm.INJECTION_SNIPPET_MAX_CHARS
        )
    finally:
        stack.close()


def test_durable_renderer_preserves_unicode_in_canonical_json() -> None:
    stack = _stack()
    try:
        rendered = stack.finalizer()._render(
            (_render_candidate(1, '雪 "quoted"\nline'),),
            _render_attempt(stack, "summary"),
        )
        envelope = json.loads(rendered.snippet)

        assert "雪" in rendered.snippet
        assert rendered.snippet == json.dumps(
            envelope,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    finally:
        stack.close()


def test_durable_finalization_never_renders_system_blocked_revision() -> None:
    stack = _stack(memory_type="episodic")
    blocked_revision_id = stack.source.candidates[
        "memory_finalization"
    ].revision.revision_id
    try:
        result = stack.finalizer().finalize(
            stack.context,
            stack.scope,
            _request(stack),
        )

        envelope = json.loads(result.snippet)
        assert envelope["memory_items"] == []
        assert result.usage_decision.system_allowed_memory_revision_ids == ()
        assert result.usage_decision.final_memory_revision_ids == ()
        assert blocked_revision_id in (
            result.usage_decision.blocked_memory_revision_ids
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


def test_durable_finalization_rejects_non_lease_stale_revision() -> None:
    stack = _stack()
    try:
        stale = replace(
            _request(stack),
            expected_session_version=stack.decided.version - 1,
        )
        with pytest.raises(tbm.DurableFinalizationV3Error) as captured:
            stack.finalizer().finalize(
                stack.context,
                stack.scope,
                stale,
            )
        assert captured.value.code == ("TBM_DURABLE_FINALIZATION_SESSION_CHANGED")
        assert stack.sessions.get(stack.decided.session_id) == stack.decided
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


def test_durable_finalization_rebuilds_exact_retained_bundle_after_claim_crash() -> (
    None
):
    stack = _stack()
    writer = _FailingFinalizedTransition(stack.sessions)
    request = _request(stack)
    try:
        with pytest.raises(tbm.DurableFinalizationRecoveryRequiredError) as captured:
            stack.finalizer(session_writer=writer).finalize(
                stack.context,
                stack.scope,
                request,
            )
        retained_usage = captured.value.usage_decision
        retained_injection = captured.value.injection
        assert retained_usage is not None
        assert retained_injection is not None
        claimed = stack.sessions.get(stack.decided.session_id)
        assert claimed.status == "decided"
        assert claimed.version == stack.decided.version + 1
        artifact_count = stack.connection.execute(
            "SELECT COUNT(*) FROM v3_replay_artifacts"
        ).fetchone()

        recovered = stack.finalizer(
            clock=_Clock(datetime(2026, 7, 30, 1, 6, tzinfo=timezone.utc))
        ).finalize(
            stack.context,
            stack.scope,
            request,
        )

        assert recovered.session.status == "finalized"
        assert recovered.usage_decision == retained_usage
        assert recovered.injection == retained_injection
        assert (
            stack.connection.execute(
                "SELECT COUNT(*) FROM v3_replay_artifacts"
            ).fetchone()
            == artifact_count
        )
    finally:
        stack.close()


def test_durable_finalization_recovers_concurrent_duplicate_lease_claim() -> None:
    stack = _stack()
    try:
        result = stack.finalizer(
            session_writer=_ConcurrentDecisionLeaseClaim(stack.sessions)
        ).finalize(
            stack.context,
            stack.scope,
            _request(stack),
        )

        assert result.session.status == "finalized"
        assert [
            session.status
            for session in stack.sessions.history(stack.decided.session_id)
        ][-3:] == ["decided", "decided", "finalized"]
    finally:
        stack.close()


def test_durable_finalization_rejects_expired_lease_only_recovery() -> None:
    stack = _stack()
    request = _request(stack)
    try:
        claimed = stack.sessions.renew_lease(
            stack.decided.session_id,
            expected_version=stack.decided.version,
            lease_seconds=1_800,
        )
        with pytest.raises(tbm.DurableFinalizationV3Error) as captured:
            stack.finalizer(clock=lambda: "2026-07-30T03:00:00Z").finalize(
                stack.context,
                stack.scope,
                request,
            )
        assert captured.value.code == ("TBM_DURABLE_FINALIZATION_SESSION_CHANGED")
        assert stack.sessions.get(claimed.session_id) == claimed
        assert stack.connection.execute(
            "SELECT COUNT(*) FROM v3_replay_artifacts"
        ).fetchone() == (0,)
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
        with (
            stack.sessions.bind_event_context(trusted),
            stack.replay.bind_event_context(trusted),
        ):
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
        assert (
            stack.replay.load_manifest(result.manifest.manifest_sha256)
            == result.manifest
        )
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
        with (
            stack.sessions.bind_event_context(trusted),
            stack.replay.bind_event_context(trusted),
        ):
            with pytest.raises(tbm.DurableFinalizationV3Error) as captured:
                stack.finalizer().finalize(
                    stack.context,
                    stack.scope,
                    _request(stack),
                )
        assert captured.value.code == ("TBM_DURABLE_FINALIZATION_EVENT_FIRST_FAILED")
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

        monkeypatch.undo()
        with (
            stack.sessions.bind_event_context(trusted),
            stack.replay.bind_event_context(trusted),
        ):
            recovered = stack.finalizer().finalize(
                stack.context,
                stack.scope,
                _request(stack),
            )
        assert recovered.session.status == "finalized"
        assert recovered.replayed is False
        ledger = tbm.SQLiteEventLedgerV1(
            stack.connection,
            _event_access(stack),
        )
        try:
            recovered_types = tuple(
                event.event_type for event in ledger.read_global().events
            )
        finally:
            ledger.close()
        assert recovered_types.count(
            tbm.GATE_SESSION_LEASE_RENEWED_EVENT
        ) == event_types.count(tbm.GATE_SESSION_LEASE_RENEWED_EVENT)
        assert recovered_types.count(tbm.USAGE_DECISION_FINALIZED_EVENT) == 1
        assert recovered_types.count(tbm.INJECTION_RENDERED_EVENT) == 1
    finally:
        stack.close()
