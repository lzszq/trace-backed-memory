from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import shutil
import subprocess
from threading import Thread
from typing import Iterator

import anyio
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
from trace_backed_memory.durable_mcp_server import (
    DurableMCPTrustedContexts,
    create_durable_mcp_server,
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
    events: tuple[tuple[str, int, str], ...]
    projection_sha256: str


def _complete_request(session: tbm.GateSession) -> DurableCompleteRequest:
    completion = _completion(session)
    return DurableCompleteRequest(
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
    )


def _signature(runtime, session_id: str) -> _LifecycleSignature:
    completed = runtime.sessions.get(session_id)
    rows = runtime._connection.execute(
        "SELECT canonical_event FROM v3_event_ledger_events "
        "WHERE stream_id = ? ORDER BY stream_version",
        (session_id,),
    ).fetchall()
    events = tuple(loads_canonical_event(row[0]) for row in rows)
    assert tuple(event.event_type for event in events) == (
        tbm.GATE_SESSION_CREATED_EVENT,
        tbm.GATE_SESSION_PREPARED_EVENT,
        tbm.SEMANTIC_GATE_REQUESTED_EVENT,
        tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
        tbm.SEMANTIC_GATE_DECIDED_EVENT,
        tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
        tbm.USAGE_DECISION_FINALIZED_EVENT,
        tbm.EXECUTION_STARTED_EVENT,
        tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
        tbm.GATE_SESSION_COMPLETED_EVENT,
    )
    reducer = tbm.build_gate_session_reducer()
    state = reducer.initial_state()
    projection_sha256 = ""
    for event in events:
        step = execute_reducer_step(
            reducer,
            state,
            ReducerEvent(
                event,
                DEFAULT_EVENT_TYPE_REGISTRY.consume(event),
            ),
        )
        state = step.state
        projection_sha256 = step.state_sha256
    tbm.verify_gate_session_projection_parity(state, (completed,))
    return _LifecycleSignature(
        tuple(
            (
                event.event_type,
                event.stream_version,
                event.event_sha256,
            )
            for event in events
        ),
        projection_sha256,
    )


def _open_runtime(tmp_path: Path, name: str):
    dependencies, context = _dependencies(_Clock())
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
            _completion(resumed),
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


def _mcp_signature(tmp_path: Path) -> _LifecycleSignature:
    runtime, context = _open_runtime(tmp_path, "trusted-local-mcp")
    server = create_durable_mcp_server(
        runtime.dispatcher,
        DurableMCPTrustedContexts(
            context,
            _provider_context(),
            EVALUATOR_CONTEXT,
        ),
    )

    async def scenario() -> str:
        prepared_response = await server._tool_manager.call_tool(
            "tbm_durable_prepare",
            {"request": _prepare_request().model_dump(mode="json")},
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        await server._tool_manager.call_tool(
            "tbm_durable_decide",
            {
                "request": _decide_request(
                    prepared,
                    evaluation,
                ).model_dump(mode="json")
            },
        )
        decided = runtime.sessions.get(prepared.session_id)
        await server._tool_manager.call_tool(
            "tbm_durable_finalize",
            {
                "request": DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                ).model_dump(mode="json")
            },
        )
        finalized = runtime.sessions.get(decided.session_id)
        await server._tool_manager.call_tool(
            "tbm_durable_start",
            {
                "request": DurableStartRequest(
                    session_id=finalized.session_id,
                    expected_session_version=finalized.version,
                ).model_dump(mode="json")
            },
        )
        executing = runtime.sessions.get(finalized.session_id)
        await server._tool_manager.call_tool(
            "tbm_durable_resume",
            {
                "request": DurableResumeRequest(
                    session_id=executing.session_id,
                    expected_session_version=executing.version,
                    **LIFECYCLE_FIXTURE["resume"],
                ).model_dump(mode="json")
            },
        )
        resumed = runtime.sessions.get(executing.session_id)
        await server._tool_manager.call_tool(
            "tbm_durable_complete",
            {"request": _complete_request(resumed).model_dump(mode="json")},
        )
        return resumed.session_id

    try:
        session_id = anyio.run(scenario)
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
        assert signature.projection_sha256 == expected.projection_sha256, (
            transport
        )


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
        assert actual.projection_sha256 == expected.projection_sha256
    finally:
        runtime.close()
