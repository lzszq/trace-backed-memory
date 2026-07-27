# Trace-backed Memory contributor guide

## Purpose

Trace-backed Memory converts execution evidence into scoped, reviewed,
gate-controlled engineering memory. It is not conversational memory, a raw
transcript store, or an unrestricted vector-search service.

Start with:

- `docs/product.en.md` for the product contract.
- `docs/architecture.md` for the complete system model.
- `docs/usage-policy.md` for runtime and operator rules.
- `docs/product-program.md` for delivered and planned work.
- `docs/index.md` for the documentation map.

## Environment

- Python 3.11 or newer is required.
- Runtime dependencies are optional; the kernel remains dependency-free.
- Install development tooling with `python -m pip install -e ".[dev]"`.
- Install PostgreSQL support with `python -m pip install -e ".[postgres]"`.
- Do not add implicit network access to runtime, tests, or verification tools.

## Verification

- Fast local verification: `python tools/verify.py --fast`.
- Full verification: `python tools/verify.py --full`.
- Required PostgreSQL verification:
  `python tools/verify.py --full --postgres`.
- A focused test is useful while iterating, but it never replaces the full
  command before handoff.

## Package map

- `models.py`: immutable protocol/domain records.
- `lifecycle.py`: review, verification, publication, and obsolescence.
- `policy.py`: context parsing, System Gate, LLM decision parsing, rendering.
- `store.py`: reference Store and cross-record invariants.
- `agent.py`: focused local application façade and `tbm.agent.v1`.
- `mcp_entry.py` / `mcp_server.py`: optional bounded, runtime-only local STDIO
  MCP profile over the application façade.
- `contracts_v3.py`: strict version-3 identities, mappings, and preflight plans.
- `gate_session_v3.py`: persistence-neutral durable-session contract and
  explicit lifecycle transitions; not an active repository.
- `sqlite_gate_session_v3.py`: opt-in side-by-side append-only GateSession
  revisions and CAS heads; not wired to the active Agent/MCP.
- `replay_v3.py`: content-addressed artifact, exact injection, and fixed
  decision replay manifest contracts; not an artifact repository.
- `migration_v3.py`: inert content-addressed migration bundles and replay.
- `execution.py`: callback orchestration and recovery context.
- `sqlite.py` / `postgres.py`: persistence adapters.
- `sqlite_v3.py`: isolated local staging for immutable migration bundles.
- `resources.py`: strict installed-resource allowlist.
- `cli.py`: operational command adapter.
- `schemas/`: canonical external and persistence contracts.
- `src/trace_backed_memory/_resources/`: byte-identical installed copies.

## Invariants that must not be weakened

- Raw traces are evidence; they are never default prompt memory.
- A model may propose or narrow, but may not verify or activate its own lesson.
- System Gate blocks cannot be reopened by an LLM decision.
- Runtime rendering uses only the final allowed memory set.
- Verified lessons retain reviewed, regression-backed provenance.
- Persisted identities and provenance are immutable.
- Obsolescence and measured outcomes are forward-only.
- Every runtime injection is linked to a Trace and usage decision.
- Scope matching is not authorization; do not present it as tenant security.
- External JSON is bounded, finite-number checked, and duplicate-key rejecting.
- Writes remain staged, atomic, and all-or-nothing.

## Schema and persistence changes

- Snapshot version 2, SQLite schema version 1, and PostgreSQL schema version 2
  are the current compatibility boundary.
- Do not increment a version without an explicit migration, verifier, rollback
  policy, compatibility documentation, and fixture coverage.
- Adding a stored field normally requires synchronized updates to models,
  validation, serialization, JSON Schema, SQLite, PostgreSQL, migrations,
  examples, documentation, and tests.
- Never broaden missing repository, tenant, or scope values during migration.
- Keep database constraints aligned with domain validation.
- Preserve PostgreSQL lock order and transaction/savepoint behavior.

## Resource contract

- Edit canonical files under `schemas/`, `examples/`, or `memory/` first.
- Update `_RESOURCE_SPECS` and `pyproject.toml` package data deliberately.
- Copy the exact bytes into `src/trace_backed_memory/_resources/`.
- Missing, extra, or changed packaged bytes must fail distribution verification.
- Do not infer package filesystem paths; use the public resource API.

## Public interfaces

- Keep package-root exports intentional and typed.
- External errors use stable codes and bounded, sanitized messages.
- CLI output remains deterministic JSON when documented as machine-readable.
- MCP, HTTP, CLI, and SDK adapters must reuse the same kernel and application
  lifecycle; they may not reproduce gate policy independently.
- Pending `MemoryGateRequest` state is process-local until a versioned durable
  gate-session migration is implemented. Never claim otherwise.

## Tests and documentation

- Test every state transition, exact replay, rejection path, and rollback path.
- Add cross-adapter conformance tests for persistence behavior.
- Keep English and Simplified Chinese reference documents linked and aligned.
- Update current-contract text without rewriting historical phase baselines.
- Keep generated/build artifacts out of source control.
- Record architectural boundary changes in the product program and relevant
  reference documents.

## Security review triggers

Treat changes to gate monotonicity, authorization inputs, provenance, artifact
paths, JSON parsing, repository identity, migrations, secrets, audit records,
or multi-tenant behavior as security-sensitive. Require focused negative tests
and document the threat boundary.
