# Batch Measured Completion CLI Implementation Plan

## Goal

Expose `complete_memory_runs()` to local snapshot operators through one strict
file-backed command while retaining the Store's all-or-nothing semantics.

## Tests First

- Add CLI dry-run and write tests for multiple ordered measured results.
- Cover full optional evidence, omitted fields, explicit null, and explicit
  empty tool outputs.
- Reject unreadable, non-UTF-8, malformed, non-finite, duplicate-key, empty,
  wrong-shape, missing-field, unknown-field, and wrong-type manifests as input
  errors without writing.
- Prove Store state errors, including duplicate/unknown decisions and a later
  incompatible result, roll back the complete batch.
- Extend help, module, console, write-failure, and post-commit stdout coverage.

## Implementation

- Add `complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]` to the existing
  parser.
- Add a strict reusable UTF-8 JSON-file loader with context-specific errors.
- Convert each allowlisted object into an immutable `MemoryRunResult`, including
  list-to-tuple conversion for tool outputs.
- Call `complete_memory_runs()` exactly once and reuse the current completion
  envelope, serialization-before-write flow, exit codes, and broken-pipe rule.

## Documentation And Distribution

- Update README, product, architecture, usage policy, and roadmap Phase 31.
- Keep the manifest documented inline rather than adding a persisted schema.
- Exercise an actual two-result installed-wheel completion in CI.
- Preserve snapshot version 2, JSON Schemas, active-lessons YAML, packaged
  resource bytes, `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Release Verification

- Run focused CLI and documentation tests, bytecode compilation, and the full
  suite.
- Build wheel and sdist, run the distribution verifier, and smoke-test an
  isolated installed wheel.
- Obtain independent implementation, test, and compatibility reviews.
- Inspect repository hygiene, merge to `main`, push, and observe all remote CI
  jobs before starting the next phase.
