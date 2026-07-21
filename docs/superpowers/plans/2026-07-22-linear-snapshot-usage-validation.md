# Linear Snapshot Usage-Log Validation Plan

## Scope

Remove multiplicative and quadratic scans from snapshot usage-log loading
without changing accepted data, failure precedence, or persistence formats.

## Steps

1. Add deterministic complexity-regression tests for unique decision IDs,
   known-memory reuse, legacy run lookup, trace tool-name reuse, and
   candidate/used/blocked membership.
2. Add load-local decision, memory, legacy run, and trace-tool indexes.
3. Replace repeated list relationship membership with per-log sets while
   preserving reported ID order.
4. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 51.
5. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Preserve validation and duplicate-error precedence, exact error messages,
  input processing order, canonical output ordering, and load atomicity.
- Preserve public signatures, dependencies, snapshot version 2, every JSON
  Schema, active-lessons YAML, all packaged resources, PostgreSQL DDL, and
  PostgreSQL schema version 1.
