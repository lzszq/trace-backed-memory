# Deferred Trace Completion Design

## Problem

The safe memory workflow must link finalization to an existing Trace before the
caller executes with the returned snippet. A complete Trace, however, contains
evidence that only exists after execution: output identity, tool outputs,
measured evaluation, latency, cost, and errors. Because Trace rows are
currently immutable, a caller must either fabricate future evidence before
execution or leave the current Trace permanently `unknown` and incomplete.

## Public Lifecycle

Add a store-owned completion API:

```python
completed = store.complete_trace(
    trace_id,
    eval_result="pass",
    output_hash="sha256:...",
    tool_outputs=[{"documents": 3}],
    latency_ms=125,
    cost_usd=0.002,
)
```

The initial Trace must have `eval_result="unknown"`. Completion requires one
measured result: `pass`, `fail`, or `error`.

Completion fields are:

- `output_hash`;
- `tool_outputs`;
- `eval_result`;
- `latency_ms`;
- `cost_usd`;
- `error`;
- `trace_uri`.

Optional completion arguments that are omitted preserve their current values.
An empty completion slot may be filled, and an already populated slot may be
repeated exactly, but existing non-empty execution evidence cannot be changed.
All supplied fields are validated through the normal Trace contract and copied
before storage.

Every other Trace field is immutable, including identity, repo and commit
provenance, tenant, branch and dirty state, prompt/tool/model/eval-suite
metadata, input hash, retrieved context, tool calls, and `created_at`.

The transition is atomic under the store lock. Exact replay of an already
completed Trace is idempotent. A different measured result, a changed
completion field, an unmeasured result, or an unknown Trace ID is rejected
without mutation. The method returns a defensive copy.

Callers may still record a fully measured Trace in one `record_trace()` call.
Calling `complete_trace()` against such a record is allowed only as exact
replay.

## Decision Outcome Relationship

Trace completion and decision-outcome sealing are separate audit operations.
`complete_trace()` never mutates usage logs, and
`record_decision_outcome()` never mutates a Trace. A normal memory run completes
the current Trace and seals the returned decision ID after execution. Callers
should use the same measured evaluator result when those records describe the
same evaluation, but this phase does not reinterpret or rewrite legacy logs.

## PostgreSQL Synchronization

The existing Trace columns already store every completion field. Repository
synchronization gains one controlled forward transition:

- insert absent Trace rows as before;
- treat exact rows as unchanged;
- lock an existing Trace row;
- allow stored `unknown` to become `pass`, `fail`, or `error` while filling
  only empty completion slots or preserving populated slots exactly;
- reject provenance/input changes, populated-evidence rewrites, measured-result
  rewrites, and stale downgrades to `unknown`;
- roll back a Trace update if any later row in the synchronization conflicts.

The SQL schema file, schema version 1, snapshot version 2, JSON Schemas, and
active-lessons YAML remain unchanged.

## Verification

Tests cover each completion field, omitted-field preservation, pre-populated
evidence, copy isolation, exact replay, fully measured Trace compatibility,
immutable and sealed conflicts, invalid values, concurrency, snapshot round
trips, end-to-end memory execution, PostgreSQL update/load/idempotence,
distributed replay, stale and conflicting syncs, and transaction rollback.
