# 实体注册表 v3

**简体中文** | [English](entity-registry-v3.md)

`tbm.entity-registry.v3` 是补全 authorization-v3 租户命名空间的、与存储实现
无关的身份层级。它新增不可变的 Organization、Tenant 与 Environment 记录，同时
复用现有授权 policy 中的 Principal、AgentClient、canonical Repository、
RepositoryAlias 与 RoleBinding 记录。

## 不变量

- 每个 tenant 必须属于一个已知 organization。
- principal、client、repository binding、alias 或 role-binding scope 引用的
  每个 tenant 都必须存在于注册表、处于 active 状态，且属于 active organization。
- authorization-v3 继续要求每个 canonical repository 恰好有一个 tenant
  binding，并要求 tenant 范围内的 alias 无歧义。
- environment 属于一个 tenant；若它指定 repository，该 repository 必须已知且
  绑定到同一 tenant。
- 各 collection 内的实体标识唯一。status 必须显式表达；后续持久化应以
  forward-only 方式保留不可变身份字段。

注册表是带版本且内容寻址的快照。`registry_sha256` 由 canonical JSON 派生，
嵌套授权 policy 仍保留独立的 `policy_sha256`。

## 信任边界

该契约验证引用完整性，但不认证调用方，也不授权操作。服务必须从可信认证中派生
Principal 与 AgentClient，加载已接受的注册表快照，在 retrieval 之前完成授权，
并记录授权 decision。仅做 scope matching 不是 tenant security。

opt-in `SQLiteEntityRegistryV3Repository` 会安装隔离、side-by-side schema。
其规范化 snapshot namespace 通过复合外键保存全部 entity、binding、permission 以及
scope/environment attribute。canonical JSON 只是完整性见证：每次读取都会把每张
规范化表逐行与 descriptor 复验。所有行不可变；精确重放幂等；version/hash 冲突
fail closed；每次操作检查 schema drift 与必需 PRAGMA；调用方事务通过 savepoint
保留。

active v2 Store、Agent 与 MCP adapter 尚未使用该注册表。PostgreSQL 持久化和
authenticated service integration 是后续独立交付步骤。

## 资源

- `schemas/entity_registry_v3.schema.json`
- `examples/entity_registry_v3.example.json`
- `schemas/authorization_policy_v3.schema.json`
- `schemas/sqlite-v3-entity-registry.sql`
