# Memory Run Execution Design

## Problem

The Store already owns safe preparation, finalization, and atomic completion,
but every runtime caller must preserve the exact order and transfer the gate
request, Trace ID, decision ID, snippet, measured result, and completion
evidence across external LLM and harness calls. Repeating that orchestration in
each harness makes linkage mistakes and incomplete error handling likely.

Add one synchronous, dependency-free orchestration module for the common
single-run path. It must deepen the existing workflow without moving retrieval,
gate, validation, linkage, recovery, or persistence rules out of
`TraceBackedMemoryStore`.

## Interface

Expose these names from the package root:

```python
MemoryDecisionCallback = Callable[
    [MemoryGateRequest],
    str | Mapping[str, Any],
]
MemoryExecutionCallback = Callable[
    [GatedMemoryResult],
    MemoryRunMeasurement,
]


@dataclass(frozen=True)
class MemoryRunMeasurement:
    eval_result: MeasuredEvalResult
    memory_caused_failure: bool = False
    output_hash: str | None = None
    tool_outputs: tuple[dict[str, object], ...] | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    trace_uri: str | None = None


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
    ...
```

The caller must register an `unknown` Trace first. The decision callback
receives the complete public `MemoryGateRequest`, and the execution callback
receives the complete public `GatedMemoryResult`. The executor returns a
measurement without a decision ID; the orchestration module always uses the
decision ID created by the Store.

Advanced callers that pause between stages, retry with custom policies, or own
separate Trace and decision lifecycles continue using the existing Store
methods directly.

## Sequence And Ownership

The implementation performs exactly this sequence:

1. Call `store.prepare_memory()` with the caller's retrieval options.
2. Call `decide(request)`.
3. Call `store.finalize_memory(request, payload, trace_id=trace_id)` without an
   execution outcome.
4. Call `execute(gated_result)`.
5. Require an exact `MemoryRunMeasurement` record.
6. Call `store.complete_memory_run()` with the Store-produced decision ID and
   only the non-`None` optional evidence from the measurement.
7. Return the Store's `MemoryRunCompletion` unchanged.

The Store remains the only owner of candidate retrieval, System Gate, decision
parsing, LLM narrowing, request ownership and consumption, snippet rendering,
usage logging, Trace/context binding, decision linkage, measured-outcome
validation, immutable evidence, replay/conflict behavior, locks, and atomic
Trace plus usage-log completion.

The orchestration module must not access private Store state, construct usage
logs, generate IDs, infer an evaluator result or attribution, write snapshots,
synchronize PostgreSQL, or invoke low-level one-sided completion methods.

## Callback Errors

Expose one `MemoryRunCallbackError(RuntimeError)` for failures raised by the
two true-external callbacks. It has these public attributes:

- `phase`: `"decision"` or `"execution"`;
- `trace_id`;
- `request`: the prepared `MemoryGateRequest`;
- `request_id`;
- `gated_result`: `None` before finalization, otherwise the finalized result;
- `decision_id`: `None` before finalization, otherwise the Store decision ID.

The original callback exception is retained as `__cause__`. Catch ordinary
`Exception` only; `KeyboardInterrupt` and `SystemExit` pass through.

A decision callback failure creates no usage log and leaves the request
pending. An execution callback failure occurs after the usage decision exists,
does not guess an `error` outcome, and leaves Trace and decision unevaluated.
The error exposes enough public state for a caller to retry or explicitly
complete later.

A non-`MemoryRunMeasurement` execution return is an execution callback contract
failure and uses the same contextual error. Store errors from prepare,
finalize, and complete propagate unchanged so callers retain exact validation
and conflict diagnostics.

## Measurement Semantics

`MemoryRunMeasurement` deliberately omits `decision_id`; this is the leverage
over the existing batch-oriented `MemoryRunResult` command. `None` on optional
evidence means omitted, matching `MemoryRunResult`. A non-`None`
`tool_outputs` tuple is converted to a list only when calling the Store.

The orchestration module does not pre-validate measured outcomes, attribution,
JSON evidence, costs, or field bounds. It forwards them to
`complete_memory_run()`, which validates both candidate records before any
assignment. Completion failure therefore keeps the existing all-or-nothing
Store behavior.

## Dependencies And Seam

`TraceBackedMemoryStore` is an in-process dependency and is used directly; do
not add a one-implementation Store protocol. The decision and execution
callables are true-external seams with production adapters in caller code and
fixed adapters in tests. Type aliases are sufficient for their one-method
shape and avoid expanding the public interface with nominal adapter classes.

The module imports only the standard library and existing package types. It
does not depend on an LLM SDK, harness framework, PostgreSQL driver, filesystem,
thread, or async runtime.

## Persistence And Compatibility

`MemoryRunMeasurement`, callback types, and callback errors are ephemeral.
Only the existing Trace and usage-log records completed by the Store are
persisted. Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 1 remain unchanged.

## Verification

Tests cover the successful call order and callback inputs; no-memory and used-
memory decisions; all measurement evidence; decision and execution callback
failures; callback contract violations; Store prepare/finalize/completion
errors; invalid evidence atomicity; exact completion replay behavior; no access
to Store private state; public exports; executable README usage; and unchanged
persistence schemas.

