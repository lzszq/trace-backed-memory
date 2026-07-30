from __future__ import annotations

import base64

from pydantic import ValidationError
import pytest

import trace_backed_memory as tbm
from tests.test_durable_agent_v3 import _completion, _stack
from tests.test_durable_execution_v3 import (
    EVALUATOR,
    EVALUATOR_CONTEXT,
)
from tests.test_durable_retrieval_preparation_v3 import _durable_request
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
    _provider_result,
    _request as _semantic_request,
)
from trace_backed_memory.durable_agent_wire_v1 import (
    DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
    DurableAbandonRequest,
    DurableAgentProtocolDispatcher,
    DurableAgentWireConfiguration,
    DurableAgentWireError,
    DurableCancelRequest,
    DurableCompleteRequest,
    DurableDecideRequest,
    DurableFinalizeRequest,
    DurableGetSessionRequest,
    DurablePrepareRequest,
    DurableReplayRequest,
    DurableResumeRequest,
    DurableStartRequest,
    public_durable_agent_wire_error,
)


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _dispatcher(
    stack,
    *,
    expose_injection_content: bool = True,
    expose_replay_content: bool = True,
) -> DurableAgentProtocolDispatcher:
    repository_id = _durable_request().context.repository_id
    return DurableAgentProtocolDispatcher(
        DurableAgentWireConfiguration(
            "sqlite",
            expose_injection_content=expose_injection_content,
            expose_replay_content=expose_replay_content,
        ),
        stack.agent(),
        repository_id_resolver=lambda _context: repository_id,
        evaluator_resolver=lambda _context: EVALUATOR,
    )


def _prepare_request() -> DurablePrepareRequest:
    source = _durable_request()
    semantic = source.semantic_query
    return DurablePrepareRequest(
        request_id=source.request_id,
        trace_id=source.trace_id,
        run_id=source.run_id,
        task_mode=source.context.task_mode,
        commit_sha=source.context.commit_sha,
        attributes=dict(source.context.attributes),
        evaluation_suite=source.context.evaluation_suite,
        evaluation_case_id=source.context.evaluation_case_id,
        retrieval_mode=source.retrieval_mode,
        retriever_id=source.retriever_id,
        retriever_version=source.retriever_version,
        top_k=source.top_k,
        idempotency_key=source.idempotency_key,
        expires_in_seconds=3_600,
        lease_seconds=source.lease_seconds,
        query_base64=None if source.query is None else _b64(source.query),
        semantic_query=(
            None
            if semantic is None
            else {
                "provider_id": semantic.provider_id,
                "provider_version": semantic.provider_version,
                "vector": list(semantic.vector),
            }
        ),
    )


def _decide_request(
    prepared: tbm.GateSession,
    evaluation: tbm.SystemGateEvaluation,
    *,
    response: bytes | None = None,
) -> DurableDecideRequest:
    source = _semantic_request(prepared)
    provider = _provider_result(evaluation)
    return DurableDecideRequest(
        session_id=source.session_id,
        expected_session_version=source.expected_session_version,
        prompt_base64=_b64(source.prompt),
        expected_previous_attempt_id=source.expected_previous_attempt_id,
        lease_seconds=source.lease_seconds,
        response_base64=_b64(
            provider.response if response is None else response
        ),
        provider_request_id=provider.provider_request_id,
        decision_id=provider.decision_id,
        final_allowed_revision_ids=list(
            provider.final_allowed_revision_ids
        ),
        final_blocked_revision_ids=list(
            provider.final_blocked_revision_ids
        ),
        reason=provider.reason,
        risk=provider.risk,
        recommended_injection=provider.recommended_injection,
        input_tokens=provider.input_tokens,
        output_tokens=provider.output_tokens,
    )


def _prepare(
    stack,
    dispatcher: DurableAgentProtocolDispatcher,
) -> tuple[tbm.GateSession, tbm.SystemGateEvaluation]:
    response = dispatcher.prepare(stack.context, _prepare_request())
    session = stack.sessions.get(response["result"]["session"]["session_id"])
    evaluation = stack.evidence.load_evaluation(
        session.system_gate_evaluation_id
    )
    return session, evaluation


def _decide(
    stack,
    dispatcher: DurableAgentProtocolDispatcher,
    prepared: tbm.GateSession,
    evaluation: tbm.SystemGateEvaluation,
) -> tbm.GateSession:
    response = dispatcher.decide(
        stack.context,
        _provider_context(),
        _decide_request(prepared, evaluation),
    )
    return stack.sessions.get(response["result"]["session"]["session_id"])


def test_durable_wire_dispatcher_maps_complete_durable_lifecycle() -> None:
    stack = _stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    try:
        dispatcher = _dispatcher(stack)
        capabilities = dispatcher.capabilities()
        assert capabilities["protocol_version"] == (
            DURABLE_AGENT_WIRE_PROTOCOL_VERSION
        )
        assert capabilities["identity_source"] == "trusted_adapter"
        assert capabilities["caller_identity_fields"] is False
        assert capabilities["process_local_records"] == []
        assert "export_replay" in capabilities["operations"]

        prepared, evaluation = _prepare(stack, dispatcher)
        decided = _decide(
            stack,
            dispatcher,
            prepared,
            evaluation,
        )
        finalized_response = dispatcher.finalize(
            stack.context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = stack.sessions.get(decided.session_id)
        assert finalized_response["result"]["content_exposed"] is True
        assert finalized_response["result"]["snippet"]
        assert finalized_response["result"]["session"]["status"] == "finalized"

        started_response = dispatcher.start(
            stack.context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        started = stack.sessions.get(finalized.session_id)
        assert started_response["result"]["session"]["status"] == "executing"
        assert started_response["result"]["snippet"] == (
            finalized_response["result"]["snippet"]
        )
        resumed_response = dispatcher.resume(
            stack.context,
            DurableResumeRequest(
                session_id=started.session_id,
                expected_session_version=started.version,
                lease_seconds=2_700,
            ),
        )
        started = stack.sessions.get(started.session_id)
        assert resumed_response["result"]["session"]["status"] == "executing"

        completion = _completion(started)
        completed_response = dispatcher.complete(
            stack.context,
            EVALUATOR_CONTEXT,
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(
                    completion.evidence_artifact_sha256s
                ),
                output_sha256=completion.output_sha256,
                tool_outputs_sha256=completion.tool_outputs_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
                error_code=completion.error_code,
            ),
        )
        completed = stack.sessions.get(started.session_id)
        assert completed_response["result"]["session"]["status"] == "completed"
        assert completed_response["result"]["outcome"]["result"] == "pass"
        assert completed_response["result"]["outbox_event"]["event_id"]

        session_response = dispatcher.get_session(
            stack.context,
            DurableGetSessionRequest(session_id=completed.session_id),
        )
        assert session_response["result"]["session"] == completed.to_dict()

        replay_response = dispatcher.export_replay(
            stack.context,
            DurableReplayRequest(
                session_id=completed.session_id,
                expected_session_version=completed.version,
                allowed_classifications=["internal"],
            ),
        )
        assert replay_response["result"]["content_exposed"] is True
        assert replay_response["result"]["bundle"]["artifacts"]
        assert all(
            artifact["content_base64"]
            for artifact in replay_response["result"]["bundle"]["artifacts"]
        )
    finally:
        stack.close()


def test_durable_wire_cancel_and_abandon_are_exactly_replayable() -> None:
    cancel_stack = _stack()
    try:
        dispatcher = _dispatcher(cancel_stack)
        prepared, _evaluation = _prepare(cancel_stack, dispatcher)
        request = DurableCancelRequest(
            session_id=prepared.session_id,
            expected_session_version=prepared.version,
            reason="caller canceled durable request",
        )

        canceled = dispatcher.cancel(cancel_stack.context, request)
        replayed = dispatcher.cancel(cancel_stack.context, request)

        assert canceled["result"]["session"]["status"] == "canceled"
        assert canceled["result"]["replayed"] is False
        assert replayed["result"]["session"] == canceled["result"]["session"]
        assert replayed["result"]["replayed"] is True
    finally:
        cancel_stack.close()

    abandon_stack = _stack()
    try:
        dispatcher = _dispatcher(abandon_stack)
        prepared, evaluation = _prepare(abandon_stack, dispatcher)
        decided = _decide(
            abandon_stack,
            dispatcher,
            prepared,
            evaluation,
        )
        dispatcher.finalize(
            abandon_stack.context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = abandon_stack.sessions.get(decided.session_id)
        dispatcher.start(
            abandon_stack.context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        executing = abandon_stack.sessions.get(finalized.session_id)
        request = DurableAbandonRequest(
            session_id=executing.session_id,
            expected_session_version=executing.version,
            reason="caller stopped durable execution",
        )

        abandoned = dispatcher.abandon(abandon_stack.context, request)
        replayed = dispatcher.abandon(abandon_stack.context, request)

        assert abandoned["result"]["session"]["status"] == "abandoned"
        assert abandoned["result"]["replayed"] is False
        assert replayed["result"]["session"] == abandoned["result"]["session"]
        assert replayed["result"]["replayed"] is True
    finally:
        abandon_stack.close()


def test_durable_wire_content_exposure_is_explicit_and_fail_closed() -> None:
    stack = _stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    try:
        dispatcher = _dispatcher(
            stack,
            expose_injection_content=False,
            expose_replay_content=False,
        )
        prepared, evaluation = _prepare(stack, dispatcher)
        decided = _decide(stack, dispatcher, prepared, evaluation)
        finalized = dispatcher.finalize(
            stack.context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        session = stack.sessions.get(decided.session_id)

        assert finalized["result"]["snippet"] is None
        assert finalized["result"]["content_exposed"] is False
        assert "export_replay" not in dispatcher.capabilities()["operations"]
        with pytest.raises(DurableAgentWireError) as raised:
            dispatcher.export_replay(
                stack.context,
                DurableReplayRequest(
                    session_id=session.session_id,
                    expected_session_version=session.version,
                    allowed_classifications=["internal"],
                ),
            )
        assert raised.value.code == "TBM_DURABLE_WIRE_REPLAY_CONTENT_DISABLED"
        assert raised.value.category == "authorization"
    finally:
        stack.close()


def test_durable_wire_rejects_mismatched_decided_response_replay() -> None:
    stack = _stack()
    try:
        dispatcher = _dispatcher(stack)
        prepared, evaluation = _prepare(stack, dispatcher)
        _decide(stack, dispatcher, prepared, evaluation)

        with pytest.raises(DurableAgentWireError) as raised:
            dispatcher.decide(
                stack.context,
                _provider_context(),
                _decide_request(
                    prepared,
                    evaluation,
                    response=b'{"different":"response"}',
                ),
            )

        assert raised.value.code == (
            "TBM_DURABLE_WIRE_DECISION_REPLAY_MISMATCH"
        )
        assert raised.value.category == "state"
    finally:
        stack.close()


def test_durable_wire_requests_forbid_caller_identity_fields() -> None:
    payload = _prepare_request().model_dump()
    payload["tenant_id"] = "tenant_forged"
    with pytest.raises(ValidationError):
        DurablePrepareRequest.model_validate(payload)

    with pytest.raises(ValidationError):
        DurableCompleteRequest.model_validate(
            {
                "session_id": "session_001",
                "expected_session_version": 1,
                "result": "pass",
                "evidence_artifact_sha256s": [],
                "output_sha256": "sha256:" + "a" * 64,
                "evaluator_id": "evaluator_forged",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("evidence_artifact_sha256s", []),
        ("evidence_artifact_sha256s", ["not-a-digest"]),
        (
            "evidence_artifact_sha256s",
            ["sha256:" + "b" * 64, "sha256:" + "a" * 64],
        ),
        (
            "evidence_artifact_sha256s",
            ["sha256:" + "a" * 64, "sha256:" + "a" * 64],
        ),
        (
            "evidence_artifact_sha256s",
            ["sha256:" + f"{index:064x}" for index in range(65)],
        ),
        ("cost_usd", float("inf")),
        ("latency_ms", 2_147_483_648),
    ),
)
def test_durable_wire_rejects_noncanonical_completion_measurements(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "session_id": "session_001",
        "expected_session_version": 1,
        "result": "pass",
        "evidence_artifact_sha256s": ["sha256:" + "a" * 64],
        "output_sha256": "sha256:" + "b" * 64,
        "cost_usd": 0.125,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        DurableCompleteRequest.model_validate(payload)


def test_durable_wire_normalizes_integer_cost_to_canonical_float() -> None:
    request = DurableCompleteRequest.model_validate(
        {
            "session_id": "session_001",
            "expected_session_version": 1,
            "result": "pass",
            "evidence_artifact_sha256s": ["sha256:" + "a" * 64],
            "output_sha256": "sha256:" + "b" * 64,
            "cost_usd": 1,
        }
    )

    assert request.cost_usd == 1.0
    assert type(request.cost_usd) is float


@pytest.mark.parametrize(
    ("result", "error_code"),
    (("error", None), ("pass", "unexpected_error")),
)
def test_durable_wire_requires_result_shaped_error_code(
    result: str,
    error_code: str | None,
) -> None:
    with pytest.raises(ValidationError):
        DurableCompleteRequest.model_validate(
            {
                "session_id": "session_001",
                "expected_session_version": 1,
                "result": result,
                "evidence_artifact_sha256s": ["sha256:" + "a" * 64],
                "output_sha256": "sha256:" + "b" * 64,
                "error_code": error_code,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("final_allowed_revision_ids", ["not-a-revision"]),
        (
            "final_allowed_revision_ids",
            [
                "memory_revision_sha256_" + "b" * 64,
                "memory_revision_sha256_" + "a" * 64,
            ],
        ),
        (
            "final_blocked_revision_ids",
            [
                "memory_revision_sha256_" + "a" * 64,
                "memory_revision_sha256_" + "a" * 64,
            ],
        ),
    ),
)
def test_durable_wire_rejects_noncanonical_decision_ids(
    field: str,
    value: object,
) -> None:
    stack = _stack()
    try:
        dispatcher = _dispatcher(stack)
        prepared, evaluation = _prepare(stack, dispatcher)
        payload = _decide_request(prepared, evaluation).model_dump()
        payload[field] = value

        with pytest.raises(ValidationError):
            DurableDecideRequest.model_validate(payload)
    finally:
        stack.close()


def test_durable_wire_sanitizes_trusted_resolver_failures() -> None:
    stack = _stack()
    try:
        repository_id = _durable_request().context.repository_id

        def fail_repository(_context) -> str:
            raise RuntimeError("secret repository credential")

        repository_dispatcher = DurableAgentProtocolDispatcher(
            DurableAgentWireConfiguration("sqlite"),
            stack.agent(),
            repository_id_resolver=fail_repository,
            evaluator_resolver=lambda _context: EVALUATOR,
        )
        with pytest.raises(DurableAgentWireError) as repository_error:
            repository_dispatcher.prepare(stack.context, _prepare_request())
        assert repository_error.value.code == (
            "TBM_DURABLE_WIRE_REPOSITORY_AUTHENTICATION_FAILED"
        )
        assert "secret" not in str(repository_error.value)

        invalid_repository_dispatcher = DurableAgentProtocolDispatcher(
            DurableAgentWireConfiguration("sqlite"),
            stack.agent(),
            repository_id_resolver=lambda _context: " invalid ",
            evaluator_resolver=lambda _context: EVALUATOR,
        )
        with pytest.raises(DurableAgentWireError) as invalid_repository_error:
            invalid_repository_dispatcher.prepare(
                stack.context,
                _prepare_request(),
            )
        assert invalid_repository_error.value.code == (
            "TBM_DURABLE_WIRE_REPOSITORY_AUTHENTICATION_FAILED"
        )

        mismatched_evaluator = tbm.TrustedOutcomeEvaluator(
            evaluator_id="other_evaluator",
            evaluator_version="v1",
            authenticator_id=EVALUATOR.authenticator_id,
            credential_id=EVALUATOR_CONTEXT.credential_id,
        )
        evaluator_dispatcher = DurableAgentProtocolDispatcher(
            DurableAgentWireConfiguration("sqlite"),
            stack.agent(),
            repository_id_resolver=lambda _context: repository_id,
            evaluator_resolver=lambda _context: mismatched_evaluator,
        )
        with pytest.raises(DurableAgentWireError) as evaluator_error:
            evaluator_dispatcher.complete(
                stack.context,
                EVALUATOR_CONTEXT,
                DurableCompleteRequest(
                    session_id="session_001",
                    expected_session_version=1,
                    result="pass",
                    evidence_artifact_sha256s=["sha256:" + "a" * 64],
                    output_sha256="sha256:" + "b" * 64,
                ),
            )
        assert evaluator_error.value.code == (
            "TBM_DURABLE_WIRE_EVALUATOR_AUTHENTICATION_FAILED"
        )
        assert "credential" not in str(evaluator_error.value)
    finally:
        stack.close()


def test_durable_wire_sanitizes_coded_domain_error_messages() -> None:
    class CodedDomainError(RuntimeError):
        code = "TBM_EXAMPLE_PERSISTENCE_FAILED"

    error = public_durable_agent_wire_error(
        CodedDomainError("secret database contents"),
        "prepare",
    )

    assert error.code == "TBM_EXAMPLE_PERSISTENCE_FAILED"
    assert error.category == "persistence"
    assert str(error) == "durable Agent persistence operation failed"
    assert "secret" not in str(error)


def test_durable_wire_rejects_noncanonical_content_and_stale_state() -> None:
    payload = _prepare_request().model_dump()
    payload["query_base64"] = "not base64!"
    with pytest.raises(ValidationError):
        DurablePrepareRequest.model_validate(payload)

    stack = _stack()
    try:
        dispatcher = _dispatcher(stack)
        prepared, evaluation = _prepare(stack, dispatcher)
        decided = _decide(stack, dispatcher, prepared, evaluation)

        with pytest.raises(DurableAgentWireError) as raised:
            dispatcher.finalize(
                stack.context,
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version - 1,
                ),
            )

        assert raised.value.code.endswith("SESSION_CHANGED")
        assert raised.value.category == "state"
        envelope = raised.value.to_dict()
        assert envelope["protocol_version"] == (
            DURABLE_AGENT_WIRE_PROTOCOL_VERSION
        )
        assert envelope["error"]["retryable"] is False
    finally:
        stack.close()
