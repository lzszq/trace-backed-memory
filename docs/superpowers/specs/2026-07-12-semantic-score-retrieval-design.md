# Semantic Score Retrieval Design

## Summary

The store currently narrows metadata-matched memory with optional keyword
overlap. This project adds a dependency-free semantic retrieval boundary for
callers that already own an embedding model, vector index, or learned ranker.
The caller supplies precomputed scores keyed by runtime memory ID; the store
validates those scores, applies metadata scope first, selects a bounded top-k,
and preserves the existing System Gate and LLM Gate workflow.

The store does not compute embeddings, call a model, or persist vectors. A
semantic score is retrieval evidence only. It can select which records reach
the gates, but it can never approve, activate, or inject memory.

## Goals

- Add optional semantic ranking after metadata-first retrieval.
- Keep the core package dependency-free and provider-neutral.
- Bound every semantic candidate set before LLM prompt construction.
- Make score validation and tie-breaking deterministic.
- Preserve System Gate, LLM Gate, stale-state, trace-link, and audit guarantees.
- Keep existing keyword and metadata-only calls behaviorally compatible.
- Avoid snapshot, JSON Schema, and PostgreSQL schema changes.

## Non-goals

- Computing embeddings or similarity inside the store.
- Choosing an embedding provider, model, distance metric, or score scale.
- Adding pgvector, a vector table, an ANN index, or a core dependency.
- Combining keyword and semantic scores into a hybrid rank.
- Persisting raw scores, embeddings, model identity, or index provenance.
- Treating similarity as proof that memory is safe or applicable.
- Changing LLM prompt ordering, decision parsing, or injection rendering.

## Alternatives Considered

### 1. Caller-provided semantic scores (selected)

Accept a mapping from stored memory IDs to finite numeric scores. This supports
embeddings, vector databases, rerankers, and domain-specific retrieval without
running external code while the store lock is held. It introduces no provider
contract, dependency, or persistence migration.

### 2. Built-in TF-IDF or cosine ranking

A dependency-free lexical ranker would be easy to package, but it would mostly
duplicate the existing token-overlap filter and would not provide a genuine
semantic integration point. It would also force the store to define corpus and
normalization behavior that external retrieval systems already own.

### 3. First-party embeddings and pgvector

Embedding and storing memory in PostgreSQL could provide end-to-end vector
search, but it requires provider configuration, an extension and schema
migration, index lifecycle rules, and synchronization semantics. Those choices
are deployment-specific and remain a separate project.

## Public API

Extend both retrieval entry points with the same keyword-only options:

```python
from collections.abc import Mapping


def candidate_memories(
    self,
    context: MemoryContext,
    *,
    query: str | None = None,
    semantic_scores: Mapping[str, float] | None = None,
    max_candidates: int | None = None,
    minimum_score: float | None = None,
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
    context_summary: str = "",
) -> MemoryGateRequest: ...
```

`float` in the public annotation follows Python's numeric typing convention;
at runtime, exact finite `int` and `float` values are accepted. Booleans are
not scores.

Existing calls that omit all three semantic options retain exact metadata-only
or keyword behavior, including memory-ID ordering. The new options do not
change any dataclass or package-root export.

## Retrieval Modes

Each call selects exactly one retrieval mode:

- Metadata-only: `query is None` and `semantic_scores is None`.
- Keyword: `query is not None` and `semantic_scores is None`.
- Semantic: `query is None` and `semantic_scores is not None`.

Supplying both `query` and `semantic_scores` is an error, including a blank or
punctuation-only query. The explicit arguments select the mode; tokenization
does not silently turn a requested hybrid call into semantic mode.

`max_candidates` and `minimum_score` are semantic-only options. Either option
without `semantic_scores` is rejected. Semantic mode requires an explicit
`max_candidates` so every caller intentionally chooses a retrieval budget.

## Validation Contract

Validation occurs before candidate selection and before `prepare_memory()`
allocates or registers a gate request.

- `semantic_scores` must implement `Mapping`.
- Every score key must be a non-empty string no longer than
  `MEMORY_ID_MAX_CHARS`.
- Every score key must identify a stored failure case, lesson, or project
  policy. Trace IDs and stale external-index IDs are rejected.
- Every score value must be an exact finite `int` or `float`; booleans, NaN,
  infinities, strings, decimals, and custom numeric objects are rejected.
- `max_candidates` must be an exact integer from 1 through
  `LLM_GATE_MAX_CANDIDATES`; booleans are rejected.
- `minimum_score`, when supplied, must be an exact finite `int` or `float`.
  It has no fixed range because cosine similarity, distance transforms, and
  learned rankers use different scales.
- The caller must orient every score so a larger value means a more relevant
  record. Distance-based systems must transform or negate raw distances before
  calling the store.

An empty score mapping is valid and yields no candidates. All supplied entries
are validated, including entries whose memory later fails metadata matching.
This makes a stale or malformed external index fail visibly instead of being
accepted only for some contexts.

Errors use stable `ValueError` messages naming the invalid option or memory ID.
The store does not retain the caller's mapping after the method returns.

## Selection Algorithm

The existing metadata candidate construction remains authoritative:

1. Validate `MemoryContext` and retrieval options.
2. Gather lessons and project policies whose entire declared scope matches.
3. In debug or repair mode, also gather verified, regression-backed failure
   cases whose source scope and tool constraints match.
4. Apply the selected retrieval mode.

Metadata-only and keyword modes keep their current algorithm and final
memory-ID sort.

Semantic mode processes only metadata-eligible candidates:

1. Exclude candidates with no supplied score.
2. Exclude candidates whose score is below `minimum_score`, when present.
3. Stream candidates through bounded top-k selection keyed by score descending,
   then `memory_id` ascending for stable ties.
4. Return the selected records in that exact key order without a full sort.

Scores for valid stored records that are not metadata-eligible in the current
context are ignored. Unscored eligible records are excluded. Negative scores
and duplicate score values are valid; larger scores always rank first.

Stored-ID validation uses a non-copying membership view over the three runtime
catalogs only when semantic scores are present. Metadata-only and keyword calls
do not build or iterate a separate ID universe. With `K` eligible candidates and
`k <= 50` requested results, semantic ranking is `O(K log k)` time and `O(k)`
ranking storage.

The returned semantic candidate order is the rank order. `prepare_memory()`
preserves that order in `MemoryGateRequest.candidate_memory_ids` and subsequent
usage-log candidate evidence. The existing LLM prompt builder may continue to
render selected records in canonical memory-ID order; semantic ranking controls
selection, not prompt-position preference.

## Gate And Audit Integration

After semantic top-k selection, `prepare_memory()` follows the unchanged safe
workflow:

1. System Gate evaluates every selected candidate.
2. Only System Gate allowed records enter the LLM Gate prompt.
3. `finalize_memory()` resolves the selected IDs from current store state,
   reruns System Gate, intersects the LLM decision, renders the requested
   injection mode, and appends one trace-linked usage event.

A sensitive, eval-leaking, obsolete, low-confidence, cross-mode, or otherwise
blocked record remains blocked regardless of score. The semantic score is not
passed to the LLM Gate as authority and is not available to decision parsing.

The selected candidate IDs and their order remain existing audit evidence.
Raw scores are intentionally not persisted because doing so would change the
snapshot, JSON Schema, PostgreSQL schema, adapter synchronization, and legacy
migration contract. Score/model/index provenance can be designed as a separate
versioned audit extension.

## Concurrency And Ownership

The caller computes scores before entering the store. The store never invokes
a scorer callback, model, network client, or database index while holding its
`RLock`. It validates and copies the score entries during the synchronized
call, then discards that copy on return.

Pending requests continue to retain immutable candidate IDs rather than the
external score mapping. Mutating the caller's mapping after preparation cannot
change a registered request or finalization behavior.

## Persistence Compatibility

No persisted model changes:

- snapshot version remains 2;
- memory and full-store JSON Schemas remain unchanged;
- `public.memory_usage_logs` remains unchanged;
- `PostgresMemoryRepository` load and sync behavior remains unchanged;
- active-lessons YAML remains unchanged.

Existing snapshots and PostgreSQL databases therefore require no migration.

## Testing

Implementation follows red-green-refactor. Focused tests cover:

- unchanged metadata-only and keyword results;
- rejection of keyword-plus-semantic hybrid calls;
- mapping, key, unknown-ID, score, limit, and threshold validation;
- validation failures not consuming a gate request ID;
- metadata filtering before semantic ranking;
- exclusion of unscored and below-threshold candidates;
- descending scores, stable ID tie-breaking, and exact top-k behavior;
- ranked candidate IDs flowing through preparation and usage audit evidence;
- high-scoring sensitive and obsolete records still blocked by System Gate;
- caller mapping mutation not changing a pending request;
- unchanged snapshot and PostgreSQL round trips;
- README, architecture, usage policy, and roadmap examples and claims.

Completion requires focused tests, the full pytest suite, compile checks, and
`git diff --check` from the feature worktree.
