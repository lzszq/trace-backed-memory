# ADR-0002：统一 version-3 数据库 bundle

**状态：** 已接受
**日期：** 2026-07-30
**English:** [0002-unified-v3-database-bundles.md](0002-unified-v3-database-bundles.md)

## 背景

当前 version-3 SQLite/PostgreSQL authority 使用隔离、side-by-side 的 component
schema。这些文件适合作为实现源，但文件数量、安装顺序、版本与 rollback 依赖不能
成为最终 operator interface。

## 决策

- 保留 component SQL 作为经过 review 的实现源。
- 通过有序 component manifest 生成一个 SQLite install bundle 和一个
  PostgreSQL install bundle。
- 每个受支持数据库 profile 提供一个 v2→v3 migration、一个 verifier 和一个
  rollback plan。
- manifest 记录精确 component 名称、契约版本、规范字节 digest 与安装顺序。
- Runtime factory 必须在发布 service bundle 前核验完整 catalog。仅检查 metadata
  row 不足够；表、列、约束、索引、trigger 和 PostgreSQL function 必须匹配。
- Runtime 启动绝不隐式安装或迁移 PostgreSQL。

## 影响

Bundle version 成为兼容边界；没有 migration、rollback、文档和 fixture coverage
时不能变化。任何 component drift 都必须在接受 durable request 前失败。

## 退出证据

SQLite 与 PostgreSQL 都覆盖 fresh install、upgrade、restart、drift rejection、
failed-install rollback 和 operator rollback。
