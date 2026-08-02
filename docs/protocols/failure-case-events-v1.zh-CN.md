# FailureCase 事件与结构化证据 reducer v1

[English](failure-case-events-v1.md) | **简体中文**

`tbm.failure-case-event.v1` 是 storage-neutral、opt-in 的 F4-01/F4-02
协议，用于把有序 Trace 证据转换成可审查的 FailureCase projection。它不会替换
兼容 `FailureCase` 模型，也不会改变默认 Agent、MCP、HTTP 或 SDK profile。

## 信任边界

Extractor 接收一条完整且已核验的 TraceEvent stream，并输出 content-addressed
proposal。Proposal 绑定精确 Trace event hash、Trace/run identity、source Artifact
ID、受保护 proposal Artifact descriptor、extractor version 与 configuration digest。
它的状态永远是 `candidate`；extractor 及其输出都不能审查或验证 case。

事件 stream 以 `case_id` 为键，接受五种带版本的事实：

- `tbm.failure_case.extractor_proposed`；
- `tbm.failure_case.reviewed`；
- `tbm.failure_case.fix_evidence_recorded`；
- `tbm.failure_case.regression_evidence_recorded`；
- `tbm.failure_case.legacy_imported`。

Native review 必须与 extractor 独立。验证要求 accepted review、精确
`FixEvidence`，以及 case、source Trace、source commit 与 fix commit 全部匹配的
passing `StructuredRegressionEvidence`。Regression verifier 必须与提取和 case
review 独立。失败或 error 的 regression attempt 保持未验证，之后可以追加一次通过的
attempt。

## Legacy 边界

Legacy `regression_passed=true` 只能导入为
`evidence_quality=legacy_unstructured`。它永远不会提升为
`structured_verified`，对应 projection 始终是
`eligible_for_new_memory=false`。因此新 Memory 生产只能消费上述结构化路径支撑的
projection。

## 确定性 projection

Sealed registry 校验精确 payload 字段，并拒绝未知 type/version。纯 reducer 校验
stream identity、parent hash、连续 version、单调 timestamp、transition 顺序、actor
分离与 evidence linkage。它由共享 deterministic reducer kernel 双执行，并输出包含
last event hash 与 global position 的 content-addressed projection。

原始 Trace、proposal 的 symptom/root-cause 文本和受保护 evidence bytes 都不会进入
ledger payload；event 只保留有界 ID、digest、code 与 Artifact descriptor。

## 当前边界

安全验收仍未关闭：draft replacement 可以在替换 evidence payload 时保留内部 producer
capability。该缺口关闭前，本协议不得提供新 Memory eligibility，也不得作为后续
MemoryCatalog reducer 输入。F4-03 至 F4-07、默认 runtime cutover、物理 Artifact storage
与 legacy database migration 仍是独立工作。
