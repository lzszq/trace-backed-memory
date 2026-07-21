# Indexed Run-To-Trace Lookup Plan

## Scope

Remove repeated full Trace scans from live usage-decision logging without
changing run-ID ambiguity or persisted data.

## Steps

1. Add regressions for unknown, unique, and ambiguous run-ID lookup, including
   a deterministic assertion that live decision logging does not iterate Trace
   history.
2. Add the private run-to-Trace-ID index and maintain it only through
   `record_trace()` with rollback on index failure.
3. Prove duplicate Trace-ID failures do not pollute a different run bucket and
   snapshot reconstruction rebuilds both unique and ambiguous lookup behavior.
4. Add concurrent Trace recording and decision logging coverage under the
   existing Store lock.
5. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 53.
6. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Preserve the exact 0/1/many run-ID lookup contract and duplicate Trace-ID
  validation precedence.
- Keep the derived index private and nonserialized.
- Preserve public signatures, dependencies, snapshot version 2, every JSON
  Schema, active-lessons YAML, all packaged resources, PostgreSQL DDL, and
  PostgreSQL schema version 1.
