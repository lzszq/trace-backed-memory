# SQLite MemoryRevision proposal ledger v3

[English](sqlite-memory-revision-v3.md) | **简体中文**

`SQLiteMemoryRevisionV3Repository` 是 opt-in、side-by-side authority，用于保存
immutable MemoryRevision proposal 及其精确 FixEvidence 和
StructuredRegressionEvidence bundle。它不改变 active SQLite schema version 1，
active Store、Agent 与 MCP runtime 也不使用它。

`store_proposal()` 会在 I/O 前验证完整 evidence bundle，原子保存全部记录及有序
链接，为每个 stable memory ID 强制一条线性 parent/revision sequence，并在 commit
前读回整个 bundle。精确 replay 幂等；冲突 ID、sequence slot、parent、descriptor
或 link 都会 fail closed。update/delete trigger 使记录只能向前。caller-owned
transaction 使用 savepoint，最终仍由 caller 控制。

该 ledger 只保存 proposal。它不认证 actor、不授权发布、不验证 artifact 字节或
签名、不 approve/activate revision、不投影到 active v2 memory，也不提供 retention/
encryption。这些操作必须由独立的认证 service 与 audit event 完成。
