# Gate Evidence Event v1

[English](gate-evidence-event-v1.md) | **简体中文**

`tbm.gate-evidence-event.v1` 是 F2 第一组 Retrieval 与 System Gate evidence
cutover 增量使用的紧凑 canonical-event 契约。它定义：

- `tbm.retrieval.prepared`：对应一条 immutable `RetrievalSnapshot`；
- `tbm.system_gate.evaluated`：对应一条 immutable `SystemGateEvaluation`。

每条 evidence 记录拥有一个 version 1、仅含一条 event 的 `gate_evidence` stream。
System Gate event 通过 `causation_id` 指向对应 Retrieval event。可信 organization、
tenant、repository、environment、principal 与 client identity 均来自 adapter 持有的
`EventTrustedContext`；request JSON 不能自行选择这些身份。

## Payload 与 Artifact 引用

payload 只包含紧凑 linkage：

- evidence kind、record ID 与 GateSession ID；
- authorization decision ID；
- 适用时的 retrieval snapshot parent ID；
- exact-content Artifact ID 与 SHA-256；
- occurrence time 与 causation event ID。

精确 canonical JSON 暂时仍保存在迁移期 Gate evidence repository。event 携带一条
descriptor-only `EventArtifactRef`：media type 为 `application/json`、classification
为 internal，并绑定精确字节大小、digest 与 `available` 状态。reducer 不读取这些
字节。在 Artifact store 成为唯一字节事实源之前，仍需把 exact bytes 迁入 authenticated
encrypted Artifact Authority。

## Event-first 持久化

显式启用后，SQLite 与 PostgreSQL Gate evidence repository 都会先 append 并读回
Retrieval/System Gate events，再插入现有 evidence rows。event append 与 row projection
共享同一 connection、cursor 和 transaction；event、projection 或 read-back 任一步失败，
整个 unit 都会回滚。exact retry 会核验已保留的单 event streams，不会分配新的 global
position。

PostgreSQL writer 先锁 event-ledger schema/global head，再锁 Gate evidence schema 与
rows，从而保持仓库统一的 event-first 锁序。

## Reducer

`gate-evidence-current` version 1 是 pure、deterministic reducer。它保留精确 record/
Artifact linkage、每个 session 当前的一组 Retrieval/System Gate evidence、authorization
连续性、causation 与 canonical event heads。如果 System Gate evidence 早于或不匹配其
Retrieval evidence，reducer 会 fail closed。projection 可通过现有 `tbm.projection.v1`
runtime 执行 checkpoint、resume、compare、activate 与 rollback。

后续增量现已 event-source Semantic Gate attempt 与 finalization，显式 durable replay
export 也会从 ledger 派生 metadata；详见
[Semantic Gate Attempt Event v1](semantic-gate-attempt-event-v1.zh-CN.md)、
[Finalization Event v1](finalization-event-v1.zh-CN.md) 与
[Ledger Replay Export v1](ledger-replay-export-v1.zh-CN.md)。outcome/attribution 与本地
completion-effect event/reducer（含 delivery history 和 dead-letter parity）也已交付。
provider receipt、unknown-result reconciliation、durable compensation 与其余 transport
commit point 仍未完成，因此产品继续报告 `full_persistence=false`。
