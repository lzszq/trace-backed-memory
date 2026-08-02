# Codex 集成

[English](codex.md) | **简体中文**

仓库现在提供六层边界清晰的 Codex 接口：

1. 根目录与嵌套 `AGENTS.md` 映射不变量、验证、Schema 和适配器边界。
2. 仓库本地技能分别指导维护者与运行时调用方。
3. `LocalAgentMemory` 和 `tbm.agent.v1` 为宿主应用提供聚焦、带版本的
   Python/JSON 边界。
4. 默认 `tbm-mcp` 以长驻本地 STDIO MCP server 暴露同一套兼容生命周期。
5. 显式 `tbm-mcp --profile durable-v3` 暴露可跨重启续接的 durable Agent
   生命周期，并把可信 identity 保留在 tool JSON 之外。
6. `tbm.codex-ingestion.v1` 提供 opt-in 的结构化 Hook/App Server event capture，
   把它们转换成有序 TraceEvent evidence，但不选择默认 transport。

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

Codex Desktop、Codex CLI 与 Codex IDE 扩展共享 MCP 配置。替换 checkout 路径后
增加以下 project-scoped `.codex/config.toml`；Codex 只会为受信任项目加载项目配置。

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
输出的绝对路径。加载此文件前先打开或信任仓库。

### Desktop

打开 **Settings > MCP servers** 检查该 server，选择 **Restart**，再在输入区使用
`/mcp` 确认 `trace_backed_memory` 已连接。

### CLI

从配置的 `cwd` 启动新 session，并使用以下只读检查：

```bash
codex mcp get trace_backed_memory --json
codex mcp list
```

在 terminal UI 内使用 `/mcp` 查看 active tools。也可执行全局配置命令
`codex mcp add trace_backed_memory -- tbm-mcp --repo-path
/absolute/path/to/repository --sqlite .tbm/memory.sqlite3`；如果 server 只属于当前仓库，
应优先使用项目文件。

### IDE 扩展

打开 gear menu，选择 **MCP servers**，检查共享配置，再选择
**Restart extension**。这里同样遵守受信任项目规则。

其他客户端：[Claude Code](claude-code.zh-CN.md) | [Pi](pi.zh-CN.md)

`--repo-path` 必填，并固定 Git provenance root。必须且只能选择一种存储：

- `--memory`：显式使用不持久化的进程内存储；
- `--sqlite PATH`：持久化本地记录；相对路径在配置的 repository 下解析；
- `--postgres-env ENV_NAME`：从环境变量读取 PostgreSQL conninfo，避免把
  secret 写入项目配置。

`--tenant VALUE` 为所有请求固定可选的 declared-scope 值。在 snapshot
version 2 中，这只是适用性元数据，不是授权边界。

## 运行时顺序

默认兼容 MCP 进程在 Codex 完成整个生命周期期间保持存活：

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

## Opt-in Hook 与 App Server evidence

只应在 owner 控制且能认证本地 Codex 来源的 adapter 内使用
[Codex 摄取协议](../protocols/codex-ingestion-v1.zh-CN.md)。它把计划中的 12 个 session、
prompt、tool、permission、subagent、compaction、stop、diff 与 final-response 事实映射成
有序 TraceEvent draft。可信 binding 固定 Trace、run、lineage 与允许的 Hook session/App
Server thread；来源 JSON 不能选择 scope 或 ledger authorization。

每个被接受的原始帧都会作为精确受保护 Artifact 保留，event ledger 只接收其 descriptor。
adapter 会拒绝 transcript-only 事实源、畸形或超限 JSON、不匹配的 lifecycle transition、
有歧义的 Hook permission、未绑定 active item 的 App Server approval，以及不可信 clock
drift。permission result 绑定精确 approval frame，但绝不会授权 append。

这是 Python integration boundary，不是自动 Hook registration 或 App Server process manager。
默认兼容和 durable MCP/HTTP/SDK profile 都不会调用它。后续 lifecycle 或 ledger 校验拒绝
event batch 时，一个合法受保护 Artifact 可能作为 orphan evidence 留存；它不是 Trace 事实，
仍受配置的 retention policy 管理。可信 operator 可以把该 descriptor 解析为 opt-in
[Artifact retention 协调器](../protocols/artifact-retention-v1.zh-CN.md)的显式 target；
capture rejection 本身绝不授权擦除。

## 可跨重启的 durable profile

当 GateSession 必须跨 Codex 或 MCP 进程重启续接时，使用显式
`tbm-mcp --profile durable-v3`。它要求 operator 持有的 application factory 提供统一
authority graph，以及固定的 service、Semantic Gate provider 与 evaluator identity。
factory、一次性 SQLite 初始化、PostgreSQL 选择、content exposure flag 和精确工具顺序见
[durable MCP profile 指南](../protocols/durable-mcp-v1.zh-CN.md)。

这是可信本地 STDIO profile，不是带 peer authentication 的共享服务。重启后用已持久化的
`session_id` 调用 `tbm_durable_get_session`，并从返回的精确版本继续。默认兼容 profile
仍把 pending Gate request 与 finalized replay tombstone 保存在进程内；其 client
重启后必须重新 prepare，也不得重建私有 request token。

如需让一个进程同时持有 loopback HTTP、recovery 与 outbox delivery，先初始化一次
[本地 daemon](../protocols/local-daemon-v1.zh-CN.md)，再替换项目 MCP command：

```text
tbmd init --state-dir .tbm
```

```toml
[mcp_servers.trace_backed_memory]
command = "tbmd"
args = ["local", "--state-dir", ".tbm"]
```

daemon 使用同一套 durable tool 与精确版本续接规则。

远程 Streamable HTTP MCP、OAuth 与不可信多租户服务仍不属于该本地 profile。

## 一致性验证

接入其他宿主前：

- 检查 `tbm_capabilities`；
- direct JSON 集成使用打包的 `tbm.agent.v1` Schema 校验；
- 覆盖无记忆、相关记忆、System Gate 阻断、非法 decision、cancel、进程重启、
  精确重试与 measured completion；
- 避免原生或其他 memory system 把 TBM snippet 再次固化成独立验证知识。
