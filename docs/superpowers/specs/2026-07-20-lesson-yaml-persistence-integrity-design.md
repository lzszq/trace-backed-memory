# Lesson YAML Persistence Integrity Design

## Problem

`save_lessons_yaml()` currently writes directly to its destination. A process,
filesystem, or encoding failure after truncation can destroy a previously valid
active-lessons file. Snapshot saving already uses a sibling temporary file and
replacement, but does not sync the temporary file before publication.

The constrained YAML parser also removes every blank physical line and strips
the completed lesson-text block. A valid multi-paragraph `lesson_text` therefore
loses blank lines and leading or trailing line breaks during a save/load round
trip even though snapshots preserve the original string.

Make local Store persistence fail without damaging the prior file and make the
canonical lesson YAML representation preserve LF-delimited lesson text.

## Alternatives

### A. Shared atomic UTF-8 writer and literal blocks

Use one internal context manager for sibling temporary creation, flushing,
file synchronization, replacement, and cleanup. Route both snapshot and lesson
YAML saves through it. Emit lesson text with a literal `|` block and retain
blank block lines while parsing. This centralizes the publication invariant and
keeps lesson files readable. This is the selected design.

### B. JSON-quoted lesson text on one YAML line

The existing scalar parser can round-trip escaped newlines exactly, but every
multi-line lesson becomes difficult for humans to review and edit. JSON quoting
remains available for caller-authored scalar values but is not the canonical
serializer output.

### C. General-purpose YAML dependency

A third-party YAML library could implement the complete block-scalar standard,
but it would add a core runtime dependency and widen a deliberately constrained
format. The repository needs a precise small contract, not arbitrary YAML.

## Atomic Publication

Add a private UTF-8 text-writer context manager with this order:

1. Create a uniquely named sibling temporary file in the destination directory.
2. Let the caller write the complete canonical document.
3. Flush Python buffers and call `os.fsync()` on the temporary file.
4. Close the file and atomically replace the destination with `os.replace()`.

Any exception before successful replacement removes the temporary sibling and
leaves an existing destination byte-for-byte unchanged. Cleanup errors do not
hide the original failure. Serialization happens while the Store lock is held,
matching the current save methods. Missing parent directories and invalid paths
still fail rather than being created implicitly.

`save_json()` moves to the same writer without changing its sorted, indented,
strict JSON bytes. It gains the same pre-publication file synchronization.
`save_lessons_yaml()` continues to include active lessons only and preserves
their Store iteration order.

## Lesson Text Contract

Canonical output uses:

```yaml
    lesson_text: |
      First paragraph.

      Second paragraph.
```

The serializer splits on LF rather than using `splitlines()`, so leading,
interior, and trailing LF characters are represented by block lines. Every
line receives six spaces of block indentation, including an empty line.
Intra-line leading and trailing spaces are not stripped.

The parser stops globally discarding blank lines. While a `lesson_text` block
is active, indented content and blank physical lines are appended in order;
finishing the block joins them with LF without stripping. Blank lines outside a
block remain insignificant. Existing `lesson_text: >` and `lesson_text: |`
inputs stay accepted and retain the adapter's historical literal-line behavior;
this phase does not claim general YAML folding or chomping support.

`Path.read_text()` continues to apply Python's universal-newline behavior, so
the canonical in-memory line separator is LF. Quoted scalar input remains
supported for strings that require explicit escaped characters.

## Failure And Compatibility Contract

- An `fsync`, close, or replace failure leaves the old destination unchanged.
- A failed save leaves no sibling temporary file when cleanup is possible.
- A successful save publishes one complete document and leaves no temporary
  sibling.
- Empty active-lesson sets still serialize as `lessons: []` plus one newline.
- Loader duplicate-key, provenance, validation, and all-or-nothing Store commit
  behavior remains unchanged.
- Snapshot version 2, JSON Schemas, the accepted active-lessons YAML fields,
  packaged resource bytes, and PostgreSQL schema version 1 remain unchanged.

## Verification

Tests cover sibling replacement, flush/fsync ordering, existing-target
preservation after sync or replace failure, temporary cleanup, unchanged JSON
snapshot bytes, empty lesson documents, active-only export, multi-paragraph and
leading/trailing LF round trips, intra-line whitespace, legacy `>` input,
duplicate and semantic failure atomicity, documentation, and the full suite.
