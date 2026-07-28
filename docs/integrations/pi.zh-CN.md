# Pi + pi-mcp-adapter 集成

[English](pi.md) | **简体中文**

本教程把 `pi-mcp-adapter` 作为 Pi 的 MCP 客户端，并以 Trace-backed Memory 作为
通过该客户端连接的 MCP server 示例。Pi extension 以当前用户权限执行，因此必须审查
adapter 源码，并且只在自己控制的仓库中信任项目配置。

## 安装服务与适配器

从 Trace-backed Memory checkout 安装：

```powershell
py -m pip install -e ".[mcp]"
(Get-Command tbm-mcp).Source
```

macOS 或 Linux 使用 `python3` 与 `command -v tbm-mcp`。

如尚未安装 Pi，先安装 Pi，再安装
[`pi-mcp-adapter` MCP 客户端](https://pi.dev/packages/pi-mcp-adapter?name=mcp)：

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi install npm:pi-mcp-adapter
```

接受安装前，应在 catalog 检查 package 当前版本，并审查 adapter 的
[上游源码仓库](https://github.com/nicobailon/pi-mcp-adapter)，然后重启 Pi。adapter 会自动读取项目
`.mcp.json`。首次打开 `/mcp` 时检查状态；若当前只有其他 host 的配置，或尚无标准 MCP
配置文件，则运行 `/mcp setup`，确认写入预览后再生成项目文件。下面是连接
Trace-backed Memory 时使用的项目级显式配置。

## 连接 Pi

在需要使用 memory 的仓库中创建 `.mcp.json`，并替换三个绝对路径：

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

Windows JSON 路径使用正斜杠。从该仓库启动 Pi，批准可信项目资源，再运行 `/mcp`
检查客户端与 server。adapter 还支持：

```text
/mcp tools
/mcp reconnect
/mcp reconnect trace-backed-memory
```

当前两阶段 Gate 生命周期要求 `keep-alive`：

```text
tbm_capabilities / tbm_health
-> tbm_prepare_memory
-> 只在 system_allowed_memory_ids 中作决定
-> tbm_finalize_memory
-> 只使用 finalized.snippet
-> 执行并测量
-> tbm_complete_run
```

若不再执行，应在 finalize 前调用 `tbm_cancel_run`。不得在 prepare 与 finalize 或
cancel 之间重启 MCP process。

Pi 的 extension 与 trust model 见
[Pi 官方文档](https://pi.dev/docs/latest)和
[安全指南](https://pi.dev/docs/latest/security)。本教程使用
[Pi package catalog 中列出的 `pi-mcp-adapter` MCP 客户端](https://pi.dev/packages/pi-mcp-adapter?name=mcp)；
它的 setup、配置、命令与 lifecycle 行为以
[adapter 上游项目](https://github.com/nicobailon/pi-mcp-adapter)为准。它是可执行第三方代码，
不属于 Pi core，也不属于 Trace-backed Memory。

其他客户端：[Codex](codex.zh-CN.md) | [Claude Code](claude-code.zh-CN.md)
