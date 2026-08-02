# 已认证加密 Artifact Authority v3

[English](artifact-authority-v3.md) | **简体中文**

该 opt-in 部署边界保存 `ContentAddressedArtifact` 引用的精确字节，但不会改变
active snapshot version 2、SQLite schema version 1 或 PostgreSQL schema version 2。

## 契约

`AuthenticatedArtifactService` 在接触 artifact repository 之前，必须获取并持久化读回
一条新的 `artifact:write` 或 `artifact:read` 授权 decision。授权复用现有 v3 request
形状，不向其内容派生身份加入新字段。

明文 SHA-256 仍是 artifact identity。调用方提供 authenticated-encryption/KMS
provider，返回密文与 nonce。service 将 descriptor、tenant、repository、environment、
写授权、provider、algorithm、key、retention 与可信存储时间绑定为 AAD；随后立即解密并
核验精确明文，再写入配置的不可变 authority。读取时再次执行授权、scope 检查、解密，以及
明文 digest/size 核验。provider 与 persistence failure 只通过稳定、净化后的 service
错误公开。

`schemas/sqlite-v3-artifact-authority.sql` 提供隔离的本地 version-1 schema。
`schemas/postgres-v3-artifact-authority.sql` 与
`PostgresArtifactV3Repository` 提供隔离 PostgreSQL version-1 对等实现，并由 active
PostgreSQL schema version 2 门禁。两者都只保存密文，拒绝
update/delete/truncate，使完全相同的 replay 幂等，拒绝内容派生身份冲突，通过
savepoint 保留调用方 transaction，并在 schema/catalog drift 时 fail closed。

PostgreSQL 对等实现会在受保护操作中固定 `search_path`，锁定并指纹化完整受管 catalog，
由数据库核验密文 SHA-256，支持并发精确 replay，并在成功前精确读回。其 `RESTRICT`
rollback 核验同一固定 catalog，并拒绝意外受管对象或外部依赖。

## Retention 边界

超过可信 `retain_until` 后读取会被拒绝，除非 `legal_hold` 为 true。不可变 authority
本身不执行物理清除、redaction、密钥销毁或 legal-hold 解除。opt-in
[Artifact retention 协议 v1](artifact-retention-v1.zh-CN.md) 在这些不可变 row 外组合独立的
可信 legal-hold/KMS 边界：记录受保护 manifest、推进当前 managed-index head、核验外部
key-destruction receipt，并追加 availability 为 `erased` 的 tombstone overlay。它绝不
删除或修改 ciphertext authority，也不会被默认 runtime profile 选择。object-storage
对等实现、产品级 KMS 配置、legal-hold 解除、active MemoryRevision 写入、GateSession
linkage 与 runtime injection 仍待完成。
