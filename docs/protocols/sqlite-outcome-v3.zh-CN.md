# SQLite RunOutcome Completion v3

[English](sqlite-outcome-v3.md) | **简体中文**

这个 opt-in authority 会把持久化 SQLite GateSession 生命周期从
`EXECUTING` 闭合到 `COMPLETED`。它在一个 transaction 中写入一份 immutable、
content-addressed `RunOutcome` 与对应 GateSession revision。它不会替代 active
snapshot-v2 Store，也不会接管 process-local Agent/MCP 生命周期。

## 完成事务

`SQLiteOutcomeV3Repository.complete_session()` 接受 canonical
`GateCompletionRequest`。session、Trace、run 与 usage-decision identity
来自当前持久化 GateSession，而不是 caller 字段。repository 会：

1. 获取 SQLite writer reservation 或 caller-owned savepoint；
2. 校验 canonical GateSession 与 RunOutcome schema；
3. 重新加载当前 session，并要求其处于 `EXECUTING` 且 version 精确匹配；
4. 获取一个晚于上一 revision 的 server-owned timestamp；
5. 用同一 timestamp 构造 content-addressed RunOutcome 与 `COMPLETED`
   revision；
6. 通过 GateSession CAS 追加 revision、插入 outcome，并在 commit 前精确读回
   两条记录。

任何 validation、SQL、CAS、trigger 或 read-back 失败都会回滚两条记录。
完成后重放同一 measurement 会直接返回已保留记录并给出
`inserted=false`，不会再次读取时钟；不同 measurement 会冲突。

共享的 `gate_sessions` 初始化 authority 会拒绝直接转换到 `COMPLETED`；
调用方必须使用 `complete_session()`，因此公开的组合 repository 不会留下缺少
outcome 的 completed revision。独立 GateSession adapter 仍是更底层、单独 opt-in
的 authority。

## SQL 与 service 边界

`schemas/sqlite-v3-outcome.sql` 是独立 schema version 1 资源，依赖
side-by-side GateSession schema。它强制每个 session 只有一个 outcome、
canonical descriptor 重建、有序且唯一的 evidence digest、completed-session
identity linkage，以及 immutable update/delete guard。repository 读取时会重新
解析 descriptor、重算 content ID、比较每个存储列、验证 completed session，并
拒绝缺失、额外或改变的 managed schema object。

`GateSessionCompletionService` 是 storage-neutral receipt 与持久化读回
verifier。它不会认证 evaluator、校验 evidence artifact byte，也不会从执行异常
推断 result；这些值必须来自可信 service boundary。

## 当前边界

隔离[PostgreSQL completion authority](postgres-outcome-v3.zh-CN.md)现已提供
PostgreSQL parity。SQLite OutcomeAttribution persistence 由独立的
[immutable attribution ledger](sqlite-outcome-attribution-v3.zh-CN.md)提供，
并有隔离的
[PostgreSQL parity](postgres-outcome-attribution-v3.zh-CN.md)。opt-in
[SQLite 与 PostgreSQL completion outbox](completion-outbox-v3.zh-CN.md)会在
对应 transaction 中加入 immutable event 与 append-only delivery state。
authenticated evaluator derivation、artifact authorization、network dispatch
以及 active Agent/MCP/HTTP/SDK 集成仍是后续工作。active snapshot
version 2、SQLite schema version 1、PostgreSQL schema version 2 与
`tbm.agent.v1` 均保持不变。
