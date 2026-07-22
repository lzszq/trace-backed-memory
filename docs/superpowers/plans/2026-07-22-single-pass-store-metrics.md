# Single-Pass Store Metrics Plan

## Scope

Collapse repeated usage-log traversals inside `TraceBackedMemoryStore.metrics()`
without changing any reported value or adjacent metrics API.

## Steps

1. Add deterministic iteration-count coverage proving `metrics()` traverses
   `_usage_logs` exactly once.
2. Add a measured `error` attribution regression for cohort, pass-rate, and
   wrong-memory semantics.
3. Replace the repeated comprehensions and result lists with scalar counters in
   one usage-log loop.
4. Preserve empty-cohort `None`, nonempty zero-pass `0.0`, lesson confidence,
   obsolete candidate-status counting, and conservation identities.
5. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 55.
6. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Keep `memory_outcome_metrics()`, memory-run metrics, and CLI lock boundaries
  unchanged.
- Preserve public APIs, snapshot version 2, every JSON Schema, active-lessons
  YAML, all packaged resources, PostgreSQL DDL, and PostgreSQL schema version 1.
