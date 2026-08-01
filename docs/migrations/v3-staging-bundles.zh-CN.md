# Version-3 迁移 bundle 与隔离 staging

[English](v3-staging-bundles.md) | **简体中文**

本文定义可持久化的迁移准备层。Runtime snapshot 仍为 version 2，兼容 SQLite
schema 仍为 version 1，PostgreSQL 仍为 schema version 2，兼容 Agent 协议仍为
`tbm.agent.v1`。Ready bundle 现在可以通过
[apply/verify/rollback 工作流](sqlite-v3-apply.zh-CN.md)应用到独立本地 SQLite v3
目标。

## 不可激活的 bundle 契约

`tbm.snapshot.v2-to-v3.bundle.v1` 会冻结：

- 经过严格验证的原始 version-2 source snapshot；
- normalized source-snapshot digest；
- canonical operator mapping；
- 确定性的 preflight plan；
- 三份文档各自独立的 SHA-256；
- 由内容派生的 `bundle_id`；
- `ready` 或 `blocked` staging state。

原始与 normalized source digest 被刻意分开。原始 digest 用于发现 preflight
与未来 writer 之间的 array order 或 materialization 变化；normalized digest
标识严格 version-2 重建后的 Store state。

创建和复验命令：

```text
tbm migration bundle-v3 SNAPSHOT_V2 MAPPING_JSON \
  [--repository-root REPOSITORY_ID=PATH]...

tbm migration verify-v3-bundle BUNDLE_JSON \
  [--repository-root REPOSITORY_ID=PATH]...
```

对应 Python API：

```python
bundle = create_snapshot_v3_migration_bundle(
    source,
    mapping,
    commit_relation_verifier=trusted_verifier,
)
encoded = dumps_snapshot_v3_migration_bundle(bundle)
parsed = loads_snapshot_v3_migration_bundle(encoded)
verify_snapshot_v3_migration_bundle(
    parsed,
    commit_relation_verifier=trusted_verifier,
)
```

解析会检查封闭字段、有界 UTF-8 JSON、duplicate key、finite number、Unicode、
所有内嵌契约、所有文档 hash、plan readiness 与派生 bundle ID。复验会重新执行
完整 preflight，并要求 plan 精确重放。使用 required ancestry 创建的 bundle，
必须提供等价的 trusted verifier 才能再次通过验证。

## SQLite staging repository

`SQLiteV3MigrationRepository` 使用
`schemas/sqlite-v3-migration.sql` 持久化 immutable bundle。该 schema 既能独立
安装用于 staging，也是统一 SQLite v3 bundle 的一个 component。它以独立名称与
兼容表共存。Staging 不会修改 `trace_backed_memory_schema`，不会把 bundle
加载成 runtime memory，也不提供 activation 或 deletion API。

```python
with SQLiteV3MigrationRepository.connect(
    ".tbm/migrations.sqlite3",
    initialize=True,
) as staging:
    result = staging.stage(
        bundle,
        commit_relation_verifier=trusted_verifier,
    )
    replayed = staging.load(result.bundle_id)
```

Stage 使用一个 transaction；精确 replay 幂等；冲突内容会被拒绝；每次 load
都会重新验证 payload 与重复 digest columns。每次操作还会把已安装的 table/trigger
definition 与 canonical packaged DDL 比较，因此被削弱但仍声称相同 version 的 schema
会 fail closed。Update/delete trigger 保证 bundle row immutable。若调用方已经持有
SQLite transaction，stage 使用 savepoint，因此会随 outer transaction 一起 commit
或 rollback；内部 savepoint 重试后仍无法 release 时，会 rollback outer transaction，
而不会留下可被提交的残余状态。

Apply 工作流会在这个本地 ledger 中增加 immutable application、逐记录
disposition 与 profile event。它仍不提供 PostgreSQL 式 row lock、
database-side tenant authorization 或 multi-client isolation。

## PostgreSQL staging 与 rollback

提供两份 operator resource：

- `schemas/postgres-v3-staging.sql`
- `schemas/postgres-v3-staging-rollback.sql`

Staging script 在一个 transaction 中锁定 active public schema metadata row
`FOR UPDATE`，要求 PostgreSQL schema version 精确为 2，并创建
`trace_backed_memory_v3_staging`。它不会修改 public runtime table 或 active
schema version。因为隔离 schema 已存在，重复执行 creation script 会失败。
数据库 trigger 会拒绝对 staging metadata 和 bundle row 的 update、delete 与
truncate，但前提是 trigger 仍保持安装状态。Schema owner 与 database
superuser 能够修改或禁用 trigger，因此属于可信 operator 边界；application
writer role 不应拥有 staging objects。

Rollback script 会分别锁定并验证 active 与 staging metadata，删除已知 table
和 function，再用 `RESTRICT` 删除 `trace_backed_memory_v3_staging`。Metadata
缺失、被修改或版本不匹配，以及存在意外 staging object 或外部 dependency，
都会使 transaction 中止并完整回滚。Python runtime 不会自动执行任一 script。

PostgreSQL resource 只创建隔离存储，不提供 public bundle insertion adapter。
数据库 column check 可以约束有界 shape，但无法证明内嵌 JSON 与重复 digest
column 一致。未来 PostgreSQL writer 在 insert 前必须执行同一套 Python bundle
verification 与 exact-replay 检查。

## 安全边界

Bundle 是 content-addressed，不是 signed artifact。Operator 提供的 repository
locator hash、evaluator name、artifact hash、`verified_by` identity 与
global-policy approval ID 仍只是 declaration；未来 writer 必须把它们绑定到
认证 registry 与保留的 evidence。`ready` 只表示迁移输入通过当前 preflight，
不表示：

- memory 已激活；
- tenant authorization 已建立；
- evaluator 或 approval attestation 可信；
- version-3 runtime database 已存在；
- retriever/model/renderer decision replay 已完整。

Blocked bundle 可以为诊断而 staging，但不能 apply。禁用 ancestry 的 bundle
会继续保留明确 warning。应用 ready bundle 会创建 durable database 并保留
legacy records，但不会把这些记录发布为 ActivatedRevision runtime memory。
