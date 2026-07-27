# Durable GateSession version-3 contract

**English** | [简体中文](gate-session-v3.zh-CN.md)

`tbm.gate-session.v3` defines the durable runtime lifecycle planned for
SQLite v2, PostgreSQL v3, `tbmd`, HTTP, MCP, and SDK adapters. Its domain
record remains persistence-neutral. Opt-in, side-by-side SQLite and isolated
PostgreSQL repositories now persist immutable revisions, but this is not a claim that the current
local MCP server persists pending requests. The active runtime remains
snapshot v2, SQLite v1, PostgreSQL v2, and process-local pending requests.

## Identity and concurrency

Every session binds these server-resolved identities:

- tenant, canonical repository, principal, and agent client;
- Trace and run IDs;
- a canonical request fingerprint and caller idempotency key.

The record is immutable. Each transition creates a new record with
`version + 1`; callers must present the exact `expected_version`. A stale
revision fails with `TBM_GATE_SESSION_STALE_VERSION`. Repository adapters must
enforce the same optimistic check atomically.

`created_at`, `updated_at`, `expires_at`, and lease timestamps are
service-authoritative fields. An agent client must never choose them. The
contract functions stay deterministic for replay and therefore do not consult
the wall clock; a repository/service supplies transactional database or
trusted service time and rejects a request received after its live lease or
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

## Side-by-side SQLite repository

`SQLiteGateSessionRepository` stores append-only revision payloads plus one
compare-and-swap current head under
`schemas/sqlite-v3-gate-session.sql`. `create_or_get()` scopes idempotency by
tenant, repository, principal, and agent client. Exact request replay returns
the existing session; conflicting identity or fingerprint fails without
overwrite. `transition()` and `renew_lease()` use a trusted service clock,
append one validated revision, and advance the head by exactly one version in
one `BEGIN IMMEDIATE` transaction or caller-owned savepoint. `history()`
retains the revision chain and `list_due()` returns bounded current candidates
without mutating them.

The adapter additionally emits stable `TBM_SQLITE_GATE_SESSION_*` errors for
closed connections, disabled foreign keys or recursive triggers, schema drift,
persistence failures, missing sessions, clock regression, scoped
idempotency/session-ID conflicts, and transitions attempted after lease or
session expiry. Database triggers
protect append-only history, revision continuity, the lifecycle graph, and
immutable head identity even from direct SQL; repository reads still
revalidate canonical payloads and head identity before returning them.

## Isolated PostgreSQL repository

`PostgresGateSessionRepository` exposes the same create/get/history/transition,
lease-renewal, and bounded due-discovery contract over the separately installed
`trace_backed_memory_v3_gate_session` schema. The canonical install and
fail-closed rollback scripts are `schemas/postgres-v3-gate-session.sql` and
`schemas/postgres-v3-gate-session-rollback.sql`; both require and preserve the
active PostgreSQL schema version 2.

Every operation locks active and GateSession metadata before session rows.
Create uses database-enforced C-collated scoped idempotency. Mutations lock the
head row before sampling `clock_timestamp()`, append one canonical revision,
and advance the head with an exact-version CAS inside a transaction or caller
savepoint. Catalog checks reject missing, extra, or reshaped relations,
constraints, indexes, functions, triggers, and columns. Fixed-search-path
trigger functions protect immutable identity, history, lifecycle continuity,
and truncate boundaries. Deferred consistency triggers reject a transaction
that leaves a head without its exact maximum revision, including an orphan
direct-SQL append; reads still cross-check every stored payload.

These repositories are opt-in persistence seams, not active SQLite schema v2
or active PostgreSQL schema v3. They do not reconstruct
`MemoryGateRequest._store_token`, alter the current Store, or make STDIO MCP
restart-resumable. Expiry/recovery workers, service integration,
authorization, and cross-adapter conformance remain required before
GateSession is the distributed runtime authority.
