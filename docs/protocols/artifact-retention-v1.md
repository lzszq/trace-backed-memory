# Governed Artifact retention and crypto-erasure v1

**English** | [简体中文](artifact-retention-v1.zh-CN.md)

## Status and scope

`artifact_retention_event_v1.py` is an opt-in, storage-neutral coordinator for
F3 retention evidence. It does not change the compatibility Agent, durable
HTTP/MCP/SDK profiles, or the immutable Artifact repositories. A trusted
adapter must supply an access-bound event ledger, managed-index repository,
protected manifest store, target resolver, legal-hold guard, KMS adapter,
receipt verifier, and trusted clock.

The contract covers five outcomes: a protected redaction manifest,
cryptographic key destruction, a tombstone event, immutable managed-index
purge by successor-head CAS, and a replay-partial marker. Physical row deletion,
object-store lifecycle, legal-hold release, and default-runtime cutover are out
of scope.

## Plan and protected manifest

`RetentionRequest` fixes organization, tenant, repository, environment,
authorization decision, exact Artifact IDs, deletion policy, reason, and
idempotency digest. The resolver returns the exact encrypted Artifact
descriptors, affected MemoryRevision IDs, complete-replay impacts, and the
complete key-reference closure. The policy guard returns a content-addressed
snapshot containing retention expiry, legal-hold state, and hold epochs.

`RedactionManifest` binds those inputs, the expected and successor managed-index
bundle IDs, deterministic replay markers, and planning time. Its operation ID
is stable across replanning time and derives from the request identity, scope,
targets, policy/reason, resolution digest, and policy-state digest. Exact
canonical manifest bytes are stored as a confidential or restricted encrypted
Artifact under a governance key that is not being destroyed. Ledger events
retain only its descriptor and hashes.

## Ordered lifecycle

The sealed event registry contains these ordered facts:

1. `retention_applied`, `redaction_manifest_recorded`, and
   `crypto_erasure_requested` record intent before external effects.
2. `index_purged` records the immutable successor managed-index head selected
   with scope-local compare-and-swap.
3. `crypto_erasure_authorized` binds the exact trusted KMS registration,
   attestation, provider request identities, request digests, and legal-hold
   authorization digest before the destructive call.
4. `crypto_erasure_blocked`, `crypto_erasure_rejected`, or
   `crypto_erasure_unknown` preserve fail-closed outcomes.
5. `replay_partial_marked`, `cryptographically_erased`, and `tombstoned` are
   appended together only after every key has an independently verified exact
   receipt.

The reducer verifies the producer/version, authorization, deletion policy,
manifest descriptor, exact pre/post-erasure target descriptors, receipt
descriptors and digests, provider bindings, index heads, replay-marker closure,
parent hashes, and transition order. Terminal states are immutable.

## Crash and external-effect rules

Managed-index publication and KMS destruction cannot join the ledger
transaction, so the coordinator uses an event-first saga. If the successor
index head is published but its outcome event is not, recovery recognizes the
content-addressed successor and records the missing fact before continuing. If
KMS execution completes or becomes ambiguous before the outcome batch is
durable, the caller receives `TBM_RETENTION_RECOVERY_REQUIRED`.

`destroy()` is the only provider method allowed to initiate key destruction.
After `crypto_erasure_authorized` exists, recovery never calls it again.
`reconcile()` is a non-mutating status query for the exact provider request; it
must never initiate, retry, or recreate destruction. Unknown and partial
provider outcomes therefore remain recoverable without blind retry.

The legal-hold guard's `authorize_destruction()` operation is an atomic
hold-epoch compare-and-swap/lease boundary: a hold must fail or wait while a
valid destruction authorization is active. The coordinator rechecks policy and
the current index head immediately before authorization. A hold observed after
an independently confirmed irreversible provider action is recorded as late
evidence; it cannot be presented as rollback.

## Purge, tombstone, and replay meaning

Managed-index purge builds a new content-addressed bundle without the named
MemoryRevision candidates and orphaned evidence edges, then advances only the
current scope head. Old bundles, Artifact ciphertext rows, and replay rows
remain immutable history. Direct loading of an old bundle is never
authorization; authenticated retrieval must use the current head and current
Artifact-read decision.

Crypto-erasure destroys only keys whose reference closure contains exactly the
target Artifacts. Manifest and receipt governance keys must be disjoint. A
tombstone changes the event descriptor availability to `erased` and records
verified provider receipts; it does not claim a physical row deletion.

Runtime erasure markers are sidecar evidence over a previously `complete`
replay manifest. They bind the exact erased Artifact IDs and missing replay
components. They do not mutate or reuse migration-only `legacy_partial`.
Consumers must reject exact replay when `require_replay_not_erased()` finds a
matching marker. Existing replay authorities are not silently rewritten.

## Bounds and resources

Requests, targets, key references, policy decisions, receipts, and event
histories are bounded; a retention operation accepts at most 60 targets and a
manifest at most 2 MiB. JSON decoding rejects duplicate keys, non-finite
numbers, excessive depth/nodes, invalid UTF-8, and unknown fields. Ledger
payloads contain IDs, digests, provider metadata, and failure codes, never
manifest, receipt, prompt, output, diff, or key bytes.

The generated external contracts are:

- `schemas/artifact_retention_event_payload_registry_v1.schema.json`
- `examples/artifact_retention_event_type_registry_v1.example.json`

Canonical and installed copies are byte-identical and covered by the strict
resource manifest.
