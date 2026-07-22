# Linear Legacy PR Warnings Plan

## Scope

Normalize permissive legacy PR warning fields once and make stable report
deduplication linear without changing any report output or accepted input.

## Steps

1. Add a Store regression test with repeated supported and unknown fields,
   exact first-occurrence warning order, and instrumented warning-call bounds.
2. Add a one-pass legacy field validator that retains at most the seven
   supported warning names while preserving all validation behavior.
3. Replace stable list-membership deduplication with an ordered result plus a
   `seen` set.
4. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 60.
5. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Keep empty, duplicate, unknown, whitespace-containing, and `model_family`
  legacy strings accepted exactly as before.
- Preserve first-occurrence warning order, warning text, context matching,
  ancestry filtering, suggestions, related cases, and provenance.
- Keep exact `PRChangeSet`, snapshot version 2, PostgreSQL schema version 1,
  and all packaged resources unchanged.
