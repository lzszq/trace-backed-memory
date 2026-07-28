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

如尚未安装 Pi，先安装 Pi，再从 Pi package catalog 安装 adapter：

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi install npm:pi-mcp-adapter
```

接受安装前应检查 package 当前版本并审查源码，然后重启 Pi。首次打开 `/mcp` 时确认
提示；若尚无标准 MCP 配置文件，运行 `/mcp setup` 创建。下面是最终生成的项目级显式配置。

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
[安全指南](https://pi.dev/docs/latest/security)。MCP 客户端的安装、setup、配置、
命令与 lifecycle 选项来自
[Pi package catalog 中的 `pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter?name=mcp)。
catalog 将它标记为可执行第三方代码，并非 Pi core 的一部分。

其他客户端：[Codex](codex.zh-CN.md) | [Claude Code](claude-code.zh-CN.md)
