# Durable MCP profile：`tbm.durable-agent-wire.v1`

[English](durable-mcp-v1.md) | **简体中文**

显式的 `tbm-mcp --profile durable-v3` profile 把完整 durable Agent 生命周期映射为
runtime-only MCP tools。它与 durable HTTP profile 共用同一个
`DurableRuntimeFactory`，以及统一 SQLite 或隔离 PostgreSQL v3 authority graph。
默认 `tbm-mcp` 仍选择 version-2 兼容生命周期。

## 信任边界

- MCP client 启动本地 STDIO 子进程；继承的 pipe 上没有独立 peer authentication
  handshake。
- operator 持有的 application factory 在 tool JSON 之外提供
  `DurableRuntimeDependencies`，以及固定的可信 service、Semantic Gate provider
  和 outcome evaluator context。
- tool request schema 拒绝 caller、tenant、repository、environment、provider
  authentication、evaluator authentication 与 authority identity。
- durable service 在每次操作时仍会复查当前 registry 与 authorization 状态。
- 除非启动时显式启用，否则 injection 与 replay content 均隐藏。
- input frame 复用兼容 MCP profile 的有界、拒绝重复键的 JSONL STDIO transport。

这是可信本地进程 profile。启动配置说明该进程代表哪些 identity 执行操作；它不会认证
任意 peer，也不得被描述为共享或不可信多租户服务。

## 可信 application factory

在 operator 控制的环境中创建可导入模块：

```python
from trace_backed_memory.durable_mcp_entry import DurableMCPApplication
from trace_backed_memory.durable_mcp_server import DurableMCPTrustedContexts

from my_service.tbm_dependencies import (
    durable_runtime_dependencies,
    trusted_evaluator_context,
    trusted_provider_context,
    trusted_service_context,
)


def create_application() -> DurableMCPApplication:
    return DurableMCPApplication(
        dependencies=durable_runtime_dependencies(),
        contexts=DurableMCPTrustedContexts(
            service=trusted_service_context(),
            provider=trusted_provider_context(),
            evaluator=trusted_evaluator_context(),
        ),
    )
```

先创建数据库父目录（macOS/Linux 使用 `mkdir -p .tbm`，PowerShell 使用
`New-Item -ItemType Directory -Force .tbm`），再启动新的统一 SQLite v3 数据库：

```bash
tbm-mcp \
  --profile durable-v3 \
  --application-factory my_service.tbm_mcp:create_application \
  --sqlite .tbm/durable.sqlite3 \
  --initialize
```

后续启动时省略 `--initialize`。factory 路径也可由
`TBM_DURABLE_MCP_APPLICATION_FACTORY` 提供。PostgreSQL 使用
`--postgres-env ENV_NAME`。

content 默认隐藏。`--expose-injection-content` 启用精确 runtime snippet；
`--expose-replay-content` 进一步启用已保留 replay bytes，因此要求同时启用
injection 暴露。

## Runtime-only tools

该 profile 仅暴露：

- `tbm_durable_capabilities`；
- `tbm_durable_prepare`；
- `tbm_durable_decide`；
- `tbm_durable_finalize`；
- `tbm_durable_start`；
- `tbm_durable_resume`；
- `tbm_durable_abandon`；
- `tbm_durable_complete`；
- `tbm_durable_cancel`；
- `tbm_durable_get_session`；
- `tbm_durable_export_replay`。

它不暴露 extraction、review、verification、publication、activation 或其他 curation
tool。tool annotation 会标明只读操作与可幂等重试的 durable transition。

capabilities 返回 `transport_profile=durable-v3`、
`transport_security=trusted-local-stdio` 与 `peer_authentication=false`。
client 使用 durable `session_id` 和精确的当前 GateSession version 续接，绝不使用
进程内 handle。

## 重启与重放

SQLite/PostgreSQL session state 属于 authority graph，而不属于 MCP 进程。重启后先调用
`tbm_durable_get_session`，检查当前 status/version，再继续合法 transition。
prepare、finalization、start、cancellation、abandonment 与 completion retry 保留
durable service 的精确重放规则。replay export 仍要求精确版本、classification 上限与
新鲜授权，并且只有显式启用后才可用。

真实进程测试会用 SDK 的 STDIO client 连续启动三个 MCP 子进程：第一个执行 prepare，
第二个续接并 complete，第三个重试 completion 并 export replay。
