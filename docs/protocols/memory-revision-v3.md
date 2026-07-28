# Immutable MemoryRevision v3

**English** | [简体中文](memory-revision-v3.zh-CN.md)

`tbm.memory-revision.v3` is a storage-neutral, immutable proposal contract. It
does not approve, activate, suspend, supersede, or obsolete memory, and active
snapshot/SQL adapters do not persist it.

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
regression evidence and is not sufficient for publication. This is only a
proposal preflight:
evidence hashes and attestation hashes are content identities, not signatures
or authorization. Approval and activation require separate authenticated
service operations, current authorization decisions, and append-only audit
events. Models may propose revisions but may not verify or activate them.

The contract intentionally stores content metadata rather than plaintext.
Owning services must verify artifact bytes, encryption, access control,
retention, evidence existence, parent continuity, and monotonic revision
numbers transactionally before publication.
