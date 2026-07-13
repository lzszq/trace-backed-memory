# Memory Run Remediation Plan Design

## Problem

`memory_run_audits()` exposes pending, one-sided, complete, and conflicting
memory runs, while `recover_memory_run()` and `recover_memory_runs()` provide
safe repair operations. Callers still have to duplicate the state-to-action
policy between those APIs. In particular, a failed or errored `trace_only` run
cannot be recovered until a caller supplies causal attribution, while a passed
`trace_only` run and every `decision_only` run are immediately recoverable.

That distinction is operationally important but currently visible only by
reinterpreting raw audit fields or by attempting recovery and handling an
exception. The store should publish a derived, immutable remediation plan
without making a write, guessing attribution, or adding persisted state.

## Public Model And API

Export the action alias:

```python
MemoryRunRemediationAction = Literal[
    "measure",
    "recover",
    "recover_with_attribution",
    "investigate",
    "none",
]
```

Export a frozen `MemoryRunRemediation` with the audit identity and raw state:

- `decision_id`, `trace_id`, and `run_id`;
- `status`, `trace_eval_result`, `decision_eval_result`, and the usage log's
  raw `memory_caused_failure` value;
- `action`;
- `resolved_eval_result` and `resolved_memory_caused_failure`, which contain
  values safe to use for recovery only when the current records establish
  them.

`memory_run_remediations()` returns one item for every usage decision, sorted
by `decision_id` exactly like the audit view. The tuple and records are
immutable snapshots. Traces without a usage decision remain outside the
memory-run boundary, and multiple decisions linked to one Trace remain
separate items.

## Classification

| Audit status | Action | Resolved result | Resolved attribution |
|---|---|---|---|
| `pending` | `measure` | `None` | `None` |
| `trace_only` pass | `recover` | Trace result | `False` |
| `trace_only` fail/error | `recover_with_attribution` | Trace result | `None` |
| `decision_only` | `recover` | decision result | sealed decision value |
| `complete` | `none` | matching result | sealed decision value |
| `conflict` | `investigate` | `None` | `None` |

`recover_with_attribution` is intentionally distinct from `recover`: the
Trace result is known, but the store must not infer whether memory caused a
failed run. `investigate` never chooses a side of a conflict. `measure` means a
new evaluator result is still required and should later be supplied through
`complete_memory_run()` or `complete_memory_runs()`.

## Health Metrics

Extend frozen `MemoryRunMetrics` with defaulted fields:

- `auto_recoverable_count`: `decision_only` runs plus passed `trace_only`
  runs;
- `attribution_required_count`: failed or errored `trace_only` runs.

The invariant is:

```text
recoverable_count == auto_recoverable_count + attribution_required_count
```

The existing five-state conservation invariant remains unchanged. Defaults
preserve source compatibility for callers that directly construct the metrics
record with the Phase 20 fields.

## Consistency And Ownership

The method derives every item under the store lock from the same audit
snapshot. It does not invoke recovery, mutate records, or return references to
mutable store state. A helper maps only validated audit states to actions, so
the planner and metrics use the same classification rule.

Remediation data is advisory current-state evidence. A caller must still use
the write APIs, which revalidate state under the lock and reject stale plans.
The plan therefore does not weaken atomic completion or recovery semantics.

## Persistence Boundary

`MemoryRunRemediation`, its action, and the two additional metric counts are
derived and not persisted. Snapshot version 2, JSON Schemas, active-lessons
YAML, `schemas/postgres.sql`, and PostgreSQL schema version 1 remain unchanged.
Snapshot and PostgreSQL reloads reconstruct identical plans from Trace and
usage rows.

## Verification

Tests cover exports and frozen records, every action including both
`trace_only` branches, deterministic decision ordering, shared Trace decisions,
empty stores, metric conservation, stale-plan revalidation, snapshot and
PostgreSQL reconstruction, README execution, documentation compatibility, and
the unchanged persistence schemas.
