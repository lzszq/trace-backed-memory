# 审计事件与恢复动作 v3

[English](audit-recovery-v3.md) | **简体中文**

`AuditEvent` 是 storage-neutral、append-only 的事件 envelope。内容派生 ID
覆盖 stream、单调 sequence、精确 parent、tenant/repository/session/run 身份、
已认证 actor context、有界 reason code、payload 摘要、规范 typed reference
与 server timestamp。parent verifier 会拒绝缺口、伪装成线性延续的分叉、
跨 stream parent 与时间倒置。

`RecoveryAction` 记录一次已完成恢复尝试。memory-run action 必须对照精确 before/
after 派生 `MemoryRunRemediation` 核验，不能替代 Store-owned source of truth。
GateSession action 记录 expected immutable session version，并对照允许的 before
state 与精确 resulting revision（失败时状态不变）核验。request fingerprint、
请求 principal 与 event executor 也必须绑定 GateSession。每个 recovery action
必须由匹配的 `recovery_succeeded` 或
`recovery_failed` AuditEvent 引用。

两个契约都不会执行恢复、授权 actor、认证 identity 或自行持久化。Schema 只做
结构预检。service 必须执行 runtime 自哈希与跨记录核验，从 authenticated context
派生 identity，在事务中强制 stream sequence 与 request-hash 唯一性，认证所有
绑定 identity slot，使用可信时间，
禁止 update/delete/truncate，并原子写入 action、event 以及底层 Store 或
GateSession transition。raw prompt、tool output、secret 与无限 error 应保存在
受控 artifact 中；event payload 只保存 hash 与 identifier。

opt-in `SQLiteAuditV3Repository` 通过 `schemas/sqlite-v3-audit.sql` 实现隔离的
本地 evidence ledger。它保留精确 stream identity，通过 CAS head 每次推进一个
parent-linked event，原子追加一条 RecoveryAction 及其匹配的成功/失败 event，
拒绝 session-scoped request-digest 碰撞，在读取时重新核验 canonical descriptor，
并禁止 update 或 delete event/action。该 ledger 不派生 authenticated actor，
也不包含底层 Store/GateSession transition；service integration 必须补齐这些检查
和更大的原子 unit of work，才能把 append 视为已授权 recovery。

opt-in `PostgresAuditV3Repository` 通过 `schemas/postgres-v3-audit.sql`
及其 fail-closed rollback 提供匹配的隔离多进程 ledger。它使用 stream-head
row lock 串行化 writer，通过精确 CAS 推进 head，借助 psycopg savepoint 保留
调用方 transaction，并在每次操作核验 metadata、relation、index、constraint、
column、trigger 绑定/状态及 canonical 固定 `search_path` function body。
deferred database check 要求每个 event 都提交到对应 head，且每个
RecoveryAction 形成唯一精确匹配 pair。这不会扩大 authorization 或 service
transaction boundary。

现有派生 `MemoryRunAudit`、`MemoryRunRemediation`、health metrics 与 version-2
usage log 保持不变。event ledger 是操作证据，不是另一套 lifecycle/outcome
authority。

规范 Schema：

- `schemas/audit_event_v3.schema.json`
- `schemas/recovery_action_v3.schema.json`
- `schemas/sqlite-v3-audit.sql`
- `schemas/postgres-v3-audit.sql`
- `schemas/postgres-v3-audit-rollback.sql`
