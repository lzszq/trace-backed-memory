from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3Repository,
)
from trace_backed_memory.sqlite_gate_evidence_v3 import (
    SQLiteGateEvidenceV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]
PROMPT = b"Review the exact bounded candidate set."
RESPONSE = b'{"decision":"allow","reason":"applicable"}'


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 27, 7, 59, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(seconds=1)
        return value.isoformat().replace("+00:00", "Z")

    def set_next(self, value: datetime) -> None:
        self._next = value


@dataclass
class _SQLiteStack:
    connection: sqlite3.Connection
    clock: _Clock
    sessions: tbm.SQLiteGateSessionRepository
    evidence: SQLiteGateEvidenceV3Repository
    authority: tbm.SQLiteSemanticGateArtifactV3Repository
    semantic: tbm.AuthenticatedSemanticGateService
    service: tbm.AuthenticatedSemanticGateSessionService
    evaluation: tbm.SystemGateEvaluation
    prepared: tbm.GateSession

    def close(self) -> None:
        self.authority.close()
        self.evidence.close()
        self.sessions.close()
        self.connection.close()


def _evidence() -> tuple[tbm.RetrievalSnapshot, tbm.SystemGateEvaluation]:
    return (
        tbm.loads_retrieval_snapshot(
            (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
        ),
        tbm.loads_system_gate_evaluation(
            (
                ROOT / "examples" / "system_gate_evaluation_v3.example.json"
            ).read_bytes()
        ),
    )


def _context() -> tbm.AuthenticatedSemanticProviderContext:
    return tbm.AuthenticatedSemanticProviderContext(
        provider_id="provider_openai",
        authenticator_id="authenticator_oidc",
        credential_id="credential_prod_01",
    )


def _semantic_service(
    authority: object,
    evidence: object,
    times: Iterator[str],
) -> tbm.AuthenticatedSemanticGateService:
    return tbm.AuthenticatedSemanticGateService(
        provider=tbm.TrustedSemanticProvider(
            provider_id="provider_openai",
            authenticator_id="authenticator_oidc",
            credential_id="credential_prod_01",
            model_id="model_gate",
            model_version="2026-07-01",
            endpoint_id="endpoint_primary",
        ),
        configuration=tbm.SemanticGateServiceConfiguration(
            prompt_template_id="semantic_gate_default",
            prompt_template_version="v1",
            generation_config_sha256="sha256:" + ("3" * 64),
            response_media_type="application/json",
        ),
        evidence_reader=evidence,  # type: ignore[arg-type]
        authority=authority,  # type: ignore[arg-type]
        clock=lambda: next(times),
    )


def _provider_result(
    evaluation: tbm.SystemGateEvaluation,
    *,
    decision_id: str = "decision_allow_exact",
) -> tbm.SemanticProviderResult:
    allowed = tuple(
        sorted(
            decision.memory_revision_id
            for decision in evaluation.decisions
            if decision.outcome == "allowed"
        )
    )
    blocked = tuple(
        sorted(
            decision.memory_revision_id
            for decision in evaluation.decisions
            if decision.outcome == "blocked"
        )
    )
    return tbm.SemanticProviderResult(
        response=RESPONSE,
        provider_request_id="provider_request_01",
        decision_id=decision_id,
        final_allowed_revision_ids=allowed,
        final_blocked_revision_ids=blocked,
        reason="The candidate remains directly applicable.",
        risk="low",
        recommended_injection="summary",
        input_tokens=12,
        output_tokens=8,
    )


def _request(
    prepared: tbm.GateSession,
    *,
    expected_session_version: int | None = None,
    expected_previous_attempt_id: str | None = None,
    prompt: bytes = PROMPT,
) -> tbm.DurableSemanticGateRequest:
    return tbm.DurableSemanticGateRequest(
        session_id=prepared.session_id,
        expected_session_version=(
            prepared.version
            if expected_session_version is None
            else expected_session_version
        ),
        prompt=prompt,
        expected_previous_attempt_id=expected_previous_attempt_id,
    )


def _sqlite_stack(times: Iterator[str]) -> _SQLiteStack:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-session.sql",
        "schemas/sqlite-v3-gate-evidence.sql",
        "schemas/sqlite-v3-semantic-gate.sql",
        "schemas/sqlite-v3-semantic-gate-artifacts.sql",
    ):
        connection.executescript(
            tbm.read_packaged_resource(resource).decode("utf-8")
        )
    clock = _Clock()
    sessions = tbm.SQLiteGateSessionRepository(
        connection,
        clock=clock,
        allow_direct_completion=False,
    )
    evidence = SQLiteGateEvidenceV3Repository(connection)
    authority = tbm.SQLiteSemanticGateArtifactV3Repository(connection)
    snapshot, evaluation = _evidence()
    evidence.store_bundle(snapshot, evaluation)
    created = sessions.create_or_get(
        session_id=snapshot.session_id,
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id=snapshot.trace_id,
        run_id=snapshot.run_id,
        request_fingerprint="sha256:" + ("a" * 64),
        idempotency_key="durable_semantic_gate_001",
        expires_in_seconds=3600,
    ).session
    prepared = sessions.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=1800,
        retrieval_snapshot_id=snapshot.snapshot_id,
        system_gate_evaluation_id=evaluation.evaluation_id,
    )
    semantic = _semantic_service(authority, evidence, times)
    service = tbm.AuthenticatedSemanticGateSessionService(
        semantic_gate_service=semantic,
        session_writer=sessions,
    )
    return _SQLiteStack(
        connection,
        clock,
        sessions,
        evidence,
        authority,
        semantic,
        service,
        evaluation,
        prepared,
    )


def test_durable_semantic_gate_decides_and_exactly_replays_without_provider() -> None:
    stack = _sqlite_stack(
        iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:00.125Z"))
    )
    calls: list[tbm.SemanticProviderCall] = []
    request = _request(stack.prepared)
    try:
        result = stack.service.decide(
            _context(),
            request,
            lambda call: calls.append(call)
            or _provider_result(stack.evaluation),
        )

        assert result.replayed is False
        assert result.session.status == "decided"
        assert result.session.version == stack.prepared.version + 3
        assert result.session.semantic_gate_attempt_ids == (
            result.semantic_gate.attempt.attempt_id,
        )
        assert (
            result.session.decision_id
            == result.semantic_gate.attempt.decision_id
        )
        assert [
            session.status
            for session in stack.sessions.history(stack.prepared.session_id)
        ] == [
            "created",
            "prepared",
            "awaiting_decision",
            "awaiting_decision",
            "decided",
        ]

        replay = stack.service.decide(
            _context(),
            request,
            lambda _call: pytest.fail("exact replay called the provider"),
        )
        assert replay.replayed is True
        assert replay.session == result.session
        assert replay.semantic_gate == result.semantic_gate
        assert len(calls) == 1
    finally:
        stack.close()


def test_failed_attempt_stays_awaiting_and_explicit_retry_decides() -> None:
    stack = _sqlite_stack(
        iter(
            (
                "2026-07-27T08:03:00Z",
                "2026-07-27T08:03:01Z",
                "2026-07-27T08:04:00Z",
                "2026-07-27T08:04:01Z",
            )
        )
    )
    try:
        with pytest.raises(
            tbm.DurableSemanticGateProviderFailedError
        ) as failed:
            stack.service.decide(
                _context(),
                _request(stack.prepared),
                lambda _call: (_ for _ in ()).throw(
                    tbm.SemanticProviderCallError(
                        "provider_timeout",
                        provider_request_id="provider_request_timeout",
                    )
                ),
            )

        assert failed.value.session.status == "awaiting_decision"
        assert failed.value.attempt.status == "failed"
        assert failed.value.attempt.error_code == "provider_timeout"
        assert failed.value.session.semantic_gate_attempt_ids == ()
        assert "provider_request_timeout" not in str(failed.value)

        result = stack.service.decide(
            _context(),
            _request(
                stack.prepared,
                expected_session_version=failed.value.session.version,
                expected_previous_attempt_id=failed.value.attempt.attempt_id,
            ),
            lambda _call: _provider_result(stack.evaluation),
        )
        assert result.session.status == "decided"
        assert result.session.semantic_gate_attempt_ids == (
            failed.value.attempt.attempt_id,
            result.semantic_gate.attempt.attempt_id,
        )
        assert result.semantic_gate.attempt.sequence == 2
        first_artifacts = stack.authority.load_attempt_with_artifacts(
            failed.value.attempt.attempt_id
        )
        assert (
            first_artifacts.prompt.binding.artifact
            == result.semantic_gate.artifacts.prompt.binding.artifact
        )
        assert stack.connection.execute(
            "SELECT count(*) FROM v3_semantic_gate_artifacts"
        ).fetchone() == (2,)
    finally:
        stack.close()


def test_retained_success_recovers_awaiting_session_without_provider() -> None:
    stack = _sqlite_stack(
        iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z"))
    )
    try:
        awaiting = stack.sessions.transition(
            stack.prepared.session_id,
            "awaiting_decision",
            expected_version=stack.prepared.version,
        )
        retained = stack.semantic.invoke(
            _context(),
            tbm.SemanticGateInvocationRequest(
                stack.evaluation.evaluation_id,
                PROMPT,
            ),
            lambda _call: _provider_result(stack.evaluation),
        )

        recovered = stack.service.decide(
            _context(),
            _request(
                stack.prepared,
                expected_session_version=awaiting.version,
            ),
            lambda _call: pytest.fail("recovery called the provider"),
        )
        assert recovered.replayed is True
        assert recovered.semantic_gate == retained
        assert recovered.session.status == "decided"
        assert recovered.session.semantic_gate_attempt_ids == (
            retained.attempt.attempt_id,
        )
    finally:
        stack.close()


class _RejectDecided:
    def __init__(self, delegate: tbm.SQLiteGateSessionRepository) -> None:
        self._delegate = delegate

    def get(self, session_id: str) -> tbm.GateSession:
        return self._delegate.get(session_id)

    def renew_lease(
        self,
        session_id: str,
        *,
        expected_version: int,
        lease_seconds: int,
    ) -> tbm.GateSession:
        return self._delegate.renew_lease(
            session_id,
            expected_version=expected_version,
            lease_seconds=lease_seconds,
        )

    def transition(
        self,
        session_id: str,
        target_status: str,
        **kwargs: object,
    ) -> tbm.GateSession:
        if target_status == "decided":
            raise RuntimeError("private transition detail")
        return self._delegate.transition(  # type: ignore[arg-type]
            session_id,
            target_status,
            **kwargs,
        )


class _RejectArtifactReadback:
    def __init__(
        self,
        delegate: tbm.SQLiteSemanticGateArtifactV3Repository,
    ) -> None:
        self._delegate = delegate

    def load_attempt_chain(
        self,
        evaluation_id: str,
    ) -> tuple[tbm.SemanticGateAttempt, ...]:
        return self._delegate.load_attempt_chain(evaluation_id)

    def store_attempt_with_artifacts(
        self,
        attempt: tbm.SemanticGateAttempt,
        prompt: tbm.StoredSemanticGateArtifact,
        response: tbm.StoredSemanticGateArtifact | None,
    ) -> object:
        return self._delegate.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )

    def load_attempt_with_artifacts(
        self,
        attempt_id: str,
    ) -> tbm.StoredSemanticGateAttemptArtifacts:
        raise RuntimeError(f"private read-back detail for {attempt_id}")


def test_decision_transition_failure_is_recoverable_without_second_call() -> None:
    stack = _sqlite_stack(
        iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z"))
    )
    failing = tbm.AuthenticatedSemanticGateSessionService(
        semantic_gate_service=stack.semantic,
        session_writer=_RejectDecided(stack.sessions),  # type: ignore[arg-type]
    )
    request = _request(stack.prepared)
    try:
        with pytest.raises(
            tbm.DurableSemanticGateRecoveryRequiredError
        ) as recovery:
            failing.decide(
                _context(),
                request,
                lambda _call: _provider_result(stack.evaluation),
            )

        assert recovery.value.session.status == "awaiting_decision"
        assert recovery.value.attempt is not None
        assert recovery.value.attempt.status == "succeeded"
        assert "private" not in str(recovery.value)
        assert recovery.value.__cause__ is None

        recovered = stack.service.decide(
            _context(),
            _request(
                stack.prepared,
                expected_session_version=recovery.value.session.version,
            ),
            lambda _call: pytest.fail("recovery called the provider twice"),
        )
        assert recovered.replayed is True
        assert recovered.session.status == "decided"
    finally:
        stack.close()


def test_retained_attempt_with_failed_readback_requires_recovery() -> None:
    stack = _sqlite_stack(
        iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z"))
    )
    semantic = _semantic_service(
        _RejectArtifactReadback(stack.authority),
        stack.evidence,
        iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z")),
    )
    service = tbm.AuthenticatedSemanticGateSessionService(
        semantic_gate_service=semantic,
        session_writer=stack.sessions,
    )
    try:
        with pytest.raises(
            tbm.DurableSemanticGateRecoveryRequiredError
        ) as recovery:
            service.decide(
                _context(),
                _request(stack.prepared),
                lambda _call: _provider_result(stack.evaluation),
            )

        assert recovery.value.__cause__ is None
        assert "private" not in str(recovery.value)
        assert recovery.value.session.status == "awaiting_decision"
        assert recovery.value.attempt is not None
        assert recovery.value.attempt.status == "succeeded"
        assert stack.authority.load_attempt_chain(
            stack.evaluation.evaluation_id
        ) == (recovery.value.attempt,)
    finally:
        stack.close()


def test_provider_side_effect_followed_by_cancellation_requires_recovery() -> None:
    stack = _sqlite_stack(
        iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z"))
    )

    def cancel_then_return(
        _call: tbm.SemanticProviderCall,
    ) -> tbm.SemanticProviderResult:
        awaiting = stack.sessions.get(stack.prepared.session_id)
        assert awaiting.status == "awaiting_decision"
        canceled = stack.sessions.transition(
            awaiting.session_id,
            "canceled",
            expected_version=awaiting.version,
            terminal_reason="operator canceled in-flight decision",
        )
        assert canceled.status == "canceled"
        return _provider_result(stack.evaluation)

    try:
        with pytest.raises(
            tbm.DurableSemanticGateRecoveryRequiredError
        ) as recovery:
            stack.service.decide(
                _context(),
                _request(stack.prepared),
                cancel_then_return,
            )

        assert recovery.value.__cause__ is None
        assert recovery.value.session.status == "canceled"
        assert recovery.value.attempt is not None
        assert recovery.value.attempt.status == "succeeded"
        assert stack.sessions.get(stack.prepared.session_id).status == "canceled"
        assert stack.authority.load_attempt_chain(
            stack.evaluation.evaluation_id
        ) == (recovery.value.attempt,)
    finally:
        stack.close()


def test_expired_awaiting_lease_fails_before_provider_or_attempt() -> None:
    stack = _sqlite_stack(iter(()))
    called = False

    def provider(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
        nonlocal called
        called = True
        return _provider_result(stack.evaluation)

    try:
        awaiting = stack.sessions.transition(
            stack.prepared.session_id,
            "awaiting_decision",
            expected_version=stack.prepared.version,
        )
        stack.clock.set_next(
            datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        )

        with pytest.raises(tbm.DurableSemanticGateV3Error) as expired:
            stack.service.decide(
                _context(),
                _request(
                    stack.prepared,
                    expected_session_version=awaiting.version,
                ),
                provider,
            )

        assert (
            expired.value.code
            == "TBM_DURABLE_SEMANTIC_GATE_SESSION_CHANGED"
        )
        assert expired.value.__cause__ is None
        assert called is False
        assert stack.sessions.get(awaiting.session_id) == awaiting
        assert stack.authority.load_attempt_chain(
            stack.evaluation.evaluation_id
        ) == ()
    finally:
        stack.close()


def test_reentrant_decision_returns_the_concurrent_exact_replay() -> None:
    stack = _sqlite_stack(
        iter(
            (
                "2026-07-27T08:03:00Z",
                "2026-07-27T08:03:01Z",
                "2026-07-27T08:03:02Z",
                "2026-07-27T08:03:03Z",
            )
        )
    )
    inner_results: list[tbm.DurableSemanticGateResult] = []

    def decide_inside_provider(
        _call: tbm.SemanticProviderCall,
    ) -> tbm.SemanticProviderResult:
        awaiting = stack.sessions.get(stack.prepared.session_id)
        inner_results.append(
            stack.service.decide(
                _context(),
                _request(
                    stack.prepared,
                    expected_session_version=awaiting.version,
                ),
                lambda _inner_call: _provider_result(stack.evaluation),
            )
        )
        return _provider_result(stack.evaluation)

    try:
        result = stack.service.decide(
            _context(),
            _request(stack.prepared),
            decide_inside_provider,
        )

        assert result.replayed is True
        assert result.session == inner_results[0].session
        assert result.semantic_gate == inner_results[0].semantic_gate
        assert result.session.status == "decided"
        assert stack.authority.load_attempt_chain(
            stack.evaluation.evaluation_id
        ) == (result.semantic_gate.attempt,)
    finally:
        stack.close()


def test_stale_session_and_chain_fail_before_provider() -> None:
    stack = _sqlite_stack(iter(()))
    called = False

    def provider(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
        nonlocal called
        called = True
        return _provider_result(stack.evaluation)

    try:
        with pytest.raises(tbm.DurableSemanticGateV3Error) as stale:
            stack.service.decide(
                _context(),
                _request(
                    stack.prepared,
                    expected_session_version=stack.prepared.version + 1,
                ),
                provider,
            )
        assert (
            stale.value.code
            == "TBM_DURABLE_SEMANTIC_GATE_SESSION_CHANGED"
        )

        with pytest.raises(tbm.DurableSemanticGateV3Error) as parent:
            stack.service.decide(
                _context(),
                _request(
                    stack.prepared,
                    expected_previous_attempt_id=(
                        "semantic_attempt_sha256_" + ("f" * 64)
                    ),
                ),
                provider,
            )
        assert parent.value.code == "TBM_DURABLE_SEMANTIC_GATE_CHAIN_CHANGED"
        assert called is False
        assert stack.sessions.get(stack.prepared.session_id) == stack.prepared
    finally:
        stack.close()


def test_provider_authentication_and_evidence_linkage_fail_before_transition() -> None:
    stack = _sqlite_stack(iter(()))
    called = False

    def provider(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
        nonlocal called
        called = True
        return _provider_result(stack.evaluation)

    try:
        wrong = tbm.AuthenticatedSemanticProviderContext(
            provider_id="provider_openai",
            authenticator_id="authenticator_oidc",
            credential_id="credential_wrong",
        )
        with pytest.raises(tbm.SemanticGateServiceV3Error) as auth:
            stack.service.decide(
                wrong,
                _request(stack.prepared),
                provider,
            )
        assert (
            auth.value.code
            == "TBM_SEMANTIC_SERVICE_AUTHENTICATION_FAILED"
        )
        with pytest.raises(tbm.SemanticGateServiceV3Error) as hidden:
            stack.service.decide(
                wrong,
                tbm.DurableSemanticGateRequest(
                    session_id="session_does_not_exist",
                    expected_session_version=1,
                    prompt=PROMPT,
                ),
                provider,
            )
        assert (
            hidden.value.code
            == "TBM_SEMANTIC_SERVICE_AUTHENTICATION_FAILED"
        )

        mismatched = stack.sessions.create_or_get(
            session_id="session_mismatched",
            tenant_id="tenant_001",
            repository_id="repository_001",
            principal_id="principal_001",
            agent_client_id="agent_client_001",
            trace_id="trace_other",
            run_id="run_001",
            request_fingerprint="sha256:" + ("b" * 64),
            idempotency_key="durable_semantic_gate_mismatched",
            expires_in_seconds=3600,
        ).session
        mismatched = stack.sessions.transition(
            mismatched.session_id,
            "prepared",
            expected_version=mismatched.version,
            lease_seconds=1800,
            retrieval_snapshot_id=stack.prepared.retrieval_snapshot_id,
            system_gate_evaluation_id=(
                stack.prepared.system_gate_evaluation_id
            ),
        )
        with pytest.raises(tbm.DurableSemanticGateV3Error) as linkage:
            stack.service.decide(
                _context(),
                _request(mismatched),
                provider,
            )
        assert (
            linkage.value.code
            == "TBM_DURABLE_SEMANTIC_GATE_LINKAGE_INVALID"
        )
        assert stack.sessions.get(stack.prepared.session_id) == stack.prepared
        assert stack.sessions.get(mismatched.session_id) == mismatched
        assert called is False
    finally:
        stack.close()


def test_same_connection_outer_rollback_removes_attempt_and_transitions() -> None:
    stack = _sqlite_stack(
        iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z"))
    )
    try:
        stack.connection.execute("BEGIN IMMEDIATE")
        result = stack.service.decide(
            _context(),
            _request(stack.prepared),
            lambda _call: _provider_result(stack.evaluation),
        )
        assert result.session.status == "decided"
        stack.connection.rollback()

        assert stack.sessions.get(stack.prepared.session_id) == stack.prepared
        assert stack.authority.load_attempt_chain(
            stack.evaluation.evaluation_id
        ) == ()
        assert [
            session.status
            for session in stack.sessions.history(stack.prepared.session_id)
        ] == ["created", "prepared"]
    finally:
        stack.close()


def test_durable_semantic_gate_configuration_and_exports_are_intentional() -> None:
    stack = _sqlite_stack(iter(()))
    try:
        with pytest.raises(TypeError):
            tbm.AuthenticatedSemanticGateSessionService(
                semantic_gate_service=object(),  # type: ignore[arg-type]
                session_writer=stack.sessions,
            )
        with pytest.raises(TypeError):
            tbm.AuthenticatedSemanticGateSessionService(
                semantic_gate_service=stack.semantic,
                session_writer=object(),  # type: ignore[arg-type]
            )
        with pytest.raises(tbm.DurableSemanticGateV3Error):
            tbm.DurableSemanticGateRequest(
                session_id="session_001",
                expected_session_version=0,
                prompt=PROMPT,
            )
        with pytest.raises(tbm.DurableSemanticGateV3Error):
            tbm.DurableSemanticGateRequest(
                session_id="s" * (tbm.MEMORY_ID_MAX_CHARS + 1),
                expected_session_version=1,
                prompt=PROMPT,
            )
        with pytest.raises(tbm.DurableSemanticGateV3Error):
            tbm.DurableSemanticGateRequest(
                session_id="\ud800",
                expected_session_version=1,
                prompt=PROMPT,
            )
        with pytest.raises(tbm.DurableSemanticGateV3Error):
            tbm.DurableSemanticGateRequest(
                session_id="session_001",
                expected_session_version=1,
                prompt=PROMPT,
                lease_seconds=tbm.GATE_SESSION_MAX_LEASE_SECONDS + 1,
            )
        with pytest.raises(tbm.DurableSemanticGateV3Error) as parent_type:
            tbm.DurableSemanticGateRequest(
                session_id="session_001",
                expected_session_version=1,
                prompt=PROMPT,
                expected_previous_attempt_id=123,  # type: ignore[arg-type]
            )
        assert (
            parent_type.value.code
            == "TBM_DURABLE_SEMANTIC_GATE_INVALID"
        )
        assert parent_type.value.__cause__ is None
        with pytest.raises(tbm.DurableSemanticGateV3Error):
            stack.service.decide(
                _context(),
                object(),  # type: ignore[arg-type]
                lambda _call: _provider_result(stack.evaluation),
            )
        assert (
            tbm.DURABLE_SEMANTIC_GATE_CONTRACT_VERSION
            == "tbm.durable-semantic-gate.v3"
        )
        assert "AuthenticatedSemanticGateSessionService" in tbm.__all__
        assert "DurableSemanticGateRequest" in tbm.__all__
    finally:
        stack.close()


def test_postgres_durable_semantic_gate_parity(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    postgres_cluster.load_schema()
    for script in (
        "postgres-v3-gate-session.sql",
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
    ):
        installed = postgres_cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr

    snapshot, evaluation = _evidence()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        sessions = tbm.PostgresGateSessionRepository(
            connection,
            allow_direct_completion=False,
        )
        evidence = PostgresGateEvidenceV3Repository(connection)
        authority = tbm.PostgresSemanticGateArtifactV3Repository(connection)
        evidence.store_bundle(snapshot, evaluation)
        created = sessions.create_or_get(
            session_id=snapshot.session_id,
            tenant_id="tenant_001",
            repository_id="repository_001",
            principal_id="principal_001",
            agent_client_id="agent_client_001",
            trace_id=snapshot.trace_id,
            run_id=snapshot.run_id,
            request_fingerprint="sha256:" + ("a" * 64),
            idempotency_key="durable_semantic_gate_postgres",
            expires_in_seconds=3600,
        ).session
        prepared = sessions.transition(
            created.session_id,
            "prepared",
            expected_version=created.version,
            lease_seconds=1800,
            retrieval_snapshot_id=snapshot.snapshot_id,
            system_gate_evaluation_id=evaluation.evaluation_id,
        )
        semantic = _semantic_service(
            authority,
            evidence,
            iter(
                (
                    "2026-07-29T08:03:00Z",
                    "2026-07-29T08:03:01Z",
                    "2026-07-29T08:04:00Z",
                    "2026-07-29T08:04:01Z",
                )
            ),
        )
        service = tbm.AuthenticatedSemanticGateSessionService(
            semantic_gate_service=semantic,
            session_writer=sessions,
        )

        with pytest.raises(
            tbm.DurableSemanticGateProviderFailedError
        ) as failed:
            service.decide(
                _context(),
                _request(prepared),
                lambda _call: (_ for _ in ()).throw(
                    tbm.SemanticProviderCallError("provider_timeout")
                ),
            )
        assert failed.value.session.status == "awaiting_decision"
        assert failed.value.attempt.status == "failed"

        result = service.decide(
            _context(),
            _request(
                prepared,
                expected_session_version=failed.value.session.version,
                expected_previous_attempt_id=failed.value.attempt.attempt_id,
            ),
            lambda _call: _provider_result(evaluation),
        )
        assert result.session.status == "decided"
        assert sessions.get(prepared.session_id) == result.session
        assert authority.load_attempt_chain(evaluation.evaluation_id) == (
            failed.value.attempt,
            result.semantic_gate.attempt,
        )
        failed_artifacts = authority.load_attempt_with_artifacts(
            failed.value.attempt.attempt_id
        )
        assert (
            failed_artifacts.prompt.binding.artifact
            == result.semantic_gate.artifacts.prompt.binding.artifact
        )

        replay = service.decide(
            _context(),
            _request(
                prepared,
                expected_session_version=failed.value.session.version,
                expected_previous_attempt_id=failed.value.attempt.attempt_id,
            ),
            lambda _call: pytest.fail("PostgreSQL replay called the provider"),
        )
        assert replay.replayed is True
        assert replay.session == result.session
        assert replay.semantic_gate == result.semantic_gate


def test_postgres_same_connection_outer_rollback(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    postgres_cluster.load_schema()
    for script in (
        "postgres-v3-gate-session.sql",
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
    ):
        installed = postgres_cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr

    snapshot, evaluation = _evidence()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        sessions = tbm.PostgresGateSessionRepository(
            connection,
            allow_direct_completion=False,
        )
        evidence = PostgresGateEvidenceV3Repository(connection)
        authority = tbm.PostgresSemanticGateArtifactV3Repository(connection)
        evidence.store_bundle(snapshot, evaluation)
        created = sessions.create_or_get(
            session_id=snapshot.session_id,
            tenant_id="tenant_001",
            repository_id="repository_001",
            principal_id="principal_001",
            agent_client_id="agent_client_001",
            trace_id=snapshot.trace_id,
            run_id=snapshot.run_id,
            request_fingerprint="sha256:" + ("a" * 64),
            idempotency_key="durable_semantic_gate_postgres_rollback",
            expires_in_seconds=3600,
        ).session
        prepared = sessions.transition(
            created.session_id,
            "prepared",
            expected_version=created.version,
            lease_seconds=1800,
            retrieval_snapshot_id=snapshot.snapshot_id,
            system_gate_evaluation_id=evaluation.evaluation_id,
        )
        connection.commit()
        semantic = _semantic_service(
            authority,
            evidence,
            iter(
                (
                    "2026-07-29T08:03:00Z",
                    "2026-07-29T08:03:01Z",
                )
            ),
        )
        service = tbm.AuthenticatedSemanticGateSessionService(
            semantic_gate_service=semantic,
            session_writer=sessions,
        )

        class _Rollback(Exception):
            pass

        with pytest.raises(_Rollback):
            with connection.transaction():
                result = service.decide(
                    _context(),
                    _request(prepared),
                    lambda _call: _provider_result(evaluation),
                )
                assert result.session.status == "decided"
                raise _Rollback

        assert sessions.get(prepared.session_id) == prepared
        assert authority.load_attempt_chain(evaluation.evaluation_id) == ()
        assert [
            session.status
            for session in sessions.history(prepared.session_id)
        ] == ["created", "prepared"]


def test_postgres_expired_awaiting_lease_fails_before_provider(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    postgres_cluster.load_schema()
    for script in (
        "postgres-v3-gate-session.sql",
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
    ):
        installed = postgres_cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr

    snapshot, evaluation = _evidence()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        sessions = tbm.PostgresGateSessionRepository(
            connection,
            allow_direct_completion=False,
        )
        evidence = PostgresGateEvidenceV3Repository(connection)
        authority = tbm.PostgresSemanticGateArtifactV3Repository(connection)
        evidence.store_bundle(snapshot, evaluation)
        created = sessions.create_or_get(
            session_id=snapshot.session_id,
            tenant_id="tenant_001",
            repository_id="repository_001",
            principal_id="principal_001",
            agent_client_id="agent_client_001",
            trace_id=snapshot.trace_id,
            run_id=snapshot.run_id,
            request_fingerprint="sha256:" + ("a" * 64),
            idempotency_key="durable_semantic_gate_postgres_expired",
            expires_in_seconds=3600,
        ).session
        prepared = sessions.transition(
            created.session_id,
            "prepared",
            expected_version=created.version,
            lease_seconds=1,
            retrieval_snapshot_id=snapshot.snapshot_id,
            system_gate_evaluation_id=evaluation.evaluation_id,
        )
        awaiting = sessions.transition(
            prepared.session_id,
            "awaiting_decision",
            expected_version=prepared.version,
        )
        connection.commit()
        connection.execute("SELECT pg_sleep(1.1)")
        semantic = _semantic_service(authority, evidence, iter(()))
        service = tbm.AuthenticatedSemanticGateSessionService(
            semantic_gate_service=semantic,
            session_writer=sessions,
        )
        called = False

        def provider(
            _call: tbm.SemanticProviderCall,
        ) -> tbm.SemanticProviderResult:
            nonlocal called
            called = True
            return _provider_result(evaluation)

        with pytest.raises(tbm.DurableSemanticGateV3Error) as expired:
            service.decide(
                _context(),
                _request(
                    prepared,
                    expected_session_version=awaiting.version,
                ),
                provider,
            )

        assert (
            expired.value.code
            == "TBM_DURABLE_SEMANTIC_GATE_SESSION_CHANGED"
        )
        assert called is False
        assert sessions.get(awaiting.session_id) == awaiting
        assert authority.load_attempt_chain(evaluation.evaluation_id) == ()
