# Architecture

## Goal

Build a trace-backed, commit-aware, gated memory layer for LLM / agent harness engineering.

The system should answer:

- Has this failure happened before?
- Which commit introduced or fixed it?
- Which prompt version, tool schema, model, or eval suite is involved?
- Is there a verified lesson that applies to the current task?
- Should that memory be injected, summarized, or blocked?

## Data flow

```text
1. Harness Run
   ↓
2. Immutable Trace Store
   ↓
3. Failure Case Extraction
   ↓
4. Human / Eval Verification
   ↓
5. Verified Lesson Memory
   ↓
6. Memory Applicability Gate
   ↓
7. Controlled Runtime Injection
   ↓
8. Memory Usage Log
```

## Layer 1: Trace Store

Trace Store records facts. It should be append-only and auditable.

Recommended fields:

- trace_id
- run_id
- commit_sha
- repo
- tenant
- branch
- dirty
- prompt_version
- prompt_family
- tool_schema_version
- model
- eval_suite
- input_hash
- output_hash
- retrieved_context
- tool_calls
- tool_outputs
- eval_result
- latency_ms
- cost_usd
- error
- trace_uri
- created_at

Raw trace is not runtime memory. It is evidence.

The MVP includes `capture_trace_metadata()` for reading repo name, commit SHA,
current branch, and dirty state from git before a harness records the trace.
Prompt version, prompt family, tool schema version, model, and eval suite are
first-class trace fields that callers attach from the harness runtime. Git
command failures are wrapped in a trace metadata capture error that includes
the command and repository path.

The in-memory store can persist a dependency-free JSON snapshot of traces,
failure cases, lessons, project policies, and usage logs. Loading a snapshot
reuses the same recording methods as live writes, so duplicate IDs, global
memory ID uniqueness, and lesson provenance checks remain enforced.

Trace writes require non-empty `trace_id`, `run_id`, and `commit_sha`, and
`eval_result` must be one of `pass`, `fail`, `error`, or `unknown`.
`retrieved_context`, `tool_calls`, and `tool_outputs` must be lists of JSON
objects so downstream extraction and reporting can safely inspect them.
The store validates the caller-owned `Trace`, deep-copies it, validates the
copy again, and only then inserts it. Expected concurrent copy mutation fails
with `ValueError`, while unrelated copy programming errors remain visible.

## Layer 2: Failure Case Store

Failure cases are structured postmortems derived from failed traces.

Fields:

- case_id
- source_trace_id
- commit_sha
- failure_type
- symptom
- root_cause
- reviewed_by
- review_notes
- reviewed_at
- fix
- fix_commit_sha
- regression_passed
- status: draft | verified | obsolete
- created_at

Failure cases are episodic memory.

The MVP includes `load_failure_taxonomy()` plus conservative extraction helpers
that classify obvious trace failures into taxonomy IDs before drafting a case.
When a taxonomy is supplied, classifier output must be present in that taxonomy.
Specific context-missing and stale-context signals take precedence over generic
tool-argument and evaluator-mismatch fallbacks.
`review_failure_case()` keeps ambiguous or heuristic drafts in `draft` status
while recording reviewer, root cause, notes, and timestamp. Only draft cases can
become verified, and a case still needs fix and regression evidence before that
transition.

The in-memory store rejects failure cases whose `source_trace_id` has not been
recorded, and rejects cases whose `commit_sha` differs from the source trace.
It also rejects empty identity fields, unsupported statuses, and `verified`
cases without fix, fix commit, and passing regression evidence. This keeps the
trace record as the provenance anchor for every postmortem.

## Layer 3: Lesson Store

Lessons are validated reusable rules derived from verified cases.

Fields:

- lesson_id
- source_case_id
- lesson_text
- memory_type: procedural | semantic | episodic | policy
- scope_json
- confidence: 0.0 to 1.0
- sensitive
- eval_leaking
- status: active | obsolete
- created_at

Only active, verified, scoped lessons with bounded confidence may be injected
into runtime prompts.

The in-memory store enforces the provenance chain by rejecting lessons whose
`source_case_id` is missing from the store or does not point to a verified,
regression-backed failure case. It also rejects lessons with empty IDs, invalid
memory type or status, empty scope, unknown scope fields, non-string or empty
scope values, or confidence outside the inclusive 0.0 to 1.0 range. Lesson
`sensitive` and `eval_leaking` flags are preserved when lessons become
`MemoryItem` candidates so System Gate can block unsafe memory before LLM
applicability checks.

## Layer 3b: Project Policy

Project policies are manually maintained prompt, tool, or eval rules. They are
not derived from failure cases, but they still need source identity, scope,
status, and safety flags before they can be considered for injection.
`ProjectPolicy` and `memory_item_from_project_policy()` provide the MVP bridge
from maintained policy records to sourced `MemoryItem` policy memory. The
in-memory store rejects policies with empty IDs or text, invalid status, invalid
scope fields, confidence outside the inclusive 0.0 to 1.0 range, or IDs that
collide with the shared runtime memory ID namespace across failure cases, lessons, and project policies.

For the MVP, `TraceBackedMemoryStore.to_snapshot()`, `from_snapshot()`,
`save_json()`, and `load_json()` provide a stable full-store persistence
boundary for traces, failure cases, lessons, project policies, and usage logs.
The boundary requires a JSON object, accepts JSON-serializable integer costs,
rejects non-finite floats and integers beyond the runtime serialization limit,
keeps confidence bounded to 0.0 through 1.0, and parses strict JSON without
`NaN` or infinity constants.
`save_lessons_yaml()` and `load_lessons_yaml()` provide a small dependency-free
adapter for active lessons using the repository's `memory/lessons.example.yaml`
shape; loading still reuses `add_lesson()` so source-case and lesson-contract
checks remain enforced. YAML serialization quotes strings so numeric-looking
scope values remain strings when loaded.

## Layer 4: Memory Gate

Memory use requires two gates:

```text
System Gate -> LLM Gate
```

System Gate is deterministic and blocks unsafe or invalid memory.

Runtime contexts can enter the system through `parse_memory_context()`, a
dependency-free validator for JSON strings or mappings. It enforces the required
`mode`, `repo`, and `commit_sha` fields, validates supported modes, and drops
unknown fields before a `MemoryContext` reaches retrieval or gating.
All public gate helpers validate contexts, list containers, `MemoryItem`
records, unique IDs, and string mappings before iterating, sorting, or reading
record fields. Gate tasks must be non-empty strings, context summaries must be
strings, and retrieval queries must be strings or `None`; malformed direct
calls fail with `ValueError` before a store request ID is consumed.
Direct low-level application also rejects any ID present in both the System
Gate allowed list and blocked map, so deterministic blocks cannot appear in a
final allowed result.

LLM Gate judges semantic usefulness after System Gate has filtered candidates.

LLM Gate output is parsed through a strict dependency-free validator before it
is applied. Invalid fields, unknown enum values, or non-string memory IDs are
rejected before runtime injection is built. Decisions must also keep
`use_memory`, `allowed_memory_ids`, and `recommended_injection` consistent:
using memory requires at least one allowed ID and a non-`none` injection mode,
while declining memory requires an empty allowed list and `recommended_injection`
set to `none`.
Task text, context summaries, and candidate memory text in the gate prompt are
JSON-quoted and capped so long or instruction-like dynamic inputs stay data,
not prompt structure.
Runtime injection honors the parsed `recommended_injection` mode: `none` emits
no snippet, `pointer_only` emits IDs/source/scope without lesson text, and
summary modes JSON-quote and cap injected text.

Runtime output is bounded by fixed contract constants:
`MEMORY_ID_MAX_CHARS` is 128, `METADATA_VALUE_MAX_CHARS` is 512,
`LLM_GATE_MAX_CANDIDATES` is 50, `LLM_GATE_PROMPT_MAX_CHARS` is 32,000,
`INJECTION_MAX_MEMORIES` is 20, and `INJECTION_SNIPPET_MAX_CHARS` is
12,000. Identifier and metadata limits are enforced before rendering; total
prompt and snippet limits are checked before either value is returned.

Candidate retrieval is metadata-first. The in-memory MVP retrieves lessons and
project policies when every declared scope metadata field matches the current
context. In debug and repair modes, it also exposes verified,
regression-backed failure cases by deriving runtime memory scope from the
source trace plus failure type. Retrieval can then apply an optional keyword
query. Keyword overlap is only a retrieval aid and does not replace System Gate
or LLM applicability checks. Short domain tokens such as `AI` and `v2` are
preserved in keyword filtering.

Callers may alternatively provide precomputed semantic scores keyed by stored
runtime memory ID. Semantic mode remains metadata-first, requires an explicit
integer `max_candidates` from 1 through 50 inclusive, accepts only finite
numeric scores, and breaks ties by
memory ID. Scores select candidates only; System Gate and LLM Gate remain the
approval boundary. The store neither computes nor persists embeddings or raw
scores.

The safe store workflow is `prepare_memory()` followed by `finalize_memory()`.
Preparation performs retrieval, System Gate, and bounded LLM prompt creation;
finalization rechecks current state, narrows the LLM decision, renders the
snippet, and atomically records trace-linked evidence. Only this workflow
provides ownership, replay, stale-state, trace-link, and atomic logging
guarantees. Low-level helpers remain public for callers that own equivalent
orchestration.

Usage logs persist a non-empty trace ID, serialized context, candidate IDs,
candidate status snapshots, System Gate blocked reasons, used IDs, blocked IDs,
risk, reason, recommended injection, optional eval result, and whether memory
was attributed as the cause of a failure. These fields feed pass-rate and
wrong-memory metrics. The in-memory store rejects usage logs whose used memory
IDs were not present in the recorded candidate set, whose identity fields are
empty, whose imported decision IDs are duplicated, whose memory ID lists contain
duplicate, empty-string, or non-string memory IDs, whose used and blocked IDs
overlap, whose used or blocked IDs were not recorded candidates, or whose mode, risk,
recommended injection, or optional `eval_result` values are unsupported.

The JSON Schema requires the four safe-workflow audit fields for persisted
usage logs while Python keeps defaults to migrate exact legacy snapshots.

### Benchmark example identity boundary

Benchmark leakage identity is the exact pair `(eval_suite, input_hash)`.
Callers choose a stable suite name, canonicalize one benchmark example
deterministically, compute a collision-resistant privacy-preserving hash, and
attach it to the trace for that example. Each trace carries the hash of its own
example, and the current `MemoryContext` must match the current trace. Source
and current traces use the same hash only when they represent the same
canonical example; different examples keep their own hashes. Exact comparison
is the library boundary; digest selection, encoding, collision handling,
canonicalization stability, and suite-name stability are caller
responsibilities.

The store resolves lesson provenance through lesson -> failure case -> trace
and enriches lessons and failure cases with ephemeral `source_eval_suite` and
`source_input_hash`. These values are used only by runtime contract validation
and the System Gate. Candidate `source_eval_suite` and `source_input_hash`
fields are not serialized into prompts or snippets. The builders do not render
structured `input_hash` fields; `eval_suite` remains ordinary prompt context
and may also appear in memory scope. Complete pair equality blocks in every mode with
`memory originates from current benchmark example`. Static `sensitive` and
`eval_leaking` checks retain precedence and their existing reasons.

Incomplete identities never trigger a guessed match. An eval suite without a
context input hash preserves legacy behavior, a context input hash without an
eval suite is invalid, and incomplete trace provenance enriches neither source
field. A partial pair supplied directly on a `MemoryItem` is a contract error.
Different hashes in one suite and equal hashes in different suites remain
eligible under this rule.

Finalization provides context/trace binding by requiring the current trace to
match both values before request consumption. Existing usage evidence records
the current pair, candidate IDs/statuses, and the automatic block reason.
`input_hash` is identity evidence, not memory scope; metadata retrieval does not
filter or scope lessons and policies by this field. Persistence remains
snapshot version 2 and PostgreSQL schema version 1 with no new persisted memory
fields: trace identity uses existing trace columns, current identity and the
block reason use existing usage JSON/JSONB, and source identity is rebuilt from
the trace/case/lesson graph.

Trace context/tool JSON is validated recursively before storage. Only JSON
semantic values with string object keys and finite numbers are accepted;
cycles, excessive nesting, and values that cannot be serialized by the runtime
fail with a path-specific `ValueError`.
PostgreSQL rejects empty required identifiers and text, uses composite
`(source_trace_id, commit_sha)` provenance to bind cases to their source trace,
requires non-null lesson and policy confidence, and checks audit JSONB objects
and values. Failure-case, lesson, and project-policy IDs are immutable and their
records cannot be deleted. The shared runtime ID registry rejects direct DML;
only schema-qualified source-table triggers can register IDs, and usage evidence
must resolve both a registry entry and its concrete source row.
The fresh-install DDL runs in one transaction with a local
`public, pg_catalog` search path. Statement triggers reject `TRUNCATE` on the
registry and all three runtime-memory source tables, and `TRUNCATE` is revoked
from `PUBLIC`, preserving registry/source parity. The file is a fresh-install
schema, not an in-place migration for an already deployed database.
The schema requires PostgreSQL 12+ because its hardened JSONB shape constraints
use `jsonb_path_exists`.

Every SQL and PL/pgSQL invariant function executes with
`search_path = pg_catalog`; application relations remain explicitly qualified
as `public.*`, preventing caller-owned helper functions from changing checks.
Status INSERTs remain valid for snapshot restoration, while UPDATEs are
forward-only (`draft -> verified|obsolete`, `verified -> obsolete`, and
`active -> obsolete`, with same-state updates allowed). Active lesson validation locks the verified,
regression-backed parent case `FOR SHARE`, so concurrent lesson insertion and
parent obsoletion serialize; parent obsoletion atomically cascades active lessons
to obsolete. A wrong-memory failure requires used memory plus a non-null `fail`
or `error` result.

The SQL schema is kept aligned with the dataclass contracts through tests for
model defaults, required usage-decision audit fields, JSONB object/array and
element-type checks, and the runtime memory context example. A dependency-free
integration test loads the complete DDL into a temporary PostgreSQL cluster and
uses controllable advisory-lock latches across real sessions to verify both
lifecycle lock orderings without timing sleeps. It also verifies failed-install
rollback, non-owner helper shadowing, non-default caller search paths, and
independent client/server/directory cleanup.
Portable JSON Schema files document trace, failure case, lesson, project policy,
usage log, and full snapshot shapes; cross-record provenance checks still live
in the store because they require current store state.

## PostgreSQL Runtime Repository

`PostgresMemoryRepository` is the implemented synchronous persistence boundary
for a complete `TraceBackedMemoryStore`. It is public from the package root,
but its `psycopg` dependency remains optional and lazy: core package import does
not import the driver, while `PostgresMemoryRepository.connect()` requires the
`postgres` extra.

The repository operates only on a fresh `public` schema installed from
`schemas/postgres.sql`. It locks the one schema metadata row and requires schema version 1.
This schema is not an in-place migration mechanism.

`sync(store)` first snapshots the in-memory store, then opens one database
transaction and locks schema metadata `FOR UPDATE`. Synchronization is additive:
it inserts absent records, retains database records that are not in the supplied
snapshot, and never performs destructive reconciliation. Existing records are
compared in canonical form before a write. Immutable ID conflicts abort the
operation, so the transaction rolls back every earlier insert or lifecycle
update in that synchronization.

The repository treats traces and usage logs as immutable: an existing row must
be canonically equal to the incoming record or synchronization conflicts.
Failure cases may update only diagnosis (`failure_type`, `symptom`, and
`root_cause`), review (`reviewed_by`, `review_notes`, and `reviewed_at`), fix and
regression (`fix`, `fix_commit_sha`, and `regression_passed`), and `status`.
Their identity, source provenance, and creation timestamp remain immutable. For
existing rows, lessons and project policies may update only `status`; a
difference in any other field is a conflict.

Database triggers still enforce forward-only status transitions. Failure cases
may move from `draft` to `verified` or `obsolete`, and from `verified` to
`obsolete`. Lessons and project policies may move from `active` to `obsolete`.
Same-state writes remain valid, obsolete records cannot be reactivated, and
obsoleting a failure case cascades its active lessons to obsolete. Other parent
updates that would leave an active lesson without a verified,
regression-backed source case are rejected.

`load()` opens a transaction, locks schema metadata `FOR SHARE`, reads the
persisted collections, normalizes their database representation into the
canonical snapshot shape, and reconstructs the store through its normal
validation path. It therefore rejects database data that cannot form a valid
store rather than returning partial or unvalidated records.

`PostgresMemoryRepository(connection)` borrows a caller-provided connection;
`close()` and context-manager exit do not close that borrowed connection.
`PostgresMemoryRepository.connect(...)` creates an owned connection, and its
context manager closes it. The repository does not provide connection pooling.
When the supplied connection already has an active caller transaction, each
repository operation uses a nested savepoint and does not commit or roll back
the outer transaction; the caller owns the final commit or rollback. Without an
outer transaction, the repository transaction commits normally.

## Layer 5: PR / CI Memory Report

The in-memory MVP can generate a PR-oriented memory report from the same trace
and failure-case stores. It only treats verified, regression-backed failure
cases with trace `repo` and `tenant` exactly matching the current context as
reportable historical failures. Traces without repo provenance are not eligible
for PR reports. The report suggests targeted regression tests and emits
warnings when prompt, tool schema, tool, model, or eval-suite changes touch
known failure areas. It also includes case-level provenance records with source
trace ID, source commit, fix commit, trace URI, failure type, and optional
endpoint-match provenance.

`pr_memory_report()` accepts exactly one change input. The legacy
`changed_fields` list retains its existing broad field-name-only matching,
warning order, and `None` endpoint provenance, including legacy
`model_family` warning behavior. Value-aware matching instead accepts an
immutable `PRChangeSet` of `(field_name, old_value, new_value)` tuples. Store
boundaries validate an exact non-empty tuple shape, supported unique field
names, non-blank bounded endpoint strings or `None`, and different old/new
values. The only supported exact-provenance fields are `prompt_version`,
`prompt_family`, `tool`, `tool_schema_version`, `model`, and `eval_suite`.
`model_family` is rejected for a change set because traces do not store exact
model-family provenance.

Every change-set new value must exactly equal the post-change `MemoryContext`,
including `None`; validation never mutates the caller-owned change set. The
common matcher first applies the verified/regression-backed, repo, tenant,
failure-type, and unchanged declared trace-backed context checks. It then
requires all changed fields to match the full old endpoint or all to match the
full new endpoint. A mixed old/new trace is excluded. `tool` compares exact
non-empty tool-call names, while the other fields compare direct trace
metadata. A match is tagged `old`, `new`, or `both`; `both` can occur for a
tool-only change when the trace invoked both endpoint tool names.

`pr_report_commit_anchors(context, change_set=...)` uses that same complete
endpoint matcher and returns sorted unique source commits. Callers must reuse
the same immutable change set when they capture ancestry and call
`pr_memory_report(change_set=..., commit_ancestry=...)`. The report first
matches endpoints, then requires complete ancestry evidence for every matched
source commit, and finally excludes explicitly false relations before building
case IDs, suggestions, warnings, and provenance. Missing evidence therefore
fails closed, while harmless extra valid evidence remains allowed.

Change sets and endpoint tags are ephemeral report-boundary values. They are
not serialized or stored: snapshot version remains 2, JSON Schemas and
active-lessons YAML remain unchanged, and PostgreSQL schema version remains 1.

## Git Ancestry Applicability

`CommitAncestryEvidence` is an immutable request-time record of whether each
discovered anchor is an ancestor of one exact current commit. Callers first
obtain the complete metadata-scoped runtime anchor set with
`candidate_commit_anchors(context)`, then run
`capture_commit_ancestry(context.commit_sha, anchors, repo_path=...)` outside
the store lock. Capture evaluates `git merge-base --is-ancestor anchor
current`: exit 0 is `True`, exit 1 is `False`, and any other command failure
raises an error that stops the workflow.

Runtime anchor meaning is exact: lesson memory uses its source failure case's
`fix_commit_sha`; failure-case memory uses its source `commit_sha`; project
policy has no anchor. Evidence must bind `current_commit_sha` to the exact
`MemoryContext.commit_sha`, and when evidence is provided it must include a
relation for every candidate anchor. Missing evidence fails closed before
filtering, while a recorded `False` excludes the associated historical
candidate. Unanchored project policies bypass ancestry filtering only and
still pass metadata filtering, System Gate, and LLM Gate.

The runtime order is metadata candidate discovery, external ancestry capture,
ancestry filtering, optional keyword or semantic retrieval, System Gate, and
LLM Gate. Omitting evidence preserves the legacy runtime path. PR callers
follow the same pattern with `pr_report_commit_anchors(context)`, capture
against the context commit, and pass the same evidence to
`pr_memory_report()` before it builds related cases, regression suggestions,
warnings, or provenance.

Ancestry evidence is never added to snapshots, usage logs, YAML, schemas, or
the PostgreSQL repository. The feature changes neither persistence contracts
nor the existing gate contracts.

## Non-goals

- Do not build generic personalization memory first.
- Do not inject raw traces directly into prompts.
- Do not treat vector similarity as sufficient proof of relevance.
- Do not allow the LLM to mark memory active without verification.
- Do not provide in-place migration between deployed schema versions.
- Do not provide connection pooling or pool lifecycle management.
- Do not provide `async` PostgreSQL repository support.
