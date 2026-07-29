# Durable finalization v3

**English** | [简体中文](durable-finalization-v3.zh-CN.md)

`DurableFinalizationService` is the opt-in internal composition that advances
one durable GateSession from `DECIDED` to `FINALIZED`. It rechecks live
authorization, active revision heads, and policy; renders only the final
allowed set; retains a complete replay bundle; and publishes the exact
UsageDecision and InjectionArtifact linkage through GateSession CAS.

## Preconditions and order

The caller supplies an authenticated service context, the exact authorized
retrieval scope, and a `DurableFinalizationRequest` containing only the
session ID, expected revision, and bounded lease request. It cannot supply a
snippet, candidate set, policy, renderer, decision, or artifact identity.

For a fresh decided session, the service:

1. verifies the current retrieval authorization and exact session scope;
2. checks the expected GateSession revision and renews its decision lease by
   compare-and-swap;
3. reloads the exact RetrievalSnapshot, System Gate evaluation, complete
   Semantic Gate attempt chain, and successful prompt/response artifacts;
4. requires the snapshot authorization event to equal the current authorized
   scope, including when the final allowed set is empty;
5. loads only the allowed current ActivatedRevision candidates and verifies
   their revision and candidate hashes;
6. rechecks current authorization, active heads, and policy immediately before
   and after rendering, and once more after bundle retention;
7. stores and reads back the exact replay bundle; and
8. compare-and-swap publishes and reads back `FINALIZED`.

System Gate blocks remain monotonic because the complete Semantic Gate chain is
verified before any render, and UsageDecision separately retains every
deterministic block reason and rule.

## Rendering and retained bundle

The v1 renderer emits canonical JSON data with an explicit notice that quoted
memory is evidence, not executable instructions. It preserves snapshot order,
caps each item, caps the item count, and caps the total snippet. `none` emits
an empty snippet and an empty final set. `summary` and `full` use different
per-item limits but the same deterministic envelope.

Only `public` and `internal` UTF-8 memory bytes are supported by this plaintext
composition. Confidential or restricted content fails closed until an
encrypted finalization/replay authority exists.

Before `FINALIZED`, the replay authority atomically retains the UsageDecision,
RetrievalSnapshot, System Gate evaluation, exact Semantic Gate prompt and
response, ancestry commitment, policy bundle, renderer descriptor, exact
injection bytes, InjectionArtifact, and complete DecisionReplayManifest. Every
component is content-addressed and read back byte-for-byte.

## Replay and recovery

An exact retry against a finalized session derives the UsageDecision artifact
from the stored usage ID, reparses its content-derived identity, reloads every
replay component, verifies snapshot/evaluation/policy/renderer/injection
linkage, reconstructs the manifest hash, verifies the exact injection bytes,
and rereads the unchanged GateSession. It does not rerender or call a model.

The authenticated `replay()` boundary applies the same verification to
`FINALIZED`, `EXECUTING`, `COMPLETED`, or `ABANDONED` revisions that still
carry the exact finalization linkage. It exists for the durable execution
composition; callers must not bypass it with raw replay-repository reads.

If the replay bundle was retained but the GateSession CAS cannot be confirmed,
the service retries only the exact finalization transition when safe. Otherwise
it raises `DurableFinalizationRecoveryRequiredError` with the retained
UsageDecision and InjectionArtifact when available. It never deletes immutable
evidence or creates a new final decision silently.

## Transaction boundary

Across independent authorities, finalization is ordered recovery rather than a
distributed transaction. A retained replay bundle may require an explicit
GateSession recovery transition.

When SQLite or PostgreSQL GateSession, evidence, Semantic Gate artifact, and
replay repositories deliberately share one caller-owned connection, the
caller can wrap finalization in an outer transaction. Repository savepoints
then allow the caller to commit or roll back the renewed lease, complete
bundle, and `FINALIZED` revision together. The service does not open or own
that outer transaction.

## Integration boundary

This service is opt-in and is not called by the active Store, local Agent,
STDIO MCP, HTTP, or SDK adapters. It does not advance `EXECUTING` or
`COMPLETED`, emit RunOutcome, provide Review Console behavior, implement
retention/encryption, or make the process-local active MCP Gate durable.
The separate opt-in
[durable execution composition](durable-execution-v3.md) consumes its
authenticated exact replay boundary.
