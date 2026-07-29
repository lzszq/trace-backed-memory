# MemoryRevision proposal and publication events v3

**English** | [简体中文](memory-revision-v3.zh-CN.md)

`tbm.memory-revision.v3` is a storage-neutral, immutable proposal contract.
The separate `tbm.memory-revision-approval.v3` and
`tbm.memory-revision-activation.v3` contracts record approval and activation
events without adding mutable lifecycle fields to the proposal. The active
snapshot/SQL adapters do not persist or emit any of these version-3 records.

Each revision has a content-derived ID and binds a stable memory ID and kind,
revision number and exact parent, content-addressed artifact, canonical
authorization scope, confidence and sensitivity metadata, source
FailureCase/Fix references, structured regression-evidence IDs, and a
server-owned proposer/client/attestation context. Lesson proposals require all
case, fix, and regression references. Project-policy proposals forbid those
case-bound references and require `policy` memory type.

`verify_memory_revision_evidence_bundle` resolves the exact
[FixEvidence](fix-evidence-v3.md) and every lesson regression-evidence ID. It
requires the same Failure Case, source Trace, source commit, and fix commit,
passing regression evidence, and a proposer independent from all evidence
actors. The older `verify_memory_revision_evidence` helper checks only
regression evidence and is not sufficient for publication.

`approve_memory_revision` re-verifies the exact revision lineage, content
bytes, FixEvidence/regression bundle, actor separation, and an exact allowed
`memory:review` decision for the revision's tenant or repository at the
approval timestamp. `activate_memory_revision` replays that complete approval
verification rather than trusting an isolated approval hash, then independently
checks `memory:activate`, a third actor, exact immediate predecessor linkage,
and monotonic sequence. The builder validates the supplied predecessor's
content-derived shape and linkage; it cannot prove that an untrusted standalone
event is the durable current head. Publication forbids global scope; global
policies require a separate PolicyBundle lifecycle. Scope relocation also
requires a separate workflow rather than changing targets inside a revision
chain.

Approval and activation IDs are canonical content identities. Evidence and
attestation hashes are linkage values, not signatures, authentication, or
authorization. The caller-owned service must authenticate actors and validate
attestations before invoking these builders. Models may propose revisions but
may not approve or activate their own output.

The contracts intentionally store content metadata rather than plaintext.
Their canonical Schemas and examples are
`schemas/memory_revision_approval_v3.schema.json`,
`schemas/memory_revision_activation_v3.schema.json`,
`examples/memory_revision_approval_v3.example.json`, and
`examples/memory_revision_activation_v3.example.json`. The Python contract
enforces equality between activation sequence and revision number; this
cross-field invariant is stronger than the standalone JSON Schema.

The opt-in [SQLite](sqlite-memory-publication-v3.md) and
[PostgreSQL](postgres-memory-publication-v3.md) publication authorities now
persist approval and activation events with exact authorization provenance and
attestation-verifier identity. Each transition is transactional, append-only,
idempotent, and read back before commit. Activation locks and verifies the
durable current head instead of trusting caller-supplied predecessor fields.
The older proposal ledgers remain proposal-only dependencies.

These authorities do not store artifact plaintext, provide encryption or
retention, or project activations into active version 2. Callers must supply
exact artifact bytes/evidence and a trusted attestation verifier. Active-v2
integration still requires an explicit migration.
