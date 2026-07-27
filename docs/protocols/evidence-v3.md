# Structured regression evidence v3

**English** | [简体中文](evidence-v3.zh-CN.md)

`tbm.regression-evidence.v3` is a storage-neutral, immutable verification
contract. It supplements the migration-only `RegressionEvidence` record and
does not change snapshot version 2 or make the active adapters evidence-aware.

The record binds a source Failure Case and Trace to a distinct verification
Trace/run, evaluator identity and version, suite and case, expected and
observed outcomes, bounded environment metadata, exact source/fix/verification
commit relationships, artifact hashes, and an attestation hash. The submitter
and verifier must be different principals. The evidence ID is derived from the
canonical record content, so mutation fails closed.

A `pass` result is evidence, not permission to publish. Activation still
requires independent review, authorization, lifecycle policy, and an immutable
MemoryRevision. Models may propose or narrow memory but may not verify or
activate their own output. Hashes establish byte identity only; the owning
service must authenticate principals and verify attestations and commit
relationships.

External JSON is limited to 1 MiB, depth 32, and 10,000 nodes. Parsing rejects
duplicate keys, invalid UTF-8, non-finite numbers, unknown or missing fields,
invalid timestamps, inconsistent commit linkage, self-verification, and
content-hash mismatches.
