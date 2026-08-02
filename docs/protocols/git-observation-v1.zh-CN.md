# Git Observation 协议 v1

[English](git-observation-v1.md) | **简体中文**

`tbm.git-observation.v1` 是 F3-02 的 opt-in、storage-neutral Git 证据协议。
它记录有界本地 Git runner 实际观察到的内容，不把 repository scope 当成授权，也不改变
兼容的 `TraceMetadata` 或 `CommitAncestryEvidence` contract。

## 观察点

sealed registry 只接受以下七类 version-1 observation event：

| 观察点 | Event type | 持久化证据 |
|---|---|---|
| checkout | `tbm.git.checkout_observed` | 由路径派生的 checkout identity、repository name、object format、HEAD、dirty state |
| ref | `tbm.git.ref_observed` | 完整 symbolic ref 或显式 detached state、target object |
| commit | `tbm.git.commit_observed` | commit、tree 与保持顺序的 parent object IDs |
| diff | `tbm.git.diff_observed` | HEAD 到 index/worktree 的 byte digest、size 与精确 protected Artifact descriptor |
| ancestry | `tbm.git.ancestry_observed` | 每个 anchor 的 `ancestor`、`not_ancestor` 或 `unknown` |
| object availability | `tbm.git.object_availability_observed` | 每个完整 object ID 的 `present`、`missing` 或 `unknown` |
| shallow state | `tbm.git.shallow_state_observed` | `full`、`shallow` 或 `unknown` |

每个 payload 同时保存 `runner_id`、`runner_version`、`algorithm_id`、
`algorithm_version` 与实际观察到的 Git version。canonical envelope 是带精确
`EventSource` 的 `observation` event；sequence 与 stream hash chain 复用
`tbm.event.v1` 和 access-bound ledger port。

## Capture 与兼容性

`capture_trace_metadata()` 保持原签名、四条命令的顺序以及精确
`TraceMetadata` 返回类型。`capture_commit_ancestry()` 也保持原签名和
`CommitAncestryEvidence` 返回类型。

显式 F3 路径新增：

- `capture_trace_metadata_detailed()`：返回兼容 metadata，并产生
  checkout/ref/commit/diff/shallow observation drafts；
- `capture_commit_ancestry_detailed()`：先观察 object availability，再形成
  ancestry evidence，并返回 availability/ancestry drafts；
- `capture_and_append_git_observations()`：捕获全部七个观察点，并将事件作为一个
  bounded、atomic、exactly-idempotent ledger batch 追加。

只要任何必需 object 缺失或不可确定，详细 ancestry 结果就不包含兼容 evidence，
relation 一律为 `unknown`；缺失对象绝不会被转换成 `not_ancestor`。

## Diff 与 runner 边界

原始 diff bytes 永不进入 event payload。详细捕获必须使用可信 Artifact writer；其返回
descriptor 必须精确绑定原始 bytes，media type 为 `application/vnd.git.diff`，状态为
available，classification 为 confidential 或 restricted，并包含 encryption key。
restricted descriptor 会把 event classification 提升为 restricted。event 只保存该
descriptor、digest 与 size；event verifier 会独立复核完整绑定，不能只信任 draft 构造。

Artifact writer 在 ledger append 前运行，因此必须 content-addressed 且 exactly
idempotent。event append 失败后可能留下供治理式垃圾回收处理的未引用 protected blob，
但在 committed event 引用前，它不是 Git observation 或 source of truth；此边界不会
dual-write projection。

Git 子进程最长 30 秒；metadata output 上限 64 KiB，diff output 上限 64 MiB。
详细 diff 命令禁用 external diff 与 text conversion。整个默认 detailed 路径（包括兼容
metadata projection）以及 ancestry runner 都设置 `GIT_NO_LAZY_FETCH=1`，因此本地对象
缺失不会隐式触发网络 fetch；standalone 兼容 API 保持原 environment 行为。详细路径只
接受完整 SHA-1 或 SHA-256 object ID。batch idempotency 会绑定 expected stream head、
next global position、capture time 与每个 draft。

## 当前边界

本协议与 runtime composition 仍为 opt-in，默认兼容 Agent/MCP/HTTP profile 尚未 cut
over。F3-03 现在提供独立的 opt-in
[Git Graph Reducer 与 Projection v1](git-graph-reducer-v1.zh-CN.md)；Codex/App Server hook
的 diff notification 可以另行通过 opt-in
[Codex 摄取 adapter](codex-ingestion-v1.zh-CN.md)进入 TraceEvent，但不会选择本七观察点
Git 协议。Artifact authorization 与 authenticated ledger access context 始终来自可信
runtime，而不是 request JSON。
