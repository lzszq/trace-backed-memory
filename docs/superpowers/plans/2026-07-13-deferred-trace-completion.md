# Deferred Trace Completion Implementation Plan

## Goal

Allow a Trace registered before memory execution to receive its measured
execution evidence afterward without making provenance or completed evidence
mutable.

## Store Contract

- Add `TraceBackedMemoryStore.complete_trace()`.
- Accept only `pass`, `fail`, and `error` as completion results.
- Fill only the seven declared completion fields.
- Preserve omitted and already equal evidence; reject populated-field rewrites.
- Make exact replay idempotent and every failure atomic.
- Reuse Trace validation and defensive-copy guarantees.
- Prove the full Trace -> memory decision -> execution completion -> decision
  outcome workflow.

## PostgreSQL Contract

- Add a dedicated Trace synchronization helper and row lock.
- Update only the declared completion columns from an `unknown` row.
- Keep all provenance, input, context, call, and creation evidence immutable.
- Reject stale, conflicting, and reverse transitions.
- Preserve transaction rollback when a later row conflicts.
- Keep `schemas/postgres.sql` and schema version 1 unchanged.

## Documentation Contract

- Publish the chronological end-to-end workflow in README.
- Document the completion boundary in architecture and usage policy.
- Add an implemented Phase 16 roadmap section.
- State that Trace completion and decision-outcome sealing remain distinct
  audit operations.

## Verification

- Run focused store, PostgreSQL, README, and documentation tests.
- Run the full suite and bytecode compilation.
- Confirm the SQL schema hash remains unchanged and no PostgreSQL runtime
  resources leak.
- Review the branch, merge to `main`, push, and verify the exact remote SHA.
