# Single-Pass Memory-Run Metrics Design

## Problem

`TraceBackedMemoryStore.memory_run_metrics()` currently calls
`memory_run_audits()`. That public audit API sorts every usage decision by
`decision_id` and materializes the complete audit tuple before metrics count
the five statuses and remediation actions.

The ordering is part of the useful audit and remediation presentation
contract, but it is not observable in the frozen aggregate metrics value. The
current metrics path therefore pays O(u log u) sorting time and O(u) retained
space for a result that only needs counts, where `u` is the usage-log count.

## Design

Extract construction of one `MemoryRunAudit` from one usage log into a private
store helper. The helper resolves the linked Trace and delegates status
classification to the existing `_memory_run_audit_status()` function.

Keep `memory_run_audits()` responsible for sorting usage logs by `decision_id`,
then map the single-log helper over that ordered input. Keep
`memory_run_remediations()` based on the ordered public audit view, so both
public tuple APIs retain their existing order.

Change `memory_run_metrics()` to iterate `_usage_logs` directly once. For each
log it builds one transient audit, derives the action through the existing
`_memory_run_remediation()` function, and updates scalar status and recovery
counters. No audit collection is retained and no ordering operation is used.

## Semantics

The five statuses remain mutually exclusive and exhaustive:

- `pending`: neither side has a measured result;
- `trace_only`: only the Trace has a measured result;
- `decision_only`: only the usage decision has a measured result;
- `complete`: both measured results agree;
- `conflict`: both measured results disagree.

Recovery counts continue to come from remediation actions. `recover` is
automatically recoverable, while `recover_with_attribution` requires caller
input. Their sum remains `recoverable_count`.

## Complexity

The metrics path performs one O(u) usage-log traversal with O(1) accumulator
space. It still performs one O(1) Trace lookup per decision and creates only
transient per-record derived values. Ordered audits and remediations remain
O(u log u), because their public order is intentionally preserved.

## Compatibility

The store lock continues to cover the complete point-in-time derivation. No
public signature, model field, error, ordering contract, dependency, snapshot
field, JSON Schema, active-lessons YAML, packaged resource, PostgreSQL DDL, or
schema version changes. Snapshot version remains 2 and PostgreSQL schema
version remains 1.
