# Effect Event v1

**English** | [简体中文](effect-event-v1.zh-CN.md)

`tbm.effect-event.v1` is the storage-neutral canonical event contract for the
local completion-notification effect lifecycle. It binds the existing durable
completion outbox to an append-only effect stream without treating delivery
state as proof of a remote provider-side result.

## Event family

Version 1 registers eight typed events:

- `tbm.effect.requested`;
- `tbm.effect.started`;
- `tbm.effect.succeeded`;
- `tbm.effect.failed`;
- `tbm.effect.retry_scheduled`;
- `tbm.effect.dead_lettered`;
- `tbm.effect.compensation_requested`; and
- `tbm.effect.compensated`.

Every effect uses its own `effect` stream. The immutable `EffectContract` binds
the effect identity/type, idempotency key, requesting event, input Artifact
digest, authorization event, and whether compensation is supported. A
completion-notification request additionally embeds the exact
`tbm.completion-outbox-event.v3` descriptor and its initial `pending` delivery
revision. Its effect ID and idempotency key equal the outbox event ID, and its
input digest equals the RunOutcome descriptor digest.

Delivery events retain the exact previous and current outbox delivery
revisions. The parser revalidates the outbox transition, identity, trusted
scope, stream parent, authorization linkage, and causation. Claim or reclaim
produces `EffectStarted`; acknowledgement produces `EffectSucceeded`; a
retryable failure produces `EffectFailed` followed by
`EffectRetryScheduled`; a terminal failure produces `EffectFailed` followed by
`EffectDeadLettered`. A retry or dead-letter disposition cannot appear without
the immediately preceding failure event.

## Event-first persistence

The explicit SQLite and PostgreSQL durable completion repositories append, in
one transaction or caller savepoint:

1. `EvaluationAuthenticated`;
2. `RunOutcomeRecorded`;
3. `GateSessionCompleted`; and
4. `EffectRequested` with the completion event and initial delivery.

Claim, reclaim, acknowledgement, retry, and dead-letter operations append the
corresponding effect event batch in the same transaction as the new delivery
revision and head. The canonical event actor is the actual `worker_id`, not a
repository placeholder. PostgreSQL locks the event-ledger schema/global head
before outbox row locks. Either every canonical event and synchronized
authority row is retained and read back, or the complete operation rolls back.

Exact completion replay returns the retained effect request; it does not append
a duplicate event. Missing, ambiguous, cross-partition, reordered, or
projection-divergent evidence fails closed.

## EffectQueue projection

The registered `effect-queue` reducer rebuilds `effect_queue_v1` with states
`ready`, `leased`, `retry`, `dead_letter`, `succeeded`, and `compensated`. It
retains compact immutable event metadata, the exact outbox delivery history,
attempt count, pending failure, current delivery, and stream head. Parity
verification compares the rebuilt contract, status, completion event, and
delivery revisions with the transitional completion-outbox authority.

The reducer enforces linear streams, terminal-state monotonicity, exact
failed-before-retry/dead-letter ordering, and the rule that compensation is a
new causally linked effect stream. The storage-neutral compensation builders,
parsers, and reducer transitions are available, but the current SQLite and
PostgreSQL completion repositories do not expose a durable compensation append
API.

## Trust boundary

`EffectSucceeded` means only that the local consumer callback returned a valid
acknowledgement and the local outbox revision became `delivered`. A response
digest is audit metadata. Neither fact proves that an external provider
performed a side effect, performed it exactly once, or returned a durable
receipt.

Provider request IDs, provider receipts, authorization events beyond the
retained effect contract, unknown-result classification, reconciliation, and
durable compensation orchestration remain F3 work. The current adapters retain
at-least-once delivery semantics, remain opt-in, and do not change
`persistence_model="authority_graph"` or `full_persistence=false`.
