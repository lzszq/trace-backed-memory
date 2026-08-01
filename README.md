# Trace-backed Memory

**English** | [简体中文](README.zh-CN.md)

A provenance-backed memory layer for LLM and agent harness engineering.

Trace-backed Memory turns agent traces, evaluation results, and Git evidence
into reviewed, scoped, auditable memory:

```text
Trace -> Failure Case -> Verified Lesson -> Gated Runtime Memory
```

[Documentation](docs/index.md) ·
[Current status](docs/status/current-capability-matrix.md) ·
[Detailed reference](docs/reference.md) ·
[Product and capabilities](docs/product.en.md) ·
[Architecture](docs/architecture.md) ·
[Usage policy](docs/usage-policy.md) ·
[Delivery program](docs/product-program.md)

## Why it exists

This is not generic chatbot memory or a raw transcript store. It is an
engineering memory system with five core guarantees:

- raw traces remain evidence and are not prompt memory by default;
- a model cannot verify or activate its own lesson;
- System Gate blocks cannot be reopened by an LLM;
- runtime rendering uses only the final allowed memory set;
- every injection is linked to a Trace and an auditable decision.

See the [product contract](docs/product.en.md) for the complete capability
matrix and [architecture](docs/architecture.md) for the system model.

## Quick start

Python 3.11 or newer is required.

```powershell
python -m pip install -e .
```

The first complete Python lifecycle is in the
[`tbm.agent.v1` guide](docs/protocols/agent-v1.md); storage, review, migration,
and operations examples live in the [detailed reference](docs/reference.md).
Existing snapshot-v2/SQLite-v1 projects use the focused
[SQLite v3 migration guide](docs/migrations/sqlite-v3-apply.md).

## MCP + Codex in 2 minutes (Claude Code and Pi too)

Install the MCP profile and create project-local state:

Windows PowerShell:

```powershell
py -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm,.codex
```

macOS or Linux:

```bash
python3 -m pip install -e '.[mcp]'
mkdir -p .tbm .codex
```

Codex Desktop, Codex CLI, and the IDE extension share MCP configuration. Save
this project-level file:

```toml
# .codex/config.toml
[mcp_servers.trace_backed_memory]
command = "tbm-mcp"
args = ["--repo-path", ".", "--sqlite", ".tbm/memory.sqlite3"]
```

Reopen the trusted repository, restart the current Codex surface, and ask
Codex to call `tbm_capabilities`. The
[Codex multi-surface guide](docs/integrations/codex.md) covers Desktop, CLI,
the IDE extension, troubleshooting, and the restart-safe durable profile.
Other clients:

- **Claude Code:** run the
  [one-command setup](docs/integrations/claude-code.md#connect-claude-code),
  verify it with `claude mcp get trace-backed-memory`, and open `/mcp`.
- **Pi + `pi-mcp-adapter`:** install the adapter as Pi's MCP client, then follow
  the [Pi client tutorial](docs/integrations/pi.md#connect-pi). Review the
  executable adapter before granting project trust.

Clients using the default compatibility profile must keep `tbm-mcp` alive for
the complete
`prepare -> finalize -> complete` lifecycle, or call `cancel` before
finalization. The server exposes runtime operations only; it cannot review,
verify, or activate memory.

For restart-resumable GateSession state, use the advanced explicit
[durable MCP profile](docs/protocols/durable-mcp-v1.md), or let one
[`tbmd local` daemon](docs/protocols/local-daemon-v1.md) own MCP, HTTP,
recovery, and outbox delivery over the same SQLite v3 graph.

## Interfaces

- Python: `TraceBackedMemoryStore` and `LocalAgentMemory`
- CLI: capabilities, snapshot operations, migration
  plan/apply/verify/rollback, and resource discovery
- Local MCP: `tbm-mcp` with the optional `mcp` dependency
- Local HTTP SDKs: synchronous/asynchronous Python and Node.js TypeScript for
  the compatibility profile, plus explicit durable-v3 Python/TypeScript
  clients; see the [compatibility](docs/protocols/agent-http-v1.md) and
  [durable](docs/protocols/durable-http-v1.md) guides
- Opt-in authenticated local MCP: trusted startup selects version-3 identity
  and environment; see the [reference](docs/reference.md#long-running-local-mcp)
- Restart-safe local MCP: explicit `--profile durable-v3`; see the
  [durable MCP guide](docs/protocols/durable-mcp-v1.md)
- Restartable local durable service: `tbmd init/local/doctor/health`; see the
  [local daemon guide](docs/protocols/local-daemon-v1.md)
- Persistence: in-memory, SQLite, and PostgreSQL adapters
- Version-3 preparation: authenticated pre-retrieval boundary, GateSession,
  authorization, entity registry, replay, audit/recovery, structured evidence,
  immutable revisions, retrieval snapshots, gate evaluations, and outcomes

Use the [documentation index](docs/index.md) to reach each protocol, migration,
integration, and operations guide. Canonical Schemas and examples are
available through `tbm resource list` or the Python resource API.

## Current boundary

The active compatibility boundary remains snapshot version 2, SQLite schema
version 1, PostgreSQL schema version 2, and `tbm.agent.v1`. Version-3
contracts and their isolated opt-in repositories do not silently change the
default Store, Agent, or MCP lifecycle. Explicit durable HTTP and trusted-local
MCP profiles, including `tbmd local`, select the version-3 authority graph.

Pending gate requests in the default compatibility profile remain
process-local; the explicit durable profiles persist GateSession state.
Scope matching is not tenant authorization. Do not deploy the current Alpha as
an untrusted shared multi-tenant service.

Read [Product and current capabilities](docs/product.en.md) for exact delivered
behavior and [Memory usage policy](docs/usage-policy.md) for operator rules.

## Development

```powershell
python -m pip install -e ".[dev]"
python tools/verify.py --fast
python tools/verify.py --full
python tools/verify.py --full --postgres
python tools/verify.py --all
```

PostgreSQL verification is required for changes to PostgreSQL behavior.
Runtime, tests, and verification tools must not add implicit network access.
See [Development and verification](docs/development.md) for environment and
distribution details.

## License

[MIT](LICENSE)
