# 认证 retrieval service 边界

[English](authenticated-service-v3.md) | **简体中文**

`AuthenticatedRetrievalService` 是首个在调用 retrieval 前应用 version-3 实体
注册表与授权契约的 active orchestration 边界。它与存储实现无关，通过
`AuthorizationDecisionWriter` 接入 SQLite 或 PostgreSQL authorization
authority。

## 信任边界

`AuthenticatedServiceContext` 必须由可信 service 代码在 transport
authentication 完成后构造。它包含精确的 `PrincipalIdentity`、
`AgentClientIdentity`，以及服务端持有的 tenant、repository reference 与
environment。本模块不验证 OAuth token、签名、操作系统凭据或调用方 JSON；
context object 也不是可重用 capability。

每次受保护的 retrieval 都按以下顺序执行：

1. 加载当前 immutable `EntityRegistrySnapshot`；
2. 若 principal/client identity 已存在，则要求认证记录与当前 registry 逐字段
   完全一致；
3. 使用服务端 clock 与 request-ID factory 创建 `memory:retrieve` request；
4. 对精确的当前 authorization policy 求值；
5. 追加 allow 或 deny decision，并从 authority 读回完全相同的 decision；
6. deny 或持久化失败时立即停止；
7. 重新加载 registry，授权期间发生任何 content-hash 变化都拒绝；
8. 要求 active environment 与同一 tenant、canonical repository 绑定；
9. 只有此后才以 `AuthorizedRetrievalScope` 调用 retrieval callback。

返回 scope 带有 durable `authorization_event_id`、canonical repository ID、
environment ID、principal、client 与 tenant。稳定 service error 会清洗 registry、
persistence、clock、request factory 与 retrieval callback failure。

## 当前集成边界

该边界是可复用 kernel module，尚未接入 `LocalAgentMemory`、`tbm-mcp`、CLI、
HTTP 或 SDK。transport 仍需 authenticator 派生精确 service context，不得从请求
JSON 接受 identity ID。Durable GateSession、RetrievalSnapshot、audit actor
linkage、expiry/recovery worker 与原子的跨记录 service transaction 仍是独立后续
交付。
