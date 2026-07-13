# Memory Run Remediation Plan Implementation Plan

## Goal

Turn the existing memory-run audit state into an immutable, actionable plan
without mutating or persisting derived remediation data.

## Model And API

- Export `MemoryRunRemediationAction` and frozen `MemoryRunRemediation`.
- Add `memory_run_remediations()` with one decision-sorted item per audit.
- Map pending runs to measurement, safe one-sided runs to recovery, failed or
  errored Trace-only runs to attributed recovery, conflicts to investigation,
  and complete runs to no action.
- Publish only results and attribution established by current records.

## Metrics

- Add defaulted `auto_recoverable_count` and
  `attribution_required_count` fields to `MemoryRunMetrics`.
- Keep five-state conservation and require both new counts to sum to
  `recoverable_count`.
- Share action classification between the plan and metrics.

## Persistence And Documentation

- Prove snapshot and PostgreSQL reloads reconstruct the same plan.
- Keep snapshot version 2, all JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1 unchanged.
- Document the API workflow, stale-plan boundary, architecture, usage policy,
  implemented API inventory, and roadmap Phase 23.

## Verification

- Run focused remediation, metrics, README, documentation, and PostgreSQL
  tests.
- Run the complete test suite and compile all source and tests.
- Check the diff, conflict markers, SQL hash, process cleanup, and worktree
  status before merge and push.
