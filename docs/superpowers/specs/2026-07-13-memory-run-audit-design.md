# Memory Run Audit Design

## Problem

`complete_memory_run()` prevents new half-completed audit state in the normal
workflow, while `complete_trace()` and `record_decision_outcome()` deliberately
remain available for separately owned lifecycles and legacy recovery. The store
therefore permits valid records in which neither side is measured, only one
side is measured, or both sides carry conflicting historical results.

Aggregate metrics expose unevaluated decision counts but do not identify the
linked `trace_id` and `decision_id` that need attention. Callers currently have
to join defensive copies of both public collections and reproduce the measured
outcome boundary themselves.

## Public Model And API

Add a frozen `MemoryRunAudit` model and export it from the package root. Each
record contains:

- `decision_id`;
- `trace_id`;
- `run_id`;
- `status`;
- the raw `trace_eval_result`;
- the raw `decision_eval_result`;
- `memory_caused_failure` from the validated usage log.

`TraceBackedMemoryStore.memory_run_audits()` returns an immutable tuple with
one record for every usage decision, sorted by `decision_id`. Traces without a
memory decision are not memory runs and do not produce audit rows. Multiple
decisions linked to one Trace remain separate audit rows.

## Status Classification

`pass`, `fail`, and `error` are measured. Trace `unknown` and decision `unknown`
or `None` are unevaluated.

| Trace | Decision | Status |
|---|---|---|
| unevaluated | unevaluated | `pending` |
| measured | unevaluated | `trace_only` |
| unevaluated | measured | `decision_only` |
| measured | same measured result | `complete` |
| measured | different measured result | `conflict` |

`trace_only` and `decision_only` identify the two supported partial-recovery
directions for `complete_memory_run()`. `pending` still needs a measured
result. `conflict` requires caller review because neither historical result is
silently authoritative.

## Integrity And Concurrency

The audit method runs under the store lock and reads private validated records,
so its result describes one consistent in-memory instant. Store and import
validation already require every usage log to reference an existing Trace with
the same run ID and context provenance. Missing or malformed linkage therefore
remains a load error rather than a synthetic audit status.

The method is observational only. It does not consume gate requests, alter
Trace or usage records, update metrics, or perform automatic recovery. Frozen
records with scalar fields and an immutable tuple keep the return value isolated
from store state.

## Persistence Boundary

`MemoryRunAudit` is derived from existing Trace and usage-log fields. It is not
serialized to snapshots, active-lessons YAML, JSON Schemas, or PostgreSQL.
Snapshot version 2 and PostgreSQL schema version 1 remain unchanged. A store
loaded from either persistence backend must reproduce the same audit tuple.

## Verification

Tests cover an empty store, all five states, stable ordering, public export,
frozen values, multiple decisions per Trace, no mutation, snapshot round trips,
PostgreSQL load parity, and unchanged persistence schemas.
