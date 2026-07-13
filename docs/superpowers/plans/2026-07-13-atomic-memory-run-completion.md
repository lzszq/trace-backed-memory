# Atomic Memory Run Completion Implementation Plan

## Goal

Make the normal post-execution Trace and decision updates one atomic,
consistent store operation.

## Model And Store

- Add and export frozen `MemoryRunCompletion`.
- Add `TraceBackedMemoryStore.complete_memory_run()`.
- Require exact decision-to-Trace linkage.
- Apply one measured result to both records.
- Reuse Trace completion and decision sealing through pure candidate builders.
- Assign neither candidate until both validate.
- Support exact replay and same-result partial recovery.
- Return defensive copies.

## PostgreSQL

- Reuse the existing Trace and usage forward-update paths.
- Prove both updates occur in one sync and are counted independently.
- Prove a later usage conflict rolls an earlier Trace update back.
- Keep the SQL schema and schema version unchanged.

## Documentation

- Make `complete_memory_run()` the preferred README post-execution path.
- Retain the two low-level methods as explicit advanced/recovery operations.
- Document linkage, consistency, recovery, and persistence behavior.
- Add an implemented Phase 17 roadmap section.

## Verification

- Run focused store, model/export, PostgreSQL, README, and documentation tests.
- Run the full suite and bytecode compilation.
- Confirm SQL hash and PostgreSQL cleanup invariants.
- Review, merge to `main`, push, and verify the exact remote SHA.
