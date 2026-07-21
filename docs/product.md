# Trace-backed Memory 产品文档

- 当前版本：`0.1.0`（Alpha）
- 交付形态：Python 库 + `tbm` CLI + JSON/YAML/JSON Schema + 可选 PostgreSQL Repository
- 运行要求：Python 3.11+；PostgreSQL 能力要求 PostgreSQL 12+
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
| 平台工程师 | 在多仓库、多租户环境安全接入记忆 | scope、tenant/repo 隔离、固定预算、审计日志 |
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
| 安全门控 | System Gate + LLM applicability Gate；严格 JSON 输入/输出校验；LLM decision 的 `allowed_memory_ids` / `blocked_memory_ids` 各限 50 项 |
| 注入 | `none`、`pointer_only`、`short_summary`、`full_case_summary`；固定数量与字符预算 |
| 运行闭环 | 两阶段 prepare/finalize、单项/批量原子完成、延迟 outcome sealing |
| 运行编排 | `run_memory_execution()` 同步串联 decision callback、execution callback 与原子完成；`MemoryRunMeasurement` 无需调用方复制 decision ID |
| 运维修复 | 五态 audit、remediation action、单项/批量恢复、ready recovery sweep |
| 运维 CLI | dependency-free `tbm` / `python -m trace_backed_memory`；snapshot validate/stats、active lessons 原子导出与 dry-run 导入、failure case/lesson/project policy forward-only 淘汰预览与显式写入、audit/metrics/remediation、只读 PR report、单项与清单式批量 measured completion、dry-run 恢复与显式 `--write` 原子替换 |
| 分发资源 | wheel/sdist/editable 内置 18 份 byte-identical Schema、taxonomy 与示例；支持发现、读取、校验元数据和原子导出 |
| 证据摄取 | Trace、tool call 与顶层 `tool_outputs.error` 按顺序参与失败提取；成功输出不触发分类；bounded local document ingestion 对本地 JSON/YAML 先限额再校验，并以 all-or-nothing 方式导入 |
| 质量度量 | with/without-memory pass rate、错误记忆计数、per-memory observed outcomes、run health |
| PR/CI | 相关历史失败、source/fix provenance、回归建议、old/new endpoint 匹配，以及可直接接入流水线的 `pr-report` JSON 输出 |
| 持久化 | 同目录临时文件、落盘同步和原子替换的 JSON snapshot / active lesson YAML；lesson 多段文本保真；可选同步 PostgreSQL Repository；五表锁后 `count(*)` count preflight 在记录物化前限制数据库加载规模 |

所有 caller-owned JSON 都在转换为普通 mapping 前执行对象键唯一性检查：`TraceBackedMemoryStore.load_json()`、`parse_memory_context()`、`parse_memory_decision()` 和 CLI JSON 文件解析会在任意嵌套层拒绝 duplicate object keys，不采用 last-key-wins。有效 JSON、直接 Mapping 输入、snapshot version 2 与 PostgreSQL schema version 1 保持兼容。

## 5. 关键产品流程

### 5.1 安全运行时记忆

1. Harness 以 `eval_result="unknown"` 注册当前 Trace。
2. `prepare_memory()` 按 metadata、可选 query/semantic score 和 ancestry 找候选，并执行 System Gate。
3. 外部 LLM 返回结构化 applicability decision。
4. `finalize_memory()` 重新检查状态、收窄 decision、生成受限 snippet，并记录关联 Trace 的 usage audit。
5. Harness 执行并评估任务。
6. `complete_memory_run()` 或 `complete_memory_runs()` 原子写入 Trace 与 decision outcome；本地 snapshot 运维也可用 `tbm complete` 或 `tbm complete-batch` 提交显式实测结果。

普通同步调用方可以用 `run_memory_execution()` 把第 2-6 步收敛为一次调用；LLM 与 harness 仍由调用方 callback 提供，Store 继续拥有门控、linkage 和原子完成。需要暂停、人工重试或独立生命周期控制的高级调用方继续直接使用底层方法。

### 5.2 从失败到可复用 Lesson

1. 从失败 Trace 分类并生成 Failure Case 草稿。
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

- **Provenance chain**：Lesson 必须可追溯到 verified、regression-backed Failure Case，再追溯到 source Trace 和 commit。
- **严格 scope**：memory 声明的每个 scope 字段都必须与当前 context 精确匹配；缺失字段不算匹配。
- **租户与仓库隔离**：`tenant` 和 `repo` 是硬边界。
- **评测泄漏防护**：相同 `(eval_suite, input_hash)` 的历史示例自动阻断；sensitive 和 eval-leaking memory 更早阻断。
- **不可逆历史**：身份、来源和已填充的执行证据不可重写；生命周期只允许前向变化。
- **原子写入**：Trace/decision 的单项和批量完成先构建并验证全部候选，再一次提交。
- **固定预算**：最多 50 个 gate candidates、20 个 injected memories、32,000 字符 gate prompt 和 12,000 字符 snippet。
- **本地文档限额**：snapshot 为 64 MiB、每集合 100,000 条且总计 250,000 条；active lessons 与 CLI JSON 为 8 MiB；failure taxonomy 为 1 MiB。CLI JSON 另限 10,000 个顶层项目、100,000 个 JSON nodes 和 depth 100；Python API 可通过 `max_bytes` 等关键字参数显式传入 `None`，仅用于可信离线迁移。
- **防御性所有权**：store 使用锁与 defensive copies，调用方不能通过返回对象修改内部状态。

## 7. 部署与集成

### Core 模式

- 核心包无第三方运行时依赖。
- 适合嵌入现有 Python harness、eval runner 或 CI 工具。
- JSON snapshot version 2 用于完整 store 的本地持久化。
- YAML adapter 用于导入/导出 active lessons；新导出使用 literal block，并保留空行、首尾换行与行内空格。
- 安装后提供 `tbm` console script；`python -m trace_backed_memory` 提供等价入口。
- CLI 通过现有 snapshot validation、audit、metrics、remediation、completion 和 recovery API 工作，不复制领域规则；`complete` 不推断 outcome、关联 ID、归因或证据，`complete-batch` 由 Store 从 decision 推导 Trace 并整批提交。
- `tbm lessons export SNAPSHOT DESTINATION [--overwrite]` 复用 `save_lessons_yaml()` 的 canonical active-only serializer，默认以原子 no-replace 发布并拒绝 snapshot 文件别名；`tbm lessons import SNAPSHOT SOURCE_YAML [--write]` 复用 `load_lessons_yaml()` 的 8 MiB/10,000 条受限、all-or-nothing 全量暂存与来源校验，默认不改源文件。
- `tbm obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]` 只输出 ID、前后状态与 case→lesson 级联清单，默认作为 dry-run 预览；转换复用 Store 的 forward-only、幂等和原子级联规则，不回显敏感 memory/Trace 内容，不提供非原子批量循环或 reactivation。
- `tbm obsolete-batch SNAPSHOT REQUESTS_JSON [--write]` 将受限 strict JSON 转为公开的 `MemoryObsolescenceRequest` tuple，并只调用一次 `obsolete_memories()`；Store 从同一入口状态暂存 failure-case、lesson、project-policy 与完整 cascade，全部校验后一次提交，显式结果保持 request order，重叠 lesson 通过 `affected_count` 去重，默认 dry-run。
- 只读 `pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH` 将严格 JSON 转为 `MemoryContext` / `PRChangeSet`，依次复用 `pr_report_commit_anchors()`、`capture_commit_ancestry()` 和 `pr_memory_report()`；不接受 `--write` 或调用方伪造的 ancestry。
- `tbm resource list/read/export` 和 Python resource interface 在不依赖 checkout 路径的情况下提供严格 allowlist 的规范资源；包通过 `py.typed` 声明类型信息。
- `run_memory_execution()` 提供无第三方依赖的同步 harness 编排；`MemoryRunExecutionError` 保留各阶段的 request/decision 恢复上下文与原始异常，但不自动猜测执行 outcome。

### PostgreSQL 模式

- 安装 `trace-backed-memory[postgres]`。
- 使用 PostgreSQL 12+ 和 fresh-install `schemas/postgres.sql`；pip 安装用户可先用 `tbm resource export schemas/postgres.sql postgres.sql` 导出同一份字节。
- 当前 PostgreSQL schema version 为 1。
- Repository 提供同步 `sync()` / `load()`、事务回滚、borrowed/owned connection 和 caller transaction savepoint；`load()` 使用 schema owner 或具备表级写权限的 repository role，以五表有序 `SHARE` 锁读取一致状态并等待 external writer，再以单条五表 `count(*)` count preflight 在任何记录读取前执行每集合 100,000 条、总计 250,000 条的既有限额；`sync()` 对全部既有目标行使用 `FOR UPDATE` 后再做 canonical conflict validation；嵌套调用取得的锁持续到 caller outer transaction 最终 commit/rollback。
- 缺失行的 INSERT 使用 nested savepoint；same-primary-key concurrent INSERT 返回 `23505` 或 registry 精确 `P0001` 时重新 `FOR UPDATE`。精确重放为 `unchanged`，合法前向转换为 `updated`，保护字段差异为 `PostgresConflictError`，目标仍缺失或其他驱动错误保持 `PostgresPersistenceError`。

## 8. 产品成熟度

当前版本已完成路线图 Phase 0-43，主要产品链路均有可执行 README 示例、JSON Schema、SQL invariants 和 pytest 覆盖。bounded local document ingestion 使用 single file handle 施加 64 MiB、8 MiB 和 1 MiB 的输入上限；LLM decision 的 `allowed_memory_ids` / `blocked_memory_ids` 各限 50 项；`capture_commit_ancestry()` 以 `COMMIT_ANCESTRY_MAX_ANCHORS` 在去重前限制 1,000 个输入并在 overflow 时不启动 Git；read-only `pr-report` 保留 `commit_ancestry` 与 `report` 审计输出；active-lessons CLI 在默认 no-replace 导出和 dry-run 导入下复用同一 Store 原子边界；单项及 batch obsolescence CLI 以非敏感 dry-run 预览复用 forward-only failure-case/lesson/project-policy 状态与 case→lesson 原子 cascade，批次由 Store all-or-nothing 提交；decision-only `outcome` CLI 以最小非敏感摘要封存 deferred evaluation，不修改关联 Trace；PostgreSQL load/sync 通过表锁与行锁避免跨时刻快照和 stale protected-field validation，并在五表锁后以 `count(*)` count preflight 将加载限制为每集合 100,000 条、总计 250,000 条，在记录物化前拒绝超限数据库；缺失行 INSERT 的保存点重查进一步将同主键并发提交分类为 `unchanged`、`updated` 或 `PostgresConflictError`，且保留目标缺失碰撞的 `PostgresPersistenceError`；所有 caller-owned JSON 在任意层拒绝重复对象键。该 guard 不限制单个 JSONB/text 值的字节数。Phase 43 仅统一严格 JSON object-key 解析；snapshot version 2、active-lessons YAML、18 份 packaged resource 路径/数量与 PostgreSQL schema version 1 保持不变。

- 纯 Python store、策略、生命周期和解析；
- Git metadata 与 ancestry，包括 1,000 项输入边界、重复项计数、有界 generator 消费与 overflow 零子进程；
- snapshot/YAML 原子写入、失败清理、多段文本 round trip 与恶意 JSON 边界；
- tool-output-only 失败提取、错误证据优先级，以及 taxonomy/lesson/scope duplicate 或语义错误 YAML 的 all-or-nothing 导入；
- CLI structured JSON/exit-code contract、deterministic ordering、active-only lesson export/import、no-replace 与路径别名保护、failure-case/lesson/project-policy 单项与原子 batch obsolescence、case→lesson cascade、重叠去重、幂等与非敏感输出、单项/批量 fresh measured completion、严格清单和 file-backed tool evidence、dry-run isolation、原子写入、batch all-or-nothing 与 module/console-script smoke；
- deferred decision `outcome` CLI 的 dry-run/write、精确重放、冲突与归因约束、最小非敏感输出、故障原子性、BrokenPipe 和 wheel/sdist 独立安装 smoke；
- wheel/sdist 资源清单、逐字节 parity、隔离安装、默认 taxonomy、`py.typed` 与 PostgreSQL Schema 导出；
- callback memory-run execution 的顺序、measurement evidence、异常恢复上下文、Store 错误透传与原子失败；
- LLM decision ID 列表 50/51 精确边界、direct-call 防绕过与 JSON Schema `maxItems` parity；
- snapshot、MemoryContext、MemoryDecision 与 CLI JSON 的 top-level/nested duplicate object key 拒绝和 no last-key-wins 契约；
- 真实临时 PostgreSQL 集群上的 DDL、事务、并发锁和同步；
- PostgreSQL 五表一致 load snapshot、外部 writer exclusion，以及 failure-case/lesson/project-policy 行锁后的冲突重验证；
- PostgreSQL 锁后 count preflight 的精确边界、异常计数结果、物化前拒绝、sanitized error wrapping 与既有一致性并发路径；
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
- PostgreSQL 暂不提供 in-place migration、connection pool 或 async repository。
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

- [README / Quick start](../README.md)
- [Architecture](architecture.md)
- [Memory usage policy](usage-policy.md)
- [Implemented roadmap](mvp-roadmap.md)
- [MIT License](../LICENSE)
