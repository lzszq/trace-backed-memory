# Outcome 与 Effect 事件 v1

[English](outcome-effect-events-v1.md) | **简体中文**

`outcome_effect_event_v1.py` 是 F2-05 的事件适配器与确定性投影边界，覆盖
已完成运行的 outcome 和外部 effect 交付。F2-06 已让 `tbmd local` 选择该边界；
F2-07 让独立 SQLite durable HTTP/MCP 及其 Python/TypeScript client 选择同一命令
协调器。独立 PostgreSQL 路径尚未完成相同切换。

## 事件流

每个精确的 `GateSession.session_id` 对应一条 `outcome_effect` 流。规范事件
类型为：

- `tbm.execution.run_outcome_recorded`；
- `tbm.execution.outcome_attribution_recorded`；
- `tbm.effect.requested`；
- `tbm.effect.started`；
- `tbm.effect.retry_scheduled`；
- `tbm.effect.succeeded`；
- `tbm.effect.dead_lettered`；
- `tbm.effect.compensation_requested`；
- `tbm.effect.compensated`。

密封事件注册表随
`examples/outcome_effect_event_type_registry_v1.example.json` 分发；dispatch
schema 为
`schemas/outcome_effect_event_payload_registry_v1.schema.json`。

## 六个确定性投影

模块提供带版本的纯 reducer，用于：

1. 不可变 `RunOutcome` 当前记录；
2. 有序、不可变的 `OutcomeAttribution` 集合；
3. `EffectQueue` 当前状态；
4. 只追加的 delivery history；
5. dead-letter 状态；
6. 显式 compensation 历史。

`hydrate_outcome_effect_views` 会重建现有 `RunOutcome`、
`OutcomeAttribution` 与 `CompletionOutboxDelivery` 领域记录，并核对内容 ID、
记录 digest、session 关联、连续 delivery version 和当前 head 相等性。

## Completion outbox 映射

现有 completion outbox 按其真实语义映射，不增加更强的交付承诺：

| Completion delivery | Effect 事件 | Queue 状态 |
|---|---|---|
| `pending` | `EffectRequested` | `ready` |
| `leased` | `EffectStarted` | `leased` |
| `retry_wait` | `EffectRetryScheduled` | `retry` |
| `delivered` | `EffectSucceeded` | `succeeded` |
| `dead_letter` | `EffectDeadLettered` | `dead_letter` |

该映射仍然是 at-least-once。当前 authority 若保存 response digest，投影会保留
它，但不会把它升级成 provider receipt 或 exactly-once 保证。完整的 provider
request/receipt 协议仍属于 F3-04。

## Compensation 边界

Compensation 是新的 effect，绝不改写旧事件。Compensation request 必须使用
不同的新 effect ID，引用一个已成功且显式声明
`compensation_supported=true` 的 effect，并针对完全相同的 request 完成。旧
completion outbox 没有声明 compensation 支持，因此不能把其 delivery 重新
标记为 compensated。

这个投影与 `RecoveryAction` 无关：运维恢复与外部 effect compensation 仍是
不同的证据域。

## 失败行为

以下情况一律失败关闭：事件父链破损、stream/session 不匹配、重复 outcome 或
attribution、delivery revision 不连续、非法 terminal transition、不支持的
compensation、digest 漂移或保留 JSON 畸形。Outcome association 绝不会被
提升为 causal attribution；现有 `OutcomeAttribution` 契约仍是权威来源。

## `tbmd local` event-first 事务

`tbmd local` 会显式用 `event_first_commands=true` 打开 SQLite runtime。每个
dispatcher 命令串行执行，并共享一个外层 `BEGIN IMMEDIATE` 事务。类型化请求与
可信上下文先完成校验，之后才允许写入；domain event batch 在相应旧 completion
或 delivery 投影之前追加，六个关键视图同步重建并核对，wire response 构造完成后
才提交事务。校验、追加、reducer、投影、读回或 response 构造任一失败都会回滚
整个命令。
如果 commit 失败，runtime 会重试 rollback；rollback 无法恢复时会使 SQLite connection
失效并关闭，后续命令不能复用语义不明的状态。BaseException 清理也必须释放进程锁。

完成命令会先追加 `RunOutcomeRecorded` 与初始 `EffectRequested`，再投影 completed
GateSession、RunOutcome 与 outbox 行。claim、retry、acknowledgement 与 dead-letter
转换会先追加并重建其精确 Effect 事件，再推进 delivery head。worker 不会在调用
外部 consumer 期间持有数据库事务：claim 与最终 acknowledgement/failure 仍是两个
短小的“事件加投影”同事务操作，因此保留现有 at-least-once 语义。

默认的编程式 SQLite factory 仍要求显式设置 `event_first_commands`。`tbmd local`、
独立 SQLite durable HTTP 与独立 SQLite durable MCP 会显式选择它；其他直接 Python
组合不会隐式改变行为。

## 当前边界

F2-05 提供 ledger-ready 事件草稿、密封注册表、确定性 batch 物化、六个 reducer
及现有视图的精确 hydration。F2-06 为 `tbmd local` 激活同事务路径；F2-07 把该
选择扩展到独立 SQLite durable HTTP/MCP、Python 同步/异步与 TypeScript，并用一份
已提交的事件序列和 projection digest golden 做一致性验收。兼容路径与独立
PostgreSQL durable transport 尚未切换。因此整体 `persistence_model` 仍为
`authority_graph`，`full_persistence` 仍为 `false`，直到整份计划的其余 gate 全部完成。
