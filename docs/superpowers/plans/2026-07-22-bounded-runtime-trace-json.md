# Bounded Runtime Trace JSON Implementation Plan

1. Add failing Store tests for shared node/text budgets, exact boundaries,
   early wide-container rejection, UTF-8 measurement, and completion/import
   atomicity.
2. Add one shared Trace JSON budget to `_validate_trace()`, preflight container
   width before traversal-stack expansion, and preserve existing diagnostics.
3. Run focused Store/execution/PostgreSQL tests, then update README,
   architecture, usage policy, product status, roadmap, and executable doc
   contracts for Phase 65.
4. Run the complete suite, compile sources, build and verify distributions, and
   smoke-test the installed wheel against boundary acceptance and rejection.
5. Independently review, merge into `main`, push, wait for every CI job, clean
   the worktree, and continue the next audit.
