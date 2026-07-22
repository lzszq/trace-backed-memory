# Public Snapshot Write Lock Implementation Plan

1. Add failing public API tests for export, timeout validation, canonical alias
   contention, exception recovery, and cross-process read-modify-write safety.
2. Extract the cross-platform backend to `locking.py`, add the public context
   manager, and retain CLI private wrappers with the existing timeout behavior.
3. Run focused lock and CLI tests, then update README, architecture, usage
   policy, product status, roadmap, API examples, and executable doc contracts.
4. Run the full suite, build and verify distributions, and smoke-test the
   installed wheel with a direct public locking transaction.
5. Independently review, merge into `main`, push, wait for every CI job, clean
   the worktree, and continue the next audit.
