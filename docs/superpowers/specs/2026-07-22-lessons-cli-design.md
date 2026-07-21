# Active Lessons CLI Design

## Problem

The Store already has a bounded, provenance-validating, all-or-nothing YAML
adapter for portable lessons, but operators must write Python glue to export or
import it. Add a small CLI surface that exposes the existing adapter without
creating a second parser, merge policy, or persistence format.

## Commands

Expose:

```text
tbm lessons export SNAPSHOT DESTINATION [--overwrite]
tbm lessons import SNAPSHOT SOURCE_YAML [--write]
```

Both commands load `SNAPSHOT` through the existing bounded snapshot loader.
They accept local paths only and do not read stdin, remote URLs, packaged
resources, or PostgreSQL connections. The `lessons` namespace keeps the two
directions explicit and leaves the existing top-level snapshot and resource
commands unchanged.

## Export

`lessons export` delegates canonical serialization to
`TraceBackedMemoryStore.save_lessons_yaml()`. It exports active lessons only,
in Store order, with quoted string scalars, canonical LF delimiters, and
literal `lesson_text: |` blocks. The snapshot is never mutated or saved.

The destination is caller-owned. By default, publication must fail if any
filesystem entry already exists there. `--overwrite` explicitly permits
atomic replacement. Extend `save_lessons_yaml()` with a backward-compatible
keyword-only `overwrite` argument whose default remains `True` for existing
Python callers; the CLI always passes its explicit flag. The common sibling
temporary-file writer keeps its write, flush, `fsync`, and cleanup behavior.
It publishes with `os.link()` when replacement is forbidden and `os.replace()`
when replacement is allowed, so the no-overwrite check and publication are one
filesystem operation rather than a racy pre-check. `save_json()` continues to
use replacement behavior.

Success emits:

```json
{
  "destination": "lessons.active.yaml",
  "exported_count": 2,
  "exported_lesson_ids": ["lesson-1", "lesson-2"],
  "overwrite": false
}
```

The IDs describe the active records actually selected for serialization.
Empty stores export the canonical `lessons: []` document and a zero count.

## Import

`lessons import` calls `load_lessons_yaml()` exactly once with its fixed safe
defaults: 8 MiB and 10,000 records. It does not expose the Python API's trusted
offline limit opt-outs. The Store remains authoritative for the constrained
YAML grammar, duplicate record and scope keys, exact Lesson construction,
shared memory-ID collisions, source Trace and verified/regression-backed case
provenance, scope contracts, and all-or-nothing staging.

Import is a merge, not replacement or upsert. IDs already present in the
snapshot are rejected even when their values are identical. An empty canonical
document is a valid no-op. Legacy `>` lesson text remains accepted with the
adapter's existing literal-line behavior; the CLI does not claim support for
general YAML tags, anchors, folding, or chomping.

The command mutates only the reconstructed in-memory Store by default, which
makes it a full validation dry-run. `--write` is the sole publication gate and
calls `save_json()` on the same snapshot only after every lesson succeeds.
Success emits:

```json
{
  "imported_count": 2,
  "imported_lesson_ids": ["lesson-1", "lesson-2"],
  "written": false
}
```

The IDs preserve source-document order. No imported YAML document or command
metadata is stored.

## Output, Errors, And Durability

Success is one canonical JSON value plus a newline. The result is serialized
before either destination publication or snapshot replacement. Path, UTF-8,
size, YAML shape, duplicate, provenance, scope, collision, and other import
validation failures are input errors with exit code 2. Export destination and
snapshot persistence failures are write errors with exit code 4. Unexpected
failures use exit code 1. These commands do not use state exit code 3.

An export that has published its destination, or an import whose requested
snapshot write has committed, remains successful if stdout closes afterward;
retrying either committed operation could otherwise produce a false conflict.
A dry-run import still reports stdout failure as internal because it committed
nothing. Existing error truncation and structured stderr JSON remain unchanged.

## Compatibility And Non-Goals

This phase adds a CLI adapter and an additive no-replace option to the existing
lesson writer. It does not add lesson creation, editing, replacement, deletion,
obsolescence, directory creation, PostgreSQL import, or a general YAML parser.
It changes no Lesson field, YAML record shape, snapshot record, public export,
or database column. Snapshot version 2, all JSON Schemas, the 18 packaged
resources and their bytes, `schemas/postgres.sql`, and PostgreSQL schema
version 1 remain unchanged.

## Verification

Tests cover active-only and empty export, canonical multiline bytes, safe
no-overwrite and explicit replacement, temporary-file cleanup, dry-run import,
persisted import, merge conflicts, provenance and duplicate rejection,
malformed UTF-8, missing paths, byte and record ceilings, unchanged snapshots,
structured exit codes, post-publication stdout failures, module and installed
entry points, documentation, the full suite, and isolated wheel/source-
distribution smoke tests.
