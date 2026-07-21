# PostgreSQL Load Payload Budget Design

## Summary

Phase 40 bounded `PostgresMemoryRepository.load()` by record count, but each
accepted PostgreSQL row can still contain an arbitrarily large `TEXT`, array,
or `JSONB` value. The five collection selectors use `fetchall()`, so a database
within the count limits can force the client to transfer, decode, and retain an
unbounded payload before normal Store validation can reject it.

Phase 48 adds a second preflight while the existing five-table `SHARE` locks
are held. After count validation and before the first collection selector, the
repository measures the UTF-8 JSON payload represented by the persisted rows.
It rejects a row above 64 MiB or an aggregate payload above 64 MiB without
fetching a collection record.

## Byte Accounting

The preflight converts each physical row from the five snapshot tables with
PostgreSQL 12's `pg_catalog.to_jsonb(anyelement)`, casts the resulting object to
text, converts that text to UTF-8 with `pg_catalog.convert_to`, and measures the
result with `pg_catalog.octet_length`. A `UNION ALL` CTE feeds one aggregate
query that returns:

- `max_record_bytes`: the largest encoded row;
- `total_bytes`: the sum across all five tables.

Empty tables return zero for both fields through `COALESCE`. The sum is cast to
`bigint`; the existing record ceilings keep the theoretical result within that
range. Functions and tables are schema-qualified so caller `search_path`
cannot substitute a helper.

This is a PostgreSQL load-payload budget, not an assertion that its byte count
equals `TraceBackedMemoryStore.save_json()`. The file serializer includes a
snapshot envelope, indentation, separators, and a trailing newline; the SQL
preflight uses one compact row object at a time. Both limits intentionally
reuse the existing 64 MiB snapshot input budget because it is the product's
established ceiling for materializing one complete Store document.

## Ordering and Validation

`load()` retains this order:

1. lock schema metadata `FOR SHARE`;
2. lock all five snapshot tables in ordered `SHARE` mode;
3. run and validate the existing five-table count query;
4. run and validate the scalar payload-size query;
5. fetch and decode the five collections;
6. reconstruct the Store through its normal validators.

The payload result must be exactly one mapping row containing non-negative,
exact Python integers for both fields. The maximum record size cannot exceed
the total. Boundary values are accepted; one byte above either limit raises a
`ValueError` before a collection loader runs. The existing `load()` exception
boundary wraps that cause as the sanitized
`PostgresPersistenceError("failed to load memory store from PostgreSQL")` and
leaves the connection reusable.

The table locks make both preflights and the later collection reads observe the
same committed state. The SQL aggregate may detoast and serialize values on the
server, but it returns only two integers to the client and prevents oversized
rows from entering psycopg's collection result sets.

## Compatibility

The new guard applies only to database-to-client loading. `sync()` already
receives a caller-owned Store in client memory, and changing its accepted write
set is outside this threat boundary. Existing databases with oversized direct
SQL data remain intact but must be reduced before repository loading succeeds.

Public signatures, dependencies, models, snapshot shape and version 2, JSON
Schemas, active-lessons YAML, all 18 packaged resources, PostgreSQL DDL, and
PostgreSQL schema version 1 remain unchanged.

## Tests

- Unit tests accept both exact 64 MiB boundaries and reject one-byte overflow,
  malformed mappings, missing fields, non-integers, negatives, and inconsistent
  maximum/total pairs.
- Query tests require one mapping row, deterministic empty-table zeros, and
  UTF-8 byte accounting for non-ASCII content on PostgreSQL 12+.
- A live repository test proves an oversized preflight reaches none of the five
  collection loaders, preserves the sanitized error/cause boundary, rolls back
  cleanly, and leaves the connection reusable.
- Existing count, lock, coherent-snapshot, Store validation, and sync tests
  remain unchanged.
