# 受治理 Artifact retention 与加密擦除 v1

[English](artifact-retention-v1.md) | **简体中文**

## 状态与范围

`artifact_retention_event_v1.py` 是 F3 retention evidence 的 opt-in、存储中立
协调器。它不会改变兼容 Agent、durable HTTP/MCP/SDK profile，也不会改写不可变
Artifact repository。可信 adapter 必须提供绑定访问上下文的 event ledger、managed-index
repository、受保护 manifest store、target resolver、legal-hold guard、KMS adapter、receipt
verifier 与可信时钟。

本契约覆盖五种结果：受保护 redaction manifest、加密密钥销毁、tombstone event、通过
successor-head CAS 完成的不可变 managed-index purge，以及 replay-partial marker。物理删行、
object-store lifecycle、legal-hold release 与默认 runtime cutover 不在范围内。

## 计划与受保护 manifest

`RetentionRequest` 固定 organization、tenant、repository、environment、authorization
decision、精确 Artifact ID、deletion policy、reason 与幂等摘要。resolver 返回精确的加密
Artifact descriptor、受影响 MemoryRevision ID、完整 replay impact，以及完整 key-reference
closure。policy guard 返回 content-addressed snapshot，其中包含 retention expiry、legal-hold
状态与 hold epoch。

`RedactionManifest` 绑定上述输入、预期与后继 managed-index bundle ID、确定性 replay
marker 和计划时间。operation ID 不受重新计划时间影响，由 request identity、scope、target、
policy/reason、resolution digest 与 policy-state digest 派生。规范 manifest bytes 以
confidential/restricted 加密 Artifact 保存，且使用不会被销毁的 governance key；ledger event
只保留 descriptor 与摘要。

## 有序生命周期

sealed event registry 包含以下有序事实：

1. `retention_applied`、`redaction_manifest_recorded` 与
   `crypto_erasure_requested` 在外部副作用之前记录 intent。
2. `index_purged` 记录通过 scope-local compare-and-swap 选择的不可变后继 index head。
3. `crypto_erasure_authorized` 在破坏性调用前，绑定精确可信 KMS registration、
   attestation、provider request identity、request digest 与 legal-hold authorization digest。
4. `crypto_erasure_blocked`、`crypto_erasure_rejected` 或
   `crypto_erasure_unknown` 保存 fail-closed 结果。
5. 只有每个 key 都取得经独立校验的精确 receipt 后，才会在同一 batch 追加
   `replay_partial_marked`、`cryptographically_erased` 与 `tombstoned`。

reducer 会核验 producer/version、authorization、deletion policy、manifest descriptor、
擦除前后精确 target descriptor、receipt descriptor/digest、provider 绑定、index head、
replay-marker closure、parent hash 与 transition 顺序。terminal state 不可变。

## 崩溃与外部副作用规则

managed-index publication 和 KMS destruction 无法加入 ledger transaction，因此协调器使用
event-first saga。若后继 index head 已发布而 outcome event 尚未写入，恢复会识别同一
content-addressed successor，先补记事实再继续。若 KMS 已执行或结果不确定、但 outcome batch
尚未持久化，调用方得到 `TBM_RETENTION_RECOVERY_REQUIRED`。

只有 `destroy()` 可以发起 key destruction。`crypto_erasure_authorized` 一旦存在，恢复绝不
再次调用它。`reconcile()` 只是对精确 provider request 的非变更状态查询，禁止发起、重试或
重建销毁。因此 unknown/partial 结果可以恢复，而不会 blind retry。

legal-hold guard 的 `authorize_destruction()` 是原子的 hold-epoch CAS/lease 边界：有效
destruction authorization 存续期间，新 hold 必须失败或等待。协调器会在授权前立即复核 policy
与当前 index head。若在 provider 已独立确认不可逆操作之后才观察到 hold，只能记录为 late
evidence，不能声称已回滚。

## Purge、tombstone 与 replay 语义

managed-index purge 会构造不含目标 MemoryRevision candidate 与 orphan evidence edge 的新
content-addressed bundle，并只推进当前 scope head。旧 bundle、Artifact ciphertext row 与
replay row 仍是不可变历史。直接加载旧 bundle 从来不是 authorization；已认证 retrieval 必须
使用 current head 与当前 Artifact-read decision。

crypto-erasure 只销毁 reference closure 恰好覆盖目标 Artifact 的 key。manifest/receipt
governance key 必须与目标 key 分离。tombstone 把 event descriptor availability 标记为
`erased` 并记录经校验的 provider receipt；它不声称物理删行。

runtime erasure marker 是针对先前 `complete` replay manifest 的 sidecar evidence，绑定精确
erased Artifact ID 与缺失 replay component。它不会修改或复用仅供迁移使用的
`legacy_partial`。consumer 在 `require_replay_not_erased()` 找到匹配 marker 时必须拒绝
exact replay；既有 replay authority 不会被静默改写。

## 边界与资源

request、target、key reference、policy decision、receipt 与 event history 都有硬上限；单个
operation 最多 60 个 target，manifest 最大 2 MiB。JSON 解码拒绝重复键、非有限数、过深/
过多节点、非法 UTF-8 与未知字段。ledger payload 只包含 ID、摘要、provider metadata 与
failure code，绝不包含 manifest、receipt、prompt、output、diff 或 key bytes。

生成的外部契约为：

- `schemas/artifact_retention_event_payload_registry_v1.schema.json`
- `examples/artifact_retention_event_type_registry_v1.example.json`

规范副本与 installed copy 逐字节相同，并受严格 resource manifest 保护。
