# Memory Run Recovery Implementation Plan

## Goal

Recover valid one-sided memory runs without asking callers to repeat correlated
Trace IDs or measured outcomes and without guessing failure attribution.

## Store API

- Add `TraceBackedMemoryStore.recover_memory_run()` keyed by decision ID.
- Reuse the five-state audit classifier.
- Derive the measured result only from the completed side.
- Require explicit attribution for failed or errored `trace_only` records.
- Preserve sealed decision attribution for `decision_only` and `complete`.
- Delegate all writes and replay checks to `complete_memory_run()` under the
  same reentrant store lock.

## Persistence

- Prove a recovered one-sided run synchronizes through existing PostgreSQL
  forward-update rules and reloads as `complete`.
- Keep snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1 unchanged.

## Documentation

- Add an executable recovery example and state-specific guidance.
- Update architecture, usage policy, and the roadmap with Phase 19.
- State that pending results, outcome conflicts, and failed-run attribution are
  never guessed.

## Verification

- Run focused recovery, audit, completion, PostgreSQL, README, and docs tests.
- Run the full suite and bytecode compilation.
- Confirm SQL hash, conflict markers, PostgreSQL cleanup, and worktree state.
- Review, merge to `main`, push, and verify the exact remote SHA.
