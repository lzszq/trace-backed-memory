# Atomic Batch Memory Run Completion Design

## Problem

`complete_memory_run()` atomically records one measured execution result on a
Trace and its linked usage decision. Evaluation workers commonly finish more
than one run at a time, but looping over that API creates a caller-owned batch
boundary: a conflict or invalid evidence on a later run leaves earlier runs
committed. `recover_memory_runs()` closes this gap only for results already
recorded on one side and deliberately cannot introduce new measurements.

## Public Models And API

Export a measured-result alias and frozen input model:

```python
result = MemoryRunResult(
    decision_id="decision_000002",
    eval_result="pass",
    output_hash="sha256:output",
    tool_outputs=({"documents": 3},),
    latency_ms=125,
)
completions = store.complete_memory_runs((result,))
```

`MeasuredEvalResult` is exactly `pass`, `fail`, or `error`.
`MemoryRunResult` contains `decision_id`, `eval_result`,
`memory_caused_failure`, and optional `output_hash`, `tool_outputs`,
`latency_ms`, `cost_usd`, `error`, and `trace_uri` evidence. `tool_outputs` is
an optional tuple at the immutable request boundary and becomes a list on the
completed Trace. `None` means an evidence field was omitted and preserves an
existing value; an explicit empty tool-output tuple requests an empty list.

`complete_memory_runs()` accepts a non-empty tuple of exact
`MemoryRunResult` values with unique decision IDs. It derives each `trace_id`
and `run_id` from the validated usage decision, so callers cannot repeat or
spoof linkage. It returns a tuple of defensive `MemoryRunCompletion` values in
request order.

## Shared Trace Semantics

Multiple decisions may link to one Trace. Every requested result for that Trace
must use the same measured outcome. Supplied completion evidence is merged by
field: omitted fields contribute nothing, disjoint fields combine, and repeated
fields must normalize to equal values. A result or evidence disagreement rejects
the entire batch.

Each request is first applied independently to the original Trace candidate so
invalid nested JSON, types, limits, immutable evidence rewrites, and measured
Trace replay rules fail before evidence comparison. One final candidate is then
built from the merged normalized evidence. Request order does not determine the
resulting shared Trace.

## Partial State And Replay

The same candidate functions as single completion remain authoritative. A
pending pair advances to the supplied result. A Trace-only or decision-only
pair completes only when the supplied outcome agrees with the measured side.
A complete pair replays exactly. Existing result, attribution, or Trace evidence
conflicts reject the whole batch. `memory_caused_failure=True` still requires a
failed or errored decision that actually used memory.

## Atomicity And Reuse

The method validates the exact request tuple, builds a decision index, derives
linkage, normalizes and merges shared Trace evidence, builds all final Trace and
sealed usage candidates, validates every candidate, and creates all defensive
return copies under the store lock. Only then are private Trace and usage
collections assigned.

Extract a common staging helper that accepts already resolved result rows.
`complete_memory_runs()` supplies caller-measured rows and evidence;
`recover_memory_runs()` retains its entry-state eligibility and attribution
logic, then supplies derived rows with no new evidence. The staging helper does
not mutate state. This keeps batch commit, shared Trace, candidate validation,
and defensive-copy behavior in one implementation without weakening recovery.

Invalid containers or records, empty or duplicate IDs, unknown decisions,
unsupported outcomes, invalid attribution, malformed evidence, shared Trace
disagreement, partial-state conflict, or an injected later candidate failure
leave every record unchanged. Exact batch replay is idempotent.

## Persistence Boundary

`MemoryRunResult` is an ephemeral command and is not persisted. Batch
completion changes only existing Trace completion fields and decision outcome
pairs. PostgreSQL synchronization persists all changes in its existing
transaction. Snapshot version 2, JSON Schemas, active-lessons YAML,
`schemas/postgres.sql`, and PostgreSQL schema version 1 remain unchanged.

## Verification

Tests cover frozen exports, mixed measured outcomes and evidence, request order,
defensive copies, exact replay, partial states, same-Trace evidence merge,
same-Trace result/evidence conflicts, invalid request boundaries, later
candidate rollback, snapshot reconstruction, PostgreSQL multi-row forward
updates and reload, README execution, and documentation compatibility.
