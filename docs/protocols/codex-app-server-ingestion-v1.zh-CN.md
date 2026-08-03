# Codex App Server 摄取 v1

[English](codex-app-server-ingestion-v1.md) | **简体中文**

## 目的

`tbm.codex-app-server-ingestion.v1` 是一个 opt-in、存储中立的 adapter，把 Codex
CLI `0.146.0` 生成的固定 Codex App Server v2 notification surface 映射到既有
`tbm.trace.event_recorded` family。它提供严格 evidence boundary；默认 Agent、MCP、
HTTP 或 daemon profile 都不会自动接入它。

深模块为 `CodexAppServerTraceRecorder`。可信 factory 会绑定唯一 ledger、Trace/run/
thread identity、`EventTrustedContext`、clock、classification、retention policy、sequence
cursor、global-position cursor，以及可选的已保留 Trace parent。notification JSON 不能提供
或覆盖这些值。

## 支持的 observation

adapter 只接受 App Server wire version `v2` 的 notification envelope，并映射：

- `hook/started` 与 `hook/completed` 的 `preToolUse`、`permissionRequest`、
  `postToolUse`、`preCompact`、`postCompact`、`sessionStart`、`sessionEnd`、
  `userPromptSubmit`、`subagentStart`、`subagentStop` 与 `stop`；
- `turn/diff/updated`；
- 仅当 item 是 phase=`final_answer` 的 `agentMessage` 时映射 `item/completed`。

permission-request start 记为 `pending`；Hook completion notification 不能证明 allow 或
deny，因此 completion 仍为 `unknown`。工具相关 Hook 的 started/completed observation 会从
可信 Trace/run 绑定与固定 Hook `run.id` 派生同一个稳定 correlation identifier。

通过校验的 streaming delta、patch update、reasoning delta 与已弃用 context-compaction
notification 会被跳过且不写 evidence。commentary/phase-unknown agent message，以及固定的
非 agent completed-item variant，也会在校验 thread 与顶层 shape 后跳过。未知 method、未知
item variant、request/response、realtime transcript notification 与猜测的 direct-Hook
stdin payload 都 fail closed。

## Artifact-first 内容边界

recorder 不存储 Artifact 字节。调用 `ingest_notification()` 前，调用方必须先通过已授权
Artifact Authority 持久化精确原始 notification frame，再传入该 frame 唯一的
`EventArtifactRef`。recorder 会核验：

- content-derived Artifact ID 与 SHA-256 精确匹配输入字节；
- media type 为 `application/json` 且 byte size 精确；
- trusted classification 与 retention policy；
- state 为 `available`。

固定 App Server frame 可能包含 prompt、path、diff、tool output 或 response，因此 recorder
拒绝 `public` classification。必须使用 `internal` 或更强的受保护 classification，并在构造
recorder 前确认 ledger classification filter 允许该级别。

canonical Trace event 只包含 descriptor。Hook output、source path、diff、final response、
prompt fragment 与其他 raw frame 内容绝不进入 event payload metadata。受保护 frame 仍必须
携带正常 encryption-key descriptor。Artifact 已成功保存而 Trace append 被拒绝时，可能留下
未被引用的 Artifact；本 adapter 不声明跨 authority 原子性。

## 严格输入与顺序

每帧必须是 strict UTF-8 JSON，不超过 8 MiB、100,000 nodes 与 depth 100；重复 key 与
非有限数字会被拒绝。固定 method overlay 会拒绝未知字段，以及非法 identifier、enum、
timestamp、Hook entry、path、patch kind 或 trusted thread identity。Hook output text 可以包含
正常换行与 Tab；结构 identifier 不能包含控制字符。

每条成功映射的 notification 都会在配置的精确 sequence/global cursor 上创建一条连续 Trace
event。规范 `occurred_at`、`recorded_at` 与 source observation time 来自 trusted clock；App
Server 数字时间只作为 evidence 保留在精确 frame Artifact 中，不作为可信 scope 或
authorization input。

若 ledger 可能已经 commit 但 response 丢失，recorder 会私下保留完全相同的 pending
event。稳定的 pre-commit ledger rejection 会清除该 pending candidate，并返回
`TBM_CODEX_APP_SERVER_APPEND_REJECTED`，让调用方从当前 durable head 重建。结果不确定时，
调用 `resume_pending()` 重放相同 idempotent command 之前，新输入会以
`TBM_CODEX_APP_SERVER_PENDING_RESUME_REQUIRED` 被拒绝。只有确认 insert 或 exact replay
后，cursor 与 parent 才会前进。

## 当前边界

本 adapter 不增加 event type、database schema、packaged resource 或 projection；它复用已注册
Trace-event family 与既有 SQLite/PostgreSQL ledger 实现。它不会：

- 把 transcript fragment 解析成最终事实；
- 从 Hook completion 推断 allow/deny；
- 认证 Codex 或持久化 Artifact 字节；
- 构建 Trace aggregate 或 reducer projection；
- 选择自动 Hook capture 或默认 Agent/MCP/HTTP ingestion；
- 改变 snapshot 2、SQLite 1、PostgreSQL 2 或 `full_persistence=false`。

另见[有序 Trace Event 协议 v1](trace-event-v1.zh-CN.md)与
[Codex 集成指南](../integrations/codex.zh-CN.md)。
