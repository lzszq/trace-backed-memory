# 已认证 Semantic Gate 服务 v3

[English](semantic-gate-service-v3.md) | **简体中文**

`AuthenticatedSemanticGateService` 是单次已认证 Semantic Gate provider 调用的
存储中立服务边界。它把已有不可变 RetrievalSnapshot/SystemGateEvaluation evidence
与 SQLite 或 PostgreSQL Semantic Gate artifact authority 组合起来；它不定义新的
存储 schema 或 wire format。

## 可信输入

可信 bootstrap 代码持有 `TrustedSemanticProvider` 与
`SemanticGateServiceConfiguration`，由它们选择 provider、authenticator、credential
标识、model/version、endpoint、prompt-template version、generation-config digest、
media type、classification 与 redaction policy。credential 标识只是非敏感 metadata；
credential 和 token 绝不能写入这些记录。

transport authenticator 生成 `AuthenticatedSemanticProviderContext`。服务必须先确认
provider/authenticator/credential 与可信 registration 完全一致，之后才能加载
evidence、读取 retry chain、采样时间或调用 provider。调用方只提供 System Gate
evaluation ID、精确且有界的 UTF-8 prompt 字节，以及预期的 durable parent attempt ID。

## 调用顺序

每次调用按以下顺序执行：

1. 用可信 registration 认证 provider context；
2. 加载并交叉核验精确 System Gate evaluation 与 retrieval snapshot；
3. 读取并完整核验 durable attempt chain，并在 provider 工作前拒绝 stale expected
   parent；
4. 在 provider callback 前后立即采样可信 service clock，并派生有界 latency；
5. 根据服务端持有的 provenance 构造内容寻址 attempt 与精确 prompt/response role
   binding；retry 字节相同时复用已有不可变 descriptor；
6. 通过配置的 artifact authority 原子追加，并要求精确 durable read-back。

provider callback 只能收到 `SemanticProviderCall`，并可返回
`SemanticProviderResult`；它不能选择 provider/model/template/config identity、
timestamp、sequence 或 parent。已有跨记录核验仍强制结果覆盖全部 candidate，且永远
不能重新打开 System Gate block。

## 失败与 retry 语义

`SemanticProviderCallError` 只接受封闭稳定 taxonomy：
`provider_authentication_failed`、`provider_content_rejected`、
`provider_rate_limited`、`provider_response_invalid`、`provider_timeout`、
`provider_unavailable` 或 `provider_error`，并可附带 provider request/token metadata。
其他 provider exception 一律归一化为 `provider_error`；原始异常消息既不
持久化，也不由 service error 暴露。服务会保存只有 prompt 的 failed attempt，随后以
`SemanticProviderInvocationFailedError` 返回精确 durable result。

clock 非法、evidence 缺失、parent stale、provider result 非法、存储冲突或 read-back
不一致都会 fail closed。并发调用可能在读取同一个 parent 后都到达外部 provider，但
authority CAS 最多接受一个 append；被拒绝的调用不会静默重试。retry 必须重新加载当前
head，并显式指定该 parent。

## 剩余边界

当前 artifact authority 只保存 `public` 或 `internal` 明文，因此在静态加密 provider
交付前，服务会拒绝敏感 classification。authenticator/credential identity 会在进程内
核验，但还不是有签名的 durable attestation。这个 single-call service 本身仍不持有
GateSession transition；opt-in
[`AuthenticatedSemanticGateSessionService`](durable-semantic-gate-v3.zh-CN.md)
会把它组合推进到 `DECIDED`，更高层 durable facade 再通过显式 durable HTTP/MCP
与 Python/TypeScript client 暴露该组合。受保护内容 retention/access-control
policy、外部 checkpoint 与默认兼容路径 cutover 仍是独立后续工作。active
snapshot-v2、SQLite-v1 与 PostgreSQL-v2 兼容边界保持不变。
