# Ordered Trace Event Protocol v1

Simplified Chinese: [trace-event-v1.zh-CN.md](trace-event-v1.zh-CN.md)

## Purpose

`tbm.trace-event.v1` is the storage-neutral protocol for ordered engineering
execution evidence. It records a strict Trace event inside the existing
`tbm.event.v1` canonical envelope and reuses the existing event-ledger append
transaction. It does not add an independent authority or database schema.

The protocol is evidence-first. Prompt text, tool input/output, diffs, final
responses, and other potentially sensitive bytes are not payload fields. They
are retained by an Artifact authority and referenced by descriptor only.

## Canonical event

The sealed event registry contains one observation type:

- `tbm.trace.event_recorded`

The concrete engineering event name is the bounded `trace_event_type` payload
field. A future trusted adapter can therefore map `SessionStart`,
`UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, subagent,
compaction, stop, diff, and final-response records without consuming one
registry slot per integration-specific event.

Every event has:

- one `trace_id` stream and one `run_id`;
- a positive `sequence` equal to canonical `stream_version`;
- an exact canonical UTC `occurred_at` timestamp;
- a required `EventSource` descriptor with source record identity and evidence
  quality;
- zero or more sorted, unique Artifact references, with only their IDs repeated
  in the payload;
- optional `tool_correlation_id`, `parent_trace_id`, `subagent_id`, and
  `causation_event_id` links;
- a required permission result: `not_applicable`, `allowed`, `denied`,
  `pending`, or `unknown`;
- the exact trusted authorization decision, classification, and retention
  policy in the canonical envelope.

The first event has sequence 1 and no stream parent. Later events advance by
exactly one and bind the preceding event hash. Parent verification also requires
the same Trace and run identities.

## Atomic batches

`build_trace_event_batch()` accepts between 1 and 100 records, matching
`EVENT_LEDGER_MAX_APPEND_BATCH`. One batch has:

- contiguous Trace sequence and global positions;
- one `batch_first_sequence` and `batch_size` descriptor in every payload;
- one partition-scoped, identity-addressed batch idempotency key;
- one command digest over every record, source descriptor, Artifact descriptor,
  classification, retention policy, complete trusted context, and recorded time;
- one common request ID and command digest across all canonical events, as
  required by the event-ledger port.

Changing any record while reusing the same batch identity produces an
idempotency conflict. `verify_trace_event_batch()` reconstructs the complete
command digest and rejects partial, reordered, non-contiguous, or mixed-command
batches. A one-event append is the same protocol with `batch_size=1`.

`append_trace_event_batch()` is the required persistence entry point for this
typed protocol. It verifies the complete batch and exact ledger access context
before calling the generic atomic append port. The generic event ledger remains
extensible and intentionally does not infer Trace-specific payload semantics;
calling its raw `append()` or `append_once()` methods is not equivalent to a
typed Trace append.

## Validation and replay

`TraceEventRecordRef` validates bounded identifiers, canonical timestamps,
permission state, parent identity, Artifact descriptors, classification, and
retention. `parse_trace_event()` verifies payload/envelope linkage and the
deterministic event and batch identity. `verify_trace_event_parent()` checks one
stream edge; `verify_trace_event_batch()` checks the entire append command.

After the typed preflight, SQLite and PostgreSQL use the generic canonical event
ledger. Exact replay,
stream verification, tenant/repository partition checks, classification
filtering, transaction rollback, and packaged registry schema parity therefore
reuse the same tested storage path.

Stable validation failures use `TBM_TRACE_EVENT_INVALID` with bounded messages.

## Security boundary

- Trace events are evidence. They are never default prompt memory.
- Scope and repository fields come from the trusted ledger access context, not
  the observation payload.
- The payload contains Artifact IDs, never raw prompt/tool/diff/response bytes.
- Protected Artifact descriptors still require encryption-key identity and an
  event classification at least as restrictive as the referenced Artifact.
- `unknown` permission evidence is not equivalent to `allowed`.
- Source identity, quality, occurrence time, and observation time are evidence
  supplied by the trusted adapter. The Trace contract validates and binds those
  claims; it does not independently attest their truth.
- Exact ordering is not authorization and does not make an untrusted source
  trustworthy.

## Current boundary

This increment delivers the typed Trace event and atomic batch protocol. The
separate opt-in [Codex App Server ingestion adapter](codex-app-server-ingestion-v1.md)
now maps a pinned v2 notification subset into this family with trusted identity,
Artifact-only exact frames, and exact pending-append resume. The protocol and
adapter still do not:

- parse unstable transcripts into final facts;
- embed Git checkout or ancestry details in the Trace payload (the separate
  Git observation protocol owns those records);
- build a Trace projection or Git graph reducer;
- cut over the compatibility Trace aggregate or default Agent/MCP profiles.

Other integrations must use a trusted, versioned adapter and may not weaken
the canonical event, Artifact, authorization, or ledger contracts.
