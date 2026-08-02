# 外部 Effect Receipt 协议 v1

[English](effect-receipt-v1.md) | **简体中文**

`tbm.effect-receipt.v1` 是 F3-04 的 storage-neutral 外部副作用契约。它把
请求意图、可信授权关联、每次 provider attempt、provider request ID、精确
receipt 证据、结果未知、有限重试、死信以及补偿记录为有序 canonical event
stream。

协议不声称数据库事务能够原子覆盖远端 provider 调用。远端调用仍在 ledger
事务之外；协议的职责是把这条边界变成显式、可恢复、可重放的事实。

## Event registry

密封 registry 包含：

- `tbm.effect.requested`
- `tbm.effect.authorized`
- `tbm.effect.started`
- `tbm.effect.provider_request_recorded`
- `tbm.effect.receipt_recorded`
- `tbm.effect.succeeded`
- `tbm.effect.result_unknown`
- `tbm.effect.failed`
- `tbm.effect.retry_scheduled`
- `tbm.effect.dead_lettered`
- `tbm.effect.compensation_requested`
- `tbm.effect.compensated`

生成的 payload dispatch schema 与 registry catalog 分别作为
`schemas/effect_receipt_payload_registry_v1.schema.json` 和
`examples/effect_receipt_type_registry_v1.example.json` 打包。

## Effect contract 与信任边界

每个 stream 保持一个不可变 `EffectContract`：

- 稳定的 `effect_id`、`effect_type`，以及由完整不可变 intent 确定性派生的幂等键；
- 发起 effect 的 canonical event；
- 精确 input Artifact digest；
- 由可信 adapter context 选择的 authorization event；
- 是否显式支持 compensation；
- 有界最大 attempt 数。

`EffectContract.authorization_event_id` 必须与 access-bound
`EventLedgerPort` 的 authorization decision 完全相同。外部请求 JSON 无权选择
tenant、repository、principal、actor、authorization decision 或 ledger
partition。`TrustedEffectProvider` 同样来自服务端组合；当前没有 durable wire
允许 caller 自选 provider identity 或 receipt authority。接入服务必须先验证所
引用的 authorization decision 和 provider registration，再调用本协议。

原始 input 和 provider receipt 字节绝不进入 ledger metadata。请求与 receipt
event 只携带精确 `EventArtifactRef` descriptor；其他 event 不携带字节内容。
Canonical event 的边界、严格 schema、重复 key 拒绝和 secret-key 拒绝继续生效。

## Attempt 与 provider identity

只有 authorization 完成或 retry 已调度后才能开始 attempt。Attempt ID 与
canonical provider-request digest 由不可变 effect contract、attempt number 和
可信 provider registration 确定性派生。Attempt number 必须连续且不得超过
`max_attempts`。

在完整 reducer input 内，每个已接受的 provider request ID 必须唯一绑定 provider、
effect、attempt 与 canonical request digest。同一个 ID 绑定不同请求时 fail closed。
未来 active multi-stream writer 必须用原子 projection 或 authority 强制相同唯一性；当前
per-effect append helper 不声称提供跨 stream 写入保证。这为 reconciliation 与去重提供
证据，但不构成 exactly-once 声明。

## Receipt 与 unknown-result 规则

成功必须包含两个 event：

1. `EffectReceiptRecorded` 绑定 provider request ID、canonical request、result
   digest 和 available receipt Artifact；
2. `EffectSucceeded`（或 `EffectCompensated`）精确重复已经持久化的 provider
   request、receipt 与 result digest。

Timeout、response 丢失、进程中断或 acknowledgement 不确定都记录成
`EffectResultUnknown`，绝不能视为失败。Unknown 状态不能直接 retry、dead-letter
或 compensation；reconciliation 必须先把它解析成精确 receipt，或带精确、available
reconciliation Artifact 的 `reconciled_absent` failure，并且不得凭空新增或改变 provider
request ID。

已知 failure 区分 `pre_send`、`provider_rejected` 和
`reconciled_absent`。只有可重试的已知失败且仍有预算，才能产生
`EffectRetryScheduled`。Dead-letter 是终态证据，但不能证明 provider 没有执行
副作用。

## Compensation

Compensation 是独立的 child effect stream。其首个 event 精确绑定 parent 的
successful event hash 与 provider receipt digest，且 parent 必须事先声明支持
补偿。Child 有自己的 authorization、attempt、provider request ID 和 receipt；
已有 parent event 永不修改。

## Ledger 与 reducer 行为

`build_effect_receipt_batch()` 生成 content-addressed canonical events 和一个
ledger idempotency binding。`append_effect_receipt_batch()` 通过 access-bound
ledger 读取有界历史、原子追加，并验证精确 ledger receipt。完全相同的 command
重放返回原 receipt；改变后的 command 冲突。

`reduce_effect_receipt_events()` 从全局有序 event 重建 lifecycle projection，
并拒绝不完整 stream 历史、跨 partition 输入、contract 漂移、无效 parent、重复
provider-request 绑定、receipt mismatch、unknown 状态盲重试以及不合格 parent
的 compensation。

## 当前边界

本协议是 opt-in、storage-neutral 能力。它不替换 F2-05
`outcome_effect_event_v1` compatibility projection 或现有 completion outbox
worker。特别是 legacy `response_sha256` 仍只是 response digest，不能升级为
provider receipt。Default Agent、MCP、HTTP、SDK 与 opt-in Codex 摄取 adapter 都不会选择
本 effect 协议；这仍属于后续阶段集成 gate。跨 effect provider-request 唯一性会在完整
全局 replay 中验证；shared concurrent dispatch 启用前，active writer 仍需增加原子
uniqueness projection 或 authority。
