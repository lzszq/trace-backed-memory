# Git Graph Reducer 与 Projection v1

[English](git-graph-reducer-v1.md) | **简体中文**

`tbm.git-graph.v1` 是 F3-03 的 opt-in、storage-neutral reducer，输入为七类 canonical
Git observation event，并确定性重建 immutable `GitGraphProjection`。它不会调用 Git、读取
当前时间、授予 repository 权限，也不会切换默认 Agent/MCP/HTTP profile。

## Replay contract

`reduce_git_graph_events()` 要求从 version 1 开始的单一完整 Git observation stream，覆盖
精确 organization、tenant、repository、environment 与可见 classification 的 authenticated
ledger access context，以及可选且有界的现有 `FixEvidence`、
`StructuredRegressionEvidence` 与 `PRCaseProvenance`。replay 会先校验每个 canonical event、
sealed typed payload、parent hash、stream version、可信 partition、classification 与不倒退的
observation time，再执行带版本且确定性的 reducer。它还会从完整 event group 重建每个 capture
command，核对 command/idempotency digest、连续 position 与派生 event/source identity；绝不只
信任 `request_sha256` 作为 capture 边界。reducer state 继续受通用 1 MiB、depth 32、32,768 node
限制，并额外限制 10,000 个输入 event、20,000 个 commit node、50,000 条 parent edge，以及各
1,000 个 evidence 或 PR case 输入。

projection 保留：

- repository partition 与被观察到的 checkout identity；
- observed commit、被关系引用的 placeholder commit ID 与 parent edge；
- 原始及有效 ancestry status；
- object availability 与明确的 missing/unknown-object reason；
- 最新 observation 的 runner、algorithm、Git version、source record、event digest、position 与
  observation time；
- 经独立验证的 source-to-fix 与 fix-to-verification 关系；
- 按 source commit 排序去重、同时保留 endpoint 与 case provenance 的 PR anchor；
- reducer descriptor digest、最新 validated time 与 content-addressed projection digest。

对同一组有序输入重复 replay 会得到同一个 projection digest。commit 内容、object format、
checkout identity、可信 scope 与已知 ancestry 不得冲突。同一 capture request 的重复观察点、
graph cycle、sequence gap、parent-hash drift 与 evidence chain 错配都 fail closed。
一个 capture command 必须占据一个连续 stream segment，并且在其他 command 之后不得重新打开。

## Relation confidence

confidence 是可解释枚举，不是概率、授权结果或 retrieval ranking 输入：

| 值 | 含义 |
|---|---|
| `independently_verified` | immutable Fix/Regression evidence 关系存在，且 full repository 中两个 endpoint 都在本地 present |
| `locally_observed` | parent 或 ancestry 关系具有本地校验所需的精确 Git observation 集合 |
| `degraded` | 关系证据仍保留，但 shallow state 或本地 object availability 阻止当前重新校验 |
| `indeterminate` | 有效 ancestry 是 `unknown`，不能转换为兼容 boolean |

已知的有效 ancestry status 必须在同一 capture request 中同时观察到 `full` shallow state，且
current 与 anchor commit 都为 `present`。否则 projection 保留 `reported_status`，但强制
`status=unknown`、`confidence=indeterminate`，且不写 validation time。后续 missing/unknown
object 或 shallow observation 也会降低受影响关系的可信度。因此 `missing` 永远不等于
`not_ancestor`。

## Evidence relationship 与 PR anchor

补充关系输入来自现有 immutable authority，不是新增 Git observation。regression record 只有在
存在 exact `FixEvidence`，且 case、source trace、source commit 与 fix commit 全部一致时才会
被接受。projection 随后记录每个 evidence ID、有方向的 source→fix 或 fix→verification edge、
verifier、verification time、regression result 与派生 confidence。完整 SHA-1 或 SHA-256 ID
必须与被观察到的 object format 一致。

PR anchor 使用 `PRCaseProvenance.commit_sha`，保持兼容 API 中 PR **source** commit 的语义。
fix 与 verification commit 保持为独立 provenance/relationship 字段。anchor 按 commit 排序去重，
同时保留全部 unique case ID、fix commit 与 old/new/both/legacy endpoint tag。缺失 ancestry 或
不可用 object 会产生显式 unknown anchor。只有每个 anchor 都已本地验证时，
`pr_anchor_commit_ancestry_evidence()` 才会生成兼容 boolean；否则 fail closed。

scope match 与 Git ancestry 始终只是 applicability evidence，不是 tenant 或 repository
authorization。System Gate 与 Semantic Gate authority 保持不变。

## 当前边界

本 reducer 是 opt-in rebuild/read model；它不新增 database schema，不形成新的 persistence
authority，也不会被兼容或 durable transport 默认选择。opt-in Codex 摄取 adapter 不会选择本
Git read model。effect receipt、retention/crypto-erasure 完成、active projection persistence
与 F3 exit gate 仍是独立后续工作。
