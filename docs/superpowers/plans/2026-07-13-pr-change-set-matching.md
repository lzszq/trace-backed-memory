# PR Change-Set Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add value-aware PR reporting that matches historical failures against complete old or new metadata endpoints while preserving the legacy field-name-only API.

**Architecture:** A frozen `PRChangeSet` carries deterministic `(field, old, new)` entries across ancestry-anchor discovery and report generation. Store-boundary validation binds new values to the post-change context; one private matcher applies unchanged context constraints, classifies complete old/new/both endpoint matches, and feeds both anchors and reports.

**Tech Stack:** Python 3.11+ dataclasses and typing, the dependency-free in-memory store, pytest 8+, existing Git ancestry evidence, Markdown documentation.

## Global Constraints

- Existing `changed_fields=[...]` calls and outputs must remain unchanged.
- Exact change-set fields are limited to `prompt_version`, `prompt_family`, `tool`, `tool_schema_version`, `model`, and `eval_suite`.
- New endpoint values must exactly equal the post-change `MemoryContext`, including `None`.
- Match all changed fields at the complete old endpoint or all at the complete new endpoint; exclude mixed old/new configurations.
- Repo and tenant remain hard exact-match isolation boundaries.
- Unchanged declared trace-backed context metadata remains exact-match.
- `PRChangeSet` is immutable, validated at every store boundary, and never persisted.
- Endpoint provenance is `old`, `new`, `both`, or `None` for the legacy path.
- Change-set anchor discovery and report matching must use the same matcher.
- Missing ancestry evidence for every matched record fails closed; extra valid evidence remains allowed.
- Snapshot version 2, JSON Schemas, active-lessons YAML, PostgreSQL schema version 1, and repository synchronization must not change.
- Add no dependency and use `apply_patch` for manual edits.

---

### Task 1: Immutable change-set model and strict validation

**Files:**
- Modify: `src/trace_backed_memory/models.py`
- Modify: `src/trace_backed_memory/__init__.py`
- Modify: `src/trace_backed_memory/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `MemoryContext`, `METADATA_VALUE_MAX_CHARS`, and existing context validation.
- Produces: exported `PRChangeEndpoint`, `PRChangeSet`, `PRCaseProvenance.matched_change_endpoint`, and private `_validated_pr_change_set(context, change_set)` returning normalized sorted entries.

- [ ] **Step 1: Write failing public-model and validation tests**

Add imports for `FrozenInstanceError`, `PRChangeEndpoint`, and `PRChangeSet`.
Test that `PRChangeSet((('model', 'gpt-old', 'gpt-new'),))` is frozen and
package-exported, and that legacy `PRCaseProvenance` construction defaults
`matched_change_endpoint` to `None`.

Parametrize malformed values against an empty store so validation cannot be
masked by record scanning:

```python
@pytest.mark.parametrize(
    ("change_set", "message"),
    [
        (object(), "change_set must be a PRChangeSet"),
        (PRChangeSet([]), "change_set.field_changes must be a non-empty tuple"),
        (PRChangeSet(()), "change_set.field_changes must be a non-empty tuple"),
        (PRChangeSet((("model", "old"),)), "change_set entries must be 3-item tuples"),
        (PRChangeSet((("model_family", "old", "new"),)), "unsupported change_set fields: model_family"),
        (PRChangeSet((("model", "old", "new"), ("model", "a", "b"))), "duplicate change_set fields: model"),
        (PRChangeSet((("model", "same", "same"),)), "change_set model old and new values must differ"),
    ],
)
```

Add separate cases for string/bytes replacing an entry tuple, non-string field
names, endpoint booleans/numbers/containers, empty and whitespace strings,
values longer than `METADATA_VALUE_MAX_CHARS`, and new values that do not equal
the corresponding context value. Cover `None` as a valid endpoint and as an
exact context-bound new value.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/test_store.py -q -k "pr_change_set or change_set_validation"
```

Expected: import/attribute failures because the model and validator do not
exist.

- [ ] **Step 3: Add the public model and provenance field**

In `models.py` add:

```python
PRChangeEndpoint = Literal["old", "new", "both"]


@dataclass(frozen=True)
class PRChangeSet:
    field_changes: tuple[tuple[str, str | None, str | None], ...]
```

Add `matched_change_endpoint: PRChangeEndpoint | None = None` as the final
`PRCaseProvenance` field. Re-export both public names from `__init__.py` and add
them to `__all__`.

- [ ] **Step 4: Implement exact store-boundary validation**

In `store.py`, define the supported-field tuple and implement:

```python
def _validated_pr_change_set(
    context: MemoryContext,
    change_set: PRChangeSet,
) -> tuple[tuple[str, str | None, str | None], ...]:
```

Validate exact outer/entry tuple shapes, supported string field names,
duplicates collected and sorted in one error, endpoint types and string bounds,
different endpoint values, and exact new-value/context binding. Validate the
whole value before returning entries sorted by field name. Do not normalize or
strip caller strings.

Temporarily invoke this validator from `pr_report_commit_anchors(...,
change_set=...)`; Task 2 will consume the normalized result for matching.

- [ ] **Step 5: Run focused and legacy validation tests**

Run:

```powershell
python -m pytest tests/test_store.py -q -k "pr_change_set or change_set_validation or pr_memory_report_validates_changed_fields"
```

Expected: all selected tests pass and the existing changed-fields error text is
unchanged.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/trace_backed_memory/models.py src/trace_backed_memory/__init__.py src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: add immutable pr change sets"
```

### Task 2: Complete endpoint matching and report provenance

**Files:**
- Modify: `src/trace_backed_memory/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: Task 1's normalized change entries and endpoint type.
- Produces: one private endpoint matcher shared by `_pr_related_case_records()`, `pr_report_commit_anchors()`, and `pr_memory_report()`; change-set report API; endpoint-tagged provenance.

- [ ] **Step 1: Write failing endpoint behavior tests**

Build verified cases whose traces represent:

- the complete old endpoint;
- the complete new endpoint;
- old prompt plus new model;
- new prompt plus old model;
- an unrelated endpoint;
- a correct endpoint with one unchanged context field mismatched.

Use a context bound to the new prompt/model and a `PRChangeSet` containing both
field changes. Assert only complete old and new cases are reported, case IDs are
sorted, warnings use sorted change-field names, and provenance tags are `old`
and `new`.

Add tests for:

- optional metadata addition (`None -> value`) and removal (`value -> None`);
- tool rename old/new traces and a trace invoking both tools tagged `both`;
- expected tool `None` matching only a trace with no named tool call;
- repo, tenant, failure type, and unchanged metadata still excluding records;
- reversed change-set entry order producing an identical report;
- legacy `changed_fields` report equality and `None` endpoint provenance.

- [ ] **Step 2: Run endpoint tests and verify RED**

Run:

```powershell
python -m pytest tests/test_store.py -q -k "pr_change_set_matches or pr_change_endpoint or legacy_pr_report"
```

Expected: failures because report generation does not accept `change_set` and
does not classify endpoints.

- [ ] **Step 3: Implement trace endpoint matching**

Add private helpers with these responsibilities:

```python
def _trace_matches_change_value(
    trace: Trace, field_name: str, expected: str | None
) -> bool:
    # tool uses named tool-call membership; other fields use exact attributes


def _matched_pr_change_endpoint(
    trace: Trace,
    changes: tuple[tuple[str, str | None, str | None], ...],
) -> PRChangeEndpoint | None:
    # all-old, all-new, both, or no match
```

Extend `_case_matches_context()` with an immutable set of changed fields to
ignore only at the ordinary context comparison step. Keep verified/regression,
repo, tenant, failure type, and every unchanged field authoritative.

Make `_pr_related_case_records()` return `(case, trace, endpoint)` records. For
the legacy path, endpoint is `None`; for a change set, exclude records whose
endpoint helper returns `None`.

- [ ] **Step 4: Extend `pr_memory_report()` without changing legacy behavior**

Change the signature so `changed_fields` defaults to `None` and add
`change_set`. Reject both omitted or both supplied with:

```text
exactly one of changed_fields or change_set must be provided
```

If `changed_fields` is present, run its exact current validation and preserve
caller warning order, including an empty list. If `change_set` is present,
validate it, derive sorted warning field names, and use endpoint records.

Pass the endpoint into `_case_provenance()` and populate
`matched_change_endpoint`. Keep related case, suggestion, warning, and
provenance ordering deterministic.

- [ ] **Step 5: Run endpoint and broad PR report tests**

Run:

```powershell
python -m pytest tests/test_store.py -q -k "pr_memory_report or pr_change_set or pr_change_endpoint or pr_report"
```

Expected: all selected tests pass, including all pre-existing legacy cases.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: match pr failures to change endpoints"
```

### Task 3: Endpoint-aware ancestry discovery and filtering

**Files:**
- Modify: `src/trace_backed_memory/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: Task 2's shared `(case, trace, endpoint)` records.
- Produces: `pr_report_commit_anchors(context, change_set=...)` and ancestry-filtered endpoint reports with unchanged fail-closed evidence semantics.

- [ ] **Step 1: Write failing anchor and ancestry tests**

Create old-endpoint, new-endpoint, mixed, and unrelated cases with distinct
source commits. Assert change-set anchor discovery returns only sorted old/new
endpoint commits and is independent of store insertion and change-entry order.

Then assert:

- missing evidence names every matched endpoint anchor in sorted order;
- mixed/unrelated commits do not require ancestry evidence;
- false old-endpoint ancestry removes old case IDs, suggestions, warnings, and
  provenance while retaining the new case;
- endpoint tags survive ancestry filtering;
- omitted ancestry includes both endpoints;
- legacy anchor discovery and legacy report ancestry outputs remain unchanged.

- [ ] **Step 2: Run ancestry tests and verify RED**

Run:

```powershell
python -m pytest tests/test_store.py -q -k "change_set and ancestry"
```

Expected: anchor discovery ignores the supplied change set or report evidence
requirements include the wrong records.

- [ ] **Step 3: Route anchors and reports through the same records**

Validate `change_set` in `pr_report_commit_anchors()` before scanning and pass
the normalized entries to `_pr_related_case_records()`. Return sorted unique
source commits from those exact records.

In `pr_memory_report()`, match endpoint records before
`_require_commit_relations()`. Require evidence only for those records, filter
false relations, and keep endpoint values attached to retained records.

- [ ] **Step 4: Run all ancestry and PR report tests**

Run:

```powershell
python -m pytest tests/test_store.py -q -k "ancestry or pr_memory_report or pr_report_commit_anchors or pr_change"
```

Expected: all selected tests pass with deterministic outputs.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: scope pr ancestry to change endpoints"
```

### Task 4: Executable workflow, documentation, and compatibility

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/usage-policy.md`
- Modify: `docs/mvp-roadmap.md`
- Modify: `tests/test_readme_api.py`
- Modify: `tests/test_examples_and_schema.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: Tasks 1-3 public API.
- Produces: executable value-aware PR workflow and Phase 10 documentation without persistence changes.

- [ ] **Step 1: Add an executable README workflow test**

Extend the README API test to construct a `PRChangeSet` for an old/new tool
schema version, call `pr_report_commit_anchors(context, change_set=...)`,
capture ancestry evidence, and call `pr_memory_report(change_set=...,
commit_ancestry=...)`. Assert old and new endpoint cases are reported with
their provenance tags while a mixed case is absent.

- [ ] **Step 2: Run the README test and verify RED**

Run:

```powershell
python -m pytest tests/test_readme_api.py -q
```

Expected: failure until the README example imports and executes the new API.

- [ ] **Step 3: Document the public workflow and policy**

Update `README.md` with the immutable change set, complete-endpoint semantics,
context binding, endpoint provenance, and the requirement to reuse the same
change set for anchor discovery and reporting. Keep the legacy
`changed_fields` example and state it remains broad field-name-only behavior.

Update `docs/architecture.md` with validation, hard isolation, complete old/new
matching, `both` tool behavior, ancestry order, and zero persistence changes.

Update `docs/usage-policy.md` to require caller-supplied exact values, reject
mixed-state interpretation, and prohibit value-aware `model_family` claims.

Add `Phase 10: PR change-set endpoint matching (implemented)` to
`docs/mvp-roadmap.md`.

- [ ] **Step 4: Add explicit persistence compatibility assertions**

In `tests/test_examples_and_schema.py` assert the docs state that change sets
and endpoint tags are ephemeral and that snapshot/PostgreSQL schema versions
remain unchanged. In `tests/test_store.py`, keep snapshot round trips equal
before and after creating reports and verify report-only endpoint metadata does
not appear in exported snapshots.

- [ ] **Step 5: Run documentation and compatibility suites**

Run:

```powershell
python -m pytest tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_store.py -q
```

Expected: all tests pass without schema, example JSON, YAML, or PostgreSQL SQL
changes.

- [ ] **Step 6: Commit Task 4**

```powershell
git add README.md docs/architecture.md docs/usage-policy.md docs/mvp-roadmap.md tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_store.py
git commit -m "docs: publish pr change-set workflow"
```

### Task 5: Full verification and branch review

**Files:**
- Modify only if verification exposes a defect: files from Tasks 1-4

**Interfaces:**
- Consumes: complete feature branch.
- Produces: a reviewed, merge-ready branch and measured compatibility evidence.

- [ ] **Step 1: Run focused tests**

```powershell
python -m pytest tests/test_store.py tests/test_readme_api.py tests/test_examples_and_schema.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run full verification**

Run separately:

```powershell
python -m pytest -q --durations=20
python -m compileall -q src tests
git diff --check main...HEAD
git status --short
```

Expected: full suite passes with no warnings; compile and diff checks are
clean; the worktree has no uncommitted tracked changes.

- [ ] **Step 3: Review the complete branch**

Review the full `main...HEAD` range against the design. Verify strict model
validation, complete endpoint rather than hybrid matching, context binding,
hard isolation, tool `both` behavior, ancestry parity, deterministic reports,
legacy compatibility, and zero persisted-contract changes. Resolve every
Critical, Important, and Minor finding and rerun covering tests.

- [ ] **Step 4: Merge and push**

Fetch `origin/main`. If it is still an ancestor, fast-forward local `main`.
If it advanced, rebase the feature branch, resolve conflicts by preserving
both upstream behavior and this design, and rerun verification. Run the full
suite on the merged `main`, push, and verify remote `refs/heads/main` equals
local `main` exactly before removing the owned worktree and branch.
