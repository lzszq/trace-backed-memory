# FixEvidence v3

[English](fix-evidence-v3.md) | **简体中文**

`tbm.fix-evidence.v3` 是 storage-neutral、内容寻址记录，把已 review 的 Failure
Case 绑定到一个精确 source Trace、source/fix commit、已验证的 source-to-fix
ancestry、有限 artifact hash，以及相互独立的 submitter/reviewer 身份。

submitter 与 reviewer 必须不同。commit ancestry 必须在提交前完成验证，review
不得早于提交。evidence ID 覆盖完整 canonical record，因此任何变化都必须生成新
ID。artifact/attestation hash 只是内容身份，不是签名、授权，也不证明引用字节存在。

`verify_memory_revision_evidence_bundle` 会为 lesson proposal 解析精确的
FixEvidence 与 StructuredRegressionEvidence，要求这些记录具有相同 case、source
Trace、source commit 和 fix commit，并确保 revision proposer 不属于任何 evidence
submitter、reviewer 或 verifier。

该契约不 approve 或 activate memory。所属 service 仍必须认证 actor，验证 artifact
与 attestation，授权读取和发布，执行 retention，并把 proposal/approval/activation
作为相互独立的 append-only operation 持久化。
