# Trace-backed Memory TypeScript SDK

Typed, dependency-free Node.js client for the local `tbm.agent.v1` HTTP
profile. The package follows the repository's canonical OpenAPI 3.1 document
and uses the same six routes as the Python client.

This is a single-user, single-host client. It accepts only explicit IPv4
loopback `http://` URLs, requires a 32–512 character bearer secret, rejects
redirects and malformed or oversized JSON, and never retries automatically.
It does not provide TLS, user identity, tenant isolation, or remote/shared
service support.

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
