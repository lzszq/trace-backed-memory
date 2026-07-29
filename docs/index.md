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
- [Replayable RetrievalSnapshot v3](protocols/retrieval-snapshot-v3.md)
- [System and Semantic Gate evaluation v3](protocols/gate-evaluation-v3.md)
- [Semantic Gate artifact binding v3](protocols/semantic-gate-artifact-v3.md)
- [Authenticated Semantic Gate service v3](protocols/semantic-gate-service-v3.md)
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
- [Content-addressed replay contract v3](protocols/replay-v3.md)
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
published, but the active Store/MCP, workers, and service integration do not
use them yet. The storage-neutral authorization-v3 policy/evaluator contract
defines canonical repositories, exact tenant aliases, authenticated identity
slots, role bindings, and linked decisions. The authenticated retrieval
service kernel now persists and rechecks those decisions before a retrieval
callback, but transport authentication and active Agent/MCP/HTTP/SDK wiring
remain outstanding. The storage-neutral, content-addressed FixEvidence and
structured regression evidence contracts are published with a strict
cross-record MemoryRevision preflight and opt-in isolated SQLite/PostgreSQL
proposal ledgers. Active v2 records/adapters do not use these ledgers, and
proposal persistence does not approve or activate memory.
The content-addressed RetrievalSnapshot contract records exact authorized
ranking inputs/results, index versions, scores, hashes, and truncation reasons,
while immutable System/Semantic Gate records bind deterministic policy and
model-attempt provenance under a monotonic narrowing rule. No active retriever,
gate, or GateSession repository emits them yet.
The opt-in SQLite and isolated PostgreSQL RunOutcome authorities now atomically
complete an executing GateSession with one content-addressed outcome. The
isolated SQLite and PostgreSQL OutcomeAttribution ledgers persist multiple
independently verified claims with exact durable outcome/session linkage. The
opt-in SQLite and isolated PostgreSQL Completion Outbox authorities atomically
add one immutable completion event and an append-only leased delivery chain to
that transaction. Authenticated evaluator/artifact checks and active runtime
emission remain outstanding.
Storage-neutral approval/activation contracts and isolated SQLite/PostgreSQL
publication authorities are published. An opt-in authenticated SQLite Artifact
Authority now encrypts exact bytes through a caller-owned provider, authorizes
every read/write, and enforces read-time retention/legal hold. PostgreSQL/object
storage parity, physical purge/key destruction, active-v2 projection, and broader service integration remain part of the
coordinated schema-version-3 program. The
storage-neutral `tbm.replay.v3` artifact and replay-manifest contract is
published with an opt-in isolated SQLite immutable byte/descriptor ledger, but
no active adapter uses it and it provides no authorization, retention,
encryption, or GateSession authority. The
read-only v3 migration preflight and inert
staging bundles are implemented, but they cannot activate memory or be loaded
as version-3 runtime state.
