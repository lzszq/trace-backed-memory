# 文档索引

[English](index.md) | **简体中文**

本页是 Trace-backed Memory 的文档地图。README 用于快速了解项目，以下文档定义工程契约。

## 产品与架构

- [产品定义与当前能力](product.md)
- [参考架构](architecture.zh-CN.md)
- [记忆使用策略](usage-policy.zh-CN.md)
- [产品交付计划](product-program.zh-CN.md)

## Agent 集成

- [本地 Agent 协议 `tbm.agent.v1`](protocols/agent-v1.zh-CN.md)
- [授权 v3 契约](protocols/authorization-v3.zh-CN.md)
- [结构化 regression evidence v3](protocols/evidence-v3.zh-CN.md)
- [不可变 MemoryRevision v3](protocols/memory-revision-v3.zh-CN.md)
- [可回放 RetrievalSnapshot v3](protocols/retrieval-snapshot-v3.zh-CN.md)
- [System 与 Semantic Gate evaluation v3](protocols/gate-evaluation-v3.zh-CN.md)
- [Durable GateSession v3 领域契约](protocols/gate-session-v3.zh-CN.md)
- [内容寻址重放契约 v3](protocols/replay-v3.zh-CN.md)
- [Codex 集成](integrations/codex.zh-CN.md)
- 仓库技能：`.agents/skills/maintain-trace-backed-memory/` 与
  `.agents/skills/use-trace-backed-memory/`

## 开发与运维

- [开发与验证](development.zh-CN.md)
- [Snapshot v3 迁移预检](migrations/snapshot-v3-preflight.zh-CN.md)
- [Version-3 迁移 bundle 与隔离 staging](migrations/v3-staging-bundles.zh-CN.md)
- `schemas/sqlite.sql`：受支持的本地 SQL 形态
- `schemas/postgres.sql` 与 `schemas/postgres-v1-to-v2.sql`：PostgreSQL
- `tests/verify_distribution.py`：安装资源逐字节验证

## 兼容性边界

当前格式为 snapshot version 2、SQLite schema version 1、PostgreSQL schema
version 2 和 Agent 协议 `tbm.agent.v1`。可选 `tbm-mcp` 命令是该协议的长驻
本地 STDIO transport，不是新的持久化版本。pending gate request 仍为进程内
状态。与持久化实现无关的 `tbm.gate-session.v3` 生命周期契约及 opt-in、
side-by-side SQLite 和隔离 PostgreSQL revision repository 已经发布，但 active
Store/MCP、worker 与 service integration 尚未使用它们。与存储实现无关的授权
v3 policy/evaluator 契约已定义 canonical repository、精确租户别名、认证身份
位置、role binding 与关联 decision，但尚未接入 active adapter。storage-neutral、
content-addressed 结构化 regression evidence 契约已经发布，但 active v2
record/adapter 尚未使用它；proposal-only immutable MemoryRevision 契约也已发布。
内容寻址 RetrievalSnapshot 契约会记录精确的已授权排序输入/结果、索引版本、
分数、哈希与截断原因；不可变 System/Semantic Gate 记录以单调缩小规则绑定确定性
策略与模型 attempt provenance。active retriever/gate/GateSession repository
尚不产生它们。
approval、activation、persistence 与 active integration 仍属于统一推进的 schema
version 3 计划。与存储实现无关的
`tbm.replay.v3` artifact 与 replay manifest 契约及 opt-in 隔离 SQLite immutable
字节/descriptor 账本已经发布，但 active adapter 尚不使用它，且它不提供授权、
retention、encryption 或 GateSession authority。只读 v3 迁移预检和不可激活的 staging
bundle 已经实现，但它们不能激活 memory，也不能作为 version-3 runtime state
加载。
