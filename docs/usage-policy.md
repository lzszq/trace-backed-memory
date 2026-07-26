# Memory Usage Policy

**English** | [简体中文](usage-policy.zh-CN.md)

## Rule

Memory is not default context. Memory is historical experience that must be filtered, scoped, and approved before use.

```text
raw trace -> failure case -> verified lesson -> gated runtime memory
```

## SQLite Persistence Boundary

`SQLiteMemoryRepository` persists the same gated Store records and
does not change retrieval or injection eligibility. It uses Python's standard
library `sqlite3`, requires no extra, and operates on schema version 1 from the
canonical or packaged `schemas/sqlite.sql`. `connect(..., initialize=True)` is
the convenience path for a fresh file database.

Synchronization is additive and atomic. A top-level operation uses `BEGIN
IMMEDIATE`; an operation inside a caller-owned transaction uses a savepoint and
does not own the outer commit or rollback. Sync retains database-only records,
allows only the documented Trace, usage-outcome, Failure Case, Lesson, and
Project Policy forward transitions, applies the Failure Case-to-Lesson obsolete
cascade, and rolls back the complete operation on conflict.
One repository instance serializes `sync()`, `load()`, and `close()` with an
`RLock`. Top-level rollback cleanup preserves the primary exception, retries
once, and closes the connection if the transaction still cannot be rolled
back. This catastrophic cleanup path also closes a caller-supplied connection
so its partial transaction cannot be committed.

Load executes in one read transaction, requires schema version 1, and rejects
more than 100,000 rows in any collection or 250,000 overall. It also rejects a
largest canonical JSON payload or aggregate payload above 64 MiB before
returning a fully reconstructed and validated `TraceBackedMemoryStore`. Exact
boundaries are accepted and failures leave the connection reusable.

SQLite rows contain stable IDs and canonical JSON payload envelopes. The Store
is the authority for domain and cross-record invariants; direct SQL payload
mutation is unsupported and may cause the next load or sync to fail. Use a file
database for durable local persistence. SQLite is intended for local harnesses,
CI, and single-host tools; PostgreSQL remains the choice for database-side
JSONB, triggers, row locks, shared-ID enforcement, and multi-client workloads.

## PostgreSQL Persistence Boundary

The optional synchronous PostgreSQL repository persists the same gated store
records; it does not make raw traces eligible for injection or bypass System
Gate and LLM Gate policy. It requires PostgreSQL 12+ because the schema's
hardened JSONB constraints use `jsonb_path_exists`. Install
`trace-backed-memory[postgres]`, apply the canonical `schemas/postgres.sql`
bytes to a fresh `public` schema at version 2, then use
`PostgresMemoryRepository` for persistence. Existing version-1 installations
must first apply the packaged `schemas/postgres-v1-to-v2.sql` migration. A
checkout may use those paths directly. An installed package must first export
the required resource with `tbm resource export`; the exported bytes are
identical.
Existing version-2 databases created before the lesson/source-case lock-order
fix must apply the idempotent, version-gated
`schemas/postgres-v2-lock-order-hotfix.sql` operator script. Fresh installs and
the current v1-to-v2 migration already contain the fix.

Synchronization is additive and atomic. A sync retains database records absent
from the submitted store, permits only supported forward lifecycle updates, and
rolls back the entire transaction on an immutable ID conflict. A pending Trace
may complete only from `unknown` to a measured result while preserving its
provenance and existing execution evidence. A usage decision may separately
advance only from `NULL` or `unknown` to a measured outcome pair; every other
usage field remains immutable. All other protected Trace fields also remain
immutable. Loading normalizes persisted values and
reconstructs the regular validated store. Before reading collections, load
holds ordered `SHARE` locks on all five persistence tables: concurrent readers
remain allowed and external writers wait, so one Store cannot be assembled from
different committed table states. While those locks are held, a five-table
`count(*)` count preflight rejects more than 100,000 records in any collection
or more than 250,000 records overall before any collection row is fetched or
decoded. Accepted counts are followed by a scalar UTF-8 payload preflight. It
measures each loaded row projection as a PostgreSQL JSON object and rejects
either a largest row or five-table aggregate above 64 MiB before a collection
selector runs. Exclude only the internal `updated_at` column from failure-case,
lesson, and project-policy projections because their selectors do not fetch
it; retain every physical Trace and usage-decision column. Store reconstruction
repeats the same count validation after the bounded reads. Sync locks every
existing target row
`FOR UPDATE` before canonical comparison, including failure cases, lessons, and
project policies; a newly committed protected-field difference is a conflict,
not a stale successful lifecycle write. A repository created from a caller
connection borrows it; `connect()` owns and closes the connection. Failure Case
source Trace/commit bindings and Lesson source Case bindings are
immutable even for direct SQL. Automatic online migration beyond the explicit
v1-to-v2 script, connection pooling, and async access are outside this
repository's current policy and implementation.

Because a missing primary-key row cannot be locked, each absent-row INSERT uses
a nested savepoint. A concurrent same-primary-key INSERT that reports SQLSTATE
`23505`, or the runtime registry trigger's exact `P0001` signal, is reselected
`FOR UPDATE` and classified by the same rules: exact replay is `unchanged`, a
supported forward transition is `updated`, and a protected difference raises
`PostgresConflictError`. A collision with no target row and every unrelated
driver error remain `PostgresPersistenceError`; sync never overwrites the
concurrent value.

The count and payload preflights are runtime load guards only. Payload bytes are
the compact PostgreSQL loaded-row JSON representation, not the indented
snapshot file envelope. Exact 64 MiB boundaries are accepted; overflow is
returned through
the existing sanitized `PostgresPersistenceError`, with no partial Store and a
reusable connection. `sync()` behavior is unchanged. The guards change no
public API, snapshot version 2, JSON Schema, active-lessons YAML, packaged
resource, PostgreSQL DDL, or PostgreSQL schema version 2.

Before persistence, require at least one non-whitespace character in stored
identity/linkage fields, required failure text, lesson and policy scope values,
Memory Context string values, and usage-audit mapping keys and values. Preserve
accepted bytes exactly; do not trim or normalize. Keep optional Trace metadata,
unrelated Failure Case narrative fields, and candidate/used/blocked memory-ID
arrays on their existing contract. Canonical and packaged Schemas publish the
same `pattern: "\\S"` boundary, and snapshot CLI reads report violations as
input errors without rewriting the source.

Schema-version-1 `btrim` checks already reject ordinary-space-only values but
are narrower than the portable Python/JSON Schema rule. Repository sync always
receives a validated Store. Direct SQL that writes other whitespace-only values
is outside that write contract and may make repository load fail until the row
is cleaned. Phase 49 does not alter PostgreSQL DDL or schema version 1.

Persisted timestamps must be strict RFC 3339 with an explicit `Z` or numeric
UTC offset. Fractional seconds may contain at most six digits. Lifecycle APIs,
snapshot import, SQLite, PostgreSQL, and canonical JSON Schemas reject
sub-microsecond precision instead of silently truncating it.

Use the schema owner or a write-capable repository role. PostgreSQL 12 requires
table-level `UPDATE`, `DELETE`, or `TRUNCATE` privilege for the explicit
`SHARE` locks used by load. Inside a caller-owned transaction, successful table
and row locks survive the repository savepoint and remain held until the outer
commit or rollback.

When the supplied connection already has an active caller transaction, each
repository operation uses a nested savepoint and does not commit or roll back
the outer transaction; the caller owns the final commit or rollback. Without an
outer transaction, the repository transaction commits normally.

## PostgreSQL Test Runtime Policy

PostgreSQL server tools remain optional for ordinary local pytest runs. When
`initdb`, `pg_ctl`, or `psql` is missing, or when `initdb` cannot legally run as
the current user, the database-backed suite skips with a diagnostic. CI must
set `TBM_REQUIRE_POSTGRES=1` in its dedicated PostgreSQL job so either condition
is a failure, preflight the three executables and `psycopg`, and run the
integration and repository test modules against the private session cluster.
The complete suite also runs on Windows independently of that required Ubuntu
database job. This test-only switch must not be read by package runtime code or
persisted in snapshots, YAML, packaged resources, or PostgreSQL.

## Packaged Resource Policy

Use `packaged_resources()`, `read_packaged_resource()`, or
`export_packaged_resource()` when canonical Schemas, examples, or memory
support files must be available from an installed distribution. Do not infer a
package filesystem path or fall back to the current checkout. Resource names
must come from the fixed canonical allowlist; unknown names and traversal-like
strings are rejected before package access.

The 21 installed resource copies must remain byte-identical to the top-level
authoring files. Wheel and source-distribution verification must fail on a
missing, extra, or changed copy. `PackagedResource` metadata is derived from
installed bytes and includes SHA-256 and byte size. `load_failure_taxonomy()`
without a path uses the packaged canonical taxonomy; an explicit path remains
caller-owned input and follows the existing parser contract.
The allowlist includes fresh-install PostgreSQL schema version 2, the
atomic `schemas/postgres-v1-to-v2.sql` operator migration, and the idempotent
`schemas/postgres-v2-lock-order-hotfix.sql` operator script.

CLI resource reads emit deterministic JSON rather than unframed raw content.
Export is the shell integration path. It must refuse an existing destination
unless `--overwrite` is explicit, publish through a same-directory temporary
file, map name errors to exit 2 and write errors to exit 4, and treat a closed
stdout after a successful export as success to prevent unsafe retry.

## Evidence Ingestion Integrity

Treat only explicit structured failure text as extraction evidence. The
classifier reads `Trace.error`, then top-level `error` fields from
`tool_calls`, followed by top-level `error` fields from `tool_outputs`. Tool
names never select a failure taxonomy entry. Do not search arbitrary tool
fields or nested result text for keywords: provider data may contain examples,
historical errors, or quoted content that does not describe the current run.
Trace errors retain precedence over tool calls, and tool-call errors retain
precedence over tool-output errors when selecting a root cause. A call or
output `name` may label a tool-failure symptom only when that record has a
truthy top-level `error`; without that evidence it must not label a later
Trace or output failure. Treat explicit
`invalid argument` text and the narrow `required argument`, `required
parameter`, `required field`, or `required property` tool-error markers as
argument failures. Do not classify permission or authentication text from the
bare word `required`.

Caller-owned failure-taxonomy and active-lessons YAML must use the repository's
constrained shapes. Duplicate taxonomy descriptions, lesson record fields, or
lesson scope keys are invalid; do not rely on last-key-wins replacement. The
lessons adapter must reject a duplicate anywhere in the document before adding
any lesson to the Store. It must also construct and validate every candidate
against staged state before one all-or-nothing commit, so duplicate IDs or later
semantic failures leave existing Store state unchanged. These checks add no
persisted evidence and leave snapshot version 2, JSON Schemas, active-lessons
YAML, and PostgreSQL schema version 2 unchanged.

Apply the same rule to caller-owned JSON. `TraceBackedMemoryStore.load_json()`,
`parse_memory_context()`, `parse_memory_decision()`, and CLI JSON file parsing
must reject duplicate object keys at every nesting level before conversion to
a mapping; never rely on last-key-wins for identity, provenance, scope, safety,
or Gate fields. Valid JSON and Mapping inputs remain compatible, and the rule
changes no snapshot version 2 or PostgreSQL schema version 2.

Persist local snapshots and active lessons only through `save_json()` and
`save_lessons_yaml()`. Both write canonical LF text to a sibling temporary
file, flush it, call `os.fsync()`, and publish atomically. Replacement uses
`os.replace()`; lesson export may set `overwrite=False` to publish with
`os.link()` and reject an existing destination in the same filesystem
operation. After a successful atomic publish and normal temporary-name cleanup,
POSIX must open and `fsync()` the parent directory; non-POSIX platforms retain
portable atomic publication without claiming directory-sync durability. A
failed serialization, temporary-file sync, link, or replacement must preserve
the old destination and remove the temporary file. A post-publication
parent-directory sync failure must propagate even though the target may already
contain the new bytes; classify that result as indeterminate durability and
inspect the destination before retrying. New lesson exports use
`lesson_text: |`. Imports may accept legacy `>` blocks, but must preserve blank
lines, leading and trailing LF characters, intra-line spaces, and the adapter's
historical literal line breaks exactly; do not assume general YAML folding or
chomping. These rules do not change snapshot version 2, JSON Schemas, or
PostgreSQL schema version 2.

## Bounded Local Document Ingestion

Treat every caller-owned snapshot, active-lessons document, failure taxonomy,
measurement manifest, and tool-output file as bounded local document ingestion.
Read it through a single file handle and reject it before UTF-8 decoding when
it exceeds its byte budget. The defaults are 64 MiB for snapshots, 8 MiB for
active-lessons and CLI JSON, and 1 MiB for failure taxonomies.

Also reject snapshots above 100,000 records per collection or 250,000 total
records, lesson files above 10,000 lessons, taxonomies above 1,000 failure
types, and CLI JSON above 10,000 top-level items, 100,000 JSON nodes, or depth
100. Python callers may set keyword limits such as `max_bytes` to explicit
`None` only for trusted offline migrations. CLI commands always retain the safe
defaults. Fail before Store mutation and do not persist the limits: snapshot
version 2, JSON Schemas, active-lessons YAML, packaged resource bytes, and
PostgreSQL schema version 2 remain unchanged.

Treat `Trace.retrieved_context`, `Trace.tool_calls`, and `Trace.tool_outputs`
as one live structured-JSON domain. Count the three outer lists and every
nested semantic value against a fixed 100,000-node aggregate; count every
object key and string value against a fixed 8 MiB aggregate of UTF-8 text.
Retain depth 100. Reject a container whose immediate children cannot fit before
expanding a traversal stack, and reject non-UTF-8 strings before copying.
Apply this non-configurable guard to record, completion, snapshot import, and
PostgreSQL load paths; boundary failure must leave Store state unchanged.

Keep snapshot usage-log validation average O(n) in records and nested ID/tool
evidence. Reuse load-local indexes for `decision_id`, known memory IDs, legacy
`run_id` resolution, and per-trace tool names, plus per-log sets for
candidate/used/blocked relationships. Do not persist those indexes or use
unordered iteration for diagnostics. Preserve validation precedence, exact
errors, input processing order, snapshot version 2, and PostgreSQL schema
version 2.

Maintain a private derived `decision_id` index for live usage-log operations.
Route snapshot import, finalization, and direct logging through one append
boundary; keep outcome/completion/recovery replacements on the same stable
index. Allocation, duplicate checking, and single lookup must remain average
O(1), with max numeric suffix semantics for imported IDs and no ID consumed by
a failed write. Never serialize the derived index or bypass canonical sorting,
snapshot version 2, or PostgreSQL schema version 2.

Maintain the live `run_id`-to-ordered-`trace_id` derived index only through
`record_trace()`. Commit its entry with the copied Trace under the Store lock
and roll the Trace insertion back if index publication fails. Resolve missing,
unique, and ambiguous run IDs in average O(1); never select one record from a
duplicate run. Store IDs rather than Trace objects so completion replacements
remain current. Rebuild this nonserialized index during validated snapshot
loading without changing legacy migration, canonical output, snapshot version
2, or PostgreSQL schema version 2.

Bound live usage-log memory existence validation by its referenced IDs. When
no snapshot-local `known_memory_ids` set is provided, check each distinct ID
directly against the failure-case, lesson, and project-policy maps in average
O(r), where `r` is the number of referenced IDs. Keep one reused
`known_memory_ids` set for snapshot reconstruction. Add no new derived index,
and preserve deduplication, sorted unknown-ID diagnostics, validation order,
snapshot version 2, and PostgreSQL schema version 2.

Keep `metrics()` to one usage-log pass and O(1) accumulator space. Classify
evaluated cohorts from the persisted `used_memory_ids`, count `pass`, `fail`,
and `error` as evaluated, and keep `unknown` or a missing result unevaluated.
Return `None` only for an empty cohort and `0.0` for a nonempty cohort with no
passes. Count obsolete candidate statuses and wrong-memory attribution in the
same pass. Do not change `memory_outcome_metrics()`, memory-run ordering, CLI
call boundaries, snapshot version 2, or PostgreSQL schema version 2.

Also cap `recover-batch` at 10,000 decision IDs and 10,000 attribution options.
Count submitted values before duplicate detection and reject overflow as input
before snapshot loading, Store construction, recovery, or publication. Do not
offer a CLI opt-out or persist this argument budget.

## Snapshot Operations CLI

Use `tbm` or the equivalent `python -m trace_backed_memory` entry point for
local snapshot operations. The CLI is an operations adapter, not a new policy
or persistence layer: `snapshot validate`, `snapshot stats`, `audit`,
`metrics`, and `remediation` must reuse the store's validation and derived
views. Commands accept one local snapshot only; they do not connect to the
PostgreSQL repository.

Use `lessons export SNAPSHOT DESTINATION [--overwrite]` only as an active-only
portable artifact export. Refuse every existing destination by default, reject
the snapshot itself or any file alias as a destination even with overwrite,
and let `save_lessons_yaml()` own canonical serialization and atomic
publication. Report selected lesson IDs in Store order. Do not mutate or save
the source snapshot.

Use `lessons import SNAPSHOT SOURCE_YAML [--write]` as a complete validation
dry-run by default. Always retain the fixed 8 MiB and 10,000-lesson limits and
call `load_lessons_yaml()` exactly once. Do not add a second YAML parser,
replace existing IDs, skip provenance, or partially accept a document. The
Store's merge, duplicate, shared-ID, scope, source-case, active-only status,
and all-or-nothing rules remain authoritative. Reject every `status` other than
`active`; `obsolete` belongs to full Store lifecycle persistence, not portable
lesson import. The CLI must report this as an input error with exit code 2, and
even explicit `--write` must leave the snapshot unchanged. Only explicit
`--write` may publish a fully validated active document back to the same
snapshot.

Use `obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID
[--write]` only for forward-only deactivation. Require an explicit kind; do not
guess from the shared runtime memory ID namespace. The Store must remain
authoritative for identity validation, current state, idempotence, and the
failure-case cascade to every active derived lesson. Preview the exact sorted
cascade by default and require `--write` for same-snapshot publication. Do not
reactivate obsolete memory, claim an actor/reason audit field that the record
does not store, echo memory text or execution evidence, or implement a batch
through repeated single-record calls. Keep this as the single-item command.

Use `obsolete-batch SNAPSHOT REQUESTS_JSON [--write]` for multi-record
deactivation. Require strict UTF-8 JSON with a non-empty array of exact
`memory_kind`/`memory_id` objects, canonical `failure_case`, `lesson`, and
`project_policy` kinds, and the fixed 8 MiB and 10,000-item limits. Convert the
document to one exact tuple of `MemoryObsolescenceRequest` records and call
`obsolete_memories()` exactly once. The Store must resolve, stage, validate, and
publish explicit records and failure-case cascades all-or-nothing. Preserve
request order for explicit results and sort cascade IDs. An explicitly requested
lesson may overlap the same batch's cascade; union-based `affected_count` must
not double count it. Do not skip invalid items, reactivate memory, expose record
content, or persist the manifest.

Treat every `obsolete`, `obsolete-batch`, `complete`, `complete-batch`, `recover`,
`recover-batch`, and `recover-ready` command as a dry-run unless `--write` is
explicit. A dry-run may mutate the reconstructed store in memory but must leave
the source bytes unchanged. A write is permitted only after the whole operation
succeeds, and it must use `save_json()` to replace the same snapshot atomically.

For explicit snapshot `--write`, acquire the canonical sibling `.tbm.lock`
exclusive advisory lock before snapshot load and hold it across the full
read-modify-write sequence through `save_json()`. Release it before stdout.
Initialize the sidecar with one placeholder byte and keep it persistent so
waiters and newcomers cannot split across different lockfile inodes; lock
ownership belongs to the open OS descriptor and is released after exceptions
or process exit. Before placeholder initialization, require the canonical path
and opened descriptor to identify the same single-link regular file. Reject
symbolic links, Windows reparse points, hard links, and special files without
writing an alias target or loading the snapshot. Report that `OSError`, like a
timeout, as a write error with exit code 4. Repeat the descriptor/path identity
check after OS acquisition and before starting the snapshot transaction. Bound
contention waits to 30 seconds. Do not acquire this lock for dry runs, read-only
snapshot commands, lessons export, or resource export. This coordination adds
no domain state: snapshot version 2 and PostgreSQL schema version 2 remain
unchanged.

For Python-owned snapshot transactions, use the public
`snapshot_write_lock(snapshot_path, timeout_seconds=...)` context manager around
the complete load, mutate, and `save_json()` read-modify-write sequence. It uses
the same canonical sibling `.tbm.lock` as the CLI. Treat it as advisory and
non-reentrant: every local writer must cooperate, and a held scope must be
passed down rather than reacquired. Timeout validation and lock acquisition
must happen before snapshot load; sidecar safety validation must happen before
placeholder writes; exceptions must release descriptor ownership. This helper
is not the Store `RLock` and does not replace a PostgreSQL transaction. It
changes no snapshot version 2 or PostgreSQL schema version 2.

Use `complete` only to submit a fresh measured result for an exact linked
Trace and decision. Require `--eval-result` to state `pass`, `fail`, or `error`;
the command does not infer the outcome, IDs, causal attribution, or execution
evidence. `--memory-caused-failure` defaults to false and the Store remains
authoritative for valid attribution. Optional `--tool-outputs-file` input must
be strict UTF-8 JSON containing an array of objects. Omitted evidence options
must not be forwarded, while an explicit `[]` remains meaningful empty
tool-output evidence. Malformed evidence is an input error and must not write
the snapshot.

Use `complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]` for a non-empty
ordered set of fresh results. `MEASUREMENTS_JSON` must be strict UTF-8 JSON with
a non-empty array of allowlisted `MemoryRunResult` objects. Reject duplicate
object keys, missing or unknown fields, wrong JSON types, and non-finite numbers
as input errors. Do not accept Trace IDs: call `complete_memory_runs()` once so
the Store derives linkage, preserves manifest order, and applies duplicate,
shared-Trace, evidence, replay, and attribution rules all-or-nothing. The
default remains dry-run; only a complete successful batch may reach `--write`.

`recover-ready` may select only remediation action `recover`; it must continue
to skip pending, conflicting, complete, and `recover_with_attribution` work.
Single recovery passes `memory_caused_failure` only when the operator states it
explicitly. Batch decision IDs must be unique, repeated attribution values must
use exact `DECISION_ID=true|false` syntax, and an invalid item must reject the
whole batch all-or-nothing. Operators must investigate conflicts rather than
using the CLI to choose a historical side.

For each attribution value, split on the final `=`. Preserve the complete
non-empty prefix as the decision ID, including any earlier `=` characters, and
accept only exact lowercase `true` or `false` as the suffix. Missing or empty
components, invalid booleans, unrequested IDs, and duplicate attributions are
input errors with exit code 2 and must not reach recovery or publication.

For `recover-batch`, enforce the 10,000 decision IDs and 10,000 attribution
limits before snapshot loading. Overflow is input exit code 2 and must not read,
mutate, or replace the snapshot. At or below the limits, retain the existing
Store-owned uniqueness, attribution, eligibility, ordering, and atomicity
rules.

Automation may consume the single deterministic JSON value written on
success. Failures write one structured JSON error without a traceback. Exit
codes are 0 for success or no-op, 1 for an internal failure, 2 for usage,
snapshot, or lesson input, 3 for recovery-state, attribution, or obsolescence
rejection, and 4 for a lesson destination or snapshot write failure. Error text
is capped at 2,048 characters. JSON serialization must
finish before persistence. After an export or requested write commits, a
downstream stdout pipe closure must not falsely report that committed operation
as failed. Human-readable `--help` output is outside the JSON contract.

CLI reads, audits, metrics, remediation plans, and completion wrappers are not
persisted. Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 2 remain unchanged.

## Suitable modes

| Mode | Default | Allowed memory | Blocked memory |
|---|---:|---|---|
| debug | use | trace summaries, verified failure cases, fix history | secrets, unrelated raw traces |
| repair | use | verified lessons, previous fixes, tool/prompt policy | draft cases, weak guesses |
| regression | use | commit history, eval history, PR memory reports | unrelated project memory |
| planning | cautious | project policy, tool policy, procedural lessons | raw traces |
| eval | usually skip | prompt contract, tool schema policy | prior answers, gold labels, evaluator comments |
| production | minimal | active verified scoped lessons | raw trace, draft memory, sensitive memory |

## System Gate

Runtime context should be parsed through `parse_memory_context()` before
retrieval or gating. The parser accepts JSON strings or mappings, requires
`mode`, `repo`, and `commit_sha`, validates supported modes, and keeps only
known nonblank string fields from `schemas/memory_context.schema.json`.
Direct helper calls are held to the same boundary: candidates and injection
inputs must be lists of unique `MemoryItem` records, System Gate block reasons
must be a string mapping, gate tasks must be non-empty strings, summaries must
be strings, and retrieval queries must be strings or `None`. Invalid structures
raise `ValueError` before rendering or store request registration.

A memory item must satisfy:

```text
status in ["active", "verified"]
memory_type in ["procedural", "semantic", "episodic", "policy"]
scope matches current task
scope keys are known MemoryContext fields
scope values contain at least one non-whitespace character
repo / branch / tenant allowed
not obsolete
not sensitive
not eval-leaking
has source_case_id, source_trace_id, or source_policy_id
```

When context names a tool, Failure Case memory additionally requires that exact
named tool in its source Trace; absent tool evidence is not a wildcard.

Reject immediately when:

```text
status = draft
status = obsolete
missing scope
missing source
contains sensitive raw trace
memory marked sensitive
memory marked eval-leaking
same benchmark expected output
cross-tenant memory
```

## LLM Gate

After System Gate, ask the LLM to judge whether candidate memory should be used.

Recommended prompt:

```text
You are deciding whether retrieved memory should be used for the current LLM/agent task.

Current task:
{{task}}

Current mode:
{{mode}}

Current context:
{{context_summary}}

Candidate memory:
{{candidate_memory}}

Decide whether this memory should be used.

Rules:
1. Use memory only if it is directly relevant to the current task.
2. Do not use memory if it is obsolete, draft, low-confidence, or missing source.
3. Do not use memory if its scope does not match the current repo, tenant, tool, prompt family, model family, or eval suite.
4. Do not use memory if it may leak benchmark answers, evaluator reasoning, private user data, secrets, or sensitive tool output.
5. In eval mode, use only project policy, prompt contract, and tool schema policy. Do not use prior answers or failure traces from the same dataset.
6. In debug or repair mode, similar failure cases and verified lessons may be used.
7. In production mode, use only active, verified, scope-matched, short procedural memory.

Return only this JSON:

{
  "use_memory": true,
  "allowed_memory_ids": [],
  "blocked_memory_ids": [],
  "reason": "brief explanation",
  "risk": "none | low | medium | high",
  "recommended_injection": "none | short_summary | full_case_summary | pointer_only"
}
```

The MVP exposes `parse_memory_decision()` to validate this JSON shape before it
is applied. Together with `parse_memory_context()`, this keeps both external
runtime context and LLM applicability output behind deterministic validators.
The decision must keep `use_memory`, `allowed_memory_ids`, and
`recommended_injection` consistent: memory use requires at least one allowed ID
and a non-`none` injection mode; declining memory requires no allowed IDs and
`recommended_injection: "none"`.
Each decision list accepts at most 50 IDs, the same fixed budget as
`LLM_GATE_MAX_CANDIDATES`. `parse_memory_decision()` and direct
`apply_llm_gate_decision()` calls reject the 51st ID before entry validation,
deduplication, or set construction. `memory_decision.schema.json` encodes this
as `maxItems: 50` for both arrays.
Before field validation, a decision response is limited to 65,536 UTF-8 bytes,
1,000 JSON nodes, and depth 20. `reason` is limited to 2,000 characters across
parsed and direct decisions, usage logs, JSON Schema, and fresh-install
PostgreSQL DDL.
System Gate still remains authoritative: parsed LLM decisions can only narrow
the system-approved memory set, not reopen blocked memory. If the LLM output
lists the same memory ID as both allowed and blocked, blocked wins and the
memory is not injected.
Every system-approved candidate not present in the final allowed set is added
to final blocked IDs, including candidates the LLM simply omitted.
Low-level callers must also provide disjoint System Gate allowed and blocked
results; `apply_llm_gate_decision()` rejects contradictory inputs before it
constructs a final decision.

## Safe Store Workflow

Use `TraceBackedMemoryStore.prepare_memory()` to retrieve candidates, apply
System Gate, and create the bounded LLM prompt. When more than 50 candidates
pass System Gate, preparation keeps the first 50 in deterministic candidate
order and records the overflow as `LLM gate candidate limit exceeded`. Pass the decision payload to
`finalize_memory()` with the trace ID; it rechecks stale state, applies the
LLM decision as a narrowing operation, renders the snippet, and atomically
persists trace ID, context, candidate statuses, and System Gate block reasons.
Only this workflow provides ownership, replay, stale-state, trace-link, and
atomic logging guarantees. Low-level helpers remain available for callers that
own equivalent orchestration.

For the common synchronous case, use `run_memory_execution()` with an already
registered `unknown` Trace. Its `MemoryDecisionCallback` receives the Store's
public `MemoryGateRequest`, and its `MemoryExecutionCallback` receives the
final `GatedMemoryResult`. The executor must return an explicit
`MemoryRunMeasurement`; the module uses the Store-produced `decision_id` and
delegates final validation and atomic assignment to `complete_memory_run()`.

Do not infer an evaluator outcome, `memory_caused_failure`, or error evidence
from an exception. After preparation, `MemoryRunExecutionError` identifies the
`decision`, `finalization`, `execution`, or `completion` phase, retains the
original callback or Store exception, and exposes the request plus any
finalized result and decision ID. A decision or finalization failure must
create no usage log. An execution or completion failure must leave the Trace
and decision unevaluated until an advanced caller explicitly retries,
completes, or applies existing recovery policy. `KeyboardInterrupt` and
`SystemExit` must not be wrapped. `run_memory_execution()` is not an
idempotency token: each call prepares a new request. Retry against the request
or finalized result exposed by the error instead of rerunning the whole helper.

Store preparation errors propagate unchanged because no request has been
created. Later Store validation, stale-state, linkage, evidence, and conflict
errors are retained as the execution error's cause. Advanced callers that
require a pause, manual LLM retry, external side-effect policy, or one-sided
lifecycle control should continue to call
`prepare_memory()`, `finalize_memory()`, and `complete_memory_run()` directly.
The callback module is not a persistence adapter and adds no stored record:
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 2 remain unchanged.

The usual chronology is decision first and evaluation later. Call
`record_trace()` first with an `unknown` current Trace, call
`finalize_memory()` without an outcome, execute the task with the returned
snippet, then call `complete_memory_run()` with the returned `trace_id` and
`decision_id`. One measured result completes the Trace and seals the decision
atomically. The frozen `MemoryRunCompletion` return value exposes defensive
copies of both records.

For intentionally separate audit lifecycles, use `tbm outcome SNAPSHOT
DECISION_ID --eval-result {pass,fail,error}
[--memory-caused-failure true|false] [--write]`. It delegates once to
`record_decision_outcome()` and does not modify the linked Trace. The default is
a complete validation dry-run; `--write` publishes the same snapshot only after
the transition and non-sensitive result serialization succeed. Exact replay of
the pair is a successful no-op, while conflicting result or attribution is a
state error. Output must contain only previous/current outcome fields, the
decision ID, `changed`, and `written`, never runtime context, reason, memory ID
lists, Trace data, or tool evidence. No command record is persisted; snapshot
version 2 and PostgreSQL schema version 2 remain unchanged.

Both records may be pending, either one may already contain the matching result
for partial recovery, or both may match for exact replay. A result, attribution,
Trace evidence, or linkage conflict leaves both records unchanged. Use this
high-level operation for normal memory execution.

Use `complete_memory_runs()` for a batch of newly evaluated runs only when the
whole set must be all-or-nothing. Supply a non-empty tuple of unique frozen
`MemoryRunResult` commands. `MeasuredEvalResult` permits only `pass`, `fail`, or
`error`; the store derives `trace_id` from each decision and preserves request
order in returned completions.

Optional evidence follows the single-run rules: `None` means omitted, while
`tool_outputs` must be a tuple and becomes a Trace list. Results for decisions
on a shared Trace must agree. Evidence fields merge only when disjoint or equal;
never submit different values for the same shared field. Invalid results,
attribution, evidence, partial state, or linkage reject the entire batch before
any Trace or decision changes.

Use `complete_memory_run()` for one new result. Use `recover_memory_runs()` only
when the measured result already exists on the Trace or decision side and must
be derived rather than supplied. Both batch paths share candidate staging, but
recovery retains its stricter eligibility checks. `MemoryRunResult` is not
persisted; snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 2 remain unchanged.

Use `memory_run_audits()` to locate work that did not finish through the normal
path. It returns one record for every usage decision, sorted by `decision_id`.
Each frozen `MemoryRunAudit` carries `trace_id`, `run_id`, both raw result
values, failure attribution, and a derived state: `pending` means neither side
is measured, `trace_only` and `decision_only` are the two partial recovery
directions, `complete` means both measured results agree, and `conflict` means
they differ.

Do not guess through a conflict. The store will never auto-repair one or select
a preferred historical result; review the source evidence instead. Traces with
no usage decision are outside this decision-oriented view, and multiple
decisions for one Trace remain separate. The audit view is derived and not
persisted. Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 2 remain unchanged.

Use `memory_run_remediations()` rather than duplicating state-to-action rules.
Each frozen `MemoryRunRemediation` has a `MemoryRunRemediationAction`:
`measure` means obtain a new evaluator result, `recover` is safe one-sided
recovery, `recover_with_attribution` requires an explicit causal boolean,
`investigate` requires manual conflict review, and `none` means no repair.

Read `resolved_eval_result` and `resolved_memory_caused_failure` only as
current-state evidence. A failed or errored Trace-only run deliberately has no
resolved attribution. Batch only compatible plain `recover` items through
`recover_memory_runs()`; decisions sharing one Trace must resolve to the same
outcome. Complete measured `measure` items through `complete_memory_runs()`.
Do not execute `investigate` automatically.

Plans can become stale immediately after they are returned. The completion and
recovery APIs must reclassify and validate under their own lock; callers must
not treat a plan as authorization to bypass a conflict. Remediations are
derived and not persisted, leaving snapshot version 2, JSON Schemas,
active-lessons YAML, and PostgreSQL schema version 2 unchanged.

Use no-argument `recover_ready_memory_runs()` when one worker should apply all
currently automatic recoveries without a plan-to-write race. The store holds
one reentrant lock while selecting action `recover` in `decision_id` order and
delegating to `recover_memory_runs()`. No ready work returns an empty tuple, and
a second scan after success does not replay complete decisions.

The sweep skips pending, `recover_with_attribution`, `investigate`, and `none`
items. Never use it to avoid explicit attribution for a failed or errored
Trace-only run. Ready decisions on one shared Trace must resolve to the same
result; disagreement or any candidate validation failure rejects the selected
set all-or-nothing. Use explicit recovery APIs when selecting a subset or
supplying attribution.

Concurrent sweeps serialize and re-plan under the lock. The selection is not
persisted; only existing Trace and usage rows are synchronized, leaving
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 2 unchanged.

Use `memory_run_metrics()` for monitoring and alert thresholds rather than
reimplementing audit aggregation. Its frozen `MemoryRunMetrics` counts one
usage decision at a time and exposes `decision_count`, `pending_count`,
`trace_only_count`, `decision_only_count`, `complete_count`, `conflict_count`,
`recoverable_count`, `auto_recoverable_count`, and
`attribution_required_count`. The sum of the five status counts must equal
`decision_count`. `recoverable_count` is the sum of the one-sided status counts
and also equals `auto_recoverable_count + attribution_required_count`. Treat
pending as awaiting measurement and conflict as manual-review work.

The Store computes `memory_run_metrics()` in one usage-log pass without
sorting and with O(1) accumulator space. Continue using
`memory_run_audits()` when decision-ID order is required; the metrics method is
an unordered point-in-time aggregate and does not replace the audit detail.

These health metrics are derived and not persisted, and they do not replace
outcome-oriented `metrics()`. Snapshot and PostgreSQL loads reconstruct them
from existing records, leaving snapshot version 2, JSON Schemas, active-lessons
YAML, and PostgreSQL schema version 2 unchanged.

Recover only audited one-sided states with `recover_memory_run()` using their
`decision_id`. The method does not accept `trace_id` or `eval_result`; it derives
them from the linked validated records, delegates to atomic
`complete_memory_run()`, and returns `MemoryRunCompletion`. `trace_only` uses
the measured Trace result, `decision_only` preserves the sealed result and
`memory_caused_failure`, and `complete` is an exact replay.

Recovery rejects `pending` and `conflict` and never guesses a result. A passed
`trace_only` record implies `memory_caused_failure=False`, but callers must
explicitly choose `True` or `False` for failed or errored Trace-only recovery.
Do not treat the default false value on an unevaluated usage log as causal
evidence. Recovery changes no persistence shape: snapshot version 2, JSON
Schemas, active-lessons YAML, and PostgreSQL schema version 2 remain unchanged.

Use `recover_memory_runs()` only for a preselected non-empty tuple of unique
decision IDs when the whole recovery set must be all-or-nothing. It preserves
request order in the returned completion tuple. Each item must already be
`trace_only`, `decision_only`, or `complete`; any `pending` or `conflict` item
rejects the batch without changing an earlier valid item.

Supply `memory_caused_failures` for every failed or errored Trace-only item.
Omission remains safe only for passing `trace_only` and for preserving sealed
attribution on `decision_only` or `complete`. Results derived by decisions
linked to a shared Trace must agree. Eligibility is fixed at method entry, so a
pending item cannot become recoverable because another item completes that
Trace during candidate staging.

The batch method does not accept `trace_id` or `eval_result` and does not attach
completion evidence. Use `recover_memory_run()` for an individual recovery that
must add output hash, tool outputs, latency, cost, error, or Trace URI. A batch
wrapper is not persisted; only the existing Trace and usage records are
synchronized. Snapshot version 2, JSON Schemas, active-lessons YAML, and
PostgreSQL schema version 2 remain unchanged.

`complete_trace()` accepts only `pass`, `fail`, or `error` and can fill
`output_hash`, `tool_outputs`, `latency_ms`, `cost_usd`, `error`, and
`trace_uri`. Existing non-empty completion evidence and every other Trace
field remain immutable. Exact replay is idempotent; conflicting, reverse, and
partial post-completion rewrites are rejected atomically.

Require `latency_ms` to be `None` or an integer from 0 through 2,147,483,647;
both boundaries are valid. Apply this through the Store's shared Trace
validation for direct recording, snapshot loading, execution, and every
completion path. Do not duplicate the range rule in CLI manifest or argparse
code: a parsed out-of-range measurement is a Store-owned `state` error with
exit code 3, while malformed types remain `input` errors with exit code 2.
Rejection must precede any Trace or usage-log commit. Do not change the existing
finite-number contract for `cost_usd`.

At the Phase 47 baseline, keep canonical and packaged `trace.schema.json` at `minimum: 0` and
`maximum: 2147483647`. Keep the named `traces_latency_ms_non_negative` CHECK
and signed `INTEGER` column in both fresh-install PostgreSQL DDL copies. The
column already supplies the upper bound for every schema-version-1 database,
so Phase 47 is not a database migration; operators missing the earlier CHECK
still own the lower-bound migration. Only the canonical and packaged Trace
Schema bytes changed in Phase 47; that baseline had 18 packaged resource names
and PostgreSQL schema version 2. The current contract is snapshot version 2,
21 packaged resources, and PostgreSQL schema version 2.

Trace completion never seals a usage decision automatically, and decision
outcome sealing never changes a Trace. Use the same evaluator result for both
when using these low-level transitions for separately owned lifecycles or
recovery. `complete_trace()` and `record_decision_outcome()` remain available,
but they are not the preferred normal post-execution workflow.

Only `pass`, `fail`, and `error` can seal an initial `None` or `unknown` result.
The result and `memory_caused_failure` flag are one pair: exact replay is
idempotent, while any rewrite of a sealed pair is rejected atomically. A true
wrong-memory attribution still requires failed or errored use of memory.

For semantic retrieval, compute scores outside the store and pass
`semantic_scores` with an explicit `max_candidates` that must be an integer from
1 through 50 inclusive, and optional `minimum_score`. Do not combine it with
`query`. Treat scores as retrieval
evidence only: sensitive, obsolete, leaking, low-confidence, or out-of-scope
memory must still be blocked by the normal gates.

Validate every score and stored memory ID before metadata, ancestry, or ranking
work. Use a non-copying membership view over the failure-case, lesson, and
policy catalogs; metadata-only and keyword retrieval must not construct that
view. After unchanged filters, use bounded semantic top-k selection rather than
a full sort, while returning score-descending results with
memory-ID-ascending ties. The optimization changes no snapshot version 2 or
PostgreSQL schema version 2 contract.

At finalization and low-level logging, `repo`, `commit_sha`, and `tenant` always
match the linked Trace. `branch`, `prompt_version`, `prompt_family`,
`tool_schema_version`, `model`, and `eval_suite` bind only when the context
declares them. A declared tool must match an exact plain-string Trace tool call;
callers must not rely on coercion. Omitted optional provenance remains broad and
allows a more specific Trace. `model_family`, `task_type`, and `failure_type`
remain unbound because Trace has no corresponding stored provenance.

The store validates this evidence before pending request consumption or
usage-log append. Imported version-2 and supplied legacy context evidence is
subject to the same checks. No persistence contract changes: snapshot version
2 and PostgreSQL schema version 2 remain current.

`MemoryRunCompletion` itself is not persisted. Snapshots retain the existing
Trace and usage log, and PostgreSQL synchronization updates their linked
`trace_id` and `decision_id` rows inside one transaction. A usage-row conflict
therefore rolls back an earlier Trace update. Snapshot version 2, JSON Schemas,
active-lessons YAML, and PostgreSQL schema version 2 remain unchanged.

## Git Ancestry Opt-in

Callers that opt in must first discover the complete anchor set with
`candidate_commit_anchors(context)` for runtime retrieval, or
`pr_report_commit_anchors(context)` for a PR report. They must capture each
anchor against the exact `context.commit_sha` with
`capture_commit_ancestry()` outside the store lock, then pass that unchanged
`CommitAncestryEvidence` object to `candidate_memories()`,
`prepare_memory()`, or `pr_memory_report()`.

One capture accepts at most `COMMIT_ANCESTRY_MAX_ANCHORS` (1,000) submitted
entries. Count entries before deduplication and reject overflow before any Git
runner is called; duplicate-heavy or lazy iterables do not bypass the budget.
Callers must narrow an oversized candidate/report scope before capture.

Default Git capture must use `stdin=DEVNULL`, a 30 seconds timeout, binary
pipes, and explicit UTF-8 replacement decoding. Retain at most 64 KiB for each
ordinary stdout/stderr stream; kill and reap on timeout or output overflow.
For `git status --porcelain`, retain only the first byte for dirty detection
while draining all remaining output. Do not change injected runner signatures,
arguments, or conforming output behavior. Require strings from all four
injected commands. Reject a blank commit SHA, blank repository root, non-string
output, or commit/branch/repository name above 512 characters with
`TraceMetadataCaptureError` before starting the next command. Do not echo the
malformed value. Preserve blank branch as detached HEAD and blank status as
clean. These controls do not alter snapshot version 2 or PostgreSQL schema
version 2.

An exit status of 0 from `git merge-base --is-ancestor` means the anchor is an
ancestor; exit status 1 means it is not and the anchored history is excluded.
Any command error must stop the workflow. Incomplete evidence is rejected:
callers must not omit a discovered anchor or substitute evidence captured for
another current commit. Lesson anchors are their source cases' fix commits,
failure-case anchors are their source commits, and project policies have no
anchor. That policy exemption applies only to ancestry; scope, status,
safety, System Gate, and LLM Gate requirements remain in force.

Passing no ancestry evidence is supported for backward compatibility and
preserves legacy retrieval and PR-report behavior. Evidence is not persisted
in snapshots, YAML, usage logs, or PostgreSQL.

## Outcome Metrics

`pass`, `fail`, and `error` are evaluated outcomes; `error` is an evaluated
non-pass. `unknown` and `None` are unevaluated and must not depress pass rates.
Use `evaluated_with_memory_count` and `evaluated_without_memory_count` as the
rate denominators and `unevaluated_decision_count` as the missing-outcome count.
For values returned by `store.metrics()`, together they equal
`decision_count`; directly constructed legacy values retain zero defaults for
the appended fields.

Use `complete_memory_run()` after evaluation and before reading completed
metrics or synchronizing the completed audit. It moves the decision from the
unevaluated bucket to exactly one evaluated denominator while completing the
linked Trace. Use `record_decision_outcome()` only when Trace completion is
owned separately; neither path creates or persists a separate metric record.

These are decision counts, not per-memory causal attribution. With-memory means
the audited decision has at least one `used_memory_id`; it does not prove that
one particular memory caused the result. Metrics remain derived and are not
persisted; snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 2 do not change.

`memory_outcome_metrics()` returns a stable tuple covering every stored failure
case, lesson, and project policy, including unused IDs. Use `candidate_count`,
`used_count`, and `blocked_count` to inspect retrieval and final decisions;
blocked count includes both deterministic and LLM-narrowing blocks. For used
IDs, inspect `evaluated_use_count`, `passed_use_count`,
`failed_or_errored_use_count`, `unevaluated_use_count`, and
`observed_pass_rate`.

These are observed associations, not causal effectiveness estimates. When one
decision uses several memories, each receives the same observed outcome. The
API does not derive per-memory wrong-memory attribution from
`memory_caused_failure`. Metrics remain derived and are not persisted; callers
must use the validated usage logs when they need deeper audit evidence.

## PR Change-Set Policy

For value-aware PR reporting, callers must supply exact old and new values in
an immutable `PRChangeSet` and bind every new value to the post-change
`MemoryContext`, including `None`. Use the same change set first with
`pr_report_commit_anchors()` and then with `pr_memory_report()`; ancestry
evidence must cover every resulting anchor for the exact context commit.

The report accepts only complete old or complete new endpoints. Callers must
not interpret a trace containing a mixture of old and new values as related.
Repo and tenant are mandatory exact-match Trace filters for report selection,
not multi-tenant authorization boundaries. Unchanged declared trace-backed
context metadata remains exact-match. Exact value-aware change
sets support only `prompt_version`, `prompt_family`, `tool`,
`tool_schema_version`, `model`, and `eval_suite`. Callers must not claim exact
`model_family` provenance: it is unsupported because traces do not record it.

Limit `PRChangeSet` to at most 6 entries, matching the six supported unique
fields. Reject a seventh item before entry shape, endpoint, or historical case
scanning. For accepted cardinality, detect unsupported and duplicate field
names in one pass while reporting unsupported names first. The CLI must return
an input error with exit code 2 and stop without Git ancestry capture.

Existing `changed_fields` reports remain available for legacy broad
field-name-only behavior, including legacy `model_family` warnings. Change
sets and endpoint tags are ephemeral report inputs and outputs, not persisted
records or schema extensions.

Validate legacy PR warning names in one pass before scanning cases. Retain the
first occurrence of at most 7 supported names; continue accepting duplicate
and unknown non-empty strings, but do not let them multiply case-level work.
Stable output deduplication must preserve warning order and keep expected
legacy warning complexity at `O(W + C)`. Snapshot version 2 and PostgreSQL
schema version 2 remain unchanged.

For CI, use the read-only `pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON
--repo-path REPO_PATH` command. `CONTEXT_JSON` must be an exact validated
`MemoryContext` object, and `CHANGE_SET_JSON` must contain exact
`field_changes` entries. The command must pass the same immutable
`PRChangeSet` to `pr_report_commit_anchors()` and `pr_memory_report()`, with
`capture_commit_ancestry()` against the explicitly named repository between
those calls. Do not add `--write`, supplied ancestry, implicit Git fetching, or
legacy broad changed fields to this command.

Treat malformed documents and change sets as input errors. Treat missing Git
objects and other ancestry capture failures as state errors; never continue
with an unfiltered report. The canonical output must retain both
`commit_ancestry` and `report`. The command persists nothing and leaves
snapshot version 2, JSON Schemas, active-lessons YAML, packaged resource bytes,
and PostgreSQL schema version 2 unchanged.

When `memory_caused_failure` is true, persisted evidence must include a
non-null `eval_result` of `fail` or `error` and at least one used memory ID.

## Benchmark Example Leakage Policy

The automatic benchmark identity is exactly `(eval_suite, input_hash)`. A
caller opting in must use a stable suite name, canonicalize one benchmark
example deterministically, compute a collision-resistant privacy-preserving
hash, and attach it to the trace for that example. Each trace carries the hash
of its own example, and the current `MemoryContext` must match the current
trace. Source and current traces use the same hash only when they represent the
same canonical example; different examples keep their own hashes. The caller
owns digest selection, encoding, collision risk, canonicalization consistency,
and suite-name consistency; the library performs only exact bounded-string
comparison.

Lessons and failure cases receive ephemeral `source_eval_suite` and
`source_input_hash` from their source trace during candidate construction and
finalization. Source identity is checked before LLM narrowing. Candidate
`source_eval_suite` and `source_input_hash` fields are not serialized into
prompts or snippets. The builders do not render structured `input_hash` fields;
`eval_suite` remains ordinary prompt context and may also appear in memory
scope. A complete exact match blocks in every mode with
`memory originates from current benchmark example`. Static `sensitive` and
`eval_leaking` checks retain precedence and their stable reasons.

Incomplete identities never trigger a guessed match. `eval_suite` alone is a
valid legacy context; `input_hash` requires `eval_suite`; incomplete source
trace identity yields neither ephemeral source field; and a directly supplied
partial `MemoryItem` source pair is invalid. Different hashes within one suite
and equal hashes across different suites do not trigger the automatic rule.

The safe store workflow enforces context/trace binding at finalization before
state changes. The audit log records the current pair, candidate/status
evidence, and the automatic block reason. `input_hash` is identity evidence,
not memory scope, and must not be added to lesson or policy scope. Storage stays
at snapshot version 2 and PostgreSQL schema version 2 with no new persisted
memory fields for benchmark identity: existing trace storage keeps the source hash, existing usage
context JSON/JSONB keeps current identity, and ephemeral source fields are never
serialized.

## Injection format

`recommended_injection` controls the final runtime snippet:

- `none`: inject nothing.
- `pointer_only`: inject only memory ID, source, and scope.
- `short_summary`: inject a quoted rule capped at 500 characters.
- `full_case_summary`: inject up to 2,000 characters of Store-enriched Lesson, reviewed failure/root-cause/fix, commit, regression, and reviewer evidence.

Task text, context summaries, and candidate memory shown to the LLM
applicability gate should also be bounded and quoted as data. Long or
instruction-like dynamic text must not be allowed to merge with the gate
prompt's own rules.
Runtime snippets require the final parsed `MemoryDecision`; callers should not
render non-empty memory snippets directly from retrieved candidates.

## Fixed runtime budgets

The runtime fails closed at these fixed boundaries:

- `MEMORY_ID_MAX_CHARS`: 128 characters for memory and provenance IDs.
- `METADATA_VALUE_MAX_CHARS`: 512 characters for context and scope values.
- `LLM_GATE_MAX_CANDIDATES`: 50 candidates per gate request.
- `allowed_memory_ids` / `blocked_memory_ids`: 50 IDs per LLM decision list.
- `LLM_GATE_PROMPT_MAX_CHARS`: 32,000 characters in the final gate prompt.
- `LLM_GATE_RESPONSE_MAX_BYTES`: 65,536 UTF-8 bytes per decision response.
- `LLM_GATE_RESPONSE_MAX_NODES`: 1,000 nodes per decision response.
- `LLM_GATE_RESPONSE_MAX_DEPTH`: depth 20 per decision response.
- `MEMORY_DECISION_REASON_MAX_CHARS`: 2,000 characters.
- `INJECTION_MAX_MEMORIES`: 20 memories per injection.
- `INJECTION_SNIPPET_MAX_CHARS`: 12,000 characters in the final snippet.
- `COMMIT_ANCESTRY_MAX_ANCHORS`: 1,000 input anchors per capture call.
- `TRACE_JSON_MAX_NODES`: 100,000 aggregate nodes across the three Trace JSON
  fields.
- `TRACE_JSON_MAX_TEXT_BYTES`: 8 MiB of aggregate UTF-8 object-key and string
  text across the three Trace JSON fields.

Metadata and keyword retrieval use Unicode-aware tokenization. Non-ASCII words
also contribute two-character grams, so CJK query substrings can filter longer
candidate text without disabling either gate.

For production deployments, treat declared-scope matching as applicability,
not authorization. A memory that omits `repo` or `tenant` is not implicitly
isolated by that field. Canonical repository identity, explicit scope kind,
durable Gate requests, replay metadata, structured regression evidence, and
required ancestry remain schema v3 / PostgreSQL schema v3 work.

Repair verified-but-unreviewed cases before loading an existing version-2
snapshot. Existing PostgreSQL schema-version-1 installations must apply the
packaged `schemas/postgres-v1-to-v2.sql` migration before synchronization.

Recommended:

```text
Relevant verified memory:

1. [lesson_id: lesson_001]
Scope: planner / search_docs
Rule: When calling search_docs, always provide a non-empty natural-language query.
Source: case_001
```

Forbidden:

```text
raw trace
full prompt history
full user input
tool output with private data
eval expected output
unverified root cause
draft failure case
obsolete lesson
```
