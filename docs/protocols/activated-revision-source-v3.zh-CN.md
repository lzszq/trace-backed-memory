# 已核验 ActivatedRevision source v3

[English](activated-revision-source-v3.md) | **简体中文**

`ActivatedRevisionSource` 是从 durable MemoryRevision publication 通向未来 v3
retrieval 的 storage-neutral、只读桥接层。它不会把记录投影到 active version-2 Store。

## 读取顺序

对于一条已认证的 `memory:retrieve` 请求，source 会：

1. 解析 target-scoped 当前 publication head；
2. 读取精确 activation、approval、authorization policy/request/decision，以及写入时
   attestation verifier identity；
3. 读取不可变 proposal、结构化 evidence 与 immediate lineage；
4. 要求配置明确信任 approval 与 activation 的 verifier ID；
5. 通过另一条已授权 `artifact:read` 解密内容，并核验明文 digest 与 size；
6. 重新执行完整 approval/activation/evidence/authorization verifier；
7. 再次读取 head，并拒绝并发变化；
8. 返回带规范 candidate digest 和两条访问授权 event ID 的
   `ActivatedRevisionCandidate`。

SQLite 与 PostgreSQL publication authority 均提供 `load_approval_bundle` 与
`load_activation_bundle`。其 storage-neutral bundle record 会在读取时重新核验精确
持久化 authorization decision。attestation 签名字节没有被保存：source 只信任明确
配置的写入时 verifier identity，不宣称重新认证原始签名字节。

candidate digest 覆盖不可变 revision、approval、activation 与 attestation verifier
identity。每次读取的 authorization ID 属于 audit evidence，刻意不参与 candidate identity。

## 边界

该 source 不执行 applicability selector、classification/leakage filter、Git ancestry、
ranking、RetrievalSnapshot emission、System/Semantic Gate、rendering 或 injection。
它不会把 proposal 变成 active，也绝不从 version-2 Lesson/Policy/usage record 推导
revision ID。当前加密 Artifact Authority 仍是 SQLite-local；PostgreSQL/object-storage
artifact 对等实现与 active Agent/MCP integration 仍待完成。
