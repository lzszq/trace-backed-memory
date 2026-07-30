# ADR-0001：v2 兼容与 durable-v3 切换

**状态：** 已接受
**日期：** 2026-07-30
**English:** [0001-v2-compatibility-durable-v3-cutover.md](0001-v2-compatibility-durable-v3-cutover.md)

## 背景

当前可运行产品使用 snapshot 2、SQLite 1、PostgreSQL 2 和
`tbm.agent.v1`。opt-in v3 authority 与 durable lifecycle 在内部更完整，但默认
MCP、HTTP、CLI 和 SDK 尚未选择它们。如果继续同时扩展两条路径，identity、
evidence、session、publication 与 retrieval 的语义会持续分叉。

## 决策

- 将 `tbm.agent.v1` 冻结为兼容协议，只处理安全、损坏和兼容性缺陷。
- 将 `tbm.durable-agent-wire.v1` 保持为 durable transport 契约；durable
  transport 稳定前不创建 `tbm.agent.v2`。
- 初始阶段显式提供 `compat-v2` 与 `durable-v3` profile。
- 只有 restart、migration 和跨 adapter exit test 全部通过后，新项目才可以默认
  durable-v3；旧项目绝不隐式切换。
- v2 进入只读及最终移除必须经过文档化 release 与 deprecation window。
- 禁止在没有单一原子事务或 append-only 兼容投影时独立 dual-write v2/v3。

## 影响

Transport 与 migration 优先于新的 standalone v3 capability。文档必须区分
`active`、`opt-in`、`contract-only`、`planned`。只有直接 Python 组合存在，
不能把 durable profile 标为 active。

## 退出证据

切换必须证明 prepare、decide、finalize、execute、complete、outbox delivery 和
已授权 replay export 在逐阶段 kill 后都能续接，并提供显式兼容回滚路径。
