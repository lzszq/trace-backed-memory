# PostgreSQL Runtime Adapter Design

## Summary

The repository already publishes a hardened PostgreSQL schema, but Python
runtime state still persists only through JSON snapshots and the limited active
lesson YAML format. This project adds the first production-facing persistence
adapter without coupling the in-memory store to SQL or making PostgreSQL a core
installation requirement.

The adapter synchronizes validated `TraceBackedMemoryStore` state into the
existing normalized tables, restores a complete store from those tables, and
uses the database lifecycle and provenance triggers as a second line of
validation. It is deliberately synchronous and psycopg-specific.

## Goals

- Persist all five store collections: traces, failure cases, lessons, project
  policies, and usage decisions.
- Restore a complete, validated `TraceBackedMemoryStore` from PostgreSQL.
- Make repeated synchronization idempotent.
- Support the store's documented forward lifecycle updates.
- Detect immutable-record conflicts before accepting divergent state.
- Roll back the entire synchronization if any record conflicts or violates a
  database invariant.
- Keep `import trace_backed_memory` usable when psycopg is not installed.
- Verify behavior against a real temporary PostgreSQL cluster.

## Non-goals

- Running or migrating `schemas/postgres.sql` from the adapter.
- Supporting an already deployed pre-adapter schema without an explicit
  migration.
- Deleting database records that are absent from an in-memory store.
- Async connections, connection pools, retry policy, or failover.
- Supporting psycopg2 or arbitrary Python DB-API drivers.
- Replacing `TraceBackedMemoryStore` with a query-per-operation remote store.
- Vector retrieval, learned ranking, or Git ancestry applicability.

## Alternatives Considered

### 1. Snapshot blob table

Store the existing v2 snapshot as one JSONB value. This would be simple and
preserve exact snapshots, but it would bypass the normalized tables, provenance
foreign keys, lifecycle triggers, runtime ID registry, and usage-log checks.
It would duplicate persistence instead of completing the current SQL design.

### 2. SQL-backed subclass of `TraceBackedMemoryStore`

Override every mutating store method and persist immediately. This creates a
large inheritance surface, makes transaction ownership ambiguous across the
two-phase gate workflow, and couples policy logic to a connection lifecycle.
It also makes loading and testing substantially harder.

### 3. Explicit repository adapter (selected)

Keep the in-memory domain store authoritative during one unit of work and use a
separate repository to synchronize or restore it at explicit transaction
boundaries. This preserves current APIs, keeps connection ownership visible,
and lets the database validate a coherent snapshot atomically.

## Public API

Create `src/trace_backed_memory/postgres.py` with these public types:

```python
@dataclass(frozen=True)
class PostgresSyncCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class PostgresSyncResult:
    traces: PostgresSyncCounts
    failure_cases: PostgresSyncCounts
    lessons: PostgresSyncCounts
    project_policies: PostgresSyncCounts
    usage_logs: PostgresSyncCounts


class PostgresAdapterError(RuntimeError): ...
class PostgresDependencyError(PostgresAdapterError): ...
class PostgresSchemaError(PostgresAdapterError): ...
class PostgresConflictError(PostgresAdapterError): ...
class PostgresPersistenceError(PostgresAdapterError): ...


class PostgresMemoryRepository:
    def __init__(self, connection: object, *, owns_connection: bool = False): ...

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: object) -> "PostgresMemoryRepository": ...

    def sync(self, store: TraceBackedMemoryStore) -> PostgresSyncResult: ...
    def load(self) -> TraceBackedMemoryStore: ...
    def close(self) -> None: ...
    def __enter__(self) -> "PostgresMemoryRepository": ...
    def __exit__(self, exc_type, exc, traceback) -> None: ...
```

`__init__()` borrows a caller-owned connection by default. `connect()` creates
and owns a psycopg connection configured for dictionary rows. Closing a borrowed
repository does not close the connection. Both `sync()` and `load()` use
`connection.transaction()`, so a caller's existing psycopg transaction receives
a nested savepoint instead of an implicit commit.

The package root re-exports these types. The module performs no top-level
psycopg import. `connect()`, `sync()`, and `load()` raise
`PostgresDependencyError` with an installation command when the optional
dependency is unavailable.

## Dependency Packaging

Add these extras to `pyproject.toml`:

```toml
postgres = ["psycopg>=3.2,<4"]
dev = ["pytest>=8.0.0", "psycopg[binary]>=3.2,<4"]
```

Library users can choose the pure Python, C, or binary psycopg distribution.
The development extra uses the binary distribution so the live adapter tests
do not depend on a system `libpq` installation. Python remains `>=3.11`.

## Schema Compatibility

Extend `schemas/postgres.sql` with exactly one metadata row:

```sql
CREATE TABLE trace_backed_memory_schema (
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
  schema_version INTEGER NOT NULL CHECK (schema_version > 0)
);

INSERT INTO trace_backed_memory_schema(singleton, schema_version)
VALUES (true, 1);
```

The database accepts positive future version numbers so a newer migration can
publish its version. This adapter requires `schema_version == 1`. `sync()` locks this row
`FOR UPDATE`; `load()` locks it `FOR SHARE`. These locks serialize adapter loads
with adapter writes and provide a consistent multi-table view without imposing
a process-global lock. Direct SQL writers remain outside this repository
contract and must own equivalent transaction isolation.

Missing metadata, duplicate metadata, or another version raises
`PostgresSchemaError` before domain rows are read or written. The adapter does
not execute DDL because schema installation and migration require deployment
privileges and operational policy that do not belong in a runtime library.

## Synchronization Semantics

`sync(store)` obtains one deterministic v2 snapshot before opening the database
transaction. It processes records in dependency order:

1. traces;
2. failure cases;
3. lessons;
4. project policies;
5. usage decisions.

Every table is processed in sorted primary-key order. The operation returns a
`PostgresSyncResult` with inserted, updated, and unchanged counts for each
collection.

### Immutable records

Traces and usage logs are immutable. A missing ID is inserted. An existing ID
must equal the canonical incoming row after PostgreSQL-to-Python normalization;
otherwise the adapter raises `PostgresConflictError` and the transaction rolls
back.

### Failure-case updates

`case_id`, `source_trace_id`, `commit_sha`, and `created_at` are immutable.
Review, diagnosis, fix, regression, and status fields may advance to the
incoming values. The existing SQL status and lesson-cascade triggers remain
authoritative, so reverse transitions fail atomically.

### Lesson and project-policy updates

All lesson and project-policy fields except `status` and `updated_at` are
immutable. Revisions require a new memory ID. The adapter may update only status,
and the database allows only active-to-obsolete transitions.

### Additive database state

Rows already present in PostgreSQL but absent from the incoming store are left
unchanged. This follows the append-only identity contract and prevents one
partial worker snapshot from deleting another worker's history. A later
`load()` returns all valid database rows.

### Atomic errors

The adapter pre-compares existing rows and uses parameterized statements only.
Expected driver errors are wrapped as `PostgresPersistenceError` with the
operation and record ID but without connection strings or parameter values.
`PostgresConflictError` names the table and primary key. Original psycopg errors
remain available as `__cause__`. Any error rolls back every insert and update in
the synchronization transaction.

## Loading Semantics

`load()` locks the schema metadata row `FOR SHARE`, reads all five tables in
primary-key order with a psycopg `dict_row` cursor, and converts the result into
an exact version 2 snapshot envelope. It then calls
`TraceBackedMemoryStore.from_snapshot()` instead of constructing private store
state directly.

This reuses all existing validation for:

- trace JSON and finite-number limits;
- case/trace provenance;
- lesson/source lifecycle;
- shared runtime memory IDs;
- usage-log trace, context, candidate, and block-reason evidence;
- duplicate identities and deterministic snapshot shape.

PostgreSQL `TIMESTAMPTZ` values become UTC RFC 3339 strings ending in `Z`.
`NUMERIC` confidence values become floats. A `NUMERIC` trace cost becomes an
integer when it is integral and JSON-serializable; otherwise it becomes a finite
float. Invalid driver values raise `PostgresPersistenceError` before snapshot
construction.

## SQL Mapping

Mapping code is private and table-specific. Each table has:

- one ordered SELECT column list;
- one domain-to-parameter encoder;
- one database-row-to-snapshot decoder;
- an immutable-field comparison set;
- a narrow mutable update statement where supported.

JSONB parameters use `psycopg.types.json.Jsonb`. SQL identifiers are static;
only values use `%s` parameters. The adapter always references `public.*` and
does not accept a caller-provided schema name.

## Connection and Threading Contract

The repository is synchronous and is no more thread-safe than the supplied
psycopg connection. Callers must not use one repository concurrently from
multiple threads. A repository created by `connect()` owns and closes its
connection. A repository created around an existing connection borrows it and
leaves lifecycle management to the caller.

`close()` is idempotent. Calling `sync()` or `load()` after closing an owned
repository raises `PostgresAdapterError`. The adapter never logs a DSN.

## Testing

### Unit tests

- Package import works without importing psycopg eagerly.
- Missing optional dependency produces a stable installation error.
- Connection ownership and idempotent close behavior are explicit.
- Timestamp, decimal, JSONB, and snapshot conversion helpers cover edge cases.
- Canonical row comparison distinguishes mutable lifecycle updates from
  immutable conflicts.

### Live PostgreSQL tests

Refactor the existing temporary-cluster utility into shared test support, then
run these against a fresh cluster with psycopg:

- empty database round trip for all five collections;
- second synchronization reports every row unchanged;
- draft review/verification and obsoletion synchronize forward;
- parent obsoletion cascades lessons and policy obsoletion persists;
- immutable trace, lesson, policy, and usage-log conflicts roll back atomically;
- an extra database record is preserved and appears after load;
- schema metadata missing or version mismatch fails before writes;
- driver failure does not leave a partial synchronization;
- borrowed and owned connection lifecycle behavior.

The existing psql integration tests continue to validate raw DDL invariants.
The complete Python suite must run with the PostgreSQL adapter tests executing,
not skipped, in the local development environment.

## Documentation

Update README and architecture documentation with:

- installation through `pip install 'trace-backed-memory[postgres]'`;
- a short `PostgresMemoryRepository.connect()` / `sync()` / `load()` example;
- explicit schema installation and version requirements;
- additive synchronization and immutable conflict semantics;
- the distinction between the in-memory workflow and persistence boundaries.

Move the PostgreSQL runtime adapter out of the documented non-goals while
keeping async, pooling, migration, and remote query-per-operation behavior out
of scope.
