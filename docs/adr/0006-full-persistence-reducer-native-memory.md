# ADR-0006: Full Persistence and Reducer-Native Memory

**Status:** Accepted
**Date:** 2026-08-01
**简体中文:** [0006-full-persistence-reducer-native-memory.zh-CN.md](0006-full-persistence-reducer-native-memory.zh-CN.md)

## Context

The repository has two mature operational paths: the version-2 compatibility
Store and an explicit durable-v3 authority graph. The durable graph retains
authorization, GateSession, retrieval, Semantic Gate, finalization, execution,
outcome, outbox, and replay state across restarts, but those records remain
distributed across independent authorities. There is no canonical event history
from which every supported projection can be rebuilt with versioned reducers.

Several existing components are append-only or content-addressed, but none is
the complete system ledger. In particular, `tbm.audit-event.v3` is audit
evidence rather than the source of lifecycle truth. Continuing to add isolated
authorities would increase migration and consistency cost without closing this
architectural gap.

## Decision

- The canonical append-only event ledger is the final source of truth for
  domain, lifecycle, control, and effect state.
- The authenticated content-addressed Artifact Authority is the source of truth
  for large or protected bytes. Events reference artifacts; they do not embed
  unbounded or sensitive payloads.
- Versioned deterministic reducers consume canonical events and build
  replaceable projections. A projection may be discarded and rebuilt; it is
  not an independent source of truth.
- Existing compatibility and durable-v3 authorities remain supported migration
  assets until their event-first cutovers are complete. Their current records
  must be imported, shadow-compared, and preserved rather than relabeled as the
  canonical ledger.
- During cutover, an event append and every critical projection update use the
  same database connection, unit of work, and transaction. Independent
  best-effort dual writes are forbidden.
- Review, structured verification, authorization, System Gate monotonicity,
  Semantic Gate narrowing, finalization, and exact replay checks remain product
  invariants. Reducers reconstruct decisions; they do not weaken them.
- Git is an observation and evidence provider. Git history is not the system
  ledger and cannot replace retained execution, authorization, Gate, or effect
  evidence.
- External effects use explicit intent, attempt, and idempotent receipt events.
  A database transaction cannot be presented as atomic with an external side
  effect.
- New independent authorities, protocol families, or standalone SQL components
  are frozen unless a documented security/corruption fix or a ledger, reducer,
  or migration cutover blocker requires one.
- Snapshot version 2, SQLite schema version 1, PostgreSQL schema version 2,
  `tbm.agent.v1`, and the explicit durable-v3 profiles remain compatibility
  boundaries until a separately verified cutover changes them.

## Consequences

This ADR changes architectural priority, not current runtime behavior. The
machine-readable capability status therefore reports
`persistence_model="authority_graph"` and `full_persistence=false`.

Delivery proceeds incrementally through the F0-F6 release train: event
contracts and ledger ports, SQLite/PostgreSQL ledgers, reducer runtime and
rebuild tooling, event-first lifecycle adapters, legacy import and shadow
comparison, then shared-service and stable-release qualification. Rollback
selects a prior reducer/projection head; it does not delete canonical events.

## Exit evidence

Full Persistence may be reported only when the complete Definition of Done is
met: canonical event append and artifact linkage, deterministic cross-version
rebuilds, event-first local and shared transports, verified legacy import and
cutover, authorization and Gate invariants, idempotent external-effect receipts,
backup/restore and disaster-recovery evidence, retention/crypto-erasure, and
production observability and governance. Until then, delivered increments keep
`full_persistence=false`.
