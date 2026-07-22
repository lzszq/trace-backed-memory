# Bounded Semantic Top-K Implementation Plan

1. Add failing tests for zero catalog-view construction outside semantic mode,
   membership-only semantic ID validation, and generator-backed bounded top-k.
2. Replace the eager stored-ID union with a conditional `ChainMap` view and
   replace full semantic list sorting with `heapq.nsmallest()` over a generator.
3. Run focused retrieval tests and update the original semantic design, README,
   architecture, usage policy, product status, roadmap, and executable docs.
4. Run the full suite, build and verify distributions, and smoke-test the wheel
   in an isolated environment.
5. Independently review, merge into `main`, push, wait for all remote CI jobs,
   clean the worktree, and continue the next audit.
