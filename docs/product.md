# Trace-backed Memory 产品文档

[English](product.en.md) | **简体中文**

- 当前版本：`0.1.0`（Alpha）
- 交付形态：Python 库 + `tbm` CLI + JSON/YAML/JSON Schema + SQLite + 可选 PostgreSQL Repository
- 运行要求：Python 3.11+（标准库 SQLite）；PostgreSQL 能力要求 PostgreSQL 12+
- 开源协议：MIT

## 1. 产品定位

Trace-backed Memory 是面向 LLM/Agent harness 工程的、以执行证据为基础的记忆层。它把 agent trace、评估结果和 Git 提交历史转化为经过验证、具有适用范围、可审计的工程记忆，并在 debug、repair、regression、planning、eval 和 production 场景中选择性使用。

它不是通用聊天记忆，也不负责用户画像。产品关注的是一个更窄但更关键的问题：

> 如何让 agent 从真实失败中复用已经验证的经验，同时避免错误记忆、过期记忆、跨项目记忆和评测答案泄漏进入当前运行？

核心链路如下：

```text
Trace -> Failure Case -> Human/Eval Verification -> Verified Lesson
      -> Metadata/Ancestry Retrieval -> System Gate -> LLM Gate
      -> Bounded Injection -> Usage Audit -> Measured Outcome
```

## 2. 目标用户

| 用户 | 主要诉求 | 产品提供的能力 |
|---|---|---|
| Agent/Harness 工程师 | 让运行失败形成可复用经验 | Trace、失败案例、验证 lesson、运行时门控 |
| Eval/质量团队 | 防止历史答案污染评测并量化记忆效果 | benchmark identity、阻断原因、outcome metrics |
| 平台工程师 | 在多仓库、多租户环境接入记忆 | declared-scope 适用性、固定预算、审计日志与明确的部署边界 |
| PR/CI 维护者 | 在变更时找到相关历史故障 | Git ancestry、endpoint-aware PR report、回归建议 |
| 运行维护人员 | 发现并恢复中断或半完成的记忆运行 | audit、remediation plan、原子单项/批量恢复 |

## 3. 核心价值

### 3.1 证据优先，而不是模型自述

Trace 保存 commit、repo、branch、prompt/tool/model/eval provenance、输入/输出哈希、工具调用、结果、延迟、成本和错误。原始 Trace 是证据，不会默认进入运行时 prompt。

### 3.2 经验必须先验证再生效

失败 Trace 先形成结构化 Failure Case，经人工复核、修复提交和回归通过后，才能生成 active Lesson。LLM 无权自行把草稿或猜测升级为可用记忆。

### 3.3 两层门控，确定性安全规则优先

System Gate 先检查来源、状态、scope、tenant、敏感性、评测泄漏和运行模式；LLM Gate 只能在系统允许的集合中继续缩小，不能重新放行被阻断的记忆。

### 3.4 每次使用都可解释、可恢复

每个 decision 记录候选、允许、阻断、原因、风险、注入模式和最终 outcome。Trace 与 decision 的完成支持原子提交、审计、修复计划和并发安全的恢复扫描。

## 4. 已实现能力

| 能力域 | 当前能力 |
|---|---|
| Trace 采集 | Git metadata、prompt/tool/model/eval provenance、执行证据、延迟/成本/错误 |
| 失败学习 | 六类具体 failure taxonomy（另有 `unknown` fallback）、草稿提取、人工 review、回归验证、obsolete 生命周期 |
| 记忆类型 | Verified Lesson、Verified Failure Case、Project Policy |
| 检索 | metadata-first、关键词、调用方提供的 semantic score、可选 Git ancestry 过滤 |
| 安全门控 | System Gate + LLM applicability Gate；响应限制为 64 KiB、1,000 nodes、depth 20、reason 2,000 字符；decision ID 列表各限 50 项；blocked 审计完整覆盖未选择项 |
| 注入 | `none`、`pointer_only`、`short_summary`、`full_case_summary`；固定数量与字符预算 |
| 运行闭环 | 两阶段 prepare/finalize、单项/批量原子完成、延迟 outcome sealing |
| 运行编排 | `run_memory_execution()` 同步串联 decision callback、execution callback 与原子完成；`MemoryRunMeasurement` 无需调用方复制 decision ID |
| Agent 应用边界 | `LocalAgentMemory`、Git-backed Trace capture、稳定错误、`tbm capabilities`、带版本的 `tbm.agent.v1` Schema/示例，以及可选长驻本地 STDIO MCP |
| 运维修复 | 五态 audit、remediation action、单项/批量恢复、ready recovery sweep |
| 运维 CLI | dependency-free `tbm` / `python -m trace_backed_memory`；snapshot validate/stats、v3 migration preflight/bundle verification、active lessons 原子导出与 dry-run 导入、failure case/lesson/project policy forward-only 淘汰预览与显式写入、audit/metrics/remediation、只读 PR report、单项与清单式批量 measured completion、dry-run 恢复与显式 `--write` 原子替换 |
| 迁移准备 | content-addressed、不可激活的 v2→v3 bundle、精确 plan replay、immutable SQLite staging，以及不改变 active runtime version 的 PostgreSQL version-gated staging/rollback |
| GateSession 持久化准备 | opt-in side-by-side SQLite 与隔离 PostgreSQL append-only revision、scoped idempotency、CAS transition、可信时钟、有界 due discovery 和 fail-closed PostgreSQL rollback；active SQLite v1/PostgreSQL v2 不变 |
| 重放持久化准备 | 与存储实现无关的内容寻址 artifact、精确 injection 与固定 component decision-manifest v3 契约，以及 opt-in 隔离 SQLite immutable 字节/descriptor 账本；active adapter 尚不使用它 |
| 授权契约准备 | 与存储实现无关的 canonical repository、精确 alias、principal/client、role binding 与关联 decision v3 契约；active adapter 尚不执行它 |
| 分发资源 | wheel/sdist/editable 内置 55 份 byte-identical Schema、SQL/迁移、taxonomy 与示例；支持发现、读取、校验元数据和原子导出 |
| 证据摄取 | Trace、tool call 与顶层 `tool_outputs.error` 按顺序参与失败提取；成功输出不触发分类；bounded local document ingestion 对本地 JSON/YAML 先限额再校验，并以 all-or-nothing 方式导入 |
| 质量度量 | with/without-memory pass rate、错误记忆计数、per-memory observed outcomes、run health |
| PR/CI | 相关历史失败、source/fix provenance、回归建议、old/new endpoint 匹配，以及可直接接入流水线的 `pr-report` JSON 输出 |
| 持久化 | 原子 JSON snapshot / active lesson YAML；标准库 SQLite Repository；可选同步 PostgreSQL Repository；两种 SQL 选择都执行有界、完整验证的 Store 重建 |

所有 caller-owned JSON 都在转换为普通 mapping 前执行对象键唯一性检查：`TraceBackedMemoryStore.load_json()`、`parse_memory_context()`、`parse_memory_decision()` 和 CLI JSON 文件解析会在任意嵌套层拒绝 duplicate object keys，不采用 last-key-wins。有效 JSON、直接 Mapping 输入、snapshot version 2 与 PostgreSQL schema version 2 保持兼容。

## 5. 关键产品流程

### 5.1 安全运行时记忆

1. Harness 以 `eval_result="unknown"` 注册当前 Trace。
2. `prepare_memory()` 按 metadata、可选 query/semantic score 和 ancestry 找候选，并执行 System Gate。
3. 外部 LLM 返回结构化 applicability decision。
4. `finalize_memory()` 重新检查状态、收窄 decision、生成受限 snippet，并记录关联 Trace 的 usage audit。
5. Harness 执行并评估任务。
6. `complete_memory_run()` 或 `complete_memory_runs()` 原子写入 Trace 与 decision outcome；本地 snapshot 运维也可用 `tbm complete` 或 `tbm complete-batch` 提交显式实测结果。

普通同步调用方可以用 `run_memory_execution()` 把第 2-6 步收敛为一次调用；LLM 与 harness 仍由调用方 callback 提供，Store 继续拥有门控、linkage 和原子完成。不需要直接管理底层 Store 生命周期的应用可以使用 `LocalAgentMemory`，由它同时负责 Trace 注册、Repository 同步、稳定错误与 callback 恢复 ID。可选 `tbm-mcp` 命令只通过有界本地 STDIO 暴露这套 runtime 生命周期，把 provenance 固定到配置的 checkout root，并在检索前捕获完整 Git ancestry。SQLite/PostgreSQL 同步持久阶段；pending request 仍为进程内状态。与持久化实现无关的 `tbm.gate-session.v3` 契约已经定义目标 lifecycle、revision、lease 与 expiry 语义，opt-in、side-by-side SQLite 与隔离 PostgreSQL repository 已能持久化其 immutable revision；授权 v3 契约定义未来 retrieval 前的 policy boundary。当前 Agent/MCP 尚未使用这些 repository 或求值器；worker、服务端认证 context 与 service integration 仍未交付。需要暂停、人工重试或独立生命周期控制的高级调用方继续直接使用底层方法。

### 5.2 从失败到可复用 Lesson

1. 从 clean 且结果为 `fail`/`error` 的 Trace 分类并生成 Failure Case 草稿。
2. 人工补充 root cause、reviewer 和 review notes。
3. 绑定修复 commit，并要求 regression 通过。
4. 从 verified case 生成带 scope、confidence 和 source identity 的 Lesson。
5. Lesson 只有在 active、scope 匹配且通过双门控时才可注入。
6. 本地运维可用 `tbm lessons export` 生成 active-only YAML，并用 `tbm lessons import` 在固定限额和完整来源校验下 dry-run 合并；只有显式 `--write` 才发布到 snapshot。
7. 发现过期或错误经验时，用 `tbm obsolete` 预览不可逆状态变化；淘汰 source Failure Case 会在 Store 内原子级联所有 active derived lessons，只有显式 `--write` 才发布。

### 5.3 PR/CI 回归辅助

1. 用 `PRChangeSet` 描述 prompt、tool、model 或 eval 字段的 old/new endpoint。
2. 发现相关历史案例的 commit anchors，并在 store 锁外采集 Git ancestry。
3. 生成相关案例、修复 provenance、建议回归测试和风险警告。
4. 混合 old/new provenance 或缺失 ancestry evidence 时 fail closed。

### 5.4 中断运行恢复

1. `memory_run_audits()` 把每个 decision 分类为 `pending`、`trace_only`、`decision_only`、`complete` 或 `conflict`。
2. `memory_run_remediations()` 映射为 `measure`、`recover`、`recover_with_attribution`、`investigate` 或 `none`。
3. `recover_ready_memory_runs()` 在同一把锁内选择并恢复当前安全的自动项。
4. 失败/错误的 Trace-only 项必须显式提供 causal attribution；冲突永不自动选边。

## 6. 安全与信任模型

产品采用 fail-closed 策略：

- **Provenance chain**：Lesson 必须可追溯到经过 review、verified、regression-backed 的 Failure Case，再追溯到 `fail`/`error` source Trace 和 commit；dirty source Trace 不能激活 Lesson。
- **严格 scope**：memory 声明的每个 scope 字段都必须与当前 context 精确匹配；缺失字段不算匹配。
- **Declared-scope 匹配**：已声明的 `tenant` 和 `repo` 必须精确匹配，但 snapshot version 2 中省略字段仍会扩大适用范围，因此不能把它当作多租户授权边界。
- **评测泄漏防护**：相同 `(eval_suite, input_hash)` 的历史示例自动阻断；sensitive 和 eval-leaking memory 更早阻断。
- **不可逆历史**：身份、来源和已填充的执行证据不可重写；生命周期只允许前向变化。
- **原子写入**：Trace/decision 的单项和批量完成先构建并验证全部候选，再一次提交。
- **固定预算**：每个 prepared request 最多 1,000 个审计候选、所有 pending request 合计最多 100,000 个候选引用、最多 50 个 LLM Gate candidates、20 个 injected memories、32,000 字符 gate prompt、65,536 bytes/1,000 nodes/depth 20 的 LLM response、2,000 字符 reason 和 12,000 字符 snippet；Trace 的 `retrieved_context`、`tool_calls`、`tool_outputs` 合计最多 100,000 个 JSON nodes 与 8 MiB 的 key/string UTF-8 文本，保留 depth 100。
- **可移植时间戳**：持久化 RFC 3339 值必须带显式时区，小数秒最多六位。
- **测量语义**：`latency_ms` 只能为 `None` 或 0 至 2,147,483,647 的 integer，两个边界均有效；越界值由共享 Store validator 在提交前拒绝。CLI 保留 structured `state` error / exit code 3，`cost_usd` 的 finite-number 契约不变。
- **本地文档限额**：snapshot 为 64 MiB、每集合 100,000 条且总计 250,000 条；active lessons 与 CLI JSON 为 8 MiB；failure taxonomy 为 1 MiB。CLI JSON 另限 10,000 个顶层项目、100,000 个 JSON nodes 和 depth 100；`recover-batch` 另限 10,000 decision IDs 与 10,000 attribution options，并在 snapshot 读取前拒绝超限。Python API 可通过 `max_bytes` 等关键字参数显式传入 `None`，仅用于可信离线迁移；CLI 固定安全上限不提供 opt-out。
- **防御性所有权**：store 使用锁与 defensive copies，调用方不能通过返回对象修改内部状态。

## 7. 部署与集成

### Core 模式

- 核心包无第三方运行时依赖。
- 适合嵌入现有 Python harness、eval runner 或 CI 工具。
- JSON snapshot version 2 用于完整 store 的本地持久化。
- YAML adapter 用于 active-only lessons 导入/导出；`load_lessons_yaml()` 拒绝 `status: obsolete` 并保持 all-or-nothing，新导出使用 literal block，并保留空行、首尾换行与行内空格。
- 安装后提供 `tbm` console script；`python -m trace_backed_memory` 提供等价入口。
- 安装 `trace-backed-memory[mcp]` 后增加 `tbm-mcp` 长驻本地 STDIO
  profile，提供 capability/health、prepare/finalize、completion 和
  cancellation 工具，不暴露 curation 或 activation 接口。
- CLI 通过现有 snapshot validation、audit、metrics、remediation、completion 和 recovery API 工作，不复制领域规则；`complete` 不推断 outcome、关联 ID、归因或证据，`complete-batch` 由 Store 从 decision 推导 Trace 并整批提交。
- scalar 与 manifest completion 的越界 `latency_ms` 都交给 Store 统一拒绝，确保 API、snapshot、CLI 与 PostgreSQL 不分叉；错误为 state/exit code 3，`--write` 不会发布部分结果。
- `tbm recover-batch` 在 argparse 后、before snapshot loading 执行基数预检；超过 10,000 decision IDs 或 10,000 attribution options 时返回 structured input exit code 2，不构造 recovery 集合、不读取或写回 snapshot。边界内 `DECISION_ID=true|false` 在 final `=` 切分，完整非空前缀（含更早的 `=`）作为 ID，后缀仍须 exact lowercase boolean；有效输入继续复用 Store 的顺序、归因与 all-or-nothing 规则。
- `tbm lessons export SNAPSHOT DESTINATION [--overwrite]` 复用 `save_lessons_yaml()` 的 canonical active-only serializer，默认以原子 no-replace 发布并拒绝 snapshot 文件别名；`tbm lessons import SNAPSHOT SOURCE_YAML [--write]` 复用 `load_lessons_yaml()` 的 8 MiB/10,000 条受限、active-only status、all-or-nothing 全量暂存与来源校验，`obsolete` 记录按 input error/exit code 2 拒绝且默认不改源文件。
- `tbm obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]` 只输出 ID、前后状态与 case→lesson 级联清单，默认作为 dry-run 预览；转换复用 Store 的 forward-only、幂等和原子级联规则，不回显敏感 memory/Trace 内容，不提供非原子批量循环或 reactivation。
- `tbm obsolete-batch SNAPSHOT REQUESTS_JSON [--write]` 将受限 strict JSON 转为公开的 `MemoryObsolescenceRequest` tuple，并只调用一次 `obsolete_memories()`；Store 从同一入口状态暂存 failure-case、lesson、project-policy 与完整 cascade，全部校验后一次提交，显式结果保持 request order，重叠 lesson 通过 `affected_count` 去重，默认 dry-run。
- 所有 snapshot `--write` 命令在 before snapshot load 获取 canonical sibling `.tbm.lock` exclusive advisory lock，将完整 read-modify-write 串行化到原子发布后，并在 before stdout 释放；持久 sidecar 初始化一个 placeholder byte，不含领域或进程数据，异常与进程退出由 OS 释放 ownership。跨平台 contention 最多等待 30 seconds，超时在 snapshot load 前按 write error/exit code 4 失败。dry-run、read-only、lessons export 与 resource export 不取该锁；当前持久化契约为 snapshot version 2 与 PostgreSQL schema version 2。
- 根包公开 `obsolete_failure_case()`、`obsolete_lesson()` 与 `obsolete_project_policy()` 三个低层纯转换函数；需要 lookup、cascade、replay 或原子批处理时仍使用 Store API。
- 只读 `pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH` 将严格 JSON 转为 `MemoryContext` / `PRChangeSet`；`PRChangeSet` accepts at most 6 entries，在 before entry scanning 拒绝第 7 项，边界内字段以 one pass 识别 unsupported/duplicate。oversized input 返回 input error/exit code 2 without Git ancestry capture；有效输入依次复用 `pr_report_commit_anchors()`、`capture_commit_ancestry()` 和 `pr_memory_report()`，不接受 `--write` 或调用方伪造的 ancestry。
- `tbm resource list/read/export` 和 Python resource interface 在不依赖 checkout 路径的情况下提供严格 allowlist 的规范资源；包通过 `py.typed` 声明类型信息。
- `run_memory_execution()` 提供无第三方依赖的同步 harness 编排；`MemoryRunExecutionError` 保留各阶段的 request/decision 恢复上下文与原始异常，但不自动猜测执行 outcome。

### SQLite 模式

- 不需要额外依赖，适配器使用 Python 标准库 `sqlite3`。
- `SQLiteMemoryRepository.connect(path, initialize=True)` 创建或打开文件数据库，并应用包内 schema 版本 1 的 `schemas/sqlite.sql`。
- `sync()` 是增量原子同步；顶层写入使用 `BEGIN IMMEDIATE`，保留受支持的前向转换，并在不可变冲突时完整回滚。
- `load()` 限制每集合 100,000 条、总计 250,000 条，以及最大单条/累计 UTF-8 payload 各 64 MiB，之后才返回完整验证的 Store。
- borrowed connection 由调用方管理；已有外层事务时 Repository 使用 savepoint。
- 同一 Repository 实例的公开操作会串行化；顶层回滚失败保留主异常并重试，重试仍失败则即使连接由调用方传入也会关闭，避免之后提交部分事务。
- 规范 JSON payload envelope 属于 adapter 边界；不支持直接 SQL 修改领域 payload 或原地 schema migration。

### PostgreSQL 模式

- 安装 `trace-backed-memory[postgres]`。
- 使用 PostgreSQL 12+ 和 fresh-install `schemas/postgres.sql`；pip 安装用户可先用 `tbm resource export schemas/postgres.sql postgres.sql` 导出同一份字节。
- 当前 PostgreSQL schema version 为 2。
- Phase 71 的 fresh-install DDL 强制 Failure Case 来源为 `fail`/`error` Trace、verified case 包含 review 证据，并阻止 dirty source 激活 Lesson；版本 2 进一步持久化 Gate `request_id`，保护 Trace/usage 审计不可变性与 outcome 前向转换。
- 既有 schema-version-1 数据库必须先执行包内原子 `schemas/postgres-v1-to-v2.sql`；起始版本不匹配或成功后重放会失败并回滚。canonical 与 packaged Trace Schema 继续使用 `minimum: 0` 与 `maximum: 2147483647`。
- Repository 提供同步 `sync()` / `load()`、事务回滚、borrowed/owned connection 和 caller transaction savepoint；`load()` 使用 schema owner 或具备表级写权限的 repository role，以五表有序 `SHARE` 锁读取一致状态并等待 external writer，再依次执行单条五表 `count(*)` count preflight（每集合 100,000 条、总计 250,000 条）和 loaded-row projection UTF-8 JSON payload preflight（最大单行与五表总计均为 64 MiB）；failure cases、lessons、project policies 只排除 selector 不读取的内部 `updated_at`，Trace 与 usage decision 保留全部 physical columns；全部通过后才读取记录。`sync()` 对全部既有目标行使用 `FOR UPDATE` 后再做 canonical conflict validation，其 accepted set 不因 load guard 改变；嵌套调用取得的锁持续到 caller outer transaction 最终 commit/rollback。
- 缺失行的 INSERT 使用 nested savepoint；same-primary-key concurrent INSERT 返回 `23505` 或 registry 精确 `P0001` 时重新 `FOR UPDATE`。精确重放为 `unchanged`，合法前向转换为 `updated`，保护字段差异为 `PostgresConflictError`，目标仍缺失或其他驱动错误保持 `PostgresPersistenceError`。
- 数据库 trigger 保证 Failure Case 的 source Trace/commit 与 Lesson 的 source Case 在插入后不可改绑，直接 SQL 同样受约束。

## 8. 产品成熟度

当前版本已完成路线图 Phase 0-73、本地 agent/MCP 集成增量，并完成 Phase 74 的 migration-preparation 部分。主要产品链路均有可执行 README 示例、JSON Schema、SQL invariants 和 pytest 覆盖。本地 MCP 采用固定 Git provenance、有界 strict frame、完整 ancestry capture 与 session-namespaced opaque request handle，阻止 stale finalize/cancel 在重启后命中新 request。bounded local document ingestion 使用 single file handle 施加 64 MiB、8 MiB 和 1 MiB 的输入上限；Trace 三个 structured JSON 字段共享 100,000 nodes 与 8 MiB key/string UTF-8 text 的固定 runtime budget；LLM decision 的 `allowed_memory_ids` / `blocked_memory_ids` 各限 50 项；`capture_commit_ancestry()` 以 `COMMIT_ANCESTRY_MAX_ANCHORS` 在去重前限制 1,000 个输入并在 overflow 时不启动 Git；read-only `pr-report` 保留 `commit_ancestry` 与 `report` 审计输出；active-lessons CLI 在默认 no-replace 导出和 dry-run 导入下复用同一 Store 原子边界；单项及 batch obsolescence CLI 以非敏感 dry-run 预览复用 forward-only failure-case/lesson/project-policy 状态与 case→lesson 原子 cascade，批次由 Store all-or-nothing 提交；decision-only `outcome` CLI 以最小非敏感摘要封存 deferred evaluation，不修改关联 Trace；PostgreSQL load/sync 通过表锁与行锁避免跨时刻快照和 stale protected-field validation，并在五表锁后依次以 `count(*)` count preflight 将加载限制为每集合 100,000 条、总计 250,000 条，再以 loaded-row projection UTF-8 JSON preflight 将最大单行与五表总计限制为 64 MiB，在 collection fetch 前拒绝超限数据库，且不再计入三个 selector 未读取的内部 `updated_at`；缺失行 INSERT 的保存点重查进一步将同主键并发提交分类为 `unchanged`、`updated` 或 `PostgresConflictError`，且保留目标缺失碰撞的 `PostgresPersistenceError`；所有 caller-owned JSON 在任意层拒绝重复对象键。Phase 71 更新可信提升与 LLM 边界；Phase 72 增加 schema 版本 1 的 SQLite Repository 与第 19 项 packaged resource；Phase 73 增加运行时容量/文本限制、Gate Trace/run/request 审计绑定、SQLite/摄取故障加固、PostgreSQL schema 版本 2 和第 20 项 v1-to-v2 迁移资源；Phase 74 当前交付 strict v3 mapping/preflight、不可激活 content-addressed bundle、immutable SQLite staging 与隔离 PostgreSQL staging/rollback。

Phase 71 强化可信提升与运行时边界：Failure Case 只能来自 `fail`/`error` Trace，verify 前必须具备 reviewer、root cause 与 review timestamp，dirty source 不能激活 Lesson；LLM response 限制为 64 KiB、1,000 nodes、depth 20，reason 最多 2,000 字符；所有未被 LLM 选中的系统候选都会进入 blocked 审计，超过 50 项时确定性保留前 50 项并记录其余项；`short_summary` 与 `full_case_summary` 使用不同 renderer，关键词检索支持 Unicode。

Phase 72 增加标准库 `SQLiteMemoryRepository`：增量原子同步使用 `BEGIN IMMEDIATE`，caller transaction 使用 savepoint，load 在 Store 重建前执行记录数与 UTF-8 payload 限制。SQLite 使用 schema 版本 1，PostgreSQL 当时仍使用 schema 版本 1；资源总数为 19。

Phase 73 收敛 review 指出的运行边界：限制 query、semantic mapping、batch、pending/finalized Gate 状态和 lesson/policy 文本；高层 Gate request 绑定 Trace/run，usage log 持久化 `request_id`；每个 request 与 pending request 聚合候选都有硬上限；本地摄取拒绝特殊文件，SQLite 同实例操作串行化并在顶层回滚失败时保留主异常；PostgreSQL v2 通过原子迁移增加 request audit、不可变 Trace/usage/source trigger 和前向 outcome 保护；持久化时间戳统一为最多六位小数的严格 RFC 3339；CI 增加 lint、类型、覆盖率和依赖审计门禁。

当前仍有明确的生产边界：snapshot version 2 没有 canonical `repository_id` 或显式 global/repository/tenant scope kind；独立的授权 v3 准备契约不会把现行 scope matching 变成多租户授权；`regression_passed` 仍不是结构化 run/evaluator 证据；Gate request 与 finalized tombstone 仍只存在于进程内，但 pending request 已有容量限制和显式取消，finalized tombstone 已有容量限制，高层请求会绑定 Trace/run，最终 usage log 会持久化 `request_id`；与存储实现无关的 replay descriptor 已定义 retriever/index、Gate prompt/response、ancestry、policy、renderer 与精确 snippet hash，opt-in SQLite replay ledger 可保存精确字节/descriptor，但 usage log 与 active adapter 尚不使用它，access control、retention、encryption、GateSession linkage 与 PostgreSQL parity 仍待完成；Git ancestry 仍是 opt-in。version-3 GateSession 已有 opt-in SQLite 与隔离 PostgreSQL revision repository，但尚未成为 active Agent/MCP state；授权 v3 policy/decision 契约已发布但 active adapter 尚不执行；expiry/recovery worker、服务端认证 context 与 service integration 仍属于后续统一 schema 工作。既有 version-2 snapshot 中 verified 但未 review 的 case 必须补齐证据后才能加载。

以下 Phase 49–70 段落保留各功能落地时的版本与资源基线，属于历史兼容记录；当前发布契约以 PostgreSQL schema version 2 和 55 项资源说明为准。历史上的第 21 项资源是面向既有 version-2 数据库、可重复运行且带版本门禁的 `schemas/postgres-v2-lock-order-hotfix.sql`；新增的十一项资源提供 `tbm.agent.v1` 的五份 Schema、五份 JSON 示例与一份可执行 quickstart；v3 资源包含 migration mapping/plan preflight、不可激活 bundle、隔离的 SQLite/PostgreSQL staging DDL、显式 PostgreSQL staging rollback、GateSession/内容寻址 injection/replay manifest 契约 Schema/示例、隔离 SQLite GateSession DDL，隔离 PostgreSQL GateSession install/rollback，以及授权 policy/decision Schema/示例。全新安装与当前 v1→v2 迁移已包含同一数据库修复。

Phase 49 将持久化 identity、linkage、必填 failure 文本、lesson/policy scope、Memory Context 与 usage-audit mapping 的键值统一为“至少包含一个 non-whitespace 字符”，但不会 trim 已接受的值。可选 Trace metadata、无关 Failure Case narrative 字段和 candidate/used/blocked memory-ID arrays 保持既有契约。PostgreSQL schema version 1 的默认 `btrim` 已拒绝全普通空格，但比 Python/JSON Schema 边界更窄；受支持的 Store→Repository 写入路径使用更严格的 portable validation，直接 SQL 写入的其他 whitespace-only 数据需由 operator 清理。

Phase 50 收紧失败提取的证据边界：只有带 truthy top-level `error` evidence 的具名 tool call 才能标记 tool-failure symptom，successful named call 会继续回退到 errored output、`Trace.error` 或 trace-ID symptom。显式 `invalid argument` 仍然有效，但 bare `required` 不再触发 `invalid_tool_argument`；只有 `required argument`、`required parameter`、`required field` 和 `required property` 这些保守标记会触发该分类。snapshot version 2、PostgreSQL schema version 1 和 packaged resources 保持不变。

Phase 51 将 snapshot usage-log 重建收敛为 records 与 nested ID/tool evidence 的 average O(n) validation。单次 `from_snapshot()` 复用 load-local `decision_id`、known memory IDs、legacy `run_id` 与 per-trace tool-name indexes，并以 per-log sets 检查 candidate/used/blocked relationships；诊断 ID 顺序、错误消息与验证优先级不变。索引不会进入 Store state 或持久化数据，snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 保持不变。

Phase 52 为 live Store 增加不持久化的 derived index（`decision_id`→stable list position）与 next numeric suffix。三处 append 统一维护索引，outcome/completion/recovery 在 stable list position 原位替换；allocation、duplicate check 与 single lookup 为 average O(1)，batch lookup 为 average O(k)。导入 ID 继续遵循 max numeric suffix，nonnumeric ID 不推进编号，失败写入不耗号；canonical sorting、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 保持不变。

Phase 53 为 live Store 增加第二个不持久化的 derived index（`run_id`→ordered `trace_id` values）。`record_trace()` 在同一锁内原子提交 Trace 与索引项，失败时回滚主表；lookup 以 average O(1) 区分 missing、unique 与 ambiguous run IDs，不会从 duplicate run 中任取一条。索引只保存 ID，因此 Trace completion 后仍解析当前记录；validated snapshot load 会重建索引，canonical output、legacy migration、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 保持不变。

Phase 54 将 live usage-log 的 memory existence validation 限定为 referenced IDs。未提供 snapshot-local `known_memory_ids` 时，每个去重引用直接对 failure-case、lesson、project-policy 三张主表做 membership，复杂度为 average O(r)，其中 `r` 是 referenced IDs 数量；snapshot reconstruction 继续为所有日志复用同一个 `known_memory_ids`。实现遵循 no new derived index 原则，不增加需要同步的 Store 状态；排序后的 unknown-ID error、validation order、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 保持不变。

Phase 55 将 `metrics()` 收敛为 one usage-log pass 和 O(1) accumulator space，同时累计 candidate/used/blocked、obsolete、evaluated cohorts、unevaluated 与 wrong-memory counters。pass rate 改由 pass/total counts 计算，empty cohort 仍为 None，nonempty zero-pass cohort 仍为 0.0；lesson confidence 继续单独聚合。`memory_outcome_metrics()`、memory-run ordering、CLI call boundaries、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 保持不变。

Phase 56 将 `memory_run_metrics()` 收敛为 one usage-log pass、without sorting 和 O(1) accumulator space；单条 log 的 Trace lookup、status 与 remediation 分类继续复用 `memory_run_audits()` 的同一私有构造路径。公开 audits/remediations 保持 decision-ID order，Store lock boundary、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 保持不变。

Phase 57 为 snapshot CLI `--write` 增加跨平台 serialized read-modify-write：canonical `.tbm.lock` exclusive advisory lock 在 before snapshot load 获取，覆盖 Store mutation、success serialization 与 atomic publication，并在 before stdout 释放。persistent sidecar 初始化一个 placeholder byte 且不保存领域/进程数据，OS descriptor ownership 在异常或进程退出时释放；跨平台 contention 最多等待 30 seconds，超时在 snapshot load 前按 write error/exit code 4 失败。dry-run/read-only/lessons export/resource export 保持 lock-free。snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 58 在现有 `load_lessons_yaml()` interface 内兑现 portable active-only contract：完整 Lesson/来源校验后、staged insertion 前拒绝 `status: obsolete`，混合文档仍 all-or-nothing。CLI `lessons import --write` 将该拒绝映射为 input error/exit code 2 且不保存 snapshot；`add_lesson()`、snapshot 与 PostgreSQL 继续保留 obsolete lifecycle history。snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 59 将 exact `PRChangeSet` 收紧到 at most 6 entries（即六个 supported unique fields），并在 before entry/case scanning 完成 fail-fast cardinality preflight；边界内 field-name validation 改为 one pass sets，同时保留 unsupported-before-duplicate 与 canonical sorting。CLI oversized input 返回 input error/exit code 2 without Git ancestry capture。legacy `changed_fields`、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 60 将 legacy PR warning fields 在 case scanning 前以 one pass 验证并归一化，只保留 at most 7 supported names 的 first occurrence；duplicate 与 unknown non-empty strings 继续接受，但不再放大 case-level warning work。set-backed stable deduplication 保持文本和顺序，将 expected complexity 收敛为 `O(W + C)`。exact `PRChangeSet`、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 61 将 default Git capture 改为 bounded binary `Popen`：`stdin=DEVNULL`、30 seconds timeout、explicit UTF-8 replacement decoding，ordinary stdout/stderr 各 retain at most 64 KiB，timeout/output overflow 会 kill and reap。`git status --porcelain` 只 retain first byte 并 drain/discard 剩余输出；injected runner 契约不变。snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 62 补齐 local atomic publish 的 POSIX durability：temporary file flush/`fsync()` 与 `os.replace()`/`os.link()` 成功后，在正常 temporary-name cleanup 之后打开并 `fsync()` parent directory；non-POSIX 保留 portable atomic publication。pre-publication failure 继续保留旧目标，post-publication parent-directory sync failure 上抛且目标可能已更新，调用方必须按 indeterminate durability 检查后重试。snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 63 将 semantic retrieval 改为 bounded semantic top-k：metadata-only/keyword 路径不再构造 memory ID catalog，semantic path 通过 non-copying membership view 完整验证 caller scores，再在 metadata/ancestry filter 后以 generator + heap 选择结果而不做 full sort。输出仍为 score-descending、memory-ID-ascending，ranking 从 `O(K log K)`/`O(K)` 降为 `O(K log k)`/`O(k)`。snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 64 将 CLI snapshot lock backend 抽取为根包公开 `snapshot_write_lock(snapshot_path, timeout_seconds=...)`：Python caller 可用同一 canonical `.tbm.lock` advisory、non-reentrant context 覆盖完整 read-modify-write，避免多进程 load→mutate→save 的 lost update。finite non-negative timeout 在 filesystem access 前验证，CLI private wrapper、30 seconds default、write error/exit code 4 与 dry-run/read-only lock-free 行为不变。snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 65 将 `Trace.retrieved_context`、`Trace.tool_calls` 与 `Trace.tool_outputs` 收敛到同一 fixed runtime budget：三个 outer lists 与全部 nested semantic values 合计最多 100,000 nodes，object keys 与 string values 合计最多 8 MiB UTF-8 text，并保留 depth 100。validator 在 traversal stack、`dict.items()` materialization 与 defensive copy 前对 wide container fail fast，lone surrogate 也按 path-specific `ValueError` 拒绝；record、completion、snapshot import 与 PostgreSQL load 复用同一边界且失败不修改 Store。snapshot version 2、PostgreSQL schema version 1、JSON Schemas 与 18 份 packaged resources 不变。

Phase 66 将 PostgreSQL payload preflight 从 complete physical-row proxy 收敛到实际 loaded-row projection：`failure_cases`、`lessons`、`project_policies` 通过 schema-qualified JSONB subtraction 排除 selector 不读取的内部 `updated_at`，traces 与 memory usage decisions 仍计量全部 physical columns。单条 scalar query、五表 `SHARE` 锁、count-first/prefetch ordering、compact row JSON、最大单行/累计 64 MiB、sanitized errors 与连接复用均不变；snapshot version 2、PostgreSQL schema version 1、DDL、JSON Schemas 与 18 份 packaged resources 不变。

Phase 67 收紧本地 snapshot 锁 sidecar 的文件身份：placeholder 初始化前，canonical `.tbm.lock` 路径必须与打开的 descriptor 指向同一个 single-link regular file；缺失路径使用 exclusive create，既有路径使用 no-follow metadata 以及 pre-open/descriptor/post-open identity validation，并在取得 OS lock 后、yield 前复核 descriptor/path identity。symbolic link、Windows reparse point、hard link 和 special file 在不修改 alias target、不加载 snapshot 的情况下按 `OSError` 拒绝，CLI 继续返回 write error/exit code 4。公开 API、30 seconds timeout、persistent placeholder、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 68 将 Git trace metadata 的 injected-runner 输出校验前移到 capture boundary：四条命令都必须返回 string；blank commit SHA、blank repository root、non-string output，以及超过既有 512 characters 上限的 commit/branch/repository name 都在下一条命令启动前按 command-specific `TraceMetadataCaptureError` 拒绝，且错误不回显 malformed value。blank branch 仍表示 detached HEAD，blank status 仍表示 clean；runner signature、命令顺序、bounded default process、snapshot version 2、PostgreSQL schema version 1 与 18 份 packaged resources 不变。

Phase 69 将 failure taxonomy 分类证据收敛为 explicit failure text：只搜索 `Trace.error` 与 tool call/output 的 top-level `error`，tool names 永不选择 failure type；带 truthy error 的名称仍可作为 deterministic symptom label，root-cause priority 与 evaluator fallback 不变。公开 API、taxonomy IDs、snapshot version 2、PostgreSQL schema version 1、JSON Schemas 与 18 份 packaged resources 不变。

Phase 70 修复 `recover-batch --attribution` 对合法 decision ID 的分隔歧义：`DECISION_ID=true|false` 从 final `=` 切分，完整非空前缀（包括任意更早的 `=`）原样作为 ID，后缀继续只接受 exact lowercase `true`/`false`。malformed、unrequested 与 duplicate entries 仍为 structured input exit code 2；request order、Store atomicity、snapshot version 2、PostgreSQL schema version 1、JSON Schemas 与 18 份 packaged resources 不变。

- 纯 Python store、策略、生命周期和解析；
- Git metadata 与 ancestry，包括 1,000 项输入边界、重复项计数、有界 generator 消费与 overflow 零子进程；
- snapshot/YAML 原子写入、失败清理、多段文本 round trip 与恶意 JSON 边界；
- tool-output-only 失败提取、错误证据优先级，以及 taxonomy/lesson/scope duplicate 或语义错误 YAML 的 all-or-nothing 导入；
- CLI structured JSON/exit-code contract、deterministic ordering、active-only lesson export/import、no-replace 与路径别名保护、failure-case/lesson/project-policy 单项与原子 batch obsolescence、case→lesson cascade、重叠去重、幂等与非敏感输出、单项/批量 fresh measured completion、严格清单和 file-backed tool evidence、dry-run isolation、原子写入、batch all-or-nothing 与 module/console-script smoke；
- deferred decision `outcome` CLI 的 dry-run/write、精确重放、冲突与归因约束、最小非敏感输出、故障原子性、BrokenPipe 和 wheel/sdist 独立安装 smoke；
- wheel/sdist 资源清单、逐字节 parity、隔离安装、默认 taxonomy、`py.typed` 与 PostgreSQL Schema 导出；
- callback memory-run execution 的顺序、measurement evidence、异常恢复上下文、Store 错误透传与原子失败；
- `latency_ms` 的 None/0/2,147,483,647 精确边界、上下越界跨 API/CLI/snapshot 拒绝、Trace Schema min/max 与真实 PostgreSQL `INTEGER`；
- 三个低层 obsolescence helper 的根包导出、`__all__`、函数对象身份、输入不可变性与隔离 wheel 导入；
- LLM decision ID 列表 50/51 精确边界、direct-call 防绕过与 JSON Schema `maxItems` parity；
- snapshot、MemoryContext、MemoryDecision 与 CLI JSON 的 top-level/nested duplicate object key 拒绝和 no last-key-wins 契约；
- `recover-batch` 两类 10,000 项精确边界、去重前计数、snapshot 读取前拒绝、structured input exit code 2 与无写入保证；
- 真实临时 PostgreSQL 集群上的 DDL、事务、并发锁和同步；
- PostgreSQL 五表一致 load snapshot、外部 writer exclusion，以及 failure-case/lesson/project-policy 行锁后的冲突重验证；
- PostgreSQL 锁后 count preflight 的精确边界、异常计数结果、物化前拒绝、sanitized error wrapping 与既有一致性并发路径；
- PostgreSQL 锁后 payload preflight 的最大单行/累计 64 MiB 精确边界、UTF-8 非 ASCII 计数、异常统计结果、collection fetch 前拒绝、sanitized error wrapping 与连接复用；
- PostgreSQL 五类记录 same-primary-key concurrent INSERT 的 exact replay、forward update、protected conflict、registry `P0001` 与 target-absent persistence error；
- CI 通过 `TBM_REQUIRE_POSTGRES=1` 强制执行 PostgreSQL 集成与 Repository 测试，并在独立 `windows-latest` job 运行完整回归；本地缺少数据库工具时仍保持可选 skip；
- README 工作流与产品文档契约。

当前定位仍是 Alpha：API 已系统化，但尚未承诺长期向后兼容或在线 schema migration。

## 9. 明确边界与非目标

- 不做通用聊天历史或个性化记忆。
- 不把 raw trace、完整 prompt history、private tool output 或 eval expected output 直接注入。
- 不内置 embedding/vector database；semantic scores 由调用方计算。
- 不把向量相似度视为安全或适用性的充分证明。
- 不允许 LLM 自行激活、验证或重新放行 memory。
- SQLite 使用规范 JSON payload envelope，不支持直接 SQL 领域修改、原地迁移、异步访问或跨主机共享 writer 协调。
- PostgreSQL 提供明确的 v1→v2 与 version-2 锁序 operator 脚本，不提供自动在线迁移框架、connection pool 或 async repository。
- Outcome metrics 是观测关联，不是单个 memory 的因果效果估计。
- 冲突运行只提供调查入口，不自动覆盖任一已封存结果。

## 10. 成功指标

接入团队可以直接观测：

- candidate / used / blocked memory 数量；
- with-memory 与 without-memory 的测量样本和 pass rate；
- wrong-memory failure 和 obsolete usage attempt；
- 每条 memory 的 observed pass rate；
- pending、recoverable、attribution-required 和 conflict run 数量；
- PR 变更命中的历史失败与建议回归测试。

## 11. 相关文档

- [README / 快速开始](../README.zh-CN.md)
- [架构](architecture.zh-CN.md)
- [记忆使用策略](usage-policy.zh-CN.md)
- [产品交付计划](product-program.zh-CN.md)
- [MIT License](../LICENSE)
