# PostgreSQL Semantic Gate artifact repository v3

## Boundary

`PostgresSemanticGateArtifactV3Repository` is an opt-in, isolated PostgreSQL
repository for exact Semantic Gate provider bytes. It composes the existing
PostgreSQL SemanticGateAttempt ledger with public/internal prompt and response
artifacts and their `tbm.semantic-gate-artifact.v3` role bindings. It does not
change active PostgreSQL schema version 2. Explicit durable PostgreSQL
composition now enables its event-first mode; compatibility Agent/MCP profiles
remain unchanged.

The repository does not provide encryption at rest. It therefore rejects
`confidential` and `restricted` bytes and rejects non-null encryption-key
claims. Artifact role means prompt or response linkage; it is not principal,
tenant, or provider authorization.

## Install and rollback

Install, in order:

1. `schemas/postgres-v3-gate-evidence.sql`;
2. `schemas/postgres-v3-semantic-gate.sql`;
3. `schemas/postgres-v3-semantic-gate-artifacts.sql`.

The artifact install requires active schema version 2 and Semantic Gate schema
version 1. It creates only
`trace_backed_memory_v3_semantic_gate_artifacts`.

`schemas/postgres-v3-semantic-gate-artifacts-rollback.sql` locks active,
Semantic Gate, and artifact metadata in that order, then locks the artifact
tables. It validates the complete schema/relation/column/constraint/function,
ACL, function-body, and trigger fingerprint before using `RESTRICT`. Catalog
drift or an external dependency aborts the transaction and leaves the schema
installed.

## Atomic store and exact replay

`store_attempt_with_artifacts()` opens one outer PostgreSQL transaction. In
event-first mode it first locks the event-ledger schema/global head, verifies
the retained System Gate or attempt parent, and appends the canonical attempt
event. The attempt append then runs in a nested savepoint, followed by Artifact
and binding projections. An Artifact conflict therefore rolls back event
heads/idempotency plus a newly appended attempt. Caller-owned transactions
remain caller-owned. See
[Semantic Gate Attempt Event v1](semantic-gate-attempt-event-v1.md).

Artifacts are deduplicated by both derived artifact ID and SHA-256. Bindings
are deduplicated by attempt and role. Exact replay returns insertion flags;
different content under an existing identity is a conflict. Every store and
load reparses the canonical descriptor, compares all relational columns,
rehashes the exact bytes, and verifies the attempt/role digest.

Database triggers independently:

- recompute SHA-256 from `bytea` and verify the derived artifact ID;
- enforce prompt media/size and response status/size rules;
- compare every descriptor field with the artifact and binding columns;
- block update, delete, and truncate for metadata, artifacts, and bindings.

## Catalog and trust boundary

Operations replace the transaction-local `search_path` with `pg_catalog`,
lock the isolated schema, and verify the complete security-catalog fingerprint
before and after work. Disabled or changed triggers, functions, owners, ACLs,
policies, rules, columns, or constraints fail closed.

The schema owner and PostgreSQL superusers remain trusted operators: they can
rewrite database state outside the repository boundary. Hashes establish byte
identity, not provider authorship or trusted time. Provider authentication,
trusted timestamps, signed external checkpoints, GateSession/replay
transaction linkage, and encrypted sensitive storage remain separate work.
Trusted adapters alone bind event identity and mutation authorization context;
request JSON cannot select it.
