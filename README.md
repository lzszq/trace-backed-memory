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

- Core models: `Trace`, `FailureCase`, `Lesson`, `ProjectPolicy`, `MemoryUsageLog`, and `MemoryMetrics`.
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
- PR/CI helper that reports related verified, regression-backed historical failures from repo-matched traces, includes source/fix provenance, suggests regressions, and warns on risky prompt/tool/model/eval-suite changes.
- Basic metrics for decisions, candidates, used/blocked memory, pass rates with/without memory, wrong-memory failures, obsolete attempts, and lesson confidence.

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
