# Memory Obsolescence CLI Implementation Plan

## Goal

Expose safe, forward-only Store obsolescence as a deterministic snapshot CLI
preview with explicit atomic publication.

## Tests First

- Add CLI fixtures with draft/verified/obsolete failure cases, multiple active
  and obsolete dependent lessons, unrelated lessons, and active/obsolete
  project policies.
- Add command tests for each kind, exact output, sorted cascade IDs, dry-run
  isolation, `--write`, idempotent replay, unknown and invalid IDs/kinds, and
  exactly one Store transition call.
- Add failure tests for transition rejection, JSON serialization, snapshot
  publication, and stdout closure before and after persistence.
- Add module entry-point, README contract, and installed artifact smoke tests.

## Implementation

- Add `obsolete SNAPSHOT MEMORY_KIND MEMORY_ID [--write]` with three explicit
  CLI kind choices.
- Capture only statuses and active dependent IDs needed for the result, then
  delegate exactly once to the selected Store obsolescence method.
- Emit a canonical, non-sensitive envelope with change and cascade details.
- Reuse the existing generic dry-run, serialization-before-write, atomic
  `save_json()`, structured error, and committed stdout-failure behavior.

## Documentation And Compatibility

- Document irreversible forward-only status behavior, case-to-lesson cascade,
  idempotence, preview/`--write`, output, exit codes, and no batch/reactivation
  boundary in README, product, architecture, usage policy, and roadmap Phase
  35.
- Preserve snapshot version 2, all JSON Schemas, active-lessons YAML, all 18
  packaged resources, `schemas/postgres.sql`, PostgreSQL schema version 1, and
  public Python exports.

## Release Verification

- Run focused CLI/documentation tests, compilation, diff checks, and the full
  suite.
- Build and verify wheel/sdist artifacts; install and exercise obsolescence
  through console and module entry points in isolated environments.
- Obtain independent domain, safety, compatibility, and packaging reviews.
- Merge to `main`, push, observe GitHub Actions, clean the feature worktree,
  and continue to the next repository improvement.
