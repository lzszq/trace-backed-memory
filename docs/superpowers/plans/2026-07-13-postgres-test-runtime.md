# PostgreSQL Test Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-test PostgreSQL server startup with one server per pytest process and a clean database per test while preserving isolation and cleanup diagnostics.

**Architecture:** A session-scoped internal fixture owns the PostgreSQL data directory and server process. The existing function-scoped `postgres_cluster` fixture creates a validated unique database, points the unchanged `PostgresCluster` API at it, and independently cleans clients, sessions, the database, cluster-level roles, and test files.

**Tech Stack:** Python 3.11+, pytest 8+, PostgreSQL 12+ command-line tools, standard-library `subprocess`, `json`, `uuid`, and `pathlib`.

## Global Constraints

- Run `initdb` once and retain at most one running PostgreSQL server per pytest
  process; failed address-in-use starts may be retried before a server exists.
- Every test database name must match `tbm_test_[0-9a-f]{32}`.
- Keep PostgreSQL access local to `127.0.0.1` with trust authentication in a pytest-owned temporary directory.
- Do not add a runtime or test dependency.
- Keep `PostgresCluster`'s current public test-helper API behavior unchanged.
- A failed cleanup stage must not prevent later stages from running.
- Per-test cleanup must execute inside the pytest call phase when the call runs,
  with an exactly-once finalizer fallback for setup failures.
- Never remove the session root until server shutdown is confirmed by absence
  of `postmaster.pid`.
- Choose a port immediately before startup and retry only a bounded,
  unambiguous address-in-use failure.
- Preserve existing skip behavior when executables are absent or `initdb` cannot run as the current user.
- Do not change production schemas, persistence formats, or repository behavior.
- Baseline: 64 PostgreSQL tests pass in 198.06 seconds; their slowest 20 durations are all fixture setup.

---

### Task 1: Safe PostgreSQL lifecycle primitives

**Files:**
- Modify: `tests/postgres_support.py`
- Test: `tests/test_postgres_integration.py`

**Interfaces:**
- Consumes: existing subprocess conventions and `_report_cleanup_errors()`.
- Produces: `PostgresServer`, `_new_test_database_name()`, `_quote_identifier()`, `_read_role_names()`, and `_run_psql()` for later fixture tasks.

- [ ] **Step 1: Write failing tests for generated names, identifier quoting, and structured role parsing**

Add imports for the new helpers and these tests:

```python
def test_test_database_names_are_unique_safe_identifiers():
    names = {_new_test_database_name() for _ in range(100)}
    assert len(names) == 100
    assert all(re.fullmatch(r"tbm_test_[0-9a-f]{32}", name) for name in names)


def test_postgres_identifier_quoting_escapes_embedded_quotes():
    assert _quote_identifier('role "owner"') == '"role ""owner"""'


def test_read_role_names_decodes_structured_output(monkeypatch):
    result = subprocess.CompletedProcess(
        ["psql"], 0, '["postgres", "role with newline\\ninside"]\n', ""
    )
    monkeypatch.setattr(
        postgres_support,
        "_run_psql",
        lambda *_args, **_kwargs: result,
    )

    assert _read_role_names("psql", {}) == frozenset(
        {"postgres", "role with newline\ninside"}
    )
```

Parametrize three additional `_read_role_names()` cases with stdout values
`"not-json"`, `"{}"`, and `'["postgres", 1]'`; each must raise
`RuntimeError("PostgreSQL role discovery returned invalid JSON")`.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py -q -k "database_names or identifier_quoting or role_names"
```

Expected: collection fails because the new helpers do not exist.

- [ ] **Step 3: Add the lifecycle values and pure helpers**

Implement these boundaries in `tests/postgres_support.py`:

```python
_TEST_DATABASE_RE = re.compile(r"tbm_test_[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class PostgresServer:
    psql: str
    pg_ctl: str
    env: Mapping[str, str]
    root: Path
    data: Path
    baseline_roles: frozenset[str]


def _new_test_database_name() -> str:
    return f"tbm_test_{uuid.uuid4().hex}"


def _require_test_database_name(database_name: str) -> str:
    if _TEST_DATABASE_RE.fullmatch(database_name) is None:
        raise ValueError("invalid PostgreSQL test database name")
    return database_name


def _quote_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or "\x00" in identifier:
        raise ValueError("PostgreSQL identifier must be a string without NUL")
    return '"' + identifier.replace('"', '""') + '"'
```

Construct `PostgresServer.env` with `MappingProxyType` so the frozen server
value cannot be mutated through its environment. Extract the common psql invocation from `PostgresCluster.run()` into
`_run_psql(psql, env, sql, timeout=15.0)`. Implement `_read_role_names()` by
querying a JSON aggregate of `pg_roles`, decoding with `json.loads()`, and
rejecting every shape other than `list[str]` with a `RuntimeError` naming role
discovery.

- [ ] **Step 4: Run the focused and existing client-helper tests**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py -q -k "database_names or identifier_quoting or role_names or client_"
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the primitives**

```powershell
git add tests/postgres_support.py tests/test_postgres_integration.py
git commit -m "test: add postgres lifecycle primitives"
```

### Task 2: Session server and isolated per-test databases

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/postgres_support.py`
- Test: `tests/test_postgres_integration.py`

**Interfaces:**
- Consumes: Task 1's `PostgresServer`, `_new_test_database_name()`, `_require_test_database_name()`, `_read_role_names()`, and `_run_psql()`.
- Produces: session fixture `_postgres_server`, `_create_test_database(server, database_name)`, `_terminate_database_sessions(server, database_name)`, `_drop_test_database(server, database_name)`, and the existing `postgres_cluster` fixture backed by one unique database per invocation.

- [ ] **Step 1: Write failing integration tests for database identity and sequential isolation**

Add a direct assertion driven by the existing fixture:

```python
def test_postgres_cluster_targets_an_isolated_test_database(postgres_cluster):
    database_name = assert_sql_succeeds(postgres_cluster, "SELECT current_database()")
    assert database_name == postgres_cluster.env["PGDATABASE"]
    assert re.fullmatch(r"tbm_test_[0-9a-f]{32}", database_name)
```

Expose `_postgres_server` through `tests/conftest.py` and add one test that uses
`_create_test_database()`, `_terminate_database_sessions()`, and
`_drop_test_database()` to create two sequential databases on the same server.
Create a table and set a database-local setting in the first, remove it, then
assert the second has neither the table nor that setting. This test must call
the termination and drop helpers for both databases in `finally` blocks.

- [ ] **Step 2: Run the new identity test and verify the old fixture fails**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py::test_postgres_cluster_targets_an_isolated_test_database -q
```

Expected: FAIL because the old fixture targets the built-in `postgres`
database.

- [ ] **Step 3: Extract one session-scoped server fixture**

Move executable discovery, temporary server root creation, `initdb`, and
`pg_ctl start` into the exact fixture signature
`_postgres_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresServer]`
decorated with `@pytest.fixture(scope="session")`.

Use `tmp_path_factory.mktemp("postgres-server")` and preserve the current host,
user, timeout, statement timeout, no-locale, UTF-8, and skip behavior. Choose a
port immediately before `pg_ctl start`; on a recognized address-in-use failure
with no `postmaster.pid`, choose a new port and retry up to three total
attempts. Store the successful port in `PostgresServer.env`, capture
`baseline_roles` immediately after startup, and export the fixture from
`tests/conftest.py` so pytest can resolve the dependency.

- [ ] **Step 4: Create one database for every existing `postgres_cluster` request**

Implement `_create_test_database()` with a validated generated name and an
administrator psql call:

```sql
CREATE DATABASE "tbm_test_<uuid>" TEMPLATE template0
```

Change `postgres_cluster` to depend on `_postgres_server` and `tmp_path`, make
`root = tmp_path / "postgres-cluster"`, copy the administrator environment with
`PGDATABASE` set to the generated name, and yield the existing
`PostgresCluster(server.psql, env, root)` object. Install an idempotent cleanup
callback on the pytest item before database creation and register the same
callback as a fixture finalizer fallback. A `tryfirst` `pytest_runtest_call`
wrapper invokes cleanup with the actual call exception before call reporting;
the finalizer handles setup paths where the call hook never runs. Task 3 fills
that callback with independently reported stages and cluster-level role
restoration.

- [ ] **Step 5: Run identity, schema, and repository smoke tests**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py::test_postgres_cluster_targets_an_isolated_test_database tests/test_postgres_integration.py::test_postgres_schema_install_is_atomic_and_public tests/test_postgres_repository.py::test_repository_loads_a_complete_normalized_snapshot -q
```

Expected: all three tests pass and only one server startup appears in setup
timing.

- [ ] **Step 6: Commit the fixture ownership change**

```powershell
git add tests/conftest.py tests/postgres_support.py tests/test_postgres_integration.py
git commit -m "test: reuse postgres server across test databases"
```

### Task 3: Independent database, role, and server cleanup

**Files:**
- Modify: `tests/postgres_support.py`
- Modify: `tests/test_postgres_integration.py`

**Interfaces:**
- Consumes: Task 2's `PostgresServer`, `_postgres_server`, and generated database contract.
- Produces: `_cleanup_postgres_database_resources()` and `_cleanup_postgres_server_resources()`, each returning `list[BaseException]`; fixture teardown invokes them through `_report_cleanup_errors()`.

- [ ] **Step 1: Replace the old cleanup unit test with two failing stage-order tests**

For database cleanup, inject failures into tracked-client termination,
administrator connection termination, database drop, role cleanup, and
directory removal. Record stage names and assert all five stages execute in
that order and every raised error is returned with a stage note.

For server cleanup, inject repeated `pg_ctl` timeouts while `postmaster.pid`
remains. Assert the root is retained and both the stop failure and skipped
directory cleanup are returned. Also cover a nonzero stop result racing with
PID-file disappearance and assert directory cleanup then succeeds. Keep the
existing `_report_cleanup_errors()` assertions for original-error notes and
no-original-error `ExceptionGroup`.

- [ ] **Step 2: Run cleanup tests and verify the new boundaries are missing**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py -q -k "cleanup_stages or cleanup_preserves"
```

Expected: collection or selected tests fail because the split cleanup helpers
do not exist.

- [ ] **Step 3: Implement per-test cleanup as independent stages**

`_cleanup_postgres_database_resources()` must:

1. call `cluster.terminate_clients()`;
2. use the administrator environment to run
   `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '<validated generated name>' AND pid <> pg_backend_pid()`;
3. run `DROP DATABASE IF EXISTS <quoted validated name>`;
4. compare `_read_role_names()` with `server.baseline_roles` and attempt
   `DROP ROLE <quoted role>` for every sorted new role, collecting per-role
   failures;
5. verify `root.resolve().is_relative_to(tmp_path.resolve())` before deleting
   the per-test directory.

Each stage catches `Exception`, adds a precise PostgreSQL cleanup-stage note,
and appends it without skipping later stages. The item cleanup callback invokes
this helper during `pytest_runtest_call`, then passes its errors and the real
call exception to `_report_cleanup_errors()`. A passing call therefore fails
with the cleanup `ExceptionGroup`; a failing call retains its original
exception with a cleanup note.

- [ ] **Step 4: Implement session-server cleanup as independent stages**

`_cleanup_postgres_server_resources()` must preserve immediate `pg_ctl stop`,
retry it at most once while `postmaster.pid` remains, and tolerate an
unsuccessful command when the PID file disappears. Only after that confirmation
may it verify the root remains under `tmp_path_factory.getbasetemp()` and remove
the directory. If the PID file survives, retain the root and report both stop
and skipped-directory errors. Call it from the session fixture's `finally`
block.

- [ ] **Step 5: Add real cleanup tests for untracked sessions and roles**

Using `_postgres_server`, create a test database and `PostgresCluster`, then:

- open an untracked long-lived psql client against it;
- create `memory_cleanup_role`;
- invoke `_cleanup_postgres_database_resources()`;
- assert the client exits, the database no longer appears in `pg_database`,
  the role no longer appears in `pg_roles`, and the returned error list is
  empty.

Use `try/finally` fallback cleanup so a failing assertion does not leak the
client or database.

- [ ] **Step 6: Run all integration and repository PostgreSQL tests**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py tests/test_postgres_repository.py -q
```

Expected: all 64 baseline tests plus the new lifecycle tests pass.

- [ ] **Step 7: Commit cleanup isolation**

```powershell
git add tests/postgres_support.py tests/test_postgres_integration.py
git commit -m "test: isolate postgres database cleanup"
```

### Task 4: Runtime benchmark and repository verification

**Files:**
- Modify only if verification exposes a defect: files from Tasks 1-3

**Interfaces:**
- Consumes: completed fixture lifecycle.
- Produces: measured performance evidence and a clean, reviewed branch ready for `main`.

- [ ] **Step 1: Measure the optimized PostgreSQL subset**

Run:

```powershell
python -m pytest tests/test_postgres_integration.py tests/test_postgres_repository.py -q --durations=20
```

Expected: all tests pass, total runtime is materially below 198.06 seconds,
and the slowest list no longer shows a 3+ second server setup for every test.

- [ ] **Step 2: Run the full suite and static checks**

Run these commands separately:

```powershell
python -m pytest -q --durations=20
python -m compileall -q src tests
git diff --check main...HEAD
git status --short
```

Expected: full suite passes; compileall and diff checks emit no errors; only
intentional tracked changes are present.

- [ ] **Step 3: Review the complete branch against the design**

Inspect `git diff --stat main...HEAD`, `git diff main...HEAD`, and the measured
durations. Verify session ownership, database isolation, role restoration,
failure-stage independence, stable public helper behavior, and absence of
production changes. Resolve every Critical, Important, and Minor finding and
rerun affected tests.

- [ ] **Step 4: Record any verification-only correction**

If Step 2 or review required code changes, stage only those files and commit:

```powershell
git commit -m "test: harden postgres fixture lifecycle"
```

If no correction was required, do not create an empty commit.

- [ ] **Step 5: Final-review remediation and branch handoff**

Add real nested pytest coverage for simultaneous call and cleanup failure,
passing-call cleanup failure, setup fallback, and exactly-once cleanup. Add
focused server-shutdown and startup-retry tests, run them RED then GREEN, and
repeat the PostgreSQL subset, full suite, `compileall`, branch diff check, and
leak audit. Commit only the owned runtime, tests, and documentation on the
feature branch. Merge, rebase, push, worktree removal, and changes to `main`
remain separate owner actions.
