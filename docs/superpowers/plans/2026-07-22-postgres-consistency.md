# PostgreSQL Consistency Hardening Plan

## Scope

Make multi-table loads coherent in the presence of external writers and make
all existing lifecycle-row validation operate on a locked current row.

## Steps

1. Add one ordered `SHARE` table-lock statement and execute it after schema
   validation but before the first collection read.
2. Add `FOR UPDATE` to failure-case, lesson, and project-policy ID selectors.
3. Require exactly one affected row from every post-select lifecycle update.
4. Add real PostgreSQL concurrency tests for load writer exclusion and stale
   protected-field conflicts across all three lifecycle tables.
5. Document the load and sync locking guarantees in README, architecture,
   usage policy, product status, and the Phase 39 roadmap entry.
6. Run focused and full tests, distribution verification, compatibility checks,
   whole-branch review, and required remote PostgreSQL CI.

## Compatibility

- No public Python signature or return type changes.
- No snapshot, model, JSON Schema, SQL schema, or packaged-resource changes.
- Snapshot version remains 2 and PostgreSQL schema version remains 1.
- Borrowed/owned connection and nested-savepoint ownership remain unchanged.
- External writers may wait while `load()` holds its short-lived coherent
  snapshot boundary; concurrent readers remain allowed.

