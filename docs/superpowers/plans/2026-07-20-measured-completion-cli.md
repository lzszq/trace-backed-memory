# Measured Completion CLI Implementation Plan

## Goal

Expose the existing atomic single-run completion boundary to local snapshot
operators without duplicating completion rules or weakening dry-run safety.

## CLI Contract

- Add `complete` with required snapshot, trace ID, decision ID, and measured
  `pass`, `fail`, or `error` outcome.
- Add optional failure attribution and scalar trace evidence flags.
- Load tool outputs from a strict UTF-8 JSON array-of-objects file.
- Preserve omitted evidence by forwarding only options that were supplied.
- Reuse the deterministic recovery completion envelope and JSON errors.

## Persistence

- Keep dry run as the default.
- Use the existing snapshot serializer and same-path atomic replacement only
  after the completion and output serialization both succeed.
- Preserve exit codes 0/1/2/3/4 and post-commit stdout behavior.
- Keep snapshot version 2 and PostgreSQL schema version 1 unchanged.

## Tests First

- Add focused CLI tests for success, complete evidence, omission, explicit
  empty outputs, attribution, replay, linkage/state failures, malformed files,
  non-finite costs, persistence isolation, write failure, and stdout failure.
- Extend module and installed-console smoke tests with the new help surface.
- Add README and schema-boundary assertions where existing contract tests
  require them.

## Documentation

- Add the command and structured-evidence rules to README.
- Update product, architecture, usage policy, repository layout, roadmap, and
  test inventory documents for the measured completion workflow.

## Release Verification

- Run focused CLI and documentation tests, then the complete suite and source
  compilation.
- Build wheel and sdist, run the distribution verifier, and smoke-test the
  installed console and module entry points.
- Independently review behavior and tests, inspect the final diff and repository
  hygiene, merge to `main`, push, and observe every remote CI job.
