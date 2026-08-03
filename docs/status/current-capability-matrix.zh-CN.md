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
| `compat.agent-v1` | 兼容路径 | `tbm.agent.v1`、`LocalAgentMemory`、CLI | `active` | pending gate request 保存在进程内。 |
| `transport.local-mcp-v1` | 本地 transport | STDIO MCP | `active` | 选择 version-2 兼容生命周期。 |
| `transport.loopback-http-v1` | 本地 transport | Loopback HTTP | `active` | 选择 version-2 兼容生命周期。 |
| `sdk.python-v1` | SDK | Python 同步/异步 client | `active` | 面向本地 `tbm.agent.v1` HTTP profile。 |
| `sdk.typescript-v1` | SDK | TypeScript client | `active` | 面向本地 `tbm.agent.v1` HTTP profile。 |
| `distribution.strict-resources` | 分发 | 严格 packaged-resource allowlist | `active` | 规范资源与安装资源执行逐字节核验。 |
| `governance.authority-registry-v1` | 治理 | 持久化 authority 角色登记与仓库守卫 | `active` | 每个 SQLite/PostgreSQL v3 持久化模块都登记为 ledger、projection、compatibility migration 或 bundle coordinator；未登记 authority 会使仓库验证失败。 |
| `identity.authorization-v3` | 身份 | Entity registry 与 authorization authority v3 | `opt-in` | 仅可信直接 Python context；默认 transport 尚无身份。 |
| `session.gate-session-v3` | Durable session | SQLite/PostgreSQL GateSession 与恢复 | `opt-in` | side-by-side authority；不是 active Store 生命周期。 |
| `agent.durable-lifecycle-v3` | Durable lifecycle | 从 prepare 到 completion/replay 的 facade | `opt-in` | adapter-neutral 组合，默认 transport 尚未选择。 |
| `agent.durable-wire-v1` | Durable wire | `tbm.durable-agent-wire.v1` | `opt-in` | 严格 dispatcher；自身不认证 peer。 |
| `memory.structured-evidence-v3` | 证据 | 结构化 regression evidence | `opt-in` | active v2 publication 仍使用兼容模型。 |
| `memory.revision-publication-v3` | Publication | 不可变 revision authority | `opt-in` | active v2 publication 仍使用兼容模型。 |
| `retrieval.activated-revision-v3` | Retrieval | ActivatedRevision source | `opt-in` | 默认 adapter 检索兼容记录；显式 durable runtime 使用 operator 提供的 v3 source。 |
| `retrieval.managed-index-v3` | Retrieval | Managed-index source | `opt-in` | 默认 adapter 检索兼容记录；显式 durable runtime 可使用该 source。 |
| `artifact.encrypted-authority-v3` | 受保护内容 | 加密 Artifact authority | `opt-in` | 显式 durable runtime 使用配置的 authority；尚无 object storage/KMS 产品路径。 |
| `replay.durable-v3` | Replay | Durable replay authority | `opt-in` | 启动 policy 允许 content 时，显式 durable HTTP/MCP 会从 canonical finalization event 重建 session-bound replay metadata，并从 authenticated replay authority 读取精确字节；默认 adapter 仍使用 projection-backed path 且不暴露 replay。 |
| `completion.outbox-v3` | 完成 | Outcome、outbox 与本地 effect authority/worker | `opt-in` | 显式 durable SQLite/PostgreSQL completion 会追加 `EffectRequested`；worker transition 会追加 canonical started/succeeded/failed/retry/dead-letter evidence，`effect-queue` 会重建精确 delivery history。`tbmd local` 运行有界 SQLite page；active provider-receipt integration、durable compensation 与 shared-service dispatch 仍待完成。 |
| `operations.audit-recovery-v3` | 运维 | Audit/recovery authority/worker | `opt-in` | 显式 `tbmd local` 会 expire 到期的 PREPARED/AWAITING_DECISION session；它不会执行任意 audit remediation action。 |
| `protocol.canonical-event-v1` | Full Persistence 协议 | `tbm.event.v1` 规范信封 | `opt-in` | 已交付严格、存储中立的信封、Schema、示例和双语参考；隔离 SQLite/PostgreSQL event ledger 会精确保留它，显式 durable root 已选择 event-first GateSession、Gate evidence、Semantic attempt、finalization、outcome/attribution 与本地 effect slice。默认 compatibility 保持不变。 |
| `protocol.event-type-registry-v1` | Full Persistence 协议 | sealed typed event registry、payload schema 与 upcaster | `contract-only` | 未知 type/version 可以保留，但不能被静默消费；sealed 默认 registry 现包含 30 个 typed Gate、finalization、outcome 与 effect event。generic/domain reducer 均可绑定它，operator activation 保持显式并 fail closed。 |
| `protocol.provider-effect-ledger-v1` | Effect evidence | 内容寻址 provider receipt 与 unknown-result recovery | `opt-in` | 一个严格 provider-transition event、`effect-queue` reducer version 2 与 authenticated generic-ledger service 会保留 attempt、provider request ID、receipt、unknown result、reconciliation、显式 retry schedule 与精确 response-loss replay。active semantic/completion callback 与 provider-specific reconciliation adapter 尚未选择它。 |
| `protocol.event-ledger-port-v1` | Full Persistence 协议 | 原子 append/read/verify/subscribe 应用端口 | `opt-in` | 存储中立契约已有 WAL/单 owner SQLite 与 row-lock PostgreSQL 后端，覆盖精确 replay、integrity/catalog 校验和跨后端一致性。 |
| `migration.snapshot-v3` | 迁移 | Snapshot v3 plan/bundle/verify/staging | `contract-only` | 没有 apply、cutover、rollback 编排。 |
| `persistence.unified-sqlite-v3` | 持久化切换 | 统一 SQLite v3 schema | `opt-in` | 一个生成 bundle 安装并指纹校验全部 16 个 durable authority schema（含 event ledger）；active 兼容边界仍为 SQLite 1。 |
| `persistence.unified-postgresql-v3` | 持久化切换 | 统一 PostgreSQL v3 schema | `planned` | 当前兼容边界为 PostgreSQL 2。 |
| `persistence.canonical-event-ledger` | Full Persistence | Canonical append-only event ledger | `opt-in` | 已交付隔离 SQLite/PostgreSQL 后端和仅描述符 Artifact 引用；显式 durable root 已选择 event-first GateSession、Gate evidence、Semantic attempt、finalization、outcome/attribution 与本地 completion-effect adapter，并选择 ledger-backed replay reader；同步 authority 仍是过渡 projection，source-of-truth model 仍为 `authority_graph`。 |
| `persistence.reducer-runtime` | Full Persistence | Versioned deterministic reducer 与可重建 projection | `opt-in` | `tbm.reducer.v1` 提供 sealed version/code/config registry、有界双执行 state、typed upcasting、checkpoint/resume、poison evidence、shadow compare、显式批准的 CAS activation 与 append-only rollback。SQLite/PostgreSQL 保留 checkpoint/head history；F2 reducer 会校验 GateSession、Gate-evidence、Semantic-attempt、final decision/injection、RunOutcome、OutcomeAttribution、EffectQueue delivery-history/dead-letter parity 与存储中立 provider receipt/reconciliation 状态。generic runtime 尚未成为全部 active Gate/Memory projection 的唯一重建路径。 |
| `transport.durable-http` | Durable transport | Durable HTTP profile | `active` | 显式 `tbm-http --profile durable-v3`；可信 application factory、bearer 边界、统一 SQLite/PostgreSQL v3 runtime，默认隐藏内容。 |
| `transport.durable-mcp` | Durable transport | Durable MCP profile | `active` | 显式 `tbm-mcp --profile durable-v3`；可信本地 application factory、有界 STDIO、统一 SQLite/PostgreSQL v3 runtime、跨重启续接，且默认隐藏内容。它不是带 peer authentication 的 shared-service MCP。 |
| `sdk.durable-python-typescript` | SDK | Durable Python/TypeScript client | `active` | 显式 durable HTTP profile 已提供同步/异步 Python client 与无运行时依赖的 Node.js TypeScript client；同一份共享 fixture 会通过 Python 与 TypeScript lifecycle 测试。 |
| `service.local-daemon` | 本地服务 | 可重启的 `tbmd local` daemon | `active` | 一个带锁、由 owner 控制的 SQLite 进程会让有界 STDIO MCP、loopback HTTP、GateSession recovery 与 outbox delivery 共用同一 runtime/dispatcher；`init`、`doctor` 与 `health` 输出确定。独立 `tbmd ledger/projection` operator 命令要求显式 event-ledger database，不改变 `tbmd local` 的事实来源选择。 |
| `service.shared-multitenant` | 共享服务 | Remote transport、OIDC、RBAC/RLS、workload identity | `planned` | 当前 Alpha 不能作为不可信多租户服务。 |
| `integration.review-console` | 工程集成 | Review Console | `planned` | 尚无 control-plane 实现。 |
| `integration.codex-hooks` | 工程集成 | Codex hooks/App Server adapter | `planned` | 当前集成为文档、skills 与本地 MCP。 |
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
`full_persistence=false`。事件信封、ledger backend 与 generic reducer/projection operator
路径均为 opt-in；显式 durable lifecycle slice 会使用 event-first write 与 ledger-derived
replay metadata，但尚无经过核验的完整 cutover 把 ledger 选为唯一当前事实来源。

## 状态提升规则

只有用户路径、失败语义、聚焦负向测试、双语文档和必要分发验证在同一变更中落地，
对应行才允许变化。`opt-in` 只有在真实 transport 或 daemon 选择该能力，并由
restart/conformance 测试覆盖后，才能提升为 `active`。
