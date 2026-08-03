# Git Observation 协议 v1

English: [git-observation-v1.md](git-observation-v1.md)

## 目的

`tbm.git-observation.v1` 把某一时点的 Git/workspace evidence 记录为 typed
`tbm.event.v1` observation。Git 仍是外部 evidence provider；它不是 event-ledger
authority、authorization source，也不是永恒不变的 repository truth service。

该协议复用 generic SQLite/PostgreSQL event ledger，不新增 SQL schema 或 compatibility
version。raw diff、路径、remote URL、command output 与 error text 都不得进入 payload。
精确 diff 字节属于已授权 Artifact authority；event 只携带 Artifact ID 与 canonical
descriptor。

## Event type

sealed registry 包含八种 version-1 observation：

- `tbm.git.checkout_observed`；
- `tbm.git.commit_observed`；
- `tbm.git.ref_observed`；
- `tbm.git.worktree_status_observed`；
- `tbm.git.diff_captured`；
- `tbm.git.commit_relation_observed`；
- `tbm.git.object_availability_observed`；
- `tbm.git.shallow_state_observed`。

每条 record 都绑定 canonical repository ID、local checkout alias、Trace/run identity、
有序 sequence、规范 occurrence time、authorization decision、`EventSource`、Git version、
runner name/version、algorithm name/version、classification、retention policy 与可选
causation event。repository/tenant scope 来自 trusted ledger context，绝不来自 Git command
payload。

kind-specific `details` object 是严格对象：

- checkout：current commit、ref 或 detached state、可选 remote digest；
- commit：精确 40/64 位十六进制 object ID；
- ref：精确 resolved commit 与 ref/detached state；
- worktree status：commit、ref/detached、dirty、可选 diff Artifact ID；
- diff：commit、可选 base commit、必填 exact diff Artifact ID；
- ancestry：ancestor、descendant 与 `ancestor`、`not_ancestor` 或 `unknown`；
- object availability：object/type、`available`、`unavailable` 或 `unknown`，非 available 时
  还必须有有界 reason；
- shallow state：显式 boolean 与至多 100 个唯一 boundary commit。

attached ref name 使用有界 ASCII Git-ref 子集。absolute path、URI syntax、whitespace、
control character、option-like prefix、hidden/`.lock` path segment、reflog syntax、
traversal 与空 path segment 都会被拒绝。Git、runner 与 algorithm version 同样只能使用
有界 version token，不能承载任意 command output。

`unknown` 永远不能转换成 `not_ancestor`。当前 Git object database 会在 force-push、
fetch、garbage collection 或 shallow-boundary 变化后改变，因此原 observation 必须作为
immutable evidence 保留。

## Identity、批次与重放

Git observation stream 由 trusted partition、repository、Trace、run 与 checkout alias
派生；event ID 具有 partition scope。批次包含 1 至 100 条连续 event，共享一个
partition-scoped identity key，以及一个覆盖每条 record、source/Artifact descriptor、
version 字段、classification、retention、trusted context 与 recorded time 的
content-bound command digest。

`build_git_observation_batch()` 构造并复验完整 parent chain。
`append_git_observation_batch()` 是要求使用的 typed 持久化入口；它会在 generic atomic
append 前拒绝截断/混合批次与不匹配 ledger access context。raw generic-ledger append
不会执行 Git-specific 语义校验。精确 retry 返回已保留 receipt；复用 identity 却改变
evidence 会产生 idempotency conflict。

## Capture 兼容

`capture_trace_metadata()` 继续返回原 frozen `TraceMetadata`；
`capture_commit_ancestry()` 继续返回原 frozen `CommitAncestryEvidence`，保留去重前
1,000-anchor 预算、Store lock 外执行、`GIT_NO_LAZY_FETCH=1`，且 ancestry exit code 只接受
0/1。

两个函数现在都接受可选 keyword-only `observation_recorder`。未传入时，command 顺序、
返回类型、错误与持久化行为完全不变。传入 `GitObservationEventRecorder` 后，成功 metadata
capture 会向同一有序 stream 添加 checkout/commit/ref/worktree/可选 diff/shallow
observation；成功 ancestry capture 会添加 object-availability 与 relation observation。
ancestry probe 失败仍抛出原 capture error，只记录 `unknown/capture_failed` object evidence，
绝不伪造 false relation。

recorder 要求 adapter-authenticated ledger context，以及显式 Git、runner、algorithm version。
它会在首次写入前预构造全部 record，再按 ledger 的 100-event atomic batch 上限提交。若
append 在 commit 后丢失 response，recorder 会保留精确 pending event、parent、position 与
idempotency command。任何新 capture 前都必须调用 `resume_pending()`；不得把重新执行 capture
操作当作恢复。adapter 还必须把 recorder 串行化为 ledger 的 global-position owner，或在
capture 前预留完整 position 区间。若其他 stream 占用了已预构造 position，exact replay 应
继续报告 conflict；此时必须停止该 recorder 并要求 operator recovery，不能重编号已保留
event，也不能声称该逻辑 observation 已完成。

## 安全边界

- Git observation 是 evidence，不是默认 prompt memory。
- checkout alias 是有界 opaque identifier；不持久化 absolute path。
- remote identity 是 SHA-256 digest；不持久化 URL/credential。
- diff 字节与 path listing 属于 Artifact content，不是 ledger metadata。
- 受保护 diff descriptor 仍需正常 encryption-key metadata，event classification 不得低于
  Artifact。
- revision 参数保持有界、无 shell；ancestry 保留 `--` option terminator 与 no-lazy-fetch。
- source quality、timestamp、Git version、runner/algorithm version 都是 trusted adapter
  提交的 evidence 声明；校验会绑定这些声明，但不会独立证明其真实性。
- ordering/repository matching 不是 authorization。

## 当前边界

本增量交付 typed Git 协议、ledger recorder、strict registry schema、capture 兼容 seam 与
SQLite/PostgreSQL parity test。默认 compatibility Agent/MCP profile 不配置 recorder。
自动 Git-version/remote/diff Artifact capture、checkout-binding authority、
GitGraphReducer/projection、force-push reconciliation、Codex Hook/App Server ingestion 与
默认 cutover 仍属于后续工作。
