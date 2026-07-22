# Git Metadata Output Validation Plan

1. Add red injected-runner tests for blank `HEAD`/repository root and
   non-string results from all four metadata commands.
2. Add boundary tests for 512-character commit, branch, and repository names,
   plus overflow, detached HEAD, clean status, and filesystem-root repositories.
3. Validate runner result types inside `_run_git()` and normalize/validate each
   metadata field before constructing `TraceMetadata`.
4. Preserve command order and short-circuit on the first malformed result.
5. Update README, architecture, usage policy, product status, roadmap, and the
   bounded Git capture design addendum; assert snapshot/schema/resource parity.
6. Run independent review, complete tests, compile/build/distribution and clean
   wheel smoke tests, then merge, push, and verify every remote CI job.
