# Memory Run Metrics Design

## Problem

`memory_run_audits()` exposes the completion state of each Trace-linked usage
decision, but operational callers must still fetch every audit and aggregate
the five states themselves. Existing `metrics()` summarizes memory selection
and decision outcomes; it cannot reveal a Trace/decision mismatch or distinguish
pending work from an interrupted one-sided completion.

## Public API

Export a frozen derived model and one store method:

```python
metrics = store.memory_run_metrics()
assert metrics.decision_count == (
    metrics.pending_count
    + metrics.trace_only_count
    + metrics.decision_only_count
    + metrics.complete_count
    + metrics.conflict_count
)
```

`MemoryRunMetrics` contains `decision_count`, one count for each audit status,
and `recoverable_count`. The recovery count is exactly the sum of
`trace_only_count` and `decision_only_count`; pending and conflicting runs are
not presented as automatically recoverable.

## Counting Semantics

The aggregation uses one usage decision as its unit, matching
`memory_run_audits()`. A Trace with multiple linked decisions contributes one
count per decision, while a Trace with no usage decision contributes nothing.
The five audit statuses are mutually exclusive and exhaustive, so their counts
always sum to `decision_count`. An empty store returns zero for every field.

The method reuses the existing audit view and its private status classifier.
This keeps one source of truth for the distinction between unevaluated values
(`None` and `unknown`) and measured values (`pass`, `fail`, and `error`). The
existing `MemoryMetrics` and `MemoryOutcomeMetrics` contracts remain unchanged;
they answer outcome and per-memory questions rather than cross-record health.

## Consistency And Persistence

`memory_run_metrics()` runs under the store's reentrant lock and derives one
immutable point-in-time value from validated private state. It performs no
mutation and stores no aggregate. Snapshot and PostgreSQL loads reproduce the
same value from their restored Trace and usage records.

No snapshot field, JSON Schema, YAML shape, SQL column, or table is added.
Snapshot version 2, `schemas/postgres.sql`, and PostgreSQL schema version 1
remain unchanged.

## Verification

Tests cover the frozen public model, empty stores, every audit status, the sum
and recoverability invariants, multiple decisions linked to one Trace, snapshot
round trips, recovery-driven state transitions, PostgreSQL reload parity, and
documentation compatibility.
