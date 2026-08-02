# Outcome and Effect events v1

**English** | [简体中文](outcome-effect-events-v1.zh-CN.md)

`outcome_effect_event_v1.py` is the F2-05 event adapter and deterministic
projection boundary for completed-run outcomes and external-effect delivery.
F2-06 selects that boundary in `tbmd local`; F2-07 selects the same command
coordinator for standalone SQLite durable HTTP/MCP and their Python/TypeScript
clients. The standalone PostgreSQL path has not made the equivalent cutover.

## Event stream

One `outcome_effect` stream is derived from each exact `GateSession.session_id`.
The canonical event types are:

- `tbm.execution.run_outcome_recorded`;
- `tbm.execution.outcome_attribution_recorded`;
- `tbm.effect.requested`;
- `tbm.effect.started`;
- `tbm.effect.retry_scheduled`;
- `tbm.effect.succeeded`;
- `tbm.effect.dead_lettered`;
- `tbm.effect.compensation_requested`;
- `tbm.effect.compensated`.

The sealed event registry is distributed as
`examples/outcome_effect_event_type_registry_v1.example.json`; its dispatch
schema is
`schemas/outcome_effect_event_payload_registry_v1.schema.json`.

## Six deterministic projections

The module supplies versioned pure reducers for:

1. the immutable `RunOutcome` current record;
2. the ordered immutable `OutcomeAttribution` set;
3. `EffectQueue` current state;
4. append-only delivery history;
5. dead-letter state;
6. explicit compensation history.

`hydrate_outcome_effect_views` reconstructs the existing `RunOutcome`,
`OutcomeAttribution`, and `CompletionOutboxDelivery` domain records and checks
their content identifiers, record digests, session links, contiguous delivery
versions, and current-head equality.

## Completion-outbox mapping

The existing completion outbox is mapped without inventing stronger delivery
claims:

| Completion delivery | Effect event | Queue state |
|---|---|---|
| `pending` | `EffectRequested` | `ready` |
| `leased` | `EffectStarted` | `leased` |
| `retry_wait` | `EffectRetryScheduled` | `retry` |
| `delivered` | `EffectSucceeded` | `succeeded` |
| `dead_letter` | `EffectDeadLettered` | `dead_letter` |

The mapping remains at-least-once. A response digest is retained when the
current authority has one, but it is not promoted into a provider receipt or
an exactly-once guarantee. The full provider-request/receipt protocol remains
F3-04 work.

## Compensation boundary

Compensation is a new effect and never rewrites an earlier event. A
compensation request must name a distinct effect, reference one successful
effect that explicitly declared `compensation_supported=true`, and complete
against the exact request. The legacy completion outbox declares no
compensation support, so its deliveries cannot be relabelled as compensated.

This projection is unrelated to `RecoveryAction`: operational recovery and
external-effect compensation remain separate evidence domains.

## Failure behavior

Reducers fail closed on a broken event parent chain, mismatched stream/session,
duplicate outcomes or attributions, non-contiguous delivery revisions, illegal
terminal transitions, unsupported compensation, digest drift, or malformed
retained JSON. Outcome association is never promoted to causal attribution;
the existing `OutcomeAttribution` contract remains authoritative.

## `tbmd local` event-first transaction

`tbmd local` explicitly opens the SQLite runtime with
`event_first_commands=true`. Each dispatcher command is serialized and runs in
one outer `BEGIN IMMEDIATE` transaction. Typed request and trusted-context
validation happens before any write. Domain event batches are appended before
the corresponding legacy completion or delivery projection, all six critical
views are rebuilt and checked, and the transaction commits only after the
wire response has been constructed. A validation, append, reducer, projection,
read-back, or response-construction failure rolls back the whole command.
If commit fails, rollback is retried; an unrecoverable rollback invalidates and
closes the SQLite connection so a later command cannot reuse ambiguous state.
Base-exception cleanup also releases the process lock.

Completion appends `RunOutcomeRecorded` and the initial `EffectRequested`
before the completed GateSession, RunOutcome, and outbox rows are projected.
Claim, retry, acknowledgement, and dead-letter transitions append and rebuild
their exact Effect event before the delivery head advances. Worker delivery
does not hold a database transaction across the external consumer call: claim
and terminal acknowledgement/failure remain separate, short, same-transaction
event-plus-projection operations, preserving the existing at-least-once model.

The default programmatic SQLite factory remains opt-in through the explicit
`event_first_commands` parameter. `tbmd local`, standalone SQLite durable HTTP,
and standalone SQLite durable MCP select it explicitly; unrelated direct
Python compositions do not change behavior implicitly.

## Current boundary

F2-05 provides ledger-ready event drafts, a sealed registry, deterministic
batch materialization, six reducers, and exact legacy-view hydration. F2-06
activates the same-transaction path for `tbmd local`; F2-07 extends that
selection to standalone SQLite durable HTTP/MCP, Python sync/async, and
TypeScript, with one committed event-sequence and projection-digest golden.
Compatibility paths and standalone PostgreSQL durable transports have not cut
over, so the aggregate `persistence_model` remains `authority_graph` and
`full_persistence` remains `false` until all remaining full-plan gates are
complete.
