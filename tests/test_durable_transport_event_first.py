from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Thread
from typing import Awaitable, Callable, Iterator, cast
from urllib.request import Request, urlopen

import anyio

from tests.durable_event_first_support import (
    LIFECYCLE_FIXTURE,
    event_first_dependencies,
    event_first_report,
    open_event_first_runtime,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_semantic_gate_v3 import _context as _provider_context
from trace_backed_memory.artifact_service_v3 import AuthenticatedServiceContext
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
from trace_backed_memory.durable_runtime_v3 import DurableRuntimeFactory
from trace_backed_memory.durable_sdk import (
    AsyncDurableAgentHTTPClient,
    DurableAgentHTTPClient,
)


TOKEN = "durable_event_first_token_" + "a" * 40
GOLDEN = Path(__file__).resolve().parent / (
    "fixtures/durable_event_first_projection_v1.json"
)
_PATHS = {
    "prepare": "prepare",
    "decide": "decide",
    "finalize": "finalize",
    "start": "start",
    "resume": "resume",
    "complete": "complete",
    "get_session": "get-session",
    "export_replay": "export-replay",
}


@contextmanager
def _running_http_runtime(
    runtime: object,
    context: AuthenticatedServiceContext,
) -> Iterator[str]:
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
        getattr(runtime, "dispatcher"),
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


def _reference(response: dict[str, object]) -> dict[str, object]:
    result = cast(dict[str, object], response["result"])
    session = cast(dict[str, object], result["session"])
    return {
        "session_id": session["session_id"],
        "expected_session_version": session["version"],
    }


def _drive_sync(
    call: Callable[[str, dict[str, object]], dict[str, object]],
) -> str:
    prepared = call("prepare", dict(LIFECYCLE_FIXTURE["prepare"]))
    decided = call(
        "decide",
        {**_reference(prepared), **LIFECYCLE_FIXTURE["decide"]},
    )
    finalized = call("finalize", _reference(decided))
    started = call("start", _reference(finalized))
    resumed = call(
        "resume",
        {**_reference(started), **LIFECYCLE_FIXTURE["resume"]},
    )
    complete_request = {
        **_reference(resumed),
        **LIFECYCLE_FIXTURE["complete"],
    }
    completed = call("complete", complete_request)
    replayed = call("complete", complete_request)
    assert cast(dict[str, object], completed["result"])["replayed"] is False
    assert cast(dict[str, object], replayed["result"])["replayed"] is True
    session_id = cast(str, _reference(completed)["session_id"])
    call("get_session", {"session_id": session_id})
    call(
        "export_replay",
        {
            **_reference(completed),
            **LIFECYCLE_FIXTURE["replay"],
        },
    )
    return session_id


async def _drive_async(
    call: Callable[
        [str, dict[str, object]],
        Awaitable[dict[str, object]],
    ],
) -> str:
    prepared = await call("prepare", dict(LIFECYCLE_FIXTURE["prepare"]))
    decided = await call(
        "decide",
        {**_reference(prepared), **LIFECYCLE_FIXTURE["decide"]},
    )
    finalized = await call("finalize", _reference(decided))
    started = await call("start", _reference(finalized))
    resumed = await call(
        "resume",
        {**_reference(started), **LIFECYCLE_FIXTURE["resume"]},
    )
    complete_request = {
        **_reference(resumed),
        **LIFECYCLE_FIXTURE["complete"],
    }
    completed = await call("complete", complete_request)
    replayed = await call("complete", complete_request)
    assert cast(dict[str, object], completed["result"])["replayed"] is False
    assert cast(dict[str, object], replayed["result"])["replayed"] is True
    session_id = cast(str, _reference(completed)["session_id"])
    await call("get_session", {"session_id": session_id})
    await call(
        "export_replay",
        {
            **_reference(completed),
            **LIFECYCLE_FIXTURE["replay"],
        },
    )
    return session_id


def _raw_http_call(
    base_url: str,
    operation: str,
    payload: dict[str, object],
) -> dict[str, object]:
    body = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    request = Request(
        f"{base_url}/durable/v1/{_PATHS[operation]}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=10) as response:
        return cast(dict[str, object], json.loads(response.read()))


def _golden() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(GOLDEN.read_text(encoding="utf-8")),
    )


def test_raw_http_python_sync_and_async_share_event_first_projection(
    tmp_path: Path,
) -> None:
    reports: list[dict[str, object]] = []

    runtime, context = open_event_first_runtime(tmp_path / "raw-http.sqlite3")
    try:
        with _running_http_runtime(
            runtime,
            cast(AuthenticatedServiceContext, context),
        ) as base_url:
            session_id = _drive_sync(
                lambda operation, payload: _raw_http_call(
                    base_url,
                    operation,
                    payload,
                )
            )
        reports.append(event_first_report(runtime, session_id))
    finally:
        runtime.close()

    runtime, context = open_event_first_runtime(tmp_path / "sync-sdk.sqlite3")
    try:
        with _running_http_runtime(
            runtime,
            cast(AuthenticatedServiceContext, context),
        ) as base_url:
            client = DurableAgentHTTPClient(base_url, TOKEN)
            session_id = _drive_sync(
                lambda operation, payload: cast(
                    object,
                    getattr(client, operation)(payload),
                ).to_dict()
            )
        reports.append(event_first_report(runtime, session_id))
    finally:
        runtime.close()

    runtime, context = open_event_first_runtime(tmp_path / "async-sdk.sqlite3")
    try:
        with _running_http_runtime(
            runtime,
            cast(AuthenticatedServiceContext, context),
        ) as base_url:
            async def scenario() -> str:
                client = AsyncDurableAgentHTTPClient(base_url, TOKEN)
                try:
                    async def call(
                        operation: str,
                        payload: dict[str, object],
                    ) -> dict[str, object]:
                        response = await getattr(client, operation)(payload)
                        return response.to_dict()

                    return await _drive_async(call)
                finally:
                    await client.aclose()

            session_id = asyncio.run(scenario())
        reports.append(event_first_report(runtime, session_id))
    finally:
        runtime.close()

    assert reports == [reports[0]] * 3
    assert reports[0] == _golden()


def test_mcp_shares_event_first_projection(tmp_path: Path) -> None:
    runtime, context = open_event_first_runtime(tmp_path / "mcp.sqlite3")
    server = create_durable_mcp_server(
        runtime.dispatcher,
        DurableMCPTrustedContexts(
            cast(AuthenticatedServiceContext, context),
            _provider_context(),
            EVALUATOR_CONTEXT,
        ),
    )

    async def scenario() -> str:
        async def call(
            operation: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            return cast(
                dict[str, object],
                await server._tool_manager.call_tool(
                    f"tbm_durable_{operation}",
                    {"request": payload},
                ),
            )

        return await _drive_async(call)

    try:
        session_id = anyio.run(scenario)
        assert event_first_report(runtime, session_id) == _golden()
    finally:
        runtime.close()


def test_event_first_selection_does_not_change_public_wire_contract(
    tmp_path: Path,
) -> None:
    plain_dependencies, plain_context = event_first_dependencies()
    event_dependencies, event_context = event_first_dependencies()
    factory = DurableRuntimeFactory(plain_dependencies)
    plain = factory.open_sqlite(
        tmp_path / "plain.sqlite3",
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    event_first = DurableRuntimeFactory(event_dependencies).open_sqlite(
        tmp_path / "event-first.sqlite3",
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
        event_first_commands=True,
    )
    try:
        assert plain.dispatcher.capabilities() == (
            event_first.dispatcher.capabilities()
        )
        plain_server = create_durable_mcp_server(
            plain.dispatcher,
            DurableMCPTrustedContexts(
                cast(AuthenticatedServiceContext, plain_context),
                _provider_context(),
                EVALUATOR_CONTEXT,
            ),
        )
        event_server = create_durable_mcp_server(
            event_first.dispatcher,
            DurableMCPTrustedContexts(
                cast(AuthenticatedServiceContext, event_context),
                _provider_context(),
                EVALUATOR_CONTEXT,
            ),
        )
        plain_tools = {
            tool.name: tool.parameters
            for tool in plain_server._tool_manager.list_tools()
        }
        event_tools = {
            tool.name: tool.parameters
            for tool in event_server._tool_manager.list_tools()
        }
        assert plain_tools == event_tools
    finally:
        event_first.close()
        plain.close()
