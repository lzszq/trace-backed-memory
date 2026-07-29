# Local HTTP and Python SDK: `tbm.agent.v1`

**English** | [简体中文](agent-http-v1.zh-CN.md)

The optional `tbm-http` process and dependency-free `AgentHTTPClient` expose
the active version-2 local Agent lifecycle over loopback HTTP. STDIO MCP and
HTTP call the same `AgentProtocolDispatcher`; neither transport reimplements
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
The packaged resource allowlist includes response schemas/examples, including
cancel and the stable error envelope.

## Security and lifecycle boundary

- The server binds only to an explicit loopback IPv4 address. The client also
  rejects non-loopback URLs, HTTPS, URL credentials, paths, queries, and
  fragments.
- Every route requires exactly one matching bearer header. The client disables
  environment proxies and redirects. The token is never accepted as a CLI
  value or included in object representations.
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

Loopback plus a bearer secret protects this local process boundary; it does not
provide TLS, user identity, tenant isolation, or shared-service authorization.
`AuthenticatedDurableAgentMemory`, durable GateSession continuation, transport
identity, remote deployment, and TypeScript SDK support remain separate future
work.
