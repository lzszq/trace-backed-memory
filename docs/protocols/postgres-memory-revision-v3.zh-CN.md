# PostgreSQL MemoryRevision v3 proposal ledger

[English](postgres-memory-revision-v3.md)

可选启用的 `PostgresMemoryRevisionV3Repository` 会把不可变的
`MemoryRevision` proposal 连同其精确 `FixEvidence` 和有序
`StructuredRegressionEvidence` 闭包一起保存。它是隔离 SQLite proposal ledger
的 PostgreSQL 对等实现，不改变当前 PostgreSQL schema version 2。

安装与回滚资源：

- `schemas/postgres-v3-memory-revision.sql`
- `schemas/postgres-v3-memory-revision-rollback.sql`

安装器在创建隔离的 `trace_backed_memory_v3_memory_revision` schema 前锁定当前
schema metadata。repository 会在每次成功操作的前后校验 catalog fingerprint，将
`search_path` 固定为 `pg_catalog`，并按 active metadata、ledger metadata、ledger table
的顺序加锁。读操作使用 `ACCESS SHARE`；写操作使用 `ROW EXCLUSIVE`，因此写入角色必须
具备表写权限。每次读取都会验证最多 10,000 层的完整父 revision 链与精确 evidence
闭包。任何 descriptor、evidence link、trigger、ACL 或 catalog 漂移都会 fail closed，
重放也不会自动修补被篡改的 evidence。

Schema owner 与 PostgreSQL superuser 仍是可信 operator：它们可以修改 function 或绕过
数据库保护，包括在操作过程中短暂修改。不要让 repository 流量与特权 DDL 并发运行。

该 ledger 只保存 proposal。它不执行 review、verify、approve、activate、authorize、
retain、obsolete 或 memory injection；这些状态转换仍由正常 lifecycle 与 Gate 契约控制。

rollback 会锁定全部 ledger relation，并在 metadata、relation、function、trigger、ACL
或 catalog 不匹配时 fail closed。它只删除隔离 schema，不改变当前 PostgreSQL version 2。

另见：[SQLite proposal ledger](sqlite-memory-revision-v3.zh-CN.md)。
