# Deferred Decision Outcome Sealing Design

## Problem

The primary runtime API currently records a memory decision and its optional
evaluation result in one `finalize_memory()` call. Real execution is ordered
differently: finalization returns the gated snippet, the caller executes the
task, and only then can an evaluator produce `pass`, `fail`, or `error`.

Callers can omit the result, but the store has no supported way to attach it
later. Reconstructing or editing a usage log would bypass store ownership and
conflict with PostgreSQL's immutable-row synchronization. This leaves genuine
production decisions permanently unevaluated or encourages unsupported audit
mutation.

## Public Lifecycle

Add this store-owned API:

```python
store.record_decision_outcome(
    decision_id,
    eval_result,
    memory_caused_failure=False,
)
```

`eval_result` must be a measured outcome: `pass`, `fail`, or `error`.
`None` and `unknown` remain valid unevaluated states when a decision is first
logged, but they cannot be used to seal an outcome.

The outcome pair is `(eval_result, memory_caused_failure)`:

- an unevaluated decision may transition once to a valid measured pair;
- exact replay of an already sealed pair is idempotent;
- a different result or attribution after sealing is rejected;
- `memory_caused_failure=True` still requires failed or errored use of at
  least one memory ID;
- an unknown decision ID is rejected.

Validation and replacement happen under the store lock. Every failure leaves
the existing usage log unchanged. The method returns a defensive copy of the
sealed log.

## Existing Runtime Compatibility

Keep `eval_result` and `memory_caused_failure` on `finalize_memory()` and
`log_decision()` for callers that already know the outcome. Such logs are
already sealed; an exact later replay succeeds and any conflicting replay
fails.

Metrics continue to derive from usage logs. They move automatically from the
unevaluated bucket to the appropriate evaluated bucket after sealing, without
new persisted metric fields.

## Persistence

The existing `MemoryUsageLog` fields already carry the outcome pair, so JSON
snapshots, JSON Schemas, active-lessons YAML, and PostgreSQL schema version 1
do not change.

PostgreSQL repository synchronization gains one controlled lifecycle update
for `memory_usage_decisions`:

- insert absent rows as before;
- treat exact rows as unchanged;
- update only `eval_result` and `memory_caused_failure` when the stored result
  is `NULL` or `unknown` and the incoming result is measured;
- reject changes to any other field, measured-result rewrites, attribution
  rewrites, unevaluated-to-unevaluated changes, and downgrades to unevaluated.

The existing schema lock and transaction make the read/compare/update sequence
atomic. A later conflict in the same sync rolls the outcome update back.

## Documentation Workflow

The executable README flow should reflect real chronology:

1. finalize the memory decision without an outcome;
2. execute or evaluate using the returned snippet;
3. seal the measured result by decision ID;
4. derive metrics or persist the store.

## Verification

Tests cover successful sealing from both unevaluated representations, metrics
movement, defensive copies, exact replay, conflict rejection, wrong-memory
invariants, unknown IDs, snapshot round trips, concurrent sealing, PostgreSQL
insert/update/load/idempotence, stale and conflicting sync rejection, and
transaction rollback.
