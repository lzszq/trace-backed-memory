# 应用与回滚本地 SQLite v3 迁移

[English](sqlite-v3-apply.md) | **简体中文**

本指南说明如何把经过验证的 snapshot version 2 或 SQLite schema version 1
源显式迁移到一个独立 SQLite v3 目标。命令不会修改源文件。PostgreSQL cutover
不属于这条命令。

## 应用前准备

先创建 migration mapping、运行预检并冻结一个 ready bundle：

```text
tbm migration plan-v3 SNAPSHOT_V2 MAPPING_JSON
tbm migration bundle-v3 SNAPSHOT_V2 MAPPING_JSON > migration-bundle.json
tbm migration verify-v3-bundle migration-bundle.json
```

若 mapping 要求可信 Git ancestry 验证，三条命令都要增加
`--repository-root REPOSITORY_ID=PATH`。Blocked bundle 不能 apply。

## 应用 snapshot 源

```text
tbm migration apply-v3 migration-bundle.json project.snapshot.json \
  --source-kind snapshot \
  --target sqlite \
  --database .tbm/durable.sqlite3 \
  --backup .tbm/project.snapshot.v2.bak
```

## 应用 SQLite v1 源

```text
tbm migration apply-v3 migration-bundle.json legacy.sqlite3 \
  --source-kind sqlite \
  --target sqlite \
  --database .tbm/durable.sqlite3 \
  --backup .tbm/legacy.sqlite3.v1.bak
```

源必须是已经存在的 single-link regular file。目标与备份必须是不同的输出路径；如果
任一路径已经存在，它也必须是 single-link regular file。目标不能包含无关数据。SQLite
源使用一致性 backup，因此会包含已经提交的 WAL state。

发布目标前，apply 会依次：

1. 精确重放 bundle preflight；
2. 要求 `state=ready`；
3. 严格加载源并匹配 normalized snapshot digest；
4. 创建或复验源备份；
5. 构造临时 SQLite v1 兼容副本；
6. 安装并指纹校验完整的 16-component SQLite v3 bundle；
7. 写入 immutable migration bundle、application、逐记录 disposition 与初始
   `durable-v3` profile event；
8. 验证 schema、源副本、备份、disposition 与 SQLite integrity；
9. 在不替换既有文件的前提下发布目标。

目标已经包含完全相同的 application 时，同一命令是幂等的。失败尝试可以留下
有效备份；下次尝试会复验并复用它。尚未发布的临时目标绝不会被视为迁移成功。
临时 payload 位于排他私有目录（POSIX 上为 `0700`）中，从写入到发布始终绑定 inode
identity，并接受 single-link regular-file 检查；发布不会跟随或替换符号链接。SQLite
writer 使用 `synchronous=FULL`，发布文件会执行同步，在平台提供 directory `fsync`
时也会同步父目录项。

## Legacy evidence 处理

迁移会把全部 v2 记录保留在兼容表中，但不会把旧状态伪装成 v3 authorization
或 publication：

| v2 记录 | v3 migration disposition |
|---|---|
| clean Trace | `legacy_trace`、`retained_legacy` |
| dirty Trace | `legacy_dirty_trace`、`retained_legacy` |
| 有 mapped regression preflight 的 FailureCase | `mapped_regression_preflight`、`retained_legacy` |
| 没有 mapped proof 的 FailureCase | `legacy_unverified`、`retained_legacy` |
| Lesson 或 ProjectPolicy | 即使 v2 status 是 `active`，也只为 `unpublished_v3` |
| MemoryUsageLog | `legacy_partial_replay`、`legacy_partial` |

原始 status（包括 `obsolete`）会被保留。迁移不会伪造独立 verifier attestation、
authorization decision、artifact encryption metadata、replay bytes、approval
event、activation event、tenant ID、repository ID 或 scope。ActivatedRevision
publication 与 active v3 retrieval 仍是后续独立工作。

## 验证

```text
tbm migration verify-v3 .tbm/durable.sqlite3
```

遇到 schema/catalog drift、bundle replay 差异、兼容 payload 被修改、备份缺失或
变化、record disposition 变化、profile history 无效、rollback database 改变或
SQLite integrity check 失败时，验证会 fail closed。命令会报告当前
`durable-v3` / `compat-v2` profile，以及确定性的 evidence/status counts。

## 回滚到 compat-v2

先停止 writer，再生成独立 SQLite v1 兼容数据库：

```text
tbm migration rollback-v3 .tbm/durable.sqlite3 \
  --compat-database .tbm/compat-v2.sqlite3
```

Rollback 会重建并验证精确 normalized v2 Store，再向 durable 目标追加一条
immutable `compat-v2` profile event。它不会删除 durable 数据库或其中的 v3
证据。写入该 event 后，durable runtime 会拒绝打开这个 migrated target；请把
兼容 client 指向命令报告的 SQLite v1 数据库。重复同一 rollback 是幂等的。
验证 snapshot、compatibility 发布和 profile event 由同一个 SQLite
`BEGIN IMMEDIATE` writer 边界保护；停止外部 writer 仍是 cutover 的操作前提。

## Python API

```python
from trace_backed_memory import (
    apply_sqlite_v3_migration,
    load_snapshot_v3_migration_bundle,
    rollback_sqlite_v3_migration,
    verify_sqlite_v3_migration,
)

bundle = load_snapshot_v3_migration_bundle("migration-bundle.json")
applied = apply_sqlite_v3_migration(
    bundle,
    source="legacy.sqlite3",
    source_kind="sqlite",
    target_database=".tbm/durable.sqlite3",
    backup=".tbm/legacy.sqlite3.v1.bak",
)
verified = verify_sqlite_v3_migration(".tbm/durable.sqlite3")
rolled_back = rollback_sqlite_v3_migration(
    ".tbm/durable.sqlite3",
    compatibility_database=".tbm/compat-v2.sqlite3",
)
```

若 bundle 使用 required ancestry，apply、verify、rollback 都必须传入同一个可信
`commit_relation_verifier`。
