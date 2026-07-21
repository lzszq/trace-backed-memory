# Non-Negative Trace Latency Plan

## Scope

Reject negative `latency_ms` consistently at the Store, Trace JSON Schema, CLI
delegation path, and fresh-install PostgreSQL boundary.

## Steps

1. Add failing runtime tests for record, snapshot, single completion, and
   all-or-nothing batch completion with negative latency.
2. Add failing scalar and manifest CLI tests that preserve Store-owned state
   error classification and leave `--write` snapshots unchanged.
3. Add failing JSON Schema, canonical/package parity, PostgreSQL DDL, and live
   database constraint tests while retaining zero and null.
4. Add one non-negative check to the shared Trace validator after the existing
   integer serialization guard.
5. Update both Trace Schema copies and both fresh-install PostgreSQL DDL copies.
6. Publish Phase 45 behavior and compatibility in README, architecture, usage
   policy, product, roadmap, and documentation contract tests.
7. Run focused and full tests, required PostgreSQL coverage, build and verify
   wheel/sdist artifacts, obtain independent review, merge, push, and require
   every CI job to pass.

## Compatibility

- `None`, zero, positive latency, omission, replay, and cost behavior stay
  unchanged; negative latency now fails deterministically.
- Public APIs, dependencies, snapshot version 2, active-lessons YAML, packaged
  resource names/count, and PostgreSQL schema version 1 do not change.
- Trace Schema and PostgreSQL DDL bytes change in canonical and packaged copies;
  the DDL remains a fresh-install baseline rather than an in-place migration.
