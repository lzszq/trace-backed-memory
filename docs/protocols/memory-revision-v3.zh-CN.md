# MemoryRevision proposal 与 publication event v3

[English](memory-revision-v3.md) | **简体中文**

`tbm.memory-revision.v3` 是 storage-neutral、immutable proposal 契约。独立的
`tbm.memory-revision-approval.v3` 与
`tbm.memory-revision-activation.v3` 契约记录 approval/activation event，而不向
proposal 增加可变 lifecycle 字段。active snapshot/SQL adapter 尚不持久化或产生
任何这些 version-3 记录。

每个 revision 使用内容派生 ID，并绑定 stable memory ID/kind、revision number 与精确
parent、content-addressed artifact、canonical authorization scope、confidence/
sensitivity metadata、source FailureCase/Fix 引用、结构化 regression-evidence ID，
以及 server-owned proposer/client/attestation context。Lesson proposal 必须具备
全部 case、fix 与 regression 引用；Project-policy proposal 禁止这些 case-bound
引用，并必须使用 `policy` memory type。

`verify_memory_revision_evidence_bundle` 会解析精确
[FixEvidence](fix-evidence-v3.zh-CN.md) 与每个 lesson regression-evidence ID，
要求相同的 Failure Case、source Trace、source/fix commit、passing regression
evidence，并确保 proposer 独立于全部 evidence actor。旧的
`verify_memory_revision_evidence` 只检查 regression evidence，不足以用于发布。

`approve_memory_revision` 会重新验证精确 revision lineage、content 字节、
FixEvidence/regression bundle、actor 分离，以及 approval 时刻针对该 revision
tenant 或 repository 的精确、允许的 `memory:review` decision。
`activate_memory_revision` 不信任孤立 approval hash，而是重放完整 approval
verification；随后独立检查 `memory:activate`、第三位 actor、精确 immediate
predecessor linkage 与单调 sequence。builder 会验证传入 predecessor 的内容派生
shape 与 linkage，但无法证明一个不受信的独立 event 就是 durable current head。
publication 禁止 global scope；global policy 必须使用独立 PolicyBundle lifecycle。
scope relocation 也必须使用独立 workflow，不得在 revision chain 内更换 target。

approval/activation ID 是 canonical 内容身份。evidence/attestation hash 只是 linkage
值，不是签名、认证或授权。调用方 service 必须先认证 actor 并验证 attestation，再调用
这些 builder。模型可以 propose revision，但不能 approve 或 activate 自己的输出。

契约只保存 content metadata，不保存 plaintext。其 canonical Schema 与 example 为
`schemas/memory_revision_approval_v3.schema.json`、
`schemas/memory_revision_activation_v3.schema.json`、
`examples/memory_revision_approval_v3.example.json` 与
`examples/memory_revision_activation_v3.example.json`。Python 契约强制
activation sequence 等于 revision number；这一跨字段 invariant 强于独立 JSON
Schema。

opt-in [SQLite](sqlite-memory-publication-v3.zh-CN.md) 与
[PostgreSQL](postgres-memory-publication-v3.zh-CN.md) publication authority
现已持久化 approval/activation event、精确 authorization provenance 与
attestation-verifier identity。每次 transition 都是 transactional、append-only、
幂等的，并在 commit 前读回。activation 会锁定并验证 durable current head，而不是
信任调用方传入的 predecessor 字段。旧 proposal ledger 仍只是 proposal-only
dependency。

这些 authority 不保存 artifact plaintext、不提供 encryption/retention，也不把
activation 投影到 active version 2。调用方必须提供精确 artifact 字节/evidence 与
受信 attestation verifier。active-v2 integration 仍要求显式 migration。
