# Runtime Cardinality Limits Design

## Summary

Two public runtime boundaries currently accept unbounded caller-controlled
collections. An LLM decision can contain arbitrarily many allowed or blocked
memory IDs before validation builds sets and copies the lists. Git ancestry
capture first materializes every supplied anchor and then starts one Git
process per unique value.

Phase 41 gives both paths fixed, published default bounds. It does not truncate
input or silently omit evidence.

## Memory Decision ID Budget

Each `allowed_memory_ids` and `blocked_memory_ids` input list accepts at most
50 entries. This is the existing `LLM_GATE_MAX_CANDIDATES` budget: one LLM gate
prompt cannot contain more than 50 candidate memories, so a larger response
list cannot be justified by the request.

The length check runs before per-entry validation, duplicate detection, set
construction, or list copying. It applies both when `parse_memory_decision()`
parses JSON/mapping input and when `apply_llm_gate_decision()` receives a
caller-constructed `MemoryDecision` that bypassed the parser.

Internally produced final decisions may contain additional System Gate block
records. Those records were derived from the bounded Store request rather than
supplied by the LLM, so downstream injection validation preserves them without
reapplying the response-list limit.

The canonical `memory_decision.schema.json` adds `maxItems: 50` to both ID
arrays, and the packaged resource copy remains byte-identical to it.

## Commit Anchor Budget

`capture_commit_ancestry()` accepts at most 1,000 submitted anchors, exposed as
`COMMIT_ANCESTRY_MAX_ANCHORS`. The budget counts input entries before
deduplication so an infinite or duplicate-heavy iterable cannot bypass it.

The implementation validates and appends one anchor at a time. It consumes at
most the first 1,001 iterable values, raises before invoking the Git runner on
overflow, and never materializes the remainder. At or below the limit,
existing commit-string validation, deterministic sorting, deduplication,
`GIT_NO_LAZY_FETCH`, error context, and frozen evidence remain unchanged.

## Errors

Oversized decision lists raise:

```text
allowed_memory_ids accepts at most 50 memory IDs
blocked_memory_ids accepts at most 50 memory IDs
```

Oversized ancestry input raises:

```text
anchor_commit_shas accepts at most 1000 commit strings
```

These remain ordinary `ValueError` input failures. Store finalization remains
atomic, and an ancestry overflow starts no Git command.

## Compatibility

Existing calls within the published budgets retain their output, ordering, and
errors. Inputs above the budgets now fail closed instead of consuming
unbounded work. `COMMIT_ANCESTRY_MAX_ANCHORS` is an additive package export.

The memory-decision JSON Schema bytes intentionally change to publish the new
limit; the number and paths of the 18 packaged resources do not change.
Snapshot version 2, PostgreSQL schema version 1, record models, active-lessons
YAML, and PostgreSQL DDL remain unchanged.

