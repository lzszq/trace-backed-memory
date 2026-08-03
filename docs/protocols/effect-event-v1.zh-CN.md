# Effect Event v1

[English](effect-event-v1.md) | **简体中文**

`tbm.effect-event.v1` 是本地 completion delivery 与 authenticated provider-effect
evidence 的存储中立 canonical event 契约。它把既有 durable completion outbox 和
provider request/receipt/reconciliation 记录绑定到 append-only effect stream，但绝不会
把本地 delivery 状态当作远端结果的证明。

## 事件族

Version 1 注册九类 typed event：

- `tbm.effect.requested`；
- `tbm.effect.started`；
- `tbm.effect.succeeded`；
- `tbm.effect.failed`；
- `tbm.effect.retry_scheduled`；
- `tbm.effect.dead_lettered`；
- `tbm.effect.compensation_requested`；
- `tbm.effect.compensated`；以及
- `tbm.effect.provider_transition`。

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

## Provider transition 与 receipt evidence

严格判别的 `ProviderEffectTransitionRef` 用一个 registry type 表达全部 provider
子状态，而不为每个子状态消耗独立 type。stage 包括 `attempt_started`、
`request_submitted`、`result_unknown`、`receipt_recorded`、`reconciled` 与
`retry_scheduled`、`dead_lettered`。每个 transition 都绑定精确 effect、attempt sequence、内容派生的
attempt/invocation identity、provider/model/endpoint registration 与 request digest。
provider request ID 只有在 authenticated adapter 报告后才会保留。

成功 receipt 必须携带 provider request ID 与 response digest；其
`provider_receipt_id` 由 invocation、request ID 与 response digest 内容派生。
reconciliation 独立排序并内容寻址：`confirmed` 必须携带同一份精确 receipt；
`still_unknown` 继续保持 unknown；只有 `not_found` 才允许随后显式安排 retry。
unknown 或跨崩溃遗留的 in-flight/submitted attempt 绝不会静默转成 retry；只有
not-found reconciliation 与 retry-scheduled evidence 都存在时才能开始新 attempt。
新 attempt 的 event time 不得早于已保留的 `retry_at`；attempt 耗尽会追加 terminal
`dead_lettered` evidence。

`ProviderEffectLedgerService` 通过 authenticated `EventLedgerPort` 追加 transition，
只对过期 stream/global position 重试，并在 response loss 后精确重放已保留 append
receipt。恢复结果只有 `start_attempt`、`reconcile`、`schedule_retry`、`dead_letter` 或 `complete`。
重启后若只看到 `attempt_started` 或 `request_submitted`，结果必须是 `reconcile`，
因为 ledger 无法证明外部请求是否已经执行。该分类不会授权 Semantic adapter 改写
attempt：缺少 durable owner-abandonment evidence 时，已保留的 in-flight/submitted work
保持 recovery-required，且不会调用 reconciler。服务绑定一份 server-owned
`TrustedProviderEffectRegistration`，并拒绝 provider/model/version/endpoint 不一致的
transition，也会核验已保留 transition history 的 registration。`result_unknown` 后直接写
receipt 会被拒绝，只有 trusted confirmed reconciliation 可以把 unknown 变为 success。
对声明支持 compensation 的 contract，服务还提供幂等 `request_compensation` 与
`complete_compensation`；compensation 使用新 stream，且没有精确 provider receipt 就不能
完成。application 必须把服务放在 authenticated provider adapter 之后；服务不会
从 request JSON 认证远端 provider。

显式 durable SQLite/PostgreSQL runtime 配置可信 Semantic provider invoker 时，
`SemanticProviderEffectService` 会在 provider 调用前选择该 ledger。它先追加
`EffectRequested`，并用 `attempt_started` 原子 claim request-only stream，再保留
submission/unknown/receipt evidence。只有成功插入 claim 的 owner 才调用 provider。不可变
request idempotency key 会绑定可信 provider registration 与任何 retry-policy descriptor；
callback 则收到稳定 effect ID 作为 provider 幂等键。
对该 Semantic adapter 而言，transition 字段 `response_sha256` 绑定带版本的完整
`SemanticProviderResult` descriptor，包括 raw response 字节摘要、provider request 与
decision ID、allowed/blocked ID、reason、risk、recommended injection 与 token count；
raw response 字节仍由 Semantic artifact authority 保存。崩溃后即使 prompt 或 provider
配置变化，也不能创建第二条 effect stream 或重复调用 provider。

active 或 unknown retained attempt 在没有新 evidence 时不会再次调用 provider。
in-flight/submitted work 保持 recovery-required，除非服务端 verifier 证明精确 retained
owner actor、attempt、invocation 与 head 已被 fence。该操作只追加 `result_unknown`；迟到
owner receipt 必须经过 reconciliation。可信 provider-specific reconciler 可以确认精确
结果、继续 unknown 或报告 not found；retained successful receipt 必须得到精确确认。只有
trusted `not_found` 才允许执行原始 request 绑定的有界 retry policy；其 digest 保存在 effect
idempotency key 中，每条 retained `retry_at` 都会按精确 policy deadline 重新核验，耗尽后追加
terminal dead-letter。原始 request authorization 保持不可变；同 scope 的 reconciliation transition
可以使用新的 authorization decision，且每条 event 都记录自己的当前 decision。
已保留 transition 的 exact append replay 仍要求该 transition 的原始 authorization，
因为 receipt 会绑定完整 canonical event。
Semantic provider effect 自身声明 compensation unsupported；generic receipt-backed
compensation 只适用于显式声明支持它的 effect contract；global event CAS 保证每个 original
effect 最多一条 compensation stream。completion-provider integration
仍是独立边界。

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

已注册的 `effect-queue` reducer version 3 会重建 `effect_queue_v1` schema version 2，
状态为 `ready`、
`leased`、`retry`、`dead_letter`、`succeeded` 与 `compensated`。它保留紧凑不可变
event metadata、精确 outbox delivery history、attempt count、pending failure、当前
delivery 与 stream head。parity verifier 会把重建出的 contract、status、completion
event 与 delivery revision 同过渡期 completion-outbox authority 逐项比较。

同一 projection 现在还会保留 provider attempt/transition、精确 receipt/reconciliation
identity，以及 `not_started`、`in_flight`、`submitted`、`unknown`、`not_found`、
`retry_wait`、`dead_lettered` 与 `succeeded` provider 状态。receipt/request ID 不一致、
reconciliation 不连续、not-found 或已保留 `retry_at` 之前 retry、unknown 后直接 receipt，
或 provider provenance 变化都会 fail closed。

reducer 强制线性 stream、终态单调性、`failed` 在 retry/dead-letter 之前的精确顺序，
并要求 compensation 使用新的因果关联 effect stream。当前已经提供存储中立的
compensation builder、parser 与 reducer transition，并由 generic provider-ledger append API
执行。completion-outbox repository 仍未集成 completion-provider compensation adapter。

## 信任边界

`EffectSucceeded` 只表示本地 consumer callback 返回有效 acknowledgement，且本地
outbox revision 进入 `delivered`。response digest 只是审计 metadata。两者都不能证明
外部 provider 已执行副作用、只执行一次或返回了 durable receipt。

`provider_receipt_id` 只证明 authenticated 本地 adapter 已保留内容绑定的 provider
报告，不能证明远端 exactly-once execution。raw provider body、secret 与无界错误绝不
嵌入 event。

存储中立 provider event/reducer/ledger service 已交付，generic SQLite/PostgreSQL ledger
无需新增 authority 或 schema component 即可保留它。配置后的显式 durable runtime 会选择
server-owned Semantic provider invocation；trusted reconciliation、owner fencing 与有界
retry/dead-letter 只有在配置对应依赖时才激活。Python facade、
同步/异步 HTTP、可信本地 MCP 与 TypeScript SDK parity 均包含 provider transition，但尚未
捆绑具体 remote-provider adapter。completion-provider integration、自动 background sweep/
lease fencing、shared-service worker 与其余 transport/crash matrix 仍属于 F3。PostgreSQL
provider crash probe 已加入，但当前机器未运行；不声明 remote exactly-once。当前 adapter 仍仅显式
opt-in，且不会改变 `persistence_model="authority_graph"` 或
`full_persistence=false`。
