# Pi + pi-mcp-adapter integration

**English** | [简体中文](pi.zh-CN.md)

This tutorial uses `pi-mcp-adapter` as Pi's MCP client. Trace-backed Memory is
the example MCP server connected through that client. Pi extensions execute
with your user permissions, so review the adapter source and trust project
configuration only in repositories you control.

## Install the server and adapter

Install Trace-backed Memory from its checkout:

```powershell
py -m pip install -e ".[mcp]"
(Get-Command tbm-mcp).Source
```

On macOS or Linux, use `python3` and `command -v tbm-mcp`.

Install Pi if needed, then install the
[`pi-mcp-adapter` MCP client](https://pi.dev/packages/pi-mcp-adapter?name=mcp):

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi install npm:pi-mcp-adapter
```

Before accepting the install, review the current package version in the catalog
and the adapter's [upstream source repository](https://github.com/nicobailon/pi-mcp-adapter),
then restart Pi. The adapter reads a project `.mcp.json` automatically. Open
`/mcp` for its first-run status; if you only have another host's configuration,
or no standard MCP file exists yet, run `/mcp setup` and review its preview
before it writes the project file. The explicit configuration below is the
project-local form used to connect Trace-backed Memory.

## Connect Pi

Create `.mcp.json` in the repository that will use memory. Replace the three
absolute paths:

```json
{
  "mcpServers": {
    "trace-backed-memory": {
      "command": "/absolute/path/to/tbm-mcp",
      "args": [
        "--repo-path",
        "/absolute/path/to/repository",
        "--sqlite",
        "/absolute/path/to/repository/.tbm/memory.sqlite3"
      ],
      "cwd": "/absolute/path/to/repository",
      "lifecycle": "keep-alive"
    }
  }
}
```

Use forward slashes in Windows JSON paths. Start Pi from that repository,
approve the trusted project resources, then run `/mcp` to inspect the client
and server. The adapter also supports:

```text
/mcp tools
/mcp reconnect
/mcp reconnect trace-backed-memory
```

`keep-alive` is required for the current two-phase Gate lifecycle:

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
Do not restart the MCP process between prepare and finalize or cancel.

For restart-safe durable sessions, initialize the
[local daemon](../protocols/local-daemon-v1.md) once and point
`pi-mcp-adapter` at `"command": "/absolute/path/to/tbmd"` with
`"args": ["local", "--state-dir", "/absolute/path/to/repository/.tbm"]`.
The external adapter remains the MCP client; `tbmd` is the server/process
owner and continues by persisted `session_id` plus exact version.

Pi's extension and trust model is documented in the
[official Pi documentation](https://pi.dev/docs/latest) and
[security guide](https://pi.dev/docs/latest/security). This tutorial uses the
MCP client named in the
[`pi-mcp-adapter` package catalog entry](https://pi.dev/packages/pi-mcp-adapter?name=mcp);
its setup, configuration, commands, and lifecycle behavior are documented by
the [adapter's upstream project](https://github.com/nicobailon/pi-mcp-adapter).
It is third-party executable code, not part of Pi core or Trace-backed Memory.

Other clients: [Codex](codex.md) | [Claude Code](claude-code.md)
