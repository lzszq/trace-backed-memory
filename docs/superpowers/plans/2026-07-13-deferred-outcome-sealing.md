# Deferred Decision Outcome Sealing Implementation Plan

## Goal

Support the real decision-then-evaluation workflow without permitting audit
log rewrites.

## Store Contract

- Add `TraceBackedMemoryStore.record_decision_outcome()`.
- Accept only `pass`, `fail`, and `error` as sealable outcomes.
- Allow one transition from `None` or `unknown` to a valid outcome pair.
- Make exact replay idempotent and reject conflicting replay.
- Validate before replacement and return defensive copies.
- Prove metrics and snapshots observe the sealed result.

## PostgreSQL Contract

- Add a dedicated usage-log synchronization helper.
- Keep all non-outcome usage fields immutable.
- Allow only the same forward outcome transition as the store.
- Report the transition through `PostgresSyncCounts.updated`.
- Preserve transaction rollback on later conflicts.
- Keep schema version 1 and `schemas/postgres.sql` unchanged.

## Documentation Contract

- Update the executable README workflow to finalize before evaluation and seal
  the result afterward.
- Document lifecycle, idempotence, conflict, metrics, and persistence behavior
  in README, architecture, and usage policy.
- Add an implemented Phase 15 roadmap section.

## Verification

- Run focused store, PostgreSQL repository, README, and documentation tests.
- Run the full test suite and bytecode compilation.
- Confirm the SQL schema hash is unchanged and no PostgreSQL process or data
  marker leaks remain.
- Review the complete branch diff, merge to `main`, push, and verify the exact
  remote SHA.
