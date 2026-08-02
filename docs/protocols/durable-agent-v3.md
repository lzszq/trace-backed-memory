# Authenticated durable Agent composition v3

Status: opt-in application composition; not the default Agent or MCP profile.

Chinese: [durable-agent-v3.zh-CN.md](durable-agent-v3.zh-CN.md)

## Purpose

`tbm.durable-agent.v3` is the adapter-neutral application facade over the
published durable retrieval, Semantic Gate, finalization, execution, outcome,
and completion-outbox services. It gives an adapter one lifecycle boundary
without copying Gate policy or accepting caller-constructed authorization
scopes.

`AuthenticatedDurableAgentMemory` is intentionally stateless between calls.
Durable continuation comes from the configured authorities, not an in-memory
request handle. A new facade instance can continue a retained session when it
uses the same authorities and receives current trusted service, provider, and
evaluator contexts.

## Service graph

Construction requires exact instances of:

- `AuthenticatedRetrievalService`;
- `DurableRetrievalPreparationService`;
- `AuthenticatedSemanticGateSessionService`;
- `DurableFinalizationService`; and
- `DurableExecutionService`.

The constructor rejects a graph unless every stage shares the same
authorization service, GateSession authority, Gate evidence authority,
Semantic Gate authority, and ActivatedRevision source. Execution must replay
through the configured finalizer, and its completion outbox must use that
exact GateSession authority. This prevents a valid record or atomic completion
from one authority graph from being attached to a different lifecycle.

## Lifecycle

The facade exposes:

1. `prepare(context, request)`:
   authorizes `memory:retrieve`, creates the GateSession, stores exact
   RetrievalSnapshot/System Gate evidence, and publishes `PREPARED`;
2. `decide(context, provider_context, request, call_provider)`:
   first re-authenticates the session owner from retained retrieval evidence,
   persists a fresh `gate_session:transition` authorization, rechecks both
   scopes around provider work, and then invokes the authenticated Semantic
   Gate service;
3. `finalize(context, request)`:
   recovers the original retrieval scope, rechecks current authorization and
   revision state, persists a fresh transition authorization, stores the exact
   replay bundle, and publishes `FINALIZED`;
4. `start(context, request)` and `resume(context, request)`:
   recover the original retrieval scope, persist a fresh
   `gate_session:transition` authorization, and return the exact retained
   snippet only when execution is required;
5. `complete(context, evaluator_context, request)`:
   recovers retained retrieval evidence, persists a fresh transition
   authorization, authenticates the live evaluator, and atomically publishes
   evaluator/RunOutcome/completed-session events, the completion outbox event
   and initial delivery, and `EffectRequested`;
6. `abandon(context, request)`:
   recovers retained retrieval evidence, authorizes, and publishes
   exact-version terminal abandonment;
7. `cancel(context, request)`:
   recovers retained retrieval evidence and authorizes exact-version
   cancellation from `PREPARED` or `AWAITING_DECISION`, with exact idempotent
   replay; and
8. `get_session(context, session_id)`:
   returns current durable state only after recovering and verifying the
   original retrieval authorization; and
9. `export_replay_bundle(context, request)`:
   resolves the manifest from the exact durable-session linkage, persists and
   reads back a fresh `artifact:read` decision, preflights every descriptor,
   and returns the canonical portable replay bundle plus both authorization
   event IDs.

The facade does not expose `AuthorizedRetrievalScope` in its public method
inputs. Adapters pass only trusted contexts and versioned requests.

## Authorization recovery

`AuthenticatedRetrievalService.recover_authorized_scope()` reconstructs one
server-owned scope from a retained allowed decision. Recovery:

- reloads the current registry and policy;
- exact-matches the trusted Principal and AgentClient records;
- requires the retained decision to remain allowed, permission-matched, and
  bound to the current policy hash;
- resolves the current canonical repository reference or exact tenant alias;
- rechecks the active environment; and
- returns the original authorization event ID and canonical scope.

The durable Agent loads the session's retained RetrievalSnapshot, verifies its
session/Trace/run linkage, and takes the original authorization event ID from
that snapshot. Caller JSON cannot select or replace the event.

Policy rotation, identity rotation, a changed repository target, missing
evidence, or cross-owner session state fails closed. Recovery does not append a
new `memory:retrieve` decision. Every post-prepare GateSession mutation
separately appends a current `gate_session:transition` decision.

## Cancellation and replay

`DurableAgentCancelRequest` binds:

- `session_id`;
- `expected_session_version`; and
- a bounded terminal reason.

The first successful cancellation advances exactly one revision. A retry is
an exact replay only when the retained canceled revision is the requested
parent plus one and the terminal reason matches. Changed versions, changed
reasons, or non-cancelable states are rejected. `CREATED` cancellation remains
an internal preparation-compensation transition; callers cannot cancel a
session before retained retrieval evidence exists.

Finalization, execution start, completion, resume, and abandonment preserve
the replay and recovery semantics of their underlying services. The facade
does not rerender a snippet, repeat a retained provider success, or infer a
measurement.

## Authorized replay export

`DurableReplayExportRequest` binds the trusted operation to:

- `session_id`;
- `expected_session_version`;
- a non-empty, duplicate-free classification allowlist; and
- a caller content limit no larger than the global 8 MiB export limit.

The request deliberately does not accept a manifest digest or artifact ID.
After recovering the original `memory:retrieve` scope, the facade requires an
exact current session version and persists a new repository-scoped
`artifact:read` decision. Only inside that authorized callback does it resolve
the unique manifest matching the session's retained decision, usage decision,
and injection IDs. Missing or ambiguous linkage fails closed.

Manifest lookup reads bounded manifest and injection descriptors, not artifact
content. The portable exporter then checks every descriptor's classification,
size, digest, and manifest linkage before reading bytes. It rechecks the
current artifact-read authorization and the unchanged GateSession before
returning `DurableReplayExportResult`. The result links the bundle to both the
fresh read event and the original retrieval event.

The current SQLite and PostgreSQL replay authorities only support
`public`/`internal` plaintext. This method does not broaden that storage
boundary and does not grant access based on the classification allowlist.

## Trust boundary

This composition is not a transport authenticator. The embedding service must:

- derive `AuthenticatedServiceContext` from a trusted authenticator;
- derive Semantic Gate provider and outcome evaluator contexts from live
  authenticated transports;
- keep provider credentials and evaluator credentials out of caller JSON;
- keep external executor effects idempotent by GateSession `run_id`; and
- configure durable authorities and provider callbacks on the server.

The current composition supports the existing public/internal plaintext replay
profile and an authenticated session-bound replay-read boundary selected by
explicit durable HTTP/MCP and Python/TypeScript clients.
Protected-content encryption, retention integration, transport-authenticated
replay exposure, durable transition-authorization linkage in GateSession
revisions, and physical repository attestation remain separate required work.

## Adapter status

The facade is the shared application boundary for MCP, HTTP, CLI-daemon, and
SDK adapters. The optional
[`tbm.durable-agent-wire.v1`](durable-agent-wire-v1.md) dispatcher now maps all
facade operations with strict identity-free request models, trusted context
injection, stable errors, and fail-closed content profiles. Explicit durable
HTTP and trusted-local MCP profiles select it; synchronous/asynchronous Python
and Node.js TypeScript clients select durable HTTP. The default
`LocalAgentMemory`, compatibility `tbm-mcp`/HTTP profile, and general CLI still
use `tbm.agent.v1` with process-local pending handles. This protocol does not
claim shared multi-tenant readiness, peer-authenticated local STDIO, or a
default schema-version-3 cutover.

SQLite and PostgreSQL lifecycle parity is covered by the facade tests while
the underlying authorities preserve their existing transaction, savepoint,
CAS, and rollback contracts.
