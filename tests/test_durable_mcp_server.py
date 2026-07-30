from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace
from typing import cast

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_runtime_v3 import _Clock, _dependencies
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
)
from trace_backed_memory import durable_mcp_entry, mcp_entry
from trace_backed_memory._timestamps import utc_timestamp
from trace_backed_memory.durable_agent_wire_v1 import (
    DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
    DurableAbandonRequest,
    DurableCancelRequest,
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableGetSessionRequest,
    DurableReplayRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_mcp_entry import DurableMCPApplication
from trace_backed_memory.durable_mcp_server import (
    DURABLE_MCP_SERVER_INSTRUCTIONS,
    DurableMCPTrustedContexts,
    create_durable_mcp_server,
)
from trace_backed_memory.durable_runtime_v3 import DurableRuntimeFactory


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_FACTORY = (
    "tests.test_durable_mcp_server:create_test_application"
)
TOOL_NAMES = {
    "tbm_durable_capabilities",
    "tbm_durable_prepare",
    "tbm_durable_decide",
    "tbm_durable_finalize",
    "tbm_durable_start",
    "tbm_durable_resume",
    "tbm_durable_abandon",
    "tbm_durable_complete",
    "tbm_durable_cancel",
    "tbm_durable_get_session",
    "tbm_durable_export_replay",
}


def create_test_application() -> DurableMCPApplication:
    """Return deterministic trusted dependencies for real-process tests."""
    dependencies, context = _dependencies(_Clock())
    dependencies = replace(
        dependencies,
        clock=utc_timestamp,
        authorization_request_id_factory=lambda: (
            f"authorization_mcp_{secrets.token_hex(16)}"
        ),
        session_id_factory=lambda: (
            f"gate_session_mcp_{secrets.token_hex(16)}"
        ),
    )
    return DurableMCPApplication(
        dependencies,
        DurableMCPTrustedContexts(
            context,
            _provider_context(),
            EVALUATOR_CONTEXT,
        ),
    )


def _server_parameters(
    database: Path,
    *,
    initialize: bool,
    expose_content: bool = True,
) -> StdioServerParameters:
    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not prior_pythonpath
        else source_path + os.pathsep + prior_pythonpath
    )
    arguments = [
        "-m",
        "trace_backed_memory.mcp_entry",
        "--profile",
        "durable-v3",
        "--application-factory",
        APPLICATION_FACTORY,
        "--sqlite",
        str(database),
    ]
    if initialize:
        arguments.append("--initialize")
    if expose_content:
        arguments.extend(
            [
                "--expose-injection-content",
                "--expose-replay-content",
            ]
        )
    return StdioServerParameters(
        command=sys.executable,
        args=arguments,
        cwd=str(ROOT),
        env=environment,
    )


def _request_payload(request: object) -> dict[str, object]:
    return cast(
        dict[str, object],
        request.model_dump(mode="json"),
    )


def _tool_error_payload(result: object) -> dict[str, object]:
    assert result.isError is True
    assert len(result.content) == 1
    text = result.content[0].text
    return cast(dict[str, object], json.loads(text[text.index("{") :]))


def _complete_request(session) -> DurableCompleteRequest:
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


def test_mcp_entry_routes_only_explicit_durable_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(durable_mcp_entry, "main", lambda argv: 23)
    assert (
        mcp_entry.main(
            ["--profile", "durable-v3", "--sqlite", "runtime.sqlite3"]
        )
        == 23
    )
    assert (
        mcp_entry.main(
            ["--profile=durable-v3", "--sqlite", "runtime.sqlite3"]
        )
        == 23
    )


def test_durable_mcp_parser_and_application_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = durable_mcp_entry._build_parser()
    arguments = parser.parse_args(
        [
            "--profile",
            "durable-v3",
            "--sqlite",
            "runtime.sqlite3",
        ]
    )
    assert arguments.profile == "durable-v3"
    assert arguments.sqlite == Path("runtime.sqlite3")
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "durable-v3"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--profile",
                "durable-v3",
                "--sqlite",
                "runtime.sqlite3",
                "--postgres-env",
                "POSTGRES_DSN",
            ]
        )

    application = create_test_application()
    with pytest.raises(TypeError, match="dependencies"):
        DurableMCPApplication(
            cast(object, object()),
            application.contexts,
        )
    with pytest.raises(TypeError, match="contexts"):
        DurableMCPApplication(
            application.dependencies,
            cast(object, object()),
        )
    with pytest.raises(TypeError, match="service context"):
        DurableMCPTrustedContexts(
            cast(object, object()),
            application.contexts.provider,
            application.contexts.evaluator,
        )
    with pytest.raises(TypeError, match="provider context"):
        DurableMCPTrustedContexts(
            application.contexts.service,
            cast(object, object()),
            application.contexts.evaluator,
        )
    with pytest.raises(TypeError, match="evaluator context"):
        DurableMCPTrustedContexts(
            application.contexts.service,
            application.contexts.provider,
            cast(object, object()),
        )
    for path in (
        "",
        " ",
        "missing-colon",
        "too:many:colons",
        "bad-module!:create",
        "trusted.application:bad-name!",
    ):
        with pytest.raises(ValueError, match="MODULE:CALLABLE"):
            durable_mcp_entry._load_application(path)

    monkeypatch.setattr(
        durable_mcp_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(
            nested=SimpleNamespace(create=lambda: application)
        ),
    )
    assert (
        durable_mcp_entry._load_application(
            "trusted.application:nested.create"
        )
        is application
    )
    monkeypatch.setattr(
        durable_mcp_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=object()),
    )
    with pytest.raises(ValueError, match="not callable"):
        durable_mcp_entry._load_application("trusted.application:create")
    monkeypatch.setattr(
        durable_mcp_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=lambda: object()),
    )
    with pytest.raises(ValueError, match="invalid data"):
        durable_mcp_entry._load_application("trusted.application:create")

    def missing_module(_name: str) -> object:
        raise ModuleNotFoundError("private factory detail")

    monkeypatch.setattr(
        durable_mcp_entry.importlib,
        "import_module",
        missing_module,
    )
    with pytest.raises(RuntimeError, match="could not be loaded"):
        durable_mcp_entry._load_application("trusted.application:create")


@pytest.mark.parametrize(
    "value",
    (None, "", " POSTGRES_DSN", "BAD=NAME", "BAD\x00NAME"),
)
def test_durable_mcp_environment_name_is_strict(value: object) -> None:
    with pytest.raises(ValueError, match="environment variable name"):
        durable_mcp_entry._validate_environment_name(value, "test")
    assert (
        durable_mcp_entry._validate_environment_name(
            "POSTGRES_DSN",
            "test",
        )
        == "POSTGRES_DSN"
    )


def test_durable_mcp_open_runtime_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_test_application()
    with pytest.raises(
        ValueError,
        match=(
            "--expose-replay-content requires "
            "--expose-injection-content"
        ),
    ):
        durable_mcp_entry._open_runtime(
            SimpleNamespace(
                sqlite=tmp_path / "durable.sqlite3",
                postgres_env=None,
                initialize=True,
                expose_injection_content=False,
                expose_replay_content=True,
            ),
            application,
        )

    runtime = durable_mcp_entry._open_runtime(
        SimpleNamespace(
            sqlite=tmp_path / "durable.sqlite3",
            postgres_env=None,
            initialize=True,
            expose_injection_content=False,
            expose_replay_content=False,
        ),
        application,
    )
    try:
        assert runtime.dispatcher.capabilities()["storage_mode"] == "sqlite"
    finally:
        runtime.close()

    with pytest.raises(ValueError, match="only valid with --sqlite"):
        durable_mcp_entry._open_runtime(
            SimpleNamespace(
                sqlite=None,
                postgres_env="POSTGRES_DSN",
                initialize=True,
                expose_injection_content=False,
                expose_replay_content=False,
            ),
            application,
        )
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError, match="variable is missing"):
        durable_mcp_entry._open_runtime(
            SimpleNamespace(
                sqlite=None,
                postgres_env="POSTGRES_DSN",
                initialize=False,
                expose_injection_content=False,
                expose_replay_content=False,
            ),
            application,
        )

    opened: list[tuple[str, bool, bool]] = []

    class _Factory:
        def __init__(self, dependencies: object) -> None:
            assert dependencies is application.dependencies

        def open_postgres(
            self,
            conninfo: str,
            *,
            expose_injection_content: bool,
            expose_replay_content: bool,
        ) -> object:
            opened.append(
                (
                    conninfo,
                    expose_injection_content,
                    expose_replay_content,
                )
            )
            return object()

    monkeypatch.setattr(
        durable_mcp_entry,
        "DurableRuntimeFactory",
        _Factory,
    )
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://trusted")
    postgres_runtime = durable_mcp_entry._open_runtime(
        SimpleNamespace(
            sqlite=None,
            postgres_env="POSTGRES_DSN",
            initialize=False,
            expose_injection_content=True,
            expose_replay_content=True,
        ),
        application,
    )
    assert type(postgres_runtime) is object
    assert opened == [("postgresql://trusted", True, True)]


def test_durable_mcp_main_closes_runtime_and_sanitizes_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application = create_test_application()

    class _Runtime:
        dispatcher = object()

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runtime = _Runtime()
    server = object()
    monkeypatch.setattr(
        durable_mcp_entry,
        "_load_application",
        lambda path: (
            application
            if path == "trusted.application:create"
            else None
        ),
    )
    monkeypatch.setattr(
        durable_mcp_entry,
        "_open_runtime",
        lambda _args, _application: runtime,
    )
    monkeypatch.setattr(
        durable_mcp_entry,
        "create_durable_mcp_server",
        lambda dispatcher, contexts: (
            server
            if dispatcher is runtime.dispatcher
            and contexts is application.contexts
            else None
        ),
    )
    calls: list[object] = []
    monkeypatch.setattr(
        durable_mcp_entry,
        "run_stdio_server",
        lambda value: calls.append(value),
    )
    assert (
        durable_mcp_entry.main(
            [
                "--profile",
                "durable-v3",
                "--application-factory",
                "trusted.application:create",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
                "--initialize",
            ]
        )
        == 0
    )
    assert calls == [server]
    assert runtime.closed is True
    assert capsys.readouterr().err == ""

    monkeypatch.delenv(
        durable_mcp_entry.DURABLE_MCP_APPLICATION_FACTORY_ENV,
        raising=False,
    )
    assert (
        durable_mcp_entry.main(
            [
                "--profile",
                "durable-v3",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
            ]
        )
        == 2
    )
    input_error = json.loads(capsys.readouterr().err)
    assert input_error["error"]["category"] == "input"
    assert input_error["error"]["operation"] == "open"

    monkeypatch.setattr(
        durable_mcp_entry,
        "_load_application",
        lambda _path: (_ for _ in ()).throw(
            RuntimeError("private secret")
        ),
    )
    assert (
        durable_mcp_entry.main(
            [
                "--profile",
                "durable-v3",
                "--application-factory",
                "trusted.application:create",
                "--sqlite",
                str(tmp_path / "runtime.sqlite3"),
            ]
        )
        == 2
    )
    internal_error = json.loads(capsys.readouterr().err)
    assert internal_error["error"]["category"] == "internal"
    assert internal_error["error"]["retryable"] is True
    assert "secret" not in internal_error["error"]["message"]


def test_durable_mcp_profile_is_runtime_only_and_strictly_described(
    tmp_path: Path,
) -> None:
    application = create_test_application()
    runtime = DurableRuntimeFactory(application.dependencies).open_sqlite(
        tmp_path / "durable.sqlite3",
        initialize=True,
    )
    try:
        server = create_durable_mcp_server(
            runtime.dispatcher,
            application.contexts,
        )
        with pytest.raises(TypeError, match="dispatcher"):
            create_durable_mcp_server(
                cast(object, object()),
                application.contexts,
            )
        with pytest.raises(TypeError, match="contexts"):
            create_durable_mcp_server(
                runtime.dispatcher,
                cast(object, object()),
            )
        tools = {
            tool.name: tool
            for tool in server._tool_manager.list_tools()
        }
        assert set(tools) == TOOL_NAMES
        assert tools["tbm_durable_capabilities"].annotations.readOnlyHint
        assert tools["tbm_durable_get_session"].annotations.readOnlyHint
        assert tools["tbm_durable_prepare"].annotations.idempotentHint
        assert not tools["tbm_durable_prepare"].annotations.readOnlyHint
        assert len(DURABLE_MCP_SERVER_INSTRUCTIONS) <= 512
        assert "process restart" in DURABLE_MCP_SERVER_INSTRUCTIONS
        assert "runtime-only" in DURABLE_MCP_SERVER_INSTRUCTIONS

        schemas = json.dumps(
            {
                name: tool.parameters
                for name, tool in tools.items()
            },
            sort_keys=True,
        )
        for identity_field in (
            '"tenant_id"',
            '"principal_id"',
            '"agent_client_id"',
            '"environment_id"',
            '"evaluator_id"',
        ):
            assert identity_field not in schemas
    finally:
        runtime.close()


def test_durable_mcp_tools_delegate_complete_lifecycle(
    tmp_path: Path,
) -> None:
    application = create_test_application()
    runtime = DurableRuntimeFactory(application.dependencies).open_sqlite(
        tmp_path / "durable.sqlite3",
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
        check_same_thread=False,
    )
    server = create_durable_mcp_server(
        runtime.dispatcher,
        application.contexts,
    )

    async def exercise() -> None:
        capabilities = await server._tool_manager.call_tool(
            "tbm_durable_capabilities",
            {},
        )
        assert capabilities["protocol_version"] == (
            DURABLE_AGENT_WIRE_PROTOCOL_VERSION
        )
        assert capabilities["durable_sessions"] is True
        assert capabilities["process_local_records"] == []
        assert capabilities["transport_profile"] == "durable-v3"
        assert capabilities["transport_security"] == "trusted-local-stdio"
        assert capabilities["peer_authentication"] is False

        prepared_response = await server._tool_manager.call_tool(
            "tbm_durable_prepare",
            {"request": _request_payload(_prepare_request())},
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        decided_response = await server._tool_manager.call_tool(
            "tbm_durable_decide",
            {
                "request": _request_payload(
                    _decide_request(prepared, evaluation)
                )
            },
        )
        decided = runtime.sessions.get(prepared.session_id)
        assert decided_response["result"]["session"]["status"] == "decided"

        finalized_response = await server._tool_manager.call_tool(
            "tbm_durable_finalize",
            {
                "request": _request_payload(
                    DurableFinalizeRequest(
                        session_id=decided.session_id,
                        expected_session_version=decided.version,
                    )
                )
            },
        )
        finalized = runtime.sessions.get(prepared.session_id)
        assert finalized_response["result"]["content_exposed"] is True

        await server._tool_manager.call_tool(
            "tbm_durable_start",
            {
                "request": _request_payload(
                    DurableStartRequest(
                        session_id=finalized.session_id,
                        expected_session_version=finalized.version,
                    )
                )
            },
        )
        executing = runtime.sessions.get(prepared.session_id)
        complete_request = _complete_request(executing)
        completed_response = await server._tool_manager.call_tool(
            "tbm_durable_complete",
            {"request": _request_payload(complete_request)},
        )
        completed = runtime.sessions.get(prepared.session_id)
        assert completed_response["result"]["session"]["status"] == "completed"

        loaded = await server._tool_manager.call_tool(
            "tbm_durable_get_session",
            {
                "request": _request_payload(
                    DurableGetSessionRequest(
                        session_id=completed.session_id
                    )
                )
            },
        )
        assert loaded["result"]["session"] == completed.to_dict()
        replay = await server._tool_manager.call_tool(
            "tbm_durable_export_replay",
            {
                "request": _request_payload(
                    DurableReplayRequest(
                        session_id=completed.session_id,
                        expected_session_version=completed.version,
                        allowed_classifications=["internal"],
                    )
                )
            },
        )
        assert replay["result"]["content_exposed"] is True
        assert replay["result"]["bundle"]["artifacts"]

    try:
        anyio.run(exercise)
    finally:
        runtime.close()


def test_durable_mcp_cancel_abandon_resume_and_sanitized_errors(
    tmp_path: Path,
) -> None:
    application = create_test_application()
    runtime = DurableRuntimeFactory(application.dependencies).open_sqlite(
        tmp_path / "durable.sqlite3",
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
        check_same_thread=False,
    )
    server = create_durable_mcp_server(
        runtime.dispatcher,
        application.contexts,
    )

    async def exercise() -> None:
        with pytest.raises(ToolError) as missing:
            await server._tool_manager.call_tool(
                "tbm_durable_get_session",
                {
                    "request": _request_payload(
                        DurableGetSessionRequest(
                            session_id="gate_session_missing"
                        )
                    )
                },
            )
        assert "TBM_DURABLE_AGENT_SESSION_UNAVAILABLE" in str(missing.value)
        assert "private" not in str(missing.value)

        cancel_source = _prepare_request().model_copy(
            update={
                "request_id": "durable_mcp_cancel_request",
                "trace_id": "trace_durable_mcp_cancel",
                "run_id": "run_durable_mcp_cancel",
                "idempotency_key": "durable_mcp_cancel",
            }
        )
        cancel_prepared = await server._tool_manager.call_tool(
            "tbm_durable_prepare",
            {"request": _request_payload(cancel_source)},
        )
        cancel_session = cancel_prepared["result"]["session"]
        cancel_request = DurableCancelRequest(
            session_id=cancel_session["session_id"],
            expected_session_version=cancel_session["version"],
            reason="caller canceled durable MCP request",
        )
        canceled = await server._tool_manager.call_tool(
            "tbm_durable_cancel",
            {"request": _request_payload(cancel_request)},
        )
        cancel_replay = await server._tool_manager.call_tool(
            "tbm_durable_cancel",
            {"request": _request_payload(cancel_request)},
        )
        assert canceled["result"]["session"]["status"] == "canceled"
        assert cancel_replay["result"]["replayed"] is True

        abandon_source = _prepare_request().model_copy(
            update={
                "request_id": "durable_mcp_abandon_request",
                "trace_id": "trace_durable_mcp_abandon",
                "run_id": "run_durable_mcp_abandon",
                "idempotency_key": "durable_mcp_abandon",
            }
        )
        abandon_prepared = await server._tool_manager.call_tool(
            "tbm_durable_prepare",
            {"request": _request_payload(abandon_source)},
        )
        prepared = runtime.sessions.get(
            abandon_prepared["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        await server._tool_manager.call_tool(
            "tbm_durable_decide",
            {
                "request": _request_payload(
                    _decide_request(prepared, evaluation)
                )
            },
        )
        decided = runtime.sessions.get(prepared.session_id)
        await server._tool_manager.call_tool(
            "tbm_durable_finalize",
            {
                "request": _request_payload(
                    DurableFinalizeRequest(
                        session_id=decided.session_id,
                        expected_session_version=decided.version,
                    )
                )
            },
        )
        finalized = runtime.sessions.get(prepared.session_id)
        await server._tool_manager.call_tool(
            "tbm_durable_start",
            {
                "request": _request_payload(
                    DurableStartRequest(
                        session_id=finalized.session_id,
                        expected_session_version=finalized.version,
                    )
                )
            },
        )
        executing = runtime.sessions.get(prepared.session_id)
        resumed = await server._tool_manager.call_tool(
            "tbm_durable_resume",
            {
                "request": {
                    "session_id": executing.session_id,
                    "expected_session_version": executing.version,
                    "lease_seconds": 2_700,
                }
            },
        )
        resumed_session = resumed["result"]["session"]
        abandon_request = DurableAbandonRequest(
            session_id=executing.session_id,
            expected_session_version=resumed_session["version"],
            reason="execution lease relinquished",
        )
        abandoned = await server._tool_manager.call_tool(
            "tbm_durable_abandon",
            {"request": _request_payload(abandon_request)},
        )
        abandon_replay = await server._tool_manager.call_tool(
            "tbm_durable_abandon",
            {"request": _request_payload(abandon_request)},
        )
        assert abandoned["result"]["session"]["status"] == "abandoned"
        assert abandon_replay["result"]["replayed"] is True

    try:
        anyio.run(exercise)
    finally:
        runtime.close()


def test_durable_mcp_real_process_restart_completion_and_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "durable-mcp.sqlite3"

    async def prepare_in_first_process() -> str:
        async with stdio_client(
            _server_parameters(database, initialize=True)
        ) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.instructions == (
                    DURABLE_MCP_SERVER_INSTRUCTIONS
                )
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == TOOL_NAMES
                prepared = await session.call_tool(
                    "tbm_durable_prepare",
                    {"request": _request_payload(_prepare_request())},
                )
                assert prepared.isError is False
                return cast(
                    str,
                    prepared.structuredContent["result"]["session"][
                        "session_id"
                    ],
                )

    session_id = anyio.run(prepare_in_first_process)

    application = create_test_application()
    runtime = DurableRuntimeFactory(application.dependencies).open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        prepared = runtime.sessions.get(session_id)
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        decide_request = _decide_request(prepared, evaluation)
    finally:
        runtime.close()

    async def continue_in_second_process() -> DurableCompleteRequest:
        async with stdio_client(
            _server_parameters(database, initialize=False)
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                loaded = await session.call_tool(
                    "tbm_durable_get_session",
                    {
                        "request": _request_payload(
                            DurableGetSessionRequest(session_id=session_id)
                        )
                    },
                )
                assert loaded.structuredContent["result"]["session"][
                    "status"
                ] == "prepared"

                forged = _request_payload(_prepare_request())
                forged["tenant_id"] = "forged_tenant"
                rejected = await session.call_tool(
                    "tbm_durable_prepare",
                    {"request": forged},
                )
                assert rejected.isError is True

                decided = await session.call_tool(
                    "tbm_durable_decide",
                    {"request": _request_payload(decide_request)},
                )
                assert decided.isError is False, _tool_error_payload(decided)
                decided_session = decided.structuredContent["result"][
                    "session"
                ]
                finalized = await session.call_tool(
                    "tbm_durable_finalize",
                    {
                        "request": _request_payload(
                            DurableFinalizeRequest(
                                session_id=session_id,
                                expected_session_version=decided_session[
                                    "version"
                                ],
                            )
                        )
                    },
                )
                finalized_session = finalized.structuredContent["result"][
                    "session"
                ]
                started = await session.call_tool(
                    "tbm_durable_start",
                    {
                        "request": _request_payload(
                            DurableStartRequest(
                                session_id=session_id,
                                expected_session_version=finalized_session[
                                    "version"
                                ],
                            )
                        )
                    },
                )
                started_version = started.structuredContent["result"][
                    "session"
                ]["version"]
                current = SimpleNamespace(
                    session_id=session_id,
                    version=started_version,
                )
                complete_request = _complete_request(current)
                completed = await session.call_tool(
                    "tbm_durable_complete",
                    {"request": _request_payload(complete_request)},
                )
                assert completed.structuredContent["result"]["session"][
                    "status"
                ] == "completed"
                return complete_request

    complete_request = anyio.run(continue_in_second_process)

    async def replay_in_third_process() -> None:
        async with stdio_client(
            _server_parameters(database, initialize=False)
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                retried = await session.call_tool(
                    "tbm_durable_complete",
                    {"request": _request_payload(complete_request)},
                )
                assert retried.structuredContent["result"]["replayed"] is True
                completed_session = retried.structuredContent["result"][
                    "session"
                ]
                replay = await session.call_tool(
                    "tbm_durable_export_replay",
                    {
                        "request": _request_payload(
                            DurableReplayRequest(
                                session_id=session_id,
                                expected_session_version=completed_session[
                                    "version"
                                ],
                                allowed_classifications=["internal"],
                            )
                        )
                    },
                )
                assert replay.isError is False
                assert replay.structuredContent["result"][
                    "content_exposed"
                ] is True
                assert replay.structuredContent["result"]["bundle"][
                    "artifacts"
                ]

    anyio.run(replay_in_third_process)
