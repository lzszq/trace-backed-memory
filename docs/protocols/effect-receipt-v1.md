# External Effect Receipt Protocol v1

**English** | [简体中文](effect-receipt-v1.zh-CN.md)

`tbm.effect-receipt.v1` is the storage-neutral F3-04 contract for external
side effects. It records the intent, trusted authorization linkage, every
provider attempt, the provider request identity, exact receipt evidence,
ambiguous results, bounded retries, dead-lettering, and compensation as an
ordered canonical event stream.

This protocol does not claim that a database transaction can atomically cover
a remote provider call. The remote call remains outside the ledger
transaction. The protocol makes that boundary explicit and recoverable.

## Event registry

The sealed registry contains:

- `tbm.effect.requested`
- `tbm.effect.authorized`
- `tbm.effect.started`
- `tbm.effect.provider_request_recorded`
- `tbm.effect.receipt_recorded`
- `tbm.effect.succeeded`
- `tbm.effect.result_unknown`
- `tbm.effect.failed`
- `tbm.effect.retry_scheduled`
- `tbm.effect.dead_lettered`
- `tbm.effect.compensation_requested`
- `tbm.effect.compensated`

The generated payload dispatch schema and registry catalog are packaged as
`schemas/effect_receipt_payload_registry_v1.schema.json` and
`examples/effect_receipt_type_registry_v1.example.json`.

## Effect contract and trust boundary

Each stream preserves one immutable `EffectContract`:

- stable `effect_id`, `effect_type`, and an idempotency key deterministically
  derived from the complete immutable intent;
- the canonical event that requested the effect;
- the exact input Artifact digest;
- the authorization event selected by trusted adapter context;
- whether compensation was explicitly supported;
- a bounded maximum attempt count.

`EffectContract.authorization_event_id` must equal the authorization decision
on the access-bound `EventLedgerPort`. External request JSON cannot select the
tenant, repository, principal, actor, authorization decision, or ledger
partition. `TrustedEffectProvider` is likewise service-composed; no durable
wire profile accepts caller-selected provider identity or receipt authority.
An integrating service must verify the referenced authorization decision and
provider registration before it calls this protocol.

Raw input and provider receipt bytes never enter ledger metadata. Request and
receipt events carry exact `EventArtifactRef` descriptors; all other events
carry no byte payload. Canonical event bounds, strict schemas, duplicate-key
rejection, and secret-key rejection continue to apply.

## Attempts and provider identity

An attempt starts only after authorization or a scheduled retry. Attempt IDs
and canonical provider-request digests are deterministically derived from the
immutable effect contract, attempt number, and trusted provider registration.
Attempt numbers are contiguous and may not exceed `max_attempts`.

Within a complete reducer input, every accepted provider request ID is bound to
exactly one provider, effect, attempt, and canonical request digest. Reusing the
same provider request ID for a different binding fails closed. A future active
multi-stream writer must enforce the same uniqueness with an atomic projection
or authority; the current per-effect append helper does not claim that
cross-stream write guarantee. This is evidence for reconciliation and
deduplication, not an exactly-once claim.

## Receipt and unknown-result rules

A success requires two events:

1. `EffectReceiptRecorded`, binding the provider request ID, canonical request,
   result digest, and available receipt Artifact;
2. `EffectSucceeded` (or `EffectCompensated`), exactly repeating the persisted
   provider request, receipt, and result digests.

A timeout, lost response, interrupted process, or uncertain acknowledgement is
`EffectResultUnknown`, never failure. Unknown state cannot be retried,
dead-lettered, or compensated. Reconciliation must resolve it to an exact
receipt or to a known `reconciled_absent` failure with an exact available
reconciliation Artifact. That resolution cannot invent or change a provider
request ID.

Known failures distinguish `pre_send`, `provider_rejected`, and
`reconciled_absent`. Only a retryable known failure with remaining budget may
produce `EffectRetryScheduled`. Dead-letter is terminal evidence; it does not
prove the provider performed no side effect.

## Compensation

Compensation is a distinct child effect stream. Its first event binds the
exact successful parent event hash and provider receipt digest. The parent
must have declared compensation support. The child obtains its own
authorization, attempts, provider request ID, and receipt. Existing parent
events are never modified.

## Ledger and reducer behavior

`build_effect_receipt_batch()` produces content-addressed canonical events and
one ledger idempotency binding. `append_effect_receipt_batch()` reads bounded
stream history through the access-bound ledger, appends atomically, and
verifies the exact ledger receipt. Exact command replay returns the prior
receipt; a changed command conflicts.

`reduce_effect_receipt_events()` rebuilds lifecycle projections from globally
ordered events. It rejects incomplete stream history, cross-partition input,
contract drift, invalid parents, duplicate provider-request bindings, receipt
mismatch, blind retry from unknown state, and compensation of an ineligible
parent.

## Current boundary

This protocol is opt-in and storage-neutral. It does not replace the F2-05
`outcome_effect_event_v1` compatibility projection or the existing completion
outbox worker. In particular, legacy `response_sha256` remains only a response
digest and is not promoted into a provider receipt. Default Agent, MCP, HTTP,
SDK, and the opt-in Codex ingestion adapter do not select this effect protocol;
that remains a later-stage integration gate. Cross-effect provider-request uniqueness is verified during
complete global replay, but an active atomic uniqueness projection or authority
is still required before shared concurrent dispatch.
