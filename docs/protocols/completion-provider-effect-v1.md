# Completion Provider Effect v1

**English** | [简体中文](completion-provider-effect-v1.zh-CN.md)

`tbm.completion-provider-effect.v1` is an opt-in, storage-neutral consumer
bridge from one leased `tbm.completion-outbox-event.v3` delivery to the
provider-transition evidence already defined by `tbm.effect-event.v1`. It is a
callable that can be supplied as `DurableRuntimeDependencies.completion_consumer`;
the runtime and local daemon do not construct it by default.

## Trusted inputs

The bridge is constructed with:

- one `EventLedgerAtomicAppendPort` whose authenticated actor is the exact
  delivery `worker_id`;
- one server-owned `TrustedProviderEffectRegistration`;
- one trusted provider callback;
- one trusted clock; and
- optionally, one trusted read-only reconciliation callback.

Caller JSON cannot choose the worker, provider, endpoint, authorization, or
reconciler. The retained `EffectRequested` event must embed the exact completion
event, immutable event-ID idempotency key, RunOutcome descriptor digest, and
`compensation_supported=false`. The current outbox delivery must be an unexpired
lease owned by the ledger worker.

`CompletionProviderCall` contains only the provider registration, immutable
completion descriptor, and the completion event ID as the provider idempotency
key. `CompletionProviderResult` contains only a bounded provider request ID and
response SHA-256. Raw response bodies, credentials, and provider errors are not
accepted. Adapter failures use a fixed sanitized error-code allowlist.

## Append and fencing order

For a new delivery the bridge:

1. verifies the exact completion request and active worker lease;
2. appends `attempt_started` before calling the provider;
3. calls the provider with the stable completion event ID;
4. appends `request_submitted` when a request ID is available; and
5. appends the content-derived provider receipt before returning the response
   digest to the completion worker.

Every new provider transition is fenced by both the exact retained delivery
revision and the exact effect-stream head observed immediately before append.
Exact replay of an already retained transition remains allowed. A lease reclaim,
delivery transition, or other stream append invalidates the old fence, so a late
owner cannot append a direct receipt.

The provider callback is outside the database transaction. Delivery remains
at least once, and remote exactly-once depends on the provider honoring the
stable idempotency key. Local `EffectSucceeded` remains only the completion
worker acknowledgement; the provider receipt is separate evidence.

## Recovery

Recovery is monotonic:

| Provider state | Action |
|---|---|
| `not_started` | atomically append the first `attempt_started`, then invoke |
| `in_flight` / `submitted` under the same delivery revision | return recovery-required; do not invoke or reconcile |
| `in_flight` / `submitted` under a later valid lease | append `result_unknown` with `owner_fenced`, then reconcile |
| `unknown` | call only the trusted reconciler |
| `not_found` | append `retry_scheduled`, then let a later bounded outbox delivery start the next attempt |
| `retry_wait` | start the next attempt only at or after retained `retry_at` |
| `succeeded` | replay the exact retained response digest without another provider call |
| `dead_lettered` | return the stable dead-letter error |

`confirmed` reconciliation must match any retained provider request ID,
response digest, and receipt. `still_unknown` remains recovery-required.
`not_found` is the only state that permits retry. Completion retry/dead-letter
bounds remain owned by the durable outbox delivery chain; this bridge never
supports completion compensation.

## Boundary

The bridge reuses the generic SQLite/PostgreSQL event-ledger ports and adds no
SQL authority, schema, migration, or packaged resource. Focused SQLite tests
cover receipt-before-ack response loss, unknown confirmation, not-found retry,
late-owner fencing, stable error sanitization, and exact receipt replay. The
generic provider-ledger and completion-outbox suites retain SQLite/PostgreSQL
authority parity.

No concrete remote adapter, credential loader, automatic provider sweep, shared-
service worker, or default runtime/daemon wiring is bundled. PostgreSQL hard-
crash execution for this bridge and the remaining F3 crash matrix are still
open. The capability is opt-in and does not change
`persistence_model="authority_graph"` or `full_persistence=false`.
