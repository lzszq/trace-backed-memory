# Authenticated encrypted Artifact Authority v3

**English** | [简体中文](artifact-authority-v3.zh-CN.md)

This opt-in deployment boundary stores the exact bytes referenced by a
`ContentAddressedArtifact` without changing active snapshot version 2, SQLite
schema version 1, or PostgreSQL schema version 2.

## Contract

`AuthenticatedArtifactService` obtains and durably reads back a fresh
`artifact:write` or `artifact:read` authorization decision before touching the
artifact repository. Authorization uses the existing v3 request shape; no new
field is inserted into its content-derived identity.

The plaintext SHA-256 remains the artifact identity. A caller-supplied
authenticated-encryption/KMS provider returns ciphertext and a nonce. The
service binds descriptor, tenant, repository, environment, write authorization,
provider, algorithm, key, retention, and trusted storage time into AAD. It then
decrypts and verifies the exact plaintext before an immutable authority append.
Reads repeat authorization, scope checks, decryption, and plaintext digest/size
verification. Provider and persistence failures are exposed only through stable,
sanitized service errors.

`schemas/sqlite-v3-artifact-authority.sql` provides an isolated local version-1
schema. `schemas/postgres-v3-artifact-authority.sql` and
`PostgresArtifactV3Repository` provide an isolated PostgreSQL version-1 peer
gated on active PostgreSQL schema version 2. Both store ciphertext only, reject
update/delete/truncate, make exact replay idempotent, reject conflicting
content-derived identities, preserve caller transactions through savepoints,
and fail closed on schema/catalog drift.

The PostgreSQL peer fixes `search_path` during protected work, locks and
fingerprints the complete managed catalog, verifies the ciphertext SHA-256 in
the database, supports concurrent exact replay, and performs exact read-back
before success. Its `RESTRICT` rollback checks the same fixed catalog and
refuses unexpected managed objects or external dependencies.

## Retention boundary

`retain_until` denies reads after the trusted timestamp unless `legal_hold` is
true. The immutable authorities themselves do not perform physical purge,
redaction, key destruction, or legal-hold release. The opt-in
[Artifact retention protocol v1](artifact-retention-v1.md) composes a separate
trusted legal-hold/KMS boundary around those immutable rows: it records a
protected manifest, advances the current managed-index head, verifies external
key-destruction receipts, and appends an `erased` tombstone overlay. It never
deletes or mutates the ciphertext authority and is not selected by default
runtime profiles. Object-storage parity, product KMS configuration,
legal-hold release, active MemoryRevision writes, GateSession linkage, and
runtime injection remain outstanding.
