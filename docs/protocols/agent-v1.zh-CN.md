# 本地 Agent 协议：`tbm.agent.v1`

[English](agent-v1.md) | **简体中文**

`trace_backed_memory.agent` 是现有 evidence、Gate、Store 与持久化内核之上的
聚焦应用边界。它不复制策略，也不暴露 Store request token。

## 能力发现

```text
tbm capabilities
```

确定性结果包含协议/存储版本、支持的模式与操作、硬限制、持久记录和进程内记录。
该命令不需要快照，也不会访问网络。

`LocalAgentMemory.health()` 只报告非敏感 pending/replay 数量、memory metrics
与 measured-run metrics。可选 STDIO MCP profile 把这两个操作映射为
`tbm_capabilities` 与 `tbm_health`。

## 生命周期

```text
捕获 pending Trace
  -> prepare（注册 Trace、检索、System Gate、有界 prompt）
  -> finalize（严格 decision、缩小候选、重检、渲染、审计）
  -> 只使用 snippet 执行
  -> 使用显式 measurement 完成
```

不再 finalize 的请求必须 `cancel`。`run` 使用同一组阶段组合 decision/execution
callback；callback 失败时，`AgentMemoryError` 会保留可恢复的 request/decision ID。

MCP 映射为 `tbm_prepare_memory` -> `tbm_finalize_memory` ->
`tbm_complete_run`；放弃 prepared request 时调用 `tbm_cancel_run`。它返回同一
协议 payload，并保留进程内 request 边界。

## 持久化语义

`open_sqlite()` 与 `open_postgres()` 会加载 Store，并同步每个持久阶段。Trace
先于 prepare 持久化，usage decision 先于 finalize 返回持久化，measured Trace
与 usage completion 由 Store 原子完成。

pending request 和同进程 finalization replay 条目不持久化。同一个
`LocalAgentMemory` 实例必须拥有 prepare 到 finalize/cancel 的完整过程；进程
重启会使 prepared handle 失效。request ID 是 opaque value，并包含每个 Store
session 新生成的 128-bit namespace，因此遗留 handle 不会在重启后与新 request
碰撞。数字后缀从已审计 request 的最大值继续递增，但不会恢复 pending handle。
replay cache 最多保留 10,000 个 finalized request；capability discovery 会报告
这些进程内记录及其限制。

## 幂等与错误

同一 runtime 内且 bounded replay 条目仍被保留时，以相同规范 decision 重试
finalize 会返回原结果；不同 decision 返回
`TBM_AGENT_DECISION_CONFLICT`。文档化的 capture、生命周期、callback 与持久化
边界失败使用带稳定 `TBM_*` code、category、operation、retryable 与经校验的
可选 ID 的 `AgentMemoryError`；message 非空且最多 2,048 字符。解释器级意外
失败和直接构造低层 domain record 不属于协议错误 envelope。

## 协议资源

发行包包含 capability、prepared、finalized、completed 与 error 的字节一致
Schema/示例。它们属于独立版本的应用协议，不改变 snapshot、SQLite 或
PostgreSQL schema version。
