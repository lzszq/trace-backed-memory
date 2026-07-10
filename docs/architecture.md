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
The boundary requires a JSON object, rejects non-finite costs and confidence,
and serializes and parses strict JSON without `NaN` or infinity constants.
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
PostgreSQL rejects empty required identifiers and text, uses composite
`(source_trace_id, commit_sha)` provenance to bind cases to their source trace,
requires non-null lesson and policy confidence, and checks audit JSONB objects
and values. Failure-case, lesson, and project-policy IDs are immutable and their
records cannot be deleted. The shared runtime ID registry rejects direct DML;
only schema-qualified source-table triggers can register IDs, and usage evidence
must resolve both a registry entry and its concrete source row.
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
uses two real sessions to verify lifecycle lock serialization.
Portable JSON Schema files document trace, failure case, lesson, project policy,
usage log, and full snapshot shapes; cross-record provenance checks still live
in the store because they require current store state.

## Layer 5: PR / CI Memory Report

The in-memory MVP can generate a PR-oriented memory report from the same trace
and failure-case stores. It only treats verified, regression-backed failure
cases with trace `repo` matching the current context as reportable historical
failures for the current repo/tenant/tool/failure type/eval suite. Traces
without repo provenance are not eligible for PR reports. The report suggests
targeted regression tests and emits warnings when prompt, tool schema, tool,
model, or eval-suite changes touch known failure areas. It also includes
case-level provenance records with source trace ID, source commit, fix commit,
trace URI, and failure type.

## Non-goals

- Do not build generic personalization memory first.
- Do not inject raw traces directly into prompts.
- Do not treat vector similarity as sufficient proof of relevance.
- Do not allow the LLM to mark memory active without verification.
