# Atomic Batch Memory Run Completion Implementation Plan

## Goal

Complete multiple newly evaluated memory runs with per-run Trace evidence as
one all-or-nothing store operation while deriving linkage from decision IDs.

## Model And API

- Add and export `MeasuredEvalResult` and frozen `MemoryRunResult`.
- Represent omitted evidence with `None` and optional tool outputs with a tuple.
- Add `TraceBackedMemoryStore.complete_memory_runs()` for a non-empty exact
  result tuple with unique decision IDs.
- Derive Trace linkage from each validated usage decision and preserve request
  order in defensive completion results.
- Require shared-Trace results to agree and merge only compatible evidence.
- Preserve pending, partial recovery, exact replay, attribution, and immutable
  evidence behavior from `complete_memory_run()`.

## Internal Staging

- Extract a non-mutating batch staging helper for resolved result rows.
- Normalize each request against the original Trace before evidence merging.
- Build every final Trace, usage log, and defensive return before assignment.
- Reuse the staging helper from `recover_memory_runs()` without changing its
  eligibility or result-derivation contract.
- Keep all lookup and staging work linear in usage-log and request counts.

## Persistence And Documentation

- Prove two pending runs synchronize as two Trace and two usage forward updates
  and reload as complete through PostgreSQL.
- Keep `MemoryRunResult` out of snapshots, schemas, YAML, and SQL.
- Add an executable README example and update architecture, usage policy,
  implemented API inventory, and the roadmap with Phase 22.
- Preserve snapshot version 2 and PostgreSQL schema version 1.

## Verification

- Run focused model, store, recovery regression, PostgreSQL, README, and docs
  tests.
- Run the full suite and bytecode compilation.
- Confirm SQL hash, conflict markers, PostgreSQL cleanup, and worktree state.
- Review, synchronize with the remote, merge to `main`, push, and verify the
  exact remote SHA.
