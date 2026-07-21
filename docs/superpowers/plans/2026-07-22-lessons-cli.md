# Active Lessons CLI Implementation Plan

## Goal

Expose the Store's existing active-lessons YAML portability boundary as safe,
deterministic import and export CLI workflows.

## Tests First

- Add Store tests for backward-compatible replacement and atomic
  `overwrite=False` publication, including existing destinations and cleanup.
- Add CLI tests for active-only and empty exports, deterministic result
  envelopes, refusal to replace, explicit overwrite, unchanged snapshots, and
  export write failures.
- Add CLI tests for dry-run and persisted imports, source order, empty input,
  duplicate/provenance/UTF-8/path failures, fixed byte and record limits,
  all-or-nothing behavior, and post-commit stdout handling.
- Add parser/help, module entry-point, README contract, and installed artifact
  smoke coverage.

## Implementation

- Add the `lessons export` and `lessons import` parser surfaces with distinct
  `--overwrite` and `--write` publication gates.
- Extend the shared Store writer and `save_lessons_yaml()` with an additive
  no-replace path while preserving existing Python-call replacement semantics.
- Delegate export selection/serialization and import parsing/validation to the
  Store; do not duplicate YAML or provenance logic in the CLI.
- Classify import document/semantic failures as input errors and destination or
  snapshot publication failures as write errors.
- Serialize output before publication and preserve committed success after a
  downstream stdout closure.

## Documentation And Compatibility

- Document commands, dry-run/overwrite behavior, fixed ingestion budgets,
  merge semantics, outputs, errors, and constrained YAML behavior in README,
  product, architecture, usage policy, and roadmap Phase 34.
- Preserve snapshot version 2, all JSON Schemas, active-lessons YAML shape,
  all 18 packaged resources, `schemas/postgres.sql`, PostgreSQL schema version
  1, and public Python exports.

## Release Verification

- Run focused Store/CLI/documentation tests, compilation, diff checks, and the
  full suite.
- Build and verify wheel/sdist artifacts; install and exercise both lesson
  commands through console and module entry points in isolated environments.
- Obtain independent domain, filesystem-safety, compatibility, and packaging
  reviews.
- Merge to `main`, push, observe GitHub Actions, clean the feature worktree,
  and continue to the next repository improvement.
