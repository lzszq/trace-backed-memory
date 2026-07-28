# PostgreSQL Semantic Gate artifact 仓库 v3

## 边界

`PostgresSemanticGateArtifactV3Repository` 是一个 opt-in、隔离的 PostgreSQL
精确 Semantic Gate provider 字节仓库。它把现有 PostgreSQL
SemanticGateAttempt ledger 与 public/internal prompt、response artifact 及其
`tbm.semantic-gate-artifact.v3` 角色绑定组合起来。它不修改 active PostgreSQL
schema version 2，也未接入 Agent、MCP 或 GateSession transition。

该仓库不提供静态加密，因此拒绝 `confidential`、`restricted` 字节和非空
encryption-key 声明。artifact role 仅表示 prompt/response 关联，不代表
principal、tenant 或 provider authorization。

## 安装与回滚

按以下顺序安装：

1. `schemas/postgres-v3-gate-evidence.sql`；
2. `schemas/postgres-v3-semantic-gate.sql`；
3. `schemas/postgres-v3-semantic-gate-artifacts.sql`。

artifact 安装要求 active schema version 2 与 Semantic Gate schema version 1，
且只创建 `trace_backed_memory_v3_semantic_gate_artifacts`。

`schemas/postgres-v3-semantic-gate-artifacts-rollback.sql` 按 active、
Semantic Gate、artifact metadata 的顺序加锁，再锁 artifact 表。它在执行
`RESTRICT` 前验证完整的 schema/relation/column/constraint/function、ACL、
函数体与 trigger 指纹。catalog drift 或外部依赖会中止整个事务，并保留已安装
schema。

## 原子存储与精确重放

`store_attempt_with_artifacts()` 开启一个外层 PostgreSQL transaction。attempt
append 在嵌套 savepoint 中执行，随后写入 artifact 与 binding；因此 artifact
冲突会同时回滚新 append 的 attempt。调用方拥有的 transaction 仍由调用方控制。

artifact 同时按派生 artifact ID 与 SHA-256 去重；binding 按 attempt 与角色去重。
精确 replay 返回各项 insertion flag；相同 identity 下的不同内容属于冲突。每次
存储和读取都会重新解析 canonical descriptor、比较全部关系列、重新计算精确字节
hash，并验证 attempt/role digest。

数据库 trigger 还会独立执行：

- 从 `bytea` 重算 SHA-256 并验证派生 artifact ID；
- 执行 prompt media/size 与 response status/size 规则；
- 比较 descriptor 与 artifact/binding 的每个字段；
- 阻止 metadata、artifact 与 binding 的 update、delete 和 truncate。

## Catalog 与信任边界

operation 会把 transaction-local `search_path` 替换为 `pg_catalog`，锁定隔离
schema，并在操作前后验证完整 security-catalog 指纹。trigger/function/owner/
ACL/policy/rule/column/constraint 的禁用或漂移都会 fail closed。

schema owner 与 PostgreSQL superuser 仍是可信 operator；它们可以绕过仓库边界
改写数据库。hash 只能证明字节 identity，不能证明 provider authorship 或可信时间。
provider 认证、可信时间戳、签名外部 checkpoint、GateSession/replay transaction
挂接、加密敏感存储与 active emission 仍是独立后续工作。
