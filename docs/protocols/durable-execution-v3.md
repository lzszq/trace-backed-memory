# Durable execution v3

**English** | [简体中文](durable-execution-v3.zh-CN.md)

`DurableExecutionService` is the opt-in application composition for the
durable runtime back half. It verifies the retained finalization bundle,
advances `FINALIZED -> EXECUTING`, supports explicit lease-based resume or
abandonment, and completes one run through the existing atomic
`RunOutcome + COMPLETED + completion outbox` authority.

## Authorization and ownership

Every operation receives `AuthenticatedServiceContext` from a trusted
transport authenticator. Start and resume require both:

- the original `memory:retrieve` scope referenced by the retained
  UsageDecision; and
- a current `gate_session:transition` scope for the same principal, agent
  client, tenant, repository, and environment.

Abandonment and completion require the current transition scope and recheck it
immediately before loading or mutating the session. A policy or registry
rotation therefore invalidates an old scope; an allowed decision is not a
bearer capability.

The transition authorization event ID returned by this service identifies the
authorization verified for that call. GateSession v3 does not yet store this
event ID in its immutable revision, so it must not be described as durable
transition-event linkage. Authorization decisions remain durable in their
separate authority.

## Exact start and resume

`start()` accepts only a session ID and the expected `FINALIZED` revision. It
uses `DurableFinalizationService.replay()` to reload and verify the exact
UsageDecision, InjectionArtifact, complete replay manifest, and injection
bytes. It never rerenders and never calls a model. Only then does it CAS the
session to `EXECUTING`.

The execution transition inherits the finalization lease. It cannot silently
replace the lease. A live exact retry returns the same retained snippet and
executing revision. A retry after an exact terminal transition returns no
snippet and sets `execution_required=false`, preventing a completed or
abandoned run from being executed again through this result.

`resume()` is the explicit crash-recovery path. It requires the exact current
executing revision, verifies the same retained injection again, and renews the
lease by CAS before returning the snippet. External execution cannot be
transactional with the database; executors must use the stable GateSession
`run_id` as their idempotency key and must not interpret a stale lease as
permission to repeat a side effect.

## Completion and evaluator authentication

Completion accepts the existing bounded `GateCompletionRequest`. The service
invokes its trusted `OutcomeEvaluatorAuthenticator` on every call. That
server-owned boundary must validate the live transport proof and return the
current `TrustedOutcomeEvaluator` registration. The returned evaluator,
authenticator, credential, status, and version must match the authenticated
context and request. IDs are provenance, not authentication proof;
caller-supplied `evaluator_id` or credential text alone is insufficient.
Reloading through the authenticator on every call makes revocation and
credential rotation effective without reconstructing the service.

The completion authority then performs one atomic write:

1. derive and insert the content-addressed RunOutcome from the executing
   session and measured result;
2. CAS `EXECUTING -> COMPLETED` with the exact outcome ID;
3. append the immutable completion event and initial delivery revision; and
4. read back and verify the session, outcome, event, and current delivery.

Exact retries return the retained outcome and event without creating a second
event. Delivery remains at-least-once: consumers deduplicate by event ID, and
the bounded outbox worker owns lease, retry, acknowledgement, and dead-letter
transitions.

## Abandonment and recovery

`abandon()` requires the exact live executing revision and a bounded terminal
reason. It CAS-appends `ABANDONED`, reads it back, and supports exact retry.
An expired execution lease is not silently abandoned or completed. Existing
due scans report `recovery_required`; an operator or future policy-specific
recovery authority must decide the next action.

## Transaction and integration boundaries

SQLite and PostgreSQL use the same storage-neutral composition. When the
GateSession and completion-outbox repositories share a caller-owned database
connection, their savepoints preserve caller-controlled outer commit or
rollback for each individual start or completion operation. No database
transaction can include an external executor side effect, so a crash between
execution and completion leaves an explicit `EXECUTING` recovery state.

This service is opt-in. The default Store, LocalAgentMemory, STDIO MCP, HTTP,
and SDK adapters do not call it, and the v1 process-local request-token
contract is unchanged. Protected-content encryption, retention, replay-read
authorization, transport authentication, active adapter wiring, and a durable
transition-authorization linkage field remain separate production work.
