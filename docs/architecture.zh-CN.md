# 架构

[English](architecture.md) | **简体中文**

## 目标

构建一个面向 LLM / Agent Harness 工程、以 Trace 为依据、感知 Git commit 且受门控控制的记忆层。

系统需要回答：

- 这个失败以前是否发生过？
- 哪个 commit 引入或修复了它？
- 涉及哪个 prompt 版本、tool schema、model 或 eval suite？
- 是否存在适用于当前任务的已验证 Lesson？
- 该记忆应该被注入、摘要，还是阻止？

## 数据流

```text
1. Harness Run
   ↓
2. Immutable Trace Store
   ↓
3. Failure Case Extraction
   ↓
4. Human / Eval Verification
   ↓
5. Verified Lesson Memory
   ↓
6. Memory Applicability Gate
   ↓
7. Controlled Runtime Injection
   ↓
8. Memory Usage Log
```

## 第 1 层：Trace Store

Trace Store 记录事实，应当追加写且可审计。核心字段包括运行身份、Git provenance、prompt/tool/model/eval 元数据、输入输出哈希、检索上下文、工具调用与输出、结果、延迟、成本、错误、URI 和创建时间。原始 Trace 是证据，不是运行时记忆。

`capture_trace_metadata()` 在 Harness 记录 Trace 前读取仓库名、commit SHA、当前分支和 dirty state。四个注入 runner 命令都必须返回字符串；空 commit SHA、空仓库根、非字符串输出，以及超过 512 字符的 commit、branch 或 repository 名称会在同一命令边界产生 `TraceMetadataCaptureError`，且不回显非法值。空 branch 表示 detached HEAD，空 status 表示干净工作树。

内存 Store 可以把 Trace、Failure Case、Lesson、Project Policy 和 usage log 保存为无依赖 JSON 快照。加载快照复用实时写入验证，因此重复 ID、全局 memory ID 唯一性和 Lesson provenance 继续强制执行。

Trace 要求非空白 `trace_id`、`run_id`、`commit_sha`，`eval_result` 只能是 `pass`、`fail`、`error` 或 `unknown`。`retrieved_context`、`tool_calls`、`tool_outputs` 必须是 JSON 对象列表。Store 验证调用方对象、深拷贝、再次验证副本后才插入。

三个结构化 JSON 字段共享 `_TraceJSONBudget`：最多 100,000 个节点和 8 MiB 对象键/字符串 UTF-8 文本，并保留深度 100。验证器在扩展遍历栈或 `dict.items()` 前检查宽容器，避免防御性复制放大超限输入。

当前运行可以先以 `eval_result="unknown"` 注册。执行后，`complete_trace()` 原子转换到 `pass`、`fail` 或 `error`，并只填充原先为空的完成字段。省略字段保留已有值，已填充字段只能精确重放，其他 Trace 字段不可变。

`latency_ms` 的跨持久化统一范围是 `None` 或 0 到 2,147,483,647 的整数。共享验证器覆盖记录、快照重建、回调执行和单项/批量完成。CLI 中越界属于退出码 3 的结构化状态错误。

## 第 2 层：Failure Case Store

Failure Case 是从失败 Trace 派生的结构化事后复盘，保存 case ID、source Trace、commit、失败类型、症状、root cause、review 信息、修复、回归结果、状态和时间戳。它属于 episodic memory。

`load_failure_taxonomy()` 与保守提取辅助函数先从明确失败证据分类，再生成草稿。证据顺序为 `Trace.error`、`tool_calls[].error`、`tool_outputs[].error`。工具名称永远不能选择 taxonomy；只有同一条记录存在 truthy 顶层 error 时才可为症状命名。任意参数、成功结果、嵌套内容和标识符都不参与关键字分类。

显式 `invalid argument` 有效；单独的 `required` 无效，只有 `required argument`、`required parameter`、`required field`、`required property` 这些保守短语触发 `invalid_tool_argument`。`review_failure_case()` 在记录 reviewer、root cause、notes 和时间的同时保持草稿状态。只有已经包含 `reviewed_by`、`root_cause`、`reviewed_at`、修复 commit 和通过回归证据的 draft 才能验证。

Store 拒绝不存在的 `source_trace_id`、结果不是 `fail`/`error` 的 source Trace、与 source Trace 不一致的 `commit_sha`、空身份、非法状态，以及缺少完整 review、修复和回归证据的 verified case。

## 第 3 层：Lesson Store

Lesson 是从 verified case 派生的可复用规则，包含 lesson ID、source case、文本、memory type、scope、0.0 到 1.0 的 confidence、安全标志、状态和时间戳。

只有 active、来源已验证、作用域匹配且 confidence 合法的 Lesson 才能进入运行时门控。Store 会拒绝缺失或未验证的 source case；active Lesson 还会拒绝 dirty source Trace，因为 commit 无法唯一描述当时执行的工作区。空 ID、非法 memory type/status、空或未知 scope、非字符串/空 scope 值，以及越界 confidence 同样会被拒绝。`sensitive` 与 `eval_leaking` 标志会保留到 `MemoryItem`，供 System Gate 在 LLM 判断前阻止。

## 第 3b 层：Project Policy

Project Policy 是人工维护的 prompt、tool 或 eval 规则，不从 Failure Case 派生，但仍必须有来源身份、scope、状态和安全标志。`ProjectPolicy` 与 `memory_item_from_project_policy()` 把维护记录转换为 policy memory。

Store 会拒绝空 ID/文本、非法状态或 scope、越界 confidence，以及与 Failure Case、Lesson、Project Policy 共享运行时命名空间发生冲突的 ID。

完整 Store 的稳定持久化边界由 `to_snapshot()`、`from_snapshot()`、`save_json()` 和 `load_json()` 提供。它要求 JSON 对象，拒绝非有限浮点数、运行时序列化上限之外的整数和非法 confidence。

`save_lessons_yaml()` 与 `load_lessons_yaml()` 为 active lesson 提供无依赖适配器。导入只接受 active 状态，并在一次提交前解析完整文档、拒绝重复字段、构造所有候选并按暂存状态验证，因此后续记录失败不会部分导入前面的记录。

JSON 与 Lesson YAML 使用同一持久性边界：同目录临时文件、规范 LF、flush、`os.fsync()`、关闭和原子发布。覆盖使用 `os.replace()`，no-replace Lesson 导出使用 `os.link()`。POSIX 在发布与临时名清理后同步父目录。发布前失败保留旧目标；发布后目录同步失败会传播，此时目标状态被视为持久性不确定。

## 打包分发资源

`trace_backed_memory.resources` 提供 122 个规范 Schema、SQL/迁移、memory support 和 example 文件的安装后访问接口：`packaged_resources()`、`read_packaged_resource()` 与 `export_packaged_resource()`。

资源名来自固定、按字典序排列的白名单。模块在接触 `importlib.resources` 前验证名称，不接受任意遍历、当前目录 fallback 或暴露包路径。wheel、sdist、editable 与 zip import 使用同一行为。每个 `PackagedResource` 都包含 kind、media type、byte size 和 SHA-256。

顶层文件是规范编辑源，包内 `_resources/` 是字节一致副本。构建验证会比较 wheel 与 sdist 中每个成员。`py.typed` 声明安装包类型信息。无路径 `load_failure_taxonomy()` 使用包内规范 taxonomy；显式路径仍属于调用方输入。

### 有界本地文档摄取

私有摄取边界只打开调用方路径一次，通过同一句柄读取后再严格 UTF-8 解码。默认限制：

- 快照 64 MiB、每集合 100,000 条、合计 250,000 条；
- active Lesson YAML 8 MiB、10,000 条；
- failure taxonomy 1 MiB、1,000 类；
- CLI JSON 8 MiB、10,000 顶层项、100,000 节点、深度 100。

Python API 可对单个限制显式传 `None`，只用于可信离线迁移；CLI 始终使用安全默认值。限制不是持久化配置。

快照 usage-log 重建通过 load-local `decision_id`、known memory、legacy `run_id` 与 tool-name 索引保持平均 O(n)。实时 Store 还维护不持久化的 decision ID 位置/后缀索引，以及 `run_id` 到有序 `trace_id` 的索引，使单项查找平均 O(1)。

实时 usage-log 存在性检查只访问被引用的 ID，不复制完整 memory catalog。`metrics()` 与 `memory_run_metrics()` 各使用一次 usage-log 扫描和 O(1) 累加空间。`recover-batch` 在快照加载前分别把 decision ID 与 attribution 限制为 10,000 项。

## 快照操作 CLI

无依赖适配器通过 `tbm` 和 `python -m trace_backed_memory` 暴露。快照命令只接受一个本地路径，并统一通过 `TraceBackedMemoryStore.load_json()` 重建 Store；不接受 stdin、远程 URL、SQL Repository 连接或备用快照输出路径。

读取接口直接映射现有 Store 视图：`snapshot validate`、`snapshot stats`、`audit`、`remediation` 和 `metrics`。变更接口把 `complete`、`complete-batch`、`recover`、`recover-batch`、`recover-ready`、`obsolete` 与 `obsolete-batch` 委托给对应 Store API，不复制状态机。

`complete` 要求明确的 Trace、decision 和测量结果；`complete-batch` 解析严格的 `MemoryRunResult` 数组并只调用一次批量 API。重复对象键、非法类型、未知字段、共享 Trace 冲突或后续条目失败都会在任何变更前拒绝整个批次。

所有调用方 JSON 在普通 dict 形成前拒绝任意层级重复键。快照、MemoryContext、MemoryDecision 和 CLI 文件共用相同 ordered-pairs 原语，同时保留各自错误边界。

Lesson 导出委托 active-only 选择和规范 YAML 序列化，默认拒绝覆盖及源快照别名。Lesson 导入固定使用 8 MiB/10,000 条限制，只接受 active 状态，并保持来源、共享 ID 与 all-or-nothing 验证。

单项和批量 obsolescence 都只允许向前转换。Store 从同一入口状态暂存显式记录与 Failure Case 的完整 active Lesson cascade，验证后一次提交。CLI 只输出 ID、状态、计数和 `written`，不泄露 memory 文本或 Trace 证据。

所有变更默认 dry-run。只有显式 `--write` 且操作完整成功时，才调用 `save_json()` 原子替换输入快照。每个写命令在快照加载前获取 sibling `.tbm.lock` 排他建议锁，并持有到原子发布结束。sidecar 必须是与 descriptor 相同的单链接普通文件；符号链接、Windows reparse point、硬链接和特殊文件会在加载快照前失败。默认争用超时为 30 秒。

公开的 `snapshot_write_lock()` 让 Python 调用方遵循同一协议。它必须包围完整 load、mutate、save 事务，是跨进程、建议性、非重入的锁，不替代 Store `RLock` 或 SQLite/PostgreSQL 事务。

成功输出一个确定性 JSON 与换行；失败向 stderr 输出一个结构化 JSON，不带 traceback。退出码 0 到 4 分别表示成功/空操作、内部错误、输入错误、状态拒绝和写入失败。成功输出在持久化前序列化，已经提交后发生 stdout 关闭不会把操作误报为失败。

## 第 4 层：Memory Gate

记忆使用必须通过两层门控：

```text
System Gate -> LLM Gate
```

`parse_memory_context()` 验证 JSON 字符串或 mapping，要求 `mode`、`repo`、`commit_sha`，并只保留已知字段。所有公开 gate helper 都会在迭代、排序或读取字段前验证 context、列表、`MemoryItem`、唯一 ID 与字符串映射。

System Gate 是确定性的，检查来源、状态、scope、tenant、confidence、敏感性、评估泄漏和运行模式。LLM Gate 只对通过 System Gate 的候选判断语义适用性。

`parse_memory_decision()` 严格验证 LLM JSON。完整响应最多 65,536 UTF-8 bytes、1,000 个 JSON nodes、深度 20，`reason` 最多 2,000 字符；`use_memory`、allowed IDs 和 injection mode 必须一致，`allowed_memory_ids` 与 `blocked_memory_ids` 各最多 50 项。LLM 只能缩小系统允许集合，不能重新放行被阻止的记忆；系统允许但未被 LLM 选择的候选会自动进入最终 blocked 审计。

动态 task、context summary 和候选文本都以 JSON 字符串编码并受长度限制。注入模式 `none` 不输出内容，`pointer_only` 只输出 ID/source/scope，`short_summary` 输出最多 500 字符的规则，`full_case_summary` 输出最多 2,000 字符的 Lesson 与经过 review 的 failure/fix provenance。

固定预算包括 128 字符 ID、512 字符 metadata、每个 request 最多 1,000 个审计候选、所有进程内 pending request 合计最多 100,000 个候选引用、最多 50 个 LLM Gate 候选、32,000 字符 gate prompt、65,536 bytes decision response、1,000 个 response nodes、response 深度 20、2,000 字符 reason、20 条注入 memory 和 12,000 字符 snippet。

候选检索 metadata-first。Lesson 与 Project Policy 要求所有已声明 scope 精确匹配；debug/repair 模式还能从 source Trace 派生 verified Failure Case memory。关键词解析支持 Unicode，并为非 ASCII 词生成双字符 gram，使 CJK 子串能够匹配。关键词或调用方 semantic score 只能帮助检索，不能替代两层门控。

Semantic 模式要求 `max_candidates` 为 1 到 50，验证完整 score mapping 后使用有界 heap top-k，结果按 score 降序、同分按 memory ID 升序，复杂度为 `O(K log k)` 时间与 `O(k)` 排名存储。score 与 embedding 不持久化。

安全主流程是 `prepare_memory()` 后接 `finalize_memory()`：准备阶段检索并运行 System Gate；context 指定工具时，episodic Failure Case 的 source Trace 必须包含同名工具，无工具证据按 fail-closed 处理而不是通配；若系统允许候选超过 50 个，则按确定性候选顺序保留前 50 个，并把其余项以 `LLM gate candidate limit exceeded` 记录为系统阻止。最终化重新验证状态、收窄 LLM decision、渲染 snippet 并原子记录 Trace-linked 证据。

### 同步 Memory Run 执行

`run_memory_execution()` 为常见同步路径提供无依赖编排：prepare、decision callback、finalize、execution callback、`complete_memory_run()`。`MemoryRunMeasurement` 不含 decision ID；模块总是传递 Store 生成的 ID，并只转发非 `None` 可选证据。

prepare 后的普通异常包装为 `MemoryRunExecutionError`，阶段为 `decision`、`finalization`、`execution` 或 `completion`，同时保留原始 cause 与恢复上下文。prepare 错误原样传播，`KeyboardInterrupt` 和 `SystemExit` 不包装。一次性 helper 每次调用都会准备新 request，不能作为幂等重试令牌。

### Agent 应用门面

`LocalAgentMemory` 在同一 Store 生命周期之上提供聚焦应用边界，负责 Trace
注册、进程内 request lookup、Repository load/sync/close、稳定 Agent 错误与
prepare/finalize/complete 主流程。`capture_local_trace()` 从显式 checkout root
派生 Git provenance；`agent_capabilities()` 与 `tbm capabilities` 不加载快照
即可发布协议、存储、操作和硬限制。

门面不会序列化私有 Store token。SQLite/PostgreSQL 分别同步 prepare 前的
Trace、finalize 后的 usage decision 和原子 measured completion。pending request
与本地 finalization tombstone 仍为进程内状态；同一 runtime 的相同 decision
重放幂等，不同 decision 返回稳定冲突。打包的 `tbm.agent.v1` Schema 覆盖
capability、prepared、finalized、completed 与 error，不改变 snapshot version
2、SQLite schema version 1 或 PostgreSQL schema version 2。

每个 Store runtime 都会为 opaque Gate request ID 生成新的 128-bit namespace。
持久化数字后缀可在 reload 后继续递增，但已放弃进程中的 stale ID 无法在下一
进程中命名新 request。这样既阻止 stale finalization/cancellation 跨 runtime
restart，又不会假装 pending state 已持久化。

`tbm-mcp` 是一个可选的薄 STDIO adapter，建立在单个进程持有的
`LocalAgentMemory` 之上，只暴露 capability、health、prepare、finalize、
complete 与 cancel 工具。配置的 checkout root 和可选 tenant 由 server 持有；
prepare 从该 root 捕获 Trace Git provenance 与完整 ancestry，再调用门面。
adapter 不复制 Gate policy，也不能 curate、verify、publish、activate、查看原始
Store 或运行 migration。

all-or-none 的本地 `--auth-*` 启动 profile 可以进一步用
`AuthenticatedLocalAgentMemory` 包装该 runtime。可信有界 registry 文件与 SQLite
authorization authority 在启动时选择精确 active principal、client 与 environment
记录；由 environment 派生 canonical tenant/repository，请求 Schema 不暴露这些
字段。prepare 在注册 Trace 前持久化并读回授权，而门面本地 ownership 索引保护
finalize/complete/cancel handle。该 profile 不是 transport authentication，也不是
共享多租户部署。

STDIO reader 在 JSON 解码前限制每一行；超限行会先完整排空，之后才接受下一
请求；随后复用 duplicate-key、finite-number、UTF-8、node 与 depth 检查。严格
工具 request model 拒绝未知字段。transport/request 错误转换为有界 agent
envelope，意外故障会净化。稳定 MCP v1 SDK 仍是可选依赖，因此导入核心包和运行
`tbm` 保持无第三方运行时依赖。

server 有意保留门面的进程内 session 边界。持久 Repository 只同步已有的
Trace、finalized usage decision 与 measured completion。MCP 进程重启会放弃
尚未 finalized 的 request 与 replay tombstone，不会从 durable data 重建私有
Store token。每个 session 的 request namespace 还保证 stale client handle 不会
在重启后与新 prepared request 碰撞。

正常时间顺序为注册 unknown Trace、finalize decision、执行、再原子完成。`complete_memory_run()` 在一把 Store 锁下验证 Trace 与 usage-log 候选后同时赋值，支持 pending、匹配部分完成和精确重放；任何 outcome、归因、证据或 linkage 冲突都不会留下半完成状态。

`complete_memory_runs()` 对唯一 decision ID 的非空 `MemoryRunResult` tuple 应用同一状态机，从已验证 decision 推导 Trace ID。共享 Trace 的结果必须一致，证据只在互不重叠或相同的情况下合并。批量完成与批量恢复共用不变更状态的 candidate stager。

`memory_run_audits()` 把 decision 分类为 `pending`、`trace_only`、`decision_only`、`complete` 或 `conflict`。`memory_run_remediations()` 再映射为 `measure`、`recover`、`recover_with_attribution`、`investigate` 或 `none`。冲突只供人工调查，Store 不自动选边。

`recover_ready_memory_runs()` 在同一可重入锁下重新规划、选择 `recover` 项并批量提交，避免 plan-to-write 竞态。`memory_run_metrics()` 用一次扫描统计状态与恢复工作。`recover_memory_run()` 和 `recover_memory_runs()` 只从已测量一侧推导结果；失败/错误 Trace-only 状态必须由调用方显式提供 attribution。

finalization 与底层日志要求 `repo`、`commit_sha`、`tenant` 始终匹配 Trace；其他 provenance 只有 context 声明时绑定。验证发生在 request 消费或 usage append 之前。

usage log 保存 Trace ID、序列化 context、候选与状态、System Gate 阻止原因、最终 ID、风险、理由、注入模式、可选结果和 failure attribution。Store 拒绝未知、重复、空或相互矛盾的 ID 证据。

### 结果感知指标

`pass`、`fail`、`error` 是已评估结果，其中 `error` 为未通过；`unknown` 与 `None` 不进入通过率分母。`evaluated_with_memory_count`、`evaluated_without_memory_count` 与 `unevaluated_decision_count` 之和等于 `decision_count`。

`tbm outcome` 是 `record_decision_outcome()` 的 decision-only CLI，输出仅包含前后 outcome pair、decision ID、`changed` 与 `written`，不会完成 Trace 或泄露 context、memory IDs 和工具证据。

`memory_outcome_metrics()` 为每个 Failure Case、Lesson 和 Project Policy 返回按 ID 排序的候选、使用、阻止和 outcome 观测。多记忆 decision 会把同一结果关联到每个 used ID；这些是观测关联，不是因果估计。

### 基准示例身份边界

基准泄漏身份是精确二元组 `(eval_suite, input_hash)`。调用方负责稳定 suite 名称、确定性规范化、隐私保护哈希和碰撞策略。source-derived memory 在运行时临时获得 `source_eval_suite` 与 `source_input_hash`；这些字段不进入 prompt、snippet 或持久化。

完整身份相同会在所有模式下以 `memory originates from current benchmark example` 自动阻止。`sensitive` 与 `eval_leaking` 检查优先。身份不完整时不会猜测匹配；不同 hash 或不同 suite 不触发规则。finalization 在状态变更前绑定当前 context 与 Trace 身份。

Trace 嵌套 JSON 只接受字符串键与有限数，拒绝环、超深结构、预算溢出和 lone surrogate。持久化身份、linkage、必需文本、scope、Memory Context 与 audit mapping 键值都必须至少包含一个非空白字符；六组 JSON Schema 使用 `pattern: "\\S"` 发布同一规则。持久化生命周期时间戳统一使用严格 RFC 3339，必须带 `Z` 或数字 UTC offset，小数秒最多六位，避免 Python、SQLite 与 PostgreSQL 静默丢失精度。

PostgreSQL 使用 composite provenance、confidence、JSONB shape、runtime memory ID registry、前向状态 trigger 和行锁保持跨层约束。Failure Case 的 source Trace/commit 与 Lesson 的 source Case 在 INSERT 后不可改绑，直接 SQL 同样受 trigger 保护。fresh-install DDL 在一个事务中执行，函数固定 `pg_catalog` search path，并要求 PostgreSQL 12+。Schema 版本 2 的普通空格检查比 Python 规则窄，因此受支持写入始终先经过 Store 验证。

PostgreSQL 集成测试在普通本地环境可跳过；CI 的 `TBM_REQUIRE_POSTGRES=1` 将缺少工具或非法 initdb 用户转换为失败。独立 Ubuntu job 运行真实集群测试，Windows job 运行完整 Python 套件。

## SQLite 运行时存储库

`SQLiteMemoryRepository` 是完整 `TraceBackedMemoryStore` 的标准库嵌入式 SQL 持久化边界。它使用 `sqlite3`，从包根公开且不需要可选依赖。文件数据库适合本地 harness、CI 和单机工具；owned `:memory:` 数据库只在该连接生命周期内存在。

Repository 要求规范 `schemas/sqlite.sql` 的 schema 版本 1。`connect(..., initialize=True)` 应用包内 fresh-install schema；运维也可以通过 `tbm resource export schemas/sqlite.sql sqlite.sql` 导出相同字节。五张表保存稳定 ID 与规范 JSON payload envelope；领域及跨记录不变量仍由 Store 最终验证。直接 SQL 修改 payload 不属于支持契约，会在 `load()` 或 `sync()` 重建 Store 时被拒绝。

`sync(store)` 是增量原子同步。顶层写入用 `BEGIN IMMEDIATE` 提前取得 writer reservation，并按与 PostgreSQL adapter 相同的 Trace、usage outcome、Failure Case、Lesson 和 Project Policy 规则，把记录分类为精确重放、受支持前向转换或不可变冲突。Failure Case 淘汰会级联 active Lesson；任何冲突或最终 Store 验证失败都回滚完整同步。同一个实例用 `RLock` 串行化 `sync()`、`load()`、`close()` 与 context entry；顶层回滚失败时保留主异常并重试，重试仍失败则即使连接由调用方传入也会关闭，防止之后提交部分事务。

`load()` 在一个读事务内检查 schema 版本 1，并执行每集合 100,000 条、总计 250,000 条、最大单条与累计 UTF-8 payload 各 64 MiB 的限制，再返回完整验证的 Store。传入既有连接时 Repository 借用连接；`connect()` 创建并拥有连接。已有调用方事务时，每次操作使用 savepoint，最终 commit/rollback 仍由调用方负责。

SQLite 是嵌入式选择，不替代 PostgreSQL 的数据库侧 JSONB、trigger、row lock、共享 ID registry 和多客户端约束。两个适配器共享公开 sync/load 生命周期语义；SQLite 使用 schema 版本 1，PostgreSQL 使用 schema 版本 2，DDL 和并发保证独立。

## PostgreSQL 运行时存储库

`PostgresMemoryRepository` 是完整 `TraceBackedMemoryStore` 的同步持久化边界。`psycopg` 是可选、延迟导入的 extra；核心包导入不加载驱动。

存储库只操作由规范 `schemas/postgres.sql` 安装的新 `public` schema，并锁定 metadata 行要求 schema 版本 2。既有版本 1 安装必须先执行包内原子 `schemas/postgres-v1-to-v2.sql`；在 Lesson/source-case 锁序修复前创建的版本 2 数据库应执行可重复运行且带版本门禁的 `schemas/postgres-v2-lock-order-hotfix.sql`。全新安装与当前 v1→v2 迁移已包含该修复；适配器不会自动执行迁移。

`sync(store)` 先获取内存快照，再在一个数据库事务中增量同步。它插入缺失记录，保留数据库中额外记录，不执行破坏性 reconciliation。已有记录在写前按规范形式比较；不可变冲突回滚整个事务。

Trace 只允许 unknown 到 measured 的前向完成，并只填充空执行证据；usage log 只允许封存未评估 outcome pair；Failure Case 只允许诊断、review、修复与状态字段的受支持更新；Lesson 与 Project Policy 只允许状态前向更新。

缺失主键行的 INSERT 在 nested savepoint 中执行。并发同主键 `23505` 或 registry 精确 `P0001` 会回滚保存点、重新 `FOR UPDATE` 并按相同规则判断 exact replay、forward update 或 protected conflict。找不到目标的碰撞和其他 driver 错误保持 `PostgresPersistenceError`。

`load()` 在第一次集合读取前按序对五表获取 `SHARE` 锁，然后执行 count 预检和 loaded-row UTF-8 JSON payload 预检。它在 fetch 前拒绝每集合超过 100,000、总计超过 250,000、单行或总载荷超过 64 MiB。Failure Case、Lesson、Project Policy projection 只排除 selector 不读取的内部 `updated_at`。

显式 `SHARE` 锁要求 schema owner 或具备表级写权限的角色。在调用方事务中，锁会持续到外层 commit/rollback。`PostgresMemoryRepository(connection)` 借用连接；`connect()` 创建并拥有连接。已有外层事务时使用 savepoint，否则正常提交。当前不提供 connection pool。

## 第 5 层：PR / CI Memory Report

PR 报告只考虑 Trace 的 repo/tenant 与当前 context 精确匹配、verified 且 regression-backed 的历史 Failure Case。它输出相关案例、source/fix provenance、建议回归测试，以及 prompt、tool、model 或 eval 变化风险。

`pr_memory_report()` 接受一种 change 输入。Legacy `changed_fields` 保留宽泛字段名匹配；值感知模式使用不可变 `PRChangeSet(field_name, old_value, new_value)`，支持 `prompt_version`、`prompt_family`、`tool`、`tool_schema_version`、`model` 和 `eval_suite`，最多 6 项。`model_family` 因 Trace 无精确 provenance 而不支持。

Store 要求每个 new value 与变更后 context 相等，并只匹配完整 old endpoint 或完整 new endpoint，排除混合配置。匹配 provenance 标为 `old`、`new` 或 `both`。Legacy warning 名称在扫描案例前一次验证，只保留最多 7 个支持名称的首次出现，使工作量保持 `O(W + C)`。

`pr_report_commit_anchors()` 与 `pr_memory_report()` 必须复用同一个 change set。报告先匹配 endpoint，再要求完整 ancestry evidence，最后排除 false 关系。只读 `pr-report` CLI 在显式 repo path 捕获 ancestry，输出 `commit_ancestry` 与 `report`，不接受 `--write` 或调用方伪造证据。

## Git 祖先关系适用性

`CommitAncestryEvidence` 是绑定一个当前 commit 的不可变 request-time 证据。运行时先通过 `candidate_commit_anchors()` 发现 metadata-scoped anchor，在 Store 锁外执行 `capture_commit_ancestry()`，再把同一对象传给检索；PR 流程使用对应 report anchor API。

单次捕获最多接收 1,000 个输入，重复项在去重前计数，overflow 时最多消费 1,001 项且不启动 Git。默认 runner 使用 binary `Popen`、`stdin=DEVNULL`、30 秒 timeout、UTF-8 replacement 解码和每个 stdout/stderr 64 KiB 上限；超时或输出溢出会 kill/reap。

Lesson anchor 是 source case 的 fix commit，Failure Case anchor 是 source commit，Project Policy 无 anchor。提供证据时必须覆盖所有候选 anchor 并绑定当前 `context.commit_sha`；缺失证据 fail closed，false 关系排除对应历史。省略证据保留兼容路径。

处理顺序是 metadata discovery、外部 ancestry capture、ancestry filter、可选关键词/semantic retrieval、System Gate、LLM Gate。证据不写入快照、usage log、YAML、Schema 或 PostgreSQL。

## Version-3 迁移准备

Version-3 准备路径与 runtime Store 严格分离。`SnapshotV3MigrationMapping`
显式提供 canonical repository/tenant binding、authorization scope、结构化
regression evidence、global-policy privileged approval 与明确 ancestry policy。
`plan_snapshot_v3_migration()` 只重建一次并冻结严格 version-2 source snapshot，
验证 mapping，在 required ancestry 下调用可信 relation verifier，然后返回带稳定
issue code 的确定性只读 plan；它不会合成不完整的 version-3 snapshot。

`tbm.snapshot.v2-to-v3.bundle.v1` 对原始 source、normalized source state、
mapping 与 plan 进行内容寻址。Bundle 解析有界并拒绝 duplicate key；验证会重跑
完整 preflight 并要求 plan 精确重放。内容寻址可以发现篡改，但不是 identity
signature 或 evidence attestation。

`SQLiteV3MigrationRepository` 使用独立表保存这些不可激活的 bundle。PostgreSQL
operator resource 只创建或删除 `trace_backed_memory_v3_staging`；其 metadata 与
bundle row 的 canonical trigger 会拒绝普通 update、delete 和 truncate；schema
owner 与 superuser 能修改 trigger，因此仍属于可信 operator。Rollback 枚举已知
object 并使用 `RESTRICT`，所以意外 object 或外部 dependency 会 fail closed。
两条 staging 路径都对 active adapter 不可见，且不提供 publication/activation 操作。因此 runtime
兼容边界仍是 snapshot version 2、SQLite schema version 1 与 PostgreSQL schema
version 2。完整契约见
[Version-3 迁移 bundle 与隔离 staging](migrations/v3-staging-bundles.zh-CN.md)。

## Durable GateSession version-3 契约

`gate_session_v3.py` 发布与持久化实现无关的 `tbm.gate-session.v3` 记录和显式
转换图，供未来 SQLite v2、PostgreSQL v3、`tbmd`、HTTP、MCP 与 SDK
实现共同使用。一条不可变记录绑定 tenant、canonical repository、principal、
agent client、Trace/run identity、request fingerprint、idempotency key、expiry、
lease 与各阶段证据 ID。每次状态转换都要求当前 revision，并返回 `version + 1`；
stale revision 与非法转换使用不同的稳定错误码。

生命周期与 lease 时间戳由服务端权威生成。纯领域契约接收显式时间戳以保证 replay
确定性，但不允许 client 自行选择时间。未来 repository 在提交转换前必须使用事务内
数据库/service 时间与已持久化 lease/expiry 比较。

记录按生命周期顺序累积引用：retrieval snapshot 与 System Gate evaluation、
semantic Gate decision attempt、精确 final memory revision、injection artifact 与
usage decision，最后是 run outcome。cancel、expiry 与 abandon 均为 terminal，
并保留有界 reason。active 状态必须持有 lease，terminal 状态清除 lease。严格
外部 parser 有界、拒绝 duplicate key、检查 finite number，并拒绝未知字段。完整
契约见 [Durable GateSession version-3 契约](protocols/gate-session-v3.zh-CN.md)。

`sqlite_gate_session_v3.py` 增加 opt-in、side-by-side 本地 repository。它保存
append-only canonical revision payload 与 CAS head，按
tenant/repository/principal/agent 限定原子 idempotency index，使用可信 service
time，通过 savepoint 保留调用方 transaction，检测 canonical DDL drift，并提供有界
due-session discovery。其独立 metadata 与 `schemas/sqlite-v3-gate-session.sql`
使 active SQLite schema version 仍为 1。

`postgres_gate_session_v3.py` 在隔离
`trace_backed_memory_v3_gate_session` schema 中增加对应的 opt-in PostgreSQL
adapter。带版本门禁的 install 与 fail-closed rollback 保留 active PostgreSQL
schema version 2。repository operation 先获取 metadata lock，再锁定 head、采样
database time、追加 canonical revision 并执行 exact CAS。确定性 C-collation
identity index、固定 search path 的 trigger function、catalog shape 校验、调用方
savepoint 与 payload/head 交叉检查提供数据库侧约束，但不会激活该 schema。

两个 repository 都不会连接私有 Store request token。当前本地 Agent 与 STDIO MCP
仍为进程内状态。expiry/recovery worker、service orchestration、authorization 与
跨 adapter conformance 全部实现后，该 session 契约才能成为 distributed runtime
authority。

## 内容寻址重放 version-3 契约

`replay_v3.py` 发布与存储实现无关的精确 artifact 字节 descriptor、最终渲染
injection，以及重放一个 decision 所需的固定八项 evidence manifest。artifact ID
从带标签的 SHA-256 摘要派生。complete manifest 绑定 retrieval、两层 Gate、
ancestry、policy、renderer 与 injection evidence；`legacy_partial` 只精确记录缺失
component，不允许声称可精确重放。manifest 还绑定自身的规范内容 hash。

敏感 descriptor 必须带 encryption-key metadata，但该纯模块不存储、加密、授权、
保留或记录 artifact 字节。hash 只能证明内容身份，不能证明 provenance 真实性或授权。
严格 parser 有界，并拒绝 duplicate key、未知字段、非法时间戳与非规范 component
set。详见[内容寻址重放契约 v3](protocols/replay-v3.zh-CN.md)。

`sqlite_replay_v3.py` 增加 opt-in 隔离账本，以 immutable row 保存 artifact、
injection 与 manifest。每个 bundle 在一个事务中保存；manifest-to-injection 使用
foreign key；load 时复验 canonical schema object、重复 column、descriptor、边界
和精确字节。调用方 transaction 通过 savepoint 保留 ownership。

`schemas/postgres-v3-replay.sql` 与 fail-closed rollback 在不改变 active schema
version 2 的前提下建立匹配的隔离 PostgreSQL 关系边界。安装先锁定 active metadata，
再原子创建有界 artifact bytes、injection descriptor、manifest、foreign key、index
与固定 `search_path` immutability trigger；rollback 锁定两份 metadata，并在
`RESTRICT` 删除前核对预期 catalog membership。SQL 强制 derived ID、精确关系
linkage 与 injection shape；canonical descriptor 和 content-digest 验证仍由可信
opt-in `PostgresReplayV3Repository` 在 write 前和 load 后执行。该 repository 与
SQLite 的 idempotency/conflict 语义对等，通过 psycopg savepoint 保留 caller
transaction ownership，并在每次操作检查 metadata、catalog、trigger shape/state
与 canonical function body。跨记录 authorization 与 GateSession service
transaction 仍待完成。

active v2 Store 与 persistence adapter 尚不输出这些契约。SQLite/PostgreSQL replay
repository 都提供原子 artifact storage；两者都不提供
GateSession linkage、access control、encryption、retention 或 runtime authority。
统一 version-3 runtime 仍需交付这些边界和 cross-adapter conformance。

## 授权 version-3 契约

`authorization_v3.py` 为未来 service boundary 发布与存储实现无关的 policy 与
evaluator。policy 把 principal 和 agent client 绑定到精确 canonical repository、
租户作用域 alias、显式 permission，以及 global/tenant/repository role scope。
策略构造会在任何请求求值前校验注册表唯一性与全部跨记录目标。
仓库操作要求精确 tenant/repository 目标；`memory:review` 与
`memory:activate` 可面向精确仓库或其所属租户。全局策略的创建与批准使用彼此
分离且不携带目标的权限。

求值器有意先于检索运行：先取得服务端认证 identity context，再检查状态与租户、
精确解析 repository，最后求值 active binding。scope attribute 复用有界适用性
词汇，但授权求值器会忽略它们；它们可以在后续缩小检索，不能授予访问权。decision
绑定精确 canonical request 与 policy hash，并可通过
`verify_authorization_decision` 重新计算核验。

这些 hash 是内容身份，不是签名。opt-in 隔离
`SQLiteAuthorizationV3Repository` 与 `PostgresAuthorizationV3Repository`
持久化 immutable policy/decision，在追加前
要求精确 request/policy/decision 核验，强制每个 request 只有一个 decision
identity，重验已存 descriptor，并在 schema drift 时 fail closed，同时使用
savepoint 保留调用方事务。PostgreSQL 使用受 active-v2 门禁的原子 install、
fail-closed rollback、immutable trigger 与精确 catalog 检查。两者都不认证调用
方、不签发可重用 capability，也不连接 active Store、Agent、MCP 或 GateSession
repository。
详见[授权 v3 契约](protocols/authorization-v3.zh-CN.md)。

`entity_registry_v3.py` 以带版本、内容寻址的 Organization、正式 Tenant 与
Environment identity 补全 authorization namespace。它复用 authorization policy
中的 Principal、AgentClient、canonical Repository、alias 与 RoleBinding，并要求
每个被引用的 tenant 处于 active 状态且属于 active organization；repository 范围
environment 也必须与 repository 处于同一 tenant。这是引用完整性，不是调用方认证
或授权。opt-in 隔离 `SQLiteEntityRegistryV3Repository` 会把全部 record、permission
与 attribute 物化为带复合外键的规范化不可变行，并在每次读取时对照 canonical
descriptor 字节复验；它通过 savepoint 保留调用方事务，并在 schema drift 时
fail closed。`PostgresEntityRegistryV3Repository` 提供匹配的规范化持久化，包括
active-v2 安装门禁、完整 catalog/ACL fingerprint、不可变 DML/TRUNCATE guard、
并发精确重放、调用方 savepoint，以及保留 schema 外部依赖的 fail-closed rollback。
active adapter 尚未使用这两个 authority。详见
[实体注册表 v3 契约](protocols/entity-registry-v3.zh-CN.md)。

`service_v3.py` 在这些 authority 之上增加首个与存储无关的认证 retrieval
orchestrator。可信 transport 代码提供精确 Principal/AgentClient record 与服务端持有
的 tenant、repository、environment context。orchestrator 会求值并持久化授权、读回
完全相同的 decision、重新加载完整 registry 以检测 policy/entity 轮换、对 canonical
target 校验 active environment，之后才调用 retrieval。deny、持久化失败、drift 与
callback failure 都使用清洗后的稳定错误 fail closed。
`authenticated_agent_v3.py` 提供可选启用的 active 本地 Agent 门面：调用方输入不含
identity 或 target 字段，旧版 Trace 的 tenant/repository 值会被覆盖，授权在 Trace
注册前完成，并以 canonical authorized tenant/repository 同时绑定 Trace 与
retrieval context。进程内 ownership 索引阻止一个门面使用另一个门面的 lifecycle
handle。它不是 transport authentication；MCP 可通过可信本地启动选择它，普通 CLI
operation、HTTP 与 SDK adapter 尚未选择它。详见
[认证 retrieval service 边界](protocols/authenticated-service-v3.zh-CN.md)。

`gate_service_v3.py` 把该边界与任一 GateSession authority 组合起来。它在
preparation 前持久化并读回 scoped `CREATED` session；精确 idempotent replay 不会
重复 preparation；RetrievalSnapshot/SystemGateEvaluation evidence 必须由可信
verifier 核验；验证后才通过 CAS 发布 `PREPARED`。失败会尝试精确 `CANCELED`
补偿，并发或异常 durable state 会返回 recovery required。它不持久化 Store token，
也不宣称跨 authority 原子事务。详见
[认证 durable Gate preparation](protocols/authenticated-gate-service-v3.zh-CN.md)。

`sqlite_gate_evidence_v3.py` 与 `postgres_gate_evidence_v3.py` 为该 verifier
提供 immutable evidence authority。两者都在一个 transaction 中保存精确的
RetrievalSnapshot/System Gate 记录对，并在 storage-neutral verifier 将记录绑定到
已授权 session、Trace、run 与 identity scope 之前读回两份记录。SQLite 通过
recursive immutable trigger 拒绝 replacement write；PostgreSQL 增加 active-v2
安装门禁、完整安全 catalog fingerprint、并发精确重放与 fail-closed `RESTRICT`
rollback。evidence 写入与 GateSession transition 仍是跨 authority 的有序补偿，
而不是一个 atomic transaction。详见
[SQLite 与 PostgreSQL Gate evidence v3](protocols/sqlite-gate-evidence-v3.zh-CN.md)。

`sqlite_semantic_gate_v3.py` 在该 SQLite evidence 边界上，为每个 System Gate
evaluation 增加一条不可变有序 SemanticGateAttempt chain。唯一 sequence 与 CAS
head 拒绝 fork；canonical 读回会核验全部 descriptor 与关系列；完整 chain verifier
则根据持久化 Gate evidence 重新检查单调缩小规则。它仍是 opt-in、side-by-side
ledger。`postgres_semantic_gate_v3.py` 提供隔离 PostgreSQL 对等实现，包含
active-v2 install 门禁、row-lock 串行化、deferred chain consistency、精确安全
catalog 校验、调用方 savepoint 与 fail-closed `RESTRICT` rollback。两者均未接入
active Agent/MCP emission。`semantic_gate_artifact_v3.py` 现已把精确非空
prompt/response 字节、内容派生 ID、classification 与 encryption metadata
绑定到 attempt 对应角色，但不会把字节嵌入 JSON。durable artifact 仓库、
`sqlite_semantic_gate_artifact_v3.py` 现已提供 SQLite 持久化：一个外层
transaction 组合 attempt append、精确 public/internal 字节、角色 binding、SQL
digest/descriptor guard 与完整读回。`postgres_semantic_gate_artifact_v3.py`
现已提供 PostgreSQL 对等存储：隔离且受 active-v2 门禁的 schema 增加数据库
SHA-256/descriptor guard、catalog 校验、并发精确 replay、调用方 savepoint 与
fail-closed `RESTRICT` rollback。两个字节仓库都不提供静态加密，因此均拒绝敏感
明文。`semantic_gate_service_v3.py` 现会核验精确可信的
provider/authenticator/credential registration，在调用前重新加载 Gate evidence 与当前
retry parent，由服务端持有 provider/model/template/config provenance，采样可信开始/结束
时间，并通过任一 repository 原子保存 attempt 与精确字节。GateSession/replay 事务挂接、
有签名 provider attestation、retention/access control 与 active emission 尚未提供。详见
[已认证 Semantic Gate 服务 v3](protocols/semantic-gate-service-v3.zh-CN.md)、
[Semantic Gate artifact 绑定 v3](protocols/semantic-gate-artifact-v3.zh-CN.md)、
[SQLite Semantic Gate artifact 仓库 v3](protocols/sqlite-semantic-gate-artifact-v3.zh-CN.md)、
[PostgreSQL Semantic Gate artifact 仓库 v3](protocols/postgres-semantic-gate-artifact-v3.zh-CN.md)、
[SQLite Semantic Gate attempt ledger v3](protocols/sqlite-semantic-gate-v3.zh-CN.md)
与
[PostgreSQL Semantic Gate attempt ledger v3](protocols/postgres-semantic-gate-v3.zh-CN.md)。

`gate_worker_v3.py` 在两个 GateSession authority 上增加首个有界 recovery
worker。它预先验证未锁定 due page；只对 session 已到期的
`PREPARED`/`AWAITING_DECISION` head 执行精确 CAS 与读回；lease-only 与 state
graph 不允许的状态返回 recovery required；并发 head 移动标记为 superseded。
每个 candidate 是独立 operation，而不是一个 batch transaction。详见
[GateSession recovery worker](protocols/gate-recovery-worker-v3.zh-CN.md)。

storage-neutral `tbm.regression-evidence.v3` 是 migration mapping 之外第一层面向
生产的 evidence boundary。其内容派生 identity 绑定不同的 source/verification
Trace、expected/observed outcome、evaluator/environment provenance、精确
source→fix→verification commit 关系、artifact、相互独立的 submitter/verifier
principal 与 attestation hash。它不会激活 memory、验证签名或替代 active v2
boolean。详见[结构化 regression evidence v3](protocols/evidence-v3.zh-CN.md)。

proposal-only `tbm.memory-revision.v3` 随后把 stable memory identity 绑定到
immutable、内容派生 revision、精确 parent revision、content artifact、canonical
authorization scope、case/fix/evidence 引用与 server-owned proposer context。其
evidence preflight 会拒绝缺失、未通过、跨 case 或 proposer 冲突的 evidence。独立的
`tbm.memory-revision-approval.v3` 与
`tbm.memory-revision-activation.v3` 内容派生 event 现提供 storage-neutral
publication contract。approval 重新验证精确字节、evidence、lineage、actor 分离与
`memory:review`；activation 重放完整 approval verification，并独立检查
`memory:activate`、第三位 actor 与线性 immediate-predecessor linkage。
storage-neutral builder 自身不能证明 durable currentness。禁止 global revision
publication 与 chain 内 target relocation。这些 event 不是签名。opt-in SQLite 与
隔离 PostgreSQL publication authority 现提供 durable head lock、精确 authorization
provenance、调用方拥有的 attestation-verifier boundary、append-only row、幂等
replay 与 commit 前读回，但不投影到 active v2。详见
[MemoryRevision proposal 与 publication event v3](protocols/memory-revision-v3.zh-CN.md)。

Opt-in、隔离的 SQLite 与 PostgreSQL proposal ledger 会把该 revision 连同精确
FixEvidence 和有序 regression-evidence 闭包一起持久化。两者在 replay 时都会先
验证完整存储 bundle，再执行任何插入，因此会拒绝而不是修补被篡改的记录。
PostgreSQL 对等实现还提供 active-metadata 锁顺序、catalog/ACL fingerprint、
immutable UPDATE/DELETE/TRUNCATE trigger、caller-compatible transaction 与
fail-closed rollback 资源。两种 proposal ledger 都不持久化 approval/activation
event，也不执行 publication authority、authorization、retention 或 active-v2
projection。详见
[SQLite](protocols/sqlite-memory-revision-v3.zh-CN.md)与
[PostgreSQL](protocols/postgres-memory-revision-v3.zh-CN.md) ledger 契约。
对应的
[SQLite](protocols/sqlite-memory-publication-v3.zh-CN.md)与
[PostgreSQL](protocols/postgres-memory-publication-v3.zh-CN.md) publication
authority 依赖这些 proposal ledger，同时隔离保存 approval、activation、
authorization provenance 与 target-scoped CAS head。

storage-neutral `tbm.retrieval-snapshot.v3` 契约记录 prepared GateSession
引用的精确已授权检索结果。它在内容派生身份下绑定授权事件、context/query 摘要、
retriever 与不可变 index 版本、有序 memory-revision 命中、候选哈希、有限的
逐阶段/融合分数、top-K 上限及显式截断原因。它不记录 System Gate 或 Semantic
Gate 结果，不能授予访问权或重新打开 block。active retrieval 仍只返回
`MemoryItem`，不会产生该快照。详见
[可回放 RetrievalSnapshot v3](protocols/retrieval-snapshot-v3.zh-CN.md)。

配套 `tbm.system-gate-evaluation.v3` 与 `tbm.semantic-gate-attempt.v3`
随后绑定逐候选确定性策略结果及有序模型 attempt provenance。跨记录核验要求精确
session/snapshot/candidate 覆盖，并强制最终 semantic allow 是 System Gate allow
的子集、全部 System block 保持 blocked。prompt/response 内容保留在引用 artifact
中。active policy execution 尚不产生这些记录。详见
[门禁评估 v3](protocols/gate-evaluation-v3.zh-CN.md)。

配套 `tbm.run-outcome.v3` 与 `tbm.outcome-attribution.v3` 补全了
storage-neutral runtime evidence 链。RunOutcome 把 completed GateSession
绑定到精确 trace/run/usage decision、evaluator、output/tool-output 摘要、
artifact evidence 与测量值。OutcomeAttribution 刻意保持独立：runtime
observation 只能产生 association；causal 结论必须使用非观察性方法并由独立
verifier 核验。active Store 仍以既有 v2 outcome 字段为准，不能静默升级。
详见[运行结果与归因 v3](protocols/outcome-v3.zh-CN.md)。

opt-in `SQLiteOutcomeV3Repository` 与 `schemas/sqlite-v3-outcome.sql`，以及
隔离 `PostgresOutcomeV3Repository` 与 `schemas/postgres-v3-outcome*.sql`
提供对等的持久化 completion transaction。两者都与受保护的 GateSession
authority 共用 connection 与 lock，从当前 `EXECUTING` session 派生
trace/run/usage identity，用同一个可信 timestamp 构造两条记录，通过 CAS 追加
`COMPLETED`、插入 immutable RunOutcome，并在 commit 前精确读回。PostgreSQL
只在锁定当前 head 后读取数据库时间；其 insert trigger 会重建 canonical
descriptor bytes 并在接收记录前重算 outcome payload SHA-256，同时校验完整
install/rollback catalog。
调用方拥有的 transaction 使用 savepoint；相同 terminal replay 幂等。不同
measurement、stale version、时钟倒退、trigger/catalog 失败或 read-back
mismatch 会回滚整个操作。`GateSessionCompletionService` 会验证返回记录对与
持久化读回，而不复制 lifecycle policy。opt-in
`SQLiteOutcomeAttributionV3Repository` 与隔离的
`PostgresOutcomeAttributionV3Repository` 在 completed outcome 上提供 immutable
multi-claim ledger；append 与读取都会重新核验 canonical descriptor 以及精确
outcome/session/usage/final-revision linkage，保留 caller savepoint，并拒绝
replacement write 或 schema drift。PostgreSQL peer 还提供数据库 content-ID
重算、row lock、完整 catalog 校验、并发 replay 与 fail-closed rollback。

storage-neutral completion-outbox 契约把一条 immutable
`execution_completed` event 与 append-only delivery revision 分离。opt-in
`SQLiteCompletionOutboxV3Repository` 与隔离
`PostgresCompletionOutboxV3Repository` 扩展各自 completion transaction，使
completed GateSession revision、RunOutcome、event、初始 `pending` delivery
与 delivery head 一起 commit 或 rollback。claim 使用有界 lease 与 versioned
head；acknowledgement、retry wait、expired-lease reclaim 和 dead-letter
transition 都追加新 revision。Delivery 是 at least once，因此 consumer 必须按
内容派生 event ID 去重。SQLite 使用 thread-local mutation scope 与共享 connection
lock；PostgreSQL 使用 database-time transition、row-locked `SKIP LOCKED`
claim、CAS head、canonical database trigger、精确 catalog 校验与 fail-closed
rollback。evaluator authentication、artifact authorization 与 active runtime
emission 仍是独立后续工作。storage-neutral
`CompletionOutboxDeliveryWorker` 可以在任一 authority 上执行一次有界 dispatch：
在 consumer side effect 前校验整个 claim page，只持久化清洗后的 consumer error
code，使用精确 version 写入 acknowledgement/failure，核验完整 transition 与
durable read-back，并明确报告 delivered、retry、dead-letter、superseded 或
recovery-required 状态。它不提供 network client；调用方 consumer 必须按 event ID
幂等，并选择能够覆盖最长处理时间的 lease。详见
[SQLite RunOutcome 完成事务 v3](protocols/sqlite-outcome-v3.zh-CN.md)与
[PostgreSQL RunOutcome 完成事务 v3](protocols/postgres-outcome-v3.zh-CN.md)，
以及 [SQLite OutcomeAttribution ledger v3](protocols/sqlite-outcome-attribution-v3.zh-CN.md)
与 [PostgreSQL OutcomeAttribution ledger v3](protocols/postgres-outcome-attribution-v3.zh-CN.md)，
以及 [Completion outbox v3](protocols/completion-outbox-v3.zh-CN.md)。

`tbm.audit-event.v3` 提供内容寻址 append-only stream，绑定精确 parent
及 actor/reference provenance。配套 `tbm.recovery-action.v3` 记录一次已完成
恢复尝试，并对照既有派生 MemoryRunRemediation 或 expected GateSession revision
核验。opt-in `SQLiteAuditV3Repository` 与隔离
`schemas/sqlite-v3-audit.sql` ledger 保存 immutable stream event 与 CAS head，
原子追加 RecoveryAction 及其匹配 event，拒绝同一 session 内的 request-digest
碰撞，读取时重新核验 canonical descriptor，通过 savepoint 保留调用方事务，并在
schema drift 时 fail closed。该 repository 只是 evidence storage，不替代 Store
lifecycle、authorization service、authenticated actor boundary 或原子的
GateSession/remediation transition。详见
[审计事件与恢复动作 v3](protocols/audit-recovery-v3.zh-CN.md)。

`PostgresAuditV3Repository` 与 `schemas/postgres-v3-audit*.sql` 提供匹配的
opt-in 多进程 ledger，且不改变 active PostgreSQL schema version 2。安装先锁定
active metadata，再原子创建有界 stream head、immutable event、精确
RecoveryAction/event 配对、固定 `search_path` trigger 与 deferred consistency
check。repository append 会锁定单一 stream head、复核当前 parent、插入
event/action，并通过精确 CAS 推进 head。每次操作都会核验 relation、index、
constraint、column、trigger 绑定/状态、function 配置/body 与 metadata catalog。
rollback 锁定 ledger，并在 `RESTRICT` 删除前拒绝 catalog drift 或外部依赖。
psycopg nested transaction 通过 savepoint 保留调用方 ownership。与 SQLite
ledger 相同，它仍只是 evidence storage，不是 authorization 或
Store/GateSession transition boundary。

## 非目标

当前 scope 是“仅匹配 memory 已声明字段”的语义，不是多租户授权模型；省略 `repo` 或 `tenant` 的 memory 不会自动获得对应硬边界。已发布的授权 v3 契约准备了 canonical repository 与 global/repository/tenant role boundary，但生产隔离仍需要认证 service integration 和 durable policy enforcement。snapshot version 2 也不会持久化 Gate request、retriever/gate/renderer 版本与 hash 或结构化 regression run 证据，Git ancestry 仍为 opt-in。这些属于 schema v3 / PostgreSQL schema v3，而不是当前 Alpha 契约。

version-2 snapshot 中 verified 但未 review 的 case 必须先补齐 review 证据；既有 PostgreSQL schema-version-1 安装必须先应用包内 `schemas/postgres-v1-to-v2.sql`，再进行同步。

- 不优先构建通用个性化记忆。
- 不把原始 Trace 直接注入 prompt。
- 不把向量相似度视为相关性的充分证明。
- 不允许 LLM 在未验证时激活记忆。
- 除明确的 PostgreSQL v1→v2 operator 脚本外，不提供自动在线迁移框架。
- 不提供 connection pool 或其生命周期管理。
- 不提供异步 PostgreSQL repository。
