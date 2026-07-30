from __future__ import annotations

from dataclasses import dataclass, replace
import sqlite3

import pytest

import trace_backed_memory as tbm
from tests.test_artifact_service_v3 import (
    _context as _service_context,
    _registry,
)
from tests.test_durable_execution_v3 import (
    DIGEST_A,
    DIGEST_B,
    EVALUATOR,
    EVALUATOR_CONTEXT,
    _authenticate_evaluator,
)
from tests.test_durable_finalization_v3 import _Clock
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
from trace_backed_memory.durable_agent_v3 import (
    AuthenticatedDurableAgentMemory,
    DurableAgentCancelRequest,
    DurableReplayExportRequest,
    DurableAgentV3Error,
)
from trace_backed_memory.durable_composition_v3 import (
    DurableCompositionV3Error,
    DurableServiceBundle,
)


@dataclass
class _AgentStack:
    connection: sqlite3.Connection
    decisions: tbm.SQLiteAuthorizationV3Repository
    context: tbm.AuthenticatedServiceContext
    sessions: tbm.SQLiteGateSessionRepository
    evidence: tbm.SQLiteGateEvidenceV3Repository
    semantic: tbm.SQLiteSemanticGateArtifactV3Repository
    replay: tbm.SQLiteReplayV3Repository
    outbox: tbm.SQLiteCompletionOutboxV3Repository
    preparation: tbm.DurableRetrievalPreparationService
    semantic_session: tbm.AuthenticatedSemanticGateSessionService
    finalization: tbm.DurableFinalizationService
    execution: tbm.DurableExecutionService
    authorization: tbm.AuthenticatedRetrievalService

    def agent(self) -> AuthenticatedDurableAgentMemory:
        services = DurableServiceBundle.from_services(
            preparation_service=self.preparation,
            semantic_service=self.semantic_session,
            finalization_service=self.finalization,
            execution_service=self.execution,
        )
        return AuthenticatedDurableAgentMemory(
            service_bundle=services,
        )

    def close(self) -> None:
        self.outbox.close()
        self.replay.close()
        self.semantic.close()
        self.evidence.close()
        self.sessions.close()
        self.decisions.close()
        self.connection.close()


def _stack(
    *,
    permissions: tuple[str, ...] = (
        "memory:retrieve",
        "gate_session:transition",
    ),
    check_same_thread: bool = True,
) -> _AgentStack:
    registry = _registry(permissions=permissions)
    context = _service_context(registry)
    authorization, decisions = _retrieval_authorization(
        registry,
        check_same_thread=check_same_thread,
    )
    connection = sqlite3.connect(
        ":memory:",
        check_same_thread=check_same_thread,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-session.sql",
        "schemas/sqlite-v3-gate-evidence.sql",
        "schemas/sqlite-v3-semantic-gate.sql",
        "schemas/sqlite-v3-semantic-gate-artifacts.sql",
        "schemas/sqlite-v3-replay.sql",
        "schemas/sqlite-v3-outcome.sql",
        "schemas/sqlite-v3-completion-outbox.sql",
    ):
        connection.executescript(
            tbm.read_packaged_resource(resource).decode("utf-8")
        )
    evidence = tbm.SQLiteGateEvidenceV3Repository(connection)
    semantic = tbm.SQLiteSemanticGateArtifactV3Repository(connection)
    replay = tbm.SQLiteReplayV3Repository(connection)
    outbox = tbm.SQLiteCompletionOutboxV3Repository(
        connection,
        clock=_Clock(),
    )
    sessions = outbox.gate_sessions
    policy = _policy()
    candidate = _candidate("memory_durable_agent")
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
        session_id_factory=lambda: "gate_session_durable_agent_001",
        evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
    )
    preparation = tbm.DurableRetrievalPreparationService(
        gate_session_service=gate,
        retrieval_service=retrieval,
        evidence_authority=evidence,
    )
    semantic_gate = _semantic_service(
        semantic,
        evidence,
        iter(
            (
                "2026-07-30T01:03:00Z",
                "2026-07-30T01:03:00.125Z",
            )
        ),
    )
    semantic_session = tbm.AuthenticatedSemanticGateSessionService(
        semantic_gate_service=semantic_gate,
        session_writer=sessions,
    )
    finalization = tbm.DurableFinalizationService(
        authorization_service=authorization,
        session_writer=sessions,
        evidence_reader=evidence,
        semantic_authority=semantic,
        revision_source=source,
        policy_loader=lambda: policy,
        replay_authority=replay,
        clock=lambda: "2026-07-30T01:05:00Z",
    )
    execution = tbm.DurableExecutionService(
        authorization_service=authorization,
        session_writer=sessions,
        finalization_reader=finalization,
        completion_authority=outbox,
        evaluator_authenticator=_authenticate_evaluator,
        clock=lambda: "2026-07-30T01:10:00Z",
    )
    return _AgentStack(
        connection,
        decisions,
        context,
        sessions,
        evidence,
        semantic,
        replay,
        outbox,
        preparation,
        semantic_session,
        finalization,
        execution,
        authorization,
    )


def _completion(session: tbm.GateSession) -> tbm.GateCompletionRequest:
    return tbm.GateCompletionRequest(
        session_id=session.session_id,
        expected_version=session.version,
        result="pass",
        evaluator_id=EVALUATOR.evaluator_id,
        evaluator_version=EVALUATOR.evaluator_version,
        evidence_artifact_sha256s=(DIGEST_B,),
        output_sha256=DIGEST_A,
        latency_ms=150,
        cost_usd=0.15,
    )


def _finalize(stack: _AgentStack) -> tbm.DurableFinalizationResult:
    prepared = stack.agent().prepare(
        stack.context,
        replace(_durable_request(), expires_in_seconds=3_600),
    )
    decided = stack.agent().decide(
        stack.context,
        _provider_context(),
        _semantic_request(prepared.session),
        lambda _call: _provider_result(
            prepared.value.system_gate_evaluation
        ),
    )
    return stack.agent().finalize(
        stack.context,
        tbm.DurableFinalizationRequest(
            decided.session.session_id,
            decided.session.version,
        ),
    )


def test_durable_agent_composes_exact_cross_instance_lifecycle() -> None:
    stack = _stack()
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )
        assert prepared.session.status == "prepared"

        decided = stack.agent().decide(
            stack.context,
            _provider_context(),
            _semantic_request(prepared.session),
            lambda _call: _provider_result(
                prepared.value.system_gate_evaluation
            ),
        )
        assert decided.session.status == "decided"

        finalized = stack.agent().finalize(
            stack.context,
            tbm.DurableFinalizationRequest(
                decided.session.session_id,
                decided.session.version,
            ),
        )
        assert finalized.session.status == "finalized"

        started = stack.agent().start(
            stack.context,
            tbm.DurableExecutionStartRequest(
                finalized.session.session_id,
                finalized.session.version,
            ),
        )
        assert started.session.status == "executing"
        assert started.snippet == finalized.snippet
        assert started.transition_authorization_event_id != (
            prepared.scope.authorization_event_id
        )

        completed = stack.agent().complete(
            stack.context,
            EVALUATOR_CONTEXT,
            _completion(started.session),
        )
        assert completed.session.status == "completed"
        assert completed.outcome.usage_decision_id == (
            finalized.usage_decision.usage_decision_id
        )
        assert stack.agent().get_session(
            stack.context,
            completed.session.session_id,
        ) == completed.session
        assert stack.outbox.get_event(completed.event.event_id) == completed.event
        replayed = stack.agent().complete(
            stack.context,
            EVALUATOR_CONTEXT,
            _completion(started.session),
        )
        assert replayed.session == completed.session
        assert replayed.replayed is True

        decisions = stack.decisions.list_decisions(
            stack.authorization._registry_provider().authorization_policy.policy_sha256,  # noqa: SLF001
        )
        permissions = tuple(decision.permission for decision in decisions)
        assert permissions.count("memory:retrieve") == 1
        assert permissions.count("gate_session:transition") == 5
    finally:
        stack.close()


def test_durable_agent_authorizes_session_bound_replay_export() -> None:
    stack = _stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    try:
        finalized = _finalize(stack)
        result = stack.agent().export_replay_bundle(
            stack.context,
            DurableReplayExportRequest(
                finalized.session.session_id,
                finalized.session.version,
                ("internal",),
            ),
        )

        assert result.session == finalized.session
        assert result.bundle.manifest == finalized.manifest
        assert result.bundle.injection == finalized.injection
        assert tbm.verify_replay_bundle_export(result.bundle)
        assert tuple(
            artifact.component_name for artifact in result.bundle.artifacts
        ) == tbm.REPLAY_COMPONENT_NAMES
        assert (
            result.retrieval_authorization_event_id
            == finalized.usage_decision.authorization_event_id
        )

        decisions = stack.decisions.list_decisions(
            stack.authorization._registry_provider().authorization_policy.policy_sha256,  # noqa: SLF001
        )
        read_decisions = tuple(
            decision
            for decision in decisions
            if decision.permission == "artifact:read"
        )
        assert len(read_decisions) == 1
        assert read_decisions[0].allowed is True
        assert (
            result.read_authorization_event_id
            == read_decisions[0].authorization_event_id
        )
    finally:
        stack.close()


def test_durable_agent_denies_replay_export_before_replay_storage_reads() -> None:
    stack = _stack()
    statements: list[str] = []
    try:
        finalized = _finalize(stack)
        stack.connection.set_trace_callback(statements.append)

        with pytest.raises(tbm.AuthorizationDeniedError):
            stack.agent().export_replay_bundle(
                stack.context,
                DurableReplayExportRequest(
                    finalized.session.session_id,
                    finalized.session.version,
                    ("internal",),
                ),
            )

        assert not any(
            "v3_replay_" in statement.lower()
            for statement in statements
        )
        decisions = stack.decisions.list_decisions(
            stack.authorization._registry_provider().authorization_policy.policy_sha256,  # noqa: SLF001
        )
        read_decisions = tuple(
            decision
            for decision in decisions
            if decision.permission == "artifact:read"
        )
        assert len(read_decisions) == 1
        assert read_decisions[0].allowed is False
    finally:
        stack.close()


def test_durable_agent_rejects_stale_replay_version_before_read_authorization():
    stack = _stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    try:
        finalized = _finalize(stack)
        policy_sha256 = (
            stack.authorization._registry_provider().authorization_policy.policy_sha256  # noqa: SLF001
        )
        before = stack.decisions.list_decisions(policy_sha256)

        with pytest.raises(DurableAgentV3Error) as raised:
            stack.agent().export_replay_bundle(
                stack.context,
                DurableReplayExportRequest(
                    finalized.session.session_id,
                    finalized.session.version - 1,
                    ("internal",),
                ),
            )

        assert raised.value.code == "TBM_DURABLE_AGENT_SESSION_CHANGED"
        assert stack.decisions.list_decisions(policy_sha256) == before
    finally:
        stack.close()


def test_durable_agent_replay_classification_fails_before_content_read() -> None:
    stack = _stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    statements: list[str] = []
    try:
        finalized = _finalize(stack)
        stack.connection.set_trace_callback(statements.append)

        with pytest.raises(DurableAgentV3Error) as raised:
            stack.agent().export_replay_bundle(
                stack.context,
                DurableReplayExportRequest(
                    finalized.session.session_id,
                    finalized.session.version,
                    ("public",),
                ),
            )

        assert raised.value.code == "TBM_REPLAY_EXPORT_FORBIDDEN"
        normalized = " ".join(
            " ".join(statement.lower().split())
            for statement in statements
        )
        assert "redaction_policy_id, content from v3_replay_artifacts" not in (
            normalized
        )
    finally:
        stack.close()


def test_durable_agent_maps_replay_storage_failures_to_stable_error() -> None:
    stack = _stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    try:
        finalized = _finalize(stack)
        agent = stack.agent()

        class FailingReader:
            def load_manifest_for_session(
                self,
                session_id: str,
                decision_id: str,
                usage_decision_id: str,
                injection_artifact_id: str,
            ):
                return stack.replay.load_manifest_for_session(
                    session_id,
                    decision_id,
                    usage_decision_id,
                    injection_artifact_id,
                )

            def load_manifest(self, manifest_sha256: str):
                return stack.replay.load_manifest(manifest_sha256)

            def load_artifact_descriptor(self, artifact_id: str):
                raise RuntimeError(f"storage unavailable for {artifact_id}")

            def load_artifact(self, artifact_id: str):
                return stack.replay.load_artifact(artifact_id)

            def load_injection(self, artifact_id: str):
                return stack.replay.load_injection(artifact_id)

        agent._replay_export_reader = FailingReader()  # type: ignore[assignment]  # noqa: SLF001

        with pytest.raises(DurableAgentV3Error) as raised:
            agent.export_replay_bundle(
                stack.context,
                DurableReplayExportRequest(
                    finalized.session.session_id,
                    finalized.session.version,
                    ("internal",),
                ),
            )

        assert raised.value.code == "TBM_DURABLE_AGENT_REPLAY_UNAVAILABLE"
        assert "artifact_sha256_" not in str(raised.value)
    finally:
        stack.close()


def test_durable_agent_rechecks_registry_after_manifest_resolution() -> None:
    stack = _stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    try:
        finalized = _finalize(stack)
        agent = stack.agent()
        original = stack.authorization._registry_provider()  # noqa: SLF001
        rotated = replace(
            original,
            registry_version="registry_rotated_during_replay_export",
            authorization_policy=replace(
                original.authorization_policy,
                policy_version="policy_rotated_during_replay_export",
            ),
        )

        class RotatingReader:
            def load_manifest_for_session(
                self,
                session_id: str,
                decision_id: str,
                usage_decision_id: str,
                injection_artifact_id: str,
            ):
                manifest = stack.replay.load_manifest_for_session(
                    session_id,
                    decision_id,
                    usage_decision_id,
                    injection_artifact_id,
                )
                stack.authorization._registry_provider = lambda: rotated  # noqa: SLF001
                return manifest

            def load_manifest(self, manifest_sha256: str):
                return stack.replay.load_manifest(manifest_sha256)

            def load_injection(self, artifact_id: str):
                return stack.replay.load_injection(artifact_id)

            def load_artifact_descriptor(self, artifact_id: str):
                return stack.replay.load_artifact_descriptor(artifact_id)

            def load_artifact(self, artifact_id: str):
                return stack.replay.load_artifact(artifact_id)

        agent._replay_export_reader = RotatingReader()  # type: ignore[assignment]  # noqa: SLF001

        with pytest.raises(tbm.AuthenticatedServiceV3Error):
            agent.export_replay_bundle(
                stack.context,
                DurableReplayExportRequest(
                    finalized.session.session_id,
                    finalized.session.version,
                    ("internal",),
                ),
            )
    finally:
        stack.close()


def test_durable_agent_authenticates_owner_before_semantic_provider() -> None:
    stack = _stack()
    called = False
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )

        def call_provider(_call):
            nonlocal called
            called = True
            return _provider_result(prepared.value.system_gate_evaluation)

        with pytest.raises(tbm.AuthenticatedServiceV3Error) as raised:
            stack.agent().decide(
                replace(
                    stack.context,
                    repository_reference="other/repository",
                ),
                _provider_context(),
                _semantic_request(prepared.session),
                call_provider,
            )

        assert raised.value.code == "TBM_SERVICE_ENTITY_CONTEXT_REJECTED"
        assert called is False
        assert stack.sessions.get(prepared.session.session_id) == prepared.session
    finally:
        stack.close()


def test_durable_agent_cancel_is_authorized_and_exactly_replayed() -> None:
    stack = _stack()
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )
        request = DurableAgentCancelRequest(
            session_id=prepared.session.session_id,
            expected_session_version=prepared.session.version,
            reason="caller canceled before provider invocation",
        )

        canceled = stack.agent().cancel(stack.context, request)
        replayed = stack.agent().cancel(stack.context, request)

        assert canceled.session.status == "canceled"
        assert canceled.replayed is False
        assert replayed.session == canceled.session
        assert replayed.replayed is True
    finally:
        stack.close()


def test_durable_agent_resume_and_abandon_are_exactly_replayed() -> None:
    stack = _stack()
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )
        decided = stack.agent().decide(
            stack.context,
            _provider_context(),
            _semantic_request(prepared.session),
            lambda _call: _provider_result(
                prepared.value.system_gate_evaluation
            ),
        )
        finalized = stack.agent().finalize(
            stack.context,
            tbm.DurableFinalizationRequest(
                decided.session.session_id,
                decided.session.version,
            ),
        )
        started = stack.agent().start(
            stack.context,
            tbm.DurableExecutionStartRequest(
                finalized.session.session_id,
                finalized.session.version,
            ),
        )
        resumed = stack.agent().resume(
            stack.context,
            tbm.DurableExecutionResumeRequest(
                started.session.session_id,
                started.session.version,
                lease_seconds=2_700,
            ),
        )
        abandoned = stack.agent().abandon(
            stack.context,
            tbm.DurableExecutionAbandonRequest(
                resumed.session.session_id,
                resumed.session.version,
                "caller stopped execution",
            ),
        )
        replayed = stack.agent().abandon(
            stack.context,
            tbm.DurableExecutionAbandonRequest(
                resumed.session.session_id,
                resumed.session.version,
                "caller stopped execution",
            ),
        )

        assert resumed.session.status == "executing"
        assert resumed.replayed is True
        assert abandoned.session.status == "abandoned"
        assert replayed.session == abandoned.session
        assert replayed.replayed is True
    finally:
        stack.close()


def test_durable_agent_rejects_transition_denial_before_provider_or_finalize():
    stack = _stack(permissions=("memory:retrieve",))
    called = False
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )

        def provider(_call):
            nonlocal called
            called = True
            return _provider_result(prepared.value.system_gate_evaluation)

        with pytest.raises(tbm.AuthorizationDeniedError):
            stack.agent().decide(
                stack.context,
                _provider_context(),
                _semantic_request(prepared.session),
                provider,
            )
        assert called is False
        assert stack.sessions.get(prepared.session.session_id) == prepared.session

        decided = stack.semantic_session.decide(
            _provider_context(),
            _semantic_request(prepared.session),
            provider,
        )
        assert called is True
        with pytest.raises(tbm.AuthorizationDeniedError):
            stack.agent().finalize(
                stack.context,
                tbm.DurableFinalizationRequest(
                    decided.session.session_id,
                    decided.session.version,
                ),
            )
        assert stack.sessions.get(decided.session.session_id) == decided.session
    finally:
        stack.close()


def test_durable_agent_fails_closed_when_registry_rotates_during_provider():
    stack = _stack()
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )
        original = stack.authorization._registry_provider()  # noqa: SLF001
        rotated = replace(
            original,
            registry_version="registry_rotated_during_provider",
            authorization_policy=replace(
                original.authorization_policy,
                policy_version="policy_rotated_during_provider",
            ),
        )

        def provider(_call):
            stack.authorization._registry_provider = lambda: rotated  # noqa: SLF001
            return _provider_result(prepared.value.system_gate_evaluation)

        with pytest.raises(
            (tbm.AuthenticatedServiceV3Error, tbm.DurableSemanticGateV3Error)
        ):
            stack.agent().decide(
                stack.context,
                _provider_context(),
                _semantic_request(prepared.session),
                provider,
            )

        retained = stack.sessions.get(prepared.session.session_id)
        assert retained.status == "awaiting_decision"
    finally:
        stack.close()


def test_durable_agent_cancel_rejects_stale_and_non_cancelable_state():
    stack = _stack()
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )
        with pytest.raises(DurableAgentV3Error) as stale:
            stack.agent().cancel(
                stack.context,
                DurableAgentCancelRequest(
                    prepared.session.session_id,
                    prepared.session.version - 1,
                    "stale caller cancellation",
                ),
            )
        assert stale.value.code == "TBM_DURABLE_AGENT_SESSION_CHANGED"
        assert stack.sessions.get(prepared.session.session_id) == prepared.session

        decided = stack.agent().decide(
            stack.context,
            _provider_context(),
            _semantic_request(prepared.session),
            lambda _call: _provider_result(
                prepared.value.system_gate_evaluation
            ),
        )
        with pytest.raises(DurableAgentV3Error) as invalid_status:
            stack.agent().cancel(
                stack.context,
                DurableAgentCancelRequest(
                    decided.session.session_id,
                    decided.session.version,
                    "too late to cancel",
                ),
            )
        assert (
            invalid_status.value.code
            == "TBM_DURABLE_AGENT_CANCEL_STATUS_INVALID"
        )
        assert stack.sessions.get(decided.session.session_id) == decided.session
    finally:
        stack.close()


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    (
        ("failure", "TBM_DURABLE_AGENT_CANCEL_FAILED"),
        ("receipt", "TBM_DURABLE_AGENT_CANCEL_RECEIPT_INVALID"),
    ),
)
def test_durable_agent_cancel_rejects_transition_failure_or_bad_receipt(
    mode: str,
    expected_code: str,
) -> None:
    stack = _stack()
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )
        agent = stack.agent()

        class InvalidCancelAuthority:
            def get(self, session_id: str) -> tbm.GateSession:
                return stack.sessions.get(session_id)

            def transition(self, *args, **kwargs) -> tbm.GateSession:
                if mode == "failure":
                    raise RuntimeError("private transition failure")
                return stack.sessions.get(prepared.session.session_id)

        agent._session_authority = InvalidCancelAuthority()  # type: ignore[assignment]  # noqa: SLF001
        with pytest.raises(DurableAgentV3Error) as raised:
            agent.cancel(
                stack.context,
                DurableAgentCancelRequest(
                    prepared.session.session_id,
                    prepared.session.version,
                    "caller requested cancellation",
                ),
            )

        assert raised.value.code == expected_code
        assert stack.sessions.get(prepared.session.session_id) == prepared.session
    finally:
        stack.close()


def test_durable_agent_rejects_missing_evidence_before_cancel_authorization():
    stack = _stack()
    try:
        prepared = stack.agent().prepare(
            stack.context,
            replace(_durable_request(), expires_in_seconds=3_600),
        )
        agent = stack.agent()

        class MissingEvidence:
            def load_snapshot(self, snapshot_id: str) -> tbm.RetrievalSnapshot:
                raise KeyError(snapshot_id)

        agent._evidence_reader = MissingEvidence()  # type: ignore[assignment]  # noqa: SLF001
        before = stack.decisions.list_decisions(
            stack.authorization._registry_provider().authorization_policy.policy_sha256,  # noqa: SLF001
        )
        with pytest.raises(DurableAgentV3Error) as raised:
            agent.cancel(
                stack.context,
                DurableAgentCancelRequest(
                    prepared.session.session_id,
                    prepared.session.version,
                    "cancel after evidence loss",
                ),
            )
        after = stack.decisions.list_decisions(
            stack.authorization._registry_provider().authorization_policy.policy_sha256,  # noqa: SLF001
        )

        assert raised.value.code == "TBM_DURABLE_AGENT_EVIDENCE_UNAVAILABLE"
        assert after == before
        assert stack.sessions.get(prepared.session.session_id) == prepared.session
    finally:
        stack.close()


def test_durable_agent_rejects_malformed_inputs_before_authorization() -> None:
    stack = _stack()
    try:
        agent = stack.agent()
        calls = (
            lambda: agent.prepare(stack.context, object()),  # type: ignore[arg-type]
            lambda: agent.decide(
                stack.context,
                _provider_context(),
                object(),  # type: ignore[arg-type]
                lambda _call: pytest.fail("provider must not be called"),
            ),
            lambda: agent.finalize(stack.context, object()),  # type: ignore[arg-type]
            lambda: agent.start(stack.context, object()),  # type: ignore[arg-type]
            lambda: agent.resume(stack.context, object()),  # type: ignore[arg-type]
            lambda: agent.abandon(stack.context, object()),  # type: ignore[arg-type]
            lambda: agent.complete(
                stack.context,
                EVALUATOR_CONTEXT,
                object(),  # type: ignore[arg-type]
            ),
            lambda: agent.cancel(stack.context, object()),  # type: ignore[arg-type]
            lambda: agent.get_session(stack.context, ""),
            lambda: agent.export_replay_bundle(
                stack.context,
                object(),  # type: ignore[arg-type]
            ),
        )
        policy_sha256 = (
            stack.authorization._registry_provider().authorization_policy.policy_sha256  # noqa: SLF001
        )
        before = stack.decisions.list_decisions(policy_sha256)

        for call in calls:
            with pytest.raises(DurableAgentV3Error) as raised:
                call()
            assert raised.value.code == "TBM_DURABLE_AGENT_INVALID"

        assert stack.decisions.list_decisions(policy_sha256) == before
    finally:
        stack.close()


def test_durable_agent_rejects_mismatched_service_graph() -> None:
    stack = _stack()
    other = _stack()
    try:
        with pytest.raises(DurableCompositionV3Error) as raised:
            DurableServiceBundle.from_services(
                preparation_service=stack.preparation,
                semantic_service=stack.semantic_session,
                finalization_service=stack.finalization,
                execution_service=other.execution,
            )
        assert raised.value.code == "TBM_DURABLE_AUTHORITY_GRAPH_MISMATCH"
    finally:
        other.close()
        stack.close()


def test_durable_agent_rejects_invalid_cancel_request() -> None:
    with pytest.raises(DurableAgentV3Error) as raised:
        DurableAgentCancelRequest(
            session_id="gate_session",
            expected_session_version=0,
            reason="cancel",
        )
    assert raised.value.code == "TBM_DURABLE_AGENT_INVALID"


@pytest.mark.parametrize(
    "replay_request",
    (
        DurableReplayExportRequest(
            "gate_session",
            1,
            ("internal",),
        ),
        None,
    ),
)
def test_durable_agent_replay_request_contract_is_exact(
    replay_request: DurableReplayExportRequest | None,
) -> None:
    if replay_request is not None:
        assert (
            replay_request.contract_version
            == tbm.DURABLE_REPLAY_EXPORT_CONTRACT_VERSION
        )
        return
    with pytest.raises(DurableAgentV3Error) as raised:
        DurableReplayExportRequest(
            "gate_session",
            0,
            (),
        )
    assert raised.value.code == "TBM_DURABLE_AGENT_INVALID"


def test_durable_agent_replay_request_rejects_unhashable_classification() -> None:
    with pytest.raises(DurableAgentV3Error) as raised:
        DurableReplayExportRequest(
            "gate_session",
            1,
            ([],),  # type: ignore[arg-type,list-item]
        )
    assert raised.value.code == "TBM_DURABLE_AGENT_INVALID"


def test_durable_agent_public_exports_are_available() -> None:
    assert (
        tbm.AuthenticatedDurableAgentMemory
        is AuthenticatedDurableAgentMemory
    )
    assert tbm.DurableAgentCancelRequest is DurableAgentCancelRequest
    assert tbm.DurableReplayExportRequest is DurableReplayExportRequest
    assert "DurableReplayExportResult" in tbm.__all__
    assert (
        tbm.DURABLE_REPLAY_EXPORT_CONTRACT_VERSION
        == "tbm.durable-replay-export.v3"
    )
    assert "AuthenticatedDurableAgentMemory" in tbm.__all__
