# SQLite Semantic Gate artifact 仓库 v3

[English](sqlite-semantic-gate-artifact-v3.md)

`SQLiteSemanticGateArtifactV3Repository` 是
`tbm.semantic-gate-artifact.v3` 的 opt-in 本地 durable store。它在同一
connection 上组合 Gate evidence 与 Semantic Gate attempt authority，不修改任何
既有 schema version。

## 安装与 schema

`connect(initialize=True)` 按顺序安装：

1. `schemas/sqlite-v3-gate-evidence.sql`；
2. `schemas/sqlite-v3-semantic-gate.sql`；
3. `schemas/sqlite-v3-semantic-gate-artifacts.sql`。

artifact 资源具有独立的 version-1 metadata singleton，并依赖既有 version-1
Semantic Gate ledger。它增加不可变 artifact-byte 表以及
`(attempt_id, artifact_role)` binding 表。Repository 会把全部受管 table、index
与 trigger 对照 packaged canonical definition，并拒绝附着在这些表上的意外
index 或 trigger。它还会拒绝遮蔽 artifact/parent-evidence 表或附着在这些受管
表上的 temporary object。

## 原子 operation

`store_attempt_with_artifacts()` 要求：

- 一份精确 prompt binding 与字节；
- succeeded attempt 的一份精确 response binding 与字节；
- failed attempt 不得带 response。

一个 `BEGIN IMMEDIATE` transaction 或一个嵌套 savepoint 会追加
SemanticGateAttempt、保存去重字节、保存角色 binding、重新加载完整 attempt
chain，并核验精确 artifact 读回。任何冲突或读回失败都会回滚整个 unit，包括新
追加的 attempt。精确 retry 返回逐行 insertion flag，不复制数据。
`load_attempt_with_artifacts()` 会重新核验 attempt chain、descriptor 列、角色
digest、内容派生 ID、长度与精确字节。

Repository 在自己的 connection 上注册确定性 `tbm_sha256(BLOB)`。数据库 trigger
重新计算 digest 与派生 artifact ID，解析并逐字段比较 binding descriptor，执行
prompt 媒体/长度和 response 长度/status 约束，并阻止 update、delete 与
replacement write。专用 insert-conflict guard 在关闭 `recursive_triggers` 时仍
有效；正常 repository operation 要求 foreign keys 与 recursive triggers 均启用。

## 安全边界

此仓库只保存 `public` 与 `internal` 字节，因为它不提供静态加密。
`confidential` 与 `restricted` binding 仍是有效的存储中立契约，但即使声明
encryption key 也会被本仓库拒绝。Artifact descriptor JSON 永不嵌入原始字节。

本仓库不认证 provider、不建立可信 timestamp、不执行 retention/access-control
policy、不追加 GateSession/replay 记录，也不从 active Agent/MCP 路径产生数据。
PostgreSQL 对等实现已单独记录；provider trust 与 active integration 仍是后续
工作。拥有 DDL 权限的 SQLite 文件所有者与管理员仍属于信任边界；检测完整离线
重写需要外部签名 checkpoint。
