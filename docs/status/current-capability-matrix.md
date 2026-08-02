# Current capability status

**English** | [简体中文](current-capability-matrix.zh-CN.md)
Machine-readable ledger:
[`current-capabilities.json`](current-capabilities.json)

This ledger is the concise source of truth for whether a capability is on the
default product path. It supplements the detailed
[product contract](../product.en.md); historical delivery phases are not
evidence that a capability is active.

## Status rules

| Status | Meaning |
|---|---|
| `active` | A supported default or explicitly documented compatibility surface is runnable by users. |
| `opt-in` | Executable implementation and focused tests exist, but default transports or storage do not select it. |
| `contract-only` | Contracts, preflight, staging, or an incomplete adapter exist without an end-user lifecycle. |
| `planned` | The repository does not yet provide the required runnable capability. |

Uncommitted work, design-only modules, direct-Python composition that is not
selected by a product transport, and historical phase descriptions never
advance a status.

## Current matrix

| ID | Area | Capability | Status | Current boundary |
|---|---|---|---:|---|
| `core.gated-memory-v2` | Core | Trace/evidence capture and gated v2 memory lifecycle | `active` | Raw Trace remains evidence; the System Gate stays authoritative. |
| `trace.ordered-events-v1` | Full Trace evidence | Ordered typed TraceEvent protocol and bounded ledger adapter | `opt-in` | A sealed 12-type TraceEvent registry binds sequence, exact time, descriptor-only artifacts, tool/permission correlation, and parent/subagent provenance; batches of 1-100 append through the existing ledger port. The opt-in Codex ingestion adapter selects it, while compatibility Trace and default transports do not. |
| `git.observations-v1` | Git evidence | Seven-point Git observation protocol and atomic capture adapter | `opt-in` | Checkout/ref/commit/protected diff/ancestry/object-availability/shallow observations preserve compatibility capture types, record runner/algorithm versions, and retain missing-object uncertainty. Default transports and Codex ingestion do not select this separate Git protocol. |
| `git.graph-projection-v1` | Git evidence | Deterministic Git graph reducer and immutable projection | `opt-in` | Replays complete access-bound observation streams into commit/parent/ancestry/missing-object views, exact source/fix/verification edges, and fail-closed PR source anchors. Shallow or unavailable objects force unknown ancestry. No active profile selects or persists this read model. |
| `effect.receipts-v1` | External effects | Ordered provider attempt and receipt lifecycle | `opt-in` | A sealed 12-type registry, access-bound authorization link, trusted provider registration, deterministic attempt/request binding, exact receipt Artifacts, unknown-result reconciliation, bounded retry/dead-letter, and distinct compensation child effects are executable and tested. Existing completion outbox and default transports do not select it. |
| `artifact.retention-erasure-v1` | Protected content | Governed retention, crypto-erasure, index purge, and tombstones | `opt-in` | A protected content-addressed manifest binds exact targets, hold epochs, key closure, immutable index successor, and replay impacts. Intent precedes effects; exact independently verified KMS receipts precede replay-partial/erasure/tombstone facts; recovery never blindly retries destruction. A durable publication fence for the final index-head/terminal-ledger race is still outstanding; default transports do not select the coordinator. |
| `compat.agent-v1` | Compatibility | `tbm.agent.v1`, `LocalAgentMemory`, CLI | `active` | Pending gate requests are process-local. |
| `transport.local-mcp-v1` | Local transport | STDIO MCP | `active` | Selects the version-2 compatibility lifecycle. |
| `transport.loopback-http-v1` | Local transport | Loopback HTTP | `active` | Selects the version-2 compatibility lifecycle. |
| `sdk.python-v1` | SDK | Python sync/async clients | `active` | Targets the local `tbm.agent.v1` HTTP profile. |
| `sdk.typescript-v1` | SDK | TypeScript client | `active` | Targets the local `tbm.agent.v1` HTTP profile. |
| `distribution.strict-resources` | Distribution | Strict packaged-resource allowlist | `active` | Canonical and installed bytes are verified exactly. |
| `governance.authority-registry-v1` | Governance | Persistence authority role registry and repository guard | `active` | Every SQLite/PostgreSQL v3 persistence module is registered as a ledger, projection, compatibility migration, or bundle coordinator; unregistered authorities fail repository verification. |
| `identity.authorization-v3` | Identity | Entity registry and authorization authorities v3 | `opt-in` | Trusted direct-Python contexts only; no default transport identity. |
| `session.gate-session-v3` | Durable session | SQLite/PostgreSQL GateSession and recovery | `opt-in` | Explicit durable runtimes now append each GateSession revision as a canonical event and synchronously verify the existing revision projection; this is not the active compatibility Store lifecycle. |
| `session.gate-session-events-v1` | Full Persistence lifecycle | GateSession lifecycle event adapter and current-state reducer | `opt-in` | Explicit durable SQLite/PostgreSQL runtimes select the event-first adapter, including baseline import and exact projection rebuild; retrieval/Gate/replay/outcome/effect views and the default compatibility lifecycle remain authority-backed. |
| `replay.gate-evidence-export-v3` | Full Persistence replay | Existing replay export rebuilt from Gate evidence events and Artifacts | `opt-in` | Finalized explicit durable SQLite/PostgreSQL sessions rebuild `tbm.replay-export.v3` from the descriptor-only Gate evidence stream plus exact Artifact bytes; canonical JSON and `export_sha256` must equal the current replay-authority path. This does not complete the Gate evidence crash matrix or aggregate cutover. |
| `projection.outcome-effect-v1` | Full Persistence projection | RunOutcome, OutcomeAttribution, EffectQueue, delivery, dead-letter, and compensation reducers | `active` | `tbmd local` and standalone SQLite durable HTTP/MCP use one command transaction for validation, event append, synchronous rebuild/read-back, response construction, and commit. Raw HTTP, MCP, Python sync/async, and TypeScript share one event/projection golden. A hard-kill matrix covers all 11 command commit points before commit and after commit/before response; legacy delivery remains at-least-once and standalone PostgreSQL cutover is outstanding. |
| `agent.durable-lifecycle-v3` | Durable lifecycle | Prepare through completion and replay facade | `opt-in` | Adapter-neutral composition with no default transport selection. |
| `agent.durable-wire-v1` | Durable wire | `tbm.durable-agent-wire.v1` | `opt-in` | Strict dispatcher; it does not authenticate peers. |
| `memory.structured-evidence-v3` | Evidence | Structured regression evidence | `opt-in` | Active v2 publication still uses the compatibility model. |
| `memory.failure-case-events-v1` | Engineering Memory projection | Event-derived FailureCase and structured-evidence eligibility | `contract-only` | Exact TraceEvent-linked extractor proposals remain candidates and legacy booleans remain `legacy_unstructured`. Draft replacement can still preserve the internal producer capability while changing evidence payloads, so security acceptance and MemoryCatalog/default-profile wiring remain open. |
| `memory.catalog-events-v1` | Engineering Memory projection | Event-rebuilt MemoryCatalog and formal ActivatedMemoryHead | `opt-in` | Exact stored publication/evidence/authorization provenance and reducer trust configuration use a shared SQLite/PostgreSQL ledger path. Cross-page rebuild currently repeats the boundary event, and a filter excluding `internal` can yield an empty partial snapshot; F4-03/F4-04 acceptance, F4-01/F4-02 producer acceptance, F4-07, and default compatibility cutover remain open. |
| `policy.active-bundle-events-v1` | Engineering Memory policy | Event-derived active policy bundle and head | `opt-in` | Exact global create/approve authorization and independent actors activate a content-addressed eight-dimension policy bundle through the shared SQLite/PostgreSQL ledger path. It can be explicitly supplied as the existing retrieval-policy provider; default consumption and downstream trust-tier/renderer/Semantic enforcement are not cut over. |
| `memory.revision-publication-v3` | Publication | Immutable revision authorities | `opt-in` | Active v2 publication still uses the compatibility model. |
| `retrieval.activated-revision-v3` | Retrieval | ActivatedRevision source | `opt-in` | Default adapters retrieve compatibility records; explicit durable runtimes consume operator-supplied v3 sources. |
| `retrieval.index-events-v1` | Engineering Memory projection | Event-rebuilt five-index manifest and active/stale head | `opt-in` | Repository-authorized build/completion, independent activation, exact predecessor/watermark checks, complete classification views, and trusted embedding provider/model configuration use the shared SQLite/PostgreSQL EventLedgerPort path. The read-only selector verifies the exact managed-index bundle and rejects stale heads; F4-06 passed independent acceptance, while default selection remains open. |
| `outcome.harm-events-v1` | Engineering Memory projection | Event-rebuilt outcome, cohort, causal-harm, and suspension-advice views | `opt-in` | Exact evaluation contexts use repository `memory:verify` authorization and a trusted attestation-verifier configuration. Associations remain non-causal, unbound attributions cannot enter derived views, and suspension output is recommendation-only. SQLite/PostgreSQL share EventLedgerPort; independent F4-07 acceptance and default cutover remain open. |
| `retrieval.managed-index-v3` | Retrieval | Managed-index source | `opt-in` | Default adapters retrieve compatibility records; explicit durable runtimes may use this source. |
| `artifact.encrypted-authority-v3` | Protected content | Encrypted Artifact authorities | `opt-in` | Explicit durable runtimes use configured authorities; no object-storage/KMS product path. |
| `replay.durable-v3` | Replay | Durable replay authorities | `opt-in` | Explicit durable HTTP/MCP export session-bound replay when startup policy enables content; default adapters do not. |
| `completion.outbox-v3` | Completion | Outcome and outbox authority/worker | `opt-in` | Explicit `tbmd local` runs bounded SQLite delivery pages and reclaims expired leases; shared-service dispatch remains outstanding. |
| `operations.audit-recovery-v3` | Operations | Audit and recovery authority/worker | `opt-in` | Explicit `tbmd local` expires due PREPARED/AWAITING_DECISION sessions; it does not execute arbitrary audit remediation actions. |
| `protocol.canonical-event-v1` | Full Persistence protocol | `tbm.event.v1` canonical envelope | `opt-in` | Strict storage-neutral envelope, schema, example, and bilingual reference are delivered; explicit durable SQLite/PostgreSQL runtimes now retain GateSession lifecycle events through it, while other domain lifecycles and the compatibility path do not. |
| `protocol.event-type-registry-v1` | Full Persistence protocol | Sealed typed event registry, payload schemas, and upcasters | `opt-in` | Unknown types/versions remain preservable but cannot be silently consumed; the explicit durable runtime selects a sealed 12-type GateSession registry, while the generic operator path still exposes the envelope-only inventory reducer. |
| `protocol.event-ledger-port-v1` | Full Persistence protocol | Atomic append/read/verify/subscribe application port | `opt-in` | Storage-neutral contracts are implemented by WAL/single-owner SQLite and row-locked PostgreSQL backends with exact replay, integrity/catalog verification, and cross-backend conformance. |
| `migration.snapshot-v3` | Migration | Snapshot v3 plan/bundle/verify/staging | `contract-only` | No apply, cutover, or rollback orchestration. |
| `persistence.unified-sqlite-v3` | Persistence cutover | Unified SQLite v3 schema | `opt-in` | One generated bundle installs and fingerprints all 16 durable authority schemas, including the event ledger; active compatibility remains SQLite 1. |
| `persistence.unified-postgresql-v3` | Persistence cutover | Unified PostgreSQL v3 schema | `planned` | Current compatibility boundary is PostgreSQL 2. |
| `persistence.canonical-event-ledger` | Full Persistence | Canonical append-only event ledger | `opt-in` | SQLite/PostgreSQL backends and descriptor-only Artifact references are delivered; explicit durable runtimes select the ledger as the GateSession revision source of truth, but the aggregate product model remains an authority graph until all F2-F5 cutovers pass. |
| `persistence.reducer-runtime` | Full Persistence | Versioned deterministic reducers and rebuildable projections | `opt-in` | `tbm.reducer.v1` supplies sealed version/code/config registries, bounded double-executed state, typed upcasting, checkpoint/resume, poison evidence, shadow compare, approved CAS activation, and append-only rollback. Explicit durable runtimes synchronously rebuild GateSession current state; operator commands rebuild inventory, while the remaining lifecycle views are not reducer-native. |
| `transport.durable-http` | Durable transport | Durable HTTP profile | `active` | Explicit `tbm-http --profile durable-v3`; trusted application factory, bearer boundary, content hidden by default. SQLite selects the event-first command coordinator; PostgreSQL remains authority-graph backed for Outcome/Effect. |
| `transport.durable-mcp` | Durable transport | Durable MCP profile | `active` | Explicit `tbm-mcp --profile durable-v3`; trusted local application factory, bounded STDIO, restart continuation, and content hidden by default. SQLite selects the event-first command coordinator; PostgreSQL does not yet. This is not peer-authenticated shared-service MCP. |
| `sdk.durable-python-typescript` | SDK | Durable Python/TypeScript clients | `active` | The explicit durable HTTP profile has synchronous/asynchronous Python clients and a dependency-free Node.js TypeScript client; raw HTTP, both Python clients, MCP, and TypeScript reproduce one committed SQLite event sequence and projection digest without wire changes. |
| `service.local-daemon` | Local service | Restartable `tbmd local` daemon | `active` | One locked owner-controlled SQLite process shares one event-first command coordinator across bounded STDIO MCP and loopback HTTP; GateSession/Gate evidence and Outcome/Effect writes append and synchronously rebuild before projection. A real-process hard-kill matrix checks exact rollback or exact replay at all 11 commit points. Worker claim/ack transitions stay short and at-least-once. Remaining authorities, compatibility paths, and standalone PostgreSQL have not cut over. `init`, `doctor`, and `health` remain deterministic. |
| `service.shared-multitenant` | Shared service | Remote transports, OIDC, RBAC/RLS, workload identity | `planned` | The Alpha release is not an untrusted multi-tenant service. |
| `integration.review-console` | Engineering integration | Review Console | `planned` | No control-plane implementation is delivered. |
| `integration.codex-hooks` | Engineering integration | Codex hooks/App Server adapter | `opt-in` | Strict bounded capture maps all 12 structured Hook/App Server facts to ordered TraceEvents with trusted scope, exact protected source descriptors, permission/request binding, lifecycle validation, and atomic event batches. It does not install hooks or change default transports. |
| `integration.github-pr-check` | Engineering integration | GitHub PR Check | `planned` | The current CLI only emits a deterministic report. |
| `operations.production-readiness` | Production operations | OTEL/SLO, backup/DR, retention, load/chaos | `planned` | Stable-release qualification is not complete. |
| `governance.stable-release` | Governance | Security/support/compatibility/governance contracts | `planned` | Alpha has no stable support or deprecation window. |

## Accepted convergence decisions

- [ADR-0001: v2 compatibility and durable-v3 cutover](../adr/0001-v2-compatibility-durable-v3-cutover.md)
- [ADR-0002: unified version-3 database bundles](../adr/0002-unified-v3-database-bundles.md)
- [ADR-0003: transport identity ownership](../adr/0003-transport-identity-ownership.md)
- [ADR-0004: canonical resource manifest](../adr/0004-canonical-resource-manifest.md)
- [ADR-0005: public and internal package boundaries](../adr/0005-public-internal-package-boundaries.md)
- [ADR-0006: Full Persistence and reducer-native memory](../adr/0006-full-persistence-reducer-native-memory.md)

The current machine-readable boundary is
`persistence_model="authority_graph"`, `ledger_protocol="tbm.event.v1"`,
`reducer_protocol="tbm.reducer.v1"`, and `full_persistence=false`. The envelope,
ledger backends, and reducer/projection paths remain opt-in. Explicit durable
`tbmd local` and standalone SQLite durable HTTP/MCP now select them for
GateSession/Gate evidence and Outcome/Effect writes. Standalone PostgreSQL,
compatibility paths, and the aggregate source of truth remain the authority
graph until the other lifecycle projections and cutover gates are complete.

## Promotion rule

A row changes only when its documented end-user path, failure semantics,
focused negative tests, bilingual documentation, and required distribution
verification land together. `opt-in` may become `active` only after a real
transport or daemon selects the capability and restart/conformance tests cover
that selection.
