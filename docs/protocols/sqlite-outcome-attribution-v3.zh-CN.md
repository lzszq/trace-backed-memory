# SQLite OutcomeAttribution Ledger v3

[English](sqlite-outcome-attribution-v3.md) | **简体中文**

这个 opt-in ledger 会针对已完成的 durable GateSession 及其保留的 RunOutcome
持久化 immutable `tbm.outcome-attribution.v3` 记录。它与 RunOutcome completion
transaction 分离，因为契约允许同一个 outcome 保存多条独立产生的 association 或
causal claim。它不会改变 active SQLite schema version 1，也不会改变 active
Agent/MCP 行为。

## 安装与 API

`SQLiteOutcomeAttributionV3Repository.connect(..., initialize=True)` 会依次安装
隔离 GateSession、RunOutcome 与 OutcomeAttribution schema。既有安装必须在前两个
依赖已经存在后，单独应用 `schemas/sqlite-v3-outcome-attribution.sql`。

repository 暴露：

- `put_attribution(attribution)`：精确 immutable append 或 replay；
- `get_attribution(attribution_id)`：读取一条已验证记录；
- `list_attributions(run_outcome_id)`：按 `recorded_at`/identity 确定性排序；
- `outcomes`：共享的 RunOutcome 与受保护 GateSession authority。

相同 content ID 的精确 replay 返回 `inserted=False`。同一个 outcome 可以保存多条
attribution；领域契约没有定义“每 outcome 只能一条 attribution”的唯一性规则。

## 完整性与事务

每次操作都要求 SQLite foreign key 与 recursive trigger 开启，校验精确 canonical
managed schema，并拒绝临时 shadow 或额外 index/trigger。一次 transaction，或调用方
兼容 savepoint，会完成 linkage 校验、immutable insert、精确读回与第二次跨记录校验。

insert guard 通过同一个严格 Python contract parser 校验完整 canonical descriptor，
要求每个重复关系字段与 JSON 字段逐字节等于 Python canonical serialization，独立
重新校验关联的 RunOutcome row，要求 memory revision 与 evidence 数组有序且唯一，
强制 association/causal shape，以解析后的 RFC3339 instant 比较时间，并把 claim
关联到：

- 已存在的 immutable RunOutcome；
- 其当前 `COMPLETED` GateSession revision；
- 精确 usage decision；
- 仅该 session 已 finalized 的 memory revisions；
- 不早于 outcome measurement 的 `recorded_at`。

repository 读回时会独立重新解析并重算 descriptor hash、比较每一列、重新加载 outcome
与 session，并执行 `verify_outcome_attribution()`。UPDATE、DELETE、replacement
INSERT、malformed descriptor、schema drift 或 partial write 都会 fail closed。

## 信任边界

该 ledger 把调用方提供的 evaluator/verifier ID、artifact hash 与 timestamp 作为
storage-neutral provenance 保存。它不认证 principal、不核验 artifact bytes、不建立
trusted clock，也不会把 association 提升为 causal claim。service 必须在构造
content-addressed record 前派生 authenticated identity 与 trusted time。

隔离的
[PostgreSQL attribution ledger](postgres-outcome-attribution-v3.zh-CN.md)提供
database parity。独立的
[SQLite 与 PostgreSQL completion outbox](completion-outbox-v3.zh-CN.md)
会通过更高层 durable execution/facade 组合发布 completion event。显式 durable
HTTP/MCP 或 Python/TypeScript client lifecycle 尚不会追加 attribution claim；
attribution outbox delivery、独立 verifier/artifact check 与默认兼容路径 cutover
仍是后续工作。
