# Durable HTTP profile：`tbm.durable-agent-wire.v1`

[English](durable-http-v1.md) | **简体中文**

显式的 `tbm-http --profile durable-v3` profile 通过有界 HTTP 暴露完整 durable
Agent 生命周期。它使用统一 SQLite v3 authority graph 或隔离 PostgreSQL v3
authority；服务器与数据库 runtime 重新打开后仍可继续原 session。

`tbm-http` 的默认行为仍为 `compat-v2`，绝不会隐式选择 durable v3。

## 安全与身份边界

- 每条 route 都要求来自 `TBM_DURABLE_HTTP_TOKEN` 的 bearer secret。
- request JSON 绝不提供 caller、provider、evaluator、tenant、environment、
  authorization event 或 authority identity。
- operator 持有的 `DurableHTTPApplication` factory 提供
  `DurableRuntimeDependencies`，并在 bearer 认证后派生实时可信 context。
- 除非设置相应的显式启动参数，否则 injection 与 replay bytes 均不暴露。
- 本地 profile 应绑定 IPv4 loopback；非 loopback IPv4 绑定必须使用 TLS，最低
  TLS 1.2。可启用 client certificate 校验，但它不替代 bearer 边界；本 profile
  拒绝 IPv6。
- header、body、JSON 深度/节点数、canonical base64、response 大小、worker 数、
  queue 长度以及 connection/TLS handshake 时间均有上限。

本地 bearer 只证明调用方可访问该进程；它不是 tenant identity，也不会把此
profile 变成不可信多租户服务。

## 可信 application factory

在 operator 控制的环境中创建可导入模块：

```python
from trace_backed_memory.durable_http_entry import DurableHTTPApplication

from my_service.tbm_dependencies import (
    durable_runtime_dependencies,
    trusted_contexts_for_http_request,
)


def create_application() -> DurableHTTPApplication:
    return DurableHTTPApplication(
        dependencies=durable_runtime_dependencies(),
        context_provider=trusted_contexts_for_http_request,
    )
```

只有 bearer secret 匹配后，context provider 才会收到有界 transport evidence，
并返回 `DurableHTTPAuthenticatedContexts`。repository 解析、entity registry
查询、provider 注册与 evaluator 认证始终由服务端持有。

## 启动 SQLite v3

安装 service 依赖，并创建至少 32 个字符的私密 secret：

```powershell
python -m pip install -e ".[service]"
$env:TBM_DURABLE_HTTP_TOKEN = "<random-secret-at-least-32-characters>"
$env:TBM_DURABLE_HTTP_APPLICATION_FACTORY = "tbm_local_app:create_application"
tbm-http --profile durable-v3 --sqlite .tbm\durable.sqlite3 --initialize
```

```bash
python -m pip install -e '.[service]'
export TBM_DURABLE_HTTP_TOKEN='<random-secret-at-least-32-characters>'
export TBM_DURABLE_HTTP_APPLICATION_FACTORY='tbm_local_app:create_application'
tbm-http --profile durable-v3 --sqlite .tbm/durable.sqlite3 --initialize
```

数据库父目录必须已经存在。只在第一次原子安装 bundle 时使用
`--initialize`；后续重启不带该参数：

```text
tbm-http --profile durable-v3 --sqlite .tbm/durable.sqlite3
```

PostgreSQL 使用 `--postgres-env ENV_NAME`；该环境变量保存 connection string，
并且隔离 v3 schema 必须已经安装和验证。

## Route 与 capability negotiation

`GET /durable/v1/capabilities` 返回 `tbm.durable-agent-wire.v1`、durable
session、storage mode 以及 content exposure 状态。
`GET /durable/v1/openapi` 返回规范 OpenAPI 3.1 契约。生命周期 route 为：

```text
prepare → decide → finalize → start/resume/abandon
        → complete/cancel → get-session → export-replay
```

每次状态修改都携带 expected session version；stale transition 会 fail closed。
重试复用 durable session/run identity，dispatcher 不保存进程内 lifecycle handle。

## Python 与 TypeScript client

包根导出的 `DurableAgentHTTPClient` 与
`AsyncDurableAgentHTTPClient` 分别提供同步/异步 Python 调用。无运行时依赖的
Node.js 包会导出自己的 `DurableAgentHTTPClient`、类型化 request/response、
`DurableAgentHTTPError` 与 `durableSessionReference()`：

```ts
import {
  DurableAgentHTTPClient,
  durableSessionReference,
} from "@trace-backed-memory/agent-http";

const client = new DurableAgentHTTPClient({
  baseUrl: "http://127.0.0.1:8766",
  token: process.env.TBM_DURABLE_HTTP_TOKEN!,
});

const capabilities = await client.negotiate();
if (capabilities.transport_profile !== "durable-v3") {
  throw new Error("durable HTTP was not selected");
}

const nonce = Date.now().toString(36);
const prepared = await client.prepare({
  request_id: `request_${nonce}`,
  trace_id: `trace_${nonce}`,
  run_id: `run_${nonce}`,
  task_mode: "repair",
  commit_sha: "replace-with-the-current-commit",
  attributes: { branch: "main" },
  retrieval_mode: "hybrid",
  retriever_id: "reference_retriever",
  retriever_version: "v1",
  top_k: 10,
  idempotency_key: `durable_retrieval_${nonce}`,
  expires_in_seconds: 3600,
  lease_seconds: 60,
  query_base64: "cmVwYWlyIHRoZSBjYWNoZQ==",
  semantic_query: {
    provider_id: "reference_embeddings",
    provider_version: "v1",
    vector: [0.6, 0.8],
  },
});
await client.cancel({
  ...durableSessionReference(prepared),
  reason: "caller canceled the run",
});
```

仓库测试会让 Python 与 TypeScript client 运行同一份完整 lifecycle fixture。request
JSON 绝不接受 caller、tenant、repository、provider authentication 或 evaluator
authentication identity。每个后续调用都必须使用上一次响应返回的精确
`session_id` 与 version。

Python 与 TypeScript 默认都不自动重试。TypeScript 可通过 `maxAttempts` 显式启用
最多五次尝试；它只重试公开 error envelope 中标记 `retryable: true` 的失败，并复用
完全相同的序列化 request。服务端 idempotency 与精确版本检查始终是最终依据。
`heartbeat()` 是 TypeScript 对 `resume()` 的别名，用来续签 execution lease。
abort 或 timeout 只会停止 client 等待，不能证明服务端已经收到的 mutation 被取消。

## Content 与 replay

默认只返回 descriptor。精确 rendered injection bytes 需要
`--expose-injection-content`；replay bytes 还需要
`--expose-replay-content`、当前 `artifact:read` authorization decision、显式
classification allowlist、精确 session version 与 request byte limit。

另见 [durable Agent wire v1](durable-agent-wire-v1.zh-CN.md)、
[durable Agent v3](durable-agent-v3.zh-CN.md) 与
[统一 SQLite v3 bundle](sqlite-bundle-v3.zh-CN.md)。
