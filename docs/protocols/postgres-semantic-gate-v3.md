# PostgreSQL Semantic Gate attempt ledger v3

**English** | [简体中文](postgres-semantic-gate-v3.zh-CN.md)

This opt-in authority provides PostgreSQL parity for the ordered
`SemanticGateAttempt` chain. It lives in the isolated
`trace_backed_memory_v3_semantic_gate` schema, requires active PostgreSQL
schema version 2 and Gate evidence v3, and does not change the active runtime
schema or Agent/MCP lifecycle.

## Append and replay contract

`PostgresSemanticGateV3Repository.store_attempt()` locks active, Gate evidence,
and semantic-ledger metadata in a fixed order. It then reloads the exact
`RetrievalSnapshot` and `SystemGateEvaluation`, locks the snapshot before the
evaluation, verifies both descriptors, and serializes one evaluation's chain
through its head row.

The first writer creates a temporary sequence-zero head inside the same
transaction. A new attempt must use the next sequence and exact current parent;
after insertion, an exact CAS advances the head. A second writer for the same
evaluation waits on that head. Exact canonical replay returns
`inserted=False`; sibling forks, skipped sequences, or conflicting immutable
content fail atomically.

Every read reparses canonical descriptors, compares relational columns,
revalidates Gate evidence, and runs the bounded whole-chain verifier. The
repository never repairs stored data.

## Database enforcement

The install resource is `schemas/postgres-v3-semantic-gate.sql`. The isolated
schema enforces:

- one head per System Gate evaluation;
- unique `(system_gate_evaluation_id, sequence)` and attempt identity;
- sequences from 1 through 100;
- exact session/snapshot/evaluation scope;
- immutable attempts and head identity;
- one-step head advancement; and
- deferred commit-time chain consistency, so an empty head, unadvanced
  attempt, orphan, gap, or branch cannot commit.

All trigger functions use `search_path=pg_catalog`. Operations replace hostile
caller search paths locally, take deterministic table and row locks, verify an
exact security-catalog fingerprint before and after work, and preserve an
existing caller transaction through a psycopg savepoint.

`schemas/postgres-v3-semantic-gate-rollback.sql` locks active, Gate evidence,
and semantic metadata, takes access-exclusive table locks, verifies the exact
relations, functions, triggers, ACL/security catalog, and canonical
fingerprint, and then uses `RESTRICT`. Catalog drift or an external dependency
aborts the complete rollback.

## Current boundary

The ledger stores attempt provenance and artifact hashes, not prompt/response
artifact bytes. It does not authenticate providers or timestamps, append a
GateSession revision, or emit attempts from the active Store, Agent, or MCP
path. Artifact validation, retention, authorization, and transactionally
integrated GateSession/replay service operations remain outstanding.

PostgreSQL schema owners and superusers remain inside the database trust
boundary; runtime roles must not own, alter, disable triggers on, or broaden
privileges for this schema. External signed audit/checkpoint evidence is still
required to detect a trusted administrator rewriting a complete internally
valid history.
