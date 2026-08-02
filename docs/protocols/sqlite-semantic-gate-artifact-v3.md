# SQLite Semantic Gate artifact repository v3

[简体中文](sqlite-semantic-gate-artifact-v3.zh-CN.md)

`SQLiteSemanticGateArtifactV3Repository` is the opt-in local durable store for
`tbm.semantic-gate-artifact.v3`. It composes the Gate evidence and Semantic
Gate attempt authorities on one connection without changing any existing
schema version.

## Installation and schema

`connect(initialize=True)` installs, in order:

1. `schemas/sqlite-v3-gate-evidence.sql`;
2. `schemas/sqlite-v3-semantic-gate.sql`;
3. `schemas/sqlite-v3-semantic-gate-artifacts.sql`.

The artifact resource has its own version-1 metadata singleton and requires
the existing version-1 Semantic Gate ledger. It adds immutable artifact-byte
and `(attempt_id, artifact_role)` binding tables. The repository compares all
managed tables, indexes, and triggers with the packaged canonical definitions
and rejects unexpected indexes or triggers attached to those tables. It also
rejects temporary objects that shadow an artifact or parent-evidence table, or
attach to one of those managed tables.

## Atomic operations

`store_attempt_with_artifacts()` requires:

- one exact prompt binding and bytes;
- one exact response binding and bytes for a succeeded attempt;
- no response for a failed attempt.

By default, one `BEGIN IMMEDIATE` transaction, or one nested savepoint, appends
the SemanticGateAttempt, stores deduplicated bytes, stores role bindings,
reloads the full attempt chain, and verifies exact artifact read-back. The
explicit durable runtime enables the event-first mode documented by
[Semantic Gate Attempt Event v1](semantic-gate-attempt-event-v1.md): the same
transaction first verifies the retained System Gate parent and appends the
canonical attempt event, then writes the attempt and Artifact projections.
Any conflict or read-back failure rolls back the whole unit, including event
heads/idempotency and a newly appended attempt. Exact retries return per-row
insertion flags without duplicating data or allocating another global
position. `load_attempt_with_artifacts()` revalidates the attempt chain,
descriptor columns, role digest, content-derived ID, size, and exact bytes.

The repository registers deterministic `tbm_sha256(BLOB)` on its connection.
Database triggers recompute the digest and derived artifact ID, parse and
compare every binding descriptor field, enforce prompt media/size and response
size/status, and block update, delete, and replacement writes. Dedicated
insert-conflict guards remain effective if `recursive_triggers` is disabled;
normal repository operations require both foreign keys and recursive triggers.

## Security boundary

This repository stores only `public` and `internal` bytes because it does not
provide encryption at rest. `confidential` and `restricted` bindings remain
valid storage-neutral contracts but are rejected here even when they name an
encryption key. Artifact descriptor JSON never embeds the bytes.

The repository does not authenticate providers, establish trusted timestamps,
apply retention/access-control policy, or append GateSession/replay records.
Only trusted durable composition may bind event identity context and enable
event-first mode; external request JSON cannot do so. Compatibility Agent/MCP
profiles remain unchanged. PostgreSQL parity and provider authentication are
separate boundaries. SQLite
file owners and administrators
with DDL authority remain trusted; external signed checkpoints are required
to detect a complete offline rewrite.
