# 检索快照 v3

[English](retrieval-snapshot-v3.md) | **简体中文**

`tbm.retrieval-snapshot.v3` 是一次已授权检索结果的不可变、存储中立记录。
它让排序可解释、可回放，但不会把相似度误当成权限、适用性、验证证据或门禁决定。

快照绑定：

- GateSession、请求、Trace 与 run 身份；
- 授权事件以及精确的上下文/查询摘要；
- 检索器实现/版本与不可变索引身份；
- 有序的 memory revision 命中及候选内容摘要；
- metadata、lexical、semantic、evidence-graph 各阶段分数；
- 确定性融合分数及每个命中的参与阶段；
- 候选总数、top-K 上限与显式截断原因；
- 规范时间戳与内容派生的快照身份。

`RetrievalHit` 的 rank 必须连续且唯一。memory revision 与候选摘要不得重复。
索引身份唯一并按规范顺序排列。所有分数必须是有限 JSON 数值。builder 只规范
为唯一 float 表示；selected stage 必须精确匹配已记录的 stage score，每个选中
stage 必须有对应 index version；即使零命中，retrieval mode 也必须有匹配的
index provenance；hybrid hit 至少使用两个排序 stage，候选总数
上限为 1,000,000。builder 只规范表示顺序和时间戳；任何语义变化都会被不可变
快照哈希检测。这些跨字段不变量由 runtime parser 在结构 JSON Schema 之外执行。

## 信任边界

授权必须先于检索。`authorization_event_id` 只是授权决定的引用，不证明调用方
真实身份，也不能替代服务端授权执行。上下文、查询、候选和索引哈希都是内容
身份，不是签名。

快照只记录排序证据。System Gate evaluation 与 Semantic Gate attempt 是独立
记录。语义相似度或高融合分数都不能重新打开 System Gate block、激活 memory
或授权 artifact 读取。

精确回放消费已记录的有序命中、分数、版本与哈希，不得从可变 catalog/index
静默重算。未来服务仓库在把快照挂接到 prepared session 前，必须验证引用的
授权事件、GateSession、memory revision、候选字节、索引 artifact、访问控制、
保留策略和事务边界。

当前 snapshot-v2 Store、SQLite-v1/PostgreSQL-v2 adapter、local agent 与 MCP
runtime 尚不产生此契约；接入需要显式版本迁移与服务编排。
