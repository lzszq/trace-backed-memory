# Per-memory Outcome Metrics Design

## Problem

Global outcome-aware metrics expose whether memory use correlates with pass
rates, but they cannot show which stored memory IDs are repeatedly selected,
blocked, unused, or associated with measured non-passes. Operators therefore
cannot identify stale or risky memory without manually scanning usage logs.

## Public API

Add one frozen public model:

```python
@dataclass(frozen=True)
class MemoryOutcomeMetrics:
    memory_id: str
    candidate_count: int
    used_count: int
    blocked_count: int
    evaluated_use_count: int
    passed_use_count: int
    failed_or_errored_use_count: int
    unevaluated_use_count: int
    observed_pass_rate: float | None
```

Add a zero-argument store method:

```python
def memory_outcome_metrics(self) -> tuple[MemoryOutcomeMetrics, ...]:
```

The tuple contains every currently stored runtime memory ID across failure
cases, lessons, and project policies, sorted by `memory_id`. Unobserved memory
is included with zero counts and `observed_pass_rate=None`.

## Counting Semantics

For each usage log and memory ID:

- `candidate_count` increments when the ID appears in
  `candidate_memory_ids`;
- `used_count` increments when it appears in `used_memory_ids`;
- `blocked_count` increments when it appears in `blocked_memory_ids`, covering
  both deterministic and LLM-narrowing blocks recorded in the final decision;
- a used `pass` increments evaluated and passed use;
- a used `fail` or `error` increments evaluated and failed/errored use;
- a used `unknown` or `None` increments unevaluated use;
- outcomes on decisions where the memory was only a candidate or was blocked
  do not contribute to that memory's observed pass rate.

Store-generated values satisfy:

```text
used_count = evaluated_use_count + unevaluated_use_count
evaluated_use_count = passed_use_count + failed_or_errored_use_count
observed_pass_rate = passed_use_count / evaluated_use_count
```

The rate is `None` when `evaluated_use_count` is zero.

## Non-causal Boundary

These are observed usage associations, not causal effectiveness estimates. If
one decision uses multiple memory IDs, the same run outcome is associated with
each used ID. The API does not claim which item caused a pass, failure, or
error and does not derive per-item wrong-memory attribution from the log-level
`memory_caused_failure` flag.

## Compatibility

- Existing `metrics()` behavior and `MemoryMetrics` stay unchanged.
- The new model is exported from the package root.
- Usage logs remain the source of truth; no metric is persisted.
- Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
  version 1 remain unchanged.
- Snapshot and legacy-log restoration naturally reconstruct the same metrics
  from validated usage evidence.

## Verification

Tests cover stable ordering, all memory kinds, zero-observation values,
candidate/use/block counts, pass/fail/error/unknown/None classification,
invariants, multi-memory non-causal association, snapshot/legacy reconstruction,
immutability, package export, documentation, and persistence non-impact.
