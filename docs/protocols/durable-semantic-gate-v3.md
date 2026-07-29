# Durable Semantic Gate v3

**English** | [简体中文](durable-semantic-gate-v3.zh-CN.md)

`AuthenticatedSemanticGateSessionService` is the opt-in composition boundary
that advances one prepared durable GateSession through authenticated Semantic
Gate work. It reuses the existing GateSession, Gate evidence, Semantic Gate
attempt, and exact artifact-byte authorities. It does not change the active
snapshot-v2 Store or the default Agent/MCP lifecycle.

## Request and authority

`DurableSemanticGateRequest` binds the durable `session_id`, expected session
revision, exact bounded UTF-8 prompt bytes, and the exact expected Semantic Gate
attempt parent. Its bounded `lease_seconds` is the requested server-side
decision claim. The caller cannot select a RetrievalSnapshot or System Gate
evaluation: the service derives both from the durable session and rejects any
session/evaluation/snapshot/Trace/run mismatch before provider work.

The provider context is still transport-derived and must exactly match the
server-owned provider, authenticator, and credential registration. This
provider authentication is not tenant authorization. The session must already
be the output of the authenticated durable retrieval-preparation boundary; the
composition service remains a trusted internal service and is not exposed by
the active adapters. Its provider callback must remain server-owned; provider
authentication does not turn a caller-supplied callback into trustworthy model
provenance, and signed provider attestation remains future work.

## Decision order

For a fresh prepared session, the service:

1. authenticates the provider context before any session or evidence read;
2. reads the current GateSession and checks the exact expected revision;
3. reloads the complete immutable
   RetrievalSnapshot/SystemGateEvaluation/attempt chain;
4. verifies session, Trace, run, snapshot, evaluation, and chain linkage;
5. CAS-publishes and reads back `AWAITING_DECISION`;
6. CAS-renews and reads back the live decision lease, so stale or competing
   revisions fail before provider work;
7. invokes `AuthenticatedSemanticGateService`, which owns provider/model,
   prompt-template, generation, timing, result, and exact prompt/response
   provenance;
8. reloads and verifies the complete attempt chain, including monotonic System
   Gate narrowing and exact artifact read-back; and
9. CAS-publishes and reads back `DECIDED` with the complete ordered attempt-ID
   chain and the successful attempt's `decision_id`.

A failed provider call remains an immutable prompt-only attempt and leaves the
session in `AWAITING_DECISION`. A retry must name that exact failed attempt as
its parent and the current session revision. A retry may use different prompt
bytes, and every attempt retains its own exact prompt provenance; exact replay
or recovery of an existing success must use that success's original prompt
bytes. Retrying identical prompt or response bytes reuses the existing
content-addressed artifact descriptor while creating a new attempt-specific
binding; immutable content is not rewritten.

## Replay and recovery

If a successful attempt was retained but the later session transition did not
commit, an exact request can finish `AWAITING_DECISION -> DECIDED` without
calling the provider again. An exact retry against an already decided session
also returns the stored session and attempt/artifact receipt without another
provider call. Both paths require the same prompt bytes, the successful
attempt's original parent, the complete chain, and the same decision linkage.
The expected revision is enforced before mutable work. A decided replay
deliberately accepts the original request's older revision because the terminal
session has necessarily advanced; the exact prompt, parent, chain, decision,
and read-back receipt become the idempotency proof.

A succeeded attempt followed by another attempt, a prompt or parent mismatch,
tampered read-back, an expired/canceled session, or a transition that cannot be
confirmed is recovery-required. The service never deletes or rewrites an
orphan attempt and never silently repeats an external provider call after a
successful result may have been retained.

Cancellation or expiry can race with an in-flight provider callback. Once
provider work has started, the composition cannot promise that the external
provider had no side effect. It can promise that a retained successful attempt
is never attached to a canceled, expired, or otherwise terminal session: the
caller receives recovery-required and the immutable evidence remains visible
for operator review. An already expired `AWAITING_DECISION` lease, by contrast,
fails its claim before the callback and retains no new attempt.

## Transaction boundary

The default composition is ordered recovery across authorities, not a
distributed transaction. A provider failure leaves `AWAITING_DECISION` plus a
failed attempt. A successful attempt followed by an unconfirmed GateSession
CAS may leave an immutable attempt that an exact recovery call can attach.

When the SQLite or PostgreSQL GateSession, Gate evidence, and Semantic Gate
artifact repositories deliberately share one caller-owned connection, an
outer caller transaction can roll back the session transitions, attempt, and
artifact bindings together. The service itself does not open that outer
transaction. Holding it across a provider call also holds database locks and
is therefore an explicit deployment tradeoff.

The separate opt-in durable finalization composition now provides rendering,
content-addressed final injection, complete replay-bundle retention, and the
`DECIDED -> FINALIZED` CAS. `EXECUTING`, outcome completion, active transport
authentication, and Agent/MCP/HTTP/SDK emission remain separate work.
