# Ordered TraceEvent protocol v1

**English** | [简体中文](trace-event-v1.zh-CN.md)

## Status and boundary

F3-01 introduces ordered engineering `TraceEvent` records instead of treating
the final compatibility `Trace` aggregate as the only evidence. The protocol is
an opt-in typed adapter over the existing `tbm.event.v1` canonical envelope and
`tbm.event-ledger-port.v1`; it does not add another ledger, database schema, or
source of truth.

The compatibility `Trace` model, snapshot version 2, SQLite schema version 1,
PostgreSQL schema version 2, Agent/MCP/HTTP/SDK wire contracts, and default
runtime selection are unchanged. Codex/App Server hook ingestion is F3-05 and
is now provided by the separate opt-in
[Codex ingestion protocol](codex-ingestion-v1.md); it does not change those
default selections or the historical F3-01 credit. `full_persistence` remains
`false`.

## Typed event family

`tbm.trace-event.v1` seals version 1 of these event types:

- session started and ended;
- user prompt submitted;
- tool started, permission recorded, and tool completed;
- subagent started and stopped;
- pre-compact and stop;
- diff observed;
- final response recorded.

Unknown event types or versions remain preservable by the canonical event
ledger but cannot be consumed by the sealed TraceEvent registry. Every payload
uses an exact `additionalProperties: false` schema.

## Frozen payload semantics

Each TraceEvent payload binds:

- `trace_id` and `run_id`;
- a positive `sequence` equal to the canonical event `stream_version`;
- a canonical UTC RFC 3339 `occurred_at` equal to the envelope timestamp;
- sorted, unique `artifact_ids` exactly equal to the envelope's
  content-addressed `EventArtifactRef` descriptors;
- a typed tool correlation or `null`;
- a typed permission result or `null`;
- explicit root/subagent lineage;
- a related subagent identity only for subagent start/stop events.

The stream ID is a stable SHA-256-derived identifier for the `trace_id`.
Sequence must be contiguous from the expected stream head. Canonical stream
parents retain the previous event hash; causation names the previous event, or
the exact parent event for the first event in a subagent stream. Occurrence
timestamps cannot move backwards inside one Trace stream, and neither an event
nor a permission decision may be timestamped after trusted `recorded_at`.

## Tool and permission evidence

A tool correlation contains the bounded `tool_call_id`, tool name, exact phase,
content digest of the invocation, and optional parent tool call. The event type
fixes the phase: request for tool-started, permission for permission-recorded,
and result for tool-completed. Raw prompts, tool inputs, outputs, and final
responses do not belong in ledger metadata; protected or large bytes are
Artifact Authority content referenced by descriptors.

A permission-recorded event contains the permission, decision identity,
`allowed`/`denied`/`unknown` status, bounded reason code, exact decision time,
and request/policy digests. `null` means no permission result was checked or
recorded; it is not synthesized as a denial. The helper for an immutable v3
`AuthorizationDecision` copies its exact content-addressed evidence. The
canonical envelope's `authorization_decision_id` authorizes the ledger append;
it is not silently reinterpreted as the observed tool permission result.

## Parent and subagent lineage

Root lineage has no parent or subagent identity. Subagent lineage requires the
subagent ID, parent Trace ID, and exact parent event ID. The cross-stream
verifier requires that parent to be a same-scope `subagent_started` event whose
related subagent ID matches, and rejects self-parenting, orphaned, cross-scope,
or time-reversed lineage.

Lineage, correlation, and causation are provenance only. They never grant,
inherit, or replace authorization.

## Bounded append

`build_trace_event_append_request()` accepts an exact non-empty tuple of at
most 100 drafts. One batch must share Trace, run, and lineage; sequence must be
contiguous; canonical command and idempotency digests bind every draft and the
trusted recorded time. `append_trace_event_batch()` delegates to the existing
access-bound event ledger port, which atomically commits the entire batch or no
mutation and returns the exact prior receipt on an exact retry. A changed
timestamp, payload, descriptor, scope, expected head, or idempotency command is
not an exact retry.

The SQLite integration test additionally proves caller-transaction rollback
removes the complete TraceEvent batch and that retry returns the byte-identical
receipt. SQLite/PostgreSQL ledger atomicity and cross-backend parity remain
owned by F1; F3-01 does not duplicate those points or introduce a new schema.

## Qualification

The executable contract covers all eight fixed F3-01 requirements:

1. contiguous sequence and exact stream parent;
2. sealed versioned event types;
3. exact canonical timestamp binding;
4. descriptor-only Artifact references;
5. tool-call correlation and invocation digest;
6. explicit permission result versus no check;
7. exact parent/subagent provenance with same-scope verification;
8. bounded atomic/idempotent batch append through the existing ledger port.

The canonical schema, registry catalog, packaged copies, public exports, and
focused positive/negative tests must remain byte-aligned.
