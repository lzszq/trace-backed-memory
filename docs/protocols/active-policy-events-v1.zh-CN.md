# Active policy 事件 v1

`tbm.active-policy-event.v1` 是单个 organization/tenant/repository/
environment ledger partition 内 active retrieval policy 的 opt-in event/reducer
边界。它不会改变兼容 Store，也不会选择默认 Agent、MCP、HTTP 或 durable runtime
profile。

## 内容寻址 policy bundle

`ActivePolicyBundle` 绑定 F4-05 的全部八个维度：

- minimum trust tier、必须 active revision，并永久排除
  `legacy_unstructured` evidence；
- `RetrievalPolicyBundle` 中精确的五种 task-mode memory rule；
- required/disabled ancestry 与显式 bypass reason；
- allowed classification set；
- mandatory eval-leakage blocking；
- 在 kernel hard cap 内单调收窄的 discovery、System Gate、Semantic Gate、
  injection 与 payload candidate budget；
- 内容寻址 renderer descriptor，包括 mode、逐项限制、memory 数量、字符与
  UTF-8 byte 限制、输出格式和 media type；
- `semantic_gate_required=true`。

bundle 嵌入精确的既有内容寻址 `RetrievalPolicyBundle`；reducer 不会重新实现
task-mode、ancestry、classification、fusion、score、payload 与 eval-leakage
校验。规范有界 JSON、duplicate-key rejection、finite number、精确字段和
content-derived ID 均 fail closed。

trust tier 是 event-first catalog 的 policy 声明。candidate trust-tier 的生成与
执行仍受未完成的 F4-03/F4-04 验收及后续默认 cutover 阻断；confidence value 与
attestation-verifier ID 不会被静默当作 trust tier。

## Registration、activation 与 active head

每个 ledger partition 只有一个 `active_policy_<partition-sha256>` stream：

- `tbm.policy.bundle_registered` 保留精确 bundle、已验证的
  `policy:create_global` policy/request/decision 与 verifier identity；
- `tbm.policy.bundle_activated` 保留独立执行的 `policy:approve_global`
  decision、精确 registration、predecessor bundle、actor、client、target partition
  与 activation time。

event envelope 必须绑定完整 partition、principal/actor、client、authorization
event、occurrence time、record JSON 与 record digest。Reducer 要求 trusted
attestation verifier、registrar/activator 相互独立、已知不可变 registration、精确
当前 predecessor，以及严格向前的 activation time。head 推进时，旧 bundle 与
activation 保持不可变。

`ActivePolicyHead` 以内容寻址方式保留 bundle/retrieval/renderer、registration/
activation、两个 authorization event、两个 attestation verifier、actor、time、
predecessor 与精确 source-event hash。

## Durable 使用

`append_active_policy_records()` 会先运行 reducer，通过共用 `EventLedgerPort`
原子 append，验证 receipt，再次读取并核验 stream，并要求 durable projection
等于预计算 projection。`rebuild_active_policy_from_ledger()` 会读取单一 partition
stream 两次，要求 verified head 不变，并返回绑定 reducer descriptor 与
trusted-verifier configuration 的内容寻址 snapshot。SQLite 与 PostgreSQL 共用
同一代码并有 conformance 覆盖。

`ActivePolicyProjection` 与 `DurableActivePolicySnapshot` 都是可调用的精确
`RetrievalPolicyBundle` provider，因此显式 durable composition 可使用 event-derived
head，而无需改写现有 preparation/finalization kernel。完整 `ActivePolicyBundle`
保留给后续 renderer、trust-tier 与 Semantic Gate enforcement wiring。

## 仍未关闭的边界

- 默认 adapter 尚未选择该 source；默认 cutover 属于 F5。
- 当前 finalization 仍使用固定 renderer descriptor 与 mandatory Semantic Gate
  path。本 bundle 记录 governed source，但不声称已完成默认 enforcement。
- F4-03/F4-04 验收阻断、F4-06 index 与 F4-07 outcome/harm projection 仍未关闭；
  `full_persistence=false` 仍是正确声明。

规范资源：

- `schemas/active_policy_bundle_v1.schema.json`
- `schemas/active_policy_event_payload_registry_v1.schema.json`
- `examples/active_policy_bundle_v1.example.json`
- `examples/active_policy_event_type_registry_v1.example.json`

另见[英文参考](active-policy-events-v1.md)。
