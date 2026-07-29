# PostgreSQL MemoryRevision publication authority v3

[English](postgres-memory-publication-v3.md) | **简体中文**

`PostgresMemoryPublicationV3Repository` 是 SQLite publication authority 的隔离
PostgreSQL 对等实现。安装要求 active PostgreSQL schema version 2 与隔离
MemoryRevision proposal schema version 1；只创建
`trace_backed_memory_v3_memory_publication`，不改变 active table 或 schema version。

runtime lock 顺序为 active metadata、MemoryRevision proposal metadata/table，最后是
publication metadata/table。每个 operation 都把 `search_path` 固定为 `pg_catalog`，
验证精确 proposal/publication catalog fingerprint，并恢复调用方 search path。写入
使用 caller-compatible transaction/savepoint。approval 在验证 proposal/evidence/
artifact/attestation 后保存精确 event 与 authorization provenance。activation 对
durable head 加 row lock，重新验证完整 approval 与当前 activation authorization，
追加一条 immutable event，以 compare-and-swap 推进 head，并在 commit 前精确读回。

install SQL 撤销 public schema、table mutation 与 function execution privilege。
固定 search-path 的 trigger function 会拒绝 mutation、truncate、无效 proposal/
approval linkage、非 current predecessor 与无效 head advance。rollback script
按相同顺序锁定 dependency，检查预期 relation/function/trigger 与 security shape，
并使用 `DROP ... RESTRICT`；因此外部 dependent 会 fail closed。

attestation verifier 仍是调用方拥有的 authentication boundary。authority 不会把
version-3 activation 投影到 active version 2，也不提供 retrieval、retention、
encryption、suspension 或 obsolescence。
