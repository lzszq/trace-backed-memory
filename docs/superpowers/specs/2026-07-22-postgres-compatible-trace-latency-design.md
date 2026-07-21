# PostgreSQL-Compatible Trace Latency Design

## Summary

Phase 45 made Trace latency non-negative, but the persistence layers still
disagree on its upper range. The Store and Trace JSON Schema accept any
JSON-serializable non-negative Python integer. PostgreSQL stores the field as a
signed `INTEGER`, whose maximum is 2,147,483,647. A larger value can therefore
enter a valid snapshot and fail only when `PostgresMemoryRepository.sync()`
attempts to persist it.

Phase 47 defines one portable range: `latency_ms` is `None` or an integer from
0 through 2,147,483,647 inclusive. This is approximately 24.85 days in
milliseconds and matches the existing PostgreSQL column without a migration.

## Store Authority and Error Priority

Define `TRACE_LATENCY_MAX_MS = 2_147_483_647` beside the Store's other runtime
contract constants. Extend the shared Trace validator with an upper-bound check
after its existing checks:

1. require an exact integer rather than `bool` or `float`;
2. preserve the JSON integer serialization guard;
3. reject negative values with the existing non-negative error;
4. reject values above the maximum with
   `ValueError("latency_ms must be at most 2147483647")`.

Every record, snapshot reconstruction, callback execution, scalar completion,
and batch completion path already reaches this validator. Candidate staging
therefore rejects an out-of-range value before Trace or usage state changes.
The order also preserves the existing serialization-limit error for extremely
large positive and negative integers.

The CLI continues to delegate the domain range to the Store. Parsed integers
above the maximum are structured `state` errors with exit code 3; malformed
numeric input remains an `input` error with exit code 2. No second range
implementation belongs in argparse or manifest parsing.

## Portable and PostgreSQL Contracts

Add `maximum: 2147483647` beside `minimum: 0` in the canonical Trace JSON
Schema and its installed package copy. Keep PostgreSQL DDL unchanged:
`latency_ms INTEGER` already enforces the same inclusive upper boundary, and
the named `traces_latency_ms_non_negative` CHECK continues to make the lower
boundary explicit.

Existing PostgreSQL schema-version-1 databases already have the physical
upper bound, so Phase 47 needs no database migration. The canonical and
packaged Trace Schema bytes change; PostgreSQL DDL bytes do not. The packaged
resource allowlist, names, and count remain 18.

## Compatibility

Existing snapshots and API inputs with latency above 2,147,483,647 become
invalid. Such values were never portable to the supported PostgreSQL backend.
`None`, zero, values through the inclusive maximum, completion omission and
replay, and `cost_usd` behavior remain unchanged. Public signatures,
dependencies, snapshot shape and version 2, active-lessons YAML, PostgreSQL DDL
and schema version 1 remain unchanged.

## Tests

- Runtime tests accept 2,147,483,647 and reject 2,147,483,648 during recording,
  snapshot reconstruction, and later-item batch completion without mutation.
- Execution completion propagates the Store error while leaving Trace and usage
  records pending.
- Scalar and manifest CLI tests require state/exit-code-3 rejection and an
  unchanged `--write` snapshot; an inclusive-boundary completion succeeds.
- Schema tests require `minimum: 0` and `maximum: 2147483647` in canonical and
  packaged copies while PostgreSQL DDL bytes remain unchanged.
- A live PostgreSQL test accepts the maximum and rejects one above it through
  the existing `INTEGER` type.
- Existing huge-integer tests continue to lock JSON serialization error
  priority for both signs.
