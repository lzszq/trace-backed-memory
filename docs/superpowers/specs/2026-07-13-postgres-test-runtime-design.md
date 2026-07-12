# PostgreSQL Test Runtime Design

## Summary

The PostgreSQL test fixture currently creates, initializes, starts, stops, and
deletes a complete PostgreSQL cluster for every test invocation. The 64
PostgreSQL tests therefore take 198.06 seconds on the current development
machine, and the slowest 20 durations are all 3.36-3.86 second fixture setup
steps.

This project keeps one private PostgreSQL server alive for the pytest session
and gives every test a fresh database on that server. Per-test cleanup still
terminates clients, removes database state, restores cluster-level roles, and
preserves the existing original-error reporting behavior. Existing tests keep
using the `PostgresCluster` API unchanged.

## Goals

- Run `initdb` once and retain at most one running PostgreSQL server per pytest
  process; failed address-in-use starts may be retried before a server exists.
- Give every `postgres_cluster` consumer a unique, empty database.
- Prevent tables, functions, settings, connections, and newly created roles
  from leaking between tests.
- Keep client output and temporary SQL files isolated by pytest test case.
- Preserve skip behavior when PostgreSQL executables are unavailable or
  `initdb` cannot run as the current user.
- Attempt every cleanup stage even when an earlier stage fails.
- Preserve the original test failure and attach cleanup failures as notes.
- Keep the fixture compatible with serial pytest and independent xdist worker
  processes without adding an xdist dependency.
- Demonstrably remove repeated cluster setup from the PostgreSQL test runtime.

## Non-goals

- Changing production PostgreSQL schemas or repository behavior.
- Sharing one PostgreSQL server between separate pytest processes.
- Supporting concurrent tests inside one pytest process.
- Reusing one database by truncating tables or rebuilding only `public`.
- Adding containers, external services, or a PostgreSQL Python dependency to
  the fixture implementation.
- Optimizing individual SQL integration tests beyond fixture lifecycle cost.

## Alternatives Considered

### 1. Session server with a fresh database per test (selected)

Initialize and start one private server, then create and drop a uniquely named
database for each test. This removes the measured startup cost while retaining
database-level isolation, including database settings and namespaces. Explicit
role restoration handles the one cluster-level side effect in the current
suite.

### 2. Session server with a rebuilt `public` schema

Dropping and recreating `public` would be slightly faster, but it would not
isolate database-level settings, extensions, other schemas, or ownership. It
also makes future tests silently depend on an incomplete reset contract.

### 3. One server per test module

Module scope would reduce startup count from 64 to two with a smaller fixture
change. It still pays repeated server startup as PostgreSQL coverage grows and
requires the same per-test state reset logic, so it provides less benefit for
nearly the same isolation work.

## Resource Model

Add an internal immutable `PostgresServer` value containing:

- resolved `initdb`, `pg_ctl`, and `psql` executable paths;
- the session root, data directory, and log path;
- an administrator environment targeting the built-in `postgres` database;
- the role names present immediately after server startup.

The session-scoped `_postgres_server` fixture owns this value and the server
process. Its root comes from `tmp_path_factory`, so every pytest process,
including each xdist worker, gets an independent data directory and port.

The existing function-scoped `postgres_cluster` fixture depends on that server.
It generates an internal identifier matching
`tbm_test_[0-9a-f]{32}`, creates that database from `template0`, copies the
administrator environment with `PGDATABASE` changed to the generated name,
and yields the existing `PostgresCluster` value with a per-test `tmp_path`
root.

No production package imports or depends on these test-support values.

## Server Lifecycle

Session setup performs executable discovery and `initdb` once. It chooses a
free port immediately before `pg_ctl start`. If startup fails with a
cross-platform address-in-use diagnostic and `postmaster.pid` is absent, it
chooses a new port and retries, up to three startup attempts. Other startup
failures, and failures where a PID file means a server may be running, are not
retried. The immutable `PostgresServer.env` retains the port from the
successful attempt. Setup then queries and stores the initial role set through
`psql` against the administrator database. Role discovery returns JSON and is
decoded with the standard library rather than parsed from ad hoc delimited
text.

Session teardown runs bounded, ordered stages:

1. stop the server with immediate mode, retrying at most once while
   `postmaster.pid` remains;
2. confirm shutdown through disappearance of `postmaster.pid`;
3. if shutdown is confirmed, verify that the session root remains inside the
   pytest temporary root and remove it.

A nonzero or timed-out stop command is tolerated when the PID file disappears,
covering a command/termination race. If the PID file remains after both stop
attempts, the root is retained and cleanup reports both the stop failure and
the deliberately skipped directory stage. This prevents recursive deletion of
potentially live server state on POSIX and Windows. The current skip semantics
remain in session setup.

## Per-Test Lifecycle

Database setup runs one administrator `CREATE DATABASE` statement using an
internally generated and validated identifier. The database is created from
`template0`, with UTF-8 encoding and locale settings inherited from the
no-locale server initialization. A setup failure is reported with sanitized
command output and does not yield a partially configured cluster.

Per-test teardown runs all of these stages even if one fails:

1. terminate every subprocess tracked by `PostgresCluster`;
2. from the administrator database, terminate all remaining sessions connected
   to the test database;
3. drop the test database;
4. query roles and drop every role not present in the server's startup role
   set;
5. verify and remove the per-test client-output directory.

Role cleanup happens after dropping the database so test-owned schemas and
objects disappear before their owner is removed. Role identifiers are quoted
as PostgreSQL identifiers by a dedicated helper. Database identifiers are
accepted only if they match the fixture's generated-name pattern before SQL is
constructed.

Each stage records an explanatory exception note. The fixture installs an
idempotent cleanup callback on the pytest item. A `tryfirst` wrapper around
`pytest_runtest_call` invokes that callback before pytest constructs the call
report, passing the actual call exception when one exists. Cleanup failures are
therefore added as `PostgreSQL cleanup also failed` notes to an original test
failure, while cleanup after a passing call raises the cleanup
`ExceptionGroup` as a call-phase failure. A fixture finalizer invokes the same
callback as a fallback when setup prevents the call hook from running; the
idempotence guard makes every path clean exactly once.

## Compatibility

`PostgresCluster.psql`, `.env`, `.root`, `.clients`, `connection_kwargs()`,
`run()`, `run_script()`, `load_schema()`, `spawn()`, advisory-latch helpers,
and client cleanup retain their current behavior. In particular:

- `connection_kwargs()` and all psql children target the per-test database;
- `cluster.root` remains writable for generated SQL and subprocess logs;
- `PGOPTIONS`, connection timeout, statement timeout, host, port, and user keep
  their existing values;
- tests may open untracked psycopg connections, which teardown handles through
  `pg_stat_activity` before dropping the database.

The old combined `_cleanup_postgres_resources()` boundary is split into server
and database cleanup helpers with the same independent-stage and error-reporting
contract. Existing direct cleanup tests are updated to target the appropriate
new helper.

## Failure Handling

All subprocess calls use argument lists, `stdin=DEVNULL`, explicit timeouts,
captured UTF-8 output, and `check=False`. Return-code failures become bounded
`RuntimeError` messages with the failed lifecycle operation and PostgreSQL
output. Secrets are not introduced into command arguments or messages.

Database teardown must use the administrator environment, never the test
database environment, so it can still terminate connections and drop a broken
test database. A failed client cleanup, connection termination, database drop,
role cleanup, or directory removal does not suppress subsequent stages.

Server-root removal has a stricter dependency: it is not attempted until
shutdown is confirmed by absence of `postmaster.pid`. Failure to confirm
shutdown produces separate server-stop and skipped-directory cleanup errors.
Startup retry is limited to recognized address-in-use diagnostics, is bounded
at three attempts, and is disabled whenever a PID file leaves server state
ambiguous.

## Testing

Implementation follows red-green-refactor. Focused tests cover:

- generated database names being unique and restricted to safe identifiers;
- one per-test cluster targeting a database other than `postgres`;
- two sequential databases on one server not sharing tables or database
  settings;
- cluster-level roles created by one database being removed during cleanup;
- untracked sessions being terminated before database drop;
- database cleanup stages continuing after injected failures;
- server cleanup stages continuing after injected failures;
- nested pytest call-phase behavior preserving an original test error with a
  cleanup note, failing a passing test with an `ExceptionGroup`, exercising
  setup fallback, and running cleanup exactly once;
- server roots being retained while a PID file survives bounded stop retries,
  plus removal after shutdown wins a stop-command race;
- address-in-use startup retry success, three-attempt exhaustion, unrelated
  startup failure, and ambiguous PID-file state;
- all existing schema, concurrency, repository load, and repository sync tests
  remaining unchanged in observable behavior.

Completion requires the 64-test PostgreSQL subset, the full pytest suite,
`compileall`, `git diff --check`, and a whole-branch review. The optimized
PostgreSQL subset must be materially faster than the 198.06-second baseline,
and repeated cluster startup must no longer appear in per-test setup durations.
