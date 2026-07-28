# 产品交付计划

[English](product-program.md) | **简体中文**

## Phase 0：项目定义

- 定义 memory object model、人工维护的 Project Policy 和 System Gate 策略。
- 定义 debug、repair、regression、planning、eval、production 六种模式。
- 规定 repo、commit、prompt、tool schema、model 与 eval suite 的采集和 Trace provenance。

## Phase 1：Trace 采集

- 记录运行身份、Git metadata、prompt/tool/model/eval provenance、执行证据与 Trace URI。
- 拒绝空身份、非法 `eval_result` 与畸形 JSON-like collection，并把 raw Trace 排除在运行时 prompt 之外。
- 使用无依赖 helper 捕获 Git metadata，并把命令失败包装为带上下文的 `TraceMetadataCaptureError`。

## Phase 2：Failure Case 提取

- 从失败 Trace 分类并创建 Failure Case 草稿，绑定 source Trace 与 commit。
- 加载 failure taxonomy，并通过保守证据优先级覆盖仓库中的具体分类。
- 提供人工 review，记录 reviewer、root cause、notes 和时间。
- Store 拒绝缺失来源、commit 不一致、非法状态和缺失 verified 证据的案例。

## Phase 3：验证闭环

- 只有 draft case 可以转换为 verified。
- verified 必须绑定修复 commit 并有通过的 regression evidence。
- 提供 Failure Case 与 derived Lesson 的 forward-only obsolete 转换。

## Phase 4：Lesson Memory

- 只从 verified case 创建 Lesson，要求 source case、已知非空 scope 与 0.0 到 1.0 confidence。
- 提供 JSON snapshot、active Lesson YAML 与 PostgreSQL 持久化。
- 保留 numeric-looking scope string，拒绝缺失/未验证来源。
- 存储人工 Project Policy，并保证三类 runtime memory ID 全局唯一。

## Phase 5：Memory 检索与门控

- metadata-first 检索，所有声明 scope 必须匹配；再执行可选关键词或 bounded semantic score。
- 先运行确定性 System Gate，再运行 LLM applicability Gate；关键词和 score 都不是 approval gate。
- 把 `prepare_memory()` / `finalize_memory()` 定义为主路径，包含 stale-state recheck、Trace linkage、bounded injection 与 atomic audit logging。
- 严格验证 context、decision、usage log 与 JSON Schema，并固定 prompt、candidate、snippet 预算。

## Phase 6：CI / PR 集成

- 根据 repo-matched Trace 报告 verified、regression-backed 历史失败。
- 输出 Trace/case/fix provenance、建议 regression test 和 prompt/tool/model/eval 风险。
- 排除缺失 repo provenance 或与当前 repo 不匹配的历史记录。

## Phase 7：指标

- 跟踪 candidate、allowed/blocked、with/without-memory pass rate、wrong-memory failure、obsolete use 与 Lesson confidence。

## Phase 8：PostgreSQL 持久化（已实现）

- 通过可选 `postgres` extra 提供同步 `PostgresMemoryRepository`，要求 PostgreSQL 12+ 和 fresh `public` schema version 1。
- 支持增量、原子 sync，规范比较、冲突回滚、前向生命周期、正常 Store load 和 borrowed/owned connection。
- 明确不提供原地 migration、connection pool 和 async repository。

## Phase 9：Git 祖先关系适用性（已实现）

- 在 Store 内发现 runtime/PR anchor，在锁外针对当前 commit 捕获不可变 ancestry evidence。
- Lesson 使用 fix commit，Failure Case 使用 source commit，Project Policy 仅豁免 ancestry。
- 提供证据时必须完整且绑定正确 commit；false relation 排除历史，但两层门控仍具权威性。

## Phase 10：PR Change-Set 端点匹配（已实现）

- 用不可变 `PRChangeSet` 对 prompt、tool、model、eval provenance 做精确 old/new endpoint 匹配。
- new value 必须绑定 post-change context，只接受完整 old 或完整 new 配置，并输出 `old`、`new`、`both` provenance。
- 保留 legacy `changed_fields` 宽泛行为，不支持精确 `model_family`。

## Phase 11：基准示例泄漏分类（已实现）

- 把 benchmark identity 定义为 `(eval_suite, input_hash)`，由调用方负责稳定命名、规范化与隐私哈希。
- source-derived memory 在运行时临时携带 source identity，完整相同 pair 在所有模式阻止。
- 不完整 identity 不猜测匹配；finalization 绑定 context/Trace 并把 block reason 写入审计。
- 不增加持久化 memory 字段，保持 snapshot version 2 与 PostgreSQL schema version 1。

## Phase 12：结果感知指标（已实现）

- `pass`、`fail`、`error` 为已评估；`unknown`、`None` 不进入分母。
- 暴露 with/without-memory 样本数与 unevaluated decision count，三者之和为 decision count。
- 这些是 decision 观测，不是逐 memory 因果归因；指标不持久化。

## Phase 13：逐 Memory Outcome 指标（已实现）

- `memory_outcome_metrics()` 为所有 Failure Case、Lesson、Project Policy 返回按 ID 排序的稳定 tuple，包括零观测项。
- 统计 candidate、used、blocked 与实际使用后的 evaluated/pass/fail-or-error/unevaluated/pass-rate。
- 多 memory run 把同一结果关联到每个 used ID，不推导逐项 causal attribution。

## Phase 14：声明式 Trace Provenance 绑定（已实现）

- `repo`、`commit_sha`、`tenant` 始终与关联 Trace 匹配；其他 provenance 只在 context 声明时绑定。
- 声明 tool 必须匹配纯字符串 Trace tool call；无等价 Trace 字段的 context 保持不绑定。
- 在 request 消费和 usage append 前验证，保证 mismatch 原子失败。

## Phase 15：延迟 Decision Outcome 封存（已实现）

- 添加 `record_decision_outcome()`，允许在 finalization 后按 decision ID 写入 measured outcome。
- 只允许未评估到 `pass`/`fail`/`error` 的一次前向转换；相同 pair 幂等，重写结果或归因原子拒绝。
- PostgreSQL 只允许相同 usage outcome pair 前向更新，其他字段不可变。

## Phase 16：延迟 Trace 完成（已实现）

- 添加 `complete_trace()`，允许先记录 unknown Trace，执行后补充 measured outcome 与执行证据。
- identity、provenance、输入与创建时间不可变；完成字段只填空槽或精确重放。
- PostgreSQL 提供相同行锁前向转换，拒绝 stale/reverse/conflict。

## Phase 17：原子 Memory Run 完成（已实现）

- 添加 `complete_memory_run()`，要求准确 Trace/decision linkage，并在一把锁下验证两侧候选后同时赋值。
- 支持精确重放和匹配部分恢复；任何结果、归因、证据或 linkage 冲突都不留下半完成状态。
- PostgreSQL 在同一事务同步两行，usage conflict 会回滚 Trace update。

## Phase 18：Memory Run 审计视图（已实现）

- `memory_run_audits()` 按 decision ID 返回冻结记录。
- 状态为 `pending`、`trace_only`、`decision_only`、`complete`、`conflict`。
- one-sided 状态用于恢复；conflict 只供人工调查，不自动选边；视图不持久化。

## Phase 19：安全 Memory Run 恢复（已实现）

- `recover_memory_run()` 只接受 decision ID，从关联记录推导 Trace ID 与结果。
- 支持 Trace-only、decision-only 和 complete replay；pending/conflict 拒绝。
- fail/error Trace-only 必须显式提供 `memory_caused_failure`，并复用原子完成。

## Phase 20：Memory Run 健康指标（已实现）

- `memory_run_metrics()` 按 usage decision 统计五种状态与 recoverable count。
- 五种状态互斥且总和等于 decision count，pending/conflict 不算自动可恢复。
- 汇总为派生值，快照与 PostgreSQL load 后可重建。

## Phase 21：原子批量 Memory Run 恢复（已实现）

- `recover_memory_runs()` 接受唯一 decision ID 的非空 tuple 与可选 attribution mapping，并保持请求顺序。
- 只处理入口状态已是 one-sided 或 complete 的项；任意 pending/conflict/缺失归因拒绝整个批次。
- 共享 Trace 的推导结果必须一致；批量接口不接受 caller outcome 或 Trace evidence。

## Phase 22：原子批量 Memory Run 完成（已实现）

- 导出 `MeasuredEvalResult` 与冻结 `MemoryRunResult`，支持 outcome、attribution 和可选 Trace evidence。
- `complete_memory_runs()` 从 decision 推导 Trace ID，按请求顺序返回，且只合并互不重叠或相同的共享证据。
- 完成与恢复共用 non-mutating stager，同时保持 recovery 的严格派生语义。

## Phase 23：Memory Run 补救计划（已实现）

- `memory_run_remediations()` 把审计映射为 `measure`、`recover`、`recover_with_attribution`、`investigate`、`none`。
- 只有当前记录确定时才输出 resolved result/attribution；plan 不授予写权限，写 API 必须重新验证。
- 健康指标加入自动恢复和需要归因数量，两者之和等于 recoverable。

## Phase 24：原子 Ready Memory Run 恢复（已实现）

- `recover_ready_memory_runs()` 在同一可重入锁下规划并应用当前 `recover` 项。
- 保持 decision ID 顺序，无工作返回空 tuple，跳过 pending、需归因、conflict 与 complete。
- 并发 sweep 串行化并重新规划，选择本身不持久化。

## Phase 25：快照操作 CLI（已实现）

- 提供 `tbm` 与 `python -m trace_backed_memory`，包含 validate、stats、audit、metrics、remediation 与 recovery 命令。
- 所有变更默认 dry-run，只有显式 `--write` 且完全成功时才原子替换同一快照。
- 成功输出确定性 JSON，失败输出结构化 JSON；退出码 0-4 覆盖成功、内部、输入、状态和写入结果。
- CI 构建 wheel/sdist，smoke test 两个入口并覆盖 Python 3.11-3.13。

## Phase 26：同步 Memory Run 执行（已实现）

- 添加无依赖 `run_memory_execution()`，固定 prepare、decide、finalize、execute、atomic complete 顺序。
- 定义公开 decision/execution callback 与不含 decision ID 的 `MemoryRunMeasurement`。
- `MemoryRunExecutionError` 保留阶段、request、finalized result、ID 和原始 cause，不从异常猜测 outcome。

## Phase 27：打包分发资源（已实现）

- 在 wheel、sdist、editable 中发布 18 个字节一致的 schema、memory、example 文件。
- 添加 allowlisted `packaged_resources()`、`read_packaged_resource()`、`export_packaged_resource()` 与 metadata/SHA-256。
- 无路径 taxonomy 使用包内资源；CLI 提供 list/read/export；发行包声明 `py.typed`。

## Phase 28：证据摄取完整性（已实现）

- 失败提取扩展到 `tool_outputs` 顶层 error，但不搜索成功输出、任意字段或嵌套文本。
- taxonomy 与 Lesson YAML 拒绝重复字段，完整解析后暂存验证并一次提交。
- 不改变有效 YAML shape、provenance、快照和数据库 schema。

## Phase 29：Measured Memory Run Completion CLI（已实现）

- 添加 `complete`，要求快照、Trace ID、decision ID 和明确的 measured result。
- 可选归因与 Trace evidence，不推断任何值；tool outputs 只从严格 UTF-8 JSON 对象数组读取。
- 委托 `complete_memory_run()`，默认 dry-run，显式 `--write` 才原子发布。

## Phase 30：Lesson YAML 持久化完整性（已实现）

- JSON 与 Lesson YAML 统一经过 sibling temporary file、LF、flush、`fsync()` 与 `os.replace()`。
- 失败保留旧目标并清理临时文件；Lesson 输出规范 `lesson_text: |`，输入兼容历史 `>`。
- 精确保留空行、首尾 LF 和行内空格，同时保留受限 parser 的 all-or-nothing 语义。

## Phase 31：批量 Measured Completion CLI（已实现）

- 添加 `complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]`。
- 严格解析非空 allowlisted `MemoryRunResult` 数组，拒绝重复键、缺失/未知字段、非法类型与非有限数。
- 只调用一次 `complete_memory_runs()`，保持 manifest 顺序和批量原子性。

## Phase 32：有界本地文档摄取（已实现）

- 单句柄读取并在 UTF-8 解码前拒绝超限字节。
- 快照限制 64 MiB/每集合 100,000/总计 250,000；Lesson 8 MiB/10,000；taxonomy 1 MiB/1,000；CLI JSON 8 MiB/10,000/100,000 nodes/depth 100。
- Python 可显式 `None` 关闭单项限制用于可信迁移；CLI 限制固定。

## Phase 33：PR Report CLI（已实现）

- 添加只读 `pr-report`，严格解析 context 与 `field_changes` 为 `MemoryContext` 和 `PRChangeSet`。
- 复用同一 change set 完成 anchor discovery、锁外 ancestry capture 和 report。
- 输出 `commit_ancestry` 与 `report`，不提供 `--write`、caller ancestry 或 legacy fields。

## Phase 34：Active Lesson 可移植 CLI（已实现）

- 添加 Lesson export/import；export 默认 no-replace 并拒绝源快照别名，import 默认完整 dry-run。
- 固定使用 8 MiB/10,000 条限制，复用 Store duplicate、shared-ID、provenance、active-only 与 all-or-nothing 规则。
- `save_lessons_yaml()` 增加向后兼容的 keyword-only `overwrite`。

## Phase 35：Memory Obsolescence CLI（已实现）

- 添加单项 `obsolete`，显式 kind，委托对应 Store 生命周期方法。
- Failure Case 输出 active derived Lesson cascade 预览；只返回非敏感 ID、状态、计数和 `written`。
- 默认 dry-run，不提供 reactivation、actor/reason 或伪批量循环。

## Phase 36：原子批量 Memory Obsolescence（已实现）

- 添加 `MemoryKind`、冻结 `MemoryObsolescenceRequest` 和 `obsolete_memories()`。
- 从同一入口状态解析显式目标与 Failure Case cascade，全部暂存验证后一次提交。
- CLI 接受严格、受限 JSON，保持请求顺序，重叠 Lesson 不重复计数，默认 dry-run。

## Phase 37：强制 PostgreSQL 与 Windows CI 覆盖（已实现）

- 本地数据库工具缺失继续可跳过；CI 的 `TBM_REQUIRE_POSTGRES=1` 把环境 skip 转换为失败。
- 独立 Ubuntu job 预检并运行真实 PostgreSQL integration/repository 测试。
- 独立 Windows Python 3.13 job 运行完整 pytest，保留 Ubuntu 3.11-3.13 matrix 与 package job。

## Phase 38：延迟 Decision Outcome CLI（已实现）

- 添加 `outcome`，只调用一次 `record_decision_outcome()`，不完成关联 Trace。
- 默认完整 dry-run，显式 `--write` 原子发布；精确 pair replay 为 no-op，冲突为状态错误。
- 只输出 decision ID、前后 outcome/attribution、`changed`、`written`，不泄露敏感字段。

## Phase 39：PostgreSQL 一致快照与生命周期行锁（已实现）

- `load()` 读取前按序对五表获取 `SHARE` 锁，使外部 writer 等待且 reader 仍可并发。
- sync 对所有已有目标使用 `FOR UPDATE`，包括 Failure Case、Lesson 与 Project Policy。
- caller transaction 继续使用 savepoint，锁保持到外层最终 commit/rollback。
- 用真实集群覆盖 load lock、external writer exclusion 与等待期间保护字段变化。

## Phase 40：PostgreSQL 有界 Load 物化（已实现）

- 五表锁后、首次 collection selector 前执行一条五表 `count(*)` 预检。
- 每集合最多 100,000，总计 250,000，保持 Store 既有错误文本。
- 在锁持有期间完成计数验证与 bounded read，异常结果通过净化错误边界。

## Phase 41：运行时集合基数限制（已实现）

- LLM decision 的 allowed/blocked 列表各最多 50，在逐项验证与 set 构造前检查。
- JSON/mapping parser 与直接 gate 调用使用相同限制，Schema 发布 `maxItems: 50`。
- ancestry capture 输入上限为 1,000，在去重前计数，最多消费 1,001 项，overflow 零 Git 子进程。

## Phase 42：PostgreSQL 并发 Insert 重验证（已实现）

- 每个 absent-row INSERT 在 nested savepoint 中执行。
- 捕获同主键 `23505` 与 registry 精确 `P0001` 后重新 `FOR UPDATE`。
- exact replay、forward update、protected conflict 使用与已有行相同的规范规则；目标仍缺失保持 persistence error。
- 真实并发测试覆盖五种持久化记录、trigger、回滚与连接复用。

## Phase 43：严格 JSON 对象键唯一性（已实现）

- 添加 shared ordered-pairs parser，在第二次出现同名键时拒绝。
- Store snapshot、MemoryContext、MemoryDecision 与 CLI 文件解析在所有层级使用同一原语。
- 保持直接 Mapping 和规范 JSON 兼容，并补回归测试证明不采用 last-key-wins。

## Phase 44：有界 Recover-Batch 参数（已实现）

- decision IDs 和 attribution options 各自最多 10,000，在 duplicate detection 前计数。
- argparse 后、snapshot load 与集合构造前执行，overflow 为退出码 2 且不读取/写入快照。
- 边界内继续保持顺序、严格 attribution、dry-run 与 Store atomicity。

## Phase 45：非负 Trace Latency（已实现）

- 把 `latency_ms` 定义为 `None` 或非负整数，覆盖 record、snapshot、execution 与 completion。
- Store 作为范围权威，CLI 负数是退出码 3 的状态错误，畸形输入是退出码 2。
- Trace Schema 增加 `minimum: 0`，fresh-install DDL 增加命名 CHECK；既有数据库由 operator 迁移下界约束。

## Phase 46：公开 Project Policy Obsolescence 导出（已实现）

- 从包根导出现有 `obsolete_project_policy()` 并加入 `__all__`。
- 根导出与 lifecycle 函数对象相同，不增加 wrapper 或第二套转换实现。
- 覆盖 import、行为、输入不可变、源码身份和隔离 wheel 安装。

## Phase 47：PostgreSQL 兼容的 Trace Latency 范围（已实现）

- 把 `latency_ms` 上界设为 2,147,483,647，与 PostgreSQL signed `INTEGER` 一致。
- 在 exact type、JSON serialization、non-negative 检查后应用上界，保持错误优先级。
- 两份 Trace Schema 增加 `maximum`；数据库物理上界已存在，无需 schema-version-1 迁移。

## Phase 48：PostgreSQL 有界 Load Payload（已实现）

- count 预检后、collection fetch 前执行一条 scalar payload query，并继续持有五表锁。
- 把每行转为 PostgreSQL JSON 计算 UTF-8，返回最大单行与合计字节。
- 两个精确 64 MiB 边界有效，超一字节在 psycopg 物化前拒绝。
- 异常/不可能的 scalar 结果保持净化 `PostgresPersistenceError` 与连接复用。

## Phase 49：可移植的持久化非空白字符串（已实现）

- 持久化 identity、linkage、必需 failure 文本、scope、Memory Context 与 audit mapping 键值必须含非空白字符。
- 接受字符串原样保留；可选 Trace metadata 与无关 narrative 保持原契约。
- 六组 canonical/package Schema 增加 `pattern: "\\S"`。
- PostgreSQL `btrim` 只覆盖普通空格，Repository 写入依赖更强 Store 预验证。

## Phase 50：保守失败提取准确性（已实现）

- 只有带 truthy 顶层 error 的 tool call 名称可以标记症状。
- 保留显式 `invalid argument`，把裸 `required` 收紧为四个具体 required marker。
- 保持 missing-context 优先级、taxonomy fallback、证据顺序、症状和 root-cause 语义。

## Phase 51：线性快照 Usage-Log 验证（已实现）

- 用 load-local decision ID set 替代二次 duplicate scan，同时保留 per-log 验证优先级。
- 复用 known memory、legacy run ID、tool-name 索引和 per-log relationship sets。
- 在 records 与 nested evidence 上保持平均 O(n)，不依赖 wall-clock threshold。

## Phase 52：索引化 Usage-Log 操作（已实现）

- 维护私有 `decision_id -> list position` 索引和下一个 numeric suffix。
- snapshot import、finalization、direct logging 共享 append helper；outcome/completion/recovery 原位替换。
- allocation、duplicate、single lookup 平均 O(1)，batch lookup 平均 O(k)，失败写入不耗号。
- 索引不持久化，canonical snapshot sorting 保持不变。

## Phase 53：索引化 Run-to-Trace 查找（已实现）

- 通过唯一 `record_trace()` 边界维护 `run_id -> ordered trace_id` 私有索引。
- Trace 与索引项在同一 Store 锁下提交，索引失败回滚 Trace insertion。
- 平均 O(1) 区分 missing、unique、ambiguous；duplicate run 合法但不会任选记录。
- snapshot reconstruction 重建索引，completion replacement 仍解析当前 Trace。

## Phase 54：按引用验证实时 Memory ID（已实现）

- 实时 usage-log 只检查其 distinct referenced IDs 对三个权威 Store map 的 membership。
- 平均 O(r)，不复制完整 memory catalog；snapshot 继续复用一个 known-memory set。
- 不增加新派生索引，并保持 unknown-ID 排序和验证顺序。

## Phase 55：单次扫描 Store 指标（已实现）

- `metrics()` 在一次 usage-log 扫描与 O(1) 累加空间中统计所有 derived field。
- 用 pass/total counter 替代 cohort list，保持 empty `None` 与 nonempty zero-pass `0.0`。
- Lesson confidence、per-memory metrics、run ordering 与 CLI API 边界不变。

## Phase 56：单次扫描 Memory Run 指标（已实现）

- `memory_run_metrics()` 一次扫描、无排序、O(1) 累加。
- 单条 log-to-audit constructor 统一 Trace lookup、状态与 remediation classification。
- audits/remediations 仍按 decision ID 排序，指标只是无序时间点汇总。

## Phase 57：串行化快照 CLI 写入（已实现）

- 每个显式 `--write` 在 snapshot load 前获取 canonical sibling `.tbm.lock` 排他建议锁。
- 锁覆盖完整 read-modify-write、成功序列化和原子发布，在 stdout 前释放。
- POSIX 使用 `flock`，Windows 使用单字节锁区；persistent sidecar 只含 placeholder。
- 争用最多 30 秒，超时退出码 4；dry-run/read-only/export 不取锁。

## Phase 58：Active-Only Lesson 导入（已实现）

- 在 `load_lessons_yaml()` 的一般 Lesson/provenance 验证后强制 active-only artifact domain。
- `status: obsolete` 在 staged insertion 前拒绝，混合文档仍 all-or-nothing。
- CLI 映射退出码 2 且不写快照；full snapshot/PostgreSQL 仍保留 obsolete 历史。

## Phase 59：有界 PR Change Set（已实现）

- `PRChangeSet` 上限由六个支持且唯一的字段确定，最多 6 项。
- 第 7 项在 entry shape、endpoint 或 case scanning 前拒绝，CLI 不捕获 ancestry。
- 边界内用一次 set pass 收集 unsupported/duplicate，并保留错误优先级。

## Phase 60：线性 Legacy PR Warning（已实现）

- 在 ancestry/case scan 前一次验证宽泛 `changed_fields`。
- 只保留最多 7 个支持名称的首次出现，duplicate/unknown 仍兼容但不能放大 case work。
- set-backed stable dedupe 把期望复杂度降为 `O(W + C)`，文本和顺序不变。

## Phase 61：有界 Git 捕获（已实现）

- 默认 metadata/ancestry Git 命令使用 `stdin=DEVNULL`、binary pipe、UTF-8 replacement 和 30 秒 timeout。
- stdout/stderr 各最多保留 64 KiB，timeout/overflow kill/reap。
- `git status --porcelain` 只保留首字节判断 dirty，同时 drain 其余输出。
- 注入 runner signature、命令、`GIT_NO_LAZY_FETCH=1` 与 ancestry 0/1 语义不变。

## Phase 62：持久原子发布（已实现）

- 保持 LF serialization、sibling temp、file flush/`fsync()` 和 replace/link publication。
- 成功发布并清理临时名后，POSIX 打开并 `fsync()` 父目录；non-POSIX 保留可移植行为。
- 所有目录 descriptor 必须关闭；发布前失败保留旧目标，发布后目录 sync 失败视为持久性不确定。

## Phase 63：有界 Semantic Top-K（已实现）

- metadata-only、keyword 和 option-error 路径不构造 semantic catalog。
- 通过 non-copying membership view 验证 score ID，经过 metadata/ancestry filter 后使用 heap top-k。
- 保持完整 mapping validation、inclusive minimum、score 降序与 ID 升序 tie-break。
- 排名复杂度降为 `O(K log k)` 时间和 `O(k)` 存储。

## Phase 64：公开快照写锁（已实现）

- 把 CLI 跨平台锁 backend 抽为无依赖模块并从包根导出 `snapshot_write_lock()`。
- Python 调用方用同一 `.tbm.lock` 协调完整 load、mutate、save 事务。
- `timeout_seconds` 必须有限且非负，在文件系统访问前验证；锁为建议性、非重入。
- CLI wrapper、30 秒默认、错误映射与 dry-run/read-only 行为不变。

## Phase 65：有界运行时 Trace JSON（已实现）

- 每个 Trace 候选的三个 JSON 字段共享 100,000 nodes 与 8 MiB key/string UTF-8 text，深度 100。
- 宽容器在 traversal stack/`dict.items()` 前拒绝，lone surrogate 在复制前拒绝。
- direct record、completion、snapshot import、PostgreSQL load 使用同一不可配置边界且失败原子。

## Phase 66：PostgreSQL Loaded-Row Payload（已实现）

- 保持锁后、count 后、fetch 前的 scalar payload query 与两个 64 MiB 边界。
- 计量实际 loader projection，而不是完整物理行。
- 只从 Failure Case、Lesson、Project Policy 排除未读取 `updated_at`；Trace 与 usage 保留所有列。
- SQL 操作固定 `pg_catalog`/`public`，错误净化与连接复用不变。

## Phase 67：快照锁 Sidecar 安全（已实现）

- `.tbm.lock` 在 placeholder 与 OS lock 前必须是单链接普通文件。
- 缺失时 exclusive create；已存在时使用 no-follow metadata 与 pre-open/descriptor/post-open identity validation。
- OS lock 后、yield 前再次验证 descriptor/path identity。
- 符号链接、Windows reparse point、硬链接、特殊文件在不修改目标或加载快照的情况下拒绝。

## Phase 68：Git Metadata 输出验证（已实现）

- 注入的四个 metadata 命令都必须返回字符串，非字符串包装为命令特定 `TraceMetadataCaptureError`。
- 空 commit SHA、空 repository root 在启动下一命令前拒绝，不回显值。
- 在捕获边界强制 512 字符 metadata 上限；空 branch/status 继续表示 detached HEAD/clean。

## Phase 69：显式失败文本分类（已实现）

- 只从 `Trace.error` 和 tool call/output 顶层 `error` 分类。
- 工具名称无论是否伴随 error 都不能选择 taxonomy；带 error 的名称仍可作为确定性 symptom label。
- 保持 taxonomy ID、keyword precedence、root-cause priority、evaluator fallback 与持久化契约。

## Phase 70：Recover Attribution 最终分隔符（已实现）

- `recover-batch --attribution DECISION_ID=true|false` 使用最后一个 `=` 分隔。
- 完整非空前缀原样作为 decision ID，包括更早的 `=`，不 trim 或 normalize。
- 后缀仍只接受小写 `true`/`false`；畸形、未请求和重复项保持退出码 2。
- 请求顺序、Store 原子性、API、快照、Schema 与 18 项包资源不变。

## Phase 71：Review 驱动的可信提升与有界 LLM Decision（已实现）

- Failure Case 只能引用 `fail` 或 `error` Trace；verify 前必须具备 reviewer、root cause 与 review timestamp；dirty source Trace 不能激活 Lesson。
- Store、JSON Schema 与 fresh-install PostgreSQL DDL 使用同一提升约束。既有 schema-version-1 数据库需要 operator migration，旧 version-2 snapshot 可能需要先补齐 review 证据。
- LLM decision response 在进入持久审计前最多为 65,536 UTF-8 bytes、1,000 个 JSON nodes、depth 20，reason 最多 2,000 字符。
- LLM narrowing 遗漏的所有 system-approved candidate 都计入 blocked；确定性保留前 50 个允许项，并用稳定 System Gate 原因审计 overflow。
- `short_summary` 与 `full_case_summary` 使用不同 renderer；Store-owned full summary 包含经过 review 的 failure/fix provenance；关键词过滤支持 Unicode。
- snapshot version 2、PostgreSQL schema version 1、公开 lifecycle signatures 与 18 个 packaged resource 路径保持不变。

## Phase 72：SQLite Repository 选择（已实现）

- 在可选 `PostgresMemoryRepository` 之外增加标准库 `SQLiteMemoryRepository`，保持一致的增量 `sync()` 与完整校验 `load()` 生命周期语义。
- 发布 SQLite schema 版本 1 的规范 `schemas/sqlite.sql` 与字节一致包内副本，把 packaged resource 白名单从 18 项增加到 19 项。
- 顶层写入使用 `BEGIN IMMEDIATE`，caller-owned transaction 内使用 nested savepoint，并保持 borrowed/owned connection 边界。
- 支持精确重放、Store 允许的前向转换、Failure Case 到 Lesson 的 obsolete 级联，以及冲突时全有或全无回滚。
- 返回完整验证 Store 前执行每集合/总记录数限制，以及最大单条/累计 64 MiB UTF-8 payload 限制；重建时拒绝不受支持的直接 SQL payload 修改。
- 双语 README、架构、产品和使用策略文档将 SQLite 与 PostgreSQL 分别说明为嵌入式和服务端 SQL 选择。
- snapshot version 2 与 PostgreSQL schema version 1 保持不变；SQLite 使用独立 schema version 1。

## Phase 73：Review 驱动的运行时与持久化加固（已实现）

- 限制 query 文本、semantic score mapping、批量操作、进程内 pending request/finalized tombstone、单个 request 候选与 pending request 聚合候选引用，以及持久化 lesson/policy 文本。
- 增加显式 Gate request 取消；高层请求绑定 Trace/run；usage audit 持久化最终 `request_id`。
- 有界本地摄取拒绝特殊文件；SQLite savepoint 回滚本身失败时中止外层事务，同实例操作串行化，顶层回滚清理保留主异常。
- PostgreSQL 使用不可变与前向 trigger 保护 Trace/usage audit，并保证 Failure Case 与 Lesson 来源不可改绑；锁定 usage Trace context 读取，并将契约推进到 schema version 2。
- 生命周期 API、snapshot、JSON Schema、SQLite 与 PostgreSQL 统一使用最多六位小数的严格 RFC 3339 时间戳契约。
- CI 在隔离质量环境中增加 Ruff、mypy、分支覆盖率和依赖审计门禁。
- 将原子 `schemas/postgres-v1-to-v2.sql` operator migration 作为第 20 项打包资源，覆盖 fresh install、迁移、重放拒绝、wheel、sdist、Windows、SQLite 与真实 PostgreSQL 测试。
- 保持 snapshot version 2 与 SQLite schema version 1。

## Agent 集成基础（已实现）

- 增加 `LocalAgentMemory`，在现有 Store、Gate、completion、SQLite 与
  PostgreSQL 契约之上提供聚焦本地应用边界。
- 增加 Git-backed pending Trace capture、capability discovery、稳定有界
  Agent 错误、cancel、callback 恢复 ID 与同 runtime 精确 decision 幂等。
- 发布独立版本的 `tbm.agent.v1` capability、prepared、finalized、completed
  和 error Schema，并提供字节一致的打包示例。
- 增加 `tbm capabilities`、根/嵌套 `AGENTS.md`、仓库本地维护/运行技能、
  Codex 集成指南与统一跨平台验证命令。
- pending Gate request 仍为进程内状态并明确报告；在统一 schema version 3
  工作完成前，不宣称 durable MCP/HTTP session。
- 保持 snapshot version 2、SQLite schema version 1 与 PostgreSQL schema
  version 2。

## 本地 STDIO MCP 运行时（已实现）

- 增加可选 `trace-backed-memory[mcp]` 打包与 `tbm-mcp` console entry，同时
  不给核心 runtime 增加第三方依赖。
- 只通过一个长驻 STDIO 进程暴露 capability/health discovery 与
  prepare/finalize/complete/cancel runtime 生命周期；不暴露 curator、
  activation、原始 Store、snapshot 或 migration 操作。
- 在 server 配置中固定 repository provenance 与可选 declared tenant，在检索前
  捕获完整 Git ancestry，并且只从命名环境变量读取 PostgreSQL conninfo。
- SDK dispatch 前把每个输入帧限制为 8 MiB、100,000 个 JSON nodes 与 depth
  100；拒绝 duplicate key、非法 UTF-8、非有限数字、未知 request 字段和错误的
  strict type。
- 保留进程内 pending request 与 replay tombstone，并通过真实 MCP client 验证：
  即使 durable SQLite 记录仍存在，server 重启后也不能恢复尚未 finalized 的
  request。每个 Store runtime 使用新的 128-bit request namespace，防止 stale
  handle 在重启后与新 request 碰撞。
- 同步发布 Codex 项目配置、运行策略、架构、产品、README 与仓库技能指南。
- 保持 snapshot version 2、SQLite schema version 1、PostgreSQL schema
  version 2，以及 50 项资源的发行契约。

## Phase 74：可部署信任边界与可重放审计（进行中）

- 交付只读 `tbm.snapshot.v2-to-v3.mapping.v1` 与
  `tbm.snapshot.v2-to-v3.plan.v1` 预检契约、严格 Python value object、稳定
  issue code、canonical SHA-256 绑定、打包 Schema/示例，以及
  `tbm migration plan-v3`。
- Mapping ready 前必须显式提供 Trace repository/tenant binding、memory
  authorization scope、结构化 regression evidence、global policy privileged
  approval，以及 `required` 或带审计 reason 的 `disabled` ancestry policy。
- `required` ancestry 必须使用可信应用 verifier（CLI 中为显式映射的本地 Git
  对象库）；拒绝无 version 的 legacy snapshot，在 hash 前归一化语义无序的
  mapping 字段，并报告每次 disabled ancestry bypass。
- 预检保持只读期间，继续保留当前 snapshot/SQLite/PostgreSQL/agent 版本；
  不生成无法运行的不完整 version-3 snapshot。
- 交付不可激活的 `tbm.snapshot.v2-to-v3.bundle.v1`，包含原始与 normalized
  source hash、严格 plan replay、有界 duplicate-rejecting JSON 和内容派生 ID。
- 增加 immutable、side-by-side SQLite staging repository，以及带版本门禁的
  PostgreSQL staging/rollback scripts；所有 staging 对 runtime v2 adapter
  不可见，且不提供 activation operation。
- 发布不可变的 `tbm.gate-session.v3` 领域契约、显式生命周期转换图、乐观
  revision 检查、lease/expiry 不变量、有界严格 JSON parser，以及打包
  Schema/示例。领域契约保持 persistence-neutral；opt-in repository 不会自动
  成为 active runtime authority。
- 发布与存储实现无关的 `tbm.replay.v3` 内容寻址 artifact、injection 与固定
  component decision manifest 契约，包含 canonical self-hash、有界严格 JSON、
  打包 Schema/示例，以及明确的 `complete`/`legacy_partial` 语义。active v2
  adapter 不得宣称已持久化 artifact 或支持精确 decision replay。
- 增加 opt-in 隔离 SQLite replay ledger，保存精确 artifact 字节、injection
  descriptor 与 decision manifest，提供原子 bundle 写入、immutable row、精确
  idempotency/conflict、foreign-key linkage、canonical schema drift 检查、有界
  defensive load、字节 rehash、调用方 savepoint 与并发 replay 测试。access
  control、encryption、retention、GateSession linkage 和 active integration
  仍待完成。
- 增加带版本门禁的隔离 PostgreSQL replay-ledger install 与 fail-closed rollback
  资源：提供有界精确字节、不可变 injection/manifest descriptor、关系链接、固定
  `search_path` mutation guard、active metadata 锁与精确 catalog 校验。PostgreSQL
  repository adapter 现提供精确 byte/descriptor 复验、精确 idempotency/conflict、
  caller savepoint、schema/function/trigger drift 检查、有界 load 与并发 conformance。
  authorization/encryption/retention、GateSession 事务链接和 active integration
  仍待完成。
- 发布 `tbm.regression-evidence.v3` storage-neutral、content-addressed 验证记录，
  要求不同的 submitter/verifier principal、精确 source/fix/verification commit
  关系、evaluator/environment provenance、expected/observed outcome、artifact hash
  与 attestation。它不替代 active v2 boolean，也不授予发布权限；immutable
  MemoryRevision 与 service integration 仍是后续工作。
- 发布 `tbm.fix-evidence.v3` storage-neutral、content-addressed 记录，绑定精确的
  case/Trace、source/fix commit、已验证 ancestry、有限 artifact hash 与相互独立的
  submitter/reviewer principal。增加严格 MemoryRevision evidence-bundle preflight，
  要求 fix/regression evidence 具有相同 case、source Trace 与 commit。持久化、
  approval 和 activation 仍是后续 service 工作。
- 发布 proposal-only `tbm.memory-revision.v3` storage-neutral、内容派生 immutable
  revision，绑定精确 parent、content artifact、canonical scope、case/fix/structured
  evidence 引用与 server-owned proposer/client attestation context。在认证
  authorization 与 audit service operation 交付前，不把 approval/activation 放入
  该契约。
- 增加 opt-in、隔离的 SQLite immutable MemoryRevision proposal ledger：原子保存
  精确 FixEvidence/regression bundle，强制线性 parent/revision continuity，支持
  精确幂等 replay 与 caller savepoint，并在 commit 前读回。approval、activation、
  active v2 projection、authorization 与 retention 保持在该 ledger 之外。
- 增加隔离的 PostgreSQL 对等实现及 install/fail-closed rollback：校验精确 catalog
  fingerprint，兼容 caller transaction，保存 immutable evidence 闭包，强制线性
  parent continuity，并在 replay 时拒绝而不是修补被篡改的 proposal。它仍只保存
  proposal，不接入 active v2 projection、approval、activation 或 authorization。
- 发布内容寻址 `tbm.retrieval-snapshot.v3` 以及嵌套 RetrievalHit/IndexVersion，
  绑定精确 authorization/context/query、有序 revision 命中、候选哈希、有限的
  stage/fusion 分数、retriever/index 版本、边界与截断原因。在 UTF-8 编码前拒绝
  超大字符串，并在产生可避免的分配前校验 direct-parser object shape 与集合基数。
  System/Semantic Gate 结果保持独立，active Store/GateSession 接入仍是后续工作。
- 发布内容寻址 System Gate evaluation 与 Semantic Gate attempt 契约，绑定精确
  retrieval/policy/provider/model/prompt/response provenance、success/failure
  shape、有序 retry parent 与有界 metrics；跨记录核验保证 semantic decision
  只能缩小确定性 System Gate 结果。增加严格有界的完整 chain verifier，并在产生
  可避免的分配前拒绝超大 direct-parser 输入。artifact 校验、durable adapter
  对等实现与 active runtime 接入仍是后续工作。
- 增加 opt-in、side-by-side SQLite SemanticGateAttempt ledger：依赖 immutable
  Gate evidence，通过唯一 sequence 与 CAS head 为每个 System Gate evaluation
  强制一条有界线性 chain，支持精确幂等重放，通过 savepoint 保留调用方
  transaction，检测 canonical schema drift，并在每次读取时复核完整 chain。
  GateSession 事务挂接与 active Agent/MCP emission 仍待完成；精确字节由下述
  独立 opt-in repository 提供。
- 增加隔离 PostgreSQL SemanticGateAttempt 对等实现：active-v2 与 Gate evidence
  install 门禁、parent-before-head lock、单一 row-locked CAS head、deferred
  commit-time chain consistency、精确 descriptor/完整 chain 读回、完整安全 catalog
  fingerprint、调用方 savepoint、并发精确 replay/fork conformance，以及
  fail-closed `RESTRICT` rollback。精确字节由下述独立 opt-in PostgreSQL
  repository 提供；provider 认证、GateSession/replay 事务挂接与 active adapter
  emission 仍待完成。
- 发布存储中立的 `tbm.semantic-gate-artifact.v3` 绑定：把精确非空
  prompt/response 字节连接到一条 SemanticGateAttempt 的对应角色与 digest，保留
  classification/encryption/redaction 元数据，执行 prompt/response 字节上限，拒绝
  为失败 attempt 绑定 response，并提供有界、拒绝重复键的 JSON 以及规范
  Schema/example 资源。provider 认证、可信时间戳、GateSession/replay 事务与
  active emission 仍待完成。
- 增加独立 version-1 SQLite Semantic Gate artifact schema 与
  `SQLiteSemanticGateArtifactV3Repository`。一个外层 transaction/savepoint
  原子追加 attempt、精确 public/internal prompt/response 字节与角色 binding；
  精确 replay 会去重并完整读回。SQL 会重算字节 digest 与派生 ID、比较每个
  descriptor 字段、执行角色/status/长度/媒体约束、即使关闭 recursive trigger
  也阻止 replacement write，并拒绝意外受管对象。加密敏感存储、provider
  trust、GateSession/replay linkage 与 active emission 仍待完成。
- 增加独立 version-1 PostgreSQL Semantic Gate artifact schema 与
  `PostgresSemanticGateArtifactV3Repository`。一个外层 transaction/savepoint
  原子追加 attempt、精确 public/internal prompt/response 字节与角色 binding。
  数据库 trigger 会重算 SHA-256 与派生 ID、比较 descriptor 字段、执行角色/
  status/长度/媒体约束并阻止 mutation。operation 会验证完整 security catalog、
  保留调用方 transaction、支持并发精确 replay，并提供带固定指纹、fail-closed
  的 `RESTRICT` rollback。加密敏感存储、provider 认证/可信时间、
  GateSession/replay linkage 与 active emission 仍待完成。
- 发布内容寻址 `tbm.run-outcome.v3` 与 `tbm.outcome-attribution.v3`
  契约，把 completed GateSession 绑定到显式 evaluator evidence，并严格区分
  观察关联与独立核验的因果结论。
- 增加 `GateSessionCompletionService`、隔离 version-1 SQLite RunOutcome
  schema 与 `SQLiteOutcomeV3Repository`。同一个可信 timestamp 与外层
  transaction/savepoint 会构造 content-addressed outcome、通过 CAS 追加
  `EXECUTING` → `COMPLETED` session revision、插入 immutable outcome，并精确
  读回两条记录。完全相同的 terminal replay 幂等；不同 measurement、schema
  drift、时钟倒退或 partial write 均 fail closed。
- 增加 PostgreSQL RunOutcome parity：提供隔离 version-1 install 与 fail-closed
  exact-catalog rollback，在取得 GateSession head lock 后读取数据库时间，通过
  CAS 完成 session、插入 immutable outcome，并支持精确重放/读回、caller
  savepoint 与并发单写。authenticated evaluator/artifact 检查与 active
  Agent/MCP/HTTP/SDK 集成仍待完成。
- 增加 opt-in 隔离 SQLite 与 PostgreSQL OutcomeAttribution ledger：使用各自独立的
  version-1 schema，提供精确 content-ID replay、immutable 多 claim 存储、
  completed outcome/session/usage/revision linkage、canonical descriptor 复验、
  replacement-write guard、schema/catalog drift 检查、caller savepoint 与并发幂等。
  PostgreSQL 另提供数据库侧校验、row lock 与 fail-closed rollback。authenticated
  evaluator/verifier 派生、trusted-time 构造、artifact authorization、
  attribution outbox delivery 与 active runtime integration 仍待完成。
- 发布 storage-neutral `tbm.completion-outbox-event.v3` 与
  `tbm.completion-outbox-delivery.v3` 契约，并增加 opt-in 隔离 SQLite 与
  PostgreSQL authority：原子完成 GateSession、插入 RunOutcome 与 immutable
  event，并创建初始 append-only delivery revision/head。提供有界 claim、过期
  lease reclaim、精确 version acknowledgement、retry/dead-letter transition、
  canonical 读回、schema/catalog-drift 检查、caller savepoint、并发单 claim、
  PostgreSQL database-time/row-lock/CAS 对等能力与 fail-closed rollback，以及
  明确的 at-least-once consumer 语义。增加 storage-neutral 有界 delivery
  worker：callback 前校验完整 claim page，只持久化清洗后的 consumer error code，
  核验精确 transition/read-back receipt，执行配置的 retry/dead-letter 限制，并
  报告 superseded 或 recovery-required 写入不确定性。具体 network transport、
  authenticated evaluator/artifact 校验及 active Agent/MCP/HTTP/SDK integration
  仍待完成。
- 发布 storage-neutral `tbm.audit-event.v3` 与
  `tbm.recovery-action.v3` 契约，提供内容派生 identity、精确 stream parent、
  authenticated actor slot、typed reference、显式 request digest，以及对照
  GateSession 与派生 MemoryRunRemediation state 的跨记录核验。
- 增加 opt-in 隔离 SQLite audit ledger，提供 immutable stream event、精确
  parent/head CAS、RecoveryAction/event 原子追加、session-scoped request-digest
  唯一性、canonical 读取复核、schema-drift 检测、调用方 savepoint 与并发幂等。
  authenticated actor 派生、底层 GateSession/remediation transition、
  active service integration 仍是后续工作。
- 增加匹配的 opt-in PostgreSQL audit ledger：version-gated 隔离安装、
  fail-closed 精确 catalog rollback、确定性 collation、row-lock stream CAS、
  deferred stream 与 RecoveryAction/event consistency、canonical 读取复核、
  调用方 savepoint、并发幂等及 catalog/function-body drift 检查。active
  PostgreSQL schema version 2、authenticated actor 派生与更宽的 service
  transaction 保持不变。
- 增加 opt-in、side-by-side SQLite GateSession repository，提供 append-only
  canonical revision、scoped 原子 idempotency index、可信时钟 CAS transition 与
  lease renewal、schema-drift 检测、调用方 savepoint、并发测试和有界 due
  discovery。active SQLite schema version 1 与进程内 Agent/MCP request token
  保持不变。
- 在隔离 schema 中增加对应的 opt-in PostgreSQL GateSession repository，提供带
  版本门禁的 install/fail-closed rollback、确定性 identity collation、行锁后
  database time、append-only trigger、exact-version CAS、catalog drift 检查、
  调用方 savepoint 与并发 idempotency 测试。保持 active PostgreSQL schema
  version 2 与 Agent/MCP lifecycle 不变。
- 发布与存储实现无关的授权 v3 policy 与 decision 契约，包含 canonical
  repository/tenant binding、精确 alias、principal/client registry、显式
  global/tenant/repository role binding、时点评估、精确 policy/request 核验、
  有界严格 JSON 和打包 Schema/示例。认证 identity context、持久化与 active
  adapter enforcement 仍待完成。
- 增加 opt-in 隔离 SQLite authorization authority：持久化 immutable policy
  bundle 与关联 allow/deny decision，在追加前核验精确 request，强制唯一 request
  identity，重验已存 descriptor，检测精确 schema drift，并保留调用方
  savepoint。调用方认证及 active Store/Agent/MCP 集成仍待实现。
- 增加对应的隔离 PostgreSQL authorization authority：提供受 active-v2 门禁的
  原子 install、精确 catalog 的 fail-closed rollback、immutable trigger、并发精确
  重放幂等、descriptor 重验与调用方 savepoint 保留。认证服务集成仍待实现。
- 发布与存储实现无关、内容寻址的 `tbm.entity-registry.v3` 快照：新增
  Organization、正式 Tenant 与 Environment identity，同时复用 authorization-v3
  的 Principal、AgentClient、canonical Repository、alias 与 role-binding 记录；
  强制 organization/tenant 引用闭包及 environment/repository 同租户关联。
  规范化持久化和 authenticated service enforcement 仍待实现。
- 增加 opt-in 隔离 SQLite 规范化 entity-registry authority：通过复合外键保存
  snapshot 的全部 entity、binding、permission 与 attribute；以 canonical JSON
  作为完整性见证；读取时复验全部行；强制不可变行、精确重放、schema drift 检查与
  调用方 savepoint。PostgreSQL 对等实现和 active service integration 仍待完成。
- 增加 PostgreSQL 规范化 entity registry 对等实现：提供 active-v2 安装门禁、
  不可变 DML/TRUNCATE guard、完整 catalog/ACL fingerprint、并发精确重放、调用方
  savepoint 与 fail-closed 精确 catalog rollback。active service integration
  仍待完成。
- 增加与存储无关的 `AuthenticatedRetrievalService` kernel：只接受可信
  Principal/AgentClient record 与服务端 target context；求值并持久化精确 allow/deny
  decision，读回验证，复查完整 registry 轮换与 environment binding，并且只在全部
  检查通过后调用 retrieval。增加可选启用的 `AuthenticatedLocalAgentMemory` 门面，
  在注册 Trace 前完成授权，并绑定服务端 canonical tenant/repository identity。
  增加可选本地 `tbm-mcp --auth-*` profile：使用可信有界 registry、SQLite
  authorization authority、服务端选择的 identity、无请求 identity 字段及门面所有的
  lifecycle handle。transport authentication 与普通 CLI/HTTP/SDK integration 仍待完成。
- 通过 `AuthenticatedGateSessionService` 把授权与 SQLite/PostgreSQL GateSession
  authority 组合：preparation 前 durable create/read-back scoped session；阻止
  idempotent duplicate retrieval；要求可信 retrieval/System-Gate evidence 验证；
  通过 CAS 发布 `PREPARED`；失败时使用带 version 检查的 cancellation 或显式
  recovery-required state 补偿。后续 lifecycle phase 与 active adapter integration
  仍待完成。
- 在 SQLite/PostgreSQL due discovery 上增加与存储无关的有界
  `GateSessionRecoveryWorker`：预先验证完整 page；只通过精确 CAS/read-back
  expire session 已到期的 prepared/awaiting head；lease-only 与 graph-blocked
  state 进入显式 recovery；并发 revision 标记 superseded，不盲目重试。
- 增加 opt-in、immutable SQLite RetrievalSnapshot/SystemGateEvaluation
  authority 与 storage-neutral durable evidence verifier：原子保存每个精确记录对，
  通过 recursive trigger 阻止 replacement-delete 绕过，并把 PREPARED evidence
  绑定到已授权 session、Trace、run 与 identity scope。
- 增加 immutable RetrievalSnapshot/SystemGateEvaluation evidence 的 PostgreSQL
  对等实现：提供 active-v2 安装门禁、精确 descriptor 读回、并发幂等重放、完整安全
  catalog fingerprint、不可变 DML/TRUNCATE guard、调用方 savepoint 与 fail-closed
  `RESTRICT` rollback。active adapter emission 仍待完成。

- 用结构化 Trace/run/evaluator 证据替代 regression boolean，并验证 source/fix/regression commit 关系。
- 把 transport-authenticated 服务端 identity 与已发布的 retrieval 前授权 kernel
  接入 shared-service MCP 与 active CLI/HTTP/SDK adapter，使 scope 成为可执行的
  transport boundary。
- 持久化 Gate request 或使用 signed envelope，支持 idempotency、expiry、cancel、capacity control 与 crash recovery。
- 集成已发布的 retriever/index snapshot，并记录可重放 decision 所需的 gate model/prompt、ancestry、policy、renderer、response 与 snippet version/hash。
- 用显式 `required`/`disabled` policy 替代可选 ancestry，并审计 bypass reason。
- 以上 breaking contracts 统一随 snapshot schema version 3 与 PostgreSQL schema version 3 发布，并提供迁移文档。
