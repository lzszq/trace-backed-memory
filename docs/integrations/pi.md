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

Install Pi if needed, then install the adapter from Pi's package catalog:

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi install npm:pi-mcp-adapter
```

Review the package's current version and source before accepting the install,
then restart Pi. Open `/mcp` for the first-run notice. If no standard MCP file
exists yet, run `/mcp setup` to create one; the explicit configuration below
is the resulting project-local form.

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

Pi's extension and trust model is documented in the
[official Pi documentation](https://pi.dev/docs/latest) and
[security guide](https://pi.dev/docs/latest/security). Adapter installation,
client installation, setup, configuration, commands, and lifecycle options
come from the
[`pi-mcp-adapter` entry in Pi's package catalog](https://pi.dev/packages/pi-mcp-adapter?name=mcp).
The catalog marks it as third-party executable code; it is not part of Pi core.

Other clients: [Codex](codex.md) | [Claude Code](claude-code.md)
