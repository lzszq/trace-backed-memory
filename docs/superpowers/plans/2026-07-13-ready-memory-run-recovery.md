# Atomic Ready Memory Run Recovery Implementation Plan

## Goal

Select and recover every currently automatic memory-run remediation under one
store lock, without guessing through attribution or outcome conflicts.

## Store API

- Add no-argument `recover_ready_memory_runs()`.
- Derive the current remediation plan and select only action `recover` in
  decision order.
- Return an empty tuple when no decision is ready.
- Delegate the selected tuple to `recover_memory_runs()` while retaining the
  reentrant store lock.

## Safety

- Skip pending, attribution-required, conflicting, and complete decisions.
- Preserve shared Trace outcome validation and all-or-nothing candidate
  staging.
- Prove concurrent calls serialize and a second successful scan is empty.
- Keep explicit attribution on the existing recovery APIs only.

## Persistence And Documentation

- Prove existing PostgreSQL rows synchronize and reload after a sweep.
- Keep snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1 unchanged.
- Update README, architecture, usage policy, implemented API inventory, and
  roadmap Phase 24.

## Verification

- Run focused store, README, documentation, and PostgreSQL tests.
- Run the complete test suite and compile source plus tests.
- Check the diff, schema hash, conflict markers, process cleanup, and worktree
  state before merge and push.
