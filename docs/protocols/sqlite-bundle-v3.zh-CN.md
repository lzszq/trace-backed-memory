# 统一 SQLite v3 bundle

[English](sqlite-bundle-v3.md) | **简体中文**

## 状态与边界

统一 SQLite v3 bundle 是显式 durable HTTP/MCP/SDK profile 与 `tbmd local`
选择的 storage graph。它不会改变 snapshot version 2、兼容 SQLite schema
version 1、PostgreSQL schema version 2 或 `tbm.agent.v1` 兼容边界。

`schemas/sqlite-v3.components.json` 是有序 component manifest。
`schemas/sqlite-v3.sql` 由其中 16 个 durable authority 与 migration-ledger
component 生成。`schemas/sqlite-v3-migration.sql` 仍可独立安装用于 staging，
同时也是 runtime bundle 的一个 component。

## 安装与校验

`install_sqlite_v3_bundle()` 在调用方提供的一条 connection 上，以一个外层
`BEGIN IMMEDIATE` 事务完成安装。确定性顺序保证 revision 先于 publication、
Gate evidence 先于 Semantic Gate、GateSession 先于 outcome 和 completion
outbox。它可与 active SQLite v1 表并存，不会修改这些表。

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
