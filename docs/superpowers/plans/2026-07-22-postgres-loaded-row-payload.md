# PostgreSQL Loaded-Row Payload Implementation Plan

1. Add red SQL-shape and real PostgreSQL tests that compare full physical-row
   bytes with the actual loaded-row projection for all five tables.
2. Exclude `updated_at` in exactly the failure-case, lesson, and project-policy
   payload branches while retaining the one-query prefetch boundary.
3. Run focused repository tests, then update README, architecture, usage policy,
   product status, roadmap, and executable documentation contracts for Phase 66.
4. Run the complete suite, compile sources, build and verify distributions, and
   smoke-test the installed wheel without changing resources or schemas.
5. Independently review, merge into `main`, push, wait for every CI job, clean
   the worktree, and continue the next audit.
