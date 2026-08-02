# Gate evidence events and ledger replay export v1

**English** | [Simplified Chinese](gate-evidence-events-v1.zh-CN.md)

`tbm.gate-evidence-event.v1` is the opt-in event adapter for evidence retained
around one durable GateSession. It is selected only by the explicit durable
SQLite and PostgreSQL compositions. Compatibility Agent, HTTP, MCP, CLI, and
SDK contracts are unchanged.

## Stream and event set

Each session has one `gate_evidence` stream whose ID is derived from the exact
session ID. The sealed registry accepts five version-1 domain events:

- `tbm.gate_evidence.retrieval_snapshot_recorded`;
- `tbm.gate_evidence.system_gate_evaluated`;
- `tbm.gate_evidence.semantic_gate_attempt_recorded`;
- `tbm.gate_evidence.usage_decision_recorded`;
- `tbm.gate_evidence.injection_artifact_recorded`.

Payloads contain only bounded identity, linkage, status, sequence, and
content-addressed Artifact descriptors. Retrieval scores, prompt and response
bytes, rendered injection bytes, and replay component bytes remain in their
authenticated authorities. Canonical events refer to those bytes with exact
`EventArtifactRef` values; they never copy protected content into projection
state.

Preparation synchronizes retrieval and System Gate events before the
`PREPARED` GateSession revision is committed. Every successful or failed
Semantic attempt synchronizes its record, prompt, and optional response in the
attempt repository transaction. Finalization synchronizes the UsageDecision,
injection descriptor, exact injection bytes, and replay-manifest descriptor
before the `FINALIZED` revision is committed. An append, artifact read-back, or
projection mismatch aborts the shared database transaction.

## Deterministic views

Five deterministic reducers build the retrieval-current, System-Gate-current,
Semantic-attempt-chain, final-decision-current, and injection-current views.
Reducer state contains descriptors and linkage only. Hydration loads exact
content-addressed records, validates every event Artifact reference, parses the
existing strict v3 records, and re-verifies System Gate monotonicity, Semantic
parent order, final-decision linkage, and replay-manifest linkage.

The stream order is monotonic: retrieval, System Gate, zero or more Semantic
attempts, final decision, then injection. Singleton views cannot be replaced,
Semantic attempts must extend the exact parent chain, and retained events must
be an exact prefix of authority evidence. Restart rebuild never reads wall
clock time or calls an external provider.

## Existing replay export from the ledger

`GateEvidenceEventLedgerProjector.export_replay_bundle` rebuilds the finalized
injection and replay manifest from events and their referenced Artifact bytes.
It resolves each manifest component by content digest, applies an explicit
classification allowlist and byte limit, and calls the existing
`build_replay_bundle_export` constructor. Its output remains exactly
`tbm.replay-export.v3`; there is no new export wire format.

SQLite and PostgreSQL conformance tests compare the complete canonical export
JSON and `export_sha256` against `export_replay_bundle` reading the current
replay authority. Any missing component, digest mismatch, disallowed
classification, or size overflow fails closed.

## Current boundary

This adapter does not make the aggregate product event-first. Outcome/effect,
Memory, index, outbox, audit, metrics, migration, compatibility cutover, and the
complete F2 crash matrix remain separate work. The machine-readable product
status therefore remains `persistence_model="authority_graph"` and
`full_persistence=false`.
