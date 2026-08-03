# Durable Semantic Gate v3

[English](durable-semantic-gate-v3.md) | **简体中文**

`AuthenticatedSemanticGateSessionService` 是可选启用的组合边界，用于让一个已经
prepared 的 durable GateSession 经过已认证的 Semantic Gate 工作继续推进。它复用
现有 GateSession、Gate evidence、Semantic Gate attempt 与精确 artifact-byte
authority；不会改变 active snapshot-v2 Store 或默认 Agent/MCP lifecycle。

## 请求与 authority

`DurableSemanticGateRequest` 绑定 durable `session_id`、预期 session revision、
精确且有界的 UTF-8 prompt 字节，以及精确的预期 Semantic Gate attempt parent。
其中有界 `lease_seconds` 是请求的服务端 decision claim。调用方不能选择
RetrievalSnapshot 或 System Gate evaluation：服务会从 durable
session 派生两者，并在 provider 工作前拒绝任何
session/evaluation/snapshot/Trace/run 不匹配。

provider context 仍由 transport 派生，且必须与服务端持有的 provider、
authenticator、credential registration 完全一致。该 provider authentication
不是 tenant authorization。session 必须已由 authenticated durable retrieval
preparation 边界生成；组合服务仍是可信内部 kernel，而不是 transport authenticator。
显式 durable HTTP/MCP 与 SDK profile 会通过 authenticated durable Agent facade
暴露它；默认 compatibility adapter 不会。它的 provider callback 必须由服务端持有；provider authentication 不会把调用方
提供的 callback 变成可信模型 provenance，签名 provider attestation 仍是后续工作。
显式 durable runtime 配置可信 provider invoker 时，durable Agent 会用该 invoker
替换 wire callback，并用 `SemanticProviderEffectService` 包装它。调用方 response 字段
仍只是 compatibility input，不是可信 provider provenance。

## 决策顺序

对于新的 prepared session，服务会：

1. 在读取任何 session 或 evidence 前认证 provider context；
2. 读取当前 GateSession，并检查精确的预期 revision；
3. 重新加载完整且不可变的
   RetrievalSnapshot/SystemGateEvaluation/attempt chain；
4. 核验 session、Trace、run、snapshot、evaluation 与 chain linkage；
5. 通过 CAS 发布并读回 `AWAITING_DECISION`；
6. 通过 CAS 续期并读回 live decision lease，让 stale 或竞争 revision 在 provider
   工作前失败；
7. 调用 `AuthenticatedSemanticGateService`；配置 trusted effect path 时，会先追加
   Semantic effect request/attempt，只调用一次 server-owned provider，再记录一份
   digest 绑定完整 structured provider result、而非只绑定 response bytes 的 receipt；
8. 重新加载并核验完整 attempt chain，包括 System Gate 单调收窄规则与精确
   artifact 读回；以及
9. 通过 CAS 发布并读回 `DECIDED`，其中记录完整有序 attempt-ID chain，以及
   successful attempt 的 `decision_id`。

provider 调用失败会保留为不可变的 prompt-only attempt，并让 session 停留在
`AWAITING_DECISION`。retry 必须指定该 failed attempt 为精确 parent，并使用当前
session revision。retry 可以使用不同 prompt 字节，每个 attempt 都保留自己的精确
prompt provenance；对已有 success 做精确 replay 或 recovery 时，必须使用该 success
原始 prompt 字节。相同 prompt 或 response 字节的 retry 会复用已有
content-addressed artifact descriptor，同时创建新的 attempt-specific binding；
不会改写不可变 content。

## Replay 与恢复

如果 successful attempt 已持久化，而后续 session transition 没有提交，精确请求
可以在不再次调用 provider 的情况下完成
`AWAITING_DECISION -> DECIDED`。对已经 decided 的 session 进行精确 retry，也会
返回已保存的 session 与 attempt/artifact receipt，不会再次调用 provider。两条
路径都要求相同 prompt 字节、successful attempt 的原始 parent、完整 chain 与相同
decision linkage。
预期 revision 会在任何可变工作前强制核验。decided replay 会刻意接受原始请求中较旧
的 revision，因为 terminal session 必然已经推进；此时精确 prompt、parent、chain、
decision 与读回 receipt 共同构成幂等证明。

effect request/attempt 已存在但 Semantic attempt 尚未保存时，provider invoker 绝不会
再次调用。当前 adapter 没有 invocation owner 已放弃的 durable 证明，因此
in-flight/submitted work 会保持 recovery-required 且不产生改写。只有 unknown result
已经持久保留或 successful receipt 已存在时，才会使用配置的可信 reconciler，并允许其
返回 `confirmed`、`still_unknown` 或 `not_found`。confirmed recovery 只有在完整 result
digest 与 provider receipt 都精确一致后，才能保存 Semantic attempt。同 scope 的
reconciliation 可以使用新的 transition authorization，但原始 request authorization
保持固定。`still_unknown` 与 request-only stream 继续 recovery-required；`not_found`
也不能直接开始新 attempt，必须先有独立 durable retry schedule/claim，而当前 adapter
尚未提供该能力。

successful attempt 后又出现另一个 attempt、prompt 或 parent 不匹配、读回被篡改、
session 已 expired/canceled，或 transition 无法确认时，都必须进入
recovery-required。服务绝不会删除或改写 orphan attempt，也不会在 successful
result 可能已保存后静默重复外部 provider 调用。

cancellation 或 expiry 可能与执行中的 provider callback 竞争。一旦 provider 工作已
开始，组合层无法承诺外部 provider 没有发生副作用；它能保证的是，已保留的 successful
attempt 永远不会挂接到 canceled、expired 或其他 terminal session。调用方会收到
recovery-required，不可变 evidence 则保留给 operator 审查。相对地，已经过期的
`AWAITING_DECISION` lease 会在 callback 前 claim 失败，不会保留新 attempt。

## 事务边界

默认组合是跨 authority 的有序恢复，不是 distributed transaction。provider
失败会留下 `AWAITING_DECISION` 与一条 failed attempt。successful attempt 后的
GateSession CAS 如果无法确认，可能留下不可变 attempt，之后可由精确恢复调用挂接。

如果 SQLite 或 PostgreSQL 的 GateSession、Gate evidence 与 Semantic Gate
artifact repository 明确共享同一个 caller-owned connection，调用方外层
transaction 可以一起回滚 session transition、attempt 与 artifact binding。服务
本身不会开启这个外层 transaction。跨 provider 调用持有该 transaction 也会持续
持有数据库锁，因此这是显式部署权衡。

独立的 opt-in durable finalization 组合现在已经提供 rendering、content-addressed
final injection、完整 replay-bundle 保留与 `DECIDED -> FINALIZED` CAS。
opt-in [已认证 durable Agent](durable-agent-v3.zh-CN.md) 会继续组合 execution 与
outcome completion。显式 durable HTTP/MCP 与 Python/TypeScript SDK profile 已暴露
该 facade；默认 compatibility adapter 与具备 peer authentication 的 shared-service
transport 仍是独立边界。
