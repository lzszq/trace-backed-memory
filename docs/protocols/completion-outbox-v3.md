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

## SQLite authority

`SQLiteCompletionOutboxV3Repository` composes the isolated SQLite
RunOutcome/GateSession authority. `complete_session()` performs all of the
following in one outer transaction or caller savepoint:

1. verifies and appends `EXECUTING` to `COMPLETED`;
2. inserts the immutable RunOutcome;
3. inserts the immutable completion event;
4. inserts its initial `pending` delivery revision and head;
5. reads back and verifies every retained record before commit.

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

The SQLite schema uses immutable event and delivery-revision rows, one
compare-and-swap head, canonical descriptor validation, integer-microsecond due
ordering, schema-drift detection, caller-transaction preservation, and a
repository-scoped mutation guard. Multiple repository wrappers over the same
connection share one re-entrant lock and one thread-local mutation scope.
Direct DML through a repository-owned connection is rejected.

## Delivery semantics and boundary

Delivery is **at least once**. A worker can publish successfully and crash
before acknowledgement, after which the lease is reclaimed. Consumers must
therefore deduplicate by `event_id`; a response digest is audit metadata, not
proof that a remote side effect occurred exactly once.

This is an opt-in, side-by-side SQLite authority. It does not change active
SQLite schema version 1, emit network traffic, authenticate an evaluator,
authorize artifact bytes, create an OutcomeAttribution event, or wire durable
completion into the active Agent/MCP/HTTP/SDK lifecycle. PostgreSQL parity and
active adapter integration remain part of the coordinated version-3 program.
The SQLite connection owner remains a trusted operator boundary: code that can
replace registered SQLite functions or drop and recreate triggers can also
rewrite the database and must not be exposed to untrusted callers.
