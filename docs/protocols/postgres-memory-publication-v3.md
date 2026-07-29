# PostgreSQL MemoryRevision publication authority v3

**English** | [简体中文](postgres-memory-publication-v3.zh-CN.md)

`PostgresMemoryPublicationV3Repository` is the isolated PostgreSQL peer of the
SQLite publication authority. Installation requires active PostgreSQL schema
version 2 and the isolated MemoryRevision proposal schema version 1. It creates
only `trace_backed_memory_v3_memory_publication`; active tables and schema
versions remain unchanged.

Runtime lock order is active metadata, MemoryRevision proposal metadata/tables,
then publication metadata/tables. Every operation pins `search_path` to
`pg_catalog`, verifies the exact proposal and publication catalog
fingerprints, and restores the caller search path. Writes use caller-compatible
transactions/savepoints. Approval stores exact event and authorization
provenance after proposal/evidence/artifact/attestation verification.
Activation row-locks the durable head, re-verifies the complete approval and
current activation authorization, appends one immutable event, advances the
head with compare-and-swap, and performs exact read-back before commit.

Install SQL revokes public schema, table-mutation, and function-execution
privileges. Fixed-search-path trigger functions reject mutation, truncation,
invalid proposal/approval linkage, non-current predecessors, and invalid head
advances. The rollback script locks dependencies in the same order, checks the
expected relations/functions/triggers and security shape, and uses
`DROP ... RESTRICT`; external dependents therefore fail closed.

The attestation verifier remains a caller-owned authentication boundary. The
authority does not project version-3 activations into active version 2 and does
not provide retrieval, retention, encryption, suspension, or obsolescence.
