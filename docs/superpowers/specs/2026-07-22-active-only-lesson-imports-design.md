# Active-Only Lesson Imports Design

## Problem

`save_lessons_yaml()` exports only active lessons, and the public documentation
describes the portable YAML artifact and CLI import workflow as active-only.
`load_lessons_yaml()` nevertheless delegates to the general Lesson validator,
which correctly accepts both `active` and `obsolete` lifecycle states. A
caller-authored YAML document can therefore import an obsolete lesson into the
Store even though that state is outside the portable artifact contract.

System Gate prevents an obsolete lesson from being injected, but accepting the
record still violates the import interface, pollutes the destination Store, and
makes exported and accepted artifact domains asymmetric.

## Validation Seam

The restriction belongs in `TraceBackedMemoryStore.load_lessons_yaml()`. The
constrained parser, exact Lesson construction, shared-ID checks, provenance
checks, and all-or-nothing staging remain owned by that existing interface.
The CLI continues to call it exactly once and maps its `ValueError` through the
existing structured input-error path.

The general Lesson model and `_validated_lesson_candidate()` continue to accept
both lifecycle states. `add_lesson()`, snapshot reconstruction, PostgreSQL
loading, obsolescence transitions, and metrics need obsolete records and must
not inherit an import-only restriction.

## Import Rule

Each parsed Lesson is validated against the same staged mapping used today.
Before the candidate is added to that mapping, its status must equal `active`.
The first source-ordered non-active candidate raises a deterministic
`ValueError` naming its lesson ID and the active-only requirement.

Earlier valid candidates exist only in the temporary mapping. The Store is
updated once after every candidate succeeds, so a later obsolete record leaves
the Store unchanged. An empty `lessons: []` document remains a valid no-op.

## CLI Behavior

`tbm lessons import SNAPSHOT SOURCE_YAML [--write]` retains its current byte and
record budgets, duplicate-key handling, dry-run behavior, and single Store call.
An obsolete record is an input error with exit code 2. With `--write`, the
snapshot transaction lock is released without calling `save_json()`, and the
source snapshot bytes remain unchanged.

## Compatibility

Canonical exports already contain only active lessons, so valid round trips do
not change. No public signature, dependency, model, snapshot field, JSON Schema,
YAML field, packaged resource, or PostgreSQL DDL changes. Snapshot version
remains 2 and PostgreSQL schema version remains 1.

## Verification

Store tests cover a mixed document whose first record is active and later
record is obsolete, proving source-order rejection and all-or-nothing state.
CLI tests cover explicit `--write`, structured input exit code 2, and unchanged
snapshot bytes. Existing round-trip, empty import, provenance, duplicate, size,
record-count, atomic-write, and snapshot lifecycle tests remain green.
