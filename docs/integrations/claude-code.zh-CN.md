# Claude Code 集成

[English](claude-code.md) | **简体中文**

Claude Code 可以把 Trace-backed Memory 作为本地 STDIO MCP server 启动。以下配置
显式指定 executable、repository root 与 SQLite 路径，避免 provenance 和存储位置
受 shell 工作目录影响。

## 安装服务

在 Trace-backed Memory checkout 中运行：

```powershell
py -m pip install -e ".[mcp]"
(Get-Command tbm-mcp).Source
```

macOS 或 Linux 使用 `python3`，并通过 `command -v tbm-mcp` 查找 executable。

## 连接 Claude Code

从需要使用 memory 的仓库运行以下命令，并替换三个绝对路径：

```bash
claude mcp add --transport stdio --scope project trace-backed-memory -- \
  /absolute/path/to/tbm-mcp \
  --repo-path /absolute/path/to/repository \
  --sqlite /absolute/path/to/repository/.tbm/memory.sqlite3
```

PowerShell 可把同一命令写成单行；Windows 路径使用正斜杠。Claude Code 会写入项目级
`.mcp.json`，批准项目使用该 server 前应先审查这个文件。

验证注册结果：

```text
claude mcp get trace-backed-memory
claude mcp list
```

从仓库中启动 Claude Code，再打开 `/mcp` 检查状态，并按提示认证或重连。

## 必须遵守的运行时顺序

客户端必须依次调用：

```text
tbm_capabilities / tbm_health
-> tbm_prepare_memory
-> 只在 system_allowed_memory_ids 中作决定
-> tbm_finalize_memory
-> 只使用 finalized.snippet
-> 执行并测量
-> tbm_complete_run
```

若不再执行，应在 finalize 前调用 `tbm_cancel_run`。从 prepare 到 finalize 或 cancel
必须保持同一个 server process 存活；当前 schema 的 pending request 是进程内状态。

`tbm-mcp` 从 `--repo-path` 派生 Git provenance，拒绝调用方提供 provenance，也不暴露
review、verification、publication、activation、snapshot 或 migration 操作。项目 MCP
配置能够执行代码；只有当所有协作者都应信任其 command 与 arguments 时才提交该文件。

配置语法与 scope 行为见
[Claude Code 官方 MCP 指南](https://code.claude.com/docs/en/mcp)。

其他客户端：[Codex](codex.zh-CN.md) | [Pi](pi.zh-CN.md)
