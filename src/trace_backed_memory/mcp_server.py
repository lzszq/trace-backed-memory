from __future__ import annotations

import argparse
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import BinaryIO, Literal, NoReturn, Sequence

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations

from ._ingestion import (
    CLI_JSON_FILE_MAX_BYTES,
    CLI_JSON_MAX_DEPTH,
    CLI_JSON_MAX_NODES,
    decode_bounded_utf8,
    parse_bounded_json,
    read_bounded_utf8,
)
from ._timestamps import utc_timestamp
from .agent import (
    AGENT_PROTOCOL_VERSION,
    AgentMemoryError,
    LocalAgentMemory,
)
from .agent_wire_v1 import (
    AgentProtocolConfiguration,
    AgentProtocolDispatcher,
    CancelRunRequest,
    CompleteRunRequest,
    FinalizeMemoryRequest,
    PrepareMemoryRequest,
    public_agent_error,
)
from .authenticated_agent_v3 import (
    AuthenticatedLocalAgentMemory,
)
from .entity_registry_v3 import (
    ENTITY_REGISTRY_JSON_MAX_BYTES,
    EntityRegistrySnapshot,
    loads_entity_registry,
)
from .policy import METADATA_VALUE_MAX_CHARS
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
)
from .sqlite_authorization_v3 import SQLiteAuthorizationV3Repository


MCP_INPUT_FRAME_MAX_BYTES = CLI_JSON_FILE_MAX_BYTES
MCP_INPUT_MAX_NODES = CLI_JSON_MAX_NODES
MCP_INPUT_MAX_DEPTH = CLI_JSON_MAX_DEPTH
MCP_SERVER_INSTRUCTIONS = (
    "Call tbm_prepare_memory before using historical memory. Never use or "
    "reconstruct memory omitted by System Gate. Finalize one prepared request "
    "with tbm_finalize_memory and use only its snippet. Then call "
    "tbm_complete_run, or call tbm_cancel_run before finalization. Pending "
    "requests are process-local. Never claim to verify or activate a lesson."
)

StorageMode = Literal["memory", "sqlite", "postgres"]


@dataclass(frozen=True)
class MCPAuthenticationConfiguration:
    registry_path: Path
    authorization_sqlite_path: Path
    principal_id: str
    agent_client_id: str
    environment_id: str


@dataclass(frozen=True)
class MCPServerConfiguration:
    repo_path: Path
    storage_mode: StorageMode
    tenant: str | None = None
    sqlite_path: Path | None = None
    postgres_env: str | None = None
    authentication: MCPAuthenticationConfiguration | None = None


def create_mcp_server(
    configuration: MCPServerConfiguration,
    runtime: LocalAgentMemory,
    *,
    authenticated_runtime: AuthenticatedLocalAgentMemory | None = None,
) -> FastMCP:
    """Build the runtime-only MCP profile over one process-owned façade."""
    if type(configuration) is not MCPServerConfiguration:
        raise TypeError(
            "configuration must be exactly an MCPServerConfiguration"
        )
    if configuration.authentication is not None and (
        type(configuration.authentication)
        is not MCPAuthenticationConfiguration
    ):
        raise TypeError(
            "authentication must be exactly "
            "MCPAuthenticationConfiguration or None"
        )
    if type(runtime) is not LocalAgentMemory:
        raise TypeError("runtime must be exactly a LocalAgentMemory")
    if authenticated_runtime is not None and (
        type(authenticated_runtime) is not AuthenticatedLocalAgentMemory
    ):
        raise TypeError(
            "authenticated_runtime must be exactly "
            "AuthenticatedLocalAgentMemory or None"
        )
    if (configuration.authentication is None) != (
        authenticated_runtime is None
    ):
        raise ValueError(
            "authenticated MCP configuration and runtime must be paired"
        )
    dispatcher = AgentProtocolDispatcher(
        AgentProtocolConfiguration(
            repo_path=configuration.repo_path,
            tenant=configuration.tenant,
        ),
        runtime,
        authenticated_runtime=authenticated_runtime,
    )
    server = FastMCP(
        "Trace-backed Memory",
        instructions=MCP_SERVER_INSTRUCTIONS,
        log_level="ERROR",
    )

    @server.tool(
        name="tbm_capabilities",
        title="Trace-backed Memory capabilities",
        description=(
            "Return the versioned local agent contract and hard limits."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def tbm_capabilities() -> dict[str, object]:
        return dispatcher.capabilities()

    @server.tool(
        name="tbm_health",
        title="Trace-backed Memory health",
        description=(
            "Return non-sensitive pending, recovery, and outcome metrics."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def tbm_health() -> dict[str, object]:
        try:
            return dispatcher.health()
        except Exception as error:
            _raise_tool_error(error, "health")

    @server.tool(
        name="tbm_prepare_memory",
        title="Prepare trace-backed memory",
        description=(
            "Capture Git provenance from the configured repository, register "
            "a pending Trace, require complete Git ancestry evidence, run "
            "System Gate, and return the bounded applicability prompt."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def tbm_prepare_memory(
        request: PrepareMemoryRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.prepare(request)
        except Exception as error:
            _raise_tool_error(error, "prepare")

    @server.tool(
        name="tbm_finalize_memory",
        title="Finalize trace-backed memory",
        description=(
            "Apply one structured semantic narrowing decision. The returned "
            "snippet is the only memory that may enter the runtime prompt."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def tbm_finalize_memory(
        request: FinalizeMemoryRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.finalize(request)
        except Exception as error:
            _raise_tool_error(error, "finalize")

    @server.tool(
        name="tbm_complete_run",
        title="Complete a trace-backed memory run",
        description=(
            "Atomically seal the linked Trace and usage decision with an "
            "explicit measured result and optional execution evidence."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def tbm_complete_run(
        request: CompleteRunRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.complete(request)
        except Exception as error:
            _raise_tool_error(error, "complete")

    @server.tool(
        name="tbm_cancel_run",
        title="Cancel a prepared trace-backed memory request",
        description=(
            "Release one process-local prepared request before finalization."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def tbm_cancel_run(
        request: CancelRunRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.cancel(request)
        except Exception as error:
            _raise_tool_error(error, "cancel")

    return server


class _BoundedStdin:
    """Async JSONL iterator that bounds and canonicalizes MCP input frames."""

    def __init__(
        self,
        source: BinaryIO,
        *,
        max_bytes: int = MCP_INPUT_FRAME_MAX_BYTES,
    ) -> None:
        if not hasattr(source, "readline"):
            raise TypeError("source must provide binary readline()")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._source = source
        self._max_bytes = max_bytes

    def __aiter__(self) -> "_BoundedStdin":
        return self

    async def __anext__(self) -> str:
        frame, overflow = await anyio.to_thread.run_sync(
            self._read_frame
        )
        if frame is None:
            raise StopAsyncIteration
        if overflow:
            return "\n"
        try:
            return _canonical_mcp_frame(
                frame,
                max_bytes=self._max_bytes,
            )
        except (UnicodeError, ValueError, TypeError, OverflowError):
            return "\n"

    def _read_frame(self) -> tuple[bytes | None, bool]:
        frame = self._source.readline(self._max_bytes + 1)
        if frame == b"":
            return None, False
        if len(frame) <= self._max_bytes:
            return frame, False
        while not frame.endswith(b"\n"):
            frame = self._source.readline(self._max_bytes + 1)
            if frame == b"":
                break
        return b"", True


def _canonical_mcp_frame(
    frame: bytes,
    *,
    max_bytes: int = MCP_INPUT_FRAME_MAX_BYTES,
) -> str:
    source = decode_bounded_utf8(
        frame,
        max_bytes=max_bytes,
        description="MCP input frame",
    )
    payload = parse_bounded_json(
        source,
        description="MCP input frame",
        max_nodes=MCP_INPUT_MAX_NODES,
        max_depth=MCP_INPUT_MAX_DEPTH,
    )
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


async def _run_stdio_async(server: FastMCP) -> None:
    bounded_stdin = _BoundedStdin(sys.stdin.buffer)
    async with stdio_server(stdin=bounded_stdin) as (
        read_stream,
        write_stream,
    ):
        await server._mcp_server.run(
            read_stream,
            write_stream,
            server._mcp_server.create_initialization_options(),
        )


def run_stdio_server(server: FastMCP) -> None:
    """Run one FastMCP server with the bounded strict JSONL transport."""
    if type(server) is not FastMCP:
        raise TypeError("server must be exactly a FastMCP instance")
    anyio.run(_run_stdio_async, server)


def _raise_tool_error(
    error: Exception,
    operation: Literal[
        "prepare",
        "finalize",
        "complete",
        "cancel",
        "health",
    ],
) -> NoReturn:
    public_error = public_agent_error(
        error,
        operation,
        internal_code="TBM_MCP_INTERNAL_ERROR",
        internal_message="MCP runtime operation failed",
    )
    raise ToolError(
        json.dumps(
            public_error.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    ) from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbm-mcp",
        description=(
            "Run the local runtime-only Trace-backed Memory MCP server "
            "over STDIO."
        ),
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        required=True,
        help="Explicit Git checkout root used to derive provenance.",
    )
    parser.add_argument(
        "--tenant",
        help=(
            "Optional fixed declared tenant applicability value. "
            "This is not authorization in schema version 2."
        ),
    )
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument(
        "--memory",
        action="store_true",
        help="Use explicit process-local, non-durable storage.",
    )
    storage.add_argument(
        "--sqlite",
        type=Path,
        help=(
            "Use a SQLite database. Relative paths resolve under --repo-path."
        ),
    )
    storage.add_argument(
        "--postgres-env",
        metavar="ENV_NAME",
        help=(
            "Read the PostgreSQL conninfo from this environment variable."
        ),
    )
    parser.add_argument(
        "--auth-registry",
        type=Path,
        help="Trusted bounded entity-registry v3 JSON file.",
    )
    parser.add_argument(
        "--auth-sqlite",
        type=Path,
        help="SQLite authorization-v3 authority database.",
    )
    parser.add_argument(
        "--auth-principal-id",
        help="Authenticated principal selected by trusted server startup.",
    )
    parser.add_argument(
        "--auth-agent-client-id",
        help="Authenticated agent client selected by trusted server startup.",
    )
    parser.add_argument(
        "--auth-environment-id",
        help="Server-owned environment target selected from the registry.",
    )
    return parser


def _configuration_from_args(
    args: argparse.Namespace,
) -> MCPServerConfiguration:
    repo_path = args.repo_path.resolve(strict=True)
    if not repo_path.is_dir():
        raise ValueError("repo_path must be a directory")
    tenant = args.tenant
    if tenant is not None and (
        not isinstance(tenant, str)
        or not tenant.strip()
        or len(tenant) > METADATA_VALUE_MAX_CHARS
    ):
        raise ValueError(
            "tenant must be a nonblank string at most "
            f"{METADATA_VALUE_MAX_CHARS} characters"
        )
    authentication = _authentication_configuration_from_args(
        args,
        repo_path,
    )
    if authentication is not None and tenant is not None:
        raise ValueError(
            "--tenant cannot be combined with authenticated MCP mode"
        )
    if args.memory:
        return MCPServerConfiguration(
            repo_path=repo_path,
            storage_mode="memory",
            tenant=tenant,
            authentication=authentication,
        )
    if args.sqlite is not None:
        sqlite_path = args.sqlite
        if not sqlite_path.is_absolute():
            sqlite_path = repo_path / sqlite_path
        sqlite_path = sqlite_path.resolve(strict=False)
        if not sqlite_path.parent.is_dir():
            raise ValueError("SQLite database parent directory must exist")
        return MCPServerConfiguration(
            repo_path=repo_path,
            storage_mode="sqlite",
            tenant=tenant,
            sqlite_path=sqlite_path,
            authentication=authentication,
        )
    postgres_env = args.postgres_env
    if (
        not isinstance(postgres_env, str)
        or not postgres_env
        or "=" in postgres_env
    ):
        raise ValueError(
            "postgres environment variable name must be nonblank "
            "and must not contain '='"
        )
    return MCPServerConfiguration(
        repo_path=repo_path,
        storage_mode="postgres",
        tenant=tenant,
        postgres_env=postgres_env,
        authentication=authentication,
    )


def _authentication_configuration_from_args(
    args: argparse.Namespace,
    repo_path: Path,
) -> MCPAuthenticationConfiguration | None:
    values = (
        args.auth_registry,
        args.auth_sqlite,
        args.auth_principal_id,
        args.auth_agent_client_id,
        args.auth_environment_id,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(
            "authenticated MCP mode requires --auth-registry, "
            "--auth-sqlite, --auth-principal-id, "
            "--auth-agent-client-id, and --auth-environment-id"
        )
    registry_path = args.auth_registry
    authorization_path = args.auth_sqlite
    if not registry_path.is_absolute():
        registry_path = repo_path / registry_path
    registry_path = registry_path.resolve(strict=True)
    if not registry_path.is_file():
        raise ValueError("auth registry must be a regular file")
    if not authorization_path.is_absolute():
        authorization_path = repo_path / authorization_path
    authorization_path = authorization_path.resolve(strict=False)
    if not authorization_path.parent.is_dir():
        raise ValueError(
            "authorization SQLite database parent directory must exist"
        )
    identifiers = (
        args.auth_principal_id,
        args.auth_agent_client_id,
        args.auth_environment_id,
    )
    if any(
        type(value) is not str
        or not value.strip()
        or len(value) > METADATA_VALUE_MAX_CHARS
        for value in identifiers
    ):
        raise ValueError(
            "authenticated MCP identity selectors must be nonblank bounded "
            "strings"
        )
    return MCPAuthenticationConfiguration(
        registry_path=registry_path,
        authorization_sqlite_path=authorization_path,
        principal_id=args.auth_principal_id,
        agent_client_id=args.auth_agent_client_id,
        environment_id=args.auth_environment_id,
    )


def _open_runtime(
    configuration: MCPServerConfiguration,
) -> LocalAgentMemory:
    if configuration.storage_mode == "memory":
        return LocalAgentMemory.in_memory()
    if configuration.storage_mode == "sqlite":
        if configuration.sqlite_path is None:
            raise ValueError(
                "sqlite_path is required for SQLite MCP storage"
            )
        return LocalAgentMemory.open_sqlite(
            configuration.sqlite_path,
            initialize=True,
        )
    if configuration.postgres_env is None:
        raise ValueError(
            "postgres_env is required for PostgreSQL MCP storage"
        )
    conninfo = os.environ.get(configuration.postgres_env)
    if conninfo is None or not conninfo.strip():
        raise ValueError(
            "configured PostgreSQL environment variable is missing"
        )
    return LocalAgentMemory.open_postgres(conninfo)


def _open_authenticated_runtime(
    configuration: MCPServerConfiguration,
    runtime: LocalAgentMemory,
) -> tuple[
    AuthenticatedLocalAgentMemory,
    SQLiteAuthorizationV3Repository,
] | None:
    authentication = configuration.authentication
    if authentication is None:
        return None

    def load_registry() -> EntityRegistrySnapshot:
        source = read_bounded_utf8(
            authentication.registry_path,
            max_bytes=ENTITY_REGISTRY_JSON_MAX_BYTES,
            description="authenticated MCP entity registry",
        )
        return loads_entity_registry(source)

    registry = load_registry()
    policy = registry.authorization_policy
    principal = next(
        (
            item
            for item in policy.principals
            if item.principal_id == authentication.principal_id
        ),
        None,
    )
    agent_client = next(
        (
            item
            for item in policy.agent_clients
            if item.agent_client_id == authentication.agent_client_id
        ),
        None,
    )
    environment = next(
        (
            item
            for item in registry.environments
            if item.environment_id == authentication.environment_id
        ),
        None,
    )
    tenant = (
        None
        if environment is None
        else next(
            (
                item
                for item in registry.tenants
                if item.tenant_id == environment.tenant_id
            ),
            None,
        )
    )
    organization = (
        None
        if tenant is None
        else next(
            (
                item
                for item in registry.organizations
                if item.organization_id == tenant.organization_id
            ),
            None,
        )
    )
    if (
        principal is None
        or principal.status != "active"
        or agent_client is None
        or agent_client.status != "active"
        or environment is None
        or environment.status != "active"
        or environment.repository_id is None
        or tenant is None
        or tenant.status != "active"
        or organization is None
        or organization.status != "active"
    ):
        raise ValueError(
            "authenticated MCP selectors must resolve to active registry "
            "identities and a repository-bound environment"
        )
    try:
        decisions = SQLiteAuthorizationV3Repository.connect(
            authentication.authorization_sqlite_path,
            initialize=True,
        )
    except Exception as error:
        raise AgentMemoryError(
            "TBM_MCP_AUTH_STARTUP_FAILED",
            "persistence",
            "open",
            "authenticated MCP authorization authority could not be opened",
        ) from error
    try:
        service = AuthenticatedRetrievalService(
            registry_provider=load_registry,
            decision_writer=decisions,
            clock=utc_timestamp,
            request_id_factory=lambda: (
                f"authorization_request_{secrets.token_hex(16)}"
            ),
        )
        facade = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=service,
            service_context=AuthenticatedServiceContext(
                principal=principal,
                agent_client=agent_client,
                tenant_id=environment.tenant_id,
                repository_reference=environment.repository_id,
                environment_id=environment.environment_id,
            ),
        )
    except Exception:
        decisions.close()
        raise
    return facade, decisions


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    runtime: LocalAgentMemory | None = None
    opened_authentication: tuple[
        AuthenticatedLocalAgentMemory,
        SQLiteAuthorizationV3Repository,
    ] | None = None
    try:
        configuration = _configuration_from_args(args)
        runtime = _open_runtime(configuration)
        opened_authentication = _open_authenticated_runtime(
            configuration,
            runtime,
        )
    except (
        AgentMemoryError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        if opened_authentication is not None:
            opened_authentication[1].close()
        if runtime is not None:
            runtime.close()
        public_message = (
            "configured MCP path could not be opened"
            if isinstance(error, OSError)
            else str(error)[:2_048]
        )
        message = (
            error.to_dict()
            if isinstance(error, AgentMemoryError)
            else {
                "protocol_version": AGENT_PROTOCOL_VERSION,
                "error": {
                    "code": "TBM_MCP_STARTUP_FAILED",
                    "category": "input",
                    "message": public_message,
                    "operation": "open",
                    "retryable": False,
                },
            }
        )
        sys.stderr.write(
            json.dumps(
                message,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    try:
        authenticated_runtime = (
            None
            if opened_authentication is None
            else opened_authentication[0]
        )
        server = create_mcp_server(
            configuration,
            runtime,
            authenticated_runtime=authenticated_runtime,
        )
        run_stdio_server(server)
    finally:
        if opened_authentication is not None:
            opened_authentication[1].close()
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
