# Semantic Gate artifact 绑定 v3

[English](semantic-gate-artifact-v3.md)

`tbm.semantic-gate-artifact.v3` 将 provider 的精确 prompt 或 response 字节绑定到
一条不可变 `SemanticGateAttempt`，但不会把这些字节当作 prompt memory 或已经
finalize 的 runtime injection。

## 契约

`SemanticGateArtifactBinding` 记录：

- 精确的 `SemanticGateAttempt.attempt_id`；
- `prompt` 或 `response` 角色；
- 通用 `ContentAddressedArtifact` 描述符，其中包含由内容派生的 artifact ID、
  SHA-256、字节长度、媒体类型、分类、创建时间以及可选加密/脱敏元数据。

描述符不会内嵌或记录原始字节。Prompt artifact 上限为 128,000 个 UTF-8 字节，
与 32,000 字符 Gate prompt 边界按每字符最多四字节对齐。Response artifact 保留
64 KiB provider response 边界。空 artifact 会被拒绝；`confidential` 与
`restricted` 描述符必须提供 encryption-key 标识。

`create_semantic_gate_artifact_binding()` 对精确字节计算哈希，并要求 digest 与
attempt 对应角色的 digest 一致。`verify_semantic_gate_artifact_binding()` 联合
核验 attempt ID、角色、预期 digest、描述符长度、内容派生 ID 与精确字节。失败
attempt 可以绑定 prompt，但 attempt 契约禁止 response decision output，因此不能
绑定 response。

JSON 加载有大小边界、拒绝重复键、严格拒绝未知或缺失字段；规范序列化只包含
描述符。JSON Schema 校验结构 shape；内容派生 artifact-ID 关系与精确字节仍必须
由 Python parser/verifier 核验。

## 边界

这是存储中立的绑定契约。它本身不持久化 artifact 字节、不认证 provider、不建立可信
服务端时间戳、不验证静态加密，也不会以事务方式追加 Semantic Gate ledger 行、
GateSession revision 或 replay manifest。opt-in
[SQLite 仓库](sqlite-semantic-gate-artifact-v3.zh-CN.md)现已把精确
public/internal 字节与 attempt 原子持久化；opt-in
[PostgreSQL 仓库](postgres-semantic-gate-artifact-v3.zh-CN.md)现已提供同等的
原子精确字节边界及 catalog-validated install/rollback。
[认证 provider invocation 服务](semantic-gate-service-v3.zh-CN.md)与 opt-in
[durable GateSession 组合](durable-semantic-gate-v3.zh-CN.md)现已创建、核验并挂接
这些记录；更高层 durable facade 会继续推进 finalization，并由显式 durable
HTTP/MCP/SDK profile 暴露。静态加密、policy-backed artifact access control、签名
provider attestation 与默认兼容路径 cutover 仍是后续工作。Artifact 哈希只证明字节
身份，不证明作者身份或内容真实性。
