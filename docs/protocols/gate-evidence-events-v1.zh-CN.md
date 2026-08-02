# Gate evidence 事件与 ledger replay export v1

[English](gate-evidence-events-v1.md) | **简体中文**

`tbm.gate-evidence-event.v1` 是围绕单个 durable GateSession 保留证据的 opt-in
事件适配器。只有显式 durable SQLite 与 PostgreSQL 组合会选择它；兼容 Agent、HTTP、
MCP、CLI 与 SDK 契约保持不变。

## Stream 与事件集合

每个 session 都有一个 `gate_evidence` stream，其 ID 由精确 session ID 派生。密封
registry 接受五种 version-1 领域事件：

- `tbm.gate_evidence.retrieval_snapshot_recorded`；
- `tbm.gate_evidence.system_gate_evaluated`；
- `tbm.gate_evidence.semantic_gate_attempt_recorded`；
- `tbm.gate_evidence.usage_decision_recorded`；
- `tbm.gate_evidence.injection_artifact_recorded`。

Payload 只包含有界 identity、linkage、status、sequence 与内容寻址 Artifact descriptor。
retrieval score、prompt/response bytes、渲染后的 injection bytes 与 replay component
bytes 继续留在各自 authenticated authority。规范事件通过精确 `EventArtifactRef`
引用这些字节，绝不会把受保护内容复制进 projection state。

Preparation 会在提交 `PREPARED` GateSession revision 前同步 retrieval 与 System Gate
事件。每次成功或失败的 Semantic attempt 都会在 attempt repository 的同一事务中同步
record、prompt 与可选 response。Finalization 会在提交 `FINALIZED` revision 前同步
UsageDecision、injection descriptor、精确 injection bytes 与 replay-manifest descriptor。
事件追加、Artifact read-back 或 projection comparison 任一失败都会中止共享数据库事务。

## 确定性视图

五个确定性 reducer 分别构建 retrieval-current、System-Gate-current、
Semantic-attempt-chain、final-decision-current 与 injection-current 视图。Reducer state
只保存 descriptor 与 linkage。Hydration 会加载精确内容寻址记录，核验每个事件 Artifact
引用，解析现有严格 v3 record，并重新核验 System Gate 单调性、Semantic parent 顺序、
final-decision linkage 与 replay-manifest linkage。

Stream 顺序单调固定：retrieval、System Gate、零到多个 Semantic attempt、final
decision，最后 injection。Singleton 视图不可替换；Semantic attempt 必须扩展精确 parent
chain；已保留事件必须是 authority evidence 的精确前缀。重启重建不读取 wall clock，
也不调用外部 provider。

## 从 ledger 生成现有 replay export

`GateEvidenceEventLedgerProjector.export_replay_bundle` 从事件及其引用的 Artifact bytes
重建 finalized injection 与 replay manifest。它按内容 digest 解析每个 manifest component，
应用显式 classification allowlist 与字节上限，然后调用现有
`build_replay_bundle_export` constructor。输出仍然精确属于 `tbm.replay-export.v3`，没有
新增 wire format。

SQLite 与 PostgreSQL conformance test 会把完整规范 export JSON 和 `export_sha256`
与当前 replay authority 经 `export_replay_bundle` 读取的结果比较。component 缺失、digest
不匹配、classification 不允许或尺寸超限都会关闭失败。

## 当前边界

该适配器不会把整个产品声明为 event-first。Outcome/effect、Memory、index、outbox、
audit、metrics、migration、兼容 cutover 与完整 F2 crash matrix 仍是独立后续工作。因此
机器可读产品状态仍为 `persistence_model="authority_graph"` 且
`full_persistence=false`。
