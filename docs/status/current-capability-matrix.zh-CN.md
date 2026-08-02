# 当前能力状态

[English](current-capability-matrix.md) | **简体中文**
机器可读账本：
[`current-capabilities.json`](current-capabilities.json)

本账本用于简洁、明确地说明某项能力是否已经进入默认产品路径。详细契约仍见
[产品文档](../product.md)；历史交付阶段不能作为能力已经 active 的证据。

## 状态规则

| 状态 | 含义 |
|---|---|
| `active` | 用户可以运行的受支持默认路径，或明确记录的兼容路径。 |
| `opt-in` | 已有可执行实现和聚焦测试，但默认 transport 或存储尚未选择。 |
| `contract-only` | 只有契约、预检、staging 或不完整 adapter，没有用户级生命周期。 |
| `planned` | 仓库尚未提供所要求的可运行能力。 |

未提交工作、只有设计的模块、没有被产品 transport 选择的直接 Python 组合，以及
历史 phase 描述，都不能提升状态。

## 当前矩阵

| ID | 领域 | 能力 | 状态 | 当前边界 |
|---|---|---|---:|---|
| `core.gated-memory-v2` | 核心 | Trace/证据采集与 v2 门控记忆生命周期 | `active` | 原始 Trace 仍是证据；System Gate 始终具有最终约束力。 |
| `trace.ordered-events-v1` | Full Trace evidence | 有序 typed TraceEvent 协议与有界 ledger adapter | `opt-in` | sealed 12-type TraceEvent registry 会绑定 sequence、精确 time、仅含 descriptor 的 artifact、tool/permission correlation 与 parent/subagent provenance；1-100 条 batch 通过现有 ledger port 追加。opt-in Codex 摄取 adapter 会选择它，兼容 Trace 与默认 transport 不会。 |
| `git.observations-v1` | Git evidence | 七观察点 Git Observation 协议与原子 capture adapter | `opt-in` | checkout/ref/commit/受保护 diff/ancestry/object-availability/shallow observation 保持兼容 capture 类型、保存 runner/algorithm version，并保留 missing-object uncertainty。默认 transport 与 Codex 摄取不会选择这套独立 Git 协议。 |
| `git.graph-projection-v1` | Git evidence | 确定性 Git graph reducer 与 immutable projection | `opt-in` | replay 完整 access-bound observation stream，生成 commit/parent/ancestry/missing-object view、exact source/fix/verification edge 与 fail-closed PR source anchor。shallow 或不可用 object 强制 ancestry 为 unknown。active profile 尚未选择或持久化本 read model。 |
| `effect.receipts-v1` | 外部副作用 | 有序 provider attempt 与 receipt 生命周期 | `opt-in` | sealed 12-type registry、access-bound authorization link、可信 provider registration、确定性 attempt/request 绑定、精确 receipt Artifact、unknown-result reconciliation、有界 retry/dead-letter 与独立 compensation child effect 已可执行并有测试。现有 completion outbox 与默认 transport 尚未选择它。 |
| `artifact.retention-erasure-v1` | 受保护内容 | 受治理 retention、crypto-erasure、index purge 与 tombstone | `opt-in` | 受保护 content-addressed manifest 绑定精确 target、hold epoch、key closure、不可变 index successor 与 replay impact。intent 位于 effect 之前；精确且经独立核验的 KMS receipt 位于 replay-partial/erasure/tombstone fact 之前；恢复绝不 blind retry destruction。最终 index-head/终态-ledger 竞态仍需 durable publication fence；默认 transport 不选择协调器。 |
| `compat.agent-v1` | 兼容路径 | `tbm.agent.v1`、`LocalAgentMemory`、CLI | `active` | pending gate request 保存在进程内。 |
| `transport.local-mcp-v1` | 本地 transport | STDIO MCP | `active` | 选择 version-2 兼容生命周期。 |
| `transport.loopback-http-v1` | 本地 transport | Loopback HTTP | `active` | 选择 version-2 兼容生命周期。 |
| `sdk.python-v1` | SDK | Python 同步/异步 client | `active` | 面向本地 `tbm.agent.v1` HTTP profile。 |
| `sdk.typescript-v1` | SDK | TypeScript client | `active` | 面向本地 `tbm.agent.v1` HTTP profile。 |
| `distribution.strict-resources` | 分发 | 严格 packaged-resource allowlist | `active` | 规范资源与安装资源执行逐字节核验。 |
| `governance.authority-registry-v1` | 治理 | 持久化 authority 角色登记与仓库守卫 | `active` | 每个 SQLite/PostgreSQL v3 持久化模块都登记为 ledger、projection、compatibility migration 或 bundle coordinator；未登记 authority 会使仓库验证失败。 |
| `identity.authorization-v3` | 身份 | Entity registry 与 authorization authority v3 | `opt-in` | 仅可信直接 Python context；默认 transport 尚无身份。 |
| `session.gate-session-v3` | Durable session | SQLite/PostgreSQL GateSession 与恢复 | `opt-in` | 显式 durable runtime 现在会把每个 GateSession revision 追加为 canonical event，并同步核验现有 revision projection；它不是 active 兼容 Store 生命周期。 |
| `session.gate-session-events-v1` | Full Persistence 生命周期 | GateSession 生命周期 event adapter 与 current-state reducer | `opt-in` | 显式 durable SQLite/PostgreSQL runtime 会选择 event-first adapter，包含 baseline import 与精确 projection rebuild；retrieval/Gate/replay/outcome/effect view 和默认兼容生命周期仍由 authority 支撑。 |
| `replay.gate-evidence-export-v3` | Full Persistence replay | 从 Gate evidence 事件与 Artifact 重建现有 replay export | `opt-in` | Finalized 显式 durable SQLite/PostgreSQL session 会从只含 descriptor 的 Gate evidence stream 与精确 Artifact bytes 重建 `tbm.replay-export.v3`；规范 JSON 与 `export_sha256` 必须等于当前 replay-authority 路径。它不会完成 Gate evidence crash matrix 或聚合 cutover。 |
| `projection.outcome-effect-v1` | Full Persistence projection | RunOutcome、OutcomeAttribution、EffectQueue、delivery、dead-letter 与 compensation reducer | `active` | `tbmd local` 与独立 SQLite durable HTTP/MCP 使用同一个命令事务完成校验、event append、同步 rebuild/read-back、response 构造与 commit。原始 HTTP、MCP、Python 同步/异步与 TypeScript 共用一个事件/projection golden。hard-kill matrix 覆盖全部 11 个命令 commit 点的 commit 前与 commit 后/response 前；旧 delivery 仍是 at-least-once，独立 PostgreSQL 切换尚未完成。 |
| `agent.durable-lifecycle-v3` | Durable lifecycle | 从 prepare 到 completion/replay 的 facade | `opt-in` | adapter-neutral 组合，默认 transport 尚未选择。 |
| `agent.durable-wire-v1` | Durable wire | `tbm.durable-agent-wire.v1` | `opt-in` | 严格 dispatcher；自身不认证 peer。 |
| `memory.structured-evidence-v3` | 证据 | 结构化 regression evidence | `opt-in` | active v2 publication 仍使用兼容模型。 |
| `memory.failure-case-events-v1` | Engineering Memory projection | Event-derived FailureCase 与 structured-evidence eligibility | `contract-only` | 精确 TraceEvent-linked extractor proposal 始终保持 candidate，legacy boolean 保持 `legacy_unstructured`。Draft replacement 仍可在替换 evidence payload 时保留内部 producer capability，因此安全验收以及 MemoryCatalog/default-profile wiring 均未关闭。 |
| `memory.catalog-events-v1` | Engineering Memory projection | Event-rebuilt MemoryCatalog 与正式 ActivatedMemoryHead | `opt-in` | 精确 stored publication/evidence/authorization provenance 与 reducer trust configuration 使用同一 SQLite/PostgreSQL ledger 路径。跨页 rebuild 当前会重复边界事件，排除 `internal` 的 filter 还可能得到空的 partial snapshot；F4-03/F4-04 验收、F4-01/F4-02 producer 验收、F4-07 与默认 compatibility cutover 均未关闭。 |
| `policy.active-bundle-events-v1` | Engineering Memory policy | Event-derived active policy bundle 与 head | `opt-in` | 精确 global create/approve authorization 与独立 actor 通过共用 SQLite/PostgreSQL ledger 路径激活内容寻址的八维 policy bundle。它可显式作为既有 retrieval-policy provider；默认消费以及下游 trust-tier/renderer/Semantic enforcement 尚未 cut over。 |
| `memory.revision-publication-v3` | Publication | 不可变 revision authority | `opt-in` | active v2 publication 仍使用兼容模型。 |
| `retrieval.activated-revision-v3` | Retrieval | ActivatedRevision source | `opt-in` | 默认 adapter 检索兼容记录；显式 durable runtime 使用 operator 提供的 v3 source。 |
| `retrieval.index-events-v1` | Engineering Memory projection | Event-rebuilt 五索引 manifest 与 active/stale head | `opt-in` | repository 授权的 build/completion、独立 activation、精确 predecessor/watermark、完整 classification view 与可信 embedding provider/model configuration 使用共用 SQLite/PostgreSQL EventLedgerPort 路径。只读 selector 会核验精确 managed-index bundle 并拒绝 stale head；F4-06 已通过独立验收，默认 selection 仍未关闭。 |
| `outcome.harm-events-v1` | Engineering Memory projection | Event-rebuilt outcome、cohort、causal-harm 与 suspension advice 视图 | `opt-in` | 精确 evaluation context 使用 repository `memory:verify` 授权与可信 attestation verifier configuration。association 保持非因果语义，未绑定 attribution 不得进入派生视图，suspension 输出仅为 recommendation。SQLite/PostgreSQL 共用 EventLedgerPort；F4-07 独立验收与默认 cutover 仍未关闭。 |
| `retrieval.managed-index-v3` | Retrieval | Managed-index source | `opt-in` | 默认 adapter 检索兼容记录；显式 durable runtime 可使用该 source。 |
| `artifact.encrypted-authority-v3` | 受保护内容 | 加密 Artifact authority | `opt-in` | 显式 durable runtime 使用配置的 authority；尚无 object storage/KMS 产品路径。 |
| `replay.durable-v3` | Replay | Durable replay authority | `opt-in` | 启动 policy 允许 content 时，显式 durable HTTP/MCP 会导出 session-bound replay；默认 adapter 不会。 |
| `completion.outbox-v3` | 完成 | Outcome 与 outbox authority/worker | `opt-in` | 显式 `tbmd local` 会运行有界 SQLite delivery page 并 reclaim 过期 lease；shared-service dispatch 仍待完成。 |
| `operations.audit-recovery-v3` | 运维 | Audit/recovery authority/worker | `opt-in` | 显式 `tbmd local` 会 expire 到期的 PREPARED/AWAITING_DECISION session；它不会执行任意 audit remediation action。 |
| `protocol.canonical-event-v1` | Full Persistence 协议 | `tbm.event.v1` 规范信封 | `opt-in` | 已交付严格、存储中立的信封、Schema、示例和双语参考；显式 durable SQLite/PostgreSQL runtime 现在会用它保留 GateSession 生命周期事件，其他领域生命周期与兼容路径尚未选择它。 |
| `protocol.event-type-registry-v1` | Full Persistence 协议 | sealed typed event registry、payload schema 与 upcaster | `opt-in` | 未知 type/version 可以保留，但不能被静默消费；显式 durable runtime 会选择密封的 12-type GateSession registry，generic operator 路径仍只暴露 envelope-only inventory reducer。 |
| `protocol.event-ledger-port-v1` | Full Persistence 协议 | 原子 append/read/verify/subscribe 应用端口 | `opt-in` | 存储中立契约已有 WAL/单 owner SQLite 与 row-lock PostgreSQL 后端，覆盖精确 replay、integrity/catalog 校验和跨后端一致性。 |
| `migration.snapshot-v3` | 迁移 | Snapshot v3 plan/bundle/verify/staging | `contract-only` | 没有 apply、cutover、rollback 编排。 |
| `persistence.unified-sqlite-v3` | 持久化切换 | 统一 SQLite v3 schema | `opt-in` | 一个生成 bundle 安装并指纹校验全部 16 个 durable authority schema（含 event ledger）；active 兼容边界仍为 SQLite 1。 |
| `persistence.unified-postgresql-v3` | 持久化切换 | 统一 PostgreSQL v3 schema | `planned` | 当前兼容边界为 PostgreSQL 2。 |
| `persistence.canonical-event-ledger` | Full Persistence | Canonical append-only event ledger | `opt-in` | 已交付 SQLite/PostgreSQL 后端与仅描述符 Artifact 引用；显式 durable runtime 会把 ledger 选为 GateSession revision 的事实源，但在全部 F2-F5 cutover 通过前，整体产品模型仍是 authority graph。 |
| `persistence.reducer-runtime` | Full Persistence | Versioned deterministic reducer 与可重建 projection | `opt-in` | `tbm.reducer.v1` 提供 sealed version/code/config registry、有界双执行 state、typed upcasting、checkpoint/resume、poison evidence、shadow compare、显式批准的 CAS activation 与 append-only rollback。显式 durable runtime 会同步重建 GateSession current state；operator 命令重建 inventory，其余 lifecycle view 尚未 reducer-native。 |
| `transport.durable-http` | Durable transport | Durable HTTP profile | `active` | 显式 `tbm-http --profile durable-v3`；可信 application factory、bearer 边界，默认隐藏内容。SQLite 选择 event-first 命令协调器；PostgreSQL 的 Outcome/Effect 仍由 authority graph 支撑。 |
| `transport.durable-mcp` | Durable transport | Durable MCP profile | `active` | 显式 `tbm-mcp --profile durable-v3`；可信本地 application factory、有界 STDIO、跨重启续接，且默认隐藏内容。SQLite 选择 event-first 命令协调器，PostgreSQL 尚未选择。它不是带 peer authentication 的 shared-service MCP。 |
| `sdk.durable-python-typescript` | SDK | Durable Python/TypeScript client | `active` | 显式 durable HTTP profile 已提供同步/异步 Python client 与无运行时依赖的 Node.js TypeScript client；原始 HTTP、两个 Python client、MCP 与 TypeScript 会在不改变 wire 的前提下重现同一份已提交 SQLite 事件序列与 projection digest。 |
| `service.local-daemon` | 本地服务 | 可重启的 `tbmd local` daemon | `active` | 一个带锁、由 owner 控制的 SQLite 进程让有界 STDIO MCP 与 loopback HTTP 共用 event-first 命令协调器；GateSession/Gate evidence 与 Outcome/Effect 写入会先追加并同步重建，再写投影。真实进程 hard-kill matrix 会在全部 11 个 commit 点核验精确 rollback 或精确 replay。worker claim/ack 保持短事务和 at-least-once。其余 authority、兼容路径与独立 PostgreSQL 尚未切换。`init`、`doctor` 与 `health` 保持确定。 |
| `service.shared-multitenant` | 共享服务 | Remote transport、OIDC、RBAC/RLS、workload identity | `planned` | 当前 Alpha 不能作为不可信多租户服务。 |
| `integration.review-console` | 工程集成 | Review Console | `planned` | 尚无 control-plane 实现。 |
| `integration.codex-hooks` | 工程集成 | Codex hooks/App Server adapter | `opt-in` | 严格有界 capture 把全部 12 个结构化 Hook/App Server 事实映射成有序 TraceEvent，并执行可信 scope、精确受保护 source descriptor、permission/request 绑定、lifecycle 校验和原子 event batch；它不安装 Hook，也不改变默认 transport。 |
| `integration.github-pr-check` | 工程集成 | GitHub PR Check | `planned` | 当前 CLI 只生成确定性报告。 |
| `operations.production-readiness` | 生产运维 | OTEL/SLO、backup/DR、retention、load/chaos | `planned` | 稳定版资格验证尚未完成。 |
| `governance.stable-release` | 治理 | Security/support/compatibility/governance 契约 | `planned` | Alpha 尚无稳定支持与弃用窗口。 |

## 已接受的收敛决策

- [ADR-0001：v2 兼容与 durable-v3 切换](../adr/0001-v2-compatibility-durable-v3-cutover.zh-CN.md)
- [ADR-0002：统一 version-3 数据库 bundle](../adr/0002-unified-v3-database-bundles.zh-CN.md)
- [ADR-0003：transport identity 归属](../adr/0003-transport-identity-ownership.zh-CN.md)
- [ADR-0004：规范 resource manifest](../adr/0004-canonical-resource-manifest.zh-CN.md)
- [ADR-0005：公开与内部 package 边界](../adr/0005-public-internal-package-boundaries.zh-CN.md)
- [ADR-0006：Full Persistence 与 reducer-native memory](../adr/0006-full-persistence-reducer-native-memory.zh-CN.md)

当前机器可读边界是 `persistence_model="authority_graph"`、
`ledger_protocol="tbm.event.v1"`、`reducer_protocol="tbm.reducer.v1"` 与
`full_persistence=false`。事件信封、ledger backend 与 reducer/projection 路径仍为
opt-in；`tbmd local` 现在针对 GateSession/Gate evidence 与 Outcome/Effect 写入选择
它们；独立 SQLite durable HTTP/MCP 现在也会选择它们。独立 PostgreSQL、兼容路径与
整体事实来源在其他 lifecycle projection 与 cutover gate 完成前仍是 authority graph。

## 状态提升规则

只有用户路径、失败语义、聚焦负向测试、双语文档和必要分发验证在同一变更中落地，
对应行才允许变化。`opt-in` 只有在真实 transport 或 daemon 选择该能力，并由
restart/conformance 测试覆盖后，才能提升为 `active`。
