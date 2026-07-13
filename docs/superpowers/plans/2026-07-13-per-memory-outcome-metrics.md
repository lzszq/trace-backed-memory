# Per-memory Outcome Metrics Implementation Plan

**Goal:** Derive stable per-memory usage/outcome summaries from existing audit
logs without adding persistence or claiming causality.

**Constraints:** Add no dependency; keep existing APIs unchanged; include all
stored memory IDs; use validated usage logs only; preserve snapshot version 2,
JSON Schemas, YAML, and PostgreSQL schema version 1.

## Task 1: Public contract tests

- Add/export frozen `MemoryOutcomeMetrics` contract expectations.
- Build failure-case, lesson, and policy memory with a zero-observation control.
- Record pass, fail, error, unknown, and missing outcomes with both single- and
  multi-memory decisions.
- Assert sorted tuple output and all field invariants.
- Run focused tests and capture RED.

## Task 2: Aggregation

- Aggregate candidate, used, and blocked IDs once per validated usage log.
- Classify outcomes only for used IDs using the Phase 12 evaluated-outcome
  boundary.
- Return immutable records for all known IDs, with `None` rates for zero
  evaluated uses.
- Keep `metrics()` unchanged and run full store tests.

## Task 3: Reconstruction and docs

- Prove snapshot v2 and legacy usage-log migration reconstruct identical
  per-memory metrics.
- Add an executable README example and document association versus causality,
  blocked-count semantics, invariants, and non-persistence.
- Add `Phase 13: Per-memory outcome metrics (implemented)`.

## Task 4: Delivery

- Run focused/full tests, compileall, diff checks, SQL hash, and PostgreSQL
  cleanup checks.
- Independently review the whole branch and resolve every finding.
- Fetch/rebase if needed, fast-forward `main`, rerun full tests, push, verify
  the remote SHA, and remove the feature worktree/branch.
