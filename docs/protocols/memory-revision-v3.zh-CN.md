# 不可变 MemoryRevision v3

[English](memory-revision-v3.md) | **简体中文**

`tbm.memory-revision.v3` 是 storage-neutral、immutable proposal 契约。它不负责
approve、activate、suspend、supersede 或 obsolete memory；active snapshot/SQL
adapter 也不持久化它。

每个 revision 使用内容派生 ID，并绑定 stable memory ID/kind、revision number 与精确
parent、content-addressed artifact、canonical authorization scope、confidence/
sensitivity metadata、source FailureCase/Fix 引用、结构化 regression-evidence ID，
以及 server-owned proposer/client/attestation context。Lesson proposal 必须具备
全部 case、fix 与 regression 引用；Project-policy proposal 禁止这些 case-bound
引用，并必须使用 `policy` memory type。

`verify_memory_revision_evidence` 会解析每个 lesson evidence ID，要求同一 Failure
Case 的 passing evidence，并拒绝同时提交或验证该 evidence 的 proposer。这只是
proposal preflight：evidence/attestation hash 是内容身份，不是签名或授权。approval
与 activation 需要独立认证 service operation、当前 authorization decision 与
append-only AuditEvent。模型可以 propose revision，但不能 verify 或 activate。

契约只保存 content metadata，不保存 plaintext。所属 service 必须在发布前以事务方式
验证 artifact 字节、encryption、access control、retention、evidence existence、
parent continuity 与 monotonic revision number。
