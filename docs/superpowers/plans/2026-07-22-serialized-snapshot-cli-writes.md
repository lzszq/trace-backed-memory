# Serialized Snapshot CLI Writes Plan

## Scope

Serialize each snapshot CLI `--write` read-modify-write transaction across
cooperating local processes without changing Store, snapshot, or command
payload contracts.

## Steps

1. Add deterministic tests for the load-through-save lock boundary, lock
   acquisition failure, bounded contention timeout, exception release,
   contender serialization, and lock-free dry-run/read-only commands.
2. Add a private canonical sidecar path helper and cross-platform advisory
   lock context manager using only the Python standard library.
3. Hold the lock before snapshot load through successful same-path atomic
   publication, then release it before stdout output. Bound cross-platform
   contention waits to 30 seconds and fail before load with exit code 4.
4. Preserve every existing input/state/write exit code, serialization-before-
   publication rule, and post-publication BrokenPipe success behavior.
5. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 57.
6. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Lock only snapshot mutation commands with explicit `--write`.
- Keep dry runs, read-only commands, lessons export, and resource export free
  of the snapshot mutation lock.
- Add no runtime dependency or persisted domain state; preserve snapshot
  version 2, PostgreSQL schema version 1, and all packaged resources.
