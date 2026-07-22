# PostgreSQL Loaded-Row Payload Design

**Status:** Accepted for Phase 66 implementation

## Problem

`PostgresMemoryRepository.load()` limits the largest database row and the
five-table aggregate to 64 MiB before fetching any collection. The current
scalar preflight serializes every complete physical row with
`to_jsonb(snapshot_row)`. `failure_cases`, `lessons`, and `project_policies`
contain an internal `updated_at` column that the repository never selects,
decodes, or exposes in a Store snapshot. Those three timestamp key/value pairs
therefore consume load budget without contributing a byte to the loaded record.
Across large collections this can reject a load whose actual selected record
projection remains inside the fixed boundary.

## Measurement Contract

Measure the compact PostgreSQL JSONB text representation of each row projection
that enters the repository's collection loaders:

- `traces` and `memory_usage_decisions` retain the complete physical row because
  every column is selected;
- `failure_cases`, `lessons`, and `project_policies` exclude only the internal
  `updated_at` column;
- all remaining database column names and PostgreSQL JSONB value rendering stay
  unchanged.

This remains a database-load work budget. It is not an exact byte count of the
indented, sorted `save_json()` envelope, whose record field names may also be
normalized by decoders.

## SQL Shape And Security

Keep the existing single scalar query and five `UNION ALL` branches. In the
three affected branches, remove `updated_at` from `to_jsonb(snapshot_row)` with
the schema-qualified PostgreSQL JSONB subtraction operator before converting
the result to UTF-8 text. Keep all functions, casts, tables, and operator lookup
pinned to `pg_catalog`/`public`; do not materialize collection rows in Python to
perform the preflight.

The query still runs after the ordered five-table `SHARE` locks and accepted
count preflight, and before any collection selector. It returns only
`max_record_bytes` and `total_bytes`.

## Errors And Boundaries

Retain `POSTGRES_LOAD_MAX_RECORD_BYTES = 64 MiB` and
`POSTGRES_LOAD_MAX_TOTAL_BYTES = 64 MiB`. Exact boundaries remain accepted;
either value one byte above its limit raises the existing sanitized
`PostgresPersistenceError` with the underlying `ValueError`, performs no
partial load, and leaves the connection reusable. Malformed scalar results and
query failures retain their existing behavior.

## Compatibility

This changes only a conservative runtime-load metric by excluding bytes that
the loader never fetches. It changes no row, selected value, normalized Store,
public signature, dependency, transaction/lock boundary, snapshot field,
snapshot version 2, JSON Schema, active-lessons YAML, packaged resource,
PostgreSQL DDL, or PostgreSQL schema version 1.

## Verification

Tests must prove the query excludes `updated_at` in exactly three branches,
keeps five row encodings and one scalar result, and uses schema-qualified
objects. A real PostgreSQL test must compare repository measurement with an
independent loaded-row projection across all five populated tables, show that
the three physical-row measurements are strictly larger, and preserve the
existing exact-boundary, overflow-before-fetch, sanitized-error, and connection
reuse coverage. Complete local and remote PostgreSQL/package matrices must stay
green.
