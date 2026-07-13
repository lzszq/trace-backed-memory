# Trace-backed Memory

A provenance-backed memory layer for LLM / agent harness engineering.

## One-liner

Trace-backed Memory turns immutable agent traces, eval results, and git commits into verified, scoped, auditable memory that can be used selectively during debug, repair, regression analysis, planning, and production runtime.

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

## MVP architecture

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

For one-off local commands, setting `PYTHONPATH=src` also works.

## PostgreSQL Repository

PostgreSQL support is optional. Core installs do not import or require
`psycopg`; install the extra when using the synchronous repository:

```powershell
python -m pip install -e ".[postgres]"
pip install 'trace-backed-memory[postgres]'
```

The adapter requires PostgreSQL 12+ because `schemas/postgres.sql` uses
`jsonb_path_exists` in its hardened JSONB constraints.

Before connecting, install `schemas/postgres.sql` into a fresh `public` schema.
The adapter requires the schema metadata row at `schema_version` 1. The SQL file
is a fresh-install schema, not a migration for an existing database.

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
updates, compares records in canonical form, and rejects immutable ID conflicts.
Any conflict rolls back the whole synchronization. `repository.load()` returns
a normalized, validated `TraceBackedMemoryStore`, not a snapshot object.

## Safe Store Workflow

Use the store's two-phase workflow for runtime memory. `prepare_memory()`
retrieves candidates, applies System Gate, and creates the bounded LLM gate
prompt. After the LLM returns a decision payload, `finalize_memory()` rechecks
state, renders the allowed snippet, and records one trace-linked audit event.

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
    eval_result="pass",
)
snippet = result.snippet
```

Only this store workflow provides ownership, replay, stale-state, trace-link,
and atomic logging guarantees.

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
        draft_failure_case(
            source_trace,
            case_id="case_benchmark_source",
            failure_type="invalid_tool_argument",
            symptom="search_docs received an empty query",
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
Compatibility remains snapshot version 2 and PostgreSQL schema version 1 with
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
System Gate and LLM Gate remain
authoritative, and scores are not persisted in snapshots or PostgreSQL.

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

Evidence is request-time input only: it is neither stored in snapshots nor
persisted to PostgreSQL, and it does not replace either gate. PR callers use
`pr_report_commit_anchors(context)`, capture against the same context commit,
and pass that same evidence object to `pr_memory_report()`.

## Endpoint-aware PR reports

Use an immutable `PRChangeSet` when a PR changes trace-backed metadata values.
Each tuple is `(field_name, old_value, new_value)` and supports only
`prompt_version`, `prompt_family`, `tool`, `tool_schema_version`, `model`, and
`eval_suite`. `new_value` must exactly equal the post-change `MemoryContext`
value, including `None`. Repo and tenant remain hard exact-match isolation
boundaries, and unchanged declared context metadata continues to match exactly.

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
`PRChangeSet` values and endpoint provenance are ephemeral report-only values:
they are not persisted and do not change snapshot version 2, JSON schemas,
active-lessons YAML, or PostgreSQL schema version 1.

```python
# PR_CHANGE_SET_WORKFLOW_START
from trace_backed_memory import (
    MemoryContext,
    PRChangeSet,
    Trace,
    TraceBackedMemoryStore,
    capture_commit_ancestry,
    draft_failure_case,
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
            draft_failure_case(
                trace,
                case_id=case_id,
                failure_type="invalid_tool_argument",
                symptom="search_docs rejected an empty query",
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

## Implemented MVP API

The package now implements the README pipeline as dependency-free Python objects and helpers:

```python
from trace_backed_memory import (
    MemoryContext,
    MemoryDecision,
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
    lesson_from_failure_case,
    load_failure_taxonomy,
    memory_item_from_failure_case,
    memory_item_from_lesson,
    memory_item_from_project_policy,
    obsolete_failure_case,
    obsolete_lesson,
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
taxonomy = load_failure_taxonomy("memory/failure_taxonomy.yaml")

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
    trace_id=trace.trace_id,
    eval_result="pass",
)
snippet = result.snippet
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
store.save_lessons_yaml("lessons.active.yaml")
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

These are decision counts, not per-memory causal attribution. A decision is
classified as with-memory when its usage log has non-empty `used_memory_ids`.
Metrics remain derived and are not persisted; snapshot version 2, active-lessons
YAML, JSON Schemas, and PostgreSQL schema version 1 are unchanged.

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
version 1 stay unchanged.

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
lesson_memory = memory_item_from_lesson(lesson)
case_memory = memory_item_from_failure_case(verified, trace)
policy_memory = memory_item_from_project_policy(
    ProjectPolicy(
        policy_id="project_policy_001",
        policy_text="Planner responses must include a tool-call rationale.",
        scope={"prompt_family": "planner"},
    )
)
gate_prompt = build_llm_gate_prompt(context, [lesson_memory], task="repair failed tool call")
old_case = obsolete_failure_case(verified)
old_lesson = obsolete_lesson(lesson)
```

Implemented pieces:

- Core models: `Trace`, `FailureCase`, `Lesson`, `ProjectPolicy`, `MemoryUsageLog`, `MemoryMetrics`, and `MemoryOutcomeMetrics`.
- Git metadata capture for repo name, commit SHA, branch, and dirty state, with command failure errors wrapped for harness diagnostics.
- Git ancestry capture produces immutable, current-commit-bound relations for caller-discovered local commit anchors.
- Trace provenance fields for repo, prompt version, prompt family, tool schema version, model, and eval suite.
- Store-level checks that validate both the incoming and copied trace, preserve copy isolation, reject concurrent copy mutation, and reject empty identity fields, unsupported eval results, or malformed nested JSON trace collections, including non-string object keys, non-finite numbers, reference cycles, and excessive nesting.
- Lifecycle helpers: failed trace -> validated draft failure case -> verified case -> validated active lesson -> `MemoryItem`.
- Failure extraction helpers that load the failure taxonomy, classify failed traces against it with ordered conservative heuristics, and draft failure cases.
- Manual review helper that records reviewer, root cause, notes, and review timestamp on draft failure cases.
- Verification loop hardening: only draft cases can be verified, and verified cases require a fix commit and passing regression evidence.
- Obsolete transitions for failure cases and lessons.
- Store-level checks that reject failure cases with empty identity fields, invalid status, missing verified evidence, missing source trace, or source commit mismatch.
- Project policy helper that turns manually maintained prompt/tool/eval policy into sourced `MemoryItem` policy memory.
- Deterministic System Gate with strict source, tenant-aware scope, status, memory-type, confidence, sensitivity, eval-leak, and mode checks.
- Gate boundary helpers that validate runtime context JSON and direct-call container/record types before use, require non-empty string tasks and string-or-`None` queries, JSON-quote and cap dynamic gate prompt fields, validate LLM decision JSON with non-empty unique IDs and consistent `use_memory` / `recommended_injection` fields, reject contradictory System Gate allowed/blocked inputs, require the final `MemoryDecision` before rendering non-empty runtime snippets, honor `none`/`pointer_only`/`short_summary` injection modes, and prevent the LLM decision from overriding System Gate.
- In-memory MVP store for trace/case/lesson/project-policy records, metadata-first candidate retrieval that requires all declared scope fields to match, optional opt-in Git ancestry filtering before keyword or semantic ranking, debug/repair visibility for verified regression-backed failure cases, optional keyword filtering including short domain tokens, optional bounded caller-provided semantic scores ranked score-descending with memory-ID-ascending ties, and usage decision logs; retrieval cannot bypass System Gate or LLM Gate.
- Usage-log validation and persisted contract that require trace ID, serialized context, candidate status snapshots, and System Gate block reasons; reject empty identities, duplicate imported decision IDs, invalid mode/risk/injection fields, duplicate, empty-string, or non-string memory ID lists, unsupported eval results, unknown runtime memory IDs, and used or blocked memory IDs outside the candidate set.
- Dependency-free strict JSON snapshot save/load for trace, failure case, lesson, project policy, and usage-log records; non-object snapshots, non-finite floats, over-limit integers, and non-standard JSON numeric constants are rejected while JSON-serializable integer costs remain valid.
- Dependency-free active lesson YAML save/load for the repository's simple `memory/lessons.example.yaml` shape, preserving numeric-looking scope strings.
- Store-level checks that reject lessons with empty identity fields, invalid memory type/status, unknown non-empty scope fields, unbounded confidence, or a missing, unverified, non-regression-backed source case.
- Store-level checks that reject project policies with empty identity/text fields, invalid status, invalid scope, unbounded confidence, or IDs that collide with failure case, lesson, or project policy memory IDs.
- JSON schemas for stored records and full memory-store snapshots.
- Postgres schema parity checks for model defaults, an atomic fresh-install transaction pinned to `public`, invariant functions pinned to `pg_catalog`, a trigger-owned shared runtime memory ID registry that rejects direct DML, `TRUNCATE`, helper-shadow bypasses, and ghost usage, non-empty required text, composite case/trace commit provenance, forward-only status updates, `FOR SHARE` parent/lesson lifecycle serialization and cascades, JSONB object/array and element-type checks, required usage-decision audit evidence, and context example parsing.
- Lesson safety flags for sensitive or eval-leaking memory are preserved through retrieval and blocked by System Gate.
- PR reports can reuse current-commit-bound ancestry evidence to exclude unrelated historical failure cases before generating report content.
- PR/CI helper that reports related verified, regression-backed historical failures from repo-matched traces, includes source/fix provenance, suggests regressions, warns on risky prompt/tool/model/eval-suite changes, and supports immutable complete-endpoint `PRChangeSet` matching with old/new/both provenance.
- Outcome-aware metrics for decisions, candidates, used/blocked memory, measured pass rates with explicit denominators, unevaluated decisions, wrong-memory failures, obsolete attempts, and lesson confidence.

## Repository layout

```text
.
|-- docs/
|   |-- architecture.md
|   |-- usage-policy.md
|   `-- mvp-roadmap.md
|-- examples/
|   |-- trace.example.json
|   |-- failure_case.example.json
|   |-- lesson.example.json
|   |-- memory_context.example.json
|   |-- project_policy.example.json
|   `-- memory_decision.example.json
|-- memory/
|   |-- lessons.example.yaml
|   `-- failure_taxonomy.yaml
|-- schemas/
|   |-- postgres.sql
|   |-- trace.schema.json
|   |-- failure_case.schema.json
|   |-- lesson.schema.json
|   |-- project_policy.schema.json
|   |-- memory_usage_log.schema.json
|   |-- memory_store_snapshot.schema.json
|   |-- memory_context.schema.json
|   `-- memory_decision.schema.json
|-- src/trace_backed_memory/
|   |-- __init__.py
|   |-- capture.py
|   |-- extraction.py
|   |-- lifecycle.py
|   |-- models.py
|   |-- policy.py
|   `-- store.py
`-- tests/
    |-- test_capture.py
    |-- test_examples_and_schema.py
    |-- test_extraction.py
    |-- test_lifecycle.py
    |-- test_postgres_integration.py
    |-- test_policy.py
    |-- test_readme_api.py
    `-- test_store.py
```
