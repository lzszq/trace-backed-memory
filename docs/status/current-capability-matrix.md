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
| `compat.agent-v1` | Compatibility | `tbm.agent.v1`, `LocalAgentMemory`, CLI | `active` | Pending gate requests are process-local. |
| `transport.local-mcp-v1` | Local transport | STDIO MCP | `active` | Selects the version-2 compatibility lifecycle. |
| `transport.loopback-http-v1` | Local transport | Loopback HTTP | `active` | Selects the version-2 compatibility lifecycle. |
| `sdk.python-v1` | SDK | Python sync/async clients | `active` | Targets the local `tbm.agent.v1` HTTP profile. |
| `sdk.typescript-v1` | SDK | TypeScript client | `active` | Targets the local `tbm.agent.v1` HTTP profile. |
| `distribution.strict-resources` | Distribution | Strict packaged-resource allowlist | `active` | Canonical and installed bytes are verified exactly. |
| `identity.authorization-v3` | Identity | Entity registry and authorization authorities v3 | `opt-in` | Trusted direct-Python contexts only; no default transport identity. |
| `session.gate-session-v3` | Durable session | SQLite/PostgreSQL GateSession and recovery | `opt-in` | Side-by-side authorities; not the active Store lifecycle. |
| `agent.durable-lifecycle-v3` | Durable lifecycle | Prepare through completion and replay facade | `opt-in` | Adapter-neutral composition with no default transport selection. |
| `agent.durable-wire-v1` | Durable wire | `tbm.durable-agent-wire.v1` | `opt-in` | Strict dispatcher; it does not authenticate peers. |
| `memory.structured-evidence-v3` | Evidence | Structured regression evidence | `opt-in` | Active v2 publication still uses the compatibility model. |
| `memory.revision-publication-v3` | Publication | Immutable revision authorities | `opt-in` | Active v2 publication still uses the compatibility model. |
| `retrieval.activated-revision-v3` | Retrieval | ActivatedRevision source | `opt-in` | Default adapters retrieve compatibility records; explicit durable runtimes consume operator-supplied v3 sources. |
| `retrieval.managed-index-v3` | Retrieval | Managed-index source | `opt-in` | Default adapters retrieve compatibility records; explicit durable runtimes may use this source. |
| `artifact.encrypted-authority-v3` | Protected content | Encrypted Artifact authorities | `opt-in` | Explicit durable runtimes use configured authorities; no object-storage/KMS product path. |
| `replay.durable-v3` | Replay | Durable replay authorities | `opt-in` | Explicit durable HTTP/MCP export session-bound replay when startup policy enables content; default adapters do not. |
| `completion.outbox-v3` | Completion | Outcome and outbox authority/worker | `opt-in` | No product daemon operates the worker. |
| `operations.audit-recovery-v3` | Operations | Audit and recovery authority/worker | `opt-in` | No product daemon operates the worker. |
| `migration.snapshot-v3` | Migration | Snapshot v3 plan/bundle/verify/staging | `contract-only` | No apply, cutover, or rollback orchestration. |
| `persistence.unified-sqlite-v3` | Persistence cutover | Unified SQLite v3 schema | `opt-in` | One generated bundle installs and fingerprints all 15 durable authority schemas; active compatibility remains SQLite 1. |
| `persistence.unified-postgresql-v3` | Persistence cutover | Unified PostgreSQL v3 schema | `planned` | Current compatibility boundary is PostgreSQL 2. |
| `transport.durable-http` | Durable transport | Durable HTTP profile | `active` | Explicit `tbm-http --profile durable-v3`; trusted application factory, bearer boundary, unified SQLite/PostgreSQL v3 runtime, content hidden by default. |
| `transport.durable-mcp` | Durable transport | Durable MCP profile | `active` | Explicit `tbm-mcp --profile durable-v3`; trusted local application factory, bounded STDIO, unified SQLite/PostgreSQL v3 runtime, restart continuation, and content hidden by default. This is not peer-authenticated shared-service MCP. |
| `sdk.durable-python-typescript` | SDK | Durable Python/TypeScript clients | `active` | The explicit durable HTTP profile has synchronous/asynchronous Python clients and a dependency-free Node.js TypeScript client; one shared fixture runs through the Python and TypeScript lifecycle suites. |
| `service.local-daemon` | Local service | Restartable `tbmd local` daemon | `planned` | No service owns workers and the durable authority graph. |
| `service.shared-multitenant` | Shared service | Remote transports, OIDC, RBAC/RLS, workload identity | `planned` | The Alpha release is not an untrusted multi-tenant service. |
| `integration.review-console` | Engineering integration | Review Console | `planned` | No control-plane implementation is delivered. |
| `integration.codex-hooks` | Engineering integration | Codex hooks/App Server adapter | `planned` | Existing integration is documentation, skills, and local MCP. |
| `integration.github-pr-check` | Engineering integration | GitHub PR Check | `planned` | The current CLI only emits a deterministic report. |
| `operations.production-readiness` | Production operations | OTEL/SLO, backup/DR, retention, load/chaos | `planned` | Stable-release qualification is not complete. |
| `governance.stable-release` | Governance | Security/support/compatibility/governance contracts | `planned` | Alpha has no stable support or deprecation window. |

## Accepted convergence decisions

- [ADR-0001: v2 compatibility and durable-v3 cutover](../adr/0001-v2-compatibility-durable-v3-cutover.md)
- [ADR-0002: unified version-3 database bundles](../adr/0002-unified-v3-database-bundles.md)
- [ADR-0003: transport identity ownership](../adr/0003-transport-identity-ownership.md)
- [ADR-0004: canonical resource manifest](../adr/0004-canonical-resource-manifest.md)
- [ADR-0005: public and internal package boundaries](../adr/0005-public-internal-package-boundaries.md)

## Promotion rule

A row changes only when its documented end-user path, failure semantics,
focused negative tests, bilingual documentation, and required distribution
verification land together. `opt-in` may become `active` only after a real
transport or daemon selects the capability and restart/conformance tests cover
that selection.
