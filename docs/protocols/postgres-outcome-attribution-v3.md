# PostgreSQL OutcomeAttribution Ledger v3

**English** | [简体中文](postgres-outcome-attribution-v3.zh-CN.md)

This opt-in isolated ledger provides PostgreSQL parity for immutable
`tbm.outcome-attribution.v3` records. It appends independently produced
association or causal claims over retained RunOutcomes and their completed
GateSessions. It does not change active PostgreSQL schema version 2 or activate
the v3 lifecycle in Agent, MCP, HTTP, or SDK adapters.

## Install and rollback

Install, in order:

1. `schemas/postgres-v3-gate-session.sql`;
2. `schemas/postgres-v3-outcome.sql`;
3. `schemas/postgres-v3-outcome-attribution.sql`.

The installer locks and validates active-v2, GateSession-v1, and
RunOutcome-v1 metadata before creating the isolated
`trace_backed_memory_v3_outcome_attribution` schema. It leaves all active
tables and compatibility versions unchanged.

Rollback uses `schemas/postgres-v3-outcome-attribution-rollback.sql` before the
RunOutcome and GateSession rollback scripts. It obtains exclusive table locks,
checks exact metadata, relations, functions, triggers, constraints, and
columns, and drops with `RESTRICT`. Drift or external dependencies abort the
whole rollback.

## API and transaction

`PostgresOutcomeAttributionV3Repository` exposes:

- `put_attribution(attribution)` for immutable append or exact content-ID
  replay;
- `get_attribution(attribution_id)` for one fully verified record;
- `list_attributions(run_outcome_id)` in deterministic
  `recorded_at`/identity order;
- `outcomes` for the shared PostgreSQL RunOutcome and protected GateSession
  authorities.

One RunOutcome may retain multiple claims. A concurrent exact replay
serializes through the primary key and returns `inserted=False`; a retained
record with different content conflicts. Every operation uses one transaction,
or a PostgreSQL savepoint when the caller already owns a transaction. Append
performs linkage verification before insert, exact read-back, a second
cross-record verification, and final catalog validation before commit.

## SQL integrity

The insert trigger reconstructs the exact Python-canonical revision, evidence,
confidence, timestamp, payload, and descriptor text; recomputes the attribution
SHA-256 ID; and rejects non-canonical JSON, alternate numeric spellings,
unknown descriptor shape, invalid identifiers/text, or an invalid
association-versus-causal claim.

The trigger independently reparses and rehashes the linked RunOutcome row,
locks the current completed GateSession head and revision, and requires exact
trace, run, usage-decision, outcome, timestamp, and finalized-memory linkage.
PostgreSQL `timestamptz(6)` instant comparison preserves microsecond ordering.
UPDATE, DELETE, TRUNCATE, schema drift, catalog/function-body drift, and
partial writes fail closed.

Repository reads independently parse and hash the descriptor, compare every
stored column, reload the outcome and current session, and execute
`verify_outcome_attribution()`. Lock order is GateSession, RunOutcome, then
OutcomeAttribution so all composed operations share the same dependency order.

## Trust boundary

The ledger preserves evaluator/verifier IDs, artifact hashes, and the supplied
trusted timestamp as provenance. It does not authenticate those identities,
authorize artifact bytes, produce completion/attribution outbox events, or
promote an observed association to causation. Those responsibilities remain
with a trusted service boundary. Active Agent/MCP/HTTP/SDK integration remains
follow-up work.
