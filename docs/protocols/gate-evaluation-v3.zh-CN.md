# 门禁评估 v3

[English](gate-evaluation-v3.md) | **简体中文**

本协议发布两类不可变、存储中立记录：

- `tbm.system-gate-evaluation.v3` 为每个有序 retrieval hit 记录确定性决定，
  包含候选哈希、allow/block 结果、reason/rule、精确授权事件、policy bundle
  与 evaluator 版本。
- `tbm.semantic-gate-attempt.v3` 记录一次有序模型尝试，包含 provider/model/
  endpoint provenance、prompt/response artifact hash、prompt template 与
  generation-config 身份、provider request、status、latency/token、结果及错误码。

两种记录都有 canonical 内容派生身份与有界 strict JSON。它们只保存哈希，不保存
raw prompt/response；哈希是内容身份，不是签名或认证。

## 单调门禁规则

`verify_system_gate_evaluation()` 要求 evaluation 在同一 session、授权事件与
snapshot 下，逐项覆盖精确有序的 retrieval revision 与候选哈希，且发生在检索之后。

`verify_semantic_gate_attempt()` 要求相同 session/snapshot/System Gate 身份与
正确时间顺序。成功语义结果必须把每个 System Gate 候选划分到最终 allowed/blocked
集合。最终 allowed 必须是 System Gate allowed 的子集，全部确定性 block 必须继续
blocked。任何被模型省略的 System-allowed 候选都必须显式放入 final blocked set；
不完整 partition 会被拒绝。失败 attempt 只记录 provenance 与 error，不能产生 decision。

因此模型只能缩小确定性策略，永远不能重新打开它。

## 信任与持久化边界

契约本身不调用模型、不认证 provider、不校验 artifact 字节，也不以事务挂接
GateSession。opt-in
[SQLite Semantic Gate attempt ledger](sqlite-semantic-gate-v3.zh-CN.md)
现已在 SQLite Gate evidence authority 旁持久化精确有序的 retry chain。完整服务
仍必须：

- 授权并验证 RetrievalSnapshot/System Gate 引用；
- 按 classification、encryption、retention 与 access-control policy 保存
  prompt/response artifact；
- 验证 provider 身份与可信 server 时间；
- 为每个 System Gate authority 强制一条线性 sequence。SQLite ledger 通过唯一
  `(system_gate_evaluation_id, sequence)` 与 CAS head 实现；low-level parent
  verifier 核验单个 link，`verify_semantic_gate_attempt_chain()` 核验完整有界
  chain；
- shared database deployment 使用对等的
  [PostgreSQL ledger](postgres-semantic-gate-v3.zh-CN.md)；
- 原子追加 GateSession 引用与 replay component。

active snapshot-v2 Store、SQLite-v1/PostgreSQL-v2 adapter、Agent 与 MCP 尚不产生
这些记录；side-by-side SQLite ledger 不改变该 active compatibility boundary。

runtime parser 会在 UTF-8 encode 前拒绝超大字符串，并执行结构 JSON Schema 无法
表达的跨字段不变量：唯一 System
decision、有序且不相交的 final set、时间顺序、内容派生 ID 与精确跨记录 linkage。
