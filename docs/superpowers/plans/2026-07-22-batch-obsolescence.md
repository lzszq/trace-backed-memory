# Atomic Batch Obsolescence Implementation Plan

## Goal

Provide one Store-owned all-or-nothing transition for reviewed memory sets and
expose it as a bounded, deterministic snapshot CLI workflow.

## Tests First

- Add public-model and Store tests for exact request tuples, mixed memory kinds,
  request order, active case-to-lesson cascades, explicit/cascade overlap,
  idempotent replay, and deep-copy results.
- Prove atomic rejection for malformed requests, duplicates, wrong kinds,
  unknown later IDs, and injected validation failure after earlier candidates.
- Add CLI red tests for strict manifest shape and limits, exact output counts,
  one Store batch call, dry-run bytes, `--write`, serialization/write errors,
  closed stdout, and module entry points.
- Add README/product/architecture/usage-policy/roadmap contract assertions and
  installed wheel/sdist smoke coverage.

## Implementation

- Add public `MemoryKind` and frozen `MemoryObsolescenceRequest` records.
- Implement `obsolete_memories()` by resolving and staging every explicit and
  cascaded candidate from the entry state, validating all candidates, then
  committing collection updates once.
- Add the strict bounded `obsolete-batch` manifest loader and parser command.
- Capture only entry statuses and active dependent IDs, call the Store method
  once, and derive manifest-ordered results plus sorted deduplicated impact
  counts without exposing record content.
- Reuse generic serialization-before-write, same-snapshot atomic publication,
  structured errors, and committed stdout-failure behavior.

## Documentation And Compatibility

- Document request format, explicit kinds, all-or-nothing behavior, overlap,
  idempotence, dry-run/`--write`, output counts, and error classes.
- Record roadmap Phase 36 and update product maturity to Phase 0-36.
- Preserve snapshot version 2, every JSON Schema, active-lessons YAML, all 18
  packaged resources, PostgreSQL schema version 1, and existing command/API
  behavior.

## Release Verification

- Run focused model/Store/CLI/documentation tests, compile and diff checks, then
  the full suite.
- Build and verify wheel/sdist artifacts and exercise both installed entry
  points in isolated environments.
- Obtain independent domain, safety, compatibility, test, and packaging
  reviews.
- Merge to `main`, push, observe GitHub Actions, clean the feature worktree,
  and immediately continue to the next repository improvement.
