# Indexed Usage-Log Operations Plan

## Scope

Remove cumulative quadratic decision-ID allocation and repeated history scans
from in-memory usage-log operations without changing persisted data.

## Steps

1. Add deterministic scan-count tests for repeated decision creation and
   single-ID lookup, plus sparse/non-numeric import and failed-write numbering
   tests.
2. Add the private decision-ID index and next numeric suffix state.
3. Route snapshot import, finalization, and direct decision logging through one
   append helper; reuse the index for single and batch lookups.
4. Add a concurrent multi-write uniqueness regression while retaining existing
   completion/recovery atomicity coverage.
5. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 52.
6. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Preserve max-numeric-suffix allocation, sparse and nonnumeric import
  behavior, duplicate validation precedence, exact errors, and canonical
  ordering.
- Preserve public signatures, dependencies, snapshot version 2, every JSON
  Schema, active-lessons YAML, all packaged resources, PostgreSQL DDL, and
  PostgreSQL schema version 1.
