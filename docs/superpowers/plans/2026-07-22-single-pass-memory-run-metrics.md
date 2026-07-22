# Single-Pass Memory-Run Metrics Plan

## Scope

Remove audit sorting and collection materialization from
`TraceBackedMemoryStore.memory_run_metrics()` without changing any public
count, status, action, ordering, or persistence behavior.

## Steps

1. Add deterministic coverage proving `memory_run_metrics()` traverses
   `_usage_logs` exactly once and does not sort it.
2. Extract one private usage-log-to-audit helper while preserving the existing
   audit status classifier and Trace lookup semantics.
3. Aggregate status and remediation counts directly in one usage-log loop with
   O(1) counters.
4. Retain decision-ID ordering for `memory_run_audits()` and
   `memory_run_remediations()`.
5. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 56.
6. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Keep all `MemoryRunMetrics` values and conservation identities unchanged.
- Keep public audit/remediation order and store lock boundaries unchanged.
- Preserve public APIs, snapshot version 2, every JSON Schema, active-lessons
  YAML, all packaged resources, PostgreSQL DDL, and PostgreSQL schema version 1.
