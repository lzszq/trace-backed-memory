from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
from typing import Iterator, cast

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

import trace_backed_memory as tbm
from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_http_sdk import LIFECYCLE_FIXTURE, TOKEN
from tests.test_durable_retrieval_preparation_v3 import _durable_request
from tests.test_durable_runtime_v3 import _Clock, _dependencies
from tests.test_retrieval_preparation_v3 import _candidate
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
    _provider_result,
    _request as _semantic_request,
)
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableResumeRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_http_server import (
    DurableAgentHTTPServer,
    DurableHTTPAuthenticatedContexts,
    DurableHTTPAuthenticationRequest,
    DurableHTTPServerConfiguration,
)
from trace_backed_memory.durable_mcp_entry import DurableMCPApplication
from trace_backed_memory.durable_mcp_server import (
    DurableMCPTrustedContexts,
)
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeFactory,
)
from trace_backed_memory.durable_sdk import (
    AsyncDurableAgentHTTPClient,
    DurableAgentHTTPClient,
)
from trace_backed_memory.event_registry_v1 import (
    DEFAULT_EVENT_TYPE_REGISTRY,
)
from trace_backed_memory.event_v1 import loads_canonical_event
from trace_backed_memory.reducer import ReducerEvent, execute_reducer_step


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "durable_client_lifecycle.json"
TYPESCRIPT_DRIVER = ROOT / "tests" / "typescript_durable_transport_parity.ts"


@dataclass(frozen=True)
class _LifecycleSignature:
    events: tuple[tuple[int, str, int, str, str, int, str, str], ...]
    projections: tuple[tuple[str, str], ...]
    global_head: tuple[int, str, str]
    stream_heads: tuple[tuple[str, int, str, str], ...]


def create_transport_parity_application() -> DurableMCPApplication:
    dependencies, context = _dependencies(
        _Clock(),
        semantic_provider_invoker=lambda _call: _trusted_provider_result(),
    )
    return DurableMCPApplication(
        dependencies,
        DurableMCPTrustedContexts(
            context,
            _provider_context(),
            EVALUATOR_CONTEXT,
        ),
    )


def _parity_completion(session: tbm.GateSession) -> tbm.GateCompletionRequest:
    return replace(_completion(session), cost_usd=None)


def _complete_request(session: tbm.GateSession) -> DurableCompleteRequest:
    completion = _parity_completion(session)
    return DurableCompleteRequest(
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


def _trusted_provider_result() -> tbm.SemanticProviderResult:
    candidate = _candidate("memory_durable_runtime")
    return tbm.SemanticProviderResult(
        response=b'{"decision":"allow"}',
        provider_request_id="provider_request_transport_001",
        decision_id="decision_transport_allow",
        final_allowed_revision_ids=(candidate.revision.revision_id,),
        final_blocked_revision_ids=(),
        reason="The trusted provider retained the applicable revision.",
        risk="low",
        recommended_injection="summary",
    )


def _reduce(reducer, events):
    state = reducer.initial_state()
    projection_sha256 = ""
    for event in events:
        step = execute_reducer_step(
            reducer,
            state,
            ReducerEvent(
                event,
                (
                    None
                    if reducer.descriptor.envelope_only
                    else DEFAULT_EVENT_TYPE_REGISTRY.consume(event)
                ),
            ),
        )
        state = step.state
        projection_sha256 = step.state_sha256
    assert projection_sha256
    return state, projection_sha256


def _signature(runtime, session_id: str) -> _LifecycleSignature:
    completed = runtime.sessions.get(session_id)
    rows = runtime._connection.execute(
        "SELECT canonical_event FROM v3_event_ledger_events ORDER BY global_position",
    ).fetchall()
    events = tuple(loads_canonical_event(row[0]) for row in rows)
    assert tuple(event.global_position for event in events) == tuple(range(1, 22))
    assert tuple(event.event_type for event in events) == (
        tbm.GATE_SESSION_CREATED_EVENT,
        tbm.RETRIEVAL_PREPARED_EVENT,
        tbm.SYSTEM_GATE_EVALUATED_EVENT,
        tbm.GATE_SESSION_PREPARED_EVENT,
        tbm.SEMANTIC_GATE_REQUESTED_EVENT,
        tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
        tbm.EFFECT_REQUESTED_EVENT,
        tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
        tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
        tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
        tbm.SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
        tbm.SEMANTIC_GATE_DECIDED_EVENT,
        tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
        tbm.USAGE_DECISION_FINALIZED_EVENT,
        tbm.INJECTION_RENDERED_EVENT,
        tbm.EXECUTION_STARTED_EVENT,
        tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
        tbm.EVALUATION_AUTHENTICATED_EVENT,
        tbm.RUN_OUTCOME_RECORDED_EVENT,
        tbm.GATE_SESSION_COMPLETED_EVENT,
        tbm.EFFECT_REQUESTED_EVENT,
    )

    projections: list[tuple[str, str]] = []
    inventory = tbm.build_event_inventory_reducer()
    _inventory_state, inventory_sha256 = _reduce(inventory, events)
    projections.append((inventory.descriptor.reducer_id, inventory_sha256))

    session_events = tuple(
        event for event in events if event.event_type in tbm.GATE_SESSION_EVENT_TYPES
    )
    session_reducer = tbm.build_gate_session_reducer()
    session_state, session_sha256 = _reduce(session_reducer, session_events)
    tbm.verify_gate_session_projection_parity(session_state, (completed,))
    projections.append((session_reducer.descriptor.reducer_id, session_sha256))

    snapshot = runtime.evidence_repository.load_snapshot(
        completed.retrieval_snapshot_id
    )
    evaluation = runtime.evidence_repository.load_evaluation(
        completed.system_gate_evaluation_id
    )
    evidence_events = tuple(
        event
        for event in events
        if event.event_type
        in {tbm.RETRIEVAL_PREPARED_EVENT, tbm.SYSTEM_GATE_EVALUATED_EVENT}
    )
    evidence_reducer = tbm.build_gate_evidence_reducer()
    evidence_state, evidence_sha256 = _reduce(
        evidence_reducer,
        evidence_events,
    )
    tbm.verify_gate_evidence_projection_parity(
        evidence_state,
        (snapshot,),
        (evaluation,),
    )
    projections.append((evidence_reducer.descriptor.reducer_id, evidence_sha256))

    semantic_events = tuple(
        event
        for event in events
        if event.event_type
        in {
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
            tbm.SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
        }
    )
    semantic_bundles = tuple(
        runtime.semantic_repository.load_attempt_with_artifacts(attempt_id)
        for attempt_id in completed.semantic_gate_attempt_ids
    )
    semantic_reducer = tbm.build_semantic_gate_attempt_reducer()
    semantic_state, semantic_sha256 = _reduce(
        semantic_reducer,
        semantic_events,
    )
    tbm.verify_semantic_gate_attempt_projection_parity(
        semantic_state,
        semantic_bundles,
        semantic_events,
    )
    projections.append((semantic_reducer.descriptor.reducer_id, semantic_sha256))

    finalization_events = tuple(
        event
        for event in events
        if event.event_type
        in {tbm.USAGE_DECISION_FINALIZED_EVENT, tbm.INJECTION_RENDERED_EVENT}
    )
    finalization_ref = tbm.parse_injection_rendered_event(finalization_events[1])
    supporting_ids = tuple(
        dict.fromkeys(
            artifact_id
            for role, artifact_id in finalization_ref.artifact_roles
            if role != "injection_artifact"
        )
    )
    supporting_artifacts = tuple(
        runtime.replay_repository.load_artifact(artifact_id)
        for artifact_id in supporting_ids
    )
    injection, injection_content = runtime.replay_repository.load_injection(
        finalization_ref.injection.artifact.artifact_id
    )
    manifest = runtime.replay_repository.load_manifest(
        finalization_ref.replay_manifest_sha256
    )
    finalized_session = next(
        session
        for session in runtime.sessions.history(session_id)
        if session.status == "finalized"
    )
    finalization_reducer = tbm.build_finalization_reducer()
    finalization_state, finalization_sha256 = _reduce(
        finalization_reducer,
        finalization_events,
    )
    tbm.verify_finalization_projection_parity(
        finalization_state,
        (
            tbm.FinalizationProjectionAuthority(
                finalized_session,
                finalization_ref.usage_decision,
                supporting_artifacts,
                injection,
                injection_content,
                manifest,
            ),
        ),
        finalization_events,
    )
    projections.append(
        (finalization_reducer.descriptor.reducer_id, finalization_sha256)
    )

    outcome_events = tuple(
        event
        for event in events
        if event.event_type
        in {tbm.EVALUATION_AUTHENTICATED_EVENT, tbm.RUN_OUTCOME_RECORDED_EVENT}
    )
    evaluator = tbm.parse_evaluation_authenticated_event(outcome_events[0]).evaluator
    outcome = runtime.outbox_repository.outcomes.get_outcome(
        tbm.parse_run_outcome_recorded_event(outcome_events[1]).outcome.run_outcome_id
    )
    outcome_reducer = tbm.build_outcome_current_reducer()
    outcome_state, outcome_sha256 = _reduce(outcome_reducer, outcome_events)
    tbm.verify_outcome_projection_parity(
        outcome_state,
        (tbm.OutcomeProjectionAuthority(outcome, completed, evaluator),),
        outcome_events,
    )
    projections.append((outcome_reducer.descriptor.reducer_id, outcome_sha256))

    attribution_events = (outcome_events[1],)
    attribution_reducer = tbm.build_outcome_attribution_reducer()
    attribution_state, attribution_sha256 = _reduce(
        attribution_reducer,
        attribution_events,
    )
    tbm.verify_outcome_attribution_projection_parity(
        attribution_state,
        (outcome,),
        (),
        attribution_events,
    )
    projections.append((attribution_reducer.descriptor.reducer_id, attribution_sha256))

    effect_events = tuple(
        event for event in events if event.event_type in tbm.EFFECT_EVENT_TYPES
    )
    provider_effect_events = effect_events[:4]
    assert tuple(event.actor_type for event in provider_effect_events) == (
        "agent_client",
        "service",
        "service",
        "service",
    )
    assert tuple(
        tbm.parse_provider_effect_transition_event(event).stage
        for event in provider_effect_events[1:]
    ) == (
        "attempt_started",
        "request_submitted",
        "receipt_recorded",
    )
    completion_effect_events = effect_events[4:]
    effect_ref = tbm.parse_effect_requested_event(completion_effect_events[0])
    assert effect_ref.outbox_event is not None
    outbox_event = runtime.outbox_repository.get_event(effect_ref.outbox_event.event_id)
    delivery_history = runtime.outbox_repository.list_delivery_history(
        outbox_event.event_id
    )
    effect_reducer = tbm.build_effect_queue_reducer()
    effect_state, effect_sha256 = _reduce(effect_reducer, effect_events)
    completion_effect_state, _ = _reduce(
        effect_reducer,
        completion_effect_events,
    )
    tbm.verify_effect_projection_parity(
        completion_effect_state,
        (tbm.EffectProjectionAuthority(outbox_event, delivery_history),),
        completion_effect_events,
    )
    projections.append((effect_reducer.descriptor.reducer_id, effect_sha256))

    global_head = cast(
        tuple[int, str, str],
        runtime._connection.execute(
            "SELECT current_global_position, current_event_id, "
            "current_event_sha256 FROM v3_event_ledger_global_head "
            "WHERE singleton = 1"
        ).fetchone(),
    )
    stream_heads = cast(
        tuple[tuple[str, int, str, str], ...],
        tuple(
            runtime._connection.execute(
                "SELECT stream_id, current_stream_version, current_event_id, "
                "current_event_sha256 FROM v3_event_ledger_stream_heads "
                "ORDER BY stream_id"
            ).fetchall()
        ),
    )
    assert global_head == (
        events[-1].global_position,
        events[-1].event_id,
        events[-1].event_sha256,
    )
    expected_stream_heads = {
        event.stream_id: (
            event.stream_id,
            event.stream_version,
            event.event_id,
            event.event_sha256,
        )
        for event in events
    }
    assert stream_heads == tuple(
        expected_stream_heads[stream_id] for stream_id in sorted(expected_stream_heads)
    )
    assert len(stream_heads) == 8
    return _LifecycleSignature(
        tuple(
            (
                event.global_position,
                event.event_type,
                event.event_version,
                event.stream_type,
                event.stream_id,
                event.stream_version,
                event.event_id,
                event.event_sha256,
            )
            for event in events
        ),
        tuple(sorted(projections)),
        global_head,
        stream_heads,
    )


def _open_runtime(tmp_path: Path, name: str):
    dependencies, context = _dependencies(
        _Clock(),
        semantic_provider_invoker=lambda _call: _trusted_provider_result(),
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(
        tmp_path / f"{name}.sqlite3",
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
        check_same_thread=False,
    )
    return runtime, context


@contextmanager
def _running_http(runtime, context) -> Iterator[str]:
    def authenticate(
        request: DurableHTTPAuthenticationRequest,
    ) -> DurableHTTPAuthenticatedContexts:
        if request.authorization != f"Bearer {TOKEN}":
            raise ValueError("untrusted credential")
        return DurableHTTPAuthenticatedContexts(
            context,
            provider=_provider_context(),
            evaluator=EVALUATOR_CONTEXT,
        )

    server = DurableAgentHTTPServer(
        DurableHTTPServerConfiguration(port=0),
        runtime.dispatcher,
        authenticate,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _facade_signature(tmp_path: Path, name: str) -> _LifecycleSignature:
    runtime, context = _open_runtime(tmp_path, name)
    try:
        prepared = runtime.agent.prepare(
            context,
            replace(_durable_request(), expires_in_seconds=3_600),
        ).session
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        provider_result = _provider_result(evaluation)
        decided = runtime.agent.decide(
            context,
            _provider_context(),
            _semantic_request(prepared),
            lambda _call: provider_result,
        ).session
        finalized = runtime.agent.finalize(
            context,
            tbm.DurableFinalizationRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        ).session
        executing = runtime.agent.start(
            context,
            tbm.DurableExecutionStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        ).session
        resumed = runtime.agent.resume(
            context,
            tbm.DurableExecutionResumeRequest(
                session_id=executing.session_id,
                expected_session_version=executing.version,
                lease_seconds=LIFECYCLE_FIXTURE["resume"]["lease_seconds"],
            ),
        ).session
        completed = runtime.agent.complete(
            context,
            EVALUATOR_CONTEXT,
            _parity_completion(resumed),
        ).session
        return _signature(runtime, completed.session_id)
    finally:
        runtime.close()


def _sync_http_signature(tmp_path: Path) -> _LifecycleSignature:
    runtime, context = _open_runtime(tmp_path, "python-sync-http")
    try:
        with _running_http(runtime, context) as base_url:
            client = DurableAgentHTTPClient(base_url, TOKEN)
            prepared_response = client.prepare(_prepare_request())
            prepared = runtime.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            evaluation = runtime.evidence_repository.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            client.decide(_decide_request(prepared, evaluation))
            decided = runtime.sessions.get(prepared.session_id)
            client.finalize(
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                )
            )
            finalized = runtime.sessions.get(decided.session_id)
            client.start(
                DurableStartRequest(
                    session_id=finalized.session_id,
                    expected_session_version=finalized.version,
                )
            )
            executing = runtime.sessions.get(finalized.session_id)
            client.resume(
                DurableResumeRequest(
                    session_id=executing.session_id,
                    expected_session_version=executing.version,
                    **LIFECYCLE_FIXTURE["resume"],
                )
            )
            resumed = runtime.sessions.get(executing.session_id)
            client.complete(_complete_request(resumed))
        completed = runtime.sessions.get(resumed.session_id)
        return _signature(runtime, completed.session_id)
    finally:
        runtime.close()


def _async_http_signature(tmp_path: Path) -> _LifecycleSignature:
    runtime, context = _open_runtime(tmp_path, "python-async-http")

    async def scenario(base_url: str) -> str:
        client = AsyncDurableAgentHTTPClient(base_url, TOKEN)
        try:
            prepared_response = await client.prepare(_prepare_request())
            prepared = runtime.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            evaluation = runtime.evidence_repository.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            await client.decide(_decide_request(prepared, evaluation))
            decided = runtime.sessions.get(prepared.session_id)
            await client.finalize(
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                )
            )
            finalized = runtime.sessions.get(decided.session_id)
            await client.start(
                DurableStartRequest(
                    session_id=finalized.session_id,
                    expected_session_version=finalized.version,
                )
            )
            executing = runtime.sessions.get(finalized.session_id)
            await client.resume(
                DurableResumeRequest(
                    session_id=executing.session_id,
                    expected_session_version=executing.version,
                    **LIFECYCLE_FIXTURE["resume"],
                )
            )
            resumed = runtime.sessions.get(executing.session_id)
            await client.complete(_complete_request(resumed))
            return resumed.session_id
        finally:
            await client.aclose()

    try:
        with _running_http(runtime, context) as base_url:
            session_id = asyncio.run(scenario(base_url))
        return _signature(runtime, session_id)
    finally:
        runtime.close()


def _mcp_server_parameters(database: Path) -> StdioServerParameters:
    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not prior_pythonpath
        else source_path + os.pathsep + prior_pythonpath
    )
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "trace_backed_memory.mcp_entry",
            "--profile",
            "durable-v3",
            "--application-factory",
            (
                "tests.test_durable_transport_event_parity:"
                "create_transport_parity_application"
            ),
            "--sqlite",
            str(database),
            "--initialize",
            "--expose-injection-content",
            "--expose-replay-content",
        ],
        cwd=str(ROOT),
        env=environment,
    )


def _mcp_result(response: object) -> dict[str, object]:
    assert response.isError is False
    payload = response.structuredContent
    assert isinstance(payload, dict)
    result = payload.get("result")
    assert isinstance(result, dict)
    return result


def _mcp_signature(tmp_path: Path) -> _LifecycleSignature:
    database = tmp_path / "trusted-local-mcp.sqlite3"

    async def scenario() -> str:
        async with stdio_client(_mcp_server_parameters(database)) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                prepared_response = await session.call_tool(
                    "tbm_durable_prepare",
                    {"request": _prepare_request().model_dump(mode="json")},
                )
                prepared_result = _mcp_result(prepared_response)
                prepared = tbm.parse_gate_session(
                    cast(dict[str, object], prepared_result["session"])
                )
                evaluation = tbm.parse_system_gate_evaluation(
                    cast(
                        dict[str, object],
                        prepared_result["system_gate_evaluation"],
                    )
                )
                assert prepared.status == "prepared"
                session_id = prepared.session_id

                decided_response = await session.call_tool(
                    "tbm_durable_decide",
                    {
                        "request": _decide_request(
                            prepared,
                            evaluation,
                        ).model_dump(mode="json")
                    },
                )
                decided_result = _mcp_result(decided_response)
                decided = tbm.parse_gate_session(
                    cast(dict[str, object], decided_result["session"])
                )
                assert decided.status == "decided"
                assert decided_result["replayed"] is False
                finalized_response = await session.call_tool(
                    "tbm_durable_finalize",
                    {
                        "request": DurableFinalizeRequest(
                            session_id=session_id,
                            expected_session_version=decided.version,
                        ).model_dump(mode="json")
                    },
                )
                finalized_result = _mcp_result(finalized_response)
                finalized = tbm.parse_gate_session(
                    cast(dict[str, object], finalized_result["session"])
                )
                assert finalized.status == "finalized"
                assert finalized_result["replayed"] is False
                started_response = await session.call_tool(
                    "tbm_durable_start",
                    {
                        "request": DurableStartRequest(
                            session_id=session_id,
                            expected_session_version=finalized.version,
                        ).model_dump(mode="json")
                    },
                )
                started_result = _mcp_result(started_response)
                started = tbm.parse_gate_session(
                    cast(dict[str, object], started_result["session"])
                )
                assert started.status == "executing"
                assert started_result["replayed"] is False
                resumed_response = await session.call_tool(
                    "tbm_durable_resume",
                    {
                        "request": DurableResumeRequest(
                            session_id=session_id,
                            expected_session_version=started.version,
                            **LIFECYCLE_FIXTURE["resume"],
                        ).model_dump(mode="json")
                    },
                )
                resumed_result = _mcp_result(resumed_response)
                resumed = tbm.parse_gate_session(
                    cast(dict[str, object], resumed_result["session"])
                )
                assert resumed.status == "executing"
                assert resumed_result["replayed"] is True
                completed_response = await session.call_tool(
                    "tbm_durable_complete",
                    {"request": _complete_request(resumed).model_dump(mode="json")},
                )
                completed_result = _mcp_result(completed_response)
                completed = tbm.parse_gate_session(
                    cast(dict[str, object], completed_result["session"])
                )
                assert completed.status == "completed"
                assert completed_result["replayed"] is False
                return completed.session_id

    session_id = anyio.run(scenario)
    dependencies, _context = _dependencies(_Clock())
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=False,
        expose_injection_content=True,
        expose_replay_content=True,
        check_same_thread=False,
    )
    try:
        return _signature(runtime, session_id)
    finally:
        runtime.close()


def test_durable_python_facade_sync_async_http_and_mcp_share_event_projection(
    tmp_path: Path,
) -> None:
    expected = _facade_signature(tmp_path, "python-facade")
    signatures = {
        "python-sync-http": _sync_http_signature(tmp_path),
        "python-async-http": _async_http_signature(tmp_path),
        "trusted-local-mcp": _mcp_signature(tmp_path),
    }
    for transport, signature in signatures.items():
        assert signature.events == expected.events, transport
        assert signature.projections == expected.projections, transport
        assert signature.global_head == expected.global_head, transport
        assert signature.stream_heads == expected.stream_heads, transport


@pytest.mark.skipif(shutil.which("bun") is None, reason="Bun is unavailable")
def test_durable_typescript_sdk_matches_python_event_projection(
    tmp_path: Path,
) -> None:
    expected = _facade_signature(tmp_path, "typescript-python-reference")
    runtime, context = _open_runtime(tmp_path, "typescript-http")
    try:
        with _running_http(runtime, context) as base_url:
            completed = subprocess.run(
                [
                    shutil.which("bun") or "bun",
                    str(TYPESCRIPT_DRIVER),
                    base_url,
                    TOKEN,
                    str(FIXTURE),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        assert completed.returncode == 0, completed.stderr
        session_id = completed.stdout.strip()
        actual = _signature(runtime, session_id)
        assert actual.events == expected.events
        assert actual.projections == expected.projections
        assert actual.global_head == expected.global_head
        assert actual.stream_heads == expected.stream_heads
    finally:
        runtime.close()
