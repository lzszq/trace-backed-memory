# Retrieval index 事件 v1

状态：基于共用 event-ledger port 的 opt-in F4-06 domain reducer。默认
compatibility Agent、MCP、HTTP、SDK 与 Store profile 尚未选择它。

## 合同

`RetrievalIndexManifest` 把一个不可变 `ManagedIndexBundle` 精确绑定到：

- `metadata`、`lexical`、`semantic`、`evidence_graph`、`git_graph` 各一个内容摘要；
- source event watermark 与 source event digest；
- 精确 source catalog 与排序后的 memory-revision ID；
- retriever、tokenizer、embedding provider/model 与 Git-graph version；
- 精确 managed-index build digest，以及 fresh/stale lifecycle 边界。

manifest 复用既有 managed-index hash recipe 与 bundle 校验，不会再实现另一套
tokenizer、vector normalization、evidence graph、Git graph 或 ranking 路径。

四类 internal event 构成一个 partition-local stream：

- `tbm.index.build_requested`
- `tbm.index.build_completed`
- `tbm.index.activated`
- `tbm.index.marked_stale`

每个 event 都嵌入精确 authorization policy、request、decision 和 attestation
verifier identity。build request/completion 需要 `memory:create`；activation/stale
mark 需要 `memory:activate`。activation principal 必须与完成构建的 principal
不同，并绑定精确 predecessor bundle。source watermark 与 activation time 只能前进。

## Replay 与持久化

`retrieval-index-current` 是确定性的 `tbm.reducer.v1` reducer。它的 configuration
digest 同时绑定可信 authorization-attestation verifier 与可信 embedding
provider/model pair，并重建内容寻址的 current head；每个 lifecycle projection
record 都保留精确 source-event hash 与 global position。

`append_retrieval_index_records()` 和
`rebuild_retrieval_index_from_ledger()` 只使用 `EventLedgerPort`，同一路径已覆盖
SQLite 与 PostgreSQL event ledger。rebuild 必须拥有完整
public/internal/confidential/restricted classification view，核验 retained stream，
拒绝不前进的分页 cursor，并在返回绑定 reducer descriptor/configuration hash 的
snapshot 前再次读取整个 stream。

`EventManagedIndexRepository` 是只读 selection adapter。它只加载 event 选中的
fresh bundle，逐字段核对 manifest 与精确不可变 bundle，并在读取后复查 head。
经该 adapter 的直接 publish 会被拒绝。索引命中或相似度仍只是 discovery
evidence，绝不是 authorization。

## 边界

- 既有隔离 SQLite/PostgreSQL managed-index repository 仍是 opt-in immutable
  bundle store，不是 event source of truth。
- 本模块不新增 SQL table、migration、network/provider call 或默认 runtime cutover。
- F4-03/F4-04 MemoryCatalog 验收阻塞、F4-07 outcome reducer 与 F5 默认
  migration/cutover 仍是独立工作。
- Raw Trace bytes 不进入 manifest，也不会成为默认 prompt memory。

Canonical resources：

- `schemas/retrieval_index_manifest_v1.schema.json`
- `schemas/retrieval_index_event_payload_registry_v1.schema.json`
- `examples/retrieval_index_manifest_v1.example.json`
- `examples/retrieval_index_event_type_registry_v1.example.json`
