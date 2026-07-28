# FixEvidence v3

**English** | [简体中文](fix-evidence-v3.zh-CN.md)

`tbm.fix-evidence.v3` is a storage-neutral, content-addressed record that binds
a reviewed Failure Case to one exact source Trace, the source and fix commits,
a verified source-to-fix ancestry relation, bounded artifact hashes, and
independent submitter/reviewer identities.

The submitter and reviewer must differ. Commit ancestry must be verified before
submission, and review cannot precede submission. The evidence ID covers the
entire canonical record, so any change requires a new ID. Artifact and
attestation hashes are content identities, not signatures, authorization, or
proof that referenced bytes are available.

`verify_memory_revision_evidence_bundle` resolves the exact FixEvidence and
StructuredRegressionEvidence records for a lesson proposal. It requires the
same case, source Trace, source commit, and fix commit across those records and
keeps the revision proposer independent from every evidence submitter,
reviewer, and verifier.

This contract does not approve or activate memory. Owning services must still
authenticate actors, verify artifacts and attestations, authorize reads and
publication, enforce retention, and persist proposal/approval/activation as
separate append-only operations.
