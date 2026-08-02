# Finalization Event v1

[English](finalization-event-v1.md) | **简体中文**

`tbm.finalization-event.v1` 是 F2 final decision 与 rendered injection cutover
增量使用的 canonical-event 契约。它把现有 `tbm.usage_decision.finalized`
GateSession transition 连接到一条 `tbm.injection.rendered` domain event。transition
event 是必需的 causation parent；不能只凭 identifier 或 replay projection 创建
injection event。

每份 replay manifest 拥有一个确定性的 version-1 `finalization` stream。两条 event
必须保留相同的可信 organization、tenant、repository、environment、principal、agent
client、actor 与 transition authorization scope。可信 context 来自 adapter，request
JSON 不能自行选择。

## Payload 与 Artifact 引用

有界 payload 会保留完整 `UsageDecision` 与 `InjectionArtifact` metadata、replay
manifest digest、固定 Artifact role mapping，以及 finalized transition event ID。它绝不
嵌入 snippet、prompt、response、policy、retrieval 或 renderer bytes。

按 Artifact ID 排序、唯一的 descriptor-only `EventArtifactRef` 会覆盖 UsageDecision
artifact、全部七个 replay component 和精确 injection artifact。每条 reference 都绑定
content digest、media type、byte size、classification、retention policy、需要时的
encryption-key metadata 与 availability。role set、descriptor、UsageDecision、injection
与重建 manifest 必须完全一致。

## Event-first 持久化

显式 durable SQLite/PostgreSQL runtime 会启用
`store_complete_finalization()`。一个外层 transaction 依次执行：

```text
读取精确 DECIDED GateSession event head
→ append/read back tbm.usage_decision.finalized 并发布 FINALIZED
→ append/read back tbm.injection.rendered
→ 写 replay Artifact/injection/manifest projections
→ 精确读回 projection 与 event
```

event、transition、replay projection 或 read-back 任一步失败，都会回滚两条新 event、
GateSession revision/head 与全部 replay rows。此前 claim 阶段已经提交的 lease renewal
保持提交。PostgreSQL 先取得 event-ledger schema/global lock，再验证 replay schema，最后
取得 GateSession transition/session-head locks。caller transaction 继续由 caller 持有。

兼容调用方继续使用 `store_complete_bundle()`，再执行现有 GateSession CAS。只有显式
启用并绑定可信 event context 时，才会选择 event-first 行为。

## Reducer 与 parity

`final-decision-injection` version 1 只消费 `tbm.usage_decision.finalized` 与
`tbm.injection.rendered`。它会重建 `final_decision_injection_v1` projection，精确保留
finalized session、UsageDecision、injection、replay manifest、Artifact role、
authorization、causation 与 event-head linkage。parent 缺失、decision/injection 重复、
scope drift、stale authorization 或 Artifact set 不完整都会 fail closed。

parity verification 会把重建 projection 与迁移期 GateSession/replay authorities 比较。
generic reducer runtime 仍不是唯一 active rebuild path。outcome/attribution 与本地
completion-effect reducer 已覆盖 delivery history 与 dead-letter parity。provider receipt、
unknown-result reconciliation、durable compensation、完整 transport conformance 与 F2
crash matrix 仍未完成；因此产品继续保持
`persistence_model="authority_graph"` 与 `full_persistence=false`。
