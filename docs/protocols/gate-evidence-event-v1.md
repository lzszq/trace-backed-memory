# Gate Evidence Event v1

**English** | [简体中文](gate-evidence-event-v1.zh-CN.md)

`tbm.gate-evidence-event.v1` is the compact canonical-event contract for the
first F2 retrieval and System Gate evidence cutover increment. It defines:

- `tbm.retrieval.prepared` for one immutable `RetrievalSnapshot`;
- `tbm.system_gate.evaluated` for one immutable `SystemGateEvaluation`.

Each evidence record owns a one-event `gate_evidence` stream at version 1.
The System Gate event uses `causation_id` to identify its retrieval event.
Trusted organization, tenant, repository, environment, principal, and client
identity come from the adapter-owned `EventTrustedContext`; request JSON cannot
select them.

## Payload and Artifact reference

The payload contains only compact linkage:

- evidence kind, record ID, and GateSession ID;
- authorization decision ID;
- retrieval snapshot parent ID when applicable;
- exact-content Artifact ID and SHA-256;
- occurrence time and causation event ID.

The exact canonical JSON remains in the transitional Gate evidence repository.
The event carries one descriptor-only `EventArtifactRef` with media type
`application/json`, internal classification, exact byte size and digest, and
`available` state. Reducers never read those bytes. Moving the exact bytes to
the authenticated encrypted Artifact Authority is still required before the
Artifact store can become the sole byte source of truth.

## Event-first persistence

When explicitly enabled, both SQLite and PostgreSQL Gate evidence repositories
append and read back the retrieval and System Gate events before inserting the
existing evidence rows. Event append and row projection share one connection,
cursor, and transaction. Any event, projection, or read-back failure rolls back
the whole unit. An exact retry validates the retained one-event streams and does
not allocate new global positions.

PostgreSQL writers lock the event-ledger schema/global head before the Gate
evidence schema and rows. This preserves the repository-wide event-first lock
order.

## Reducer

`gate-evidence-current` version 1 is pure and deterministic. It retains exact
record/Artifact linkage, one current retrieval/System Gate pair per session,
authorization continuity, causation, and canonical event heads. It fails closed
if System Gate evidence precedes or mismatches its retrieval evidence. Its
projection can be checkpointed, resumed, compared, activated, and rolled back
through the existing `tbm.projection.v1` runtime.

The following increments now event-source Semantic Gate attempts and
finalization, and explicit durable replay export derives metadata from the
ledger; see [Semantic Gate Attempt Event v1](semantic-gate-attempt-event-v1.md),
[Finalization Event v1](finalization-event-v1.md), and
[Ledger Replay Export v1](ledger-replay-export-v1.md). Outcome/attribution and
local completion-effect events/reducers, including delivery history and
dead-letter parity, are also delivered. The storage-neutral provider receipt/
reconciliation event, reducer, and ledger service are delivered. Configured
explicit durable runtimes also provide the server-owned Semantic provider
callback, provider/policy binding, stable idempotency, and invocation parity
across the durable transports. Trusted reconciliation, owner fencing, and
bounded retry/dead-letter require their corresponding configured dependencies;
generic compensation is limited to supporting contracts. Concrete remote adapters, completion-provider integration,
automatic background sweep/lease fencing, shared-service workers, and the
remaining crash/cutover gates stay open; Semantic provider effects do not claim
compensation or remote exactly-once. The product therefore continues to report
`full_persistence=false`.
