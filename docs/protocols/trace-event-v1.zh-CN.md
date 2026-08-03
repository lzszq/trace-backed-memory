# 有序 Trace Event 协议 v1

English: [trace-event-v1.md](trace-event-v1.md)

## 目的

`tbm.trace-event.v1` 是用于有序工程执行证据的存储中立协议。它把严格 Trace event
放入既有 `tbm.event.v1` canonical envelope，并复用既有 event-ledger append
transaction；不会新增独立 authority 或数据库 schema。

该协议坚持 evidence-first。prompt 文本、tool 输入/输出、diff、final response 等潜在
敏感字节都不是 payload 字段；它们由 Artifact authority 保留，event 只携带 descriptor。

## Canonical event

sealed event registry 包含一个 observation type：

- `tbm.trace.event_recorded`

具体工程事件名由有界 `trace_event_type` payload 字段表达。未来可信 adapter 因而可以
映射 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PermissionRequest`、
`PostToolUse`、subagent、compaction、stop、diff 与 final-response record，而不必让
每种 integration-specific event 各占一个 registry slot。

每条 event 都包含：

- 一个 `trace_id` stream 与一个 `run_id`；
- 与 canonical `stream_version` 相等的正整数 `sequence`；
- 精确、规范 UTC 的 `occurred_at` 时间戳；
- 必填 `EventSource` descriptor，绑定 source record identity 与 evidence quality；
- 零个或多个已排序且唯一的 Artifact reference，payload 只重复其 ID；
- 可选 `tool_correlation_id`、`parent_trace_id`、`subagent_id` 与
  `causation_event_id` linkage；
- 必填 permission result：`not_applicable`、`allowed`、`denied`、`pending` 或
  `unknown`；
- canonical envelope 中精确的 trusted authorization decision、classification 与
  retention policy。

首条 event 的 sequence 为 1，且没有 stream parent。后续 event 每次只前进 1，并绑定
上一条 event hash。parent 校验还要求 Trace/run identity 完全相同。

## 原子批次

`build_trace_event_batch()` 接受 1 至 100 条记录，与
`EVENT_LEDGER_MAX_APPEND_BATCH` 一致。一个批次具备：

- 连续的 Trace sequence 与 global position；
- 每个 payload 中相同的 `batch_first_sequence` 与 `batch_size` descriptor；
- 一个 partition-scoped、identity-addressed batch idempotency key；
- 一个覆盖全部 record、source descriptor、Artifact descriptor、classification、
  retention policy、完整 trusted context 与 recorded time 的 command digest；
- event-ledger port 要求的、所有 canonical event 共享的 request ID 与 command digest。

若复用同一 batch identity 却改变任一记录，会产生 idempotency conflict。
`verify_trace_event_batch()` 会重建完整 command digest，并拒绝 partial、乱序、非连续或
混合 command 的批次。单条 append 就是 `batch_size=1` 的同一协议。

`append_trace_event_batch()` 是该 typed 协议要求的持久化入口。它会先校验完整批次与
精确 ledger access context，再调用 generic atomic append port。generic event ledger 为
保持可扩展性，不会推断 Trace-specific payload 语义；直接调用其 raw `append()` 或
`append_once()` 不等于 typed Trace append。

## 校验与重放

`TraceEventRecordRef` 校验有界 identifier、规范时间戳、permission state、parent
identity、Artifact descriptor、classification 与 retention。`parse_trace_event()` 校验
payload/envelope linkage 和确定性 event/batch identity；`verify_trace_event_parent()`
校验单条 stream edge；`verify_trace_event_batch()` 校验完整 append command。

完成 typed preflight 后，SQLite/PostgreSQL 复用 generic canonical event ledger。因此精确 replay、stream
verification、tenant/repository partition、classification filtering、transaction
rollback 与 packaged registry schema parity 都沿用同一条已测试存储路径。

稳定校验错误使用 `TBM_TRACE_EVENT_INVALID` 和有界消息。

## 安全边界

- Trace event 是证据，绝不是默认 prompt memory。
- scope/repository 字段来自可信 ledger access context，而不是 observation payload。
- payload 只包含 Artifact ID，不包含原始 prompt/tool/diff/response 字节。
- 受保护 Artifact descriptor 仍要求 encryption-key identity；event classification 不得
  低于所引用 Artifact。
- `unknown` permission evidence 不等于 `allowed`。
- source identity、quality、occurrence time 与 observation time 都是 trusted adapter 提交的
  evidence 声明；Trace contract 会校验并绑定这些声明，但不会独立证明其真实性。
- 精确顺序不是授权，也不会让不可信 source 自动可信。

## 当前边界

本增量只交付 typed Trace event 与 atomic batch 协议，尚未：

- 摄取 Codex Hooks 或 App Server frame；
- 解析不稳定 transcript；
- 生成 Git checkout 或 ancestry observation；
- 构建 Trace projection 或 Git graph reducer；
- 切换 compatibility Trace aggregate 或默认 Agent/MCP profile。

后续 integration 必须使用可信、versioned adapter，且不得削弱 canonical event、
Artifact、authorization 或 ledger contract。
