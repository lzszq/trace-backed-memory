# Durable GateSession version-3 契约

[English](gate-session-v3.md) | **简体中文**

`tbm.gate-session.v3` 是未来 SQLite v2、PostgreSQL v3、`tbmd`、HTTP、MCP
与 SDK adapter 共用的、与持久化实现无关的 durable runtime 生命周期领域契约。
它不表示当前本地 MCP server 已经持久化 pending request。当前 runtime 仍是
snapshot v2、SQLite v1、PostgreSQL v2，以及进程内 pending Gate request。

## 身份与并发

每个 session 绑定以下由服务端解析的身份：

- tenant、canonical repository、principal 与 agent client；
- Trace 与 run ID；
- canonical request fingerprint 与调用方 idempotency key。

记录不可变。每次转换产生 `version + 1` 的新记录；调用方必须提交精确的
`expected_version`。stale revision 使用 `TBM_GATE_SESSION_STALE_VERSION`
失败。后续 repository adapter 必须在一个原子操作中执行同样的乐观并发检查。

`created_at`、`updated_at`、`expires_at` 与 lease 时间戳都是 service-authoritative
字段，agent client 绝不能自行选择。为保证 replay 的确定性，契约函数不会读取
wall clock；未来 repository/service 必须使用事务内数据库时间或可信 service
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

## 当前边界

`GateSession`、`create_gate_session()`、`transition_gate_session()` 与
`renew_gate_session_lease()` 定义并测试目标生命周期。它们不会重建
`MemoryGateRequest._store_token`、修改当前 Store、提升 snapshot/database
版本，也不会让现有 STDIO MCP 生命周期在重启后可恢复。只有后续统一迁移交付
authoritative repository、原子 idempotency index、expiry worker、recovery
与 adapter conformance 后，才能宣称 durable runtime 已实现。
