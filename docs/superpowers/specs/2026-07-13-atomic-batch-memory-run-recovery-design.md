# Atomic Batch Memory Run Recovery Design

## Problem

`memory_run_audits()` and `memory_run_metrics()` expose one-sided memory runs,
and `recover_memory_run()` safely repairs one decision. Operational workers
must still loop over recoverable decisions. If a later call fails, earlier
calls remain committed, producing a partially applied recovery batch whose
boundary exists only in caller code.

## Public API

Add one store operation:

```python
completions = store.recover_memory_runs(
    ("decision_000002", "decision_000003"),
    memory_caused_failures={"decision_000003": False},
)
```

`decision_ids` must be a non-empty tuple of unique, valid decision IDs. The
returned tuple preserves that order and contains one frozen
`MemoryRunCompletion` with defensive copies per decision.

`memory_caused_failures` is an optional mapping whose keys must be requested
decision IDs and whose values must be exact booleans. A failed or errored
`trace_only` decision requires an explicit mapping entry. A passing
`trace_only` decision defaults to `False`. For `decision_only` and `complete`,
omission preserves the sealed attribution and a different supplied value is a
conflict.

The batch method does not accept caller-supplied Trace IDs, evaluation results,
or Trace completion evidence. It derives correlated IDs and results from
validated state. Call `recover_memory_run()` individually when output hash,
tool output, latency, cost, error, or Trace URI evidence must be attached.

## Eligibility And Shared Traces

Every requested decision is classified from state at method entry. Only
`trace_only`, `decision_only`, and `complete` are eligible. `pending` and
`conflict` reject the whole batch. A pending decision cannot become eligible
merely because another requested decision shares its Trace.

Multiple requested decisions may share one Trace. Their independently derived
measured results must agree. One Trace candidate is then completed once and
all linked decision candidates are sealed against that result. Different
derived results reject the whole batch before mutation. This makes behavior
independent of request order while retaining one decision per returned item.

## Atomicity

The operation runs under the store reentrant lock. It validates the immutable
input snapshot, resolves every result and attribution, groups shared Traces,
builds all Trace and usage-log candidates, validates all candidates, and builds
defensive return copies before assigning any private record.

Invalid containers, empty or duplicate IDs, unknown IDs, extra attribution
keys, invalid attribution values, pending or conflicting audits, missing
failed-run attribution, shared-Trace result disagreement, and candidate
validation errors leave every Trace and usage decision unchanged. Exact replay
of an already complete batch is idempotent.

## Persistence Boundary

Batch recovery changes only existing Trace completion fields and decision
outcome pairs. `PostgresMemoryRepository.sync()` persists all staged changes in
its existing transaction and reloads them as complete audits. No batch wrapper
or aggregate is persisted. Snapshot version 2, JSON Schemas, active-lessons
YAML, `schemas/postgres.sql`, and PostgreSQL schema version 1 remain unchanged.

## Verification

Tests cover mixed one-sided success, order and defensive-copy guarantees,
exact replay, pending/conflict/missing-attribution rollback, invalid input,
same-result and conflicting-result shared Traces, snapshot reconstruction,
PostgreSQL synchronization and reload, and documentation compatibility.
