from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent import (
    AGENT_PROTOCOL_VERSION,
    AgentMemoryError,
    LocalAgentMemory,
    agent_capabilities,
    capture_local_trace,
)
from .authenticated_agent_v3 import (
    AuthenticatedAgentPrepareContext,
    AuthenticatedLocalAgentMemory,
)
from .models import MemoryContext, MemoryRunMeasurement
from .policy import (
    LLM_GATE_MAX_CANDIDATES,
    LLM_GATE_PROMPT_MAX_CHARS,
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)
from .service_v3 import AuthenticatedServiceV3Error
from .store import RETRIEVAL_QUERY_MAX_CHARS


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
AgentWireOperation = Literal[
    "open",
    "prepare",
    "finalize",
    "complete",
    "cancel",
    "health",
]


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
class AgentProtocolConfiguration:
    repo_path: Path
    tenant: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repo_path, Path) or not self.repo_path.is_absolute():
            raise ValueError("repo_path must be an absolute Path")
        if self.tenant is not None and (
            type(self.tenant) is not str
            or not self.tenant.strip()
            or self.tenant.strip() != self.tenant
            or len(self.tenant) > METADATA_VALUE_MAX_CHARS
        ):
            raise ValueError("tenant must be a nonblank bounded string")


class AgentProtocolDispatcher:
    """Shared strict agent.v1 lifecycle boundary for local transports."""

    def __init__(
        self,
        configuration: AgentProtocolConfiguration,
        runtime: LocalAgentMemory,
        *,
        authenticated_runtime: AuthenticatedLocalAgentMemory | None = None,
    ) -> None:
        if type(configuration) is not AgentProtocolConfiguration:
            raise TypeError(
                "configuration must be exactly AgentProtocolConfiguration"
            )
        if type(runtime) is not LocalAgentMemory:
            raise TypeError("runtime must be exactly LocalAgentMemory")
        if authenticated_runtime is not None and (
            type(authenticated_runtime) is not AuthenticatedLocalAgentMemory
        ):
            raise TypeError(
                "authenticated_runtime must be exactly "
                "AuthenticatedLocalAgentMemory or None"
            )
        if authenticated_runtime is not None and configuration.tenant is not None:
            raise ValueError(
                "declared tenant cannot be combined with authenticated runtime"
            )
        self._configuration = configuration
        self._runtime = runtime
        self._authenticated_runtime = authenticated_runtime

    def capabilities(self) -> dict[str, object]:
        return agent_capabilities().to_dict()

    def health(self) -> dict[str, object]:
        try:
            return self._runtime.health()
        except Exception as error:
            _raise_public_error(error, "health")

    def prepare(self, request: PrepareMemoryRequest) -> dict[str, object]:
        if type(request) is not PrepareMemoryRequest:
            _raise_public_error(
                TypeError("request must be exactly PrepareMemoryRequest"),
                "prepare",
            )
        try:
            trace = capture_local_trace(
                self._configuration.repo_path,
                run_id=request.run_id,
                trace_id=request.trace_id,
                tenant=(
                    None
                    if self._authenticated_runtime is not None
                    else self._configuration.tenant
                ),
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
            authenticated_runtime = self._authenticated_runtime
            if authenticated_runtime is None:
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
                    tenant=self._configuration.tenant,
                    input_hash=request.input_hash,
                )
                prepared = self._runtime.prepare_with_git_ancestry(
                    trace,
                    context,
                    repo_path=self._configuration.repo_path,
                    task=request.task,
                    query=request.query,
                    semantic_scores=request.semantic_scores,
                    max_candidates=request.max_candidates,
                    minimum_score=request.minimum_score,
                    context_summary=request.context_summary,
                )
            else:
                prepared = authenticated_runtime.prepare_with_git_ancestry(
                    trace,
                    AuthenticatedAgentPrepareContext(
                        mode=request.mode,
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
                        input_hash=request.input_hash,
                    ),
                    repo_path=self._configuration.repo_path,
                    task=request.task,
                    query=request.query,
                    semantic_scores=request.semantic_scores,
                    max_candidates=request.max_candidates,
                    minimum_score=request.minimum_score,
                    context_summary=request.context_summary,
                ).value
            return prepared.to_dict()
        except Exception as error:
            _raise_public_error(error, "prepare")

    def finalize(self, request: FinalizeMemoryRequest) -> dict[str, object]:
        if type(request) is not FinalizeMemoryRequest:
            _raise_public_error(
                TypeError("request must be exactly FinalizeMemoryRequest"),
                "finalize",
            )
        try:
            finalized = self._lifecycle_runtime().finalize(
                request.request_id,
                {
                    "use_memory": request.use_memory,
                    "allowed_memory_ids": request.allowed_memory_ids,
                    "blocked_memory_ids": request.blocked_memory_ids,
                    "reason": request.reason,
                    "risk": request.risk,
                    "recommended_injection": request.recommended_injection,
                },
            )
            return finalized.to_dict()
        except Exception as error:
            _raise_public_error(error, "finalize")

    def complete(self, request: CompleteRunRequest) -> dict[str, object]:
        if type(request) is not CompleteRunRequest:
            _raise_public_error(
                TypeError("request must be exactly CompleteRunRequest"),
                "complete",
            )
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
            return self._lifecycle_runtime().complete(
                request.decision_id,
                measurement,
            ).to_dict()
        except Exception as error:
            _raise_public_error(error, "complete")

    def cancel(self, request: CancelRunRequest) -> dict[str, object]:
        if type(request) is not CancelRunRequest:
            _raise_public_error(
                TypeError("request must be exactly CancelRunRequest"),
                "cancel",
            )
        try:
            self._lifecycle_runtime().cancel(request.request_id)
            return {
                "protocol_version": AGENT_PROTOCOL_VERSION,
                "request_id": request.request_id,
                "canceled": True,
            }
        except Exception as error:
            _raise_public_error(error, "cancel")

    def _lifecycle_runtime(
        self,
    ) -> LocalAgentMemory | AuthenticatedLocalAgentMemory:
        return self._authenticated_runtime or self._runtime


def public_agent_error(
    error: Exception,
    operation: AgentWireOperation,
    *,
    internal_code: str = "TBM_AGENT_INTERNAL_ERROR",
    internal_message: str = "agent runtime operation failed",
) -> AgentMemoryError:
    if isinstance(error, AgentMemoryError):
        return error
    if isinstance(error, AuthenticatedServiceV3Error):
        return AgentMemoryError(
            error.code,
            "state",
            operation,
            str(error),
        )
    if isinstance(error, (TypeError, ValueError, OverflowError)):
        return AgentMemoryError(
            "TBM_AGENT_INVALID_INPUT",
            "input",
            operation,
            str(error),
        )
    return AgentMemoryError(
        internal_code,
        "internal",
        operation,
        internal_message,
        retryable=True,
    )


def _raise_public_error(
    error: Exception,
    operation: AgentWireOperation,
) -> NoReturn:
    raise public_agent_error(error, operation) from None


__all__ = [
    "AgentProtocolConfiguration",
    "AgentProtocolDispatcher",
    "AgentWireOperation",
    "CancelRunRequest",
    "CompleteRunRequest",
    "FinalizeMemoryRequest",
    "InjectionMode",
    "MeasuredResult",
    "Mode",
    "PrepareMemoryRequest",
    "Risk",
    "public_agent_error",
]
