# 认证 durable Gate preparation

[English](authenticated-gate-service-v3.md) | **简体中文**

`AuthenticatedGateSessionService` 把认证 retrieval 边界与 SQLite 或 PostgreSQL
GateSession repository 组合起来。它在 retrieval 前建立 durable session identity，
并且只在可信 version-3 evidence 验证后推进 session。

## 顺序与幂等

每个请求按以下顺序执行：

1. 完成认证授权、durable decision 追加/读回、registry 轮换检查与 environment
   binding；
2. 只使用授权后的 canonical tenant、repository、principal 与 client 创建或解析
   scoped、idempotent `CREATED` GateSession；
3. 从 durable storage 读回并核对 create receipt；
4. 已有精确 `PREPARED` 或更后状态时只做 replay、不重复执行 preparation
   callback；已有中断的精确 `CREATED` session 则仍可恢复；
5. 只有新的或恢复的 `CREATED` revision 已存在后才调用 preparation；
6. 要求 `PreparedGateEvidence`，并由可信 evidence verifier 核验引用的
   RetrievalSnapshot 与 SystemGateEvaluation；
7. 通过带 version 检查的 repository transition 发布带 lease 的 `PREPARED`，并
   验证全部 immutable session field 均未改变。

callback 只收到 `AuthorizedRetrievalScope` 与公开 immutable GateSession，绝不会
收到或持久化私有 Store token。

durable `CREATED` session 可恢复，但不算 replay-complete。后续 attempt 必须先完成
新的授权，复用精确 idempotency/session identity，再继续 preparation。durable
`PREPARED` session 已完成 replay：durable retrieval 组合会返回已保留的
snapshot/evaluation 与精确 response，不重复 discovery、evidence write 或 callback。

## 失败与恢复

preparation 或 evidence verification 失败时，会尝试使用 version check 执行
`CREATED -> CANCELED` 补偿，并写入有界 reason `prepare_failed`。精确 canceled
receipt 产生 `GatePreparationFailedError`。并发 revision、异常 transition receipt
或补偿失败会产生 `GatePreparationRecoveryRequiredError`，其中携带最后一次可读取
的 durable session；coordinator 绝不重建进程内 request token。

中断 `CREATED` session 的恢复只能在新的授权完成后开始。中断 attempt 已提交的
evidence 保持不可变；如果 preparation 在新的 authorization decision 下再次执行，最终
`PREPARED` revision 只关联新核验的 evidence，不会改写 orphan。

这是有顺序的补偿，不是跨 authorization/GateSession authority 的事务。默认
compatibility Agent/MCP 尚未产生所需 RetrievalSnapshot 与 SystemGateEvaluation
record。opt-in 下游服务现已提供 durable retrieval preparation、
`AWAITING_DECISION`/`DECIDED`、有界 expiry recovery、event-first finalization、
execution 与 completion authority。完整 transport/crash conformance 与 shared-service
cross-process operation 仍是后续 slice。
