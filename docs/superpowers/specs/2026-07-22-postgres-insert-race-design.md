# PostgreSQL Concurrent Insert Revalidation Design

## Summary

`PostgresMemoryRepository.sync()` locks rows that already exist, but PostgreSQL
`SELECT ... FOR UPDATE` cannot lock a missing primary key. An external writer
can therefore insert the same ID after the repository observes no row and
before its plain `INSERT`. If the external transaction commits, the repository
currently receives `unique_violation` and reports a generic persistence error
instead of applying its canonical replay/conflict rules.

Phase 42 recovers only this absent-row race. It keeps the existing table order,
row locks, lifecycle rules, triggers, and error boundaries.

## Why Not ON CONFLICT

`ON CONFLICT (<primary key>) DO NOTHING` would work for traces and usage
decisions, but not uniformly for runtime memory tables. Failure cases, lessons,
and project policies have `BEFORE INSERT` triggers that first register a global
memory ID. PostgreSQL runs those triggers before conflict arbitration, so a
same-row replay can raise the registry's custom `unique_violation` before
`DO NOTHING` sees the source-table primary key.

Changing those security-definer triggers or the registry is outside this
runtime adapter fix and would require a separate DDL/version decision.

## Narrow Savepoint Recovery

After an initial ID selector returns no rows, a shared private helper:

1. opens a nested `connection.transaction()` savepoint;
2. executes the existing plain `INSERT` with all existing triggers intact;
3. returns `inserted` when it succeeds;
4. catches only `psycopg.errors.UniqueViolation` after the savepoint has rolled
   back the failed statement and all trigger side effects;
5. reruns the same primary-key selector `FOR UPDATE` in a fresh READ COMMITTED
   command;
6. re-raises the original unique violation when the target row is still absent;
7. otherwise returns the locked row to the existing canonical comparison.

Every non-unique driver error bypasses recovery. A cross-kind memory registry
collision leaves the target table row absent and therefore remains a sanitized
`PostgresPersistenceError`, not an immutable row conflict.

## Canonical Outcomes

The reselected row follows the same code as any row present at the first
selector:

- an exact canonical replay is `unchanged`;
- a supported Trace, failure-case, status, or usage-outcome transition may be
  `updated`;
- a protected-field or unsupported lifecycle difference raises
  `PostgresConflictError` and rolls back the whole repository sync;
- malformed multiplicity remains a conflict.

No concurrent value is overwritten merely because it arrived during the
absent-row window.

## Transactions and Locks

The helper savepoint is inside the existing repository transaction, or inside
the repository savepoint when a caller already owns an outer transaction. A
failed INSERT is rolled back before the reselect, so the cursor and caller
connection remain usable. The reselected row lock follows the existing rule:
inside a caller transaction it remains held until the caller commits or rolls
back.

An uncommitted external insert may make the repository INSERT wait on a unique
index or trigger-owned registry row. If the external transaction rolls back,
the repository insert succeeds. If it commits, revalidation observes the new
row at READ COMMITTED.

## Tests

Real-cluster tests cover all five persisted record kinds, including the three
runtime memory registration triggers. They hold a direct external insert
uncommitted, verify the repository/helper is waiting on a lock, commit it, and
then assert exact replay or protected-field conflict. A repository-level Trace
test additionally verifies contextual conflict wrapping, full rollback, row
preservation, and connection reuse.

Tests also prove that a unique violation with no target row is not converted to
`PostgresConflictError`.

## Compatibility

No public API, model, SQL statement shape outside runtime savepoints, DDL,
dependency, or resource changes. Snapshot version 2, every JSON Schema,
active-lessons YAML, all 18 packaged resources, and PostgreSQL schema version 1
remain unchanged. The only new behavior is correct canonical classification of
a same-primary-key insert committed during sync.

