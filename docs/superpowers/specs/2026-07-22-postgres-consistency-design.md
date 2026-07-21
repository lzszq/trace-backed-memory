# PostgreSQL Consistency Hardening Design

## Summary

`PostgresMemoryRepository.load()` currently reads five tables with separate
statements in a default `READ COMMITTED` transaction. An external writer that
does not participate in the repository's schema-row lock can commit between
those statements, so one load can combine records from different database
states. The sync path already locks Trace and usage-decision rows, but its
failure-case, lesson, and project-policy selectors do not lock. A concurrent
external update can therefore change a protected field after validation and
before the repository's lifecycle update.

Phase 39 closes both consistency gaps without changing the public Python API or
persisted formats.

## Load Snapshot Contract

`load()` keeps its existing repository transaction and schema metadata
`FOR SHARE` lock. Before reading any collection, it acquires `SHARE` table locks
on all five persisted record tables in dependency and sync order:

1. `public.traces`;
2. `public.failure_cases`;
3. `public.lessons`;
4. `public.project_policies`;
5. `public.memory_usage_decisions`.

The locks allow concurrent readers and repository loads but make external
`INSERT`, `UPDATE`, and `DELETE` wait until the load transaction finishes. Once
the combined lock statement succeeds, no uncommitted writer remains on any
collection and no new writer can change one collection between the subsequent
reads. This gives the existing `READ COMMITTED` implementation one coherent
database state without changing a borrowed connection's isolation setting.

The same rule applies inside a caller-owned transaction. The repository still
uses a nested savepoint; PostgreSQL retains successful locks until the outer
transaction commits or rolls back, so the caller continues to own publication.

The repository uses the schema owner or an equivalent write-capable role.
PostgreSQL 12 requires table-level `UPDATE`, `DELETE`, or `TRUNCATE` privilege
for these explicit `SHARE` locks. A role intended only for plain `SELECT`
cannot provide this consistency boundary.

## Sync Row Contract

All existing-record selectors used by `sync()` lock their target row
`FOR UPDATE`. Trace and usage-decision selectors already do this. Phase 39 adds
the same clause to failure cases, lessons, and project policies.

If an external transaction already owns the row, sync waits. After that writer
commits, the locking select evaluates the current row and canonical conflict
checks run against that current version. A protected-field change therefore
raises `PostgresConflictError` instead of allowing a stale validation followed
by a lifecycle write. Exact matches and supported forward transitions retain
their existing behavior.

Every post-select lifecycle update must affect exactly one row. A zero-row or
multi-row result is reported as a conflict rather than as a successful update.

## Errors and Transactions

Schema/version failures remain `PostgresSchemaError`; canonical record
differences remain sanitized `PostgresConflictError`; lock timeouts and other
driver failures remain sanitized `PostgresPersistenceError`. No SQL text,
connection string, or record payload is added to public errors.

Any conflict or driver failure rolls back the repository transaction or nested
savepoint. In a caller transaction, earlier caller work remains owned by that
outer transaction exactly as before.

## Compatibility and Verification

The change adds no table, column, trigger, model field, public method, or
dependency. Snapshot version 2, PostgreSQL schema version 1, JSON Schemas,
active-lessons YAML, and all 18 packaged resources remain byte-compatible.

Real-cluster regression tests verify that:

- a completed load holds all five table `SHARE` locks and blocks an external
  writer until the surrounding transaction ends;
- failure-case, lesson, and project-policy sync wait behind an external row
  writer, then reject the newly committed protected-field conflict;
- the external change remains committed while the repository lifecycle change
  is rolled back;
- existing savepoint, rollback, schema-lock, lifecycle, and round-trip tests
  continue to pass.
