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
The standalone TypeScript SDK requires Node.js 20 or newer for development and
has no runtime package dependency.

## Canonical verification

```text
python tools/verify.py --fast
python tools/verify.py --full
python tools/verify.py --full --postgres
python tools/verify.py --all
```

Fast mode compiles sources, runs Ruff, runs mypy, and runs pytest. Full mode
uses branch coverage, then builds wheel and sdist in a fresh temporary
directory and verifies their contents byte-for-byte. `--postgres` sets
`TBM_REQUIRE_POSTGRES=1`, so missing server prerequisites become failures.
`--all` is the offline full-repository gate: it also checks the persistence
authority registry and canonical resource manifest, requires PostgreSQL, runs
`pip check`, and runs the already installed TypeScript toolchain. It never runs
`npm ci` or downloads tools.

## Change checklist

- Read `AGENTS.md` and the exact implementation being modified.
- Keep policy in the kernel; keep CLI, agent, and persistence code as adapters.
- Add rejection and exact-replay tests with each new state transition.
- Update domain, schema, persistence, examples, resources, and documentation
  together for stored-contract changes.
- Keep canonical and installed resource bytes identical.
- Run `python tools/verify_authority_registry.py`; every new
  `sqlite_*_v3.py` or `postgres_*_v3.py` module must declare its ledger,
  projection, migration, or coordinator role and event/projection impact.
- Run `python tools/generate_sqlite_v3_bundle.py --check`; after deliberately
  editing a listed SQLite v3 component, run its `--refresh` mode before the
  resource generator.
- Run `python tools/generate_resources.py --check`; use explicit `--refresh`
  and `--write` only when changing manifest-listed canonical bytes.
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

The TypeScript SDK has a pinned toolchain and a separate no-network verification
step after `npm ci`:

```text
cd packages/typescript-sdk
npm ci --ignore-scripts
npm run check
npm test
npm run pack:check
```

`npm run check` verifies the canonical OpenAPI binding, `npm test` includes a
real Python HTTP lifecycle, and `pack:check` inspects the publishable package.
The dedicated CI job runs all four commands; Python `tools/verify.py` remains
dependency-free from Node and does not install npm packages implicitly.
