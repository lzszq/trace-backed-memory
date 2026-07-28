# SQLite OutcomeAttribution Ledger v3

**English** | [简体中文](sqlite-outcome-attribution-v3.zh-CN.md)

This opt-in ledger persists immutable `tbm.outcome-attribution.v3` records
against completed durable GateSessions and their retained RunOutcomes. It is
separate from the RunOutcome completion transaction because the contract
permits multiple independently produced association or causal claims for one
outcome. It does not change active SQLite schema version 1 or active Agent/MCP
behavior.

## Install and API

`SQLiteOutcomeAttributionV3Repository.connect(..., initialize=True)` installs,
in order, the isolated GateSession, RunOutcome, and OutcomeAttribution schemas.
Existing installations apply `schemas/sqlite-v3-outcome-attribution.sql` only
after the first two dependencies.

The repository exposes:

- `put_attribution(attribution)` for exact immutable append or replay;
- `get_attribution(attribution_id)` for one verified record;
- `list_attributions(run_outcome_id)` for deterministic
  `recorded_at`/identity order;
- `outcomes` for the shared RunOutcome and guarded GateSession authorities.

An exact content-ID replay returns `inserted=False`. The same outcome may retain
multiple attribution records; the domain contract defines no one-attribution
uniqueness rule.

## Integrity and transactions

Every operation requires SQLite foreign keys and recursive triggers, validates
the exact canonical managed schema, and rejects temporary shadows or unexpected
indexes/triggers. One transaction, or a caller-compatible savepoint, performs
linkage verification, immutable insertion, exact read-back, and a second
cross-record verification.

The insert guard validates the complete canonical descriptor through the same
strict Python contract parser, requires every duplicated relational and JSON
field to equal the Python canonical serialization byte-for-byte, independently
revalidates the linked RunOutcome row, requires sorted unique memory-revision
and evidence arrays, enforces association-versus-causal shape, compares
timestamps as parsed RFC3339 instants, and links the claim to:

- an existing immutable RunOutcome;
- its current `COMPLETED` GateSession revision;
- the exact usage decision;
- only memory revisions finalized in that session;
- a `recorded_at` value at or after the outcome measurement.

Independent repository reads reparse and rehash the descriptor, compare every
column, reload the outcome and session, and run
`verify_outcome_attribution()`. UPDATE, DELETE, replacement INSERT, malformed
descriptor, schema drift, or partial writes fail closed.

## Trust boundary

This ledger preserves supplied evaluator/verifier IDs, artifact hashes, and
timestamps as storage-neutral provenance. It does not authenticate principals,
verify artifact bytes, establish a trusted clock, or turn an association into a
causal claim. A service must derive authenticated identities and trusted time
before constructing the content-addressed record.

The isolated
[PostgreSQL attribution ledger](postgres-outcome-attribution-v3.md) provides
database parity. Authenticated evaluator and artifact checks, completion and
attribution outbox delivery, and active Agent/MCP/HTTP/SDK integration remain
follow-up work.
