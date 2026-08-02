# Effect Event v1

[English](effect-event-v1.md) | **简体中文**

`tbm.effect-event.v1` 是本地 completion-notification effect 生命周期的存储中立
canonical event 契约。它把既有 durable completion outbox 绑定到 append-only effect
stream，但绝不会把 delivery 状态当作远端 provider 结果的证明。

## 事件族

Version 1 注册八类 typed event：

- `tbm.effect.requested`；
- `tbm.effect.started`；
- `tbm.effect.succeeded`；
- `tbm.effect.failed`；
- `tbm.effect.retry_scheduled`；
- `tbm.effect.dead_lettered`；
- `tbm.effect.compensation_requested`；以及
- `tbm.effect.compensated`。

每个 effect 都使用独立的 `effect` stream。不可变 `EffectContract` 绑定 effect
identity/type、idempotency key、请求来源 event、输入 Artifact 摘要、authorization
event，以及是否支持补偿。completion-notification request 还会嵌入精确的
`tbm.completion-outbox-event.v3` descriptor 与初始 `pending` delivery revision。
其 effect ID 和 idempotency key 都等于 outbox event ID，输入摘要等于 RunOutcome
descriptor 摘要。

Delivery event 会保留精确的前一版和当前版 outbox delivery revision。parser 会重新
核验 outbox transition、identity、可信 scope、stream parent、authorization linkage
与 causation。claim 或 reclaim 生成 `EffectStarted`；acknowledgement 生成
`EffectSucceeded`；可重试失败生成 `EffectFailed`，随后生成
`EffectRetryScheduled`；终态失败生成 `EffectFailed`，随后生成
`EffectDeadLettered`。retry/dead-letter disposition 之前必须紧邻对应 failure event。

## Event-first 持久化

显式 SQLite/PostgreSQL durable completion repository 会在一个 transaction 或调用方
savepoint 中依次追加：

1. `EvaluationAuthenticated`；
2. `RunOutcomeRecorded`；
3. `GateSessionCompleted`；
4. 带 completion event 与初始 delivery 的 `EffectRequested`。

claim、reclaim、acknowledgement、retry 与 dead-letter 操作会在新 delivery revision/head
所在的同一 transaction 内追加对应 effect event batch。canonical event actor 使用真实
`worker_id`，而不是 repository 占位身份。PostgreSQL 会在 outbox row lock 之前锁定
event-ledger schema/global head。所有 canonical event 与同步 authority row 必须一起保留
并读回，否则整个操作回滚。

精确 completion replay 会返回已保留 effect request，不会追加重复 event。evidence
缺失、不唯一、跨 partition、顺序错误或 projection 不一致时一律 fail closed。

## EffectQueue projection

已注册的 `effect-queue` reducer 会重建 `effect_queue_v1`，状态为 `ready`、
`leased`、`retry`、`dead_letter`、`succeeded` 与 `compensated`。它保留紧凑不可变
event metadata、精确 outbox delivery history、attempt count、pending failure、当前
delivery 与 stream head。parity verifier 会把重建出的 contract、status、completion
event 与 delivery revision 同过渡期 completion-outbox authority 逐项比较。

reducer 强制线性 stream、终态单调性、`failed` 在 retry/dead-letter 之前的精确顺序，
并要求 compensation 使用新的因果关联 effect stream。当前已经提供存储中立的
compensation builder、parser 与 reducer transition，但 SQLite/PostgreSQL completion
repository 尚未提供 durable compensation append API。

## 信任边界

`EffectSucceeded` 只表示本地 consumer callback 返回有效 acknowledgement，且本地
outbox revision 进入 `delivered`。response digest 只是审计 metadata。两者都不能证明
外部 provider 已执行副作用、只执行一次或返回了 durable receipt。

provider request ID、provider receipt、effect contract 之外的 authorization event、
unknown-result 分类、reconciliation 与 durable compensation orchestration 仍属于 F3。
当前 adapter 保持 at-least-once delivery、仅显式 opt-in，且不会改变
`persistence_model="authority_graph"` 或 `full_persistence=false`。
