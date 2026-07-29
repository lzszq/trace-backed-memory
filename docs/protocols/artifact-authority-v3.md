# Authenticated encrypted Artifact Authority v3

**English** | [简体中文](artifact-authority-v3.zh-CN.md)

This opt-in local deployment boundary stores the exact bytes referenced by a
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
decrypts and verifies the exact plaintext before an immutable SQLite append.
Reads repeat authorization, scope checks, decryption, and plaintext digest/size
verification. Provider and persistence failures are exposed only through stable,
sanitized service errors.

`schemas/sqlite-v3-artifact-authority.sql` is an isolated version-1 schema. It
stores no plaintext and rejects update/delete. Exact replay is idempotent;
conflicting content-derived identities and schema drift fail closed. The
repository preserves a caller transaction through a savepoint.

## Retention boundary

`retain_until` denies reads after the trusted timestamp unless `legal_hold` is
true. The immutable local ledger does not yet perform physical purge,
redaction, key destruction, or legal-hold release. Operators must retain the
external key lifecycle and storage policy. PostgreSQL/object-storage parity,
KMS/provider authentication, signed attestation, active MemoryRevision use,
GateSession linkage, and runtime injection remain outstanding.
