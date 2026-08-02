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
