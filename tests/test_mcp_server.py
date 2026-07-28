import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import builtins

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

import trace_backed_memory as tbm
import trace_backed_memory.mcp_entry as mcp_entry
import trace_backed_memory.mcp_server as mcp_server


def _configuration(
    root: Path,
    *,
    storage_mode: str = "memory",
    sqlite_path: Path | None = None,
) -> mcp_server.MCPServerConfiguration:
    return mcp_server.MCPServerConfiguration(
        repo_path=root,
        storage_mode=storage_mode,
        sqlite_path=sqlite_path,
    )


def _server_parameters(
    root: Path,
    database: Path,
) -> StdioServerParameters:
    environment = dict(os.environ)
    source_path = str(root / "src")
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
            "--repo-path",
            str(root),
            "--sqlite",
            str(database),
        ],
        cwd=str(root),
        env=environment,
    )


def _authenticated_configuration(
    root: Path,
    tmp_path: Path,
    *,
    allow: bool = True,
) -> mcp_server.MCPServerConfiguration:
    registry = json.loads(
        (root / "examples" / "entity_registry_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    if not allow:
        registry["authorization_policy"]["role_bindings"][0][
            "status"
        ] = "revoked"
    registry_path = tmp_path / (
        "registry-allow.json" if allow else "registry-deny.json"
    )
    registry_path.write_text(
        json.dumps(registry),
        encoding="utf-8",
    )
    return mcp_server.MCPServerConfiguration(
        repo_path=root,
        storage_mode="memory",
        authentication=mcp_server.MCPAuthenticationConfiguration(
            registry_path=registry_path,
            authorization_sqlite_path=tmp_path / (
                "authorization-allow.sqlite3"
                if allow
                else "authorization-deny.sqlite3"
            ),
            principal_id="principal_tenant_001",
            agent_client_id="agent_client_001",
            environment_id="environment_001",
        ),
    )


def _tool_error_payload(result) -> dict[str, object]:
    assert result.isError is True
    assert len(result.content) == 1
    text = result.content[0].text
    return json.loads(text[text.index("{") :])


def test_mcp_profile_is_runtime_only_and_strictly_described():
    root = Path(__file__).resolve().parents[1]
    runtime = tbm.LocalAgentMemory.in_memory()
    server = mcp_server.create_mcp_server(
        _configuration(root),
        runtime,
    )

    tools = {
        tool.name: tool
        for tool in server._tool_manager.list_tools()
    }

    assert set(tools) == {
        "tbm_capabilities",
        "tbm_health",
        "tbm_prepare_memory",
        "tbm_finalize_memory",
        "tbm_complete_run",
        "tbm_cancel_run",
    }
    assert tools["tbm_capabilities"].annotations.readOnlyHint is True
    assert tools["tbm_prepare_memory"].annotations.readOnlyHint is False
    prepare_schema = tools["tbm_prepare_memory"].parameters
    request_schema = prepare_schema["$defs"]["PrepareMemoryRequest"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["mode"]["enum"] == [
        "debug",
        "repair",
        "regression",
        "planning",
        "eval",
        "production",
    ]
    assert len(mcp_server.MCP_SERVER_INSTRUCTIONS) <= 512
    assert "process-local" in mcp_server.MCP_SERVER_INSTRUCTIONS
    assert "Never claim to verify or activate" in (
        mcp_server.MCP_SERVER_INSTRUCTIONS
    )


def test_mcp_tools_delegate_to_one_runtime_in_process():
    root = Path(__file__).resolve().parents[1]
    runtime = tbm.LocalAgentMemory.in_memory()
    server = mcp_server.create_mcp_server(
        _configuration(root),
        runtime,
    )

    async def exercise() -> None:
        capabilities = await server._tool_manager.call_tool(
            "tbm_capabilities",
            {},
        )
        assert capabilities["protocol_version"] == "tbm.agent.v1"

        prepared = await server._tool_manager.call_tool(
            "tbm_prepare_memory",
            {
                "request": {
                    "task": "exercise the in-process MCP lifecycle",
                    "mode": "planning",
                }
            },
        )
        assert prepared["candidate_memory_ids"] == []

        finalized = await server._tool_manager.call_tool(
            "tbm_finalize_memory",
            {
                "request": {
                    "request_id": prepared["request_id"],
                    "use_memory": False,
                    "allowed_memory_ids": [],
                    "blocked_memory_ids": [],
                    "reason": "no applicable memory",
                    "risk": "none",
                    "recommended_injection": "none",
                }
            },
        )
        assert finalized["snippet"] == ""

        completed = await server._tool_manager.call_tool(
            "tbm_complete_run",
            {
                "request": {
                    "decision_id": finalized["decision_id"],
                    "eval_result": "pass",
                }
            },
        )
        assert completed["eval_result"] == "pass"

        cancel_prepared = await server._tool_manager.call_tool(
            "tbm_prepare_memory",
            {
                "request": {
                    "task": "exercise cancellation",
                    "mode": "planning",
                }
            },
        )
        canceled = await server._tool_manager.call_tool(
            "tbm_cancel_run",
            {"request": {"request_id": cancel_prepared["request_id"]}},
        )
        assert canceled["canceled"] is True

        health = await server._tool_manager.call_tool(
            "tbm_health",
            {},
        )
        assert health["pending_request_count"] == 0
        assert health["memory_run_metrics"]["complete_count"] == 1

    try:
        anyio.run(exercise)
    finally:
        runtime.close()


def test_authenticated_mcp_authorizes_and_binds_canonical_scope(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    configuration = _authenticated_configuration(root, tmp_path)
    runtime = tbm.LocalAgentMemory.in_memory()
    opened = mcp_server._open_authenticated_runtime(
        configuration,
        runtime,
    )
    assert opened is not None
    authenticated_runtime, decisions = opened
    server = mcp_server.create_mcp_server(
        configuration,
        runtime,
        authenticated_runtime=authenticated_runtime,
    )

    async def exercise() -> None:
        prepared = await server._tool_manager.call_tool(
            "tbm_prepare_memory",
            {
                "request": {
                    "task": "authenticated MCP retrieval",
                    "mode": "planning",
                }
            },
        )
        assert prepared["candidate_memory_ids"] == []

    try:
        anyio.run(exercise)
        trace = runtime.snapshot()["traces"][0]
        assert trace["repo"] == "repository_001"
        assert trace["tenant"] == "tenant_001"
        authentication = configuration.authentication
        assert authentication is not None
        with sqlite3.connect(
            authentication.authorization_sqlite_path
        ) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM v3_authorization_decisions"
            ).fetchone()[0]
        assert count == 1
    finally:
        decisions.close()
        runtime.close()


def test_authenticated_mcp_denial_persists_without_trace(tmp_path):
    root = Path(__file__).resolve().parents[1]
    configuration = _authenticated_configuration(
        root,
        tmp_path,
        allow=False,
    )
    runtime = tbm.LocalAgentMemory.in_memory()
    opened = mcp_server._open_authenticated_runtime(
        configuration,
        runtime,
    )
    assert opened is not None
    authenticated_runtime, decisions = opened
    server = mcp_server.create_mcp_server(
        configuration,
        runtime,
        authenticated_runtime=authenticated_runtime,
    )

    async def exercise() -> None:
        with pytest.raises(mcp_server.ToolError) as failure:
            await server._tool_manager.call_tool(
                "tbm_prepare_memory",
                {
                    "request": {
                        "task": "denied authenticated MCP retrieval",
                        "mode": "planning",
                    }
                },
            )
        assert "TBM_SERVICE_AUTHORIZATION_DENIED" in str(failure.value)

    try:
        anyio.run(exercise)
        assert runtime.snapshot()["traces"] == []
    finally:
        decisions.close()
        runtime.close()


def test_authenticated_mcp_bootstrap_rejects_inactive_environment(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    configuration = _authenticated_configuration(root, tmp_path)
    authentication = configuration.authentication
    assert authentication is not None
    registry = json.loads(
        authentication.registry_path.read_text(encoding="utf-8")
    )
    registry["environments"][0]["status"] = "disabled"
    authentication.registry_path.write_text(
        json.dumps(registry),
        encoding="utf-8",
    )
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with pytest.raises(ValueError, match="active registry identities"):
            mcp_server._open_authenticated_runtime(
                configuration,
                runtime,
            )
    finally:
        runtime.close()


def test_authenticated_mcp_bootstrap_sanitizes_authority_open_failure(
    tmp_path,
    monkeypatch,
):
    root = Path(__file__).resolve().parents[1]
    configuration = _authenticated_configuration(root, tmp_path)
    runtime = tbm.LocalAgentMemory.in_memory()

    def fail_connect(_cls, *_args, **_kwargs):
        raise RuntimeError("secret database detail")

    monkeypatch.setattr(
        mcp_server.SQLiteAuthorizationV3Repository,
        "connect",
        classmethod(fail_connect),
    )
    try:
        with pytest.raises(
            tbm.AgentMemoryError,
            match="authorization authority could not be opened",
        ) as failure:
            mcp_server._open_authenticated_runtime(
                configuration,
                runtime,
            )
        assert failure.value.code == "TBM_MCP_AUTH_STARTUP_FAILED"
        assert "secret" not in str(failure.value)
    finally:
        runtime.close()


def test_authenticated_mcp_request_schema_rejects_identity_fields():
    schema = mcp_server.PrepareMemoryRequest.model_json_schema()
    properties = schema["properties"]
    for field_name in (
        "principal_id",
        "agent_client_id",
        "tenant",
        "tenant_id",
        "repo",
        "repository_id",
        "repository_reference",
        "environment_id",
        "authorization_event_id",
        "registry_path",
        "authorization_sqlite_path",
    ):
        assert field_name not in properties
    with pytest.raises(ValueError, match="principal_id"):
        mcp_server.PrepareMemoryRequest.model_validate(
            {
                "task": "must reject identity",
                "mode": "planning",
                "principal_id": "attacker",
            }
        )


def test_mcp_server_rejects_wrong_configuration_types():
    root = Path(__file__).resolve().parents[1]
    runtime = tbm.LocalAgentMemory.in_memory()

    with pytest.raises(TypeError, match="MCPServerConfiguration"):
        mcp_server.create_mcp_server({}, runtime)
    with pytest.raises(TypeError, match="LocalAgentMemory"):
        mcp_server.create_mcp_server(
            _configuration(root),
            object(),
        )
    authenticated_configuration = mcp_server.MCPServerConfiguration(
        repo_path=root,
        storage_mode="memory",
        authentication=mcp_server.MCPAuthenticationConfiguration(
            registry_path=(
                root / "examples" / "entity_registry_v3.example.json"
            ),
            authorization_sqlite_path=root / ".tbm" / "auth.sqlite3",
            principal_id="principal_tenant_001",
            agent_client_id="agent_client_001",
            environment_id="environment_001",
        ),
    )
    with pytest.raises(ValueError, match="must be paired"):
        mcp_server.create_mcp_server(
            authenticated_configuration,
            runtime,
        )
    runtime.close()


def test_mcp_frame_parser_is_bounded_and_duplicate_rejecting():
    canonical = mcp_server._canonical_mcp_frame(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    )
    assert canonical == (
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    )

    with pytest.raises(ValueError, match="duplicate object key"):
        mcp_server._canonical_mcp_frame(
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n'
        )
    with pytest.raises(ValueError, match="non-finite number"):
        mcp_server._canonical_mcp_frame(
            b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n'
        )
    with pytest.raises(ValueError, match="maximum size"):
        mcp_server._canonical_mcp_frame(
            b'{"value":"too long"}\n',
            max_bytes=5,
        )


def test_bounded_stdin_drains_oversized_frame_and_continues():
    source = io.BytesIO(
        b'{"oversized":"abcdefghijklmnopqrstuvwxyz'
        b'abcdefghijklmnopqrstuvwxyz"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    bounded = mcp_server._BoundedStdin(source, max_bytes=50)

    async def read_two() -> tuple[str, str]:
        return await anext(bounded), await anext(bounded)

    first, second = anyio.run(read_two)

    assert first == "\n"
    assert json.loads(second)["id"] == 2


def test_bounded_stdin_validates_constructor_invalid_input_and_eof():
    with pytest.raises(TypeError, match="binary readline"):
        mcp_server._BoundedStdin(object())
    with pytest.raises(ValueError, match="positive integer"):
        mcp_server._BoundedStdin(io.BytesIO(), max_bytes=0)

    bounded = mcp_server._BoundedStdin(
        io.BytesIO(b'{"id":NaN}\n'),
    )

    async def read_invalid_then_eof() -> tuple[str, str]:
        invalid = await anext(bounded)
        with pytest.raises(StopAsyncIteration):
            await anext(bounded)
        return invalid, "eof"

    assert anyio.run(read_invalid_then_eof) == ("\n", "eof")


def test_mcp_configuration_requires_explicit_safe_storage(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    parser = mcp_server._build_parser()

    memory = mcp_server._configuration_from_args(
        parser.parse_args(
            ["--repo-path", str(root), "--memory"]
        )
    )
    assert memory.storage_mode == "memory"
    assert memory.repo_path == root

    sqlite = mcp_server._configuration_from_args(
        parser.parse_args(
            [
                "--repo-path",
                str(root),
                "--sqlite",
                str(tmp_path / "memory.sqlite3"),
                "--tenant",
                "tenant",
            ]
        )
    )
    assert sqlite.storage_mode == "sqlite"
    assert sqlite.sqlite_path == (tmp_path / "memory.sqlite3")
    assert sqlite.tenant == "tenant"

    postgres = mcp_server._configuration_from_args(
        parser.parse_args(
            [
                "--repo-path",
                str(root),
                "--postgres-env",
                "TBM_TEST_CONNINFO",
            ]
        )
    )
    assert postgres.storage_mode == "postgres"
    assert postgres.postgres_env == "TBM_TEST_CONNINFO"

    authenticated = mcp_server._configuration_from_args(
        parser.parse_args(
            [
                "--repo-path",
                str(root),
                "--memory",
                "--auth-registry",
                str(root / "examples" / "entity_registry_v3.example.json"),
                "--auth-sqlite",
                str(tmp_path / "authorization.sqlite3"),
                "--auth-principal-id",
                "principal_tenant_001",
                "--auth-agent-client-id",
                "agent_client_001",
                "--auth-environment-id",
                "environment_001",
            ]
        )
    )
    assert authenticated.authentication is not None
    assert (
        authenticated.authentication.environment_id == "environment_001"
    )

    with pytest.raises(ValueError, match="requires --auth-registry"):
        mcp_server._configuration_from_args(
            parser.parse_args(
                [
                    "--repo-path",
                    str(root),
                    "--memory",
                    "--auth-registry",
                    str(
                        root
                        / "examples"
                        / "entity_registry_v3.example.json"
                    ),
                ]
            )
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        mcp_server._configuration_from_args(
            parser.parse_args(
                [
                    "--repo-path",
                    str(root),
                    "--memory",
                    "--tenant",
                    "caller-tenant",
                    "--auth-registry",
                    str(
                        root
                        / "examples"
                        / "entity_registry_v3.example.json"
                    ),
                    "--auth-sqlite",
                    str(tmp_path / "authorization.sqlite3"),
                    "--auth-principal-id",
                    "principal_tenant_001",
                    "--auth-agent-client-id",
                    "agent_client_001",
                    "--auth-environment-id",
                    "environment_001",
                ]
            )
        )

    with pytest.raises(ValueError, match="parent directory"):
        mcp_server._configuration_from_args(
            parser.parse_args(
                [
                    "--repo-path",
                    str(root),
                    "--sqlite",
                    str(tmp_path / "missing" / "memory.sqlite3"),
                ]
            )
        )


def test_authenticated_mcp_configuration_validates_relative_paths_and_ids(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "registry.json").write_bytes(
        (root / "examples" / "entity_registry_v3.example.json").read_bytes()
    )
    parser = mcp_server._build_parser()

    arguments = [
        "--repo-path",
        str(repo_path),
        "--memory",
        "--auth-registry",
        "registry.json",
        "--auth-sqlite",
        "authorization.sqlite3",
        "--auth-principal-id",
        "principal_tenant_001",
        "--auth-agent-client-id",
        "agent_client_001",
        "--auth-environment-id",
        "environment_001",
    ]
    configuration = mcp_server._configuration_from_args(
        parser.parse_args(arguments)
    )
    authentication = configuration.authentication
    assert authentication is not None
    assert authentication.registry_path == repo_path / "registry.json"
    assert (
        authentication.authorization_sqlite_path
        == repo_path / "authorization.sqlite3"
    )

    invalid_registry = list(arguments)
    invalid_registry[invalid_registry.index("registry.json")] = "."
    with pytest.raises(ValueError, match="regular file"):
        mcp_server._configuration_from_args(
            parser.parse_args(invalid_registry)
        )

    invalid_parent = list(arguments)
    invalid_parent[
        invalid_parent.index("authorization.sqlite3")
    ] = "missing/authorization.sqlite3"
    with pytest.raises(ValueError, match="parent directory"):
        mcp_server._configuration_from_args(
            parser.parse_args(invalid_parent)
        )

    invalid_identity = list(arguments)
    invalid_identity[
        invalid_identity.index("principal_tenant_001")
    ] = ""
    with pytest.raises(ValueError, match="nonblank bounded"):
        mcp_server._configuration_from_args(
            parser.parse_args(invalid_identity)
        )

    not_directory = repo_path / "registry.json"
    with pytest.raises(ValueError, match="must be a directory"):
        mcp_server._configuration_from_args(
            parser.parse_args(
                ["--repo-path", str(not_directory), "--memory"]
            )
        )


def test_open_runtime_supports_memory_and_sqlite(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    memory = mcp_server._open_runtime(_configuration(root))
    assert memory.health()["memory_metrics"]["decision_count"] == 0
    memory.close()

    database = tmp_path / "memory.sqlite3"
    sqlite = mcp_server._open_runtime(
        _configuration(
            root,
            storage_mode="sqlite",
            sqlite_path=database,
        )
    )
    sqlite.close()
    assert database.is_file()
    unauthenticated_runtime = tbm.LocalAgentMemory.in_memory()
    assert (
        mcp_server._open_authenticated_runtime(
            _configuration(root),
            unauthenticated_runtime,
        )
        is None
    )
    unauthenticated_runtime.close()


def test_open_runtime_reads_postgres_only_from_named_environment(
    monkeypatch,
):
    root = Path(__file__).resolve().parents[1]
    configuration = mcp_server.MCPServerConfiguration(
        repo_path=root,
        storage_mode="postgres",
        postgres_env="TBM_TEST_CONNINFO",
    )

    monkeypatch.delenv("TBM_TEST_CONNINFO", raising=False)
    with pytest.raises(ValueError, match="environment variable is missing"):
        mcp_server._open_runtime(configuration)

    monkeypatch.setenv("TBM_TEST_CONNINFO", "secret-conninfo")
    observed = {}

    def open_postgres(_cls, conninfo):
        observed["conninfo"] = conninfo
        return tbm.LocalAgentMemory.in_memory()

    monkeypatch.setattr(
        tbm.LocalAgentMemory,
        "open_postgres",
        classmethod(open_postgres),
    )
    runtime = mcp_server._open_runtime(configuration)
    assert observed == {"conninfo": "secret-conninfo"}
    runtime.close()


def test_mcp_main_reports_bounded_startup_failure(tmp_path, capsys):
    root = Path(__file__).resolve().parents[1]

    result = mcp_server.main(
        [
            "--repo-path",
            str(root),
            "--sqlite",
            str(tmp_path / "missing" / "memory.sqlite3"),
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == {
        "code": "TBM_MCP_STARTUP_FAILED",
        "category": "input",
        "message": "SQLite database parent directory must exist",
        "operation": "open",
        "retryable": False,
    }

    secret_path = tmp_path / "secret-missing-repository"
    result = mcp_server.main(
        [
            "--repo-path",
            str(secret_path),
            "--memory",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["message"] == (
        "configured MCP path could not be opened"
    )
    assert str(secret_path) not in json.dumps(payload)


def test_mcp_main_runs_authenticated_profile(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    authorization_path = tmp_path / "authorization.sqlite3"
    observed = []

    monkeypatch.setattr(
        mcp_server,
        "run_stdio_server",
        lambda server: observed.append(server),
    )
    result = mcp_server.main(
        [
            "--repo-path",
            str(root),
            "--memory",
            "--auth-registry",
            str(root / "examples" / "entity_registry_v3.example.json"),
            "--auth-sqlite",
            str(authorization_path),
            "--auth-principal-id",
            "principal_tenant_001",
            "--auth-agent-client-id",
            "agent_client_001",
            "--auth-environment-id",
            "environment_001",
        ]
    )

    assert result == 0
    assert len(observed) == 1
    assert authorization_path.is_file()


def test_mcp_entry_reports_missing_optional_external_dependency(
    monkeypatch,
    capsys,
):
    original_import = builtins.__import__

    def import_without_anyio(name, *args, **kwargs):
        if name == "mcp_server":
            raise ModuleNotFoundError(
                "No module named 'anyio'",
                name="anyio",
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_anyio)

    assert mcp_entry.main([]) == 2
    assert capsys.readouterr().err == (
        "trace-backed-memory MCP support is not installed; "
        "install trace-backed-memory[mcp]\n"
    )


def test_mcp_stdio_full_runtime_and_process_local_restart(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "memory.sqlite3"
    parameters = _server_parameters(root, database)

    async def first_process() -> str:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.instructions == (
                    mcp_server.MCP_SERVER_INSTRUCTIONS
                )
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "tbm_capabilities",
                    "tbm_health",
                    "tbm_prepare_memory",
                    "tbm_finalize_memory",
                    "tbm_complete_run",
                    "tbm_cancel_run",
                }

                capabilities = await session.call_tool(
                    "tbm_capabilities",
                    {},
                )
                assert capabilities.isError is False
                assert capabilities.structuredContent[
                    "protocol_version"
                ] == "tbm.agent.v1"

                prepared = await session.call_tool(
                    "tbm_prepare_memory",
                    {
                        "request": {
                            "task": "inspect the local repository",
                            "mode": "planning",
                        }
                    },
                )
                assert prepared.isError is False
                prepared_payload = prepared.structuredContent
                assert prepared_payload["candidate_memory_ids"] == []

                finalized = await session.call_tool(
                    "tbm_finalize_memory",
                    {
                        "request": {
                            "request_id": prepared_payload["request_id"],
                            "use_memory": False,
                            "allowed_memory_ids": [],
                            "blocked_memory_ids": [],
                            "reason": "no applicable memory",
                            "risk": "none",
                            "recommended_injection": "none",
                        }
                    },
                )
                assert finalized.isError is False
                finalized_payload = finalized.structuredContent
                assert finalized_payload["snippet"] == ""

                completed = await session.call_tool(
                    "tbm_complete_run",
                    {
                        "request": {
                            "decision_id": (
                                finalized_payload["decision_id"]
                            ),
                            "eval_result": "pass",
                            "latency_ms": 1,
                        }
                    },
                )
                assert completed.isError is False
                assert completed.structuredContent["eval_result"] == "pass"

                cancel_prepared = await session.call_tool(
                    "tbm_prepare_memory",
                    {
                        "request": {
                            "task": "cancel this local request",
                            "mode": "planning",
                        }
                    },
                )
                cancel_id = cancel_prepared.structuredContent[
                    "request_id"
                ]
                canceled = await session.call_tool(
                    "tbm_cancel_run",
                    {"request": {"request_id": cancel_id}},
                )
                assert canceled.structuredContent["canceled"] is True

                canceled_finalize = await session.call_tool(
                    "tbm_finalize_memory",
                    {
                        "request": {
                            "request_id": cancel_id,
                            "use_memory": False,
                            "allowed_memory_ids": [],
                            "blocked_memory_ids": [],
                            "reason": "canceled",
                            "risk": "none",
                            "recommended_injection": "none",
                        }
                    },
                )
                canceled_error = _tool_error_payload(
                    canceled_finalize
                )
                assert canceled_error["error"]["code"] == (
                    "TBM_AGENT_REQUEST_NOT_FOUND"
                )

                abandoned = await session.call_tool(
                    "tbm_prepare_memory",
                    {
                        "request": {
                            "task": "leave this request process-local",
                            "mode": "planning",
                        }
                    },
                )
                return abandoned.structuredContent["request_id"]

    abandoned_request_id = anyio.run(first_process)

    async def second_process() -> None:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                new_prepared = await session.call_tool(
                    "tbm_prepare_memory",
                    {
                        "request": {
                            "task": "new request after restart",
                            "mode": "planning",
                        }
                    },
                )
                new_request_id = new_prepared.structuredContent[
                    "request_id"
                ]
                assert new_request_id != abandoned_request_id

                result = await session.call_tool(
                    "tbm_finalize_memory",
                    {
                        "request": {
                            "request_id": abandoned_request_id,
                            "use_memory": False,
                            "allowed_memory_ids": [],
                            "blocked_memory_ids": [],
                            "reason": "retry after restart",
                            "risk": "none",
                            "recommended_injection": "none",
                        }
                    },
                )
                payload = _tool_error_payload(result)
                assert payload["error"]["code"] == (
                    "TBM_AGENT_REQUEST_NOT_FOUND"
                )

                stale_cancel = await session.call_tool(
                    "tbm_cancel_run",
                    {"request": {"request_id": abandoned_request_id}},
                )
                cancel_payload = _tool_error_payload(stale_cancel)
                assert cancel_payload["error"]["code"] == (
                    "TBM_AGENT_REQUEST_NOT_FOUND"
                )

                canceled = await session.call_tool(
                    "tbm_cancel_run",
                    {"request": {"request_id": new_request_id}},
                )
                assert canceled.structuredContent["canceled"] is True

                health = await session.call_tool("tbm_health", {})
                assert health.isError is False
                assert health.structuredContent[
                    "pending_request_count"
                ] == 0
                assert health.structuredContent[
                    "finalized_request_replay_count"
                ] == 0
                assert health.structuredContent["memory_run_metrics"][
                    "complete_count"
                ] == 1

    anyio.run(second_process)

    with tbm.LocalAgentMemory.open_sqlite(
        database,
        initialize=False,
    ) as runtime:
        snapshot = runtime.snapshot()
    assert len(snapshot["usage_logs"]) == 1
    completed_trace = next(
        trace
        for trace in snapshot["traces"]
        if trace["trace_id"]
        == snapshot["usage_logs"][0]["trace_id"]
    )
    assert completed_trace["eval_result"] == "pass"
