# Semantic Gate Attempt Event v1

**English** | [简体中文](semantic-gate-attempt-event-v1.zh-CN.md)

`tbm.semantic-gate-attempt-event.v1` is the compact canonical-event contract
for the F2 Semantic Gate attempt-chain cutover increment. It defines:

- `tbm.semantic_gate.attempt_failed` for an immutable failed attempt;
- `tbm.semantic_gate.attempt_succeeded` for an immutable succeeded attempt.

Each System Gate evaluation owns one deterministic
`semantic_gate_stream_sha256_<digest>` stream. Event stream version equals the
attempt sequence. A retry names the previous attempt event as causation and
retains its exact event digest as `previous_stream_event_sha256`.

## Parent and trusted scope

The first attempt cannot be built or persisted from a derived parent ID alone.
It requires the retained, valid `tbm.system_gate.evaluated` event and verifies
its evaluation, session, retrieval-snapshot, and event identities. The parent
and attempt event must have the same trusted organization, tenant, repository,
environment, principal, agent client, and actor scope.

The two events intentionally may name different authorization decisions. The
System Gate event retains the original retrieval authorization, while the
Semantic attempt event retains the fresh `gate_session:transition`
authorization for the mutation. Both contexts come from trusted adapters;
request JSON cannot select either context.

## Compact payload and exact Artifact linkage

The payload retains bounded attempt-chain metadata, provider/model/template
identity, configuration and provider-request IDs, prompt/response digests,
final revision sets, decision/risk/injection metadata, token and latency
measurements, timestamps, and role-specific Artifact IDs. It never embeds the
prompt bytes, response bytes, or decision reason.

Canonical Artifact references are sorted and deduplicated by Artifact ID and
cover:

1. the exact canonical `SemanticGateAttempt` descriptor JSON;
2. exact prompt bytes;
3. exact response bytes for a succeeded attempt.

Every reference binds content digest, media type, byte size, classification,
retention, encryption-key metadata when required, and availability. Duplicate
Artifact IDs with different descriptors fail closed. Exact bytes remain in the
transitional authenticated Semantic Gate artifact authority; reducers read no
Artifact bytes.

## Event-first persistence and reducer

Explicit durable SQLite and PostgreSQL runtimes enable event-first Semantic
attempt writes. One transaction performs:

```text
retained System Gate/attempt-parent verification
→ canonical event append and read-back
→ SemanticGateAttempt row projection
→ exact Artifact bytes and role-binding projection
→ exact read-back
```

Any failure rolls back the event, stream/global heads, idempotency receipt,
attempt row/head, Artifact bytes, and bindings. Exact retries validate the
retained event without allocating another global position. PostgreSQL writers
hold the fixed order `event-ledger schema/global head → semantic attempt
projection → semantic Artifact projection`; caller transactions remain owned
by the caller.

`semantic-gate-attempt-chain` version 1 is pure and deterministic. It consumes
the System Gate parent plus failed/succeeded attempt events, rejects missing or
mismatched parents and non-monotonic retries, and rebuilds exact current stream,
attempt, Artifact-linkage, and event-head views. Fieldwise parity requires both
retained authority bundles and the canonical events, so duplicate authority
inputs or synchronized fake event hashes cannot pass.

Final decision/injection views and ledger-backed replay export are delivered in
the current F2 increment; see [Finalization Event v1](finalization-event-v1.md) and
[Ledger Replay Export v1](ledger-replay-export-v1.md). Outcome/attribution and
local completion-effect reducers now provide event-first parity through
delivery history and dead letter. The storage-neutral provider receipt/
reconciliation event, reducer, and ledger service are delivered. Configured
explicit durable runtimes select server-owned Semantic-provider invocation;
trusted reconciliation, owner-fence attestation, and bounded retry/dead-letter
activate only when their corresponding trusted dependencies are supplied.
Atomic request claiming, provider/policy binding, receipt-backed generic
compensation for contracts that support it, and transport invocation parity are
delivered. Semantic provider effects do not support compensation or claim remote
exactly-once. Concrete
remote/completion adapters, background fencing workers, locally unexecuted
PostgreSQL crash probes, and the remaining F2 crash matrix remain open.
`full_persistence` therefore remains `false`.
