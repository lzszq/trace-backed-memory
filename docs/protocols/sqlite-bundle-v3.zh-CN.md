# 统一 SQLite v3 bundle

[English](sqlite-bundle-v3.md) | **简体中文**

## 状态与边界

统一 SQLite v3 bundle 是 opt-in 的本地 durable storage 契约。它不会改变
active snapshot version 2、SQLite schema version 1、PostgreSQL schema
version 2 或 `tbm.agent.v1` 兼容边界。默认 compatibility MCP/HTTP/CLI/SDK
profile 不会选择它；显式 durable HTTP/MCP profile 与 durable SDK client 会选择它。

`schemas/sqlite-v3.components.json` 是有序 component manifest。
`schemas/sqlite-v3.sql` 由其中 16 个非迁移 authority component 生成。
`schemas/sqlite-v3-migration.sql` 继续作为隔离 staging，不进入 runtime bundle。
event-ledger component 会被安装并纳入指纹。durable runtime 已为 event-first
GateSession、Retrieval/System Gate evidence、Semantic attempt 与 finalization slice
选择它，并使用 ledger metadata + replay-authority bytes 提供 finalized replay export；
但它尚未成为每个 authority 或 projection 的唯一事实源。

## 安装与校验

对新数据库，`install_sqlite_v3_bundle()` 在调用方提供的一条 connection 上，以一个
外层 `BEGIN IMMEDIATE` 事务完成安装。确定性顺序保证 revision 先于 publication、
Gate evidence 先于 Semantic Gate、GateSession 先于 outcome 和 completion outbox。
它可与 active SQLite v1 表并存，不会修改这些表。

bundle 会记录：

- bundle contract 与 schema version；
- 精确有序 component set 的 SHA-256；
- 每个 component 的版本及可选 contract version；
- 完整受控 SQLite catalog 的 SHA-256。

`verify_sqlite_v3_bundle()` 要求启用 foreign keys 与 recursive triggers，
校验每个 component metadata row，并对 `sqlite_master` 和
`sqlite_temp_master` 中全部受控 table、index、automatic index 与 trigger
做指纹。缺失、变更、部分安装或额外的 `v3_*` 对象都会 fail closed。bundle
metadata 不可变。

application factory 使用同一 installer 与 verifier：

```python
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeFactory,
)

runtime = DurableRuntimeFactory(dependencies).open_sqlite(
    ".tbm/durable-v3.sqlite3",
    initialize=True,
)
```

已安装数据库用 `initialize=False` 重新打开。初始化要求 connection 处于 idle；
安装错误会回滚整个 bundle transaction。

## GateSession timestamp 兼容修复

单独打包的 `schemas/sqlite-v3-gate-session-timestamp-hotfix.sql` 是兼容修复输入，
不是第 17 个 bundle component。公开
`apply_sqlite_v3_gate_session_timestamp_hotfix()` 边界要求 idle connection，且数据库
必须是精确 current bundle 或唯一受支持的 legacy component set。legacy 路径取得
`BEGIN IMMEDIATE` 锁后，会核验精确 bundle metadata、catalog、GateSession schema 与
variable-precision timestamp trigger，再替换 trigger、更新 component-set metadata、
核验当前 16-component bundle 并提交。前置条件、替换、读回或 bundle verification
任何一步失败，整个 transaction 都会回滚。current 路径只做幂等核验。

`install_sqlite_v3_bundle()` 发现已有 bundle metadata table 时会自动调用该修复。
operator 应调用 Python 边界，而不是直接执行打包 SQL，因为精确 source-schema 核验属于
应用边界。无关 schema drift 会 fail closed，绝不会被覆盖。该修复不支持原地 downgrade；
如需运维回滚，应恢复升级前 backup。

## 编写流程

各 component SQL 仍是 authoring source。明确修改其中任何文件后，依次重新生成
并校验两个生成层：

```text
python tools/generate_sqlite_v3_bundle.py --refresh
python tools/generate_sqlite_v3_bundle.py --check
python tools/generate_resources.py --refresh
python tools/generate_resources.py --check
```

生成器不访问网络。`python tools/verify.py --all` 会在测试与发行校验前运行这两项
检查。
