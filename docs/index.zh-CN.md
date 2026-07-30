# 文档索引

[English](index.md) | **简体中文**

本页是 Trace-backed Memory 的文档地图。README 用于快速了解项目，以下文档定义工程契约。

## 产品与架构

- [当前能力状态](status/current-capability-matrix.zh-CN.md)
- [机器可读能力状态](status/current-capabilities.json)
- [产品定义与当前能力](product.md)
- [详细 API 与运维参考](reference.zh-CN.md)
- [参考架构](architecture.zh-CN.md)
- [记忆使用策略](usage-policy.zh-CN.md)
- [产品交付计划](product-program.zh-CN.md)
- 已接受架构决策：
  [ADR-0001](adr/0001-v2-compatibility-durable-v3-cutover.zh-CN.md)、
  [ADR-0002](adr/0002-unified-v3-database-bundles.zh-CN.md)、
  [ADR-0003](adr/0003-transport-identity-ownership.zh-CN.md)、
  [ADR-0004](adr/0004-canonical-resource-manifest.zh-CN.md) 与
  [ADR-0005](adr/0005-public-internal-package-boundaries.zh-CN.md)

## Agent 集成

- [本地 Agent 协议 `tbm.agent.v1`](protocols/agent-v1.zh-CN.md)
- [本地 HTTP 服务与 Python/TypeScript SDK](protocols/agent-http-v1.zh-CN.md)
- [Durable HTTP profile](protocols/durable-http-v1.zh-CN.md)
- [Durable MCP profile](protocols/durable-mcp-v1.zh-CN.md)
- [可重启本地 durable daemon](protocols/local-daemon-v1.zh-CN.md)
- [Node.js TypeScript SDK 包](../packages/typescript-sdk/README.md)
- [规范本地 Agent OpenAPI 3.1](../schemas/agent-http-v1.openapi.json)
- [授权 v3 契约](protocols/authorization-v3.zh-CN.md)
- [实体注册表 v3 契约](protocols/entity-registry-v3.zh-CN.md)
- [认证 retrieval service 边界](protocols/authenticated-service-v3.zh-CN.md)
- [认证 durable Gate preparation](protocols/authenticated-gate-service-v3.zh-CN.md)
- [GateSession recovery worker](protocols/gate-recovery-worker-v3.zh-CN.md)
- [SQLite 与 PostgreSQL Gate evidence v3](protocols/sqlite-gate-evidence-v3.zh-CN.md)
- [Append-only 审计与恢复 v3](protocols/audit-recovery-v3.zh-CN.md)
- [结构化 regression evidence v3](protocols/evidence-v3.zh-CN.md)
- [FixEvidence v3](protocols/fix-evidence-v3.zh-CN.md)
- [MemoryRevision proposal 与 publication event v3](protocols/memory-revision-v3.zh-CN.md)
- [SQLite MemoryRevision proposal ledger v3](protocols/sqlite-memory-revision-v3.zh-CN.md)
- [PostgreSQL MemoryRevision proposal ledger v3](protocols/postgres-memory-revision-v3.zh-CN.md)
- [SQLite MemoryRevision publication authority v3](protocols/sqlite-memory-publication-v3.zh-CN.md)
- [PostgreSQL MemoryRevision publication authority v3](protocols/postgres-memory-publication-v3.zh-CN.md)
- [已认证检索准备 v3](protocols/retrieval-preparation-v3.zh-CN.md)
- [Durable 检索准备 v3](protocols/durable-retrieval-preparation-v3.zh-CN.md)
- [托管索引 bundle v3](protocols/managed-index-v3.zh-CN.md)
- [可回放 RetrievalSnapshot v3](protocols/retrieval-snapshot-v3.zh-CN.md)
- [System 与 Semantic Gate evaluation v3](protocols/gate-evaluation-v3.zh-CN.md)
- [Semantic Gate artifact 绑定 v3](protocols/semantic-gate-artifact-v3.zh-CN.md)
- [已认证 Semantic Gate 服务 v3](protocols/semantic-gate-service-v3.zh-CN.md)
- [Durable Semantic Gate session 组合 v3](protocols/durable-semantic-gate-v3.zh-CN.md)
- [Durable finalization 组合 v3](protocols/durable-finalization-v3.zh-CN.md)
- [Durable execution 组合 v3](protocols/durable-execution-v3.zh-CN.md)
- [已认证 durable Agent 组合 v3](protocols/durable-agent-v3.zh-CN.md)
- [Durable Agent wire 边界 v1](protocols/durable-agent-wire-v1.zh-CN.md)
- [统一 SQLite v3 bundle](protocols/sqlite-bundle-v3.zh-CN.md)
- [UsageDecision v3](protocols/usage-decision-v3.zh-CN.md)
- [SQLite Semantic Gate artifact 仓库 v3](protocols/sqlite-semantic-gate-artifact-v3.zh-CN.md)
- [PostgreSQL Semantic Gate artifact 仓库 v3](protocols/postgres-semantic-gate-artifact-v3.zh-CN.md)
- [SQLite Semantic Gate attempt ledger v3](protocols/sqlite-semantic-gate-v3.zh-CN.md)
- [PostgreSQL Semantic Gate attempt ledger v3](protocols/postgres-semantic-gate-v3.zh-CN.md)
- [运行结果与归因 v3](protocols/outcome-v3.zh-CN.md)
- [SQLite RunOutcome 完成事务 v3](protocols/sqlite-outcome-v3.zh-CN.md)
- [SQLite OutcomeAttribution ledger v3](protocols/sqlite-outcome-attribution-v3.zh-CN.md)
- [PostgreSQL RunOutcome 完成事务 v3](protocols/postgres-outcome-v3.zh-CN.md)
- [PostgreSQL OutcomeAttribution ledger v3](protocols/postgres-outcome-attribution-v3.zh-CN.md)
- [Completion outbox 契约与 SQLite/PostgreSQL authority v3](protocols/completion-outbox-v3.zh-CN.md)
- [Durable GateSession v3 领域契约](protocols/gate-session-v3.zh-CN.md)
- [内容寻址重放与可移植导出契约 v3](protocols/replay-v3.zh-CN.md)
- [已认证加密 Artifact Authority v3](protocols/artifact-authority-v3.zh-CN.md)
- [已核验 ActivatedRevision source v3](protocols/activated-revision-source-v3.zh-CN.md)
- [Codex 集成](integrations/codex.zh-CN.md)
- [Claude Code 集成](integrations/claude-code.zh-CN.md)
- [Pi 集成](integrations/pi.zh-CN.md)
- 仓库技能：`.agents/skills/maintain-trace-backed-memory/` 与
  `.agents/skills/use-trace-backed-memory/`

## 开发与运维

- [开发与验证](development.zh-CN.md)
- [生成的 packaged-resource 索引](resources.zh-CN.md)
- [规范 resource manifest](../resources/manifest.json)
- [Snapshot v3 迁移预检](migrations/snapshot-v3-preflight.zh-CN.md)
- [Version-3 迁移 bundle 与隔离 staging](migrations/v3-staging-bundles.zh-CN.md)
- `schemas/sqlite.sql`：受支持的本地 SQL 形态
- `schemas/postgres.sql` 与 `schemas/postgres-v1-to-v2.sql`：PostgreSQL
- `tests/verify_distribution.py`：安装资源逐字节验证

## 兼容性边界

当前格式为 snapshot version 2、SQLite schema version 1、PostgreSQL schema
version 2 和 Agent 协议 `tbm.agent.v1`。可选的默认 `tbm-mcp` 命令是该协议的长驻
本地 STDIO transport，不是新的持久化版本；其 pending gate request 仍为进程内
状态。与持久化实现无关的 `tbm.gate-session.v3` 生命周期契约及 opt-in、
side-by-side SQLite 和隔离 PostgreSQL revision repository 已经发布。opt-in
preparation、Semantic Gate、completion 与 recovery service/worker 已经使用它们，
但默认兼容 Store/MCP lifecycle 尚未使用；显式 durable HTTP 与可信本地 MCP
profile 会选择统一 version-3 authority graph。与存储实现无关的授权
v3 policy/evaluator 契约已定义 canonical repository、精确租户别名、认证身份
位置、role binding 与关联 decision。认证 retrieval service kernel 现在会在
retrieval callback 前持久化并复查这些 decision；带 peer authentication 的共享服务
接入仍待完成。默认 loopback-only、bearer-authenticated HTTP profile 与类型化
Python client 会暴露 active version-2 lifecycle，并与默认 STDIO MCP 共用
dispatcher。storage-neutral、content-addressed
FixEvidence 与结构化 regression evidence 契约已经发布，并提供严格的跨记录
MemoryRevision preflight 及 opt-in、隔离的 SQLite/PostgreSQL proposal ledger。
active v2 record/adapter 尚未使用这些 ledger；proposal 持久化不代表 approval 或
activation。
内容寻址 retrieval policy 与可选的存储中立 preparation kernel 现在会先授权，再读取
已核验 activated revision，执行 classification/applicability/eval-leakage/Git-ancestry
过滤，对 versioned adapter 分数做确定性融合，并生成配对的 RetrievalSnapshot/System
Gate evidence，最后复查 head/policy。opt-in durable Semantic Gate 组合现在会让
prepared GateSession 经 `AWAITING_DECISION` 推进到 `DECIDED`，保存精确
prompt/response 字节和完整、单调收窄的 attempt chain，并提供明确 retry/recovery
语义。opt-in durable finalization 组合现在会复查当前 authorization event、active
revision head 与 policy，确定性渲染最终允许集合，原子保留精确 UsageDecision、
injection 与完整八组件 replay bundle，并通过 CAS 发布 `FINALIZED`；SQLite 与
PostgreSQL 具备 caller-transaction 对等性。`DurableExecutionService` 随后会在
`FINALIZED -> EXECUTING` 前核验该精确保留 injection，支持已认证、精确 revision
的 resume 与 abandonment，通过每次调用的可信 authenticator 认证 outcome
evaluator，并以 SQLite/PostgreSQL 对等性组合原子
`RunOutcome + COMPLETED + completion outbox` 发布。托管生产索引、受保护内容的
  加密 finalization、持久 transition-event linkage、默认 adapter cutover、
  CLI durable 选择与共享服务 transport 仍待完成。
`AuthenticatedDurableAgentMemory` 现在会在一个 adapter-neutral lifecycle
后面组合上述 opt-in 阶段。它从已保留 RetrievalSnapshot authorization linkage
重建原始 retrieval scope，拒绝错配的 service graph，增加已授权的精确版本取消，
并为 `PREPARED` 之后的每次 GateSession 修改取得新的 transition decision。只要同一组
  authority 与当前可信 context 仍可用，该 facade 就可以跨实例续接。默认兼容
  Agent/MCP/HTTP adapter 与普通 CLI 不构造它；显式 durable HTTP 与可信本地 MCP
  profile 会通过共享 runtime factory 构造它，Node.js
  `DurableAgentHTTPClient` 则选择该显式 HTTP profile。显式 `tbmd local` profile
会持有一个带锁的 SQLite v3 graph，并让本地 MCP、loopback HTTP、有界 recovery
与 outbox delivery 共用其 dispatcher；它不会改变默认兼容 adapter。可选严格
durable wire dispatcher 会映射该 facade，但不接受调用方 identity 字段，也不保存
进程内 handle。
opt-in SQLite 与隔离 PostgreSQL RunOutcome authority 现在都可以用一份
content-addressed outcome 原子完成 executing GateSession。隔离 SQLite
与 PostgreSQL OutcomeAttribution ledger 会用精确 durable outcome/session
linkage 持久化多条独立核验的 claim。opt-in SQLite 与隔离 PostgreSQL
Completion Outbox authority 会在同一 transaction 原子增加一条 immutable
completion event 与 append-only leased delivery chain。opt-in durable execution
组合现已围绕这些 authority 提供可信 evaluator authenticator 与精确保留 injection
replay，且显式 durable transport 会选择它。Protected Artifact
attestation/integration 与 shared-service worker deployment 仍待完成。
storage-neutral approval/activation 契约与隔离 SQLite/PostgreSQL publication
authority 已经发布。opt-in、已认证的 SQLite 与隔离 PostgreSQL Artifact Authority
会通过调用方 provider 加密精确字节、授权每次读写，并执行读取时 retention/legal
hold。object-storage 对等实现、物理清除/密钥销毁、active-v2 projection 与更广泛
service integration 仍属于统一推进的 schema version 3 计划。
与存储实现无关的
`tbm.replay.v3` artifact 与 replay manifest 契约及 opt-in 隔离 SQLite/PostgreSQL
immutable 字节/descriptor 账本已经发布，但 active adapter 尚不使用它们，且它们不提供
授权、retention、encryption 或 GateSession authority。只读 v3 迁移预检和不可激活的 staging
bundle 已经实现，但它们不能激活 memory，也不能作为 version-3 runtime state
加载。
