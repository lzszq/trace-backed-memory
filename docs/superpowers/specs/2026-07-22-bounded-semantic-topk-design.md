# Bounded Semantic Top-K Design

**Status:** Accepted for Phase 63 implementation

## Problem

`TraceBackedMemoryStore.candidate_memories()` currently builds a set containing
every stored failure-case, lesson, and policy ID for every call, including
metadata-only and keyword retrieval. Semantic retrieval then materializes every
eligible scored candidate, sorts the complete list, and slices the requested
one-to-fifty results.

The metadata scan remains required by the current in-memory Store, but the ID
set allocation and full semantic ranking are avoidable.

## Compatibility Boundary

Phase 63 changes no public signature, exception type or message, validation
order, filter order, returned `MemoryItem`, or rank order. It preserves:

- context and query type validation before semantic option validation;
- complete semantic mapping validation before ancestry and metadata work;
- lexicographically sorted unknown-ID diagnostics across all three catalogs;
- metadata filtering before ancestry filtering and semantic ranking;
- `minimum_score` inclusivity;
- score-descending and memory-ID-ascending result order;
- the one-through-fifty `max_candidates` boundary;
- the Store lock across validation, selection, and result construction.

Snapshot version 2, PostgreSQL schema version 1, active-lessons YAML, packaged
resources, and caller-owned score mappings remain unchanged.

## ID Membership

When `semantic_scores is None`, `_validated_semantic_scores()` receives no
catalog view. It can validate the absence of semantic-only options and return
without iterating any stored ID catalog.

When semantic scores are present, construct a `collections.ChainMap` over the
failure-case, lesson, and policy dictionaries. Unknown-ID validation checks each
already validated score key for membership in that view. It does not iterate or
copy the complete catalogs. Unknown IDs are still sorted before the existing
error is raised.

## Bounded Ranking

After unchanged metadata and ancestry filtering, stream eligible scored
candidates through a generator. Use `heapq.nsmallest()` with the exact key
`(-score, memory_id)` and the validated `max_candidates` value.

`nsmallest()` consumes all eligible candidates, so no validation or eligibility
decision is skipped. It returns selected elements in key order, which is exactly
the existing score-descending, memory-ID-ascending order. Built-in finite int
and float scores remain the only accepted numeric values, and runtime memory IDs
are unique, so heap comparison does not introduce a new tie contract.

For `S` stored memories, `M` supplied scores, `K` eligible scored candidates,
and `k <= 50` requested results:

- catalog membership changes from `Theta(S)` temporary construction to
  `Theta(1)` view construction plus `Theta(M)` membership checks;
- semantic ranking changes from `Theta(K log K)` time and `Theta(K)` ranking
  storage to `Theta(K log k)` time and `Theta(k)` ranking storage;
- the authoritative metadata scan remains `Theta(S)`.

## Verification

Tests must prove that metadata-only and invalid semantic-option calls do not
construct a semantic catalog view; semantic unknown-ID checks do not iterate the
complete dictionaries; ranking receives a single-pass iterator and the bounded
limit; ties, thresholds, filtering, ancestry, validation errors, and maximum
limit output remain unchanged.

The complete suite, distribution verification, installed-wheel smoke, and
remote Python, PostgreSQL, and Windows jobs must remain green.
