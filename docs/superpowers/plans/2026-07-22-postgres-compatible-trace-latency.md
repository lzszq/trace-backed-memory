# PostgreSQL-Compatible Trace Latency Plan

## Scope

Align Store and portable Trace Schema latency with the existing PostgreSQL
signed-`INTEGER` range without changing the database column or schema version.

## Steps

1. Add failing Store tests for the inclusive 2,147,483,647 boundary and
   rejection of 2,147,483,648 across record, snapshot, and atomic batch paths.
2. Add failing execution and scalar/manifest CLI tests that preserve pending
   state, structured state errors, exit code 3, and unchanged snapshot bytes.
3. Add failing canonical/package Trace Schema tests for `maximum: 2147483647`
   and extend live PostgreSQL coverage across both upper-bound values.
4. Add one shared maximum constant and one upper-bound condition to the Trace
   validator after existing type, JSON serialization, and non-negative checks.
5. Update both Trace Schema copies; leave both PostgreSQL DDL copies unchanged.
6. Publish Phase 47 behavior and compatibility in README, architecture, usage
   policy, product, roadmap, and documentation contract tests.
7. Run focused and full tests, build and verify wheel/sdist artifacts, obtain
   independent review, merge, push, and require every CI job to pass.

## Compatibility

- `None`, zero, values through 2,147,483,647, omission, replay, cost behavior,
  and huge-integer error priority remain unchanged.
- Values above the maximum become invalid in Store and snapshot paths instead
  of failing later during PostgreSQL synchronization.
- Public APIs, dependencies, snapshot version 2, active-lessons YAML, packaged
  resource names/count, PostgreSQL DDL, and PostgreSQL schema version 1 do not
  change. Only canonical and packaged Trace Schema bytes change.
