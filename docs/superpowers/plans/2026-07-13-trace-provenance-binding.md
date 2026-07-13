# Declared Trace Provenance Binding Implementation Plan

**Goal:** Ensure declared runtime provenance and persisted audit context agree
with the linked Trace without making omitted optional context fields strict.

**Constraints:** Keep public models/signatures and all persistence versions
unchanged; bind only fields with existing Trace evidence; perform validation
before any request/log state change.

## Task 1: Runtime RED tests

- Build source and final traces with branch, prompt, model, eval-suite, and tool
  provenance.
- Parameterize each scalar mismatch plus missing/extraneous exact tool names.
- Prove failed finalization writes no log and does not consume the request, then
  finalize successfully with a matching trace.
- Prove omitted optional context fields allow richer Trace provenance.
- Cover low-level `log_decision()` atomicity.

## Task 2: Import RED tests

- Mutate each declared optional field in a version-2 usage-log context and
  require snapshot rejection.
- Cover exact tool matching, ignored non-string tool names, optional field
  omission, and supplied legacy context evidence.
- Keep benchmark pair and reserved source-identity tests passing.

## Task 3: Shared implementation

- Define the declared scalar field set once.
- Extend `_validate_trace_context()` with declared-only matching and exact tool
  evidence.
- Extend `_validate_usage_log_trace()` with the same rule over present context
  keys.
- Reuse the existing exact tool-name helper; do not coerce values.
- Run Store and full tests, fixing only true compatibility issues.

## Task 4: Public contract and delivery

- Update README, architecture, usage policy, and roadmap Phase 14.
- Add executable/documentation contract tests and prove no persistence change.
- Run full verification and independent review, resolve every finding, fetch
  and rebase if required, merge to `main`, rerun tests, push, verify SHA, and
  clean the worktree/branch.
