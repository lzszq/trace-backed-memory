# Git Ancestry Applicability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional, fail-closed Git ancestry applicability to runtime memory retrieval and PR reports without running Git inside the store lock or changing persisted contracts.

**Architecture:** The store exposes the commit anchors required for a context. A standalone capture helper resolves those anchors with `git merge-base --is-ancestor` outside the store and returns immutable evidence bound to the exact current commit. Runtime retrieval and PR reporting validate complete evidence after metadata matching and exclude confirmed non-ancestor history before ranking or report rendering.

**Tech Stack:** Python 3.11+, standard library `subprocess`, Git CLI, pytest, existing trace-backed-memory store and policy helpers.

## Global Constraints

- Keep `[project].dependencies = []`; add no core or optional package.
- Existing calls that omit `commit_ancestry` must retain exact retrieval, prompt, PR report, error, and deterministic ordering behavior.
- Never execute Git, a callback, filesystem lookup, or network operation while `TraceBackedMemoryStore._lock` is held.
- `CommitAncestryEvidence` is immutable, bound to one exact `context.commit_sha`, completely runtime-validated, and never persisted.
- Exit code 0 from `git merge-base --is-ancestor` means true, 1 means false, and every other outcome is an error rather than false.
- Ancestry is applied after metadata matching but before keyword or semantic retrieval; it remains independent of System Gate and LLM Gate.
- Lesson valid-from anchor is its source case `fix_commit_sha`; episodic failure-case anchor is its source `commit_sha`; project policies have no ancestry anchor.
- Missing evidence for any metadata-eligible history-backed record fails closed; extra valid relations are allowed.
- PR ancestry uses each context-matched failure case's source `commit_sha`; old/new change-set matching remains out of scope.
- Do not change snapshot version 2, `MemoryGateRequest`, `MemoryUsageLog`, `PRMemoryReport`, JSON Schemas, PostgreSQL schema version 1, repository synchronization, or active-lessons YAML.
- Use `apply_patch` for every manual edit and preserve unrelated work.

---

## File Structure

- `src/trace_backed_memory/models.py`: define the ephemeral immutable evidence value.
- `src/trace_backed_memory/capture.py`: validate inputs and capture true/false Git ancestry outside the store.
- `src/trace_backed_memory/store.py`: discover anchors, validate evidence, filter runtime candidates and PR cases, and thread evidence through preparation.
- `src/trace_backed_memory/__init__.py`: export the public model, helper, and error.
- `tests/test_capture.py`: unit and real temporary-DAG tests for Git capture.
- `tests/test_store.py`: runtime, gate, audit, persistence, PR, validation, and compatibility tests.
- `tests/test_readme_api.py`: executable public workflow example.
- `README.md`, `docs/architecture.md`, `docs/usage-policy.md`, `docs/product-program.md`: public contract and implemented roadmap phase.

No package metadata, schema, PostgreSQL, YAML, or example JSON file changes.

---

### Task 1: Immutable Evidence And Git Capture

**Files:**
- Modify: `tests/test_capture.py`
- Modify: `src/trace_backed_memory/models.py`
- Modify: `src/trace_backed_memory/capture.py`
- Modify: `src/trace_backed_memory/__init__.py`

**Interfaces:**
- Produces: `CommitAncestryEvidence(current_commit_sha: str, commit_relations: tuple[tuple[str, bool], ...])`.
- Produces: `capture_commit_ancestry(current_commit_sha, anchor_commit_shas, repo_path=None, *, runner=None) -> CommitAncestryEvidence`.
- Produces: `CommitAncestryCaptureError(RuntimeError)`.

- [ ] **Step 1: Add failing deterministic capture tests**

Extend `tests/test_capture.py` imports with `subprocess`, `FrozenInstanceError`, `Path`, `pytest`,
`CommitAncestryCaptureError`, `CommitAncestryEvidence`, and
`capture_commit_ancestry`, then add:

```python
def test_capture_commit_ancestry_sorts_deduplicates_and_records_false():
    calls: list[tuple[str, ...]] = []

    def runner(args: list[str], cwd: str | None = None) -> int:
        calls.append(tuple(args))
        return {"ancestor": 0, "unrelated": 1}[args[-2]]

    evidence = capture_commit_ancestry(
        "current",
        ["unrelated", "ancestor", "ancestor"],
        repo_path="C:/work/repo",
        runner=runner,
    )

    assert evidence == CommitAncestryEvidence(
        current_commit_sha="current",
        commit_relations=(("ancestor", True), ("unrelated", False)),
    )
    assert calls == [
        ("git", "merge-base", "--is-ancestor", "ancestor", "current"),
        ("git", "merge-base", "--is-ancestor", "unrelated", "current"),
    ]


def test_capture_commit_ancestry_accepts_empty_anchors_without_running_git():
    def runner(_args: list[str], _cwd: str | None = None) -> int:
        raise AssertionError("Git must not run for empty anchors")

    assert capture_commit_ancestry("current", [], runner=runner) == (
        CommitAncestryEvidence(current_commit_sha="current", commit_relations=())
    )


def test_commit_ancestry_evidence_is_frozen():
    evidence = CommitAncestryEvidence("current", (("anchor", True),))

    with pytest.raises(FrozenInstanceError):
        evidence.current_commit_sha = "other"  # type: ignore[misc]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m pytest -q \
  tests/test_capture.py::test_capture_commit_ancestry_sorts_deduplicates_and_records_false \
  tests/test_capture.py::test_capture_commit_ancestry_accepts_empty_anchors_without_running_git
```

Expected: collection fails because the three new public names do not exist.

- [ ] **Step 3: Add failing validation and command-error tests**

Add:

```python
@pytest.mark.parametrize(
    ("current", "anchors", "message"),
    [
        ("", [], "current_commit_sha must be a non-empty string"),
        ([], [], "current_commit_sha must be a non-empty string"),
        ("x" * 513, [], "current_commit_sha must be at most 512 characters"),
        ("current", "ancestor", "anchor_commit_shas must be an iterable of commit strings"),
        ("current", [""], "anchor commit must be a non-empty string"),
        ("current", [1], "anchor commit must be a non-empty string"),
        ("current", ["x" * 513], "anchor commit must be at most 512 characters"),
    ],
)
def test_capture_commit_ancestry_rejects_malformed_inputs(
    current: object, anchors: object, message: str
):
    with pytest.raises(ValueError, match=message):
        capture_commit_ancestry(current, anchors)  # type: ignore[arg-type]


@pytest.mark.parametrize("return_code", [True, -1, 2, "0"])
def test_capture_commit_ancestry_rejects_invalid_runner_results(return_code: object):
    with pytest.raises(CommitAncestryCaptureError, match="git merge-base"):
        capture_commit_ancestry(
            "current",
            ["anchor"],
            runner=lambda _args, _cwd=None: return_code,  # type: ignore[return-value]
        )


def test_capture_commit_ancestry_wraps_command_failures_with_context():
    failure = subprocess.CalledProcessError(
        128,
        ["git", "merge-base"],
        stderr="fatal: bad object anchor",
    )

    def runner(_args: list[str], _cwd: str | None = None) -> int:
        raise failure

    with pytest.raises(CommitAncestryCaptureError) as captured:
        capture_commit_ancestry(
            "current", ["anchor"], repo_path="C:/work/repo", runner=runner
        )

    assert "git merge-base --is-ancestor anchor current" in str(captured.value)
    assert "C:/work/repo" in str(captured.value)
    assert "fatal: bad object anchor" in str(captured.value)
    assert captured.value.__cause__ is failure
```

- [ ] **Step 4: Add a failing real Git DAG test**

Add:

```python
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_file(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def test_capture_commit_ancestry_against_real_git_dag(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Trace Tests")
    base = _commit_file(repo, "base.txt", "base")
    _git(repo, "checkout", "-b", "side")
    side = _commit_file(repo, "side.txt", "side")
    _git(repo, "checkout", "main")
    current = _commit_file(repo, "current.txt", "current")

    evidence = capture_commit_ancestry(
        current,
        [side, current, base],
        repo_path=str(repo),
    )

    assert dict(evidence.commit_relations) == {
        base: True,
        current: True,
        side: False,
    }
```

- [ ] **Step 5: Implement the model and capture helper**

Add to `models.py` after `TraceMetadata`:

```python
@dataclass(frozen=True)
class CommitAncestryEvidence:
    current_commit_sha: str
    commit_relations: tuple[tuple[str, bool], ...]
```

In `capture.py`, import `Iterable`, `CommitAncestryEvidence`, and
`METADATA_VALUE_MAX_CHARS`, then implement:

```python
AncestryRunner = Callable[[list[str], str | None], int]


class CommitAncestryCaptureError(RuntimeError):
    """Raised when Git ancestry evidence cannot be captured."""


def capture_commit_ancestry(
    current_commit_sha: str,
    anchor_commit_shas: Iterable[str],
    repo_path: str | None = None,
    *,
    runner: AncestryRunner | None = None,
) -> CommitAncestryEvidence:
    _validate_commit_string(current_commit_sha, "current_commit_sha")
    if isinstance(anchor_commit_shas, (str, bytes)) or not isinstance(
        anchor_commit_shas, Iterable
    ):
        raise ValueError("anchor_commit_shas must be an iterable of commit strings")
    anchors = list(anchor_commit_shas)
    for anchor in anchors:
        _validate_commit_string(anchor, "anchor commit")

    run = runner or _run_ancestry_command
    relations: list[tuple[str, bool]] = []
    for anchor in sorted(set(anchors)):
        args = ["git", "merge-base", "--is-ancestor", anchor, current_commit_sha]
        return_code = _capture_ancestry_result(
            run, args, repo_path, anchor=anchor, current=current_commit_sha
        )
        relations.append((anchor, return_code == 0))
    return CommitAncestryEvidence(
        current_commit_sha=current_commit_sha,
        commit_relations=tuple(relations),
    )


def _validate_commit_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > METADATA_VALUE_MAX_CHARS:
        raise ValueError(
            f"{field_name} must be at most {METADATA_VALUE_MAX_CHARS} characters"
        )


def _run_ancestry_command(args: list[str], cwd: str | None = None) -> int:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise subprocess.CalledProcessError(
            completed.returncode,
            args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.returncode


def _capture_ancestry_result(
    run: AncestryRunner,
    args: list[str],
    repo_path: str | None,
    *,
    anchor: str,
    current: str,
) -> int:
    command = " ".join(args)
    location = repo_path or "."
    try:
        result = run(args, repo_path)
    except Exception as exc:
        raise CommitAncestryCaptureError(
            f"failed to capture git ancestry with `{command}` in {location}: "
            f"{_command_error_detail(exc)}"
        ) from exc
    if type(result) is not int or result not in {0, 1}:
        raise CommitAncestryCaptureError(
            f"failed to capture git ancestry with `{command}` in {location}: "
            f"runner returned invalid exit code {result!r} for {anchor} against {current}"
        )
    return result
```

Re-export the model, helper, and error from `__init__.py` and add all three to
`__all__`.

- [ ] **Step 6: Run Task 1 tests and full capture module**

Run:

```powershell
python -m pytest -q tests/test_capture.py
python -m compileall -q src tests
git diff --check
```

Expected: every capture test passes and both checks exit 0.

- [ ] **Step 7: Run the full suite and commit Task 1**

Run `python -m pytest -q`; expected: all tests pass.

Commit:

```powershell
git add src/trace_backed_memory/models.py src/trace_backed_memory/capture.py src/trace_backed_memory/__init__.py tests/test_capture.py
git commit -m "feat: capture immutable git ancestry evidence"
```

---

### Task 2: Runtime Anchor Discovery And Gated Retrieval

**Files:**
- Modify: `tests/test_store.py`
- Modify: `src/trace_backed_memory/store.py`

**Interfaces:**
- Consumes: Task 1 `CommitAncestryEvidence`.
- Produces: `candidate_commit_anchors(context) -> tuple[str, ...]`.
- Extends: `candidate_memories(..., commit_ancestry=None)` and `prepare_memory(..., commit_ancestry=None)`.
- Preserves: request, finalization, usage-log, snapshot, keyword, and semantic-score models.

- [ ] **Step 1: Add failing anchor discovery and filtering tests**

Import `CommitAncestryEvidence` in `tests/test_store.py`, then add near the
candidate retrieval tests:

```python
def ancestry_evidence(current: str, **relations: bool) -> CommitAncestryEvidence:
    return CommitAncestryEvidence(
        current_commit_sha=current,
        commit_relations=tuple(relations.items()),
    )


def ancestry_context() -> MemoryContext:
    return MemoryContext(
        mode="debug",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        failure_type="invalid_tool_argument",
    )


def test_candidate_commit_anchors_are_sorted_and_exclude_project_policies():
    store = store_with_retrieval_records_in_order(["b", "a"])

    assert store.candidate_commit_anchors(ancestry_context()) == (
        "commit_a",
        "commit_b",
        "fix_commit_a",
        "fix_commit_b",
    )


def test_candidate_memories_filter_history_but_not_project_policies_by_ancestry():
    store = store_with_retrieval_records_in_order(["b", "a"])
    evidence = ancestry_evidence(
        "current",
        commit_a=True,
        commit_b=False,
        fix_commit_a=True,
        fix_commit_b=False,
        unused_anchor=False,
    )

    candidates = store.candidate_memories(
        ancestry_context(), commit_ancestry=evidence
    )

    assert [memory.memory_id for memory in candidates] == [
        "case_a",
        "lesson_a",
        "policy_a",
        "policy_b",
    ]
```

- [ ] **Step 2: Run these tests and verify RED**

Expected: failures because the discovery method and keyword argument do not
exist.

- [ ] **Step 3: Add failing evidence validation tests**

Add parameterized cases for:

```python
@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ({}, "commit_ancestry must be a CommitAncestryEvidence or None"),
        (CommitAncestryEvidence("", ()), "commit ancestry evidence requires current_commit_sha"),
        (CommitAncestryEvidence("other", ()), "does not match context commit_sha"),
        (CommitAncestryEvidence("current", []), "commit_relations must be a tuple"),
        (CommitAncestryEvidence("current", (["commit_a", True],)), "relations must be two-item tuples"),
        (CommitAncestryEvidence("current", (("", True),)), "relations require anchor commit"),
        (CommitAncestryEvidence("current", (("commit_a", 1),)), "relation values must be booleans"),
        (CommitAncestryEvidence("current", (("commit_a", True), ("commit_a", False))), "duplicate commit ancestry relation"),
    ],
)
def test_candidate_memories_rejects_malformed_commit_ancestry(
    evidence: object, message: str
):
    store = store_with_retrieval_records_in_order(["a"])
    with pytest.raises(ValueError, match=message):
        store.candidate_memories(
            ancestry_context(), commit_ancestry=evidence  # type: ignore[arg-type]
        )


def test_candidate_memories_rejects_sorted_missing_ancestry_anchors():
    store = store_with_retrieval_records_in_order(["b", "a"])
    evidence = ancestry_evidence("current", commit_a=True)

    with pytest.raises(
        ValueError,
        match="commit ancestry evidence is missing anchors: commit_b, fix_commit_a, fix_commit_b",
    ):
        store.candidate_memories(ancestry_context(), commit_ancestry=evidence)
```

Pass `query="lesson a"` in the last call. The same missing-anchor error must
occur, proving ancestry completeness is checked before keyword filtering.

Use direct malformed construction with `# type: ignore[arg-type]` where the
dataclass annotation is intentionally violated.

- [ ] **Step 4: Add failing ordering, preparation, and non-persistence tests**

Add:

```python
def test_ancestry_filters_before_semantic_ranking():
    store = store_with_retrieval_records_in_order(["a"])
    evidence = ancestry_evidence(
        "current", commit_a=False, fix_commit_a=False
    )

    candidates = store.candidate_memories(
        ancestry_context(),
        semantic_scores={"case_a": 1.0, "lesson_a": 0.9, "policy_a": 0.1},
        max_candidates=3,
        commit_ancestry=evidence,
    )

    assert [memory.memory_id for memory in candidates] == ["policy_a"]


def test_semantic_scores_are_validated_even_for_false_ancestry():
    store = store_with_retrieval_records_in_order(["a"])
    evidence = ancestry_evidence(
        "current", commit_a=False, fix_commit_a=False
    )

    with pytest.raises(
        ValueError,
        match="semantic score for 'lesson_a' must be a finite number",
    ):
        store.candidate_memories(
            ancestry_context(),
            semantic_scores={"lesson_a": "invalid"},  # type: ignore[dict-item]
            max_candidates=1,
            commit_ancestry=evidence,
        )


def test_true_ancestry_does_not_bypass_system_gate():
    store, trace, case, lesson = store_with_active_lesson()
    sensitive = replace(
        lesson,
        lesson_id="lesson_sensitive",
        lesson_text="Sensitive guidance",
        sensitive=True,
    )
    store.add_lesson(sensitive)
    assert case.fix_commit_sha is not None
    evidence = CommitAncestryEvidence(
        trace.commit_sha,
        ((case.fix_commit_sha, True),),
    )

    request = store.prepare_memory(
        matching_context(trace),
        task="repair",
        commit_ancestry=evidence,
    )

    assert sensitive.lesson_id in request.candidate_memory_ids
    assert sensitive.lesson_id not in request.system_allowed_memory_ids
    assert dict(request.system_blocked)[sensitive.lesson_id] == (
        "memory is marked sensitive"
    )


def test_invalid_ancestry_prepare_does_not_consume_request_id():
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="repo", commit_sha="current")

    with pytest.raises(ValueError, match="does not match context commit_sha"):
        store.prepare_memory(
            context,
            task="repair",
            commit_ancestry=CommitAncestryEvidence("other", ()),
        )

    assert store.prepare_memory(context, task="repair").request_id == (
        "gate_request_000001"
    )


def test_prepare_uses_ancestry_without_persisting_evidence():
    store = store_with_retrieval_records_in_order(["a"])
    store.record_trace(
        Trace(
            trace_id="trace_current",
            run_id="run_current",
            commit_sha="current",
            repo="repo",
            tenant="tenant",
        )
    )
    context = ancestry_context()
    evidence = ancestry_evidence(
        "current", commit_a=True, fix_commit_a=True
    )
    before = store.to_snapshot()

    request = store.prepare_memory(
        context, task="repair", commit_ancestry=evidence
    )
    assert store.to_snapshot() == before
    result = store.finalize_memory(
        request,
        allow_decision("lesson_a"),
        trace_id="trace_current",
        eval_result="pass",
    )

    assert result.allowed_memory_ids == ("lesson_a",)
    encoded = json.dumps(store.to_snapshot(), sort_keys=True)
    assert "commit_ancestry" not in encoded
    assert "commit_relations" not in encoded
```

- [ ] **Step 5: Refactor metadata candidate construction once**

In `store.py`, import `CommitAncestryEvidence`. Extract the current lesson,
policy, and debug/repair failure-case loops into an undecorated private method:

```python
    def _metadata_candidates(self, context: MemoryContext) -> list[MemoryItem]:
        context_values = _context_values(context)
        candidates: list[MemoryItem] = []
        # Move the existing loops here without changing their conditions.
        return candidates
```

Both public callers hold the existing `RLock`; do not decorate this helper and
do not change candidate construction behavior.

- [ ] **Step 6: Implement evidence validation and runtime anchor filtering**

Add these public/private store methods:

```python
    @_synchronized
    def candidate_commit_anchors(
        self, context: MemoryContext
    ) -> tuple[str, ...]:
        validate_memory_context(context)
        return tuple(
            sorted(
                {
                    anchor
                    for memory in self._metadata_candidates(context)
                    if (anchor := self._commit_anchor(memory.memory_id)) is not None
                }
            )
        )

    def _commit_anchor(self, memory_id: str) -> str | None:
        lesson = self._lessons.get(memory_id)
        if lesson is not None:
            anchor = self._failure_cases[lesson.source_case_id].fix_commit_sha
            if not isinstance(anchor, str) or not anchor:
                raise ValueError(
                    f"lesson source case lacks fix_commit_sha: {memory_id}"
                )
            return anchor
        case = self._failure_cases.get(memory_id)
        if case is not None:
            return case.commit_sha
        return None

    def _filter_candidates_by_ancestry(
        self,
        candidates: list[MemoryItem],
        relations: dict[str, bool],
    ) -> list[MemoryItem]:
        anchors = {
            memory.memory_id: self._commit_anchor(memory.memory_id)
            for memory in candidates
        }
        _require_commit_relations(anchors.values(), relations)
        return [
            memory
            for memory in candidates
            if anchors[memory.memory_id] is None
            or relations[anchors[memory.memory_id]]
        ]
```

Import `Iterable` beside the existing `Mapping` import, then add module helpers:

```python
def _validated_commit_ancestry(
    context: MemoryContext,
    evidence: CommitAncestryEvidence | None,
) -> dict[str, bool] | None:
    if evidence is None:
        return None
    if type(evidence) is not CommitAncestryEvidence:
        raise ValueError(
            "commit_ancestry must be a CommitAncestryEvidence or None"
        )
    _validate_required_string(
        evidence.current_commit_sha,
        "current_commit_sha",
        "commit ancestry evidence requires",
        max_chars=METADATA_VALUE_MAX_CHARS,
    )
    if evidence.current_commit_sha != context.commit_sha:
        raise ValueError(
            "commit ancestry current_commit_sha does not match context commit_sha"
        )
    if not isinstance(evidence.commit_relations, tuple):
        raise ValueError("commit ancestry commit_relations must be a tuple")

    relations: dict[str, bool] = {}
    for relation in evidence.commit_relations:
        if type(relation) is not tuple or len(relation) != 2:
            raise ValueError(
                "commit ancestry relations must be two-item tuples"
            )
        anchor, is_ancestor = relation
        _validate_required_string(
            anchor,
            "anchor commit",
            "commit ancestry relations require",
            max_chars=METADATA_VALUE_MAX_CHARS,
        )
        if type(is_ancestor) is not bool:
            raise ValueError("commit ancestry relation values must be booleans")
        if anchor in relations:
            raise ValueError(f"duplicate commit ancestry relation: {anchor}")
        relations[anchor] = is_ancestor
    return relations


def _require_commit_relations(
    anchors: Iterable[str | None], relations: Mapping[str, bool]
) -> None:
    missing = sorted(
        {anchor for anchor in anchors if anchor is not None and anchor not in relations}
    )
    if missing:
        raise ValueError(
            "commit ancestry evidence is missing anchors: " + ", ".join(missing)
        )
```

Extend `candidate_memories()` with `commit_ancestry=None`. Validate semantic
options and evidence before calling `_metadata_candidates()`. Apply
`_filter_candidates_by_ancestry()` before the existing semantic or keyword
branches.

- [ ] **Step 7: Thread evidence through `prepare_memory()`**

Add the same keyword-only parameter and pass it to `candidate_memories()`.
Leave request construction, System Gate, prompt building, request IDs,
finalization, and logs unchanged.

- [ ] **Step 8: Run runtime and compatibility tests**

Run:

```powershell
python -m pytest -q tests/test_store.py -k "candidate or prepare or ancestry or semantic"
python -m pytest -q tests/test_store.py
python -m compileall -q src tests
git diff --check
```

Expected: all selected tests and the full store module pass.

- [ ] **Step 9: Run the full suite and commit Task 2**

Run `python -m pytest -q`, then commit:

```powershell
git add src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: gate runtime memory by git ancestry"
```

---

### Task 3: PR Report Ancestry

**Files:**
- Modify: `tests/test_store.py`
- Modify: `src/trace_backed_memory/store.py`

**Interfaces:**
- Consumes: Task 2 evidence validation and missing-relation helper.
- Produces: `pr_report_commit_anchors(context) -> tuple[str, ...]`.
- Extends: `pr_memory_report(..., changed_fields, commit_ancestry=None)`.
- Preserves: `PRMemoryReport` fields and existing report output when evidence is omitted.

- [ ] **Step 1: Add failing PR anchor/filter tests**

Add:

```python
def test_pr_report_commit_anchors_are_sorted():
    store = store_with_retrieval_records_in_order(["b", "a"])

    assert store.pr_report_commit_anchors(ancestry_context()) == (
        "commit_a",
        "commit_b",
    )


def test_pr_memory_report_excludes_unrelated_git_history_everywhere():
    store = store_with_retrieval_records_in_order(["b", "a"])
    evidence = ancestry_evidence("current", commit_a=True, commit_b=False)

    report = store.pr_memory_report(
        ancestry_context(),
        changed_fields=["model"],
        commit_ancestry=evidence,
    )

    assert report.related_case_ids == ["case_a"]
    assert [item.case_id for item in report.related_case_provenance] == [
        "case_a"
    ]
    assert len(report.suggested_regression_tests) == 1
    assert len(report.warnings) == 1
    assert "case_a" in report.warnings[0]
```

- [ ] **Step 2: Add missing-evidence and legacy compatibility tests**

Add:

```python
def test_pr_memory_report_rejects_missing_ancestry_evidence():
    store = store_with_retrieval_records_in_order(["b", "a"])
    with pytest.raises(
        ValueError,
        match="commit ancestry evidence is missing anchors: commit_b",
    ):
        store.pr_memory_report(
            ancestry_context(),
            changed_fields=["model"],
            commit_ancestry=ancestry_evidence("current", commit_a=True),
        )


def test_pr_memory_report_without_ancestry_preserves_all_related_cases():
    store = store_with_retrieval_records_in_order(["b", "a"])
    legacy_report = store.pr_memory_report(
        ancestry_context(), changed_fields=["model"]
    )
    evidence_report = store.pr_memory_report(
        ancestry_context(),
        changed_fields=["model"],
        commit_ancestry=ancestry_evidence(
            "current", commit_a=True, commit_b=True
        ),
    )

    assert evidence_report == legacy_report
    assert legacy_report.related_case_ids == ["case_a", "case_b"]
```

- [ ] **Step 3: Run the tests and verify RED**

Expected: missing method/argument failures.

- [ ] **Step 4: Implement PR anchor discovery and filtering**

Extract the existing PR context match into one undecorated private method and
use it from both public paths:

```python
    def _pr_related_case_records(
        self, context: MemoryContext
    ) -> list[tuple[FailureCase, Trace]]:
        return [
            (case, trace)
            for case in self._failure_cases.values()
            if (trace := self._traces.get(case.source_trace_id)) is not None
            and _case_matches_context(case, trace, context)
        ]

    @_synchronized
    def pr_report_commit_anchors(
        self, context: MemoryContext
    ) -> tuple[str, ...]:
        validate_memory_context(context)
        return tuple(
            sorted(
                {
                    case.commit_sha
                    for case, _trace in self._pr_related_case_records(context)
                }
            )
        )
```

Extend `pr_memory_report()` with `commit_ancestry=None`. Preserve context and
`changed_fields` validation order, then validate evidence. After building the
context-matched records through `_pr_related_case_records()` and before
sorting/rendering:

```python
        if ancestry_relations is not None:
            _require_commit_relations(
                (case.commit_sha for case, _trace in related_case_records),
                ancestry_relations,
            )
            related_case_records = [
                record
                for record in related_case_records
                if ancestry_relations[record[0].commit_sha]
            ]
```

Do not change report models or rendering helpers.

- [ ] **Step 5: Run PR and insertion-order tests**

Run:

```powershell
python -m pytest -q tests/test_store.py -k "pr_memory_report or pr_report_commit_anchors or insertion_order"
python -m pytest -q tests/test_store.py
git diff --check
```

Expected: all selected and store tests pass.

- [ ] **Step 6: Run the full suite and commit Task 3**

Run `python -m pytest -q`, then commit:

```powershell
git add src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: scope pr memory reports by git ancestry"
```

---

### Task 4: Public Workflow And Documentation

**Files:**
- Modify: `tests/test_readme_api.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/usage-policy.md`
- Modify: `docs/product-program.md`

**Interfaces:**
- Consumes: all Task 1-3 public APIs.
- Produces: executable ancestry capture/retrieval workflow and aligned architecture/policy/roadmap claims.
- Preserves: dependency, persistence, and prior README examples.

- [ ] **Step 1: Add an executable README ancestry workflow test**

Extend root imports in `tests/test_readme_api.py`, then add:

```python
def test_readme_git_ancestry_workflow_stays_executable():
    store, trace, _case, lesson = readme_store_fixture()
    context = MemoryContext(
        mode="repair",
        repo=trace.repo,
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
    )
    anchors = store.candidate_commit_anchors(context)

    evidence = capture_commit_ancestry(
        context.commit_sha,
        anchors,
        repo_path=".",
        runner=lambda _args, _cwd=None: 0,
    )
    request = store.prepare_memory(
        context,
        task="repair failed search_docs call",
        commit_ancestry=evidence,
    )
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert result.allowed_memory_ids == (lesson.lesson_id,)
```

- [ ] **Step 2: Run the README test**

Run the single test; expected PASS because Tasks 1-3 already implement the
public behavior.

- [ ] **Step 3: Document the lock-free capture workflow**

Add a README subsection after semantic retrieval with this public flow:

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

Explain:

- capture runs outside the store lock;
- lesson anchors use fix commits and failure cases use source commits;
- missing evidence fails closed and false excludes history;
- policies still use normal scope/gates;
- omitted evidence preserves legacy behavior;
- evidence is not persisted and does not replace either gate;
- PR callers use `pr_report_commit_anchors()` and the same evidence object.

Update the Implemented pieces bullets for Git capture, runtime retrieval, and PR
reports without claiming fetch/remote support or old/new change matching.

- [ ] **Step 4: Align architecture, policy, and roadmap**

In `docs/architecture.md`, add a Git ancestry applicability section describing
the immutable evidence model, lock boundary, anchor meanings, fail-closed
completeness, runtime/PR order, and zero persistence changes.

In `docs/usage-policy.md`, require callers opting in to discover all anchors,
capture them against the exact context commit, and pass the evidence unchanged.
State that exit 1 is false while command errors must stop the workflow.

Add `Phase 9: Git ancestry applicability (implemented)` to
`docs/product-program.md` with runtime and PR anchor discovery, lock-free capture,
current-commit binding, missing-evidence failure, and persistence compatibility.

- [ ] **Step 5: Run documentation and persistence compatibility tests**

Run:

```powershell
python -m pytest -q tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_postgres_repository.py
python -m compileall -q src tests
git diff --check
```

Expected: all tests and checks pass with no schema or adapter changes.

- [ ] **Step 6: Run the full suite and commit Task 4**

Run `python -m pytest -q`, then commit:

```powershell
git add README.md docs/architecture.md docs/usage-policy.md docs/product-program.md tests/test_readme_api.py
git commit -m "docs: document git ancestry applicability"
```

---

## Final Verification

- [ ] Run the complete suite from a fresh command: `python -m pytest -q`.
- [ ] Run `python -m compileall -q src tests`.
- [ ] Run `git diff --check main...HEAD`.
- [ ] Verify `git diff main...HEAD -- pyproject.toml schemas src/trace_backed_memory/postgres.py memory` has no output.
- [ ] Verify branch scope contains only the design, plan, four source files, three test files, README, and three public docs.
- [ ] Generate a full review package from `git merge-base main HEAD` through `HEAD` and resolve every Critical, Important, and Minor finding.
- [ ] Rerun the complete suite after all review fixes.
- [ ] Fast-forward merge into `main`, rerun the complete suite on merged `main`, push, and verify `refs/heads/main` equals local `HEAD`.
