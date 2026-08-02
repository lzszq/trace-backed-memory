# GateSession lifecycle events v1

**English** | [简体中文](gate-session-events-v1.zh-CN.md)

`tbm.gate-session-event.v1` is the event-first adapter and deterministic
current-state reducer for the durable version-3 GateSession lifecycle. It is
active only in the explicit durable SQLite and PostgreSQL runtime composition.
Compatibility Agent, HTTP, MCP, CLI, and SDK wire contracts are unchanged.

The canonical event ledger is the source of truth for GateSession revisions
created after the adapter is bound. Existing GateSession revision tables remain
synchronous, queryable projections. A failed event append, reducer rebuild, or
projection comparison aborts the same database transaction before the revision
row becomes visible.

## Stream and trusted partition

Each GateSession has one stream of type `gate_session`. Its stream ID is a
domain-separated digest of the exact `session_id`; callers cannot choose it.
The ledger partition is resolved from the trusted entity registry as the exact
organization, tenant, repository, and active environment associated with the
persisted GateSession. An ambiguous or missing mapping fails closed. A runtime
may provide an equivalent trusted resolver for an explicitly composed
multi-environment deployment; request JSON never supplies ledger identity.

Every native event uses classification `internal`, the durable service actor,
the exact revision `updated_at` as `recorded_at`, and a deterministic command
and idempotency digest. The previous event hash and expected stream version are
verified by the ledger.

## Event types

The sealed domain registry contains the following version-1 event types:

- `tbm.gate_session.created`;
- `tbm.gate_session.prepared`;
- `tbm.gate_session.awaiting_decision`;
- `tbm.gate_session.semantic_gate_decided`;
- `tbm.gate_session.usage_decision_finalized`;
- `tbm.gate_session.execution_started`;
- `tbm.gate_session.completed`;
- `tbm.gate_session.canceled`;
- `tbm.gate_session.expired`;
- `tbm.gate_session.execution_abandoned`;
- `tbm.gate_session.lease_renewed`;
- `tbm.gate_session.baseline_imported`.

`awaiting_decision` and `lease_renewed` preserve revisions that do not map to a
new terminal or phase name. They are required for exact lease/version replay.
`baseline_imported` is an observation event used once when a pre-adapter
GateSession first transitions after upgrade. It records the exact retained
projection with `legacy_partial` evidence quality; it does not invent missing
historical lifecycle events.

Every payload is a strict object with exactly:

- `transition` — the registered transition constant;
- `previous_session_sha256` — the previous projection digest, or `null` only
  for a newly created or baseline-imported stream;
- `session_sha256` — the digest of the resulting GateSession;
- `session` — the complete strict `tbm.gate-session.v3` projection.

Unknown fields, unknown events, non-canonical timestamps, invalid digests, and
payloads that do not satisfy the registered transition are rejected.

## Synchronous append and projection

For a new revision, the adapter performs the following unit of work:

1. derive the exact event draft from the current and next GateSession;
2. append the canonical event with expected stream/global positions;
3. read and verify the complete stream;
4. run the `gate-session-current` reducer;
5. compare the rebuilt GateSession with the proposed next revision;
6. write the existing GateSession revision and current-head projection.

All six operations share the repository transaction and lock order. Global
position contention is retried a bounded eight times; semantic conflicts and
idempotency conflicts are never hidden. A failure after append rolls back both
the event and the projection write.

The reducer verifies the event parent chain, registry payload, transition
legality, previous projection digest, resulting projection digest, stream
position, and complete GateSession domain validation. Rebuild equality covers
status, version, lease and expiry timestamps, retrieval/System/Semantic evidence
IDs, decision IDs, final memory and injection IDs, outcome ID, and terminal
reason. It never reads wall-clock time or external services.

## Deterministic artifacts and boundaries

The domain registry publishes byte-verified packaged resources:

- `examples/gate_session_event_type_registry_v1.example.json` — the sealed
  content-addressed registry catalog;
- `schemas/gate_session_event_payload_registry_v1.schema.json` — the generated
  strict payload dispatch schema.

These GateSession types intentionally remain separate from the small generic
`DEFAULT_EVENT_TYPE_REGISTRY`; the durable adapter selects the domain registry
explicitly. The reducer does not yet replace retrieval, Gate-evaluation,
Semantic-attempt, final-decision, injection, replay, outcome, or delivery-state
authorities. Those views require their own event contracts and reducers before
the complete F2 cutover can be declared finished.
