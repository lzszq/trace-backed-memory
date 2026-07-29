# SQLite MemoryRevision publication authority v3

**English** | [简体中文](sqlite-memory-publication-v3.zh-CN.md)

`SQLiteMemoryPublicationV3Repository` is an opt-in, side-by-side authority for
immutable `MemoryRevisionApproval` and `MemoryRevisionActivation` events. It
depends on the SQLite MemoryRevision proposal ledger and leaves the active
SQLite version-1 Store, Agent, MCP profile, and snapshot format unchanged.

Approval writes re-verify the exact stored proposal/evidence closure, artifact
bytes, `memory:review` policy/request/decision, actor separation, and a
caller-provided attestation verifier. The canonical policy, request, decision,
event, and verifier identity are stored together and read back before commit.
Exact replay is idempotent; an identity or revision-slot mismatch fails closed.

Activation reads the durable target/memory head inside `BEGIN IMMEDIATE`, locks
out competing writers, replays the complete stored approval provenance,
re-verifies artifact/evidence and `memory:activate`, invokes the attestation
verifier, appends the immutable activation, and advances the head with an exact
sequence/parent compare-and-swap. It never trusts a caller-provided predecessor.
Tenant-scoped heads use an explicit non-null repository key so SQL `NULL`
uniqueness cannot create duplicate heads.

The schema uses immutable approval/activation rows, guarded head transitions,
foreign keys to proposals and approvals, no-delete heads, exact canonical
schema checks, recursive-trigger/foreign-key checks, connection-wide locking,
nested savepoints, rollback-on-failure, and pre-commit read-back. The
approval/activation rows are the append-only publication audit trail and retain
exact authorization provenance.

The attestation callback is a service boundary, not a built-in signature
scheme. It must authenticate the actor and validate the referenced attestation
without adding implicit network access to this repository. SQLite has no
database ACL boundary; an actor with unrestricted direct database writes
remains outside the adapter's trust boundary. No active-v2 projection,
retrieval, retention, encryption, suspension, or obsolescence is performed.
