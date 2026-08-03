from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_event_ledger_v1 as sqlite_event_ledger_v1
from tests.postgres_support import PostgresCluster
from tests.test_artifact_service_v3 import (
    _context as _service_context,
    _registry,
)
from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import (
    EVALUATOR,
    EVALUATOR_CONTEXT,
    _authenticate_evaluator,
)
from tests.test_durable_retrieval_preparation_v3 import _durable_request
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
    _provider_result,
)
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _Source,
    _candidate,
    _indexes,
    _policy,
    _record,
    _result,
)
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableAgentWireError,
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableGetSessionRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeDependencies,
    DurableRuntimeFactory,
    DurableRuntimeV3Error,
)
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import loads_canonical_event
from trace_backed_memory.reducer import ReducerEvent, execute_reducer_step


ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(milliseconds=125)
        return value.isoformat().replace("+00:00", "Z")

    def advance(self, *, seconds: int) -> None:
        self._next += timedelta(seconds=seconds)


def _dependencies(
    clock: _Clock,
    *,
    completion_consumer=None,
    semantic_provider_invoker=None,
    semantic_provider_reconciler=None,
) -> tuple[
    DurableRuntimeDependencies,
    tbm.AuthenticatedServiceContext,
]:
    registry = _registry(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    context = _service_context(registry)
    policy = _policy()
    candidate = _candidate("memory_durable_runtime")
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
    repository_id = _durable_request().context.repository_id
    request_numbers = iter(range(1, 10_000))
    session_numbers = iter(range(1, 10_000))
    dependencies = DurableRuntimeDependencies(
        registry_provider=lambda: registry,
        policy_provider=lambda: policy,
        discovery=discovery,
        revision_source=source,
        semantic_provider=tbm.TrustedSemanticProvider(
            provider_id="provider_openai",
            authenticator_id="authenticator_oidc",
            credential_id="credential_prod_01",
            model_id="model_gate",
            model_version="2026-07-01",
            endpoint_id="endpoint_primary",
        ),
        semantic_configuration=tbm.SemanticGateServiceConfiguration(
            prompt_template_id="semantic_gate_default",
            prompt_template_version="v1",
            generation_config_sha256="sha256:" + "3" * 64,
            response_media_type="application/json",
        ),
        evaluator_authenticator=_authenticate_evaluator,
        repository_id_resolver=lambda _context: repository_id,
        clock=clock,
        authorization_request_id_factory=lambda: (
            f"authorization_runtime_{next(request_numbers):04d}"
        ),
        session_id_factory=lambda: f"gate_session_runtime_{next(session_numbers):04d}",
        completion_consumer=completion_consumer,
        semantic_provider_invoker=semantic_provider_invoker,
        semantic_provider_reconciler=semantic_provider_reconciler,
    )
    return dependencies, context


def _run_prepare_crash_probe(database: str, checkpoint: str) -> None:
    targets = {
        "authorization": "INSERT INTO V3_AUTHORIZATION_DECISIONS",
        "created": "INSERT INTO GATE_SESSION_HEADS",
        "evidence": "INSERT INTO V3_SYSTEM_GATE_EVALUATIONS",
    }
    target = targets[checkpoint]
    dependencies, context = _dependencies(_Clock())
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    target_seen = False
    commit_seen = False

    def trace(statement: str) -> None:
        nonlocal target_seen, commit_seen
        normalized = " ".join(statement.upper().split())
        if commit_seen:
            os.kill(os.getpid(), signal.SIGKILL)
        if target in normalized:
            target_seen = True
        elif target_seen and normalized == "COMMIT":
            commit_seen = True

    runtime._connection.set_trace_callback(trace)
    runtime.dispatcher.prepare(context, _prepare_request())
    raise RuntimeError("prepare crash checkpoint was not reached")


def _run_finalization_crash_probe(
    database: str,
    session_id: str,
    expected_session_version: str,
) -> None:
    clock = _Clock()
    clock.advance(seconds=30)
    dependencies, context = _dependencies(clock)
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            "authorization_runtime_finalization_crash"
        ),
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    manifest_inserted = False

    def trace(statement: str) -> None:
        nonlocal manifest_inserted
        if manifest_inserted:
            os.kill(os.getpid(), signal.SIGKILL)
        normalized = " ".join(statement.upper().split())
        if "INSERT INTO V3_REPLAY_MANIFESTS" in normalized:
            manifest_inserted = True

    runtime._connection.set_trace_callback(trace)
    runtime.dispatcher.finalize(
        context,
        DurableFinalizeRequest(
            session_id=session_id,
            expected_session_version=int(expected_session_version),
        ),
    )
    raise RuntimeError("finalization crash checkpoint was not reached")


def _run_semantic_provider_effect_crash_probe(
    database: str,
    session_id: str,
    checkpoint: str,
) -> None:
    if checkpoint not in {
        "before_provider",
        "before_submitted",
        "after_submitted",
        "after_receipt",
    }:
        raise ValueError("semantic provider crash checkpoint is invalid")
    clock = _Clock()
    clock.advance(seconds=30)
    provider_results: list[tbm.SemanticProviderResult] = []

    def invoke_provider(
        _call: tbm.SemanticProviderCall,
    ) -> tbm.SemanticProviderResult:
        if checkpoint == "before_provider":
            os.kill(os.getpid(), signal.SIGKILL)
        return provider_results[-1]

    dependencies, context = _dependencies(
        clock,
        semantic_provider_invoker=invoke_provider,
    )
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            f"authorization_provider_crash_{checkpoint}"
        ),
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    prepared = runtime.sessions.get(session_id)
    evaluation = runtime.evidence_repository.load_evaluation(
        prepared.system_gate_evaluation_id
    )
    provider_results.append(_provider_result(evaluation))
    if checkpoint in {"before_submitted", "after_submitted"}:
        original_append = getattr(
            tbm.SemanticProviderEffectService,
            "_append_transition",
        )

        def kill_around_submitted(
            service: tbm.SemanticProviderEffectService,
            reference: tbm.ProviderEffectTransitionRef,
            effect_id: str,
            *,
            provider_service: tbm.ProviderEffectLedgerService | None = None,
        ) -> tbm.ProviderEffectAppendResult:
            if (
                checkpoint == "before_submitted"
                and reference.stage == "request_submitted"
            ):
                os.kill(os.getpid(), signal.SIGKILL)
            result = original_append(
                service,
                reference,
                effect_id,
                provider_service=provider_service,
            )
            if (
                checkpoint == "after_submitted"
                and reference.stage == "request_submitted"
            ):
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(
            tbm.SemanticProviderEffectService,
            "_append_transition",
            kill_around_submitted,
        )
    if checkpoint == "after_receipt":
        original_invoke = tbm.SemanticProviderEffectService.invoke

        def kill_after_receipt(
            service: tbm.SemanticProviderEffectService,
            *,
            session_id: str,
            expected_previous_attempt_id: str | None,
            call: tbm.SemanticProviderCall,
            call_provider,
        ) -> tbm.SemanticProviderResult:
            result = original_invoke(
                service,
                session_id=session_id,
                expected_previous_attempt_id=expected_previous_attempt_id,
                call=call,
                call_provider=call_provider,
            )
            os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(
            tbm.SemanticProviderEffectService,
            "invoke",
            kill_after_receipt,
        )
    runtime.dispatcher.decide(
        context,
        _provider_context(),
        _decide_request(prepared, evaluation),
    )
    raise RuntimeError("semantic provider crash checkpoint was not reached")


def _run_completion_crash_probe(
    database: str,
    session_id: str,
) -> None:
    clock = _Clock()
    clock.advance(seconds=120)
    dependencies, context = _dependencies(clock)
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            "authorization_runtime_completion_crash"
        ),
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    executing = runtime.sessions.get(session_id)
    completion = _completion(executing)
    outbox_inserted = False

    def trace(statement: str) -> None:
        nonlocal outbox_inserted
        if outbox_inserted:
            os.kill(os.getpid(), signal.SIGKILL)
        normalized = " ".join(statement.upper().split())
        if "INSERT INTO V3_COMPLETION_OUTBOX_EVENTS" in normalized:
            outbox_inserted = True

    runtime._connection.set_trace_callback(trace)
    runtime.dispatcher.complete(
        context,
        EVALUATOR_CONTEXT,
        DurableCompleteRequest(
            session_id=completion.session_id,
            expected_session_version=completion.expected_version,
            result=completion.result,
            evidence_artifact_sha256s=list(completion.evidence_artifact_sha256s),
            output_sha256=completion.output_sha256,
            tool_outputs_sha256=completion.tool_outputs_sha256,
            latency_ms=completion.latency_ms,
            cost_usd=completion.cost_usd,
            error_code=completion.error_code,
        ),
    )
    raise RuntimeError("completion crash checkpoint was not reached")


def _run_outbox_ack_crash_probe(
    database: str,
    delivery_file: str,
) -> None:
    clock = _Clock()
    clock.advance(seconds=300)
    consumer_returned = False

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        nonlocal consumer_returned
        Path(delivery_file).write_text(event.event_id, encoding="utf-8")
        consumer_returned = True
        return tbm.CompletionOutboxConsumerReceipt(response_sha256="sha256:" + "a" * 64)

    dependencies, _context = _dependencies(
        clock,
        completion_consumer=consume,
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    acknowledgement_inserted = False

    def trace(statement: str) -> None:
        nonlocal acknowledgement_inserted
        if acknowledgement_inserted:
            os.kill(os.getpid(), signal.SIGKILL)
        normalized = " ".join(statement.upper().split())
        if (
            consumer_returned
            and "INSERT INTO V3_COMPLETION_OUTBOX_DELIVERY_REVISIONS" in normalized
        ):
            acknowledgement_inserted = True

    runtime._connection.set_trace_callback(trace)
    runtime.deliver_outbox(
        worker_id="worker_completion_ack_crash",
        lease_seconds=1,
        limit=1,
    )
    raise RuntimeError("outbox acknowledgement crash checkpoint was not reached")


def _run_post_commit_response_loss_probe(
    database: str,
    checkpoint: str,
    session_id: str,
    delivery_file: str,
) -> None:
    clock = _Clock()
    clock.advance(seconds=60)

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        Path(delivery_file).write_text(event.event_id, encoding="utf-8")
        return tbm.CompletionOutboxConsumerReceipt(response_sha256="sha256:" + "c" * 64)

    dependencies, context = _dependencies(
        clock,
        completion_consumer=(consume if checkpoint == "acknowledged" else None),
    )
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            f"authorization_runtime_post_commit_{checkpoint}"
        ),
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)

    if checkpoint in {"decided", "executing"}:
        original_transition = runtime.sessions.transition
        target_status = "decided" if checkpoint == "decided" else "executing"

        def transition_then_crash(*args, **kwargs):
            result = original_transition(*args, **kwargs)
            retained_target = args[1] if len(args) > 1 else kwargs["target_status"]
            if retained_target == target_status:
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(runtime.sessions, "transition", transition_then_crash)
    elif checkpoint == "finalized":
        original_finalization = runtime.replay_repository.store_complete_finalization

        def finalization_then_crash(*args, **kwargs):
            result = original_finalization(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(
            runtime.replay_repository,
            "store_complete_finalization",
            finalization_then_crash,
        )
    elif checkpoint == "completed":
        original_completion = runtime.outbox_repository.complete_session

        def completion_then_crash(*args, **kwargs):
            result = original_completion(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(
            runtime.outbox_repository,
            "complete_session",
            completion_then_crash,
        )
    elif checkpoint == "acknowledged":
        original_acknowledgement = runtime.outbox_repository.acknowledge

        def acknowledgement_then_crash(*args, **kwargs):
            result = original_acknowledgement(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(
            runtime.outbox_repository,
            "acknowledge",
            acknowledgement_then_crash,
        )
    else:
        raise ValueError("unsupported post-commit crash checkpoint")

    current = runtime.sessions.get(session_id)
    history = runtime.sessions.history(session_id)
    if checkpoint == "decided":
        prepared = next(item for item in history if item.status == "prepared")
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
    elif checkpoint == "finalized":
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=session_id,
                expected_session_version=current.version,
            ),
        )
    elif checkpoint == "executing":
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=session_id,
                expected_session_version=current.version,
            ),
        )
    elif checkpoint == "completed":
        completion = _completion(current)
        runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(completion.evidence_artifact_sha256s),
                output_sha256=completion.output_sha256,
                tool_outputs_sha256=completion.tool_outputs_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
                error_code=completion.error_code,
            ),
        )
    else:
        runtime.deliver_outbox(
            worker_id="worker_post_commit_ack",
            lease_seconds=60,
            limit=1,
        )
    raise RuntimeError("post-commit response-loss checkpoint was not reached")


def test_durable_sqlite_runtime_builds_one_restart_safe_authority_graph(
    tmp_path: Path,
) -> None:
    delivered: list[tbm.CompletionOutboxEvent] = []

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivered.append(event)
        return tbm.CompletionOutboxConsumerReceipt(response_sha256="sha256:" + "f" * 64)

    database = tmp_path / "durable-runtime.sqlite3"
    clock = _Clock()
    dependencies, context = _dependencies(
        clock,
        completion_consumer=consume,
    )
    factory = DurableRuntimeFactory(dependencies)
    runtime = factory.open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        assert runtime.sessions is runtime.outbox_repository.gate_sessions
        assert runtime.agent.service_bundle is runtime.service_bundle
        assert runtime.service_bundle.authority_graph is runtime.authority_graph
        assert runtime.authority_graph.authorization_service is (
            runtime.authorization_service
        )
        assert runtime.dispatcher.capabilities()["durable_sessions"] is True

        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = runtime.sessions.get(decided.session_id)
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        executing = runtime.sessions.get(finalized.session_id)
        completion = _completion(executing)
        completed_response = runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(completion.evidence_artifact_sha256s),
                output_sha256=completion.output_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
            ),
        )
        completed = runtime.sessions.get(executing.session_id)
        assert completed_response["result"]["session"]["status"] == "completed"

        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (completed.session_id,),
        ).fetchall()
        events = tuple(loads_canonical_event(row[0]) for row in event_rows)
        assert tuple(event.event_type for event in events) == (
            tbm.GATE_SESSION_CREATED_EVENT,
            tbm.GATE_SESSION_PREPARED_EVENT,
            tbm.SEMANTIC_GATE_REQUESTED_EVENT,
            tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
            tbm.SEMANTIC_GATE_DECIDED_EVENT,
            tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
            tbm.USAGE_DECISION_FINALIZED_EVENT,
            tbm.EXECUTION_STARTED_EVENT,
            tbm.GATE_SESSION_COMPLETED_EVENT,
        )
        authorization_ids = tuple(event.authorization_decision_id for event in events)
        assert authorization_ids[0] == authorization_ids[1]
        assert authorization_ids[2] == authorization_ids[3] == authorization_ids[4]
        assert authorization_ids[5] == authorization_ids[6]
        assert (
            len(
                {
                    authorization_ids[0],
                    authorization_ids[2],
                    authorization_ids[5],
                    authorization_ids[7],
                    authorization_ids[8],
                }
            )
            == 5
        )
        reducer = tbm.build_gate_session_reducer()
        state = reducer.initial_state()
        for event in events:
            state = execute_reducer_step(
                reducer,
                state,
                ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
            ).state
        tbm.verify_gate_session_projection_parity(state, (completed,))

        snapshot = runtime.evidence_repository.load_snapshot(
            completed.retrieval_snapshot_id
        )
        evidence_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id IN (?, ?) ORDER BY global_position",
            (
                snapshot.snapshot_id,
                evaluation.evaluation_id,
            ),
        ).fetchall()
        evidence_events = tuple(loads_canonical_event(row[0]) for row in evidence_rows)
        assert tuple(event.event_type for event in evidence_events) == (
            tbm.RETRIEVAL_PREPARED_EVENT,
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
        )
        evidence_reducer = tbm.build_gate_evidence_reducer()
        evidence_state = evidence_reducer.initial_state()
        for event in evidence_events:
            evidence_state = execute_reducer_step(
                evidence_reducer,
                evidence_state,
                ReducerEvent(
                    event,
                    DEFAULT_EVENT_TYPE_REGISTRY.consume(event),
                ),
            ).state
        tbm.verify_gate_evidence_projection_parity(
            evidence_state,
            (snapshot,),
            (evaluation,),
        )

        semantic_bundles = tuple(
            runtime.semantic_repository.load_attempt_with_artifacts(attempt_id)
            for attempt_id in completed.semantic_gate_attempt_ids
        )
        semantic_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (tbm.semantic_gate_attempt_stream_id(evaluation.evaluation_id),),
        ).fetchall()
        semantic_events = tuple(loads_canonical_event(row[0]) for row in semantic_rows)
        assert tuple(event.event_type for event in semantic_events) == (
            tbm.SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
        )
        assert semantic_events[0].causation_id == tbm.gate_evidence_event_id(
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
            evaluation.evaluation_id,
        )
        assert semantic_events[0].authorization_decision_id == (authorization_ids[2])
        semantic_reducer = tbm.build_semantic_gate_attempt_reducer()
        semantic_state = semantic_reducer.initial_state()
        for event in (evidence_events[1], *semantic_events):
            semantic_state = execute_reducer_step(
                semantic_reducer,
                semantic_state,
                ReducerEvent(
                    event,
                    DEFAULT_EVENT_TYPE_REGISTRY.consume(event),
                ),
            ).state
        tbm.verify_semantic_gate_attempt_projection_parity(
            semantic_state,
            semantic_bundles,
            (evidence_events[1], *semantic_events),
        )

        deliveries = runtime.deliver_outbox(worker_id="worker_runtime_01")
        assert len(deliveries) == 1
        assert deliveries[0].outcome == "delivered"
        assert [event.event_id for event in delivered] == [
            completed_response["result"]["outbox_event"]["event_id"]
        ]
        session_id = completed.session_id
        session_version = completed.version
    finally:
        runtime.close()

    with pytest.raises(DurableRuntimeV3Error) as raised:
        runtime.dispatcher.capabilities()
    assert raised.value.code == "TBM_DURABLE_RUNTIME_CLOSED"

    reopened = factory.open_sqlite(
        database,
        initialize=False,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        loaded = reopened.dispatcher.get_session(
            context,
            DurableGetSessionRequest(session_id=session_id),
        )
        assert loaded["result"]["session"]["version"] == session_version
        assert loaded["result"]["session"]["status"] == "completed"
        assert reopened.deliver_outbox(worker_id="worker_runtime_02") == ()
    finally:
        reopened.close()


def test_durable_sqlite_runtime_uses_trusted_provider_effect_invoker(
    tmp_path: Path,
) -> None:
    provider_calls: list[tbm.SemanticProviderCall] = []
    provider_results: list[tbm.SemanticProviderResult] = []

    def invoke_provider(
        call: tbm.SemanticProviderCall,
    ) -> tbm.SemanticProviderResult:
        provider_calls.append(call)
        return provider_results[-1]

    clock = _Clock()
    dependencies, context = _dependencies(
        clock,
        semantic_provider_invoker=invoke_provider,
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(
        tmp_path / "durable-provider-effect.sqlite3",
        initialize=True,
    )
    connection = runtime._connection
    with sqlite_event_ledger_v1._CONNECTION_LOCKS_GUARD:
        assert connection not in sqlite_event_ledger_v1._CONNECTION_LOCKS
    try:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        provider_result = _provider_result(evaluation)
        provider_results.append(provider_result)
        caller_request = _decide_request(prepared, evaluation).model_copy(
            update={
                "response_base64": base64.b64encode(
                    b'{"caller":"must not become provider evidence"}'
                ).decode("ascii"),
                "provider_request_id": "caller_provider_request",
                "decision_id": "caller_decision",
            }
        )

        decided_response = runtime.dispatcher.decide(
            context,
            _provider_context(),
            caller_request,
        )
        attempt_payload = decided_response["result"]["attempt"]
        assert attempt_payload["provider_request_id"] == (
            provider_result.provider_request_id
        )
        assert attempt_payload["decision_id"] == provider_result.decision_id
        assert len(provider_calls) == 1
        assert provider_calls[0].prompt == base64.b64decode(
            caller_request.prompt_base64
        )

        effect_id = tbm.semantic_provider_effect_id(
            session_id=prepared.session_id,
            system_gate_evaluation_id=evaluation.evaluation_id,
            expected_previous_attempt_id=None,
        )
        rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (tbm.effect_event_stream_id(effect_id),),
        ).fetchall()
        events = tuple(loads_canonical_event(row[0]) for row in rows)
        assert tuple(event.event_type for event in events) == (
            tbm.EFFECT_REQUESTED_EVENT,
            tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
            tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
            tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
        )
        assert tuple(event.actor_type for event in events) == (
            "agent_client",
            "service",
            "service",
            "service",
        )
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in events[1:]
        ) == (
            "attempt_started",
            "request_submitted",
            "receipt_recorded",
        )
        retained = runtime.semantic_repository.load_attempt_with_artifacts(
            attempt_payload["attempt_id"]
        )
        assert retained.response is not None
        assert retained.response.content == provider_result.response
        with sqlite_event_ledger_v1._CONNECTION_LOCKS_GUARD:
            assert connection not in sqlite_event_ledger_v1._CONNECTION_LOCKS
    finally:
        runtime.close()
    with sqlite_event_ledger_v1._CONNECTION_LOCKS_GUARD:
        assert connection not in sqlite_event_ledger_v1._CONNECTION_LOCKS


def test_durable_sqlite_provider_unknown_blocks_retry_without_reconciliation(
    tmp_path: Path,
) -> None:
    provider_calls: list[tbm.SemanticProviderCall] = []

    def timeout_provider(
        call: tbm.SemanticProviderCall,
    ) -> tbm.SemanticProviderResult:
        provider_calls.append(call)
        raise tbm.SemanticProviderCallError(
            "provider_timeout",
            provider_request_id="provider_request_unknown_001",
        )

    dependencies, context = _dependencies(
        _Clock(),
        semantic_provider_invoker=timeout_provider,
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(
        tmp_path / "durable-provider-unknown.sqlite3",
        initialize=True,
    )
    try:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )

        with pytest.raises(DurableAgentWireError) as first:
            runtime.dispatcher.decide(
                context,
                _provider_context(),
                _decide_request(prepared, evaluation),
            )
        assert first.value.category == "recovery"
        assert first.value.code == (
            "TBM_DURABLE_SEMANTIC_PROVIDER_EFFECT_RECOVERY_REQUIRED"
        )
        awaiting = runtime.sessions.get(prepared.session_id)
        assert awaiting.status == "awaiting_decision"
        assert runtime.semantic_repository.load_attempt_chain(
            evaluation.evaluation_id
        ) == ()

        with pytest.raises(DurableAgentWireError) as replay:
            runtime.dispatcher.decide(
                context,
                _provider_context(),
                _decide_request(awaiting, evaluation),
            )
        assert replay.value.category == "recovery"
        assert len(provider_calls) == 1

        effect_id = tbm.semantic_provider_effect_id(
            session_id=prepared.session_id,
            system_gate_evaluation_id=evaluation.evaluation_id,
            expected_previous_attempt_id=None,
        )
        rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (tbm.effect_event_stream_id(effect_id),),
        ).fetchall()
        events = tuple(loads_canonical_event(row[0]) for row in rows)
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in events[1:]
        ) == (
            "attempt_started",
            "request_submitted",
            "result_unknown",
        )
    finally:
        runtime.close()


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash provider probes require SIGKILL",
)
@pytest.mark.parametrize(
    ("checkpoint", "expected_stages"),
    [
        (
            "before_provider",
            ("attempt_started",),
        ),
        (
            "before_submitted",
            ("attempt_started",),
        ),
        (
            "after_submitted",
            (
                "attempt_started",
                "request_submitted",
            ),
        ),
        (
            "after_receipt",
            (
                "attempt_started",
                "request_submitted",
                "receipt_recorded",
            ),
        ),
    ],
)
def test_durable_sqlite_provider_effect_hard_kill_requires_reconciliation(
    tmp_path: Path,
    checkpoint: str,
    expected_stages: tuple[str, ...],
) -> None:
    database = tmp_path / f"durable-provider-{checkpoint}.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )

    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_semantic_provider_effect_crash_probe; "
        "_run_semantic_provider_effect_crash_probe("
        "sys.argv[1], sys.argv[2], sys.argv[3])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            prepared.session_id,
            checkpoint,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    provider_calls: list[tbm.SemanticProviderCall] = []
    reconciliations: list[tbm.SemanticProviderReconciliationCall] = []

    def invoke_again(
        call: tbm.SemanticProviderCall,
    ) -> tbm.SemanticProviderResult:
        provider_calls.append(call)
        pytest.fail("reconciliation must not invoke the provider again")

    def reconcile(
        call: tbm.SemanticProviderReconciliationCall,
    ) -> tbm.SemanticProviderReconciliationResult:
        reconciliations.append(call)
        if checkpoint != "before_provider":
            return tbm.SemanticProviderReconciliationResult(
                "confirmed",
                _provider_result(evaluation),
            )
        return tbm.SemanticProviderReconciliationResult("still_unknown")

    recovery_clock = _Clock()
    recovery_clock.advance(seconds=90)
    recovery_dependencies, recovery_context = _dependencies(
        recovery_clock,
        semantic_provider_invoker=invoke_again,
        semantic_provider_reconciler=reconcile,
    )
    recovery_authorizations = iter(range(1, 100))
    recovery_dependencies = replace(
        recovery_dependencies,
        authorization_request_id_factory=lambda: (
            f"authorization_provider_recovery_{checkpoint}_"
            f"{next(recovery_authorizations):02d}"
        ),
    )
    with DurableRuntimeFactory(recovery_dependencies).open_sqlite(
        database
    ) as recovered:
        awaiting = recovered.sessions.get(prepared.session_id)
        assert awaiting.status == "awaiting_decision"
        decision_request = _decide_request(awaiting, evaluation)
        if checkpoint != "after_receipt":
            with pytest.raises(DurableAgentWireError) as raised:
                recovered.dispatcher.decide(
                    recovery_context,
                    _provider_context(),
                    decision_request,
                )
            assert raised.value.category == "recovery"
            assert recovered.semantic_repository.load_attempt_chain(
                evaluation.evaluation_id
            ) == ()
        else:
            decided = recovered.dispatcher.decide(
                recovery_context,
                _provider_context(),
                decision_request,
            )
            assert decided["result"]["session"]["status"] == "decided"
            attempt_id = decided["result"]["attempt"]["attempt_id"]
            retained = recovered.semantic_repository.load_attempt_with_artifacts(
                attempt_id
            )
            assert retained.response is not None
            assert retained.response.content == _provider_result(evaluation).response
            replayed = recovered.dispatcher.decide(
                recovery_context,
                _provider_context(),
                decision_request,
            )
            assert replayed["result"]["replayed"] is True
        assert provider_calls == []
        assert len(reconciliations) == (
            1 if checkpoint == "after_receipt" else 0
        )

        effect_id = tbm.semantic_provider_effect_id(
            session_id=prepared.session_id,
            system_gate_evaluation_id=evaluation.evaluation_id,
            expected_previous_attempt_id=None,
        )
        rows = recovered._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (tbm.effect_event_stream_id(effect_id),),
        ).fetchall()
        events = tuple(loads_canonical_event(row[0]) for row in rows)
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in events[1:]
        ) == expected_stages


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
@pytest.mark.parametrize(
    ("checkpoint", "expected_session_count", "expected_evidence_count"),
    [
        ("authorization", 0, 0),
        ("created", 1, 0),
        ("evidence", 1, 1),
    ],
)
def test_durable_sqlite_prepare_recovers_after_committed_crash_boundaries(
    tmp_path: Path,
    checkpoint: str,
    expected_session_count: int,
    expected_evidence_count: int,
) -> None:
    database = tmp_path / f"prepare-crash-{checkpoint}.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
    ):
        pass

    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_prepare_crash_probe; "
        "_run_prepare_crash_probe(sys.argv[1], sys.argv[2])"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(database), checkpoint],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    recovery_clock = _Clock()
    recovery_clock.advance(seconds=30)
    dependencies, context = _dependencies(recovery_clock)
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            f"authorization_runtime_prepare_recovery_{checkpoint}"
        ),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(database) as runtime:
        connection = runtime._connection
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_authorization_decisions"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM gate_session_heads"
        ).fetchone() == (expected_session_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_retrieval_snapshots"
        ).fetchone() == (expected_evidence_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_system_gate_evaluations"
        ).fetchone() == (expected_evidence_count,)

        response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        session = runtime.sessions.get(response["result"]["session"]["session_id"])
        assert session.status == "prepared"
        assert [
            revision.status for revision in runtime.sessions.history(session.session_id)
        ] == ["created", "prepared"]
        assert connection.execute(
            "SELECT COUNT(*) FROM gate_session_heads"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_retrieval_snapshots"
        ).fetchone() == ((2 if checkpoint == "evidence" else 1),)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_system_gate_evaluations"
        ).fetchone() == ((2 if checkpoint == "evidence" else 1),)
        authorization_rows = connection.execute(
            "SELECT authorization_event_id FROM v3_authorization_decisions "
            "ORDER BY decided_at, authorization_event_id"
        ).fetchall()
        assert len(authorization_rows) == 2
        snapshot_rows = connection.execute(
            "SELECT snapshot_id, authorization_event_id "
            "FROM v3_retrieval_snapshots ORDER BY snapshot_id"
        ).fetchall()
        snapshot_authorizations = dict(snapshot_rows)
        assert session.retrieval_snapshot_id in snapshot_authorizations
        if checkpoint == "evidence":
            assert len(frozenset(snapshot_authorizations.values())) == 2
        assert snapshot_authorizations[session.retrieval_snapshot_id] in {
            row[0] for row in authorization_rows
        }

        event_rows = connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (session.session_id,),
        ).fetchall()
        events = tuple(loads_canonical_event(row[0]) for row in event_rows)
        assert tuple(event.event_type for event in events) == (
            tbm.GATE_SESSION_CREATED_EVENT,
            tbm.GATE_SESSION_PREPARED_EVENT,
        )


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
def test_durable_sqlite_finalization_recovers_after_replay_transaction_crash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "finalization-replay-crash.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        finalize_request = DurableFinalizeRequest(
            session_id=decided.session_id,
            expected_session_version=decided.version,
        )

    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_finalization_crash_probe; "
        "_run_finalization_crash_probe(sys.argv[1], sys.argv[2], sys.argv[3])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            decided.session_id,
            str(decided.version),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    clock = _Clock()
    clock.advance(seconds=120)
    dependencies, context = _dependencies(clock)
    recovery_request_ids = iter(
        (
            "authorization_runtime_finalization_recovery_001",
            "authorization_runtime_finalization_recovery_002",
        )
    )
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: next(recovery_request_ids),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        current = runtime.sessions.get(decided.session_id)
        assert current.status == "decided"
        assert current.version == decided.version + 1
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_artifacts"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_injections"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_manifests"
        ).fetchone() == (0,)

        recovered = runtime.dispatcher.finalize(
            context,
            finalize_request,
        )
        assert recovered["result"]["session"]["status"] == "finalized"
        assert recovered["result"]["replayed"] is False
        replayed = runtime.dispatcher.finalize(
            context,
            finalize_request,
        )
        assert replayed["result"]["replayed"] is True
        assert replayed["result"]["manifest"] == recovered["result"]["manifest"]
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_injections"
        ).fetchone() == (1,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_manifests"
        ).fetchone() == (1,)

        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "ORDER BY global_position"
        ).fetchall()
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        assert event_types.count(tbm.USAGE_DECISION_FINALIZED_EVENT) == 1
        assert event_types.count(tbm.INJECTION_RENDERED_EVENT) == 1


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
def test_durable_sqlite_completion_rolls_back_partial_outbox_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion-outbox-crash.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = runtime.sessions.get(decided.session_id)
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        executing = runtime.sessions.get(finalized.session_id)
        completion = _completion(executing)
        complete_request = DurableCompleteRequest(
            session_id=completion.session_id,
            expected_session_version=completion.expected_version,
            result=completion.result,
            evidence_artifact_sha256s=list(completion.evidence_artifact_sha256s),
            output_sha256=completion.output_sha256,
            tool_outputs_sha256=completion.tool_outputs_sha256,
            latency_ms=completion.latency_ms,
            cost_usd=completion.cost_usd,
            error_code=completion.error_code,
        )

    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_completion_crash_probe; "
        "_run_completion_crash_probe(sys.argv[1], sys.argv[2])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            executing.session_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    clock = _Clock()
    clock.advance(seconds=180)
    dependencies, context = _dependencies(clock)
    recovery_request_ids = iter(
        (
            "authorization_runtime_completion_recovery_001",
            "authorization_runtime_completion_recovery_002",
        )
    )
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: next(recovery_request_ids),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(database) as runtime:
        current = runtime.sessions.get(executing.session_id)
        assert current == executing
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_run_outcomes"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_completion_outbox_events"
        ).fetchone() == (0,)

        completed = runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            complete_request,
        )
        assert completed["result"]["session"]["status"] == "completed"
        assert completed["result"]["replayed"] is False
        replayed = runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            complete_request,
        )
        assert replayed["result"]["replayed"] is True
        assert replayed["result"]["outcome"] == completed["result"]["outcome"]
        assert replayed["result"]["outbox_event"] == completed["result"]["outbox_event"]
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_run_outcomes"
        ).fetchone() == (1,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_completion_outbox_events"
        ).fetchone() == (1,)

        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "ORDER BY global_position"
        ).fetchall()
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        assert event_types.count(tbm.EVALUATION_AUTHENTICATED_EVENT) == 1
        assert event_types.count(tbm.RUN_OUTCOME_RECORDED_EVENT) == 1
        assert event_types.count(tbm.GATE_SESSION_COMPLETED_EVENT) == 1
        assert event_types.count(tbm.EFFECT_REQUESTED_EVENT) == 1
        event_id = completed["result"]["outbox_event"]["event_id"]

    delivery_file = tmp_path / "completion-consumer-before-ack.txt"
    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_outbox_ack_crash_probe; "
        "_run_outbox_ack_crash_probe(sys.argv[1], sys.argv[2])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            str(delivery_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    assert delivery_file.read_text(encoding="utf-8") == event_id

    redelivered: list[str] = []

    def consume_reclaimed(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        redelivered.append(event.event_id)
        return tbm.CompletionOutboxConsumerReceipt(response_sha256="sha256:" + "b" * 64)

    clock = _Clock()
    clock.advance(seconds=360)
    dependencies, _context = _dependencies(
        clock,
        completion_consumer=consume_reclaimed,
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(database) as runtime:
        leased = runtime.outbox_repository.get_delivery(event_id)
        assert leased.status == "leased"
        assert leased.attempt_count == 1
        results = runtime.deliver_outbox(
            worker_id="worker_completion_ack_recovery",
            lease_seconds=1,
            limit=1,
        )
        assert len(results) == 1
        assert results[0].outcome == "delivered"
        assert redelivered == [event_id]
        history = runtime.outbox_repository.list_delivery_history(event_id)
        assert tuple(item.status for item in history) == (
            "pending",
            "leased",
            "leased",
            "delivered",
        )
        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "ORDER BY global_position"
        ).fetchall()
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        assert event_types.count(tbm.EFFECT_REQUESTED_EVENT) == 1
        assert event_types.count(tbm.EFFECT_STARTED_EVENT) == 2
        assert event_types.count(tbm.EFFECT_SUCCEEDED_EVENT) == 1


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
@pytest.mark.parametrize(
    ("checkpoint", "expected_status", "unique_event_types"),
    [
        (
            "decided",
            "decided",
            (
                tbm.SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
                tbm.SEMANTIC_GATE_DECIDED_EVENT,
            ),
        ),
        (
            "finalized",
            "finalized",
            (
                tbm.USAGE_DECISION_FINALIZED_EVENT,
                tbm.INJECTION_RENDERED_EVENT,
            ),
        ),
        (
            "executing",
            "executing",
            (tbm.EXECUTION_STARTED_EVENT,),
        ),
        (
            "completed",
            "completed",
            (
                tbm.EVALUATION_AUTHENTICATED_EVENT,
                tbm.RUN_OUTCOME_RECORDED_EVENT,
                tbm.GATE_SESSION_COMPLETED_EVENT,
                tbm.EFFECT_REQUESTED_EVENT,
            ),
        ),
    ],
)
def test_durable_sqlite_replays_after_committed_response_loss(
    tmp_path: Path,
    checkpoint: str,
    expected_status: str,
    unique_event_types: tuple[str, ...],
) -> None:
    database = tmp_path / f"post-commit-{checkpoint}.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        if checkpoint != "decided":
            evaluation = runtime.evidence_repository.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            runtime.dispatcher.decide(
                context,
                _provider_context(),
                _decide_request(prepared, evaluation),
            )
        current = runtime.sessions.get(prepared.session_id)
        if checkpoint in {"executing", "completed"}:
            runtime.dispatcher.finalize(
                context,
                DurableFinalizeRequest(
                    session_id=current.session_id,
                    expected_session_version=current.version,
                ),
            )
            current = runtime.sessions.get(current.session_id)
        if checkpoint == "completed":
            runtime.dispatcher.start(
                context,
                DurableStartRequest(
                    session_id=current.session_id,
                    expected_session_version=current.version,
                ),
            )
            current = runtime.sessions.get(current.session_id)
        session_id = current.session_id

    delivery_file = tmp_path / f"post-commit-{checkpoint}.txt"
    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_post_commit_response_loss_probe; "
        "_run_post_commit_response_loss_probe("
        "sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            checkpoint,
            session_id,
            str(delivery_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    clock = _Clock()
    clock.advance(seconds=120)
    request_ids = iter(
        f"authorization_runtime_post_commit_recovery_{checkpoint}_{index:02d}"
        for index in range(1, 10)
    )
    dependencies, context = _dependencies(clock)
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: next(request_ids),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        current = runtime.sessions.get(session_id)
        assert current.status == expected_status
        history = runtime.sessions.history(session_id)

        def retained_state():
            return (
                tuple(
                    runtime._connection.execute(
                        "SELECT canonical_event FROM v3_event_ledger_events "
                        "ORDER BY global_position"
                    ).fetchall()
                ),
                runtime._connection.execute(
                    "SELECT current_global_position, current_event_id, "
                    "current_event_sha256 FROM v3_event_ledger_global_head "
                    "WHERE singleton = 1"
                ).fetchone(),
                tuple(
                    runtime._connection.execute(
                        "SELECT stream_id, current_stream_version, "
                        "current_event_id, current_event_sha256 "
                        "FROM v3_event_ledger_stream_heads ORDER BY stream_id"
                    ).fetchall()
                ),
                runtime.sessions.history(session_id),
            )

        def replay_once():
            if checkpoint == "decided":
                parent = next(item for item in history if item.status == "prepared")
                evaluation = runtime.evidence_repository.load_evaluation(
                    parent.system_gate_evaluation_id
                )
                return runtime.dispatcher.decide(
                    context,
                    _provider_context(),
                    _decide_request(parent, evaluation),
                )
            if checkpoint == "finalized":
                parent = next(item for item in history if item.status == "decided")
                return runtime.dispatcher.finalize(
                    context,
                    DurableFinalizeRequest(
                        session_id=session_id,
                        expected_session_version=parent.version,
                    ),
                )
            if checkpoint == "executing":
                parent = next(item for item in history if item.status == "finalized")
                return runtime.dispatcher.start(
                    context,
                    DurableStartRequest(
                        session_id=session_id,
                        expected_session_version=parent.version,
                    ),
                )
            parent = next(item for item in history if item.status == "executing")
            completion = _completion(parent)
            return runtime.dispatcher.complete(
                context,
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

        before = retained_state()
        first = replay_once()
        assert first["result"]["replayed"] is True
        assert retained_state() == before
        second = replay_once()
        assert second["result"]["replayed"] is True
        assert retained_state() == before
        first_result = dict(first["result"])
        second_result = dict(second["result"])
        first_result.pop("transition_authorization_event_id", None)
        second_result.pop("transition_authorization_event_id", None)
        assert second_result == first_result

        event_rows = before[0]
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        for event_type in unique_event_types:
            assert event_types.count(event_type) == 1


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
def test_durable_sqlite_does_not_redeliver_after_committed_ack_response_loss(
    tmp_path: Path,
) -> None:
    database = tmp_path / "post-commit-acknowledgement.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = runtime.sessions.get(decided.session_id)
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        executing = runtime.sessions.get(finalized.session_id)
        completion = _completion(executing)
        completed = runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(completion.evidence_artifact_sha256s),
                output_sha256=completion.output_sha256,
                tool_outputs_sha256=completion.tool_outputs_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
                error_code=completion.error_code,
            ),
        )
        session_id = executing.session_id
        event_id = completed["result"]["outbox_event"]["event_id"]

    delivery_file = tmp_path / "post-commit-acknowledgement.txt"
    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_post_commit_response_loss_probe; "
        "_run_post_commit_response_loss_probe("
        "sys.argv[1], 'acknowledged', sys.argv[2], sys.argv[3])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            session_id,
            str(delivery_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    assert delivery_file.read_text(encoding="utf-8") == event_id

    redelivered: list[str] = []

    def consume_again(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        redelivered.append(event.event_id)
        return tbm.CompletionOutboxConsumerReceipt()

    clock = _Clock()
    clock.advance(seconds=120)
    dependencies, _context = _dependencies(
        clock,
        completion_consumer=consume_again,
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(database) as runtime:
        assert (
            runtime.deliver_outbox(
                worker_id="worker_post_commit_ack_recovery",
                limit=1,
            )
            == ()
        )
        assert redelivered == []
        delivery = runtime.outbox_repository.get_delivery(event_id)
        assert delivery.status == "delivered"
        history = runtime.outbox_repository.list_delivery_history(event_id)
        assert tuple(item.status for item in history) == (
            "pending",
            "leased",
            "delivered",
        )
        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "ORDER BY global_position"
        ).fetchall()
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        assert event_types.count(tbm.EFFECT_REQUESTED_EVENT) == 1
        assert event_types.count(tbm.EFFECT_STARTED_EVENT) == 1
        assert event_types.count(tbm.EFFECT_SUCCEEDED_EVENT) == 1


def test_durable_sqlite_runtime_recovery_worker_expires_due_preparation() -> None:
    clock = _Clock()
    dependencies, context = _dependencies(clock)
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        payload = _prepare_request().model_dump()
        payload["expires_in_seconds"] = 300
        prepared_response = runtime.dispatcher.prepare(
            context,
            tbm.durable_agent_wire_v1.DurablePrepareRequest.model_validate(payload),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        assert prepared.status == "prepared"

        clock.advance(seconds=600)
        recovered = runtime.recover_due(limit=10)

        assert len(recovered) == 1
        assert recovered[0].session_id == prepared.session_id
        assert recovered[0].outcome == "expired"
        assert recovered[0].current.status == "expired"


def test_durable_sqlite_runtime_fails_closed_on_missing_schema(
    tmp_path: Path,
) -> None:
    dependencies, _context = _dependencies(_Clock())
    with pytest.raises(DurableRuntimeV3Error) as raised:
        DurableRuntimeFactory(dependencies).open_sqlite(
            tmp_path / "missing-schema.sqlite3",
            initialize=False,
        )
    assert raised.value.code == ("TBM_DURABLE_RUNTIME_SQLITE_SCHEMA_INVALID")


def test_durable_sqlite_runtime_requires_explicit_outbox_consumer() -> None:
    dependencies, _context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        with pytest.raises(DurableRuntimeV3Error) as raised:
            runtime.deliver_outbox(worker_id="worker_missing_consumer")
        assert raised.value.code == ("TBM_DURABLE_RUNTIME_OUTBOX_CONSUMER_MISSING")


def test_durable_sqlite_runtime_dependency_guards() -> None:
    dependencies, _context = _dependencies(_Clock())
    with pytest.raises(TypeError):
        DurableRuntimeDependencies(
            **{
                **dependencies.__dict__,
                "repository_id_resolver": None,
            }
        )
    with pytest.raises(TypeError):
        DurableRuntimeFactory(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        DurableRuntimeDependencies(
            **{
                **dependencies.__dict__,
                "semantic_provider_invoker": object(),
            }
        )
    with pytest.raises(ValueError):
        DurableRuntimeDependencies(
            **{
                **dependencies.__dict__,
                "semantic_provider_reconciler": lambda _call: None,
            }
        )
    with pytest.raises(ValueError):
        DurableRuntimeDependencies(
            **{
                **dependencies.__dict__,
                "semantic_provider_effect_actor_id": "invalid actor",
            }
        )

    assert EVALUATOR.status == "active"


def test_durable_postgres_runtime_parity_and_catalog_verification(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    postgres_cluster.load_schema()
    for script in (
        "postgres-v3-authorization.sql",
        "postgres-v3-gate-session.sql",
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
        "postgres-v3-replay.sql",
        "postgres-v3-outcome.sql",
        "postgres-v3-completion-outbox.sql",
        "postgres-v3-event-ledger.sql",
    ):
        installed = postgres_cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr

    delivered: list[tbm.CompletionOutboxEvent] = []
    provider_results: list[tbm.SemanticProviderResult] = []

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivered.append(event)
        return tbm.CompletionOutboxConsumerReceipt()

    def invoke_provider(
        _call: tbm.SemanticProviderCall,
    ) -> tbm.SemanticProviderResult:
        return provider_results[-1]

    dependencies, context = _dependencies(
        _Clock(),
        completion_consumer=consume,
        semantic_provider_invoker=invoke_provider,
    )
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        runtime = DurableRuntimeFactory(dependencies).bind_postgres(
            connection,
            expose_injection_content=True,
        )
        try:
            assert connection.info.transaction_status.name == "IDLE"
            assert runtime.sessions is runtime.outbox_repository.gate_sessions
            assert runtime.dispatcher.capabilities()["storage_mode"] == ("postgres")

            prepared_response = runtime.dispatcher.prepare(
                context,
                _prepare_request(),
            )
            prepared = runtime.sessions.get(
                prepared_response["result"]["session"]["session_id"]
            )
            evaluation = runtime.evidence_repository.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            provider_result = _provider_result(evaluation)
            provider_results.append(provider_result)
            runtime.dispatcher.decide(
                context,
                _provider_context(),
                _decide_request(prepared, evaluation),
            )
            decided = runtime.sessions.get(prepared.session_id)
            runtime.dispatcher.finalize(
                context,
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                ),
            )
            finalized = runtime.sessions.get(decided.session_id)
            runtime.dispatcher.start(
                context,
                DurableStartRequest(
                    session_id=finalized.session_id,
                    expected_session_version=finalized.version,
                ),
            )
            executing = runtime.sessions.get(finalized.session_id)
            completion = _completion(executing)
            completed = runtime.dispatcher.complete(
                context,
                EVALUATOR_CONTEXT,
                DurableCompleteRequest(
                    session_id=completion.session_id,
                    expected_session_version=completion.expected_version,
                    result=completion.result,
                    evidence_artifact_sha256s=list(
                        completion.evidence_artifact_sha256s
                    ),
                    output_sha256=completion.output_sha256,
                    latency_ms=completion.latency_ms,
                    cost_usd=completion.cost_usd,
                ),
            )
            assert completed["result"]["session"]["status"] == "completed"

            results = runtime.deliver_outbox(worker_id="worker_postgres_runtime")
            assert len(results) == 1
            assert results[0].outcome == "delivered"
            assert [event.event_id for event in delivered] == [
                completed["result"]["outbox_event"]["event_id"]
            ]
            effect_id = tbm.semantic_provider_effect_id(
                session_id=prepared.session_id,
                system_gate_evaluation_id=evaluation.evaluation_id,
                expected_previous_attempt_id=None,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT canonical_event FROM "
                    "trace_backed_memory_v3_event_ledger.events "
                    "WHERE stream_id = %s ORDER BY stream_version",
                    (tbm.effect_event_stream_id(effect_id),),
                )
                effect_events = tuple(
                    loads_canonical_event(row[0]) for row in cursor.fetchall()
                )
            assert tuple(event.event_type for event in effect_events) == (
                tbm.EFFECT_REQUESTED_EVENT,
                tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
                tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
                tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
            )
        finally:
            runtime.close()

    dependencies, _context = _dependencies(_Clock())
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA trace_backed_memory_v3_replay CASCADE")
        connection.commit()
        with pytest.raises(DurableRuntimeV3Error) as raised:
            DurableRuntimeFactory(dependencies).bind_postgres(connection)
        assert raised.value.code == ("TBM_DURABLE_RUNTIME_POSTGRES_SCHEMA_INVALID")
