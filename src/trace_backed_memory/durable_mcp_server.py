from __future__ import annotations

from dataclasses import dataclass
import json
from typing import NoReturn

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .durable_agent_wire_v1 import (
    DurableAbandonRequest,
    DurableAgentProtocolDispatcher,
    DurableAgentWireOperation,
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
from .durable_execution_v3 import AuthenticatedOutcomeEvaluatorContext
from .semantic_gate_service_v3 import AuthenticatedSemanticProviderContext
from .service_v3 import AuthenticatedServiceContext


DURABLE_MCP_SERVER_INSTRUCTIONS = (
    "Call tbm_durable_capabilities first. Use prepare, decide, finalize, "
    "start or resume, then complete; cancel before execution or abandon an "
    "execution lease. Persist session_id and exact session version. After a "
    "process restart call get_session and continue from durable state. Use "
    "only explicitly exposed injection content. Never curate, verify, or "
    "activate memory through this runtime-only profile."
)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


@dataclass(frozen=True)
class DurableMCPTrustedContexts:
    """Operator-owned identities bound to one local STDIO process."""

    service: AuthenticatedServiceContext
    provider: AuthenticatedSemanticProviderContext
    evaluator: AuthenticatedOutcomeEvaluatorContext

    def __post_init__(self) -> None:
        if type(self.service) is not AuthenticatedServiceContext:
            raise TypeError("durable MCP service context is invalid")
        if type(self.provider) is not AuthenticatedSemanticProviderContext:
            raise TypeError("durable MCP provider context is invalid")
        if type(self.evaluator) is not AuthenticatedOutcomeEvaluatorContext:
            raise TypeError("durable MCP evaluator context is invalid")


def create_durable_mcp_server(
    dispatcher: DurableAgentProtocolDispatcher,
    contexts: DurableMCPTrustedContexts,
) -> FastMCP:
    """Build the runtime-only durable MCP profile over one authority graph."""
    if type(dispatcher) is not DurableAgentProtocolDispatcher:
        raise TypeError(
            "dispatcher must be exactly a DurableAgentProtocolDispatcher"
        )
    if type(contexts) is not DurableMCPTrustedContexts:
        raise TypeError(
            "contexts must be exactly DurableMCPTrustedContexts"
        )
    server = FastMCP(
        "Trace-backed Memory durable runtime",
        instructions=DURABLE_MCP_SERVER_INSTRUCTIONS,
        log_level="ERROR",
    )

    @server.tool(
        name="tbm_durable_capabilities",
        title="Trace-backed Memory durable capabilities",
        description=(
            "Return the durable wire version, operations, limits, and "
            "content-exposure policy."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def tbm_durable_capabilities() -> dict[str, object]:
        capabilities = dict(dispatcher.capabilities())
        capabilities["transport_profile"] = "durable-v3"
        capabilities["transport_security"] = "trusted-local-stdio"
        capabilities["peer_authentication"] = False
        return capabilities

    @server.tool(
        name="tbm_durable_prepare",
        title="Prepare durable trace-backed memory",
        description=(
            "Authorize retrieval, persist a GateSession, and store exact "
            "retrieval and System Gate evidence."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_prepare(
        request: DurablePrepareRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.prepare(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "prepare")

    @server.tool(
        name="tbm_durable_decide",
        title="Record a durable Semantic Gate decision",
        description=(
            "Authenticate the configured provider and persist one exact "
            "monotonic Semantic Gate attempt."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_decide(
        request: DurableDecideRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.decide(
                contexts.service,
                contexts.provider,
                request,
            )
        except Exception as error:
            _raise_tool_error(error, "decide")

    @server.tool(
        name="tbm_durable_finalize",
        title="Finalize durable trace-backed memory",
        description=(
            "Persist the final usage decision and bounded injection "
            "artifact for an exact session version."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_finalize(
        request: DurableFinalizeRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.finalize(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "finalize")

    @server.tool(
        name="tbm_durable_start",
        title="Start durable execution",
        description=(
            "Transition a finalized session to execution with exact "
            "injection replay."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_start(
        request: DurableStartRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.start(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "start")

    @server.tool(
        name="tbm_durable_resume",
        title="Resume durable execution",
        description=(
            "Resume or renew an execution lease from persisted session state."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_resume(
        request: DurableResumeRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.resume(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "resume")

    @server.tool(
        name="tbm_durable_abandon",
        title="Abandon durable execution",
        description=(
            "Forward-only abandon an execution lease with a bounded reason."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_abandon(
        request: DurableAbandonRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.abandon(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "abandon")

    @server.tool(
        name="tbm_durable_complete",
        title="Complete a durable run",
        description=(
            "Authenticate the configured evaluator and atomically persist "
            "the outcome and completion outbox event."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_complete(
        request: DurableCompleteRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.complete(
                contexts.service,
                contexts.evaluator,
                request,
            )
        except Exception as error:
            _raise_tool_error(error, "complete")

    @server.tool(
        name="tbm_durable_cancel",
        title="Cancel a durable session",
        description=(
            "Forward-only cancel a non-completed durable session with an "
            "exact expected version."
        ),
        annotations=_IDEMPOTENT_WRITE,
        structured_output=True,
    )
    def tbm_durable_cancel(
        request: DurableCancelRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.cancel(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "cancel")

    @server.tool(
        name="tbm_durable_get_session",
        title="Read a durable session",
        description=(
            "Read the authenticated current GateSession state after a "
            "process restart."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def tbm_durable_get_session(
        request: DurableGetSessionRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.get_session(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "get_session")

    @server.tool(
        name="tbm_durable_export_replay",
        title="Export a durable replay bundle",
        description=(
            "Export the authenticated replay bundle when explicit content "
            "exposure is enabled."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def tbm_durable_export_replay(
        request: DurableReplayRequest,
    ) -> dict[str, object]:
        try:
            return dispatcher.export_replay(contexts.service, request)
        except Exception as error:
            _raise_tool_error(error, "export_replay")

    return server


def _raise_tool_error(
    error: Exception,
    operation: DurableAgentWireOperation,
) -> NoReturn:
    public_error = public_durable_agent_wire_error(error, operation)
    raise ToolError(
        json.dumps(
            public_error.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    ) from None


__all__ = [
    "DURABLE_MCP_SERVER_INSTRUCTIONS",
    "DurableMCPTrustedContexts",
    "create_durable_mcp_server",
]
