# PostgreSQL Bounded Load Plan

## Scope

Reject oversized PostgreSQL record sets before any collection rows are fetched
or decoded, using the Store's existing default snapshot limits.

## Steps

1. Centralize the existing per-collection and total count error checks in the
   private ingestion boundary without changing Store validation order or text.
2. Add one five-table scalar count query and strict result decoding to the
   PostgreSQL adapter.
3. Run count validation after Phase 39 table locks and before the first record
   selector in `load()`.
4. Add deterministic unit and real-transaction tests for boundary success,
   malformed counts, per/total overflow, pre-materialization rejection, and
   wrapped errors without allocating large row lists.
5. Publish the bounded-load contract and Phase 40 compatibility statement in
   README, architecture, usage policy, product, and roadmap documents.
6. Run focused and full tests, distribution verification, compatibility checks,
   independent review, and required remote PostgreSQL CI.

## Compatibility

- `PostgresMemoryRepository.load()` keeps its existing signature and return.
- Store and snapshot validation remain authoritative and repeat the checks.
- Snapshot version remains 2 and PostgreSQL schema version remains 1.
- No SQL schema or packaged-resource bytes change.
- Count scans add bounded query work; no record row is transferred on reject.

