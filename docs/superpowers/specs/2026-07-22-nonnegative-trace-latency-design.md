# Non-Negative Trace Latency Design

## Summary

`Trace.latency_ms` is documented and typed as an integer measurement, but the
runtime, JSON Schema, and PostgreSQL DDL currently accept negative integers.
That permits impossible duration evidence to enter snapshots and database
rows, and later audit or aggregation cannot distinguish it from a valid
measurement.

Phase 45 defines one cross-layer rule: `latency_ms` is `None` or an integer
greater than or equal to zero. Zero remains valid. `cost_usd` is outside this
phase because credits and adjustments can have different product semantics.

## Store Authority

Add the range check to the existing private Trace validator after exact-integer
and JSON-serialization-bound checks. Every supported write and reconstruction
path already funnels through this validator:

- `record_trace()` and snapshot loading;
- `complete_trace()`;
- `complete_memory_run()` and `complete_memory_runs()`;
- callback execution through those completion APIs.

The check raises `ValueError("latency_ms must be non-negative")`. Candidate
construction and batch staging occur before commit, so a negative value leaves
Trace and usage state unchanged. Keeping the JSON-integer bound check first
preserves the existing deterministic error for extremely large positive or
negative integers.

The CLI does not duplicate this domain rule. Scalar `complete` continues to
parse an exact integer, and `complete-batch` continues to parse an exact JSON
integer or null. Both then delegate to the Store. A negative measurement is
therefore an existing structured `state` error with exit code 3, while wrong
types and malformed documents remain `input` errors with exit code 2.

## Portable and PostgreSQL Contracts

Add `minimum: 0` to `latency_ms` in the canonical Trace JSON Schema and its
installed package copy. Add a named nullable-safe PostgreSQL check constraint:

```sql
CONSTRAINT traces_latency_ms_non_negative CHECK (latency_ms >= 0)
```

to the canonical fresh-install DDL and its installed copy. PostgreSQL CHECK
semantics accept null and reject negative integers. The repository continues
to encode and decode the same column without a second range implementation.

The project has no in-place migration mechanism. The DDL constraint applies to
new installations; an existing schema-version-1 database is not altered by a
package upgrade. Library-owned snapshot loading and synchronization still
enforce the Store rule. Operators that allow direct SQL into an existing
database must apply an equivalent constraint under their own migration policy.

## Compatibility

Existing negative-latency snapshots and direct API inputs become invalid.
`None`, zero, positive integers, omission semantics, exact replay, and every
other Trace field remain unchanged. Public signatures, dependencies, snapshot
shape and version 2, active-lessons YAML, and PostgreSQL column shape and
schema version 1 remain unchanged.

The Trace Schema and PostgreSQL DDL bytes intentionally change, including their
installed copies. The packaged resource allowlist, names, and count remain 18.

## Tests

- Runtime tests cover negative record, snapshot reconstruction, single
  completion, and later-item batch completion without mutation.
- Existing zero-latency tests continue to prove the inclusive boundary.
- Scalar and manifest CLI tests require structured state exit code 3 and an
  unchanged snapshot after `--write`.
- Schema tests require `minimum: 0`; PostgreSQL text and live-cluster tests
  require the named CHECK while accepting zero and null.
- Documentation contract tests publish the rule, error boundary, fresh-install
  scope, changed resource bytes, unchanged versions, and Phase 45 maturity.

