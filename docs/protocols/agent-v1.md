# Local agent protocol: `tbm.agent.v1`

**English** | [简体中文](agent-v1.zh-CN.md)

`trace_backed_memory.agent` is the focused application boundary over the
existing evidence, Gate, Store, and persistence kernel. It does not duplicate
policy and does not expose Store request tokens.

## Capability discovery

```text
tbm capabilities
```

The deterministic result reports protocol and storage versions, supported
modes and operations, hard limits, durable records, and process-local records.
It requires no snapshot and performs no network access.

`LocalAgentMemory.health()` reports only non-sensitive pending/replay counts,
memory metrics, and measured-run metrics. The optional STDIO MCP profile maps
these operations to `tbm_capabilities` and `tbm_health`; the optional local
HTTP profile maps them to `/v1/capabilities` and `/v1/health`.

## Lifecycle

```text
capture pending Trace
  -> prepare (register Trace, retrieve, System Gate, bounded prompt)
  -> finalize (strict semantic decision, narrowing, recheck, render, audit)
  -> execute using only snippet
  -> complete with explicit measurement
```

Use `cancel` when a prepared request will not be finalized. `run` combines the
same phases with decision and execution callbacks and preserves recovery IDs in
`AgentMemoryError` if a callback fails.

The MCP mapping is `tbm_prepare_memory` -> `tbm_finalize_memory` ->
`tbm_complete_run`, with `tbm_cancel_run` for an abandoned prepared request.
The HTTP mapping is `/v1/prepare` -> `/v1/finalize` -> `/v1/complete`, with
`/v1/cancel`. Both transports use one strict dispatcher, return the same
protocol payloads, and preserve the process-local request boundary. See the
[local HTTP and Python/TypeScript SDK guide](agent-http-v1.md).

## Persistence semantics

`LocalAgentMemory.open_sqlite()` and `.open_postgres()` load an existing Store
and synchronize each durable phase. Trace registration is persisted before a
request is prepared; finalized usage is persisted before returning; measured
Trace and usage completion are persisted atomically by the Store.

Pending requests and same-process finalization replay entries are not
persisted. The same `LocalAgentMemory` instance must own prepare through
finalize or cancel. Restarting the process invalidates a prepared handle.
Request IDs are opaque and include a fresh 128-bit Store-session namespace so
an abandoned handle cannot collide with a new request after restart. The
numeric suffix resumes above the highest audited request, but it does not
restore a pending handle. The replay cache is bounded to 10,000 finalized
requests, and capability discovery reports both process-local records and
their limits.

## Idempotency and errors

Within one runtime and while its bounded replay entry is retained, repeating
finalize with the same canonical decision returns the original result. A
different decision returns `TBM_AGENT_DECISION_CONFLICT`. Durable adapters
preserve Store exact-replay semantics for usage and completion.

Documented capture, lifecycle, callback, and persistence boundary failures use
`AgentMemoryError` with a stable `TBM_*` code, category, operation,
retryability, and optional validated request/decision IDs. Messages are
nonblank and capped at 2,048 characters. Unexpected interpreter failures and
direct construction of low-level domain records are not protocol envelopes.
The packaged `agent_error.schema.json` is the external envelope contract.

## Protocol resources

The distribution includes byte-identical schemas and examples for:

- agent capabilities;
- runtime health;
- prepare, finalize, complete, and cancel requests;
- canceled request;
- prepared memory;
- finalized memory;
- completed run;
- stable error envelope.

The canonical OpenAPI 3.1 document binds the local HTTP routes to those
contracts. Dispatcher, real STDIO MCP, and HTTP lifecycle conformance tests
exercise one scenario through the same payload and error semantics. These are
a separately versioned application protocol. They do not change snapshot,
SQLite, or PostgreSQL schema versions.
