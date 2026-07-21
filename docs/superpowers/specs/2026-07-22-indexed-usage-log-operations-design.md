# Indexed Usage-Log Operations Design

## Problem

Normal usage-log creation currently performs two full history scans. It first
regex-scans every existing `decision_id` to find the largest numeric suffix,
then equality-scans the same list to prove the generated ID is unique. Creating
`n` decisions therefore performs O(n^2) cumulative work even though the Store
already serializes each write with its `RLock`.

Single decision outcome, completion, and recovery operations also scan the
entire usage-log list to resolve one `decision_id`. Batch completion and
recovery rebuild a temporary ID-to-index dictionary for each call.

## Design

Add two private, derived fields to `TraceBackedMemoryStore`:

- `_usage_log_indexes: dict[str, int]` maps each decision ID to its stable list
  position;
- `_next_decision_number: int` is one greater than the largest suffix matching
  `decision_(\d+)`, starting at 1.

There is no usage-log deletion or reordering API. Outcome/completion/recovery
replace a record at its existing position while preserving `decision_id`, so
both derived fields remain valid across replacements.

Centralize all three insertion paths in `_append_usage_log()`:

1. reject an already indexed ID with the existing error;
2. parse the candidate numeric suffix before mutation;
3. append the record and register its list index;
4. advance the numeric counter only after the append commits.

`from_snapshot()` calls the same helper only after the existing complete
per-record, memory, and trace validation. `finalize_memory()` and
`log_decision()` also call it only after building a fully validated candidate.
`_new_usage_log()` reads but does not advance the counter, so rejected writes
do not consume IDs.

## Imported IDs

- Sparse numeric IDs retain max-suffix semantics: importing
  `decision_000001` and `decision_000003` makes the next ID
  `decision_000004`.
- Leading-zero variants are distinct decision IDs but contribute their integer
  suffix to the same maximum.
- Non-matching IDs remain valid and indexed but do not affect numeric
  allocation.
- Duplicate imported IDs still fail after all earlier per-log validation.

## Lookup And Concurrency

`_usage_log_index()` and batch decision lookups reuse the persistent mapping.
The existing `RLock` encloses every public append, lookup, and replacement, so
the list, index, and counter are observed as one serialized state. Canonical
snapshot output still sorts records by `decision_id`; the index does not alter
or replace that ordering contract.

## Complexity

Decision allocation, duplicate checking, and single-ID lookup become average
O(1). A batch of `k` requested decisions performs average O(k) lookup work.
Snapshot output remains O(n log n) because canonical sorting is intentional.
The internal mapping uses O(n) derived memory.

## Compatibility

No public signature, dependency, model field, snapshot field, JSON Schema,
active-lessons YAML, packaged resource, PostgreSQL DDL, or schema version
changes. Snapshot version remains 2 and PostgreSQL schema version remains 1.
Public properties continue returning deep copies, and exact validation errors,
atomic failure behavior, deterministic serialization, and thread safety remain
unchanged.
