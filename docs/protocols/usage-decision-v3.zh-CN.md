# UsageDecision v3

[English](usage-decision-v3.md) | **简体中文**

`tbm.usage-decision.v3` 是一次最终记忆使用决策的不可变、内容寻址审计记录。它记录
retrieval 提出了什么、System Gate 允许或阻止了什么、Semantic Gate 保留了什么、
有界 renderer 实际使用了什么，以及最终生成的精确 injection artifact。

## 内容身份

`usage_decision_id` 是不含 ID 字段的规范记录的 SHA-256 身份。同一份 unsigned
规范 JSON 字节会作为 internal `ContentAddressedArtifact` 保存，因此
`usage_decision_artifact_id(usage_decision_id)` 可以确定性定位精确的决策字节。
解析时会重新计算并验证这两层关系。

规范外部契约和示例分别是 `schemas/usage_decision_v3.schema.json` 与
`examples/usage_decision_v3.example.json`；它们也会作为逐字节一致的包资源安装。

## 收窄与审计

有序集合必须单调收窄：

1. retrieval candidates；
2. System Gate 允许的 revision；
3. Semantic Gate 允许的 revision；以及
4. renderer 实际渲染的 revision。

最终集合绝不能重新加入早期阶段已经移除的 revision。
`blocked_memory_revision_ids` 是最终集合在候选集中的精确有序补集。
`system_blocked` 另行记录 System Gate 在该阶段排除每个 revision 的精确原因与规则，
因此后续模型决策无法隐藏或重新打开确定性阻止。

记录还通过 authorization event、RetrievalSnapshot、System Gate evaluation、
成功的 Semantic Gate attempt、policy digest、renderer 身份/版本、Trace、run、
decision、session、risk、reason 与建议渲染模式绑定已授权的 retrieval evidence。

## 回放关联

每份 UsageDecision 都包含 `tbm.replay.v3` 定义的固定八组件 replay map，其 injection
组件必须派生出声明的 `injection_artifact_id`。durable finalization 组合会先把
UsageDecision，以及精确的 retrieval、System Gate、Semantic Gate prompt/response、
ancestry commitment、policy、renderer 与 injection 字节保存到 replay authority，
然后才发布 `FINALIZED`。

ancestry 组件是对 retrieval preparation context commitment 的精确保留引用。
它不会把 context hash 变成完整 Git graph 归档；需要独立重建的部署必须按 retention
policy 保留被引用的 Git/index evidence。

## 解析与信任边界

外部 JSON 上限为 1 MiB、深度 24、节点数 4,096。解析器会拒绝重复键、非法 UTF-8、
非有限数字、未知或缺失字段、非法时间戳、畸形组件集合、非规范顺序与内容寻址身份不一致。

哈希证明的是字节身份，不是授权或事实真实性。只有服务重新核验当前授权、active revision
head、policy、Semantic Gate chain、已保留 artifact 与 durable GateSession 关联后，
UsageDecision 才可信。

## 集成边界

该契约与 opt-in finalization service 尚未接入 active snapshot-v2 Store、local Agent、
STDIO MCP、HTTP 或 SDK adapter。当前明文 replay authority 只接受 `public` 或
`internal` 组件字节。对 confidential 或 restricted 内容做 finalization，需要未来提供
能通过 authenticated encryption 保持精确身份的 replay authority。
