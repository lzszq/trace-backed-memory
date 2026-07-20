# Packaged Resources Implementation Plan

## Goal

Make every documented canonical Schema, taxonomy, and example available from
installed wheel and source distributions through one strict, zip-safe resource
interface and a dependency-free CLI adapter.

## Contract Tests

- Add `tests/test_resources.py` for allowlist ordering, metadata, byte parity,
  rejection, export atomicity, and public exports.
- Extend extraction tests for the packaged default taxonomy.
- Extend CLI tests for `resource list`, `resource read`, `resource export`,
  structured error classes, overwrite policy, and post-write stdout failure.
- Extend packaging tests to lock package data and typed-package metadata.

## Implementation

- Copy the 18 canonical files into `src/trace_backed_memory/_resources/`.
- Add `resources.py` with `PackagedResource`, `PackagedResourceError`, and the
  three public resource operations.
- Export the interface from the package root and make
  `load_failure_taxonomy()` default to the packaged taxonomy.
- Add `resource list/read/export` as a thin CLI adapter.
- Add `py.typed` and explicit setuptools package-data configuration.

## Documentation

- Update installation and PostgreSQL setup with pip-installed resource export.
- Document the Python resource interface and default taxonomy loading.
- Update architecture, usage policy, product overview, repository layout, and
  roadmap Phase 27.

## Verification

- Run focused resource, extraction, CLI, packaging, README, and schema tests.
- Run the complete suite and source compilation.
- Build wheel and source distribution, inspect their file lists, install each
  in isolation, and smoke-test resources, taxonomy, typing marker, and CLI.
- Review byte parity, staged diff, conflicts, secret patterns, worktree state,
  remote synchronization, and GitHub CI before merge and push.
