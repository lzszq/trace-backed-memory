# Completion outbox v3

**English** | [简体中文](completion-outbox-v3.zh-CN.md)

The completion outbox publishes one immutable `execution_completed` event for
one durable RunOutcome. It separates the content-addressed event from its
append-only delivery state so retries never rewrite completion evidence.

## Contracts

- `tbm.completion-outbox-event.v3` binds the canonical tenant, repository,
  GateSession, Trace, run, usage decision, RunOutcome, outcome descriptor
  digest, and completion timestamp.
- `tbm.completion-outbox-delivery.v3` is one content-addressed delivery
  revision. Its states are `pending`, `leased`, `retry_wait`, `delivered`, and
  `dead_letter`.
- Event and delivery JSON are bounded, reject duplicate keys and unknown
  fields, and use canonical content-derived identifiers.
- A delivery history is linear. Claims increment the attempt count and create
  a bounded lease; expired leases can be reclaimed. A failed attempt either
  schedules a bounded retry or enters terminal dead letter state.

The canonical portable resources are:

- `schemas/completion_outbox_event_v3.schema.json`;
- `schemas/completion_outbox_delivery_v3.schema.json`;
- `examples/completion_outbox_event_v3.example.json`;
- `examples/completion_outbox_delivery_v3.example.json`.

## SQLite and PostgreSQL authorities

`SQLiteCompletionOutboxV3Repository` composes the isolated SQLite
RunOutcome/GateSession authority. `complete_session()` performs all of the
following in one outer transaction or caller savepoint:

1. verifies and appends `EXECUTING` to `COMPLETED`;
2. inserts the immutable RunOutcome;
3. inserts the immutable completion event;
4. inserts its initial `pending` delivery revision and head;
5. appends the canonical `EffectRequested` event after the authenticated
   evaluator, RunOutcome, and completed-session events; and
6. reads back and verifies every retained record before commit.

An exact completion replay returns the retained event and current delivery
head without creating a second event. A pre-existing completed outcome without
the corresponding outbox event is rejected as an orphan; the repository does
not silently repair an atomicity violation.

The worker surface is deliberately bounded:

- `claim_due(worker_id, lease_seconds, limit=100)`;
- `acknowledge(event_id, expected_version, worker_id, response_sha256=None)`;
- `fail_delivery(event_id, expected_version, worker_id, error_code,
  retry_delay_seconds, max_attempts)`;
- exact event, current delivery, and delivery-history reads.

`CompletionOutboxDeliveryWorker.run_once()` is the storage-neutral dispatcher
over that surface. It claims at most one bounded page, validates the complete
claim batch before invoking any consumer callback, and accepts only a
`CompletionOutboxConsumerReceipt` with an optional canonical response digest.
`CompletionOutboxConsumerError` persists only its bounded error code; other
callback exceptions become `TBM_COMPLETION_OUTBOX_CONSUMER_FAILED`, never raw
exception text. Successful callbacks use exact-version acknowledgement.
Failures use the configured retry delay and maximum attempts. Each result is
classified as `delivered`, `retry_wait`, `dead_letter`, `superseded`, or
`recovery_required`, and every successful state write is read back exactly.
Malformed claims, transition-invalid receipts, or a different configured retry
delay fail closed.

The explicit event-first repositories append `EffectStarted` for claim or
reclaim, `EffectSucceeded` for acknowledgement, and paired
`EffectFailed`/`EffectRetryScheduled` or
`EffectFailed`/`EffectDeadLettered` events for failure disposition. Those
canonical events and the delivery revision/head are one atomic unit. The
`effect-queue` reducer rebuilds the exact delivery history and current status;
see [Effect Event v1](effect-event-v1.md).

The SQLite schema uses immutable event and delivery-revision rows, one
compare-and-swap head, canonical descriptor validation, integer-microsecond due
ordering, schema-drift detection, caller-transaction preservation, and a
repository-scoped mutation guard. Multiple repository wrappers over the same
connection share one re-entrant lock and one thread-local mutation scope.
Direct DML through a repository-owned connection is rejected.

`PostgresCompletionOutboxV3Repository` exposes the same completion, claim,
acknowledgement, failure, and read surface over
`schemas/postgres-v3-completion-outbox*.sql`. It preserves the dependency lock
order GateSession → RunOutcome → completion outbox, locks the GateSession
head before database-time completion, and commits the completed revision,
RunOutcome, event, initial delivery revision, and head in one transaction or
caller savepoint. Worker claims lock due heads with `FOR UPDATE ... SKIP
LOCKED`; acknowledgement, retry, reclaim, and dead-letter transitions append a
revision and compare-and-swap the exact head version.

The PostgreSQL insert triggers reconstruct canonical descriptor bytes,
recompute both content identifiers, verify exact completed-session/outcome
linkage, and reject invalid state transitions. Runtime reads verify projected
columns against the retained descriptor. The adapter and rollback script both
fail closed on catalog drift, including relation, index, constraint, function,
trigger, privilege, and policy changes.

## Delivery semantics and boundary

Delivery is **at least once**. A worker can publish successfully and crash
before acknowledgement, after which the lease is reclaimed. Consumers must
therefore deduplicate by `event_id`; a response digest is audit metadata, not
proof that a remote side effect occurred exactly once.
`delivered` and `EffectSucceeded` mean only that the local consumer callback
was acknowledged; they are not provider receipts or proof of provider-side
success.
The configured lease must cover the consumer's maximum processing time.
Lease expiry during a callback may allow another worker to invoke the consumer
for the same event before the first worker can acknowledge it. A
`recovery_required` result means the callback completed but the leased
revision remains current after an acknowledgement or failure-write error;
`superseded` means another durable revision now owns the truth.

These are opt-in, side-by-side SQLite and isolated PostgreSQL authorities.
Alone they do not change active SQLite schema version 1 or PostgreSQL schema
version 2, emit network traffic, authenticate an evaluator, authorize artifact
bytes, or create an OutcomeAttribution event. The durable execution/facade
composition wires them into explicit durable HTTP/MCP and Python/TypeScript
clients. Explicit `tbmd local` operates bounded SQLite delivery pages and
reclaims expired leases; default compatibility cutover, PostgreSQL
shared-service dispatch, and remote consumer operations remain part of the
coordinated version-3 program.
The SQLite connection owner remains a trusted operator boundary: code that can
replace registered SQLite functions or drop and recreate triggers can also
rewrite the database and must not be exposed to untrusted callers.
