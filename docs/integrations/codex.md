# Codex integration

**English** | [简体中文](codex.zh-CN.md)

This repository provides four Codex-facing layers without weakening the
current process-local Gate boundary:

1. Root and nested `AGENTS.md` files map invariants, verification, schemas, and
   adapter boundaries.
2. Repository-local skills guide maintainers and runtime users through the
   correct workflows.
3. `LocalAgentMemory` and `tbm.agent.v1` give host applications a focused,
   versioned Python and JSON boundary.
4. `tbm-mcp` exposes that same runtime-only lifecycle as a long-running local
   STDIO MCP server.

## Contributor use

Codex should read the root `AGENTS.md`, then select
`maintain-trace-backed-memory` for repository changes or
`use-trace-backed-memory` for runtime integration. The skills link only the
references needed for that task.

## Install the local MCP profile

Install the optional MCP dependency into the Python environment from which
Codex will launch the server.

Windows PowerShell:

```powershell
py -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm, .codex
Get-Command tbm-mcp
```

macOS or Linux:

```bash
python3 -m pip install -e '.[mcp]'
mkdir -p .tbm .codex
command -v tbm-mcp
```

## Connect Codex

Codex Desktop and Codex CLI use the same project-scoped `.codex/config.toml`.
Add it after replacing the checkout path. Codex loads project configuration
only for trusted projects.

```toml
[mcp_servers.trace_backed_memory]
enabled = true
required = true
command = "tbm-mcp"
args = [
  "--repo-path", "/absolute/path/to/repository",
  "--sqlite", ".tbm/memory.sqlite3",
]
cwd = "/absolute/path/to/repository"
startup_timeout_sec = 10.0
tool_timeout_sec = 60.0
```

Use forward slashes in Windows TOML paths, for example
`C:/Users/name/source/repository`. If Codex Desktop cannot resolve `tbm-mcp`,
set `command` to the absolute path printed by `Get-Command` or `command -v`.
Open or trust the repository and restart Codex Desktop, or start a new Codex
CLI session from the configured `cwd`.

Other clients: [Claude Code](claude-code.md) | [Pi](pi.md)

`--repo-path` is mandatory and fixes the Git provenance root. Exactly one
storage option is mandatory:

- `--memory` for explicit non-durable process-local storage;
- `--sqlite PATH` for local durable records; a relative path resolves below
  the configured repository;
- `--postgres-env ENV_NAME` to read PostgreSQL conninfo from an environment
  variable without placing the secret in project configuration.

`--tenant VALUE` fixes an optional declared-scope value for every request. In
snapshot version 2, that value is applicability metadata, not an authorization
boundary.

## Runtime sequence

The MCP process remains alive while Codex performs the complete lifecycle:

```text
tbm_capabilities / tbm_health
-> tbm_prepare_memory
-> decide only among system_allowed_memory_ids
-> tbm_finalize_memory
-> provide only finalized.snippet to the task
-> execute and measure
-> tbm_complete_run
```

Call `tbm_cancel_run` instead of finalizing when execution will not proceed.
The server derives repository, commit, branch, dirty state, and complete Git
ancestry from the fixed checkout root. It does not accept caller-supplied Git
provenance and exposes no curation, verification, publication, activation, raw
Store, snapshot, or migration tool.

STDIO input is strict, duplicate-key rejecting, finite-number checked, and
bounded to 8 MiB, 100,000 JSON nodes, and depth 100 per frame. Tool request
models reject unknown fields. Agent-facing failures use bounded
`tbm.agent.v1` error envelopes.

## Current boundary

The server is deliberately long-running because pending Gate requests and
finalized replay tombstones remain process-local. SQLite and PostgreSQL
persist Traces, finalized usage decisions, and measured completion, but a
server restart abandons every request that had not been finalized. Clients
must prepare again after restart and must not reconstruct a private request
token. Request IDs are opaque and carry a fresh per-Store 128-bit namespace,
so a stale handle cannot finalize or cancel a newly prepared request after
restart.

Remote Streamable HTTP, OAuth, canonical repository authorization, durable
idempotency/expiry, and cross-process replay still require the coordinated
schema-version-3 work in the product delivery program.

## Conformance

Before connecting another host:

- inspect `tbm_capabilities`;
- validate direct JSON integration against the packaged `tbm.agent.v1`
  schemas;
- test no-memory, relevant-memory, System-Gate block, invalid decision,
  cancellation, process restart, exact retry, and measured completion;
- keep native or unrelated memory systems from re-consolidating TBM snippets
  as independently verified knowledge.
