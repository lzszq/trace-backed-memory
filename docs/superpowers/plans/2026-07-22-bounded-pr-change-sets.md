# Bounded PR Change Sets Plan

## Scope

Reject impossible PR change sets before entry scanning and make accepted
field-name validation linear without changing valid report behavior.

## Steps

1. Add Store tests proving seven entries fail before entry and PR case scans,
   plus an exact six-field acceptance test.
2. Add a CLI test proving oversized input returns structured input exit code 2
   without Git ancestry capture.
3. Derive the six-entry maximum from `PR_CHANGE_SET_FIELDS`, preflight tuple
   length, and replace per-field `list.count()` calls with one-pass sets.
4. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 59.
5. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Keep all one-through-six unique valid change sets and canonical sorting.
- Preserve unsupported-before-duplicate, endpoint, context-binding, ancestry,
  and report semantics within the valid cardinality.
- Keep legacy broad `changed_fields`, snapshot version 2, PostgreSQL schema
  version 1, and all packaged resources unchanged.
