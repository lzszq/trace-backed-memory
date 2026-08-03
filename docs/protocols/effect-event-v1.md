# Effect Event v1

**English** | [简体中文](effect-event-v1.zh-CN.md)

`tbm.effect-event.v1` is the storage-neutral canonical event contract for local
completion delivery and authenticated provider-effect evidence. It binds the
existing durable completion outbox and provider request/receipt/reconciliation
records to append-only effect streams without treating local delivery state as
proof of a remote result.

## Event family

Version 1 registers nine typed events:

- `tbm.effect.requested`;
- `tbm.effect.started`;
- `tbm.effect.succeeded`;
- `tbm.effect.failed`;
- `tbm.effect.retry_scheduled`;
- `tbm.effect.dead_lettered`;
- `tbm.effect.compensation_requested`;
- `tbm.effect.compensated`; and
- `tbm.effect.provider_transition`.

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

## Provider transition and receipt evidence

One strictly discriminated `ProviderEffectTransitionRef` avoids spending a
separate registry type on every provider sub-state. Its stages are
`attempt_started`, `request_submitted`, `result_unknown`, `receipt_recorded`,
`reconciled`, and `retry_scheduled`. Every transition binds one exact effect,
attempt sequence, content-derived attempt/invocation identity, provider/model/
endpoint registration, and request digest. Provider request IDs are retained
only after the authenticated adapter reports them.

A successful receipt requires a provider request ID and response digest. Its
`provider_receipt_id` is content-derived from the invocation, request ID, and
response digest. Reconciliation is independently sequenced and content-
addressed. `confirmed` requires the same exact receipt shape; `still_unknown`
remains unknown; `not_found` only permits a later explicit retry schedule. An
unknown or orphaned in-flight/submitted attempt can never silently become a
retry. A new attempt may start only after the not-found reconciliation and
retry-scheduled evidence.

`ProviderEffectLedgerService` appends each transition through the authenticated
`EventLedgerPort`, retries only stale stream/global positions, and replays an
exact retained append receipt after response loss. Recovery returns one of
`start_attempt`, `reconcile`, `schedule_retry`, or `complete`. A restart that
sees only `attempt_started` or `request_submitted` returns `reconcile`, because
the ledger cannot prove whether the external request ran. The service binds one
server-owned `TrustedProviderEffectRegistration` and rejects transitions whose
provider/model/version/endpoint differ. The application must keep this service
behind its authenticated provider adapter; the service does not authenticate a
remote provider from request JSON.

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

The registered `effect-queue` reducer version 2 rebuilds `effect_queue_v1`
schema version 2 with states
`ready`, `leased`, `retry`, `dead_letter`, `succeeded`, and `compensated`. It
retains compact immutable event metadata, the exact outbox delivery history,
attempt count, pending failure, current delivery, and stream head. Parity
verification compares the rebuilt contract, status, completion event, and
delivery revisions with the transitional completion-outbox authority.

The same projection now retains provider attempts and transitions, exact
receipt/reconciliation identities, and provider states `not_started`,
`in_flight`, `submitted`, `unknown`, `not_found`, `retry_wait`, and `succeeded`.
Receipt/request mismatches, non-contiguous reconciliation, retry before a
not-found result, and changed provider provenance fail closed.

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

A `provider_receipt_id` proves that the authenticated local adapter retained a
content-bound provider report; it does not prove exactly-once execution in the
remote system. Raw provider bodies, secrets, and unbounded errors are never
embedded in the event.

The storage-neutral provider event/reducer/ledger service is delivered and the
generic SQLite/PostgreSQL ledgers can retain it without another authority or
schema component. Active semantic-provider and completion-consumer callbacks
do not yet select this service, and provider-specific reconciliation adapters,
durable compensation orchestration, and the complete transport/crash matrix
remain F3 work. Current adapters retain at-least-once delivery semantics,
remain opt-in, and do not change `persistence_model="authority_graph"` or
`full_persistence=false`.
