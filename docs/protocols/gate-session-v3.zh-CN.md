# Durable GateSession version-3 契约

[English](gate-session-v3.md) | **简体中文**

`tbm.gate-session.v3` 定义未来 SQLite v2、PostgreSQL v3、`tbmd`、HTTP、MCP
与 SDK adapter 共用的 durable runtime 生命周期；domain 记录仍与持久化实现无关。
现在已有 opt-in、side-by-side SQLite 与隔离 PostgreSQL repository 持久化
immutable revision，但这
不表示当前本地 MCP server 已经持久化 pending request。active runtime 仍是
snapshot v2、SQLite v1、PostgreSQL v2，以及进程内 pending request。

## 身份与并发

每个 session 绑定以下由服务端解析的身份：

- tenant、canonical repository、principal 与 agent client；
- Trace 与 run ID；
- canonical request fingerprint 与调用方 idempotency key。

记录不可变。每次转换产生 `version + 1` 的新记录；调用方必须提交精确的
`expected_version`。stale revision 使用 `TBM_GATE_SESSION_STALE_VERSION`
失败。repository adapter 必须在一个原子操作中执行同样的乐观并发检查。

`created_at`、`updated_at`、`expires_at` 与 lease 时间戳都是 service-authoritative
字段，agent client 绝不能自行选择。为保证 replay 的确定性，契约函数不会读取
wall clock；repository/service 必须使用事务内数据库时间或可信 service
时间，并在真实 lease/expiry 已过时拒绝请求，不能接受 client 提交的旧时间戳。

## 生命周期

```text
CREATED
  -> PREPARED
  -> AWAITING_DECISION
  -> DECIDED
  -> FINALIZED
  -> EXECUTING
  -> COMPLETED

CREATED/PREPARED/AWAITING_DECISION -> CANCELED
PREPARED/AWAITING_DECISION         -> EXPIRED
EXECUTING                          -> ABANDONED
```

terminal 状态不能再次转换。`PREPARED` 到 `EXECUTING` 必须持有 active lease；
`COMPLETED`、`CANCELED`、`EXPIRED` 与 `ABANDONED` 会清除 lease。lease renewal
也是带版本的新不可变记录，并且必须在当前 lease 到期前完成。

生命周期字段只向前累积：

- preparation 记录 retrieval snapshot 与 System Gate evaluation；
- decision 记录 decision 与有序 semantic Gate attempts；
- finalization 记录精确 memory revision、injection artifact 与 usage decision；
- completion 记录 run outcome；
- cancel、expiry 与 abandon 记录有界 terminal reason。

本契约只保存这些 artifact 的引用；其各自的 version-3 契约与 repository
属于后续独立交付单元。

## 严格外部格式

canonical Schema 是 `schemas/gate_session_v3.schema.json`，打包示例是
`examples/gate_session_v3.example.json`。包括 nullable 字段在内的所有字段都
必须出现，并拒绝未知字段。

`loads_gate_session()` 将输入限制为 1 MiB、10,000 个 JSON node、depth 32，
并拒绝 duplicate key、非法 UTF-8、非有限数字、错误类型、未知字段、不可能的
生命周期形态与非 RFC 3339 时间戳。`dumps_gate_session()` 输出确定性 canonical
JSON，并将时间戳规范化为 UTC。

稳定契约错误包括：

- `TBM_GATE_SESSION_INVALID`
- `TBM_GATE_SESSION_INVALID_JSON`
- `TBM_GATE_SESSION_INVALID_TRANSITION`
- `TBM_GATE_SESSION_STALE_VERSION`

## Side-by-side SQLite repository

`SQLiteGateSessionRepository` 在 `schemas/sqlite-v3-gate-session.sql` 下保存
append-only revision payload 与 compare-and-swap current head。
`create_or_get()` 按 tenant、repository、principal 与 agent client 限定
idempotency；相同 request replay 返回既有 session，冲突 identity/fingerprint
不会覆盖。`transition()` 与 `renew_lease()` 使用可信 service clock，在一个
`BEGIN IMMEDIATE` transaction 或调用方 savepoint 中追加一条 validated revision，
并让 head 精确前进一个 version。`history()` 保留 revision chain，`list_due()` 只
返回有界当前候选，不自动修改。

adapter 还会针对连接关闭、foreign key 或 recursive trigger 禁用、schema drift、
持久化失败、session 不存在、时钟回退、scoped idempotency/session ID 冲突，以及
lease 或 session 过期后的转换，返回稳定的 `TBM_SQLITE_GATE_SESSION_*` 错误。数据库 trigger
即使面对 direct SQL 也会保护 append-only history、revision 连续性、生命周期图和
immutable head identity；repository 读取时仍会重新校验 canonical payload 与 head
identity。

## 隔离 PostgreSQL repository

`PostgresGateSessionRepository` 在独立安装的
`trace_backed_memory_v3_gate_session` schema 上提供相同的 create/get/history/
transition、lease renewal 与有界 due discovery 契约。规范安装与 fail-closed
rollback 分别是 `schemas/postgres-v3-gate-session.sql` 和
`schemas/postgres-v3-gate-session-rollback.sql`；两者都要求并保留 active
PostgreSQL schema version 2。

每项操作先锁 active/GateSession metadata，再锁 session row。create 使用数据库
强制、C collation 的 scoped idempotency；mutation 在读取
`clock_timestamp()` 前锁定 head row，在 transaction 或调用方 savepoint 中追加
canonical revision 并执行 exact-version CAS。catalog 检查拒绝缺失、额外或变形的
relation、constraint、index、function、trigger 与 column。固定 search path 的
trigger function 保护 immutable identity、history、lifecycle 连续性与 truncate
边界。deferred consistency trigger 会拒绝提交后 head 未精确指向最大 revision 的
transaction，包括 direct SQL 追加的 orphan revision；读取仍交叉校验每个 payload。

这些 repository 是 opt-in persistence seam，不是 active SQLite schema v2 或
active PostgreSQL schema v3。它们不会重建 `MemoryGateRequest._store_token`、
修改当前 Store，也不会让 STDIO MCP 在重启后可恢复。expiry/recovery worker、
opt-in authorization、preparation、Semantic Gate 与 completion service 现在已
使用这些 repository。active transport integration、finalization/replay
orchestration 与跨 adapter conformance 仍需交付，GateSession 才能成为
distributed runtime authority。
