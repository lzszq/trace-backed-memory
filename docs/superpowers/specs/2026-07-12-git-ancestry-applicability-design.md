# Git Ancestry Applicability Design

## Summary

The store currently scopes memory by repository and other metadata, but it
cannot distinguish commits on the current history from commits that exist only
on an unrelated branch or in the future. A lesson verified on one branch can
therefore become a candidate on another branch with matching metadata, and a
PR report can surface historical failures from unrelated Git history.

This project adds optional, fail-closed Git ancestry applicability without
running Git while the store lock is held. The store exposes the commit anchors
needed for a runtime retrieval or PR report. The caller resolves those anchors
against the current commit outside the store, then supplies immutable evidence
bound to that exact current commit.

Existing calls that omit ancestry evidence retain their exact behavior.

## Goals

- Prevent opt-in runtime retrieval from using history-backed memory whose
  valid-from or source commit is not an ancestor of the current commit.
- Prevent opt-in PR reports from including failure cases from unrelated Git
  history.
- Bind ancestry evidence to one exact `MemoryContext.commit_sha` so stale
  evidence cannot be reused accidentally.
- Fail closed when a metadata-eligible history-backed record lacks evidence.
- Keep Git subprocesses outside `TraceBackedMemoryStore` and its `RLock`.
- Preserve existing keyword, semantic-score, System Gate, LLM Gate,
  finalization, and audit behavior.
- Add no dependency and require no snapshot or database migration.

## Non-goals

- Automatically enabling ancestry filtering for existing callers.
- Persisting a repository path, Git graph, ancestry result, or evidence object.
- Fetching missing commits or contacting a remote Git server.
- Defining reachability for non-Git version-control systems.
- Proving that a recorded `fix_commit_sha` semantically contains the stated
  fix.
- Matching old and new PR field values or code paths; change-set matching
  remains a separate project.
- Changing project-policy applicability, which remains scope and gate based.
- Replacing System Gate or LLM Gate with commit reachability.

## Alternatives Considered

### 1. Immutable caller-captured evidence (selected)

Expose required anchors, capture ancestry outside the store, and pass a frozen
evidence value into retrieval or PR reporting. This keeps subprocess latency
and repository-path handling outside the lock, distinguishes false relations
from missing evidence, binds results to the current commit, and remains usable
with non-local Git graph services.

### 2. Execute Git inside store methods

Accept a repository path and run `git merge-base` from `candidate_memories()`
and `pr_memory_report()`. This gives a shorter call site but holds the store
lock across subprocesses, couples the in-memory domain store to filesystem
state, and makes transaction latency and command failures part of the core
state-machine boundary.

### 3. Pass a set of ancestor commits

Accept `ancestor_commit_shas: set[str]`. This is compact, but a missing entry is
ambiguous: it may mean confirmed non-ancestor or incomplete caller evidence.
It also does not identify which current commit the set was computed for.

## Public Model

Add this ephemeral boundary value to `models.py`:

```python
@dataclass(frozen=True)
class CommitAncestryEvidence:
    current_commit_sha: str
    commit_relations: tuple[tuple[str, bool], ...]
```

Each relation means `(anchor_commit_sha, is_ancestor_or_equal)`, where the
boolean answers whether the anchor is an ancestor of or equal to
`current_commit_sha`.

The tuple representation is immutable and deterministic. Directly constructed
instances are validated at every store boundary; the dataclass constructor is
not treated as proof of validity.

The package root re-exports `CommitAncestryEvidence`.

## Git Capture API

Extend `capture.py` with:

```python
AncestryRunner = Callable[[list[str], str | None], int]


class CommitAncestryCaptureError(RuntimeError): ...


def capture_commit_ancestry(
    current_commit_sha: str,
    anchor_commit_shas: Iterable[str],
    repo_path: str | None = None,
    *,
    runner: AncestryRunner | None = None,
) -> CommitAncestryEvidence: ...
```

The helper:

1. validates `current_commit_sha` as a non-empty string no longer than
   `METADATA_VALUE_MAX_CHARS`;
2. rejects a string/bytes value in place of the anchor iterable;
3. materializes the iterable once, validates every anchor with the same string
   bound, removes duplicates, and sorts the result;
4. executes this argument-vector command once per unique anchor:

   ```text
   git merge-base --is-ancestor <anchor> <current>
   ```

5. records exit code 0 as true and exit code 1 as false;
6. returns relations sorted by anchor commit.

An empty anchor iterable returns valid empty evidence without running Git.
The default runner uses `subprocess.run()` with an argument list, no shell,
captured text output, and `check=False` so exit code 1 remains a normal false
answer.

Runner exceptions, default-runner exit codes other than 0 or 1, custom-runner
results that are not exact integers, and custom-runner integers outside 0 or 1
raise `CommitAncestryCaptureError`. The message includes the command, repository
path, anchor, current commit, and available stderr/detail; the original
exception remains the cause.

Input contract failures raise `ValueError` before any command runs.

The package root re-exports `capture_commit_ancestry` and
`CommitAncestryCaptureError`.

## Store Anchor Discovery

Add two synchronized discovery methods:

```python
def candidate_commit_anchors(self, context: MemoryContext) -> tuple[str, ...]: ...

def pr_report_commit_anchors(self, context: MemoryContext) -> tuple[str, ...]: ...
```

Both validate the context and return sorted, unique commit strings.

`candidate_commit_anchors()` uses the exact metadata-first candidate set before
keyword, semantic-score, ancestry, or gate filtering:

- lessons contribute their source case's `fix_commit_sha`;
- verified regression-backed failure-case memory contributes the case/source
  `commit_sha` in debug and repair modes;
- project policies contribute no commit anchor.

This intentionally includes metadata-matched sensitive, obsolete, or otherwise
System Gate blocked records. Discovery describes the complete evidence needed
before the later gates run.

`pr_report_commit_anchors()` uses the existing PR report context matcher and
contributes each related case's source `commit_sha`.

The discovery/apply sequence is race-safe by failure: if store state gains a
new metadata-eligible anchor between discovery and use, the consuming method
rejects the now-incomplete evidence instead of silently including the record.
Extra evidence for a record removed between calls is allowed.

## Evidence Validation

The store validates evidence before candidate or report filtering:

- the object must be an exact `CommitAncestryEvidence` instance;
- `current_commit_sha` must be a non-empty string no longer than
  `METADATA_VALUE_MAX_CHARS`;
- it must exactly equal `context.commit_sha`;
- `commit_relations` must be a tuple;
- every relation must be an exact two-item tuple;
- every anchor must be a bounded non-empty string;
- every relation value must be an exact boolean;
- duplicate anchor commits are rejected.

Relation tuple order need not already be sorted for directly constructed
evidence. The store normalizes it to a private dictionary after validating the
whole object. Extra valid relations are accepted.

After metadata matching, the store computes every applicable history-backed
anchor. Missing relations are collected, sorted, and rejected in one stable
`ValueError`. A true relation keeps the record; a false relation excludes it.
Project policies pass this step without an ancestry relation.

## Runtime Retrieval API

Extend the current signatures:

```python
def candidate_memories(
    self,
    context: MemoryContext,
    *,
    query: str | None = None,
    semantic_scores: Mapping[str, float] | None = None,
    max_candidates: int | None = None,
    minimum_score: float | None = None,
    commit_ancestry: CommitAncestryEvidence | None = None,
) -> list[MemoryItem]: ...


def prepare_memory(
    self,
    context: MemoryContext,
    *,
    task: str,
    query: str | None = None,
    semantic_scores: Mapping[str, float] | None = None,
    max_candidates: int | None = None,
    minimum_score: float | None = None,
    commit_ancestry: CommitAncestryEvidence | None = None,
    context_summary: str = "",
) -> MemoryGateRequest: ...
```

Candidate processing order becomes:

1. validate context and retrieval inputs;
2. construct the existing metadata-eligible candidate set;
3. when evidence is supplied, require and apply commit ancestry;
4. apply keyword filtering or semantic score threshold/ranking;
5. return the established deterministic order;
6. in `prepare_memory()`, run System Gate and build the LLM Gate request.

Ancestry precedes keyword and semantic retrieval because it is an applicability
boundary, not a ranking signal. Semantic scores may name stored memory that is
then excluded by false ancestry; score validation still covers the complete
caller mapping.

When `commit_ancestry` is omitted, no anchor discovery or ancestry filtering is
implicit and all existing results and errors remain unchanged.

Invalid or incomplete evidence fails before `prepare_memory()` increments the
request number or registers a pending request.

## Finalization And Audit

`MemoryGateRequest` remains unchanged. Preparation retains only selected
candidate IDs, the context, gate evidence, prompt, and store token. It does not
retain ancestry evidence.

`finalize_memory()` continues to:

- verify request ownership and one-use state;
- require a trace matching the request context;
- resolve the fixed candidate IDs from current store state;
- rerun System Gate so lifecycle and safety changes fail closed;
- narrow through the LLM decision and append one atomic usage event.

The ancestry anchor for an existing verified case/lesson cannot change through
the store's forward lifecycle methods. A newly added memory is not part of the
already prepared request. Therefore finalization does not need to rerun Git or
retain external evidence.

Usage logs keep the selected candidate IDs and normal gate evidence, but do not
persist the ancestry graph or relation values.

## PR Report API

Extend:

```python
def pr_memory_report(
    self,
    context: MemoryContext,
    *,
    changed_fields: list[str],
    commit_ancestry: CommitAncestryEvidence | None = None,
) -> PRMemoryReport: ...
```

The method first applies its existing context matcher, then optional ancestry
filtering using each case's source `commit_sha`, then existing deterministic
sorting, suggestions, warnings, and provenance rendering.

Missing evidence for a context-matched case fails closed. False evidence
removes that case from related IDs, suggestions, warnings, and provenance.

This does not inspect old/new field values or changed files. Those remain a
separate PR change-set matching extension.

## Internal Refactor

Extract the current metadata candidate construction into one private store
method used by both `candidate_commit_anchors()` and `candidate_memories()`.
This prevents the discovery and application paths from drifting.

Use one private memory-ID-to-anchor resolver:

- lesson ID -> source case `fix_commit_sha`;
- failure-case ID -> case `commit_sha`;
- project-policy ID -> `None`.

Use shared evidence validation/filtering helpers for runtime candidates and PR
case records. Do not add a generic provider abstraction or callback.

## Persistence Compatibility

No persisted contract changes:

- snapshot version remains 2;
- `MemoryGateRequest`, `MemoryUsageLog`, and `PRMemoryReport` fields remain
  unchanged;
- JSON Schemas remain unchanged;
- PostgreSQL schema version remains 1;
- `PostgresMemoryRepository` load and sync remain unchanged;
- active-lessons YAML remains unchanged.

`CommitAncestryEvidence` is an ephemeral API model and is never serialized by
the store.

## Error Handling

Input shape and evidence completeness errors raise stable `ValueError`
messages naming the invalid field or sorted missing commits.

Git execution failures raise `CommitAncestryCaptureError`; a normal
non-ancestor result does not. No Git error is swallowed or converted into a
false relation.

All validation and filtering complete before a gate request or usage event is
mutated.

## Testing

Implementation follows red-green-refactor. Focused tests cover:

- deterministic capture command order, deduplication, true/false exit codes,
  empty anchors, and immutable evidence;
- malformed capture inputs, runner result types, command failures, stderr,
  causes, and no partial result;
- an integration test against a temporary Git DAG with ancestor, equal, and
  unrelated commits;
- exact evidence model validation, context binding, duplicate relations, and
  sorted missing-anchor errors;
- deterministic runtime and PR anchor discovery;
- lesson valid-from anchors, failure-case source anchors, and policy bypass;
- ancestry filtering before keyword and semantic retrieval;
- semantic score validation remaining independent of ancestry exclusion;
- invalid ancestry preparation not consuming a request ID;
- sensitive/obsolete/System Gate and stale-finalization behavior remaining
  authoritative;
- PR IDs, warnings, suggestions, and provenance excluding unrelated history;
- omitted evidence preserving existing retrieval, prompt, report, snapshot,
  PostgreSQL, and README behavior;
- evidence and raw relations never appearing in snapshots or usage logs.

Completion requires focused tests, the full pytest suite, `compileall`,
`git diff --check`, a whole-branch review, merge-result tests on `main`, and a
verified push to `origin/main`.
