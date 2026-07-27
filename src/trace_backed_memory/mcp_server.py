from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, BinaryIO, Literal, NoReturn, Sequence

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._ingestion import (
    CLI_JSON_FILE_MAX_BYTES,
    CLI_JSON_MAX_DEPTH,
    CLI_JSON_MAX_NODES,
    decode_bounded_utf8,
    parse_bounded_json,
)
from .agent import (
    AGENT_PROTOCOL_VERSION,
    AgentMemoryError,
    LocalAgentMemory,
    agent_capabilities,
    capture_local_trace,
)
from .models import MemoryContext, MemoryRunMeasurement
from .policy import (
    LLM_GATE_MAX_CANDIDATES,
    LLM_GATE_PROMPT_MAX_CHARS,
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)
from .store import RETRIEVAL_QUERY_MAX_CHARS


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

Mode = Literal[
    "debug",
    "repair",
    "regression",
    "planning",
    "eval",
    "production",
]
Risk = Literal["none", "low", "medium", "high"]
InjectionMode = Literal[
    "none",
    "short_summary",
    "full_case_summary",
    "pointer_only",
]
MeasuredResult = Literal["pass", "fail", "error"]
StorageMode = Literal["memory", "sqlite", "postgres"]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PrepareMemoryRequest(_StrictRequest):
    task: str = Field(min_length=1, max_length=LLM_GATE_PROMPT_MAX_CHARS)
    mode: Mode
    tool: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MEMORY_ID_MAX_CHARS,
    )
    trace_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MEMORY_ID_MAX_CHARS,
    )
    prompt_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    prompt_family: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    tool_schema_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    model_family: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    eval_suite: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    input_hash: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    task_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    failure_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    query: str | None = Field(
        default=None,
        max_length=RETRIEVAL_QUERY_MAX_CHARS,
    )
    semantic_scores: dict[str, int | float] | None = None
    max_candidates: int | None = Field(
        default=None,
        ge=1,
        le=LLM_GATE_MAX_CANDIDATES,
    )
    minimum_score: int | float | None = None
    context_summary: str = Field(
        default="",
        max_length=LLM_GATE_PROMPT_MAX_CHARS,
    )

    @field_validator("task")
    @classmethod
    def _task_is_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must be nonblank")
        return value

    @field_validator("run_id", "trace_id")
    @classmethod
    def _identifier_is_nonblank(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("identifier must be nonblank")
        return value


class FinalizeMemoryRequest(_StrictRequest):
    request_id: str = Field(
        min_length=1,
        max_length=MEMORY_ID_MAX_CHARS,
    )
    use_memory: bool
    allowed_memory_ids: list[str] = Field(
        max_length=LLM_GATE_MAX_CANDIDATES,
    )
    blocked_memory_ids: list[str] = Field(
        max_length=LLM_GATE_MAX_CANDIDATES,
    )
    reason: str = Field(
        max_length=MEMORY_DECISION_REASON_MAX_CHARS,
    )
    risk: Risk
    recommended_injection: InjectionMode


class CompleteRunRequest(_StrictRequest):
    decision_id: str = Field(
        min_length=1,
        max_length=MEMORY_ID_MAX_CHARS,
    )
    eval_result: MeasuredResult
    memory_caused_failure: bool = False
    output_hash: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )
    tool_outputs: list[dict[str, Any]] | None = None
    latency_ms: int | None = None
    cost_usd: int | float | None = None
    error: str | None = Field(default=None, min_length=1)
    trace_uri: str | None = Field(
        default=None,
        min_length=1,
        max_length=METADATA_VALUE_MAX_CHARS,
    )


class CancelRunRequest(_StrictRequest):
    request_id: str = Field(
        min_length=1,
        max_length=MEMORY_ID_MAX_CHARS,
    )


@dataclass(frozen=True)
class MCPServerConfiguration:
    repo_path: Path
    storage_mode: StorageMode
    tenant: str | None = None
    sqlite_path: Path | None = None
    postgres_env: str | None = None


@dataclass
class _MCPApplication:
    configuration: MCPServerConfiguration
    runtime: LocalAgentMemory


def create_mcp_server(
    configuration: MCPServerConfiguration,
    runtime: LocalAgentMemory,
) -> FastMCP:
    """Build the runtime-only MCP profile over one process-owned façade."""
    if type(configuration) is not MCPServerConfiguration:
        raise TypeError(
            "configuration must be exactly an MCPServerConfiguration"
        )
    if type(runtime) is not LocalAgentMemory:
        raise TypeError("runtime must be exactly a LocalAgentMemory")
    application = _MCPApplication(configuration, runtime)
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
        return agent_capabilities().to_dict()

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
            return application.runtime.health()
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
            trace = capture_local_trace(
                application.configuration.repo_path,
                run_id=request.run_id,
                trace_id=request.trace_id,
                tenant=application.configuration.tenant,
                prompt_version=request.prompt_version,
                prompt_family=request.prompt_family,
                tool_schema_version=request.tool_schema_version,
                model=request.model,
                eval_suite=request.eval_suite,
                input_hash=request.input_hash,
                tool_names=(
                    () if request.tool is None else (request.tool,)
                ),
            )
            if trace.repo is None:
                raise ValueError(
                    "configured repository has no canonical local name"
                )
            context = MemoryContext(
                mode=request.mode,
                repo=trace.repo,
                commit_sha=trace.commit_sha,
                branch=trace.branch,
                prompt_version=request.prompt_version,
                prompt_family=request.prompt_family,
                tool=request.tool,
                tool_schema_version=request.tool_schema_version,
                model=request.model,
                model_family=request.model_family,
                eval_suite=request.eval_suite,
                task_type=request.task_type,
                failure_type=request.failure_type,
                tenant=application.configuration.tenant,
                input_hash=request.input_hash,
            )
            prepared = application.runtime.prepare_with_git_ancestry(
                trace,
                context,
                repo_path=application.configuration.repo_path,
                task=request.task,
                query=request.query,
                semantic_scores=request.semantic_scores,
                max_candidates=request.max_candidates,
                minimum_score=request.minimum_score,
                context_summary=request.context_summary,
            )
            return prepared.to_dict()
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
            finalized = application.runtime.finalize(
                request.request_id,
                {
                    "use_memory": request.use_memory,
                    "allowed_memory_ids": request.allowed_memory_ids,
                    "blocked_memory_ids": request.blocked_memory_ids,
                    "reason": request.reason,
                    "risk": request.risk,
                    "recommended_injection": (
                        request.recommended_injection
                    ),
                },
            )
            return finalized.to_dict()
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
            measurement = MemoryRunMeasurement(
                eval_result=request.eval_result,
                memory_caused_failure=request.memory_caused_failure,
                output_hash=request.output_hash,
                tool_outputs=(
                    None
                    if request.tool_outputs is None
                    else tuple(request.tool_outputs)
                ),
                latency_ms=request.latency_ms,
                cost_usd=request.cost_usd,
                error=request.error,
                trace_uri=request.trace_uri,
            )
            completed = application.runtime.complete(
                request.decision_id,
                measurement,
            )
            return completed.to_dict()
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
            application.runtime.cancel(request.request_id)
            return {
                "protocol_version": AGENT_PROTOCOL_VERSION,
                "request_id": request.request_id,
                "canceled": True,
            }
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
    if isinstance(error, AgentMemoryError):
        public_error = error
    elif isinstance(error, (TypeError, ValueError, OverflowError)):
        public_error = AgentMemoryError(
            "TBM_AGENT_INVALID_INPUT",
            "input",
            operation,
            str(error),
        )
    else:
        public_error = AgentMemoryError(
            "TBM_MCP_INTERNAL_ERROR",
            "internal",
            operation,
            "MCP runtime operation failed",
            retryable=True,
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
    if args.memory:
        return MCPServerConfiguration(
            repo_path=repo_path,
            storage_mode="memory",
            tenant=tenant,
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        configuration = _configuration_from_args(args)
        runtime = _open_runtime(configuration)
    except (AgentMemoryError, OSError, ValueError) as error:
        message = (
            error.to_dict()
            if isinstance(error, AgentMemoryError)
            else {
                "protocol_version": AGENT_PROTOCOL_VERSION,
                "error": {
                    "code": "TBM_MCP_STARTUP_FAILED",
                    "category": "input",
                    "message": str(error)[:2_048],
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
        server = create_mcp_server(configuration, runtime)
        run_stdio_server(server)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
