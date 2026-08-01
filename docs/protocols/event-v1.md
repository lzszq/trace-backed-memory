# Canonical Event v1

**English** | [简体中文](event-v1.zh-CN.md)

`tbm.event.v1` is the storage-neutral canonical envelope for the full-persistence
event ledger. It fixes the bytes and provenance that later ledger writers,
reducers, replay, projections, and adapters will share. This milestone defines
the event contract only: it does not append events, select a database, run a
reducer, authorize a caller, or register typed event families.

## Envelope

Every event binds:

- an extensible `event_type`, its producer-owned `event_version`, and the
  versioned `payload_schema`;
- a stream identity, positive stream version, positive global position, and
  exact previous-event hash;
- organization, tenant, repository, environment, principal, agent client,
  actor, and authorization-decision identities supplied by trusted context;
- request, idempotency, correlation, and causation identities plus a request
  digest;
- producer identity/version, classification, retention policy, structured
  payload, and descriptor-only artifact references;
- a producer occurrence time when known, a trusted recorded time, the payload
  hash, and an independently domain-separated event hash.

`event_id` is a stable logical identifier; `event_sha256` is the immutable
integrity identity of the complete unsigned envelope. The event hash is
`SHA-256("tbm.event.v1\\0" || canonical-json(unsigned-event))`. Payload hashes
use the same canonical JSON encoding without that envelope domain prefix.
Canonical JSON is UTF-8, object-key sorted, compact, and finite-number only.

## Provenance and time

Native domain events do not claim an import source. Imported or observation
events carry a source-system record, evidence quality, and observation time.
`occurred_at` may be `null` only when source observation evidence exists; this
preserves uncertainty instead of inventing historical time. `recorded_at` is
always required and must come from trusted infrastructure. Known occurrence and
observation times cannot follow the recorded time, and stream recorded time
cannot move backwards.

The initial event in a stream has `stream_version = 1` and no previous hash.
Every later event names the exact parent hash and increments the stream version
by one. The parent verifier also fixes stream/scope identity and requires a
strictly advancing global position. A ledger must enforce the same rules
atomically; this contract alone is not concurrency control.

## Payloads and artifacts

The runtime parser is duplicate-key rejecting and bounds the full document to
1 MiB, depth 32, and 10,000 nodes. The payload is an object bounded to 512 KiB,
depth 24, and 8,192 nodes. Cycles, non-JSON values, non-finite numbers, unknown
envelope fields, and common secret-bearing metadata keys are rejected.

Artifact references are sorted, unique, content-addressed descriptors. They
contain no artifact bytes. A reference binds media type, size, classification,
retention, encryption-key identity when protected, and availability. An event
cannot have a lower classification than an artifact it references. Artifact
authorization and byte availability remain the Artifact Authority's job.

## Trust and evolution boundary

External JSON never chooses trusted organization, tenant, repository,
environment, principal, client, actor, or authorization-decision identity.
Writers build events from an authenticated `EventTrustedContext`, then verify
the same slots before append. Schema validation is structural preflight only;
runtime code must recompute both hashes and verify the exact parent and trusted
context.

Well-formed unknown event types are intentionally preserved by this envelope.
The typed registry, per-type payload validators, compatibility declarations,
upcasters, and unknown-type reducer behavior are defined by
[Event Type Registry v1](event-registry-v1.md). This separation keeps the base
envelope extensible without claiming that every event is already understood by
a reducer.

The atomic append/read/verification/subscription boundary is frozen separately
by [Event Ledger Port v1](event-ledger-port-v1.md); no backend implements that
port yet.

Canonical resources:

- `schemas/event_v1.schema.json`
- `examples/event_v1.example.json`
