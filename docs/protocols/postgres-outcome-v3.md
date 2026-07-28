# PostgreSQL RunOutcome Completion v3

**English** | [简体中文](postgres-outcome-v3.zh-CN.md)

This opt-in authority closes an isolated PostgreSQL GateSession from
`EXECUTING` to `COMPLETED`. It stores one immutable, content-addressed
`RunOutcome` and the matching GateSession revision in one database transaction.
It does not change active PostgreSQL schema version 2 or wire the active
Agent/MCP lifecycle to durable v3 completion.

## Install and rollback

Install `schemas/postgres-v3-gate-session.sql` before
`schemas/postgres-v3-outcome.sql`. The outcome installer locks and verifies the
active-v2 and GateSession-v1 metadata, creates the isolated
`trace_backed_memory_v3_outcome` schema, and leaves the active schema untouched.

Rollback uses `schemas/postgres-v3-outcome-rollback.sql` before the GateSession
rollback. It verifies the exact metadata, relation, function, trigger,
constraint, and column catalog before dropping anything. Unexpected objects,
schema drift, dependency drift, or an active schema other than version 2 abort
the rollback without partial cleanup.

## Completion transaction

`PostgresOutcomeV3Repository.complete_session()` accepts a canonical
`GateCompletionRequest`. It derives session, Trace, run, and usage-decision
identities from the locked durable GateSession. The operation:

1. opens a transaction, or a savepoint when the caller already owns one;
2. locks and validates active, GateSession, and RunOutcome metadata and
   catalogs;
3. locks the current GateSession head with `FOR UPDATE`;
4. returns an exact completed replay without sampling the clock, or requires
   `EXECUTING` and the exact expected version;
5. samples PostgreSQL database time after the head lock;
6. builds and verifies the RunOutcome and `COMPLETED` revision with that same
   timestamp;
7. appends the revision with CAS, inserts the immutable outcome, and exactly
   reads both records back before commit.

Any contract, SQL, trigger, catalog, CAS, or read-back failure rolls back both
records. Concurrent exact completion serializes on the GateSession head and
retains one outcome. A different measurement for an already completed session
conflicts.

The shared `gate_sessions` setup authority rejects direct transitions to
`COMPLETED`; callers must use `complete_session()`. The standalone GateSession
adapter remains a lower-level, independently opt-in authority.

## Storage and trust boundary

The outcome schema enforces one outcome per session, bounded identifiers and
descriptors, sorted unique evidence digests, result/error and output shape,
completed-session identity linkage, and immutable update/delete/truncate
guards. Its insert trigger reconstructs the exact canonical descriptor,
recomputes the payload SHA-256 and derived outcome ID, and rejects
non-canonical JSON before persistence. Repository reads independently reparse
the descriptor, recompute its content ID, compare every stored column, verify
the completed session, and reject managed-catalog drift.

`GateSessionCompletionService` remains the storage-neutral receipt and durable
read-back verifier. Neither it nor this repository authenticates the evaluator,
authorizes evidence artifact bytes, derives a result from callbacks, or emits
an outbox event.

## Current boundary

OutcomeAttribution persistence, authenticated evaluator derivation, artifact
authorization, completion outbox delivery, and active Agent/MCP/HTTP/SDK
integration remain follow-up work. Snapshot version 2, SQLite schema version 1,
PostgreSQL schema version 2, and `tbm.agent.v1` remain unchanged.
