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

runtime crypto-erasure 不会把 `complete` manifest 改写为 `legacy_partial`。opt-in
Artifact retention 协调器会另行追加 content-addressed `ReplayPartialMarker` sidecar，
绑定精确 erased Artifact ID 与新增的不可用 component。选择该 sidecar 的 consumer 必须
拒绝 exact replay；既有 replay repository row 保持不可变。`legacy_partial` 仍仅供迁移。

manifest 对除 `manifest_sha256` 外的所有字段计算规范 self-hash。injection artifact
ID 必须从 injection component 摘要派生。规范外部契约为
`schemas/decision_replay_manifest_v3.schema.json`，打包示例为
`examples/decision_replay_manifest_v3.example.json`。

## 可移植 replay export

`tbm.replay-export.v3` 是针对单个已保留 decision replay 的只读可移植 envelope。
`ReplayBundleExport` 包含精确的 `DecisionReplayManifest`、可选的
`InjectionArtifact`，以及按固定 component 顺序排列的全部现存 descriptor 与 base64
编码字节。`export_sha256` 对除自身之外的完整 envelope 计算 canonical self-hash。
`loads_replay_bundle_export()` 会拒绝 duplicate key、未知字段、非法或非规范 base64、
摘要或 linkage 变化，以及超过 16 MiB JSON 或 8 MiB 解码内容的导出。
`verify_replay_bundle_export()` 会重新核验该不可变值。

`export_replay_bundle()` 接受两个 opt-in replay repository 都满足的小型
`ReplayExportReader` 协议。调用方必须先授权 manifest 及其每个 artifact，并显式传入
非空 classification allowlist；该 allowlist 只是第二道 fail-closed 检查，不等于授权。
helper 会在加载 bytes 前预检每个 descriptor 的 classification 与声明/物理大小，
然后执行 immutable ID-addressed read、核验每个 digest、执行调用方指定的内容上限，
并且绝不写入 repository。

规范外部契约为 `schemas/replay_bundle_export_v3.schema.json`，它引用相邻的 manifest
与 injection schema；打包示例为 `examples/replay_bundle_export_v3.example.json`。
hash 验证与 manifest/component 顺序仍属于 JSON Schema 之后的 value-level 检查。

`AuthenticatedDurableAgentMemory.export_replay_bundle()` 提供 opt-in 授权边界。
其带版本请求只包含 durable session ID，不接受 manifest 或
artifact ID。facade 会恢复原始 retrieval scope，新增并回读新的 `artifact:read`
decision，从 session 已保留 linkage 解析唯一 manifest，并在返回 bundle 前再次核验
授权与 session version。

该契约仍刻意不通过 `tbm.agent.v1` 与默认兼容 HTTP/MCP profile 暴露。显式 durable
HTTP/MCP profile 会构造已认证 facade，并仅在启动 content policy 允许时暴露 replay；
Python/TypeScript durable client 使用同一边界。本地 bearer 或可信 STDIO 启动 context
不是 shared-service transport identity，不得让 content-derived ID 变成数据探测
oracle。

## Opt-in SQLite 重放账本

`SQLiteReplayV3Repository` 使用隔离的 `schemas/sqlite-v3-replay.sql` schema，
不会改变 active SQLite schema version 1。它把精确 artifact 字节、injection
descriptor 与 replay manifest 保存为 immutable row。`store_bundle()` 在一个事务
中插入 artifact、injection 与 manifest，要求 session/decision/usage 和 injection
linkage 精确一致，并把精确重放视为幂等；冲突内容会回滚整个操作。

`load_artifact_descriptor()` 不读取 content BLOB，先检查 descriptor column 与物理
content length。完整 load 会重复该 preflight，再读取内容、重解析 descriptor、比较
重复 relational column，并重新计算已存字节 hash。canonical schema metadata、table、
index、immutable trigger、foreign key 和调用方 savepoint ownership 都会 fail closed
校验。`load_manifest_for_session()` 会按 session/decision/usage/injection linkage
解析唯一 manifest，不接受内容派生 lookup ID。manifest load 只核验有界
manifest/injection descriptor，不读取 injection bytes，因此 export classification
与 size preflight 仍发生在内容读取前。账本本身不授权读取、不加密内容、不执行
retention、不证明 evidence truth，也不连接 durable GateSession。该 repository 拒绝
confidential/restricted artifact，直到透明加密 provider 能够在加密的同时保留精确
内容身份。

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

外部 injection/manifest JSON 上限为 1 MiB、depth 32、10,000 nodes。可移植 export
使用上文独立的 16 MiB/depth-40/20,000-node envelope 与 8 MiB 解码内容上限。
parser 拒绝 duplicate key、非法 UTF-8、非有限数字、缺失或未知字段、非法时间戳与
非规范 component set。hash 只能证明字节身份，不能证明作者身份、授权或证据真实性。

JSON Schema consumer 必须执行 draft 的 `date-time` format 检查。canonical
self-hash 与内容派生 ID 的关系属于 value-level 规则，必须在 Schema 验证后继续用
Python parser 检查。

## Durable finalization 组合

opt-in durable finalization service 会调用 `store_complete_bundle()`，并在发布
`FINALIZED` 前读回每个已保留组件。对 finalized session 的精确 replay 会重新加载
UsageDecision、全部七个 supporting component artifact、injection 与重建后的 manifest，
而不会重新渲染。该服务会授权并复查一个 retrieval scope，但仍是内部组合，不是 active
runtime adapter。

`store_complete_bundle()` 要求第一个 supporting artifact 必须由 manifest 的精确
UsageDecision ID 派生，并原子保存去重后的 supporting artifact、injection 字节/descriptor
与 manifest。SQLite 和 PostgreSQL peer 都保留幂等、整体回滚与 caller-savepoint 语义。

当前 v2 Store、active SQLite v1 adapter、PostgreSQL v2 adapter、本地 Agent 与
STDIO MCP 均不会持久化或输出这些记录。opt-in SQLite 账本与 opt-in PostgreSQL
repository 提供 retained storage boundary，并满足 replay export reader surface。
已认证 durable Agent 会在这套精确 authority graph 上提供 replay-read authorization；
显式 durable HTTP/MCP 与 Python/TypeScript client 只会在启动 content policy 允许时
暴露。生产 shared-service runtime 在暴露 replay export 前，还必须认证 transport
identity 并执行 retention/encryption。
