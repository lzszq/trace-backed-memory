# Codex integration

**English** | [简体中文](codex.zh-CN.md)

This repository provides five explicit Codex-facing layers:

1. Root and nested `AGENTS.md` files map invariants, verification, schemas, and
   adapter boundaries.
2. Repository-local skills guide maintainers and runtime users through the
   correct workflows.
3. `LocalAgentMemory` and `tbm.agent.v1` give host applications a focused,
   versioned Python and JSON boundary.
4. Default `tbm-mcp` exposes that same compatibility lifecycle as a
   long-running local STDIO MCP server.
5. Explicit `tbm-mcp --profile durable-v3` exposes the restart-resumable
   durable Agent lifecycle while keeping trusted identities outside tool JSON.

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

Codex Desktop, Codex CLI, and the Codex IDE extension share MCP configuration.
Add this project-scoped `.codex/config.toml` after replacing the checkout path.
Codex loads project configuration only for trusted projects.

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
Open or trust the repository before loading this file.

### Desktop

Open **Settings > MCP servers** to inspect the server, select **Restart**, then
use `/mcp` in the composer to confirm that `trace_backed_memory` is connected.

### CLI

Start a new session from the configured `cwd`. Use these read-only checks:

```bash
codex mcp get trace_backed_memory --json
codex mcp list
```

Inside the terminal UI, use `/mcp` to inspect the active tools. As an
alternative global setup, run `codex mcp add trace_backed_memory -- tbm-mcp
--repo-path /absolute/path/to/repository --sqlite .tbm/memory.sqlite3`; prefer
the project file when the server belongs only to this repository.

### IDE extension

Open the gear menu, select **MCP servers**, inspect the shared configuration,
then select **Restart extension**. The same trusted-project rule applies.

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

The default compatibility MCP process remains alive while Codex performs the
complete lifecycle:

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

## Restart-safe durable profile

Use the explicit `tbm-mcp --profile durable-v3` profile when a GateSession must
survive a Codex or MCP process restart. It requires an operator-owned
application factory that supplies the unified authority graph and fixed
service, Semantic Gate provider, and evaluator identities. Follow the
[durable MCP profile guide](../protocols/durable-mcp-v1.md) for the factory,
one-time SQLite initialization, PostgreSQL selection, content-exposure flags,
and exact tool sequence.

This is a trusted local STDIO profile, not peer-authenticated shared service.
After a restart, call `tbm_durable_get_session` with the persisted `session_id`
and continue from the exact returned version. The default compatibility
profile still keeps pending Gate requests and finalized replay tombstones
in-process; its clients must prepare again after restart and must not recreate
private request tokens.

Remote Streamable HTTP MCP, OAuth, and an untrusted multi-tenant service remain
outside this local profile.

## Conformance

Before connecting another host:

- inspect `tbm_capabilities`;
- validate direct JSON integration against the packaged `tbm.agent.v1`
  schemas;
- test no-memory, relevant-memory, System-Gate block, invalid decision,
  cancellation, process restart, exact retry, and measured completion;
- keep native or unrelated memory systems from re-consolidating TBM snippets
  as independently verified knowledge.
