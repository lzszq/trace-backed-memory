# PostgreSQL RunOutcome 完成事务 v3

[English](postgres-outcome-v3.md) | **简体中文**

这个 opt-in authority 会把隔离 PostgreSQL GateSession 从 `EXECUTING` 闭合到
`COMPLETED`，并在一个数据库 transaction 中写入一份 immutable、
content-addressed `RunOutcome` 与对应 GateSession revision。它不会改变 active
PostgreSQL schema version 2，也不会把 active Agent/MCP 生命周期接入持久化 v3
completion。

## 安装与回滚

先安装 `schemas/postgres-v3-gate-session.sql`，再安装
`schemas/postgres-v3-outcome.sql`。outcome installer 会锁定并校验 active-v2 与
GateSession-v1 metadata，创建隔离的 `trace_backed_memory_v3_outcome` schema，
而不改变 active schema。

回滚时先运行 `schemas/postgres-v3-outcome-rollback.sql`，再回滚 GateSession。
脚本会在删除任何对象前精确校验 metadata、relation、function、trigger、
constraint 与 column catalog。出现额外对象、schema drift、依赖 drift，或者
active schema 不是 version 2 时，回滚会 fail closed，不做 partial cleanup。

## 完成事务

`PostgresOutcomeV3Repository.complete_session()` 接受 canonical
`GateCompletionRequest`，并从已锁定的持久化 GateSession 派生 session、Trace、
run 与 usage-decision identity。操作顺序为：

1. 开启 transaction；调用方已有 transaction 时使用 savepoint；
2. 锁定并校验 active、GateSession 与 RunOutcome metadata/catalog；
3. 使用 `FOR UPDATE` 锁定当前 GateSession head；
4. 已完成且输入完全相同时不读取时钟并直接重放；否则要求状态为
   `EXECUTING` 且 expected version 精确匹配；
5. 在取得 head lock 后读取 PostgreSQL 数据库可信时间；
6. 用同一 timestamp 构造并验证 RunOutcome 与 `COMPLETED` revision；
7. 通过 CAS 追加 revision、插入 immutable outcome，并在 commit 前精确读回
   两条记录。

任何 contract、SQL、trigger、catalog、CAS 或 read-back 失败都会回滚两条记录。
并发的相同完成请求在 GateSession head 上串行化，只保留一个 outcome；已完成
session 收到不同 measurement 时会冲突。

共享的 `gate_sessions` 初始化 authority 会拒绝直接转换到 `COMPLETED`；
调用方必须使用 `complete_session()`。独立 GateSession adapter 仍是更底层、
单独 opt-in 的 authority。

## 存储与信任边界

outcome schema 强制每个 session 只有一个 outcome、标识符与 descriptor 有界、
evidence digest 有序且唯一、result/error 与 output shape 一致、completed
session identity 精确关联，并禁止 update/delete/truncate。insert trigger 会重建
精确 canonical descriptor、重算 payload SHA-256 与派生 outcome ID，并在持久化前
拒绝非规范 JSON。repository 读回时会独立重新解析 descriptor、重算 content ID、
比较所有存储列、验证 completed session，并拒绝 managed catalog drift。

`GateSessionCompletionService` 仍是 storage-neutral receipt 与持久化读回
verifier。它和该底层 repository 都不会认证 evaluator、授权 evidence artifact
byte、从 callback 推导 result，也不会发送 outbox event。需要原子 completion
publication 的应用应使用配套
[`PostgresCompletionOutboxV3Repository`](completion-outbox-v3.zh-CN.md)。

## 当前边界

SQLite OutcomeAttribution persistence 由独立的
[immutable SQLite ledger](sqlite-outcome-attribution-v3.zh-CN.md)提供，并由隔离的
[PostgreSQL attribution ledger](postgres-outcome-attribution-v3.zh-CN.md)
提供 database parity。authenticated evaluator derivation、artifact
authorization、network dispatch，以及 active Agent/MCP/HTTP/SDK integration
仍是后续工作。snapshot version 2、SQLite schema version 1、
PostgreSQL schema version 2 与 `tbm.agent.v1` 均保持不变。
