# Codex App Server Ingestion v1

**English** | [简体中文](codex-app-server-ingestion-v1.zh-CN.md)

## Purpose

`tbm.codex-app-server-ingestion.v1` is an opt-in, storage-neutral adapter from
the pinned Codex App Server v2 notification surface emitted by Codex CLI
`0.146.0` into the existing `tbm.trace.event_recorded` family. It provides a
strict evidence boundary; it is not wired into the default Agent, MCP, HTTP,
or daemon profiles.

The deep module is `CodexAppServerTraceRecorder`. A trusted factory binds one
ledger, Trace/run/thread identity, `EventTrustedContext`, clock,
classification, retention policy, sequence cursor, global-position cursor,
and optional retained Trace parent. Notification JSON cannot supply or
override those values.

## Supported observations

The adapter accepts only notification envelopes from App Server wire version
`v2`. It maps:

- `hook/started` and `hook/completed` for `preToolUse`,
  `permissionRequest`, `postToolUse`, `preCompact`, `postCompact`,
  `sessionStart`, `sessionEnd`, `userPromptSubmit`, `subagentStart`,
  `subagentStop`, and `stop`;
- `turn/diff/updated`;
- `item/completed` only when the item is an `agentMessage` whose phase is
  `final_answer`.

Permission-request start is `pending`; completion remains `unknown` because a
Hook completion notification does not prove allow or deny. Tool-related Hook
start/completion observations derive one stable correlation identifier from
the trusted Trace/run binding and the pinned Hook `run.id`.

Validated streaming deltas, patch updates, reasoning deltas, and deprecated
context-compaction notifications are skipped without writing evidence.
Commentary/phase-unknown agent messages and pinned non-agent completed-item
variants are also skipped after thread and top-level shape validation. Unknown
methods, unknown item variants, requests/responses, realtime transcript
notifications, and guessed direct-Hook stdin payloads fail closed.

## Artifact-first content boundary

The recorder does not store Artifact bytes. Before calling
`ingest_notification()`, the caller must persist the exact raw notification
frame through an authorized Artifact Authority and pass exactly one
`EventArtifactRef` for that frame. The recorder verifies:

- content-derived Artifact ID and SHA-256 against the exact input bytes;
- `application/json` media type and exact byte size;
- the trusted classification and retention policy;
- `available` state.

The recorder rejects `public` classification because pinned App Server frames
may contain prompts, paths, diffs, tool output, or responses. Use `internal` or
a stronger protected classification, and ensure the ledger classification
filter permits it before constructing the recorder.

Only the descriptor enters the canonical Trace event. Hook output, source
path, diff, final response, prompt fragments, and other raw frame content never
enter event payload metadata. Protected frames continue to require the normal
encryption-key descriptor. A successfully stored Artifact followed by a
rejected Trace append may remain as an unreferenced Artifact; this adapter does
not claim cross-authority atomicity.

## Strict input and ordering

Each frame must be strict UTF-8 JSON no larger than 8 MiB, with at most
100,000 nodes and depth 100. Duplicate keys and non-finite numbers are
rejected. The pinned method overlays reject unknown fields and invalid
identifiers, enums, timestamps, Hook entries, paths, patch kinds, or trusted
thread identity. Hook output text may contain normal multiline and tab text;
structural identifiers may not contain control characters.

Each appended notification creates one contiguous Trace event at the exact
configured sequence/global cursor. Canonical `occurred_at`, `recorded_at`, and
source observation time come from the trusted clock; App Server numeric times
remain evidence inside the exact frame Artifact and are not trusted as scope
or authorization input.

If the ledger may have committed but its response is lost, the exact pending
event remains private in the recorder. A stable pre-commit ledger rejection
clears that pending candidate and returns `TBM_CODEX_APP_SERVER_APPEND_REJECTED`
so the caller can reconstruct from the current durable head. New input after an
uncertain result is rejected with
`TBM_CODEX_APP_SERVER_PENDING_RESUME_REQUIRED` until `resume_pending()`
replays the same idempotent command. Cursor and parent advance only after a
confirmed insert or exact replay.

## Current boundary

This adapter adds no event type, database schema, packaged resource, or
projection. It reuses the registered Trace-event family and the existing
SQLite/PostgreSQL ledger implementations. It does not:

- parse transcript fragments into final facts;
- infer allow/deny from Hook completion;
- authenticate Codex or persist Artifact bytes;
- construct a Trace aggregate or reducer projection;
- select automatic Hook capture or default Agent/MCP/HTTP ingestion;
- change snapshot 2, SQLite 1, PostgreSQL 2, or `full_persistence=false`.

See [Ordered Trace Event Protocol v1](trace-event-v1.md) and the
[Codex integration guide](../integrations/codex.md).
