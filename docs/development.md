# Development and verification

**English** | [简体中文](development.zh-CN.md)

## Setup

Use Python 3.11 or newer:

```text
python -m pip install -e ".[dev]"
```

The core package has no mandatory third-party runtime dependency. PostgreSQL
development additionally requires PostgreSQL 12+ server tools and the
`postgres` optional dependency. The local STDIO MCP profile uses the `mcp`
optional dependency; the `dev` extra installs both adapters for verification.

## Canonical verification

```text
python tools/verify.py --fast
python tools/verify.py --full
python tools/verify.py --full --postgres
```

Fast mode compiles sources, runs Ruff, runs mypy, and runs pytest. Full mode
uses branch coverage, then builds wheel and sdist in a fresh temporary
directory and verifies their contents byte-for-byte. `--postgres` sets
`TBM_REQUIRE_POSTGRES=1`, so missing server prerequisites become failures.

## Change checklist

- Read `AGENTS.md` and the exact implementation being modified.
- Keep policy in the kernel; keep CLI, agent, and persistence code as adapters.
- Add rejection and exact-replay tests with each new state transition.
- Update domain, schema, persistence, examples, resources, and documentation
  together for stored-contract changes.
- Keep canonical and installed resource bytes identical.
- Do not update a schema version without an explicit migration and verifier.

## Focused commands

```text
python -m pytest tests/test_agent.py -q
python -m pytest tests/test_mcp_server.py -q
python -m pytest tests/test_sqlite_repository.py -q
python -m pytest tests/test_postgres_repository.py -q
python -m ruff check src tools examples
python -m mypy src/trace_backed_memory
```

PostgreSQL tests may skip in ordinary local runs when server tools are absent.
Release and CI qualification must require them.
