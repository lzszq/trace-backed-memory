# Memory Usage Policy

## Rule

Memory is not default context. Memory is historical experience that must be filtered, scoped, and approved before use.

```text
raw trace -> failure case -> verified lesson -> gated runtime memory
```

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

## Safe Store Workflow

Use `TraceBackedMemoryStore.prepare_memory()` to retrieve candidates, apply
System Gate, and create the bounded LLM prompt. Pass the decision payload to
`finalize_memory()` with the trace ID; it rechecks stale state, applies the
LLM decision as a narrowing operation, renders the snippet, and atomically
persists trace ID, context, candidate statuses, and System Gate block reasons.
Only this workflow provides ownership, replay, stale-state, trace-link, and
atomic logging guarantees. Low-level helpers remain available for callers that
own equivalent orchestration.

When `memory_caused_failure` is true, persisted evidence must include a
non-null `eval_result` of `fail` or `error` and at least one used memory ID.

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
