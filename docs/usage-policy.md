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
`schemas/postgres.sql` to a fresh `public` schema at version 1, then use
`PostgresMemoryRepository` for persistence.

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

The usual chronology is decision first and evaluation later. Call
`record_trace()` first with an `unknown` current Trace, call
`finalize_memory()` without an outcome, execute the task with the returned
snippet, complete the Trace, then call `record_decision_outcome()` with the
returned decision ID.

`complete_trace()` accepts only `pass`, `fail`, or `error` and can fill
`output_hash`, `tool_outputs`, `latency_ms`, `cost_usd`, `error`, and
`trace_uri`. Existing non-empty completion evidence and every other Trace
field remain immutable. Exact replay is idempotent; conflicting, reverse, and
partial post-completion rewrites are rejected atomically.

Trace completion never seals a usage decision automatically, and decision
outcome sealing never changes a Trace. Use the same evaluator result for both
when they describe the same evaluation.

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

Use `record_decision_outcome()` after evaluation and before reading completed
metrics or synchronizing the completed audit. Sealing moves the decision from
the unevaluated bucket to exactly one evaluated denominator; it does not create
or persist a separate metric record.

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
