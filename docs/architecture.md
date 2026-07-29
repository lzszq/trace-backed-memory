# Architecture

**English** | [简体中文](architecture.zh-CN.md)

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

The reference kernel includes `capture_trace_metadata()` for reading repo name, commit SHA,
current branch, and dirty state from git before a harness records the trace.
Prompt version, prompt family, tool schema version, model, and eval suite are
first-class trace fields that callers attach from the harness runtime. Git
command failures are wrapped in `TraceMetadataCaptureError` with the command
and repository path. Injected runners must return strings for all
four commands. A blank commit SHA, blank repository root, non-string output, or
commit/branch/repository name above 512 characters fails at that same boundary
without echoing the malformed value. Blank branch output remains detached HEAD,
blank status remains clean, and a filesystem-root repository has no basename
and therefore reports `repo=None`. Snapshot version 2 and PostgreSQL schema
version 2 are the current persistence contracts.

The in-memory store can persist a dependency-free JSON snapshot of traces,
failure cases, lessons, project policies, and usage logs. Loading a snapshot
reuses the same recording methods as live writes, so duplicate IDs, global
memory ID uniqueness, and lesson provenance checks remain enforced.

Trace writes require nonblank `trace_id`, `run_id`, and `commit_sha`, and
`eval_result` must be one of `pass`, `fail`, `error`, or `unknown`.
`retrieved_context`, `tool_calls`, and `tool_outputs` must be lists of JSON
objects so downstream extraction and reporting can safely inspect them.
The store validates the caller-owned `Trace`, deep-copies it, validates the
copy again, and only then inserts it. Expected concurrent copy mutation fails
with `ValueError`, while unrelated copy programming errors remain visible.
One `_TraceJSONBudget` is shared across `retrieved_context`, `tool_calls`, and
`tool_outputs`: the three outer lists and all nested semantic values may total
at most 100,000 nodes, while object keys and string values may total at most
8 MiB of UTF-8 text and structured values retain depth 100. Container
cardinality is checked against the remaining node budget before traversal-stack
or `dict.items()` expansion, so wide input fails before the defensive copy
amplifies it.

A current execution may be registered before runtime with
`eval_result="unknown"`. After execution, `complete_trace()` performs one
atomic transition to `pass`, `fail`, or `error` and may fill `output_hash`,
`tool_outputs`, `latency_ms`, `cost_usd`, `error`, and `trace_uri`. Omitted
completion fields preserve their existing values. A populated completion slot
must remain exactly equal, and every non-completion Trace field is immutable.
The candidate Trace is validated, copied, and validated again before
replacement. Exact replay is idempotent and the returned Trace is a defensive
copy.

Trace latency has one domain invariant across persistence modes:
`latency_ms` is `None` or an integer in the inclusive range 0 through
2,147,483,647. The shared Trace validator checks exact type, JSON serialization,
the lower boundary, and then the PostgreSQL-compatible upper boundary, so
record, snapshot reconstruction, callback execution, and single or batch
completion cannot diverge. Candidate staging keeps an out-of-range value from
partially updating a Trace or usage outcome. CLI completion delegates this
domain check to the Store and reports a structured `state` error with exit code
3; syntax and type failures remain input errors.

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

The reference kernel includes `load_failure_taxonomy()` plus conservative extraction helpers
that classify obvious trace failures into taxonomy IDs before drafting a case.
When a taxonomy is supplied, classifier output must be present in that taxonomy.
Specific context-missing and stale-context signals take precedence over generic
tool-argument and evaluator-mismatch fallbacks. Extraction considers
`Trace.error`, then top-level `error` fields from `tool_calls`, then top-level
`error` fields from `tool_outputs`, in stored order. Tool names never select a
failure taxonomy entry. An errored call or output's name may label its symptom,
but successful tool data, arbitrary fields, identifiers, and nested payload
content are not searched. They therefore cannot match a classifier keyword or
produce a false classification.
A tool-call name labels a symptom only when that call carries truthy top-level
`error` evidence. Explicit `invalid argument` text remains authoritative, but
the word `required` selects `invalid_tool_argument` only in the conservative
`required argument`, `required parameter`, `required field`, or
`required property` markers. Permission and authentication requirements retain
the existing evaluator/unknown fallback instead of becoming argument failures.
`review_failure_case()` keeps ambiguous or heuristic drafts in `draft` status
while recording reviewer, root cause, notes, and timestamp. Only reviewed draft
cases can become verified. The transition requires `reviewed_by`, `root_cause`,
`reviewed_at`, a fix, a fix commit, and passing regression evidence.

The in-memory store rejects failure cases whose `source_trace_id` has not been
recorded, whose source Trace did not fail or error, or whose `commit_sha`
differs from the source trace. It also rejects empty identity fields,
unsupported statuses, and `verified` cases without completed review, fix
commit, and passing regression evidence. This keeps the trace record as the
provenance anchor for every postmortem.

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
regression-backed failure case. Active Lessons also reject a dirty source Trace
because its commit does not identify the executed worktree. The Store also
rejects lessons with empty IDs, invalid memory type or status, empty scope,
unknown scope fields, non-string or empty scope values, or confidence outside
the inclusive 0.0 to 1.0 range. Lesson
`sensitive` and `eval_leaking` flags are preserved when lessons become
`MemoryItem` candidates so System Gate can block unsafe memory before LLM
applicability checks.

## Layer 3b: Project Policy

Project policies are manually maintained prompt, tool, or eval rules. They are
not derived from failure cases, but they still need source identity, scope,
status, and safety flags before they can be considered for injection.
`ProjectPolicy` and `memory_item_from_project_policy()` provide the compatibility bridge
from maintained policy records to sourced `MemoryItem` policy memory. The
in-memory store rejects policies with empty IDs or text, invalid status, invalid
scope fields, confidence outside the inclusive 0.0 to 1.0 range, or IDs that
collide with the shared runtime memory ID namespace across failure cases, lessons, and project policies.

For the reference implementation, `TraceBackedMemoryStore.to_snapshot()`, `from_snapshot()`,
`save_json()`, and `load_json()` provide a stable full-store persistence
boundary for traces, failure cases, lessons, project policies, and usage logs.
The boundary requires a JSON object, accepts JSON-serializable integer costs,
rejects non-finite floats and integers beyond the runtime serialization limit,
keeps confidence bounded to 0.0 through 1.0, and parses strict JSON without
`NaN` or infinity constants.
`save_lessons_yaml()` and `load_lessons_yaml()` provide a small dependency-free
adapter for active lessons using the repository's `memory/lessons.example.yaml`
shape; loading and `add_lesson()` share the same side-effect-free candidate
validator, so source-case and lesson-contract checks remain enforced. The
portable `load_lessons_yaml()` interface adds an active-only rule after general
candidate validation: `status: obsolete` is rejected before staged insertion,
while snapshots, PostgreSQL, and lifecycle APIs continue to preserve obsolete
history. YAML serialization quotes strings so numeric-looking scope values
remain strings when loaded. The constrained taxonomy parser
rejects duplicate IDs or descriptions, and the lessons parser rejects duplicate
record or scope fields. A complete lesson document is parsed and every candidate
is constructed and validated against staged state before an all-or-nothing
Store commit. Duplicate IDs, invalid provenance, and later record failures
cannot partially import preceding lessons.

`save_json()` and `save_lessons_yaml()` share one durability boundary. Each
writes canonical LF text through a sibling temporary file, flushes it, calls
`os.fsync()`, closes it, and publishes atomically. The default replacement path
uses `os.replace()`; the additive lesson-writer `overwrite=False` path uses
`os.link()` to combine the no-existing-destination condition and publication
without a racy pre-check. After a successful atomic publish and normal
temporary-name cleanup, POSIX opens and `fsync()`s the parent directory so its
entry change is durable; non-POSIX platforms retain the existing portable
publication boundary. Serialization, temporary-file sync, link, or replacement
failure removes the temporary file and leaves an existing destination
unchanged. A post-publication parent-directory sync failure propagates after the
target may have changed, so callers must treat it as an indeterminate durability
result. The lesson serializer emits canonical `lesson_text: |` blocks. The
constrained reader accepts both `|` and legacy `>` while preserving blank lines, leading
and trailing LF characters, and intra-line spaces instead of globally trimming
block content. It retains the adapter's historical literal-line interpretation
of `>` rather than implementing general YAML folding or chomping. This changes
no stored field: snapshot version 2, JSON Schemas, and PostgreSQL schema version
2 remain unchanged.

## Packaged Distribution Resources

The `trace_backed_memory.resources` module is the installed-resource seam for
the repository's 147 canonical Schema, SQL/migration, memory-support, and
example files. Its
interface is limited to deterministic `packaged_resources()` descriptions,
exact-byte `read_packaged_resource()` reads, and explicit
`export_packaged_resource()` writes. Descriptions are immutable and carry the
canonical name, kind, media type, byte size, and SHA-256.

Resource names are a fixed lexicographically ordered allowlist. The module
validates a name before using `importlib.resources.files()` and never accepts a
filesystem-relative fallback, arbitrary traversal, current working directory,
or exposed package path. The implementation therefore behaves the same for
wheels, source distributions, editable installs, and zip importers. A single
`PackagedResourceError` identifies lookup, read, or export operations while
retaining an underlying installed-resource or filesystem exception as its
cause.

Canonical authoring files remain at repository top level. Byte-identical
package copies live under `trace_backed_memory/_resources/`; package metadata
lists them explicitly, and distribution verification compares every wheel and
source-distribution member with its authoring file. `py.typed` marks the
installed annotations as supported package typing information.

`load_failure_taxonomy()` reads the packaged taxonomy when no path is supplied
and keeps explicit caller-owned path loading unchanged. `tbm resource
list/read/export` is a thin JSON CLI adapter over the same public resource
interface. Export refuses replacement unless `--overwrite` is explicit and
uses same-directory temporary bytes before atomic publication. Resource files
are distribution artifacts, not Store records; snapshot version 2 and
PostgreSQL schema version 2 remain unchanged.

### Bounded Local Document Ingestion

The private ingestion boundary opens each caller-owned local path once and
reads through a single file handle before strict UTF-8 decoding. Snapshot JSON
is capped at 64 MiB, 100,000 records per collection, and 250,000 total records.
Active-lessons YAML is capped at 8 MiB and 10,000 lessons; failure-taxonomy YAML
is capped at 1 MiB and 1,000 failure types. Counts are checked before Store
construction or mutation.

CLI measurement and tool-output JSON is capped at 8 MiB, 10,000 top-level
items, 100,000 JSON nodes, and depth 100. Its iterative traversal prevents the
budget check itself from introducing application recursion. Python import APIs
expose keyword-only controls such as `max_bytes`; an explicit `None` disables
only that limit for trusted offline migrations. CLI adapters always use fixed
safe defaults. These are runtime ingestion controls, not stored configuration:
snapshot version 2, JSON Schemas, active-lessons YAML, packaged resource bytes,
and PostgreSQL schema version 2 remain unchanged.

Usage-log reconstruction is average O(n) in snapshot records and nested
ID/tool evidence. One `from_snapshot()` call owns temporary indexes for seen
`decision_id` values, known memory IDs, legacy `run_id` resolution, and lazily
cached tool names by trace. Candidate/used/blocked relationship checks reuse
per-log sets without reordering diagnostics. These indexes are not Store state
or serialized data, and the existing per-record validation and duplicate-error
precedence remains intact.

The live Store separately maintains a private derived `decision_id` index and
next numeric suffix. The only three append paths share one helper, while
outcome, completion, and recovery replace records at stable positions without
changing IDs. Allocation, duplicate detection, and single lookup are average
O(1), and a requested batch resolves in average O(k). Failed candidates do not
advance the counter. The derived index is rebuilt by validated snapshot import,
never serialized, and does not replace canonical output sorting, snapshot
version 2, or PostgreSQL schema version 2.

The live Store also owns a private derived index from each `run_id` to its
ordered `trace_id` values. `record_trace()` is the only insertion boundary and
commits the copied Trace and index entry together under the existing `RLock`;
an index failure rolls the Trace insertion back. A lookup classifies missing,
unique, and ambiguous run IDs in average O(1) without scanning `_traces`.
Duplicate run IDs remain valid and ambiguous, while the index stores IDs so
Trace completion resolves the current replacement record. Snapshot loading
rebuilds this nonserialized state through `record_trace()`. Canonical output,
legacy migration, snapshot version 2, and PostgreSQL schema version 2 remain
unchanged.

Live usage-log memory existence checks are bounded by the referenced IDs. If
the caller does not supply the snapshot-local `known_memory_ids` set, the
validator checks every distinct reference directly against `_failure_cases`,
`_lessons`, and `_project_policies`. This is average O(r), where `r` is the
number of referenced IDs, and allocates no full catalog copy. Snapshot loading
continues to reuse one `known_memory_ids` set across all imported logs. No new
derived index is introduced; deduplication, sorted unknown-ID errors,
validation order, snapshot version 2, and PostgreSQL schema version 2 remain
unchanged.

The Store's `metrics()` view performs one usage-log pass with O(1)
accumulator space. The loop collects candidate, used, blocked, obsolete,
evaluated-with-memory, evaluated-without-memory, unevaluated, pass, and
wrong-memory counts without materializing cohort result lists. Count-based
rates preserve `None` for an empty cohort and `0.0` for a nonempty cohort with
no passes. Lesson confidence still traverses the separate lesson map.
`memory_outcome_metrics()`, decision-sorted memory-run views, and the CLI's
independent public API calls retain their existing lock and ordering behavior.
Snapshot version 2 and PostgreSQL schema version 2 remain unchanged.

The `recover-batch` argv surface separately caps submitted values at 10,000
decision IDs and 10,000 attribution options. A preload cardinality check runs
immediately after argparse and before snapshot loading, tuple/set/dictionary
construction, Store recovery, or publication. Counts precede deduplication, so
repeated values still consume the fixed budget. This CLI-only limit has no
opt-out and persists no configuration.

## Snapshot Operations CLI

The dependency-free snapshot operations adapter is exposed as `tbm` and
`python -m trace_backed_memory`. Snapshot-backed commands accept exactly one
local snapshot path and always reconstruct the store through
`TraceBackedMemoryStore.load_json()`. They do not accept stdin, remote URLs,
PostgreSQL connections, or an alternate snapshot output path. Resource
commands are handled before snapshot loading and add no Store state. Lesson
export alone accepts one caller-owned destination.

The read surface maps directly to existing store views. `snapshot validate`
performs full reconstruction and returns validity, snapshot version, and
canonical collection counts; `snapshot stats` returns the version and counts.
`audit` and `remediation` serialize the decision-ordered records from
`memory_run_audits()` and `memory_run_remediations()`. `metrics` combines
`metrics()`, `memory_run_metrics()`, and `memory_outcome_metrics()` without
introducing a second aggregation path.

The mutation surface delegates `complete` to `complete_memory_run()`,
`complete-batch` to `complete_memory_runs()`, `recover` to
`recover_memory_run()`, `recover-batch` to `recover_memory_runs()`, and
`recover-ready` to `recover_ready_memory_runs()`. `obsolete` selects exactly one
of `obsolete_failure_case()`, `obsolete_lesson()`, or
`obsolete_project_policy()` through an explicit kind. `obsolete-batch` parses
public `MemoryObsolescenceRequest` records and delegates exactly once to
`obsolete_memories()`. `complete` supplies a
fresh measured result through required `--eval-result` and exact linked IDs;
it does not infer an outcome, linkage, attribution, or evidence. Scalar
evidence is optional. `--tool-outputs-file` reads strict UTF-8 JSON that must be
an array of objects, and absent evidence flags are not forwarded so the Store
retains its omission semantics.

The package root exports all three low-level obsolescence functions. In
particular, `trace_backed_memory.obsolete_project_policy` is the lifecycle
module function itself, not a wrapper. These pure replacements do not provide
the Store's lookup, cascade, replay, or atomic batch guarantees.

Before any snapshot-backed dispatch, `recover-batch` checks its submitted
argument cardinality. More than 10,000 decision IDs or 10,000 attribution
options is a structured `CLIInputError` with exit code 2. Accepted values then
follow the existing uniqueness, exact `DECISION_ID=true|false`, state, order,
dry-run, and atomic-write path without a second recovery implementation.
The final `=` is the attribution delimiter: the complete non-empty prefix is
the decision ID and may itself contain `=`, while the suffix remains exact
lowercase `true` or `false`. Malformed components remain structured input
errors with exit code 2; accepted IDs are not trimmed or normalized.

`complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]` reads a strict UTF-8 JSON
non-empty array. Each allowlisted object becomes one `MemoryRunResult`; the
parser rejects duplicate object keys, malformed field types, and caller-supplied
Trace linkage. One `complete_memory_runs()` call derives every Trace ID, stages
the batch all-or-nothing, and returns completions in manifest order. The
manifest is an ephemeral command input rather than a new persisted schema.

JSON object-name uniqueness is enforced before ordinary dictionaries exist.
The shared ordered-pairs parser is used by
`TraceBackedMemoryStore.load_json()`, `parse_memory_context()`, and
`parse_memory_decision()`, while CLI file parsing preserves its structured
input-error boundary. Every nesting level rejects duplicate object keys rather
than applying last-key-wins. Valid mappings and canonical package output are
unchanged, as are snapshot version 2 and PostgreSQL schema version 2.

`lessons export SNAPSHOT DESTINATION [--overwrite]` delegates active-only
selection and canonical YAML serialization to `save_lessons_yaml()`. The CLI
passes `overwrite=False` unless replacement is explicit and rejects a
destination that aliases the source snapshot. Its deterministic result names
the active lesson IDs selected in Store order. `lessons import SNAPSHOT
SOURCE_YAML [--write]` calls `load_lessons_yaml()` once with the fixed 8 MiB and
10,000-record defaults. The Store owns constrained parsing, duplicate checks,
shared-ID and provenance validation, the active-only status rule, source order,
merge semantics, and the all-or-nothing mutation boundary. A non-active record
is an input error with exit code 2 and cannot publish an explicit `--write`.

`obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]`
captures only the current status and active dependent lesson IDs needed for a
non-sensitive result, then delegates the transition to the Store exactly once.
The failure-case method validates the obsolete parent and all active derived
lessons before one atomic in-memory update. The adapter derives sorted cascade
IDs from the successful before/after status difference; it does not reproduce
the cascade predicate or lifecycle state machine. Lesson and policy operations
have empty cascade fields. Same-state replay is a deterministic no-op, and no
CLI path reactivates an obsolete record.

`obsolete-batch SNAPSHOT REQUESTS_JSON [--write]` accepts a strict UTF-8 JSON
non-empty array under the shared 8 MiB, 10,000-item, node, and depth limits.
Every exact object supplies canonical `failure_case`, `lesson`, or
`project_policy` plus one `memory_id`. The parser owns document shape only. The
Store requires an exact non-empty request tuple, validates unique IDs and exact
kinds, resolves every target against the matching collection, and stages the
complete forward-only transition.

`obsolete_memories()` builds requested cases, lessons, and policies plus every
active lesson in a requested failure-case cascade from the same entry state. It
validates all staged records before updating any collection and returns deep
copies of explicit results in request order. Explicitly requesting a cascaded
lesson is valid and order-independent. The CLI reports the sorted full cascade,
explicit `changed_count`, and union-based `affected_count` without exposing
record content. A duplicate, wrong-kind, unknown, or later invalid request is
all-or-nothing and leaves the Store unchanged.

Every mutation first changes only the loaded in-memory store and is a dry-run
unless `--write` is explicit. This includes lesson import; lesson export is an
explicit destination publication rather than a Store mutation. After a
complete successful operation, `--write` calls `save_json()` on the input path,
reusing its same-directory temporary file and atomic replacement. Completion,
batch validation, recovery, and lesson import remain all-or-nothing in the
store. Single-record obsolescence and atomic batch obsolescence, including every
case-to-lesson cascade, share the same rule. The CLI does not stage lifecycle
records, parse YAML, or classify records independently, and it never synthesizes
a batch by looping over single-record transitions. Snapshot version 2 and
PostgreSQL schema version 2 remain unchanged because the manifest is ephemeral.

Each snapshot `--write` command also acquires a sibling `.tbm.lock` exclusive
advisory lock before snapshot load and holds it across the complete
read-modify-write transaction through atomic publication. The lock is released
before stdout, so downstream backpressure cannot extend committed ownership.
The persistent sidecar is initialized with one placeholder byte and provides a
stable inode while OS descriptor ownership prevents stale locks after crashes.
Before that placeholder write, exclusive creation or pre-open/descriptor/
post-open identity validation requires one single-link regular file at the
canonical path. A final descriptor/path identity check runs after OS lock
acquisition and before control reaches the transaction. Symbolic links,
Windows reparse points, hard links, and special files fail before snapshot
loading without modifying an alias target.
Both platforms retry contention for at most 30 seconds; timeout fails before
snapshot load as a write error with exit code 4. Dry runs, read-only commands,
lessons export, and resource export remain lock-free. This coordinates
cooperating local CLI processes without adding snapshot or PostgreSQL state;
snapshot version 2 and PostgreSQL schema version 2 remain unchanged.

The root-package `snapshot_write_lock()` context manager exposes that same
canonical `.tbm.lock` protocol to Python callers. It accepts a finite
non-negative `timeout_seconds` and must surround the full load, Store mutation,
and `save_json()` read-modify-write transaction. The lock is cross-process,
advisory, and non-reentrant; it complements rather than replaces the Store's
per-instance `RLock` and is unrelated to PostgreSQL transactions. CLI and
Python writers using the helper therefore converge on one persistent sidecar
without changing snapshot version 2 or PostgreSQL schema version 2.

Successful commands emit one deterministic JSON value plus a newline. Failures
emit one structured JSON object to stderr without a traceback. Exit codes are
0 for success or no-op, 1 for an unexpected internal failure, 2 for command,
snapshot, YAML, or structured-evidence input, 3 for completion or recovery
state, obsolescence, linkage, attribution, or evidence rejection, and 4 for a
lesson destination or snapshot write failure. Error text is capped at 2,048
characters. Successful output is serialized before persistence; after an
export or requested write commits, a downstream stdout pipe closure does not
report the already-persisted operation as failed. Help is the sole normal
argparse text path.

The adapter persists no command, audit, metrics, or remediation record. It
leaves snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 2 unchanged.

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
Both `allowed_memory_ids` and `blocked_memory_ids` accept at most 50 entries,
matching `LLM_GATE_MAX_CANDIDATES`. Parser and direct low-level gate boundaries
check the list length before per-ID validation, duplicate sets, or copies. The
decision JSON Schema publishes the same `maxItems: 50` rule; internally derived
System Gate block records retain their existing audit behavior.
The complete decision response is bounded before field validation:
`LLM_GATE_RESPONSE_MAX_BYTES` is 65,536 UTF-8 bytes,
`LLM_GATE_RESPONSE_MAX_NODES` is 1,000, and
`LLM_GATE_RESPONSE_MAX_DEPTH` is 20. `reason` is limited to
`MEMORY_DECISION_REASON_MAX_CHARS`, or 2,000 characters, for parsed decisions,
direct decisions, usage logs, JSON Schema, and fresh-install PostgreSQL DDL.
Task text, context summaries, and candidate memory text in the gate prompt are
JSON-quoted and capped so long or instruction-like dynamic inputs stay data,
not prompt structure.
Runtime injection honors the parsed `recommended_injection` mode: `none` emits
no snippet, `pointer_only` emits IDs/source/scope without lesson text,
`short_summary` emits a JSON-quoted rule capped at 500 characters, and
`full_case_summary` emits up to 2,000 characters of Store-enriched lesson,
failure, root-cause, fix, commit, regression, and reviewer evidence.

Runtime output is bounded by fixed contract constants:
`MEMORY_ID_MAX_CHARS` is 128, `METADATA_VALUE_MAX_CHARS` is 512,
`LLM_GATE_MAX_CANDIDATES` is 50, `LLM_GATE_PROMPT_MAX_CHARS` is 32,000,
`LLM_GATE_RESPONSE_MAX_BYTES` is 65,536,
`LLM_GATE_RESPONSE_MAX_NODES` is 1,000,
`LLM_GATE_RESPONSE_MAX_DEPTH` is 20,
`MEMORY_DECISION_REASON_MAX_CHARS` is 2,000,
`INJECTION_MAX_MEMORIES` is 20, and `INJECTION_SNIPPET_MAX_CHARS` is
12,000. Identifier and metadata limits are enforced before rendering; total
prompt and snippet limits are checked before either value is returned.

Candidate retrieval is metadata-first. The in-memory reference store retrieves lessons and
project policies when every declared scope metadata field matches the current
context. In debug and repair modes, it also exposes verified,
regression-backed failure cases by deriving runtime memory scope from the
source trace plus failure type. Retrieval can then apply an optional keyword
query. Keyword overlap is only a retrieval aid and does not replace System Gate
or LLM applicability checks. Short domain tokens such as `AI` and `v2` are
preserved in keyword filtering. Tokenization uses Unicode alphanumeric words
and adds two-character grams for non-ASCII words so queries such as `空查询` can
match longer CJK text.

Callers may alternatively provide precomputed semantic scores keyed by stored
runtime memory ID. Semantic mode remains metadata-first, requires an explicit
integer `max_candidates` from 1 through 50 inclusive, accepts only finite
numeric scores, and breaks ties by
memory ID. Scores select candidates only; System Gate and LLM Gate remain the
approval boundary. The store neither computes nor persists embeddings or raw
scores. It validates the complete caller mapping against a non-copying
membership view of the three memory catalogs before eligibility work. After
metadata and ancestry filtering, bounded semantic top-k streams eligible
records through a heap instead of materializing a full sort, while retaining
score-descending and memory-ID-ascending output. For `K` eligible candidates and
`k <= 50` results, ranking is `O(K log k)` time with `O(k)` ranking storage.
This changes no snapshot version 2 or PostgreSQL schema version 2 contract.

The safe store workflow is `prepare_memory()` followed by `finalize_memory()`.
Preparation performs retrieval, System Gate, and bounded LLM prompt creation.
Each prepared request may retain at most 1,000 audited candidate IDs, and all
process-local pending requests may retain at most 100,000 candidate references
in aggregate. These limits are enforced before the request enters pending
state.
When context names a tool, an episodic Failure Case is eligible only if its
source Trace contains that exact named tool; missing tool evidence fails
closed rather than becoming a wildcard.
If more than 50 candidates pass System Gate, it deterministically keeps the
first 50 in candidate order and records every overflow candidate as System
blocked with `LLM gate candidate limit exceeded`. Finalization rechecks current
state, narrows the LLM decision, renders the
snippet, and atomically records trace-linked evidence. Only this workflow
provides ownership, replay, stale-state, trace-link, and atomic logging
guarantees. Low-level helpers remain public for callers that own equivalent
orchestration.

### Synchronous memory-run execution

`run_memory_execution()` is the dependency-free orchestration module for the
common synchronous path. After the caller records an `unknown` Trace, the
module calls `prepare_memory()`, invokes a `MemoryDecisionCallback` with the
public `MemoryGateRequest`, calls `finalize_memory()`, invokes a
`MemoryExecutionCallback` with the public `GatedMemoryResult`, and delegates
the resulting `MemoryRunMeasurement` to `complete_memory_run()`.

The measurement has no decision ID. The module transfers the Store-produced
`decision_id`, converts a non-`None` tool-output tuple to a list, and forwards
only non-`None` optional evidence. Retrieval, System Gate, decision parsing,
LLM narrowing, request consumption, snippet rendering, Trace/context binding,
usage logging, evidence validation, and atomic Trace plus decision completion
remain inside the Store.

`MemoryRunExecutionError` adds phase and public recovery context to every
ordinary failure after preparation while retaining the original callback or
Store exception as its cause. Its phases are `decision`, `finalization`,
`execution`, and `completion`. Every phase exposes the still-pending request;
the latter two also expose the finalized result and decision ID. Preparation
errors remain raw because no request exists, and process-control exceptions
pass through. Each orchestration call prepares a new request; retry against the
error's exposed request or finalized result rather than rerunning the whole
one-shot helper.

Advanced callers continue to use `prepare_memory()`, `finalize_memory()`, and
`complete_memory_run()` directly when they pause between stages or own custom
retry and recovery policy. The execution module does not access private Store
state, persist records, synchronize PostgreSQL, or create another completion
state machine. `MemoryRunMeasurement`, callback types, and execution errors are
ephemeral; snapshot version 2, JSON Schemas, active-lessons YAML, and
PostgreSQL schema version 2 remain unchanged.

### Agent application façade

`LocalAgentMemory` is a focused application boundary over the same Store
lifecycle. It owns Trace registration, in-memory request lookup, repository
load/sync/close, stable agent errors, and the common prepare/finalize/complete
sequence. `capture_local_trace()` derives Git provenance from an explicit
checkout root. `agent_capabilities()` and `tbm capabilities` publish protocol,
storage, operation, and hard-limit discovery without loading a snapshot.

The façade never serializes the private Store token. SQLite and PostgreSQL
synchronize the Trace before preparation, the usage decision after
finalization, and atomic measured completion. Pending requests and local
finalization tombstones remain process-local. Exact same-decision finalize
replay is idempotent within one runtime; a different retry is a stable
conflict. The packaged `tbm.agent.v1` schemas describe capabilities, canceled,
prepared, finalized, completed, and error results without changing snapshot
version 2, SQLite schema version 1, or PostgreSQL schema version 2.

Every Store runtime generates a fresh 128-bit namespace for opaque Gate
request IDs. The persisted numeric suffix can continue after reload, but a
stale ID from an abandoned process cannot name a new request in the next
process. This prevents stale finalization or cancellation from crossing a
runtime restart without pretending that pending state is durable.

`tbm-mcp` is a thin, optional STDIO adapter over one process-owned
`LocalAgentMemory`. It exposes only capability, health, prepare, finalize,
complete, and cancel tools. The configured checkout root and optional tenant
are server-owned; prepare captures Trace Git provenance and complete ancestry
from that root before calling the façade. The adapter does not reproduce Gate
policy and cannot curate, verify, publish, activate, inspect the raw Store, or
run migrations.

`AgentProtocolDispatcher` is the shared strict application-to-wire boundary
for STDIO MCP and the optional `tbm-http` adapter. HTTP binds only to loopback
IPv4, requires one server-owned bearer secret from an environment variable,
and exposes the same six operations under `/v1`. Its bounded reader rejects
duplicate keys, non-finite values, invalid UTF-8, unknown request fields,
ambiguous length/transfer headers, and oversized input before lifecycle
dispatch. A 15-second connection timeout, 32-worker semaphore, and bounded
listen queue prevent slow or excess local connections from creating unbounded
threads. `AgentHTTPClient` is a dependency-free typed Python client that also
requires loopback, disables proxies and redirects, validates bounded protocol
responses against their published field limits, and maps stable error
envelopes back to `AgentMemoryError`. `AsyncAgentHTTPClient` preserves the
same validation through worker-thread dispatch without blocking the event
loop. The dependency-free Node.js TypeScript package uses direct `node:http`
sockets rather than proxy-aware fetch, rejects redirects and duplicate JSON
keys, and applies the same protocol, body, timeout, and stable-error checks.

The canonical OpenAPI 3.1 document references the same strict prepare,
finalize, complete, cancel, health, success, and error schemas used by the
adapters. A real-process conformance scenario executes the same lifecycle
through the dispatcher, STDIO MCP, and HTTP and compares normalized protocol
payloads. The TypeScript suite additionally drives a real Python HTTP process
through prepare, finalize, complete, cancel, capability, and health calls.
OpenAPI remains the local version-2 transport contract; it does not convert
bearer authentication into service identity.

This HTTP profile is a local process boundary, not a shared service: it has no
TLS, user identity, or tenant isolation. The configured tenant remains
version-2 applicability metadata, and the CLI does not construct
`AuthenticatedDurableAgentMemory`. Like MCP, restarting HTTP abandons
unfinalized request handles even when Trace, finalized usage, and completion
records are synchronized to SQLite or PostgreSQL.

An all-or-none local `--auth-*` startup profile may additionally wrap that
runtime in `AuthenticatedLocalAgentMemory`. A bounded trusted registry file
and SQLite authorization authority select exact active principal, client, and
environment records at startup. The environment derives canonical tenant and
repository; request schemas expose none of those fields. Prepare persists and
reads back authorization before Trace registration, while facade-local owner
indexes protect finalize/complete/cancel handles. This profile is not
transport authentication and is not a shared multi-tenant deployment.

The STDIO reader bounds each line before JSON decoding, drains an oversized
line before accepting another request, and applies the shared duplicate-key,
finite-number, UTF-8, node, and depth checks. Strict tool request models reject
unknown fields. Transport and request errors become bounded agent envelopes;
unexpected failures are sanitized. The stable MCP v1 SDK remains an optional
dependency, so importing the core package and running `tbm` stay
dependency-free.

The server deliberately retains the façade's process-local session boundary.
Durable repositories synchronize only the existing Trace, finalized usage
decision, and measured completion records. Restarting the MCP process abandons
unfinalized requests and replay tombstones; it does not reconstruct private
Store tokens from durable data. Per-session request namespaces also ensure a
stale client handle cannot collide with a newly prepared request after that
restart.

Execution normally finishes after the decision is logged. The chronological
runtime path registers an `unknown` current Trace, finalizes memory, executes,
then calls `complete_memory_run()` with the linked `trace_id`, `decision_id`,
and one measured result. Under one store lock, it validates a completed Trace
candidate and a sealed usage-log candidate before assigning either. The frozen
`MemoryRunCompletion` return value contains defensive copies of both records.

Both records may be pending, one matching record may already be complete for
partial recovery, or both may match for an idempotent exact replay. A result,
attribution, evidence, or linkage conflict rejects the atomic operation without
changing either record. `complete_trace()` and `record_decision_outcome()`
remain independent low-level APIs for callers that deliberately own separate
lifecycles and for recovery; legacy records are not reinterpreted.

`complete_memory_runs()` applies the same state machine to a non-empty tuple of
unique frozen `MemoryRunResult` commands. `MeasuredEvalResult` limits each
command to `pass`, `fail`, or `error`. The store derives `trace_id` from the
validated decision linkage and preserves request order in the returned
`MemoryRunCompletion` tuple, so batch callers cannot spoof correlated IDs.

Each result may supply output hash, `tool_outputs`, latency, cost, error, and
Trace URI. `None` means omitted; tool outputs use a tuple on the command and a
list on the Trace. For decisions linked to a shared Trace, measured results must
agree and normalized evidence fields merge only when disjoint or equal. Every
request is first validated against the original Trace, then one final candidate
is built from merged evidence, making shared behavior order independent.

The common batch stager builds all Trace candidates, sealed usage candidates,
and defensive returns without mutation. `complete_memory_runs()` commits those
new evaluator results all-or-nothing. `recover_memory_runs()` reuses the stager
only after its stricter entry-state recovery resolver has derived existing
results, so recovery cannot introduce a caller-selected outcome. Single
`complete_memory_run()` behavior remains unchanged.

`MemoryRunResult` is ephemeral and not persisted. Existing PostgreSQL
transactions synchronize the resulting Trace and usage rows. Snapshot version
2, JSON Schemas, active-lessons YAML, and PostgreSQL schema version 2 remain
unchanged.

An unevaluated usage log supports its low-level store-owned transition through
`record_decision_outcome()`: `None` or `unknown` may become `pass`, `fail`, or
`error` together with its `memory_caused_failure` value. Exact replay is
idempotent; changing either member of an already sealed pair is rejected
without mutation. `complete_trace()` likewise changes only its Trace when used
directly.

`memory_run_audits()` joins the private validated Trace and usage collections
under the same store lock and returns one record for every usage decision,
sorted by `decision_id`. Each frozen `MemoryRunAudit` includes `trace_id`,
`run_id`, both raw result values, failure attribution, and a derived status.
Both unevaluated is `pending`; only Trace measured is `trace_only`; only the
decision measured is `decision_only`; equal measured results are `complete`;
different measured results are `conflict`.

The one-sided states identify partial recovery paths through
`complete_memory_run()`. A conflict remains visible for caller review and the
store will never auto-repair it or silently prefer one historical result.
Traces without usage decisions are excluded, while multiple decisions linked
to one Trace remain independent rows. The view is derived and not persisted;
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 2 remain unchanged.

`memory_run_remediations()` maps each audit under the same lock to a frozen
`MemoryRunRemediation`. Its `MemoryRunRemediationAction` is `measure` for
pending, `recover` for passing Trace-only and every decision-only record,
`recover_with_attribution` for failed or errored Trace-only records,
`investigate` for conflicts, and `none` for complete records. Decision sorting
and the one-item-per-decision boundary match the audit view, including shared
Traces.

The plan repeats raw audit state and adds `resolved_eval_result` plus
`resolved_memory_caused_failure`. Those fields are populated only when current
records establish a safe value. In particular, failed or errored Trace-only
records retain `None` attribution rather than interpreting the unevaluated
usage row's default false value as evidence.

The planner is read-only and advisory. `complete_memory_runs()` and
`recover_memory_runs()` still lock and revalidate stale state before writing,
so a returned action grants no mutation authority. Per-decision `recover`
actions on one shared Trace can resolve to different outcomes; the batch write
still rejects that group as incompatible. `MemoryRunRemediation` is derived
and not persisted; snapshot version 2, JSON Schemas, active-lessons YAML, and
PostgreSQL schema version 2 remain unchanged.

`recover_ready_memory_runs()` closes the plan-to-write race for automatic
recovery. While holding one reentrant lock in the store, it derives remediations,
selects only action `recover` in `decision_id` order, and calls
`recover_memory_runs()` before releasing the lock. An empty selection returns
an empty tuple; complete records are not replayed by later sweeps.

The delegated batch stager remains the only mutation path. Matching shared
Trace outcomes commit together, while a shared outcome disagreement or later
candidate failure rejects the full selected set all-or-nothing. Concurrent
sweeps serialize and re-plan after an earlier commit. Pending,
`recover_with_attribution`, conflicting, and complete items are skipped;
explicit attribution remains caller-owned through the existing recovery APIs.

The sweep selection is not persisted and adds no queue or marker. PostgreSQL
synchronizes only the existing Trace and usage changes. Snapshot version 2,
JSON Schemas, active-lessons YAML, and PostgreSQL schema version 2 remain
unchanged.

`memory_run_metrics()` aggregates that same locked view into a frozen
`MemoryRunMetrics`. Its unit is one usage decision, so decisions that share a
Trace remain separate. It exposes `decision_count`, `pending_count`,
`trace_only_count`, `decision_only_count`, `complete_count`, `conflict_count`,
`recoverable_count`, `auto_recoverable_count`, and
`attribution_required_count`. The five status counts are mutually exclusive
and their sum equals `decision_count`; `recoverable_count` is the sum of the
one-sided status counts and also the sum of automatic plus
attribution-required recovery actions.

The aggregate path uses one usage-log pass without sorting and O(1)
accumulator space. A private single-log constructor shares Trace lookup and
status classification with `memory_run_audits()`, while public audits and
remediations retain decision-ID order. This removes aggregate-only tuple
materialization without changing the store lock boundary.

This health summary deliberately remains separate from the outcome-oriented
`metrics()` and per-memory observations. It is derived and not persisted, so
snapshot and PostgreSQL reconstruction use existing Trace and usage rows.
Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 2 remain unchanged.

`recover_memory_run()` consumes a `decision_id` from that view and does not
accept `trace_id` or `eval_result`. Under the store lock it reclassifies current
state, derives the result from the measured side, and delegates all mutation to
`complete_memory_run()`. It returns the same frozen `MemoryRunCompletion`, so
Trace completion evidence, attribution, exact replay, defensive copies, and
atomic assignment retain one implementation.

For `trace_only`, a pass derives `memory_caused_failure=False`; a failure or
error requires the caller to state the boolean explicitly. For
`decision_only`, omission preserves the sealed `memory_caused_failure`; an
explicit mismatch is rejected. `complete` replays exactly. Recovery rejects
`pending` and `conflict` and never guesses a missing result or chooses between
incompatible results. Only existing Trace and decision fields change, so
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 2 remain unchanged.

`recover_memory_runs()` extends the same resolver to a non-empty tuple of
unique decision IDs and an optional `memory_caused_failures` mapping. It
preserves request order in the returned `MemoryRunCompletion` tuple. Every
request is classified from entry state: `trace_only`, `decision_only`, and
`complete` proceed, but `pending` and `conflict` reject the whole batch.

The method groups requested decisions by shared Trace and requires every
derived result in a group to agree. It then builds all Trace candidates, all
sealed decision candidates, and all defensive return copies before assigning
any private state. Candidate failure is therefore all-or-nothing and request
order cannot change eligibility; in particular, a pending decision cannot
borrow a result staged by another decision in the same batch.

Batch recovery does not accept `trace_id` or `eval_result` and has no Trace
completion evidence parameters. Call `recover_memory_run()` when one recovery
must attach output hash, tool output, latency, cost, error, or Trace URI. The
batch wrapper is not persisted; existing PostgreSQL transactions synchronize
the resulting Trace and usage rows. Snapshot version 2, JSON Schemas,
active-lessons YAML, and PostgreSQL schema version 2 remain unchanged.

At finalization and low-level logging, `repo`, `commit_sha`, and `tenant` always
match the linked Trace. `branch`, `prompt_version`, `prompt_family`,
`tool_schema_version`, `model`, and `eval_suite` bind only when the context
declares them. A declared tool must match an exact plain-string Trace tool call;
non-string names do not satisfy the evidence. Omitted optional provenance
remains broad and does not require missing Trace values. `model_family`,
`task_type`, and `failure_type` remain unbound because Trace has no equivalent
stored fields.

These checks run before pending request consumption or usage-log append.
Imported version-2 and supplied legacy context evidence is validated by the
same declared-only rules. This adds no record or column: snapshot version 2 and
PostgreSQL schema version 2 remain unchanged.

Usage logs persist a nonblank trace ID, serialized context, candidate IDs,
candidate status snapshots, System Gate blocked reasons, used IDs, blocked IDs,
risk, reason, recommended injection, optional eval result, and whether memory
was attributed as the cause of a failure. These fields feed pass-rate and
wrong-memory metrics. The in-memory store rejects usage logs whose used memory
IDs were not present in the recorded candidate set, whose identity fields are
blank, whose imported decision IDs are duplicated, whose memory ID lists contain
duplicate, empty-string, or non-string memory IDs, whose used and blocked IDs
overlap, whose used or blocked IDs were not recorded candidates, or whose mode, risk,
recommended injection, or optional `eval_result` values are unsupported.
Every System-approved candidate omitted from the final LLM allow list is added
to the final blocked IDs, so used plus blocked covers deterministic and LLM
narrowing. Usage-log reasons are capped at 2,000 characters.

The JSON Schema requires the four safe-workflow audit fields for persisted
usage logs while Python keeps defaults to migrate exact legacy snapshots.

### Outcome-aware metrics

`pass`, `fail`, and `error` are evaluated outcomes; `error` is an evaluated
non-pass. `unknown` and `None` are unevaluated and are excluded from pass-rate
denominators. `evaluated_with_memory_count` and
`evaluated_without_memory_count` expose the two measured sample sizes, and
`unevaluated_decision_count` reports the remainder. Their sum equals
`decision_count` for values returned by `store.metrics()`. Legacy positional
construction cannot infer historical denominators and keeps the appended
fields at their zero defaults.

These are decision counts, not per-memory causal attribution. The existing
with/without split is determined by whether a usage record has non-empty
`used_memory_ids`. Metrics remain derived and are not persisted; snapshot
version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema version 2
remain unchanged.

Sealing a deferred outcome moves the decision from the unevaluated count to an
evaluated denominator immediately. Callers can therefore finalize before task
execution, seal by the returned decision ID after evaluation, and only then
read completed metrics or persist the completed audit.

The `outcome SNAPSHOT DECISION_ID --eval-result ... [--write]` CLI is a thin
decision-only adapter over `record_decision_outcome()`. It captures the prior
pair for a status summary, delegates the transition exactly once, and never
completes the linked Trace. Its output is restricted to the decision ID,
previous/current result and attribution, `changed`, and `written`; usage-log
context, memory ID collections, Trace fields, and tool evidence remain private.
Serialization precedes optional same-path atomic publication. The command adds
no persisted wrapper, so snapshot version 2, JSON Schemas, active-lessons YAML,
and PostgreSQL schema version 2 remain unchanged.

`memory_outcome_metrics()` returns a memory-ID-sorted tuple for every stored
failure case, lesson, and project policy, including IDs with no observations.
`candidate_count`, `used_count`, and `blocked_count` summarize each final audit
record; blocked count includes both deterministic and LLM-narrowing blocks.
Outcome fields are updated only when that memory ID was used:
`evaluated_use_count`, `passed_use_count`,
`failed_or_errored_use_count`, `unevaluated_use_count`, and
`observed_pass_rate` use the global measured-outcome semantics.

These are observed associations, not causal effectiveness estimates. A
multi-memory decision associates the same run outcome with each used ID and
does not derive per-memory wrong-memory attribution from the log-level flag.
Metrics remain derived and are not persisted; no snapshot, JSON Schema, YAML,
or PostgreSQL field is added.

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
snapshot version 2 and PostgreSQL schema version 2 with no new persisted memory
fields: trace identity uses existing trace columns, current identity and the
block reason use existing usage JSON/JSONB, and source identity is rebuilt from
the trace/case/lesson graph.

Trace context/tool JSON is validated recursively before storage. Only JSON
semantic values with string object keys and finite numbers are accepted;
cycles, more than 100,000 aggregate nodes, more than 8 MiB of aggregate
key/string UTF-8 text, excessive nesting, lone surrogates, and values that
cannot be serialized by the runtime fail with `ValueError`. Existing semantic
failures and UTF-8 failures retain a field path. The shared fixed budget is
recreated for each candidate validation and is not persisted or configurable.

The portable persisted-string boundary requires at least one non-whitespace
character in identity, linkage, required failure text, lesson/policy scope,
Memory Context values, and usage-audit mapping keys and values. Shared Store,
lifecycle, and policy validators reject blank content without normalizing
accepted strings. Six canonical/package JSON Schema pairs publish the same
`pattern: "\\S"` rule. Optional Trace metadata, unrelated Failure Case
narrative fields, and candidate/used/blocked memory-ID arrays retain their
existing behavior.

All persisted lifecycle timestamps use one strict RFC 3339 parser. A timestamp
must carry `Z` or a numeric UTC offset, and fractional seconds are limited to
six digits. This prevents Python, SQLite, and PostgreSQL adapters from silently
normalizing different source precision.

PostgreSQL rejects ordinary-space-only required identifiers and text, uses composite
`(source_trace_id, commit_sha)` provenance to bind cases to their source trace,
requires non-null lesson and policy confidence, and checks audit JSONB objects
and values. Failure-case, lesson, and project-policy IDs are immutable;
Failure Case source Trace/commit bindings and Lesson source Case bindings are
also immutable after insert, including for direct SQL callers. Those records
cannot be deleted. The shared runtime ID registry rejects direct DML;
only schema-qualified source-table triggers can register IDs, and usage evidence
must resolve both a registry entry and its concrete source row.
The fresh-install DDL runs in one transaction with a local
`public, pg_catalog` search path. Statement triggers reject `TRUNCATE` on the
registry and all three runtime-memory source tables, and `TRUNCATE` is revoked
from `PUBLIC`, preserving registry/source parity. The file is a fresh-install
schema; the separate packaged `schemas/postgres-v1-to-v2.sql` resource is the
atomic operator migration for an already deployed version-1 database.
The schema requires PostgreSQL 12+ because its hardened JSONB shape constraints
use `jsonb_path_exists`.

PostgreSQL's default `btrim(text)` removes ordinary spaces rather than every
character covered by Python `str.strip()` or JSON Schema `\\S`. Store-to-
repository writes are therefore prevalidated by the stronger portable rule,
while direct-SQL rows containing other whitespace-only values can be rejected
during repository load. Phase 49 changes neither fresh-install DDL nor schema
version 1; operators of direct-SQL data own cleanup of such out-of-contract
rows.

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

The test runtime remains optional for local development: missing PostgreSQL
executables or an `initdb` user restriction skips the database-backed tests.
The CI-only `TBM_REQUIRE_POSTGRES=1` switch converts those two environmental
conditions into failures. A dedicated Ubuntu job installs and preflights
`initdb`, `pg_ctl`, `psql`, and `psycopg` before running the integration and
repository modules together, while a separate Windows job runs the complete
suite. This switch belongs only to test infrastructure and changes no runtime
configuration, package dependency, or persistence contract.
The POSIX fixture also directs the private server's Unix socket into its
pytest-owned data directory, avoiding distribution-owned runtime paths while
clients continue to use TCP loopback. Windows omits that POSIX-only option.

Portable JSON Schema files document trace, failure case, lesson, project policy,
usage log, and full snapshot shapes; cross-record provenance checks still live
in the store because they require current store state.

## SQLite Runtime Repository

`SQLiteMemoryRepository` is the standard-library embedded SQL persistence
boundary for a complete `TraceBackedMemoryStore`. It uses `sqlite3`, is public
from the package root, and requires no optional dependency. A file database is
the default durable choice for local harnesses, CI jobs, and single-host tools;
an owned `:memory:` database exists only for that connection's lifetime.

The repository requires schema version 1 from canonical
`schemas/sqlite.sql`. `connect(..., initialize=True)` applies the packaged
fresh-install schema, while operators may export the same bytes with `tbm
resource export schemas/sqlite.sql sqlite.sql`. The five tables use stable IDs
and canonical JSON payload envelopes. Store validation remains authoritative
for domain and cross-record invariants; direct SQL payload mutation is outside
the supported contract and is rejected when `load()` or `sync()` reconstructs
the Store.

`sync(store)` is additive and atomic. A top-level write uses `BEGIN IMMEDIATE`
to acquire SQLite's writer reservation before comparison. Existing records are
classified as exact replay, a supported forward transition, or an immutable
conflict using the same Trace, usage-outcome, Failure Case, Lesson, and Project
Policy rules as the PostgreSQL adapter. Obsoleting a Failure Case cascades its
active Lessons. Any conflict or final Store validation failure rolls back the
whole synchronization.

The repository serializes `sync()`, `load()`, `close()`, and context entry per
instance with an `RLock`, preventing one `sqlite3.Connection` from being used
concurrently by its public operations. Top-level rollback cleanup preserves
the primary exception and retries once. If the retry also fails, the repository
closes the connection even when it was caller-supplied, preventing a partial
transaction from being committed later.

`load()` opens one read transaction, checks schema version 1, then enforces the
snapshot defaults of 100,000 records per collection, 250,000 records overall,
and 64 MiB for both the largest UTF-8 payload and the aggregate payload before
returning a validated Store. A repository created from an existing connection
borrows it; `connect()` owns and closes its connection. If a caller transaction
is already active, each operation uses a savepoint and leaves final commit or
rollback to the caller.

SQLite is the embedded choice, not a substitute for PostgreSQL's database-side
JSONB, trigger, row-lock, registry, and multi-client enforcement. Both adapters
share public sync/load lifecycle semantics, while SQLite uses schema version 1
and PostgreSQL uses schema version 2; their DDL and operational concurrency
guarantees are intentionally separate.

## Encrypted Artifact Authority

`artifact_v3.py` defines immutable encrypted-envelope and retention records
around the existing plaintext content identity. `artifact_service_v3.py` uses
the shared authorization kernel for fresh `artifact:write/read` decisions,
then delegates authenticated encryption to caller-owned provider code.
`sqlite_artifact_v3.py` stores only ciphertext in the isolated
`schemas/sqlite-v3-artifact-authority.sql` schema and verifies exact read-back
and canonical schema definitions. `postgres_artifact_v3.py` provides the
isolated active-v2-gated PostgreSQL peer with fixed `search_path`, row/table
locking, database ciphertext-digest verification, exact catalog fingerprints,
concurrent replay, caller savepoints, and fail-closed `RESTRICT` rollback.
This remains an opt-in v3 preparation boundary; the active Store, Agent, MCP,
publication write path, and replay ledgers do not call it yet. See
[the Artifact Authority contract](protocols/artifact-authority-v3.md).

`activated_revision_v3.py` now composes the authenticated retrieval kernel,
the SQLite/PostgreSQL proposal and publication read APIs, and the authenticated
Artifact service into one fail-closed current-head source. It revalidates exact
publication authorization/evidence, trusts only configured append-time
attestation-verifier identities, records separate retrieval/artifact read
authorization events, and rejects a head that moves during the read. Its
candidate digest is a future retrieval input, not a ranking result or active-v2
projection. See [the ActivatedRevision source contract](protocols/activated-revision-source-v3.md).

`retrieval_policy_v3.py` defines the content-addressed classification,
task-mode, ancestry, fusion, minimum-score, and payload-budget policy.
`retrieval_preparation_v3.py` composes authorization, trusted candidate
discovery, verified ActivatedRevision loading, deterministic filters/ranking,
paired RetrievalSnapshot/SystemGateEvaluation construction, and final
head/policy rechecks. `managed_index_v3.py` provides one concrete discovery
adapter: a bounded content-addressed bundle with independently versioned
metadata, lexical, semantic, evidence-graph, and Git-graph views.
`sqlite_managed_index_v3.py` and `postgres_managed_index_v3.py` persist exact
immutable bytes and one CAS head per tenant/repository/environment. Semantic
query provider/version/vector evidence is bound to the raw-query digest and
prepared context. Index matching remains discovery, not authorization.
`durable_retrieval_preparation_v3.py` now attaches that authenticated
preparation to one durable `CREATED` GateSession, stores and reads back the
exact evidence pair, and CAS-publishes `PREPARED` under the same authorization
scope. `durable_semantic_gate_v3.py` then provides the opt-in verified
`AWAITING_DECISION`/`DECIDED` continuation over that evidence.
`durable_finalization_v3.py` then rechecks authorization/head/policy, renders
only the final allowed set, retains the exact UsageDecision/injection/replay
bundle, and CAS-publishes `FINALIZED`. Production sharding/workers,
protected-content encryption, and active adapter wiring remain outstanding.
See
[authenticated retrieval preparation v3](protocols/retrieval-preparation-v3.md),
[managed index bundle v3](protocols/managed-index-v3.md), and
[durable retrieval preparation v3](protocols/durable-retrieval-preparation-v3.md),
[durable Semantic Gate v3](protocols/durable-semantic-gate-v3.md), and
[durable finalization v3](protocols/durable-finalization-v3.md).

## PostgreSQL Runtime Repository

`PostgresMemoryRepository` is the implemented synchronous persistence boundary
for a complete `TraceBackedMemoryStore`. It is public from the package root,
but its `psycopg` dependency remains optional and lazy: core package import does
not import the driver, while `PostgresMemoryRepository.connect()` requires the
`postgres` extra.

The repository operates only on a fresh `public` schema installed from the
canonical `schemas/postgres.sql` bytes. Checkout users may use that path
directly; installed-package users export the same bytes with `tbm resource
export schemas/postgres.sql postgres.sql`. It locks the one schema metadata row
and requires schema version 2. Existing version-1 installations must first
apply the packaged `schemas/postgres-v1-to-v2.sql` migration. The adapter does
not run migrations automatically. Existing version-2 databases created before
the lesson/source-case lock-order fix should apply the idempotent, version-gated
`schemas/postgres-v2-lock-order-hotfix.sql` operator script. Fresh installs and
the current v1-to-v2 migration already contain the fix. This is the current PostgreSQL schema
version 2 contract.

The fresh-install Trace table uses the named
`traces_latency_ms_non_negative` CHECK, matching `minimum: 0` in the canonical
and packaged Trace JSON Schema. Its signed `INTEGER` column matches the Schema's
`maximum: 2147483647`. Version-1 databases already enforce the physical upper
bound; the v1-to-v2 migration preserves it while installing the current lower
bound and audit protections. Store construction and repository `load()` reject
either out-of-range direction. `sync()` accepts only a validated Store and
therefore never writes an out-of-range value, but its additive semantics do not
inspect unrelated database-only rows. Snapshot version 2 remains unchanged;
the current PostgreSQL contract is schema version 2 and the packaged allowlist
contains 147 resources.

`sync(store)` first snapshots the in-memory store, then opens one database
transaction and locks schema metadata `FOR UPDATE`. Synchronization is additive:
it inserts absent records, retains database records that are not in the supplied
snapshot, and never performs destructive reconciliation. Existing records are
compared in canonical form before a write. Immutable ID conflicts abort the
operation, so the transaction rolls back every earlier insert or lifecycle
update in that synchronization.

Trace identity, provenance, input hash, retrieved context, tool calls, and
creation time are immutable. A stored `unknown` Trace may complete once to a
measured result while filling only empty `output_hash`, `tool_outputs`,
`latency_ms`, `cost_usd`, `error`, and `trace_uri` slots or preserving
populated slots exactly. Usage logs are immutable except for their separate
forward outcome transition from `NULL` or `unknown`; every other usage field
remains immutable. Target rows are locked; exact replay is unchanged, and
downgrades, measured-result rewrites, populated-evidence rewrites, attribution
rewrites, or changes to every other protected field conflict. Failure cases may
update only diagnosis
(`failure_type`, `symptom`, and `root_cause`), review (`reviewed_by`,
`review_notes`, and `reviewed_at`), fix and regression (`fix`,
`fix_commit_sha`, and `regression_passed`), and `status`.
Their identity, source provenance, and creation timestamp remain immutable. For
existing rows, lessons and project policies may update only `status`; a
difference in any other field is a conflict.

An absent selector cannot lock a primary-key gap. The repository attempts each
absent-row INSERT inside a nested savepoint. SQLSTATE `23505`, or the runtime
memory registry trigger's exact `P0001` message and function context, causes the
same ID selector to run again `FOR UPDATE` after the failed INSERT is rolled
back. A concurrent same-primary-key row then follows the existing canonical
path: exact replay is `unchanged`, a supported forward transition is `updated`,
and a protected difference raises `PostgresConflictError`. If the target row is
still absent, the original collision is re-raised and sanitized as
`PostgresPersistenceError`; all other driver errors bypass revalidation. This
does not overwrite the concurrent row or change the DDL.

A store completed through `complete_memory_run()` still contains the same two
persisted records; `MemoryRunCompletion` is not stored. `sync(store)` processes
the linked `trace_id` and `decision_id` updates in its existing transaction, so
an atomic synchronization either commits both forward transitions or rolls the
Trace update back when the usage row conflicts. This also supports persistence
after partial recovery. Snapshot version 2, JSON Schemas, active-lessons YAML,
and PostgreSQL schema version 2 remain unchanged.

Database triggers still enforce forward-only status transitions. Failure cases
may move from `draft` to `verified` or `obsolete`, and from `verified` to
`obsolete`. Lessons and project policies may move from `active` to `obsolete`.
Same-state writes remain valid, obsolete records cannot be reactivated, and
obsoleting a failure case cascades its active lessons to obsolete. Other parent
updates that would leave an active lesson without a verified,
regression-backed source case are rejected.

`load()` opens a transaction, locks schema metadata `FOR SHARE`, and then takes
ordered `SHARE` locks on traces, failure cases, lessons, project policies, and
usage decisions before the first collection read. Concurrent readers remain
allowed, while external writers wait until the load transaction ends. The five
queries therefore observe one stable committed table state even at the default
`READ COMMITTED` isolation level and inside the repository's nested savepoint.
After the locks, one scalar five-table `count(*)` count preflight enforces the
snapshot defaults of 100,000 records per collection and 250,000 records in
total. An oversized database is rejected before any collection row is fetched
or decoded. Accepted counts are followed by a second scalar preflight. It uses
schema-qualified PostgreSQL 12 functions to convert each loaded row projection
to a JSON object, measure that representation in UTF-8, and return only
`max_record_bytes` and `total_bytes`. A largest row or five-table aggregate
above 64 MiB is rejected before a collection selector runs. The stable table
locks prevent counts or payloads from changing before the bounded reads
complete. The Store repeats the record-count validation as defense in depth.
The loader normalizes that database representation into the canonical snapshot
shape and reconstructs the store through its normal validation path. It rejects
database data that cannot form a valid store rather than returning partial or
unvalidated records.

The failure-case, lesson, and project-policy projections subtract their
internal `updated_at` column before measurement because their collection
selectors do not fetch it. Trace and usage-decision projections retain every
physical column. The payload byte count still describes compact PostgreSQL row
JSON, not the indented snapshot file envelope written by `save_json()`. Both
exact 64 MiB boundaries are accepted. Overflow retains the repository's
sanitized persistence error, rolls back the operation, and returns no partial
Store. `sync()` is unchanged: its input is already caller-owned client memory.
These preflights change no public API, snapshot version 2, JSON Schema,
active-lessons YAML, packaged resource, PostgreSQL DDL, or PostgreSQL schema
version 2.

The repository uses the schema owner or an equivalent write-capable role. On
PostgreSQL 12, explicit `SHARE` table locks require table-level `UPDATE`,
`DELETE`, or `TRUNCATE` privilege. A successful lock acquired inside a nested
repository savepoint belongs to the caller's outer transaction until its final
commit or rollback, so long-lived outer transactions intentionally extend the
external-writer wait boundary.

Failure-case, lesson, and project-policy ID selectors use the same `FOR UPDATE`
rule as Trace and usage-decision selectors. If an external row writer is
already active, synchronization waits and then reruns canonical validation on
the committed current row. A newly changed protected field conflicts before a
lifecycle update, and every post-select update must affect exactly one row.

`PostgresMemoryRepository(connection)` borrows a caller-provided connection;
`close()` and context-manager exit do not close that borrowed connection.
`PostgresMemoryRepository.connect(...)` creates an owned connection, and its
context manager closes it. The repository does not provide connection pooling.
When the supplied connection already has an active caller transaction, each
repository operation uses a nested savepoint and does not commit or roll back
the outer transaction; the caller owns the final commit or rollback. Without an
outer transaction, the repository transaction commits normally.

## Layer 5: PR / CI Memory Report

The in-memory reference store can generate a PR-oriented memory report from the same trace
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

Legacy PR warning names are validated and normalized in one pass before
ancestry or case scanning. The Store retains the first occurrence of at most 7
supported names. Duplicate and unknown strings remain valid inputs but cannot
increase the case cross product. Set-backed stable output deduplication keeps
the original first-occurrence order while making legacy warning work expected
`O(W + C)` for `W` caller entries and `C` related cases.

The supported unique field set also defines the cardinality boundary:
`PRChangeSet` accepts at most 6 entries. Both Store interfaces reject a seventh
item before entry shape or PR case scanning, then use one pass of bounded sets
to collect unsupported and duplicate names while preserving error priority.
The CLI maps the rejection to input error exit code 2 and returns without Git
ancestry capture.

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

The read-only CLI adapter exposes `pr-report SNAPSHOT CONTEXT_JSON
CHANGE_SET_JSON --repo-path REPO_PATH`. It strictly maps `CONTEXT_JSON` to a
validated `MemoryContext` and the exact `field_changes` objects in
`CHANGE_SET_JSON` to one immutable `PRChangeSet`. The adapter calls
`pr_report_commit_anchors()`, releases the Store lock, calls
`capture_commit_ancestry()` against the explicit repository, and then calls
`pr_memory_report()` with that same change-set object and evidence. It emits a
deterministic envelope containing `commit_ancestry` and `report` and never
calls `save_json()`.

Git capture disables lazy fetch and places an option terminator before revision
arguments. Missing objects and other Git failures are state errors rather than
silently unfiltered reports. The CLI does not expose broad `changed_fields`,
accept supplied ancestry evidence, infer a repository, or compare with `HEAD`.
Input documents retain the bounded CLI JSON contract. This adapter adds no
record or schema: snapshot version 2, JSON Schemas, active-lessons YAML,
packaged resource bytes, and PostgreSQL schema version 2 remain unchanged.

Change sets and endpoint tags are ephemeral report-boundary values. They are
not serialized or stored: snapshot version remains 2, JSON Schemas and
active-lessons YAML remain unchanged, and PostgreSQL schema version remains 2.

## Git Ancestry Applicability

`CommitAncestryEvidence` is an immutable request-time record of whether each
discovered anchor is an ancestor of one exact current commit. Callers first
obtain the complete metadata-scoped runtime anchor set with
`candidate_commit_anchors(context)`, then run
`capture_commit_ancestry(context.commit_sha, anchors, repo_path=...)` outside
the store lock. Capture evaluates `git merge-base --is-ancestor anchor
current`: exit 0 is `True`, exit 1 is `False`, and any other command failure
raises an error that stops the workflow.

Ancestry collection is also bounded before process work.
`COMMIT_ANCESTRY_MAX_ANCHORS` is 1,000 submitted entries per capture call,
counted before deduplication. The validator consumes at most 1,001 iterable
values and starts no Git command on overflow; accepted values retain sorted,
unique evidence output.

The default Git capture runner uses binary `Popen` pipes with `stdin=DEVNULL`,
a 30 seconds timeout, and explicit UTF-8 replacement decoding. Two concurrent
readers retain at most 64 KiB of ordinary stdout and stderr; timeout or output
overflow kills and reaps the process. Trace metadata status capture retains
only the first byte of `git status --porcelain` and drains the rest, preserving
dirty semantics without caller-sized memory. Injected runner APIs and command
order are unchanged. Malformed injected results are validated after each
command, before the next command or Trace construction. The runner is ephemeral
infrastructure: snapshot version 2 and PostgreSQL schema version 2 remain
unchanged.

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

## Version-3 migration preparation

The version-3 preparation path is deliberately separate from the runtime
Store. `SnapshotV3MigrationMapping` supplies explicit canonical repository and
tenant bindings, authorization scopes, structured regression evidence,
privileged global-policy approvals, and an explicit ancestry policy.
`plan_snapshot_v3_migration()` strictly reconstructs one frozen version-2
source snapshot, validates the mapping, invokes a trusted relation verifier
when ancestry is required, and returns a deterministic read-only plan with
stable issue codes. It does not synthesize a partial version-3 snapshot.

`tbm.snapshot.v2-to-v3.bundle.v1` then content-addresses the exact source,
normalized source state, mapping, and plan. Bundle parsing is bounded and
duplicate-key rejecting; verification reruns the full preflight and requires
an exact plan replay. Content addressing detects modification but is not an
identity signature or evidence attestation.

`SQLiteV3MigrationRepository` stores these inert bundles in separate tables.
The PostgreSQL operator resources create and remove only
`trace_backed_memory_v3_staging`; canonical triggers reject ordinary update,
delete, and truncate statements, while the schema owner and superusers remain
trusted because they can alter those triggers. Rollback enumerates known
objects and uses `RESTRICT`, so unexpected objects or external dependencies
fail closed. Both staging paths remain invisible to the active adapters and
provide no publication or activation operation.
Snapshot version 2, SQLite schema version 1, and PostgreSQL schema version 2
therefore remain the runtime compatibility boundary. The complete contract is
documented in
[Version-3 migration bundles and isolated staging](migrations/v3-staging-bundles.md).

## Durable GateSession version-3 contract

`gate_session_v3.py` publishes the persistence-neutral
`tbm.gate-session.v3` record and explicit transition graph required by future
SQLite v2, PostgreSQL v3, `tbmd`, HTTP, MCP, and SDK implementations. One
immutable record binds tenant, canonical repository, principal, agent client,
Trace/run identity, request fingerprint, idempotency key, expiry, lease, and
the IDs of lifecycle evidence. Every state change requires the current
revision and returns `version + 1`; stale revisions and illegal transitions
have separate stable error codes.

Lifecycle and lease timestamps are server-authoritative. The pure domain
contract accepts explicit timestamps so replay remains deterministic; it does
not authorize clients to choose time. Future repositories must compare
transactional database/service time with the persisted lease and expiry before
committing a transition.

The record accumulates references in lifecycle order: retrieval snapshot and
System Gate evaluation, semantic Gate decision attempts, exact final memory
revisions, injection artifact and usage decision, then run outcome.
Cancellation, expiry, and abandonment are terminal and retain a bounded
reason. Active states require a lease; terminal states clear it. Strict
external parsing is bounded, duplicate-key rejecting, finite-number checked,
and closed to unknown fields. The full contract is documented in
[Durable GateSession version-3 contract](protocols/gate-session-v3.md).

`sqlite_gate_session_v3.py` adds an opt-in, side-by-side local repository. It
stores append-only canonical revision payloads plus a CAS head, scopes its
atomic idempotency index by tenant/repository/principal/agent, uses trusted
service time, preserves caller transactions through savepoints, detects
canonical DDL drift, and exposes bounded due-session discovery. Its separate
metadata and `schemas/sqlite-v3-gate-session.sql` leave the active SQLite
schema version at 1.

`postgres_gate_session_v3.py` adds the matching opt-in PostgreSQL adapter in
the isolated `trace_backed_memory_v3_gate_session` schema. Its version-gated
install and fail-closed rollback preserve active PostgreSQL schema version 2.
Repository operations take metadata locks first, lock a head before sampling
database time, append a canonical revision, and advance the head by exact CAS.
Deterministic C-collated identity indexes, fixed-search-path trigger functions,
catalog-shape verification, caller savepoints, and payload/head cross-checks
provide database-specific enforcement without activating the schema.

Neither repository is wired to the private Store request token. The active
local agent and STDIO MCP remain process-local. Opt-in preparation, Semantic
Gate, finalization, completion, and expiry/recovery services now exercise the
durable lifecycle, but active transport authorization, later phase
orchestration, and full cross-adapter conformance are required before the
session contract becomes the distributed runtime authority.

## Content-addressed replay version-3 contract

`replay_v3.py` publishes storage-neutral descriptors for exact artifact bytes,
the final rendered injection, and the fixed eight-component evidence manifest
needed to replay one decision. Artifact IDs are derived from tagged SHA-256
digests. A complete manifest binds retrieval, both Gates, ancestry, policy,
renderer, and injection evidence; `legacy_partial` records exactly which
components are absent without claiming exact replay. The manifest binds its
own canonical content hash.

Sensitive descriptors require encryption-key metadata, but this pure module
does not store, encrypt, authorize, retain, or log artifact bytes. Hashes prove
content identity, not provenance truth or authorization. Strict parsers are
bounded and reject duplicate keys, unknown fields, invalid timestamps, and
noncanonical component sets. See
[Content-addressed replay contract v3](protocols/replay-v3.md).

`usage_decision_v3.py` adds the content-addressed final-use audit. It preserves
the ordered retrieval/System/Semantic/rendered sets, the exact complement and
System block reasons, current authorization/evidence/policy/renderer linkage,
and the complete replay-component map. Its unsigned canonical JSON is also the
exact retained artifact bytes, so the usage ID deterministically locates its
artifact. See [UsageDecision v3](protocols/usage-decision-v3.md).

`sqlite_replay_v3.py` adds an opt-in isolated ledger with immutable artifact,
injection, and manifest rows. It stores each bundle in one transaction, uses
foreign keys for manifest-to-injection linkage, verifies canonical schema
objects, and revalidates duplicated columns, descriptors, bounds, and exact
bytes on load. Caller transactions retain ownership through savepoints.
`store_complete_bundle()` requires the UsageDecision artifact first and
atomically retains all deduplicated supporting components with the injection
and manifest. The PostgreSQL peer provides the same linkage, idempotency, and
caller-savepoint behavior.

`schemas/postgres-v3-replay.sql` and its fail-closed rollback establish the
matching isolated PostgreSQL relation boundary without changing active schema
version 2. Installation locks active metadata and creates bounded artifact
bytes, injection descriptors, manifests, foreign keys, indexes, and
fixed-search-path immutability triggers atomically. Rollback locks both
metadata rows and ledger tables, then verifies expected catalog membership
before `RESTRICT` removal. SQL enforces derived IDs, exact relational linkage,
and injection shape, but canonical descriptor and content-digest verification
are performed by the opt-in `PostgresReplayV3Repository` before writes and
after loads. The repository mirrors SQLite idempotency/conflict behavior,
preserves caller transaction ownership through psycopg savepoints, and checks
metadata, catalog, trigger shape/state, and canonical function bodies on every
operation. Cross-record authorization and the GateSession service transaction
remain outstanding.

The active v2 Store and persistence adapters do not emit these contracts. The
SQLite and PostgreSQL replay repositories supply atomic artifact storage.
Neither supplies GateSession linkage, access control, encryption, retention,
or runtime authority. Those boundaries and active service integration remain
required in the coordinated version-3 runtime.

## Authorization version-3 contract

`authorization_v3.py` publishes a storage-neutral policy and evaluator for the
future service boundary. A policy binds principals and agent clients to exact
canonical repositories, tenant-scoped aliases, explicit permissions, and
global/tenant/repository role scopes. Policy construction validates registry
uniqueness and every cross-record target before a request can be evaluated.
Repository operations require an exact tenant/repository target, while
`memory:review` and `memory:activate` accept either an exact repository target
or the enclosing tenant target. Global policy creation and approval remain
separate targetless permissions.

The evaluator is deliberately ordered before retrieval: authenticated
server-owned identity context, status and tenant checks, exact repository
resolution, then active binding evaluation. Scope attributes reuse the bounded
applicability vocabulary but are ignored by authorization. They may narrow
later retrieval; they cannot grant access. Decisions bind the exact canonical
request and policy hashes and can be recomputed with
`verify_authorization_decision`.

The hashes are content identities, not signatures. The opt-in isolated
`SQLiteAuthorizationV3Repository` and
`PostgresAuthorizationV3Repository` persist immutable policies and decisions,
require exact request/policy/decision verification before append, enforce one
decision identity per request, revalidate stored descriptors, and fail closed
on schema drift while preserving caller transactions with savepoints.
PostgreSQL uses atomic, active-v2-gated install and fail-closed rollback
resources with immutable triggers and exact catalog checks. Neither authority
authenticates callers, issues reusable capabilities, or connects to the active
Store, Agent, MCP, or GateSession repositories. See
[Authorization v3 contract](protocols/authorization-v3.md).

`entity_registry_v3.py` closes the authorization namespace with versioned,
content-addressed Organization, formal Tenant, and Environment identities. It
reuses the authorization policy's Principal, AgentClient, canonical
Repository, alias, and RoleBinding records, then requires every referenced
tenant to be active under an active organization and every repository-scoped
environment to remain in that repository's tenant. This is referential
integrity, not caller authentication or authorization. The opt-in isolated
`SQLiteEntityRegistryV3Repository` materializes every record, permission, and
attribute into normalized immutable rows with composite foreign keys, then
revalidates those rows against canonical descriptor bytes on every read. It
preserves caller transactions through savepoints and fails closed on schema
drift. `PostgresEntityRegistryV3Repository` provides matching normalized
persistence with active-v2 install gating, full catalog/ACL fingerprinting,
immutable DML/TRUNCATE guards, concurrent exact replay, caller savepoints, and
fail-closed rollback that preserves schema-external dependencies. The active
adapters do not consume either authority yet. See
[Entity registry v3 contract](protocols/entity-registry-v3.md).

`service_v3.py` adds the first storage-neutral authenticated retrieval
orchestrator above those authorities. Trusted transport code supplies exact
Principal/AgentClient records and server-owned tenant, repository, and
environment context. The orchestrator evaluates and persists authorization,
reads the exact decision back, reloads the complete registry to detect policy
or entity rotation, validates the active environment against the canonical
target, and only then calls retrieval. Denial, persistence failure, drift, and
callback failure are fail-closed with sanitized stable errors.
`authenticated_agent_v3.py` provides an opt-in active local-agent facade:
the prepare context contains no identity or target fields, legacy Trace
tenant/repository values are overwritten, authorization completes before
Trace registration, and the canonical authorized tenant/repository bind both
Trace and retrieval context. Process-local ownership indexes prevent one
facade from using another facade's lifecycle handles. It is not transport
authentication. MCP can opt into it through trusted local startup; general
CLI operations, HTTP, and SDK adapters do not select it yet. See
[Authenticated retrieval service boundary](protocols/authenticated-service-v3.md).

`gate_service_v3.py` composes that boundary with either GateSession authority.
It persists and reads back a scoped `CREATED` session before preparation,
suppresses duplicate preparation on exact idempotent replay, requires a trusted
verifier for RetrievalSnapshot/SystemGateEvaluation evidence, and CAS-publishes
`PREPARED` only after verification. Failures attempt exact `CANCELED`
compensation; concurrent or abnormal durable state is returned as recovery
required. It does not persist a Store token and does not claim an atomic
cross-authority transaction. See
[Authenticated durable Gate preparation](protocols/authenticated-gate-service-v3.md).

`sqlite_gate_evidence_v3.py` and `postgres_gate_evidence_v3.py` supply
immutable evidence authorities behind that verifier. Each atomically stores
one exact RetrievalSnapshot/System Gate pair and reads both records back
before the storage-neutral verifier binds them to the authorized session,
Trace, run, and identity scope. SQLite rejects replacement writes through
recursive immutable triggers. PostgreSQL adds active-v2 install gating,
complete security-catalog fingerprinting, concurrent exact replay, and a
fail-closed `RESTRICT` rollback. The evidence write and GateSession transition
remain ordered compensation across authorities, not one atomic transaction.
See [SQLite and PostgreSQL Gate evidence v3](protocols/sqlite-gate-evidence-v3.md).

`durable_retrieval_preparation_v3.py` closes the opt-in composition gap between
those components. One server-derived request fingerprint binds the retrieval
request, Trace/run, context, query digest, semantic evidence, expiry, and lease.
The Gate and retrieval services must share one authorization service, so a new
session records one authorization decision and runs preparation only inside
that same authorized scope. The service writes and verifies the exact evidence
pair before the Gate service publishes `PREPARED`; exact replay does not repeat
discovery or evidence writes. With separate authorities, later-transition
failure may leave immutable orphan evidence beside a canceled session. When
both SQLite or both PostgreSQL repositories deliberately share one
caller-owned connection, the caller may wrap the operation in an outer
transaction and roll back both. This bridge remains opt-in and outside active
Agent/MCP adapters. See
[durable retrieval preparation v3](protocols/durable-retrieval-preparation-v3.md).

`sqlite_semantic_gate_v3.py` extends that SQLite evidence boundary with one
immutable ordered SemanticGateAttempt chain per System Gate evaluation. A
unique sequence and CAS head reject forks, canonical read-back verifies every
descriptor and relational column, and the whole-chain verifier rechecks
monotonic narrowing against the stored Gate evidence. It remains an opt-in
side-by-side ledger. `postgres_semantic_gate_v3.py` provides the isolated
PostgreSQL peer with active-v2 install gating, row-lock serialization,
deferred chain consistency, exact security-catalog validation, caller
savepoints, and fail-closed `RESTRICT` rollback. Both remain outside active
Agent/MCP emission. `semantic_gate_artifact_v3.py` now binds exact non-empty
prompt/response bytes, content-derived IDs, classifications, and encryption
metadata to each attempt role without embedding the bytes in JSON. Durable
SQLite storage is now provided by
`sqlite_semantic_gate_artifact_v3.py`: one outer transaction composes attempt
append, exact public/internal bytes, role bindings, SQL digest/descriptor
guards, and full read-back. PostgreSQL artifact storage is now provided by
`postgres_semantic_gate_artifact_v3.py`. Its
isolated active-v2-gated schema adds database SHA-256/descriptor guards,
catalog validation, concurrent exact replay, caller savepoints, and
fail-closed `RESTRICT` rollback. Both byte repositories reject sensitive
plaintext because neither provides encryption at rest.
`semantic_gate_service_v3.py` now authenticates an exact trusted
provider/authenticator/credential registration, reloads Gate evidence and the
current retry parent before the call, owns provider/model/template/config
provenance, samples trusted start/finish time, and atomically retains the
attempt plus exact bytes through either repository. Repeated prompt/response
bytes reuse their immutable content-addressed descriptor while each attempt
keeps a distinct role binding.
`durable_semantic_gate_v3.py` composes that authenticated service with either
GateSession authority. It verifies the prepared session against the immutable
snapshot/evaluation/attempt chain, CAS-publishes `AWAITING_DECISION`, invokes
the provider, then reads back the complete monotonic chain before
CAS-publishing `DECIDED` with every attempt ID and the successful decision.
Failed attempts remain awaiting for an explicit parent-bound retry. A retained
success can complete the session without another provider call, while
ambiguous or terminal state is recovery-required. Shared caller-owned SQLite
or PostgreSQL connections can place both session transitions and attempt-byte
storage under one outer transaction; otherwise the service provides ordered
recovery, not a distributed transaction. Replay-manifest/finalization linkage
is provided by `durable_finalization_v3.py`: it revalidates the current
authorization event, active heads, and policy around deterministic bounded
rendering; retains and reads back the complete component bundle; and
CAS-publishes `FINALIZED`. Shared SQLite/PostgreSQL connections allow caller
rollback across lease, bundle, and final session revision; separated
authorities use explicit recovery. Signed provider attestation,
protected-content encryption, retention/replay-read authorization, durable
transition-event linkage, and active emission are not yet provided.
`durable_execution_v3.py` supplies the opt-in runtime back half: it replays
and verifies the exact retained finalization bundle, requires current
owner-matched transition authorization, CAS-publishes `EXECUTING`, supports
exact-version resume/abandonment, authenticates a registered outcome
evaluator, and composes the existing atomic RunOutcome, `COMPLETED`, and
completion-outbox authority. External execution remains outside database
transactions and is idempotent by the stable session `run_id`.
`durable_agent_v3.py` supplies the adapter-neutral composition over those four
durable stages. It accepts trusted contexts and versioned requests, never a
caller-built scope. For continuation it reloads the GateSession and retained
RetrievalSnapshot, verifies session/Trace/run linkage, reconstructs the
original scope from the retained authorization decision and current registry,
and rejects any mismatched authority graph at construction. Every post-prepare
GateSession mutation obtains a fresh transition authorization. The facade adds exact-version
cancellation and current-state reads, but it is not yet constructed by the
default Agent or transport adapters. See
[Authenticated Semantic Gate service v3](protocols/semantic-gate-service-v3.md),
[durable Semantic Gate v3](protocols/durable-semantic-gate-v3.md),
[durable finalization v3](protocols/durable-finalization-v3.md),
[durable execution v3](protocols/durable-execution-v3.md),
[authenticated durable Agent v3](protocols/durable-agent-v3.md),
[UsageDecision v3](protocols/usage-decision-v3.md),
[Semantic Gate artifact binding v3](protocols/semantic-gate-artifact-v3.md),
[SQLite Semantic Gate artifact repository v3](protocols/sqlite-semantic-gate-artifact-v3.md),
[PostgreSQL Semantic Gate artifact repository v3](protocols/postgres-semantic-gate-artifact-v3.md),
[SQLite Semantic Gate attempt ledger v3](protocols/sqlite-semantic-gate-v3.md)
and
[PostgreSQL Semantic Gate attempt ledger v3](protocols/postgres-semantic-gate-v3.md).

`gate_worker_v3.py` adds the first bounded recovery worker over both
GateSession authorities. It prevalidates the unlocked due page, expires only
session-expired `PREPARED`/`AWAITING_DECISION` heads with exact CAS and
read-back, reports lease-only and graph-blocked states as recovery required,
and classifies concurrent head movement as superseded. Each candidate is an
independent operation rather than one batch transaction. See
[GateSession recovery worker](protocols/gate-recovery-worker-v3.md).

The storage-neutral `tbm.regression-evidence.v3` record is the first
production-oriented evidence boundary beyond the migration mapping. Its
content-derived identity binds distinct source and verification traces,
expected/observed outcomes, evaluator/environment provenance, exact
source-to-fix-to-verification commit relationships, artifacts, independent
submitter/verifier principals, and an attestation hash. It does not activate a
memory, verify signatures, or replace the active v2 boolean. See
[Structured regression evidence v3](protocols/evidence-v3.md).

The proposal-only `tbm.memory-revision.v3` contract then binds a stable memory
identity to an immutable, content-derived revision, exact parent revision,
content artifact, canonical authorization scope, case/fix/evidence references,
and server-owned proposer context. Its evidence preflight rejects missing,
non-passing, cross-case, or proposer-conflicted evidence. Separate
`tbm.memory-revision-approval.v3` and
`tbm.memory-revision-activation.v3` content-derived events now provide the
storage-neutral publication contract. Approval re-verifies exact bytes,
evidence, lineage, actor separation, and `memory:review`; activation replays
that complete approval verification and independently checks
`memory:activate`, a third actor, and linear immediate-predecessor linkage.
The storage-neutral builder does not establish durable currentness by itself.
Global revision publication and target relocation inside a chain are
forbidden. These events are not signatures. Opt-in SQLite and isolated
PostgreSQL publication authorities now provide the durable head lock, exact
authorization provenance, caller-owned attestation-verifier boundary,
append-only rows, idempotent replay, and pre-commit read-back without
projecting into active v2. See
[MemoryRevision proposal and publication events v3](protocols/memory-revision-v3.md).

Opt-in isolated SQLite and PostgreSQL proposal ledgers persist that revision
with its exact FixEvidence and ordered regression-evidence closure. Both
validate the complete stored bundle on replay before inserting anything, so
tampered records are rejected rather than repaired. The PostgreSQL peer adds
active-metadata lock ordering, a catalog/ACL fingerprint, immutable
UPDATE/DELETE/TRUNCATE triggers, caller-compatible transactions, and a
fail-closed rollback resource. Neither proposal ledger persists
approval/activation events or performs publication authority, authorization,
retention, or active-v2 projection. See the
[SQLite](protocols/sqlite-memory-revision-v3.md) and
[PostgreSQL](protocols/postgres-memory-revision-v3.md) ledger contracts.
The corresponding
[SQLite](protocols/sqlite-memory-publication-v3.md) and
[PostgreSQL](protocols/postgres-memory-publication-v3.md) publication
authorities depend on those proposal ledgers but isolate approval, activation,
authorization provenance, and target-scoped CAS heads.

The storage-neutral `tbm.retrieval-snapshot.v3` contract records the exact
authorized retrieval result referenced by a prepared GateSession. It binds the
authorization event, context/query digests, retriever and immutable index
versions, ordered memory-revision hits, candidate hashes, finite per-stage and
fused scores, top-K bounds, and explicit truncation reasons under a
content-derived identity. It does not record System Gate or Semantic Gate
outcomes and cannot grant access or reopen a block. The optional
storage-neutral retrieval-preparation kernel now emits this snapshot with a
paired System Gate evaluation after final head/policy rechecks. Active
retrieval still returns `MemoryItem` values and does not use that kernel. See
[Replayable RetrievalSnapshot v3](protocols/retrieval-snapshot-v3.md).

The paired `tbm.system-gate-evaluation.v3` and
`tbm.semantic-gate-attempt.v3` contracts then bind deterministic per-candidate
policy outcomes and ordered model-attempt provenance. Cross-record verification
requires exact session/snapshot/candidate coverage and enforces that final
semantic allows are a subset of System Gate allows while all System blocks
remain blocked. Prompt/response content stays in referenced artifacts. The
retrieval-preparation kernel emits System Gate records only. The opt-in durable
Semantic Gate composition now authenticates provider work, verifies the whole
attempt/artifact chain, and advances a prepared GateSession to `DECIDED`.
The opt-in finalization composition then verifies live
authorization/head/policy state, retains exact final-use evidence, and
advances it to `FINALIZED`. Active runtime policy execution and Agent/MCP
Semantic Gate/finalization/execution emission remain outstanding. See
[Gate evaluation v3](protocols/gate-evaluation-v3.md).

The paired `tbm.run-outcome.v3` and `tbm.outcome-attribution.v3` contracts
complete the storage-neutral runtime evidence chain. RunOutcome binds a
completed GateSession to its exact trace/run/usage decision, evaluator,
output/tool-output digests, artifact evidence, and measurements.
OutcomeAttribution is deliberately separate: runtime observation creates only
an association, while a causal claim requires a non-observational method and
an independent verifier. Existing v2 outcome fields remain authoritative for
the active Store and are not silently upgraded. See
[Run outcome and attribution v3](protocols/outcome-v3.md).

The opt-in `SQLiteOutcomeV3Repository` with
`schemas/sqlite-v3-outcome.sql`, and the isolated
`PostgresOutcomeV3Repository` with `schemas/postgres-v3-outcome*.sql`, provide
matching durable completion transactions. Each shares its connection and lock
with a guarded GateSession authority, derives trace/run/usage identities from
the current `EXECUTING` session, uses one trusted timestamp for both records,
CAS-appends `COMPLETED`, inserts the immutable RunOutcome, and exactly reads
both back before commit. PostgreSQL samples database time only after locking
the current head; its insert trigger reconstructs canonical descriptor bytes
and recomputes the outcome payload SHA-256 before accepting a row. It also
validates the exact install/rollback catalog.
Caller-owned transactions use savepoints. Exact terminal replay is
idempotent; mismatched measurements, stale versions, clock rollback, trigger
or catalog failure, and read-back mismatch roll back the whole operation.
`GateSessionCompletionService` verifies the returned pair and durable
read-back without reproducing lifecycle policy. The opt-in
`SQLiteOutcomeAttributionV3Repository` and the isolated
`PostgresOutcomeAttributionV3Repository` add immutable multi-claim ledgers over
completed outcomes. Both revalidate canonical descriptors and exact
outcome/session/usage/final-revision linkage on append and read, preserve caller
savepoints, and reject replacement writes or schema drift. The PostgreSQL peer
adds database-side content-ID recomputation, row locking, full catalog
validation, concurrent replay, and fail-closed rollback.

`DurableExecutionService` is the authenticated application composition around
those lower-level authorities. Start revalidates the original retrieval
authorization and exact retained injection before the executing CAS. Resume
renews only an exact current executing revision, and abandonment records one
bounded terminal reason. Completion requires a current
`gate_session:transition` decision and exact match between a
transport-authenticated evaluator context and a server-owned trusted
registration before calling the atomic completion-outbox authority. The
returned authorization event identifies the decision verified for that call;
the current GateSession revision does not persist that transition event ID.

`AuthenticatedDurableAgentMemory` is the shared application composition above
the durable services. It deliberately contains no process-local lifecycle
handle map. `prepare` returns the durable session revision; later calls recover
the original `memory:retrieve` scope from the session-linked
RetrievalSnapshot, while
decide/finalize/start/resume/cancel/abandon/complete append a current
`gate_session:transition` decision. Service-graph identity checks require one
authorization service, GateSession authority, evidence authority, Semantic
Gate authority, ActivatedRevision source, and finalization replay path.
SQLite/PostgreSQL tests exercise equivalent lifecycle continuation. The
composition still depends on trusted contexts supplied by an embedding
transport and is not default MCP/HTTP/SDK wiring.

The storage-neutral completion-outbox contract separates one immutable
`execution_completed` event from its append-only delivery revisions. The
opt-in `SQLiteCompletionOutboxV3Repository` and isolated
`PostgresCompletionOutboxV3Repository` extend their matching completion
transactions so the completed GateSession revision, RunOutcome, event, initial
`pending` delivery, and delivery head commit or roll back together. Claims use
bounded leases and versioned heads; acknowledgements, retry waits,
expired-lease reclaims, and dead-letter transitions append new revisions.
Delivery is at least once, so consumers deduplicate by the content-derived
event ID. SQLite uses a thread-local repository mutation scope and shared
connection lock. PostgreSQL uses database-time transitions, row-locked
`SKIP LOCKED` claims, compare-and-swap heads, canonical database triggers,
exact catalog validation, and fail-closed rollback.
The storage-neutral `CompletionOutboxDeliveryWorker` performs one bounded
dispatch pass over either authority. It validates a whole claim page before
consumer side effects, persists only sanitized consumer error codes, uses
exact-version acknowledgement/failure writes, verifies transition semantics
and durable read-back, and reports delivered, retry, dead-letter, superseded,
or recovery-required state explicitly. It does not provide a network client;
the caller-owned consumer must be idempotent by event ID and must choose a
lease that covers its maximum processing time.
The opt-in durable execution composition now supplies evaluator
authentication; artifact authorization and active runtime emission remain
separate work. See
[SQLite RunOutcome completion v3](protocols/sqlite-outcome-v3.md),
[PostgreSQL RunOutcome completion v3](protocols/postgres-outcome-v3.md), and
[SQLite OutcomeAttribution ledger v3](protocols/sqlite-outcome-attribution-v3.md)
and [PostgreSQL OutcomeAttribution ledger v3](protocols/postgres-outcome-attribution-v3.md),
plus [Completion outbox v3](protocols/completion-outbox-v3.md).

The `tbm.audit-event.v3` contract provides a content-addressed append-only
stream with exact parent and actor/reference provenance. The paired
`tbm.recovery-action.v3` record captures one completed recovery attempt and is
verified against the existing derived MemoryRunRemediation or the expected
GateSession revision. The opt-in `SQLiteAuditV3Repository` and isolated
`schemas/sqlite-v3-audit.sql` ledger maintain immutable stream events and CAS
heads, append a RecoveryAction with its matching event atomically, reject
request-digest collisions within a session, revalidate canonical descriptors
on read, preserve caller transactions through savepoints, and fail closed on
schema drift. This repository is evidence storage, not a replacement Store
lifecycle, authorization service, authenticated actor boundary, or atomic
GateSession/remediation transition. See
[Audit event and recovery action v3](protocols/audit-recovery-v3.md).

`PostgresAuditV3Repository` and `schemas/postgres-v3-audit*.sql` provide the
matching opt-in multi-process ledger without changing active PostgreSQL schema
version 2. Installation takes the active-metadata lock first, then creates
bounded stream heads, immutable events, exact RecoveryAction/event pairs,
fixed-search-path triggers, and deferred consistency checks atomically.
Repository appends lock one stream head, revalidate the current parent, insert
the event/action, and advance the head through exact CAS. Every operation
verifies relation, index, constraint, column, trigger binding/state, function
configuration/body, and metadata catalogs. Rollback locks the ledger and
refuses catalog drift or external dependencies before `RESTRICT` removal.
Psycopg nested transactions preserve caller ownership through savepoints.
Like the SQLite ledger, this remains evidence storage rather than an
authorization or Store/GateSession transition boundary.

## Non-goals

The current scope matcher is declared-scope matching, not a multi-tenant
authorization model: a memory that omits `repo` or `tenant` does not acquire
that boundary implicitly. The published authorization-v3 contract prepares
canonical repository and global/repository/tenant role boundaries, but
production isolation still requires authenticated service integration and
durable policy enforcement. Snapshot version 2 also does not persist Gate requests,
retrieval/gate/renderer versions or hashes, or structured regression-run
evidence. Git ancestry remains opt-in. These are schema v3 / PostgreSQL schema
v3 requirements, not properties of the current Alpha contract.

Version-2 snapshots with verified but unreviewed cases require review evidence
before loading. PostgreSQL schema-version-1 installations require the packaged
`schemas/postgres-v1-to-v2.sql` operator migration before synchronization.

- Do not build generic personalization memory first.
- Do not inject raw traces directly into prompts.
- Do not treat vector similarity as sufficient proof of relevance.
- Do not allow the LLM to mark memory active without verification.
- Do not provide an automatic online migration framework beyond the explicit
  PostgreSQL v1-to-v2 operator script.
- Do not provide connection pooling or pool lifecycle management.
- Do not provide `async` PostgreSQL repository support.
