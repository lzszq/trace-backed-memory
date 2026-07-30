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
| `identity.authorization-v3` | 身份 | Entity registry 与 authorization authority v3 | `opt-in` | 仅可信直接 Python context；默认 transport 尚无身份。 |
| `session.gate-session-v3` | Durable session | SQLite/PostgreSQL GateSession 与恢复 | `opt-in` | side-by-side authority；不是 active Store 生命周期。 |
| `agent.durable-lifecycle-v3` | Durable lifecycle | 从 prepare 到 completion/replay 的 facade | `opt-in` | adapter-neutral 组合，默认 transport 尚未选择。 |
| `agent.durable-wire-v1` | Durable wire | `tbm.durable-agent-wire.v1` | `opt-in` | 严格 dispatcher；自身不认证 peer。 |
| `memory.structured-evidence-v3` | 证据 | 结构化 regression evidence | `opt-in` | active v2 publication 仍使用兼容模型。 |
| `memory.revision-publication-v3` | Publication | 不可变 revision authority | `opt-in` | active v2 publication 仍使用兼容模型。 |
| `retrieval.activated-revision-v3` | Retrieval | ActivatedRevision source | `opt-in` | 默认 adapter 检索兼容记录；显式 durable runtime 使用 operator 提供的 v3 source。 |
| `retrieval.managed-index-v3` | Retrieval | Managed-index source | `opt-in` | 默认 adapter 检索兼容记录；显式 durable runtime 可使用该 source。 |
| `artifact.encrypted-authority-v3` | 受保护内容 | 加密 Artifact authority | `opt-in` | 显式 durable runtime 使用配置的 authority；尚无 object storage/KMS 产品路径。 |
| `replay.durable-v3` | Replay | Durable replay authority | `opt-in` | 启动 policy 允许 content 时，显式 durable HTTP/MCP 会导出 session-bound replay；默认 adapter 不会。 |
| `completion.outbox-v3` | 完成 | Outcome 与 outbox authority/worker | `opt-in` | 显式 `tbmd local` 会运行有界 SQLite delivery page 并 reclaim 过期 lease；shared-service dispatch 仍待完成。 |
| `operations.audit-recovery-v3` | 运维 | Audit/recovery authority/worker | `opt-in` | 显式 `tbmd local` 会 expire 到期的 PREPARED/AWAITING_DECISION session；它不会执行任意 audit remediation action。 |
| `migration.snapshot-v3` | 迁移 | Snapshot v3 plan/bundle/verify/staging | `contract-only` | 没有 apply、cutover、rollback 编排。 |
| `persistence.unified-sqlite-v3` | 持久化切换 | 统一 SQLite v3 schema | `opt-in` | 一个生成 bundle 安装并指纹校验全部 15 个 durable authority schema；active 兼容边界仍为 SQLite 1。 |
| `persistence.unified-postgresql-v3` | 持久化切换 | 统一 PostgreSQL v3 schema | `planned` | 当前兼容边界为 PostgreSQL 2。 |
| `transport.durable-http` | Durable transport | Durable HTTP profile | `active` | 显式 `tbm-http --profile durable-v3`；可信 application factory、bearer 边界、统一 SQLite/PostgreSQL v3 runtime，默认隐藏内容。 |
| `transport.durable-mcp` | Durable transport | Durable MCP profile | `active` | 显式 `tbm-mcp --profile durable-v3`；可信本地 application factory、有界 STDIO、统一 SQLite/PostgreSQL v3 runtime、跨重启续接，且默认隐藏内容。它不是带 peer authentication 的 shared-service MCP。 |
| `sdk.durable-python-typescript` | SDK | Durable Python/TypeScript client | `active` | 显式 durable HTTP profile 已提供同步/异步 Python client 与无运行时依赖的 Node.js TypeScript client；同一份共享 fixture 会通过 Python 与 TypeScript lifecycle 测试。 |
| `service.local-daemon` | 本地服务 | 可重启的 `tbmd local` daemon | `active` | 一个带锁、由 owner 控制的 SQLite 进程会让有界 STDIO MCP、loopback HTTP、GateSession recovery 与 outbox delivery 共用同一 runtime/dispatcher；`init`、`doctor` 与 `health` 输出确定。 |
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

## 状态提升规则

只有用户路径、失败语义、聚焦负向测试、双语文档和必要分发验证在同一变更中落地，
对应行才允许变化。`opt-in` 只有在真实 transport 或 daemon 选择该能力，并由
restart/conformance 测试覆盖后，才能提升为 `active`。
