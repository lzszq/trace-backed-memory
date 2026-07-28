# Completion outbox v3

[English](completion-outbox-v3.md) | **简体中文**

Completion outbox 为一条 durable RunOutcome 发布一条 immutable
`execution_completed` event。它把内容寻址 event 与 append-only delivery state
分离，因此 retry 不会改写 completion evidence。

## 契约

- `tbm.completion-outbox-event.v3` 绑定 canonical tenant、repository、
  GateSession、Trace、run、usage decision、RunOutcome、outcome descriptor
  digest 与 completion timestamp。
- `tbm.completion-outbox-delivery.v3` 表示一条内容寻址 delivery revision；
  状态为 `pending`、`leased`、`retry_wait`、`delivered` 或 `dead_letter`。
- Event 与 delivery JSON 均有界，拒绝重复键和未知字段，并使用由 canonical
  content 派生的 identity。
- Delivery history 必须线性增长。claim 会增加 attempt count 并创建有界 lease；
  过期 lease 可以被重新领取。失败 attempt 只能进入有界 retry 或终态 dead letter。

规范 portable 资源为：

- `schemas/completion_outbox_event_v3.schema.json`；
- `schemas/completion_outbox_delivery_v3.schema.json`；
- `examples/completion_outbox_event_v3.example.json`；
- `examples/completion_outbox_delivery_v3.example.json`。

## SQLite authority

`SQLiteCompletionOutboxV3Repository` 组合隔离的 SQLite
RunOutcome/GateSession authority。`complete_session()` 在一个 outer transaction
或调用方 savepoint 中完成：

1. 核验并追加 `EXECUTING` 到 `COMPLETED`；
2. 插入 immutable RunOutcome；
3. 插入 immutable completion event；
4. 插入初始 `pending` delivery revision 与 head；
5. commit 前读回并核验所有保留记录。

完全相同的 completion replay 会返回已保留 event 与当前 delivery head，不会创建
第二条 event。如果 completed outcome 已存在但对应 outbox event 缺失，repository
会将其视为 orphan 并拒绝；它不会静默修补已经破坏的原子边界。

Worker 接口被明确限制为：

- `claim_due(worker_id, lease_seconds, limit=100)`；
- `acknowledge(event_id, expected_version, worker_id, response_sha256=None)`；
- `fail_delivery(event_id, expected_version, worker_id, error_code,
  retry_delay_seconds, max_attempts)`；
- event、当前 delivery 与 delivery history 的精确读取。

SQLite schema 使用 immutable event/delivery revision、单个 compare-and-swap
head、canonical descriptor 校验、整数微秒级 due 排序、schema drift 检测与调用方
transaction 保留，并使用 repository-scoped mutation guard。同一个 connection
上的多个 repository wrapper 共享一把 re-entrant lock 与一个 thread-local
mutation scope。通过 repository 所有 connection 执行的 direct DML 会被拒绝。

## Delivery 语义与边界

Delivery 是 **at least once**。Worker 可能已经成功发布，却在 acknowledge 前崩溃，
之后 lease 会被重新领取。因此 consumer 必须按 `event_id` 去重；response digest
只是审计 metadata，不是远端副作用 exactly once 的证明。

这是 opt-in、side-by-side SQLite authority。它不会改变 active SQLite schema
version 1，不会发起网络请求、认证 evaluator、授权 artifact byte、创建
OutcomeAttribution event，也不会把 durable completion 接入 active
Agent/MCP/HTTP/SDK lifecycle。PostgreSQL 对等实现与 active adapter integration
仍属于统一推进的 version-3 计划。
SQLite connection owner 仍是可信 operator boundary：能够替换已注册 SQLite
function 或删除并重建 trigger 的代码同样能够改写数据库，不得把这种能力暴露给
不可信调用方。
