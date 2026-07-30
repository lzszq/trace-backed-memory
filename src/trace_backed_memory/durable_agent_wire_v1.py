from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from functools import wraps
import hmac
import math
import re
from threading import RLock
from typing import (
    Callable,
    Concatenate,
    Literal,
    NoReturn,
    ParamSpec,
    Protocol,
    TypeAlias,
    TypeVar,
    cast,
)

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .durable_agent_v3 import (
    DURABLE_AGENT_CONTRACT_VERSION,
    AuthenticatedDurableAgentMemory,
    DurableAgentCancelRequest,
    DurableReplayExportRequest,
)
from .durable_execution_v3 import (
    AuthenticatedOutcomeEvaluatorContext,
    DurableExecutionAbandonRequest,
    DurableExecutionResumeRequest,
    DurableExecutionStartResult,
    DurableExecutionStartRequest,
    TrustedOutcomeEvaluator,
)
from .durable_finalization_v3 import DurableFinalizationRequest
from .durable_retrieval_preparation_v3 import (
    DurableRetrievalPreparationRequest,
)
from .durable_semantic_gate_v3 import DurableSemanticGateRequest
from .gate_completion_v3 import GateCompletionRequest
from .gate_session_v3 import (
    GATE_SESSION_MAX_LEASE_SECONDS,
    GATE_SESSION_MAX_TTL_SECONDS,
)
from .replay_export_v3 import REPLAY_EXPORT_MAX_CONTENT_BYTES
from .outcome_v3 import OUTCOME_MAX_ARTIFACTS, OUTCOME_MAX_LATENCY_MS
from .retrieval_policy_v3 import TaskMode
from .retrieval_preparation_v3 import (
    RETRIEVAL_PREPARATION_MAX_ATTRIBUTES,
    RETRIEVAL_PREPARATION_MAX_QUERY_BYTES,
    RETRIEVAL_PREPARATION_MAX_SEMANTIC_DIMENSIONS,
    RetrievalPreparationContext,
    SemanticQueryVector,
)
from .retrieval_v3 import RetrievalMode
from .semantic_gate_artifact_v3 import (
    SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES,
    SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES,
)
from .semantic_gate_service_v3 import (
    AuthenticatedSemanticProviderContext,
    SemanticProviderCall,
    SemanticProviderResult,
)
from .service_v3 import (
    AuthenticatedServiceContext,
)


DURABLE_AGENT_WIRE_PROTOCOL_VERSION = "tbm.durable-agent-wire.v1"
DURABLE_AGENT_WIRE_ERROR_MESSAGE_MAX_CHARS = 2_000

_IDENTIFIER_MAX_CHARS = 128
_METADATA_MAX_CHARS = 512
_REASON_MAX_CHARS = 2_000
_MAX_DECISIONS = 1_000
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_MEMORY_REVISION_ID_PATTERN = r"^memory_revision_sha256_[0-9a-f]{64}$"
_PUBLIC_ERROR_CODE_PATTERN = r"^TBM_[A-Z0-9_]{1,120}$"
_DIGEST_RE = re.compile(_DIGEST_PATTERN)
_MEMORY_REVISION_ID_RE = re.compile(_MEMORY_REVISION_ID_PATTERN)
_PUBLIC_ERROR_CODE_RE = re.compile(_PUBLIC_ERROR_CODE_PATTERN)
_GATE_SESSION_STATUSES = (
    "created",
    "prepared",
    "awaiting_decision",
    "decided",
    "finalized",
    "executing",
    "completed",
    "canceled",
    "expired",
    "abandoned",
)
_SUPPORTED_ATTRIBUTES = frozenset(
    {
        "branch",
        "prompt_version",
        "prompt_family",
        "tool",
        "tool_schema_version",
        "model",
        "model_family",
        "eval_suite",
        "task_type",
        "failure_type",
    }
)

DurableAgentWireOperation = Literal[
    "prepare",
    "decide",
    "finalize",
    "start",
    "resume",
    "abandon",
    "complete",
    "cancel",
    "get_session",
    "export_replay",
]
DurableAgentWireErrorCategory = Literal[
    "input",
    "authentication",
    "authorization",
    "state",
    "not_found",
    "persistence",
    "provider",
    "evaluator",
    "recovery",
    "internal",
]
DurableTaskMode = Literal[
    "planning",
    "repair",
    "debug",
    "eval",
    "production",
]
DurableRetrievalMode = Literal[
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "hybrid",
]
SemanticRisk = Literal["low", "medium", "high", "unknown"]
RecommendedInjection = Literal["none", "summary", "full"]
RunResult = Literal["pass", "fail", "error"]
DataClassification = Literal[
    "public",
    "internal",
    "confidential",
    "restricted",
]
DurableStorageMode = Literal["sqlite", "postgres"]

RepositoryIdResolver: TypeAlias = Callable[[AuthenticatedServiceContext], str]
OutcomeEvaluatorResolver: TypeAlias = Callable[
    [AuthenticatedOutcomeEvaluatorContext],
    TrustedOutcomeEvaluator,
]
_P = ParamSpec("_P")
_R = TypeVar("_R")


class _OperationLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, *args: object) -> object: ...


def _base64_max_chars(maximum: int) -> int:
    return ((maximum + 2) // 3) * 4


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DurableSemanticQueryInput(_StrictRequest):
    provider_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    provider_version: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    vector: list[int | float] = Field(
        min_length=1,
        max_length=RETRIEVAL_PREPARATION_MAX_SEMANTIC_DIMENSIONS,
    )

    @model_validator(mode="after")
    def _finite_nonzero_vector(self) -> DurableSemanticQueryInput:
        if not any(float(value) != 0.0 for value in self.vector) or any(
            not math.isfinite(float(value)) for value in self.vector
        ):
            raise ValueError("semantic vector must be finite and non-zero")
        return self


class DurablePrepareRequest(_StrictRequest):
    request_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    trace_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    run_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    task_mode: DurableTaskMode
    commit_sha: str = Field(min_length=1, max_length=_METADATA_MAX_CHARS)
    attributes: dict[str, str] = Field(
        default_factory=dict,
        max_length=RETRIEVAL_PREPARATION_MAX_ATTRIBUTES,
    )
    evaluation_suite: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    evaluation_case_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    retrieval_mode: DurableRetrievalMode
    retriever_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    retriever_version: str = Field(
        min_length=1,
        max_length=_IDENTIFIER_MAX_CHARS,
    )
    top_k: int = Field(ge=1, le=100)
    idempotency_key: str = Field(
        min_length=1,
        max_length=_IDENTIFIER_MAX_CHARS,
    )
    expires_in_seconds: int = Field(ge=1, le=GATE_SESSION_MAX_TTL_SECONDS)
    lease_seconds: int = Field(ge=1, le=GATE_SESSION_MAX_LEASE_SECONDS)
    query_base64: str | None = Field(
        default=None,
        max_length=_base64_max_chars(RETRIEVAL_PREPARATION_MAX_QUERY_BYTES),
    )
    semantic_query: DurableSemanticQueryInput | None = None

    @model_validator(mode="after")
    def _paired_evaluation_and_attributes(self) -> DurablePrepareRequest:
        if (self.evaluation_suite is None) != (
            self.evaluation_case_id is None
        ):
            raise ValueError(
                "evaluation_suite and evaluation_case_id must be paired"
            )
        if any(
            key not in _SUPPORTED_ATTRIBUTES
            or not value
            or value.strip() != value
            or len(value) > _METADATA_MAX_CHARS
            for key, value in self.attributes.items()
        ):
            raise ValueError("attributes contain an unsupported or invalid item")
        if self.query_base64 is not None:
            _validate_canonical_base64(
                self.query_base64,
                RETRIEVAL_PREPARATION_MAX_QUERY_BYTES,
            )
        return self


class DurableDecideRequest(_StrictRequest):
    session_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    expected_session_version: int = Field(ge=1)
    prompt_base64: str = Field(
        min_length=1,
        max_length=_base64_max_chars(
            SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES
        ),
    )
    expected_previous_attempt_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=_IDENTIFIER_MAX_CHARS,
    )
    lease_seconds: int = Field(
        default=1_800,
        ge=1,
        le=GATE_SESSION_MAX_LEASE_SECONDS,
    )
    response_base64: str = Field(
        min_length=1,
        max_length=_base64_max_chars(
            SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES
        ),
    )
    provider_request_id: str = Field(
        min_length=1,
        max_length=_IDENTIFIER_MAX_CHARS,
    )
    decision_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    final_allowed_revision_ids: list[str] = Field(
        default_factory=list,
        max_length=_MAX_DECISIONS,
    )
    final_blocked_revision_ids: list[str] = Field(
        default_factory=list,
        max_length=_MAX_DECISIONS,
    )
    reason: str = Field(min_length=1, max_length=_REASON_MAX_CHARS)
    risk: SemanticRisk
    recommended_injection: RecommendedInjection
    input_tokens: int | None = Field(default=None, ge=0, le=2_147_483_647)
    output_tokens: int | None = Field(default=None, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def _canonical_content_and_decisions(self) -> DurableDecideRequest:
        _validate_canonical_base64(
            self.prompt_base64,
            SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES,
        )
        _validate_canonical_base64(
            self.response_base64,
            SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES,
        )
        allowed = self.final_allowed_revision_ids
        blocked = self.final_blocked_revision_ids
        if (
            allowed != sorted(allowed)
            or blocked != sorted(blocked)
            or len(set(allowed)) != len(allowed)
            or len(set(blocked)) != len(blocked)
            or set(allowed).intersection(blocked)
            or any(
                _MEMORY_REVISION_ID_RE.fullmatch(value) is None
                for value in (*allowed, *blocked)
            )
        ):
            raise ValueError(
                "final allowed and blocked revision IDs must be canonical"
            )
        return self


class DurableSessionRevisionRequest(_StrictRequest):
    session_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    expected_session_version: int = Field(ge=1)


class DurableFinalizeRequest(DurableSessionRevisionRequest):
    lease_seconds: int = Field(
        default=1_800,
        ge=1,
        le=GATE_SESSION_MAX_LEASE_SECONDS,
    )


class DurableStartRequest(DurableSessionRevisionRequest):
    pass


class DurableResumeRequest(DurableSessionRevisionRequest):
    lease_seconds: int = Field(
        default=1_800,
        ge=1,
        le=GATE_SESSION_MAX_LEASE_SECONDS,
    )


class DurableAbandonRequest(DurableSessionRevisionRequest):
    reason: str = Field(min_length=1, max_length=512)


class DurableCompleteRequest(_StrictRequest):
    session_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)
    expected_session_version: int = Field(ge=1)
    result: RunResult
    evidence_artifact_sha256s: list[str] = Field(
        min_length=1,
        max_length=OUTCOME_MAX_ARTIFACTS,
    )
    output_sha256: str | None = Field(
        default=None,
        pattern=_DIGEST_PATTERN,
    )
    tool_outputs_sha256: str | None = Field(
        default=None,
        pattern=_DIGEST_PATTERN,
    )
    latency_ms: int | None = Field(
        default=None,
        ge=0,
        le=OUTCOME_MAX_LATENCY_MS,
    )
    cost_usd: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=_IDENTIFIER_MAX_CHARS,
    )

    @model_validator(mode="after")
    def _measurement_shape(self) -> DurableCompleteRequest:
        artifacts = self.evidence_artifact_sha256s
        if (
            self.output_sha256 is None
            and self.tool_outputs_sha256 is None
        ):
            raise ValueError(
                "output_sha256 or tool_outputs_sha256 is required"
            )
        if (
            artifacts != sorted(artifacts)
            or len(set(artifacts)) != len(artifacts)
            or any(_DIGEST_RE.fullmatch(value) is None for value in artifacts)
        ):
            raise ValueError(
                "evidence artifact digests must be canonical, sorted, and unique"
            )
        if (self.result == "error") != (self.error_code is not None):
            raise ValueError(
                "error_code is required only when result is error"
            )
        return self


class DurableCancelRequest(DurableSessionRevisionRequest):
    reason: str = Field(min_length=1, max_length=512)


class DurableGetSessionRequest(_StrictRequest):
    session_id: str = Field(min_length=1, max_length=_IDENTIFIER_MAX_CHARS)


class DurableReplayRequest(DurableSessionRevisionRequest):
    allowed_classifications: list[DataClassification] = Field(
        min_length=1,
        max_length=4,
    )
    max_content_bytes: int = Field(
        default=REPLAY_EXPORT_MAX_CONTENT_BYTES,
        ge=1,
        le=REPLAY_EXPORT_MAX_CONTENT_BYTES,
    )

    @model_validator(mode="after")
    def _unique_classifications(self) -> DurableReplayRequest:
        if len(set(self.allowed_classifications)) != len(
            self.allowed_classifications
        ):
            raise ValueError("allowed_classifications must be unique")
        return self


@dataclass(frozen=True)
class DurableAgentWireConfiguration:
    storage_mode: DurableStorageMode
    expose_injection_content: bool = False
    expose_replay_content: bool = False

    def __post_init__(self) -> None:
        if self.storage_mode not in {"sqlite", "postgres"}:
            raise ValueError("storage_mode must be sqlite or postgres")
        if (
            type(self.expose_injection_content) is not bool
            or type(self.expose_replay_content) is not bool
        ):
            raise ValueError("content exposure settings must be booleans")
        if self.expose_replay_content and not self.expose_injection_content:
            raise ValueError(
                "replay content exposure requires injection content exposure"
            )


class DurableAgentWireError(RuntimeError):
    """Stable sanitized failure at the durable Agent adapter boundary."""

    def __init__(
        self,
        code: str,
        category: DurableAgentWireErrorCategory,
        operation: DurableAgentWireOperation,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        bounded = str(message)
        if not bounded.strip():
            bounded = "durable Agent operation failed"
        self.code = code
        self.category = category
        self.operation = operation
        self.retryable = retryable
        super().__init__(bounded[:DURABLE_AGENT_WIRE_ERROR_MESSAGE_MAX_CHARS])

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            "error": {
                "code": self.code,
                "category": self.category,
                "message": str(self),
                "operation": self.operation,
                "retryable": self.retryable,
            },
        }


def _serialized(
    method: Callable[
        Concatenate[DurableAgentProtocolDispatcher, _P],
        _R,
    ],
) -> Callable[
    Concatenate[DurableAgentProtocolDispatcher, _P],
    _R,
]:
    @wraps(method)
    def wrapped(
        self: DurableAgentProtocolDispatcher,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        with self._operation_lock:
            return method(self, *args, **kwargs)

    return wrapped


class DurableAgentProtocolDispatcher:
    """Strict adapter-neutral boundary over AuthenticatedDurableAgentMemory.

    A transport must authenticate the caller and construct all three trusted
    contexts outside request JSON. This class does not authenticate a peer.
    """

    def __init__(
        self,
        configuration: DurableAgentWireConfiguration,
        runtime: AuthenticatedDurableAgentMemory,
        *,
        repository_id_resolver: RepositoryIdResolver,
        evaluator_resolver: OutcomeEvaluatorResolver,
        operation_lock: _OperationLock | None = None,
    ) -> None:
        if type(configuration) is not DurableAgentWireConfiguration:
            raise TypeError(
                "configuration must be DurableAgentWireConfiguration"
            )
        if type(runtime) is not AuthenticatedDurableAgentMemory:
            raise TypeError(
                "runtime must be AuthenticatedDurableAgentMemory"
            )
        if not callable(repository_id_resolver):
            raise TypeError("repository_id_resolver must be callable")
        if not callable(evaluator_resolver):
            raise TypeError("evaluator_resolver must be callable")
        if operation_lock is not None and not (
            callable(getattr(operation_lock, "__enter__", None))
            and callable(getattr(operation_lock, "__exit__", None))
        ):
            raise TypeError("operation_lock must be a context manager")
        self._configuration = configuration
        self._runtime = runtime
        self._repository_id_resolver = repository_id_resolver
        self._evaluator_resolver = evaluator_resolver
        self._operation_lock: _OperationLock = operation_lock or RLock()

    @_serialized
    def capabilities(self) -> dict[str, object]:
        operations = [
            "prepare",
            "decide",
            "finalize",
            "start",
            "resume",
            "abandon",
            "complete",
            "cancel",
            "get_session",
        ]
        if self._configuration.expose_replay_content:
            operations.append("export_replay")
        return {
            "protocol_version": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            "durable_agent_contract_version": DURABLE_AGENT_CONTRACT_VERSION,
            "storage_mode": self._configuration.storage_mode,
            "operations": operations,
            "gate_session_statuses": list(_GATE_SESSION_STATUSES),
            "identity_source": "trusted_adapter",
            "transport_authentication": "required",
            "caller_identity_fields": False,
            "durable_sessions": True,
            "process_local_records": [],
            "injection_content_exposed": (
                self._configuration.expose_injection_content
            ),
            "replay_content_exposed": (
                self._configuration.expose_replay_content
            ),
            "limits": {
                "query_bytes": RETRIEVAL_PREPARATION_MAX_QUERY_BYTES,
                "semantic_dimensions": (
                    RETRIEVAL_PREPARATION_MAX_SEMANTIC_DIMENSIONS
                ),
                "session_ttl_seconds": GATE_SESSION_MAX_TTL_SECONDS,
                "session_lease_seconds": GATE_SESSION_MAX_LEASE_SECONDS,
                "replay_content_bytes": REPLAY_EXPORT_MAX_CONTENT_BYTES,
            },
        }

    @_serialized
    def prepare(
        self,
        context: AuthenticatedServiceContext,
        request: DurablePrepareRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "prepare"
        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurablePrepareRequest
        ):
            _raise_public(
                TypeError("prepare input types are invalid"),
                operation,
            )
        try:
            try:
                repository_id = self._repository_id_resolver(context)
            except Exception:
                raise DurableAgentWireError(
                    "TBM_DURABLE_WIRE_REPOSITORY_AUTHENTICATION_FAILED",
                    "authentication",
                    operation,
                    "trusted repository identity could not be resolved",
                ) from None
            if not _is_identifier(repository_id):
                raise DurableAgentWireError(
                    "TBM_DURABLE_WIRE_REPOSITORY_AUTHENTICATION_FAILED",
                    "authentication",
                    operation,
                    "trusted repository identity could not be resolved",
                )
            preparation_context = RetrievalPreparationContext(
                tenant_id=context.tenant_id,
                repository_id=repository_id,
                environment_id=context.environment_id,
                task_mode=cast(TaskMode, request.task_mode),
                commit_sha=request.commit_sha,
                attributes=tuple(sorted(request.attributes.items())),
                evaluation_suite=request.evaluation_suite,
                evaluation_case_id=request.evaluation_case_id,
            )
            semantic_query = (
                None
                if request.semantic_query is None
                else SemanticQueryVector(
                    provider_id=request.semantic_query.provider_id,
                    provider_version=request.semantic_query.provider_version,
                    vector=tuple(request.semantic_query.vector),
                )
            )
            result = self._runtime.prepare(
                context,
                DurableRetrievalPreparationRequest(
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                    context=preparation_context,
                    retrieval_mode=cast(
                        RetrievalMode,
                        request.retrieval_mode,
                    ),
                    retriever_id=request.retriever_id,
                    retriever_version=request.retriever_version,
                    top_k=request.top_k,
                    idempotency_key=request.idempotency_key,
                    expires_in_seconds=request.expires_in_seconds,
                    lease_seconds=request.lease_seconds,
                    query=_decode_optional_base64(
                        request.query_base64,
                        RETRIEVAL_PREPARATION_MAX_QUERY_BYTES,
                    ),
                    semantic_query=semantic_query,
                ),
            )
            return _success(
                operation,
                {
                    "authorization_event_id": (
                        result.authorization.authorization_event_id
                    ),
                    "session": result.session.to_dict(),
                    "retrieval_snapshot": result.value.snapshot.to_dict(),
                    "system_gate_evaluation": (
                        result.value.system_gate_evaluation.to_dict()
                    ),
                    "retrieval_policy": result.value.policy.to_dict(),
                },
            )
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def decide(
        self,
        context: AuthenticatedServiceContext,
        provider_context: AuthenticatedSemanticProviderContext,
        request: DurableDecideRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "decide"
        if (
            type(context) is not AuthenticatedServiceContext
            or type(provider_context)
            is not AuthenticatedSemanticProviderContext
            or type(request) is not DurableDecideRequest
        ):
            _raise_public(
                TypeError("decision input types are invalid"),
                operation,
            )
        try:
            prompt = _decode_base64(
                request.prompt_base64,
                SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES,
            )
            response = _decode_base64(
                request.response_base64,
                SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES,
            )
            provider_result = SemanticProviderResult(
                response=response,
                provider_request_id=request.provider_request_id,
                decision_id=request.decision_id,
                final_allowed_revision_ids=tuple(
                    request.final_allowed_revision_ids
                ),
                final_blocked_revision_ids=tuple(
                    request.final_blocked_revision_ids
                ),
                reason=request.reason,
                risk=request.risk,
                recommended_injection=request.recommended_injection,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
            )

            def call_provider(call: SemanticProviderCall) -> SemanticProviderResult:
                if (
                    type(call) is not SemanticProviderCall
                    or not hmac.compare_digest(call.prompt, prompt)
                ):
                    raise DurableAgentWireError(
                        "TBM_DURABLE_WIRE_PROVIDER_CALL_INVALID",
                        "provider",
                        operation,
                        "trusted provider call differs from the submitted prompt",
                    )
                return provider_result

            result = self._runtime.decide(
                context,
                provider_context,
                DurableSemanticGateRequest(
                    session_id=request.session_id,
                    expected_session_version=(
                        request.expected_session_version
                    ),
                    prompt=prompt,
                    expected_previous_attempt_id=(
                        request.expected_previous_attempt_id
                    ),
                    lease_seconds=request.lease_seconds,
                ),
                call_provider,
            )
            artifacts = result.semantic_gate.artifacts
            retained_response = artifacts.response
            if (
                not hmac.compare_digest(artifacts.prompt.content, prompt)
                or retained_response is None
                or not hmac.compare_digest(
                    retained_response.content,
                    response,
                )
            ):
                raise DurableAgentWireError(
                    "TBM_DURABLE_WIRE_DECISION_REPLAY_MISMATCH",
                    "state",
                    operation,
                    "durable decision replay differs from submitted bytes",
                )
            return _success(
                operation,
                {
                    "session": result.session.to_dict(),
                    "attempt": result.semantic_gate.attempt.to_dict(),
                    "prompt_artifact": (
                        artifacts.prompt.binding.to_dict()
                    ),
                    "response_artifact": (
                        retained_response.binding.to_dict()
                    ),
                    "replayed": result.replayed,
                },
            )
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def finalize(
        self,
        context: AuthenticatedServiceContext,
        request: DurableFinalizeRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "finalize"
        _require_context_request(context, request, DurableFinalizeRequest, operation)
        try:
            result = self._runtime.finalize(
                context,
                DurableFinalizationRequest(
                    request.session_id,
                    request.expected_session_version,
                    request.lease_seconds,
                ),
            )
            return _success(
                operation,
                {
                    "session": result.session.to_dict(),
                    "usage_decision": result.usage_decision.to_dict(),
                    "injection": result.injection.to_dict(),
                    "manifest": result.manifest.to_dict(),
                    "snippet": (
                        result.snippet
                        if self._configuration.expose_injection_content
                        else None
                    ),
                    "content_exposed": (
                        self._configuration.expose_injection_content
                    ),
                    "replayed": result.replayed,
                },
            )
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def start(
        self,
        context: AuthenticatedServiceContext,
        request: DurableStartRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "start"
        _require_context_request(context, request, DurableStartRequest, operation)
        try:
            result = self._runtime.start(
                context,
                DurableExecutionStartRequest(
                    request.session_id,
                    request.expected_session_version,
                ),
            )
            return _success(operation, self._execution_result(result))
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def resume(
        self,
        context: AuthenticatedServiceContext,
        request: DurableResumeRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "resume"
        _require_context_request(context, request, DurableResumeRequest, operation)
        try:
            result = self._runtime.resume(
                context,
                DurableExecutionResumeRequest(
                    request.session_id,
                    request.expected_session_version,
                    request.lease_seconds,
                ),
            )
            return _success(operation, self._execution_result(result))
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def abandon(
        self,
        context: AuthenticatedServiceContext,
        request: DurableAbandonRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "abandon"
        _require_context_request(context, request, DurableAbandonRequest, operation)
        try:
            result = self._runtime.abandon(
                context,
                DurableExecutionAbandonRequest(
                    request.session_id,
                    request.expected_session_version,
                    request.reason,
                ),
            )
            return _success(
                operation,
                {
                    "session": result.session.to_dict(),
                    "transition_authorization_event_id": (
                        result.transition_authorization_event_id
                    ),
                    "replayed": result.replayed,
                },
            )
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def complete(
        self,
        context: AuthenticatedServiceContext,
        evaluator_context: AuthenticatedOutcomeEvaluatorContext,
        request: DurableCompleteRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "complete"
        if (
            type(context) is not AuthenticatedServiceContext
            or type(evaluator_context)
            is not AuthenticatedOutcomeEvaluatorContext
            or type(request) is not DurableCompleteRequest
        ):
            _raise_public(
                TypeError("completion input types are invalid"),
                operation,
            )
        try:
            try:
                evaluator = self._evaluator_resolver(evaluator_context)
            except Exception:
                raise DurableAgentWireError(
                    "TBM_DURABLE_WIRE_EVALUATOR_AUTHENTICATION_FAILED",
                    "evaluator",
                    operation,
                    "trusted evaluator context could not be resolved",
                ) from None
            if (
                type(evaluator) is not TrustedOutcomeEvaluator
                or evaluator.status != "active"
                or evaluator.evaluator_id != evaluator_context.evaluator_id
                or evaluator.authenticator_id
                != evaluator_context.authenticator_id
                or evaluator.credential_id
                != evaluator_context.credential_id
            ):
                raise DurableAgentWireError(
                    "TBM_DURABLE_WIRE_EVALUATOR_AUTHENTICATION_FAILED",
                    "evaluator",
                    operation,
                    "trusted evaluator context could not be resolved",
                )
            result = self._runtime.complete(
                context,
                evaluator_context,
                GateCompletionRequest(
                    session_id=request.session_id,
                    expected_version=request.expected_session_version,
                    result=request.result,
                    evaluator_id=evaluator.evaluator_id,
                    evaluator_version=evaluator.evaluator_version,
                    evidence_artifact_sha256s=tuple(
                        request.evidence_artifact_sha256s
                    ),
                    output_sha256=request.output_sha256,
                    tool_outputs_sha256=request.tool_outputs_sha256,
                    latency_ms=request.latency_ms,
                    cost_usd=request.cost_usd,
                    error_code=request.error_code,
                ),
            )
            return _success(
                operation,
                {
                    "session": result.session.to_dict(),
                    "outcome": result.outcome.to_dict(),
                    "outbox_event": result.event.to_dict(),
                    "outbox_delivery": result.delivery.to_dict(),
                    "transition_authorization_event_id": (
                        result.transition_authorization_event_id
                    ),
                    "inserted": result.inserted,
                    "event_inserted": result.event_inserted,
                    "replayed": result.replayed,
                },
            )
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def cancel(
        self,
        context: AuthenticatedServiceContext,
        request: DurableCancelRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "cancel"
        _require_context_request(context, request, DurableCancelRequest, operation)
        try:
            result = self._runtime.cancel(
                context,
                DurableAgentCancelRequest(
                    request.session_id,
                    request.expected_session_version,
                    request.reason,
                ),
            )
            return _success(
                operation,
                {
                    "session": result.session.to_dict(),
                    "transition_authorization_event_id": (
                        result.transition_authorization_event_id
                    ),
                    "replayed": result.replayed,
                },
            )
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def get_session(
        self,
        context: AuthenticatedServiceContext,
        request: DurableGetSessionRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "get_session"
        _require_context_request(
            context,
            request,
            DurableGetSessionRequest,
            operation,
        )
        try:
            session = self._runtime.get_session(context, request.session_id)
            return _success(operation, {"session": session.to_dict()})
        except Exception as error:
            _raise_public(error, operation)

    @_serialized
    def export_replay(
        self,
        context: AuthenticatedServiceContext,
        request: DurableReplayRequest,
    ) -> dict[str, object]:
        operation: DurableAgentWireOperation = "export_replay"
        _require_context_request(context, request, DurableReplayRequest, operation)
        if not self._configuration.expose_replay_content:
            raise DurableAgentWireError(
                "TBM_DURABLE_WIRE_REPLAY_CONTENT_DISABLED",
                "authorization",
                operation,
                "replay content exposure is disabled for this adapter",
            )
        try:
            result = self._runtime.export_replay_bundle(
                context,
                DurableReplayExportRequest(
                    request.session_id,
                    request.expected_session_version,
                    tuple(request.allowed_classifications),
                    request.max_content_bytes,
                ),
            )
            return _success(
                operation,
                {
                    "session": result.session.to_dict(),
                    "bundle": result.bundle.to_dict(),
                    "read_authorization_event_id": (
                        result.read_authorization_event_id
                    ),
                    "retrieval_authorization_event_id": (
                        result.retrieval_authorization_event_id
                    ),
                    "content_exposed": True,
                },
            )
        except Exception as error:
            _raise_public(error, operation)

    def _execution_result(
        self,
        result: DurableExecutionStartResult,
    ) -> dict[str, object]:
        session = result.session
        snippet = result.snippet
        return {
            "session": session.to_dict(),
            "usage_decision": result.usage_decision.to_dict(),
            "injection": result.injection.to_dict(),
            "manifest": result.manifest.to_dict(),
            "transition_authorization_event_id": (
                result.transition_authorization_event_id
            ),
            "snippet": (
                snippet
                if self._configuration.expose_injection_content
                else None
            ),
            "content_exposed": (
                self._configuration.expose_injection_content
            ),
            "execution_required": result.execution_required,
            "replayed": result.replayed,
        }


def public_durable_agent_wire_error(
    error: Exception,
    operation: DurableAgentWireOperation,
) -> DurableAgentWireError:
    if isinstance(error, DurableAgentWireError):
        return error
    code = getattr(error, "code", None)
    if type(code) is str and _PUBLIC_ERROR_CODE_RE.fullmatch(code):
        category = _error_category(code)
        return DurableAgentWireError(
            code,
            category,
            operation,
            _public_error_message(category),
            retryable=_is_retryable(code, category),
        )
    if isinstance(error, (TypeError, ValueError, OverflowError)):
        return DurableAgentWireError(
            "TBM_DURABLE_WIRE_INVALID_INPUT",
            "input",
            operation,
            "durable Agent request failed validation",
        )
    return DurableAgentWireError(
        "TBM_DURABLE_WIRE_INTERNAL_ERROR",
        "internal",
        operation,
        "durable Agent operation failed",
        retryable=True,
    )


def _public_error_message(category: DurableAgentWireErrorCategory) -> str:
    return {
        "input": "durable Agent request failed validation",
        "authentication": "durable Agent authentication failed",
        "authorization": "durable Agent operation is not authorized",
        "state": "durable Agent state changed or is incompatible",
        "not_found": "durable Agent resource was not found",
        "persistence": "durable Agent persistence operation failed",
        "provider": "durable Agent provider operation failed",
        "evaluator": "durable Agent evaluator operation failed",
        "recovery": "durable Agent recovery is required",
        "internal": "durable Agent operation failed",
    }[category]


def _success(
    operation: DurableAgentWireOperation,
    result: dict[str, object],
) -> dict[str, object]:
    return {
        "protocol_version": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
        "operation": operation,
        "result": result,
    }


def _require_context_request(
    context: object,
    request: object,
    request_type: type[BaseModel],
    operation: DurableAgentWireOperation,
) -> None:
    if (
        type(context) is not AuthenticatedServiceContext
        or type(request) is not request_type
    ):
        _raise_public(
            TypeError("durable Agent input types are invalid"),
            operation,
        )


def _raise_public(
    error: Exception,
    operation: DurableAgentWireOperation,
) -> NoReturn:
    raise public_durable_agent_wire_error(error, operation) from None


def _error_category(code: str) -> DurableAgentWireErrorCategory:
    if "RECOVERY_REQUIRED" in code:
        return "recovery"
    if "AUTHENTICATION" in code:
        return "authentication"
    if (
        "AUTHORIZATION" in code
        or "FORBIDDEN" in code
        or "SCOPE_MISMATCH" in code
    ):
        return "authorization"
    if "EVALUATOR" in code:
        return "evaluator"
    if "PROVIDER" in code or "SEMANTIC_SERVICE" in code:
        return "provider"
    if "NOT_FOUND" in code:
        return "not_found"
    if (
        "INVALID" in code
        and not any(
            marker in code
            for marker in (
                "STATE",
                "STATUS",
                "LINKAGE",
                "RECEIPT",
            )
        )
    ):
        return "input"
    if any(
        marker in code
        for marker in (
            "CHANGED",
            "CONFLICT",
            "STATE",
            "STATUS",
            "TRANSITION",
            "LINKAGE",
            "MISMATCH",
            "SESSION_EXISTS",
        )
    ):
        return "state"
    if any(
        marker in code
        for marker in (
            "PERSISTENCE",
            "STORE",
            "READBACK",
            "UNAVAILABLE",
            "FAILED",
            "SCHEMA",
            "RECEIPT",
        )
    ):
        return "persistence"
    return "state"


def _is_retryable(
    code: str,
    category: DurableAgentWireErrorCategory,
) -> bool:
    return category in {"persistence", "provider", "recovery", "internal"} and (
        "INVALID" not in code
        and "CONFLICT" not in code
        and "MISMATCH" not in code
    )


def _decode_optional_base64(
    value: str | None,
    maximum: int,
) -> bytes | None:
    if value is None:
        return None
    return _decode_base64(value, maximum)


def _decode_base64(value: str, maximum: int) -> bytes:
    try:
        encoded = value.encode("ascii", errors="strict")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        raise ValueError("content must use canonical base64") from None
    if (
        len(decoded) > maximum
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise ValueError("content must use bounded canonical base64")
    return decoded


def _validate_canonical_base64(value: str, maximum: int) -> None:
    _decode_base64(value, maximum)


def _is_identifier(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.strip() == value
        and len(value) <= _IDENTIFIER_MAX_CHARS
    )


__all__ = [
    "DURABLE_AGENT_WIRE_ERROR_MESSAGE_MAX_CHARS",
    "DURABLE_AGENT_WIRE_PROTOCOL_VERSION",
    "DurableAbandonRequest",
    "DurableAgentProtocolDispatcher",
    "DurableAgentWireConfiguration",
    "DurableAgentWireError",
    "DurableAgentWireErrorCategory",
    "DurableAgentWireOperation",
    "DurableCancelRequest",
    "DurableCompleteRequest",
    "DurableDecideRequest",
    "DurableFinalizeRequest",
    "DurableGetSessionRequest",
    "DurablePrepareRequest",
    "DurableReplayRequest",
    "DurableResumeRequest",
    "DurableSemanticQueryInput",
    "DurableSessionRevisionRequest",
    "DurableStartRequest",
    "OutcomeEvaluatorResolver",
    "RepositoryIdResolver",
    "public_durable_agent_wire_error",
]
