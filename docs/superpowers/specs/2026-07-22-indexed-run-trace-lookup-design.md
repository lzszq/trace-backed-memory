# Indexed Run-To-Trace Lookup Design

## Problem

`TraceBackedMemoryStore.log_decision()` resolves a run ID by scanning every
stored Trace. Repeated decisions therefore perform O(t) trace work per write,
where `t` is the total trace count, even though Trace identities are immutable
after insertion.

The existing lookup has three observable outcomes that must remain exact:

- no matching Trace raises `unknown run_id`;
- one matching Trace resolves successfully;
- more than one matching Trace raises `run_id does not resolve to one trace`.

Duplicate run IDs are currently valid, so a single-value mapping would change
behavior by silently selecting one Trace.

## Design

Add one private, derived field to `TraceBackedMemoryStore`:

- `_trace_ids_by_run_id: dict[str, list[str]]` maps each run ID to the ordered
  Trace IDs recorded for that run.

`record_trace()` remains the only Trace insertion path. It completes exact
record validation, defensive copying, copied-record validation, and duplicate
Trace-ID rejection before mutation. It then commits the Trace to `_traces` and
registers its ID under the copied run ID. If index mutation fails, the Trace
insertion is rolled back.

Trace completion replaces a record without changing `trace_id` or `run_id`, so
it does not update the index. There is no Trace deletion or identity mutation
API.

## Lookup And Ambiguity

`_trace_for_run_id()` reads the derived list:

1. a missing or empty list raises the existing unknown-run error;
2. a list containing more than one Trace ID raises the existing ambiguity
   error;
3. one Trace ID resolves the current record from `_traces`.

The index stores IDs rather than Trace objects, so completion always returns
the current Trace value and cannot leave an obsolete object reference in the
index.

## Snapshot And Concurrency

`from_snapshot()` already inserts every Trace through `record_trace()`, so it
rebuilds the index without a second pass. The index is not serialized and does
not change canonical ordering, snapshot version 2, or legacy usage-log
migration. Legacy v1 migration retains its load-local run index and validation
precedence.

The existing Store `RLock` encloses every public Trace insertion, completion,
and decision lookup. The Trace table and derived index therefore remain one
serialized state for concurrent callers.

## Complexity

Run-to-Trace resolution becomes average O(1), including ambiguity detection.
Trace insertion remains average O(1), and the index uses O(t) derived memory.
Canonical snapshot output and aggregate reporting retain their intentional
sorting and scanning behavior.

## Compatibility

No public signature, dependency, model field, snapshot field, JSON Schema,
active-lessons YAML, packaged resource, PostgreSQL DDL, or schema version
changes. Exact lookup errors, duplicate Trace-ID rejection, defensive copies,
legacy migration, deterministic serialization, and thread safety remain
unchanged.
