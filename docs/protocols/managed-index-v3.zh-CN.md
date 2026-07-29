# 托管索引 bundle v3

状态：已作为可选、隔离的 v3 检索组件交付；尚未接入当前 Agent 或 MCP
运行时。

[English](managed-index-v3.md)

## 目的

托管索引 bundle 用一个有界、内容寻址的发现证据源取代调用方自带的检索
分数。一个 bundle 针对同一份已激活 revision 目录提供五个独立版本视图：

- metadata scope 与 classification；
- 确定性的 Unicode lexical token；
- 显式本地 semantic vector；
- query token 到结构化 evidence 的边；
- 不可变 Git commit DAG 与 lesson anchor。

`ManagedIndexDiscovery` 实现现有 `CandidateDiscovery` 端口。
`AuthenticatedRetrievalPreparationService` 仍然先鉴权，再加载并验证当前已激活
revision，应用 Retrieval Policy 与 System Gate，复核 head，且只发布最终允许
集合。索引匹配只是发现证据，不是授权。

`ManagedIndexDiscovery` 是受信的进程内端口适配器，不是授权 authority。只能
通过 `AuthenticatedRetrievalPreparationService` 调用；后者必须在 discovery
之前创建并验证持久化 authorization decision。适配器会检查 scope 与已认证
principal/client/context 的绑定，但不会自行读取 authorization ledger。直接
调用 `discover()` 或 managed-index repository 绝不授予访问权限。

## 构建契约

`build_managed_index_bundle()` 只接受精确的 `ManagedIndexBuildInput`。每个
source 都必须是精确的 `ActivatedRevisionCandidate`，每个 lesson 必须已经
带有结构化 fix 与 regression evidence。构建器会：

1. 验证 repository scope；
2. 派生规范 source-catalog digest；
3. 对调用方显式提供且已脱敏的 index text 做 tokenization；
4. 在不访问网络的情况下归一化显式、有限的 semantic vector；
5. 验证 evidence reference 与完整 Git DAG；
6. 为五种 index kind 分别创建一个内容寻址的 `IndexVersion`；
7. 从完整规范 descriptor 派生 bundle ID。

source 顺序不会改变结果。重复或未排序的持久化记录、未知 evidence、未知
Git commit、环、混合 semantic dimension、非有限数值与哈希不一致都失败
关闭。

confidential 与 restricted candidate 不得包含 lexical token、semantic
vector，或内容派生的 evidence-graph query token。除非 candidate
classification 允许，否则 operator 必须把内容派生数据排除在 managed bundle
之外。

## 查询契约

发现过程只加载与已授权 tenant、repository、environment 精确匹配的当前
bundle。retriever identity 与 version 也必须与请求一致。

semantic 查询及启用 semantic 的 hybrid 查询使用 `SemanticQueryVector`。
provider identity、provider version、vector dimension、精确 vector value
与原始 query byte 的 SHA-256 共同组成 `query_evidence_sha256`。准备服务把
该 digest 绑定进 prepared context hash。缺少匹配 query-vector evidence 的
semantic index 结果会在加载任何已激活 revision 之前被拒绝。

当 retrieval policy 要求 ancestry 时，Git ancestry 只从不可变 bundle DAG
本地计算；当前 commit 不存在时失败关闭。policy 显式禁用 ancestry 时，不
输出 ancestry relation。

## 持久化

两个隔离 repository 实现相同的 publication 契约：

- `SQLiteManagedIndexV3Repository`，使用
  `schemas/sqlite-v3-managed-index.sql`；
- `PostgresManagedIndexV3Repository`，使用
  `schemas/postgres-v3-managed-index.sql`。

bundle 是不可变的精确 UTF-8 byte。每个 tenant/repository/environment 只有
一个 head，并通过 compare-and-swap 前进。精确 publication replay 幂等。
陈旧 expected head、内容冲突、catalog drift、禁用 trigger、function body
变更与读回不一致都失败关闭。

PostgreSQL 安装继续与当前 schema version 2 隔离并存。rollback 会先验证
精确 relation、column、constraint、function、function body、trigger、ACL
及 active-schema 前置条件，再删除显式枚举的对象。

## 边界与非目标

- 每个 bundle 最多 1,000 个完整 candidate；
- 每个 candidate 最多 4,096 个 lexical token 或 semantic dimension；
- 每个 candidate 最多 64 个 scope attribute 与 4,096 个 evidence ID；
- 最多 50,000 条 evidence edge 与 50,000 条 Git edge；
- 最多 20,000 个 Git commit；
- 规范 bundle JSON 最大 64 MiB。

这些边界使参考实现可重放、可本地审计。生产分片、后台 index worker、原生
FTS/ANN engine、外部 embedding provider、object-store 分发与跨分片排名仍
属于后续工作。当前实现不宣称已经提供企业规模的托管索引服务。

## 规范资源

JSON Schema 验证有界的外部结构。对于 JSON Schema 无法表达的规范 Unicode
tokenization、UTF-8 byte 边界、排序、内容哈希、图引用与跨记录 classification
规则，以严格 runtime loader 为准。

- `schemas/managed_index_bundle_v3.schema.json`
- `examples/managed_index_bundle_v3.example.json`
- `schemas/sqlite-v3-managed-index.sql`
- `schemas/postgres-v3-managed-index.sql`
- `schemas/postgres-v3-managed-index-rollback.sql`

安装副本保持 byte-identical，并由 wheel、sdist、editable、SQLite 与
PostgreSQL 测试验证。
