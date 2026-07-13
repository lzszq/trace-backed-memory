# Memory Run Audit Implementation Plan

## Goal

Expose every trace-linked memory decision's completion consistency without
adding persisted state or weakening existing validation.

## Model And Store

- Add and export frozen `MemoryRunAudit`.
- Add `TraceBackedMemoryStore.memory_run_audits()`.
- Classify measured and unevaluated Trace/decision pairs into the five specified
  states.
- Return one decision-ID-sorted immutable record per usage log under the store
  lock.
- Keep traces without decisions outside this decision-oriented view.

## Persistence

- Prove JSON snapshot round trips reproduce the same derived audits.
- Prove PostgreSQL load reproduces the same derived audits.
- Keep snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1 unchanged.

## Documentation

- Add the audit/recovery workflow to README, architecture, and usage policy.
- Add an implemented Phase 18 roadmap section.
- Explain why `conflict` is observable but never auto-repaired.

## Verification

- Run focused model, store, persistence, README, and documentation tests.
- Run the complete suite and bytecode compilation.
- Confirm SQL hash, conflict-marker, PostgreSQL cleanup, and clean-worktree
  invariants.
- Review, merge to `main`, push, and verify the exact remote SHA.
