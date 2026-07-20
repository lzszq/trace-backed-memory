# Lesson YAML Persistence Integrity Implementation Plan

## Goal

Prevent interrupted local Store saves from damaging an existing file and make
the constrained active-lessons YAML adapter preserve multi-paragraph text.

## Tests First

- Add lesson YAML tests for sibling temporary replacement, file sync, cleanup,
  and byte-identical preservation of an existing destination after failure.
- Add exact round trips for blank lines, leading/trailing LF, indentation, and
  trailing intra-line spaces.
- Lock legacy `>` parsing, active-only export, empty output, and current
  duplicate/provenance failure behavior.
- Extend snapshot save tests to require the shared sync-before-replace path.

## Implementation

- Add one private atomic UTF-8 writer context manager in `store.py`.
- Route `save_json()` and `save_lessons_yaml()` through that writer.
- Emit canonical `lesson_text` with literal `|` blocks and explicit empty block
  lines.
- Preserve block lines during constrained parsing instead of globally deleting
  or stripping them.
- Keep the dependency-free accepted field set and Store validation boundary.

## Documentation

- Update README, product, architecture, usage policy, and roadmap Phase 30 with
  the atomic publication and text-fidelity contract.
- State the LF normalization and limited legacy block-scalar compatibility.
- Keep persistence and packaged-resource versions unchanged.

## Release Verification

- Run focused Store, README, and document contract tests.
- Run the complete suite, source compilation, distribution build, resource
  verification, and installed smoke tests.
- Independently review the parser and failure cleanup paths, inspect repository
  hygiene, merge to `main`, push, and observe every remote CI job.
