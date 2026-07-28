# 运行结果与归因 v3

[English](outcome-v3.md) | **简体中文**

`RunOutcome` 是单个已完成 `GateSession` 的不可变测量结果。它把 session、
Trace、run 和 usage decision 绑定到 evaluator 版本、执行输出摘要、至少一份
证据 artifact、有界执行测量以及规范测量时间；`run_outcome_id` 由完整 payload
内容寻址生成。

`OutcomeAttribution` 是独立的内容寻址记录。`association` 只表示列出的
memory revision 出现在本次运行中；它必须使用 `runtime_observation`，effect
可以是 `unknown`，且不能声明 verifier。`causal` 结论必须来自受控实验、
人工复核或外部评估，给出确定 effect，并由不同于 evaluator 的 verifier
复核。

运行时 verifier 会把 outcome 精确绑定到已完成 GateSession，要求测量时间不早于
session 完成；attribution 必须绑定同一 outcome 与 completed session，且每个归因
revision 都必须属于 session 的 finalized revision，并拒绝时间倒置。任何 adapter、
指标或迁移都不得把观察
关联自动提升为因果。现有 version-2 `Trace.eval_result` 与
`MemoryUsageLog` outcome 字段继续受支持，但没有显式 mapping 与证据时不能
冒充完整 v3 记录。

JSON Schema 只用于结构预检，不是完整验证：它无法重算内容 ID、强制数组规范排序、
认证 identity 或执行跨记录检查。consumer 必须使用 runtime parser 与 verifier；
builder 会在哈希前把数值输入规范化为 JSON float。内容哈希只能检测规范 payload
变化，不是签名。service 必须认证 evaluator
与 verifier，校验引用 artifact 的实际字节，使用可信时间源，强制不可变
唯一性，并原子写入 outcome、session 转换和 attribution。raw tool output
与秘密应保存在受控 artifact 中，而不是这些记录内。

opt-in SQLite 与隔离 PostgreSQL completion authority 现在都会使用同一个可信
timestamp，在一个 transaction 中原子写入 RunOutcome 与对应的 `EXECUTING` →
`COMPLETED` GateSession revision，并提供精确重放、schema/catalog guard 与
commit 前读回。详见
[SQLite RunOutcome 完成事务 v3](sqlite-outcome-v3.zh-CN.md)与
[PostgreSQL RunOutcome 完成事务 v3](postgres-outcome-v3.zh-CN.md)。
attribution persistence、evaluator authentication、artifact verification、
outbox delivery 与 active runtime integration 仍未完成。

规范 Schema：

- `schemas/run_outcome_v3.schema.json`
- `schemas/outcome_attribution_v3.schema.json`
