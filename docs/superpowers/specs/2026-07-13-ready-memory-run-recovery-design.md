# Atomic Ready Memory Run Recovery Design

## Problem

`memory_run_remediations()` identifies decisions whose current action is
`recover`, and `recover_memory_runs()` can apply a caller-selected batch
atomically. A worker that combines them still has a read/write race: state may
change after it reads the plan but before it submits the selected IDs. The
write API will reject stale input safely, but callers must retry boilerplate
that the store can perform under one lock.

The store should provide a recovery sweep that selects every currently ready
decision and applies that exact set without releasing the lock. It must not
infer failed-run attribution, hide conflicts, replay already complete work, or
weaken the shared Trace checks in batch recovery.

## Public API

Add:

```python
completions = store.recover_ready_memory_runs()
```

The method accepts no decision IDs, outcomes, attribution mapping, or Trace
evidence. It returns a tuple of defensive `MemoryRunCompletion` values sorted
by `decision_id`. An empty store or a store with no ready decisions returns an
empty tuple without mutation.

## Eligibility

The sweep derives `MemoryRunRemediation` values at method entry and selects
only action `recover`:

- a passing `trace_only` decision is ready because false failure attribution
  is established;
- every `decision_only` decision is ready because its result and attribution
  are already sealed;
- `pending`, failed or errored `trace_only`, `conflict`, and `complete`
  decisions are skipped.

Skipped records remain visible as `measure`, `recover_with_attribution`,
`investigate`, or `none`. Callers must use explicit `recover_memory_run()` or
`recover_memory_runs()` with an attribution mapping for failed or errored
Trace-only records.

## Atomicity And Shared Traces

`recover_ready_memory_runs()` holds the store's reentrant lock while deriving
the plan, selecting IDs, and delegating to `recover_memory_runs()`. Batch
recovery reclassifies those IDs from the same locked state and builds all Trace
candidates, sealed decision candidates, and defensive returns before assigning
anything.

Per-decision readiness does not guarantee that all ready decisions sharing one
Trace agree. If two `decision_only` records resolve to different outcomes,
existing shared-Trace validation rejects the entire selected sweep before
mutation. Matching shared outcomes recover together. Any injected later
candidate failure likewise rolls the whole sweep back.

Concurrent sweeps serialize. After one succeeds, a second caller re-plans and
returns an empty tuple for those now-complete decisions rather than replaying
them. Exact repeated scans are therefore idempotent at the sweep boundary.

## Persistence Boundary

The method changes only existing Trace completion and usage outcome fields.
The selection, skipped states, and return tuple are not persisted. PostgreSQL
sync uses its existing transaction and forward-update rules. Snapshot version
2, JSON Schemas, active-lessons YAML, `schemas/postgres.sql`, and PostgreSQL
schema version 1 remain unchanged.

## Verification

Tests cover empty and no-ready scans, mixed action selection, deterministic
order, exact second-scan idempotence, matching and conflicting shared Traces,
later-candidate rollback, concurrent sweeps, snapshot reconstruction,
PostgreSQL synchronization and reload, executable README usage, documentation
compatibility, and unchanged schemas.
