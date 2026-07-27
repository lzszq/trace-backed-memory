from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol, TypeAlias
from uuid import uuid4

from .capture import (
    capture_commit_ancestry,
    capture_trace_metadata,
)
from .models import (
    CommitAncestryEvidence,
    GatedMemoryResult,
    MemoryContext,
    MemoryGateRequest,
    MemoryRunCompletion,
    MemoryRunMeasurement,
    Trace,
)
from .policy import (
    INJECTION_MAX_MEMORIES,
    INJECTION_SNIPPET_MAX_CHARS,
    LLM_GATE_MAX_CANDIDATES,
    LLM_GATE_PROMPT_MAX_CHARS,
    LLM_GATE_RESPONSE_MAX_BYTES,
    LLM_GATE_RESPONSE_MAX_DEPTH,
    LLM_GATE_RESPONSE_MAX_NODES,
    METADATA_VALUE_MAX_CHARS,
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    parse_memory_decision,
)
from .postgres import POSTGRES_SCHEMA_VERSION, PostgresMemoryRepository
from .sqlite import SQLITE_SCHEMA_VERSION, SQLiteMemoryRepository
from .store import (
    FINALIZED_GATE_REQUEST_MAX_ITEMS,
    GATE_REQUEST_MAX_CANDIDATES,
    PENDING_GATE_REQUEST_MAX_CANDIDATE_IDS,
    PENDING_GATE_REQUEST_MAX_ITEMS,
    SNAPSHOT_VERSION,
    TRACE_JSON_MAX_NODES,
    TRACE_JSON_MAX_TEXT_BYTES,
    TraceBackedMemoryStore,
)


AGENT_PROTOCOL_VERSION = "tbm.agent.v1"
AGENT_ERROR_MESSAGE_MAX_CHARS = 2_048

AgentErrorCategory = Literal[
    "input",
    "state",
    "persistence",
    "callback",
    "closed",
    "internal",
]
AgentOperation = Literal[
    "open",
    "capture",
    "prepare",
    "finalize",
    "complete",
    "cancel",
    "flush",
    "health",
    "run",
    "close",
]


def _protocol_identifier(value: object) -> str | None:
    if (
        type(value) is str
        and value.strip()
        and len(value) <= MEMORY_ID_MAX_CHARS
    ):
        return value
    return None


class MemoryRepository(Protocol):
    """Persistence seam shared by the SQLite and PostgreSQL adapters."""

    def load(self) -> TraceBackedMemoryStore: ...

    def sync(self, store: TraceBackedMemoryStore) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class AgentCapabilities:
    protocol_version: str
    snapshot_version: int
    sqlite_schema_version: int
    postgres_schema_version: int
    storage_modes: tuple[str, ...]
    operations: tuple[str, ...]
    modes: tuple[str, ...]
    limits: dict[str, int]
    durable_records: tuple[str, ...]
    process_local_records: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "snapshot_version": self.snapshot_version,
            "sqlite_schema_version": self.sqlite_schema_version,
            "postgres_schema_version": self.postgres_schema_version,
            "storage_modes": list(self.storage_modes),
            "operations": list(self.operations),
            "modes": list(self.modes),
            "limits": dict(self.limits),
            "durable_records": list(self.durable_records),
            "process_local_records": list(self.process_local_records),
        }


@dataclass(frozen=True)
class AgentPreparedMemory:
    protocol_version: str
    request_id: str
    trace_id: str
    run_id: str
    candidate_memory_ids: tuple[str, ...]
    system_allowed_memory_ids: tuple[str, ...]
    system_blocked: tuple[tuple[str, str], ...]
    prompt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "candidate_memory_ids": list(self.candidate_memory_ids),
            "system_allowed_memory_ids": list(
                self.system_allowed_memory_ids
            ),
            "system_blocked": dict(self.system_blocked),
            "prompt": self.prompt,
        }


@dataclass(frozen=True)
class AgentFinalizedMemory:
    protocol_version: str
    request_id: str
    trace_id: str
    decision_id: str
    use_memory: bool
    allowed_memory_ids: tuple[str, ...]
    blocked_memory_ids: tuple[str, ...]
    reason: str
    risk: Literal["none", "low", "medium", "high"]
    recommended_injection: Literal[
        "none",
        "short_summary",
        "full_case_summary",
        "pointer_only",
    ]
    snippet: str

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "decision_id": self.decision_id,
            "use_memory": self.use_memory,
            "allowed_memory_ids": list(self.allowed_memory_ids),
            "blocked_memory_ids": list(self.blocked_memory_ids),
            "reason": self.reason,
            "risk": self.risk,
            "recommended_injection": self.recommended_injection,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class AgentCompletedRun:
    protocol_version: str
    request_id: str | None
    trace_id: str
    run_id: str
    decision_id: str
    eval_result: Literal["pass", "fail", "error"]
    memory_caused_failure: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AgentRunResult:
    prepared: AgentPreparedMemory
    finalized: AgentFinalizedMemory
    completed: AgentCompletedRun

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": AGENT_PROTOCOL_VERSION,
            "prepared": self.prepared.to_dict(),
            "finalized": self.finalized.to_dict(),
            "completed": self.completed.to_dict(),
        }


class AgentMemoryError(RuntimeError):
    """Stable application-facing error independent of adapter exception text."""

    def __init__(
        self,
        code: str,
        category: AgentErrorCategory,
        operation: AgentOperation,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        bounded_message = str(message)
        if not bounded_message.strip():
            bounded_message = "agent memory operation failed"
        bounded_message = bounded_message[:AGENT_ERROR_MESSAGE_MAX_CHARS]
        self.code = code
        self.category = category
        self.operation = operation
        self.retryable = retryable
        self.request_id = _protocol_identifier(request_id)
        self.decision_id = _protocol_identifier(decision_id)
        super().__init__(bounded_message)

    def to_dict(self) -> dict[str, object]:
        error: dict[str, object] = {
            "code": self.code,
            "category": self.category,
            "message": str(self),
            "operation": self.operation,
            "retryable": self.retryable,
        }
        if self.request_id is not None:
            error["request_id"] = self.request_id
        if self.decision_id is not None:
            error["decision_id"] = self.decision_id
        return {
            "protocol_version": AGENT_PROTOCOL_VERSION,
            "error": error,
        }


AgentDecisionCallback: TypeAlias = Callable[
    [AgentPreparedMemory],
    str | Mapping[str, Any],
]
AgentExecutionCallback: TypeAlias = Callable[
    [AgentFinalizedMemory],
    MemoryRunMeasurement,
]


def agent_capabilities() -> AgentCapabilities:
    """Return the machine-readable local agent contract."""
    return AgentCapabilities(
        protocol_version=AGENT_PROTOCOL_VERSION,
        snapshot_version=SNAPSHOT_VERSION,
        sqlite_schema_version=SQLITE_SCHEMA_VERSION,
        postgres_schema_version=POSTGRES_SCHEMA_VERSION,
        storage_modes=("memory", "sqlite", "postgres"),
        operations=(
            "capture",
            "prepare",
            "finalize",
            "complete",
            "cancel",
            "run",
            "flush",
            "health",
        ),
        modes=(
            "debug",
            "repair",
            "regression",
            "planning",
            "eval",
            "production",
        ),
        limits={
            "gate_candidates": LLM_GATE_MAX_CANDIDATES,
            "gate_prompt_chars": LLM_GATE_PROMPT_MAX_CHARS,
            "gate_response_bytes": LLM_GATE_RESPONSE_MAX_BYTES,
            "gate_response_nodes": LLM_GATE_RESPONSE_MAX_NODES,
            "gate_response_depth": LLM_GATE_RESPONSE_MAX_DEPTH,
            "decision_reason_chars": MEMORY_DECISION_REASON_MAX_CHARS,
            "injection_memories": INJECTION_MAX_MEMORIES,
            "injection_chars": INJECTION_SNIPPET_MAX_CHARS,
            "prepared_request_candidates": GATE_REQUEST_MAX_CANDIDATES,
            "pending_requests": PENDING_GATE_REQUEST_MAX_ITEMS,
            "pending_candidate_references": (
                PENDING_GATE_REQUEST_MAX_CANDIDATE_IDS
            ),
            "finalized_request_replays": (
                FINALIZED_GATE_REQUEST_MAX_ITEMS
            ),
        },
        durable_records=(
            "traces",
            "failure_cases",
            "lessons",
            "project_policies",
            "usage_logs",
        ),
        process_local_records=(
            "pending_gate_requests",
            "finalized_gate_requests",
        ),
    )


def capture_local_trace(
    repo_path: str | Path,
    *,
    run_id: str | None = None,
    trace_id: str | None = None,
    tenant: str | None = None,
    prompt_version: str | None = None,
    prompt_family: str | None = None,
    tool_schema_version: str | None = None,
    model: str | None = None,
    eval_suite: str | None = None,
    input_hash: str | None = None,
    retrieved_context: Sequence[dict[str, object]] = (),
    tool_names: Sequence[str] = (),
) -> Trace:
    """Capture Git provenance and build a pending Trace for a local agent run."""
    try:
        _validate_capture_identifier(run_id, "run_id")
        _validate_capture_identifier(trace_id, "trace_id")
        for field_name, value in (
            ("tenant", tenant),
            ("prompt_version", prompt_version),
            ("prompt_family", prompt_family),
            ("tool_schema_version", tool_schema_version),
            ("model", model),
            ("eval_suite", eval_suite),
            ("input_hash", input_hash),
        ):
            _validate_capture_metadata(value, field_name)
        _validate_capture_sequences(retrieved_context, tool_names)
    except (TypeError, ValueError, OverflowError) as error:
        raise AgentMemoryError(
            "TBM_AGENT_INVALID_INPUT",
            "input",
            "capture",
            str(error),
        ) from error

    try:
        metadata = capture_trace_metadata(str(repo_path))
    except Exception as error:
        raise AgentMemoryError(
            "TBM_AGENT_CAPTURE_FAILED",
            "state",
            "capture",
            "could not capture local repository metadata",
            retryable=True,
        ) from error
    try:
        if metadata.repo is None:
            raise ValueError(
                "repository path must resolve to a named repository"
            )
        candidate = Trace(
            trace_id=(
                f"trace_{uuid4().hex}"
                if trace_id is None
                else trace_id
            ),
            run_id=(
                f"run_{uuid4().hex}"
                if run_id is None
                else run_id
            ),
            commit_sha=metadata.commit_sha,
            repo=metadata.repo,
            tenant=tenant,
            branch=metadata.branch,
            dirty=metadata.dirty,
            prompt_version=prompt_version,
            prompt_family=prompt_family,
            tool_schema_version=tool_schema_version,
            model=model,
            eval_suite=eval_suite,
            input_hash=input_hash,
            retrieved_context=list(retrieved_context),
            tool_calls=[
                {"name": tool_name} for tool_name in tool_names
            ],
            eval_result="unknown",
        )
        return TraceBackedMemoryStore().record_trace(candidate)
    except (TypeError, ValueError, OverflowError) as error:
        raise AgentMemoryError(
            "TBM_AGENT_INVALID_INPUT",
            "input",
            "capture",
            str(error),
        ) from error


class LocalAgentMemory:
    """Deep local runtime module over the existing evidence and Gate kernel."""

    def __init__(
        self,
        store: TraceBackedMemoryStore,
        *,
        repository: MemoryRepository | None = None,
        close_repository: bool = False,
    ) -> None:
        if type(store) is not TraceBackedMemoryStore:
            raise AgentMemoryError(
                "TBM_AGENT_INVALID_INPUT",
                "input",
                "open",
                "store must be exactly a TraceBackedMemoryStore",
            )
        if type(close_repository) is not bool:
            raise AgentMemoryError(
                "TBM_AGENT_INVALID_INPUT",
                "input",
                "open",
                "close_repository must be a boolean",
            )
        self._store = store
        self._repository = repository
        self._close_repository = close_repository
        self._requests: dict[str, MemoryGateRequest] = {}
        self._finalized: dict[
            str, tuple[str, AgentFinalizedMemory]
        ] = {}
        self._finalized_order: deque[str] = deque()
        self._lock = RLock()
        self._closed = False
        self._dirty = False

    @classmethod
    def in_memory(
        cls,
        store: TraceBackedMemoryStore | None = None,
    ) -> "LocalAgentMemory":
        return cls(
            TraceBackedMemoryStore() if store is None else store
        )

    @classmethod
    def from_repository(
        cls,
        repository: MemoryRepository,
        *,
        close_repository: bool = False,
    ) -> "LocalAgentMemory":
        if repository is None:
            raise AgentMemoryError(
                "TBM_AGENT_INVALID_INPUT",
                "input",
                "open",
                "repository is required",
            )
        if type(close_repository) is not bool:
            raise AgentMemoryError(
                "TBM_AGENT_INVALID_INPUT",
                "input",
                "open",
                "close_repository must be a boolean",
            )
        try:
            store = repository.load()
            if type(store) is not TraceBackedMemoryStore:
                raise TypeError(
                    "repository.load() must return "
                    "TraceBackedMemoryStore"
                )
        except Exception as error:
            raise AgentMemoryError(
                "TBM_AGENT_REPOSITORY_LOAD_FAILED",
                "persistence",
                "open",
                "could not load the memory repository",
                retryable=True,
            ) from error
        return cls(
            store,
            repository=repository,
            close_repository=close_repository,
        )

    @classmethod
    def open_sqlite(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = True,
        **connect_kwargs: object,
    ) -> "LocalAgentMemory":
        try:
            repository = SQLiteMemoryRepository.connect(
                database,
                initialize=initialize,
                **connect_kwargs,
            )
        except Exception as error:
            raise AgentMemoryError(
                "TBM_AGENT_REPOSITORY_CONNECT_FAILED",
                "persistence",
                "open",
                "could not connect to the SQLite memory repository",
                retryable=True,
            ) from error
        try:
            return cls.from_repository(
                repository,
                close_repository=True,
            )
        except BaseException:
            try:
                repository.close()
            except Exception:
                pass
            raise

    @classmethod
    def open_postgres(
        cls,
        conninfo: str = "",
        **connect_kwargs: object,
    ) -> "LocalAgentMemory":
        try:
            repository = PostgresMemoryRepository.connect(
                conninfo,
                **connect_kwargs,
            )
        except Exception as error:
            raise AgentMemoryError(
                "TBM_AGENT_REPOSITORY_CONNECT_FAILED",
                "persistence",
                "open",
                "could not connect to the PostgreSQL memory repository",
                retryable=True,
            ) from error
        try:
            return cls.from_repository(
                repository,
                close_repository=True,
            )
        except BaseException:
            try:
                repository.close()
            except Exception:
                pass
            raise

    @property
    def capabilities(self) -> AgentCapabilities:
        return agent_capabilities()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._require_open("prepare")
            return self._store.to_snapshot()

    def prepare(
        self,
        trace: Trace,
        context: MemoryContext,
        *,
        task: str,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
        context_summary: str = "",
        commit_ancestry: CommitAncestryEvidence | None = None,
    ) -> AgentPreparedMemory:
        with self._lock:
            self._require_open("prepare")
            if type(trace) is not Trace:
                raise self._input_error(
                    "prepare",
                    "trace must be exactly a Trace record",
                )
            try:
                current = self._store.traces.get(trace.trace_id)
                if current is None:
                    self._store.record_trace(trace)
                    self._dirty = True
                elif current != trace:
                    raise ValueError(
                        f"trace_id already exists with different evidence: "
                        f"{trace.trace_id}"
                    )
                self.flush()
                request = self._store.prepare_memory(
                    context,
                    task=task,
                    trace_id=trace.trace_id,
                    query=query,
                    semantic_scores=semantic_scores,
                    max_candidates=max_candidates,
                    minimum_score=minimum_score,
                    context_summary=context_summary,
                    commit_ancestry=commit_ancestry,
                )
            except AgentMemoryError:
                raise
            except (TypeError, ValueError, OverflowError) as error:
                raise AgentMemoryError(
                    "TBM_AGENT_PREPARE_REJECTED",
                    "state",
                    "prepare",
                    str(error),
                ) from error
            self._requests[request.request_id] = request
            return _prepared_result(request)

    def finalize(
        self,
        request_id: str,
        decision_payload: str | Mapping[str, Any],
    ) -> AgentFinalizedMemory:
        with self._lock:
            self._require_open("finalize")
            request_id = self._required_identifier(
                "finalize",
                request_id,
                "request_id",
            )
            try:
                decision_hash = _decision_hash(decision_payload)
            except (TypeError, ValueError, OverflowError) as error:
                raise AgentMemoryError(
                    "TBM_AGENT_INVALID_DECISION",
                    "input",
                    "finalize",
                    str(error),
                    request_id=request_id,
                ) from error
            prior = self._finalized.get(request_id)
            if prior is not None:
                prior_hash, result = prior
                if prior_hash != decision_hash:
                    raise AgentMemoryError(
                        "TBM_AGENT_DECISION_CONFLICT",
                        "state",
                        "finalize",
                        "request was already finalized with another decision",
                        request_id=request_id,
                    )
                self.flush()
                return result

            request = self._requests.get(request_id)
            if request is None:
                raise AgentMemoryError(
                    "TBM_AGENT_REQUEST_NOT_FOUND",
                    "state",
                    "finalize",
                    "prepared request is not available in this process",
                    request_id=request_id,
                )
            if request.trace_id is None:
                raise AgentMemoryError(
                    "TBM_AGENT_REQUEST_NOT_TRACE_BOUND",
                    "state",
                    "finalize",
                    "prepared request is not bound to a Trace",
                    request_id=request_id,
                )
            try:
                gated = self._store.finalize_memory(
                    request,
                    decision_payload,
                    trace_id=request.trace_id,
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise AgentMemoryError(
                    "TBM_AGENT_FINALIZE_REJECTED",
                    "state",
                    "finalize",
                    str(error),
                    request_id=request_id,
                ) from error

            result = _finalized_result(gated)
            self._remember_finalized(
                request_id,
                decision_hash,
                result,
            )
            self._requests.pop(request_id, None)
            self._dirty = True
            self.flush(
                request_id=request_id,
                decision_id=result.decision_id,
            )
            return result

    def prepare_with_git_ancestry(
        self,
        trace: Trace,
        context: MemoryContext,
        *,
        repo_path: str | Path,
        task: str,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
        context_summary: str = "",
    ) -> AgentPreparedMemory:
        """Prepare with complete ancestry captured outside the Store lock."""
        with self._lock:
            self._require_open("prepare")
            try:
                anchors = self._store.candidate_commit_anchors(context)
            except (TypeError, ValueError, OverflowError) as error:
                raise AgentMemoryError(
                    "TBM_AGENT_PREPARE_REJECTED",
                    "state",
                    "prepare",
                    str(error),
                ) from error
        try:
            commit_ancestry = capture_commit_ancestry(
                context.commit_sha,
                anchors,
                repo_path=repo_path,
            )
        except Exception as error:
            raise AgentMemoryError(
                "TBM_AGENT_ANCESTRY_CAPTURE_FAILED",
                "state",
                "prepare",
                "could not capture complete Git ancestry evidence",
                retryable=True,
            ) from error
        return self.prepare(
            trace,
            context,
            task=task,
            query=query,
            semantic_scores=semantic_scores,
            max_candidates=max_candidates,
            minimum_score=minimum_score,
            context_summary=context_summary,
            commit_ancestry=commit_ancestry,
        )

    def complete(
        self,
        decision_id: str,
        measurement: MemoryRunMeasurement,
    ) -> AgentCompletedRun:
        with self._lock:
            self._require_open("complete")
            decision_id = self._required_identifier(
                "complete",
                decision_id,
                "decision_id",
            )
            if type(measurement) is not MemoryRunMeasurement:
                raise self._input_error(
                    "complete",
                    "measurement must be exactly a MemoryRunMeasurement",
                    decision_id=decision_id,
                )
            usage_log = next(
                (
                    log
                    for log in self._store.usage_logs
                    if log.decision_id == decision_id
                ),
                None,
            )
            if usage_log is None or usage_log.trace_id is None:
                raise AgentMemoryError(
                    "TBM_AGENT_DECISION_NOT_FOUND",
                    "state",
                    "complete",
                    "decision is not available or is not Trace-linked",
                    decision_id=decision_id,
                )
            try:
                completion_values: dict[str, Any] = {
                    field_name: getattr(measurement, field_name)
                    for field_name in (
                        "output_hash",
                        "latency_ms",
                        "cost_usd",
                        "error",
                        "trace_uri",
                    )
                    if getattr(measurement, field_name) is not None
                }
                if measurement.tool_outputs is not None:
                    completion_values["tool_outputs"] = [
                        dict(item)
                        for item in measurement.tool_outputs
                    ]
                completion = self._store.complete_memory_run(
                    trace_id=usage_log.trace_id,
                    decision_id=decision_id,
                    eval_result=measurement.eval_result,
                    memory_caused_failure=(
                        measurement.memory_caused_failure
                    ),
                    **completion_values,
                )
                result = _completed_result(completion)
            except (TypeError, ValueError, OverflowError) as error:
                raise AgentMemoryError(
                    "TBM_AGENT_COMPLETE_REJECTED",
                    "state",
                    "complete",
                    str(error),
                    request_id=usage_log.request_id,
                    decision_id=decision_id,
                ) from error
            self._dirty = True
            self.flush(
                request_id=usage_log.request_id,
                decision_id=decision_id,
            )
            return result

    def cancel(self, request_id: str) -> None:
        with self._lock:
            self._require_open("cancel")
            request_id = self._required_identifier(
                "cancel",
                request_id,
                "request_id",
            )
            request = self._requests.get(request_id)
            if request is None:
                raise AgentMemoryError(
                    "TBM_AGENT_REQUEST_NOT_FOUND",
                    "state",
                    "cancel",
                    "prepared request is not available in this process",
                    request_id=request_id,
                )
            try:
                self._store.cancel_memory_request(request)
            except (TypeError, ValueError, OverflowError) as error:
                raise AgentMemoryError(
                    "TBM_AGENT_CANCEL_REJECTED",
                    "state",
                    "cancel",
                    str(error),
                    request_id=request_id,
                ) from error
            self._requests.pop(request_id, None)

    def run(
        self,
        trace: Trace,
        context: MemoryContext,
        *,
        task: str,
        decide: AgentDecisionCallback,
        execute: AgentExecutionCallback,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
        context_summary: str = "",
        commit_ancestry: CommitAncestryEvidence | None = None,
    ) -> AgentRunResult:
        prepared = self.prepare(
            trace,
            context,
            task=task,
            query=query,
            semantic_scores=semantic_scores,
            max_candidates=max_candidates,
            minimum_score=minimum_score,
            context_summary=context_summary,
            commit_ancestry=commit_ancestry,
        )
        try:
            decision_payload = decide(prepared)
        except Exception as error:
            raise AgentMemoryError(
                "TBM_AGENT_DECISION_CALLBACK_FAILED",
                "callback",
                "run",
                "agent decision callback failed",
                retryable=True,
                request_id=prepared.request_id,
            ) from error
        finalized = self.finalize(prepared.request_id, decision_payload)
        try:
            measurement = execute(finalized)
            if type(measurement) is not MemoryRunMeasurement:
                raise TypeError(
                    "agent execution callback must return "
                    "MemoryRunMeasurement"
                )
        except Exception as error:
            raise AgentMemoryError(
                "TBM_AGENT_EXECUTION_CALLBACK_FAILED",
                "callback",
                "run",
                "agent execution callback failed",
                retryable=True,
                request_id=prepared.request_id,
                decision_id=finalized.decision_id,
            ) from error
        completed = self.complete(finalized.decision_id, measurement)
        return AgentRunResult(
            prepared=prepared,
            finalized=finalized,
            completed=completed,
        )

    def flush(
        self,
        *,
        request_id: str | None = None,
        decision_id: str | None = None,
    ) -> object | None:
        with self._lock:
            self._require_open("flush")
            request_id = self._optional_identifier(
                "flush",
                request_id,
                "request_id",
            )
            decision_id = self._optional_identifier(
                "flush",
                decision_id,
                "decision_id",
            )
            if self._repository is None or not self._dirty:
                return None
            try:
                result = self._repository.sync(self._store)
            except Exception as error:
                raise AgentMemoryError(
                    "TBM_AGENT_PERSISTENCE_FAILED",
                    "persistence",
                    "flush",
                    "could not persist the memory runtime state",
                    retryable=True,
                    request_id=request_id,
                    decision_id=decision_id,
                ) from error
            self._dirty = False
            return result

    def health(self) -> dict[str, object]:
        """Return non-sensitive runtime and measured-outcome health."""
        with self._lock:
            self._require_open("health")
            return {
                "protocol_version": AGENT_PROTOCOL_VERSION,
                "pending_request_count": len(self._requests),
                "finalized_request_replay_count": len(self._finalized),
                "memory_metrics": asdict(self._store.metrics()),
                "memory_run_metrics": asdict(
                    self._store.memory_run_metrics()
                ),
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self.flush()
            except AgentMemoryError:
                raise
            if self._repository is not None and self._close_repository:
                try:
                    self._repository.close()
                except Exception as error:
                    raise AgentMemoryError(
                        "TBM_AGENT_REPOSITORY_CLOSE_FAILED",
                        "persistence",
                        "close",
                        "could not close the memory repository",
                    ) from error
            self._closed = True

    def __enter__(self) -> "LocalAgentMemory":
        with self._lock:
            self._require_open("prepare")
            return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _require_open(self, operation: AgentOperation) -> None:
        if self._closed:
            raise AgentMemoryError(
                "TBM_AGENT_CLOSED",
                "closed",
                operation,
                "local agent memory is closed",
            )

    def _remember_finalized(
        self,
        request_id: str,
        decision_hash: str,
        result: AgentFinalizedMemory,
    ) -> None:
        self._finalized[request_id] = (decision_hash, result)
        self._finalized_order.append(request_id)
        while (
            len(self._finalized_order)
            > FINALIZED_GATE_REQUEST_MAX_ITEMS
        ):
            expired_request_id = self._finalized_order.popleft()
            self._finalized.pop(expired_request_id, None)

    @classmethod
    def _required_identifier(
        cls,
        operation: AgentOperation,
        value: object,
        field_name: str,
    ) -> str:
        identifier = _protocol_identifier(value)
        if identifier is None:
            raise cls._input_error(
                operation,
                f"{field_name} must be a nonblank string at most "
                f"{MEMORY_ID_MAX_CHARS} characters",
            )
        return identifier

    @classmethod
    def _optional_identifier(
        cls,
        operation: AgentOperation,
        value: object | None,
        field_name: str,
    ) -> str | None:
        if value is None:
            return None
        return cls._required_identifier(
            operation,
            value,
            field_name,
        )

    @staticmethod
    def _input_error(
        operation: AgentOperation,
        message: str,
        *,
        request_id: str | None = None,
        decision_id: str | None = None,
    ) -> AgentMemoryError:
        return AgentMemoryError(
            "TBM_AGENT_INVALID_INPUT",
            "input",
            operation,
            message,
            request_id=request_id,
            decision_id=decision_id,
        )


def _prepared_result(request: MemoryGateRequest) -> AgentPreparedMemory:
    if request.trace_id is None or request.run_id is None:
        raise ValueError("agent preparation requires a Trace-bound request")
    return AgentPreparedMemory(
        protocol_version=AGENT_PROTOCOL_VERSION,
        request_id=request.request_id,
        trace_id=request.trace_id,
        run_id=request.run_id,
        candidate_memory_ids=request.candidate_memory_ids,
        system_allowed_memory_ids=request.system_allowed_memory_ids,
        system_blocked=request.system_blocked,
        prompt=request.prompt,
    )


def _finalized_result(result: GatedMemoryResult) -> AgentFinalizedMemory:
    return AgentFinalizedMemory(
        protocol_version=AGENT_PROTOCOL_VERSION,
        request_id=result.request_id,
        trace_id=result.trace_id,
        decision_id=result.decision_id,
        use_memory=result.use_memory,
        allowed_memory_ids=result.allowed_memory_ids,
        blocked_memory_ids=result.blocked_memory_ids,
        reason=result.reason,
        risk=result.risk,
        recommended_injection=result.recommended_injection,
        snippet=result.snippet,
    )


def _completed_result(completion: MemoryRunCompletion) -> AgentCompletedRun:
    eval_result = completion.trace.eval_result
    if eval_result not in {"pass", "fail", "error"}:
        raise ValueError("completed agent run must have a measured result")
    return AgentCompletedRun(
        protocol_version=AGENT_PROTOCOL_VERSION,
        request_id=completion.usage_log.request_id,
        trace_id=completion.trace.trace_id,
        run_id=completion.trace.run_id,
        decision_id=completion.usage_log.decision_id,
        eval_result=eval_result,
        memory_caused_failure=(
            completion.usage_log.memory_caused_failure
        ),
    )


def _decision_hash(
    decision_payload: str | Mapping[str, Any],
) -> str:
    decision = parse_memory_decision(decision_payload)
    canonical = json.dumps(
        asdict(decision),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_capture_identifier(
    value: object | None,
    field_name: str,
) -> None:
    if value is not None and _protocol_identifier(value) is None:
        raise ValueError(
            f"{field_name} must be None or a nonblank string at most "
            f"{MEMORY_ID_MAX_CHARS} characters"
        )


def _validate_capture_metadata(
    value: object | None,
    field_name: str,
) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not value
        or len(value) > METADATA_VALUE_MAX_CHARS
    ):
        raise ValueError(
            f"{field_name} must be None or a non-empty string at most "
            f"{METADATA_VALUE_MAX_CHARS} characters"
        )


def _validate_capture_sequences(
    retrieved_context: object,
    tool_names: object,
) -> None:
    if type(retrieved_context) not in {list, tuple}:
        raise ValueError("retrieved_context must be a list or tuple")
    if type(tool_names) not in {list, tuple}:
        raise ValueError("tool_names must be a list or tuple")
    if (
        len(retrieved_context) + len(tool_names)
        > TRACE_JSON_MAX_NODES - 3
    ):
        raise ValueError(
            "retrieved_context and tool_names contain too many items"
        )
    if any(type(item) is not dict for item in retrieved_context):
        raise ValueError(
            "retrieved_context must contain exact JSON object dictionaries"
        )
    tool_name_bytes = 0
    for tool_name in tool_names:
        if (
            not isinstance(tool_name, str)
            or not tool_name.strip()
            or len(tool_name) > METADATA_VALUE_MAX_CHARS
        ):
            raise ValueError(
                "tool_names must contain nonblank strings at most "
                f"{METADATA_VALUE_MAX_CHARS} characters"
            )
        try:
            tool_name_bytes += len(tool_name.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError(
                "tool_names must contain UTF-8 encodable strings"
            ) from error
        if tool_name_bytes > TRACE_JSON_MAX_TEXT_BYTES:
            raise ValueError(
                "tool_names exceed the Trace JSON text budget"
            )
