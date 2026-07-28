# PostgreSQL MemoryRevision v3 proposal ledger

[简体中文](postgres-memory-revision-v3.zh-CN.md)

The opt-in `PostgresMemoryRevisionV3Repository` stores immutable
`MemoryRevision` proposals with their exact `FixEvidence` and ordered
`StructuredRegressionEvidence` closure. It is the PostgreSQL peer of the
isolated SQLite proposal ledger and does not change the active PostgreSQL
schema version 2.

Install and rollback resources:

- `schemas/postgres-v3-memory-revision.sql`
- `schemas/postgres-v3-memory-revision-rollback.sql`

The installer locks active schema metadata before creating the isolated
`trace_backed_memory_v3_memory_revision` schema. The repository validates a
catalog fingerprint before and after each successful operation, pins
`search_path` to `pg_catalog`, locks active metadata, ledger metadata, and
ledger tables in that order, and performs exact idempotent writes in
caller-compatible transactions/savepoints. Read operations use `ACCESS SHARE`;
writes use `ROW EXCLUSIVE` and therefore require a write-capable repository
role. Every read validates the complete parent lineage up to a bound of 10,000
revisions, plus the exact evidence closure. Descriptor, evidence-link, trigger,
ACL, or catalog drift fails closed, and replay never repairs tampered evidence.

The schema owner and PostgreSQL superusers remain trusted operators: they can
alter functions or bypass database protections, including transiently during
an operation. Do not run repository traffic concurrently with privileged DDL.

The ledger is proposal-only. It does not review, verify, approve, activate,
authorize, retain, obsolete, or inject memory. Those transitions remain under
the normal lifecycle and Gate contracts.

Rollback locks all ledger relations and fails closed on metadata, relation,
function, trigger, ACL, or catalog mismatch. It removes only the isolated
schema and leaves active PostgreSQL version 2 unchanged.

See also: [SQLite proposal ledger](sqlite-memory-revision-v3.md).
