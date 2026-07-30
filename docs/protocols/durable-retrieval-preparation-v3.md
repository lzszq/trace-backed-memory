# Durable retrieval preparation v3

**English** | [简体中文](durable-retrieval-preparation-v3.zh-CN.md)

`DurableRetrievalPreparationService` is the opt-in composition boundary that
attaches authenticated retrieval preparation to one durable GateSession. It
reuses the existing authorization, retrieval-preparation, Gate evidence, and
GateSession authorities; it does not change the active snapshot-v2 Store or
the default Agent/MCP lifecycle.

## Request identity

`DurableRetrievalPreparationRequest` binds the public retrieval request,
Trace/run identity, server-matched retrieval context, mode, retriever version,
top-K, idempotency key, expiry, and lease. Its server-derived
`request_fingerprint` also binds the raw-query digest and any semantic
provider/version/vector evidence. Raw query bytes are bounded inputs and are
never included in the fingerprint payload, representation, GateSession, or
RetrievalSnapshot.

The Gate and retrieval services must share the same
`AuthenticatedRetrievalService` instance. This prevents the trusted
composition path from silently authorizing a different scope or recording a
second authorization decision.

## Preparation order

For a new idempotency key, the service:

1. authorizes once through `AuthenticatedGateSessionService`;
2. creates and reads back the scoped `CREATED` GateSession;
3. rebuilds the retrieval request with that exact durable `session_id`;
4. runs `AuthenticatedRetrievalPreparationService` inside the already
   authorized scope;
5. stores the exact RetrievalSnapshot/SystemGateEvaluation pair through the
   configured Gate evidence authority;
6. validates the storage receipt and reads both records back through
   `DurablePreparedGateEvidenceVerifier`; and
7. lets the Gate service CAS-publish and read back `PREPARED`.

Exact replay returns the existing durable session without repeating
authorization-side discovery, revision reads, evidence generation, or evidence
writes. The durable authorities first perform a scope-local idempotency lookup;
the service then reloads the retained snapshot/evaluation, recovers the
original authorization scope, and revalidates current activated revisions and
policy before returning the same response. `prepare_for_authorized_scope()` and
`recover_persisted_evidence()` are trusted internal composition hooks. They
must never be exposed directly through MCP, HTTP, CLI, SDK, or caller-owned
callbacks.

## Failure and transaction boundary

Preparation, evidence storage, receipt, read-back, or verification failure is
sanitized and delegated to the Gate service's version-checked
`CREATED -> CANCELED` compensation. A concurrent or abnormal durable state is
reported as recovery required.

The default composition is ordered compensation across authorities, not one
atomic distributed transaction. If evidence is durably written and the later
GateSession transition fails, the immutable evidence may remain as an orphan
while the session is canceled. When the SQLite or PostgreSQL GateSession and
Gate evidence repositories deliberately share one caller-owned connection, an
outer caller transaction may roll back both authorities together; this is an
explicit deployment choice, not an automatic service guarantee.

The opt-in [durable Semantic Gate service](durable-semantic-gate-v3.md) now
continues this exact evidence through `AWAITING_DECISION` to `DECIDED`.
The opt-in [authenticated durable Agent](durable-agent-v3.md) composes that
continuation with finalization, execution, cancellation, and completion.
The explicit durable HTTP profile selects this service. Production index
workers/sharding and durable MCP/TypeScript wiring remain separate work.
