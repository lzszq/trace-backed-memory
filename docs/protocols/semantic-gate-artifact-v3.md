# Semantic Gate artifact binding v3

[简体中文](semantic-gate-artifact-v3.zh-CN.md)

`tbm.semantic-gate-artifact.v3` binds exact provider prompt or response bytes
to one immutable `SemanticGateAttempt` without treating those bytes as prompt
memory or as a finalized runtime injection.

## Contract

`SemanticGateArtifactBinding` records:

- the exact `SemanticGateAttempt.attempt_id`;
- one role, `prompt` or `response`;
- a generic `ContentAddressedArtifact` descriptor containing the
  content-derived artifact ID, SHA-256, byte length, media type,
  classification, creation time, and optional encryption/redaction metadata.

The descriptor never embeds or logs the bytes. Prompt artifacts are bounded
to 128,000 UTF-8 bytes, matching the 32,000-character Gate prompt boundary at
four bytes per character. Response artifacts retain the 64 KiB provider
response boundary. Empty artifacts are rejected. `confidential` and
`restricted` descriptors require an encryption-key identifier.

`create_semantic_gate_artifact_binding()` hashes exact bytes and requires the
digest to equal the attempt's role-specific digest.
`verify_semantic_gate_artifact_binding()` jointly verifies the attempt ID,
role, expected digest, descriptor size, content-derived ID, and exact bytes.
A failed attempt accepts its prompt but cannot bind a response because the
attempt contract forbids response decision output.

JSON loading is bounded, duplicate-key rejecting, strict about unknown and
missing fields, and canonical serialization contains only the descriptor.
JSON Schema validates structural shape; the Python parser/verifier remains
required for the content-derived artifact-ID relationship and exact bytes.

## Boundary

This is a storage-neutral binding contract. It does not persist artifact
bytes, authenticate a provider, establish trusted server timestamps, verify
encryption at rest, or transactionally append a Semantic Gate ledger row,
GateSession revision, or replay manifest. SQLite/PostgreSQL byte repositories
and the authenticated provider invocation service remain separate follow-up
work. Artifact hashes prove byte identity, not authorship or truth.
