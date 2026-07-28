# 记忆使用策略

[English](usage-policy.md) | **简体中文**

## 总则

记忆不是默认上下文。记忆是历史经验，使用前必须经过过滤、作用域匹配和批准。

```text
raw trace -> failure case -> verified lesson -> gated runtime memory
```

## SQLite 持久化边界

`SQLiteMemoryRepository` 持久化与本地 Store 相同的受门控记录，不改变检索或注入资格。它使用 Python 标准库 `sqlite3`，不需要额外依赖，并要求规范或包内 `schemas/sqlite.sql` 的 schema 版本 1。`connect(..., initialize=True)` 是初始化新文件数据库的便捷路径。

同步是增量且原子的。顶层操作使用 `BEGIN IMMEDIATE`；已有 caller-owned transaction 时使用 savepoint，不拥有外层 commit/rollback。sync 保留只存在于数据库的记录，只允许文档定义的 Trace、usage outcome、Failure Case、Lesson 与 Project Policy 前向转换，执行 Failure Case 到 Lesson 的 obsolete 级联，并在冲突时回滚完整操作。同一个 Repository 实例用 `RLock` 串行化 `sync()`、`load()` 与 `close()`；顶层回滚清理必须保留主异常并重试，再次失败则即使连接由调用方传入也会关闭，防止之后提交部分事务。

load 在一个读事务内检查 schema 版本 1；每集合超过 100,000 条、总计超过 250,000 条、最大单条或累计规范 JSON payload 超过 64 MiB 时拒绝。精确边界有效；只有完成 `TraceBackedMemoryStore` 重建与校验后才返回，失败后连接仍可复用。

SQLite 行保存稳定 ID 与规范 JSON payload envelope。领域和跨记录不变量以 Store 为准；直接 SQL 修改 payload 不属于支持契约，可能导致下一次 load 或 sync 失败。持久化应使用文件数据库。SQLite 面向本地 harness、CI 与单机工具；需要数据库侧 JSONB、trigger、row lock、共享 ID 强制和多客户端负载时选择 PostgreSQL。

## PostgreSQL 持久化边界

可选的同步 PostgreSQL Repository 持久化与本地 Store 相同的受门控记录；它不会让 raw Trace 自动具备注入资格，也不能绕过 System Gate 或 LLM Gate。安装 `trace-backed-memory[postgres]`，把规范 `schemas/postgres.sql` 应用到新的 `public` schema，并使用 schema 版本 2。既有版本 1 安装必须先应用包内 `schemas/postgres-v1-to-v2.sql`。PostgreSQL 必须为 12+。

同步必须是增量且原子的：保留提交 Store 中不存在的数据库记录，只允许支持的前向生命周期更新，并在不可变冲突时回滚完整事务。pending Trace 只能从 `unknown` 前进到已测量结果；usage decision 只能从 `NULL`/`unknown` 前进到一个测量 outcome pair。其他受保护字段不可变。

加载必须先按序对五张持久化表获取 `SHARE` 锁，使一次 Store 不会混合不同提交时刻。锁持有期间先执行 count 预检：每集合不超过 100,000 条，总计不超过 250,000 条；然后执行 loaded-row UTF-8 JSON payload 预检：最大单行与五表合计都不超过 64 MiB。Failure Case、Lesson 和 Project Policy projection 只排除 selector 不读取的 `updated_at`；Trace 和 usage decision 保留全部物理列。

同步在规范比较前对所有已有目标行使用 `FOR UPDATE`。缺失主键不能预锁，因此每个 absent-row INSERT 必须位于 nested savepoint；并发同主键 `23505` 或 registry 精确 `P0001` 信号触发重新选择并锁定。精确重放是 `unchanged`，合法前向转换是 `updated`，保护字段差异是 `PostgresConflictError`。Failure Case 的 source Trace/commit 与 Lesson 的 source Case 即使通过直接 SQL 也不可改绑。没有目标行的碰撞和其他 driver 错误保持 `PostgresPersistenceError`。

count/payload 预检只限制运行时 load。payload 是紧凑 PostgreSQL loaded-row JSON，不是缩进快照文件大小。边界值有效；超限必须返回已净化的 `PostgresPersistenceError`，不构造部分 Store，并保持连接可复用。

持久化前，身份、linkage、必需 failure 文本、Lesson/Policy scope、Memory Context 字符串和 usage-audit mapping 键值都必须至少包含一个非空白字符。接受值保持原字节，不 trim 或 normalize。规范与包内 Schema 使用相同 `pattern: "\\S"`。

PostgreSQL schema 版本 2 的 `btrim` 只覆盖普通空格，比 Python/JSON Schema 规则窄。Repository sync 总是接收已经过 Store 验证的数据；直接 SQL 写入其他空白字符组成的值不属于支持写入契约，可能导致 load 失败。

持久化时间戳必须采用带显式 `Z` 或数字 UTC offset 的严格 RFC 3339；小数秒最多六位。生命周期 API、snapshot import、SQLite、PostgreSQL 和规范 JSON Schema 都必须拒绝亚微秒精度，不能静默截断。

显式 `SHARE` 锁要求 schema owner 或具备 `UPDATE`、`DELETE`、`TRUNCATE` 表级权限的角色。在调用方事务中，Repository 使用 savepoint，成功锁会持续到外层 commit/rollback；没有外层事务时正常提交。

## PostgreSQL 测试运行策略

普通本地 pytest 中 PostgreSQL server 工具是可选的。缺少 `initdb`、`pg_ctl`、`psql`，或当前用户不能合法运行 `initdb` 时，数据库测试应带诊断跳过。

CI 的独立 PostgreSQL job 必须设置 `TBM_REQUIRE_POSTGRES=1`，使这两类环境条件成为失败；同时预检三个可执行文件与 `psycopg`，并在私有临时集群运行 integration 和 repository 测试。完整测试套件还必须在 Windows 独立运行。此开关只能存在于测试基础设施，不得进入运行时代码或持久化数据。

## 打包资源策略

安装后需要规范 Schema、example 或 memory support 文件时，只能使用 `packaged_resources()`、`read_packaged_resource()` 或 `export_packaged_resource()`。不得推断包文件系统路径或退回当前 checkout。资源名必须来自固定白名单，未知名称和遍历形式在包访问前拒绝。

99 个安装副本必须与顶层编辑源字节一致。wheel 与 sdist 验证应在缺失、额外或内容变化时失败。`PackagedResource` metadata 来自安装字节，包含 SHA-256 与大小。无路径 `load_failure_taxonomy()` 使用包内规范 taxonomy；显式路径仍按调用方文档处理。白名单包含 fresh-install PostgreSQL schema 版本 2、独立原子 `schemas/postgres-v1-to-v2.sql` migration、可重复执行的 `schemas/postgres-v2-lock-order-hotfix.sql` operator 脚本、`tbm.agent.v1` 的 Schema/JSON 示例/quickstart，以及 v3 迁移 preflight、不可激活 bundle、隔离 staging、显式 rollback、GateSession/内容寻址重放/授权/结构化 evidence/MemoryRevision contract 资源、隔离 SQLite GateSession/replay/audit/authorization/MemoryRevision/Semantic Gate ledger 与规范化 entity-registry DDL、隔离 PostgreSQL GateSession/entity-registry install/rollback，以及隔离 PostgreSQL replay/audit/authorization/MemoryRevision/Semantic Gate ledger install/fail-closed rollback。

CLI 资源读取输出确定性 JSON。export 默认拒绝现有目标，只在显式 `--overwrite` 时替换，并通过同目录临时文件发布。名称错误映射退出码 2，写错误映射退出码 4；导出已经提交后 stdout 关闭仍视为成功。

## 证据摄取完整性

失败分类只能使用显式结构化失败文本：依次读取 `Trace.error`、`tool_calls` 顶层 `error`、`tool_outputs` 顶层 `error`。工具名称永远不能选择 taxonomy；任意工具字段、成功结果和嵌套文本可能包含历史错误或示例，不得搜索其中关键字。

同一 call/output 只有存在 truthy 顶层 `error` 时，其名称才可标记症状。显式 `invalid argument` 与狭义的 `required argument`、`required parameter`、`required field`、`required property` 属于参数错误；单独的 `required` 不得把权限或认证错误误分类。

调用方 failure-taxonomy 与 active-lessons YAML 必须遵循仓库受限格式。重复 taxonomy description、Lesson 字段或 scope 键都是非法输入，不能依赖 last-key-wins。完整文档必须先解析，所有 Lesson 候选按暂存状态构造和验证，然后一次提交；任何重复 ID 或后续语义错误都不能部分导入。

调用方 JSON 使用同一规则。`load_json()`、`parse_memory_context()`、`parse_memory_decision()` 与 CLI JSON 文件解析必须在任意层级、转换为 mapping 前拒绝重复对象键。

本地快照与 active Lesson 只能通过 `save_json()`、`save_lessons_yaml()` 持久化。两者都写同目录临时文件、规范 LF、flush、`os.fsync()` 并原子发布。覆盖使用 `os.replace()`，no-replace 使用 `os.link()`。POSIX 在发布并清理临时名后同步父目录；发布前失败保留旧目标，发布后目录同步失败作为持久性不确定错误传播。

新 Lesson 导出使用 `lesson_text: |`。导入可以接受历史 `>`，但必须精确保留空行、首尾 LF、行内空格及适配器历史的 literal line 行为，不得宣称完整 YAML folding/chomping。

## 有界本地文档摄取

每个调用方快照、active Lesson、failure taxonomy、measurement manifest 和 tool-output 文件都必须通过单句柄读取，并在 UTF-8 解码前检查字节预算：快照 64 MiB，Lesson/CLI JSON 8 MiB，taxonomy 1 MiB。

同时限制：快照每集合 100,000 条且总计 250,000；Lesson 10,000 条；taxonomy 1,000 类；CLI JSON 10,000 顶层项、100,000 节点、深度 100。Python 调用方只有在可信离线迁移时才可把单项限制显式设为 `None`；CLI 始终保留安全默认值。

`Trace.retrieved_context`、`Trace.tool_calls` 和 `Trace.tool_outputs` 是同一个实时结构化 JSON 域：合计最多 100,000 节点和 8 MiB 对象键/字符串 UTF-8 文本，深度 100。宽容器在扩展遍历前拒绝，非法 UTF-8 字符串在复制前拒绝。该限制覆盖 record、completion、snapshot import 和 PostgreSQL load，且不可配置。

快照 usage-log 验证必须使用 load-local `decision_id`、known memory、legacy `run_id` 与 tool-name 索引，以及 per-log relationship sets，保持平均 O(n) 且不改变诊断顺序。

实时 Store 必须维护不持久化的 decision ID 位置/后缀索引和 `run_id` 到有序 `trace_id` 索引。ID 分配、重复检查和单项查找保持平均 O(1)，失败写入不消耗 ID，索引不进入快照。

实时 usage-log memory 存在性检查只访问被引用 ID，平均 O(r)，不得复制完整 catalog。`metrics()` 与 `memory_run_metrics()` 各使用一次 usage-log 扫描和 O(1) 累加空间。

`recover-batch` 还必须在 duplicate detection 与快照加载前把 decision ID 和 attribution options 分别限制为 10,000 项。CLI 不提供 opt-out，也不持久化预算。

## 快照操作 CLI

本地快照操作使用 `tbm` 或 `python -m trace_backed_memory`。CLI 是 operations adapter，不是新的策略或持久化层；所有 validate、stats、audit、metrics、remediation、completion、recovery 与 lifecycle 规则必须复用 Store。

`lessons export` 只导出 active-only artifact，默认拒绝现有目标以及源快照的任意别名，并让 `save_lessons_yaml()` 拥有规范序列化与原子发布。`lessons import` 默认完整 dry-run，固定使用 8 MiB 与 10,000 条限制，只接受 active 状态，禁止 upsert、跳过 provenance 或部分接收文档。

`obsolete` 只用于单项前向失活，必须显式给出 kind；不得重新激活、虚构 actor/reason、输出 memory 文本或通过单项循环实现批量。`obsolete-batch` 读取严格的 `MemoryObsolescenceRequest` 数组，只调用一次 `obsolete_memories()`，由 Store 全量暂存显式记录和 cascade 后一次提交。

`obsolete`、`obsolete-batch`、`complete`、`complete-batch`、`recover`、`recover-batch`、`recover-ready` 默认都是 dry-run。只有显式 `--write` 且整个操作成功时，才可用 `save_json()` 原子替换同一快照。

每个快照 `--write` 必须在 load 前获取规范 sibling `.tbm.lock` 排他建议锁，并持有到 `save_json()` 发布结束，在 stdout 前释放。sidecar 持久存在且只含一个 placeholder byte；路径与 descriptor 必须是同一单链接普通文件。符号链接、Windows reparse point、硬链接和特殊文件必须在写目标或加载快照前拒绝。争用最多等待 30 秒。

Python 写入方必须用公开 `snapshot_write_lock(snapshot_path, timeout_seconds=...)` 包围完整 load、mutate、save 事务。该锁是建议性、跨进程、非重入的，不是 Store `RLock`，也不替代 SQLite/PostgreSQL transaction。

`complete` 只提交准确关联 Trace/decision 的新测量结果，要求显式 `--eval-result`，不得推断 outcome、ID、因果归因或执行证据。可选 tool-output 文件必须是严格 UTF-8 JSON 对象数组，省略参数必须保留 Store omission 语义。

`complete-batch` 只接受非空、顺序明确的 `MemoryRunResult` 对象数组，不允许 caller 传 Trace ID。必须只调用一次 `complete_memory_runs()`，由 Store 推导 linkage，并对重复、共享 Trace、证据、重放与归因全有或全无验证。

`recover-ready` 只能选择 remediation action `recover`。单项恢复只有 operator 显式给出时才传 `memory_caused_failure`。批量 decision ID 必须唯一，任何非法 attribution 都拒绝整个批次。

每个 attribution 必须从最后一个 `=` 分割，完整非空前缀作为 decision ID，后缀只接受严格小写 `true` 或 `false`。缺少/空组件、非法布尔、未请求 ID 和重复 attribution 都是退出码 2 的输入错误。

成功输出一个确定性 JSON；失败向 stderr 输出一个结构化 JSON，不带 traceback。退出码 0、1、2、3、4 分别表示成功、内部错误、输入错误、状态拒绝和写失败。错误文本最多 2,048 字符。必须先序列化成功结果再持久化，提交后 stdout 关闭不应触发不安全重试。

## 适用模式

| 模式 | 默认 | 允许的记忆 | 阻止的记忆 |
|---|---:|---|---|
| debug | 使用 | Trace 摘要、verified Failure Case、修复历史 | secrets、无关 raw Trace |
| repair | 使用 | verified Lesson、历史修复、tool/prompt policy | draft case、弱猜测 |
| regression | 使用 | commit/eval 历史、PR memory report | 无关项目记忆 |
| planning | 谨慎 | project/tool policy、procedural Lesson | raw Trace |
| eval | 通常跳过 | prompt contract、tool schema policy | 历史答案、gold label、evaluator comment |
| production | 最少 | active、verified、scope-matched Lesson | raw Trace、draft、sensitive memory |

## System Gate

运行时 context 应先通过 `parse_memory_context()`。它要求 `mode`、`repo`、`commit_sha`，验证支持模式，并只保留已知的非空白字符串字段。直接 helper 调用必须遵循相同边界：候选和注入输入是唯一 `MemoryItem` 列表，block reason 是字符串 mapping，task 非空，summary 是字符串，query 为字符串或 `None`。

Memory 必须满足 active/verified 状态、受支持 memory type、已知且匹配的 scope、合法 repo/branch/tenant、非 obsolete、非 sensitive、非 eval-leaking，并且存在 source case、Trace 或 policy。

context 指定工具时，Failure Case memory 的 source Trace 还必须包含该同名工具；缺少工具证据不是通配条件。

draft、obsolete、缺少 scope/source、包含 sensitive raw Trace、跨 tenant、同 benchmark expected output 的 memory 必须立即拒绝。

## LLM Gate

System Gate 后，LLM 只判断候选的语义适用性。推荐 prompt 必须明确当前 task、mode、context、候选与以下规则：只使用直接相关、active、verified、scope-matched 且无泄漏风险的 memory；eval 模式禁止历史答案；production 只使用简短 procedural memory。

输出必须是严格 JSON：

```json
{
  "use_memory": true,
  "allowed_memory_ids": [],
  "blocked_memory_ids": [],
  "reason": "brief explanation",
  "risk": "none | low | medium | high",
  "recommended_injection": "none | short_summary | full_case_summary | pointer_only"
}
```

`parse_memory_decision()` 必须验证 shape、enum、ID 类型和字段一致性。完整响应最多 65,536 UTF-8 bytes、1,000 个 JSON nodes、深度 20，`reason` 最多 2,000 字符。使用 memory 时至少一个 allowed ID 且 injection 非 `none`；拒绝使用时 allowed 为空且 injection 为 `none`。两个 ID 数组各最多 50 项。LLM 只能缩小 System Gate allowed set；同一 ID 同时 allowed/blocked 时 blocked 胜出。系统允许但未进入最终 allowed set 的候选会自动写入 blocked 审计。底层调用方也不得提供相互矛盾的 System Gate 结果。

## 安全 Store 工作流

使用 `prepare_memory()` 检索、运行 System Gate 并创建有界 LLM prompt。若超过 50 个候选通过 System Gate，则按确定性候选顺序保留前 50 个，并把溢出项记录为 `LLM gate candidate limit exceeded`；再把 decision payload 与 Trace ID 传给 `finalize_memory()`。它重新验证陈旧状态、将 LLM decision 作为缩窄操作、渲染 snippet，并原子记录 context、候选状态和 block reason。只有该工作流提供所有权、重放、陈旧状态、Trace linkage 与原子日志保证。

常见同步路径使用 `run_memory_execution()`。调用方提供 decision callback 与 execution callback，并返回显式 `MemoryRunMeasurement`。模块使用 Store decision ID 并委托 `complete_memory_run()`；不得从异常猜测 outcome、failure attribution 或 evidence。

应用可以使用 `LocalAgentMemory` 隐藏 Store/Repository plumbing，但仍必须遵循
同一 Gate 序列。调用方只能在 `system_allowed_memory_ids` 上提交缩小 decision，
执行时只能使用 `AgentFinalizedMemory.snippet`，随后提供显式
`MemoryRunMeasurement`；放弃的 prepared request 必须 cancel。

门面会持久化 durable phase，但不会把 pending request 变为 durable。必须由同一
runtime 实例 finalize/cancel。稳定 `AgentMemoryError` code 用于恢复；callback
失败会暴露下一合法阶段所需的 request/decision ID，不得重启整个一次性序列制造
重复运行。`tbm capabilities` 是该边界的权威能力发现输出。

可选的 `tbm-mcp` 进程遵循同一规则。runtime client 必须先调用
`tbm_prepare_memory`，只在返回的 `system_allowed_memory_ids` 上缩小 decision，
再调用 `tbm_finalize_memory`，并且只把返回的 `snippet` 提供给 executor。之后
必须用显式 measurement 调用 `tbm_complete_run`；若尚未 finalization 就放弃，
则调用 `tbm_cancel_run`。server 不得暴露 lesson verification、publication、
activation、原始 Store、snapshot 或 migration 操作。

MCP 部署必须配置一个固定 checkout root 和一种显式 storage mode。Git provenance
与 ancestry 来自该 root，而不是模型。PostgreSQL conninfo 必须通过命名环境变量
提供，不能写入项目配置。可选固定 tenant 在 version 2 中仍是 declared-scope
适用性，不能宣称为授权。

可选认证本地 profile 要求在可信 server 启动时完整提供全部 `--auth-*` 选择。它只从
所选 active registry environment 派生 tenant/repository，把每个 allow/deny decision
持久化到 SQLite authorization authority，并拒绝 `--tenant`。MCP 请求 JSON 绝不能
提供 identity、target、registry 或 authority 字段。该 profile 是可信 identity 选择
之后的授权边界，不负责认证 STDIO peer，也不得暴露成不可信共享多租户服务。

每个 STDIO 输入帧在 SDK dispatch 前限制为 8 MiB、100,000 个 JSON nodes 与
depth 100，并拒绝 duplicate key、非法 UTF-8 与非有限数字。即使配置 durable
storage，pending request 仍为进程内状态。server 重启后必须重新 prepare，不得
重建或重放私有 request token。每个 request ID 都是 opaque、session-scoped
handle；新的 128-bit namespace 可防止遗留 ID 在重启后与新 prepared request
碰撞。

prepare 后的 `MemoryRunExecutionError` 保留阶段、原始 cause、request，以及可用的 finalized result/decision ID。一次 helper 调用会创建新 request，重试必须基于错误暴露状态，而不是重跑整个 helper。

正常时间顺序：注册 unknown Trace、finalize、执行、`complete_memory_run()`。一个 measured result 原子完成 Trace 与 decision。部分状态或精确重放有效；任何 outcome、attribution、evidence 或 linkage 冲突都保持两侧不变。

`complete_memory_runs()` 用于必须全有或全无的新测量批次，要求唯一 decision ID 的非空 `MemoryRunResult` tuple。Store 推导 Trace ID；共享 Trace outcome 必须一致，证据只能在互不重叠或相同的情况下合并。

`memory_run_audits()` 返回 `pending`、`trace_only`、`decision_only`、`complete`、`conflict`。不得猜测 conflict 或选择历史权威侧。`memory_run_remediations()` 返回 `measure`、`recover`、`recover_with_attribution`、`investigate`、`none`，但 plan 只是当前状态建议，写 API 必须在自己的锁下重新验证。

`recover_ready_memory_runs()` 在同一锁内选择和提交所有当前 `recover` 项，跳过 pending、需要归因、conflict 和 complete。并发 sweep 必须串行化并重新规划。

`memory_run_metrics()` 统计每个 usage decision 的五种状态、可恢复、自动恢复和需要归因数量。五种状态之和必须等于 `decision_count`；可恢复等于自动恢复加需要归因。指标用一次扫描计算且不持久化。

单项 `recover_memory_run()` 只接受 decision ID，从已验证一侧推导 Trace ID 与结果。pass Trace-only 可推导 attribution false；fail/error Trace-only 必须显式提供布尔值。批量 `recover_memory_runs()` 只处理入口状态已是 one-sided 或 complete 的唯一 ID tuple，任意 pending/conflict 拒绝整个批次。

`complete_trace()` 与 `record_decision_outcome()` 是分别拥有 Trace/decision 生命周期时的底层接口，正常流程优先使用原子完成。`latency_ms` 统一为 `None` 或 0 到 2,147,483,647；CLI 不得复制范围规则。

Semantic retrieval 的 score 在 Store 外计算，要求 `max_candidates` 为 1 到 50，不能与 `query` 同用。所有 score 与 ID 在排序前验证，使用 non-copying catalog view 和有界 top-k。score 只是检索证据，不能绕过 scope、safety 或 gate。

finalization 与底层日志始终绑定 `repo`、`commit_sha`、`tenant`；其他 provenance 只在 context 声明时绑定。验证必须发生在 request 消费与日志追加前。

## Git Ancestry Opt-in

选择启用的调用方先用 `candidate_commit_anchors(context)` 或 `pr_report_commit_anchors(context)` 获取完整 anchor 集合，在 Store 锁外调用 `capture_commit_ancestry()`，再把同一不可变证据传给检索或 PR 报告。

一次捕获最多 1,000 个提交值，重复项在去重前计数，overflow 不启动 Git。默认 runner 使用 `stdin=DEVNULL`、30 秒 timeout、binary pipe、UTF-8 replacement 解码和每个普通输出流 64 KiB 上限；超时或溢出 kill/reap。

Git exit 0 表示 ancestor，1 表示非 ancestor，其他错误停止流程。证据必须覆盖每个已发现 anchor 并绑定准确的当前 commit。Lesson anchor 是 fix commit，Failure Case anchor 是 source commit，Project Policy 只豁免 ancestry，不豁免 scope 与 gates。省略证据保留兼容行为；证据不持久化。

## Outcome Metrics

`pass`、`fail`、`error` 为已评估结果，`unknown` 与 `None` 不进入通过率分母。使用 `evaluated_with_memory_count`、`evaluated_without_memory_count` 和 `unevaluated_decision_count` 审计样本；三者之和等于 `decision_count`。

评估后应先 `complete_memory_run()` 再读取指标或同步完成审计。with-memory 仅表示 usage decision 有 `used_memory_ids`，不证明某条 memory 造成结果。指标不持久化。

`memory_outcome_metrics()` 覆盖每个 Failure Case、Lesson 与 Project Policy，包括零观测 ID。candidate/used/blocked 反映检索与决定；outcome 字段只统计实际使用。这些是 observed association，不是 causal effectiveness。

## PR Change-Set 策略

值感知 PR 报告必须使用不可变 `PRChangeSet`，提供准确 old/new value，并要求每个 new value 与变更后 `MemoryContext` 一致。anchor discovery 与 report 必须复用同一个 change set，ancestry 覆盖准确 context commit。

报告只接受完整 old endpoint 或完整 new endpoint，不接受混合。对报告筛选而言，repo 与 tenant 是必须精确匹配的 Trace 过滤条件，而不是多租户授权边界。支持字段仅 `prompt_version`、`prompt_family`、`tool`、`tool_schema_version`、`model`、`eval_suite`，最多 6 项。`model_family` 无 Trace provenance，不支持精确匹配。

Legacy `changed_fields` 保留宽泛兼容行为，但 warning 名称必须在案例扫描前一次验证，只保留最多 7 个支持名称的首次出现，保持 `O(W + C)`。

CI 应使用只读 `pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH`。它必须把同一 `PRChangeSet` 传给 anchor 与 report，在中间对显式 repository 捕获 ancestry。不得增加 `--write`、调用方证据、隐式 Git fetch 或 legacy broad fields。

`memory_caused_failure=true` 时，持久证据必须有非空 `fail`/`error` 结果和至少一个 used memory ID。

## 基准示例泄漏策略

自动 benchmark identity 精确等于 `(eval_suite, input_hash)`。调用方负责稳定 suite 名、确定性规范化、抗碰撞隐私哈希、编码和一致性。每条 Trace 携带自己示例的 hash，当前 `MemoryContext` 必须匹配当前 Trace。

Lesson/Failure Case 在候选构造时临时获得 source identity，并在 LLM narrowing 前检查。完整相同 pair 在所有模式下以固定原因 `memory originates from current benchmark example` 阻止。临时字段不写入 prompt、snippet 或持久化；`input_hash` 是身份证据，不是 scope。

不完整 identity 不触发猜测匹配：suite-only 兼容，hash-without-suite 非法，不完整 source Trace 不贡献字段，不同 hash 或不同 suite 不阻止。finalization 在状态变更前绑定 context/Trace pair。

## 注入格式

`recommended_injection` 控制最终 snippet：

- `none`：不注入。
- `pointer_only`：只注入 memory ID、source 和 scope。
- `short_summary`：注入最多 500 字符的转义规则。
- `full_case_summary`：注入最多 2,000 字符的 Lesson、经过 review 的 failure/root-cause/fix、commit、regression 与 reviewer 证据。

提供给 LLM Gate 的 task、context summary 和 candidate memory 也必须作为有界、quoted data。运行时 snippet 必须来自最终解析的 `MemoryDecision`，不得直接从检索候选渲染非空内容。

## Version-3 GateSession 契约策略

`tbm.gate-session.v3` 记录是不可变生命周期 revision。调用方执行状态转换或
lease renewal 时必须提交精确的当前 version。adapter 必须原子拒绝 stale
version，不能把调用方字段合并进更新的 session。

所有 session 与 lease 时间戳都由服务端拥有。外部 agent 可以提供 idempotency
key，但不能提供 transition time、lease deadline 或 expiry。纯契约不会读取
wall-clock time；durable repository 必须使用可信事务时间与已持久化 lease/expiry
比较。

只允许已发布的转换图。生命周期证据只能向前累积，不能被清除或重新绑定。
prepared 到 executing 的 active 状态必须持有不超过 session expiry 的 lease。
completed、canceled、expired 与 abandoned 都是 terminal，不能重新打开。
terminal failure path 必须保留有界 reason。

当前 Store 与本地 MCP 不持久化该契约。不得通过序列化或重建私有
`MemoryGateRequest` token 来模拟 durability。未来 repository 必须在不削弱
现有 Gate 的前提下，实现原子 idempotency、expiry、recovery，以及 retrieval
前 authorization。

opt-in `SQLiteGateSessionRepository` 是该记录的首个 durable adapter。只有
service-owned 代码可以配置其可信 clock 与 TTL/lease duration。原子 idempotency
replay 必须使用 `create_or_get()`；调用 `transition()` 或 `renew_lease()` 时始终
提交精确 current version；`list_due()` 只是未加锁 candidate snapshot，后续仍需
CAS transition。不得直接修改 revision row 或 identity column。借用的 SQLite
connection 必须持续启用 foreign key 与 recursive trigger。该 adapter 与 active
SQLite v1 side-by-side，且不连接 `MemoryGateRequest`；它本身不提供 expiry worker、
recovery、authorization 或 restart-resumable MCP。

opt-in `PostgresGateSessionRepository` 在隔离 schema 上提供相同 API。只能使用
打包、带版本门禁的脚本安装和 rollback；不得修改 catalog object、禁用 trigger、
提供 client timestamp，或在 operation 期间改变 connection transaction。以
`clock_timestamp()` 为权威，并保持 metadata-to-head 锁序。borrowed connection
的 outer transaction 仍由调用方拥有；repository 失败只回滚自身 savepoint。该
adapter 仍不连接 active Agent/MCP lifecycle，也不提供 worker 或 authorization。

## Version-3 授权契约策略

授权必须先于检索。identity、client、tenant 与 target 必须从服务端认证上下文派生，
绝不能只凭调用方声称的 ID 授权。只解析精确 canonical repository ID 或显式租户
alias；legacy migration alias、scope 省略和适用性属性都不能授予访问权。

每个受保护操作都应按当前 policy 求值。disabled identity、跨租户 target、revoked
binding，以及不在 inclusive `valid_from` / exclusive `expires_at` 区间内的
binding 都必须 fail closed。`platform:admin` 是全局超级用户权限，必须审计其分配
与使用。

decision 和 policy hash 只提供内容关联，不提供真实性。不得把孤立 decision 当作
签名或长期 capability；必须针对精确信任 request 与 policy 验证，并在 policy
变化、撤销或过期后重新求值。opt-in SQLite 与 PostgreSQL authorization
repository 只能持久化已针对精确 request/policy 核验的 decision；其 request
uniqueness 是 audit invariant，不是可重用授权 capability。PostgreSQL authority
只能使用包内、受 active-v2 门禁的资源安装或回滚，catalog drift 必须失败关闭。

`AuthenticatedRetrievalService` 是新 service integration 可使用的共享顺序边界。
可信 transport authenticator 必须创建其中精确的 Principal/AgentClient context；
调用方 JSON 不是 authentication。该边界必须持久化并读回完全相同的 decision，在
写入后复查完整 registry hash，对 canonical repository 校验 active environment，
并只在全部检查通过后调用 retrieval。无法返回并重新加载精确 persistence receipt
的自定义 decision writer 无效。

默认 Store、Agent、MCP 与 GateSession profile 不调用该边界。可选本地 MCP
`--auth-*` profile 只在可信启动 identity 选择后调用它，不得宣称具备 transport
authentication 或共享多租户授权。durable GateSession/RetrievalSnapshot linkage、
worker 与跨记录 service transaction 仍待完成。

需要 durable preparation 时，应使用 `AuthenticatedGateSessionService` 作为下一层
边界。在 retrieval 前创建并读回 scoped session；既有 idempotency key 不得重复
preparation；通过可信 verifier 核验精确 RetrievalSnapshot 与
SystemGateEvaluation；`PREPARED` transition 必须保留全部 immutable identity
field。失败时使用带 version 检查的 cancellation，或返回显式 recovery-required
state。绝不能从 GateSession 重建 Store request token。在两个 authority 尚未共享
同一 service transaction 前，只能称为有顺序补偿，不能称为 atomic commit。

SQLite 作为 Gate evidence authority 时，必须通过
`SQLiteGateEvidenceV3Repository.store_bundle()` 写入精确
RetrievalSnapshot/SystemGateEvaluation 记录对，并向 Gate service 注入
`DurablePreparedGateEvidenceVerifier`。不得在没有 durable readback 和精确 scope
校验时接受调用方提供的 ID。foreign key 与 recursive trigger 必须始终启用；关闭
任一项都会使 authority 失效。

`GateSessionRecoveryWorker` 只能作为有界、重复 scan 运行。mutation 前验证完整
返回 page，并把每个 candidate 当作独立 CAS operation。只有 session 已到期的
`PREPARED` 或 `AWAITING_DECISION` 才能转为 `EXPIRED`；在当前 graph 下，
lease-only、`DECIDED`、`FINALIZED` 与 `EXECUTING` 必须进入显式 recovery。不得
盲目重试 superseded version，也不得把一次 pass 描述为 all-or-nothing batch。

## Version-3 结构化 evidence 策略

只能从经过 review 的 Failure Case、关联 fix 与不同的 verification Trace/run 创建
结构化 regression evidence。必须记录 evaluator/version、suite/case、
expected/observed outcome、有界 environment、精确 source/fix/verification commit
关系与 artifact hash。submitter 和 verifier 必须是不同的认证 principal；所属
service 必须验证 attestation，不能把其 hash 当作签名。

`pass` 不授予激活权限。发布仍需独立 lifecycle/authorization 检查与 immutable
MemoryRevision。migration-only `RegressionEvidence` 和 active v2
`regression_passed` boolean 只是兼容输入，不是该契约的替代品。

## Version-3 immutable revision 策略

只能在 case review、fix evidence 与结构化 regression evidence 已存在后创建
`MemoryRevision` proposal。必须验证精确 content artifact、canonical authorization
scope、parent revision 与 server-owned proposer/client context。Lesson 必须解析
内容寻址 FixEvidence 与每个 regression evidence ID，要求它们具有相同 Failure
Case、source Trace、source/fix commit、passing result，并确保 proposer 不属于任何
evidence submitter/reviewer/verifier。只检查 regression evidence 的兼容 helper
不能作为 publication preflight。

不得把合法 revision hash 当作 approval 或 activation。active runtime 不暴露 revision
publication operation。approval/activation 必须是独立的认证、授权、append-only
service event，并以事务检查 parent/sequence 与当前 policy。修正必须创建新 revision，
不能修改既有 revision。

隔离 SQLite 与 PostgreSQL proposal ledger 只能保存已完整验证的精确 evidence
bundle。必须强制线性 parent/revision continuity、immutable 幂等 replay，并在
commit 前精确读回。PostgreSQL install/rollback 还必须校验隔离 schema catalog 与
ACL。ledger 中存在 proposal 不代表发布授权，也不得把 proposal 投影到 active v2
memory。

## Version-3 retrieval snapshot 策略

在检索前先授权经过认证的 tenant/repository/principal/client 请求，然后把精确
的已授权结果记录为内容寻址 `RetrievalSnapshot`：context/query 摘要、retriever
与 index 身份、有序 memory revision、候选哈希、有限 stage/fusion 分数、边界
和全部截断原因。不得把 raw query、候选内容、secret 或无限 evidence 放进快照。
不得把语义相似度、融合分数或索引存在性当成授权、适用性、验证或门禁证据。
System Gate evaluation 与 Semantic Gate attempt 保持独立不可变记录。精确回放
消费已记录结果，不得从已变化的 catalog/index 静默重算。active Store/adapter
尚不产生此契约；未来服务必须验证全部引用身份与字节、授权快照读取、应用保留
策略，并在同一 GateSession 事务中挂接快照。

## Version-3 gate evaluation 策略

调用模型前，为每个有序 retrieval hit 记录确定性 System Gate 结果，并绑定精确
authorization event、policy bundle、evaluator version、revision ID、candidate
hash、rule、reason 与 timestamp。每次 Semantic Gate 调用都要记录 provider/model/
endpoint、prompt template、generation config、provider request、prompt/response
artifact hash、latency/token、status 与结果。attempt record 不得包含 raw
prompt/response 或 secret；失败 attempt 只含 provenance 与有界 error code。

finalize 前必须核验跨记录 linkage。成功 semantic 结果必须覆盖全部 System Gate
候选，只能 allow System-approved revision，并保留全部 System block。retry 必须创建
下一 sequence、精确 parent 的新 immutable attempt，不能覆盖既有 attempt。

opt-in SQLite Semantic Gate ledger 只能在重新加载并核验精确
RetrievalSnapshot/SystemGateEvaluation 对之后持久化该 retry chain。它必须
强制一条有界线性 head、拒绝 fork 与直接 replacement write、通过 savepoint
保留调用方 transaction，并在每次读取时复核完整 chain。ledger 中存在记录不代表
provider 已认证或 finalize 已获授权。持久化前使用
`SemanticGateArtifactBinding` 核验精确非空 prompt/response 字节、attempt
角色与 digest、长度、classification 及必需的 encryption metadata；不得在
descriptor JSON 中记录或嵌入这些字节。绑定存在也不证明静态加密已经执行。
active Agent/MCP 集成仍是独立后续工作。隔离 PostgreSQL 对等实现必须通过 parent-before-head
row lock、exact CAS、deferred commit check、调用方 savepoint，以及 fail-closed
catalog 校验/rollback 保持相同 chain 规则。
SQLite 文件所有者属于可信边界：本地 DDL 无法证明一条内部完全自洽的离线重写
没有发生；该威胁必须由外部签名 audit/checkpoint authority 处理。

## Version-3 结果与归因策略

只能从显式 measured result 与 evidence 创建 `RunOutcome`，不得从 callback
异常推断。它必须绑定 completed GateSession 以及精确 trace、run 与 usage
decision。raw output 与 secret 保存在受控 artifact 中，outcome record 只保留
其哈希。

memory 出现在运行中只能记录为采用 `runtime_observation` 的 `association`。
不得把 association、correlation、score 变化或 legacy
`memory_caused_failure` 默认值转成因果结论。`causal` attribution 必须来自
受控实验、人工复核或外部评估，带 evidence artifact 和确定 effect，并由不同于
evaluator 的 verifier 核验。adapter 写入前必须校验精确 linkage 与时间。

## Version-3 审计与恢复策略

AuditEvent 必须按精确 stream sequence 与 parent append；不得 update、delete、
truncate 或静默分叉 stream。tenant、repository、session 与 actor identity 必须
从 authenticated service context 派生。event 只保存有界 reason code、typed
identifier 与 payload hash。

RecoveryAction 只是已尝试动作的 evidence，不授予执行权限。memory-run recovery
前必须在锁内重算当前 `MemoryRunRemediation`；GateSession recovery 前必须锁定并
比较精确 expected revision。底层 transition、RecoveryAction 与匹配的成功/失败
AuditEvent 必须原子写入。stale plan、request-hash collision、缺失显式 attribution
或未授权 actor 一律 fail closed。

opt-in SQLite 与 PostgreSQL audit ledger 可以持久化精确 event stream，并原子追加
RecoveryAction 及其匹配 event。session-scoped request digest 唯一性只是 replay
protection，不是 authorization。在 service unit of work 尚未派生 actor identity，
并把底层 GateSession 或 remediation transition 纳入同一事务前，调用方不得把
ledger append 成功描述为 recovery 已获授权或已经执行的证明。
PostgreSQL ledger 还要求 version-gated 隔离安装、精确 catalog/function 检查、
确定性 stream-head lock order 与 fail-closed rollback；它不改变 active schema
version 2。

## Version-3 重放 artifact 策略

只能在 decision finalize 后，从实际最终 snippet 创建 `InjectionArtifact`。不得对
candidate、Gate 前 rendering 或事后重建的近似内容计算该 artifact。必须绑定本次
render 实际使用的同一 session、decision、usage decision、有序 memory revision、
renderer 与 policy bundle。

只有八项 replay component 全部存在，且 injection artifact ID 与内容摘要匹配时，
才能使用 `complete`。`legacy_partial` 只用于迁移证据，并且必须精确列出 null
component；不得据此静默重建缺失的 prompt、response、policy 或 ancestry。使用前必须
验证 artifact 字节。

classification metadata 本身不执行安全策略。未来 service 必须加密
confidential/restricted 字节、授权每次读取、执行 retention 与 redaction policy，并
避免记录内容。opt-in SQLite/PostgreSQL replay repository 会逐字节保存接受的内容，
因此会拒绝 confidential/restricted artifact，直到透明加密 provider 能在加密的同时
保留精确内容身份。两者校验精确字节与 immutable descriptor linkage，把精确 replay
视为 idempotent，并通过 savepoint 保留 borrowed transaction；两者都不提供 access
control、retention 或 GateSession authority。当前 Store 与 active adapter 不持久化
这些契约，也不得宣称支持精确 decision replay。

## 固定运行时预算

运行时在以下边界 fail closed：

- `MEMORY_ID_MAX_CHARS`：128。
- `METADATA_VALUE_MAX_CHARS`：512。
- `LLM_GATE_MAX_CANDIDATES`：50。
- 每个 `allowed_memory_ids` / `blocked_memory_ids`：50。
- `LLM_GATE_PROMPT_MAX_CHARS`：32,000。
- `LLM_GATE_RESPONSE_MAX_BYTES`：65,536 UTF-8 bytes。
- `LLM_GATE_RESPONSE_MAX_NODES`：1,000。
- `LLM_GATE_RESPONSE_MAX_DEPTH`：20。
- `MEMORY_DECISION_REASON_MAX_CHARS`：2,000。
- `INJECTION_MAX_MEMORIES`：20。
- `INJECTION_SNIPPET_MAX_CHARS`：12,000。
- `COMMIT_ANCESTRY_MAX_ANCHORS`：1,000。
- `TRACE_JSON_MAX_NODES`：三个 Trace JSON 字段合计 100,000。
- `TRACE_JSON_MAX_TEXT_BYTES`：三个字段对象键与字符串 UTF-8 文本合计 8 MiB。

metadata 与关键词检索使用 Unicode-aware tokenization；非 ASCII 词还会生成双字符 gram，使 CJK 查询子串可以筛选更长文本，同时不改变两层门控。

生产部署必须把 declared-scope matching 视为适用性判断，而不是授权。省略 `repo` 或 `tenant` 的 memory 不会自动获得该字段的隔离。canonical repository identity、显式 scope kind、durable Gate request、可重放审计、结构化 regression evidence 与 required ancestry 仍属于 schema v3 / PostgreSQL schema v3。

加载既有 version-2 snapshot 前必须补齐 verified-but-unreviewed case 的 review 证据；同步前必须对既有 PostgreSQL schema-version-1 安装应用包内 `schemas/postgres-v1-to-v2.sql`。在 Lesson/source-case 锁序修复前创建的 schema-version-2 数据库必须应用可重复运行且带版本门禁的 `schemas/postgres-v2-lock-order-hotfix.sql`；全新安装与当前 v1→v2 迁移已包含该修复。

推荐格式：

```text
Relevant verified memory:

1. [lesson_id: lesson_001]
Scope: planner / search_docs
Rule: When calling search_docs, always provide a non-empty natural-language query.
Source: case_001
```

禁止注入 raw Trace、完整 prompt history、完整用户输入、含隐私的 tool output、eval expected output、未验证 root cause、draft Failure Case 和 obsolete Lesson。
