# Full Persistence 进度契约

[English](full-persistence-progress.md) | **简体中文**

机器可读事实源是
[`full-persistence-progress.json`](full-persistence-progress.json)。它冻结针对桌面执行
计划的所有进度报告分母。后续报告只有在列出受影响 atom ID 与证据时才能修改分子；
不得用某个 phase 或顶层 bullet 数替换总分母。

## 固定分母

490 个 atom 按完整计划重建如下：

| 来源 | 数量 |
| --- | ---: |
| F0-F6 release-train 顶层 bullet | 312 |
| 去重后的 test matrix | 74 |
| Definition of Done | 67 |
| Retention/erasure 要求 | 33 |
| 跨阶段 global gate | 4 |
| **总计** | **490** |

固定 phase 分母为 F0 48、F1 90、F2 62、F3 117、F4 38、F5 48、F6 87。
这些只是同一组 490 atom 的分类，不是额外工作。

## 汇报规则

正式进度只计算已提交 atom。候选进度必须单独汇报，并列出每个未提交 atom。证据未知、
测试不足或文档互相矛盾时，一律算未完成。不能根据总分子反推历史 phase 分布，因为那会
制造并不存在的证据。

当前正式基线是 182/490（37.14%）：由经审计的 162-atom 既有基线，加上已提升的
20-atom F2 event-first tranche 构成。该 tranche 覆盖 finalization/replay、
GateSession/Gate-evidence/Semantic event 与 reducer、RunOutcome/
OutcomeAttribution projection，以及本地 EffectQueue delivery-history/dead-letter
slice。机器可读契约保留上一批已提升 atom ID 与证据路径。当前没有额外未提交 atom
候选，durable compensation 仍不计入。

新增的 local-daemon 子进程 hard-restart 测试覆盖已确认的 `PREPARED`、`DECIDED`、
`FINALIZED`、`EXECUTING` 与 `COMPLETED` commit，并要求精确重试和最终 reducer parity。
这只是 F2 crash matrix 的部分证据，不是已完成 atom，因此 182/490 保持不变。

新增 SQLite `SIGKILL` probe 覆盖已提交 authorization、`CREATED` 与 Gate-evidence
边界、finalization replay 和 completion/outbox transaction 内回滚，以及 consumer
返回但 ack 尚未 durable 时的 lease reclaim 与 at-least-once redelivery。精确
`CREATED` 恢复会重新授权且不改写 orphan evidence；finalization 会重建唯一确定的
claim-time bundle。commit 后 response-loss probe 现覆盖 `DECIDED`、event-first
`FINALIZED`、`EXECUTING`、组合 completion/outbox 与已提交 acknowledgement，精确
重试不会重复 replay 或 redelivery。本地 happy path 还通过真实 JSON-RPC STDIO MCP，
与 Python facade、Python HTTP sync/async SDK 及 TypeScript HTTP SDK 对齐全部 17 个
global event、七条 stream head 和八个已注册 reducer projection。provider
receipt/reconciliation、PostgreSQL parity、其余 crash matrix、完整 F2 cross-transport
conformance 与 durable compensation 仍未完成。精确 legacy SQLite timestamp trigger
也会在 reopen 时原子修复。这些仍只是新增证据与 corruption repair，没有提升 atom；
正式与候选进度均保持 182/490（37.14%）。

下一项 F3 基础新增一个严格 provider-transition event、内容寻址 attempt/invocation/
receipt/reconciliation identity、`effect-queue` reducer version 2 与 authenticated
generic-ledger service。SQLite 会核验精确 append replay、receipt mismatch 拒绝、保守的
orphan/unknown recovery、reconciliation-gated retry 与 commit 后 response loss；同一
存储中立路径在具备所需 executable 时还有 PostgreSQL integration test。active semantic/
completion callback 与 provider-specific reconciliation adapter 尚未选择它，因此没有
完整 F3 effect atom 被提升；正式与候选进度仍为 182/490（37.14%）。
