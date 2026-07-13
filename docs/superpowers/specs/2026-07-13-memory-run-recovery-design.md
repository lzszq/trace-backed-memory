# Memory Run Recovery Design

## Problem

`memory_run_audits()` identifies pending, one-sided, complete, and conflicting
Trace/decision outcomes. Recovering a one-sided record still requires callers
to copy its `trace_id`, select the already measured result, preserve existing
failure attribution, and call `complete_memory_run()` correctly. Repeating
those correlated values creates another opportunity for a recovery script to
choose the wrong side or silently discard attribution.

## Public API

Add a high-level recovery method:

```python
completion = store.recover_memory_run(
    audit.decision_id,
    error="executor failed",
)
```

The method derives the linked Trace and measured result from validated store
state, accepts the same optional Trace completion evidence as
`complete_memory_run()`, and returns `MemoryRunCompletion`. It never accepts a
caller-supplied `trace_id` or `eval_result`.

## State Machine

Recovery uses the same five-state classification as `memory_run_audits()`:

- `pending`: reject because neither record contains a measured result;
- `trace_only`: derive `eval_result` from the Trace and seal the decision;
- `decision_only`: derive `eval_result` and `memory_caused_failure` from the
  decision and complete the Trace;
- `complete`: perform an idempotent exact replay;
- `conflict`: reject because neither measured result is silently authoritative.

For a `trace_only` pass, omitted failure attribution safely means `False` and
`True` remains invalid. For a failed or errored `trace_only` run, the caller
must explicitly pass `memory_caused_failure=True` or `False`; recovery never
guesses causal attribution. For `decision_only` and `complete`, omission
preserves the sealed value and an explicitly different value is rejected.

## Atomicity And Validation

`recover_memory_run()` runs under the store lock, looks up the decision and
linked Trace, classifies their current state, derives only the missing call
arguments, and delegates to `complete_memory_run()` under the same reentrant
lock. Existing candidate validation, exact replay, immutable Trace evidence,
decision attribution, defensive copies, and two-record assignment semantics
remain authoritative.

Unknown or invalid decision IDs, invalid attribution types, missing measured
results, conflicts, and completion-evidence rewrites leave both records
unchanged. Concurrent state changes cannot occur between classification and
completion.

## Persistence Boundary

Recovery changes only the existing Trace completion fields and decision outcome
pair. PostgreSQL synchronization uses its existing row-locked forward updates
and transaction. No model field, snapshot field, JSON Schema, YAML shape, SQL
column, or table changes. Snapshot version 2 and PostgreSQL schema version 1
remain unchanged.

## Verification

Tests cover both one-sided recovery directions, failure-attribution prompting,
sealed-attribution preservation, complete replay, pending and conflict
rejection, invalid IDs and types, immutable evidence conflicts, snapshot
atomicity, PostgreSQL synchronization, and documentation compatibility.
