# Semantic Gate Attempt Event v1

[English](semantic-gate-attempt-event-v1.md) | **简体中文**

`tbm.semantic-gate-attempt-event.v1` 是 F2 Semantic Gate attempt chain cutover
增量使用的紧凑 canonical-event 契约。它定义：

- `tbm.semantic_gate.attempt_failed`：对应 immutable failed attempt；
- `tbm.semantic_gate.attempt_succeeded`：对应 immutable succeeded attempt。

每条 System Gate evaluation 拥有一个确定性的
`semantic_gate_stream_sha256_<digest>` stream。event stream version 等于 attempt
sequence。retry 以 previous attempt event 作为 causation，并在
`previous_stream_event_sha256` 中保留其精确 event digest。

## Parent 与可信 scope

首条 attempt 不能只凭派生 parent ID 构建或持久化；它必须读取并核验已保留、有效的
`tbm.system_gate.evaluated` event，逐项检查 evaluation、session、retrieval snapshot
与 event identity。parent 与 attempt event 的可信 organization、tenant、repository、
environment、principal、agent client 与 actor scope 必须完全相同。

两条 event 可以而且通常会引用不同 authorization decision。System Gate event 保留
最初 retrieval authorization；Semantic attempt event 保留本次 mutation 新追加的
`gate_session:transition` authorization。两份 context 都只能来自可信 adapter；request
JSON 不能自行选择。

## 紧凑 payload 与精确 Artifact linkage

payload 保留有界 attempt-chain metadata、provider/model/template identity、configuration
与 provider-request ID、prompt/response digest、final revision sets、decision/risk/injection
metadata、token/latency measurement、timestamp 与各 role Artifact ID。它绝不嵌入 prompt
bytes、response bytes 或 decision reason。

canonical Artifact references 按 Artifact ID 排序并去重，覆盖：

1. 精确 canonical `SemanticGateAttempt` descriptor JSON；
2. 精确 prompt bytes；
3. succeeded attempt 的精确 response bytes。

每条 reference 都绑定 content digest、media type、byte size、classification、retention、
需要时的 encryption-key metadata 与 availability。同一 Artifact ID 对应不同 descriptor
时会 fail closed。exact bytes 暂时仍由受认证的 Semantic Gate artifact authority 保存；
reducer 不读取 Artifact bytes。

## Event-first 持久化与 Reducer

显式 durable SQLite/PostgreSQL runtime 会启用 event-first Semantic attempt write。同一
transaction 执行：

```text
核验已保留 System Gate/attempt parent
→ append 并读回 canonical event
→ SemanticGateAttempt row projection
→ exact Artifact bytes 与 role-binding projection
→ exact read-back
```

任一步失败都会回滚 event、stream/global head、idempotency receipt、attempt row/head、
Artifact bytes 与 bindings。exact retry 只核验已保留 event，不会再分配 global position。
PostgreSQL writer 固定使用 `event-ledger schema/global head → semantic attempt projection
→ semantic Artifact projection` 锁序；caller transaction 继续由 caller 持有。

`semantic-gate-attempt-chain` version 1 是 pure、deterministic reducer。它同时消费 System
Gate parent 与 failed/succeeded attempt events，拒绝缺失/错配 parent 与非单调 retry，并
重建精确 current stream、attempt、Artifact-linkage 与 event-head view。逐字段 parity 必须
同时提供保留的 authority bundles 与 canonical events，因此重复 authority 输入或同步
伪造的 event hash 无法通过。

final decision/injection view 与 ledger-backed replay export 已在当前 F2 增量交付；详见
[Finalization Event v1](finalization-event-v1.zh-CN.md) 与
[Ledger Replay Export v1](ledger-replay-export-v1.zh-CN.md)。outcome/attribution 与本地
completion-effect reducer 已提供到 delivery history/dead letter 的 event-first parity。
存储中立 provider receipt/reconciliation event、reducer 与 ledger service 已交付；active
semantic-provider selection、provider-specific reconciliation、durable compensation、完整
transport parity 与 F2 crash matrix 仍未完成，所以 `full_persistence` 继续为 `false`。
