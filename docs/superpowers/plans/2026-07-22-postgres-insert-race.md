# PostgreSQL Concurrent Insert Revalidation Plan

## Scope

Recover a same-primary-key external INSERT committed between an absent
repository selector and its INSERT, then apply existing canonical sync rules.

## Steps

1. Add real-cluster failing tests for exact and protected-conflict concurrent
   inserts, covering traces, failure cases, lessons, project policies, and
   usage decisions.
2. Add a repository-level race test for contextual errors, whole-sync rollback,
   external row preservation, and connection reuse.
3. Introduce one private insert/savepoint/reselect helper that catches only
   `psycopg.errors.UniqueViolation`.
4. Reuse the helper in all four row-sync implementations, including the generic
   lesson/project-policy status path.
5. Prove a unique registry collision without a target row remains a persistence
   error.
6. Publish Phase 42 behavior in README, architecture, usage policy, product,
   roadmap, and contract tests.
7. Run focused and full tests, build/verify wheel and sdist, independently
   review transaction/error semantics, merge, push, and require all CI jobs.

## Compatibility

- Exact concurrent replays become `unchanged`; existing forward transitions
  remain `updated`; protected differences remain conflicts.
- Non-unique errors and target-absent unique errors keep persistence semantics.
- Public signatures, DDL, snapshot version 2, PostgreSQL schema version 1,
  JSON Schemas, YAML, dependencies, and the 18-resource inventory do not
  change.

