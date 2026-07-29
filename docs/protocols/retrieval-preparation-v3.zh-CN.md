# 已认证检索准备 v3

[English](retrieval-preparation-v3.md) | **简体中文**

`AuthenticatedRetrievalPreparationService` 是一个存储中立的参考内核：它把一次
已认证、仓库范围内的检索请求转换成配对的 `RetrievalSnapshot` 与
`SystemGateEvaluation`。它组合授权服务和 `ActivatedRevisionSource`，不会使用或
重新解释 version-2 Store。

## 策略与输入身份

`tbm.retrieval-policy.v3` 是内容寻址的策略 bundle，绑定：

- 允许的数据分类；
- planning、repair、debug、eval 和 production 每种任务模式允许的 memory type；
- 必须执行的 Git ancestry，或禁用 ancestry 时的明确原因；
- 正数的 metadata、lexical、semantic 和 evidence-graph 融合权重；
- 最低融合分数与渲染 payload 字节预算；
- 始终阻断 evaluation-leaking memory 的 fail-closed 规则。

规范 Schema 与示例分别是 `schemas/retrieval_policy_v3.schema.json` 和
`examples/retrieval_policy_v3.example.json`。runtime parser 还会强制规范顺序、
五种任务模式完整覆盖、有限数值、严格有界 JSON 和内容派生 policy ID。

`RetrievalPreparationContext` 记录精确 tenant、规范 repository、environment、
task mode、commit、受支持的 applicability 属性，以及 eval 模式必须提供的
evaluation suite/case 身份；非 eval 模式不能声明该身份。snapshot 的 context digest
会同时绑定这些请求事实和可信 discovery 返回的精确 Git-ancestry relation。原始
query 字节有严格边界，
且只交给 discovery adapter；持久 snapshot 仅保存 `query_sha256`。

## 准备顺序

服务按以下 fail-closed 顺序执行：

1. 在调用 discovery 前持久化并回读授权决定；
2. 要求准备上下文与已授权 principal、client、tenant、repository 和 environment 一致；
3. 读取一份不可变策略 bundle；
4. 调用可信 `CandidateDiscovery` adapter，取得完整、有界候选集、精确
   Git-ancestry relation 及其实际使用的精确 index version；同一份不可变 policy
   bundle 会传给 discovery；
5. 通过已授权的 `ActivatedRevisionSource.load_authorized` 路径读取每个候选；
6. 拒绝 candidate hash、授权回执、仓库范围、结构化 evidence 或 index provenance
   替换；
7. 过滤 classification、精确 applicability 属性、evaluation leakage、当前
   evaluation suite/case 重叠和必须满足的 Git ancestry；
8. 确定性融合所选 stage 分数，执行最低分过滤，以“分数降序、revision ID 升序”
   排序，并执行 top-K 与 payload 字节预算；
9. 生成一份内容寻址 `RetrievalSnapshot`；
10. 生成覆盖每个有序 hit 的确定性 `SystemGateEvaluation`，阻断当前 task mode
    不允许的 memory type；
11. 重新检查每个入选 publication head，并重新读取策略；
12. 如果 head 或策略在返回前变化，则拒绝结果。

metadata 是值为 `1.0` 的二元 eligible-candidate 分数。融合分数等于加权分数和除以
实际参与 stage 的权重和。任何参与过滤或排序的分数都必须有已记录的不可变 index
version。所有省略原因使用现有 snapshot truncation reason 表示。后续模型逻辑不能
推翻 System Gate 决定。

## Adapter 契约与边界

`CandidateDiscovery` 是可信 adapter 边界，不是授权 authority。它必须返回本参考
准备实际考虑的完整集合，最多 1,000 条，并保证 memory ID 与 candidate digest
唯一。每种 index kind 最多只能报告一个不可变版本，因此每个 stage score 与 ancestry
relation 都有唯一明确的已记录版本。service 会把全部 ancestry relation 写入 snapshot
context digest，并在启用 ancestry 时要求精确 `git_graph` 版本。
semantic index 参与时，结果还必须携带由精确 provider、provider version、vector
和 raw-query digest 派生的 query evidence；service 会在 revision 读取前把它绑定到
prepared context。

adapter 被信任会从这些已记录 index 字节派生分数和 relation；index content hash 是
身份，不是签名或 attestation。`ManagedIndexDiscovery` 现在提供一个有界本地具体
adapter：使用一份内容寻址五视图 bundle，并通过精确 SQLite/PostgreSQL
publication-head CAS 发布。它验证计算所用的完整不可变输入，但不会独立为这些输入
签名。生产分片、外部 FTS/ANN provider 与后台 index worker 不属于该参考 profile。
详见[托管索引 bundle v3](managed-index-v3.zh-CN.md)。

本阶段刻意只支持 repository-scoped activated revision。当前授权服务只解析一个
repository permission，尚不提供 tenant-wide discovery authorization；不得从缺失值
推导 global 或 tenant-wide 选择。

返回的 `PreparedRetrievalEvidence` 不是已完成的 GateSession。其配对记录可由现有
SQLite/PostgreSQL Gate evidence authority 接收，但本服务不会把它们原子挂接到
session。Semantic Gate attempt、`DECIDED -> FINALIZED`、rendering、injection、
artifact retention，以及 active Agent/MCP/HTTP/SDK 接入仍属于后续工作。active
snapshot-v2 Store 和本地 MCP 仍不生成这份 v3 preparation。
