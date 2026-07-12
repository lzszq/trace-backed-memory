# Semantic Score Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, caller-scored semantic candidate retrieval without weakening metadata scope, System Gate, LLM Gate, or audit guarantees.

**Architecture:** Callers provide a precomputed `memory_id -> score` mapping to the existing store APIs. The store validates and copies the mapping, builds the existing metadata-eligible candidate set, deterministically selects score-descending top-k records, and then uses the unchanged two-phase safe workflow. No scorer callback, vector storage, persisted score, or schema migration is introduced.

**Tech Stack:** Python 3.11+, standard library only, pytest, existing trace-backed-memory models and policy helpers.

## Global Constraints

- Keep `[project].dependencies = []`; add no core or optional package.
- Existing calls that omit semantic options must retain exact metadata-only or keyword behavior and memory-ID ordering.
- `query` and `semantic_scores` are mutually exclusive whenever both are non-`None`, even when `query` is blank or punctuation-only.
- Semantic mode requires an explicit exact-integer `max_candidates` from 1 through `LLM_GATE_MAX_CANDIDATES` (50).
- Scores and `minimum_score` accept only exact finite `int` or `float` values; reject booleans and every non-finite value.
- Callers must orient scores so larger values mean greater relevance; raw distances must be transformed or negated before use.
- Metadata filtering always precedes semantic ranking; unscored or below-threshold eligible records are excluded.
- Rank by score descending and then `memory_id` ascending; similarity never bypasses System Gate or LLM Gate.
- Do not change dataclasses, snapshot version 2, JSON Schemas, PostgreSQL DDL, repository synchronization, or active-lessons YAML.
- Use `apply_patch` for manual edits and do not revert unrelated changes.

---

## File Structure

- `src/trace_backed_memory/store.py`: validate semantic retrieval options, rank metadata-eligible candidates, and thread options through `prepare_memory()`.
- `tests/test_store.py`: cover semantic selection, validation, safe-workflow integration, stale state, and audit evidence.
- `tests/test_readme_api.py`: keep the public semantic workflow example executable.
- `README.md`: document caller responsibilities and the public API example.
- `docs/architecture.md`: describe the external-score boundary and gate ordering.
- `docs/usage-policy.md`: state semantic retrieval safety requirements.
- `docs/mvp-roadmap.md`: record completion of optional semantic/vector-assisted retrieval.
- `docs/superpowers/specs/2026-07-12-semantic-score-retrieval-design.md`: authoritative design; do not rewrite during implementation unless a reviewed contradiction is found.

No other source, test, schema, packaging, or persistence file should change.

---

### Task 1: Semantic Candidate Selection And Validation

**Files:**
- Modify: `tests/test_store.py` near the existing `candidate_memories` tests
- Modify: `src/trace_backed_memory/store.py` in `candidate_memories()` and near `_tokens()`

**Interfaces:**
- Consumes: existing `MemoryContext`, `MemoryItem`, `MEMORY_ID_MAX_CHARS`, `LLM_GATE_MAX_CANDIDATES`, `is_finite_number()`, and metadata candidate construction.
- Produces: `candidate_memories(context, *, query=None, semantic_scores=None, max_candidates=None, minimum_score=None) -> list[MemoryItem]`.
- Produces: private `_validated_semantic_scores(...) -> dict[str, int | float] | None` used only during a synchronized store call.

- [ ] **Step 1: Add failing semantic ranking tests**

Add these tests beside the existing keyword retrieval tests in `tests/test_store.py`:

```python
def test_candidate_memories_ranks_semantic_scores_after_metadata_filter():
    store = store_with_retrieval_records_in_order(["c", "a", "b"])
    store.add_project_policy(
        ProjectPolicy(
            policy_id="wrong_scope",
            policy_text="This record must never enter the current scope.",
            scope={"repo": "other", "tenant": "tenant"},
        )
    )
    context = MemoryContext(
        mode="planning",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
    )
    scores = {
        "wrong_scope": 100,
        "policy_c": 0.9,
        "lesson_b": 0.9,
        "lesson_a": 0.8,
        "lesson_c": 0.7,
        "policy_a": 0.2,
    }

    candidates = store.candidate_memories(
        context,
        semantic_scores=scores,
        max_candidates=3,
        minimum_score=0.5,
    )

    assert [memory.memory_id for memory in candidates] == [
        "lesson_b",
        "policy_c",
        "lesson_a",
    ]


def test_candidate_memories_accepts_an_empty_semantic_score_mapping():
    store, trace, _case, _lesson = store_with_active_lesson()

    assert store.candidate_memories(
        matching_context(trace),
        semantic_scores={},
        max_candidates=1,
    ) == []
```

- [ ] **Step 2: Run the ranking tests and confirm the red state**

Run:

```powershell
python -m pytest -q \
  tests/test_store.py::test_candidate_memories_ranks_semantic_scores_after_metadata_filter \
  tests/test_store.py::test_candidate_memories_accepts_an_empty_semantic_score_mapping
```

Expected: both tests fail with `TypeError` because `candidate_memories()` does not accept `semantic_scores`.

- [ ] **Step 3: Add failing validation tests**

Add this parameterized contract test in `tests/test_store.py`:

```python
@pytest.mark.parametrize(
    ("semantic_kwargs", "message"),
    [
        ({"semantic_scores": [], "max_candidates": 1}, "semantic_scores must be a mapping or None"),
        ({"query": "", "semantic_scores": {}, "max_candidates": 1}, "query and semantic_scores are mutually exclusive"),
        ({"semantic_scores": {}}, "max_candidates is required with semantic_scores"),
        ({"max_candidates": 1}, "max_candidates requires semantic_scores"),
        ({"minimum_score": 0.5}, "minimum_score requires semantic_scores"),
        ({"semantic_scores": {}, "max_candidates": True}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 1.0}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 0}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 51}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 1, "minimum_score": False}, "minimum_score must be a finite number"),
        ({"semantic_scores": {}, "max_candidates": 1, "minimum_score": float("nan")}, "minimum_score must be a finite number"),
        ({"semantic_scores": {}, "max_candidates": 1, "minimum_score": float("inf")}, "minimum_score must be a finite number"),
        ({"semantic_scores": {"lesson_001": False}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {"lesson_001": float("inf")}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {1: 0.5}, "max_candidates": 1}, "semantic score memory IDs must be non-empty strings"),
        ({"semantic_scores": {"": 0.5}, "max_candidates": 1}, "semantic score memory IDs must be non-empty strings"),
        ({"semantic_scores": {"x" * 129: 0.5}, "max_candidates": 1}, "semantic score memory IDs must be at most 128 characters"),
        ({"semantic_scores": {"missing": 0.5}, "max_candidates": 1}, "semantic_scores references unknown memory IDs: missing"),
    ],
)
def test_candidate_memories_rejects_invalid_semantic_options(
    semantic_kwargs: dict[str, object], message: str
):
    store, trace, _case, _lesson = store_with_active_lesson()

    with pytest.raises(ValueError, match=message):
        store.candidate_memories(
            matching_context(trace),
            **semantic_kwargs,  # type: ignore[arg-type]
        )
```

- [ ] **Step 4: Run the validation test and confirm the red state**

Run:

```powershell
python -m pytest -q tests/test_store.py::test_candidate_memories_rejects_invalid_semantic_options
```

Expected: failures are `TypeError` for the new keyword arguments rather than the required `ValueError` contract.

- [ ] **Step 5: Implement semantic option validation**

Import `LLM_GATE_MAX_CANDIDATES` from `.policy` in `src/trace_backed_memory/store.py`, then add this helper immediately before `_tokens()`:

```python
def _validated_semantic_scores(
    semantic_scores: Mapping[str, float] | None,
    *,
    query: str | None,
    max_candidates: int | None,
    minimum_score: float | None,
    stored_memory_ids: set[str],
) -> dict[str, int | float] | None:
    if semantic_scores is None:
        if max_candidates is not None:
            raise ValueError("max_candidates requires semantic_scores")
        if minimum_score is not None:
            raise ValueError("minimum_score requires semantic_scores")
        return None
    if not isinstance(semantic_scores, Mapping):
        raise ValueError("semantic_scores must be a mapping or None")
    if query is not None:
        raise ValueError("query and semantic_scores are mutually exclusive")
    if max_candidates is None:
        raise ValueError("max_candidates is required with semantic_scores")
    if type(max_candidates) is not int or not 1 <= max_candidates <= LLM_GATE_MAX_CANDIDATES:
        raise ValueError(
            "max_candidates must be an integer from 1 through "
            f"{LLM_GATE_MAX_CANDIDATES}"
        )
    if minimum_score is not None and not is_finite_number(minimum_score):
        raise ValueError("minimum_score must be a finite number")

    validated: dict[str, int | float] = {}
    for memory_id, score in semantic_scores.items():
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("semantic score memory IDs must be non-empty strings")
        if len(memory_id) > MEMORY_ID_MAX_CHARS:
            raise ValueError(
                "semantic score memory IDs must be at most "
                f"{MEMORY_ID_MAX_CHARS} characters"
            )
        if not is_finite_number(score):
            raise ValueError(
                f"semantic score for {memory_id!r} must be a finite number"
            )
        validated[memory_id] = score

    unknown_ids = sorted(set(validated).difference(stored_memory_ids))
    if unknown_ids:
        raise ValueError(
            "semantic_scores references unknown memory IDs: "
            + ", ".join(unknown_ids)
        )
    return validated
```

Keep exact runtime-type checks. Do not coerce IDs or scores and do not accept
`Decimal`, custom `Real`, or boolean values.

- [ ] **Step 6: Extend `candidate_memories()` and rank only metadata matches**

Replace the method signature and option prelude with:

```python
    @_synchronized
    def candidate_memories(
        self,
        context: MemoryContext,
        *,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
    ) -> list[MemoryItem]:
        validate_memory_context(context)
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be a string or None")
        validated_semantic_scores = _validated_semantic_scores(
            semantic_scores,
            query=query,
            max_candidates=max_candidates,
            minimum_score=minimum_score,
            stored_memory_ids=set(self._failure_cases).union(
                self._lessons, self._project_policies
            ),
        )
        context_values = _context_values(context)
        candidates: list[MemoryItem] = []
```

Leave the lesson, policy, and debug/repair failure-case loops unchanged. Insert
this semantic branch after those loops and before keyword tokenization:

```python
        if validated_semantic_scores is not None:
            candidates = [
                memory
                for memory in candidates
                if memory.memory_id in validated_semantic_scores
                and (
                    minimum_score is None
                    or validated_semantic_scores[memory.memory_id] >= minimum_score
                )
            ]
            candidates.sort(
                key=lambda memory: (
                    -validated_semantic_scores[memory.memory_id],
                    memory.memory_id,
                )
            )
            return candidates[:max_candidates]
```

Keep the existing keyword branch and final memory-ID sort exactly as they are.

- [ ] **Step 7: Run focused and neighboring retrieval tests**

Run:

```powershell
python -m pytest -q tests/test_store.py -k "candidate_memories or semantic_options"
```

Expected: all selected tests pass, including the pre-existing metadata, keyword, project-policy, and failure-case cases.

- [ ] **Step 8: Run formatting checks and commit Task 1**

Run:

```powershell
python -m compileall -q src tests
git diff --check
git status --short
```

Expected: compile and diff checks exit 0; only `store.py` and `test_store.py` are modified.

Commit:

```powershell
git add src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: add semantic score candidate retrieval"
```

---

### Task 2: Safe Workflow And Audit Integration

**Files:**
- Modify: `tests/test_store.py` near the two-phase workflow tests
- Modify: `src/trace_backed_memory/store.py` in `prepare_memory()`

**Interfaces:**
- Consumes: Task 1 `candidate_memories(..., semantic_scores, max_candidates, minimum_score)`.
- Produces: `prepare_memory(context, *, task, query=None, semantic_scores=None, max_candidates=None, minimum_score=None, context_summary="") -> MemoryGateRequest`.
- Preserves: `finalize_memory()` API and behavior; no score mapping is retained in a pending request.

- [ ] **Step 1: Add a failing safe-workflow integration test**

Add this test near `test_prepare_and_finalize_memory_is_trace_linked_and_audited`:

```python
def test_prepare_memory_keeps_semantic_rank_and_system_gate_audit_boundaries():
    store, trace, _case, safe_lesson = store_with_active_lesson()
    sensitive_lesson = replace(
        safe_lesson,
        lesson_id="lesson_sensitive",
        lesson_text="Sensitive guidance must remain blocked.",
        sensitive=True,
    )
    obsolete_lesson = replace(
        safe_lesson,
        lesson_id="lesson_obsolete",
        lesson_text="Obsolete guidance must remain blocked.",
        status="obsolete",
    )
    store.add_lesson(sensitive_lesson)
    store.add_lesson(obsolete_lesson)
    scores = {
        sensitive_lesson.lesson_id: 1.0,
        obsolete_lesson.lesson_id: 0.9,
        safe_lesson.lesson_id: 0.8,
    }

    request = store.prepare_memory(
        matching_context(trace),
        task="repair failed call",
        semantic_scores=scores,
        max_candidates=3,
    )
    scores.clear()

    assert request.candidate_memory_ids == (
        sensitive_lesson.lesson_id,
        obsolete_lesson.lesson_id,
        safe_lesson.lesson_id,
    )
    assert request.system_allowed_memory_ids == (safe_lesson.lesson_id,)
    assert dict(request.system_blocked) == {
        sensitive_lesson.lesson_id: "memory is marked sensitive",
        obsolete_lesson.lesson_id: "status 'obsolete' is not allowed",
    }
    assert sensitive_lesson.lesson_id not in request.prompt
    assert obsolete_lesson.lesson_id not in request.prompt

    result = store.finalize_memory(
        request,
        allow_decision(safe_lesson.lesson_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert result.allowed_memory_ids == (safe_lesson.lesson_id,)
    log = store.usage_logs[-1]
    assert log.candidate_memory_ids == [
        sensitive_lesson.lesson_id,
        obsolete_lesson.lesson_id,
        safe_lesson.lesson_id,
    ]
    assert log.candidate_memory_statuses == {
        sensitive_lesson.lesson_id: "active",
        obsolete_lesson.lesson_id: "obsolete",
        safe_lesson.lesson_id: "active",
    }
    assert log.system_blocked_reasons == dict(request.system_blocked)
```

This one test proves rank preservation, caller-map isolation, high-score safety
blocking, LLM prompt narrowing, finalization, and persisted candidate evidence.

- [ ] **Step 2: Add a failing request-ID atomicity test**

Add:

```python
def test_invalid_semantic_prepare_does_not_consume_request_id():
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc")

    with pytest.raises(
        ValueError,
        match="max_candidates must be an integer from 1 through 50",
    ):
        store.prepare_memory(
            context,
            task="repair",
            semantic_scores={},
            max_candidates=0,
        )

    request = store.prepare_memory(context, task="repair")
    assert request.request_id == "gate_request_000001"
```

- [ ] **Step 3: Add a failing semantic stale-state test**

Add:

```python
def test_finalize_rechecks_semantically_selected_memory_obsoleted_after_prepare():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(
        matching_context(trace),
        task="repair",
        semantic_scores={lesson.lesson_id: 1.0},
        max_candidates=1,
    )
    store.obsolete_lesson(lesson.lesson_id)

    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
    )

    assert result.use_memory is False
    assert result.snippet == ""
    assert lesson.lesson_id in result.blocked_memory_ids
```

- [ ] **Step 4: Run the three tests and confirm the red state**

Run:

```powershell
python -m pytest -q \
  tests/test_store.py::test_prepare_memory_keeps_semantic_rank_and_system_gate_audit_boundaries \
  tests/test_store.py::test_invalid_semantic_prepare_does_not_consume_request_id \
  tests/test_store.py::test_finalize_rechecks_semantically_selected_memory_obsoleted_after_prepare
```

Expected: all three fail because `prepare_memory()` does not accept the semantic options.

- [ ] **Step 5: Thread semantic options through `prepare_memory()`**

Change only the signature and candidate call in `src/trace_backed_memory/store.py`:

```python
    @_synchronized
    def prepare_memory(
        self,
        context: MemoryContext,
        *,
        task: str,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
        context_summary: str = "",
    ) -> MemoryGateRequest:
        validate_memory_context(context)
        candidates = self.candidate_memories(
            context,
            query=query,
            semantic_scores=semantic_scores,
            max_candidates=max_candidates,
            minimum_score=minimum_score,
        )
```

Leave System Gate, prompt construction, request registration, finalization, and
usage-log creation unchanged. In particular, do not add score fields to
`MemoryGateRequest` or `MemoryUsageLog`.

- [ ] **Step 6: Run workflow, stale-state, and audit tests**

Run:

```powershell
python -m pytest -q tests/test_store.py -k "prepare_memory or finalize or usage_log"
```

Expected: all selected tests pass. Existing stale-state and replay tests prove
that semantic selection does not alter finalization's fresh System Gate check.

- [ ] **Step 7: Run the complete store test module and commit Task 2**

Run:

```powershell
python -m pytest -q tests/test_store.py
python -m compileall -q src tests
git diff --check
```

Expected: all store tests pass and both checks exit 0.

Commit:

```powershell
git add src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: integrate semantic retrieval with safe workflow"
```

---

### Task 3: Public Documentation And Compatibility Contract

**Files:**
- Modify: `tests/test_readme_api.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/usage-policy.md`
- Modify: `docs/mvp-roadmap.md`

**Interfaces:**
- Consumes: Task 2 public `prepare_memory()` signature and unchanged `finalize_memory()`.
- Produces: an executable README semantic retrieval example and aligned architecture, policy, and roadmap claims.
- Preserves: keyword example, dependency-free install, snapshot version, JSON Schema, and PostgreSQL documentation.

- [ ] **Step 1: Add the README API contract test**

Add beside `test_readme_safe_workflow_example_stays_executable`:

```python
def test_readme_semantic_retrieval_example_stays_executable():
    store, trace, _case, lesson = readme_store_fixture()
    context = MemoryContext(
        mode="repair",
        repo=trace.repo,
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
    )
    semantic_scores = {lesson.lesson_id: 0.93}

    request = store.prepare_memory(
        context,
        task="repair failed search_docs call",
        semantic_scores=semantic_scores,
        max_candidates=10,
        minimum_score=0.70,
    )
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert request.candidate_memory_ids == (lesson.lesson_id,)
    assert result.allowed_memory_ids == (lesson.lesson_id,)
```

- [ ] **Step 2: Run the public API test before editing prose**

Run:

```powershell
python -m pytest -q tests/test_readme_api.py::test_readme_semantic_retrieval_example_stays_executable
```

Expected: PASS. This is a public-example contract test, not a new behavior test;
Tasks 1 and 2 already supplied its red-green cycle.

- [ ] **Step 3: Add the semantic workflow example to README**

Immediately after the existing Safe Store Workflow example, add a
`### Semantic retrieval` subsection containing the exact setup and
`prepare_memory()` call from Step 1:

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

State directly below the example:

- metadata scope is applied before ranking;
- scores may use any finite numeric scale;
- callers must normalize them so larger values mean greater relevance;
- keyword `query` and `semantic_scores` cannot be combined in one call;
- `max_candidates` is required and capped at 50;
- System Gate and LLM Gate remain authoritative;
- scores are not persisted in snapshots or PostgreSQL.

Update the Implemented pieces retrieval bullet to include optional bounded
caller-provided semantic scores, score-descending/ID-ascending ties, and the
fact that retrieval cannot bypass either gate.

- [ ] **Step 4: Align architecture and usage policy**

In `docs/architecture.md`, extend the candidate retrieval paragraph with this
contract:

```text
Callers may alternatively provide precomputed semantic scores keyed by stored
runtime memory ID. Semantic mode remains metadata-first, requires an explicit
top-k no greater than 50, accepts only finite numeric scores, and breaks ties by
memory ID. Scores select candidates only; System Gate and LLM Gate remain the
approval boundary. The store neither computes nor persists embeddings or raw
scores.
```

In `docs/usage-policy.md`, extend Safe Store Workflow with:

```text
For semantic retrieval, compute scores outside the store and pass
`semantic_scores` with an explicit `max_candidates` and optional
`minimum_score`. Do not combine it with `query`. Treat scores as retrieval
evidence only: sensitive, obsolete, leaking, low-confidence, or out-of-scope
memory must still be blocked by the normal gates.
```

- [ ] **Step 5: Mark the roadmap item implemented without overstating scope**

Replace `Optionally add keyword/vector search.` in `docs/mvp-roadmap.md` with:

```text
- Support optional keyword filtering and bounded caller-provided semantic/vector scores after metadata filtering.
```

Do not claim that the repository stores vectors, computes embeddings, or ships
an ANN index.

- [ ] **Step 6: Run documentation and persistence compatibility tests**

Run:

```powershell
python -m pytest -q tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_postgres_repository.py
git diff --check
```

Expected: all selected tests pass. No schema fixture, package metadata, or
PostgreSQL adapter expectation changes.

- [ ] **Step 7: Commit Task 3**

Run:

```powershell
git status --short
git add README.md docs/architecture.md docs/usage-policy.md docs/mvp-roadmap.md tests/test_readme_api.py
git commit -m "docs: document semantic score retrieval"
```

Expected: the commit contains exactly those five documentation/API-test files.

---

## Final Verification

- [ ] Run every test from a fresh command:

```powershell
python -m pytest -q
```

Expected: all tests pass, including real temporary PostgreSQL integration tests.

- [ ] Run source and whitespace checks:

```powershell
python -m compileall -q src tests
git diff --check main...HEAD
```

Expected: both commands exit 0.

- [ ] Verify scope and dependency invariants:

```powershell
git diff --name-only main...HEAD
git diff main...HEAD -- pyproject.toml schemas src/trace_backed_memory/models.py src/trace_backed_memory/postgres.py
git status --short --branch
```

Expected: the first command lists only the design, plan, `store.py`, the two
test files, README, and three documentation files. The second command has no
output. The branch is clean.

- [ ] Review the full branch diff against
`docs/superpowers/specs/2026-07-12-semantic-score-retrieval-design.md`, resolve
every Critical, Important, and Minor finding, rerun affected tests, then rerun
the complete verification commands before merging.
