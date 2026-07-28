# Trace-backed Memory

**English** | [简体中文](reference.zh-CN.md)

This is the detailed API and operations reference moved from the project
homepage. For the shorter introduction, see the [root README](../README.md).

A provenance-backed memory layer for LLM / agent harness engineering.

## One-liner

Trace-backed Memory turns provenance-bound agent traces, eval results, and git commits into verified, scoped, auditable memory that can be used selectively during debug, repair, regression analysis, planning, and production runtime.

[Documentation](index.md) | [Product Overview and Current Capabilities](product.en.md) | [Architecture](architecture.md) | [Memory Usage Policy](usage-policy.md) | [Delivery Program](product-program.md)

## What this is

This project is not generic chatbot memory. It is a harness-oriented memory system:

```text
trace -> failure case -> verified lesson -> gated runtime memory
```

The system is designed around five rules:

1. Trace is the source of truth.
2. Memory is a curated projection derived from trace, eval, and git history.
3. Raw trace should not be injected into prompts by default.
4. Memory must pass both system gate and LLM applicability gate before use.
5. Every memory item must have source, scope, status, and usage logs.

## Core concepts

| Concept | Purpose | LLM-visible by default |
|---|---|---:|
| Trace | Immutable run provenance: prompts, tool calls, outputs, eval, commit | No |
| Failure Case | Structured postmortem of a failed run | Debug / repair only |
| Verified Lesson | Validated reusable rule derived from a case | Yes, if scoped and gated |
| Project Policy | Manually maintained prompt/tool/eval policy | Yes, if relevant |
| Memory Decision | Audit record of why memory was used or blocked | No |

## Reference architecture

```text
Git commit / PR / CI
        |
Harness run
        |
Trace store
        |
Eval result
        |
Failure detection
        |
Failure case draft
        |
Verification / regression
        |
Verified lesson
        |
Memory index
        |
System gate
        |
LLM applicability gate
        |
Runtime injection
        |
Memory usage log
```

## Install / Local Dev

The package uses a `src/` layout. From a checkout, install it in editable mode
before running the examples:

```powershell
python -m pip install -e .
```

Install a built wheel or source-distribution artifact with `pip`. The
distribution is marked as typed with `py.typed`. For one-off local commands from
a checkout, use `$env:PYTHONPATH = "src"` in PowerShell or
`PYTHONPATH=src` in a POSIX shell.

## Agent-facing local runtime

Applications now have a focused entry point that owns Trace registration,
prepare/finalize state, durable repository synchronization, completion, and
stable error envelopes:

```python
from trace_backed_memory import (
    LocalAgentMemory,
    MemoryContext,
    MemoryRunMeasurement,
    capture_local_trace,
)

trace = capture_local_trace(".", tenant="acme", tool_names=("search_docs",))
context = MemoryContext(
    mode="repair",
    repo=trace.repo,
    tenant=trace.tenant,
    commit_sha=trace.commit_sha,
    tool="search_docs",
)

with LocalAgentMemory.open_sqlite("tbm-memory.sqlite3") as memory:
    prepared = memory.prepare(
        trace,
        context,
        task="repair the failing search call",
    )
    finalized = memory.finalize(
        prepared.request_id,
        {
            "use_memory": False,
            "allowed_memory_ids": [],
            "blocked_memory_ids": [],
            "reason": "No prepared lesson is needed.",
            "risk": "none",
            "recommended_injection": "none",
        },
    )
    memory.complete(
        finalized.decision_id,
        MemoryRunMeasurement(eval_result="pass"),
    )
```

Run `tbm capabilities` to discover the `tbm.agent.v1` protocol, storage modes,
operations, and hard limits without loading a snapshot. Prepared gate requests
remain deliberately process-local in the current schema; keep one
`LocalAgentMemory` instance alive through finalize or cancel. Traces, finalized
usage decisions, and measured completion are synchronized to SQLite or
PostgreSQL. See [the agent protocol](protocols/agent-v1.md) and
[Codex integration guide](integrations/codex.md).

### Long-running local MCP

Install the optional profile and configure Codex to launch the runtime-only
STDIO server:

```powershell
python -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm
tbm-mcp --repo-path . --sqlite .tbm/memory.sqlite3
```

`tbm-mcp` exposes capability and health discovery plus prepare, finalize,
complete, and cancel tools. It fixes Git provenance to `--repo-path`, captures
complete ancestry before retrieval, accepts no curation or activation
operations, and requires exactly one explicit storage mode. Pending Gate
requests remain process-local, so Codex must keep the server alive from
prepare through finalize or cancel; a restart requires a new prepare. Opaque
request IDs carry a fresh 128-bit Store-session namespace so stale handles
cannot collide with new requests after restart. See the
[Codex integration guide](integrations/codex.md) for the project
`.codex/config.toml` and the exact tool sequence.

For an opt-in local authorization boundary, provide all five trusted startup
selectors together:

```powershell
tbm-mcp --repo-path . --sqlite .tbm/memory.sqlite3 `
  --auth-registry examples/entity_registry_v3.example.json `
  --auth-sqlite .tbm/authorization.sqlite3 `
  --auth-principal-id principal_tenant_001 `
  --auth-agent-client-id agent_client_001 `
  --auth-environment-id environment_001
```

The bounded registry file supplies the canonical tenant and repository through
the selected active environment. Every prepare persists and reads back its
allow/deny decision before Trace registration or retrieval. MCP request
schemas have no principal, client, tenant, repository, environment, registry,
or authority fields. These CLI selectors are trusted local bootstrap inputs,
not transport authentication or reusable credentials; do not expose this
profile as an untrusted shared multi-tenant service. `--tenant` cannot be
combined with authenticated mode. This profile uses a SQLite authorization
authority independently of the selected runtime storage mode.

## Packaged Resources

Wheel, source-distribution, and editable installs contain byte-identical copies
of every canonical runtime file under `schemas/` and `examples/`, plus the
canonical failure taxonomy and active-lesson YAML example. Contributor control
files such as `AGENTS.md` are not runtime resources. Resource names come from a strict
allowlist of canonical POSIX paths; they never resolve arbitrary filesystem
input:

```text
tbm resource list
tbm resource read schemas/trace.schema.json
tbm resource export schemas/sqlite.sql sqlite.sql
tbm resource export schemas/postgres-v1-to-v2.sql postgres-v1-to-v2.sql
tbm resource export schemas/postgres-v2-lock-order-hotfix.sql postgres-v2-lock-order-hotfix.sql
tbm resource export schemas/postgres.sql postgres.sql
tbm resource export schemas/postgres.sql postgres.sql --overwrite
```

All three commands emit one deterministic JSON value. Export refuses an
existing destination unless `--overwrite` is explicit and uses a
same-directory temporary file before replacement. Unknown names are input
errors; installed package-data failures are internal errors; export failures
use the existing write exit code 4.

Python callers use `packaged_resources()`, `read_packaged_resource()`, and
`export_packaged_resource()` to discover metadata, read exact bytes, or export
a resource without assuming that the package lives on a filesystem:

```python
from trace_backed_memory import (
    export_packaged_resource,
    packaged_resources,
    read_packaged_resource,
)

resources = packaged_resources()
sqlite_sql = read_packaged_resource("schemas/sqlite.sql")
postgres_migration_sql = read_packaged_resource("schemas/postgres-v1-to-v2.sql")
postgres_hotfix_sql = read_packaged_resource("schemas/postgres-v2-lock-order-hotfix.sql")
postgres_sql = read_packaged_resource("schemas/postgres.sql")
postgres_revision_sql = read_packaged_resource("schemas/postgres-v3-memory-revision.sql")
postgres_revision_rollback_sql = read_packaged_resource(
    "schemas/postgres-v3-memory-revision-rollback.sql"
)
export_packaged_resource("schemas/sqlite.sql", "sqlite.sql")
export_packaged_resource("schemas/postgres.sql", "postgres.sql")
```

The allowlist currently contains 94 resources. `PackagedResource` descriptions
include kind, media type, byte size, and
SHA-256. `load_failure_taxonomy()` uses the packaged canonical taxonomy by
default; passing a path continues to load a caller-owned taxonomy file.

## Evidence Ingestion Integrity

Failure extraction treats `Trace.error`, then explicit top-level `error`
fields from `tool_calls` and `tool_outputs`, as ordered failure text. Tool
names never select a failure taxonomy entry; an errored call or output's name
may still label its symptom. Arbitrary arguments, results, nested payloads,
successful tool data, and tool identifiers are never searched for classifier
keywords, so they cannot create a false classification.

A tool-call name may label a tool-failure symptom only when that same call has
truthy top-level `error` evidence; a successful named call is not blamed for a
later Trace or output failure. The generic word `required` is not an argument
error signal by itself. In addition to explicit `invalid argument` text, only
the conservative `required argument`, `required parameter`, `required field`,
and `required property` tool-error markers select `invalid_tool_argument`.

The dependency-free failure-taxonomy and active-lessons YAML adapters reject
duplicate supported fields instead of silently applying last-key-wins
replacement. Duplicate lesson record or scope keys fail while the complete
document is still being parsed. Every resulting lesson is then constructed and
validated against staged state before one all-or-nothing Store commit, so a
duplicate ID or later semantic failure cannot partially import earlier records.
This hardening changes no valid YAML shape, snapshot version 2, JSON Schema, or
PostgreSQL schema version 2.

Caller-owned JSON uses the same no-ambiguity rule. At every nesting level,
`TraceBackedMemoryStore.load_json()`, `parse_memory_context()`,
`parse_memory_decision()`, and every CLI JSON file parser reject duplicate
object keys instead of silently applying last-key-wins. Canonical JSON written
by the package is unaffected. This adds no field and changes no snapshot
version 2, JSON Schema, packaged resource, or PostgreSQL schema version 2.

`save_json()` and `save_lessons_yaml()` publish through a sibling temporary
file: they write canonical LF text, flush it, call `os.fsync()`, and then
publish atomically. Existing Python calls retain `os.replace()` behavior;
`save_lessons_yaml(..., overwrite=False)` uses one `os.link()` publication to
refuse an existing destination without a racy pre-check. On POSIX, a successful
atomic publish then `fsync()`s the parent directory after normal temporary-name
cleanup, making the directory entry durable; non-POSIX platforms retain the
portable atomic-publication behavior. Serialization, temporary-file sync,
link, or replacement failure preserves the previous destination and cleans up
the temporary file. A post-publication parent-directory sync failure is
propagated, but the destination may already expose the new bytes and must be
treated as an indeterminate durability result. Lesson exports use the canonical
`lesson_text: |` block form; imports accept both `|` and the legacy `>` form
while preserving blank lines, leading and trailing LF characters, and
intra-line spaces. This constrained adapter preserves its historical
literal-line behavior for `>`; it does not implement general YAML folding or
chomping. These durability and text-fidelity guarantees add no stored fields:
snapshot version 2, JSON Schemas, and PostgreSQL schema version 2 remain
unchanged.

## Bounded Local Document Ingestion

Bounded local document ingestion applies finite work budgets before semantic
validation. Every caller-owned path is opened once in binary mode and read
through a single file handle up to its byte limit plus one byte, then decoded
as strict UTF-8. This avoids a separate size-check race and rejects oversized
input before decoding or Store mutation.

The safe defaults are:

- snapshot JSON: 64 MiB, 100,000 records per collection, and 250,000 total
  records across the five collections;
- active-lessons YAML: 8 MiB and 10,000 lessons;
- failure-taxonomy YAML: 1 MiB and 1,000 failure types;
- CLI measurement and tool-output JSON: 8 MiB, 10,000 top-level items,
  100,000 JSON nodes, and depth 100;
- `recover-batch` arguments: 10,000 decision IDs and 10,000 attribution
  options.

`TraceBackedMemoryStore.load_json()`, `from_snapshot()`,
`load_lessons_yaml()`, and `load_failure_taxonomy()` expose keyword-only
limits, including `max_bytes` and the relevant record-count options. Each safe
default can be disabled independently with explicit `None` for trusted offline
migrations. CLI commands do not expose that opt-out and always enforce their
safe defaults. Rejected imports remain all-or-nothing. No limit metadata is
persisted: snapshot version 2, JSON Schemas, active-lessons YAML, packaged
resource bytes, and PostgreSQL schema version 2 remain unchanged.

## Bounded Runtime Trace JSON

The Store treats `Trace.retrieved_context`, `Trace.tool_calls`, and
`Trace.tool_outputs` as one structured JSON budget. Together they may contain
at most 100,000 JSON nodes and 8 MiB of UTF-8 text across object keys and string
values, while each structured value retains the depth-100 limit. The validator
checks container width before expanding its traversal stack and rejects
non-UTF-8 strings before defensive copying or persistence.

This fixed boundary applies to direct `record_trace()` calls, Trace completion,
snapshot reconstruction, and PostgreSQL loads. It has no trusted-migration
opt-out because these values become live Store state; input exactly at either
boundary remains valid. Rejection is atomic and changes no snapshot field,
JSON Schema, packaged resource, snapshot version 2, or PostgreSQL schema
version 2.

Snapshot usage-log reconstruction keeps its validation work average O(n) in
records and nested ID/tool evidence. `from_snapshot()` uses load-local indexes
for seen `decision_id` values, known memory IDs, legacy `run_id` resolution,
and per-trace tool names. Per-log candidate/used/blocked relationships use set
membership while reported IDs retain stored order. The indexes are discarded
after loading; validation precedence, error messages, snapshot version 2, and
PostgreSQL schema version 2 do not change.

Normal usage-log writes and lookups use a private derived index from
`decision_id` to stable list position. Decision allocation, duplicate checks,
and single-ID lookup are average O(1); requested batches are average O(k).
The next generated ID still follows the maximum numeric suffix, while imported
nonnumeric IDs remain indexed without advancing it. Failed writes consume no
ID. The derived index is not serialized, canonical snapshot sorting remains in
place, and snapshot version 2 and PostgreSQL schema version 2 do not change.

Live run-to-Trace resolution uses a second private derived index from
`run_id` to an ordered list of `trace_id` values. Unique and ambiguous run IDs
are detected in average O(1) without scanning Trace history; duplicate run IDs
remain valid records but still fail closed when a decision needs one Trace.
`record_trace()` updates the Trace table and index atomically under the Store
lock, and validated snapshot reconstruction rebuilds the index. It is not
serialized, and snapshot version 2 and PostgreSQL schema version 2 do not
change.

Live usage-log memory existence validation now examines only the referenced
IDs. When no load-local `known_memory_ids` set is supplied, each distinct ID is
checked directly against the failure-case, lesson, and project-policy maps in
average O(r), where `r` is the number of referenced IDs. Snapshot import keeps
reusing one `known_memory_ids` set across its logs. No new derived index is
added, unknown IDs remain sorted in errors, and snapshot version 2 and
PostgreSQL schema version 2 do not change.

The Store's `metrics()` now uses one usage-log pass and O(1)
accumulator space for candidate, used, blocked, obsolete, evaluated cohort,
unevaluated, and wrong-memory counts. Pass rates use pass/total counters, so an
empty cohort still returns `None` while a nonempty all-failure cohort returns
`0.0`. Lesson confidence remains a separate lesson-map aggregate.
`memory_outcome_metrics()`, memory-run metrics, and CLI call boundaries remain
unchanged, as do snapshot version 2 and PostgreSQL schema version 2.

`memory_run_metrics()` now uses one usage-log pass without sorting and O(1)
accumulator space. A private single-record audit constructor keeps status and
remediation classification shared with `memory_run_audits()`, while the public
audit and remediation tuples retain decision-ID order. Reported values, lock
boundaries, snapshot version 2, and PostgreSQL schema version 2 are unchanged.

## Snapshot Operations CLI

Installing the package exposes the dependency-free `tbm` console script. The
same command surface is available through `python -m trace_backed_memory`:

```text
tbm capabilities
tbm snapshot validate SNAPSHOT
tbm snapshot stats SNAPSHOT
tbm migration plan-v3 SNAPSHOT_V2 MAPPING_JSON [--repository-root REPOSITORY_ID=PATH]...
tbm migration bundle-v3 SNAPSHOT_V2 MAPPING_JSON [--repository-root REPOSITORY_ID=PATH]...
tbm migration verify-v3-bundle BUNDLE_JSON [--repository-root REPOSITORY_ID=PATH]...
tbm lessons export SNAPSHOT DESTINATION [--overwrite]
tbm lessons import SNAPSHOT SOURCE_YAML [--write]
tbm obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]
tbm obsolete-batch SNAPSHOT REQUESTS_JSON [--write]
tbm audit SNAPSHOT
tbm metrics SNAPSHOT
tbm remediation SNAPSHOT
tbm pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH
tbm outcome SNAPSHOT DECISION_ID --eval-result {pass,fail,error} [--memory-caused-failure true|false] [--write]
tbm complete SNAPSHOT TRACE_ID DECISION_ID --eval-result {pass,fail,error} [--memory-caused-failure true|false] [--output-hash VALUE] [--tool-outputs-file PATH] [--latency-ms INTEGER] [--cost-usd NUMBER] [--error VALUE] [--trace-uri VALUE] [--write]
tbm complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]
tbm recover-ready SNAPSHOT [--write]
tbm recover SNAPSHOT DECISION_ID [--memory-caused-failure true|false] [--write]
tbm recover-batch SNAPSHOT DECISION_ID... [--attribution DECISION_ID=true|false]... [--write]
```

`tbm capabilities` is snapshot-free and returns the agent protocol contract.
Each snapshot-backed command loads one local snapshot through the regular
store validation path. Read commands emit one deterministic JSON value plus a
newline. `migration plan-v3` is a read-only preflight: it validates explicit
canonical repository/tenant bindings, memory authorization scopes, structured
regression evidence, privileged global-policy approvals, and ancestry policy.
Required ancestry is verified against only the explicitly supplied
`--repository-root` Git object databases; omitting a trusted verifier blocks
readiness, while an audited `disabled` policy is reported as a warning.
It reports whether a future coordinated version-3 migration is ready without
emitting or writing a version-3 snapshot.
`migration bundle-v3` freezes the exact source, normalized source digest,
mapping, and plan into an inert content-addressed bundle.
`migration verify-v3-bundle` revalidates every embedded hash and exactly
replays the plan. Bundles may be persisted through
`SQLiteV3MigrationRepository`; they are not runtime snapshots and cannot
activate memory. See
[the staging contract](migrations/v3-staging-bundles.md).

The storage-neutral `tbm.replay.v3` contract can describe exact artifact
bytes, the finalized injection, and a fixed eight-component decision replay
manifest. Complete manifests bind their own canonical hash and every replay
component; legacy partial manifests must name exactly what is absent. The
opt-in `SQLiteReplayV3Repository` stores exact bytes, injection descriptors,
and manifests in an isolated immutable ledger with atomic bundle writes and
fail-closed load verification. Isolated PostgreSQL install/rollback resources
establish the matching immutable relational boundary, while the opt-in
`PostgresReplayV3Repository` supplies canonical descriptor and byte-digest
verification, exact idempotency, nested transaction ownership, and schema
drift checks. Fail-closed removal verifies expected catalog membership.
The current Store, active SQL adapters, local agent, and MCP do not use these
resources, and they provide no access control, encryption, retention, or
GateSession authority, so this remains preparation rather than a claim of
exact replay today. See
[the replay contract](protocols/replay-v3.md).

The storage-neutral authorization-v3 contract defines canonical repositories,
exact tenant-scoped aliases, principals, agent clients, role bindings, and
content-linked allow/deny decisions. Its evaluator keeps authorization
separate from applicability and is intended to run before any retrieval.
Identity and target fields must come from authenticated server-owned context;
decision hashes are content identities, not signatures or reusable
capabilities. `SQLiteAuthorizationV3Repository` and
`PostgresAuthorizationV3Repository` are opt-in isolated authorities that verify
the exact policy/request/decision triple before atomically persisting immutable
policies and decisions with unique request identity. PostgreSQL includes
version-gated install and fail-closed rollback resources. The default Store,
Agent, MCP, and GateSession profiles do not invoke either authority; the
opt-in local MCP `--auth-*` profile invokes the SQLite authority before
retrieval.
`AuthenticatedRetrievalService` is the shared storage-neutral ordering kernel:
trusted service context is matched to the current registry, the exact decision
is persisted and read back, registry rotation and environment binding are
rechecked, and only then may a retrieval callback run. Trusted local MCP
bootstrap integration is available; transport authentication and the other
active adapters remain outstanding. See
[the authorization contract](protocols/authorization-v3.md) and
[authenticated service boundary](protocols/authenticated-service-v3.md).
`AuthenticatedGateSessionService` adds durable `CREATED`-before-preparation
ordering, exact replay suppression, trusted retrieval/System-Gate evidence
verification, `PREPARED` CAS, and explicit cancel-or-recover compensation. See
[authenticated durable Gate preparation](protocols/authenticated-gate-service-v3.md).
`GateSessionRecoveryWorker` performs bounded due scans with exact expiry CAS,
durable read-back, superseded-version classification, and explicit
recovery-required results for lease-only or graph-blocked states. See
[GateSession recovery worker](protocols/gate-recovery-worker-v3.md).
The opt-in SQLite evidence authority atomically retains the exact retrieval
and System Gate pair, while the shared durable verifier binds both records to
the authorized session before `PREPARED`. See
[SQLite Gate evidence v3](protocols/sqlite-gate-evidence-v3.md).

The storage-neutral `tbm.regression-evidence.v3` contract replaces no active
field; it adds the strict target record needed before immutable memory
revisions can be published. It binds separate source and verification traces,
expected and observed outcomes, evaluator/environment provenance, exact commit
relationships, artifacts, independent submitter/verifier principals, and an
attestation hash under a content-derived evidence ID. A passing record is
evidence, not publication authority. See
[the evidence contract](protocols/evidence-v3.md).

The proposal-only `tbm.memory-revision.v3` contract adds immutable,
content-derived revisions with exact parent, content artifact, canonical
scope, case/fix/evidence references, and server-owned proposer context.
Evidence preflight rejects missing, non-passing, cross-case, or
self-conflicted provenance. It does not approve or activate memory; those
remain separate authenticated service operations. See
[the revision contract](protocols/memory-revision-v3.md).

The storage-neutral `tbm.retrieval-snapshot.v3` contract records one exact
authorized retrieval result under a content-derived ID. It binds session,
request, trace, authorization, context/query digests, retriever/index versions,
ordered memory-revision hits, candidate hashes, per-stage scores, deterministic
fusion, and explicit truncation reasons. Similarity remains ranking evidence,
never permission or gate evidence. Active Store/Agent/MCP paths do not emit it
yet. See
[the retrieval snapshot contract](protocols/retrieval-snapshot-v3.md).

The paired `tbm.system-gate-evaluation.v3` and
`tbm.semantic-gate-attempt.v3` contracts record deterministic per-candidate
rules and complete model-attempt provenance without raw prompt/response
content. Cross-record verification requires exact snapshot coverage and
enforces the permanent rule that a model may only narrow System Gate results.
Failed calls remain immutable provenance-only attempts. Active Store/Agent/MCP
paths do not emit these records yet. See
[the gate evaluation contract](protocols/gate-evaluation-v3.md).

The storage-neutral `tbm.run-outcome.v3` and
`tbm.outcome-attribution.v3` contracts bind completed GateSessions to explicit
evaluator and artifact evidence while keeping observed association distinct
from causal claims. Causal attribution requires a non-observational method and
an independent verifier; no exception or score is promoted automatically.
Active v2 outcome fields remain unchanged. See
[the outcome contract](protocols/outcome-v3.md).

The storage-neutral `tbm.audit-event.v3` and `tbm.recovery-action.v3`
contracts add a content-addressed append-only event chain and explicit
recovery-attempt evidence. Recovery verification reuses the derived
`MemoryRunRemediation` and immutable GateSession version; it does not create a
second lifecycle authority. `SQLiteAuditV3Repository` and
`PostgresAuditV3Repository` add opt-in isolated ledgers with exact stream-head
CAS, immutable events, atomic RecoveryAction/event append, scoped
request-digest uniqueness, bounded reads, schema-drift checks, caller
savepoints, and concurrent idempotency. The PostgreSQL adapter adds row-locked
shared-database CAS plus exact install and rollback catalog checks. Neither
adapter is wired to active Store/Agent/MCP paths or performs authorization or
the underlying GateSession/remediation transition. See
[the audit and recovery contract](protocols/audit-recovery-v3.md).

For opt-in local durability of the version-3 lifecycle itself,
`SQLiteGateSessionRepository` uses the separate
`schemas/sqlite-v3-gate-session.sql` contract. It stores append-only canonical
revisions, a one-version-at-a-time CAS head, scoped idempotency, trusted-clock
leases, and bounded due-session discovery. It can share a database file with
the active SQLite v1 Store without changing that schema. The current Agent and
MCP do not use this repository or recover private pending request tokens; see
[the GateSession contract](protocols/gate-session-v3.md).

For opt-in shared-database durability, `PostgresGateSessionRepository` uses
the isolated `trace_backed_memory_v3_gate_session` schema installed by
`schemas/postgres-v3-gate-session.sql`. It samples database time after row
locking and provides scoped idempotency, append-only revisions, exact-version
CAS, catalog drift checks, and caller savepoints. The paired
`schemas/postgres-v3-gate-session-rollback.sql` is fail-closed. Both scripts
require and preserve active PostgreSQL schema version 2; this adapter is not
yet active Agent/MCP state or a claim of distributed service readiness.

Completion and recovery commands return the serialized completions, ordered
decision IDs, and a `written` flag. They are dry-run by default: the input
bytes change only when `--write` is explicit and the complete operation
succeeds. A write reuses the store's same-directory temporary file and atomic
replacement behavior.

`recover-batch` counts submitted values before duplicate detection. It accepts
at most 10,000 decision IDs and 10,000 attribution options, and rejects either
overflow as a structured input error before snapshot loading, Store
construction, recovery, or `--write` publication. The CLI has no limit opt-out;
accepted batches retain strict attribution parsing, request ordering, and the
Store's all-or-nothing recovery rules.

Each `--attribution DECISION_ID=true|false` value uses the final `=` as its
delimiter. The complete non-empty prefix is preserved as the decision ID, so
IDs such as `decision=regional` remain addressable; the suffix must still be
exact lowercase `true` or `false`. Missing delimiters, empty components,
invalid booleans, unrequested IDs, and duplicate attributions remain
structured input errors with exit code 2.

`outcome` is the decision-only adapter for deferred evaluation. It calls
`record_decision_outcome()` once and never completes the linked Trace. Its
result contains only the decision ID, previous/current measured pair,
`changed`, and `written`; it never emits the rest of the usage log, runtime
context, memory IDs, Trace fields, or tool evidence.

`lessons export` writes the Store's active lessons only, in Store order, using
the canonical constrained YAML serializer. It reports `exported_count`,
`exported_lesson_ids`, the destination, and the overwrite choice. Export
refuses any existing filesystem entry unless `--overwrite` is explicit, and
it always rejects a destination that identifies the source snapshot through
the same path, a symbolic link, or a hard link. Empty stores produce exactly
`lessons: []`. The source snapshot is never changed.

`lessons import` reads `SOURCE_YAML` with the fixed 8 MiB and 10,000-lesson
limits, then delegates duplicate-key checks, Lesson construction, shared-ID
collision checks, and Trace/case provenance validation to
`load_lessons_yaml()`. Import merges all records or none; it is not an upsert,
so an existing lesson ID is an input error even when values match. Portable
lesson imports are active-only: any record whose `status` is `obsolete` is an
input error with exit code 2, and a later rejection cannot partially import
earlier records. General Store snapshots still preserve obsolete lifecycle
history. The command returns `imported_count`, source-ordered
`imported_lesson_ids`, and `written`. It is a full validation dry-run by default
and changes the same snapshot only after complete success with explicit
`--write`. CLI callers cannot disable the safe ingestion limits.

`obsolete` performs one forward-only Store lifecycle transition. The explicit
kind selects `obsolete_failure_case()`, `obsolete_lesson()`, or
`obsolete_project_policy()`; the CLI never infers a kind from an ID. A failure
case transition atomically obsoletes all of its active derived lessons, while
unrelated and already-obsolete lessons remain unchanged. Success reports the
record's previous/current status, `changed`, and the sorted
`cascaded_lesson_ids` without exposing record text, scope, Trace data, or tool
evidence. Repeating an already-obsolete record is a successful no-op. The
command is a preview by default and changes the same snapshot only with
explicit `--write`; it cannot reactivate records or attach actor/reason
metadata. The single-item command never synthesizes a batch loop. Multi-record
obsolescence uses the Store-level all-or-nothing command described below.

All three low-level record transition helpers are exported directly from the
`trace_backed_memory` package root. They return replaced records without
mutating their inputs; callers that need lookup, cascade, replay, or atomic
multi-record behavior should use the Store methods instead.

`obsolete-batch` provides that Store-owned atomic boundary. `REQUESTS_JSON` is
strict UTF-8 JSON containing a non-empty array of exact objects with only
`memory_kind` and `memory_id`. Kinds use the canonical `failure_case`, `lesson`,
and `project_policy` values. The bounded parser retains the 8 MiB, 10,000-item,
100,000-node, and depth-100 limits before constructing a tuple of public
`MemoryObsolescenceRequest` records and calling `obsolete_memories()` exactly
once. Unknown fields, duplicate JSON keys, unsupported kinds, and wrong types
are input errors; duplicate or unknown memory IDs reject the whole Store
transition.

The Store resolves every request from the entry state, stages all explicit
records and every active lesson cascaded by a requested failure case, validates
all candidates, and only then updates its collections. Results preserve request
order. An explicitly requested lesson may also belong to a requested case's
cascade; it remains one explicit result and is counted once in
`affected_count`. `cascaded_lesson_ids` is the sorted complete cascade, while
`changed_count` counts explicit records whose status changed. Already-obsolete
records are successful no-ops and the complete batch remains forward-only.

Like the single command, `obsolete-batch` is a dry-run until `--write`
atomically replaces the source snapshot. Its output contains only IDs, kinds,
status changes, counts, and `written`; it never emits memory text, scope, Trace
data, tool evidence, actor, or reason fields. The request manifest is not
persisted.

`complete` submits a fresh measured result for the exact linked Trace and
decision. It requires `--eval-result`; failure attribution defaults to false
and may be stated with `--memory-caused-failure true|false`. The command does
not infer an outcome, ID, attribution, or execution evidence. Optional
`--tool-outputs-file` input must be a UTF-8 JSON array of objects. Omitted
evidence flags preserve compatible evidence already present on the Trace,
while a file containing `[]` supplies an explicit empty tool-output list.
Recovery commands remain limited to outcomes already measured on one side.

`complete-batch` reads `MEASUREMENTS_JSON` as strict UTF-8 JSON containing a
non-empty array of objects. Each object requires `decision_id` and
`eval_result`, may use only the remaining `MemoryRunResult` fields, and must not
supply `trace_id`; the Store derives linkage from each decision. The parser
rejects duplicate object keys, unknown or missing fields, wrong JSON types, and
non-finite numbers before completion. It converts `tool_outputs` arrays to the
immutable tuple boundary, calls `complete_memory_runs()` exactly once, and
returns completions in manifest order. A duplicate decision, unknown decision,
shared-Trace disagreement, or later invalid item rejects the batch
all-or-nothing. Like every mutation command, it is a dry-run until `--write`.

`pr-report` is a read-only CI adapter for the endpoint-aware workflow. Its
strict `CONTEXT_JSON` object requires `mode`, `repo`, and `commit_sha`, accepts
only the remaining `MemoryContext` fields, and rejects unknown keys.
`CHANGE_SET_JSON` uses an exact `field_changes` array whose objects contain
only `field_name`, `old_value`, and `new_value`; endpoint values may be strings
or JSON `null`. The Store still validates supported unique fields, endpoint
bounds, old/new differences, and equality between each new value and the
post-change context. `PRChangeSet` accepts at most 6 entries. The Store rejects
the seventh item before entry shape scanning, then validates accepted field
names in one pass. Oversized CLI input is an input error with exit code 2 and
returns without Git ancestry capture.

The command calls `pr_report_commit_anchors()`, captures Git evidence in the
explicit `--repo-path`, and calls `pr_memory_report()` with the same immutable
`PRChangeSet`. It never accepts legacy broad `changed_fields`, caller-authored
ancestry, or `--write`. Git capture uses `GIT_NO_LAZY_FETCH=1` and an option
terminator before revisions; exit 0 records an ancestor, exit 1 records an
unrelated commit, and other Git failures stop the report. Success emits
`commit_ancestry` and `report` in one deterministic JSON object. Document and
change-set failures use exit code 2; Git capture and report-state failures use
exit code 3.

Failures emit one structured JSON error to stderr without a traceback. Exit
codes are `0` for success or a no-op, `1` for an unexpected internal failure,
`2` for usage/path/encoding/JSON/YAML/snapshot input, `3` for a rejected
completion, recovery, single or batch obsolescence, PR report, Git ancestry,
linkage,
attribution, or evidence state, and `4` for a lesson destination or snapshot
write failure.
Help remains normal human-readable argparse output. Error text is capped at
2,048 characters, and successful JSON is serialized before persistence. If a
downstream pipe closes stdout after an export or `--write` commit, the
already-persisted operation remains a success rather than inviting an unsafe
retry.

Every snapshot mutation requested with `--write` serializes its complete
read-modify-write transaction with a cross-platform exclusive advisory lock.
The persistent sibling `.tbm.lock` sidecar is acquired before snapshot load
and released after atomic publication but before stdout. It is initialized with
one placeholder byte and contains no domain or process data; OS ownership is
released on close or process exit, so crashes do not leave stale ownership.
Before placeholder initialization, the sidecar must be a single-link regular
file. Symbolic links, Windows reparse points, hard links, and special files are
rejected without writing their targets or loading the snapshot.
Acquisition waits for at most 30 seconds; unresolved contention fails before
snapshot load as a write error with exit code 4. Dry runs, read-only commands,
lessons export, and resource export do not take this lock.

Python processes can cooperate with the same advisory lock by wrapping the
complete read-modify-write transaction in the public `snapshot_write_lock()`
context manager:

```python
from trace_backed_memory import TraceBackedMemoryStore, snapshot_write_lock

with snapshot_write_lock("memory-store.json", timeout_seconds=30):
    store = TraceBackedMemoryStore.load_json("memory-store.json")
    # Apply Store mutations while ownership is held.
    store.save_json("memory-store.json")
```

The helper canonicalizes aliases to the same sibling `.tbm.lock`, validates a
finite non-negative `timeout_seconds`, and releases ownership after exceptions
or process exit. Existing sidecars are opened only after no-follow metadata and
descriptor identity checks establish the same single-link regular file; the
identity is checked again after OS lock acquisition and before yielding. It is
advisory and non-reentrant: all cooperating writers must use it, and callers
must pass one held transaction scope down instead of nesting another
acquisition. It is not the Store's in-process `RLock`, a lock inside
`save_json()` alone, or a SQLite/PostgreSQL transaction. This additive API
changes no snapshot version 2, SQLite schema-version-1, or PostgreSQL
schema-version-2 contract.

This interface accepts neither stdin nor remote URLs or SQL repository
connections. Lesson export has one explicit destination; no command accepts an
alternate snapshot output path. It adds no persisted domain, CLI, or report
record:
snapshot version 2, active-lessons YAML, JSON Schemas, and SQL repository
schema versions remain unchanged.

## SQLite Repository

SQLite support uses Python's standard-library `sqlite3` module and requires no
extra package. It is the embedded SQL choice for local harnesses, CI jobs, and
single-host tools:

```python
from trace_backed_memory import SQLiteMemoryRepository, TraceBackedMemoryStore

store = TraceBackedMemoryStore()

with SQLiteMemoryRepository.connect(
    "memory.sqlite3",
    initialize=True,
) as repository:
    result = repository.sync(store)
    restored = repository.load()
```

`initialize=True` applies the packaged `schemas/sqlite.sql` fresh-install
schema at version 1. Operators may instead export and apply those exact bytes:

```powershell
tbm resource export schemas/sqlite.sql sqlite.sql
sqlite3 memory.sqlite3 ".read sqlite.sql"
```

The second command requires the separately installed `sqlite3` command-line
program; Python's standard-library module does not install that executable.

`sync(store)` is additive and atomic. It uses `BEGIN IMMEDIATE` to serialize
writers, preserves supported Trace, Failure Case, Lesson, Project Policy, and
usage-outcome forward transitions, cascades obsolete Failure Cases to active
Lessons, and rolls the entire operation back on immutable conflicts. When a
borrowed connection already has an outer transaction, the repository uses a
savepoint and leaves the final commit or rollback to the caller.
One repository instance serializes `sync()`, `load()`, and `close()` with an
`RLock`, so closing waits for an in-flight operation. A top-level rollback
failure cannot replace the primary error: cleanup is retried, and a connection
that still cannot roll back is closed even when it was supplied by the caller,
because its partial transaction can no longer be trusted.

`load()` reads one SQLite transaction, checks schema version 1, enforces the
same 100,000-per-collection, 250,000-total, and 64 MiB record/aggregate payload
budgets, then reconstructs a normal validated `TraceBackedMemoryStore`. The
SQLite tables store canonical JSON payload envelopes; database-side JSONB and
cross-row policy enforcement remain PostgreSQL strengths. Direct SQL payload
mutation is unsupported and is rejected on the next repository load or sync.
Use a file database for persistence; a `:memory:` database lives only as long
as its owned connection.

## PostgreSQL Repository

PostgreSQL support is optional. Core installs do not import or require
`psycopg`; install the extra when using the synchronous repository:

```powershell
python -m pip install -e ".[postgres]"
pip install 'trace-backed-memory[postgres]'
```

The adapter requires PostgreSQL 12+ because `schemas/postgres.sql` uses
`jsonb_path_exists` in its hardened JSONB constraints.

Before connecting, install the PostgreSQL resource into a fresh `public`
schema. From a checkout, use `schemas/postgres.sql` directly. From any package
installation, export the byte-identical resource first:

```powershell
tbm resource export schemas/postgres.sql postgres.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f postgres.sql
```

The adapter requires the schema metadata row at `schema_version` 2. The SQL
file is a fresh-install schema. Upgrade an existing version-1 installation with
the separately packaged, atomic migration:

```powershell
tbm resource export schemas/postgres-v1-to-v2.sql postgres-v1-to-v2.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f postgres-v1-to-v2.sql
```

The migration adds final Gate `request_id` linkage; immutable Trace, usage
audit, Failure Case source, and Lesson source triggers; guarded forward outcome
transitions; and row-lock protection, then advances the metadata row. It
rejects missing metadata, any starting version other than 1, and replay after a
successful migration.

Existing version-2 databases created before the lesson/source-case lock-order
fix should apply the idempotent, version-gated hotfix:

```powershell
tbm resource export schemas/postgres-v2-lock-order-hotfix.sql postgres-v2-lock-order-hotfix.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f postgres-v2-lock-order-hotfix.sql
```

Fresh installs and the current v1-to-v2 migration already include this fix.

PostgreSQL remains optional for local test runs: the database-backed tests skip
when `initdb`, `pg_ctl`, or `psql` is unavailable. CI sets
`TBM_REQUIRE_POSTGRES=1` in a dedicated `ubuntu-latest` job, installs and
preflights those server tools plus `psycopg`, and then runs both PostgreSQL test
modules against a real private cluster. A separate `windows-latest` job runs the
complete Python suite so platform-specific path, process, and atomic-file
behavior cannot remain Ubuntu-only.

```python
from trace_backed_memory import PostgresMemoryRepository

with PostgresMemoryRepository.connect("postgresql://...") as repository:
    result = repository.sync(store)
    restored = repository.load()
```

`connect()` creates an owned connection, and the context manager closes it. Pass
an existing connection to `PostgresMemoryRepository(connection)` to borrow it;
closing that repository leaves the caller's connection open.

When the supplied connection already has an active caller transaction, each
repository operation uses a nested savepoint and does not commit or roll back
the outer transaction; the caller owns the final commit or rollback. Without an
outer transaction, the repository transaction commits normally.

`sync(store)` is additive and transactional: it inserts records that are absent
and never deletes database records. It preserves supported forward lifecycle
updates, including completing a pending Trace and sealing a previously
unevaluated decision outcome, compares records in canonical form, and rejects
immutable ID conflicts. Every existing target row is selected `FOR UPDATE`, so
a concurrent external writer completes before canonical validation and cannot
slip a protected-field change between validation and a lifecycle write. Any
conflict rolls back the whole synchronization.

A missing primary-key row cannot be locked. Each absent-row INSERT therefore
runs inside a nested savepoint. If a concurrent same-primary-key INSERT commits
during that window, sync recognizes SQLSTATE `23505` or the runtime-memory
registry trigger's exact `P0001` signal, reselects the target `FOR UPDATE`, and
uses the same canonical rules. Exact replay is `unchanged`, a supported forward
transition is `updated`, and a protected difference raises
`PostgresConflictError`. A recognized collision with no target row, including a
cross-kind runtime memory ID collision, and every other driver error remain
sanitized `PostgresPersistenceError` failures. No concurrent value is silently
overwritten.

`repository.load()` returns a normalized, validated
`TraceBackedMemoryStore`, not a snapshot object. Before its five ordered
collection reads, it takes `SHARE` locks on all five persistence tables. Other
readers remain concurrent, while external inserts, updates, and deletes wait
until the load transaction ends; one load therefore cannot combine table rows
from different committed database states, including inside a caller-owned
transaction.

After those locks and before any record query, load runs one five-table
`count(*)` count preflight. It rejects more than 100,000 records in any one
collection or more than 250,000 records in total before a record row is fetched
or decoded. After accepted counts, a second scalar preflight encodes every
loaded row projection as a PostgreSQL JSON object, measures its UTF-8 bytes,
and rejects either a largest row or five-table aggregate above 64 MiB before
any collection row enters psycopg. The failure-case, lesson, and project-policy
projections exclude their internal `updated_at` column because collection
selectors never fetch it; traces and usage decisions select every physical
column. The regular Store snapshot validator repeats the record checks after
the bounded reads.

The payload accounting is a database-load budget, not an exact measurement of
the indented `save_json()` file envelope. Boundary values are accepted. An
overflow is a sanitized `PostgresPersistenceError`, performs no partial load,
and leaves the connection reusable. `sync()` behavior is unchanged because its
Store is already caller-owned client memory. This changes no public API,
snapshot version 2, JSON Schema, active-lessons YAML, packaged resource,
PostgreSQL DDL, or schema version 2.

Persisted identities, linkage, required failure text, lesson/policy scope,
Memory Context values, and usage-audit mapping keys and values must contain at
least one non-whitespace character. The Store applies this contract before a
write or snapshot publication, and the six corresponding canonical and
packaged JSON Schemas publish `pattern: "\\S"`. Accepted strings are preserved
exactly; validation does not trim them. Optional Trace metadata, unrelated
Failure Case narrative fields, and candidate/used/blocked memory-ID arrays keep
their existing non-empty behavior. Snapshot CLI reads classify a rejected
record as an `input` error with exit code 2 and never rewrite the source file.

Persisted timestamps use strict RFC 3339 with an explicit `Z` or numeric UTC
offset. Fractional seconds, when present, contain at most six digits; values
with sub-microsecond precision are rejected consistently by lifecycle APIs,
snapshots, SQLite, PostgreSQL, and the canonical JSON Schemas.

PostgreSQL schema version 2 rejects ordinary-space-only values in these
persisted positions. Its default `btrim(text)` is narrower than Python/JSON
Schema whitespace classification, so direct SQL can still create some tab- or
Unicode-whitespace-only rows that repository load will reject. The supported
Store-to-repository path enforces the stronger portable contract before sync;
this phase changes no PostgreSQL DDL or schema version.

Use the schema owner or the same write-capable role intended for `sync()`.
PostgreSQL 12 requires table-level `UPDATE`, `DELETE`, or `TRUNCATE` privilege
to acquire these `SHARE` locks. When `load()` or `sync()` runs inside a caller
transaction, successful table and row locks remain held until that outer
transaction commits or rolls back; keep caller transactions short when writers
must remain responsive.

## Callback-based Memory Run Execution

Use `run_memory_execution()` for the common synchronous path after registering
the current Trace with `eval_result="unknown"`. Its `MemoryDecisionCallback`
receives a `MemoryGateRequest`; its `MemoryExecutionCallback` receives the
final `GatedMemoryResult` and returns a `MemoryRunMeasurement` without copying
a decision ID:

```python
from trace_backed_memory import MemoryRunMeasurement, run_memory_execution


def decide(request):
    return llm_call(request.prompt)


def execute(gated):
    outcome = harness_run(memory_snippet=gated.snippet)
    return MemoryRunMeasurement(
        eval_result=outcome.eval_result,
        memory_caused_failure=outcome.memory_caused_failure,
        output_hash=outcome.output_hash,
        tool_outputs=(
            tuple(outcome.tool_outputs)
            if outcome.tool_outputs is not None
            else None
        ),
        latency_ms=outcome.latency_ms,
        cost_usd=outcome.cost_usd,
        error=outcome.error,
        trace_uri=outcome.trace_uri,
    )


completion = run_memory_execution(
    store,
    context=context,
    trace_id=current_trace.trace_id,
    task="repair failed search_docs call",
    decide=decide,
    execute=execute,
)
```

The module fixes the order as prepare, decide, finalize, execute, and atomic
complete. It always uses the Store-produced `decision_id` and does not infer an
evaluator result, failure attribution, or execution evidence from an exception.
Preparation errors propagate unchanged because no request has been created.

After preparation, `MemoryRunExecutionError` retains the original callback or
Store exception as its cause and identifies the `decision`, `finalization`,
`execution`, or `completion` phase. Every such error exposes the pending
request; execution and completion failures also expose the finalized result
and decision ID so the caller can retry against the same state or complete
explicitly. Do not rerun the whole one-shot helper as a retry because each call
prepares a new request. `KeyboardInterrupt` and `SystemExit` still pass through.
Use the Store's existing `prepare_memory()`,
`finalize_memory()`, and `complete_memory_run()` directly when advanced callers
need to pause between stages or own custom retry and recovery policy.

Measurements and execution errors are ephemeral. Only the existing Trace and
usage log are persisted, so snapshot version 2, JSON Schemas, active-lessons
YAML, and PostgreSQL schema version 2 remain unchanged.

## Safe Store Workflow

Use the store's two-phase workflow for runtime memory. `prepare_memory()`
retrieves candidates, applies System Gate, and creates the bounded LLM gate
prompt. After the LLM returns a decision payload, `finalize_memory()` rechecks
state, renders the allowed snippet, and records one trace-linked audit event.
The linked current Trace must already exist with `eval_result="unknown"`.
Each LLM response list, `allowed_memory_ids` and `blocked_memory_ids`, accepts
at most 50 memory IDs. The bound is checked before per-ID and duplicate work by
both `parse_memory_decision()` and direct `apply_llm_gate_decision()` calls.
The canonical decision JSON Schema publishes the same `maxItems` contract.

```python
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    query="search_docs null query",
)
result = store.finalize_memory(
    request,
    {
        "use_memory": True,
        "allowed_memory_ids": ["lesson_001"],
        "blocked_memory_ids": [],
        "reason": "The lesson directly matches the current tool failure.",
        "risk": "low",
        "recommended_injection": "short_summary",
    },
    trace_id=trace.trace_id,
)
snippet = result.snippet
# Execute the task with snippet, then evaluate it.
completion = store.complete_memory_run(
    trace_id=trace.trace_id,
    decision_id=result.decision_id,
    eval_result="pass",
    tool_outputs=[{"documents": 3}],
    latency_ms=125,
)
completed_trace = completion.trace
sealed_log = completion.usage_log
(audit,) = store.memory_run_audits()
assert audit.status == "complete"
run_metrics = store.memory_run_metrics()
assert run_metrics.complete_count == 1
assert run_metrics.recoverable_count == 0
```

Only this store workflow provides ownership, replay, stale-state, trace-link,
and atomic logging guarantees.

### Atomic memory-run completion

After execution, `complete_memory_run()` is the preferred way to record the
result. It requires the exact linked `trace_id` and `decision_id`, applies one
measured `eval_result` to both records, validates both candidates under one
store lock, and returns a frozen `MemoryRunCompletion` containing defensive
copies of the completed Trace and sealed usage log.

Both records may be pending, either record may already contain the same result
for partial recovery, or both may already match for exact replay. A conflicting
result, failure attribution, Trace field, or linkage rejects the operation
without changing either record. `complete_trace()` and
`record_decision_outcome()` remain public low-level operations for separately
owned lifecycles and explicit recovery, but normal memory runs should use the
atomic high-level method.

Snapshots persist the existing Trace and usage-log records rather than the
return wrapper. PostgreSQL synchronization updates both rows in one transaction
and rolls the Trace update back if the usage update conflicts. Snapshot version
2, JSON Schemas, active-lessons YAML, and PostgreSQL schema version 2 remain
unchanged.

### Atomic batch memory-run completion

Use `complete_memory_runs()` when an evaluator finishes several new results
that must commit all-or-nothing:

```python
from trace_backed_memory import MemoryRunResult

completions = store.complete_memory_runs(
    (
        MemoryRunResult(
            decision_id="decision_000002",
            eval_result="pass",
            output_hash="sha256:output",
            tool_outputs=({"documents": 3},),
            latency_ms=125,
        ),
    )
)
```

`MeasuredEvalResult` contains only `pass`, `fail`, and `error`. Each frozen
`MemoryRunResult` carries one decision ID, its result and failure attribution,
plus optional Trace evidence. The API requires a non-empty tuple with unique
decision IDs, derives `trace_id` from each validated usage decision, and
preserves request order in defensive `MemoryRunCompletion` results.

For evidence fields, `None` means omitted and preserves an existing value.
`tool_outputs` uses an optional tuple at the request boundary and becomes a
list on the Trace; an explicit empty tuple requests an empty list. Results for
a shared Trace must agree. Evidence fields merge when disjoint or equal, while
an outcome, existing per-decision attribution, immutable-evidence, or same-field
evidence conflict rejects the whole batch before mutation.

`complete_memory_runs()` handles new pending, matching partial, and exact replay
states using the same candidate validators as `complete_memory_run()`. It also
shares its non-mutating staging engine with `recover_memory_runs()`, while the
recovery API remains limited to results already measured on one side.
`MemoryRunResult` is not persisted; snapshots and PostgreSQL store only existing
Trace and usage rows. Snapshot version 2, JSON Schemas, active-lessons YAML, and
PostgreSQL schema version 2 remain unchanged.

### Memory-run audit view

`memory_run_audits()` returns an immutable tuple of frozen `MemoryRunAudit`
values, with one record for every usage decision sorted by `decision_id`. Each
record exposes its linked `trace_id`, `run_id`, raw Trace and decision results,
failure attribution, and one derived status:

| Trace result | Decision result | Status |
|---|---|---|
| unevaluated | unevaluated | `pending` |
| measured | unevaluated | `trace_only` |
| unevaluated | measured | `decision_only` |
| same measured result | same measured result | `complete` |
| different measured results | different measured results | `conflict` |

Use `trace_only` and `decision_only` to locate supported partial recovery for
`complete_memory_run()`. A `pending` run still needs an evaluator result. A
`conflict` exposes incompatible historical low-level writes for review; the
store will never auto-repair it or choose one side as authoritative. Traces
without a usage decision are not memory runs and are omitted, while multiple
decisions for one Trace remain separate records.

The view is derived and not persisted. Snapshot and PostgreSQL round trips
reproduce it from existing Trace and usage-log fields, so snapshot version 2,
JSON Schemas, active-lessons YAML, and PostgreSQL schema version 2 remain
unchanged.

### Memory-run remediation plan

`memory_run_remediations()` turns every audit row into a frozen
`MemoryRunRemediation` with a `MemoryRunRemediationAction`:

```python
remediations = store.memory_run_remediations()
automatic_ids = tuple(
    item.decision_id
    for item in remediations
    if item.action == "recover"
)
attribution_ids = tuple(
    item.decision_id
    for item in remediations
    if item.action == "recover_with_attribution"
)
```

The decision-sorted view maps `pending` to `measure`, a passing `trace_only`
or any `decision_only` record to `recover`, and a failed or errored
`trace_only` record to `recover_with_attribution`. It maps `conflict` to
`investigate` and `complete` to `none`. Shared Trace decisions remain separate
items.

Each item retains the raw audit values. `resolved_eval_result` and
`resolved_memory_caused_failure` contain only values established by current
records. Failed or errored Trace-only records therefore expose the Trace result
but leave resolved attribution as `None`; the default false value on the
unevaluated decision is not treated as causal evidence.

This is an advisory current-state plan. Compatible `recover` items can be sent
to `recover_memory_runs()`, and `measure` items can be evaluated before
`complete_memory_runs()`. Per-decision actions do not promise that different
resolved results for one shared Trace are batch-compatible; both write APIs
revalidate shared-Trace and stale state. Never automatically process
`investigate`, and supply an explicit boolean for every
`recover_with_attribution` item. The plan is derived and not persisted;
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 2 remain unchanged.

### Atomic ready memory-run recovery

Use `recover_ready_memory_runs()` for a race-free maintenance sweep:

```python
completions = store.recover_ready_memory_runs()
```

The method accepts no arguments. Under one reentrant lock in the store it
derives the current remediation plan, selects only `action == "recover"`, and
delegates the decision-ID-sorted tuple to `recover_memory_runs()` without releasing the lock.
It returns defensive `MemoryRunCompletion` values in that order. No ready work
returns an empty tuple, so a repeated successful scan is idempotent and does
not replay complete records.

The sweep automatically handles passing `trace_only` and all `decision_only`
records. It skips `measure`, `recover_with_attribution`, `investigate`, and
`none`: failed or errored Trace-only records still require explicit attribution
through the existing recovery APIs, while pending and conflicting records are
never guessed through.

Ready decisions sharing one Trace must resolve to the same outcome. A shared
result disagreement or any later candidate failure rejects the whole selected
set before mutation. Concurrent sweeps serialize; after one commits, another
re-plans against the completed state. Selection is not persisted, and only
existing Trace and usage rows change. Snapshot version 2, JSON Schemas,
active-lessons YAML, and PostgreSQL schema version 2 remain unchanged.

### Memory-run health metrics

`memory_run_metrics()` returns one frozen `MemoryRunMetrics` point-in-time
summary of the audit view. It counts one usage decision per row, including
separate decisions linked to the same Trace, and exposes `decision_count`,
`pending_count`, `trace_only_count`, `decision_only_count`, `complete_count`,
`conflict_count`, `recoverable_count`, `auto_recoverable_count`, and
`attribution_required_count`.

The five status counts are mutually exclusive and their sum always equals
`decision_count`. `recoverable_count` is the sum of `trace_only_count` and
`decision_only_count`. It also equals `auto_recoverable_count` plus
`attribution_required_count`: the former counts passing Trace-only and all
decision-only records, while the latter counts failed or errored Trace-only
records whose causal attribution is unresolved. Pending runs still need a
measured result and conflicts need manual review.

The summary is derived and not persisted. Empty stores return zero for every
field, and snapshot and PostgreSQL loads reconstruct the same value from the
underlying records. Snapshot version 2, JSON Schemas, active-lessons YAML, and
PostgreSQL schema version 2 remain unchanged.

Aggregation uses one usage-log pass without sorting and O(1) accumulator
space. `memory_run_audits()` and `memory_run_remediations()` still expose
decision-ID order; only the unordered aggregate avoids their presentation
sort and collection materialization.

### Safe memory-run recovery

Use `recover_memory_run()` with an audited `decision_id` when a low-level write
or interrupted process left one side complete:

```python
recoverable = next(
    audit
    for audit in store.memory_run_audits()
    if audit.status == "decision_only"
)
completion = store.recover_memory_run(recoverable.decision_id)
```

The method does not accept `trace_id` or `eval_result`; it derives both from the
validated linked records and returns `MemoryRunCompletion`. For `trace_only`,
the Trace result is authoritative only for the missing decision. For
`decision_only`, the sealed decision result and `memory_caused_failure` are
preserved while the Trace is completed. A `complete` record is an idempotent
exact replay.

Recovery rejects `pending` because no measured result exists and rejects
`conflict` because the two measured results disagree. It never guesses through
either state. A passed `trace_only` run safely implies no memory-caused failure;
a failed or errored `trace_only` run requires the caller to explicitly provide
`memory_caused_failure=True` or `False` because causal attribution is missing.

Optional output hash, tool outputs, latency, cost, error, and trace URI evidence
use the same immutable-slot rules as `complete_memory_run()`. Recovery delegates
to that atomic operation under the same store lock, so every rejection leaves
both records unchanged. It changes only existing persisted fields: snapshot
version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema version 2
remain unchanged.

### Atomic batch memory-run recovery

Use `recover_memory_runs()` when one worker must repair several audited runs as
one all-or-nothing operation:

```python
recoverable_ids = tuple(
    audit.decision_id
    for audit in store.memory_run_audits()
    if audit.status in {"trace_only", "decision_only"}
)
completions = store.recover_memory_runs(recoverable_ids)
```

The first argument must be a non-empty tuple of unique decision IDs. The method
preserves request order in its returned tuple of defensive
`MemoryRunCompletion` values. Every item is classified from state at method
entry: `trace_only`, `decision_only`, and `complete` are eligible, while one
`pending` or `conflict` item rejects the whole batch without mutation.

For each failed or errored `trace_only` item, pass an exact boolean in the
`memory_caused_failures` mapping. Passing `trace_only` defaults to false;
`decision_only` and `complete` preserve sealed attribution unless an equal
value is supplied. Decisions linked to a shared Trace must independently
derive the same result, and a pending decision cannot become eligible through
another item in the same batch.

Batch recovery does not accept `trace_id` or `eval_result`, and it also omits
Trace completion evidence parameters. Use `recover_memory_run()` for one item
when output hash, tool outputs, latency, cost, error, or Trace URI must be
attached. The batch result is not persisted; only existing Trace and usage rows
change. Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 2 remain unchanged.

### Deferred Trace completion

Register the current Trace before memory finalization with all known identity,
input, provenance, retrieved-context, and tool-call evidence plus
an `eval_result` of `unknown`. After execution, `complete_trace()` requires
`pass`, `fail`, or `error` and can fill `output_hash`, `tool_outputs`,
`latency_ms`, `cost_usd`, `error`, and `trace_uri` once.

Omitted completion fields preserve their initial values. Existing non-empty
execution evidence may be repeated exactly but cannot be replaced. Every other
Trace field, including identity, repo/commit/tenant provenance, prompt/tool/model
metadata, input hash, retrieved context, tool calls, and `created_at`, remains
immutable. Exact replay is idempotent; a different completion fails without
mutation.

`complete_trace()` is the low-level Trace-only transition: it never updates a
usage log. Use it when the caller intentionally owns separate audit lifecycles
or needs partial recovery before calling `complete_memory_run()` with the same
measured result. PostgreSQL synchronization supports the same forward Trace
transition and rejects stale, reverse, or conflicting updates atomically.
Snapshot version 2, active-lessons YAML, and PostgreSQL schema version 2 remain
unchanged.

`latency_ms` is either `None` or an integer from 0 through 2,147,483,647;
both boundaries are valid measurements. The shared Trace validator applies
this rule to recording, snapshot loading, callback execution, and single or
batch completion before state is committed. The `complete` and
`complete-batch` CLI paths keep the Store authoritative: an integer outside
that range is a structured `state` error with exit code 3, while malformed
numeric input remains an `input` error with exit code 2. `cost_usd` keeps its
existing finite-number contract.

The canonical and packaged Trace Schema declare `minimum: 0` and
`maximum: 2147483647`. The canonical and packaged fresh-install PostgreSQL DDL
keep the named `traces_latency_ms_non_negative` CHECK, while the existing
signed `INTEGER` column supplies the identical upper boundary. Existing
schema-version-1 databases already enforce that physical maximum and need no
Phase 47 migration; operators missing the earlier lower-bound CHECK still own
that constraint migration. Only the canonical and packaged Trace Schema bytes
change in Phase 47. The current distribution uses the 65-resource allowlist;
snapshot version 2 and PostgreSQL schema version 2 are current.

### Deferred decision outcome sealing

`finalize_memory()` may log an outcome immediately when one is already known.
`record_decision_outcome()` remains the low-level decision-only transition for
separately owned lifecycles and partial recovery; normal runtime callers use
`complete_memory_run()` after execution. The sealable measured results are
`pass`, `fail`, and `error`; initial `None` or `unknown` values are unevaluated.
A measured result and its `memory_caused_failure` value form one outcome pair.

An unevaluated decision can be sealed once. Exact replay of the same pair is
idempotent; a different result, a different failure attribution, a downgrade
to unevaluated, or an invalid wrong-memory claim is rejected without changing
the log. Metrics immediately move the decision out of the unevaluated bucket.

The installed command mirrors this low-level transition:

```text
tbm outcome SNAPSHOT DECISION_ID --eval-result {pass,fail,error} [--memory-caused-failure true|false] [--write]
```

It is a dry-run unless `--write` is explicit. A first seal reports
`changed=true`; exact replay reports `changed=false`. The deterministic output
contains only the previous/current outcome pair, decision ID, and publication
flags. It deliberately excludes context, reason, risk, candidate/used/blocked
memory IDs, System Gate evidence, the linked Trace, and tool output.

JSON snapshots already persist the outcome pair. PostgreSQL synchronization
allows only the same forward pair update while keeping every other usage-log
field, including `created_at`, immutable. A stale or conflicting sync fails and
rolls back atomically. Snapshot version 2, JSON Schemas, active-lessons YAML,
and PostgreSQL schema version 2 remain unchanged.

### Declared Trace provenance binding

At finalization and low-level logging, `repo`, `commit_sha`, and `tenant` always
match the linked Trace. `branch`, `prompt_version`, `prompt_family`,
`tool_schema_version`, `model`, and `eval_suite` bind only when the context
declares them. A declared tool must match an exact plain-string Trace tool call;
non-string tool names are ignored. Omitted optional provenance remains broad
and does not require Trace fields to be absent. `model_family`, `task_type`, and
`failure_type` remain unbound because Trace has no equivalent persisted fields.

Validation happens before pending request consumption or usage-log append, so
a mismatch cannot consume a reusable request or write partial evidence.
Imported version-2 and supplied legacy context evidence follows the same
declared-only rule. The feature reuses existing context and Trace fields;
snapshot version 2 and PostgreSQL schema version 2 remain unchanged.

### Benchmark example leakage classification

Benchmark example identity is the exact pair `(eval_suite, input_hash)`. To opt
in, the caller must choose a stable suite name, canonicalize one benchmark
example deterministically, compute a collision-resistant privacy-preserving
hash, and attach it to the trace for that example. Each trace carries the hash
of its own example, and the current `MemoryContext` must match the current
trace. Source and current traces use the same hash only when they represent the
same canonical example; different examples keep their own hashes. The library
compares strings exactly; hash algorithm, encoding, collision handling,
canonicalization stability, and suite-name stability remain caller
responsibilities.

The complete same-example workflow is executable:

```python
# BENCHMARK_SAFE_WORKFLOW_START
from trace_backed_memory import (
    MemoryContext,
    Trace,
    TraceBackedMemoryStore,
    draft_failure_case,
    lesson_from_failure_case,
    review_failure_case,
    verify_failure_case,
)

BENCHMARK_BLOCK_REASON = "memory originates from current benchmark example"
source_input_hash = "sha256:79e820f10f2b4f322f84307a68a09f62f8342d5c824b86bd1b7f3f6fbebf01f9"
current_input_hash = source_input_hash

store = TraceBackedMemoryStore()
source_trace = store.record_trace(
    Trace(
        trace_id="trace_benchmark_source",
        run_id="run_benchmark_source",
        commit_sha="abc123",
        repo="agent-harness",
        tenant="tenant_a",
        eval_suite="tool_calling_regression",
        input_hash=source_input_hash,
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
    )
)
case = store.add_failure_case(
    verify_failure_case(
        review_failure_case(
            draft_failure_case(
                source_trace,
                case_id="case_benchmark_source",
                failure_type="invalid_tool_argument",
                symptom="search_docs received an empty query",
            ),
            reviewed_by="memory-reviewer",
            root_cause="the prompt omitted the non-empty query contract",
        ),
        fix="require a non-empty query",
        fix_commit_sha="def456",
        regression_passed=True,
    )
)
lesson = store.add_lesson(
    lesson_from_failure_case(
        case,
        lesson_id="lesson_benchmark_source",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={
            "repo": "agent-harness",
            "tenant": "tenant_a",
            "tool": "search_docs",
        },
    )
)
current_trace = store.record_trace(
    Trace(
        trace_id="trace_benchmark_current",
        run_id="run_benchmark_current",
        commit_sha="abc123",
        repo="agent-harness",
        tenant="tenant_a",
        eval_suite="tool_calling_regression",
        input_hash=current_input_hash,
        tool_calls=[{"name": "search_docs"}],
    )
)
context = MemoryContext(
    mode="production",
    repo="agent-harness",
    tenant="tenant_a",
    commit_sha="abc123",
    tool="search_docs",
    eval_suite="tool_calling_regression",
    input_hash=current_input_hash,
)

request = store.prepare_memory(context, task="answer current benchmark example")
assert request.candidate_memory_ids == (lesson.lesson_id,)
assert request.system_allowed_memory_ids == ()
assert dict(request.system_blocked) == {
    lesson.lesson_id: BENCHMARK_BLOCK_REASON
}
assert lesson.lesson_id not in request.prompt
assert source_input_hash not in request.prompt
assert current_input_hash not in request.prompt

result = store.finalize_memory(
    request,
    {
        "use_memory": False,
        "allowed_memory_ids": [],
        "blocked_memory_ids": [],
        "reason": "No eligible memory remains after the System Gate.",
        "risk": "none",
        "recommended_injection": "none",
    },
    trace_id=current_trace.trace_id,
)
usage = store.usage_logs[-1]
assert result.snippet == ""
assert usage.context["input_hash"] == current_input_hash
assert usage.system_blocked_reasons == {
    lesson.lesson_id: BENCHMARK_BLOCK_REASON
}
# BENCHMARK_SAFE_WORKFLOW_END
```

Derived lesson and failure-case candidates are enriched only at runtime with
ephemeral `source_eval_suite` and `source_input_hash`. They are checked against
the complete context pair before LLM narrowing. Candidate `source_eval_suite`
and `source_input_hash` fields are not serialized into prompts or snippets.
The builders do not render structured `input_hash` fields; `eval_suite` remains
ordinary prompt context and may also appear in memory scope. Exact equality
blocks in every mode with the automatic block reason shown above. Static
`sensitive` and `eval_leaking` checks retain precedence and their existing
reasons.

Incomplete identities never trigger a guessed match. `eval_suite` without
`input_hash` remains valid, context `input_hash` without `eval_suite` is
invalid, and an incomplete source trace contributes neither ephemeral field.
A directly constructed partial source pair is a memory contract error.
Different hashes in one suite and equal hashes in different suites do not match.

Finalization enforces context/trace binding for both identity values before it
consumes the request. Usage evidence keeps the current pair, candidate/status
evidence, and automatic block reason. `input_hash` is identity evidence, not
memory scope, so retrieval and lesson/policy scope fields remain unchanged.
Compatibility remains snapshot version 2 and PostgreSQL schema version 2 with
no new persisted memory fields; traces use the existing `input_hash` field,
usage evidence uses the existing context JSON/JSONB, and source identity is
reconstructed rather than stored.

### Semantic retrieval

```python
# Scores are computed by the caller's embedding index or reranker.
semantic_scores = {lesson.lesson_id: 0.93}

request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    semantic_scores=semantic_scores,
    max_candidates=10,
    minimum_score=0.70,
)
```

Metadata scope is applied before ranking. Scores may use any finite numeric
scale, but callers must normalize them so larger values mean greater relevance.
Keyword `query` and `semantic_scores` cannot be combined in one call.
`max_candidates` is required and must be an integer from 1 through 50 inclusive.
All supplied scores and stored-ID references are validated before filtering.
The Store uses a non-copying membership view over its memory catalogs, then
streams eligible records through bounded semantic top-k selection without a
full sort. Results remain score-descending with memory-ID-ascending ties.
System Gate and LLM Gate remain authoritative, and scores are not persisted in
snapshots or PostgreSQL. This changes no snapshot version 2 or PostgreSQL schema
version 2 contract.

### Git ancestry applicability

```python
from trace_backed_memory import capture_commit_ancestry

anchors = store.candidate_commit_anchors(context)
commit_ancestry = capture_commit_ancestry(
    context.commit_sha,
    anchors,
    repo_path=".",
)
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    commit_ancestry=commit_ancestry,
)
```

Discover anchors while reading the store, then capture Git evidence outside the
store lock before calling `prepare_memory()`. The immutable evidence is bound
to the exact `context.commit_sha`: a lesson anchors to its source case's
`fix_commit_sha`, and a failure-case memory anchors to its source
`commit_sha`. Project policies have no commit anchor, so ancestry bypasses
only that filter for them; their normal metadata scope, System Gate, and LLM
Gate checks remain unchanged.

`capture_commit_ancestry()` runs `git merge-base --is-ancestor` for every
anchor. Exit 0 records `True`, exit 1 records `False` and excludes the
anchored history, and any other command error stops the workflow. When callers
provide evidence, it must contain a relation for every discovered anchor;
missing relations fail closed rather than leaving history unfiltered. Omitting
`commit_ancestry` preserves the pre-ancestry retrieval behavior.

`COMMIT_ANCESTRY_MAX_ANCHORS` limits one capture call to 1,000 submitted
anchors. Input entries count before deduplication; overflow is detected while
boundedly consuming an iterable and before any Git command starts. Narrow the
candidate/report scope before capture when a larger history is discovered.

Default Git capture uses `stdin=DEVNULL`, a 30 seconds timeout, binary pipes,
and explicit UTF-8 replacement decoding. Ordinary stdout and stderr retain at
most 64 KiB each; timeout or output overflow stops and reaps the command. The
metadata `git status --porcelain` path retains only the first byte needed for
dirty detection while draining and discarding the remaining output. Injected
runner call signatures, commands, and error mapping remain unchanged. These
runtime limits do not change snapshot version 2 or PostgreSQL schema version 2.

Evidence is request-time input only: it is neither stored in snapshots nor
persisted to PostgreSQL, and it does not replace either gate. PR callers use
`pr_report_commit_anchors(context)`, capture against the same context commit,
and pass that same evidence object to `pr_memory_report()`.

## Endpoint-aware PR reports

Use an immutable `PRChangeSet` when a PR changes trace-backed metadata values.
Each tuple is `(field_name, old_value, new_value)` and supports only
`prompt_version`, `prompt_family`, `tool`, `tool_schema_version`, `model`, and
`eval_suite`. `new_value` must exactly equal the post-change `MemoryContext`
value, including `None`. For PR report selection, repo and tenant are mandatory
exact-match Trace filters, and unchanged declared context metadata continues to
match exactly. This report filter is not a multi-tenant authorization boundary.
Because those six names must be unique, `PRChangeSet` accepts at most 6 entries;
cardinality is checked before entry inspection or historical case scanning.
Accepted field names use one pass for unsupported and duplicate detection.

The store matches every changed field against a complete old endpoint and a
complete new endpoint. It excludes mixed configurations. Report provenance
records `old`, `new`, or `both`; `both` is possible for a tool-only change when
one trace invoked both endpoint tool names. Reuse the same immutable change set
for `pr_report_commit_anchors()` and `pr_memory_report()` so ancestry evidence
is captured for exactly the cases the report can include.

The existing `changed_fields=[...]` input remains supported with its broad,
field-name-only behavior and legacy provenance of `None`, including its
existing `model_family` warning behavior. Exact value-aware `model_family`
matching is unsupported because traces do not store that provenance.
Legacy PR warning fields are validated in one pass before case scanning. The
Store retains the first occurrence of at most 7 supported names; duplicate and
unknown strings remain accepted but cannot increase downstream warning work.
Together with stable set-backed output deduplication, legacy PR warning work is
expected `O(W + C)` for `W` input names and `C` related cases.
`PRChangeSet` values and endpoint provenance are ephemeral report-only values:
they are not persisted and do not change snapshot version 2, JSON schemas,
active-lessons YAML, or PostgreSQL schema version 2.

```python
# PR_CHANGE_SET_WORKFLOW_START
from trace_backed_memory import (
    MemoryContext,
    PRChangeSet,
    Trace,
    TraceBackedMemoryStore,
    capture_commit_ancestry,
    draft_failure_case,
    review_failure_case,
    verify_failure_case,
)

store = TraceBackedMemoryStore()

def add_case(case_id, commit_sha, prompt_version, tool_schema_version):
    trace = store.record_trace(
        Trace(
            trace_id=f"trace-{case_id}",
            run_id=f"run-{case_id}",
            commit_sha=commit_sha,
            repo="agent-harness",
            tenant="tenant_a",
            prompt_version=prompt_version,
            tool_schema_version=tool_schema_version,
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    store.add_failure_case(
        verify_failure_case(
            review_failure_case(
                draft_failure_case(
                    trace,
                    case_id=case_id,
                    failure_type="invalid_tool_argument",
                    symptom="search_docs rejected an empty query",
                ),
                reviewed_by="memory-reviewer",
                root_cause="the prompt omitted the non-empty query contract",
            ),
            fix="require a non-empty query",
            fix_commit_sha=f"fix-{case_id}",
            regression_passed=True,
        )
    )


add_case("case-old", "commit-old", "planner-v1", "search-docs-v1")
add_case("case-new", "commit-new", "planner-v2", "search-docs-v2")
add_case("case-mixed", "commit-mixed", "planner-v1", "search-docs-v2")

context = MemoryContext(
    mode="regression",
    repo="agent-harness",
    tenant="tenant_a",
    commit_sha="pr-head",
    prompt_version="planner-v2",
    tool="search_docs",
    tool_schema_version="search-docs-v2",
    failure_type="invalid_tool_argument",
)
change_set = PRChangeSet(
    (
        ("prompt_version", "planner-v1", "planner-v2"),
        ("tool_schema_version", "search-docs-v1", "search-docs-v2"),
    )
)
anchors = store.pr_report_commit_anchors(context, change_set=change_set)
commit_ancestry = capture_commit_ancestry(
    context.commit_sha,
    anchors,
    repo_path=".",
    runner=lambda _args, _cwd=None: 0,
)
report = store.pr_memory_report(
    context,
    change_set=change_set,
    commit_ancestry=commit_ancestry,
)
# report: case-new/new and case-old/old; case-mixed is excluded.
# PR_CHANGE_SET_WORKFLOW_END
```

## Low-level System Gate Helper

LLM decisions must keep `use_memory`, `allowed_memory_ids`, and
`recommended_injection` consistent: using memory requires an allowed ID and a
non-`none` mode; declining memory requires no allowed IDs and the `none` mode.

```python
from trace_backed_memory import MemoryContext, MemoryItem, system_gate

context = MemoryContext(
    mode="repair",
    repo="agent-harness",
    tenant="tenant_a",
    branch="main",
    commit_sha="abc123",
    prompt_family="planner",
    tool="search_docs",
    tool_schema_version="search_docs_v2",
    eval_suite="tool_calling_regression",
    failure_type="invalid_tool_argument",
)

candidates = [
    MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tenant": "tenant_a", "tool": "search_docs", "prompt_family": "planner"},
        text="When calling search_docs, always provide a non-empty natural-language query.",
        source_case_id="case_001",
    )
]

allowed, blocked = system_gate(context, candidates)
```

## Implemented public API

The package now implements the README pipeline as dependency-free Python objects and helpers:

```python
from dataclasses import replace

from trace_backed_memory import (
    MemoryContext,
    MemoryDecision,
    MemoryRunRemediation,
    MemoryRunResult,
    ProjectPolicy,
    Trace,
    TraceMetadataCaptureError,
    TraceBackedMemoryStore,
    apply_llm_gate_decision,
    build_injection_snippet,
    build_llm_gate_prompt,
    capture_trace_metadata,
    classify_failure_type,
    draft_failure_case,
    draft_failure_case_from_trace,
    export_packaged_resource,
    lesson_from_failure_case,
    load_failure_taxonomy,
    memory_item_from_failure_case,
    memory_item_from_lesson,
    memory_item_from_project_policy,
    obsolete_failure_case,
    obsolete_lesson,
    obsolete_project_policy,
    packaged_resources,
    parse_memory_context,
    parse_memory_decision,
    review_failure_case,
    system_gate,
    verify_failure_case,
)

store = TraceBackedMemoryStore()
try:
    metadata = capture_trace_metadata(repo_path=".")
except TraceMetadataCaptureError as exc:
    raise RuntimeError(f"cannot capture git metadata for memory trace: {exc}") from exc
if metadata.dirty:
    raise RuntimeError("refuse to promote memory from an uncommitted worktree")
taxonomy = load_failure_taxonomy()

trace = store.record_trace(
    Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha=metadata.commit_sha,
        repo=metadata.repo,
        tenant="tenant_a",
        branch=metadata.branch,
        dirty=metadata.dirty,
        prompt_family="planner",
        eval_suite="tool_calling_regression",
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        error="Invalid argument: query is required",
    )
)

failure_type = classify_failure_type(trace, taxonomy=taxonomy)
case = draft_failure_case_from_trace(
    trace,
    case_id="case_001",
    taxonomy=taxonomy,
)
assert case.failure_type == failure_type
reviewed = review_failure_case(
    case,
    reviewed_by="jason",
    root_cause="planner prompt omitted the search_docs query contract",
    review_notes="Confirmed by inspecting failed tool call arguments.",
)
verified = verify_failure_case(
    reviewed,
    fix="added schema example",
    fix_commit_sha="def456",
    regression_passed=True,
)
store.add_failure_case(verified)

lesson = lesson_from_failure_case(
    verified,
    lesson_id="lesson_001",
    lesson_text="When calling search_docs, always provide a non-empty query.",
    memory_type="procedural",
    scope={"repo": metadata.repo, "tenant": "tenant_a", "tool": "search_docs"},
)
store.add_lesson(lesson)

current_trace = store.record_trace(
    replace(
        trace,
        trace_id="trace_002",
        run_id="run_002",
        eval_result="unknown",
        tool_calls=[
            {
                "name": "search_docs",
                "arguments": {"query": "trace-backed memory"},
            }
        ],
        error=None,
    )
)

context = parse_memory_context(
    {
        "mode": "repair",
        "repo": metadata.repo,
        "tenant": "tenant_a",
        "commit_sha": metadata.commit_sha,
        "tool": "search_docs",
        "failure_type": failure_type,
        "eval_suite": "tool_calling_regression",
    }
)
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    query="search_docs null query",
)
result = store.finalize_memory(
    request,
    {
        "use_memory": True,
        "allowed_memory_ids": ["lesson_001"],
        "blocked_memory_ids": [],
        "reason": "The lesson directly matches the current tool failure.",
        "risk": "low",
        "recommended_injection": "short_summary",
    },
    trace_id=current_trace.trace_id,
)
snippet = result.snippet
completion = store.complete_memory_run(
    trace_id=current_trace.trace_id,
    decision_id=result.decision_id,
    eval_result="pass",
    output_hash="sha256:current-output",
    tool_outputs=[{"documents": 3}],
    latency_ms=125,
    cost_usd=0.002,
)
completed_trace = completion.trace
outcome_log = completion.usage_log
metrics = store.metrics()
assert metrics.evaluated_with_memory_count == 1
assert metrics.evaluated_without_memory_count == 0
assert metrics.unevaluated_decision_count == 0
memory_metrics = {
    item.memory_id: item for item in store.memory_outcome_metrics()
}
assert memory_metrics["lesson_001"].observed_pass_rate == 1.0
assert memory_metrics["case_001"].candidate_count == 0

snapshot = store.to_snapshot()
restored = TraceBackedMemoryStore.from_snapshot(snapshot)
store.save_json("memory-store.snapshot.json")
restored_from_disk = TraceBackedMemoryStore.load_json("memory-store.snapshot.json")
store.save_lessons_yaml("lessons.active.yaml", overwrite=False)
lesson_only_store = TraceBackedMemoryStore()
lesson_only_store.record_trace(trace)
lesson_only_store.add_failure_case(verified)
lesson_only_store.load_lessons_yaml("lessons.active.yaml")

pr_report = store.pr_memory_report(context, changed_fields=["tool_schema_version", "eval_suite"])
```

## Outcome-aware metrics

`pass`, `fail`, and `error` are evaluated outcomes; `error` is an evaluated
non-pass. `unknown` and `None` are unevaluated and are excluded from pass-rate
denominators. `evaluated_with_memory_count` and
`evaluated_without_memory_count` expose the two denominators, while
`unevaluated_decision_count` identifies decisions that still lack a usable
outcome. For values returned by `store.metrics()`, their sum equals
`decision_count`; legacy positional construction leaves the appended counts at
their compatible zero defaults.

When evaluation finishes after memory finalization, use
`complete_memory_run()` to complete the Trace and seal the decision before
reading metrics or persisting the completed audit. Use
`record_decision_outcome()` only when the caller deliberately owns the Trace
transition separately. Both transitions update these derived metrics without
persisting separate counters.

These are decision counts, not per-memory causal attribution. A decision is
classified as with-memory when its usage log has non-empty `used_memory_ids`.
Metrics remain derived and are not persisted; snapshot version 2, active-lessons
YAML, JSON Schemas, and PostgreSQL schema version 2 are unchanged.

### Per-memory observations

`memory_outcome_metrics()` returns a memory-ID-sorted tuple for every stored
failure case, lesson, and project policy, including zero-observation records.
`candidate_count`, `used_count`, and `blocked_count` expose retrieval and final
decision frequency; blocked count covers both deterministic and LLM-narrowing
blocks. For actual uses, `evaluated_use_count`, `passed_use_count`,
`failed_or_errored_use_count`, `unevaluated_use_count`, and
`observed_pass_rate` apply the same measured-outcome boundary as global
metrics.

These are observed associations, not causal effectiveness estimates. A run
that uses multiple memory IDs contributes its outcome to every used ID. The API
does not derive per-memory wrong-memory attribution from the decision-level
`memory_caused_failure` flag. Metrics remain derived and are not persisted;
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 2 stay unchanged.

Low-level helpers remain public for callers that own equivalent orchestration,
but only the store workflow provides ownership, replay, stale-state,
trace-link, and atomic logging guarantees:

```python
manual_case = draft_failure_case(
    trace,
    case_id="case_manual",
    failure_type="invalid_tool_argument",
    symptom="planner called search_docs with null query",
)
lesson_memory = memory_item_from_lesson(
    lesson,
    source_trace=trace,
    source_case=verified,
)
case_memory = memory_item_from_failure_case(verified, trace)
policy = ProjectPolicy(
    policy_id="project_policy_001",
    policy_text="Planner responses must include a tool-call rationale.",
    scope={"prompt_family": "planner"},
)
policy_memory = memory_item_from_project_policy(policy)
gate_prompt = build_llm_gate_prompt(context, [lesson_memory], task="repair failed tool call")
old_case = obsolete_failure_case(verified)
old_lesson = obsolete_lesson(lesson)
old_policy = obsolete_project_policy(policy)
```

`capture_trace_metadata()` requires every injected runner result to be a
string. A blank commit SHA, blank repository root, non-string output, commit/
branch/repository name above 512 characters, or other command failure raises
`TraceMetadataCaptureError` at the command boundary without echoing the
malformed value. A blank branch remains valid for detached HEAD and blank
status remains a clean tree. These checks change neither snapshot version 2
nor PostgreSQL schema version 2.

Implemented pieces:

- Core models: `Trace`, `FailureCase`, `Lesson`, `ProjectPolicy`,
  `MemoryUsageLog`, `MemoryRunResult`, `MemoryRunCompletion`, `MemoryRunAudit`,
  `MemoryRunRemediation`, `MemoryRunMetrics`, `MemoryMetrics`, and
  `MemoryOutcomeMetrics`.
- Git metadata capture for repo name, commit SHA, branch, and dirty state, with
  command failures plus malformed injected output wrapped for harness
  diagnostics before invalid metadata reaches Trace construction.
- Git ancestry capture produces immutable, current-commit-bound relations for caller-discovered local commit anchors, with a 1,000-input `COMMIT_ANCESTRY_MAX_ANCHORS` process-work budget before deduplication.
- Trace provenance fields for repo, prompt version, prompt family, tool schema version, model, and eval suite.
- Store-level checks that validate both the incoming and copied trace, preserve copy isolation, reject concurrent copy mutation, and reject empty identity fields, unsupported eval results, or malformed nested JSON trace collections, including non-string object keys, non-finite numbers, reference cycles, and excessive nesting.
- Atomic deferred Trace completion for measured output identity, tool outputs, latency, cost, error, and trace URI evidence, with immutable provenance and input fields, exact replay, copy isolation, and PostgreSQL forward synchronization.
- Atomic memory-run completion by linked `trace_id` and `decision_id`, with one measured result, exact replay, partial recovery, defensive return values, and transactional PostgreSQL synchronization.
- Atomic `complete_memory_runs()` evaluation batches with derived Trace linkage,
  per-run evidence, shared-Trace merging, and all-or-nothing assignment.
- Derived `memory_run_audits()` visibility for pending, one-sided, complete, and conflicting Trace/decision outcomes without new persistence state.
- Derived `memory_run_remediations()` actions with safe resolved recovery
  values, explicit attribution work, stale-state revalidation, and no persisted
  plan.
- Atomic `recover_ready_memory_runs()` sweeps that select and commit current
  automatic recoveries under one lock while skipping unresolved work.
- Dependency-free `run_memory_execution()` orchestration with typed decision
  and execution callbacks, Store-produced linkage, explicit measurement, and
  recoverable post-preparation error context.
- Derived `memory_run_metrics()` health counts with five-state conservation,
  automatic-versus-attributed recovery work, one unsorted usage-log pass, and
  no redundant persisted aggregate.
- Safe `recover_memory_run()` orchestration that derives correlated IDs/results, requires explicit failed-run attribution, and reuses atomic completion.
- Atomic `recover_memory_runs()` orchestration that validates and stages a unique decision tuple before committing any shared-Trace recovery.
- Dry-run measured completion through `tbm complete`, with explicit linked IDs
  and outcome, strict file-backed tool outputs, optional Trace evidence, and
  same-path atomic snapshot replacement on `--write`.
- Ordered all-or-nothing measured batches through `tbm complete-batch`, with a
  strict file-backed `MemoryRunResult` array and Store-derived Trace linkage.
- Lifecycle helpers: failed trace -> validated draft failure case -> verified case -> validated active lesson -> `MemoryItem`.
- Failure extraction helpers that load the failure taxonomy, classify failed
  traces from ordered explicit Trace, tool-call, and tool-output error text,
  and draft failure cases without classifying from tool names or arbitrary
  output content.
- Manual review helper that records reviewer, root cause, notes, and review timestamp on draft failure cases.
- Verification loop hardening: only reviewed drafts can be verified; verified cases require reviewer identity, root cause, review timestamp, a fix commit, and passing regression evidence.
- Obsolete transitions for failure cases and lessons.
- Store-level checks that reject failure cases with empty identity fields, invalid status, incomplete review/fix evidence, a non-failing source Trace, missing source Trace, or source commit mismatch. Active Lessons also reject dirty source Traces.
- Project policy helper that turns manually maintained prompt/tool/eval policy into sourced `MemoryItem` policy memory.
- Deterministic System Gate with strict source, declared-scope, status, memory-type, confidence, sensitivity, eval-leak, and mode checks.
- Gate boundary helpers that validate runtime context JSON and direct-call container/record types before use, require non-empty string tasks and string-or-`None` queries, JSON-quote and cap dynamic gate prompt fields, and bound LLM decision responses to 64 KiB, 1,000 JSON nodes, depth 20, and a 2,000-character reason. Decision ID lists remain capped at 50 each, System Gate cannot be overridden, and every system-approved candidate omitted by the LLM is audited as blocked.
- Distinct injection renderers: `none` emits nothing, `pointer_only` emits identity and scope, `short_summary` emits a 500-character rule, and `full_case_summary` emits up to 2,000 characters of lesson plus reviewed failure/fix provenance when Store-owned case evidence is available.
- In-memory reference store for trace/case/lesson/project-policy records, metadata-first candidate retrieval that requires all declared scope fields to match, optional opt-in Git ancestry filtering before keyword or semantic ranking, debug/repair visibility for verified regression-backed failure cases, Unicode-aware keyword filtering, optional bounded caller-provided semantic scores ranked score-descending with memory-ID-ascending ties, and usage decision logs. The high-level prepare/finalize path deterministically keeps the first 50 system-approved candidates and audits overflow; retrieval cannot bypass either gate.
- Usage-log validation and persisted contract that require trace ID, serialized context, candidate status snapshots, System Gate block reasons, and a reason no longer than 2,000 characters; reject empty identities, duplicate imported decision IDs, invalid mode/risk/injection fields, duplicate, empty-string, or non-string memory ID lists, unsupported eval results, unknown runtime memory IDs, and used or blocked memory IDs outside the candidate set.
- Dependency-free strict JSON snapshot save/load for trace, failure case, lesson, project policy, and usage-log records; non-object snapshots, non-finite floats, over-limit integers, and non-standard JSON numeric constants are rejected while JSON-serializable integer costs remain valid.
- Aggregate runtime validation for Trace structured JSON: 100,000 nodes,
  8 MiB of object-key/string UTF-8 text, depth 100, early wide-container
  rejection, and atomic failure before caller-owned values are copied.
- Dependency-free active lesson YAML save/load for the repository's simple
  `memory/lessons.example.yaml` shape, preserving numeric-looking scope strings
  and rejecting duplicate or semantically invalid documents through
  all-or-nothing Store mutation; saves use synchronized sibling temporary files
  and atomic replacement, and literal blocks preserve exact LF-delimited lesson
  text.
- Zip-safe packaged resource discovery, exact-byte reads, SHA-256 metadata, and
  explicit atomic export for all 94 canonical Schemas, examples, and memory
  support files in wheel, source-distribution, and editable installs.
- Synchronous SQLite and PostgreSQL repositories with additive atomic sync,
  bounded validated loads, forward-only lifecycle updates, and caller-owned
  transaction savepoints.
- Store-level checks that reject lessons with empty identity fields, invalid memory type/status, unknown non-empty scope fields, unbounded confidence, or a missing, unverified, non-regression-backed source case.
- Store-level checks that reject project policies with empty identity/text fields, invalid status, invalid scope, unbounded confidence, or IDs that collide with failure case, lesson, or project policy memory IDs.
- JSON schemas for stored records and full memory-store snapshots.
- Postgres schema parity checks for model defaults, an atomic fresh-install transaction pinned to `public`, invariant functions pinned to `pg_catalog`, a trigger-owned shared runtime memory ID registry that rejects direct DML, `TRUNCATE`, helper-shadow bypasses, and ghost usage, non-empty required text, composite case/trace commit provenance, forward-only status updates, `FOR SHARE` parent/lesson lifecycle serialization and cascades, JSONB object/array and element-type checks, required usage-decision audit evidence, and context example parsing.
- Store preparation bounds each request to 1,000 audited candidates and all
  process-local pending requests to 100,000 candidate references; only the
  deterministic first 50 System-Gate-approved items reach the LLM Gate.
- PostgreSQL load preflights for per-collection/total counts plus largest-row and aggregate UTF-8 payload bytes, including exact boundaries, malformed scalar results, pre-fetch rejection, sanitized errors, and connection reuse.
- PostgreSQL payload accounting over actual loaded-row projections, excluding
  only the unfetched `updated_at` metadata in failure cases, lessons, and
  project policies while retaining compact row-JSON semantics.
- Snapshot lock sidecar validation before placeholder writes, rejecting
  symbolic links, Windows reparse points, hard links, and special files unless
  the canonical path is the same single-link regular file as the descriptor.
- Lesson safety flags for sensitive or eval-leaking memory are preserved through retrieval and blocked by System Gate.
- PR reports can reuse current-commit-bound ancestry evidence to exclude unrelated historical failure cases before generating report content.
- PR/CI helper that reports related verified, regression-backed historical failures from repo-matched traces, includes source/fix provenance, suggests regressions, warns on risky prompt/tool/model/eval-suite changes, and supports immutable complete-endpoint `PRChangeSet` matching with old/new/both provenance.
- Legacy PR warning generation validates names in one pass, retains the first occurrence of at most 7 supported fields, and prevents duplicate or unknown strings from multiplying case-level work.
- Deferred, idempotent decision-outcome sealing plus outcome-aware metrics for decisions, candidates, used/blocked memory, measured pass rates with explicit denominators, unevaluated decisions, wrong-memory failures, obsolete attempts, and lesson confidence.

## Production-readiness boundaries

This release closes the review findings that can be enforced without replacing
the persisted domain model. It does not claim that snapshot version 2 is a
multi-tenant production service contract. The following remain explicit schema
v3 / PostgreSQL schema v3 work:

- replace `regression_passed` with structured regression Trace/run/evaluator evidence and commit relationships;
- add a canonical `repository_id`, explicit global/repository/tenant scope kind, and authorization for global policy;
- persist or cryptographically sign `MemoryGateRequest` state with idempotency,
  expiry, and crash recovery; process-local requests already support bounded
  retention, explicit cancellation, Trace/run binding, and persisted final
  `request_id` audit linkage;
- persist retriever, index, gate model/prompt, ancestry, renderer, response, and snippet hashes for reproducible audit;
- replace opt-in ancestry with an explicit required/disabled policy whose bypass is itself audited.

Until those contracts exist, use this package as a single-process harness
component or reference implementation, not as an untrusted shared multi-tenant
memory service.

This Alpha hardening keeps snapshot version 2 stable while advancing the
PostgreSQL schema to version 2. Existing version-2 snapshots whose verified
cases lack review evidence must be repaired before loading. Existing
PostgreSQL schema-version-1 installations must apply the packaged
`schemas/postgres-v1-to-v2.sql` migration before synchronization.

## Repository layout

Key current paths are shown below; historical design-plan files are omitted.

```text
.
|-- AGENTS.md
|-- .agents/skills/
|   |-- maintain-trace-backed-memory/
|   `-- use-trace-backed-memory/
|-- docs/
|   |-- architecture.md
|   |-- architecture.zh-CN.md
|   |-- development.md
|   |-- development.zh-CN.md
|   |-- index.md
|   |-- index.zh-CN.md
|   |-- integrations/
|   |   |-- codex.md
|   |   `-- codex.zh-CN.md
|   |-- migrations/
|   |   |-- snapshot-v3-preflight*.md
|   |   `-- v3-staging-bundles*.md
|   |-- protocols/
|   |   |-- agent-v1.md
|   |   |-- agent-v1.zh-CN.md
|   |   |-- authorization-v3.md
|   |   |-- authorization-v3.zh-CN.md
|   |   |-- authenticated-service-v3.md
|   |   |-- authenticated-service-v3.zh-CN.md
|   |   |-- authenticated-gate-service-v3.md
|   |   |-- authenticated-gate-service-v3.zh-CN.md
|   |   |-- gate-recovery-worker-v3.md
|   |   |-- gate-recovery-worker-v3.zh-CN.md
|   |   |-- sqlite-gate-evidence-v3.md
|   |   |-- sqlite-gate-evidence-v3.zh-CN.md
|   |   |-- audit-recovery-v3.md
|   |   |-- audit-recovery-v3.zh-CN.md
|   |   |-- evidence-v3.md
|   |   |-- evidence-v3.zh-CN.md
|   |   |-- memory-revision-v3.md
|   |   |-- memory-revision-v3.zh-CN.md
|   |   |-- gate-evaluation-v3.md
|   |   |-- gate-evaluation-v3.zh-CN.md
|   |   |-- outcome-v3.md
|   |   |-- outcome-v3.zh-CN.md
|   |   |-- retrieval-snapshot-v3.md
|   |   |-- retrieval-snapshot-v3.zh-CN.md
|   |   |-- gate-session-v3.md
|   |   |-- gate-session-v3.zh-CN.md
|   |   |-- replay-v3.md
|   |   `-- replay-v3.zh-CN.md
|   |-- product-program.md
|   |-- product-program.zh-CN.md
|   |-- product.en.md
|   |-- product.md
|   |-- usage-policy.md
|   `-- usage-policy.zh-CN.md
|-- examples/
|   |-- agent_*.example.json
|   |-- authorization_*_v3.example.json
|   |-- audit_event_v3.example.json
|   |-- decision_replay_manifest_v3.example.json
|   |-- structured_regression_evidence_v3.example.json
|   |-- gate_session_v3.example.json
|   |-- injection_artifact_v3.example.json
|   |-- quickstart.py
|   |-- snapshot_v3_migration_*.example.json
|   |-- trace.example.json
|   |-- failure_case.example.json
|   |-- lesson.example.json
|   |-- memory_context.example.json
|   |-- memory_revision_v3.example.json
|   |-- retrieval_snapshot_v3.example.json
|   |-- recovery_action_v3.example.json
|   |-- semantic_gate_attempt_v3.example.json
|   |-- system_gate_evaluation_v3.example.json
|   |-- project_policy.example.json
|   |-- memory_usage_log.example.json
|   |-- outcome_attribution_v3.example.json
|   |-- run_outcome_v3.example.json
|   `-- memory_decision.example.json
|-- memory/
|   |-- lessons.example.yaml
|   `-- failure_taxonomy.yaml
|-- schemas/
|   |-- agent_*.schema.json
|   |-- authorization_*_v3.schema.json
|   |-- audit_event_v3.schema.json
|   |-- decision_replay_manifest_v3.schema.json
|   |-- gate_session_v3.schema.json
|   |-- injection_artifact_v3.schema.json
|   |-- structured_regression_evidence_v3.schema.json
|   |-- postgres-v1-to-v2.sql
|   |-- postgres-v2-lock-order-hotfix.sql
|   |-- postgres-v3-gate-session*.sql
|   |-- postgres-v3-replay*.sql
|   |-- postgres-v3-audit*.sql
|   |-- postgres-v3-authorization*.sql
|   |-- postgres-v3-staging*.sql
|   |-- postgres.sql
|   |-- snapshot_v3_migration_*.schema.json
|   |-- sqlite-v3-audit.sql
|   |-- sqlite-v3-authorization.sql
|   |-- sqlite-v3-gate-session.sql
|   |-- sqlite-v3-migration.sql
|   |-- sqlite-v3-replay.sql
|   |-- sqlite.sql
|   |-- trace.schema.json
|   |-- failure_case.schema.json
|   |-- lesson.schema.json
|   |-- project_policy.schema.json
|   |-- memory_usage_log.schema.json
|   |-- outcome_attribution_v3.schema.json
|   |-- run_outcome_v3.schema.json
|   |-- memory_store_snapshot.schema.json
|   |-- memory_context.schema.json
|   |-- memory_revision_v3.schema.json
|   |-- retrieval_snapshot_v3.schema.json
|   |-- recovery_action_v3.schema.json
|   |-- semantic_gate_attempt_v3.schema.json
|   |-- system_gate_evaluation_v3.schema.json
|   `-- memory_decision.schema.json
|-- src/trace_backed_memory/
|   |-- _resources/
|   |-- _ingestion.py
|   |-- _timestamps.py
|   |-- __main__.py
|   |-- __init__.py
|   |-- agent.py
|   |-- capture.py
|   |-- cli.py
|   |-- contracts_v3.py
|   |-- execution.py
|   |-- extraction.py
|   |-- authorization_v3.py
|   |-- service_v3.py
|   |-- gate_service_v3.py
|   |-- gate_worker_v3.py
|   |-- audit_v3.py
|   |-- evidence_v3.py
|   |-- gate_session_v3.py
|   |-- gate_evaluation_v3.py
|   |-- lifecycle.py
|   |-- locking.py
|   |-- mcp_entry.py
|   |-- mcp_server.py
|   |-- migration_v3.py
|   |-- models.py
|   |-- memory_revision_v3.py
|   |-- outcome_v3.py
|   |-- retrieval_v3.py
|   |-- policy.py
|   |-- postgres.py
|   |-- postgres_gate_session_v3.py
|   |-- postgres_replay_v3.py
|   |-- postgres_audit_v3.py
|   |-- replay_v3.py
|   |-- sqlite.py
|   |-- sqlite_audit_v3.py
|   |-- sqlite_authorization_v3.py
|   |-- sqlite_gate_session_v3.py
|   |-- sqlite_replay_v3.py
|   |-- sqlite_v3.py
|   |-- py.typed
|   |-- resources.py
|   `-- store.py
`-- tests/
    |-- test_agent.py
    |-- test_authorization_v3.py
    |-- test_service_v3.py
    |-- test_gate_service_v3.py
    |-- test_gate_worker_v3.py
    |-- test_audit_v3.py
    |-- test_evidence_v3.py
    |-- test_contracts_v3.py
    |-- test_gate_session_v3.py
    |-- test_gate_evaluation_v3.py
    |-- test_mcp_server.py
    |-- test_migration_v3.py
    |-- test_memory_revision_v3.py
    |-- test_outcome_v3.py
    |-- test_retrieval_v3.py
    |-- test_postgres_gate_session_v3.py
    |-- test_postgres_replay_v3.py
    |-- test_postgres_audit_v3.py
    |-- test_replay_v3.py
    |-- test_sqlite_gate_session_v3.py
    |-- test_sqlite_audit_v3.py
    |-- test_sqlite_authorization_v3.py
    |-- test_sqlite_replay_v3.py
    |-- test_quickstart.py
    |-- test_sqlite_v3.py
    |-- test_verify_tool.py
    |-- test_capture.py
    |-- test_cli.py
    |-- test_execution.py
    |-- test_examples_and_schema.py
    |-- test_extraction.py
    |-- test_ingestion.py
    |-- test_lifecycle.py
    |-- test_locking.py
    |-- test_packaging.py
    |-- test_postgres_integration.py
    |-- test_postgres_repository.py
    |-- test_policy.py
    |-- test_readme_api.py
    |-- test_resources.py
    |-- test_sqlite_repository.py
    |-- test_store.py
    |-- verify_distribution.py
    `-- ...
```
