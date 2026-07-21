# Referenced Live Memory-ID Validation Design

## Problem

Every live usage-log validation currently constructs a set containing every
failure-case, lesson, and project-policy ID before checking the small set of
IDs referenced by one decision. Decision creation, outcome sealing, memory-run
completion, and recovery therefore perform O(m) catalog work per validation,
where `m` is the total stored memory count.

Usage decisions already cap their ID collections, and the three Store
dictionaries are the authoritative memory-ID indexes. Copying their complete
key sets is unnecessary on live paths.

## Design

Retain `_validate_usage_log_memory_ids()` and its optional load-local
`known_memory_ids` argument.

When an explicit set is supplied, preserve the existing set-difference path.
`from_snapshot()` already constructs this set once and reuses the same object
for every imported usage log.

When the argument is omitted, validate only the IDs referenced by the log. An
ID is known when it is present in any of `_failure_cases`, `_lessons`, or
`_project_policies`. Collect unknown IDs from the deduplicated referenced set
and sort them before raising the existing error.

No new derived Store state is introduced. This avoids an additional index
that every failure-case, lesson, policy, and snapshot insertion would need to
maintain atomically.

## Semantics And Concurrency

The candidate, used, and blocked ID lists continue through complete usage-log
validation before memory existence checking. Deduplication and sorted error
output remain unchanged. Membership checks do not depend on dictionary
iteration order.

All live callers already hold the Store `RLock`. Snapshot reconstruction owns
an unpublished Store and supplies its immutable-for-the-load known-ID set.
Memory status transitions do not remove IDs, and global cross-kind ID
uniqueness remains enforced by the existing insertion boundaries.

## Complexity

Let `r` be the number of distinct IDs referenced by one usage log. Live memory
existence validation becomes average O(r) time and O(r) temporary space,
independent of the total memory catalog size. Snapshot reconstruction retains
its average O(n) load-local index behavior.

## Compatibility

No public signature, error, validation order, dependency, model field,
snapshot field, JSON Schema, active-lessons YAML, packaged resource,
PostgreSQL DDL, or schema version changes. Snapshot version remains 2 and
PostgreSQL schema version remains 1.
