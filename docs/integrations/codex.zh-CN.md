# Codex 集成

[English](codex.md) | **简体中文**

仓库现在提供四层 Codex 接口，同时不削弱当前进程内 Gate 边界：

1. 根目录与嵌套 `AGENTS.md` 映射不变量、验证、Schema 和适配器边界。
2. 仓库本地技能分别指导维护者与运行时调用方。
3. `LocalAgentMemory` 和 `tbm.agent.v1` 为宿主应用提供聚焦、带版本的
   Python/JSON 边界。
4. `tbm-mcp` 以长驻本地 STDIO MCP server 暴露同一套 runtime-only 生命周期。

## 贡献者使用

Codex 应先读取根 `AGENTS.md`。修改仓库时选择
`maintain-trace-backed-memory`，接入运行时则选择
`use-trace-backed-memory`。技能只按任务加载必要引用。

## 安装本地 MCP profile

在 Codex 用来启动 server 的 Python 环境中安装可选 MCP 依赖。

Windows PowerShell：

```powershell
py -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm, .codex
Get-Command tbm-mcp
```

macOS 或 Linux：

```bash
python3 -m pip install -e '.[mcp]'
mkdir -p .tbm .codex
command -v tbm-mcp
```

## 连接 Codex

Codex Desktop 与 Codex CLI 共用 project-scoped `.codex/config.toml`。替换
checkout 路径后增加该文件；Codex 只会为受信任项目加载项目配置。

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

Windows TOML 路径使用正斜杠，例如 `C:/Users/name/source/repository`。若 Codex
Desktop 找不到 `tbm-mcp`，把 `command` 改为 `Get-Command` 或 `command -v`
输出的绝对路径。打开或信任仓库并重启 Codex Desktop，或从配置的 `cwd` 启动新的
Codex CLI session。

其他客户端：[Claude Code](claude-code.zh-CN.md) | [Pi](pi.zh-CN.md)

`--repo-path` 必填，并固定 Git provenance root。必须且只能选择一种存储：

- `--memory`：显式使用不持久化的进程内存储；
- `--sqlite PATH`：持久化本地记录；相对路径在配置的 repository 下解析；
- `--postgres-env ENV_NAME`：从环境变量读取 PostgreSQL conninfo，避免把
  secret 写入项目配置。

`--tenant VALUE` 为所有请求固定可选的 declared-scope 值。在 snapshot
version 2 中，这只是适用性元数据，不是授权边界。

## 运行时顺序

MCP 进程在 Codex 完成整个生命周期期间保持存活：

```text
tbm_capabilities / tbm_health
-> tbm_prepare_memory
-> 只在 system_allowed_memory_ids 中作决定
-> tbm_finalize_memory
-> 只把 finalized.snippet 提供给当前任务
-> 执行并测量
-> tbm_complete_run
```

若不再执行，应调用 `tbm_cancel_run`，而不是 finalize。server 从固定 checkout
root 派生 repository、commit、branch、dirty state 和完整 Git ancestry，不接受
调用方伪造 Git provenance，也不暴露 curation、verification、publication、
activation、原始 Store、snapshot 或 migration 工具。

STDIO 输入采用 strict JSON，每帧拒绝重复 key 和非有限数字，并限制为
8 MiB、100,000 个 JSON nodes、depth 100；工具请求模型拒绝未知字段。
Agent-facing failure 使用有界 `tbm.agent.v1` error envelope。

## 当前边界

server 必须长驻，因为 pending Gate request 与 finalized replay tombstone 仍为
进程内状态。SQLite 和 PostgreSQL 会持久化 Trace、finalized usage decision
与 measured completion，但 server 重启会放弃所有尚未 finalized 的 request。
客户端重启后必须重新 prepare，不得重建私有 request token。request ID 是
opaque value，并带有每个 Store 新生成的 128-bit namespace，因此 stale handle
不能在重启后 finalize 或 cancel 新 prepared request。

远程 Streamable HTTP、OAuth、canonical repository 授权、durable
idempotency/expiry 与跨进程 replay 仍需要产品交付计划中的统一 schema
version 3 工作。

## 一致性验证

接入其他宿主前：

- 检查 `tbm_capabilities`；
- direct JSON 集成使用打包的 `tbm.agent.v1` Schema 校验；
- 覆盖无记忆、相关记忆、System Gate 阻断、非法 decision、cancel、进程重启、
  精确重试与 measured completion；
- 避免原生或其他 memory system 把 TBM snippet 再次固化成独立验证知识。
