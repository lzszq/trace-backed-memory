# Outcome-aware Metrics Implementation Plan

**Goal:** Prevent unknown outcomes from depressing memory pass rates and expose
the exact evaluated/unevaluated sample counts behind those rates.

**Constraints:** Keep the zero-argument `metrics()` API, append only defaulted
dataclass fields, add no dependency, and do not change any persisted model,
snapshot/schema version, JSON Schema, active-lessons YAML, or PostgreSQL DDL.

## Task 1: Contract tests

- Extend store metrics tests with with-memory and without-memory `unknown` and
  `None` decisions.
- Add an `error` observation to prove it remains an evaluated non-pass.
- Assert both rates, both evaluated denominator counts, the unevaluated count,
  and their sum against `decision_count`.
- Add a positional compatibility test for the pre-existing `MemoryMetrics`
  constructor shape.
- Run focused tests and capture RED.

## Task 2: Implementation

- Append `evaluated_with_memory_count`,
  `evaluated_without_memory_count`, and `unevaluated_decision_count` to
  `MemoryMetrics`, all defaulting to zero.
- In `metrics()`, partition logs into evaluated (`pass`, `fail`, `error`) and
  unevaluated (`unknown`, `None`) outcomes before computing rates.
- Preserve every existing aggregate definition.
- Run model/store tests and the full suite.

## Task 3: Public contract

- Update the executable README pipeline to assert denominator evidence.
- Document measured versus unknown outcomes in README, architecture, usage
  policy, and a new implemented Phase 12 roadmap section.
- Add documentation contract tests proving persistence versions and DDL remain
  unchanged.

## Task 4: Review and delivery

- Run focused and full verification, compileall, diff checks, SQL hash, and
  PostgreSQL cleanup checks.
- Review the whole branch for rate semantics, compatibility, and documentation
  accuracy; resolve every finding.
- Fetch/rebase if required, fast-forward `main`, rerun the full suite, push,
  verify the remote SHA, and clean the worktree/branch.
