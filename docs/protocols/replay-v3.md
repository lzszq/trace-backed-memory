# Content-addressed replay contract v3

**English** | [简体中文](replay-v3.zh-CN.md)

`tbm.replay.v3` defines storage-neutral records for the exact bytes injected
after a finalized memory decision and for the fixed evidence set needed to
replay that decision. It does not activate memory or make the current
process-local Gate durable. An opt-in isolated SQLite repository persists
these records; isolated PostgreSQL install and fail-closed rollback schemas
establish the same immutable relational boundary, and an opt-in PostgreSQL
repository now provides exact-byte and descriptor validation. Neither ledger
is connected to active runtime state.

## Content identity

`ContentAddressedArtifact` derives `content_sha256` from the exact bytes and
derives `artifact_id` from that digest. Verification compares size, digest,
and derived identity. The descriptor records media type, classification,
creation time, and optional encryption/redaction metadata without logging the
content. `confidential` and `restricted` descriptors require an
`encryption_key_id`; the contract does not perform encryption itself.

Artifacts are bounded at 64 MiB. Injection snippets are UTF-8
`text/plain; charset=utf-8` and are bounded at 1 MiB. These artifact limits do
not enlarge the existing runtime renderer limits.

## Injection artifact

`InjectionArtifact` binds the exact final snippet to one GateSession,
decision, usage decision, ordered immutable memory revisions, renderer
identity/version, policy-bundle digest, and render time. Its content-derived
identity allows later readers to reject modified bytes. The render time must
equal the content descriptor's creation time. `verify_injection_artifact()`
verifies the snippet bytes only; descriptor metadata must come from a trusted,
atomically linked record. A future service, not this constructor, must prove
that the referenced GateSession was finalized and that the revisions are the
final allowed set.

The canonical external contract is
`schemas/injection_artifact_v3.schema.json`; the packaged example is
`examples/injection_artifact_v3.example.json`.

## Decision replay manifest

`DecisionReplayManifest` has one fixed component map, in canonical order:

1. retrieval snapshot;
2. System Gate evaluation;
3. semantic Gate prompt;
4. semantic Gate response;
5. ancestry evidence;
6. policy bundle;
7. renderer;
8. injection artifact.

Each present value is an algorithm-tagged SHA-256 digest. A `complete`
manifest must bind every component and its injection artifact ID.
`legacy_partial` is the only permitted incomplete state and must enumerate
exactly the components whose values are null. It is an explicit migration
fact, not permission to claim exact replay.

Runtime crypto-erasure does not rewrite a `complete` manifest into
`legacy_partial`. The opt-in Artifact retention coordinator instead appends a
content-addressed `ReplayPartialMarker` sidecar that binds the exact erased
Artifact IDs and newly unavailable components. Consumers that select this
sidecar must reject exact replay; existing replay repository rows remain
immutable. `legacy_partial` remains migration-only.

The manifest has a canonical self-hash over every field except
`manifest_sha256`. The injection artifact ID must be derived from the
injection component digest. The canonical external contract is
`schemas/decision_replay_manifest_v3.schema.json`; the packaged example is
`examples/decision_replay_manifest_v3.example.json`.

## Portable replay export

`tbm.replay-export.v3` is the read-only portable envelope for one retained
decision replay. `ReplayBundleExport` contains the exact
`DecisionReplayManifest`, its optional `InjectionArtifact`, and every present
component descriptor plus base64-encoded bytes in the fixed component order.
`export_sha256` is a canonical self-hash over the complete envelope except the
hash field itself. `loads_replay_bundle_export()` rejects duplicate keys,
unknown fields, malformed or noncanonical base64, digest or linkage changes,
and exports over 16 MiB of JSON or 8 MiB of decoded component content.
`verify_replay_bundle_export()` rechecks the immutable value.

`export_replay_bundle()` accepts the small `ReplayExportReader` protocol used
by both opt-in replay repositories. The caller must first authorize the
manifest and every referenced artifact, and must pass an explicit non-empty
classification allowlist; that allowlist is a second fail-closed check, not
authorization. The helper preflights each descriptor's classification and
declared/physical size before loading its bytes, then performs immutable
ID-addressed reads, validates every digest, enforces a caller-selected content
limit, and never writes repository state.

The canonical external contract is
`schemas/replay_bundle_export_v3.schema.json`; it references the sibling
manifest and injection schemas. The packaged example is
`examples/replay_bundle_export_v3.example.json`. Hash verification and
manifest/component ordering remain value-level checks after JSON Schema
validation.

`AuthenticatedDurableAgentMemory.export_replay_bundle()` supplies the opt-in
authorization boundary. Its versioned request contains a
durable session ID rather than a manifest or artifact ID. The facade recovers
the original retrieval scope, appends and reads back a fresh `artifact:read`
decision, resolves one unique manifest from the session's retained linkage,
and rechecks authorization and session version before returning the bundle.

This contract remains intentionally unexposed through `tbm.agent.v1` and the
default compatibility HTTP/MCP profiles. Explicit durable HTTP/MCP profiles
construct the authenticated facade and expose replay only when startup content
policy enables it; Python/TypeScript durable clients use that same boundary.
The local bearer or trusted STDIO startup context is not shared-service
transport identity and must not turn content-derived IDs into a data-discovery
oracle.

## Opt-in SQLite replay ledger

`SQLiteReplayV3Repository` uses the isolated
`schemas/sqlite-v3-replay.sql` schema without changing active SQLite schema
version 1. It stores exact artifact bytes, injection descriptors, and replay
manifests as immutable rows. `store_bundle()` inserts the artifact, injection,
and manifest in one transaction, requires exact session/decision/usage and
injection linkage, and treats exact replay as idempotent. Conflicting content
rolls back the whole operation.

`store_complete_bundle()` additionally requires the first supporting artifact
to be the content-derived UsageDecision artifact for the manifest's exact
usage ID. It atomically stores that record, any deduplicated replay-component
artifacts, the injection bytes/descriptor, and the manifest.

`load_artifact_descriptor()` checks descriptor columns and physical content
length without fetching the content BLOB. Full loads repeat that preflight,
then reparse the descriptor, compare duplicated relational columns, and
rehash the stored bytes. Canonical schema metadata, tables, indexes,
immutable triggers, foreign keys, and caller savepoint ownership are verified
fail closed. `load_manifest_for_session()` resolves a unique manifest from
session/decision/usage/injection linkage without accepting a content-derived
lookup ID. Manifest loading verifies bounded manifest and injection
descriptors without reading injection bytes, so export classification and size
preflight still precede content reads. The ledger itself does not authorize
reads, encrypt content, apply retention, prove evidence truth, or link to a
durable GateSession. The repository rejects
confidential/restricted artifacts until a transparent encryption provider can
preserve exact content identity.

## Isolated PostgreSQL replay schema

`schemas/postgres-v3-replay.sql` installs an isolated
`trace_backed_memory_v3_replay` schema only when the active PostgreSQL schema
is version 2. It stores bounded bytes plus immutable injection and manifest
descriptors, derives artifact IDs from declared digests, enforces exact
manifest-to-injection identity linkage and injection byte/media bounds, denies
update, delete, and truncate through fixed-search-path triggers, and leaves
the active v2 tables unchanged. Like the SQLite DDL, it intentionally rejects
confidential/restricted bytes until encryption is implemented. Installation
is one transaction and locks active schema metadata before creating any object.

`schemas/postgres-v3-replay-rollback.sql` locks both metadata records and all
ledger tables, verifies the expected relation, function, trigger, constraint,
and column catalog membership, and
drops only the isolated schema with `RESTRICT`. Unexpected objects, external
dependencies, metadata drift, or an active version other than 2 abort the
entire rollback. SQL alone does not recompute content SHA-256 or parse
canonical descriptors. `PostgresReplayV3Repository` performs those checks
before every insert and after every load, treats exact replay as idempotent,
uses nested psycopg transactions so caller work retains ownership, and rejects
catalog/function/trigger drift. It does not provide access authorization,
encryption, retention, GateSession linkage, or service transactions.
Its `store_complete_bundle()` has the same UsageDecision-first linkage,
component retention, idempotency, rollback, and caller-savepoint behavior as
the SQLite peer.

## Parsing and trust boundary

External injection and manifest JSON is limited to 1 MiB, depth 32, and
10,000 nodes. The portable export has its separate 16 MiB/depth-40/20,000-node
envelope and 8 MiB decoded-content limits described above. Parsers reject
duplicate keys, invalid UTF-8, non-finite numbers, missing or unknown fields,
invalid timestamps, and noncanonical component sets. Hashes prove byte
identity, not authorship, authorization, or evidence truth.

JSON Schema consumers must enforce the draft's `date-time` format. Canonical
self-hash and content-derived ID relationships are value-level rules enforced
by the Python parser and must be checked after Schema validation.

## Durable finalization composition

The opt-in durable finalization service uses `store_complete_bundle()` and
reads every retained component back before publishing `FINALIZED`. An exact
finalized replay reloads the UsageDecision, all seven supporting component
artifacts, the injection, and the reconstructed manifest without rerendering.
The service authorizes and rechecks one retrieval scope, but remains an
internal composition rather than an active runtime adapter.

The current v2 Store, active SQLite v1 adapter, PostgreSQL v2 adapter, local
agent, and STDIO MCP do not persist or emit these records. The opt-in SQLite
ledger and opt-in PostgreSQL repository provide the retained storage boundary
and implement the replay export reader surfaces. The authenticated durable
Agent provides replay-read authorization over that exact graph; explicit
durable HTTP/MCP and Python/TypeScript clients expose it only under startup
content policy. A production shared-service runtime must additionally
authenticate transport identity and apply retention/encryption before exposing
replay export.
