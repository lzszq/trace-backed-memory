from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeAlias

from .models import (
    CommitAncestryEvidence,
    GatedMemoryResult,
    MemoryContext,
    MemoryGateRequest,
    MemoryRunCompletion,
    MemoryRunMeasurement,
)
from .store import TraceBackedMemoryStore


MemoryDecisionCallback: TypeAlias = Callable[
    [MemoryGateRequest],
    str | Mapping[str, Any],
]
MemoryExecutionCallback: TypeAlias = Callable[
    [GatedMemoryResult],
    MemoryRunMeasurement,
]


class MemoryRunExecutionError(RuntimeError):
    """Adds recoverable run context to a post-preparation failure."""

    def __init__(
        self,
        phase: Literal[
            "decision",
            "finalization",
            "execution",
            "completion",
        ],
        *,
        trace_id: str,
        request: MemoryGateRequest,
        gated_result: GatedMemoryResult | None = None,
    ) -> None:
        self.phase = phase
        self.trace_id = trace_id
        self.request = request
        self.request_id = request.request_id
        self.gated_result = gated_result
        self.decision_id = (
            gated_result.decision_id if gated_result is not None else None
        )
        super().__init__(f"memory run {phase} failed")


def run_memory_execution(
    store: TraceBackedMemoryStore,
    *,
    context: MemoryContext,
    trace_id: str,
    task: str,
    decide: MemoryDecisionCallback,
    execute: MemoryExecutionCallback,
    query: str | None = None,
    semantic_scores: Mapping[str, float] | None = None,
    max_candidates: int | None = None,
    minimum_score: float | None = None,
    context_summary: str = "",
    commit_ancestry: CommitAncestryEvidence | None = None,
) -> MemoryRunCompletion:
    """Run the common prepare, gate, execute, and atomic completion path."""
    request = store.prepare_memory(
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
        decision_payload = decide(request)
    except Exception as error:
        raise MemoryRunExecutionError(
            "decision",
            trace_id=trace_id,
            request=request,
        ) from error

    try:
        gated_result = store.finalize_memory(
            request,
            decision_payload,
            trace_id=trace_id,
        )
    except Exception as error:
        raise MemoryRunExecutionError(
            "finalization",
            trace_id=trace_id,
            request=request,
        ) from error
    try:
        measurement = execute(gated_result)
        if type(measurement) is not MemoryRunMeasurement:
            raise TypeError(
                "memory execution callback must return MemoryRunMeasurement"
            )
    except Exception as error:
        raise MemoryRunExecutionError(
            "execution",
            trace_id=trace_id,
            request=request,
            gated_result=gated_result,
        ) from error

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
        completion_values["tool_outputs"] = list(measurement.tool_outputs)

    try:
        return store.complete_memory_run(
            trace_id=trace_id,
            decision_id=gated_result.decision_id,
            eval_result=measurement.eval_result,
            memory_caused_failure=measurement.memory_caused_failure,
            **completion_values,
        )
    except Exception as error:
        raise MemoryRunExecutionError(
            "completion",
            trace_id=trace_id,
            request=request,
            gated_result=gated_result,
        ) from error
