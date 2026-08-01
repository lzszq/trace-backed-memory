# Event Ledger Port v1

**English** | [简体中文](event-ledger-port-v1.zh-CN.md)

`tbm.event-ledger-port.v1` freezes the storage-neutral application port for a
future canonical `tbm.event.v1` ledger. It defines trusted access, atomic batch
append, exact replay, bounded reads, stream verification, and bounded
subscriptions. F0 delivers this contract only: no SQLite or PostgreSQL event
ledger backend selects it yet.

## Trusted access boundary

Every port instance is bound to a `LedgerAccessContext` assembled by a trusted
adapter. It binds the organization, tenant, repository, and environment
partition; authenticated principal and agent-client identities; actor and
authorization-decision identities; and a canonical classification filter.
These values are not accepted as untrusted event payload claims.

Append validates every event against that context. Reads fail closed when an
event is outside the authenticated partition or classification filter. Scope
matching remains distinct from authorization and is not presented as tenant
security by itself.

## Atomic append and exact replay

The frozen interface is `append(stream_id, expected_version, events,
idempotency)`. Its implementation constructs one `LedgerAppendRequest` in the
port's trusted access context. It carries one non-empty batch of at most 100
canonical events and a pair of content digests for the idempotency key and
canonical command. Events must:

- belong to the requested stream and trusted partition;
- advance contiguous stream versions from the expected head;
- preserve their exact parent-hash chain;
- bind the same idempotency key and command digest;
- satisfy the caller's classification filter.

A backend must commit the entire batch, stream-head update, global positions,
and idempotency record in one transaction or change nothing. A retry with the
same key and exact canonical request returns the exact original
`LedgerAppendReceipt`. Reusing the key for another command or request fails;
a stale expected version fails without mutation.

Immediately before that transaction commits, the backend applies
`verify_ledger_append_precondition`: the supplied current head must match the
expected stream version, the first event must extend its exact hash, and the
batch must consume the next globally consecutive positions. Head or global
sequence drift is a conflict, never a partial append.

The port expresses these invariants but does not implement the database
transaction. That backend work begins in F1.

## Bounded reads and verification

`read_stream(stream_id, from_version)` returns one contiguous stream page from
a positive version. `read_global(after_position, limit)` returns one strictly
increasing global page after a position.
Both operations are bounded to 1,000 events, retain canonical event objects,
carry a content-bound page digest and high-watermark, and expose only explicit
next cursors. A page that reports more results must contain at least one event,
advance its cursor, and retain a high-watermark later than that page. They never
expose SQL rows, tables, or a raw unfiltered ledger.

`verify_stream(stream_id)` returns a bounded verification result for exact versions,
event count, head hash, tenant partition, and stable issue codes. An empty
stream is represented explicitly rather than fabricated. Issue codes come from
the frozen v1 allowlist, and `verify_ledger_stream_verification` binds the
result back to the requested stream and authenticated partition.

## Subscription profile

`subscribe(...)` creates a bounded, classification-filtered global-page
subscription. Each poll is limited by page size and a maximum 60-second
timeout. Delivery is at least once; consumers acknowledge a delivery ID and
deduplicate by canonical event hash. Heartbeats contain no events. The port
does not promise a deployed broker, a durable consumer offset, or an active
Agent/MCP subscription.

## Stable failures

The contract separates invalid requests, stale stream heads, idempotency
conflicts, scope denial, classification denial, hidden/not-found records, and
unsupported operations. Messages are bounded and sanitized. Implementations
must preserve these meanings and may not reproduce authorization or Gate
policy independently.

This port does not make the existing durable-v3 authorities a canonical event
ledger, does not run reducers, and does not change the current compatibility
Store or default Agent/MCP behavior.
