# Documentation index

**English** | [简体中文](index.zh-CN.md)

Use this page as the map into Trace-backed Memory. The README is a quick
orientation; these documents define the engineering contracts.

## Product and architecture

- [Product definition and current capabilities](product.en.md)
- [Detailed API and operations reference](reference.md)
- [Reference architecture](architecture.md)
- [Memory usage policy](usage-policy.md)
- [Product delivery program](product-program.md)

## Agent integration

- [Local agent protocol `tbm.agent.v1`](protocols/agent-v1.md)
- [Local HTTP service and Python/TypeScript SDKs](protocols/agent-http-v1.md)
- [Node.js TypeScript SDK package](../packages/typescript-sdk/README.md)
- [Canonical local Agent OpenAPI 3.1](../schemas/agent-http-v1.openapi.json)
- [Authorization v3 contract](protocols/authorization-v3.md)
- [Entity registry v3 contract](protocols/entity-registry-v3.md)
- [Authenticated retrieval service boundary](protocols/authenticated-service-v3.md)
- [Authenticated durable Gate preparation](protocols/authenticated-gate-service-v3.md)
- [GateSession recovery worker](protocols/gate-recovery-worker-v3.md)
- [SQLite and PostgreSQL Gate evidence v3](protocols/sqlite-gate-evidence-v3.md)
- [Append-only audit and recovery v3](protocols/audit-recovery-v3.md)
- [Structured regression evidence v3](protocols/evidence-v3.md)
- [FixEvidence v3](protocols/fix-evidence-v3.md)
- [MemoryRevision proposal and publication events v3](protocols/memory-revision-v3.md)
- [SQLite MemoryRevision proposal ledger v3](protocols/sqlite-memory-revision-v3.md)
- [PostgreSQL MemoryRevision proposal ledger v3](protocols/postgres-memory-revision-v3.md)
- [SQLite MemoryRevision publication authority v3](protocols/sqlite-memory-publication-v3.md)
- [PostgreSQL MemoryRevision publication authority v3](protocols/postgres-memory-publication-v3.md)
- [Authenticated retrieval preparation v3](protocols/retrieval-preparation-v3.md)
- [Durable retrieval preparation v3](protocols/durable-retrieval-preparation-v3.md)
- [Managed index bundle v3](protocols/managed-index-v3.md)
- [Replayable RetrievalSnapshot v3](protocols/retrieval-snapshot-v3.md)
- [System and Semantic Gate evaluation v3](protocols/gate-evaluation-v3.md)
- [Semantic Gate artifact binding v3](protocols/semantic-gate-artifact-v3.md)
- [Authenticated Semantic Gate service v3](protocols/semantic-gate-service-v3.md)
- [Durable Semantic Gate session composition v3](protocols/durable-semantic-gate-v3.md)
- [Durable finalization composition v3](protocols/durable-finalization-v3.md)
- [Durable execution composition v3](protocols/durable-execution-v3.md)
- [Authenticated durable Agent composition v3](protocols/durable-agent-v3.md)
- [UsageDecision v3](protocols/usage-decision-v3.md)
- [SQLite Semantic Gate artifact repository v3](protocols/sqlite-semantic-gate-artifact-v3.md)
- [PostgreSQL Semantic Gate artifact repository v3](protocols/postgres-semantic-gate-artifact-v3.md)
- [SQLite Semantic Gate attempt ledger v3](protocols/sqlite-semantic-gate-v3.md)
- [PostgreSQL Semantic Gate attempt ledger v3](protocols/postgres-semantic-gate-v3.md)
- [Run outcome and attribution v3](protocols/outcome-v3.md)
- [SQLite RunOutcome completion v3](protocols/sqlite-outcome-v3.md)
- [SQLite OutcomeAttribution ledger v3](protocols/sqlite-outcome-attribution-v3.md)
- [PostgreSQL RunOutcome completion v3](protocols/postgres-outcome-v3.md)
- [PostgreSQL OutcomeAttribution ledger v3](protocols/postgres-outcome-attribution-v3.md)
- [Completion outbox contract and SQLite/PostgreSQL authorities v3](protocols/completion-outbox-v3.md)
- [Durable GateSession v3 domain contract](protocols/gate-session-v3.md)
- [Content-addressed replay and portable export contracts v3](protocols/replay-v3.md)
- [Authenticated encrypted Artifact Authority v3](protocols/artifact-authority-v3.md)
- [Verified ActivatedRevision source v3](protocols/activated-revision-source-v3.md)
- [Codex integration](integrations/codex.md)
- [Claude Code integration](integrations/claude-code.md)
- [Pi integration](integrations/pi.md)
- Repository skills:
  `.agents/skills/maintain-trace-backed-memory/` and
  `.agents/skills/use-trace-backed-memory/`

## Development and operations

- [Development and verification](development.md)
- [Snapshot v3 migration preflight](migrations/snapshot-v3-preflight.md)
- [Version-3 migration bundles and isolated staging](migrations/v3-staging-bundles.md)
- `schemas/sqlite.sql` for the supported local SQL profile
- `schemas/postgres.sql` and `schemas/postgres-v1-to-v2.sql` for PostgreSQL
- `tests/verify_distribution.py` for exact installed-resource verification

## Compatibility boundary

The current formats are snapshot version 2, SQLite schema version 1,
PostgreSQL schema version 2, and agent protocol `tbm.agent.v1`. The optional
`tbm-mcp` command is a long-running local STDIO transport for that protocol,
not another persistence version. Pending gate requests remain process-local.
The persistence-neutral `tbm.gate-session.v3` lifecycle contract and opt-in
side-by-side SQLite and isolated PostgreSQL revision repositories are
published. Opt-in preparation, Semantic Gate, completion, and recovery
services/workers use them, but the active Store/MCP lifecycle does not. The
storage-neutral authorization-v3 policy/evaluator contract
defines canonical repositories, exact tenant aliases, authenticated identity
slots, role bindings, and linked decisions. The authenticated retrieval
service kernel now persists and rechecks those decisions before a retrieval
callback, but transport-authenticated durable Agent wiring remains
outstanding. A loopback-only bearer-authenticated HTTP profile and typed
Python client now expose the active version-2 lifecycle through the same
dispatcher as STDIO MCP. The storage-neutral, content-addressed FixEvidence and
structured regression evidence contracts are published with a strict
cross-record MemoryRevision preflight and opt-in isolated SQLite/PostgreSQL
proposal ledgers. Active v2 records/adapters do not use these ledgers, and
proposal persistence does not approve or activate memory.
The content-addressed retrieval policy and optional storage-neutral preparation
kernel now authorize first, load verified activated revisions, apply
classification/applicability/eval-leakage/Git-ancestry filters, deterministically
fuse versioned adapter scores, and emit paired RetrievalSnapshot/System Gate
evidence with final head/policy rechecks. The opt-in durable Semantic Gate
composition now advances a prepared GateSession through
`AWAITING_DECISION` to `DECIDED`, retaining exact prompt/response bytes and
the complete monotonic attempt chain with explicit retry/recovery semantics.
The opt-in durable finalization composition now rechecks the current
authorization event, active revision heads, and policy; deterministically
renders the final allowed set; atomically retains an exact UsageDecision,
injection, and complete eight-component replay bundle; and CAS-publishes
`FINALIZED` with SQLite/PostgreSQL caller-transaction parity.
`DurableExecutionService` then verifies that exact retained injection before
`FINALIZED -> EXECUTING`, supports authenticated exact-version resume and
abandonment, authenticates the registered outcome evaluator, and composes
atomic `RunOutcome + COMPLETED + completion outbox` publication with
SQLite/PostgreSQL parity. Managed production indexes, encrypted
protected-content finalization, durable transition-event linkage, active
retriever/GateSession persistence, and durable Agent adapter wiring remain
outstanding.
`AuthenticatedDurableAgentMemory` now composes those opt-in stages behind one
adapter-neutral lifecycle. It reconstructs the original retrieval scope from
retained RetrievalSnapshot authorization linkage, rejects mismatched service
graphs, adds authorized exact-version cancellation, and obtains a fresh
transition decision for each post-prepare GateSession mutation. The facade can
continue across instances when the same authorities and current trusted
contexts remain available, but is not yet constructed by the default Agent,
MCP, HTTP, CLI, or SDK adapters.
The opt-in SQLite and isolated PostgreSQL RunOutcome authorities now atomically
complete an executing GateSession with one content-addressed outcome. The
isolated SQLite and PostgreSQL OutcomeAttribution ledgers persist multiple
independently verified claims with exact durable outcome/session linkage. The
opt-in SQLite and isolated PostgreSQL Completion Outbox authorities atomically
add one immutable completion event and an append-only leased delivery chain to
that transaction. The opt-in durable execution composition now supplies
registered evaluator authentication and exact retained-injection replay around
these authorities. Artifact attestation checks and active runtime
emission remain outstanding.
Storage-neutral approval/activation contracts and isolated SQLite/PostgreSQL
publication authorities are published. Opt-in authenticated SQLite and
isolated PostgreSQL Artifact authorities encrypt exact bytes through a
caller-owned provider, authorize every read/write, and enforce read-time
retention/legal hold. Object-storage parity, physical purge/key destruction,
active-v2 projection, and broader service integration remain part of the
coordinated schema-version-3 program. The
storage-neutral `tbm.replay.v3` artifact and replay-manifest contract is
published with opt-in isolated SQLite and PostgreSQL immutable byte/descriptor
ledgers, but no active adapter uses them and they provide no authorization,
retention, encryption, or GateSession authority. The
read-only v3 migration preflight and inert
staging bundles are implemented, but they cannot activate memory or be loaded
as version-3 runtime state.
