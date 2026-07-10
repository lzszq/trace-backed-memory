# README MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the README-described trace-backed memory MVP as a small, dependency-free Python library.

**Architecture:** Keep the current public `MemoryContext`, `MemoryItem`, `system_gate`, and `build_injection_snippet` API intact. Add focused dataclasses for traces, failure cases, lessons, and usage logs; lifecycle helpers that convert trace evidence into verified lessons; deterministic policy helpers for system and LLM gate decisions; and an in-memory store for candidate retrieval and audit logs.

**Tech Stack:** Python 3.11+, dataclasses, pytest, no runtime dependencies.

## Global Constraints

- Preserve the README suggested API import path: `from trace_backed_memory import MemoryContext, MemoryItem, system_gate`.
- Keep raw trace out of runtime prompt construction.
- Require source, scope, status, and usage decision logs for memory usage.
- Use metadata filtering before semantic or LLM gate decisions.
- Follow TDD: write a failing test, verify it fails, implement minimal code, verify it passes.

---

### Task 1: Provenance Models And Lifecycle Helpers

**Files:**
- Modify: `src/trace_backed_memory/models.py`
- Create: `src/trace_backed_memory/lifecycle.py`
- Modify: `src/trace_backed_memory/__init__.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Produces: `Trace`, `FailureCase`, `Lesson`, `MemoryUsageLog`
- Produces: `draft_failure_case(trace, case_id, failure_type, symptom, root_cause=None, fix=None) -> FailureCase`
- Produces: `verify_failure_case(case, fix, fix_commit_sha, regression_passed) -> FailureCase`
- Produces: `lesson_from_failure_case(case, lesson_id, lesson_text, memory_type, scope, confidence=1.0) -> Lesson`
- Produces: `memory_item_from_lesson(lesson) -> MemoryItem`

- [x] **Step 1: Write failing lifecycle tests**

```python
from trace_backed_memory import (
    Trace,
    draft_failure_case,
    lesson_from_failure_case,
    memory_item_from_lesson,
    verify_failure_case,
)


def test_failed_trace_can_become_verified_lesson_memory():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        branch="main",
        tool_schema_version="search_docs_v2",
        model="gpt-5.5-pro",
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        error="Invalid argument: query is required",
    )

    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
        root_cause="prompt omitted the non-empty query contract",
    )
    verified = verify_failure_case(
        draft,
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    lesson = lesson_from_failure_case(
        verified,
        lesson_id="lesson_001",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={"tool": "search_docs", "tool_schema_version": "search_docs_v2"},
        confidence=0.92,
    )

    memory = memory_item_from_lesson(lesson)

    assert draft.status == "draft"
    assert verified.status == "verified"
    assert memory.memory_id == "lesson_001"
    assert memory.status == "active"
    assert memory.source_case_id == "case_001"
```

- [x] **Step 2: Run lifecycle test and verify it fails**

Run: `python -m pytest tests/test_lifecycle.py -v`
Expected: FAIL because `Trace` and lifecycle helpers do not exist yet.

- [x] **Step 3: Implement models and lifecycle helpers**

Add frozen dataclasses matching README/docs fields, then add pure helper functions that preserve provenance links and reject lessons from unverified cases.

- [x] **Step 4: Run lifecycle test and verify it passes**

Run: `python -m pytest tests/test_lifecycle.py -v`
Expected: PASS.

### Task 2: LLM Gate Decision Boundary

**Files:**
- Modify: `src/trace_backed_memory/policy.py`
- Modify: `src/trace_backed_memory/__init__.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Produces: `build_llm_gate_prompt(context, candidates, task, context_summary="") -> str`
- Produces: `apply_llm_gate_decision(system_allowed, system_blocked, decision) -> tuple[list[MemoryItem], MemoryDecision]`

- [x] **Step 1: Write failing policy tests**

```python
def test_llm_gate_cannot_allow_memory_blocked_by_system_gate():
    allowed_memory = MemoryItem(
        memory_id="lesson_allowed",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs"},
        text="Use a non-empty query.",
        source_case_id="case_001",
    )
    blocked_memory = MemoryItem(
        memory_id="lesson_blocked",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs"},
        text="Leaky eval hint.",
        source_case_id="case_002",
    )
    decision = MemoryDecision(
        use_memory=True,
        allowed_memory_ids=["lesson_allowed", "lesson_blocked"],
        blocked_memory_ids=[],
        reason="The lessons look useful.",
        risk="low",
        recommended_injection="short_summary",
    )

    final_allowed, final_decision = apply_llm_gate_decision(
        [allowed_memory],
        {"lesson_blocked": "memory may leak eval data"},
        decision,
    )

    assert [m.memory_id for m in final_allowed] == ["lesson_allowed"]
    assert "lesson_blocked" in final_decision.blocked_memory_ids


def test_llm_gate_prompt_excludes_raw_trace_fields():
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    memory = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs"},
        text="Use a non-empty query.",
        source_case_id="case_001",
    )

    prompt = build_llm_gate_prompt(context, [memory], task="repair failed tool call")

    assert "Candidate memory" in prompt
    assert "Use a non-empty query." in prompt
    assert "tool_calls" not in prompt
    assert "tool_outputs" not in prompt
```

- [x] **Step 2: Run policy tests and verify they fail**

Run: `python -m pytest tests/test_policy.py -v`
Expected: FAIL because LLM gate helpers are not exported.

- [x] **Step 3: Implement minimal prompt and decision validation**

Add prompt construction from approved memory summaries only. Make final allowed memory the intersection of system-approved IDs and LLM-approved IDs; carry all system-blocked IDs into the final decision.

- [x] **Step 4: Run policy tests and verify they pass**

Run: `python -m pytest tests/test_policy.py -v`
Expected: PASS.

### Task 3: In-Memory Store, Retrieval, And Usage Logs

**Files:**
- Create: `src/trace_backed_memory/store.py`
- Modify: `src/trace_backed_memory/__init__.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `TraceBackedMemoryStore`
- Produces: `TraceBackedMemoryStore.record_trace(trace) -> Trace`
- Produces: `TraceBackedMemoryStore.add_failure_case(case) -> FailureCase`
- Produces: `TraceBackedMemoryStore.add_lesson(lesson) -> Lesson`
- Produces: `TraceBackedMemoryStore.candidate_memories(context) -> list[MemoryItem]`
- Produces: `TraceBackedMemoryStore.log_decision(run_id, context, candidate_memory_ids, decision) -> MemoryUsageLog`

- [x] **Step 1: Write failing store tests**

```python
from trace_backed_memory import (
    MemoryContext,
    MemoryDecision,
    TraceBackedMemoryStore,
    Trace,
    draft_failure_case,
    lesson_from_failure_case,
    verify_failure_case,
)


def test_store_retrieves_by_metadata_then_logs_usage_decision():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    )

    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    candidates = store.candidate_memories(context)
    decision = MemoryDecision(
        use_memory=True,
        allowed_memory_ids=["lesson_001"],
        blocked_memory_ids=[],
        reason="directly relevant",
        risk="low",
        recommended_injection="short_summary",
    )

    log = store.log_decision("run_001", context, [m.memory_id for m in candidates], decision)

    assert [m.memory_id for m in candidates] == ["lesson_001"]
    assert log.used_memory_ids == ["lesson_001"]
    assert store.usage_logs == [log]
```

- [x] **Step 2: Run store tests and verify they fail**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL because `TraceBackedMemoryStore` does not exist.

- [x] **Step 3: Implement in-memory store**

Use dictionaries keyed by ID for traces, failure cases, and lessons. Reject duplicate trace IDs. Candidate retrieval should require every declared scope field to match current context metadata, then leave exact policy enforcement to `system_gate`.

- [x] **Step 4: Run store tests and verify they pass**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS.

### Task 4: README API Alignment And Full Verification

**Files:**
- Modify: `README.md`
- Test: `tests/test_readme_api.py`

**Interfaces:**
- Confirms README example imports still work.
- Documents the added MVP API without changing project scope.

- [x] **Step 1: Write README API regression test**

```python
from trace_backed_memory import MemoryContext, MemoryItem, system_gate


def test_readme_suggested_initial_api_still_works():
    context = MemoryContext(
        mode="repair",
        repo="agent-harness",
        branch="main",
        commit_sha="abc123",
        prompt_family="planner",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
        eval_suite="tool_calling_regression",
        failure_type="invalid_tool_argument",
    )
    memory = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs", "prompt_family": "planner"},
        text="When calling search_docs, always provide a non-empty natural-language query.",
        source_case_id="case_001",
    )

    allowed, blocked = system_gate(context, [memory])

    assert [item.memory_id for item in allowed] == ["lesson_001"]
    assert blocked == {}
```

- [x] **Step 2: Run README API test and verify it passes or fails for the expected reason**

Run: `python -m pytest tests/test_readme_api.py -v`
Expected: PASS if the existing API remains intact.

- [x] **Step 3: Update README with implemented MVP API**

Add a compact section showing trace-to-lesson lifecycle, `TraceBackedMemoryStore`, LLM gate decision application, and usage log recording.

- [x] **Step 4: Run full verification**

Run: `python -m pytest`
Expected: all tests PASS.

## Self-Review

- Spec coverage: the tasks cover the README one-liner pipeline, source/scope/status requirements, deterministic system gate, LLM gate boundary, runtime injection safety, and usage logs.
- Placeholder scan: no `TBD`, `TODO`, or unspecified test steps remain.
- Type consistency: exported names are used consistently across tasks and existing public API remains intact.
- Final verification: `python -m pytest` passes and `git diff --check` reports no whitespace errors.
