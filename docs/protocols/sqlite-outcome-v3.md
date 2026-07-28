# SQLite RunOutcome Completion v3

**English** | [简体中文](sqlite-outcome-v3.zh-CN.md)

This opt-in authority closes the durable SQLite GateSession lifecycle from
`EXECUTING` to `COMPLETED`. It stores one immutable, content-addressed
`RunOutcome` and the matching GateSession revision in one transaction. It does
not replace the active snapshot-v2 Store or the process-local Agent/MCP
lifecycle.

## Completion transaction

`SQLiteOutcomeV3Repository.complete_session()` accepts a canonical
`GateCompletionRequest`. Session, Trace, run, and usage-decision identities
come from the current durable GateSession rather than caller fields. The
repository:

1. acquires a SQLite writer reservation or caller-owned savepoint;
2. validates both canonical GateSession and RunOutcome schemas;
3. reloads the current session and requires `EXECUTING` plus the exact
   expected version;
4. obtains one server-owned timestamp after the prior revision;
5. builds the content-addressed RunOutcome and `COMPLETED` revision with that
   same timestamp;
6. appends the revision through GateSession CAS, inserts the outcome, and
   reads both records back before commit.

Any validation, SQL, CAS, trigger, or read-back failure rolls back both
records. Repeating the same measurement after completion returns the retained
pair with `inserted=false` without consulting the clock. A different
measurement for the completed session conflicts.

The shared `gate_sessions` setup authority rejects direct transitions to
`COMPLETED`; callers must use `complete_session()` so a public composite
repository cannot retain an outcome-less completed revision. The standalone
GateSession adapter remains a lower-level, independently opt-in authority.

## SQL and service boundary

`schemas/sqlite-v3-outcome.sql` is an independent schema-version-1 resource
that depends on the side-by-side GateSession schema. It enforces one outcome
per session, canonical descriptor reconstruction, sorted unique evidence
digests, completed-session identity linkage, and immutable update/delete
guards. Repository reads reparse the descriptor, recompute the content ID,
compare every stored column, validate the completed session, and reject
missing, extra, or changed managed schema objects.

`GateSessionCompletionService` is the storage-neutral receipt and durable
read-back verifier. It does not authenticate an evaluator, verify evidence
artifact bytes, or infer a result from execution errors. Those values must
come from a trusted service boundary.

## Current boundary

PostgreSQL parity, OutcomeAttribution persistence, authenticated evaluator
derivation, artifact authorization, completion outbox delivery, and active
Agent/MCP/HTTP/SDK integration remain follow-up work. Active snapshot version
2, SQLite schema version 1, PostgreSQL schema version 2, and
`tbm.agent.v1` remain unchanged.
