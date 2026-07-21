# Linear Snapshot Usage-Log Validation Design

## Problem

Snapshot version 2 permits 100,000 records in one collection and 250,000
records across the store by default. Callers may also disable both record
budgets for trusted inputs. The usage-log portion of `from_snapshot()` performs
several repeated linear scans:

- every new `decision_id` scans all previously loaded usage logs;
- every usage log rebuilds the complete set of known memory IDs;
- legacy logs without `trace_id` scan every trace to resolve `run_id`;
- candidate, used, and blocked relationships use repeated list membership;
- repeated logs for one trace rebuild that trace's tool-name set.

At the default 100,000-log boundary, unique decision IDs alone require about
4,999,950,000 equality checks. Cross-products with trace or memory counts make
otherwise valid bounded snapshots impractical to load.

## Design

Keep all optimization state local to one `from_snapshot()` call:

1. Maintain a `seen_decision_ids` set while preserving the existing duplicate
   check position after complete per-log validation.
2. Build the known-memory-ID set once after all memory collections load, then
   reuse it for every usage log.
3. For legacy snapshots only, index traces by `run_id` once. Preserve the
   existing unknown and ambiguous run-ID errors.
4. Cache trace tool-name sets lazily by `trace_id` while validating usage-log
   context.
5. Within `_validate_usage_log()`, build candidate and blocked ID sets once and
   use them for relationship membership. Continue producing missing/conflicting
   IDs in their original list order.

These indexes are validation implementation details. They are not store state,
are not serialized, and cannot become stale after loading.

## Error And Ordering Compatibility

- Validate each usage-log record, memory reference, and trace reference before
  checking duplicate decision IDs, as today.
- Retain exact exception types and messages for duplicate IDs, unknown memory
  IDs, unknown or ambiguous run IDs, context mismatch, and list relationships.
- Preserve input processing order and canonical `to_snapshot()` ordering.
- Build a new local store exactly as before, so any error exposes no partial
  result to the caller.

## Complexity

For snapshot records and nested usage-log ID/tool data of total size `n`, the
changed work is expected O(n) average time with O(n) temporary index space.
Hash-set and hash-map operations use Python's ordinary average-time contract.
No wall-clock threshold is part of the tests; instrumented string subclasses
count comparisons, hashes, and repeated helper calls instead.

## Compatibility

No public signature, dependency, model field, snapshot field, JSON Schema,
active-lessons YAML, packaged resource, PostgreSQL DDL, or schema version
changes. Snapshot version remains 2, legacy snapshot import remains supported,
and PostgreSQL schema version remains 1.
