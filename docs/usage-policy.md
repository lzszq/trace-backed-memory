# Memory Usage Policy

## Rule

Memory is not default context. Memory is historical experience that must be filtered, scoped, and approved before use.

```text
raw trace -> failure case -> verified lesson -> gated runtime memory
```

## PostgreSQL Persistence Boundary

The optional synchronous PostgreSQL repository persists the same gated store
records; it does not make raw traces eligible for injection or bypass System
Gate and LLM Gate policy. It requires PostgreSQL 12+ because the schema's
hardened JSONB constraints use `jsonb_path_exists`. Install
`trace-backed-memory[postgres]`, apply
the canonical `schemas/postgres.sql` bytes to a fresh `public` schema at version
1, then use `PostgresMemoryRepository` for persistence. A checkout may use that
path directly. An installed package must first run `tbm resource export
schemas/postgres.sql postgres.sql`; the exported bytes are identical.

Synchronization is additive and atomic. A sync retains database records absent
from the submitted store, permits only supported forward lifecycle updates, and
rolls back the entire transaction on an immutable ID conflict. A pending Trace
may complete only from `unknown` to a measured result while preserving its
provenance and existing execution evidence. A usage decision may separately
advance only from `NULL` or `unknown` to a measured outcome pair; every other
usage field remains immutable. All other protected Trace fields also remain
immutable. Loading normalizes persisted values and
reconstructs the regular validated store. A repository created from a caller
connection borrows it; `connect()` owns and closes the connection. Schema
migration, connection pooling, and async access are outside this repository's
current policy and implementation.

When the supplied connection already has an active caller transaction, each
repository operation uses a nested savepoint and does not commit or roll back
the outer transaction; the caller owns the final commit or rollback. Without an
outer transaction, the repository transaction commits normally.

## Packaged Resource Policy

Use `packaged_resources()`, `read_packaged_resource()`, or
`export_packaged_resource()` when canonical Schemas, examples, or memory
support files must be available from an installed distribution. Do not infer a
package filesystem path or fall back to the current checkout. Resource names
must come from the fixed canonical allowlist; unknown names and traversal-like
strings are rejected before package access.

The 18 installed resource copies must remain byte-identical to the top-level
authoring files. Wheel and source-distribution verification must fail on a
missing, extra, or changed copy. `PackagedResource` metadata is derived from
installed bytes and includes SHA-256 and byte size. `load_failure_taxonomy()`
without a path uses the packaged canonical taxonomy; an explicit path remains
caller-owned input and follows the existing parser contract.

CLI resource reads emit deterministic JSON rather than unframed raw content.
Export is the shell integration path. It must refuse an existing destination
unless `--overwrite` is explicit, publish through a same-directory temporary
file, map name errors to exit 2 and write errors to exit 4, and treat a closed
stdout after a successful export as success to prevent unsafe retry.

## Evidence Ingestion Integrity

Treat only explicit structured failure fields as extraction evidence. The
classifier reads `Trace.error`, then top-level `name` and `error` fields from
`tool_calls`, followed by top-level `error` fields from `tool_outputs`. It must
not search successful output names, arbitrary output fields, or nested result
text for keywords: provider results may contain examples, historical errors,
or quoted content that does not describe the current run. Trace errors retain
precedence over tool calls, and tool-call errors retain precedence over
tool-output errors when selecting a root cause. An output `name` may label a
tool-failure symptom only when that output has a non-empty top-level `error`.

Caller-owned failure-taxonomy and active-lessons YAML must use the repository's
constrained shapes. Duplicate taxonomy descriptions, lesson record fields, or
lesson scope keys are invalid; do not rely on last-key-wins replacement. The
lessons adapter must reject a duplicate anywhere in the document before adding
any lesson to the Store. It must also construct and validate every candidate
against staged state before one all-or-nothing commit, so duplicate IDs or later
semantic failures leave existing Store state unchanged. These checks add no
persisted evidence and leave snapshot version 2, JSON Schemas, active-lessons
YAML, and PostgreSQL schema version 1 unchanged.

## Snapshot Operations CLI

Use `tbm` or the equivalent `python -m trace_backed_memory` entry point for
local snapshot operations. The CLI is an operations adapter, not a new policy
or persistence layer: `snapshot validate`, `snapshot stats`, `audit`,
`metrics`, and `remediation` must reuse the store's validation and derived
views. Commands accept one local snapshot only; they do not connect to the
PostgreSQL repository.

Treat every `recover`, `recover-batch`, and `recover-ready` command as a
dry-run unless `--write` is explicit. A dry-run may mutate the reconstructed
store in memory but must leave the source bytes unchanged. A write is permitted
only after the whole recovery succeeds, and it must use `save_json()` to
replace the same snapshot atomically.

`recover-ready` may select only remediation action `recover`; it must continue
to skip pending, conflicting, complete, and `recover_with_attribution` work.
Single recovery passes `memory_caused_failure` only when the operator states it
explicitly. Batch decision IDs must be unique, repeated attribution values must
use exact `DECISION_ID=true|false` syntax, and an invalid item must reject the
whole batch all-or-nothing. Operators must investigate conflicts rather than
using the CLI to choose a historical side.

Automation may consume the single deterministic JSON value written on
success. Failures write one structured JSON error without a traceback. Exit
codes are 0 for success or no-op, 1 for an internal failure, 2 for usage or
snapshot input, 3 for recovery-state or attribution rejection, and 4 for a
write failure. Error text is capped at 2,048 characters. JSON serialization
must finish before persistence. After a requested write commits, a downstream
stdout pipe closure must not falsely report that committed recovery as failed.
Human-readable `--help` output is outside the JSON contract.

CLI reads, audits, metrics, remediation plans, and completion wrappers are not
persisted. Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 1 remain unchanged.

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
known non-empty string fields from `schemas/memory_context.schema.json`.
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
scope values are non-empty strings
repo / branch / tenant allowed
not obsolete
not sensitive
not eval-leaking
has source_case_id, source_trace_id, or source_policy_id
```

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
System Gate still remains authoritative: parsed LLM decisions can only narrow
the system-approved memory set, not reopen blocked memory. If the LLM output
lists the same memory ID as both allowed and blocked, blocked wins and the
memory is not injected.
Low-level callers must also provide disjoint System Gate allowed and blocked
results; `apply_llm_gate_decision()` rejects contradictory inputs before it
constructs a final decision.

## Safe Store Workflow

Use `TraceBackedMemoryStore.prepare_memory()` to retrieve candidates, apply
System Gate, and create the bounded LLM prompt. Pass the decision payload to
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
version 1 remain unchanged.

The usual chronology is decision first and evaluation later. Call
`record_trace()` first with an `unknown` current Trace, call
`finalize_memory()` without an outcome, execute the task with the returned
snippet, then call `complete_memory_run()` with the returned `trace_id` and
`decision_id`. One measured result completes the Trace and seals the decision
atomically. The frozen `MemoryRunCompletion` return value exposes defensive
copies of both records.

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
schema version 1 remain unchanged.

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
schema version 1 remain unchanged.

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
active-lessons YAML, and PostgreSQL schema version 1 unchanged.

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
version 1 unchanged.

Use `memory_run_metrics()` for monitoring and alert thresholds rather than
reimplementing audit aggregation. Its frozen `MemoryRunMetrics` counts one
usage decision at a time and exposes `decision_count`, `pending_count`,
`trace_only_count`, `decision_only_count`, `complete_count`, `conflict_count`,
`recoverable_count`, `auto_recoverable_count`, and
`attribution_required_count`. The sum of the five status counts must equal
`decision_count`. `recoverable_count` is the sum of the one-sided status counts
and also equals `auto_recoverable_count + attribution_required_count`. Treat
pending as awaiting measurement and conflict as manual-review work.

These health metrics are derived and not persisted, and they do not replace
outcome-oriented `metrics()`. Snapshot and PostgreSQL loads reconstruct them
from existing records, leaving snapshot version 2, JSON Schemas, active-lessons
YAML, and PostgreSQL schema version 1 unchanged.

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
Schemas, active-lessons YAML, and PostgreSQL schema version 1 remain unchanged.

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
PostgreSQL schema version 1 remain unchanged.

`complete_trace()` accepts only `pass`, `fail`, or `error` and can fill
`output_hash`, `tool_outputs`, `latency_ms`, `cost_usd`, `error`, and
`trace_uri`. Existing non-empty completion evidence and every other Trace
field remain immutable. Exact replay is idempotent; conflicting, reverse, and
partial post-completion rewrites are rejected atomically.

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
2 and PostgreSQL schema version 1 remain current.

`MemoryRunCompletion` itself is not persisted. Snapshots retain the existing
Trace and usage log, and PostgreSQL synchronization updates their linked
`trace_id` and `decision_id` rows inside one transaction. A usage-row conflict
therefore rolls back an earlier Trace update. Snapshot version 2, JSON Schemas,
active-lessons YAML, and PostgreSQL schema version 1 remain unchanged.

## Git Ancestry Opt-in

Callers that opt in must first discover the complete anchor set with
`candidate_commit_anchors(context)` for runtime retrieval, or
`pr_report_commit_anchors(context)` for a PR report. They must capture each
anchor against the exact `context.commit_sha` with
`capture_commit_ancestry()` outside the store lock, then pass that unchanged
`CommitAncestryEvidence` object to `candidate_memories()`,
`prepare_memory()`, or `pr_memory_report()`.

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
schema version 1 do not change.

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
Repo and tenant remain exact isolation boundaries, and unchanged declared
trace-backed context metadata remains exact-match. Exact value-aware change
sets support only `prompt_version`, `prompt_family`, `tool`,
`tool_schema_version`, `model`, and `eval_suite`. Callers must not claim exact
`model_family` provenance: it is unsupported because traces do not record it.

Existing `changed_fields` reports remain available for legacy broad
field-name-only behavior, including legacy `model_family` warnings. Change
sets and endpoint tags are ephemeral report inputs and outputs, not persisted
records or schema extensions.

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
at snapshot version 2 and PostgreSQL schema version 1 with no new persisted
memory fields: existing trace storage keeps the source hash, existing usage
context JSON/JSONB keeps current identity, and ephemeral source fields are never
serialized.

## Injection format

`recommended_injection` controls the final runtime snippet:

- `none`: inject nothing.
- `pointer_only`: inject only memory ID, source, and scope.
- `short_summary` / `full_case_summary`: inject bounded, quoted memory text after System Gate and LLM Gate approval.

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
- `LLM_GATE_PROMPT_MAX_CHARS`: 32,000 characters in the final gate prompt.
- `INJECTION_MAX_MEMORIES`: 20 memories per injection.
- `INJECTION_SNIPPET_MAX_CHARS`: 12,000 characters in the final snippet.

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
