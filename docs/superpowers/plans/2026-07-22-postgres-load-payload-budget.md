# PostgreSQL Load Payload Budget Plan

## Scope

Bound PostgreSQL row materialization before the repository fetches any snapshot
collection, while preserving the existing count preflight, locks, public API,
and database schema.

## Steps

1. Add failing unit tests for exact 64 MiB maximum-record and total-payload
   boundaries, one-byte overflow, malformed size results, and impossible
   maximum/total combinations.
2. Add failing query tests for the scalar mapping contract, empty tables, and
   non-ASCII UTF-8 byte accounting on a real PostgreSQL cluster.
3. Add a failing repository test proving payload overflow occurs after count
   validation but before every collection loader, is wrapped as the existing
   sanitized persistence error, and leaves the connection reusable.
4. Add one schema-qualified five-table payload aggregate, a strict result
   decoder, and a centralized validator. Reuse the 64 MiB snapshot byte limit
   for both the largest row and aggregate payload.
5. Invoke the payload preflight under the existing ordered table locks between
   count validation and collection loading.
6. Update README, architecture, usage policy, product documentation, roadmap,
   and executable documentation contracts for Phase 48.
7. Run focused and full tests, real PostgreSQL tests, distribution verification,
   isolated install smoke tests, independent review, merge, push, and require
   every CI job to pass.

## Compatibility

- Normal loads, count-limit errors, locking, sync, conflict handling, borrowed
  transactions, and sanitized error messages remain unchanged.
- A database row or aggregate payload above the new limit becomes unloadable
  before psycopg fetches a collection; the database transaction rolls back and
  no partial Store is returned.
- Public APIs, dependencies, snapshot version 2, JSON Schemas, active-lessons
  YAML, packaged resource paths and bytes, PostgreSQL DDL, and schema version 1
  do not change.
