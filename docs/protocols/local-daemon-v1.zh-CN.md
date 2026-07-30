# 本地 durable daemon v1

[English](local-daemon-v1.md) | **简体中文**

`tbmd` 是显式的本地 SQLite v3 进程 owner。一个 `tbmd local` 进程持有统一
authority graph，通过有界 STDIO MCP 与 bearer-authenticated loopback HTTP 暴露
同一套 `tbm.durable-agent-wire.v1` dispatcher，并运行有界 GateSession recovery
与 completion-outbox delivery page。

这是可信单主机 profile，不是远程、带 peer authentication 的多租户服务。

## 安装并提供可信依赖

安装两项本地 transport extra：

```powershell
python -m pip install -e ".[mcp,service]"
```

daemon 不会从 request JSON 推断 repository、principal、provider、evaluator 或
consumer identity。创建由 operator 控制、可 import 的模块，例如
`tbm_local_app.py`：

```python
from trace_backed_memory.daemon_entry import DurableLocalApplication
from trace_backed_memory.durable_http_server import (
    DurableHTTPAuthenticatedContexts,
)
from trace_backed_memory.durable_mcp_server import DurableMCPTrustedContexts

from my_service.tbm_dependencies import (
    completion_consumer,
    durable_runtime_dependencies,
    evaluator_context,
    provider_context,
    service_context,
)


def create_application() -> DurableLocalApplication:
    dependencies = durable_runtime_dependencies(
        completion_consumer=completion_consumer,
    )
    mcp_contexts = DurableMCPTrustedContexts(
        service_context(),
        provider_context(),
        evaluator_context(),
    )
    return DurableLocalApplication(
        dependencies=dependencies,
        mcp_contexts=mcp_contexts,
        http_context_provider=lambda _request: (
            DurableHTTPAuthenticatedContexts(
                service_context(),
                provider=provider_context(),
                evaluator=evaluator_context(),
            )
        ),
    )
```

此 profile 强制要求 `DurableRuntimeDependencies.completion_consumer`。consumer
必须按 immutable outbox `event_id` 去重；delivery 是 at-least-once。

设置 factory 与至少 32 字符的私有 bearer secret：

```powershell
$env:TBM_DURABLE_DAEMON_APPLICATION_FACTORY = "tbm_local_app:create_application"
$env:TBM_DURABLE_HTTP_TOKEN = "<random-secret-at-least-32-characters>"
```

```bash
export TBM_DURABLE_DAEMON_APPLICATION_FACTORY='tbm_local_app:create_application'
export TBM_DURABLE_HTTP_TOKEN='<random-secret-at-least-32-characters>'
```

## 初始化、诊断与运行

只初始化一次，然后执行离线检查：

```text
tbmd init --state-dir .tbm
tbmd doctor --state-dir .tbm
```

`init` 会在 `.tbm/durable.sqlite3` 原子安装生成的 15-component SQLite v3
bundle。`doctor` 会取得单实例锁，并检查 state directory、固定 database target、
完整 schema fingerprint、可信 application factory、bearer 格式和已配置 outbox
consumer。另一 daemon 持有 state directory 时，它会有意 fail closed。

启动完整本地 profile：

```text
tbmd local --state-dir .tbm
```

默认 HTTP endpoint 为 `http://127.0.0.1:8766`。无需把 token 写到命令行即可检查：

```text
tbmd health --base-url http://127.0.0.1:8766
```

只需 SDK 后台服务时，显式使用 `--no-mcp`。默认命令会把 stdin/stdout 保留给有界
MCP JSONL，并在 companion thread 中启动 HTTP 与 worker。关闭 MCP STDIO、按
Ctrl+C，或收到受支持的 termination signal 后，daemon 会停止接收 HTTP、join
worker、关闭共享 runtime，并释放锁。

## MCP 客户端

完成 `tbmd init` 后，把 daemon 本身作为项目 MCP command。Codex 项目配置如下：

```toml
[mcp_servers.trace_backed_memory]
command = "tbmd"
args = ["local", "--state-dir", ".tbm"]
```

Claude Code 与 Pi 在各自 MCP 配置中使用同一 command 和 arguments。Pi 仍需使用
外部 [`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter?name=mcp) 作为 MCP
客户端；该 adapter 不属于 Trace-backed Memory。

先调用 `tbm_durable_capabilities`。每次 mutation 后都要保存返回的 `session_id`
与精确 session version。之后的 daemon 进程会重新打开同一 SQLite graph，并通过
`get_session` 继续；它不会恢复任何进程内 handle。

## Recovery 与 delivery 语义

每次 worker tick 都只处理有界 page：

- session TTL 已过期的 PREPARED 或 AWAITING_DECISION session 会通过精确版本 CAS
  转为 terminal `EXPIRED`。
- 只有 lease 过期，以及 due DECIDED、FINALIZED 或 EXECUTING session，仍返回
  `recovery_required`；daemon 不会虚构 abandonment decision。
- Pending/retry-wait outbox delivery 和已过期 lease 会由新 worker lease 重新
  claim。consumer 已执行但 acknowledgement 前 crash，可能再次投递同一 immutable
  event。
- Recovery/outbox operation 与 HTTP/MCP 共享同一 runtime RLock 和 SQLite
  connection；不会另建 dispatcher。

Worker interval、page size、outbox lease、retry delay 与 maximum attempts 都是
有界 CLI option。意外 worker failure 会缩减为稳定 error code，并在之后 tick 重试；
进程内 worker status 不会写入 request identity 或 secret。

## State 与安全边界

State directory 必须是 canonical、无 alias、可读写，且不能是 Windows reparse
point；其 ancestor chain 不得可被其他本地账户替换。在 POSIX 上，它必须属于当前
用户，且 group/other permission bit 全部关闭；database 与持久锁 placeholder 必须是
owner-only、single-link regular file。在 Windows 上，daemon 会拒绝
reparse/hard-link alias，并要求本地 read/write/traverse 访问；operator 仍负责配置
owner-only ACL。

`.tbm/tbmd.lock` 上已持有的 OS advisory lock 才是 ownership authority，PID file
不是。第二个进程会以 `TBM_LOCAL_DAEMON_ALREADY_RUNNING` 失败。crash 后由 OS
释放锁；下一次打开时会重新检查持久 placeholder 的 identity。

Loopback bearer authentication 只证明调用方可访问这个本地进程。它不是 tenant
identity、shared-service authorization，也不能作为绑定不可信网络的理由。

## 验证

仓库测试覆盖：

- 一个真实进程通过同一 dispatcher 同时提供 MCP 与 HTTP；
- MCP mutation 经 HTTP 读回、并发精确版本 HTTP mutation/replay，以及被拒绝的第二
  daemon；
- 硬终止进程、重开并继续 session；
- 过期 session recovery；
- 模拟 ack 前 crash 后的 outbox lease reclaim；
- state directory、database、symlink/reparse/hard-link 与锁检查；
- 确定性 `init`、`doctor`、`health`、startup error 与 package entry metadata。
