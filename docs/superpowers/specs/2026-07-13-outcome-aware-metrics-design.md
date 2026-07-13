# Outcome-aware Metrics Design

## Problem

`TraceBackedMemoryStore.metrics()` currently includes every non-null
`eval_result` in pass-rate denominators. The persisted enum also contains
`unknown`, so an explicitly unknown outcome is silently counted as a failed
evaluation while a missing outcome (`None`) is excluded. This biases pass rates
and exposes no denominator counts for callers to detect sparse outcome data.

## Decision

Define evaluated outcomes as exactly `pass`, `fail`, and `error`:

- `pass` contributes one pass and one evaluated decision;
- `fail` and `error` contribute one non-pass and one evaluated decision;
- `unknown` and `None` contribute no pass-rate observation;
- every `unknown` or `None` decision contributes to one shared unevaluated
  decision count.

Append these defaulted fields to `MemoryMetrics`:

```python
evaluated_with_memory_count: int = 0
evaluated_without_memory_count: int = 0
unevaluated_decision_count: int = 0
```

The existing `pass_rate_with_memory` and `pass_rate_without_memory` fields keep
their positions and types. A rate remains `None` when its corresponding
evaluated count is zero.

## Compatibility

- `TraceBackedMemoryStore.metrics()` remains a zero-argument method.
- Existing `MemoryMetrics` positional fields do not move; new fields are
  appended with defaults.
- Candidate, used, blocked, obsolete-attempt, confidence, and wrong-memory
  counts keep their current definitions.
- `MemoryUsageLog`, snapshots, JSON Schemas, active-lessons YAML, and
  PostgreSQL schema version 1 do not change.
- Metrics remain a derived in-memory view and are not persisted.

## Audit Meaning

The new fields are decision counts, not per-memory causal attribution. A
decision is classified as "with memory" when `used_memory_ids` is non-empty,
matching the existing rate split. `error` is a measured non-pass outcome;
`unknown` is absence of a usable outcome, not a failure.

The invariant is:

```text
decision_count
= evaluated_with_memory_count
+ evaluated_without_memory_count
+ unevaluated_decision_count
```

## Verification

Tests cover:

- pass/fail/error denominators;
- `unknown` and `None` exclusion from both rates;
- correct evaluated and unevaluated counts;
- zero-observation rates remaining `None`;
- appended dataclass positional compatibility;
- snapshot and PostgreSQL contracts remaining unchanged;
- executable README usage and Phase 12 documentation.
