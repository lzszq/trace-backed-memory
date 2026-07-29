# 已认证 durable Agent 组合 v3

状态：可选应用组合；不是默认 Agent 或 MCP profile。

English: [durable-agent-v3.md](durable-agent-v3.md)

## 用途

`tbm.durable-agent.v3` 是已发布 durable retrieval、Semantic Gate、
finalization、execution、outcome 与 completion-outbox 服务之上的
adapter-neutral 应用门面。adapter 可以通过一条生命周期边界调用这些服务，
无需复制 Gate policy，也不能提交自行构造的授权 scope。

`AuthenticatedDurableAgentMemory` 在调用之间不保存状态。续接能力来自所配置的
durable authority，而不是进程内 request handle。新建的 facade 实例只要使用同一组
authority，并取得当前可信的 service、provider 与 evaluator context，就能继续已保留
session。

## 服务图

构造时必须提供以下精确实例：

- `AuthenticatedRetrievalService`；
- `DurableRetrievalPreparationService`；
- `AuthenticatedSemanticGateSessionService`；
- `DurableFinalizationService`；以及
- `DurableExecutionService`。

所有阶段必须共享同一个 authorization service、GateSession authority、Gate evidence
authority、Semantic Gate authority 与 ActivatedRevision source；execution 还必须通过
所配置的 finalizer 回放，其 completion outbox 还必须使用同一个精确 GateSession
authority。任一关系不一致都会拒绝构造，避免把一套 authority 图中的有效记录或原子
completion 接到另一条生命周期上。

## 生命周期

facade 暴露：

1. `prepare(context, request)`：授权 `memory:retrieve`，创建 GateSession，
   保存精确 RetrievalSnapshot/System Gate evidence，并发布 `PREPARED`；
2. `decide(context, provider_context, request, call_provider)`：先从已保留 retrieval
   evidence 重新认证 session owner，持久化新的 `gate_session:transition` 授权，
   在 provider 工作前后复查两个 scope，再调用已认证 Semantic Gate service；
3. `finalize(context, request)`：恢复原始 retrieval scope，复查当前授权与 revision
   状态，持久化新的 transition 授权，保存精确 replay bundle，并发布 `FINALIZED`；
4. `start(context, request)` 与 `resume(context, request)`：恢复原始 retrieval scope，
   持久化一条新的 `gate_session:transition` 授权，并且只在确实需要执行时返回精确
   保留 snippet；
5. `complete(context, evaluator_context, request)`：恢复已保留 retrieval evidence，
   持久化新的 transition 授权，认证实时 evaluator，并原子发布 RunOutcome、
   `COMPLETED` 与 completion outbox event；
6. `abandon(context, request)`：恢复已保留 retrieval evidence，授权并发布精确版本的
   终态 abandonment；
7. `cancel(context, request)`：恢复已保留 retrieval evidence，从 `PREPARED` 或
   `AWAITING_DECISION` 授权精确版本取消，并支持精确幂等回放；
8. `get_session(context, session_id)`：只有恢复并核验原始 retrieval 授权后，才返回
   当前 durable 状态。

facade 的公共方法输入不暴露 `AuthorizedRetrievalScope`。adapter 只能传入可信
context 与带版本的 request。

## 授权恢复

`AuthenticatedRetrievalService.recover_authorized_scope()` 会从一条已保留的 allow
decision 重建服务端持有的 scope。恢复过程会：

- 重新加载当前 registry 与 policy；
- 精确匹配可信 Principal 与 AgentClient record；
- 要求已保留 decision 仍为 allow、permission 一致，并绑定当前 policy hash；
- 解析当前 canonical repository reference 或精确 tenant alias；
- 复查 active environment；
- 返回原 authorization event ID 与 canonical scope。

durable Agent 会加载 session 所关联的 RetrievalSnapshot，核验其 session/Trace/run
linkage，并只从该 snapshot 取得原始 authorization event ID。调用方 JSON 不能选择或
替换此 event。

policy 轮换、identity 轮换、repository target 改变、evidence 缺失或跨 owner session
都会 fail closed。恢复不会新增 `memory:retrieve` decision；`PREPARED` 之后的每次
GateSession 修改都会另行新增当前 `gate_session:transition` decision。

## 取消与回放

`DurableAgentCancelRequest` 绑定 `session_id`、`expected_session_version` 与有界
terminal reason。第一次成功取消只推进一个 revision。仅当已保留 canceled revision
恰好等于请求 parent 加一且 reason 相同时，重试才是精确回放；版本、reason 或状态
改变都会被拒绝。`CREATED` cancellation 只供 preparation 内部补偿使用；尚无已保留
retrieval evidence 的 session 不能由调用方取消。

finalization、execution start、completion、resume 与 abandonment 保留底层服务的
replay/recovery 语义。facade 不会重新渲染 snippet、重复已保留的 provider success，
也不会推断 measurement。

## 信任边界

该组合不是 transport authenticator。嵌入服务必须：

- 从可信 authenticator 派生 `AuthenticatedServiceContext`；
- 从实时已认证 transport 派生 Semantic Gate provider 与 outcome evaluator context；
- 不把 provider/evaluator credential 放入调用方 JSON；
- 按 GateSession `run_id` 保持外部 executor effect 幂等；
- 在服务端配置 durable authority 与 provider callback。

当前组合只支持既有 public/internal plaintext replay profile。受保护内容加密、
replay-read authorization、retention 集成、GateSession revision 中的持久
transition-authorization linkage 与物理 repository attestation 仍是独立必做项。

## Adapter 状态

该 facade 是未来 MCP、HTTP、CLI-daemon 与 SDK adapter 共用的应用边界。默认
`LocalAgentMemory` 与 `tbm-mcp` profile 仍使用带进程内 pending handle 的
`tbm.agent.v1`。当前尚无 network adapter 构造此 facade；本协议不宣称 transport
authentication、shared multi-tenant readiness 或 schema version 3 cutover。

facade 测试覆盖 SQLite/PostgreSQL 生命周期对等性；底层 authority 继续保持既有
transaction、savepoint、CAS 与 rollback 契约。
