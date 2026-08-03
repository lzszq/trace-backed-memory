# Event Ledger Port v1

[English](event-ledger-port-v1.md) | **简体中文**

`tbm.event-ledger-port.v1` 冻结未来 canonical `tbm.event.v1` ledger 的存储中立应用端口。它定义可信访问、原子批量追加、精确重放、有界读取、stream 校验和有界订阅。F0 交付该契约；F1 增加显式 opt-in SQLite 与隔离 PostgreSQL 实现。显式 durable v3 runtime 现在会为 GateSession、Retrieval/System Gate evidence、Semantic attempt 与 finalization 选择有界 event-first adapter，并用 ledger metadata + replay authority bytes 提供 finalized replay read。默认 compatibility Agent/MCP 行为保持不变；metadata-only `tbmd ledger` / `tbmd projection` operator 命令可以选择一份显式 SQLite event-ledger 文件，执行 verification 与 inventory rebuild。

## 可信访问边界

每个端口实例都绑定由可信 adapter 组装的 `LedgerAccessContext`。它绑定 organization、tenant、repository 与 environment 分区，已认证的 principal 和 agent-client 身份，actor 与 authorization-decision 身份，以及 canonical classification filter。这些值不会作为不可信 event payload 声明接收。

追加时会针对该上下文校验每个 event。读取遇到超出已认证分区或 classification filter 的 event 时会 fail closed。scope 匹配仍不同于授权，不能单独描述为 tenant security。

## 原子追加与精确重放

冻结的接口是 `append(stream_id, expected_version, events, idempotency)`。其实现会在端口的可信 access context 中构造一个 `LedgerAppendRequest`。它携带一个不超过 100 个 canonical event 的非空批次，以及 idempotency key 和 canonical command 的一对内容摘要。Event 必须：

- 属于请求的 stream 和可信分区；
- 从预期 head 开始连续递增 stream version；
- 保留精确的 parent-hash 链；
- 绑定相同的 idempotency key 与 command 摘要；
- 满足调用方的 classification filter。

后端必须在单个事务中提交完整批次、stream-head 更新、global position 和幂等记录，否则不得产生任何变化。使用相同 key 与完全一致的 canonical request 重试时，返回原始的同一个 `LedgerAppendReceipt`。用该 key 提交不同 command 或 request 会失败；过期的 expected version 也必须在不修改状态的情况下失败。

`EventLedgerAtomicAppendPort` 是附加的所有权扩展；它不改变冻结的 `append`
签名、receipt、digest 或端口版本。`append_once(...)` 执行同一个事务并返回
`LedgerAppendCommit(receipt, inserted)`。只有实际插入幂等记录的事务调用方会得到
`inserted=true`；精确保留重放返回同一 receipt 与 `inserted=false`。effect 编排器必须
在调用远端 provider 前使用该结果，不能通过写后读取推断所有权。该扩展还暴露后端
拥有的不透明 `authority_identity`；组合层只能按严格对象 identity 比较它，以拒绝
混用不同物理 authority。它不是 tenant 或 scope credential。

该事务提交前，后端必须立即应用 `verify_ledger_append_precondition`：提供的当前 head
必须匹配 expected stream version，第一个 event 必须扩展其精确 hash，且该批次必须消费
下一组全局连续 position。head 或 global sequence 漂移属于 conflict，不能产生部分 append。

此端口表达这些不变量。`SQLiteEventLedgerV1` 使用 `BEGIN IMMEDIATE`、进程生命周期 single-link owner lock、WAL、per-stream/global-head CAS、immutable trigger、精确 catalog 校验和已验证备份来实现它们。`PostgresEventLedgerV1` 使用 active-metadata/table lock、固定 global-head→stream-head 顺序的数据库 row lock、精确 CAS、caller savepoint、完整 catalog digest、immutable trigger 与 fail-closed rollback 脚本。跨后端测试要求同一批 event 产生逐字一致的 receipt 与 page。

## Artifact 引用边界

两个后端都会把每个 `EventArtifactRef` 精确保留为 descriptor，其中包含 content digest、classification、retention policy、encryption-key identity 与 availability。它们不会存储、读取、解密、授权或擦除所引用的内容字节；这些字节仍由已认证 Artifact Authority 管理，event ledger 只证明 canonical event 提交了哪份 descriptor。

## 有界读取与校验

`read_stream(stream_id, from_version)` 从正整数 version 返回一页连续 stream event；`read_global(after_position, limit)` 返回指定 position 之后严格递增的一页 global event。两者均限制为最多 1,000 个 event，保留 canonical event 对象，携带内容绑定的 page 摘要与 high-watermark，并只暴露显式 next cursor。声称仍有更多结果的 page 必须至少包含一个 event、推进 cursor，并保留晚于该 page 的 high-watermark。它们不会暴露 SQL row、table 或未经筛选的 raw ledger。

`verify_stream(stream_id)` 针对精确 version、event count、head hash、tenant partition 和稳定 issue code 返回有界校验结果。空 stream 会被显式表示，不会伪造记录。issue code 来自冻结的 v1 allowlist，`verify_ledger_stream_verification` 会把结果重新绑定到请求的 stream 与已认证 partition。

## 订阅 profile

`subscribe(...)` 创建有界且经过 classification 筛选的 global-page subscription。每次 poll 均受 page size 和最长 60 秒 timeout 限制。投递语义为 at least once；消费者确认 delivery ID，并按 canonical event hash 去重。Heartbeat 不包含 event。此端口不承诺已部署的 broker、持久 consumer offset 或活跃的 Agent/MCP subscription。

## 稳定失败

契约区分 invalid request、过期 stream head、idempotency conflict、scope denial、classification denial、隐藏或不存在的记录，以及 unsupported operation。消息保持有界并经过清理。实现必须保留这些含义，不得独立复制 authorization 或 Gate policy。

这些 opt-in 后端不会改变当前 compatibility Store 或默认 Agent/MCP 行为。generic F1 reducer runtime 与 operator CLI 可以在这些 schema 中保留 checkpoint 和 projection-head history。F2 为已选择的 GateSession、Gate evidence、Semantic attempt 与 final decision/injection slice 增加 typed reducer 及 event/projection parity；显式 durable replay export 会从 ledger 派生 metadata，并从 replay authority 读取精确 bytes。但同步 authority 仍是过渡 projection，generic reducer runtime 尚未成为唯一 lifecycle 重建路径。在完成已验证的 full cutover 前，机器可读持久化模型仍是 `authority_graph`，且 `full_persistence=false`。
