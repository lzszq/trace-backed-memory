# Durable GateSession version-3 contract

**English** | [简体中文](gate-session-v3.zh-CN.md)

`tbm.gate-session.v3` is the persistence-neutral domain contract for the
durable runtime lifecycle planned for SQLite v2, PostgreSQL v3, `tbmd`, HTTP,
MCP, and SDK adapters. It is not a claim that the current local MCP server
persists pending requests. The active runtime remains snapshot v2, SQLite v1,
PostgreSQL v2, and process-local pending Gate requests.

## Identity and concurrency

Every session binds these server-resolved identities:

- tenant, canonical repository, principal, and agent client;
- Trace and run IDs;
- a canonical request fingerprint and caller idempotency key.

The record is immutable. Each transition creates a new record with
`version + 1`; callers must present the exact `expected_version`. A stale
revision fails with `TBM_GATE_SESSION_STALE_VERSION`. Repository adapters must
enforce the same optimistic check atomically when they are implemented.

`created_at`, `updated_at`, `expires_at`, and lease timestamps are
service-authoritative fields. An agent client must never choose them. The
contract functions stay deterministic for replay and therefore do not consult
the wall clock; a future repository/service must supply transactional database
or trusted service time and reject a request received after its live lease or
expiry even if the client presents an older timestamp.

## Lifecycle

```text
CREATED
  -> PREPARED
  -> AWAITING_DECISION
  -> DECIDED
  -> FINALIZED
  -> EXECUTING
  -> COMPLETED

CREATED/PREPARED/AWAITING_DECISION -> CANCELED
PREPARED/AWAITING_DECISION         -> EXPIRED
EXECUTING                          -> ABANDONED
```

Terminal states cannot transition again. `PREPARED` through `EXECUTING`
require an active lease. `COMPLETED`, `CANCELED`, `EXPIRED`, and `ABANDONED`
clear it. Lease renewal is a versioned immutable update and must occur before
the current lease expires.

The lifecycle fields are cumulative:

- preparation records a retrieval snapshot and System Gate evaluation;
- decision records the decision and ordered semantic Gate attempts;
- finalization records exact memory revisions, injection artifact, and usage
  decision;
- completion records a run outcome;
- cancellation, expiry, and abandonment record a bounded terminal reason.

The contract stores only references to those artifacts. Their own version-3
contracts and repositories are separate delivery units.

## Strict external form

The canonical Schema is
`schemas/gate_session_v3.schema.json`; the packaged example is
`examples/gate_session_v3.example.json`. Every field is required, including
nullable fields, and unknown fields are rejected.

`loads_gate_session()` bounds input to 1 MiB, 10,000 JSON nodes, and depth 32.
It rejects duplicate keys, invalid UTF-8, non-finite numbers, invalid types,
unknown fields, impossible lifecycle shapes, and non-RFC-3339 timestamps.
`dumps_gate_session()` emits deterministic canonical JSON and normalizes
timestamps to UTC.

Stable contract errors are:

- `TBM_GATE_SESSION_INVALID`
- `TBM_GATE_SESSION_INVALID_JSON`
- `TBM_GATE_SESSION_INVALID_TRANSITION`
- `TBM_GATE_SESSION_STALE_VERSION`

## Current boundary

`GateSession`, `create_gate_session()`, `transition_gate_session()`, and
`renew_gate_session_lease()` define and test the target lifecycle. They do not
reconstruct `MemoryGateRequest._store_token`, alter the current Store, change
snapshot or database versions, or make the existing STDIO MCP lifecycle
restart-resumable. A later coordinated migration must implement the
authoritative repositories, atomic idempotency index, expiry worker, recovery,
and adapter conformance before durable runtime claims are valid.
