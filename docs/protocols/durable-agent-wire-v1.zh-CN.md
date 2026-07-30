# Durable Agent wire 边界 v1

状态：可选 adapter 契约；当前 active HTTP、MCP、CLI 或 SDK transport 均未选择它。

## 目的

`tbm.durable-agent-wire.v1` 是 `AuthenticatedDurableAgentMemory` 之上的严格、
adapter-neutral 请求/响应边界。它映射完整 durable facade，但不改变
`tbm.agent.v1`、snapshot version 2、SQLite schema version 1 或 PostgreSQL
schema version 2。

dispatcher 不是 transport authenticator。嵌入它的 adapter 必须认证实时调用方，并在
request JSON 之外构造：

- `AuthenticatedServiceContext`；
- `decide` 所需的 `AuthenticatedSemanticProviderContext`；
- `complete` 所需的 `AuthenticatedOutcomeEvaluatorContext`。

adapter 还必须提供 server-owned canonical repository resolver 与可信 evaluator
resolver。startup selector、本地 bearer token 或调用方提供的 identity 字段都不能
替代 transport authentication。

可选 request-model 依赖的安装方式：

```bash
python -m pip install -e ".[service]"
```

## 操作

dispatcher 映射：

- `prepare`；
- `decide`；
- `finalize`；
- `start`；
- `resume`；
- `abandon`；
- `complete`；
- `cancel`；
- `get_session`；
- 可选 `export_replay`。

每个操作的成功响应均包含：

```json
{
  "protocol_version": "tbm.durable-agent-wire.v1",
  "operation": "get_session",
  "result": {}
}
```

每个公开失败使用相同协议版本，并返回带稳定 code、category、operation、message 与
retryable 标记的有界 error。未知异常会净化为
`TBM_DURABLE_WIRE_INTERNAL_ERROR`。

## Request 信任边界

严格 Pydantic request model 会拒绝未知字段。任何 request model 都不包含
principal、AgentClient、tenant、repository、environment、authorization event、
Semantic Gate provider 的认证 identity/credential、evaluator 的认证
identity/credential 或 server authority 字段。

preparation 时，调用方提供 task/retrieval facts、使用 canonical base64 编码的精确
query bytes、idempotency、TTL 与 lease。dispatcher 使用可信 service context 和
server-owned canonical repository ID 构造 `RetrievalPreparationContext`。现有
authorization/retrieval service 仍会针对当前 registry 与 durable decision 核验该
解析结果。调用方提供的 `retriever_id`/`retriever_version` 与 semantic-query
provider/version 是 retrieval algorithm descriptor，而不是已认证 transport 或
Semantic Gate provider identity；managed-index retrieval 会针对所选 bundle 核验
这些 descriptor。

Semantic Gate decision 的 prompt/response bytes 使用 canonical base64。provider
identity 与 credential 只能来自可信 provider context。dispatcher 构造 provider
callback，并在 durable service 返回后比较已保留 prompt/response bytes。对已经
decided 的 session 使用不同 response bytes 重放时，会以
`TBM_DURABLE_WIRE_DECISION_REPLAY_MISMATCH` 失败。

completion 的操作专属字段只包含 measurement facts 与 artifact hash，此外还有
state transition 共用的 session ID 与 expected version。可信 evaluator resolver
根据已认证 evaluator context 提供 evaluator ID/version；durable execution service
会在完成 session 前再次执行当前 registration authentication。

## 内容暴露

`DurableAgentWireConfiguration` 有两项 fail-closed profile：

- `expose_injection_content=False` 返回 injection descriptor 与 manifest，但把
  runtime snippet 替换为 null；
- `expose_replay_content=False` 从 capabilities 移除 `export_replay`，并在 replay
  authorization 或 storage read 前拒绝该操作。

replay 暴露必须同时启用 injection 暴露。显式启用后，replay 仍要求 durable facade
持久化新的 repository-scoped `artifact:read` decision、核验精确 session revision、
classification allowlist 与 byte limit，并执行完整 descriptor preflight 及
unchanged-session recheck。

这些开关是 defense in depth，不是 authorization。启用内容的 adapter 必须已经具备与
数据 classification 匹配的 authenticated peer boundary。

## 持久化与重放

wire dispatcher 不保存 pending handle。client 使用 `session_id` 与精确 GateSession
version 续接。idempotency、expiry、lease、cancellation、recovery state、
finalization replay、completion replay 与 session-bound replay export 仍由 durable
domain service/repository 拥有。

该模块是未来 durable HTTP、MCP、CLI-daemon、Python 与 TypeScript adapter 的共用
契约。在任何 adapter 构造完整 authority graph 与可信 context 前，不得把它描述为
active 或 transport-authenticated service。
