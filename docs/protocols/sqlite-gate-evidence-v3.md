# SQLite and PostgreSQL Gate Evidence v3

This opt-in, side-by-side authority durably stores the exact
`RetrievalSnapshot` and `SystemGateEvaluation` pair used to prepare one
GateSession. It does not replace the active snapshot-v2 Store and is not yet
wired into `LocalAgentMemory` or `tbm-mcp`.

## Write contract

`SQLiteGateEvidenceV3Repository.store_bundle()` accepts exact v3 record
objects, verifies the complete System Gate-to-retrieval relationship, and
stores both canonical JSON descriptors in one SQLite transaction. Replaying
the same pair is idempotent. A snapshot may have exactly one System Gate
evaluation; conflicting immutable content fails closed.

The canonical schema enables foreign keys and recursive triggers. Immutable
update/delete triggers therefore also reject `INSERT OR REPLACE` replacement
deletes. Repository operations validate schema metadata and every named schema
definition before reading or writing.

`PostgresGateEvidenceV3Repository` provides the same exact-pair contract in
the isolated `trace_backed_memory_v3_gate_evidence` schema. Installation is
gated on active PostgreSQL schema version 2. Every operation locks both schema
metadata rows, rejects catalog, ACL, RLS, policy, rule, relation-kind,
function, or trigger drift, and reloads the canonical descriptor after
`ON CONFLICT DO NOTHING`. Concurrent exact writes are idempotent; a different
evaluation for the same snapshot conflicts atomically. The companion rollback
locks the authority, validates its security catalog, and uses `RESTRICT`.

## PREPARED bridge

`DurablePreparedGateEvidenceVerifier` is the standard storage-neutral verifier
for `AuthenticatedGateSessionService`. It reloads both IDs from the authority,
revalidates their content hashes and ordered candidate coverage, then requires
exact agreement on:

- GateSession, Trace, and run IDs;
- authorization event ID;
- tenant, repository, principal, and agent-client scope.

Only after this check may the service CAS-publish `PREPARED`. The evidence
write and GateSession transition remain ordered operations across separate
authorities, not an atomic cross-database transaction; service compensation
and recovery semantics still apply.

## Current boundary

This release provides SQLite and PostgreSQL authorities. An active retriever
that emits these records and Agent/MCP/HTTP/SDK integration remain future
work. PostgreSQL owner and superuser roles remain inside the database
administration trust boundary; runtime roles must not own or alter the schema.
