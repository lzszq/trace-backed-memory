# FailureCase events and structured-evidence reducer v1

**English** | [简体中文](failure-case-events-v1.zh-CN.md)

`tbm.failure-case-event.v1` is the storage-neutral, opt-in F4-01/F4-02
protocol that turns ordered Trace evidence into reviewable FailureCase
projections. It does not replace the compatibility `FailureCase` model or
change the default Agent, MCP, HTTP, or SDK profiles.

## Trust boundary

An extractor receives a complete, verified TraceEvent stream and emits a
content-addressed proposal. The proposal binds the exact Trace event hashes,
Trace/run identity, source Artifact IDs, protected proposal Artifact
descriptors, extractor version, and configuration digest. Its status is
always `candidate`; neither the extractor nor its output can review or verify
the case.

The event stream is keyed by `case_id` and accepts five versioned facts:

- `tbm.failure_case.extractor_proposed`;
- `tbm.failure_case.reviewed`;
- `tbm.failure_case.fix_evidence_recorded`;
- `tbm.failure_case.regression_evidence_recorded`;
- `tbm.failure_case.legacy_imported`.

Native review must be independent from the extractor. Verification requires
an accepted review, exact `FixEvidence`, and a passing
`StructuredRegressionEvidence` record whose case, source Trace, source
commit, and fix commit match. The regression verifier must be independent
from extraction and case review. Failed or errored regression attempts remain
unverified and may be followed by a later passing attempt.

## Legacy boundary

A legacy `regression_passed=true` value is imported only as
`evidence_quality=legacy_unstructured`. It is never promoted to
`structured_verified`, and the resulting projection always has
`eligible_for_new_memory=false`. New Memory production may therefore consume
only a projection backed by the structured path above.

## Deterministic projection

The sealed registry validates exact payload fields and rejects unknown
type/version pairs. The pure reducer checks stream identity, parent hashes,
contiguous versions, monotonic timestamps, transition ordering, actor
separation, and evidence linkage. It is executed by the shared deterministic
reducer kernel, and emits a content-addressed projection with its last event
hash and global position.

Raw Trace, proposed symptom/root-cause text, and protected evidence bytes do
not enter ledger payloads. Events retain bounded IDs, digests, codes, and
Artifact descriptors only.

## Current boundary

Security acceptance remains open because draft replacement can preserve the
internal producer capability while changing evidence payloads. This protocol
must not supply new-Memory eligibility or later MemoryCatalog input until that
gap is closed. F4-03 through F4-07, default runtime cutover, physical
Artifact storage, and legacy database migration remain separate work.
