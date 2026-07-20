# PR Report CLI Implementation Plan

## Goal

Expose the existing endpoint-aware PR report as a strict, read-only,
Git-ancestry-aware CLI workflow suitable for CI.

## Tests First

- Add context and change-set JSON loader tests for exact valid shapes,
  missing/unknown/wrong fields, nullable endpoints, common byte/node/depth/item
  budgets, and domain validation.
- Add command tests for anchor discovery, ancestry capture, positive/negative
  filtering, deterministic output, unchanged snapshots, structured input/state
  failures, and broken stdout.
- Add a capture test requiring `--` before revision values and module/installed
  entry-point smoke coverage.

## Implementation

- Add the `pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path
  REPO_PATH` parser surface without `--write`.
- Parse strict JSON into validated `MemoryContext` and immutable `PRChangeSet`
  objects while reusing the Phase 32 ingestion budgets.
- Reuse the exact change-set instance across Store anchor discovery and report
  generation, with external Git capture between the two Store calls.
- Serialize `CommitAncestryEvidence` and `PRMemoryReport` in one canonical JSON
  envelope and classify Git capture failures as state errors.
- Add Git's option terminator before ancestry revisions.

## Documentation And Compatibility

- Document the command, input files, output envelope, fail-closed Git behavior,
  exit codes, and read-only contract in README, product, architecture, usage
  policy, and roadmap Phase 33.
- Preserve snapshot version 2, all JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, packaged resource bytes, PostgreSQL schema version 1,
  and public Python exports.

## Release Verification

- Run focused CLI/capture/documentation tests, compilation, and the full suite.
- Build and verify wheel/sdist artifacts; install and exercise `pr-report` from
  each artifact in isolated environments.
- Obtain independent correctness, security, compatibility, and CI workflow
  reviews.
- Merge to `main`, push, observe GitHub Actions, clean the feature worktree,
  and continue to the next repository improvement.
