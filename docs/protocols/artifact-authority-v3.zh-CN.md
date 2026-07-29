# 已认证加密 Artifact Authority v3

[English](artifact-authority-v3.md) | **简体中文**

该 opt-in 本地部署边界保存 `ContentAddressedArtifact` 引用的精确字节，但不会改变
active snapshot version 2、SQLite schema version 1 或 PostgreSQL schema version 2。

## 契约

`AuthenticatedArtifactService` 在接触 artifact repository 之前，必须获取并持久化读回
一条新的 `artifact:write` 或 `artifact:read` 授权 decision。授权复用现有 v3 request
形状，不向其内容派生身份加入新字段。

明文 SHA-256 仍是 artifact identity。调用方提供 authenticated-encryption/KMS
provider，返回密文与 nonce。service 将 descriptor、tenant、repository、environment、
写授权、provider、algorithm、key、retention 与可信存储时间绑定为 AAD；随后立即解密并
核验精确明文，再写入不可变 SQLite 记录。读取时再次执行授权、scope 检查、解密，以及
明文 digest/size 核验。provider 与 persistence failure 只通过稳定、净化后的 service
错误公开。

`schemas/sqlite-v3-artifact-authority.sql` 是隔离的 version-1 schema。它不保存明文，
并拒绝 update/delete。完全相同的 replay 幂等；内容身份冲突与 schema drift 均 fail
closed。repository 通过 savepoint 保留调用方 transaction。

## Retention 边界

超过可信 `retain_until` 后读取会被拒绝，除非 `legal_hold` 为 true。该不可变本地 ledger
尚不执行物理清除、redaction、密钥销毁或 legal-hold 解除；operator 仍须管理外部密钥
生命周期与存储策略。PostgreSQL/object-storage 对等实现、KMS/provider 认证、签名
attestation、active MemoryRevision 使用、GateSession linkage 与 runtime injection
仍待完成。
