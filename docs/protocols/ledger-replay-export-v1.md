# Ledger Replay Export v1

**English** | [简体中文](ledger-replay-export-v1.zh-CN.md)

`tbm.ledger-replay-export.v1` is the F2 read path that reconstructs a finalized
replay export from canonical finalization events while loading exact bytes only
from the authenticated replay/Artifact authority. Explicit durable SQLite and
PostgreSQL runtimes select it; compatibility adapters keep the existing
projection-backed reader.

## Lookup and reconstruction

`LedgerReplayExportReaderV1` reads the deterministic finalization stream when a
manifest digest is known. Session-bound lookup scans canonical events by global
position with a fixed maximum of 100,000 events and accepts exactly one valid
`tbm.injection.rendered` event for the requested session. Missing, duplicate,
out-of-scope, out-of-order, or malformed events fail closed.

The reader reconstructs `DecisionReplayManifest` and `InjectionArtifact`
metadata from the canonical event. It does not trust replay projection metadata
as the event source of truth and does not read protected bytes from event
payloads or Artifact references.

## Authenticated byte reads

The contextual reader binds ledger access to the adapter-owned trusted
organization, tenant, repository, environment, principal, client, actor, and
fresh replay-read authorization decision. Exact descriptors and content bytes
are loaded only through the replay/Artifact authority. Every event Artifact
reference is revalidated against the stored descriptor, digest, size, media
type, classification, retention/encryption metadata, availability, and content.
The complete fixed role set must be present before export.

`verify_ledger_replay_export_parity()` independently exports through the
ledger-derived reader and the transitional replay-projection reader. It
requires the manifests, descriptors, bytes, injection content, and final
`export_sha256` to be identical. A projection cannot silently substitute or
repair canonical event metadata.

## Boundary

This is a bounded session replay export, not a generic canonical-event export;
`event_export` remains `false`. The event ledger provides finalization metadata
and causation, while the authenticated Artifact/replay authority provides exact
bytes. The product therefore still reports
`persistence_model="authority_graph"` and `full_persistence=false`. The storage-
neutral provider event/reducer/ledger foundation and configured server-owned
Semantic provider effect integration are delivered with provider/policy binding,
stable idempotency, and durable-transport invocation parity. Trusted
reconciliation, owner fencing, and bounded retry/dead-letter require their
corresponding configured dependencies; generic compensation is limited to
supporting contracts. Concrete remote adapters,
completion-provider integration, automatic background sweep/lease fencing,
shared-service workers, migration, and cutover gates remain open; Semantic
provider effects do not claim compensation or remote exactly-once.
Outcome/attribution and local completion-effect projections already provide
event-first parity through delivery history and dead letter.
