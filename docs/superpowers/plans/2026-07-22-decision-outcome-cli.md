# Deferred Decision Outcome CLI Plan

## Scope

Expose the existing single-decision sealing transition through both installed
CLI entry points without changing the Store or persistence schemas.

## Steps

1. Add `outcome` parser arguments with a required measured result, explicit
   boolean attribution, dry-run default, and optional atomic write.
2. Capture the previous outcome pair, call `record_decision_outcome()` exactly
   once, and build the minimal non-sensitive result object.
3. Add success, replay, state/input failure, injected failure, BrokenPipe, and
   module-entry tests.
4. Add independent wheel and sdist smoke using separate snapshots.
5. Publish the command and Phase 38 contract in README, architecture,
   usage-policy, product, and roadmap documents.
6. Run focused and full tests, distribution verification, installed smoke,
   compatibility checks, whole-branch review, and remote CI.

## Compatibility

- No Store, model, PostgreSQL adapter, SQL, Schema, or resource change.
- No command or outcome wrapper is persisted.
- Snapshot version remains 2.
- PostgreSQL schema version remains 1.
- Existing `complete`, `recover`, and direct Python lifecycle behavior remain
  unchanged.

