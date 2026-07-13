# Memory Run Metrics Implementation Plan

## Goal

Provide a stable operational summary of pending, partial, complete, and
conflicting memory runs without persisting redundant aggregate state.

## Model And Store API

- Add and export frozen `MemoryRunMetrics`.
- Add `TraceBackedMemoryStore.memory_run_metrics()`.
- Reuse `memory_run_audits()` as the classification source of truth.
- Count one row per usage decision, including separate rows for decisions that
  share a Trace.
- Enforce the five-state sum and one-sided recoverability invariants by
  construction.
- Keep existing global and per-memory metrics unchanged.

## Persistence

- Prove snapshot round trips reproduce the same derived value.
- Prove PostgreSQL synchronization and load reproduce the same derived value.
- Keep snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1 unchanged.

## Documentation

- Add an executable README example and operational interpretation.
- Update architecture, usage policy, and the roadmap with Phase 20.
- State the decision-oriented counting unit, sum invariant, recoverable states,
  and non-persistence boundary.

## Verification

- Run focused model, store, PostgreSQL, README, and documentation tests.
- Run the full suite and bytecode compilation.
- Confirm SQL hash, conflict markers, PostgreSQL cleanup, and worktree state.
- Review, synchronize with the remote, merge to `main`, push, and verify the
  exact remote SHA.
