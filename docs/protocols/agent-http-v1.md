# Local HTTP and Python/TypeScript SDKs: `tbm.agent.v1`

**English** | [简体中文](agent-http-v1.zh-CN.md)

The optional `tbm-http` process, dependency-free synchronous/asynchronous
Python clients, and dependency-free Node.js TypeScript package expose the
active version-2 local Agent lifecycle over loopback HTTP. STDIO MCP and HTTP
call the same `AgentProtocolDispatcher`; no transport or SDK reimplements
retrieval or Gate policy.

This is a single-user, single-host integration profile. It is not a remote,
shared, or multi-tenant service.

## Two-minute local setup

Install the HTTP service dependency:

```text
python -m pip install -e ".[service]"
```

Create a private bearer secret of 32 to 512 characters and pass it only through
an environment variable:

```powershell
$env:TBM_HTTP_TOKEN = "<random-secret-at-least-32-characters>"
tbm-http --repo-path C:\work\project --sqlite .tbm\memory.sqlite3
```

```bash
export TBM_HTTP_TOKEN='<random-secret-at-least-32-characters>'
tbm-http --repo-path /work/project --sqlite .tbm/memory.sqlite3
```

The SQLite parent directory must already exist. For an ephemeral test profile,
replace `--sqlite ...` with `--memory`. PostgreSQL uses
`--postgres-env ENV_NAME`; the named variable contains the connection string.

Use the typed Python client from another process on the same host:

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

# An external model may only narrow prepared.system_allowed_memory_ids.
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

# Execute using only finalized.snippet, then submit the measured result.
completed = client.complete(
    {
        "decision_id": finalized.decision_id,
        "eval_result": "pass",
    }
)
```

Call `client.cancel({"request_id": prepared.request_id})` instead when a
prepared request is abandoned before finalization.

For asyncio, use the same payloads and typed results without blocking the event
loop:

```python
from trace_backed_memory import AsyncAgentHTTPClient

async with AsyncAgentHTTPClient(
    "http://127.0.0.1:8765",
    os.environ["TBM_HTTP_TOKEN"],
) as client:
    prepared = await client.prepare(
        {"task": "repair the failing checkout", "mode": "repair"}
    )
    await client.cancel({"request_id": prepared.request_id})
```

The Node.js TypeScript SDK is isolated under
[`packages/typescript-sdk`](../../packages/typescript-sdk/README.md):

```text
cd packages/typescript-sdk
npm ci
npm run build
```

```ts
import { AgentHTTPClient } from "@trace-backed-memory/agent-http";

const client = new AgentHTTPClient({
  baseUrl: "http://127.0.0.1:8765",
  token: process.env.TBM_HTTP_TOKEN!,
});
const prepared = await client.prepare({
  task: "repair the failing checkout",
  mode: "repair",
});
await client.cancel({ request_id: prepared.request_id });
```

## Routes and responses

| Method | Route | Result |
|---|---|---|
| `GET` | `/v1/capabilities` | `AgentCapabilities` |
| `GET` | `/v1/health` | bounded, non-sensitive runtime health |
| `POST` | `/v1/prepare` | `AgentPreparedMemory` |
| `POST` | `/v1/finalize` | `AgentFinalizedMemory` |
| `POST` | `/v1/complete` | `AgentCompletedRun` |
| `POST` | `/v1/cancel` | `AgentCanceledRun` |

POST bodies are strict JSON objects. Unknown fields, duplicate keys,
non-finite numbers, invalid UTF-8, oversized input, and unsupported transfer
encoding are rejected before lifecycle dispatch. Responses use the
`tbm.agent.v1` envelopes and carry `X-TBM-Protocol-Version: tbm.agent.v1`.
The packaged resource allowlist includes the request and response
schemas/examples, including health, cancel, and the stable error envelope.

## Canonical machine contract

[`schemas/agent-http-v1.openapi.json`](../../schemas/agent-http-v1.openapi.json)
is the canonical OpenAPI 3.1 binding for these six routes. It references the
four strict request schemas, the health schema, and the existing capability,
prepared, finalized, completed, canceled, and error schemas. All referenced
files and their canonical examples are installed as byte-identical package
resources and can be exported with `tbm resource export`.

OpenAPI describes the local HTTP subset of `tbm.agent.v1`; capability
discovery also reports embedded operations that are not HTTP routes. The
contract fixes bearer authentication, success/error envelopes,
`X-TBM-Protocol-Version`, body limits, and the process-local pending-request
boundary. It does not imply TLS, remote identity, durable GateSession state,
or shared-service authorization.

## Security and lifecycle boundary

- The server binds only to an explicit loopback IPv4 address. The client also
  rejects non-loopback URLs, HTTPS, URL credentials, paths, queries, and
  fragments.
- Every route requires exactly one matching bearer header. The client disables
  environment proxies and redirects. The TypeScript client uses direct
  `node:http` sockets with no redirect or proxy layer. Tokens are never
  accepted as CLI values or included in object/error representations.
- Connections have a 15-second socket timeout, request dispatch is capped at
  32 worker threads, and the listen queue is bounded. Excess connections are
  closed rather than creating unbounded workers.
- The configured checkout root and optional declared tenant are server-owned.
  Git provenance and ancestry come from that checkout. In the active
  version-2 profile, `--tenant` is applicability metadata, not authorization.
- The server exposes no curation, verification, publication, activation, raw
  Store, snapshot, or migration operation.
- Pending request handles and finalization replay tombstones remain
  process-local. SQLite/PostgreSQL persist the Trace, finalized usage, and
  measured completion, but restarting `tbm-http` invalidates an unfinalized
  request. Prepare again after restart.
- Canceling an async Python task or TypeScript `AbortSignal`, or reaching a
  client timeout, stops waiting but cannot retract a POST already received by
  the server. SDKs do not retry automatically; explicitly call the protocol
  `cancel` operation for an abandoned prepared request.

Loopback plus a bearer secret protects this local process boundary; it does not
provide TLS, user identity, tenant isolation, or shared-service authorization.
`AuthenticatedDurableAgentMemory`, durable GateSession continuation, transport
identity, and remote/shared deployment remain separate future work.
