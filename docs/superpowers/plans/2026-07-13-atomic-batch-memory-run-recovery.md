# Atomic Batch Memory Run Recovery Implementation Plan

## Goal

Recover multiple already eligible one-sided memory runs as one all-or-nothing
store operation without allowing callers to repeat correlated Trace IDs or
measured results.

## Store API

- Add `TraceBackedMemoryStore.recover_memory_runs()` for a non-empty unique
  decision-ID tuple and optional per-decision failure-attribution mapping.
- Preserve request order in the returned completion tuple.
- Extract and reuse one recovery-state resolver for single and batch APIs.
- Classify every request from entry state and reject pending or conflict.
- Require explicit attribution for every failed or errored Trace-only item.
- Group decisions by Trace and reject inconsistent derived outcomes.
- Build and validate every candidate and defensive result before any assignment.
- Keep exact replay idempotent and single recovery behavior unchanged.

## Persistence

- Prove the batch synchronizes and reloads through existing PostgreSQL
  transactions and forward-update rules.
- Prove snapshot round trips reproduce completed audit and metric state.
- Keep snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1 unchanged.

## Documentation

- Add an executable README batch example and explain when individual recovery
  remains necessary.
- Update architecture, usage policy, implemented API inventory, and the roadmap
  with Phase 21.
- Publish eligibility, shared-Trace consistency, attribution, ordering,
  all-or-nothing, and non-persistence contracts.

## Verification

- Run focused store, PostgreSQL, README, and documentation tests.
- Run the full suite and bytecode compilation.
- Confirm SQL hash, conflict markers, PostgreSQL cleanup, and worktree state.
- Review, synchronize with the remote, merge to `main`, push, and verify the
  exact remote SHA.
