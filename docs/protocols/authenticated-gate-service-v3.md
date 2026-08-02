# Authenticated durable Gate preparation

**English** | [简体中文](authenticated-gate-service-v3.zh-CN.md)

`AuthenticatedGateSessionService` composes the authenticated retrieval
boundary with either the SQLite or PostgreSQL GateSession repository. It
establishes durable session identity before retrieval and advances the session
only after trusted version-3 evidence has been verified.

## Ordering and idempotency

For each request the coordinator:

1. completes authenticated authorization, durable decision append/read-back,
   registry rotation checks, and environment binding;
2. creates or resolves a scoped, idempotent `CREATED` GateSession using only
   the authorized canonical tenant, repository, principal, and client;
3. reads the create receipt back from durable storage;
4. treats an existing `PREPARED` or later exact session as replay-only without
   repeating the preparation callback, while an interrupted exact `CREATED`
   session remains resumable;
5. invokes preparation only after a new or resumed `CREATED` revision exists;
6. requires `PreparedGateEvidence` and a trusted evidence verifier for the
   referenced RetrievalSnapshot and SystemGateEvaluation; and
7. uses version-checked repository transition to publish `PREPARED` with a
   lease, then verifies that every immutable session field was preserved.

The callback receives only `AuthorizedRetrievalScope` and the public immutable
GateSession. It never receives or persists a private Store token.

A durable `CREATED` session is resumable, not replay-complete. A later attempt
must complete fresh authorization, reuse the exact idempotency/session
identity, and continue preparation. A durable `PREPARED` session is
replay-complete: the durable retrieval composition returns the retained
snapshot/evaluation and exact response without repeating discovery, evidence
writes, or the callback.

## Failure and recovery

Preparation or evidence verification failure attempts a version-checked
`CREATED -> CANCELED` compensation with the bounded reason
`prepare_failed`. An exact canceled receipt produces
`GatePreparationFailedError`. A concurrent revision, abnormal transition
receipt, or failed compensation produces
`GatePreparationRecoveryRequiredError` with the last readable durable session;
the coordinator never reconstructs a process-local request token.

Recovery of an interrupted `CREATED` session starts only after fresh
authorization. Evidence committed by the interrupted attempt remains immutable;
if preparation runs again under a new authorization decision, the eventual
`PREPARED` revision links only the newly verified evidence and does not rewrite
the orphan.

This is ordered compensation, not a transaction spanning the authorization
and GateSession authorities. Default compatibility Agent/MCP still does not
emit the required RetrievalSnapshot and SystemGateEvaluation records. Opt-in
downstream services now provide durable retrieval preparation,
`AWAITING_DECISION`/`DECIDED`, bounded expiry recovery, event-first
finalization, execution, and completion authorities. Full transport/crash
conformance and shared-service cross-process operation remain later slices.
