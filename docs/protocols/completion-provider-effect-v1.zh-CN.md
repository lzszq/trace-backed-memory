# Completion Provider Effect v1

[English](completion-provider-effect-v1.md) | **简体中文**

`tbm.completion-provider-effect.v1` 是 opt-in、存储中立的 consumer bridge：它把一条
已领取的 `tbm.completion-outbox-event.v3` delivery 连接到
`tbm.effect-event.v1` 已定义的 provider-transition evidence。该 callable 可作为
`DurableRuntimeDependencies.completion_consumer` 显式注入；runtime 与 local daemon
默认不会构造它。

## 可信输入

bridge 构造时需要：

- 一份 `EventLedgerAtomicAppendPort`，其 authenticated actor 必须等于精确 delivery
  `worker_id`；
- 一份服务端持有的 `TrustedProviderEffectRegistration`；
- 一个可信 provider callback；
- 一个可信时钟；以及
- 可选的可信只读 reconciliation callback。

调用方 JSON 不能选择 worker、provider、endpoint、authorization 或 reconciler。已保留
`EffectRequested` 必须嵌入精确 completion event、不可变 event-ID 幂等键、RunOutcome
descriptor digest 与 `compensation_supported=false`；当前 outbox delivery 必须是由该
ledger worker 持有且尚未过期的 lease。

`CompletionProviderCall` 只携带 provider registration、不可变 completion descriptor，
并把 completion event ID 作为 provider 幂等键。`CompletionProviderResult` 只携带有界
provider request ID 与 response SHA-256。raw response body、credential 与 provider error
不会被接收；adapter failure 只能使用固定、已净化的 error-code allowlist。

## Append 与 fencing 顺序

对一条新 delivery，bridge 会：

1. 核验精确 completion request 与 active worker lease；
2. 在调用 provider 前追加 `attempt_started`；
3. 用稳定 completion event ID 调用 provider；
4. provider request ID 可用时追加 `request_submitted`；
5. 在向 completion worker 返回 response digest 前追加内容派生 provider receipt。

每条新 provider transition 都同时绑定 append 前观察到的精确 delivery revision 与精确
effect-stream head。已保留 transition 的 exact replay 仍然允许；lease reclaim、delivery
transition 或其他 stream append 会让旧 fence 失效，因此迟到 owner 不能直接追加 receipt。

provider callback 位于数据库 transaction 之外。delivery 仍是 at least once；remote
exactly-once 取决于 provider 是否执行稳定幂等键。本地 `EffectSucceeded` 仍只表示
completion worker acknowledgement，provider receipt 是另一份独立 evidence。

## 恢复

恢复只能单调推进：

| Provider 状态 | 动作 |
|---|---|
| `not_started` | 原子追加第一条 `attempt_started`，随后调用 provider |
| 同一 delivery revision 下的 `in_flight` / `submitted` | 返回 recovery-required；不调用 provider，也不 reconciliation |
| 后续有效 lease 下的 `in_flight` / `submitted` | 先用 `owner_fenced` 追加 `result_unknown`，再 reconciliation |
| `unknown` | 只调用可信 reconciler |
| `not_found` | 追加 `retry_scheduled`，由后续有界 outbox delivery 启动下一 attempt |
| `retry_wait` | 只有达到已保留 `retry_at` 后才启动下一 attempt |
| `succeeded` | 精确重放已保留 response digest，不再次调用 provider |
| `dead_lettered` | 返回稳定 dead-letter error |

`confirmed` reconciliation 必须与任何已保留 provider request ID、response digest 与
receipt 一致；`still_unknown` 继续 recovery-required；只有 `not_found` 允许 retry。
completion retry/dead-letter 上限仍由 durable outbox delivery chain 持有；本 bridge
永远不支持 completion compensation。

## 边界

bridge 复用 generic SQLite/PostgreSQL event-ledger port，不新增 SQL authority、schema、
migration 或 packaged resource。聚焦 SQLite 测试覆盖 receipt-before-ack response loss、
unknown confirmation、not-found retry、late-owner fencing、稳定错误净化与 exact receipt
replay；generic provider-ledger 与 completion-outbox suite 继续覆盖 SQLite/PostgreSQL
authority parity。

当前不捆绑具体 remote adapter、credential loader、自动 provider sweep、shared-service
worker 或默认 runtime/daemon wiring。该 bridge 的 PostgreSQL hard-crash execution 与其余
F3 crash matrix 仍未完成。能力保持 opt-in，不改变
`persistence_model="authority_graph"` 或 `full_persistence=false`。
