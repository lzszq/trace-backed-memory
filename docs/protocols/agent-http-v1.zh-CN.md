# 本地 HTTP 与 Python SDK：`tbm.agent.v1`

[English](agent-http-v1.md) | **简体中文**

可选 `tbm-http` 进程与无依赖 `AgentHTTPClient` 通过 loopback HTTP 暴露当前
version-2 本地 Agent lifecycle。STDIO MCP 与 HTTP 调用同一个
`AgentProtocolDispatcher`；两个 transport 都不会复制 retrieval 或 Gate policy。

这是单用户、单主机集成 profile，不是远程、共享或多租户服务。

## 两分钟本地配置

安装 HTTP 服务依赖：

```text
python -m pip install -e ".[service]"
```

生成 32 到 512 字符的私密 bearer secret，并且只通过环境变量传入：

```powershell
$env:TBM_HTTP_TOKEN = "<至少32字符的随机secret>"
tbm-http --repo-path C:\work\project --sqlite .tbm\memory.sqlite3
```

```bash
export TBM_HTTP_TOKEN='<至少32字符的随机secret>'
tbm-http --repo-path /work/project --sqlite .tbm/memory.sqlite3
```

SQLite 父目录必须已经存在。临时测试 profile 可把 `--sqlite ...` 换成
`--memory`。PostgreSQL 使用 `--postgres-env ENV_NAME`，由该命名变量保存连接串。

同一主机上的另一个进程可使用类型化 Python client：

```python
import os

from trace_backed_memory import AgentHTTPClient

client = AgentHTTPClient(
    "http://127.0.0.1:8765",
    os.environ["TBM_HTTP_TOKEN"],
)
prepared = client.prepare(
    {
        "task": "repair the failing checkout",
        "mode": "repair",
        "tool": "pytest",
    }
)

# 外部模型只能缩小 prepared.system_allowed_memory_ids。
finalized = client.finalize(
    {
        "request_id": prepared.request_id,
        "use_memory": False,
        "allowed_memory_ids": [],
        "blocked_memory_ids": [],
        "reason": "no applicable memory",
        "risk": "none",
        "recommended_injection": "none",
    }
)

# 只使用 finalized.snippet 执行，然后提交实测结果。
completed = client.complete(
    {
        "decision_id": finalized.decision_id,
        "eval_result": "pass",
    }
)
```

prepared request 在 finalization 前被放弃时，改为调用
`client.cancel({"request_id": prepared.request_id})`。

## 路由与响应

| 方法 | 路由 | 结果 |
|---|---|---|
| `GET` | `/v1/capabilities` | `AgentCapabilities` |
| `GET` | `/v1/health` | 有界、非敏感的 runtime health |
| `POST` | `/v1/prepare` | `AgentPreparedMemory` |
| `POST` | `/v1/finalize` | `AgentFinalizedMemory` |
| `POST` | `/v1/complete` | `AgentCompletedRun` |
| `POST` | `/v1/cancel` | `AgentCanceledRun` |

POST body 必须是严格 JSON object。未知字段、duplicate key、非有限数字、非法
UTF-8、超限输入与不支持的 transfer encoding 都会在 lifecycle dispatch 前被拒绝。
响应使用 `tbm.agent.v1` envelope，并包含
`X-TBM-Protocol-Version: tbm.agent.v1`。安装资源白名单包含响应
Schema/示例，其中包括 cancel 与稳定 error envelope。

## 安全与生命周期边界

- server 只能绑定显式 loopback IPv4 地址。client 同样拒绝非 loopback URL、
  HTTPS、URL credential、path、query 与 fragment。
- 每个路由都要求精确一条匹配的 bearer header。client 禁用环境 proxy 与
  redirect。token 不能作为 CLI 值传入，也不会出现在对象表示中。
- connection 使用 15 秒 socket timeout，request dispatch 最多使用 32 个 worker
  thread，并且 listen queue 有界；超限 connection 会被关闭，不会无限创建 worker。
- checkout root 与可选 declared tenant 由 server 持有，Git provenance 与 ancestry
  来自该 checkout。当前 version-2 profile 中，`--tenant` 是 applicability metadata，
  不是 authorization。
- server 不暴露 curation、verification、publication、activation、原始 Store、
  snapshot 或 migration 操作。
- pending request handle 与 finalization replay tombstone 仍是进程内状态。
  SQLite/PostgreSQL 会持久化 Trace、finalized usage 与 measured completion，但
  重启 `tbm-http` 会使尚未 finalized 的 request 失效；重启后必须重新 prepare。

loopback 加 bearer secret 只保护这个本地进程边界；它不提供 TLS、用户 identity、
tenant isolation 或 shared-service authorization。`AuthenticatedDurableAgentMemory`、
durable GateSession continuation、transport identity、远程部署与 TypeScript SDK
仍是后续工作。
