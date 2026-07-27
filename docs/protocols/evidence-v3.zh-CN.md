# 结构化 regression evidence v3

[English](evidence-v3.md) | **简体中文**

`tbm.regression-evidence.v3` 是与存储实现无关、不可变的验证契约。它补充
migration-only `RegressionEvidence`，不会改变 snapshot version 2，也不会让 active
adapter 自动具备 evidence enforcement。

记录把 source Failure Case/Trace 绑定到不同的 verification Trace/run、evaluator
身份与版本、suite/case、expected/observed outcome、有界 environment metadata、
精确 source/fix/verification commit 关系、artifact hash 与 attestation hash。
submitter 与 verifier 必须是不同 principal。evidence ID 由 canonical record 内容派生，
因此任何修改都会 fail closed。

`pass` 只是证据，不是发布权限。激活仍需独立 review、authorization、lifecycle policy
与 immutable MemoryRevision。模型只能提议或收窄 memory，不能验证或激活自己的输出。
hash 只证明字节身份；所属 service 仍须认证 principal，并验证 attestation 与 commit
关系。

外部 JSON 上限为 1 MiB、depth 32、10,000 nodes。parser 拒绝 duplicate key、非法
UTF-8、非有限数、未知或缺失字段、非法时间戳、不一致 commit linkage、自我验证与
content-hash mismatch。
