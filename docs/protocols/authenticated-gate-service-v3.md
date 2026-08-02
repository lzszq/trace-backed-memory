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
4. refuses an existing exact session without repeating the preparation
   callback;
5. invokes preparation only after the new `CREATED` revision exists;
6. requires `PreparedGateEvidence` and a trusted evidence verifier for the
   referenced RetrievalSnapshot and SystemGateEvaluation; and
7. uses version-checked repository transition to publish `PREPARED` with a
   lease, then verifies that every immutable session field was preserved.

The callback receives only `AuthorizedRetrievalScope` and the public immutable
GateSession. It never receives or persists a private Store token.

## Failure and recovery

Preparation or evidence verification failure attempts a version-checked
`CREATED -> CANCELED` compensation with the bounded reason
`prepare_failed`. An exact canceled receipt produces
`GatePreparationFailedError`. A concurrent revision, abnormal transition
receipt, or failed compensation produces
`GatePreparationRecoveryRequiredError` with the last readable durable session;
the coordinator never reconstructs a process-local request token.

This is ordered compensation, not a transaction spanning the authorization
and GateSession authorities. Default compatibility Agent/MCP still does not
emit the required RetrievalSnapshot and SystemGateEvaluation records. Opt-in
downstream services now provide durable retrieval preparation,
`AWAITING_DECISION`/`DECIDED`, bounded expiry recovery, event-first
finalization, execution, and completion authorities. Full transport/crash
conformance and shared-service cross-process operation remain later slices.
