# SQLite Gate Evidence v3

这个 opt-in、side-by-side authority 会持久化一个 GateSession preparation
实际使用的精确 `RetrievalSnapshot` 与 `SystemGateEvaluation` 对。它不会替代
active snapshot-v2 Store，也尚未接入 `LocalAgentMemory` 或 `tbm-mcp`。

## 写入契约

`SQLiteGateEvidenceV3Repository.store_bundle()` 只接受精确的 v3 record
对象，先验证完整的 System Gate-to-retrieval 关联，再在一个 SQLite
transaction 中保存两份 canonical JSON descriptor。完全相同的记录可幂等
重放。每个 snapshot 只能对应一个 System Gate evaluation；任何 immutable
内容冲突都会 fail closed。

canonical schema 会启用 foreign key 与 recursive trigger。因此 immutable
update/delete trigger 也会拒绝 `INSERT OR REPLACE` 触发的替换删除。
repository 每次读写前都会核验 schema metadata 与全部具名 schema definition。

## PREPARED 桥接

`DurablePreparedGateEvidenceVerifier` 是供
`AuthenticatedGateSessionService` 使用的标准、storage-neutral verifier。
它会从 authority 重新加载两个 ID，复核内容哈希与有序候选覆盖，然后要求下列
信息完全一致：

- GateSession、Trace 与 run ID；
- authorization event ID；
- tenant、repository、principal 与 agent-client scope。

只有校验通过后，service 才能通过 CAS 发布 `PREPARED`。evidence 写入与
GateSession transition 仍是跨独立 authority 的有序操作，不是跨数据库的
atomic transaction；service compensation 与 recovery 语义仍然适用。

## 当前边界

本版本只提供 SQLite storage。PostgreSQL 对等实现、实际产生这些记录的 active
retriever，以及 Agent/MCP/HTTP/SDK 集成仍是后续工作。
