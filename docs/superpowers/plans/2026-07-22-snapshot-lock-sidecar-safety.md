# Snapshot Lock Sidecar Safety Plan

1. Add red public-helper tests for symbolic-link and hard-linked `.tbm.lock`
   sidecars, asserting that the target bytes remain unchanged.
2. Add a CLI test proving unsafe sidecars fail with write exit code 4 before
   snapshot loading or mutation.
3. Introduce a dependency-free sidecar opener that uses exclusive creation or
   pre/open/post identity validation and closes descriptors on all failures.
4. Route `snapshot_write_lock()` through the safe opener while retaining the
   existing initializer and platform lock backend.
5. Publish the single-link regular-file requirement in README, architecture,
   usage policy, product status, roadmap, and the Phase 64 design addendum.
6. Run focused tests, complete pytest, compile, build/distribution validation,
   isolated installation smoke tests, independent review, merge, push, and all
   remote CI jobs.
