# Durable retrieval preparation v3

[English](durable-retrieval-preparation-v3.md) | **简体中文**

`DurableRetrievalPreparationService` 是可选启用的组合边界，用于把已认证 retrieval
preparation 挂接到一个 durable GateSession。它复用现有 authorization、retrieval
preparation、Gate evidence 与 GateSession authority；不会改变 active snapshot-v2
Store 或默认 Agent/MCP lifecycle。

## 请求身份

`DurableRetrievalPreparationRequest` 绑定公开 retrieval request、Trace/run identity、
服务端匹配的 retrieval context、mode、retriever version、top-K、idempotency key、
expiry 与 lease。服务端派生的 `request_fingerprint` 还会绑定 raw-query digest 和
任何 semantic provider/version/vector evidence。raw query 字节只是有界输入，
不会进入 fingerprint payload、对象表示、GateSession 或 RetrievalSnapshot。

Gate 与 retrieval service 必须共享同一个 `AuthenticatedRetrievalService` 实例。
这能阻止可信组合路径静默改用另一份授权 scope，或记录第二条 authorization
decision。

## 准备顺序

对于新的 idempotency key，服务会：

1. 通过 `AuthenticatedGateSessionService` 只授权一次；
2. 创建并读回 scoped `CREATED` GateSession；
3. 使用该精确 durable `session_id` 重建 retrieval request；
4. 在已经授权的 scope 内运行 `AuthenticatedRetrievalPreparationService`；
5. 通过配置的 Gate evidence authority 保存精确
   RetrievalSnapshot/SystemGateEvaluation 记录对；
6. 校验 storage receipt，并通过 `DurablePreparedGateEvidenceVerifier` 读回、核验
   两条记录；以及
7. 由 Gate service 通过 CAS 发布并读回 `PREPARED`。

精确 replay 会返回已有 durable session，不会重复 authorization-side discovery、
revision read、evidence generation 或 evidence write。
`prepare_for_authorized_scope()` 是可信内部组合 hook：它不会追加第二条 decision，
但会重新加载并核验已有 allowed decision、当前 policy、identity、environment 与
精确 scope。绝不能把它直接暴露给 MCP、HTTP、CLI、SDK 或 caller-owned callback。

## 失败与事务边界

preparation、evidence storage、receipt、读回或核验失败都会被清洗，并交由 Gate
service 执行带版本检查的 `CREATED -> CANCELED` 补偿。并发或异常 durable state
会报告 recovery required。

默认组合是跨 authority 的有序补偿，不是一个 atomic distributed transaction。
如果 evidence 已持久化，而随后的 GateSession transition 失败，不可变 evidence
可能作为 orphan 保留，同时 session 会被取消。如果 SQLite 或 PostgreSQL
GateSession 与 Gate evidence repository 明确共享同一个 caller-owned connection，
调用方外层 transaction 可以一起回滚两个 authority；这是显式部署选择，不是服务
自动保证。

Semantic Gate、rendering、injection、后续 GateSession transition、outcome
completion、生产 index worker/分片，以及 active Agent/MCP/HTTP/SDK 接入仍是独立
后续工作。
