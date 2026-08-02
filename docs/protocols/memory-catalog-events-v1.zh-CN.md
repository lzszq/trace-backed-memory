# MemoryCatalog 事件 v1

`tbm.memory-catalog-event.v1` 是 Memory revision 生命周期的 opt-in 事件与
reducer 边界。它从规范事件重建 `MemoryCatalog` 与 `ActivatedMemoryHead`，
不会把 proposal、排序结果或兼容 `Lesson` 当作 active memory。

实现在 `trace_backed_memory.memory_catalog_event_v1`。默认兼容 Store 未改变；
默认 cutover 属于 F5。

## 事件流

每个 repository-scoped memory 使用一个 `memory_catalog_<sha256>` stream。
sealed registry 接受以下 version 1 事件：

- `revision_proposed`；
- `revision_reviewed` 与 `revision_rejected`；
- `fix_evidence_recorded` 与 `regression_evidence_recorded`；
- `revision_approved` 与 `revision_activated`；
- `revision_suspended`、`revision_superseded` 与 `revision_obsoleted`；
- `relationship_recorded` 与 `counterexample_recorded`。

每个 payload 保留规范 JSON 及其内容摘要。Replay 会解析精确的
`MemoryRevision`、`FixEvidence`、`StructuredRegressionEvidence`、review、
state-change、relationship 或 counterexample contract。Approval/activation
事件保留 `StoredMemoryRevision*Publication`，其中包括精确 policy、request、
decision、authorization event 与可信 attestation-verifier ID。

producer 把 record actor 及 tenant/repository 绑定到已认证的
`LedgerAccessContext`。Replay 再次检查 record/envelope partition、actor 与
occurrence time，调用方不能把一个 tenant 的 record 隐藏在另一个 tenant 的规范
事件下，也不能通过重算 envelope hash 替换 reviewer/provenance identity。Reducer
的 configuration digest 包含完整 trusted attestation-verifier allowlist。

## Reducer 规则

确定性 reducer 强制：

- 连续、不可变的 revision lineage；
- proposal 与 review actor 相互独立；
- 精确 evidence ID 与已验证的 fix/regression bundle；
- approval 只能出现在 accepted review 之后，且时间不能早于 review；
- approval evidence digest 必须等于 replay 得到的 evidence 集；
- approval/activation 的精确持久化 authorization provenance；
- activation 只能在 approval 之后执行，且 activator 与 proposer、approver 独立；
- 同一时间只有一个 active head，supersession 前必须有显式 relationship evidence；
- suspension、supersession 与 obsolescence 只向前推进；
- counterexample 必须是 fail/error 的 structured evidence；
- head 以内容寻址方式绑定 scope applicability、Artifact content、evidence
  bundle、authorization event、attestation verifier、activation 时间与精确
  activation event hash。

公开 replay 入口要求有界、非空的 trusted-verifier 配置。单 stream、聚合
rebuild 与 ledger scan 均有固定上限并 fail closed。

## Durable append 与 rebuild

`append_memory_catalog_records()` 接受 SQLite 或 PostgreSQL
`EventLedgerPort`。它读取并验证 retained stream、构造规范事件、在 append 前
运行 reducer、原子 append、验证 receipt、再次读取 durable stream，并要求
投影状态完全一致。global-position conflict 仅做有界重试；stream conflict 不会
被静默合并。

`rebuild_memory_catalog_from_ledger()` 冻结第一次观察到的 global high
watermark，只扫描到该边界，按 stream 分组 MemoryCatalog 事件，并返回内容寻址
的 `DurableMemoryCatalogSnapshot`。该快照除 partition、watermark、source count
与 catalog 外，还绑定 reducer descriptor 和 trusted-verifier configuration 摘要。
SQLite 与 PostgreSQL 共用同一实现并有聚焦 conformance test。

## 正式 retrieval source

`EventActivatedMemoryHeadSource` 实现既有
`ActivatedRevisionRetrievalSource` 协议。它要求 event-rebuilt head reader，先
核验 head 确实来自其 source catalog，再委托 `ActivatedRevisionSource` 完成精确
publication/evidence/Artifact 校验，最后复核 event head。Candidate 与 head 必须
在 revision、approval、activation、applicability、content、evidence、
authorization、trusted verifier 与 activation time 上完全一致。

`LegacyLessonCompatibilityProjection` 是显式 compatibility view，并固定
`eligible_for_activated_head=false`；它不是 activated-revision source。

## 仍未关闭的边界

- durable rebuild 验收目前仍被阻断：global scan 会在分页边界重复一个事件，且
  排除 `internal` classification 的 rebuild access 可能静默生成空的 partial catalog。
  这两种情况 fail closed 并具备回归覆盖前，F4-03/F4-04 仍不计分。
- F4-01/F4-02 FailureCase producer 的安全验收仍未关闭，因此它尚不能作为新
  MemoryRevision event 的已验收上游来源。
- F4-05 提供 active policy 与 renderer limit；F4-06 提供可重建 index；F4-07
  提供 outcome/harm projection。
- F5 必须让默认兼容 Store 与 transport 经过显式 compatibility projection 和
  event-derived head。在此之前本 profile 为 opt-in，`full_persistence=false`
  仍是正确声明。

规范资源：

- `schemas/memory_catalog_event_payload_registry_v1.schema.json`
- `examples/memory_catalog_event_type_registry_v1.example.json`

另见[英文参考](memory-catalog-events-v1.md)。
