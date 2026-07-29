# Verified ActivatedRevision source v3

**English** | [简体中文](activated-revision-source-v3.zh-CN.md)

`ActivatedRevisionSource` is a storage-neutral, read-only bridge from durable
MemoryRevision publication to future v3 retrieval. It does not project records
into the active version-2 Store.

## Read order

For one authenticated `memory:retrieve` request, the source:

1. resolves the target-scoped current publication head;
2. loads the exact activation, approval, authorization policy/request/decision,
   and append-time attestation-verifier identities;
3. loads the immutable proposal, structured evidence, and immediate lineage;
4. requires configured trust in both approval and activation verifier IDs;
5. performs a separately authorized `artifact:read`, decrypts the content, and
   verifies its plaintext digest and size;
6. re-runs the complete approval/activation/evidence/authorization verifier;
7. reloads the head and rejects a concurrent change; and
8. returns an `ActivatedRevisionCandidate` with a canonical candidate digest
   plus both access-authorization event IDs.

`load_approval_bundle` and `load_activation_bundle` are available on both the
SQLite and PostgreSQL publication authorities. Their storage-neutral bundle
records revalidate the exact persisted authorization decision at read time.
Attestation signatures are not stored: the source trusts only explicitly
configured append-time verifier identities and does not claim to reauthenticate
the original signature bytes.

The candidate digest covers the immutable revision, approval, activation, and
attestation-verifier identities. Per-read authorization IDs are audit evidence
and are intentionally excluded from candidate identity.

## Boundary

This source does not execute applicability selectors, classification/leakage
filters, Git ancestry, ranking, RetrievalSnapshot emission, System/Semantic
Gate, rendering, or injection. It does not make a proposal active and never
derives revision IDs from version-2 Lesson/Policy/usage records. The current
encrypted Artifact Authority is SQLite-local; PostgreSQL/object-storage
artifact parity and active Agent/MCP integration remain outstanding.
