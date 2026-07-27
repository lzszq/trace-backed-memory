# 内容寻址重放契约 v3

[English](replay-v3.md) | **简体中文**

`tbm.replay.v3` 定义与存储实现无关的记录，用来描述 memory decision finalize
之后实际注入的精确字节，以及重放该 decision 所需的固定证据集合。契约模块本身
不激活 memory，也不会把当前进程内 Gate 变成 durable Gate。现有 opt-in 隔离
SQLite repository 可持久化这些记录；隔离 PostgreSQL 安装与 fail-closed rollback
schema 建立相同的不可变关系边界；opt-in PostgreSQL repository 现已提供精确
byte/descriptor 验证。两个 ledger 都尚未连接 active runtime state。

## 内容身份

`ContentAddressedArtifact` 从精确字节派生 `content_sha256`，再从该摘要派生
`artifact_id`。验证同时比较大小、摘要与派生身份。descriptor 只记录 media type、
数据分类、创建时间，以及可选的加密/脱敏 metadata，不记录内容本身。
`confidential` 与 `restricted` descriptor 必须提供 `encryption_key_id`；契约本身
不执行加密。

一般 artifact 上限为 64 MiB。注入 snippet 必须是 UTF-8
`text/plain; charset=utf-8`，上限为 1 MiB。这些 artifact 上限不会放宽现有 runtime
renderer 的预算。

## InjectionArtifact

`InjectionArtifact` 把最终 snippet 精确绑定到一个 GateSession、decision、usage
decision、有序且不可变的 memory revision、renderer 身份/版本、policy bundle 摘要
与 render 时间。读取方可用内容派生身份拒绝被修改的字节。render 时间必须等于内容
descriptor 的创建时间。`verify_injection_artifact()` 只验证 snippet 字节；
descriptor metadata 必须来自可信且原子链接的记录。未来 service（而不是该
constructor）必须证明所引用 GateSession 已 finalized，且 revision 是最终允许集合。

规范外部契约为 `schemas/injection_artifact_v3.schema.json`，打包示例为
`examples/injection_artifact_v3.example.json`。

## DecisionReplayManifest

`DecisionReplayManifest` 使用固定且顺序规范的 component map：

1. retrieval snapshot；
2. System Gate evaluation；
3. semantic Gate prompt；
4. semantic Gate response；
5. ancestry evidence；
6. policy bundle；
7. renderer；
8. injection artifact。

每个非空值都是带算法标签的 SHA-256 摘要。`complete` manifest 必须绑定全部
component 及 injection artifact ID。`legacy_partial` 是唯一允许的不完整状态，且
必须精确列出值为 null 的 component；它只是明确的迁移事实，不允许声称可精确重放。

manifest 对除 `manifest_sha256` 外的所有字段计算规范 self-hash。injection artifact
ID 必须从 injection component 摘要派生。规范外部契约为
`schemas/decision_replay_manifest_v3.schema.json`，打包示例为
`examples/decision_replay_manifest_v3.example.json`。

## Opt-in SQLite 重放账本

`SQLiteReplayV3Repository` 使用隔离的 `schemas/sqlite-v3-replay.sql` schema，
不会改变 active SQLite schema version 1。它把精确 artifact 字节、injection
descriptor 与 replay manifest 保存为 immutable row。`store_bundle()` 在一个事务
中插入 artifact、injection 与 manifest，要求 session/decision/usage 和 injection
linkage 精确一致，并把精确重放视为幂等；冲突内容会回滚整个操作。

每次 load 都先检查有界大小，再读取大值、重解析 descriptor、比较重复 relational
column，并重新计算已存字节 hash。canonical schema metadata、table、index、
immutable trigger、foreign key 和调用方 savepoint ownership 都会 fail closed
校验。该账本不授权读取、不加密内容、不执行 retention、不证明 evidence truth，也
不连接 durable GateSession。该 repository 拒绝 confidential/restricted artifact，
直到透明加密 provider 能够在加密的同时保留精确内容身份。

## 隔离 PostgreSQL 重放 schema

`schemas/postgres-v3-replay.sql` 仅在 active PostgreSQL schema version 2 时安装
隔离的 `trace_backed_memory_v3_replay` schema。它保存有界字节与不可变
injection/manifest descriptor，从声明 digest 派生 artifact ID，强制精确的
manifest-to-injection identity linkage 以及 injection byte/media bound，并用固定
`search_path` trigger 拒绝 update、delete 与 truncate；active v2 表保持不变。
与 SQLite DDL 相同，在 encryption 实现前它会拒绝 confidential/restricted 字节。
安装在单一事务内完成，并在创建对象前锁定 active schema metadata。

`schemas/postgres-v3-replay-rollback.sql` 会锁定两份 metadata 与全部 ledger table，
核对预期 relation、function、trigger、constraint 与 column catalog membership，
再使用 `RESTRICT`
仅删除隔离 schema。额外对象、外部依赖、metadata drift 或 active version 不是 2
都会让整个 rollback 失败。SQL 本身不重算 content SHA-256 或解析 canonical
descriptor。`PostgresReplayV3Repository` 会在每次 insert 前和 load 后执行这些
检查，把精确 replay 视为 idempotent，并使用嵌套 psycopg transaction 保留 caller
work ownership，同时拒绝 catalog/function/trigger drift。它不提供访问授权、加密、
保留、GateSession linkage 或 service 事务。

## 解析与信任边界

外部 JSON 上限为 1 MiB、depth 32、10,000 nodes。parser 拒绝 duplicate key、非法
UTF-8、非有限数字、缺失或未知字段、非法时间戳与非规范 component set。hash 只能
证明字节身份，不能证明作者身份、授权或证据真实性。

JSON Schema consumer 必须执行 draft 的 `date-time` format 检查。canonical
self-hash 与内容派生 ID 的关系属于 value-level 规则，必须在 Schema 验证后继续用
Python parser 检查。

当前 v2 Store、active SQLite v1 adapter、PostgreSQL v2 adapter、本地 Agent 与
STDIO MCP 均不会持久化或输出这些记录。opt-in SQLite 账本提供原子字节/descriptor
存储；opt-in PostgreSQL repository 提供相同的原子 byte/descriptor storage。未来 runtime 只有在授权
读取、执行 retention/encryption，并链接 finalized GateSession 后，才能声称支持
完整 decision replay。
