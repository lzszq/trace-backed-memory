# Outcome 与 harm 事件 v1

状态：基于共用 event-ledger port 的 opt-in F4-07 projection。它尚未进入默认
Agent、MCP、HTTP、SDK 或 Store 路径。

## 契约

`tbm.outcome-harm-event.v1` 消费现有
`tbm.execution.run_outcome_recorded` 与
`tbm.execution.outcome_attribution_recorded` 事件，并新增 repository 授权事件
`tbm.outcome.evaluation_context_bound`。该事件把测量结果与精确 GateSession、
Trace、run、usage decision、replay manifest、retrieval snapshot、injection
artifact、memory revision、evaluation case 及可选实验 cohort 绑定。

context 事件保留精确 authorization policy、request、decision 与 attestation
verifier 身份。重放会重新计算授权决定，要求 `memory:verify`，绑定已认证的
principal/client 与精确 tenant/repository，拒绝晚于事件的决定，并要求 verifier
位于 reducer configuration 的可信列表中。

## Projection

确定性 reducer 输出：

- 明确保持非因果语义的逐 revision observed association；
- 显式 with-Memory 与 without-Memory 实验 cohort；
- 由 v3 attribution 契约独立验证的 causal claim；
- 达到 content-addressed 整数阈值策略时的 harmful-memory signal；
- 与 signal 精确关联的只读 suspension recommendation。

它同时区分 evaluated 与 unevaluated outcome。没有精确 evaluation context 的
attribution 仍保留为证据，但不能进入 association、causal、harm 或 suspension
视图。controlled-experiment causal claim 必须有匹配的 with-Memory cohort；
without-Memory cohort 不得携带 memory attribution。

## 持久化与重放

SQLite 与 PostgreSQL 复用现有 `EventLedgerPort`，本协议不新增数据库表。重建
冻结 partition-local global event watermark，要求四种 classification 的完整视图，
使用精确 forward cursor，核验每个 source stream，再次扫描同一 watermark，并把
snapshot 绑定到 reducer code/configuration hash 与 source-event count。Reducer
state 保存 canonical JSON 字符串而非浮点数；confidence 阈值使用百万分整数。

## 边界

signal 与 recommendation 绝不直接修改 MemoryCatalog 或 activated head。实际
suspension 仍必须是独立授权的 MemoryCatalog command。F5 migration、shadow
comparison、默认 projection cutover 与 compatibility authority 退役仍是独立工作。

另见 [Outcome v3](outcome-v3.zh-CN.md)、
[Outcome/Effect 事件 v1](outcome-effect-events-v1.zh-CN.md) 与
[Retrieval index 事件 v1](retrieval-index-events-v1.zh-CN.md)。
