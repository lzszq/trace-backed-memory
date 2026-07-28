# SQLite MemoryRevision proposal ledger v3

**English** | [简体中文](sqlite-memory-revision-v3.zh-CN.md)

`SQLiteMemoryRevisionV3Repository` is an opt-in, side-by-side authority for
immutable MemoryRevision proposals and their exact FixEvidence and
StructuredRegressionEvidence bundle. It does not change active SQLite schema
version 1 and is not used by the active Store, Agent, or MCP runtime.

`store_proposal()` verifies the complete evidence bundle before I/O, stores all
records and ordered links atomically, enforces one linear parent/revision
sequence per stable memory ID, and reads the bundle back before commit. Exact
replay is idempotent; conflicting IDs, sequence slots, parents, descriptors, or
links fail closed. Update and delete triggers make every stored record
forward-only. Caller-owned transactions use a savepoint and remain caller
controlled.

The ledger is proposal-only. It does not authenticate actors, authorize
publication, validate artifact bytes or signatures, approve or activate a
revision, project it into active v2 memory, or provide retention/encryption.
Those operations require separate authenticated service and audit events.
