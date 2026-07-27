# Compatible Secure Store Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a compatible store-level safe memory workflow with validated lifecycle transitions, historically correct audit events, and strict versioned persistence.

**Architecture:** Keep existing low-level models and policy helpers importable, while adding one-use `MemoryGateRequest` and immutable `GatedMemoryResult` boundary objects. Move store state behind copy-isolated collections, bind prepare/finalize to one store, record decision-time evidence, and emit deterministic version 2 snapshots with explicit legacy migration.

**Tech Stack:** Python 3.11+, frozen dataclasses, standard-library JSON/filesystem APIs, pytest, JSON Schema Draft 2020-12 documents, PostgreSQL DDL, no runtime dependencies.

## Global Constraints

- Preserve valid README imports and low-level policy helper names.
- Keep raw traces and System Gate blocked memory out of LLM prompts and runtime snippets.
- A derived lesson must retain any source trace `repo` and `tenant` boundary.
- Safe finalization must be one-use, trace-linked, all-or-nothing, and recheck live memory state.
- Historical metrics must use decision-time evidence, not current memory status.
- New snapshots use exact `snapshot_version: 2`; exact legacy five-key snapshots remain migratable.
- Follow TDD for every behavior change and do not add runtime dependencies.

## Shared Test Fixtures

Add these helpers near the top of `tests/test_store.py` before Task 1 tests.
They use only existing public APIs and are reused verbatim by later tasks:

```python
def store_with_verified_case(
    *, repo: str = "repo", tenant: str | None = "tenant_a"
) -> tuple[TraceBackedMemoryStore, Trace, FailureCase]:
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc",
            repo=repo,
            tenant=tenant,
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="invalid_tool_argument",
            symptom="search_docs received an empty query",
        ),
        fix="require a non-empty query",
        fix_commit_sha="def",
        regression_passed=True,
    )
    store.add_failure_case(case)
    return store, trace, case


def store_with_active_lesson() -> tuple[
    TraceBackedMemoryStore, Trace, FailureCase, Lesson
]:
    store, trace, case = store_with_verified_case()
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
    )
    store.add_lesson(lesson)
    return store, trace, case, lesson


def matching_context(trace: Trace) -> MemoryContext:
    return MemoryContext(
        mode="repair",
        repo=trace.repo or "repo",
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
    )


def allow_decision(memory_id: str) -> dict[str, object]:
    return {
        "use_memory": True,
        "allowed_memory_ids": [memory_id],
        "blocked_memory_ids": [],
        "reason": "direct match",
        "risk": "low",
        "recommended_injection": "short_summary",
    }


def valid_snapshot_dict() -> dict[str, object]:
    store, _trace, _case, _lesson = store_with_active_lesson()
    return store.to_snapshot()


def store_with_records_in_order(trace_ids: list[str]) -> TraceBackedMemoryStore:
    store = TraceBackedMemoryStore()
    for trace_id in trace_ids:
        store.record_trace(
            Trace(
                trace_id=trace_id,
                run_id=f"run_{trace_id}",
                commit_sha=f"commit_{trace_id}",
                repo="repo",
                eval_result="unknown",
            )
        )
    return store
```

Add this equivalent local fixture to `tests/test_readme_api.py` for Task 5 so
that the README contract test does not import another test module:

```python
def readme_store_fixture() -> tuple[
    TraceBackedMemoryStore, Trace, FailureCase, Lesson
]:
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_safe_readme",
            run_id="run_safe_readme",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_a",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_safe_readme",
            failure_type="invalid_tool_argument",
            symptom="search_docs received an empty query",
        ),
        fix="require a non-empty query",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_safe_readme",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={
            "repo": "agent-harness",
            "tenant": "tenant_a",
            "tool": "search_docs",
        },
    )
    store.add_lesson(lesson)
    return store, trace, case, lesson
```

---

### Task 1: Fail-Closed Runtime Contracts And Source Scope

**Files:**
- Modify: `src/trace_backed_memory/models.py`
- Modify: `src/trace_backed_memory/policy.py`
- Modify: `src/trace_backed_memory/store.py`
- Test: `tests/test_policy.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: exact-boolean validation for all safety and lifecycle flags.
- Produces: `MemoryGateRequest` and `GatedMemoryResult` dataclasses for later tasks.
- Produces: source `repo`/`tenant` narrowing in `TraceBackedMemoryStore.add_lesson()`.

- [ ] **Step 1: Write failing policy tests for malformed safety flags and blocked prompt candidates**

```python
def test_system_gate_rejects_non_boolean_safety_flags():
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc", tenant="tenant_a")
    malformed = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"repo": "repo", "tenant": "tenant_a"},
        text="rule",
        source_case_id="case_001",
        sensitive="false",  # type: ignore[arg-type]
    )
    allowed, blocked = system_gate(context, [malformed])
    assert allowed == []
    assert blocked == {"lesson_001": "sensitive must be a boolean"}


def test_llm_gate_prompt_rejects_system_blocked_candidates():
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc", tenant="tenant_a")
    sensitive = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"repo": "repo", "tenant": "tenant_a"},
        text="secret",
        source_case_id="case_001",
        sensitive=True,
    )
    with pytest.raises(ValueError, match="must pass System Gate"):
        build_llm_gate_prompt(context, [sensitive], task="repair")
```

- [ ] **Step 2: Write failing store tests for snapshot boolean bypass and source-scope widening**

```python
def test_snapshot_rejects_string_boolean_safety_fields():
    snapshot = valid_snapshot_dict()
    snapshot["lessons"][0]["sensitive"] = "false"
    with pytest.raises(ValueError, match="sensitive must be a boolean"):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_store_rejects_lesson_that_omits_source_tenant_scope():
    store, _trace, case = store_with_verified_case(repo="repo", tenant="tenant_a")
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="rule",
        memory_type="procedural",
        scope={"repo": "repo"},
    )
    with pytest.raises(ValueError, match="source tenant"):
        store.add_lesson(lesson)
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python -m pytest tests/test_policy.py -k "non_boolean_safety or blocked_candidates" -v`

Expected: FAIL because non-boolean safety values are accepted and prompt construction does not run System Gate.

Run: `python -m pytest tests/test_store.py -k "string_boolean or omits_source_tenant" -v`

Expected: FAIL because snapshot/store validators do not enforce these contracts.

- [ ] **Step 4: Add boundary models and fail-closed validation**

Add these frozen public models, using tuples for all collections exposed by the safe boundary:

```python
@dataclass(frozen=True)
class MemoryGateRequest:
    request_id: str
    context: MemoryContext
    candidate_memory_ids: tuple[str, ...]
    system_allowed_memory_ids: tuple[str, ...]
    system_blocked: tuple[tuple[str, str], ...]
    prompt: str
    _store_token: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class GatedMemoryResult:
    request_id: str
    trace_id: str
    decision_id: str
    use_memory: bool
    allowed_memory_ids: tuple[str, ...]
    blocked_memory_ids: tuple[str, ...]
    reason: str
    risk: Literal["none", "low", "medium", "high"]
    recommended_injection: Literal["none", "short_summary", "full_case_summary", "pointer_only"]
    snippet: str
```

In `_memory_item_contract_error()`, require `type(memory.sensitive) is bool` and `type(memory.eval_leaking) is bool`. At the start of `build_llm_gate_prompt()`, call `system_gate(context, candidates)` and raise `ValueError("LLM gate candidates must pass System Gate: ...")` when any candidate is blocked.

In store record validators, require exact booleans for `Trace.dirty`, `FailureCase.regression_passed`, `Lesson.sensitive`, `Lesson.eval_leaking`, `ProjectPolicy.sensitive`, `ProjectPolicy.eval_leaking`, and `MemoryUsageLog.memory_caused_failure`.

In `add_lesson()`, resolve the source trace and require these fields when present:

```python
for field_name in ("repo", "tenant"):
    source_value = getattr(source_trace, field_name)
    if source_value is not None and lesson.scope.get(field_name) != source_value:
        raise ValueError(
            f"lesson scope must preserve source {field_name}: {source_value}"
        )
```

- [ ] **Step 5: Run focused and full tests for GREEN**

Run: `python -m pytest tests/test_policy.py tests/test_store.py -v`

Expected: PASS after updating existing fixtures to retain declared source `repo` and `tenant` scope.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/trace_backed_memory/models.py src/trace_backed_memory/policy.py src/trace_backed_memory/store.py tests/test_policy.py tests/test_store.py
git commit -m "fix: enforce memory safety provenance contracts"
```

### Task 2: Store-Backed Lifecycle And Copy Isolation

**Files:**
- Modify: `src/trace_backed_memory/lifecycle.py`
- Modify: `src/trace_backed_memory/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `TraceBackedMemoryStore.review_failure_case(case_id, **review) -> FailureCase`.
- Produces: `TraceBackedMemoryStore.verify_failure_case(case_id, *, fix, fix_commit_sha, regression_passed) -> FailureCase`.
- Produces: `obsolete_failure_case`, `obsolete_lesson`, and `obsolete_project_policy` store methods.
- Produces: copy-isolated public collection properties preserving read/equality compatibility.

- [ ] **Step 1: Write failing persisted lifecycle tests**

```python
def test_store_can_review_and_verify_a_persisted_draft():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(
        trace_id="trace_001", run_id="run_001", commit_sha="abc",
        repo="repo", tenant="tenant_a", eval_result="fail",
    ))
    store.add_failure_case(draft_failure_case(
        trace, case_id="case_001", failure_type="bad_tool", symptom="bad call"
    ))

    reviewed = store.review_failure_case(
        "case_001", reviewed_by="reviewer", root_cause="missing contract"
    )
    verified = store.verify_failure_case(
        "case_001", fix="add contract", fix_commit_sha="def", regression_passed=True
    )

    assert reviewed.reviewed_by == "reviewer"
    assert verified.status == "verified"
    assert store.failure_cases["case_001"] == verified


def test_obsoleting_source_case_cascades_to_active_lessons():
    store, _trace, case, lesson = store_with_active_lesson()
    obsolete = store.obsolete_failure_case(case.case_id)
    assert obsolete.status == "obsolete"
    assert store.lessons[lesson.lesson_id].status == "obsolete"
```

- [ ] **Step 2: Write failing copy-isolation tests**

```python
def test_recorded_trace_is_isolated_from_caller_nested_mutation():
    calls = [{"name": "search_docs", "arguments": {"query": "before"}}]
    trace = Trace(
        trace_id="trace_001", run_id="run_001", commit_sha="abc",
        repo="repo", eval_result="fail", tool_calls=calls,
    )
    store = TraceBackedMemoryStore()
    store.record_trace(trace)
    calls[0]["arguments"]["query"] = "after"
    assert store.traces["trace_001"].tool_calls[0]["arguments"]["query"] == "before"


def test_mutating_public_collection_copy_does_not_change_store():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(
        trace_id="trace_001", run_id="run_001", commit_sha="abc", eval_result="fail"
    ))
    public = dict(store.traces)
    public["trace_001"].tool_calls.append({"name": "mutated"})
    assert store.traces["trace_001"].tool_calls == []
```

- [ ] **Step 3: Run lifecycle/isolation tests and verify RED**

Run: `python -m pytest tests/test_store.py -k "persisted_draft or cascades or isolated_from_caller or public_collection_copy" -v`

Expected: FAIL because store transitions and isolated collections do not exist.

- [ ] **Step 4: Implement private collections, deep-copy boundaries, and transitions**

Rename mutable internal collections to `_traces`, `_failure_cases`, `_lessons`, `_project_policies`, and `_usage_logs`. Return deep-copied mappings/lists from compatible properties:

```python
@property
def traces(self) -> Mapping[str, Trace]:
    return MappingProxyType(deepcopy(self._traces))

@property
def usage_logs(self) -> list[MemoryUsageLog]:
    return deepcopy(self._usage_logs)
```

Deep-copy each accepted record before insertion and return a deep copy. Store lifecycle methods call the pure lifecycle helper, validate the proposed complete state, then replace the private record. Implement project-policy obsoletion with `dataclasses.replace(policy, status="obsolete")`.

For case obsoletion, build all changed case/lesson records first, validate them, then replace the case and dependent lessons together. Return the copied obsolete case. A second obsoletion returns the already obsolete copy without changing state.

- [ ] **Step 5: Run store tests for GREEN**

Run: `python -m pytest tests/test_store.py -v`

Expected: PASS with existing read-only collection assertions unchanged.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/trace_backed_memory/lifecycle.py src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: add validated store lifecycle transitions"
```

### Task 3: Two-Phase Safe Gate And Historical Audit Events

**Files:**
- Modify: `src/trace_backed_memory/models.py`
- Modify: `src/trace_backed_memory/store.py`
- Modify: `src/trace_backed_memory/__init__.py`
- Test: `tests/test_store.py`
- Test: `tests/test_readme_api.py`

**Interfaces:**
- Produces: `prepare_memory(context, *, task, query=None, context_summary="") -> MemoryGateRequest`.
- Produces: `finalize_memory(request, decision_payload, *, trace_id, eval_result=None, memory_caused_failure=False) -> GatedMemoryResult`.
- Extends: `MemoryUsageLog` with `trace_id`, `context`, `candidate_memory_statuses`, `system_blocked_reasons`, and generated `created_at`.

- [ ] **Step 1: Write failing end-to-end safe workflow test**

```python
def test_prepare_and_finalize_memory_is_trace_linked_and_audited():
    store, trace, _case, lesson = store_with_active_lesson()
    context = MemoryContext(
        mode="repair", repo=trace.repo, tenant=trace.tenant,
        commit_sha=trace.commit_sha, tool="search_docs",
    )
    request = store.prepare_memory(context, task="repair failed call")
    result = store.finalize_memory(
        request,
        {
            "use_memory": True,
            "allowed_memory_ids": [lesson.lesson_id],
            "blocked_memory_ids": [],
            "reason": "direct match",
            "risk": "low",
            "recommended_injection": "short_summary",
        },
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert result.allowed_memory_ids == (lesson.lesson_id,)
    assert "Relevant verified memory" in result.snippet
    log = store.usage_logs[-1]
    assert log.trace_id == trace.trace_id
    assert log.context["tenant"] == trace.tenant
    assert log.candidate_memory_statuses == {lesson.lesson_id: "active"}
    assert log.created_at.endswith("Z")
```

- [ ] **Step 2: Write failing ownership, replay, stale-state, and atomicity tests**

```python
def test_gate_request_cannot_cross_stores_or_be_replayed():
    first, trace, _case, lesson = store_with_active_lesson()
    second = TraceBackedMemoryStore.from_snapshot(first.to_snapshot())
    context = matching_context(trace)
    request = first.prepare_memory(context, task="repair")
    payload = allow_decision(lesson.lesson_id)
    with pytest.raises(ValueError, match="does not belong"):
        second.finalize_memory(request, payload, trace_id=trace.trace_id)
    first.finalize_memory(request, payload, trace_id=trace.trace_id)
    with pytest.raises(ValueError, match="already finalized"):
        first.finalize_memory(request, payload, trace_id=trace.trace_id)


def test_finalize_rechecks_memory_obsoleted_after_prepare():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    store.obsolete_lesson(lesson.lesson_id)
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id
    )
    assert result.use_memory is False
    assert result.snippet == ""
    assert lesson.lesson_id in result.blocked_memory_ids


def test_failed_finalize_does_not_consume_request_or_append_log():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    before = store.usage_logs
    with pytest.raises(ValueError, match="unknown trace_id"):
        store.finalize_memory(request, allow_decision(lesson.lesson_id), trace_id="wrong")
    assert store.usage_logs == before
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id
    )
    assert result.use_memory is True
```

- [ ] **Step 3: Write failing historical-metric test**

```python
def test_obsolete_attempt_metric_does_not_reclassify_old_decisions():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id
    )
    assert store.metrics().obsolete_memory_usage_attempts == 0
    store.obsolete_lesson(lesson.lesson_id)
    assert store.metrics().obsolete_memory_usage_attempts == 0
```

- [ ] **Step 4: Run safe-workflow tests and verify RED**

Run: `python -m pytest tests/test_store.py -k "prepare_and_finalize or gate_request or rechecks_memory or failed_finalize or reclassify" -v`

Expected: FAIL because the new workflow and audit fields do not exist.

- [ ] **Step 5: Implement pending requests, finalization, and decision-time logs**

Maintain `_pending_gate_requests: dict[str, MemoryGateRequest]`, `_finalized_gate_request_ids: set[str]`, one `_store_token`, and a monotonic request ID generator.

Preparation stores a deep copy of the request and returns the immutable request. Finalization verifies object equality and token identity, validates the trace against context `repo`, `commit_sha`, and `tenant`, rehydrates the original candidate IDs, reruns System Gate, parses the decision payload, applies it, renders a snippet, and appends exactly one log before marking the request finalized.

Extend the usage log with defaults for legacy construction:

```python
trace_id: str | None = None
context: dict[str, str] = field(default_factory=dict)
candidate_memory_statuses: dict[str, Status] = field(default_factory=dict)
system_blocked_reasons: dict[str, str] = field(default_factory=dict)
```

Generate `created_at` with the existing UTC `Z` timestamp convention. Change metrics to count only statuses equal to `"obsolete"` in `log.candidate_memory_statuses`.

Harden low-level `log_decision()` to resolve one stored trace by `run_id`, validate trace/context identity, and derive context/status evidence rather than trusting caller-supplied metadata.

- [ ] **Step 6: Run safe workflow plus README tests for GREEN**

Run: `python -m pytest tests/test_store.py tests/test_readme_api.py -v`

Expected: PASS after README API tests use the safe workflow and low-level logging fixtures record matching traces.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/trace_backed_memory/models.py src/trace_backed_memory/store.py src/trace_backed_memory/__init__.py tests/test_store.py tests/test_readme_api.py
git commit -m "feat: add trace-linked safe memory workflow"
```

### Task 4: Strict Deterministic Snapshot Version 2

**Files:**
- Modify: `src/trace_backed_memory/store.py`
- Modify: `schemas/memory_store_snapshot.schema.json`
- Test: `tests/test_store.py`
- Test: `tests/test_examples_and_schema.py`

**Interfaces:**
- Produces: exact version 2 snapshots from `to_snapshot()`.
- Consumes: exact v2 or exact legacy v1 envelopes in `from_snapshot()`.
- Produces: deterministic ordering and atomic `save_json()`.

- [ ] **Step 1: Write failing envelope and migration tests**

```python
def test_snapshot_v2_has_exact_versioned_envelope():
    snapshot = TraceBackedMemoryStore().to_snapshot()
    assert snapshot == {
        "snapshot_version": 2,
        "traces": [],
        "failure_cases": [],
        "lessons": [],
        "project_policies": [],
        "usage_logs": [],
    }


@pytest.mark.parametrize("payload", [{}, {"unknown": []}, {"snapshot_version": 2}])
def test_snapshot_rejects_truncated_or_unknown_envelopes(payload):
    with pytest.raises(ValueError, match="snapshot envelope"):
        TraceBackedMemoryStore.from_snapshot(payload)


def test_exact_legacy_snapshot_is_migrated():
    legacy = {
        "traces": [], "failure_cases": [], "lessons": [],
        "project_policies": [], "usage_logs": [],
    }
    assert TraceBackedMemoryStore.from_snapshot(legacy).to_snapshot()["snapshot_version"] == 2
```

- [ ] **Step 2: Write failing deterministic and atomic save tests**

```python
def test_equivalent_stores_emit_identical_snapshot_json(tmp_path):
    first = store_with_records_in_order(["trace_b", "trace_a"])
    second = store_with_records_in_order(["trace_a", "trace_b"])
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first.save_json(first_path)
    second.save_json(second_path)
    assert first_path.read_bytes() == second_path.read_bytes()


def test_save_json_uses_sibling_replace(monkeypatch, tmp_path):
    calls = []
    real_replace = os.replace

    def recording_replace(source, target):
        calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", recording_replace)
    target = tmp_path / "snapshot.json"
    TraceBackedMemoryStore().save_json(target)
    assert calls[0][1] == target
    assert calls[0][0].parent == target.parent
```

- [ ] **Step 3: Run snapshot tests and verify RED**

Run: `python -m pytest tests/test_store.py tests/test_examples_and_schema.py -k "snapshot_v2 or snapshot_rejects or legacy_snapshot or identical_snapshot or sibling_replace" -v`

Expected: FAIL because snapshots are unversioned, permissive, insertion-ordered, and written directly.

- [ ] **Step 4: Implement strict v2, legacy migration, sorting, and atomic writes**

Validate exact envelope key sets before reading collections. Version 2 requires `snapshot_version == 2`; legacy requires exactly the five collection keys. Reject all other shapes.

Sort every collection by its identity field and usage logs by `decision_id`. Serialize with `sort_keys=True`, two-space indentation, and a trailing newline.

Implement atomic save using `tempfile.NamedTemporaryFile(delete=False, dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")`, flush/close it, then call `os.replace(temp_path, target)`. On error, unlink only `temp_path` and re-raise.

Update the snapshot schema to require all six keys, define `snapshot_version` as integer constant `2`, and retain `additionalProperties: false`.

- [ ] **Step 5: Run snapshot and full tests for GREEN**

Run: `python -m pytest tests/test_store.py tests/test_examples_and_schema.py -v`

Expected: PASS after existing snapshot assertions expect version 2 output.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/trace_backed_memory/store.py schemas/memory_store_snapshot.schema.json tests/test_store.py tests/test_examples_and_schema.py
git commit -m "feat: add strict versioned memory snapshots"
```

### Task 5: Schema, PostgreSQL, README, And Completion Audit

**Files:**
- Modify: `schemas/memory_usage_log.schema.json`
- Modify: `schemas/postgres.sql`
- Modify: `examples/memory_usage_log.example.json`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/usage-policy.md`
- Modify: `docs/product-program.md`
- Modify: `tests/test_examples_and_schema.py`
- Modify: `tests/test_readme_api.py`

**Interfaces:**
- Documents and serializes the Task 3 audit fields.
- Enforces wrong-memory result evidence and case/trace commit parity in SQL.
- Makes the safe two-phase workflow the primary README path.

- [ ] **Step 1: Write failing schema and SQL parity tests**

```python
def test_usage_log_schema_requires_safe_workflow_audit_fields():
    schema = _json_schema("memory_usage_log.schema.json")
    required = set(schema["required"])
    assert {"trace_id", "context", "candidate_memory_statuses", "system_blocked_reasons"} <= required
    caused_failure_then = schema["allOf"][2]["then"]
    assert "eval_result" in caused_failure_then["required"]


def test_postgres_enforces_case_trace_commit_and_wrong_memory_evidence():
    sql = _postgres_schema()
    assert "UNIQUE (trace_id, commit_sha)" in sql
    assert "FOREIGN KEY (source_trace_id, commit_sha)" in sql
    assert "eval_result IS NOT NULL" in sql
    assert "candidate_memory_statuses JSONB NOT NULL" in sql
```

- [ ] **Step 2: Write failing README safe workflow test**

```python
def test_readme_safe_workflow_example_stays_executable():
    store, trace, _case, lesson = readme_store_fixture()
    context = MemoryContext(
        mode="repair", repo=trace.repo, tenant=trace.tenant,
        commit_sha=trace.commit_sha, tool="search_docs",
    )
    request = store.prepare_memory(context, task="repair failed tool call")
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id,
    )
    assert result.use_memory
    assert result.decision_id == store.usage_logs[-1].decision_id
```

- [ ] **Step 3: Run documentation-contract tests and verify RED**

Run: `python -m pytest tests/test_examples_and_schema.py tests/test_readme_api.py -k "audit_fields or case_trace_commit or safe_workflow" -v`

Expected: FAIL until schemas, SQL, examples, and public docs expose the new contract.

- [ ] **Step 4: Update JSON Schema, SQL, examples, and documentation**

In `memory_usage_log.schema.json`, require and define:

```json
"trace_id": {"type": "string", "minLength": 1},
"context": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
"candidate_memory_statuses": {
  "type": "object",
  "additionalProperties": {"enum": ["draft", "verified", "active", "obsolete"]}
},
"system_blocked_reasons": {
  "type": "object",
  "additionalProperties": {"type": "string", "minLength": 1}
}
```

When `memory_caused_failure` is true, add `"required": ["eval_result"]` to the conditional `then`.

In PostgreSQL:

- add `UNIQUE (trace_id, commit_sha)` to traces;
- add `FOREIGN KEY (source_trace_id, commit_sha) REFERENCES traces(trace_id, commit_sha)` to failure cases;
- add non-empty `btrim(...) <> ''` checks for required IDs/text and verified fix fields;
- make lesson/policy confidence `NOT NULL`;
- add the four audit columns with JSONB object/value checks;
- require `eval_result IS NOT NULL AND eval_result IN ('fail', 'error')` when `memory_caused_failure` is true.

Update the example usage log and docs with the exact two-phase safe workflow. State explicitly that low-level helpers remain for callers that own equivalent orchestration, while only the store workflow provides ownership, replay, stale-state, trace-link, and atomic logging guarantees.

- [ ] **Step 5: Run full verification**

Run: `python -m pytest`

Expected: all tests PASS.

Run: `git diff --check`

Expected: exit code 0 with no whitespace errors.

Run: `python -m pip install -e . --dry-run`

Expected: exit code 0 and the local package is accepted as installable.

- [ ] **Step 6: Re-read the design and audit every requirement**

Check `docs/superpowers/specs/2026-07-10-compatible-secure-store-boundary-design.md` against current code, tests, schemas, SQL, examples, and docs. Confirm each in-scope bullet has direct implementation plus test evidence, and report any intentionally deferred non-goal without presenting it as completed.

- [ ] **Step 7: Commit Task 5**

```bash
git add README.md docs/architecture.md docs/usage-policy.md docs/product-program.md examples/memory_usage_log.example.json schemas/memory_usage_log.schema.json schemas/postgres.sql tests/test_examples_and_schema.py tests/test_readme_api.py
git commit -m "docs: publish secure store workflow contract"
```

## Self-Review

- Spec coverage: Tasks 1-5 cover safe gate ordering, source-scope narrowing, persisted lifecycle transitions, copy isolation, historical audit evidence and metrics, snapshot v2/migration/atomicity, runtime validation, schema/SQL parity, and README usage.
- Scope: Git ancestry, PR old/new matching, database adapters, vector retrieval, and automatic benchmark identity remain separate projects exactly as stated in the design.
- Type consistency: `MemoryGateRequest`, `GatedMemoryResult`, `prepare_memory()`, `finalize_memory()`, and all audit-field names are identical across tasks.
- Placeholder scan: every test, implementation step, command, and expected result is concrete.
