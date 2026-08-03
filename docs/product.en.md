# Trace-backed Memory Product Documentation

**English** | [简体中文](product.md)

- Current version: `0.1.0` (Alpha)
- Delivery: Python library, `tbm` CLI, optional local MCP/HTTP adapters, JSON/YAML/JSON Schema, SQLite, and an optional PostgreSQL repository
- Runtime: Python 3.11+ with standard-library SQLite; PostgreSQL features require PostgreSQL 12+
- License: MIT

For the authoritative `active` / `opt-in` / `contract-only` / `planned`
classification, use the
[current capability status ledger](status/current-capability-matrix.md).
“Implemented” in a historical phase does not imply selection by a default
transport.

## 1. Product Positioning

Trace-backed Memory is an execution-evidence memory layer for LLM and agent harness engineering. It converts agent traces, evaluation results, and Git history into verified, scoped, auditable engineering memory, then applies that memory selectively in debug, repair, regression, planning, evaluation, and production workflows.

It is not general-purpose conversational memory and does not build user profiles. It focuses on a narrower engineering question:

> How can an agent reuse lessons verified from real failures without allowing incorrect, obsolete, cross-project, or benchmark-leaking memory into the current run?

The core path is:

```text
Trace -> Failure Case -> Human/Eval Verification -> Verified Lesson
      -> Metadata/Ancestry Retrieval -> System Gate -> LLM Gate
      -> Bounded Injection -> Usage Audit -> Measured Outcome
```

## 2. Target Users

| User | Primary need | Product capability |
|---|---|---|
| Agent and harness engineers | Turn runtime failures into reusable experience | Trace, failure cases, verified lessons, runtime gates |
| Evaluation and quality teams | Prevent historical-answer leakage and measure memory behavior | Benchmark identity, block reasons, outcome metrics |
| Platform engineers | Integrate memory across repositories and tenants | Declared-scope applicability, fixed budgets, audit logs, and documented deployment boundaries |
| PR and CI maintainers | Find historical failures related to a change | Git ancestry, endpoint-aware PR reports, regression suggestions |
| Operations teams | Detect and recover interrupted or partially completed memory runs | Audit, remediation plans, atomic single and batch recovery |

## 3. Core Value

### 3.1 Evidence First, Not Model Self-reporting

A Trace records commit, repository, branch, prompt/tool/model/evaluation provenance, input and output hashes, tool calls, results, latency, cost, and errors. Raw Trace data is evidence and is not injected into runtime prompts by default.

### 3.2 Experience Must Be Verified Before Activation

A failed Trace first becomes a structured Failure Case. It can produce an active Lesson only after human review, a linked fix commit, and passing regression evidence. An LLM cannot promote drafts or guesses into usable memory.

### 3.3 Two Gates, with Deterministic Safety First

System Gate checks source, state, scope, tenant, sensitivity, evaluation leakage, and runtime mode. LLM Gate can only narrow the system-approved set; it cannot reopen blocked memory.

### 3.4 Every Use Is Explainable and Recoverable

Each decision records candidates, allowed and blocked IDs, reasons, risk, injection mode, and the final measured outcome. Trace and decision completion supports atomic commits, audits, remediation plans, and concurrency-safe recovery scans.

## 4. Implemented Capabilities

| Area | Current capability |
|---|---|
| Trace capture | Git metadata, prompt/tool/model/evaluation provenance, execution evidence, latency, cost, and errors |
| Failure learning | Six concrete failure taxonomy categories plus `unknown`, draft extraction, human review, regression verification, and obsolete lifecycle |
| Memory types | Verified Lesson, Verified Failure Case, and Project Policy |
| Retrieval | Metadata-first filtering, keywords, caller-provided semantic scores, and optional Git ancestry |
| Safety gating | System Gate plus LLM applicability Gate; 64 KiB/depth/node/reason response limits; 50-ID decision lists; complete blocked accounting |
| Injection | `none`, `pointer_only`, `short_summary`, and `full_case_summary` under fixed item and character budgets |
| Runtime closure | Two-phase prepare/finalize, atomic single and batch completion, and deferred outcome sealing |
| Runtime orchestration | `run_memory_execution()` joins decision and execution callbacks with atomic completion |
| Agent application boundary | `LocalAgentMemory`, Git-backed Trace capture, stable errors, `tbm capabilities`, versioned `tbm.agent.v1` request/response schemas, canonical local OpenAPI 3.1, one strict MCP/HTTP dispatcher, optional long-running local STDIO MCP and loopback HTTP, dependency-free typed synchronous/asynchronous Python clients, a dependency-free Node.js TypeScript SDK, and real-process cross-language/adapter conformance |
| Durable Agent adapter boundary | `tbm.durable-agent-wire.v1` strict request models and dispatcher map the full authenticated durable facade without caller identity fields or process-local handles; explicit `tbm-http --profile durable-v3`, trusted-local `tbm-mcp --profile durable-v3`, and `tbmd local` select it through operator-owned application factories and the unified runtime factory; synchronous/asynchronous Python and Node.js TypeScript clients select the HTTP profile, while the compatibility CLI does not |
| Operations recovery | Five-state audits, remediation actions, single/batch recovery, and ready-recovery sweeps |
| Operations CLI | Dependency-free `tbm` and module entry point for snapshots, v3 migration preflight/bundle verification, lessons, obsolescence, audits, metrics, PR reports, completion, and recovery |
| Local durable service | `tbmd init/local/doctor/health` owns one locked owner-controlled SQLite v3 graph, shares one dispatcher across bounded STDIO MCP and loopback HTTP, runs bounded GateSession recovery and completion-outbox delivery, and closes workers/transports/runtime in order |
| Migration preparation | Content-addressed inert v2-to-v3 bundles, exact plan replay, immutable SQLite staging, and version-gated PostgreSQL staging/rollback without changing active runtime versions |
| Unified SQLite v3 persistence | Opt-in generated 15-component bundle installs the complete non-migration authority catalog in one transaction and verifies exact component versions plus all controlled main/temp tables, indexes, automatic indexes, and triggers; active SQLite v1 transports remain unchanged |
| GateSession persistence preparation | Opt-in side-by-side SQLite and isolated PostgreSQL append-only revisions, scoped idempotency, CAS transitions, trusted clocks, bounded due discovery, and fail-closed PostgreSQL rollback without changing active SQLite v1/PostgreSQL v2 |
| Replay persistence preparation | Storage-neutral content-addressed artifact, exact injection, fixed-component decision-manifest, and portable `tbm.replay-export.v3` contracts plus opt-in isolated SQLite/PostgreSQL immutable byte/descriptor ledgers; explicit durable finalization appends canonical final-decision/injection events, and the durable facade reconstructs session-bound export metadata from that ledger while reading exact bytes from the authenticated replay authority under a fresh `artifact:read` decision; response bytes remain disabled by default |
| Authorization contract preparation | Storage-neutral canonical repository, exact alias, principal/client, role-binding, and linked-decision v3 contracts plus opt-in isolated SQLite/PostgreSQL immutable authorities; the explicit durable HTTP profile enforces them through server-owned contexts, while compatibility adapters do not |
| Structured evidence preparation | Content-addressed FixEvidence and regression evidence bind the exact case, source Trace, source/fix/verification commits, artifacts, independent reviewers/verifiers, and attestation provenance; strict MemoryRevision preflight verifies their cross-record linkage, while active v2 records do not use them |
| Immutable revision publication | Content-derived proposals and separate approval/activation events bind exact artifact, evidence, authorization, actors, scope, and lineage; isolated SQLite/PostgreSQL authorities persist canonical provenance, validate attestations through a caller boundary, and CAS-lock the durable target head; an authorized ActivatedRevision source revalidates the current head, publication provenance, structured evidence, and encrypted content through SQLite/PostgreSQL Artifact authorities for future v3 retrieval; active-v2 projection and retrieval integration remain outstanding |
| Replayable retrieval preparation | Content-addressed policy and optional authenticated preparation kernel authorize first, load verified activated revisions, filter classification/applicability/eval leakage/Git ancestry, deterministically fuse scores, and emit paired RetrievalSnapshot/System Gate evidence with final head/policy rechecks; an opt-in immutable five-view managed index bundle supplies content-addressed metadata/lexical/semantic/evidence/Git discovery through exact SQLite/PostgreSQL scope-head CAS; a durable composition service creates the authorized GateSession, stores and verifies the exact evidence pair, and CAS-publishes `PREPARED`; production sharding/workers and default compatibility cutover remain outstanding |
| Replayable gate preparation | Content-derived System Gate evaluations and Semantic Gate attempts bind deterministic rule outcomes and provider/model provenance while enforcing that a model can only narrow; exact prompt/response bindings verify role digests, SQLite/PostgreSQL authorities atomically retain public/internal bytes, and one shared service authenticates the provider registration, owns trusted timing and retry parents, and requires exact read-back; an opt-in session composition advances `PREPARED -> AWAITING_DECISION -> DECIDED`. Explicit durable runtimes configured with a trusted invoker record server-owned Semantic effect request/attempt/receipt evidence, bind the complete structured result digest, atomically claim request-only streams, require server-attested owner fencing, reconcile retained uncertainty without repeating the provider call, and apply a request-bound bounded retry/dead-letter policy; explicit durable finalization appends `tbm.usage_decision.finalized -> tbm.injection.rendered` in one transaction with `FINALIZED` and replay projections, while `final-decision-injection` rebuilds exact parity; active policy emission remains outstanding |
| Durable execution and completion preparation | An authenticated execution composition verifies the retained injection before `FINALIZED` → `EXECUTING`, supports exact-version lease resume and abandonment, authenticates a registered outcome evaluator, and composes opt-in SQLite or isolated PostgreSQL authorities that atomically bind one content-addressed RunOutcome to `COMPLETED`, one immutable completion event, `EffectRequested`, and an append-only leased retry/dead-letter delivery chain; canonical worker transition events feed the `effect-queue` reducer with exact delivery-history parity. A storage-neutral provider-effect transition/reducer/ledger service retains provider-bound content-addressed receipts, unknown results, trusted reconciliation, bounded retry/dead-letter, one compensation per original, and receipt-backed generic compensation through generic SQLite/PostgreSQL ledgers. The configured Semantic provider path adds provider/policy binding, stable provider idempotency, atomic request claiming, and cross-transport invocation parity; owner fencing, reconciliation, and bounded retry/dead-letter require their corresponding configured dependencies. Semantic provider effects do not support compensation or claim remote exactly-once. Completion-provider integration, concrete remote adapters, automatic background sweep/lease fencing, shared-service workers, and locally unexecuted PostgreSQL crash probes remain outstanding |
| Distribution resources | [`resources/manifest.json`](../resources/manifest.json) drives the strict byte-identical packaged Schema/OpenAPI/SQL/migration/taxonomy/example allowlist, runtime declaration, package data, metadata, and generated index |
| Ingestion integrity | Explicit failure evidence only, duplicate-key rejection, bounded local documents, and all-or-nothing imports |
| Metrics | With/without-memory pass rates, wrong-memory counts, per-memory observations, and run health |
| PR/CI | Historical failures, source/fix provenance, regression suggestions, endpoint matching, and JSON CLI reports |
| Persistence | Durable atomic JSON/YAML files, a standard-library SQLite repository, and an optional synchronous PostgreSQL repository with bounded validated loads |

All caller-owned JSON is checked for duplicate object keys before conversion to ordinary mappings. `TraceBackedMemoryStore.load_json()`, `parse_memory_context()`, `parse_memory_decision()`, and CLI JSON readers reject duplicates at every nesting level rather than applying last-key-wins behavior. Valid JSON, direct Mapping input, snapshot version 2, and PostgreSQL schema version 2 remain compatible.

## 5. Key Product Workflows

### 5.1 Safe Runtime Memory

1. The harness records the current Trace with `eval_result="unknown"`.
2. `prepare_memory()` retrieves candidates by metadata, optional query or semantic scores, and optional ancestry, then applies System Gate.
3. An external LLM returns a structured applicability decision.
4. `finalize_memory()` rechecks state, narrows the decision, renders a bounded snippet, and records a Trace-linked usage audit.
5. The harness executes and evaluates the task.
6. `complete_memory_run()` or `complete_memory_runs()` atomically writes the Trace and decision outcome. Snapshot operators can submit measured results with `tbm complete` or `tbm complete-batch`.

Synchronous callers may use `run_memory_execution()` to combine steps 2 through 6 while still supplying their own LLM and harness callbacks. Applications that do not need the lower-level Store lifecycle can use `LocalAgentMemory`, which also owns Trace registration, repository synchronization, stable errors, and callback recovery IDs. The optional default `tbm-mcp` command exposes only this runtime lifecycle over bounded local STDIO, fixes provenance to a configured checkout root, and captures complete Git ancestry before retrieval. SQLite and PostgreSQL synchronize durable phases; pending requests in that compatibility profile remain process-local. The `tbm.gate-session.v3` contract defines the target lifecycle, revision, lease, and expiry semantics, with opt-in side-by-side SQLite and isolated PostgreSQL repositories for immutable revisions. The authorization-v3 contract defines the pre-retrieval policy boundary; opt-in isolated SQLite and PostgreSQL authorities verify exact policy/request/decision triples and durably record immutable decisions. `AuthenticatedRetrievalService` provides the shared ordering kernel that matches trusted identity records, persists and reloads the decision, rechecks registry rotation and environment binding, and only then calls retrieval. `AuthenticatedGateSessionService` then durably creates and reads back a scoped session before preparation, suppresses duplicate retrieval, requires trusted retrieval/System-Gate evidence, and CAS-publishes `PREPARED` with explicit compensation. The opt-in SQLite/PostgreSQL Gate evidence authorities atomically store and reload each exact content-addressed pair, while the shared verifier binds it to the authorized session and identity scope. `DurableRetrievalPreparationService` composes these opt-in boundaries under one authorization scope: it creates the session first, prepares retrieval without a second authorization decision, stores and verifies the pair, and then publishes `PREPARED`. `AuthenticatedSemanticGateSessionService` then verifies that durable evidence and the complete immutable attempt chain, publishes `AWAITING_DECISION` before provider work, and publishes `DECIDED` only after exact attempt/artifact read-back; failed attempts remain explicitly retryable and a retained success is recoverable without another provider call. Separate authorities retain ordered recovery; same-database repositories may share a caller-owned outer transaction. `GateSessionRecoveryWorker` performs bounded due scans, expires only legally session-expired prepared/awaiting heads, and reports graph-blocked or concurrent state for explicit recovery. The default agent/MCP profile does not use these kernels; the opt-in local MCP `--auth-*` profile uses the authenticated retrieval kernel and SQLite authorization authority, while explicit durable HTTP and trusted-local MCP profiles select the complete durable facade. The opt-in `DurableFinalizationService` now rechecks authorization/head/policy, retains the complete replay bundle, and CAS-publishes `FINALIZED` with SQLite/PostgreSQL caller-transaction parity. `DurableExecutionService` then verifies that exact retained injection, authorizes and CAS-publishes `EXECUTING`, supports explicit resume/abandonment, authenticates the outcome evaluator, and composes atomic outcome/completion-outbox publication. Peer-authenticated shared-service transport, protected-content encryption, durable transition-event linkage, default adapter cutover, CLI durable selection, and shared-service MCP remain outstanding. Advanced callers retain the lower-level methods when they need pauses, manual retries, or separately owned lifecycle policy.

In explicit durable SQLite/PostgreSQL runtimes, finalization appends
`tbm.usage_decision.finalized` then `tbm.injection.rendered` in the same
transaction as `FINALIZED` and replay projections. Session replay reconstructs
canonical metadata from the ledger and reads exact bytes only from the
authenticated replay authority; default compatibility behavior is unchanged.

The default local loopback HTTP adapter, synchronous/asynchronous Python
clients, and Node.js TypeScript SDK expose the active version-2 lifecycle
through the same strict dispatcher as default MCP. Their bearer secret protects a local process
boundary; it is not the service-identity transport authentication required by
the durable v3 composition. Explicit durable HTTP and trusted-local MCP select
  the durable facade. Synchronous/asynchronous Python clients and the Node.js
  `DurableAgentHTTPClient` select the explicit HTTP profile; remote/shared
  deployment, CLI durable selection, and default-profile cutover remain
  outstanding.

`AuthenticatedDurableAgentMemory` now provides one adapter-neutral facade over
the complete opt-in durable lifecycle. It recovers the original retrieval
scope from retained RetrievalSnapshot authorization linkage, rechecks the
current registry and physical service graph, adds authorized exact-version
cancellation, and requests a fresh transition decision for each post-prepare
GateSession mutation. The facade keeps no process-local lifecycle handles and
has SQLite/PostgreSQL continuation parity. It also resolves replay manifests
from retained session linkage and authorizes bounded replay export without
accepting content-derived lookup IDs. It is not the default Agent/MCP path and
does not itself supply transport authentication. Explicit durable HTTP and
trusted-local MCP adapters select it under separate transport boundaries.

`DurableAgentProtocolDispatcher` now maps that complete facade into the
optional `tbm.durable-agent-wire.v1` Python boundary. Strict request models
exclude all caller/provider/evaluator and scope identities, exact bytes use
canonical base64, and injection/replay content are disabled unless the embedding
adapter explicitly enables them. Callback-compatibility replay rechecks
retained response bytes; when a trusted server-owned invoker is configured,
caller response bytes are not provider provenance and the effect/result receipt
is authoritative. The dispatcher itself authenticates no peer. Explicit durable
  HTTP and trusted-local MCP profiles select it. The Node.js
  `DurableAgentHTTPClient` selects the durable HTTP profile; default compatibility
  HTTP/MCP, `AgentHTTPClient`, and general CLI still use `tbm.agent.v1`.

### 5.2 From Failure to Reusable Lesson

1. Classify a clean failed/errored Trace and create a Failure Case draft.
2. Add reviewed root cause, reviewer, and notes.
3. Bind a fix commit and require a passing regression.
4. Create a scoped, confidence-bounded Lesson with source identity.
5. Inject the Lesson only while it is active, scope-matched, and approved by both gates.
6. Export/import active-only Lesson YAML through fixed limits and dry-run validation.
7. Retire incorrect experience through forward-only obsolescence; obsoleting a source Failure Case atomically cascades to active derived lessons.

### 5.3 PR/CI Regression Assistance

1. Describe old and new prompt, tool, model, or evaluation endpoints with `PRChangeSet`.
2. Discover commit anchors and capture Git ancestry outside the Store lock.
3. Produce related historical cases, fix provenance, regression suggestions, and risk warnings.
4. Fail closed for mixed endpoint provenance or missing ancestry evidence.

### 5.4 Interrupted Run Recovery

1. `memory_run_audits()` classifies each decision as `pending`, `trace_only`, `decision_only`, `complete`, or `conflict`.
2. `memory_run_remediations()` maps those states to `measure`, `recover`, `recover_with_attribution`, `investigate`, or `none`.
3. `recover_ready_memory_runs()` selects and applies currently safe automatic recovery under one lock.
4. Failed or errored Trace-only records require explicit causal attribution; conflicts are never resolved automatically.

## 6. Safety and Trust Model

The product fails closed:

- **Provenance chain:** a Lesson must resolve through a reviewed, verified, regression-backed Failure Case to a failed/errored source Trace and commit; dirty source Traces cannot activate Lessons.
- **Strict scope:** every declared memory scope field must match the current context exactly; a missing field is not a match.
- **Declared-scope matching:** every declared `repo` or `tenant` value is exact, but omission remains broad in snapshot version 2 and is not an authorization boundary.
- **Benchmark leakage protection:** historical memory from the same `(eval_suite, input_hash)` pair is blocked automatically; sensitive and explicitly leaking memory is blocked earlier.
- **Irreversible history:** identities, sources, and populated execution evidence cannot be rewritten; lifecycle transitions only move forward.
- **Atomic writes:** single and batch Trace/decision completion stages and validates all candidates before assignment.
- **Fixed runtime budgets:** 1,000 audited candidates per prepared request, 100,000 candidate references across pending requests, 50 LLM Gate candidates, 20 injected memories, a 32,000-character gate prompt, a 65,536-byte/1,000-node/depth-20 LLM response with a 2,000-character reason, a 12,000-character snippet, and a shared Trace JSON budget of 100,000 nodes plus 8 MiB of key/string UTF-8 text at depth 100.
- **Portable timestamps:** persisted RFC 3339 values require an explicit zone and allow at most six fractional digits.
- **Measured latency:** `latency_ms` is `None` or an integer from 0 through 2,147,483,647.
- **Bounded local documents:** snapshots are capped at 64 MiB, active lessons and CLI JSON at 8 MiB, failure taxonomy at 1 MiB, with record, node, item, and depth limits.
- **Defensive ownership:** the Store uses locking and defensive copies so caller-owned objects cannot mutate internal state.

## 7. Deployment and Integration

### Core Mode

- The core package has no third-party runtime dependency.
- It embeds in existing Python harnesses, evaluation runners, and CI tools.
- JSON snapshot version 2 persists the complete local Store.
- The YAML adapter transports active-only lessons with bounded, all-or-nothing import and text-preserving literal blocks.
- `tbm` and `python -m trace_backed_memory` expose equivalent command surfaces.
- Install `trace-backed-memory[mcp]` to add `tbm-mcp`, a long-running local
  STDIO profile with capability/health, prepare/finalize, completion, and
  cancellation tools. It exposes no curation or activation surface.
- Every snapshot `--write` command locks a canonical sibling `.tbm.lock` before load and holds it through atomic publication. Dry runs and read-only commands remain lock-free.
- Python callers can use `snapshot_write_lock()` around the complete load, mutate, and save transaction.
- `tbm recover-batch` caps decision IDs and attribution options at 10,000 each before snapshot loading and splits attribution values on their final `=`.
- Lesson export uses canonical active-only YAML and no-replace publication by default; lesson import is a bounded validation dry run until `--write`.
- Single and batch obsolescence reuse Store-owned forward-only, idempotent, all-or-nothing transitions and avoid returning sensitive content.
- `pr-report` is read-only and reuses exact change-set matching and externally captured Git ancestry.
- Packaged resources are accessed only through a strict allowlist and the public resource API.

### SQLite Mode

- No extra dependency is required; the adapter uses Python's `sqlite3` module.
- `SQLiteMemoryRepository.connect(path, initialize=True)` creates or opens a file database and applies packaged `schemas/sqlite.sql` at schema version 1.
- `sync()` is additive and atomic, uses `BEGIN IMMEDIATE` for top-level writes, preserves supported forward transitions, and rolls back immutable conflicts.
- `load()` enforces 100,000 rows per collection, 250,000 rows overall, and exact 64 MiB largest-row and aggregate UTF-8 payload limits before returning a validated Store.
- Borrowed connections remain caller-owned, and operations inside a caller transaction use savepoints.
- Public operations on one repository instance are serialized; top-level rollback failures retain the primary error and retry cleanup. An unrecoverable connection is closed even when caller-supplied so a partial transaction cannot be committed later.
- Canonical JSON payload envelopes are an adapter boundary; direct SQL payload mutation and in-place schema migration are unsupported.

### PostgreSQL Mode

- Install `trace-backed-memory[postgres]`.
- Use PostgreSQL 12+ and the fresh-install `schemas/postgres.sql` at schema version 2.
- Upgrade an existing version-1 database with the packaged, atomic `schemas/postgres-v1-to-v2.sql` migration before synchronization.
- The canonical and packaged Trace Schema define `latency_ms` with `minimum: 0` and `maximum: 2147483647`; PostgreSQL uses a non-negative CHECK plus signed `INTEGER`.
- `PostgresMemoryRepository` provides synchronous `sync()` and `load()`, transaction rollback, borrowed/owned connections, and caller-transaction savepoints.
- `load()` locks all five tables before a count preflight and UTF-8 payload preflight. It accepts at most 100,000 rows per collection, 250,000 rows total, and exact 64 MiB row/aggregate boundaries before fetching collection rows.
- `sync()` locks existing rows before canonical validation. Concurrent inserts are retried through nested savepoints and classified as unchanged, supported forward updates, or protected conflicts.
- Database triggers make Failure Case source Trace/commit and Lesson source Case bindings immutable after insertion, including for direct SQL.
- Automatic online migrations beyond the explicit v1-to-v2 script, connection pooling, and async repository access are explicit non-goals.

## 8. Product Maturity

The current release implements roadmap Phases 0 through 73, the local agent/MCP integration increment, and the delivered contract/isolated-authority portions of Phase 74, including opt-in SQLite and isolated PostgreSQL completion outboxes plus their bounded at-least-once delivery worker, the bounded five-view managed-index bundle with exact SQLite/PostgreSQL publication authorities, the opt-in durable retrieval-preparation bridge through Gate evidence verification and GateSession `PREPARED`, durable Semantic Gate composition through `AWAITING_DECISION` to `DECIDED`, opt-in durable finalization through exact UsageDecision/replay retention to `FINALIZED`, authenticated durable execution through exact injection replay, `EXECUTING`, evaluator-authenticated `COMPLETED`, and completion outbox emission, and the adapter-neutral authenticated durable Agent composition over those stages. The main product path has executable README examples, JSON Schemas, SQL invariants, and pytest coverage across Python 3.11, 3.12, 3.13, Windows, SQLite, and required PostgreSQL CI.

The explicit durable runtime can additionally select a server-owned Semantic
provider effect invoker. Trusted reconciliation, owner-fence verification, and
bounded retry/dead-letter require their corresponding configured dependencies.
This is opt-in
provider-effect evidence, not default-path cutover. Request-only claims,
server-attested owner abandonment, request-bound bounded retry/dead-letter,
provider/policy-bound requests, receipt-backed generic compensation for
supporting contracts, and Python/HTTP/MCP/TypeScript invocation parity are
delivered. Semantic provider effects do not support compensation or claim remote
exactly-once. Completion-provider integration, concrete remote adapters,
automatic background sweep/lease fencing, and shared-service workers remain
incomplete; PostgreSQL crash probes exist but were not run on this machine.

[ADR-0006](adr/0006-full-persistence-reducer-native-memory.md) freezes the
next product architecture around a canonical event ledger, content-addressed
artifacts, and versioned deterministic reducers. This is a delivery target, not
an active source-of-truth capability: the reducer/projection kernel is now
available only through an explicit event-ledger operator path, while the
current persistence model remains the durable authority graph and
`full_persistence` remains `false`. Existing durable-v3
authorities are migration assets until event-first import, shadow comparison,
projection rebuild, and cutover evidence are complete.

The executable F0 foundation is now available as three strict contracts:
the strict `tbm.event.v1` canonical envelope; the sealed typed event registry,
strict payload schemas, compatibility matrix, and explicit adjacent upcasters;
and the storage-neutral ledger application port for atomic batch append, exact
idempotent replay, bounded reads, verification, and subscriptions. A repository
guard also registers every current SQLite/PostgreSQL persistence module as
a ledger, replaceable projection, compatibility migration, or bundle
coordinator and rejects unregistered authorities. F1 adds opt-in SQLite and
isolated PostgreSQL event-ledger backends with exact Artifact descriptors,
atomic append/replay, integrity/catalog verification, backup/rollback, and
cross-backend conformance. The F1 foundation alone and default compatibility
paths do not select them; explicit durable F2 lifecycle slices now do. F1 also
delivers the opt-in `tbm.reducer.v1` runtime: versioned descriptors and sealed
registries, bounded canonical state, double-execution determinism checks,
typed-event/upcaster consumption, checkpoint/resume, poison evidence, shadow
comparison, CAS activation, append-only rollback, SQLite/PostgreSQL checkpoint
parity, and explicit metadata-only `tbmd ledger` / `tbmd projection` operator
commands. A single golden projection digest is checked on Python 3.11-3.13 on
Windows and Linux. These commands operate on an explicitly selected event
ledger; they do not select it for the current Gate or Memory lifecycle. See
[Canonical Event v1](protocols/event-v1.md),
[Event Type Registry v1](protocols/event-registry-v1.md), and
[Event Ledger Port v1](protocols/event-ledger-port-v1.md), and
[Reducer and Projection Runtime v1](protocols/reducer-v1.md).

F2 is now in progress. Explicit durable SQLite/PostgreSQL runtimes enable
event-first GateSession revisions: each canonical event and synchronous
revision-row projection share one transaction, and a typed reducer rebuilds
the exact current session. SQLite exercises persistent checkpoint resume,
shadow comparison, activation, rollback, and fieldwise parity. Durable
retrieval preparation also appends compact, exact-digest Artifact-linked
RetrievalSnapshot and SystemGateEvaluation events before the existing evidence
rows, with a pure current-linkage reducer and atomic rollback tests. See
[Gate Evidence Event v1](protocols/gate-evidence-event-v1.md). Semantic Gate
attempts now append compact failed/succeeded events before the attempt and
exact-byte projections, require the retained System Gate parent, and rebuild
with exact event/authority parity. See
[Semantic Gate Attempt Event v1](protocols/semantic-gate-attempt-event-v1.md).
Finalization now appends `UsageDecisionFinalized` and `InjectionRendered` in
the same transaction as `FINALIZED` and replay projections. The
`final-decision-injection` reducer rebuilds exact decision, injection, manifest,
Artifact-role, and event-head linkage. Explicit durable replay export now
derives metadata from the ledger, loads exact bytes only from the authenticated
replay authority, and verifies projection parity. See
[Finalization Event v1](protocols/finalization-event-v1.md) and
[Ledger Replay Export v1](protocols/ledger-replay-export-v1.md). Typed outcome/
attribution and local completion-effect events/reducers now cover exact
RunOutcome, OutcomeAttribution, delivery-history, and dead-letter parity.
The storage-neutral provider transition/reducer/ledger service covers
content-addressed receipts and conservative unknown-result recovery. Explicit
durable runtimes now select server-owned Semantic invocation. When configured,
trusted reconciliation has complete-result receipt binding, owner fencing is
server-attested, and bounded retry/dead-letter revalidates the retained policy
deadline. Request-only safe claims, provider/policy binding, receipt-backed
generic compensation for supporting contracts, and cross-transport server-owned invocation
parity are now covered. Completion-provider integration, concrete remote-provider
adapters, automatic background sweep/lease fencing, shared-service workers,
locally unexecuted PostgreSQL crash probes, and the remaining F2 kill matrix stay
open; Semantic provider compensation and remote exactly-once are not claimed.
The ledger is therefore not yet the sole durable source of truth and
`full_persistence` remains `false`.

The first F3 Trace protocol increment is now available through
`TraceEventRecordRef`, `build_trace_event_batch()`, and
`append_trace_event_batch()`. One registered observation family carries exact
Trace/run ordering, canonical source time, Artifact-only content linkage,
tool/permission/parent/subagent correlation, and one shared command digest for
an atomic batch of at most 100 events. The typed helper verifies the complete
command and trusted ledger context before the generic SQLite/PostgreSQL event
ledgers provide exact append and replay without another schema. An opt-in Codex
CLI `0.146.0` App Server v2 recorder now maps the eleven pinned Hook names,
turn diff updates, and final-answer item completion into that family. Trusted
constructor state owns identity, scope, authorization, time, classification,
and ordering; every mapped frame requires one exact pre-persisted JSON Artifact
descriptor, while unknown methods, requests/responses, realtime transcripts,
guessed direct-Hook payloads, and unknown item variants fail closed. Exact
pending resume covers post-commit response loss. The recorder does not persist
Artifact bytes, build a Trace reducer/projection, enable automatic Hook capture,
or select a default Trace-store cutover.

The F3 Git increment adds eight registered observation types plus an opt-in
`GitObservationEventRecorder`. Checkout/ref/commit/worktree/diff/ancestry/
object-availability/shallow evidence is version-bound and retained through the
same generic SQLite/PostgreSQL ledger. Existing metadata and ancestry capture
functions preserve their return types and default behavior; explicit callers
may supply the recorder to append evidence at capture time. The default reducer
catalog now includes an opt-in conservative Git graph projection for commit,
checkout/ref, pairwise relation confidence/conflict, current missing-object,
and last-observation views. It does not invent direct-parent, force-push,
source/fix/verification, or PR-anchor facts. Automatic Git and diff-Artifact
capture, checkout-binding authority, those missing evidence joins and
consumers, force-push reconciliation, and default Agent/MCP cutover remain
open.

The implemented hardening includes:

- conservative explicit failure-text classification and duplicate-key rejection;
- bounded local documents and a non-configurable live Trace JSON budget;
- exact LLM decision and Git ancestry cardinality limits;
- active-only Lesson portability and forward-only memory obsolescence;
- single/batch measured completion, recovery, remediation, and health metrics;
- consistent PostgreSQL loads with record-count and loaded-row payload preflights;
- standard-library SQLite persistence with atomic additive sync, savepoints,
  bounded payload loads, and Store-level reconstruction validation;
- a runtime-only local STDIO MCP adapter with fixed Git provenance, required
  ancestry capture, strict bounded frames, session-namespaced request handles,
  and process-restart conformance;
- strict v3 mapping/preflight contracts, inert content-addressed bundles,
  immutable SQLite staging, and isolated PostgreSQL staging/rollback;
- concurrent insert revalidation and row-locked lifecycle synchronization;
- portable nonblank persisted strings across Store and JSON Schema boundaries;
- average O(n) snapshot usage-log validation and private O(1) live indexes;
- single-pass Store and Memory Run metrics;
- serialized snapshot CLI writes and a public snapshot advisory lock;
- durable POSIX atomic publication and lock-sidecar identity hardening;
- bounded semantic top-k ranking and bounded Git subprocess capture;
- validated Git metadata outputs and final-delimiter attribution parsing;
- mandatory review/source/clean-worktree promotion invariants across Store,
  JSON Schema, and fresh-install PostgreSQL DDL;
- bounded LLM responses, complete narrowing audit, distinct summary renderers,
  Unicode keyword filtering, and deterministic audited candidate overflow;
- bounded process-local gate state, Trace/run/request audit binding, hardened
  local-file ingestion and SQLite failure cleanup, and PostgreSQL schema v2
  with an atomic v1-to-v2 migration;
- aggregate Gate candidate capacity, serialized SQLite operations with
  failure-resistant top-level rollback, immutable PostgreSQL source bindings,
  strict microsecond-bounded timestamps, and CI lint/type/coverage/audit gates.

The project remains Alpha. Its API is systematic and tested, but long-term backward compatibility and online schema migration are not yet promised.

## 9. Explicit Boundaries and Non-goals

- No general conversational history or personalization memory.
- No direct injection of raw traces, complete prompt history, private tool output, or expected evaluation output.
- No built-in embedding or vector database; callers compute semantic scores.
- Vector similarity is not sufficient evidence of safety or applicability.
- An LLM cannot activate, verify, or reopen memory.
- Snapshot version 2 has no canonical repository ID or explicit global/repository/tenant scope kind; the separate authorization-v3 preparation contract does not make declared-scope matching multi-tenant authorization.
- `regression_passed` is not yet structured run/evaluator evidence.
- Gate requests and finalized tombstones in the default compatibility profile
  remain process-local. Pending requests are bounded and explicitly
  cancellable, finalized tombstones are bounded, high-level requests bind
  Trace/run identity, and final usage logs persist the `request_id`. The
  version-3 GateSession contract has opt-in SQLite and isolated PostgreSQL
  revision repositories. The authenticated retrieval kernel persists and
  rechecks authorization before its callback. Default active-v2 adapters do
  not invoke it; the local MCP `--auth-*` profile and the explicit durable HTTP
  and trusted-local MCP profiles are the active exceptions.
  The opt-in durable retrieval-preparation bridge now creates the same scoped
  GateSession, stores and verifies the paired evidence, and publishes
  `PREPARED`. The durable Semantic Gate composition then verifies the exact
  evidence/attempt chain and advances it through `AWAITING_DECISION` to
  `DECIDED`, with explicit failed-attempt retry and retained-success recovery.
  The opt-in durable finalization composition then rechecks the same
  authorization event, active heads, and policy; retains the exact
  UsageDecision, injection, and replay components; and CAS-publishes
  `FINALIZED`. The opt-in durable execution composition verifies that retained
  injection, requires a current transition authorization, CAS-publishes
  `EXECUTING`, supports exact resume/abandonment, authenticates the outcome
  evaluator, and composes atomic `COMPLETED` plus completion-outbox emission.
  The authenticated durable Agent facade now recovers the original retrieval
  scope from retained evidence, rejects mismatched authority graphs, and
  provides one stateless lifecycle boundary with SQLite/PostgreSQL parity.
  Peer-authenticated shared-service transport, protected-content encryption,
  the remaining lifecycle transition-event linkage, shared-service workers, default adapter
  cutover and CLI durable selection remain out of scope.
- Storage-neutral replay descriptors now define the required retriever/index,
  Gate prompt/response, ancestry, policy, renderer, and exact snippet hashes,
  and the opt-in SQLite replay ledger stores exact bytes/descriptors. Isolated
  PostgreSQL install/rollback resources establish the immutable schema
  lifecycle, and the opt-in PostgreSQL repository provides exact-byte,
  descriptor, idempotency, savepoint, drift, and concurrency parity. The
  opt-in finalization composition authorizes and rechecks one exact scope,
  atomically retains the complete component bundle, and links it to
  `FINALIZED`; the durable facade authorizes session-bound replay reads, and
  explicit durable HTTP/MCP plus Python/TypeScript clients expose them only
  under startup content policy. Active-v2 usage-log projection, protected
  retention/encryption, and shared-service transport remain outstanding.
- The opt-in SQLite and PostgreSQL audit ledgers persist immutable parent-linked AuditEvents
  and atomically pairs RecoveryAction evidence with its matching event. It is
  not an authorization boundary or an atomic Store/GateSession recovery
  service; service-owned identity/transition integration remains outstanding.
  PostgreSQL uses an isolated version-gated schema, row-lock CAS, exact
  catalog/function validation, caller savepoints, and fail-closed rollback
  without changing active schema version 2.
- Git ancestry filtering is opt-in rather than an explicit required/disabled production policy.
- Existing version-2 snapshots with verified but unreviewed cases must be repaired with review evidence before loading; existing PostgreSQL schema-version-1 installations must apply packaged `schemas/postgres-v1-to-v2.sql`. Version-2 databases created before the lesson/source-case lock-order fix must apply the idempotent, version-gated `schemas/postgres-v2-lock-order-hotfix.sql`; fresh installs and the current v1-to-v2 migration already include the fix.
- SQLite uses canonical JSON payload envelopes and does not provide direct-SQL domain mutation, in-place migration, async access, or shared multi-host writer coordination.
- PostgreSQL provides explicit v1-to-v2 and version-2 lock-order operator scripts, not an automatic online migration framework, connection pool, or async repository.
- Outcome metrics are observed associations, not causal estimates for one memory item.
- Conflicting runs expose investigation state and never overwrite a sealed result automatically.

## 10. Success Metrics

Integrating teams can observe:

- candidate, used, and blocked memory counts;
- measured with-memory and without-memory sample sizes and pass rates;
- wrong-memory failures and obsolete-use attempts;
- observed pass rate for each memory item;
- pending, recoverable, attribution-required, and conflict run counts;
- historical failures and suggested regression tests matched by PR changes.

## 11. Related Documentation

- [README / Quick start](../README.md)
- [Architecture](architecture.md)
- [Memory usage policy](usage-policy.md)
- [Product delivery program](product-program.md)
- [MIT License](../LICENSE)
