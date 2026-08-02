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
   and after rendering;
7. appends and reads back `tbm.usage_decision.finalized`, publishes the
   `FINALIZED` revision, then appends and reads back
   `tbm.injection.rendered`;
8. stores and reads back the exact replay projections in the same event-first
   transaction; and
9. rechecks the retained result and current authorization.

An idempotent retry may present an expected revision exactly one below the
current `DECIDED` revision only when durable history proves that the sole
intervening revision is a still-live lease renewal preserving every decided
field. The retained claim's trusted `updated_at` is the bundle timestamp, so
concurrent duplicates and crash retries rebuild the same content-addressed
UsageDecision, injection, and manifest. Any other stale revision, missing
history, expired lease, or non-monotonic renewal fails closed.

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

Compatibility recovery after a committed bundle write uses the same retained
lease revision and therefore reconstructs the byte-identical bundle before
retrying CAS. It does not mint a new timestamp or leave a second divergent
manifest. Event-first recovery likewise keeps the committed lease revision and
rebuilds the same bundle after the failed transaction has rolled back.

## Transaction boundary

Explicit durable SQLite/PostgreSQL runtimes enable the replay repository's
`store_complete_finalization()` path. The repository opens one outer unit that
reads the exact decided-event head, appends the finalized transition and
rendered-injection events, writes the GateSession/replay projections, and
performs exact read-back. Any event, transition, projection, or read-back
failure rolls back all finalization writes. The lease renewal committed by the
earlier claim is intentionally not rolled back.

A SQLite subprocess `SIGKILL` after the replay-manifest insert but before
transaction commit demonstrates that the replay rows, finalization events, and
session projection roll back together. Reopen observes only the earlier live
lease claim; retry reconstructs the same bundle and publishes one logical
finalization.

PostgreSQL preserves the fixed order `event-ledger schema/global lock → replay
schema validation → GateSession transition/session-head locks`. A caller-owned
outer transaction remains caller-owned. Compatibility repositories that do not
explicitly enable event-first mode retain the earlier
`store_complete_bundle()` then GateSession-CAS recovery boundary.

## Integration boundary

This service is opt-in. `AuthenticatedDurableAgentMemory` calls it as the
finalization stage of the shared durable facade. The active Store and default
compatibility Agent/MCP/HTTP adapters do not construct that facade; explicit
durable HTTP and trusted-local MCP profiles do, and Python/TypeScript durable
clients select the HTTP profile.
It does not advance `EXECUTING` or `COMPLETED`, emit RunOutcome, provide
Review Console behavior or implement retention/encryption. It does not change
the process-local Gate of the default compatibility MCP profile.
The separate opt-in
[durable execution composition](durable-execution-v3.md) consumes its
authenticated exact replay boundary. See also
[authenticated durable Agent v3](durable-agent-v3.md),
[finalization event v1](finalization-event-v1.md), and
[ledger replay export v1](ledger-replay-export-v1.md).
