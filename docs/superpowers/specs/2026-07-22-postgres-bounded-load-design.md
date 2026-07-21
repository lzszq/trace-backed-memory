# PostgreSQL Bounded Load Design

## Summary

Snapshot ingestion rejects more than 100,000 records in one collection or
250,000 records in total. `PostgresMemoryRepository.load()` currently enforces
those limits only after five client-side `fetchall()` calls have already
materialized and decoded every database row. An oversized database can
therefore consume unbounded client memory before the regular Store validator
rejects it.

Phase 40 moves the same record-count boundary ahead of every record query. It
does not replace Store validation or add a new public load configuration.

## Count Preflight

After schema validation and the existing ordered five-table `SHARE` lock,
`load()` executes one statement that returns five scalar `count(*)` values:

- `traces` from `public.traces`;
- `failure_cases` from `public.failure_cases`;
- `lessons` from `public.lessons`;
- `project_policies` from `public.project_policies`;
- `usage_logs` from `public.memory_usage_decisions`.

The aliases match `SNAPSHOT_COLLECTION_NAMES`. The result must be exactly one
mapping row and every required count must be a non-negative integer.

Validation runs in snapshot collection order. A count above
`SNAPSHOT_MAX_RECORDS_PER_COLLECTION` raises the existing exact per-field
message. If all collections pass, their sum is checked against
`SNAPSHOT_MAX_TOTAL_RECORDS` with the existing exact total message. No
collection `SELECT`, decoder, or record `fetchall()` runs after a failed
preflight.

## Consistency and Memory Bound

The Phase 39 table locks are acquired before the count query and retained
through all collection reads. External inserts, updates, and deletes cannot
change a count between preflight and materialization, including when the
repository operation is nested in a caller transaction.

Once counts pass, the existing ordered selectors and decoders run unchanged.
At most 100,000 records can be fetched for one collection and at most 250,000
records across all five collections. `TraceBackedMemoryStore.from_snapshot()`
then repeats its normal count and domain validation as defense in depth.

An exact count adds one server-side scan before the record scans. This preserves
the existing precise error text and avoids transferring a `limit + 1` sentinel
row. The count statement returns only one client row.

## Errors

Malformed count results and count-limit failures are `ValueError` internally.
The existing `load()` boundary wraps them as sanitized
`PostgresPersistenceError("failed to load memory store from PostgreSQL")` while
retaining the `ValueError` cause. Schema and undefined-table behavior remain
unchanged.

## Non-goals and Compatibility

This phase bounds record-count materialization. It does not bound the byte size
of one JSONB/text field, change PostgreSQL execution memory, add pagination, or
stream a partial Store. Oversized individual database values remain a separate
hardening problem.

There is no public API, model, DDL, dependency, or resource change. Snapshot
version 2, PostgreSQL schema version 1, JSON Schemas, active-lessons YAML, and
all 18 packaged resources remain unchanged.

Tests cover exact per-collection and total failures, limits at the accepted
boundary, malformed count results, the one-row SQL mapping, rejection before
any record loader, sanitized error wrapping, and the existing real-cluster load
and consistency suite.

