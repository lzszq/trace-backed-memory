# GateSession 生命周期事件 v1

[English](gate-session-events-v1.md) | **简体中文**

`tbm.gate-session-event.v1` 是 durable v3 GateSession 生命周期的 event-first
适配器与确定性当前状态 reducer。它仅在显式 durable SQLite 与 PostgreSQL runtime
组合中启用；兼容 Agent、HTTP、MCP、CLI 与 SDK 的 wire 契约保持不变。

适配器绑定后创建的 GateSession revision 以规范 event ledger 为事实源。现有
GateSession revision 表继续作为同步、可查询的 projection。事件追加、reducer
重建或 projection 比较任一步失败，都会在 revision 行可见之前中止同一数据库事务。

## Stream 与可信分区

每个 GateSession 都有一个类型为 `gate_session` 的 stream。stream ID 由精确
`session_id` 经域隔离摘要生成，调用方不能自行选择。ledger 分区由可信实体注册表
解析为持久化 GateSession 对应的精确 organization、tenant、repository 与 active
environment；映射缺失或不唯一时关闭失败。显式组合的多环境部署可以提供等价的可信
resolver，但 request JSON 永远不能提供 ledger identity。

每个原生事件都使用 `internal` classification、durable service actor，并以精确
revision `updated_at` 作为 `recorded_at`；command 与 idempotency digest 均为确定性值。
ledger 会核验上一事件哈希与预期 stream version。

## 事件类型

密封的领域 registry 包含以下 version-1 事件类型：

- `tbm.gate_session.created`；
- `tbm.gate_session.prepared`；
- `tbm.gate_session.awaiting_decision`；
- `tbm.gate_session.semantic_gate_decided`；
- `tbm.gate_session.usage_decision_finalized`；
- `tbm.gate_session.execution_started`；
- `tbm.gate_session.completed`；
- `tbm.gate_session.canceled`；
- `tbm.gate_session.expired`；
- `tbm.gate_session.execution_abandoned`；
- `tbm.gate_session.lease_renewed`；
- `tbm.gate_session.baseline_imported`。

`awaiting_decision` 与 `lease_renewed` 用于保留没有新阶段名或终态名的 revision，
它们是精确回放 lease/version 所必需的。`baseline_imported` 是升级前 GateSession
在适配器绑定后的首次迁移中使用一次的 observation event。它以 `legacy_partial`
证据质量记录精确保留的 projection，不会虚构缺失的历史生命周期事件。

每个 payload 都是严格对象，只包含：

- `transition`——已注册的 transition 常量；
- `previous_session_sha256`——上一 projection digest；只有新建或 baseline import
  stream 才能为 `null`；
- `session_sha256`——结果 GateSession 的 digest；
- `session`——完整、严格的 `tbm.gate-session.v3` projection。

未知字段、未知事件、非规范时间戳、非法 digest，以及不满足已注册 transition 的
payload 都会被拒绝。

## 同步追加与 projection

对于一个新 revision，适配器在一个工作单元中执行：

1. 根据 current 与 next GateSession 推导精确 event draft；
2. 按预期 stream/global position 追加规范事件；
3. 读取并核验完整 stream；
4. 运行 `gate-session-current` reducer；
5. 比较重建 GateSession 与拟写入的 next revision；
6. 写入现有 GateSession revision 与 current-head projection。

六步共享 repository transaction 与锁顺序。global position 竞争最多重试八次；
semantic conflict 与 idempotency conflict 永远不会被隐藏。事件追加后的失败会同时回滚
事件与 projection 写入。

reducer 会核验 event parent chain、registry payload、transition 合法性、上一
projection digest、结果 projection digest、stream position，以及完整 GateSession
领域验证。重建相等性覆盖 status、version、lease 与 expiry 时间、retrieval/System/
Semantic evidence ID、decision ID、最终 memory 与 injection ID、outcome ID 和 terminal
reason。它不读取 wall clock 或外部服务。

## 确定性产物与边界

领域 registry 发布以下逐字节核验的 packaged resource：

- `examples/gate_session_event_type_registry_v1.example.json`——密封、内容寻址的
  registry catalog；
- `schemas/gate_session_event_payload_registry_v1.schema.json`——生成的严格 payload
  dispatch Schema。

这些 GateSession 类型有意不加入小型通用 `DEFAULT_EVENT_TYPE_REGISTRY`；durable
适配器会显式选择领域 registry。当前 reducer 尚未替代 retrieval、Gate evaluation、
Semantic attempt、final decision、injection、replay、outcome 或 delivery-state authority。
完整 F2 cutover 只有在这些 view 都拥有各自的事件契约与 reducer 后才能声明完成。
