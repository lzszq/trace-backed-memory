# Trace-backed Memory

[English](reference.md) | **简体中文**

这是从项目首页迁出的详细 API 与运维参考。简明介绍见
[根目录 README](../README.zh-CN.md)。历史英文参考包含更长的逐项 API 清单；
中文的当前契约以[产品与当前能力](product.md)、[架构](architecture.zh-CN.md)和
[使用策略](usage-policy.zh-CN.md)为准，三者与英文规范同步维护。

面向 LLM / Agent Harness 工程、以可验证溯源为基础的记忆层。

## 一句话简介

Trace-backed Memory 将绑定来源的 Agent Trace、评估结果和 Git 提交，转化为经过验证、受作用域约束且可审计的记忆，并允许系统在调试、修复、回归分析、规划和生产运行时有选择地使用这些记忆。

[文档索引](index.zh-CN.md) | [产品概览与当前能力](product.md) | [架构](architecture.zh-CN.md) | [记忆使用策略](usage-policy.zh-CN.md) | [交付计划](product-program.zh-CN.md)

## 项目定位

本项目不是通用聊天机器人记忆，而是面向 Harness 的工程记忆系统：

```text
Trace -> 失败案例 -> 已验证经验 -> 经门控的运行时记忆
```

系统遵循五条规则：

1. Trace 是事实来源。
2. 记忆是从 Trace、评估和 Git 历史中提炼出的受控投影。
3. 默认不把原始 Trace 直接注入提示词。
4. 记忆必须同时通过 System Gate 和 LLM 适用性门控才能使用。
5. 每条记忆都必须记录来源、作用域、状态和使用日志。

## 核心概念

| 概念 | 用途 | 默认对 LLM 可见 |
|---|---|---:|
| Trace | 不可变的运行溯源，包括提示词、工具调用、输出、评估与提交 | 否 |
| Failure Case | 对失败运行的结构化事后复盘 | 仅调试 / 修复模式 |
| Verified Lesson | 从失败案例中提炼并验证过的可复用规则 | 是，但必须匹配作用域并通过门控 |
| Project Policy | 人工维护的提示词、工具或评估策略 | 是，但必须相关 |
| Memory Decision | 记录记忆为何被使用或阻止的审计事件 | 否 |

## 参考架构

```text
Git commit / PR / CI
        |
Harness run
        |
Trace store
        |
Eval result
        |
Failure detection
        |
Failure case draft
        |
Verification / regression
        |
Verified lesson
        |
Memory index
        |
System gate
        |
LLM applicability gate
        |
Runtime injection
        |
Memory usage log
```

## 安装 / 本地开发

项目使用 `src/` 布局。从源码检出后，先以可编辑模式安装：

```powershell
python -m pip install -e .
```

也可以使用 `pip` 安装构建出的 wheel 或源码分发包。发行包通过 `py.typed` 标记为带类型信息。若只需从检出目录执行一次本地命令，PowerShell 使用 `$env:PYTHONPATH = "src"`，POSIX shell 使用 `PYTHONPATH=src`。

## 面向 Agent 的本地运行接口

应用现在可以从一个聚焦入口完成 Trace 注册、prepare/finalize 状态管理、Repository 同步、结果完成和稳定错误封装：

```python
from trace_backed_memory import (
    LocalAgentMemory,
    MemoryContext,
    MemoryRunMeasurement,
    capture_local_trace,
)

trace = capture_local_trace(".", tenant="acme", tool_names=("search_docs",))
context = MemoryContext(
    mode="repair",
    repo=trace.repo,
    tenant=trace.tenant,
    commit_sha=trace.commit_sha,
    tool="search_docs",
)

with LocalAgentMemory.open_sqlite("tbm-memory.sqlite3") as memory:
    prepared = memory.prepare(
        trace,
        context,
        task="repair the failing search call",
    )
    finalized = memory.finalize(
        prepared.request_id,
        {
            "use_memory": False,
            "allowed_memory_ids": [],
            "blocked_memory_ids": [],
            "reason": "No prepared lesson is needed.",
            "risk": "none",
            "recommended_injection": "none",
        },
    )
    memory.complete(
        finalized.decision_id,
        MemoryRunMeasurement(eval_result="pass"),
    )
```

`tbm capabilities` 不需要加载快照即可返回 `tbm.agent.v1` 协议、存储模式、操作和硬限制。当前 schema 中，prepared gate request 仍有意保留为进程内状态，因此必须让同一个 `LocalAgentMemory` 实例存活到 finalize 或 cancel。Trace、finalized usage decision 和 measured completion 会同步到 SQLite 或 PostgreSQL。更多说明见 [Agent 协议](protocols/agent-v1.zh-CN.md) 与 [Codex 集成指南](integrations/codex.zh-CN.md)。

### 长驻本地 MCP

安装可选 profile，并让 Codex 启动 runtime-only STDIO server：

```powershell
python -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm
tbm-mcp --repo-path . --sqlite .tbm/memory.sqlite3
```

`tbm-mcp` 暴露 capability/health discovery，以及 prepare、finalize、complete
和 cancel 工具。它把 Git provenance 固定到 `--repo-path`，在检索前捕获完整
ancestry，不提供 curation 或 activation 操作，并要求显式且只能选择一种存储。
Pending Gate request 仍为进程内状态，因此 Codex 必须让 server 从 prepare
存活到 finalize 或 cancel；重启后必须重新 prepare。opaque request ID 带有新的
128-bit Store-session namespace，因此 stale handle 不会与重启后的新 request
碰撞。项目
`.codex/config.toml` 与精确工具顺序见
[Codex 集成指南](integrations/codex.zh-CN.md)。

如需可选的本地授权边界，必须同时提供以下五项可信启动选择：

```powershell
tbm-mcp --repo-path . --sqlite .tbm/memory.sqlite3 `
  --auth-registry examples/entity_registry_v3.example.json `
  --auth-sqlite .tbm/authorization.sqlite3 `
  --auth-principal-id principal_tenant_001 `
  --auth-agent-client-id agent_client_001 `
  --auth-environment-id environment_001
```

有界 registry 文件通过所选 active environment 提供 canonical tenant 与
repository。每次 prepare 都会先持久化并读回 allow/deny decision，之后才注册
Trace 或 retrieval。MCP 请求 Schema 不包含 principal、client、tenant、
repository、environment、registry 或 authority 字段。这些 CLI 选择是可信本地
bootstrap 输入，不是 transport authentication 或可重用 credential；不得把此
profile 暴露成不可信共享多租户服务。认证模式不能与 `--tenant` 组合。此 profile
使用 SQLite authorization authority，与所选 runtime storage mode 相互独立。

## 打包资源

wheel、源码分发包和可编辑安装都会提供 `schemas/` 与 `examples/` 下规范运行时文件的字节一致副本，以及规范的失败分类体系和 active lesson YAML 示例。`AGENTS.md` 等贡献者指引不属于运行时资源。资源名来自严格的 POSIX 规范路径白名单，不能借此读取任意文件系统路径。

```text
tbm resource list
tbm resource read schemas/trace.schema.json
tbm resource export schemas/sqlite.sql sqlite.sql
tbm resource export schemas/postgres-v1-to-v2.sql postgres-v1-to-v2.sql
tbm resource export schemas/postgres-v2-lock-order-hotfix.sql postgres-v2-lock-order-hotfix.sql
tbm resource export schemas/postgres.sql postgres.sql
tbm resource export schemas/postgres.sql postgres.sql --overwrite
```

三个命令都输出一个确定性的 JSON 值。导出默认拒绝覆盖现有目标；只有显式传入 `--overwrite` 才允许替换。发布过程使用目标同目录的临时文件。未知资源名属于输入错误，安装包数据故障属于内部错误，导出写入失败使用退出码 `4`。

Python 调用方可以在不假设包位于真实文件系统的前提下发现、读取和导出资源：

```python
from trace_backed_memory import (
    export_packaged_resource,
    packaged_resources,
    read_packaged_resource,
)

resources = packaged_resources()
sqlite_sql = read_packaged_resource("schemas/sqlite.sql")
postgres_migration_sql = read_packaged_resource("schemas/postgres-v1-to-v2.sql")
postgres_hotfix_sql = read_packaged_resource("schemas/postgres-v2-lock-order-hotfix.sql")
postgres_sql = read_packaged_resource("schemas/postgres.sql")
postgres_revision_sql = read_packaged_resource("schemas/postgres-v3-memory-revision.sql")
postgres_revision_rollback_sql = read_packaged_resource(
    "schemas/postgres-v3-memory-revision-rollback.sql"
)
export_packaged_resource("schemas/sqlite.sql", "sqlite.sql")
export_packaged_resource("schemas/postgres.sql", "postgres.sql")
```

当前白名单包含 123 项资源。`PackagedResource` 描述包含资源种类、媒体类型、字节数和 SHA-256。`load_failure_taxonomy()` 默认加载包内规范分类体系；传入路径时仍会加载调用方拥有的文件。

## 证据摄取完整性

失败提取只按顺序使用 `Trace.error`，以及 `tool_calls`、`tool_outputs` 中显式的顶层 `error` 字段。工具名称可以为同一条带错误证据的调用或输出标注症状，但不能独立决定失败分类。任意参数、结果、嵌套载荷、成功工具数据和工具标识符都不会被搜索分类关键字，因此不能制造误分类。

通用单词 `required` 本身不构成参数错误信号。除显式的 `invalid argument` 外，只有保守限定的 `required argument`、`required parameter`、`required field` 和 `required property` 工具错误标记会选择 `invalid_tool_argument`。

无依赖的失败分类 YAML 和 active lesson YAML 适配器会拒绝重复的受支持字段，而不是静默采用最后一个值。lesson 记录、作用域键以及调用方 JSON 的任意层级对象键都遵循同样的无歧义规则。`TraceBackedMemoryStore.load_json()`、`parse_memory_context()`、`parse_memory_decision()` 和所有 CLI JSON 文件解析器都会拒绝重复键。

`save_json()` 与 `save_lessons_yaml()` 通过同目录临时文件发布：写入规范 LF 文本，刷新并调用 `os.fsync()`，再原子发布。`save_lessons_yaml(..., overwrite=False)` 使用一次 `os.link()` 发布来无竞态地拒绝已存在目标。POSIX 平台在发布成功并清理临时名称后还会同步父目录；非 POSIX 平台保留可移植的原子发布行为。

序列化、临时文件同步、链接或替换失败会保留原目标并清理临时文件。发布后的父目录同步失败会向调用方传播，但此时目标可能已经可见，必须把结果视为“持久性状态不确定”并在重试前检查。lesson 导出使用规范的 `lesson_text: |` 字面块；导入兼容 `|` 和历史 `>` 形式，并保留空行、首尾 LF 与行内空格。

这些强化不增加持久化字段，不改变快照版本 `2`、JSON Schema 或 PostgreSQL schema 版本 `2`。

## 有界本地文档摄取

所有调用方文件都会先在二进制模式下打开一次，通过同一个句柄最多读取“字节上限 + 1”字节，再以严格 UTF-8 解码。这样可以避免独立文件大小检查带来的竞态，并在语义验证或 Store 变更前拒绝超限输入。

安全默认值如下：

- 快照 JSON：64 MiB；每个集合最多 100,000 条记录；五个集合合计最多 250,000 条。
- active lesson YAML：8 MiB，最多 10,000 条 lesson。
- 失败分类 YAML：1 MiB，最多 1,000 个失败类型。
- CLI measurement 与 tool-output JSON：8 MiB、最多 10,000 个顶层条目、100,000 个 JSON 节点、深度 100。
- `recover-batch`：最多 10,000 个 decision ID 和 10,000 个 attribution 选项。

`load_json()`、`from_snapshot()`、`load_lessons_yaml()` 和 `load_failure_taxonomy()` 提供仅限关键字的限制参数。受信任的离线迁移可以对单项限制显式传入 `None`；CLI 不提供关闭限制的选项。被拒绝的导入始终保持全有或全无，并且限制元数据不会持久化。

## 有界运行时 Trace JSON

Store 将 `Trace.retrieved_context`、`Trace.tool_calls` 和 `Trace.tool_outputs` 视为一个结构化 JSON 预算。三者合计最多包含 100,000 个 JSON 节点，以及 8 MiB 的对象键与字符串 UTF-8 文本；每个结构值仍受深度 100 限制。验证器在扩展遍历栈前检查容器宽度，并在防御性复制或持久化前拒绝不可编码为 UTF-8 的字符串。

这一固定边界适用于直接 `record_trace()`、Trace 完成、快照重建和 PostgreSQL 加载。它没有受信任迁移豁免，因为这些值会成为实时 Store 状态；恰好位于边界的输入仍然有效。

快照加载使用本地索引保持使用日志重建的平均 O(n) 工作量。运行时还维护 `decision_id` 到稳定列表位置、`run_id` 到有序 `trace_id` 列表的私有派生索引，使单 ID 决策查找、重复检查和唯一 Trace 解析保持平均 O(1)。这些索引不序列化，快照规范排序保持不变。

`metrics()` 使用一次 usage-log 扫描和 O(1) 累加空间统计候选、使用、阻止、过时、已评估、未评估和错误记忆数量。`memory_run_metrics()` 同样不排序，只执行一次 usage-log 扫描；审计与补救视图仍按 decision ID 排序。所有指标都是派生值，不改变持久化格式。

## 快照操作 CLI

安装包提供无依赖的 `tbm` 控制台命令，也可以通过 `python -m trace_backed_memory` 调用同一接口：

```text
tbm capabilities
tbm snapshot validate SNAPSHOT
tbm snapshot stats SNAPSHOT
tbm migration plan-v3 SNAPSHOT_V2 MAPPING_JSON [--repository-root REPOSITORY_ID=PATH]...
tbm migration bundle-v3 SNAPSHOT_V2 MAPPING_JSON [--repository-root REPOSITORY_ID=PATH]...
tbm migration verify-v3-bundle BUNDLE_JSON [--repository-root REPOSITORY_ID=PATH]...
tbm lessons export SNAPSHOT DESTINATION [--overwrite]
tbm lessons import SNAPSHOT SOURCE_YAML [--write]
tbm obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]
tbm obsolete-batch SNAPSHOT REQUESTS_JSON [--write]
tbm audit SNAPSHOT
tbm metrics SNAPSHOT
tbm remediation SNAPSHOT
tbm pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH
tbm outcome SNAPSHOT DECISION_ID --eval-result {pass,fail,error} [--memory-caused-failure true|false] [--write]
tbm complete SNAPSHOT TRACE_ID DECISION_ID --eval-result {pass,fail,error} [--memory-caused-failure true|false] [--output-hash VALUE] [--tool-outputs-file PATH] [--latency-ms INTEGER] [--cost-usd NUMBER] [--error VALUE] [--trace-uri VALUE] [--write]
tbm complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]
tbm recover-ready SNAPSHOT [--write]
tbm recover SNAPSHOT DECISION_ID [--memory-caused-failure true|false] [--write]
tbm recover-batch SNAPSHOT DECISION_ID... [--attribution DECISION_ID=true|false]... [--write]
```

`tbm capabilities` 不读取快照，直接返回 Agent 协议契约。每个快照命令都通过标准 Store 验证路径加载一次本地快照。读取命令输出一个确定性 JSON 值和换行。`migration plan-v3` 是只读预检：它验证显式 canonical repository/tenant binding、memory authorization scope、结构化 regression evidence、global-policy privileged approval 与 ancestry policy。`required` ancestry 只会针对显式提供的 `--repository-root` Git 对象库执行验证；缺少可信 verifier 会阻止 ready，而带审计 reason 的 `disabled` policy 会产生 warning。它只报告未来协同 version-3 迁移是否 ready，不会生成或写入 version-3 snapshot。完成、恢复和生命周期变更默认只预演；只有显式 `--write` 且整个操作成功时才会原子替换输入快照。

`migration bundle-v3` 会把原始 source、normalized source digest、mapping 与
plan 冻结为不可激活的 content-addressed bundle；`migration
verify-v3-bundle` 会复验所有内嵌 hash 并精确重放 plan。Bundle 可通过
`SQLiteV3MigrationRepository` 持久化，但它不是 runtime snapshot，也不能激活
memory。详见 [staging 契约](migrations/v3-staging-bundles.zh-CN.md)。

与存储实现无关的 `tbm.replay.v3` 契约可以描述精确 artifact 字节、finalized
injection，以及固定八项 component 的 decision replay manifest。complete manifest
绑定自身 canonical hash 与全部 replay component；legacy partial manifest 必须精确
列出缺失项。opt-in `SQLiteReplayV3Repository` 在隔离 immutable 账本中保存精确
字节、injection descriptor 与 manifest，提供原子 bundle 写入和 fail-closed load
复验。隔离 PostgreSQL install/rollback 资源现已建立匹配的不可变关系边界，并在
fail-closed 删除前核对预期 catalog membership；opt-in
`PostgresReplayV3Repository` 提供 canonical descriptor/byte-digest 复验、精确
idempotency、嵌套 transaction ownership 与 schema drift 检查。当前 Store、
active SQL adapter、本地 Agent 与 MCP 均不使用这些资源；它们也不提供 access
control、encryption、retention 或 GateSession authority，因此仍是统一 version-3
runtime 的准备工作，而不是当前已支持精确重放的声明。详见
[重放契约](protocols/replay-v3.zh-CN.md)。

与存储实现无关的授权 v3 契约定义 canonical repository、精确的租户作用域别名、
principal、agent client、role binding 与内容关联的允许/拒绝 decision。求值器把
授权与适用性分开，并设计为先于任何检索运行。身份与目标字段必须来自服务端认证
上下文；decision hash 只是内容身份，不是签名或可重用 capability。
`SQLiteAuthorizationV3Repository` 与 `PostgresAuthorizationV3Repository`
提供 opt-in 隔离 authority，在原子持久化 immutable policy/decision 和唯一
request identity 前核验精确 policy/request/decision 三元组。PostgreSQL 同时
提供带版本门禁的 install 与 fail-closed rollback 资源。默认 Store、Agent、MCP
与 GateSession profile 不调用任一 authority；可选本地 MCP `--auth-*` profile
会在 retrieval 前调用 SQLite authority。
`AuthenticatedRetrievalService` 是共享、与存储无关的顺序 kernel：可信 service
context 与当前 registry 匹配，精确 decision 会被持久化并读回，registry 轮换与
environment binding 会被复查，之后 retrieval callback 才能运行。可信本地 MCP
bootstrap integration 已可用；transport authentication 与其他 active adapter
接入仍待完成。详见
[授权契约](protocols/authorization-v3.zh-CN.md)与
[认证 service 边界](protocols/authenticated-service-v3.zh-CN.md)。
`AuthenticatedGateSessionService` 增加 durable
`CREATED`-before-preparation 顺序、精确 replay 抑制、可信 retrieval/System-Gate
evidence 核验、`PREPARED` CAS 与显式 cancel-or-recover 补偿。详见
[认证 durable Gate preparation](protocols/authenticated-gate-service-v3.zh-CN.md)。
`GateSessionRecoveryWorker` 执行有界 due scan，包含精确 expiry CAS、durable
read-back、superseded version 分类，以及 lease-only 或 graph-blocked state 的
显式 recovery-required 结果。详见
[GateSession recovery worker](protocols/gate-recovery-worker-v3.zh-CN.md)。
opt-in SQLite evidence authority 会原子保留精确 retrieval/System Gate 记录对，
共享 durable verifier 则在 `PREPARED` 前把两者绑定到已授权 session。详见
[SQLite Gate evidence v3](protocols/sqlite-gate-evidence-v3.zh-CN.md)。

storage-neutral `tbm.regression-evidence.v3` 不会替换任何 active 字段；它补上发布
immutable memory revision 之前所需的严格目标记录。记录以内容派生 evidence ID
绑定不同的 source/verification Trace、expected/observed outcome、
evaluator/environment provenance、精确 commit 关系、artifact、相互独立的
submitter/verifier principal 与 attestation hash。通过的记录只是 evidence，不是
发布权限。详见[证据契约](protocols/evidence-v3.zh-CN.md)。

proposal-only `tbm.memory-revision.v3` 增加 immutable、内容派生 revision，绑定精确
parent、content artifact、canonical scope、case/fix/evidence 引用与 server-owned
proposer context。evidence preflight 会拒绝缺失、未通过、跨 case 或 self-conflicted
provenance。它不 approve/activate memory；这些仍是独立认证 service operation。
详见[revision 契约](protocols/memory-revision-v3.zh-CN.md)。

storage-neutral `tbm.retrieval-snapshot.v3` 契约以内容派生 ID 记录一次精确的
已授权检索结果。它绑定 session/request/trace、授权事件、context/query 摘要、
retriever/index 版本、有序 memory-revision 命中、候选哈希、逐阶段分数、
确定性融合与显式截断原因。相似度始终只是排序证据，不是权限或门禁证据。
active Store/Agent/MCP 尚不产生它。详见
[检索快照契约](protocols/retrieval-snapshot-v3.zh-CN.md)。

配套 `tbm.system-gate-evaluation.v3` 与 `tbm.semantic-gate-attempt.v3`
契约记录逐候选确定性 rule 和完整模型 attempt provenance，不包含 raw
prompt/response 内容。跨记录核验要求精确 snapshot 覆盖，并强制模型只能缩小
System Gate 结果；失败调用仍是 immutable、仅 provenance 的 attempt。active
Store/Agent/MCP 尚不产生这些记录。详见
[门禁评估契约](protocols/gate-evaluation-v3.zh-CN.md)。

`SemanticGateArtifactBinding` 把一段精确非空 prompt 或 response 字节连接到
匹配的 `SemanticGateAttempt` 角色与 digest。它复用通用
`ContentAddressedArtifact` 描述符，保留 classification、encryption-key 与
redaction 元数据，执行 Gate prompt/response 字节上限，拒绝失败 attempt 的
response，并提供不嵌入原始字节的严格有界 JSON。它不是字节仓库或 provider
认证边界。详见
[Semantic Gate artifact 绑定契约](protocols/semantic-gate-artifact-v3.zh-CN.md)。

`SQLiteSemanticGateArtifactV3Repository` 原子组合 attempt ledger、精确
public/internal prompt/response 字节与角色 binding。它支持精确幂等重放和调用方
savepoint；SQL guard 会重算内容 hash、比较 descriptor 字段、阻止 replacement
write，并拒绝意外的受管 trigger/index。由于 adapter 不提供静态加密，它会拒绝
敏感分类。详见
[SQLite Semantic Gate artifact 仓库契约](protocols/sqlite-semantic-gate-artifact-v3.zh-CN.md)。

`PostgresSemanticGateArtifactV3Repository` 提供隔离 PostgreSQL 对等实现。一个
外层 transaction 把 SemanticGateAttempt append、精确 public/internal 字节与
角色 binding 原子组合，因此 artifact 冲突也会回滚新 attempt。PostgreSQL 会
独立重算字节 SHA-256、核对每个 descriptor 字段、验证完整 security catalog、
保留调用方 transaction、支持并发精确 replay，并提供 fail-closed `RESTRICT`
rollback。由于 adapter 不提供静态加密，敏感分类仍会被拒绝。详见
[PostgreSQL Semantic Gate artifact 仓库契约](protocols/postgres-semantic-gate-artifact-v3.zh-CN.md)。

`AuthenticatedSemanticGateService` 是两个 artifact repository 共用的 provider-call
kernel。它把 transport 认证的 provider、authenticator 与 credential ID 同服务端持有的
registration 精确匹配；在 callback 前重新加载 Gate evidence 与 durable retry head；由
服务端持有 model、template、generation-config、sequence、parent 与可信 timestamp；
最后要求原子 append 与精确 read-back。任意 provider exception 只会以稳定
`provider_error` code 写入 prompt-only attempt。详见
[已认证 Semantic Gate 服务契约](protocols/semantic-gate-service-v3.zh-CN.md)。

`SQLiteSemanticGateV3Repository` 是有序 Semantic Gate attempt chain 的
opt-in durable 实现。它依赖 SQLite Gate evidence v3 schema，通过 CAS head
强制一条有界线性 sequence，支持精确幂等重放，通过 savepoint 保留调用方
transaction，并在读取时复核完整 chain。字节存储由上述独立 opt-in repository
提供；active GateSession/Agent/MCP transaction 集成仍未完成。详见
[SQLite Semantic Gate ledger 契约](protocols/sqlite-semantic-gate-v3.zh-CN.md)。

`PostgresSemanticGateV3Repository` 提供隔离 PostgreSQL 对等实现。它先锁 Gate
evidence parent，再锁每个 evaluation 的 head，通过 row lock 与 exact CAS
串行化 append，并增加 deferred 数据库侧 chain consistency。operation 在工作
前后验证完整安全 catalog，通过 savepoint 保留调用方 transaction。配套
install/rollback 资源受 active-v2 门禁，出现 drift 或 external dependency 时
rollback fail closed。详见
[PostgreSQL Semantic Gate ledger 契约](protocols/postgres-semantic-gate-v3.zh-CN.md)。

storage-neutral `tbm.run-outcome.v3` 与 `tbm.outcome-attribution.v3`
把 completed GateSession 绑定到显式 evaluator 与 artifact evidence，并严格
区分观察 association 与 causal claim。因果归因必须采用非观察性方法并由独立
verifier 核验；异常或 score 不能被自动提升为因果。active v2 outcome 字段保持
不变。详见[结果契约](protocols/outcome-v3.zh-CN.md)。

`GateSessionCompletionService`、`SQLiteOutcomeV3Repository` 与
`PostgresOutcomeV3Repository` 基于 `schemas/sqlite-v3-outcome.sql` 和
`schemas/postgres-v3-outcome*.sql` 增加 opt-in SQLite 与隔离 PostgreSQL
completion authority。两者都从 durable executing GateSession 派生全部
linkage，使用同一个可信 timestamp，并在一个 transaction/savepoint 中通过 CAS
与精确读回写入 content-addressed RunOutcome 及 `COMPLETED` revision。
PostgreSQL 会先锁定 head 再读取数据库时间，并支持 fail-closed exact-catalog
rollback。同一 measurement 重放返回 `inserted=false`；不同 terminal
measurement 冲突。它不会在 Store、Agent 或 MCP 中激活 v3 生命周期。详见
[SQLite 完成事务契约](protocols/sqlite-outcome-v3.zh-CN.md)与
[PostgreSQL 完成事务契约](protocols/postgres-outcome-v3.zh-CN.md)。

`SQLiteOutcomeAttributionV3Repository` 与
`PostgresOutcomeAttributionV3Repository` 在这些 retained outcome 上增加 opt-in
immutable ledger。它们允许每个 outcome 保留多条 claim，核验精确的
completed-session、usage-decision、finalized-revision 与 timestamp linkage，
支持 content-ID 精确重放与确定性列表，通过 savepoint 保留 caller transaction，
并拒绝 replacement write 或 canonical schema/catalog drift。PostgreSQL 还提供
数据库 hash 重建、row-lock concurrency 与 fail-closed rollback。两者都保存
identity 与 artifact provenance，但不认证二者。详见
[SQLite attribution ledger 契约](protocols/sqlite-outcome-attribution-v3.zh-CN.md)
与 [PostgreSQL attribution ledger 契约](protocols/postgres-outcome-attribution-v3.zh-CN.md)。

`CompletionOutboxEvent` 与 `CompletionOutboxDelivery` 定义 storage-neutral
completion notification 和 append-only delivery revision 契约。
`SQLiteCompletionOutboxV3Repository` 与
`PostgresCompletionOutboxV3Repository` 组合对应 completion authority，使
completed GateSession revision、RunOutcome、immutable event、初始 `pending`
delivery 与 delivery head 处于同一 transaction。它们支持有界 due claim、过期
lease、带 version 检查的 acknowledgement、retry wait、dead letter、精确 replay、
完整 history 核验、caller savepoint 与 schema/catalog-drift 拒绝。PostgreSQL
还提供 database time、row-locked `SKIP LOCKED` claim、canonical insert trigger、
精确 catalog 校验与 fail-closed rollback。Delivery 是 at least once；downstream
consumer 必须按内容派生 event ID 去重。`CompletionOutboxDeliveryWorker` 增加
一次有界、storage-neutral dispatch pass、严格的整页 claim 校验、清洗后的
consumer error、精确 receipt/read-back 校验，以及明确的 delivered/retry/
dead-letter/superseded/recovery-required 结果。调用方 consumer 可以执行 network
I/O，但两个 repository 都不提供 network transport，也没有接入 active
Agent/MCP adapter。详见
[completion outbox 契约](protocols/completion-outbox-v3.zh-CN.md)。

storage-neutral `tbm.audit-event.v3` 与 `tbm.recovery-action.v3` 增加
内容寻址 append-only event chain 与显式 recovery-attempt evidence。恢复核验
复用派生 `MemoryRunRemediation` 和不可变 GateSession version，不创建第二套
lifecycle authority。`SQLiteAuditV3Repository` 与
`PostgresAuditV3Repository` 增加 opt-in 隔离 ledger，提供精确 stream-head
CAS、immutable event、RecoveryAction/event 原子追加、session-scoped request
digest 唯一性、有界读取、schema-drift 检查、调用方 savepoint 与并发幂等。
PostgreSQL adapter 还提供行锁共享数据库 CAS，以及精确的安装和回滚目录校验。
两种 adapter 均尚未接入 active Store/Agent/MCP，也不执行授权或底层
GateSession/remediation transition。详见
[审计与恢复契约](protocols/audit-recovery-v3.zh-CN.md)。

如需 opt-in 的 version-3 lifecycle 本地持久化，可使用独立
`schemas/sqlite-v3-gate-session.sql` 契约与
`SQLiteGateSessionRepository`。它保存 append-only canonical revision、
逐 version CAS head、scoped idempotency、可信时钟 lease 与有界 due-session
discovery，并可与 active SQLite v1 Store 共用一个数据库文件而不改变其 schema。
当前 Agent 与 MCP 不使用该 repository，也不会恢复私有 pending request token；
详见 [GateSession 契约](protocols/gate-session-v3.zh-CN.md)。

如需 opt-in 的共享数据库持久化，`PostgresGateSessionRepository` 使用
`schemas/postgres-v3-gate-session.sql` 安装的隔离
`trace_backed_memory_v3_gate_session` schema。它在行锁后采样数据库时间，提供
scoped idempotency、append-only revision、exact-version CAS、catalog drift
检查与调用方 savepoint；配套
`schemas/postgres-v3-gate-session-rollback.sql` fail-closed。两个脚本都要求并
保留 active PostgreSQL schema version 2；该 adapter 尚不是 active Agent/MCP
state，也不表示 distributed service 已就绪。

`recover-batch` 在重复项检查前统计提交值，decision ID 与 attribution 各自最多接受 10,000 项。每个 `--attribution DECISION_ID=true|false` 使用最后一个 `=` 作为分隔符，因此 `decision=regional` 这样的 ID 仍可寻址；后缀必须是严格的小写 `true` 或 `false`。

`outcome` 只调用一次 `record_decision_outcome()`，不会完成关联 Trace。`complete` 为准确关联的 Trace 与 decision 提交一个新的测量结果；`complete-batch` 读取严格的非空 JSON 对象数组，并调用一次 `complete_memory_runs()`。重复 decision、未知 decision、共享 Trace 结果冲突或后续无效条目都会使整个批次回滚。

`lessons export` 按 Store 顺序写出 active lesson；默认拒绝任何已存在条目，也拒绝通过同一路径、符号链接或硬链接把源快照当作目标。`lessons import` 强制执行 8 MiB 和 10,000 条记录上限，并以全有或全无方式合并；它不是 upsert，且便携导入只接受 active lesson。

`obsolete` 执行单个仅向前生命周期转换。失败案例过时会原子级联其 active lesson；重复处理已过时记录是成功的空操作。`obsolete-batch` 接受仅包含 `memory_kind` 与 `memory_id` 的严格对象数组，解析为 `MemoryObsolescenceRequest` 后只调用一次 `obsolete_memories()`，任何重复或未知 ID 都会拒绝整个 Store 转换。

错误会以一个结构化 JSON 写入 stderr，不输出 traceback。退出码：`0` 表示成功或空操作；`1` 表示意外内部错误；`2` 表示用法、路径、编码、JSON、YAML 或快照输入错误；`3` 表示状态、关联、归因、证据或报告被拒绝；`4` 表示目标文件或快照写入失败。错误文本最多 2,048 个字符。

带 `--write` 的快照变更会使用跨平台排他建议锁串行化完整的读取、修改和写入事务。持久化的同目录 `.tbm.lock` 在加载快照前获取，在原子发布后、写 stdout 前释放。锁文件必须是单链接普通文件；符号链接、Windows reparse point、硬链接和特殊文件都会在触碰目标或加载快照前被拒绝。默认最多等待 30 秒。

Python 进程可以用公开的 `snapshot_write_lock()` 参与同一锁协议：

```python
from trace_backed_memory import TraceBackedMemoryStore, snapshot_write_lock

with snapshot_write_lock("memory-store.json", timeout_seconds=30):
    store = TraceBackedMemoryStore.load_json("memory-store.json")
    # 持有锁期间执行完整的 Store 变更。
    store.save_json("memory-store.json")
```

该锁是非重入的建议锁，不等同于 Store 进程内 `RLock`、只包围 `save_json()` 的锁或 SQLite/PostgreSQL 事务。所有协作写入方都必须遵循同一协议。

## SQLite 存储库

SQLite 支持使用 Python 标准库 `sqlite3`，不需要安装额外依赖。它适合本地 harness、CI 作业和单机工具：

```python
from trace_backed_memory import SQLiteMemoryRepository, TraceBackedMemoryStore

store = TraceBackedMemoryStore()

with SQLiteMemoryRepository.connect(
    "memory.sqlite3",
    initialize=True,
) as repository:
    result = repository.sync(store)
    restored = repository.load()
```

`initialize=True` 会应用包内 schema 版本 1 的 `schemas/sqlite.sql` fresh-install 脚本。运维人员也可以导出并应用完全相同的字节：

```powershell
tbm resource export schemas/sqlite.sql sqlite.sql
sqlite3 memory.sqlite3 ".read sqlite.sql"
```

第二条命令需要单独安装 `sqlite3` 命令行程序；Python 标准库模块不会安装该可执行文件。

`sync(store)` 是增量且原子的。它使用 `BEGIN IMMEDIATE` 串行化写入，保留 Trace、Failure Case、Lesson、Project Policy 和 usage outcome 的受支持前向转换，在 Failure Case 淘汰时级联 active Lesson，并在不可变冲突时回滚整个操作。借用连接已有外层事务时，Repository 使用 savepoint，把最终 commit 或 rollback 留给调用方。

同一个 Repository 实例使用 `RLock` 串行化 `sync()`、`load()` 与 `close()`，因此关闭操作会等待进行中的数据库操作。顶层回滚失败不会覆盖主异常：清理会重试；再次失败时即使连接由调用方传入也会被关闭，因为其中的部分事务已不再可信。

`load()` 在一个 SQLite 事务中读取数据，检查 schema 版本 1，执行每集合 100,000 条、总计 250,000 条以及单条/累计 64 MiB payload 限制，再重建普通且完整校验的 `TraceBackedMemoryStore`。SQLite 表保存规范 JSON payload envelope；数据库侧 JSONB 和跨行约束仍是 PostgreSQL 的优势。直接 SQL 修改 payload 不属于支持契约，会在下一次 load 或 sync 时被拒绝。持久化应使用文件数据库；`:memory:` 数据库只在其 owned connection 生命周期内存在。

## PostgreSQL 存储库

PostgreSQL 支持是可选功能。核心安装不会导入或依赖 `psycopg`；使用同步存储库时安装额外依赖：

```powershell
python -m pip install -e ".[postgres]"
pip install 'trace-backed-memory[postgres]'
```

适配器要求 PostgreSQL 12+，因为 `schemas/postgres.sql` 的强化 JSONB 约束使用了 `jsonb_path_exists`。连接前，应把该资源安装到新的 `public` schema：

```powershell
tbm resource export schemas/postgres.sql postgres.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f postgres.sql
```

适配器要求 PostgreSQL schema 版本 `2`。`schemas/postgres.sql` 是全新安装脚本；既有版本 1 安装使用单独打包的原子迁移：

```powershell
tbm resource export schemas/postgres-v1-to-v2.sql postgres-v1-to-v2.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f postgres-v1-to-v2.sql
```

在 Lesson/source-case 锁序修复之前创建的 schema version 2 数据库，应执行可重复运行且带版本门禁的热修复：

```powershell
tbm resource export schemas/postgres-v2-lock-order-hotfix.sql postgres-v2-lock-order-hotfix.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f postgres-v2-lock-order-hotfix.sql
```

全新安装脚本和当前 v1→v2 迁移已包含该修复。

迁移会增加最终 Gate `request_id` 关联、不可变 Trace/usage 审计/Failure Case 来源/Lesson 来源 trigger、受保护的 outcome 前向转换和行锁，然后推进 metadata 版本。metadata 缺失、起始版本不是 1 或成功后重放都会被拒绝。数据库测试在缺少 `initdb`、`pg_ctl` 或 `psql` 时会跳过；CI 中独立的 Ubuntu 任务会对真实私有集群执行两个 PostgreSQL 测试模块，Windows 任务则运行完整 Python 测试套件。

```python
from trace_backed_memory import PostgresMemoryRepository

with PostgresMemoryRepository.connect("postgresql://...") as repository:
    result = repository.sync(store)
    restored = repository.load()
```

`connect()` 创建由存储库拥有的连接，上下文管理器会关闭它。把现有连接传给 `PostgresMemoryRepository(connection)` 时，连接所有权仍属于调用方。若连接已经处于调用方事务中，每个存储库操作会使用嵌套 savepoint，既不提交也不回滚外层事务；否则存储库事务正常提交。

`sync(store)` 是增量且事务化的：只插入缺失记录，不删除数据库记录。它支持 Trace 完成、decision outcome 封存等仅向前生命周期变更，按规范形式比较记录，并拒绝不可变 ID 冲突。已有目标行会以 `FOR UPDATE` 锁定。缺失主键行无法预锁，因此 INSERT 在嵌套 savepoint 中运行；若并发插入获胜，sync 会识别受支持的唯一性信号、重新锁定目标，并应用同样的规范比较规则。

`repository.load()` 返回经过规范化和验证的 `TraceBackedMemoryStore`，而不是快照对象。读取五个集合前，它会对五张持久化表获取 `SHARE` 锁，避免一次 load 混合不同已提交数据库状态。随后先执行记录数预检，再执行 UTF-8 载荷预检：每个集合最多 100,000 条、合计最多 250,000 条，最大单行及五表总载荷受 64 MiB 边界约束。超限或格式错误会产生已净化的 `PostgresPersistenceError`，不留下部分 Store，并保持连接可复用。

必需身份、关联、失败文本、lesson/policy 作用域、Memory Context 值，以及 usage-audit 映射键和值都必须至少包含一个非空白字符。Store 和六个规范 JSON Schema 使用一致的 `pattern: "\\S"` 规则。PostgreSQL schema 版本 `2` 会拒绝普通空格组成的对应值，但数据库的 `btrim(text)` 比 Python/JSON Schema 的空白定义更窄，因此支持路径仍以 Store 验证为准。

持久化时间戳必须是带显式 `Z` 或数字 UTC offset 的严格 RFC 3339。小数秒如果存在，最多包含六位；生命周期 API、snapshot、SQLite、PostgreSQL 和规范 JSON Schema 会一致拒绝亚微秒精度。

在调用方事务中成功获取的表锁与行锁会一直保持到外层提交或回滚。需要保持写入响应时，应避免长时间持有调用方事务。

## 基于回调的 Memory Run 执行

在把当前 Trace 以 `eval_result="unknown"` 注册后，可使用 `run_memory_execution()` 完成常见同步流程。`MemoryDecisionCallback` 接收 `MemoryGateRequest`；`MemoryExecutionCallback` 接收最终的 `GatedMemoryResult`，并返回不需要复制 decision ID 的 `MemoryRunMeasurement`：

```python
from trace_backed_memory import MemoryRunMeasurement, run_memory_execution


def decide(request):
    return llm_call(request.prompt)


def execute(gated):
    outcome = harness_run(memory_snippet=gated.snippet)
    return MemoryRunMeasurement(
        eval_result=outcome.eval_result,
        memory_caused_failure=outcome.memory_caused_failure,
        output_hash=outcome.output_hash,
        tool_outputs=(
            tuple(outcome.tool_outputs)
            if outcome.tool_outputs is not None
            else None
        ),
        latency_ms=outcome.latency_ms,
        cost_usd=outcome.cost_usd,
        error=outcome.error,
        trace_uri=outcome.trace_uri,
    )


completion = run_memory_execution(
    store,
    context=context,
    trace_id=current_trace.trace_id,
    task="repair failed search_docs call",
    decide=decide,
    execute=execute,
)
```

模块固定执行顺序为 prepare、decide、finalize、execute、atomic complete。它始终使用 Store 生成的 `decision_id`，不会从异常猜测评估结果、失败归因或执行证据。prepare 之后的 `MemoryRunExecutionError` 会保留原始异常为 cause，并标识 `decision`、`finalization`、`execution` 或 `completion` 阶段。不要把整个一次性辅助函数当作重试入口，因为每次调用都会创建新 request；高级调用方应直接组合 Store 的分阶段 API。

## 安全 Store 工作流

运行时记忆应使用 Store 的两阶段工作流。`prepare_memory()` 检索候选、应用 System Gate 并创建有界 LLM 门控提示；LLM 返回决策载荷后，`finalize_memory()` 重新检查状态、渲染允许的片段，并记录一条关联 Trace 的审计事件。当前 Trace 必须已经存在且 `eval_result="unknown"`。

`allowed_memory_ids` 与 `blocked_memory_ids` 各自最多包含 50 个 ID；`parse_memory_decision()` 和直接调用 `apply_llm_gate_decision()` 都会在逐 ID 工作前检查边界。规范 decision JSON Schema 发布同样的 `maxItems` 契约。

```python
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    query="search_docs null query",
)
result = store.finalize_memory(
    request,
    {
        "use_memory": True,
        "allowed_memory_ids": ["lesson_001"],
        "blocked_memory_ids": [],
        "reason": "The lesson directly matches the current tool failure.",
        "risk": "low",
        "recommended_injection": "short_summary",
    },
    trace_id=trace.trace_id,
)
snippet = result.snippet
completion = store.complete_memory_run(
    trace_id=trace.trace_id,
    decision_id=result.decision_id,
    eval_result="pass",
    tool_outputs=[{"documents": 3}],
    latency_ms=125,
)
completed_trace = completion.trace
sealed_log = completion.usage_log
(audit,) = store.memory_run_audits()
assert audit.status == "complete"
run_metrics = store.memory_run_metrics()
assert run_metrics.complete_count == 1
assert run_metrics.recoverable_count == 0
```

只有 Store 工作流同时提供所有权、重放、陈旧状态、Trace 关联和原子日志保证。

### 原子 Memory Run 完成

执行结束后，优先使用 `complete_memory_run()` 记录结果。它要求准确关联的 `trace_id` 与 `decision_id`，在一个 Store 锁下把同一测量 `eval_result` 应用到 Trace 和 usage log，并返回冻结的 `MemoryRunCompletion` 防御性副本。

两条记录都可以处于 pending，一侧可以已含相同结果以支持部分恢复，也可以两侧完全一致以支持精确重放。结果、失败归因、Trace 字段或关联冲突都会使操作在不改变任一记录的情况下失败。`complete_trace()` 和 `record_decision_outcome()` 仍是供独立生命周期使用的底层接口。

PostgreSQL 同步会在一个事务中更新两行；usage 更新冲突时 Trace 更新也会回滚。返回包装器本身不持久化。

### 原子批量 Memory Run 完成

当评估器产生多条必须全有或全无提交的新结果时，使用 `complete_memory_runs()`：

```python
from trace_backed_memory import MemoryRunResult

completions = store.complete_memory_runs(
    (
        MemoryRunResult(
            decision_id="decision_000002",
            eval_result="pass",
            output_hash="sha256:output",
            tool_outputs=({"documents": 3},),
            latency_ms=125,
        ),
    )
)
```

`MeasuredEvalResult` 只包含 `pass`、`fail` 和 `error`。API 要求非空且 decision ID 唯一的 tuple，从已验证 usage decision 推导 `trace_id`，并按请求顺序返回防御性副本。

证据字段中，`None` 表示省略并保留已有值；显式空 `tool_outputs` tuple 表示写入空列表。同一 Trace 的多个结果必须一致，互不重叠或相同的证据可以合并；任何 outcome、归因、不可变证据或同字段冲突都会在变更前拒绝整个批次。

### Memory Run 审计视图

`memory_run_audits()` 按 `decision_id` 返回冻结的 `MemoryRunAudit` tuple。每条记录包含关联的 `trace_id`、`run_id`、Trace 与 decision 原始结果、失败归因和一个派生状态：

| Trace 结果 | Decision 结果 | 状态 |
|---|---|---|
| 未评估 | 未评估 | `pending` |
| 已测量 | 未评估 | `trace_only` |
| 未评估 | 已测量 | `decision_only` |
| 相同已测量结果 | 相同已测量结果 | `complete` |
| 不同已测量结果 | 不同已测量结果 | `conflict` |

`trace_only` 和 `decision_only` 表示受支持的部分恢复，`pending` 仍需要评估结果，`conflict` 需要人工调查。Store 不会自动修复冲突或替调用方选择权威侧。没有 usage decision 的 Trace 不属于 Memory Run；关联同一 Trace 的多个 decision 仍是独立审计记录。

### Memory Run 补救计划

`memory_run_remediations()` 把每条审计记录转为带 `MemoryRunRemediationAction` 的冻结 `MemoryRunRemediation`：

```python
remediations = store.memory_run_remediations()
automatic_ids = tuple(
    item.decision_id
    for item in remediations
    if item.action == "recover"
)
attribution_ids = tuple(
    item.decision_id
    for item in remediations
    if item.action == "recover_with_attribution"
)
```

映射规则为：`pending -> measure`；通过的 `trace_only` 和所有 `decision_only -> recover`；失败或出错的 `trace_only -> recover_with_attribution`；`conflict -> investigate`；`complete -> none`。失败 Trace-only 记录的归因仍是 `None`，不能把未评估 decision 上的默认 false 当作因果证据。

补救计划只是当前状态建议。写入 API 会重新验证共享 Trace 一致性和陈旧状态；不得自动处理 `investigate`，每个 `recover_with_attribution` 都必须显式提供布尔归因。

### 原子化就绪 Memory Run 恢复

使用无参数的 `recover_ready_memory_runs()` 执行无竞态维护扫描：

```python
completions = store.recover_ready_memory_runs()
```

方法在一个 Store 可重入锁下重新生成补救计划，只选择 `action == "recover"` 的 decision，并在不释放锁的情况下委托给 `recover_memory_runs()`。没有就绪工作时返回空 tuple，因此重复成功扫描是幂等的。它不会猜测需要测量、显式归因或人工调查的状态。

### Memory Run 健康指标

`memory_run_metrics()` 返回冻结的时间点汇总，包括 `decision_count`、五种状态计数、`recoverable_count`、`auto_recoverable_count` 和 `attribution_required_count`。五种状态互斥且总和恒等于 `decision_count`；`recoverable_count` 等于 `trace_only_count + decision_only_count`，也等于自动恢复与需要归因两项之和。

空 Store 的所有字段为零。聚合只扫描 usage log 一次，使用 O(1) 累加空间；排序后的审计与补救展示接口保持不变。

### 安全 Memory Run 恢复

低级写入或中断进程导致只完成一侧时，使用经过审计的 `decision_id` 调用 `recover_memory_run()`：

```python
recoverable = next(
    audit
    for audit in store.memory_run_audits()
    if audit.status == "decision_only"
)
completion = store.recover_memory_run(recoverable.decision_id)
```

方法不接受 `trace_id` 或 `eval_result`，而是从关联记录推导两者。`trace_only` 使用 Trace 结果补齐 decision；`decision_only` 保留已封存结果和 `memory_caused_failure` 并完成 Trace；`complete` 是幂等精确重放。`pending` 和 `conflict` 始终拒绝恢复。

通过的 `trace_only` 可以安全推导记忆未导致失败；失败或出错的 `trace_only` 必须由调用方显式提供 `memory_caused_failure=True` 或 `False`。可选执行证据遵循与 `complete_memory_run()` 相同的不可变槽位规则。

### 原子批量 Memory Run 恢复

需要一次全有或全无修复多个已审计运行时，使用 `recover_memory_runs()`：

```python
recoverable_ids = tuple(
    audit.decision_id
    for audit in store.memory_run_audits()
    if audit.status in {"trace_only", "decision_only"}
)
completions = store.recover_memory_runs(recoverable_ids)
```

第一个参数必须是非空、decision ID 唯一的 tuple，返回结果保持请求顺序。每项都按方法入口状态分类；任何一个 `pending` 或 `conflict` 都会在变更前拒绝整个批次。失败或出错的 `trace_only` 必须在 `memory_caused_failures` 映射中提供准确布尔值。批量接口不接受 Trace 完成证据；需要添加 output hash、tool outputs、latency、cost、error 或 Trace URI 时应使用单项恢复。

### 延迟 Trace 完成

在 memory finalization 之前注册当前 Trace，并写入已知身份、输入、溯源、retrieved context 和 tool call 证据，同时把 `eval_result` 设为 `unknown`。执行后，`complete_trace()` 要求 `pass`、`fail` 或 `error`，并允许一次性补充 `output_hash`、`tool_outputs`、`latency_ms`、`cost_usd`、`error` 和 `trace_uri`。

省略的完成字段会保留初始值；已有非空执行证据可以精确重放，但不能被替换。其他 Trace 字段全部不可变。`complete_trace()` 只修改 Trace，不更新 usage log；正常 Memory Run 应使用 `complete_memory_run()`。

`latency_ms` 必须是 `None`，或位于 0 到 2,147,483,647 之间的整数，两个边界都有效。Store 对记录、快照加载、回调执行和单项/批量完成使用同一验证规则。CLI 中超出范围的整数属于退出码 `3` 的状态错误，格式错误的数字属于退出码 `2` 的输入错误。

规范及包内 Trace Schema 同时声明 `minimum: 0` 与 `maximum: 2147483647`。PostgreSQL 的有符号 `INTEGER` 与非负 CHECK 提供相同物理边界。

### 延迟决策结果封存

`finalize_memory()` 可以在结果已知时立即记录 outcome。`record_decision_outcome()` 是由调用方单独拥有生命周期时使用的 decision-only 底层转换；正常运行时应在执行后调用 `complete_memory_run()`。

可封存的结果是 `pass`、`fail` 和 `error`。初始 `None` 或 `unknown` 表示未评估。已测量结果与 `memory_caused_failure` 组成一个不可分割的 outcome pair：首次可封存一次；相同 pair 精确重放是幂等的；不同结果、不同归因、退回未评估状态或非法 wrong-memory 声明都会在不修改日志的情况下失败。

CLI 提供相同的底层转换：

```text
tbm outcome SNAPSHOT DECISION_ID --eval-result {pass,fail,error} [--memory-caused-failure true|false] [--write]
```

默认只预演。首次封存返回 `changed=true`，精确重放返回 `changed=false`。输出只包含 decision ID、前后 outcome pair 和发布标志，不泄露上下文、原因、风险、记忆 ID、Trace 或工具输出。

### 声明式 Trace 溯源绑定

在 finalization 与底层日志写入时，`repo`、`commit_sha` 和 `tenant` 必须始终与关联 Trace 相同。`branch`、`prompt_version`、`prompt_family`、`tool_schema_version`、`model` 和 `eval_suite` 只有在 context 声明时才参与绑定。声明的工具必须匹配 Trace 中一个精确的纯字符串工具名；非字符串名称会被忽略。

省略可选溯源不会要求 Trace 对应字段也为空。`model_family`、`task_type` 与 `failure_type` 没有等价持久化 Trace 字段，因此保持不绑定。验证发生在 pending request 被消费或 usage log 追加之前，任何不匹配都不会部分写入证据。

### 基准示例泄漏分类

基准示例身份由精确二元组 `(eval_suite, input_hash)` 表示。调用方必须稳定命名 suite，对单个示例执行确定性规范化，计算抗碰撞且保护隐私的哈希，并把它附加到该示例的 Trace。源 Trace 与当前 Trace 只有在表示同一规范示例时才应使用相同哈希。

系统在 LLM 缩窄前比较完整身份对。同一 `eval_suite` 且 `input_hash` 相同的候选会在所有模式下自动阻止，原因固定为：

```text
memory originates from current benchmark example
```

候选的 `source_eval_suite` 与 `source_input_hash` 只在运行时临时补充，不写入 prompt、snippet、快照或 PostgreSQL。身份不完整时系统不会猜测匹配：只有 suite 没有 hash 仍可作为普通上下文；只有 hash 没有 suite 是无效 context；不完整源 Trace 不贡献临时身份。

最终化还会在消费 request 前验证当前身份与 Trace 的绑定。哈希算法、编码、碰撞处理、规范化稳定性和 suite 名称稳定性仍由调用方负责。

### 语义检索

```python
# 分数由调用方的 embedding index 或 reranker 计算。
semantic_scores = {lesson.lesson_id: 0.93}

request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    semantic_scores=semantic_scores,
    max_candidates=10,
    minimum_score=0.70,
)
```

元数据作用域先于排序应用。分数可以使用任意有限数值尺度，但调用方必须归一化为“越大越相关”。同一次调用不能同时使用关键字 `query` 和 `semantic_scores`。`max_candidates` 必须是 1 到 50 的整数。

Store 在过滤前验证所有分数和已存 ID 引用，再通过有界 top-k 流式选择，避免完整排序。结果按分数降序，同分时按 memory ID 升序。System Gate 与 LLM Gate 始终是最终权威，分数不会持久化。

### Git 祖先关系适用性

```python
from trace_backed_memory import capture_commit_ancestry

anchors = store.candidate_commit_anchors(context)
commit_ancestry = capture_commit_ancestry(
    context.commit_sha,
    anchors,
    repo_path=".",
)
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    commit_ancestry=commit_ancestry,
)
```

应在读取 Store 时发现 anchor，在 Store 锁外捕获 Git 证据，再调用 `prepare_memory()`。不可变证据绑定到准确的 `context.commit_sha`：lesson 锚定源 case 的 `fix_commit_sha`，failure-case memory 锚定源 `commit_sha`。Project Policy 没有 commit anchor，因此只跳过 ancestry 过滤，仍需通过普通作用域与两级门控。

`capture_commit_ancestry()` 对每个 anchor 运行 `git merge-base --is-ancestor`。退出码 0 记录 `True`，1 记录 `False` 并排除无关历史，其他错误停止工作流。若调用方提供证据，必须覆盖每个已发现 anchor；缺少关系会 fail closed。省略 `commit_ancestry` 则保持原检索行为。

单次捕获最多提交 1,000 个 anchor，并在去重与启动 Git 命令前检查。默认子进程使用 30 秒超时、二进制管道和 UTF-8 replacement 解码，stdout/stderr 各最多保留 64 KiB。证据仅存在于 request，不写入快照或 PostgreSQL。

## 端点感知 PR 报告

PR 改变 trace-backed 元数据值时，使用不可变 `PRChangeSet`。每项为 `(field_name, old_value, new_value)`，只支持 `prompt_version`、`prompt_family`、`tool`、`tool_schema_version`、`model` 和 `eval_suite`。`new_value` 必须与变更后 `MemoryContext` 精确相等，包括 `None`。

对 PR 报告筛选而言，`repo` 与 `tenant` 是必须精确匹配的 Trace 过滤条件，但这不构成多租户授权边界。Store 会把每个变化字段同时匹配完整旧端点和完整新端点，排除混合配置，并把报告来源标为 `old`、`new` 或 `both`。因为六个字段必须唯一，`PRChangeSet` 最多接受 6 项，且在扫描历史 case 前检查基数。

旧的 `changed_fields=[...]` 仍保留宽泛、仅字段名的兼容行为；其 warning 工作使用有界去重。精确值感知的 `model_family` 不受支持，因为 Trace 没有该溯源字段。change set 与 endpoint provenance 都只存在于报告，不持久化。

典型流程是用同一个 change set 发现 anchor、捕获 ancestry，再生成报告：

```python
from trace_backed_memory import PRChangeSet, capture_commit_ancestry

change_set = PRChangeSet(
    (
        ("prompt_version", "planner-v1", "planner-v2"),
        ("tool_schema_version", "search-docs-v1", "search-docs-v2"),
    )
)
anchors = store.pr_report_commit_anchors(context, change_set=change_set)
commit_ancestry = capture_commit_ancestry(
    context.commit_sha,
    anchors,
    repo_path=".",
)
report = store.pr_memory_report(
    context,
    change_set=change_set,
    commit_ancestry=commit_ancestry,
)
```

CLI 的 `tbm pr-report` 是只读适配器。它严格解析 context 与 change-set JSON，在指定 `--repo-path` 捕获 Git 证据，并输出 `commit_ancestry` 与 `report`。它不接受宽泛 legacy change list、调用方编写的 ancestry 或 `--write`。

## 底层 System Gate 辅助函数

高级调用方可以直接使用确定性的 `system_gate()`：

```python
from trace_backed_memory import MemoryContext, MemoryItem, system_gate

context = MemoryContext(
    mode="repair",
    repo="agent-harness",
    tenant="tenant_a",
    branch="main",
    commit_sha="abc123",
    prompt_family="planner",
    tool="search_docs",
    tool_schema_version="search_docs_v2",
    eval_suite="tool_calling_regression",
    failure_type="invalid_tool_argument",
)

candidates = [
    MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={
            "tenant": "tenant_a",
            "tool": "search_docs",
            "prompt_family": "planner",
        },
        text="When calling search_docs, always provide a non-empty query.",
        source_case_id="case_001",
    )
]

allowed, blocked = system_gate(context, candidates)
```

System Gate 负责严格来源、tenant-aware 作用域、状态、记忆类型、置信度、敏感性、评估泄漏和模式检查。它不能被后续 LLM decision 绕过。

## 已实现的公共 API

包根导出完整的模型、生命周期、门控、持久化、资源、捕获和执行接口。下面给出一条从失败 Trace 到已验证 lesson，再到运行时使用与原子完成的最小主流程：

```python
from dataclasses import replace

from trace_backed_memory import (
    Trace,
    TraceBackedMemoryStore,
    capture_trace_metadata,
    draft_failure_case_from_trace,
    lesson_from_failure_case,
    load_failure_taxonomy,
    parse_memory_context,
    review_failure_case,
    verify_failure_case,
)

store = TraceBackedMemoryStore()
metadata = capture_trace_metadata(repo_path=".")
if metadata.dirty:
    raise RuntimeError("拒绝从未提交的工作区生成 active memory")
taxonomy = load_failure_taxonomy()

trace = store.record_trace(
    Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha=metadata.commit_sha,
        repo=metadata.repo,
        tenant="tenant_a",
        branch=metadata.branch,
        dirty=metadata.dirty,
        eval_suite="tool_calling_regression",
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        error="Invalid argument: query is required",
    )
)
case = verify_failure_case(
    review_failure_case(
        draft_failure_case_from_trace(
            trace,
            case_id="case_001",
            taxonomy=taxonomy,
        ),
        reviewed_by="memory-reviewer",
        root_cause="planner prompt 遗漏了 search_docs query 契约",
    ),
    fix="added schema example",
    fix_commit_sha="def456",
    regression_passed=True,
)
store.add_failure_case(case)
lesson = store.add_lesson(
    lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={
            "repo": metadata.repo,
            "tenant": "tenant_a",
            "tool": "search_docs",
        },
    )
)

current_trace = store.record_trace(
    replace(
        trace,
        trace_id="trace_002",
        run_id="run_002",
        eval_result="unknown",
        error=None,
    )
)
context = parse_memory_context(
    {
        "mode": "repair",
        "repo": metadata.repo,
        "tenant": "tenant_a",
        "commit_sha": metadata.commit_sha,
        "tool": "search_docs",
        "failure_type": case.failure_type,
        "eval_suite": "tool_calling_regression",
    }
)
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    query="search_docs null query",
)
result = store.finalize_memory(
    request,
    {
        "use_memory": True,
        "allowed_memory_ids": [lesson.lesson_id],
        "blocked_memory_ids": [],
        "reason": "The lesson matches the current tool failure.",
        "risk": "low",
        "recommended_injection": "short_summary",
    },
    trace_id=current_trace.trace_id,
)
completion = store.complete_memory_run(
    trace_id=current_trace.trace_id,
    decision_id=result.decision_id,
    eval_result="pass",
)

store.save_json("memory-store.snapshot.json")
store.save_lessons_yaml("lessons.active.yaml", overwrite=False)
```

当前实现包括：

- Trace、Failure Case、Lesson、Project Policy、usage log、Memory Run 与派生指标模型。
- Git 元数据及有界 ancestry 捕获，包含 repo、commit、branch 和 dirty state。
- 失败分类、失败案例草稿、人工复核、回归验证和 active lesson 生命周期。
- 由 System Gate 与 LLM Gate 组成的不可绕过两级运行时门控。
- 关键字检索、有界调用方语义分数、Git ancestry 过滤和端点感知 PR 报告。
- 单项/批量 Memory Run 原子完成、审计、补救、就绪扫描与安全恢复。
- 严格 JSON 快照、简单 active lesson YAML、100 项 zip-safe 包资源和原子文件发布。
- 快照 advisory lock，以及 SQLite schema 版本 `1` / PostgreSQL schema 版本 `2` 的增量事务存储库。
- JSON Schema、PostgreSQL 约束、快照与发行包的跨层契约测试。

本轮 review 加固还包括：Failure Case 必须来自 `fail`/`error` Trace，verify 前必须完成 reviewer/root-cause/timestamp 证据，dirty source 不能激活 Lesson；LLM decision 响应最多 64 KiB、1,000 个 JSON 节点、深度 20，reason 最多 2,000 字符；LLM 未选择的系统候选会进入 blocked 审计；`short_summary` 与 `full_case_summary` 使用不同渲染器；关键词检索支持 Unicode；prepare/finalize 会确定性保留前 50 个系统允许候选，并记录其余候选的限制原因。

每次 prepare 最多审计 1,000 个候选，所有进程内 pending request 合计最多引用 100,000 个候选；其中只有确定性的前 50 个 System Gate 允许项会进入 LLM Gate。PostgreSQL 同时禁止通过直接 SQL 改绑 Failure Case 与 Lesson 来源，SQLite 同实例操作已串行化且顶层回滚保留主异常。

所有 Store 输入在复制和提交前验证。嵌套 JSON 会拒绝非字符串对象键、非有限数、引用环、过深结构及预算溢出；失败操作不消耗 ID，也不留下部分变更。低级辅助函数仍公开，但只有 Store 工作流承诺所有权、重放、陈旧状态、Trace 关联与原子日志语义。

`capture_trace_metadata()` 要求注入 runner 的每个结果都是字符串。空 commit SHA、空仓库根、非字符串输出、超过 512 字符的 commit/branch/repository 名称或命令错误都会在非法元数据进入 Trace 前转换为 `TraceMetadataCaptureError`。detached HEAD 的空 branch 和干净工作树的空 status 仍然有效。

## 结果感知指标

`pass`、`fail` 和 `error` 都是已评估结果，其中 `error` 是已评估但未通过；`unknown` 与 `None` 未评估，不进入通过率分母。`evaluated_with_memory_count` 与 `evaluated_without_memory_count` 给出两个分母，`unevaluated_decision_count` 标识仍缺少可用结果的 decision。三者之和等于 `decision_count`。

评估在 memory finalization 之后完成时，应先用 `complete_memory_run()` 同时完成 Trace 与封存 decision，再读取指标或持久化审计。只有在调用方故意独立管理 Trace 生命周期时才使用 `record_decision_outcome()`。

这些是 decision 计数，不是逐记忆的因果效果估计。usage log 中 `used_memory_ids` 非空的 decision 才归入 with-memory。指标均为派生值，不单独持久化。

### 逐记忆观测

`memory_outcome_metrics()` 为每个已存 Failure Case、Lesson 和 Project Policy 返回按 memory ID 排序的 tuple，包括零观测记录。`candidate_count`、`used_count` 与 `blocked_count` 分别表示检索、最终使用和阻止频率；阻止同时覆盖 System Gate 与 LLM 缩窄。

对实际使用，还会提供 `evaluated_use_count`、`passed_use_count`、`failed_or_errored_use_count`、`unevaluated_use_count` 和 `observed_pass_rate`。这些只是观测关联，不是因果估计；一次运行使用多个 memory ID 时，同一结果会计入每个 ID。API 不会从 decision 级 `memory_caused_failure` 自动推导逐记忆归因。

## 生产就绪边界

当前 snapshot version 2 仍不是多租户在线服务契约。以下事项需要 schema v3 / PostgreSQL schema v3 才能完整实现：结构化 regression Trace/run/evaluator 证据与 commit 关系、稳定 `repository_id` 和显式 global/repository/tenant scope、可持久化或签名的 GateRequest（含幂等、过期和崩溃恢复；当前进程内请求已经具备容量限制、显式取消、Trace/run 绑定和最终 `request_id` 审计关联）、可重放的 retriever/gate/renderer/hash 审计，以及显式 required/disabled 且记录绕过原因的 ancestry policy。

在这些契约完成前，应将本项目用于单进程 harness 组件或参考实现，而不是不受信任的共享多租户 memory service。

本次 Alpha 加固保持 snapshot version 2，并将 PostgreSQL schema 推进到版本 2。既有 version-2 snapshot 中 verified 但缺少 review 证据的 case 必须先补齐才能加载；既有 PostgreSQL schema-version-1 安装必须先应用打包的 `schemas/postgres-v1-to-v2.sql`，再进行同步。

## 仓库布局

下方列出当前关键路径；历史设计计划文件从略。

```text
.
|-- README.md
|-- README.zh-CN.md
|-- AGENTS.md
|-- .agents/skills/
|   |-- maintain-trace-backed-memory/
|   `-- use-trace-backed-memory/
|-- docs/
|   |-- architecture.md
|   |-- architecture.zh-CN.md
|   |-- development.md
|   |-- development.zh-CN.md
|   |-- index.md
|   |-- index.zh-CN.md
|   |-- integrations/
|   |   |-- codex.md
|   |   `-- codex.zh-CN.md
|   |-- migrations/
|   |   |-- snapshot-v3-preflight*.md
|   |   `-- v3-staging-bundles*.md
|   |-- protocols/
|   |   |-- agent-v1.md
|   |   |-- agent-v1.zh-CN.md
|   |   |-- authorization-v3.md
|   |   |-- authorization-v3.zh-CN.md
|   |   |-- authenticated-service-v3.md
|   |   |-- authenticated-service-v3.zh-CN.md
|   |   |-- authenticated-gate-service-v3.md
|   |   |-- authenticated-gate-service-v3.zh-CN.md
|   |   |-- gate-recovery-worker-v3.md
|   |   |-- gate-recovery-worker-v3.zh-CN.md
|   |   |-- sqlite-gate-evidence-v3.md
|   |   |-- sqlite-gate-evidence-v3.zh-CN.md
|   |   |-- audit-recovery-v3.md
|   |   |-- audit-recovery-v3.zh-CN.md
|   |   |-- evidence-v3.md
|   |   |-- evidence-v3.zh-CN.md
|   |   |-- memory-revision-v3.md
|   |   |-- memory-revision-v3.zh-CN.md
|   |   |-- gate-evaluation-v3.md
|   |   |-- gate-evaluation-v3.zh-CN.md
|   |   |-- outcome-v3.md
|   |   |-- outcome-v3.zh-CN.md
|   |   |-- sqlite-outcome-v3.md
|   |   |-- sqlite-outcome-v3.zh-CN.md
|   |   |-- sqlite-outcome-attribution-v3.md
|   |   |-- sqlite-outcome-attribution-v3.zh-CN.md
|   |   |-- postgres-outcome-v3.md
|   |   |-- postgres-outcome-v3.zh-CN.md
|   |   |-- postgres-outcome-attribution-v3.md
|   |   |-- postgres-outcome-attribution-v3.zh-CN.md
|   |   |-- retrieval-snapshot-v3.md
|   |   |-- retrieval-snapshot-v3.zh-CN.md
|   |   |-- gate-session-v3.md
|   |   |-- gate-session-v3.zh-CN.md
|   |   |-- replay-v3.md
|   |   `-- replay-v3.zh-CN.md
|   |-- product-program.md
|   |-- product-program.zh-CN.md
|   |-- product.en.md
|   |-- product.md
|   |-- usage-policy.md
|   `-- usage-policy.zh-CN.md
|-- examples/
|   |-- agent_*.example.json
|   |-- authorization_*_v3.example.json
|   |-- audit_event_v3.example.json
|   |-- decision_replay_manifest_v3.example.json
|   |-- structured_regression_evidence_v3.example.json
|   |-- gate_session_v3.example.json
|   |-- injection_artifact_v3.example.json
|   |-- quickstart.py
|   |-- snapshot_v3_migration_*.example.json
|   |-- trace.example.json
|   |-- failure_case.example.json
|   |-- lesson.example.json
|   |-- memory_context.example.json
|   |-- memory_revision_v3.example.json
|   |-- retrieval_snapshot_v3.example.json
|   |-- recovery_action_v3.example.json
|   |-- semantic_gate_artifact_v3.example.json
|   |-- semantic_gate_attempt_v3.example.json
|   |-- system_gate_evaluation_v3.example.json
|   |-- project_policy.example.json
|   |-- memory_usage_log.example.json
|   |-- outcome_attribution_v3.example.json
|   |-- run_outcome_v3.example.json
|   `-- memory_decision.example.json
|-- memory/
|   |-- lessons.example.yaml
|   `-- failure_taxonomy.yaml
|-- schemas/
|   |-- agent_*.schema.json
|   |-- authorization_*_v3.schema.json
|   |-- audit_event_v3.schema.json
|   |-- decision_replay_manifest_v3.schema.json
|   |-- gate_session_v3.schema.json
|   |-- injection_artifact_v3.schema.json
|   |-- structured_regression_evidence_v3.schema.json
|   |-- postgres-v1-to-v2.sql
|   |-- postgres-v2-lock-order-hotfix.sql
|   |-- postgres-v3-gate-session*.sql
|   |-- postgres-v3-outcome*.sql
|   |-- postgres-v3-replay*.sql
|   |-- postgres-v3-audit*.sql
|   |-- postgres-v3-authorization*.sql
|   |-- postgres-v3-semantic-gate*.sql
|   |-- postgres-v3-semantic-gate-artifacts*.sql
|   |-- postgres-v3-staging*.sql
|   |-- postgres.sql
|   |-- snapshot_v3_migration_*.schema.json
|   |-- sqlite-v3-audit.sql
|   |-- sqlite-v3-authorization.sql
|   |-- sqlite-v3-gate-session.sql
|   |-- sqlite-v3-outcome-attribution.sql
|   |-- sqlite-v3-outcome.sql
|   |-- sqlite-v3-migration.sql
|   |-- sqlite-v3-replay.sql
|   |-- sqlite-v3-semantic-gate-artifacts.sql
|   |-- sqlite-v3-semantic-gate.sql
|   |-- sqlite.sql
|   |-- trace.schema.json
|   |-- failure_case.schema.json
|   |-- lesson.schema.json
|   |-- project_policy.schema.json
|   |-- memory_usage_log.schema.json
|   |-- outcome_attribution_v3.schema.json
|   |-- run_outcome_v3.schema.json
|   |-- memory_store_snapshot.schema.json
|   |-- memory_context.schema.json
|   |-- memory_revision_v3.schema.json
|   |-- retrieval_snapshot_v3.schema.json
|   |-- recovery_action_v3.schema.json
|   |-- semantic_gate_artifact_v3.schema.json
|   |-- semantic_gate_attempt_v3.schema.json
|   |-- system_gate_evaluation_v3.schema.json
|   `-- memory_decision.schema.json
|-- src/trace_backed_memory/
|   |-- _resources/
|   |-- _ingestion.py
|   |-- _timestamps.py
|   |-- __main__.py
|   |-- __init__.py
|   |-- agent.py
|   |-- capture.py
|   |-- cli.py
|   |-- contracts_v3.py
|   |-- execution.py
|   |-- extraction.py
|   |-- authorization_v3.py
|   |-- service_v3.py
|   |-- gate_service_v3.py
|   |-- gate_completion_v3.py
|   |-- gate_worker_v3.py
|   |-- completion_outbox_v3.py
|   |-- completion_outbox_worker_v3.py
|   |-- audit_v3.py
|   |-- evidence_v3.py
|   |-- gate_session_v3.py
|   |-- gate_evaluation_v3.py
|   |-- lifecycle.py
|   |-- locking.py
|   |-- mcp_entry.py
|   |-- mcp_server.py
|   |-- migration_v3.py
|   |-- models.py
|   |-- memory_revision_v3.py
|   |-- outcome_v3.py
|   |-- sqlite_outcome_attribution_v3.py
|   |-- sqlite_outcome_v3.py
|   |-- sqlite_completion_outbox_v3.py
|   |-- postgres_outcome_v3.py
|   |-- postgres_outcome_attribution_v3.py
|   |-- postgres_completion_outbox_v3.py
|   |-- retrieval_v3.py
|   |-- policy.py
|   |-- postgres.py
|   |-- postgres_gate_session_v3.py
|   |-- postgres_replay_v3.py
|   |-- postgres_audit_v3.py
|   |-- replay_v3.py
|   |-- sqlite.py
|   |-- sqlite_audit_v3.py
|   |-- sqlite_authorization_v3.py
|   |-- sqlite_gate_session_v3.py
|   |-- sqlite_replay_v3.py
|   |-- sqlite_v3.py
|   |-- py.typed
|   |-- resources.py
|   `-- store.py
`-- tests/
    |-- test_agent.py
    |-- test_authorization_v3.py
    |-- test_service_v3.py
    |-- test_gate_service_v3.py
    |-- test_gate_worker_v3.py
    |-- test_completion_outbox_v3.py
    |-- test_completion_outbox_worker_v3.py
    |-- test_audit_v3.py
    |-- test_evidence_v3.py
    |-- test_contracts_v3.py
    |-- test_gate_session_v3.py
    |-- test_gate_evaluation_v3.py
    |-- test_mcp_server.py
    |-- test_migration_v3.py
    |-- test_memory_revision_v3.py
    |-- test_outcome_v3.py
    |-- test_sqlite_outcome_attribution_v3.py
    |-- test_sqlite_outcome_v3.py
    |-- test_sqlite_completion_outbox_v3.py
    |-- test_postgres_outcome_v3.py
    |-- test_postgres_outcome_attribution_v3.py
    |-- test_postgres_completion_outbox_v3.py
    |-- test_retrieval_v3.py
    |-- test_postgres_gate_session_v3.py
    |-- test_postgres_replay_v3.py
    |-- test_postgres_audit_v3.py
    |-- test_replay_v3.py
    |-- test_sqlite_gate_session_v3.py
    |-- test_sqlite_audit_v3.py
    |-- test_sqlite_authorization_v3.py
    |-- test_sqlite_replay_v3.py
    |-- test_quickstart.py
    |-- test_sqlite_v3.py
    |-- test_verify_tool.py
    |-- test_capture.py
    |-- test_cli.py
    |-- test_execution.py
    |-- test_examples_and_schema.py
    |-- test_extraction.py
    |-- test_ingestion.py
    |-- test_lifecycle.py
    |-- test_locking.py
    |-- test_packaging.py
    |-- test_postgres_integration.py
    |-- test_postgres_repository.py
    |-- test_policy.py
    |-- test_readme_api.py
    |-- test_resources.py
    |-- test_sqlite_repository.py
    |-- test_store.py
    |-- verify_distribution.py
    `-- ...
```
