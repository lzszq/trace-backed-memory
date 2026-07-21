# Required PostgreSQL and Windows CI Coverage Design

## Summary

The existing CI test matrix installs the Python PostgreSQL driver but does not
install PostgreSQL server executables. The session fixture intentionally skips
database-backed tests when `initdb`, `pg_ctl`, or `psql` is unavailable, so the
workflow can report success without executing the real DDL, transaction,
locking, and repository tests. The workflow also has no Windows runner.

Phase 37 adds an explicit CI-only required mode for the PostgreSQL fixture, a
dedicated Ubuntu PostgreSQL job that installs and verifies the server tools,
and a Windows full-suite job. Local development keeps the existing optional
PostgreSQL behavior.

## Goals

- Make a missing or unusable PostgreSQL test runtime fail in the dedicated CI
  job instead of becoming a successful skipped suite.
- Execute both `tests/test_postgres_integration.py` and
  `tests/test_postgres_repository.py` against one real private PostgreSQL
  server in CI.
- Verify the project test suite on `windows-latest` with Python 3.13 while
  retaining the existing Ubuntu Python 3.11-3.13 matrix.
- Preserve local skip behavior when PostgreSQL server tools are not installed
  or `initdb` cannot legally run as the current user.
- Leave application runtime, package dependencies, persistence formats,
  packaged resources, snapshot version 2, and PostgreSQL schema version 1
  unchanged.

## Required Runtime Contract

`tests/postgres_support.py` owns one helper for an unavailable PostgreSQL test
runtime. It receives a complete diagnostic reason and never returns:

- when `TBM_REQUIRE_POSTGRES` is exactly `1`, call `pytest.fail(reason)`;
- otherwise call `pytest.skip(reason)`.

The session fixture uses this helper for both existing environmental skip
points: missing `initdb`/`pg_ctl`/`psql`, and an `initdb` refusal to run as the
current user. Other `initdb` and server startup failures remain unconditional
test failures.

The environment variable is a test-infrastructure switch, not a library or CLI
configuration surface. It is not read by code under `src/` and is not
persisted.

## CI Jobs

The existing Ubuntu Python-version matrix and package job remain intact.

The new `postgres` job:

1. runs on `ubuntu-latest` with Python 3.13;
2. installs PostgreSQL server and client packages;
3. adds the newest `/usr/lib/postgresql/*/bin` directory to `GITHUB_PATH`;
4. installs the development dependencies;
5. preflights `initdb`, `pg_ctl`, `psql`, and the `psycopg` import;
6. sets `TBM_REQUIRE_POSTGRES=1`; and
7. runs the PostgreSQL integration and repository test files together.

On POSIX, the private server must set its Unix socket directory to its
pytest-owned data directory instead of inheriting a distribution default such
as `/var/run/postgresql`. Clients still use explicit TCP loopback. Windows
keeps the existing no-socket startup options.

The new `windows` job runs the complete pytest suite on `windows-latest` with
Python 3.13. PostgreSQL remains optional in that job because the dedicated
Ubuntu job is the authoritative required database runtime; if the Windows
image already exposes compatible server tools, the same tests may run there
as well.

## Test Contract

Focused tests prove that the unavailable-runtime helper skips by default,
fails in required mode, and includes its diagnostic reason. Documentation and
workflow contract tests require:

- both new jobs and runner names;
- exact `TBM_REQUIRE_POSTGRES: "1"` configuration;
- PostgreSQL package installation, binary preflight, driver preflight, and the
  two-file pytest command; and
- Phase 37 product and roadmap wording.

Completion requires focused PostgreSQL-infrastructure tests, the full local
suite, YAML parsing, `compileall`, `git diff --check`, and a successful remote
run containing Ubuntu 3.11/3.12/3.13, Windows, required PostgreSQL, and package
jobs.
