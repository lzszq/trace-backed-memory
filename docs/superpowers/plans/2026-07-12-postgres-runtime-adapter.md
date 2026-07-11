# PostgreSQL Runtime Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a synchronous, optional psycopg repository that atomically synchronizes and restores complete `TraceBackedMemoryStore` state through the hardened PostgreSQL schema.

**Architecture:** Keep the in-memory domain store unchanged and add `PostgresMemoryRepository` as an explicit persistence boundary. The repository converts one deterministic v2 snapshot into normalized SQL rows, serializes adapter operations through a schema metadata row, detects immutable conflicts, applies only documented forward lifecycle updates, and reconstructs stores exclusively through `TraceBackedMemoryStore.from_snapshot()`.

**Tech Stack:** Python 3.11+, psycopg 3.2+, PostgreSQL 10+, pytest, existing dependency-free domain/store modules.

## Global Constraints

- `import trace_backed_memory` must not import psycopg eagerly.
- The core project dependency list remains empty; psycopg lives in `postgres` and `dev` extras.
- The adapter supports only the fresh-install `public` schema at adapter schema version `1`.
- Every synchronization is additive and transactional; no adapter operation deletes rows.
- Traces and usage logs are immutable; failure cases have narrow mutable lifecycle fields; lessons and policies may update only `status`.
- SQL identifiers are static and schema-qualified; every runtime value uses psycopg parameters.
- Tests must execute against a real temporary PostgreSQL cluster on this machine.
- Use TDD for every new behavior and commit each task independently.

---

### Task 1: Shared PostgreSQL Test Runtime And Optional Dependency

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/postgres_support.py`
- Create: `tests/conftest.py`
- Modify: `tests/test_postgres_integration.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `PostgresCluster`, `postgres_cluster`, `assert_sql_succeeds()`, and `assert_sql_fails()` reusable by adapter integration tests.
- Produces: optional extras `postgres` and `dev`, both resolving psycopg 3 without making it a core dependency.

- [ ] **Step 1: Record the unchanged integration baseline**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py -q
```

Expected: `6 passed`.

- [ ] **Step 2: Add the dependency contract test**

Add to `tests/test_examples_and_schema.py`:

```python
def test_postgres_adapter_dependencies_are_optional():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    extras = project["optional-dependencies"]

    assert project["dependencies"] == []
    assert extras["postgres"] == ["psycopg>=3.2,<4"]
    assert "psycopg[binary]>=3.2,<4" in extras["dev"]
```

Run:

```powershell
python -m pytest tests/test_examples_and_schema.py::test_postgres_adapter_dependencies_are_optional -q
```

Expected: FAIL because the `postgres` extra is absent.

- [ ] **Step 3: Add the optional dependency declarations**

Change `pyproject.toml` to:

```toml
[project.optional-dependencies]
postgres = ["psycopg>=3.2,<4"]
dev = ["pytest>=8.0.0", "psycopg[binary]>=3.2,<4"]
```

Install the development extra:

```powershell
python -m pip install -e ".[dev]"
```

Expected: psycopg 3 and the editable project install successfully.

- [ ] **Step 4: Extract reusable PostgreSQL process management**

Move the process-safe cluster code from `tests/test_postgres_integration.py`
into `tests/postgres_support.py` with these public signatures:

```python
@dataclass
class TrackedClient:
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path


@dataclass
class PostgresCluster:
    psql: str
    env: dict[str, str]
    root: Path
    clients: list[TrackedClient] = field(default_factory=list)

    def connection_kwargs(self) -> dict[str, str]: ...
    def run(self, sql: str, *, timeout: float = 15.0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...
    def load_schema(self, *, sql: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...
    def spawn(self, name: str, sql: str) -> TrackedClient: ...
    def terminate_clients(self) -> list[BaseException]: ...


@pytest.fixture
def postgres_cluster(tmp_path: Path) -> Iterator[PostgresCluster]: ...


def assert_sql_succeeds(cluster: PostgresCluster, sql: str) -> str: ...
def assert_sql_fails(cluster: PostgresCluster, sql: str, message: str) -> None: ...
```

Keep the existing bounded startup, client, shutdown, and directory cleanup
semantics exactly. `tests/conftest.py` imports and re-exports the fixture:

```python
from tests.postgres_support import postgres_cluster

__all__ = ["postgres_cluster"]
```

Update `tests/test_postgres_integration.py` to import shared helpers instead of
defining them. Do not change its SQL assertions.

- [ ] **Step 5: Verify the refactor and dependency setup**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py tests/test_examples_and_schema.py -q
```

Expected: all tests pass and all six integration tests execute.

- [ ] **Step 6: Commit Task 1**

```powershell
git add pyproject.toml tests/__init__.py tests/postgres_support.py tests/conftest.py tests/test_postgres_integration.py tests/test_examples_and_schema.py
git commit -m "test: share postgres integration runtime"
```

---

### Task 2: Versioned Schema And Connection Boundary

**Files:**
- Create: `src/trace_backed_memory/postgres.py`
- Create: `tests/test_postgres_repository.py`
- Modify: `schemas/postgres.sql`
- Modify: `src/trace_backed_memory/__init__.py`
- Modify: `tests/test_examples_and_schema.py`

**Interfaces:**
- Produces: `PostgresMemoryRepository`, `PostgresSyncCounts`, `PostgresSyncResult`, and the five adapter exception classes.
- Produces: `public.trace_backed_memory_schema(singleton, schema_version)` with version `1`.
- Consumes: the shared `postgres_cluster` fixture from Task 1.

- [ ] **Step 1: Write failing schema metadata tests**

Add static and live tests:

```python
def test_postgres_schema_publishes_adapter_version():
    schema = _postgres_schema()
    assert "CREATE TABLE trace_backed_memory_schema" in schema
    assert "schema_version INTEGER NOT NULL CHECK (schema_version > 0)" in schema
    assert "VALUES (true, 1)" in schema


def test_repository_rejects_missing_or_unknown_schema(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSchemaError

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        with pytest.raises(PostgresSchemaError, match="schema metadata"):
            repository.load()

        postgres_cluster.load_schema()
        connection.execute(
            "UPDATE public.trace_backed_memory_schema SET schema_version = 2"
        )
        connection.commit()
        with pytest.raises(PostgresSchemaError, match="expected 1, found 2"):
            repository.load()
```

Run both tests. Expected: FAIL because neither metadata nor repository exists.

- [ ] **Step 2: Add adapter schema metadata**

Inside the existing DDL transaction, before the domain tables, add:

```sql
CREATE TABLE trace_backed_memory_schema (
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
  schema_version INTEGER NOT NULL CHECK (schema_version > 0)
);

INSERT INTO trace_backed_memory_schema(singleton, schema_version)
VALUES (true, 1);

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_schema FROM PUBLIC;
```

Update raw DDL integration assertions to include exactly one version row.

- [ ] **Step 3: Add lazy dependency loading and public types**

Create `postgres.py` with no top-level psycopg import:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import TraceBackedMemoryStore

POSTGRES_SCHEMA_VERSION = 1


class PostgresAdapterError(RuntimeError):
    pass


class PostgresDependencyError(PostgresAdapterError):
    pass


class PostgresSchemaError(PostgresAdapterError):
    pass


class PostgresConflictError(PostgresAdapterError):
    pass


class PostgresPersistenceError(PostgresAdapterError):
    pass


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


def _load_psycopg() -> tuple[Any, Any, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise PostgresDependencyError(
            "PostgreSQL support requires: pip install 'trace-backed-memory[postgres]'"
        ) from exc
    return psycopg, dict_row, Jsonb
```

- [ ] **Step 4: Implement connection ownership and schema locking**

Add:

```python
class PostgresMemoryRepository:
    def __init__(self, connection: object, *, owns_connection: bool = False) -> None:
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._closed = False

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: object) -> "PostgresMemoryRepository":
        psycopg, dict_row, _Jsonb = _load_psycopg()
        connection = psycopg.connect(conninfo, row_factory=dict_row, **kwargs)
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresAdapterError("PostgreSQL repository is closed")

    def _lock_schema(self, cursor: object, *, write: bool) -> None:
        lock = "UPDATE" if write else "SHARE"
        cursor.execute(
            f"SELECT schema_version FROM public.trace_backed_memory_schema "
            f"WHERE singleton FOR {lock}"
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise PostgresSchemaError("PostgreSQL schema metadata must contain exactly one row")
        version = rows[0]["schema_version"]
        if version != POSTGRES_SCHEMA_VERSION:
            raise PostgresSchemaError(
                f"PostgreSQL schema version mismatch: expected 1, found {version}"
            )
```

Convert missing-table driver errors to `PostgresSchemaError` without exposing
DSNs. Add an initial `load()` implementation that opens a dictionary-row cursor,
locks/validates schema metadata, and returns an empty `TraceBackedMemoryStore`;
Task 3 replaces the empty result with table loading. Implement idempotent
`close()`, `__enter__()`, and `__exit__()`; only an owned connection is closed.

- [ ] **Step 5: Add dependency and ownership tests**

Test package imports without `sys.modules["psycopg"]`, stable missing-extra
errors via an import hook, borrowed connections remaining open, owned
connections closing exactly once, and operations after close failing.

- [ ] **Step 6: Export and verify Task 2**

Re-export all public adapter types from `src/trace_backed_memory/__init__.py`.
Run:

```powershell
python -m pytest tests/test_postgres_repository.py tests/test_examples_and_schema.py tests/test_postgres_integration.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add src/trace_backed_memory/postgres.py src/trace_backed_memory/__init__.py schemas/postgres.sql tests/test_postgres_repository.py tests/test_examples_and_schema.py tests/test_postgres_integration.py
git commit -m "feat: add postgres repository boundary"
```

---

### Task 3: Complete PostgreSQL Load Path

**Files:**
- Modify: `src/trace_backed_memory/postgres.py`
- Modify: `tests/test_postgres_repository.py`

**Interfaces:**
- Produces: `PostgresMemoryRepository.load() -> TraceBackedMemoryStore`.
- Consumes: schema metadata lock and dictionary-row psycopg cursor from Task 2.

- [ ] **Step 1: Write the failing complete-load test**

Seed one row in every domain table using parameterized psycopg statements. Use
an active lesson and policy plus a trace-linked decision. Assert:

```python
loaded = PostgresMemoryRepository(connection).load()
snapshot = loaded.to_snapshot()

assert snapshot["snapshot_version"] == 2
assert snapshot["traces"][0]["trace_id"] == "trace_db"
assert snapshot["failure_cases"][0]["case_id"] == "case_db"
assert snapshot["lessons"][0]["lesson_id"] == "lesson_db"
assert snapshot["project_policies"][0]["policy_id"] == "policy_db"
assert snapshot["usage_logs"][0]["decision_id"] == "decision_db"
assert snapshot == TraceBackedMemoryStore.from_snapshot(snapshot).to_snapshot()
```

Include a `TIMESTAMPTZ` with a non-UTC offset, integral `NUMERIC` cost larger
than float range but within JSON limits, fractional cost, and decimal
confidence.

Run the test. Expected: FAIL because `load()` is not implemented.

- [ ] **Step 2: Add deterministic SELECT definitions**

Define one schema-qualified SELECT per table, including every dataclass field
and ordering by primary key. The usage query must select:

```sql
decision_id, run_id, mode, candidate_memory_ids, used_memory_ids,
blocked_memory_ids, reason, risk, recommended_injection, eval_result,
memory_caused_failure, trace_id, context, candidate_memory_statuses,
system_blocked_reasons, created_at
```

Do not select database-only `updated_at` fields into snapshots.

- [ ] **Step 3: Add conversion helpers**

Implement:

```python
def _rfc3339(value: object, field_name: str) -> str | None: ...
def _numeric_cost(value: object) -> int | float | None: ...
def _numeric_confidence(value: object, field_name: str) -> float: ...
def _json_value(value: object, field_name: str) -> object: ...
```

`_rfc3339()` requires an aware `datetime`, converts to UTC, and emits `Z`.
`_numeric_cost()` converts integral `Decimal` to `int`, fractional `Decimal`
to finite `float`, and preserves `None`. Confidence always becomes finite
`float`. JSONB values must already be list/dict values; copy them before adding
them to the snapshot.

- [ ] **Step 4: Build the v2 snapshot and reuse store validation**

Inside one `connection.transaction()`:

```python
with self._connection.transaction():
    with self._connection.cursor(row_factory=dict_row) as cursor:
        self._lock_schema(cursor, write=False)
        snapshot = {
            "snapshot_version": 2,
            "traces": self._load_traces(cursor),
            "failure_cases": self._load_failure_cases(cursor),
            "lessons": self._load_lessons(cursor),
            "project_policies": self._load_project_policies(cursor),
            "usage_logs": self._load_usage_logs(cursor),
        }
return TraceBackedMemoryStore.from_snapshot(snapshot)
```

Wrap expected driver/conversion errors as `PostgresPersistenceError` and retain
the cause.

- [ ] **Step 5: Cover invalid database state**

Use trigger disabling only in the test superuser session to insert malformed
JSON evidence, unknown runtime IDs, and provenance mismatches. Assert `load()`
fails and never returns a partially populated store.

- [ ] **Step 6: Verify and commit Task 3**

```powershell
python -m pytest tests/test_postgres_repository.py -k "load" -q
python -m pytest tests/test_store.py tests/test_postgres_repository.py -q
git add src/trace_backed_memory/postgres.py tests/test_postgres_repository.py
git commit -m "feat: load memory store from postgres"
```

---

### Task 4: Insert And Idempotently Synchronize Complete Stores

**Files:**
- Modify: `src/trace_backed_memory/postgres.py`
- Modify: `tests/test_postgres_repository.py`

**Interfaces:**
- Produces: `PostgresMemoryRepository.sync(store) -> PostgresSyncResult` for new and unchanged rows.
- Consumes: deterministic v2 snapshot and load codecs from Task 3.

- [ ] **Step 1: Build a public-API complete store fixture**

In `tests/test_postgres_repository.py`, construct a store by recording a trace,
adding a verified case, active lesson, project policy, and one usage decision
through `log_decision()` or `prepare_memory()` / `finalize_memory()`. Do not
mutate private store collections.

- [ ] **Step 2: Write the failing round-trip and idempotency test**

```python
first = repository.sync(store)
assert first.traces.inserted == 1
assert first.failure_cases.inserted == 1
assert first.lessons.inserted == 1
assert first.project_policies.inserted == 1
assert first.usage_logs.inserted == 1
assert repository.load().to_snapshot() == store.to_snapshot()

second = repository.sync(store)
assert second.traces.unchanged == 1
assert second.failure_cases.unchanged == 1
assert second.lessons.unchanged == 1
assert second.project_policies.unchanged == 1
assert second.usage_logs.unchanged == 1
```

Run it. Expected: FAIL because `sync()` is not implemented.

- [ ] **Step 3: Implement canonical encoders and insert SQL**

Create one encoder per snapshot record. Wrap every JSONB parameter with
`Jsonb(deepcopy(value))`. Use explicit `INSERT` column lists matching the
SELECT definitions. Preserve `created_at` exactly, including explicit `NULL`,
instead of allowing database defaults to change snapshot equality.

- [ ] **Step 4: Implement immutable insert-or-compare helpers**

For traces and usage logs:

```python
def _sync_immutable_row(
    cursor: object,
    *,
    table: str,
    record_id: str,
    incoming: dict[str, object],
    select_sql: str,
    insert_sql: str,
) -> Literal["inserted", "unchanged"]: ...
```

Select first. Insert only when absent. If present, decode to canonical snapshot
shape and compare exact values. Raise `PostgresConflictError` with table and ID
on any difference.

- [ ] **Step 5: Insert new failure cases, lessons, and policies**

For mutable record tables, Task 4 handles only absent rows and exact unchanged
rows. Defer status/review updates to Task 5, but return `unchanged` for exact
matches. Process every collection in sorted dependency order inside one
transaction after locking metadata `FOR UPDATE`.

- [ ] **Step 6: Verify atomic inserts and counts**

Add a test where a conflicting usage log is encountered after a new trace in
the incoming snapshot. Assert the new trace is rolled back. Verify count totals
for empty and multi-record stores.

- [ ] **Step 7: Verify and commit Task 4**

```powershell
python -m pytest tests/test_postgres_repository.py -k "sync or round_trip or idempotent" -q
git add src/trace_backed_memory/postgres.py tests/test_postgres_repository.py
git commit -m "feat: sync memory stores to postgres"
```

---

### Task 5: Forward Lifecycle Updates, Conflicts, And Additive State

**Files:**
- Modify: `src/trace_backed_memory/postgres.py`
- Modify: `tests/test_postgres_repository.py`

**Interfaces:**
- Extends: `sync()` to update documented mutable fields and reject immutable divergence.
- Preserves: database-only rows absent from the incoming store.

- [ ] **Step 1: Write failing failure-case lifecycle tests**

Persist a draft case, then use public store methods to review and verify it.
Synchronize again and assert `updated == 1` and all review/fix/regression fields
load back. Obsolete the case and assert its active lessons become obsolete in
both PostgreSQL and the loaded store.

- [ ] **Step 2: Implement failure-case immutable comparison and update**

Require equality for:

```python
{"case_id", "source_trace_id", "commit_sha", "created_at"}
```

Update only:

```python
{
    "failure_type", "symptom", "root_cause", "reviewed_by",
    "review_notes", "reviewed_at", "fix", "fix_commit_sha",
    "regression_passed", "status",
}
```

Run one parameterized `UPDATE public.failure_cases SET ... WHERE case_id = %s`.
Let SQL transition and cascade triggers reject reverse state.

- [ ] **Step 3: Write failing lesson and policy lifecycle tests**

Synchronize active rows, obsolete them through store methods, synchronize, and
assert one update for each table. Change lesson text, scope, confidence, source,
or safety flags through a separately constructed snapshot and assert
`PostgresConflictError`. Do the same for policy text/scope/confidence/safety.

- [ ] **Step 4: Implement status-only child updates**

Require every non-status snapshot field to match. If status differs, execute
only:

```sql
UPDATE public.lessons SET status = %s, updated_at = now() WHERE lesson_id = %s
UPDATE public.project_policies SET status = %s, updated_at = now() WHERE policy_id = %s
```

Return `updated`; allow database triggers to enforce forward-only status.

- [ ] **Step 5: Prove additive state**

Synchronize store A, then store B with a distinct valid trace/case chain.
Synchronize store A again and assert B remains in PostgreSQL. `load()` must
return both chains. No `DELETE` or `TRUNCATE` statement may exist in
`postgres.py`.

- [ ] **Step 6: Prove rollback and driver error context**

Create a transaction containing one valid insert followed by an immutable
conflict. Assert no valid insert remains. Assert the public exception names the
operation/table/ID but not a DSN, JSON payload, or SQL parameter values, and the
original psycopg exception or conflict remains available through `__cause__`
where applicable.

- [ ] **Step 7: Verify and commit Task 5**

```powershell
python -m pytest tests/test_postgres_repository.py -q
python -m pytest tests/test_postgres_integration.py -q
git add src/trace_backed_memory/postgres.py tests/test_postgres_repository.py
git commit -m "feat: sync postgres memory lifecycle"
```

---

### Task 6: Documentation, Contract Examples, And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/mvp-roadmap.md`
- Modify: `docs/usage-policy.md`
- Modify: `tests/test_readme_api.py`
- Modify: `tests/test_examples_and_schema.py`

**Interfaces:**
- Documents: installation, schema prerequisite, connection ownership, additive sync, load, conflicts, and remaining non-goals.
- Verifies: package exports and README example stay executable.

- [ ] **Step 1: Write failing documentation contract tests**

Require README to contain:

```text
pip install 'trace-backed-memory[postgres]'
PostgresMemoryRepository.connect
repository.sync(store)
repository.load()
schema_version
additive
```

Require architecture to move the PostgreSQL runtime adapter out of non-goals
while retaining migration, pooling, and async support as non-goals.

- [ ] **Step 2: Add an executable README adapter example**

Document:

```python
from trace_backed_memory import PostgresMemoryRepository

with PostgresMemoryRepository.connect("postgresql://...") as repository:
    result = repository.sync(store)
    restored = repository.load()
```

State that `schemas/postgres.sql` must already be installed, synchronization is
additive, and immutable ID conflicts roll back the whole operation. Do not place
a real credential or localhost assumption in executable tests.

- [ ] **Step 3: Update architecture, roadmap, and usage policy**

Describe repository boundaries, transaction/schema lock flow, lifecycle update
rules, connection ownership, optional dependency behavior, and explicit
non-goals. Mark the synchronous runtime adapter implemented in the roadmap.

- [ ] **Step 4: Run the focused documentation/API tests**

```powershell
python -m pytest tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_postgres_repository.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run all delivery gates**

```powershell
python -m pytest
python -m compileall -q src tests
git diff --check
python -m pip install -e . --dry-run
python -m pip install -e ".[postgres]" --dry-run
```

Expected:

- every test passes;
- adapter PostgreSQL tests execute rather than skip;
- compilation and diff checks exit `0`;
- core install remains psycopg-free;
- postgres extra resolves psycopg 3.

- [ ] **Step 6: Verify PostgreSQL resource cleanup**

Check that no `postgres.exe`/`postgres` process references the test cluster and
that no `postgres-cluster` test directory remains. Stop and fix cleanup before
committing if either check fails.

- [ ] **Step 7: Commit Task 6**

```powershell
git add README.md docs/architecture.md docs/mvp-roadmap.md docs/usage-policy.md tests/test_readme_api.py tests/test_examples_and_schema.py
git commit -m "docs: publish postgres repository workflow"
```

- [ ] **Step 8: Final review package**

Generate a full diff from `89c4b06` to `HEAD`, review the complete feature for
Critical/Important findings, fix all correctness findings in one atomic wave,
and rerun every delivery gate before reporting completion.
