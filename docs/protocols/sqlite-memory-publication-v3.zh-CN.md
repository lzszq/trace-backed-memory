# SQLite MemoryRevision publication authority v3

[English](sqlite-memory-publication-v3.md) | **简体中文**

`SQLiteMemoryPublicationV3Repository` 是 opt-in、side-by-side authority，用于
保存 immutable `MemoryRevisionApproval` 与 `MemoryRevisionActivation` event。
它依赖 SQLite MemoryRevision proposal ledger，并保持 active SQLite version-1
Store、Agent、MCP profile 与 snapshot format 不变。

approval 写入会重新验证精确 stored proposal/evidence 闭包、artifact 字节、
`memory:review` policy/request/decision、actor 分离与调用方提供的 attestation
verifier。canonical policy、request、decision、event 与 verifier identity 会一起
保存，并在 commit 前读回。精确 replay 幂等；identity 或 revision slot 不匹配会
fail closed。

activation 在 `BEGIN IMMEDIATE` 内读取 durable target/memory head、排斥竞争 writer、
重放完整 stored approval provenance、重新验证 artifact/evidence 与
`memory:activate`、调用 attestation verifier、追加 immutable activation，并用精确
sequence/parent compare-and-swap 推进 head。它从不信任调用方提供的 predecessor。
tenant-scoped head 使用显式非空 repository key，避免 SQL `NULL` uniqueness 产生重复
head。

schema 使用 immutable approval/activation row、受保护 head transition、指向 proposal
和 approval 的 foreign key、不可删除 head、精确 canonical schema 检查、
recursive-trigger/foreign-key 检查、connection-wide lock、nested savepoint、
失败回滚与 commit 前读回。approval/activation row 同时构成 append-only publication
audit trail，并保留精确 authorization provenance。

attestation callback 是 service boundary，不是内置签名方案。它必须认证 actor 并验证
所引用 attestation，repository 自身不会增加隐式网络访问。SQLite 没有数据库 ACL
boundary；拥有不受限 direct database write 的 actor 位于 adapter trust boundary
之外。该 authority 不执行 active-v2 projection、retrieval、retention、encryption、
suspension 或 obsolescence。
