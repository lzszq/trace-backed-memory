# Trace-backed Memory TypeScript SDK

Typed, dependency-free Node.js clients for both local HTTP profiles:

- `AgentHTTPClient` selects the compatible, process-local `tbm.agent.v1`
  profile.
- `DurableAgentHTTPClient` explicitly selects the restart-safe
  `tbm.durable-agent-wire.v1` / `durable-v3` profile.

Plain HTTP is restricted to explicit IPv4 loopback URLs. Both clients require
a bounded bearer secret and reject redirects, duplicate-key JSON, malformed
headers, and oversized bodies. The durable client also supports verified
HTTPS endpoints with an optional CA and server name; transport authentication
still supplies caller identity outside request JSON.

## Local checkout usage

Build or install the package from the repository checkout:

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
const finalized = await client.finalize({
  request_id: prepared.request_id,
  use_memory: false,
  allowed_memory_ids: [],
  blocked_memory_ids: [],
  reason: "no applicable memory",
  risk: "none",
  recommended_injection: "none",
});
await client.complete({
  decision_id: finalized.decision_id,
  eval_result: "pass",
});
```

All methods accept an optional `{ signal }`. Aborting an await or hitting the
timeout stops the client from waiting, but a POST already received by the
server may still complete. The client does not retry. Explicitly call
`cancel({request_id})` when abandoning a prepared request before finalization.

Canonical protocol documentation:
[`docs/protocols/agent-http-v1.md`](https://github.com/lzszq/trace-backed-memory/blob/main/docs/protocols/agent-http-v1.md).

## Durable v3 usage

First create the trusted application factory and storage directory described
in the canonical durable HTTP guide linked below. Then start the explicit
durable profile and negotiate capabilities before the first lifecycle
operation:

```text
export TBM_DURABLE_HTTP_TOKEN="replace-with-a-random-32+-character-secret"
export TBM_DURABLE_HTTP_APPLICATION_FACTORY="tbm_local_app:create_application"
tbm-http --profile durable-v3 --sqlite ./.tbm/durable.sqlite3 --initialize
```

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
if (!capabilities.operations.includes("prepare")) {
  throw new Error("durable prepare is unavailable");
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
const canceled = await client.cancel({
  ...durableSessionReference(prepared),
  reason: "caller canceled the run",
});
```

Every mutation requires the exact returned session version. `heartbeat(...)`
is a typed alias for `resume(...)` and renews the execution lease. Retries are
disabled by default; set `maxAttempts` only when the server marks an error
retryable. Durable idempotency and exact-version checks still determine
whether a replay is safe. The client rejects caller, tenant, provider, and
evaluator identity fields rather than serializing them.

Durable protocol documentation:
[`docs/protocols/durable-http-v1.md`](https://github.com/lzszq/trace-backed-memory/blob/main/docs/protocols/durable-http-v1.md).
