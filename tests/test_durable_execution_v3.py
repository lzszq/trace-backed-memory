from __future__ import annotations

from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

import trace_backed_memory as tbm
from tests.test_durable_finalization_v3 import _Clock, _Stack, _stack


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
EVALUATOR = tbm.TrustedOutcomeEvaluator(
    evaluator_id="outcome_evaluator",
    evaluator_version="v1",
    authenticator_id="mtls",
    credential_id="credential_outcome_evaluator",
)
EVALUATOR_CONTEXT = tbm.AuthenticatedOutcomeEvaluatorContext(
    evaluator_id=EVALUATOR.evaluator_id,
    authenticator_id=EVALUATOR.authenticator_id,
    credential_id="credential_outcome_evaluator",
)


def _authenticate_evaluator(
    context: tbm.AuthenticatedOutcomeEvaluatorContext,
) -> tbm.TrustedOutcomeEvaluator:
    if context != EVALUATOR_CONTEXT:
        raise ValueError("untrusted evaluator transport")
    return EVALUATOR


class _ExecutionStack:
    def __init__(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        for resource in (
            "schemas/sqlite-v3-gate-session.sql",
            "schemas/sqlite-v3-outcome.sql",
            "schemas/sqlite-v3-completion-outbox.sql",
        ):
            connection.executescript(
                tbm.read_packaged_resource(resource).decode("utf-8")
            )
        self.outbox = tbm.SQLiteCompletionOutboxV3Repository(
            connection,
            clock=_Clock(),
        )
        self.base: _Stack = _stack(
            permissions=(
                "memory:retrieve",
                "gate_session:transition",
            ),
            connection=connection,
            sessions=self.outbox.gate_sessions,
        )
        self.finalizer = self.base.finalizer()
        self.finalized = self.finalizer.finalize(
            self.base.context,
            self.base.scope,
            tbm.DurableFinalizationRequest(
                self.base.decided.session_id,
                self.base.decided.version,
            ),
        )
        self.transition_scope = (
            self.base.authorization.authorize_permission(
                self.base.context,
                permission="gate_session:transition",
                operation=lambda scope: scope,
            ).scope
        )
        self.service = self.make_service()

    def make_service(
        self,
        *,
        clock=lambda: "2026-07-30T01:10:00Z",
        completion_authority=None,
        evaluator_authenticator=_authenticate_evaluator,
    ) -> tbm.DurableExecutionService:
        return tbm.DurableExecutionService(
            authorization_service=self.base.authorization,
            session_writer=self.base.sessions,
            finalization_reader=self.finalizer,
            completion_authority=completion_authority or self.outbox,
            evaluator_authenticator=evaluator_authenticator,
            clock=clock,
        )

    def start(self) -> tbm.DurableExecutionStartResult:
        return self.service.start(
            self.base.context,
            self.base.scope,
            self.transition_scope,
            tbm.DurableExecutionStartRequest(
                self.finalized.session.session_id,
                self.finalized.session.version,
            ),
        )

    def completion(
        self,
        session: tbm.GateSession,
    ) -> tbm.GateCompletionRequest:
        return tbm.GateCompletionRequest(
            session_id=session.session_id,
            expected_version=session.version,
            result="pass",
            evaluator_id=EVALUATOR.evaluator_id,
            evaluator_version=EVALUATOR.evaluator_version,
            evidence_artifact_sha256s=(DIGEST_B,),
            output_sha256=DIGEST_A,
            latency_ms=125,
            cost_usd=0.125,
        )

    def close(self) -> None:
        self.outbox.close()
        self.base.close()


def test_durable_execution_starts_replays_completes_and_emits_outbox():
    stack = _ExecutionStack()
    try:
        started = stack.start()
        assert started.session.status == "executing"
        assert started.session.version == stack.finalized.session.version + 1
        assert started.snippet == stack.finalized.snippet
        assert started.execution_required is True
        assert started.replayed is False
        assert (
            started.transition_authorization_event_id
            == stack.transition_scope.authorization_event_id
        )

        replayed_start = stack.start()
        assert replayed_start.session == started.session
        assert replayed_start.snippet == started.snippet
        assert replayed_start.execution_required is True
        assert replayed_start.replayed is True

        request = stack.completion(started.session)
        completed = stack.service.complete(
            stack.base.context,
            stack.transition_scope,
            EVALUATOR_CONTEXT,
            request,
        )
        assert completed.session.status == "completed"
        assert completed.outcome.usage_decision_id == started.usage_decision.usage_decision_id
        assert completed.event.run_outcome_id == completed.outcome.run_outcome_id
        assert completed.delivery.status == "pending"
        assert completed.inserted is True
        assert completed.event_inserted is True
        assert completed.replayed is False
        assert (
            completed.transition_authorization_event_id
            == stack.transition_scope.authorization_event_id
        )

        replayed_completion = stack.service.complete(
            stack.base.context,
            stack.transition_scope,
            EVALUATOR_CONTEXT,
            request,
        )
        assert replayed_completion.session == completed.session
        assert replayed_completion.outcome == completed.outcome
        assert replayed_completion.event == completed.event
        assert replayed_completion.inserted is False
        assert replayed_completion.event_inserted is False
        assert replayed_completion.replayed is True

        with pytest.raises(tbm.DurableExecutionV3Error) as stale_replay:
            stack.service.complete(
                stack.base.context,
                stack.transition_scope,
                EVALUATOR_CONTEXT,
                replace(
                    request,
                    expected_version=request.expected_version - 1,
                ),
            )
        assert (
            stale_replay.value.code
            == "TBM_DURABLE_EXECUTION_SESSION_CHANGED"
        )

        terminal_start = stack.start()
        assert terminal_start.session == completed.session
        assert terminal_start.snippet is None
        assert terminal_start.execution_required is False
        assert terminal_start.replayed is True
    finally:
        stack.close()


def test_durable_execution_resume_renews_lease_and_replays_exact_injection():
    stack = _ExecutionStack()
    try:
        started = stack.start()
        resumed = stack.service.resume(
            stack.base.context,
            stack.base.scope,
            stack.transition_scope,
            tbm.DurableExecutionResumeRequest(
                started.session.session_id,
                started.session.version,
                lease_seconds=2_700,
            ),
        )
        assert resumed.session.status == "executing"
        assert resumed.session.version == started.session.version + 1
        assert resumed.session.lease_expires_at != started.session.lease_expires_at
        assert resumed.snippet == started.snippet
        assert resumed.execution_required is True
        assert resumed.replayed is True

        completed = stack.service.complete(
            stack.base.context,
            stack.transition_scope,
            EVALUATOR_CONTEXT,
            stack.completion(resumed.session),
        )
        assert completed.session.version == resumed.session.version + 1
    finally:
        stack.close()


def test_durable_execution_abandonment_is_exact_and_idempotent():
    stack = _ExecutionStack()
    try:
        started = stack.start()
        request = tbm.DurableExecutionAbandonRequest(
            started.session.session_id,
            started.session.version,
            "executor callback failed before completion measurement",
        )
        abandoned = stack.service.abandon(
            stack.base.context,
            stack.transition_scope,
            request,
        )
        assert abandoned.session.status == "abandoned"
        assert abandoned.session.terminal_reason == request.reason
        assert abandoned.replayed is False

        replayed = stack.service.abandon(
            stack.base.context,
            stack.transition_scope,
            request,
        )
        assert replayed.session == abandoned.session
        assert replayed.replayed is True
    finally:
        stack.close()


def test_durable_execution_rejects_wrong_permission_and_evaluator_credential():
    stack = _ExecutionStack()
    try:
        with pytest.raises(tbm.DurableExecutionV3Error) as denied:
            stack.service.start(
                stack.base.context,
                stack.base.scope,
                stack.base.scope,
                tbm.DurableExecutionStartRequest(
                    stack.finalized.session.session_id,
                    stack.finalized.session.version,
                ),
            )
        assert denied.value.code == "TBM_SERVICE_AUTHORIZATION_SCOPE_INVALID"
        assert stack.base.sessions.get(
            stack.finalized.session.session_id
        ) == stack.finalized.session

        started = stack.start()
        forged = replace(
            EVALUATOR_CONTEXT,
            credential_id="credential_forged",
        )
        with pytest.raises(tbm.DurableExecutionV3Error) as rejected:
            stack.service.complete(
                stack.base.context,
                stack.transition_scope,
                forged,
                stack.completion(started.session),
            )
        assert (
            rejected.value.code
            == "TBM_DURABLE_EXECUTION_EVALUATOR_REJECTED"
        )
        assert stack.base.sessions.get(started.session.session_id) == started.session
    finally:
        stack.close()


def test_durable_execution_rejects_changed_revision_without_mutation():
    stack = _ExecutionStack()
    try:
        with pytest.raises(tbm.DurableExecutionV3Error) as changed:
            stack.service.start(
                stack.base.context,
                stack.base.scope,
                stack.transition_scope,
                tbm.DurableExecutionStartRequest(
                    stack.finalized.session.session_id,
                    stack.finalized.session.version + 1,
                ),
            )
        assert changed.value.code == "TBM_DURABLE_EXECUTION_SESSION_CHANGED"
        assert stack.base.sessions.get(
            stack.finalized.session.session_id
        ) == stack.finalized.session
    finally:
        stack.close()


def test_durable_execution_respects_caller_owned_sqlite_transactions():
    stack = _ExecutionStack()
    try:
        stack.base.connection.execute("BEGIN")
        started = stack.start()
        assert started.session.status == "executing"
        stack.base.connection.rollback()
        assert stack.base.sessions.get(
            stack.finalized.session.session_id
        ) == stack.finalized.session

        started = stack.start()
        stack.base.connection.execute("BEGIN")
        completed = stack.service.complete(
            stack.base.context,
            stack.transition_scope,
            EVALUATOR_CONTEXT,
            stack.completion(started.session),
        )
        assert completed.session.status == "completed"
        stack.base.connection.rollback()
        assert stack.base.sessions.get(started.session.session_id) == started.session
        with pytest.raises(tbm.SQLiteCompletionOutboxV3NotFoundError):
            stack.outbox.get_event(completed.event.event_id)
    finally:
        if stack.base.connection.in_transaction:
            stack.base.connection.rollback()
        stack.close()


def test_durable_execution_expired_lease_requires_explicit_recovery():
    stack = _ExecutionStack()
    try:
        expired = stack.make_service(
            clock=lambda: "2026-07-30T01:31:00Z"
        )
        start_request = tbm.DurableExecutionStartRequest(
            stack.finalized.session.session_id,
            stack.finalized.session.version,
        )
        with pytest.raises(tbm.DurableExecutionV3Error) as start_error:
            expired.start(
                stack.base.context,
                stack.base.scope,
                stack.transition_scope,
                start_request,
            )
        assert (
            start_error.value.code
            == "TBM_DURABLE_EXECUTION_RECOVERY_REQUIRED"
        )
        assert stack.base.sessions.get(
            stack.finalized.session.session_id
        ) == stack.finalized.session

        started = stack.start()
        calls = (
            lambda: expired.resume(
                stack.base.context,
                stack.base.scope,
                stack.transition_scope,
                tbm.DurableExecutionResumeRequest(
                    started.session.session_id,
                    started.session.version,
                ),
            ),
            lambda: expired.abandon(
                stack.base.context,
                stack.transition_scope,
                tbm.DurableExecutionAbandonRequest(
                    started.session.session_id,
                    started.session.version,
                    "expired executor lease",
                ),
            ),
            lambda: expired.complete(
                stack.base.context,
                stack.transition_scope,
                EVALUATOR_CONTEXT,
                stack.completion(started.session),
            ),
        )
        for call in calls:
            with pytest.raises(tbm.DurableExecutionV3Error) as error:
                call()
            assert (
                error.value.code
                == "TBM_DURABLE_EXECUTION_RECOVERY_REQUIRED"
            )
            assert (
                stack.base.sessions.get(started.session.session_id)
                == started.session
            )
    finally:
        stack.close()


def test_durable_execution_reloads_evaluator_authentication_each_completion():
    stack = _ExecutionStack()
    try:
        started = stack.start()
        active = True

        def authenticate(context):
            if not active or context != EVALUATOR_CONTEXT:
                raise ValueError("evaluator registration was revoked")
            return EVALUATOR

        service = stack.make_service(evaluator_authenticator=authenticate)
        active = False
        with pytest.raises(tbm.DurableExecutionV3Error) as rejected:
            service.complete(
                stack.base.context,
                stack.transition_scope,
                EVALUATOR_CONTEXT,
                stack.completion(started.session),
            )
        assert (
            rejected.value.code
            == "TBM_DURABLE_EXECUTION_EVALUATOR_REJECTED"
        )
        assert stack.base.sessions.get(started.session.session_id) == started.session
    finally:
        stack.close()


def test_durable_execution_rejects_malformed_completion_receipt():
    stack = _ExecutionStack()
    try:
        started = stack.start()
        request = stack.completion(started.session)
        retained = stack.outbox.complete_session(request)

        class _MalformedAuthority:
            @property
            def gate_sessions(self):
                return stack.base.sessions

            def complete_session(self, _request):
                return SimpleNamespace(
                    completion=replace(
                        retained.completion,
                        inserted=1,
                    ),
                    event=retained.event,
                    delivery=retained.delivery,
                    event_inserted=True,
                )

            def get_event(self, event_id):
                return stack.outbox.get_event(event_id)

            def get_delivery(self, event_id):
                return stack.outbox.get_delivery(event_id)

        service = stack.make_service(
            completion_authority=_MalformedAuthority()
        )
        with pytest.raises(tbm.DurableExecutionV3Error) as invalid:
            service.complete(
                stack.base.context,
                stack.transition_scope,
                EVALUATOR_CONTEXT,
                request,
            )
        assert invalid.value.code == "TBM_DURABLE_EXECUTION_RECEIPT_INVALID"

        class _MalformedReadbackAuthority:
            @property
            def gate_sessions(self):
                return stack.base.sessions

            def complete_session(self, _request):
                return retained

            def get_event(self, event_id):
                return stack.outbox.get_event(event_id)

            def get_delivery(self, event_id):
                delivery = stack.outbox.get_delivery(event_id)
                return SimpleNamespace(
                    event_id=delivery.event_id,
                    version=delivery.version + 1,
                )

        readback_service = stack.make_service(
            completion_authority=_MalformedReadbackAuthority()
        )
        with pytest.raises(tbm.DurableExecutionV3Error) as readback:
            readback_service.complete(
                stack.base.context,
                stack.transition_scope,
                EVALUATOR_CONTEXT,
                request,
            )
        assert readback.value.code == "TBM_DURABLE_EXECUTION_RECEIPT_INVALID"
    finally:
        stack.close()


def test_durable_execution_requires_completion_authority_session_identity():
    stack = _ExecutionStack()
    other = tbm.SQLiteCompletionOutboxV3Repository.connect(
        initialize=True,
        clock=_Clock(),
    )
    try:
        with pytest.raises(TypeError, match="share the GateSession authority"):
            stack.make_service(completion_authority=other)
    finally:
        other.close()
        stack.close()
