# Codex 摄取协议 v1

[English](codex-ingestion-v1.md) | **简体中文**

## 状态与边界

`tbm.codex-ingestion.v1` 是把结构化 Codex Hook 和 Codex App Server 帧映射到
[有序 TraceEvent 协议](trace-event-v1.zh-CN.md)的 opt-in adapter。它不安装 Hook、
不建立 App Server 连接，也不改变默认 Agent、MCP、HTTP、SDK、兼容 Trace、snapshot、
SQLite 或 PostgreSQL profile。`full_persistence` 仍为 `false`。

宿主 adapter 必须可信：它负责认证本地 Codex 来源、提供接收时间和固定的
`CodexIngestionBinding`，并使用受保护 Artifact writer。来源 JSON 不能选择 organization、
tenant、repository、Trace、run、lineage、authorization 或 ledger access。scope 匹配只是
provenance 校验，不是授权。

## 来源映射

| Codex 摄取事件 | Hook 来源 | App Server 来源 | TraceEvent |
|---|---|---|---|
| `SessionStart` | `SessionStart` | `thread/started` | `tbm.trace.session_started` |
| `UserPromptSubmit` | `UserPromptSubmit` | 已完成的 `userMessage` | `tbm.trace.user_prompt_submitted` |
| `PreToolUse` | `PreToolUse` | 已开始的受支持 tool item | `tbm.trace.tool_started` |
| `PermissionRequest` | `PermissionRequest` | 三个稳定 `requestApproval` method | `tbm.trace.permission_recorded` |
| `PostToolUse` | `PostToolUse` | 已完成的受支持 tool item | `tbm.trace.tool_completed` |
| `SubagentStart` | `SubagentStart` | 已完成的 `subAgentActivity(kind=started)` | `tbm.trace.subagent_started` |
| `SubagentStop` | `SubagentStop` | 已完成的 `subAgentActivity(kind=interrupted)` | `tbm.trace.subagent_stopped` |
| `PreCompact` | `PreCompact` | 已开始的 `contextCompaction` | `tbm.trace.pre_compact` |
| `Stop` | `Stop` | `turn/completed` | `tbm.trace.stopped` |
| `SessionEnd` | `SessionEnd` | `thread/closed` | `tbm.trace.session_ended` |
| `DiffUpdate` | — | `turn/diff/updated` | `tbm.trace.diff_observed` |
| `FinalResponse` | — | 已完成的 `agentMessage(phase=final_answer)` | `tbm.trace.final_response_recorded` |

`PostCompact`、增量 delta、无关 notification、非 final agent message 和
`subAgentActivity(kind=interacted)` 都不是本协议的事实。transcript 或类似 transcript 的
notification 会作为事实源被拒绝。受保护 Hook 原始帧内可以包含 transcript path，但 adapter
不会读取其指向的 transcript，也不会把该路径复制进 ledger metadata。

## 严格捕获与受保护 bytes

每个输入最多 1 MiB、depth 64、50,000 个 JSON node。UTF-8、重复 key、非有限数值、
必需 identity、稳定 envelope 字段、source method/event 映射和 timestamp 都会 fail closed。
被接受帧的精确 bytes 会交给可信 writer；writer 必须返回一个 available、已加密、
confidential 或 restricted 的 `EventArtifactRef`，media type 为
`application/vnd.trace-backed-memory.codex-source+json`，retention policy 为
`retention_codex_source_v1`。digest 与 byte count 必须和输入逐字节一致。

TraceEvent ledger 只包含该 descriptor。原始 prompt、tool input、tool output、diff、final
response 与 transcript path 留在受保护 Artifact content 中。确定性的 source-record identity
会绑定 source profile、映射事件、method、session、turn 和 Artifact content digest。

capture record 与 Artifact descriptor 是可信进程内值，不是不可信 wire 的授权边界。调用方
必须通过 `capture_codex_*` 函数创建 record，不得从 request JSON 自行构造
`CodexSourceRecord`。Artifact 是否存在、能否访问由配置的 Artifact authority 强制，而不是由
只含 descriptor 的 record 强制。

## 时间与 permission evidence

Hook 帧没有权威 event timestamp，因此 adapter 使用可信接收时间。App Server notification
保留精确的 `startedAtMs`、`completedAtMs` 或 `emittedAtMs`；live capture 会拒绝和可信
接收时钟相差超过 300 秒的 source clock。没有 `emittedAtMs` 的较早稳定 notification 使用
接收时间。

permission decision 必须绑定精确原始 approval frame 的 SHA-256。官方 Hook
`PermissionRequest` 没有 `tool_use_id`，因此必须按 tool name 与 canonical tool-input digest
唯一匹配一个 active Hook invocation；零个或多个匹配都会 fail closed。App Server 会持久化
从 `threadId`、`turnId` 与 `itemId` 派生的 source-specific、turn-scoped correlation，因此跨
turn 复用不能命中 active item。Command/file approval method 还绑定其映射 tool family；通用
permissions method 只能缩窄精确 active item。permission event 发生在可信 decision time；
App Server request start 不得晚于该
decision，decision 与可信接收时间之差不得超过 300 秒。观察到的 permission result 不会授权
ledger append。

## 生命周期与追加

`build_codex_ingestion_trace_drafts()` 要求可信 binding 的完整既有 TraceEvent history。它强制：
唯一且最先的 `SessionStart`、`SessionEnd` 后无事件、精确且不可复用的 tool lifecycle、配对的
subagent lifecycle、session end 时没有 active tool/subagent，以及每个事件恰好一个不可复用的
受保护 source Artifact。同一个 active App Server item 可以关联多个精确 approval callback；
每个 callback 都有自己的原始帧 Artifact 与 permission request digest。

`append_codex_ingestion_batch()` 映射 1 至 100 个 record 的 batch，并委托给现有 access-bound
TraceEvent ledger append；事件 batch 依 ledger 契约原子提交且精确幂等。Artifact capture 是
独立的受保护内容操作：后续 binding、lifecycle 或 ledger CAS 校验拒绝事件 batch 时，一个合法
Artifact 可能作为 immutable orphan evidence 留存。它不是 Trace 事实或 projection input，必须
由配置的 retention policy 处理。

## 公共 API

- `capture_codex_hook_event()`
- `capture_codex_app_server_notification()`
- `capture_codex_app_server_permission()`
- `build_codex_ingestion_trace_drafts()`
- `append_codex_ingestion_batch()`
- `codex_ingestion_projection()`

聚焦一致性测试覆盖上述全部映射、无 tool ID 的官方 Hook permission correlation、当前 App
Server item/time 字段、精确 permission/request 绑定、transcript 拒绝、可信 scope、生命周期
拒绝、受保护 bytes、rollback、replay 与精确 retry。
