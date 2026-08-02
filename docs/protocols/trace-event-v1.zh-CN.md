# 有序 TraceEvent 协议 v1

[English](trace-event-v1.md) | **简体中文**

## 状态与边界

F3-01 引入有序工程 `TraceEvent`，不再把最终兼容 `Trace` aggregate 当成唯一证据。
本协议是现有 `tbm.event.v1` canonical envelope 与
`tbm.event-ledger-port.v1` 上的 opt-in typed adapter；它不会新增另一套 ledger、
数据库 schema 或事实来源。

兼容 `Trace` model、snapshot version 2、SQLite schema version 1、PostgreSQL
schema version 2、Agent/MCP/HTTP/SDK wire contract 与默认 runtime selection 均不变。
Codex/App Server hook ingestion 现在由独立的 opt-in
[Codex 摄取协议](codex-ingestion-v1.zh-CN.md)提供；它不改变这些默认选择，也不改变
F3-01 的历史计分。`full_persistence` 仍为 `false`。

## Typed event family

`tbm.trace-event.v1` 密封以下 version 1 event type：

- session started / ended；
- user prompt submitted；
- tool started、permission recorded 与 tool completed；
- subagent started / stopped；
- pre-compact 与 stop；
- diff observed；
- final response recorded。

canonical event ledger 可以保留未知 event type/version，但 sealed TraceEvent registry
不得消费它们。每个 payload 都使用精确的 `additionalProperties: false` schema。

## 冻结的 payload 语义

每个 TraceEvent payload 绑定：

- `trace_id` 与 `run_id`；
- 等于 canonical event `stream_version` 的正整数 `sequence`；
- 与 envelope timestamp 精确相等的 canonical UTC RFC 3339 `occurred_at`；
- 排序、唯一且与 envelope content-addressed `EventArtifactRef` descriptor 精确相等的
  `artifact_ids`；
- typed tool correlation 或 `null`；
- typed permission result 或 `null`；
- 显式 root/subagent lineage；
- 仅 subagent start/stop event 可携带的 related subagent identity。

stream ID 由 `trace_id` 稳定 SHA-256 派生。sequence 必须从 expected stream head 连续。
canonical stream parent 保留 previous event hash；causation 指向同 stream 前一事件，或
subagent stream 第一条事件的精确 parent event。单个 Trace stream 的 occurrence timestamp
不得倒退；event 与 permission decision 都不得晚于可信 `recorded_at`。

## Tool 与 permission evidence

tool correlation 包含有界 `tool_call_id`、tool name、精确 phase、invocation content
digest 与可选 parent tool call。event type 固定 phase：tool-started 是 request，
permission-recorded 是 permission，tool-completed 是 result。原始 prompt、tool input、
output 与 final response 不得进入 ledger metadata；受保护或大型 bytes 必须留在 Artifact
Authority，只通过 descriptor 引用。

permission-recorded event 包含 permission、decision identity、
`allowed`/`denied`/`unknown` status、有界 reason code、精确 decision time，以及
request/policy digest。`null` 表示没有核验或记录 permission result，不能合成成 denial。
immutable v3 `AuthorizationDecision` helper 会复制其精确 content-addressed evidence。
canonical envelope 的 `authorization_decision_id` 用于授权 ledger append，不得被静默
解释为被观察到的 tool permission result。

## Parent 与 subagent lineage

root lineage 不得有 parent 或 subagent identity。subagent lineage 必须包含 subagent ID、
parent Trace ID 与精确 parent event ID。跨 stream verifier 要求 parent 是同 scope 的
`subagent_started` event，且 related subagent ID 相同；self-parent、orphan、cross-scope
与时间倒退 lineage 都会被拒绝。

lineage、correlation 与 causation 只用于 provenance，绝不授予、继承或替代授权。

## Bounded append

`build_trace_event_append_request()` 只接受 1 至 100 个 draft 的精确 tuple。同一 batch
必须共享 Trace、run 与 lineage，sequence 必须连续；canonical command/idempotency
digest 会绑定全部 draft 与可信 recorded time。`append_trace_event_batch()` 委托给现有
access-bound event ledger port，由它原子提交完整 batch 或保持零修改，并在精确 retry 时
返回原 receipt。timestamp、payload、descriptor、scope、expected head 或 idempotency
command 任一变化都不是精确 retry。

SQLite integration test 还会证明 caller transaction rollback 会移除完整 TraceEvent
batch，且 retry 返回 byte-identical receipt。SQLite/PostgreSQL ledger atomicity 与跨后端
parity 仍归 F1 所有；F3-01 不重复计分，也不新增 schema。

## Qualification

可执行契约覆盖 F3-01 固定的全部八项要求：

1. 连续 sequence 与精确 stream parent；
2. sealed versioned event type；
3. 精确 canonical timestamp binding；
4. descriptor-only Artifact reference；
5. tool-call correlation 与 invocation digest；
6. 显式 permission result 与未检查状态的区别；
7. 精确 parent/subagent provenance 与同 scope 核验；
8. 通过现有 ledger port 完成有界、原子、幂等 batch append。

canonical schema、registry catalog、packaged copy、public export 与聚焦正/负向测试必须
保持 byte-aligned。
