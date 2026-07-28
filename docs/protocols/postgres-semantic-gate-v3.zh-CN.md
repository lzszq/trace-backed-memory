# PostgreSQL Semantic Gate attempt ledger v3

[English](postgres-semantic-gate-v3.md) | **简体中文**

这个 opt-in authority 为有序 `SemanticGateAttempt` chain 提供 PostgreSQL
对等实现。它位于隔离的 `trace_backed_memory_v3_semantic_gate` schema，
要求 active PostgreSQL schema version 2 与 Gate evidence v3，不改变 active
runtime schema 或 Agent/MCP 生命周期。

## 追加与重放契约

`PostgresSemanticGateV3Repository.store_attempt()` 按固定顺序锁定 active、
Gate evidence 与 semantic-ledger metadata。随后重新加载精确
`RetrievalSnapshot`/`SystemGateEvaluation`，先锁 snapshot、再锁 evaluation，
核验两份 descriptor，并通过一个 evaluation 的 head row 串行化该 chain。

首个 writer 在同一 transaction 中创建临时 sequence-zero head。新 attempt 必须
使用下一 sequence 与精确当前 parent；insert 后通过 exact CAS 推进 head。同一
evaluation 的第二个 writer 会等待该 head。完全相同的 canonical replay 返回
`inserted=False`；sibling fork、sequence 跳号或 immutable 内容冲突会原子失败。

每次读取都会重新解析 canonical descriptor、对照关系列、重验 Gate evidence，
并运行有界完整 chain verifier。repository 不修复持久化数据。

## 数据库侧约束

安装资源为 `schemas/postgres-v3-semantic-gate.sql`。隔离 schema 强制：

- 每个 System Gate evaluation 只有一个 head；
- `(system_gate_evaluation_id, sequence)` 与 attempt identity 唯一；
- sequence 只能为 1 至 100；
- session/snapshot/evaluation scope 精确一致；
- attempt 与 head identity 不可变；
- head 每次只前进一步；
- deferred commit-time chain consistency，空 head、未推进 head 的 attempt、
  orphan、断号或 branch 均不能 commit。

全部 trigger function 固定 `search_path=pg_catalog`。operation 会在本地替换
hostile caller search path，采用确定性 table/row lock，在工作前后验证精确安全
catalog fingerprint，并通过 psycopg savepoint 保留调用方已有 transaction。

`schemas/postgres-v3-semantic-gate-rollback.sql` 会依次锁 active、Gate evidence
与 semantic metadata，获取 access-exclusive table lock，核验精确 relation、
function、trigger、ACL/security catalog 与 canonical fingerprint，再使用
`RESTRICT`。catalog drift 或 external dependency 会使整个 rollback 中止。

## 当前边界

ledger 保存 attempt provenance 与 artifact hash，不保存 prompt/response
artifact 字节。它不认证 provider/timestamp、不追加 GateSession revision，也不从
active Store、Agent 或 MCP 路径产生 attempt。Artifact 校验、retention、授权，
以及与 GateSession/replay service 的事务集成仍待完成。

PostgreSQL schema owner 与 superuser 属于数据库信任边界；runtime role 不得拥有、
修改、禁用该 schema 的 trigger 或扩大其权限。检测可信管理员对一段内部完全有效
历史的重写，仍需要外部签名 audit/checkpoint evidence。
