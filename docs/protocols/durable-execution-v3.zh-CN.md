# Durable execution v3

[English](durable-execution-v3.md) | **简体中文**

`DurableExecutionService` 是 durable runtime 后半段的 opt-in 应用组合。它会核验已保留的
finalization bundle，推进 `FINALIZED -> EXECUTING`，支持显式的 lease resume 或
abandonment，并通过现有原子 authority 完成
`RunOutcome + COMPLETED + completion outbox`。构造时要求该 authority 暴露 start、
resume 与 abandonment 所用的同一个精确 GateSession authority；第二个数据库或
repository 实例会被拒绝。

## 授权与所有权

每个操作都接收由可信 transport authenticator 产生的
`AuthenticatedServiceContext`。start 与 resume 同时要求：

- 已保留 UsageDecision 引用的原始 `memory:retrieve` scope；以及
- 同一 principal、agent client、tenant、repository 与 environment 当前有效的
  `gate_session:transition` scope。

abandonment 与 completion 要求当前 transition scope，并在读取或修改 session
之前再次核验。因此 policy 或 registry 轮换会使旧 scope 失效；一条 allowed decision
不是 bearer capability。

服务返回的 transition authorization event ID 表示本次调用实际核验的授权。
GateSession v3 尚未把该 event ID 存进 immutable revision，因此不能把它描述为持久化的
transition-event linkage。authorization decision 仍会由独立 authority 持久保存。

## 精确 start 与 resume

`start()` 只接受 session ID 与预期 `FINALIZED` revision。它通过
`DurableFinalizationService.replay()` 重新加载并核验精确 UsageDecision、
InjectionArtifact、完整 replay manifest 与 injection 字节；既不重新渲染，也不调用模型。
完成这些核验后，服务才会通过 CAS 把 session 推进为 `EXECUTING`。

execution transition 继承 finalization lease，不能静默替换 lease。live exact retry
会返回同一份保留 snippet 与 executing revision。精确 terminal transition 之后的 retry
不再返回 snippet，并设置 `execution_required=false`，避免 completed 或 abandoned run
通过该结果再次执行。

`resume()` 是显式 crash-recovery 路径。它要求精确的当前 executing revision，再次核验
同一份保留 injection，并在返回 snippet 前通过 CAS 续租。外部执行无法与数据库形成同一
transaction；executor 必须用 GateSession 稳定的 `run_id` 作为 idempotency key，也不能把
stale lease 解释成重复副作用的许可。

## completion 与 evaluator 认证

completion 接受现有有界 `GateCompletionRequest`。服务要求精确注册的
`OutcomeEvaluatorAuthenticator` 在每次调用时执行。这个服务端持有的可信 boundary
必须核验实时 transport proof，并返回当前 `TrustedOutcomeEvaluator` registration。
返回的 evaluator、authenticator、credential、status 与 version 必须同 authenticated
context 和 request 全部匹配。ID 只是 provenance，不是 authentication proof；
只有调用方提交的 `evaluator_id` 或 credential 文本并不足够。每次调用都通过
authenticator 重新加载，确保 revoke 与 credential rotation 无需重建服务即可生效。

随后 completion authority 执行一次原子写入：

1. 从 executing session 与测量结果派生并插入 content-addressed RunOutcome；
2. 通过 CAS 以精确 outcome ID 推进 `EXECUTING -> COMPLETED`；
3. 追加 immutable completion event 与初始 delivery revision；
4. 在 evaluator、RunOutcome 与 completed-session event 之后追加 canonical
   `EffectRequested`；
5. 读回并核验 session、outcome、event、effect 与当前 delivery。

exact retry 返回已保留 outcome 与 event，不会创建第二个 event，但
`expected_version` 必须仍精确指向已保留 completed revision 的 parent。delivery 仍采用
at-least-once 语义：consumer 按 event ID 去重，有界 outbox worker 负责 lease、retry、
acknowledgement 与 dead-letter transition。
每次成功 delivery transition 都会把 canonical effect event batch 与 outbox revision
原子追加。`EffectSucceeded` 只代表本地 callback acknowledgement。存储中立 provider
receipt/reconciliation 基础已单独交付，并提供 opt-in server-owned invocation。trusted
reconciliation、owner fencing 与有界 retry/dead-letter 只有在配置对应依赖时才激活；generic
receipt-backed compensation 只适用于支持它的 contract，Semantic provider effect 不支持。
completion-provider integration、自动 sweep/lease fencing 与 shared-service worker 不属于
本 F2 slice。

## abandonment 与恢复

`abandon()` 要求精确、live 的 executing revision 与有界 terminal reason。它通过 CAS
追加 `ABANDONED`、读回记录，并支持 exact retry。execution lease 过期后不会被静默
abandon 或 complete；现有 due scan 会报告 `recovery_required`，由 operator 或未来的
policy-specific recovery authority 决定下一步。

## transaction 与集成边界

SQLite 与 PostgreSQL 复用同一个 storage-neutral 组合。completion-outbox repository
会暴露其内嵌 GateSession authority，`DurableExecutionService` 要求调用方使用这个精确
对象。其 savepoint 会为每一个独立 start 或 completion 操作保留由调用方控制的外层
commit/rollback。任何数据库 transaction 都无法包含外部 executor side effect，因此
execution 与 completion 之间发生 crash 时会留下显式 `EXECUTING` recovery state。

该服务为 opt-in。`AuthenticatedDurableAgentMemory` 现在会把它作为共享 durable
应用 facade 的 execution 阶段调用。默认 Store、LocalAgentMemory、兼容 STDIO
MCP/HTTP adapter 与普通 CLI 不构造该 facade；显式 durable HTTP/MCP profile 会构造，
Python/TypeScript durable client 会选择 HTTP。v1 process-local request-token
contract 保持不变。durable Agent 会在该 execution service 保留 bundle 后提供
session-bound replay-read authorization；显式 durable HTTP/MCP 与
Python/TypeScript client 会在启动 content policy 下选择该边界。受保护内容加密、retention、
transport-authenticated replay 暴露、默认 adapter cutover 以及持久化
transition-authorization linkage field 仍是独立的生产工作。详见
[已认证 durable Agent v3](durable-agent-v3.zh-CN.md)。
