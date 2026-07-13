# Atomic Memory Run Completion Design

## Problem

The chronological runtime now has two safe low-level transitions after task
execution:

1. `complete_trace()` records measured execution evidence;
2. `record_decision_outcome()` seals the memory decision outcome.

Each transition is atomic on its own, but a normal caller must perform both.
If the second validation fails or the process stops between calls, one audit
record can be complete while the other remains unevaluated. The APIs also let a
caller accidentally use different measured results for records that describe
the same evaluation.

## Public API

Add a frozen result model and one high-level store method:

```python
completion = store.complete_memory_run(
    trace_id=result.trace_id,
    decision_id=result.decision_id,
    eval_result="pass",
    output_hash="sha256:...",
    tool_outputs=[{"documents": 3}],
    latency_ms=125,
)
```

`MemoryRunCompletion` contains:

- `trace`: the completed `Trace`;
- `usage_log`: the sealed `MemoryUsageLog`.

The method accepts the same measured Trace completion fields as
`complete_trace()` plus `memory_caused_failure`. The one `eval_result` argument
is applied to both records and must be `pass`, `fail`, or `error`.

## Ownership And Linkage

The decision must exist and its `trace_id` and `run_id` must identify the
supplied Trace. A decision linked to another Trace cannot be combined even when
all provenance values happen to match.

All Trace completion rules and decision-outcome rules remain in force:

- immutable or populated Trace evidence cannot be rewritten;
- an already measured Trace must be an exact replay;
- an already measured usage outcome must replay the exact result and failure
  attribution;
- `memory_caused_failure=True` requires failed or errored use of memory;
- all copied Trace and usage-log contracts are revalidated.

## Atomicity And Recovery

The store lock covers lookup, linkage checks, candidate construction,
validation, and both assignments. Neither record changes until both candidates
are valid.

Supported starting states are:

- both pending: complete both;
- Trace already completed with the same result: seal the pending decision;
- decision already sealed with the same result: complete the pending Trace;
- both already equal: return an idempotent replay.

Any conflicting measured result, attribution, Trace evidence, or linkage
rejects the operation without changing either record. Concurrent conflicting
completion attempts therefore produce one complete, internally consistent
winner.

The low-level methods remain public for callers that intentionally own separate
lifecycles and for recovery. This phase does not retroactively reinterpret or
rewrite legacy records that used them independently.

## Persistence

`MemoryRunCompletion` is a return value, not a persisted record. Trace and usage
log fields remain unchanged, so snapshot version 2, JSON Schemas,
active-lessons YAML, and PostgreSQL schema version 1 remain unchanged.

`PostgresMemoryRepository.sync()` already processes a store snapshot in one
transaction, updating traces before usage logs. Tests must prove that a sync
containing both forward transitions reports both updates, loads the consistent
pair, is idempotent, and rolls the Trace update back if the usage row later
conflicts.

## Verification

Tests cover public export and frozen return values, successful completion,
defensive copies, exact replay, linkage rejection, Trace-side and usage-side
validation failures, both partial-recovery directions, conflicting partial
states, concurrency, snapshot round trips, metrics, PostgreSQL dual updates,
idempotence, and cross-record transaction rollback.
