# Codex ingestion protocol v1

**English** | [Simplified Chinese](codex-ingestion-v1.zh-CN.md)

## Status and boundary

`tbm.codex-ingestion.v1` is an opt-in adapter from structured Codex Hook and
Codex App Server frames to the sealed [ordered TraceEvent protocol](trace-event-v1.md).
It does not install hooks, open an App Server connection, or change the default
Agent, MCP, HTTP, SDK, compatibility Trace, snapshot, SQLite, or PostgreSQL
profiles. `full_persistence` remains `false`.

The host adapter is trusted to authenticate the local Codex source, supply the
receive time and fixed `CodexIngestionBinding`, and use a protected Artifact
writer. Source JSON cannot choose organization, tenant, repository, Trace, run,
lineage, authorization, or ledger access. Scope matching remains provenance
validation, not authorization.

## Source mapping

| Codex ingestion event | Hook source | App Server source | TraceEvent |
|---|---|---|---|
| `SessionStart` | `SessionStart` | `thread/started` | `tbm.trace.session_started` |
| `UserPromptSubmit` | `UserPromptSubmit` | completed `userMessage` | `tbm.trace.user_prompt_submitted` |
| `PreToolUse` | `PreToolUse` | started supported tool item | `tbm.trace.tool_started` |
| `PermissionRequest` | `PermissionRequest` | the three stable `requestApproval` methods | `tbm.trace.permission_recorded` |
| `PostToolUse` | `PostToolUse` | completed supported tool item | `tbm.trace.tool_completed` |
| `SubagentStart` | `SubagentStart` | completed `subAgentActivity(kind=started)` | `tbm.trace.subagent_started` |
| `SubagentStop` | `SubagentStop` | completed `subAgentActivity(kind=interrupted)` | `tbm.trace.subagent_stopped` |
| `PreCompact` | `PreCompact` | started `contextCompaction` | `tbm.trace.pre_compact` |
| `Stop` | `Stop` | `turn/completed` | `tbm.trace.stopped` |
| `SessionEnd` | `SessionEnd` | `thread/closed` | `tbm.trace.session_ended` |
| `DiffUpdate` | — | `turn/diff/updated` | `tbm.trace.diff_observed` |
| `FinalResponse` | — | completed `agentMessage(phase=final_answer)` | `tbm.trace.final_response_recorded` |

`PostCompact`, incremental deltas, unrelated notifications, non-final agent
messages, and `subAgentActivity(kind=interacted)` are not facts in this
protocol. A transcript or transcript-like notification is rejected as a fact
source. A transcript path may occur inside the protected raw Hook frame, but
the adapter never reads the referenced transcript or copies that path into
ledger metadata.

## Strict capture and protected bytes

Each input is bounded to 1 MiB, depth 64, and 50,000 JSON nodes. UTF-8, duplicate
keys, non-finite numbers, required identities, stable envelope fields, source
method/event mapping, and timestamps fail closed. The exact accepted frame
bytes are passed to the trusted writer, which must return one available,
encrypted, confidential or restricted `EventArtifactRef` using media type
`application/vnd.trace-backed-memory.codex-source+json` and retention policy
`retention_codex_source_v1`. Digest and byte count must match the input exactly.

The TraceEvent ledger contains only that descriptor. Raw prompts, tool inputs,
tool output, diffs, final responses, and transcript paths remain in protected
Artifact content. A deterministic source-record identity binds source profile,
mapped event, method, session, turn, and Artifact content digest.

Capture records and Artifact descriptors are trusted in-process values, not an
untrusted wire authorization boundary. Callers must create records through the
`capture_codex_*` functions and must not manufacture `CodexSourceRecord`
instances from request JSON. Artifact existence and access are enforced by the
configured Artifact authority, not by the descriptor-only record.

## Time and permission evidence

Hook frames have no authoritative event timestamp, so the adapter uses the
trusted receive time. App Server notifications retain exact `startedAtMs`,
`completedAtMs`, or `emittedAtMs`; live capture rejects a source clock more than
300 seconds from the trusted receive clock. Older stable notifications without
`emittedAtMs` use receive time.

A permission decision must bind the SHA-256 digest of the exact raw approval
frame. Official Hook `PermissionRequest` has no `tool_use_id`, so it must match
exactly one active Hook invocation by tool name and canonical tool-input digest;
zero or multiple matches fail closed. App Server persists a source-specific,
turn-scoped correlation derived from `threadId`, `turnId`, and `itemId`, so a
cross-turn reuse cannot match the active item. Command/file approval methods
also bind their mapped tool family; the generic permissions method may narrow
only the exact active item. The permission event occurs at trusted decision time;
the App Server request start must not follow that decision, and the decision
must be within 300 seconds of trusted receipt. The observed permission result
does not authorize the ledger append.

## Lifecycle and append

`build_codex_ingestion_trace_drafts()` requires a complete prior TraceEvent
history for the trusted binding. It enforces one first `SessionStart`, no event
after `SessionEnd`, exact non-reused tool lifecycle, paired subagent lifecycle,
no active tool or subagent at session end, and one non-reused protected source
Artifact per event. Multiple exact approval callbacks may remain attached to
one active App Server item; each has its own raw-frame Artifact and permission
request digest.

`append_codex_ingestion_batch()` maps a non-empty batch of at most 100 records
and delegates to the existing access-bound TraceEvent ledger append. The event
batch is atomic and exactly idempotent under the ledger contract. Artifact
capture is a separate protected-content operation: a valid Artifact can remain
as immutable orphan evidence when later binding, lifecycle, or ledger CAS
validation rejects the event batch. It is not a Trace fact or projection input
and must be handled by the configured retention policy.

## Public API

- `capture_codex_hook_event()`
- `capture_codex_app_server_notification()`
- `capture_codex_app_server_permission()`
- `build_codex_ingestion_trace_drafts()`
- `append_codex_ingestion_batch()`
- `codex_ingestion_projection()`

The focused conformance suite covers every mapping above, official Hook
permission correlation without a tool ID, current App Server item and clock
fields, exact permission/request binding, transcript rejection, trusted scope,
lifecycle rejection, protected bytes, rollback, replay, and exact retry.
