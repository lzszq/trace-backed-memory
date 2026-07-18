# Snapshot Operations CLI Implementation Plan

## Goal

Expose the validated snapshot inspection and recovery workflow through a
dependency-free, machine-readable CLI without creating parallel domain logic.

## CLI Core

- Add `cli.py` with JSON stdout/stderr helpers and a JSON-error
  `ArgumentParser`.
- Add `__main__.py` and the `tbm` console entry point.
- Implement snapshot validate/stats, audit, metrics, and remediation reads.
- Implement ready, single, and batch recovery with dry-run default and atomic
  `--write`.
- Preserve decision ordering and strict boolean/attribution parsing.

## Packaging And CI

- Add an explicit setuptools build backend.
- Extend CI to Python 3.11, 3.12, and 3.13.
- Build wheel/sdist and smoke-test both CLI entry points.
- Keep runtime dependencies empty.

## Documentation

- Add a CLI quick start and full command/error contract to README.
- Update the product overview, architecture, usage policy, repository layout,
  and roadmap Phase 25.
- Keep snapshot version 2 and PostgreSQL schema version 1 unchanged.

## Verification

- Run focused CLI, README, schema, and packaging tests.
- Run the complete test suite and source compilation.
- Build wheel/sdist, install the wheel in an isolated target, and smoke-test
  the console/module entry points.
- Review diff, secret patterns, conflict markers, schema hash, process cleanup,
  and remote synchronization before merge and push.
