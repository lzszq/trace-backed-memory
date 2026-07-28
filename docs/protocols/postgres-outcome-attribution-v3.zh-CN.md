# PostgreSQL OutcomeAttribution Ledger v3

[English](postgres-outcome-attribution-v3.md) | **简体中文**

这个 opt-in 隔离 ledger 为 immutable `tbm.outcome-attribution.v3` 记录提供
PostgreSQL parity。它针对 retained RunOutcome 与对应 completed GateSession，
追加独立产生的 association 或 causal claim。它不会改变 active PostgreSQL
schema version 2，也不会在 Agent、MCP、HTTP 或 SDK adapter 中激活 v3
lifecycle。

## 安装与回滚

依次安装：

1. `schemas/postgres-v3-gate-session.sql`；
2. `schemas/postgres-v3-outcome.sql`；
3. `schemas/postgres-v3-outcome-attribution.sql`。

installer 会先锁定并校验 active-v2、GateSession-v1 与 RunOutcome-v1 metadata，
再创建隔离的 `trace_backed_memory_v3_outcome_attribution` schema；active table
与兼容版本均保持不变。

回滚时先运行 `schemas/postgres-v3-outcome-attribution-rollback.sql`，然后再运行
RunOutcome 与 GateSession rollback。脚本取得 exclusive table lock，精确校验
metadata、relation、function、trigger、constraint 与 column，并通过 `RESTRICT`
删除。任何 drift 或外部依赖都会使整个回滚失败。

## API 与事务

`PostgresOutcomeAttributionV3Repository` 暴露：

- `put_attribution(attribution)`：immutable append 或精确 content-ID replay；
- `get_attribution(attribution_id)`：读取一条完整核验记录；
- `list_attributions(run_outcome_id)`：按 `recorded_at`/identity 确定性排序；
- `outcomes`：共享的 PostgreSQL RunOutcome 与受保护 GateSession authority。

一个 RunOutcome 可以保留多条 claim。并发的精确 replay 通过主键串行化并返回
`inserted=False`；相同 ID 对应不同 retained content 时冲突。每次操作使用一个
transaction；调用方已拥有 transaction 时使用 PostgreSQL savepoint。append 在
insert 前核验 linkage，随后精确读回、再次跨记录核验，并在 commit 前完成最终
catalog 校验。

## SQL 完整性

insert trigger 会重建与 Python 逐字节一致的 canonical revision、evidence、
confidence、timestamp、payload 与 descriptor 文本，重算 attribution SHA-256
ID，并拒绝非规范 JSON、替代数字写法、未知 descriptor shape、非法 identifier/
text，以及不符合 association/causal 规则的 claim。

trigger 会独立重新解析并重算 linked RunOutcome row，锁定当前 completed
GateSession head 与 revision，并要求 trace、run、usage decision、outcome、
timestamp 与 finalized memory 精确关联。PostgreSQL `timestamptz(6)` instant
比较保留微秒顺序。UPDATE、DELETE、TRUNCATE、schema drift、catalog/function
body drift 与 partial write 均 fail closed。

repository 读取时会独立解析并重算 descriptor hash、比较每个存储列、重新加载
outcome 与当前 session，并执行 `verify_outcome_attribution()`。锁序固定为
GateSession、RunOutcome、OutcomeAttribution，使所有组合操作采用相同依赖顺序。

## 信任边界

ledger 把 evaluator/verifier ID、artifact hash 与调用方提供的可信 timestamp
作为 provenance 保存。它不认证 identity、不授权 artifact bytes、不产生
completion/attribution outbox event，也不会把观察 association 提升为因果。
这些职责仍属于可信 service boundary。active Agent/MCP/HTTP/SDK 集成仍是后续
工作。
