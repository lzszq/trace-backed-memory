# Ledger Replay Export v1

[English](ledger-replay-export-v1.md) | **简体中文**

`tbm.ledger-replay-export.v1` 是 F2 read path：它从 canonical finalization events
重建 finalized replay export，但只从 authenticated replay/Artifact authority 读取精确
字节。显式 durable SQLite/PostgreSQL runtime 会选择它；compatibility adapter 继续使用
现有 projection-backed reader。

## 查找与重建

已知 manifest digest 时，`LedgerReplayExportReaderV1` 会读取确定性的 finalization
stream。session-bound lookup 按 global position 扫描 canonical event，固定上限为
100,000 条，并且要求目标 session 恰好存在一条有效 `tbm.injection.rendered` event。
event 缺失、重复、越界、乱序或格式无效都会 fail closed。

reader 从 canonical event 重建 `DecisionReplayManifest` 与 `InjectionArtifact` metadata。
它不会把 replay projection metadata 当作 event 事实源，也不会从 event payload 或
Artifact reference 读取受保护字节。

## 受认证的字节读取

contextual reader 会把 ledger access 绑定到 adapter 持有的可信 organization、tenant、
repository、environment、principal、client、actor，以及新追加的 replay-read
authorization decision。精确 descriptor 与 content bytes 只能通过 replay/Artifact
authority 加载。每条 event Artifact reference 都会与存储 descriptor、digest、size、
media type、classification、retention/encryption metadata、availability 和 content
重新核验。完整固定 role set 到齐后才允许 export。

`verify_ledger_replay_export_parity()` 会分别通过 ledger-derived reader 与迁移期
replay-projection reader 导出，并要求 manifest、descriptor、bytes、injection content
与最终 `export_sha256` 全部一致。projection 不能静默替换或修复 canonical event
metadata。

## 边界

这是有界 session replay export，不是通用 canonical-event export，因此
`event_export` 继续为 `false`。event ledger 提供 finalization metadata/causation，
authenticated Artifact/replay authority 提供精确字节。outcome/attribution 与本地
completion-effect projection 已提供到 delivery history/dead letter 的 event-first parity；
provider receipt/unknown-result reconciliation、durable compensation、transport
conformance、migration 与 cutover gate 完成前，产品仍报告
`persistence_model="authority_graph"` 与 `full_persistence=false`。
