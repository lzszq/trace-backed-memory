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

`verify_authorized_scope()` 是已有 scope 的组合专用读取路径。它会重新加载 durable
decision，要求该 decision 对精确 permission 与当前 policy 仍为 allowed，再从当前
registry 和 authenticated context 重建 scope，并拒绝任何不一致。它不会追加第二条
decision，也不会把 scope 变成可由调用方重用的 capability。

`recover_authorized_scope()` 是对应的 durable continuation 路径。可信 service
代码只能提供另一条已核验记录中保留的 authorization event ID。service 会重新加载
decision 与当前 registry，要求当前 repository reference 仍解析到同一 canonical
repository，再返回重建后的服务端 scope。未知或已轮换 decision、identity 改变、
repository target 改变、permission 不一致以及 environment 被禁用都会 fail closed。
调用方 JSON 绝不能选择此 event ID。

## 当前集成边界

`AuthenticatedLocalAgentMemory` 是可选启用的本地应用集成。它包装一个精确的
`LocalAgentMemory` 实例，并让 `prepare` 先经过此授权边界。
`AuthenticatedAgentPrepareContext` 有意不提供 principal、client、tenant、
repository 或 environment 字段。调用方传入的 Trace 仍有旧版 `repo`/`tenant`
字段，但该门面会忽略并覆盖两者。授权通过后，门面把
`AuthorizedRetrievalScope` 中的 canonical tenant/repository 同时绑定到 Trace 与
`MemoryContext`；拒绝或授权持久化失败发生在注册 Trace 之前。私有 ownership
索引把 request/decision handle 绑定到执行 prepare 的门面；即使两个鉴权门面共享
runtime，另一个门面也不能 finalize、complete 或 cancel。这些索引仍是进程内状态，
不是 durable session。

该可选门面不负责 transport authentication。`tbm-mcp` 可通过 all-or-none 的可信
本地 `--auth-*` 启动 profile 选择它；MCP 请求 JSON 仍不能提供 identity 或 target
字段。普通 CLI operation、HTTP 与 SDK adapter 尚未选择它。可信 bootstrap 代码仍
必须派生固定的 `AuthenticatedServiceContext`。Durable GateSession、
RetrievalSnapshot 挂接可通过独立的 opt-in durable preparation bridge 使用。
`AuthenticatedDurableAgentMemory` 现在会通过已保留 RetrievalSnapshot linkage 与
`recover_authorized_scope()` 组合 durable preparation、Semantic Gate、
finalization、execution、cancellation 与 completion，而不接受调用方 scope。它仍是
adapter-neutral opt-in facade；audit actor linkage、原子的跨记录 service
transaction、transport authentication 以及 MCP/HTTP/SDK wiring 仍是独立后续交付。
详见 [已认证 durable Agent v3](durable-agent-v3.zh-CN.md)。
