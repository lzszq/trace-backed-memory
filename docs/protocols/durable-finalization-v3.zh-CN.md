# Durable finalization v3

[English](durable-finalization-v3.md) | **简体中文**

`DurableFinalizationService` 是把一个 durable GateSession 从 `DECIDED` 推进到
`FINALIZED` 的 opt-in 内部组合。它会复查实时授权、active revision head 与 policy，
只渲染最终允许集合，保留完整 replay bundle，并通过 GateSession CAS 发布精确的
UsageDecision 与 InjectionArtifact 关联。

## 前置条件与顺序

调用方提供 authenticated service context、精确 authorized retrieval scope，以及只含
session ID、预期 revision 与有界 lease 请求的 `DurableFinalizationRequest`。调用方不能
提交 snippet、candidate set、policy、renderer、decision 或 artifact identity。

对于新的 decided session，服务会：

1. 核验当前 retrieval authorization 与精确 session scope；
2. 检查预期 GateSession revision，并通过 compare-and-swap 续期 decision lease；
3. 重新加载精确 RetrievalSnapshot、System Gate evaluation、完整 Semantic Gate
   attempt chain，以及成功 attempt 的 prompt/response artifact；
4. 要求 snapshot authorization event 与当前 authorized scope 完全相同，即使最终允许
   集合为空也不能跳过；
5. 只加载允许的当前 ActivatedRevision candidate，并核验 revision 与 candidate hash；
6. 在渲染前后以及 bundle 保留后，立即再次复查当前授权、active head 与 policy；
7. 保存并读回精确 replay bundle；以及
8. 通过 compare-and-swap 发布并读回 `FINALIZED`。

完整 Semantic Gate chain 会在渲染前通过核验，因此 System Gate block 保持单调；
UsageDecision 还会分别保留每个确定性 block 的原因与规则。

## 渲染与保留 bundle

v1 renderer 生成规范 JSON data，并明确提示引用的记忆是 evidence，不是可执行指令。
它保持 snapshot 顺序，限制每项长度、项目数与 snippet 总长度。`none` 生成空 snippet
与空最终集合；`summary` 和 `full` 使用不同的单项上限，但共用同一确定性 envelope。

该明文组合只支持 `public` 与 `internal` UTF-8 memory 字节。confidential 或
restricted 内容会 fail closed，直到存在加密 finalization/replay authority。

在发布 `FINALIZED` 前，replay authority 会原子保留 UsageDecision、RetrievalSnapshot、
System Gate evaluation、精确 Semantic Gate prompt/response、ancestry commitment、
policy bundle、renderer descriptor、精确 injection 字节、InjectionArtifact 与完整
DecisionReplayManifest。每个组件都内容寻址并逐字节读回。

## 回放与恢复

对 finalized session 的精确 retry 会从已存 usage ID 派生 UsageDecision artifact，
重新解析其内容寻址身份，加载每个 replay component，核验
snapshot/evaluation/policy/renderer/injection 关联，重建 manifest hash，核验精确
injection 字节，并重新读取未变化的 GateSession。它不会重新渲染，也不会调用模型。

authenticated `replay()` boundary 会对仍保留精确 finalization linkage 的
`FINALIZED`、`EXECUTING`、`COMPLETED` 或 `ABANDONED` revision 执行同一套核验。
它供 durable execution 组合使用；调用方不得用 replay repository 的 raw read 绕过它。

如果 replay bundle 已保留，但 GateSession CAS 无法确认，服务只会在安全时重试同一个
精确 finalization transition；否则抛出 `DurableFinalizationRecoveryRequiredError`，
并在可用时附带已保留的 UsageDecision 与 InjectionArtifact。服务绝不会删除不可变
evidence，也不会静默创建新的最终决策。

## 事务边界

在独立 authority 之间，finalization 是有序恢复，不是 distributed transaction。
已保留的 replay bundle 可能需要显式 GateSession recovery transition。

当 SQLite 或 PostgreSQL GateSession、evidence、Semantic Gate artifact 与 replay
repository 明确共享同一个 caller-owned connection 时，调用方可以用外层 transaction
包住 finalization。repository savepoint 允许调用方一起提交或回滚续期 lease、完整
bundle 与 `FINALIZED` revision。服务本身不会开启或拥有该外层 transaction。

## 集成边界

该服务为 opt-in，active Store、local Agent、STDIO MCP、HTTP 与 SDK adapter 都不会
调用它。它不会推进 `EXECUTING` 或 `COMPLETED`，不会产生 RunOutcome，不提供
Review Console 行为，不实现 retention/encryption，也不会让 active MCP 的进程内 Gate
变成 durable。
独立的 opt-in
[durable execution 组合](durable-execution-v3.zh-CN.md)会使用其 authenticated
exact replay boundary。
