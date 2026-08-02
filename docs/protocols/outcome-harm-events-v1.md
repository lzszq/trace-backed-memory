# Outcome and harm events v1

Status: opt-in F4-07 projection over the shared event-ledger ports. It is not
part of the default Agent, MCP, HTTP, SDK, or Store path.

## Contract

`tbm.outcome-harm-event.v1` consumes the existing
`tbm.execution.run_outcome_recorded` and
`tbm.execution.outcome_attribution_recorded` events. It adds one repository-
authorized event, `tbm.outcome.evaluation_context_bound`, that binds a measured
outcome to its exact session, Trace, run, usage decision, replay manifest,
retrieval snapshot, injection artifact, memory revisions, evaluation case, and
optional experiment cohort.

The context event retains the exact authorization policy, request, decision,
and attestation-verifier identity. Replay recomputes the authorization decision,
requires `memory:verify`, binds the authenticated principal/client and exact
tenant/repository, rejects decisions made after the event, and requires the
attestation verifier to be in reducer configuration.

## Projection

The deterministic reducer emits:

- observed per-revision associations, kept explicitly non-causal;
- explicit with-Memory and without-Memory experiment cohorts;
- independently verified causal claims from the v3 attribution contract;
- harmful-memory signals when verified causal harmed claims satisfy the
  content-addressed integer threshold policy; and
- read-only suspension recommendations linked to those signals.

It also separates evaluated from unevaluated outcomes. An attribution without
an exact evaluation context remains retained evidence but cannot enter any
association, causal, harm, or suspension view. A controlled-experiment causal
claim requires a matching with-Memory cohort, and a without-Memory cohort may
not carry a memory attribution.

## Persistence and replay

SQLite and PostgreSQL use the existing `EventLedgerPort`; this protocol adds no
database tables. Rebuild freezes a partition-local global event watermark,
requires a complete four-classification view, uses the exact forward page
cursor, verifies every source stream, repeats the fixed-watermark scan, and
binds the snapshot to reducer code/configuration hashes and source-event count.
Reducer state contains canonical JSON strings rather than floating-point
values; confidence thresholds use integer millionths.

## Boundary

Signals and recommendations never mutate MemoryCatalog or an activated head.
An actual suspension remains an independently authorized MemoryCatalog command.
F5 migration, shadow comparison, default projection cutover, and compatibility
authority retirement remain separate work.

See also [Outcome v3](outcome-v3.md),
[Outcome/Effect events v1](outcome-effect-events-v1.md), and
[Retrieval index events v1](retrieval-index-events-v1.md).
