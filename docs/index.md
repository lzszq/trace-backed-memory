# Documentation index

**English** | [简体中文](index.zh-CN.md)

Use this page as the map into Trace-backed Memory. The README is a quick
orientation; these documents define the engineering contracts.

## Product and architecture

- [Product definition and current capabilities](product.en.md)
- [Reference architecture](architecture.md)
- [Memory usage policy](usage-policy.md)
- [Product delivery program](product-program.md)

## Agent integration

- [Local agent protocol `tbm.agent.v1`](protocols/agent-v1.md)
- [Authorization v3 contract](protocols/authorization-v3.md)
- [Durable GateSession v3 domain contract](protocols/gate-session-v3.md)
- [Content-addressed replay contract v3](protocols/replay-v3.md)
- [Codex integration](integrations/codex.md)
- Repository skills:
  `.agents/skills/maintain-trace-backed-memory/` and
  `.agents/skills/use-trace-backed-memory/`

## Development and operations

- [Development and verification](development.md)
- [Snapshot v3 migration preflight](migrations/snapshot-v3-preflight.md)
- [Version-3 migration bundles and isolated staging](migrations/v3-staging-bundles.md)
- `schemas/sqlite.sql` for the supported local SQL profile
- `schemas/postgres.sql` and `schemas/postgres-v1-to-v2.sql` for PostgreSQL
- `tests/verify_distribution.py` for exact installed-resource verification

## Compatibility boundary

The current formats are snapshot version 2, SQLite schema version 1,
PostgreSQL schema version 2, and agent protocol `tbm.agent.v1`. The optional
`tbm-mcp` command is a long-running local STDIO transport for that protocol,
not another persistence version. Pending gate requests remain process-local.
The persistence-neutral `tbm.gate-session.v3` lifecycle contract and opt-in
side-by-side SQLite and isolated PostgreSQL revision repositories are
published, but the active Store/MCP, workers, and service integration do not
use them yet. The storage-neutral authorization-v3 policy/evaluator contract
defines canonical repositories, exact tenant aliases, authenticated identity
slots, role bindings, and linked decisions, but is not wired into active
adapters. Structured regression evidence remains part of the coordinated
schema-version-3 program. The
storage-neutral `tbm.replay.v3` artifact and replay-manifest contract is
published, but no active adapter stores its bytes or manifests yet. The
read-only v3 migration preflight and inert
staging bundles are implemented, but they cannot activate memory or be loaded
as version-3 runtime state.
