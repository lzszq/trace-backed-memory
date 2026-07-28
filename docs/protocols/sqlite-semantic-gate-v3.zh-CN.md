# SQLite Semantic Gate attempt ledger v3

[English](sqlite-semantic-gate-v3.md) | **简体中文**

这个 opt-in、side-by-side ledger 为一个不可变 `SystemGateEvaluation`
持久化有序 `SemanticGateAttempt` 链。它依赖 SQLite Gate evidence v3
authority，不会替代 active SQLite schema version 1，也不会替代 Agent/MCP
当前进程内的 Gate 生命周期。

## 追加契约

`SQLiteSemanticGateV3Repository.store_attempt()` 接收精确
`SemanticGateAttempt`，重新加载对应 `SystemGateEvaluation` 与
`RetrievalSnapshot`，核验三条记录后在一个 SQLite transaction 中追加 attempt。
首条 attempt 必须为 sequence 1 且没有 parent；后续 attempt 必须使用下一个
sequence，并以当前 attempt ID 作为精确 parent。

每个 System Gate evaluation 只有一个 CAS head。schema 强制：

- 每个 `(system_gate_evaluation_id, sequence)` 只能有一条 attempt；
- attempt chain 最多包含 100 条记录；
- head 与全部 attempt 必须属于同一 session 与 snapshot；
- 只能在当前 head 后追加；
- head 每次只能精确前进一步；
- attempt、head identity 与 head deletion 均不可变。专用 insert conflict guard
  还会在递归 replacement-delete trigger 被关闭时阻止 `INSERT OR REPLACE`
  替换既有 head 或 attempt。

完全相同的 canonical attempt 可幂等重放并返回 `inserted=False`。同一 parent
下的 sibling fork、跳号 sequence、同一 ID 的不同内容，或未延伸当前 head 的
attempt 都会 fail closed。

## 读取与事务契约

`load_attempt()` 与 `load_chain()` 会重新解析 canonical descriptor，对照全部关系
列、重新加载并核验 Gate evidence，并运行有界的完整 chain verifier。缺行、断链、
head 篡改、畸形 descriptor 或跨记录不匹配都会报错；repository 不会修复持久化
数据。

顶层写入使用 `BEGIN IMMEDIATE`。调用方已持有 transaction 时，repository 使用
savepoint 并保持外层 transaction 打开。每次操作都要求启用 foreign key 与
recursive trigger，并把全部具名 SQLite definition 与打包的 canonical schema
逐项比较。

canonical 资源为 `schemas/sqlite-v3-semantic-gate.sql`。必须先安装 Gate evidence
schema。connection 中存在调用方未提交工作时，不得调用 Python
`sqlite3.executescript()`：Python 会在执行 script 前提交这些工作。直接安装失败
时，必须回滚 script 仍打开的 transaction，以移除部分 schema object。优先使用
`SQLiteSemanticGateV3Repository.connect(initialize=True)`，它会在 repository
拥有的新 connection 上安装。

## 当前边界

ledger 保存 provenance descriptor 与 artifact hash，不保存 prompt/response
artifact 字节。它不认证 provider、不选择可信 timestamp、不追加 GateSession
revision，也不从 active Store、Agent 或 MCP 路径产生 attempt。
[PostgreSQL 对等实现](postgres-semantic-gate-v3.zh-CN.md)现已提供 shared-database
persistence parity；artifact 校验、授权、保留策略，以及与 GateSession/replay
服务的事务集成仍待完成。

SQLite 数据库管理员属于本地信任边界。Repository operation 会拒绝已关闭的必要
PRAGMA；只关闭 `recursive_triggers` 时，insert conflict guard 仍会阻止 replacement
write。拥有 DDL 或离线文件权限的管理员仍可移除并随后恢复 guard，同时把整条
chain 替换为另一条内部有效的 canonical chain；单个数据库无法区分这种重写与
原始历史。检测可信管理员或离线文件重写需要外部签名 audit/checkpoint
authority，本 ledger 不提供该能力。
