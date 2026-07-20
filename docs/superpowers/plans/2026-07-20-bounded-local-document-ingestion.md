# Bounded Local Document Ingestion Implementation Plan

## Goal

Place explicit resource ceilings around every caller-owned local JSON/YAML read
without weakening existing semantic validation or all-or-nothing mutation.

## Tests First

- Test the shared binary reader at exact byte limits, one byte over, multibyte
  UTF-8 boundaries, invalid limits, decode failures, and opt-out.
- Add snapshot byte, per-collection, total-record, override, and legacy/v2
  compatibility tests.
- Add lesson and taxonomy byte/record rejection tests, including unchanged Store
  state and the packaged default taxonomy.
- Add CLI measurement/tool-output byte, top-level item, node, and depth tests;
  require input exit 2 and unchanged snapshots.

## Implementation

- Add a private single-handle bounded UTF-8 reader and byte decoder.
- Route Store snapshot/lesson reads, custom taxonomy reads, and CLI JSON reads
  through it while preserving caller-specific exception normalization.
- Preflight all snapshot collection counts before constructing records.
- Stop constrained YAML parsers when their record limits are exceeded.
- Extend the CLI's iterative JSON walk with node and depth budgets and cap both
  supported top-level arrays.

## Documentation And Compatibility

- Publish defaults, Python overrides, CLI fixed budgets, and trusted-input
  guidance in README, product, architecture, usage policy, and roadmap Phase 32.
- Keep schema/version/resource bytes unchanged and add contract tests proving it.

## Release Verification

- Run focused ingestion, Store, extraction, CLI, and documentation tests, then
  compilation and the complete suite.
- Build and verify wheel/sdist artifacts and smoke-test bounded reads from an
  isolated install.
- Obtain independent correctness, compatibility, and adversarial-input reviews.
- Merge to `main`, push, and observe remote CI before continuing.
