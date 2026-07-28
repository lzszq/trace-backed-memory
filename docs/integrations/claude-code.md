# Claude Code integration

**English** | [简体中文](claude-code.zh-CN.md)

Claude Code can launch Trace-backed Memory as a local STDIO MCP server. The
configuration below uses an explicit executable, repository root, and SQLite
path so provenance and storage do not depend on the shell's working directory.

## Install the server

From the Trace-backed Memory checkout:

```powershell
py -m pip install -e ".[mcp]"
(Get-Command tbm-mcp).Source
```

On macOS or Linux, use `python3` and locate the executable with
`command -v tbm-mcp`.

## Connect Claude Code

Run this from the repository that will use memory, replacing all three
absolute paths:

```bash
claude mcp add --transport stdio --scope project trace-backed-memory -- \
  /absolute/path/to/tbm-mcp \
  --repo-path /absolute/path/to/repository \
  --sqlite /absolute/path/to/repository/.tbm/memory.sqlite3
```

PowerShell accepts the same command on one line. Use forward slashes in
Windows paths. Claude Code writes a project-scoped `.mcp.json`; inspect that
file before approving the server for the project.

Verify the registration:

```text
claude mcp get trace-backed-memory
claude mcp list
```

Start Claude Code in the repository and open `/mcp` to inspect status and
authenticate or reconnect if requested.

## Required runtime sequence

The client must call:

```text
tbm_capabilities / tbm_health
-> tbm_prepare_memory
-> decide only among system_allowed_memory_ids
-> tbm_finalize_memory
-> use only finalized.snippet
-> execute and measure
-> tbm_complete_run
```

Call `tbm_cancel_run` before finalization when execution will not proceed.
Keep the same server process alive from prepare through finalize or cancel;
pending requests are process-local in the current schema.

`tbm-mcp` derives Git provenance from `--repo-path`, rejects caller-supplied
provenance, and exposes no review, verification, publication, activation,
snapshot, or migration operation. Project MCP configuration is executable
code: commit it only when every collaborator should trust the command and
arguments.

Configuration syntax and scope behavior are documented by the
[official Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

Other clients: [Codex](codex.md) | [Pi](pi.md)
