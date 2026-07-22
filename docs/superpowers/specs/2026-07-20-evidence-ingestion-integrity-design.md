# Evidence Ingestion Integrity Design

Status: accepted for Phase 28 implementation

## Problem

Two dependency-free ingestion paths currently lose evidence integrity:

1. Failure extraction inspects `Trace.error` and top-level `name`/`error`
   fields in `Trace.tool_calls`, but ignores explicit top-level errors in
   `Trace.tool_outputs`. A tool-output-only failure therefore falls through to
   a generic evaluator mismatch and loses its useful root cause.
2. The active-lessons YAML adapter assigns parsed fields directly into Python
   dictionaries. Repeated record or scope keys silently replace earlier
   values. The failure-taxonomy adapter already rejects repeated IDs but still
   lets a repeated description replace the first description.

Both behaviors are dangerous for a trace-backed system: captured evidence may
be ignored, while edited evidence may be silently rewritten during parsing.

## Goals

- Treat explicit tool-output errors as first-class failure evidence.
- Preserve existing classifier precedence and existing tool-call behavior.
- Reject duplicate keys in the two supported simple YAML formats.
- Fail lesson YAML parsing before mutating Store state.
- Keep the package dependency-free and preserve every persisted schema version.

## Non-goals

- General YAML support, aliases, tags, comments, or arbitrary YAML documents.
- Searching arbitrary tool-output payload text for failure keywords.
- Recursive interpretation of provider-specific result envelopes.
- A new failure taxonomy or changes to classifier keyword precedence.
- Snapshot, JSON Schema, active-lessons shape, or PostgreSQL schema changes.

## Alternatives Considered

### Minimal structured evidence integration

Read top-level `name` and `error` fields from tool calls, followed by explicit
top-level `error` fields from tool outputs. Add local duplicate checks to the
two existing parsers.

This is the selected design. It fixes the reproduced failures without treating
ordinary output content as error evidence or expanding the supported formats.

### Recursive evidence discovery and a shared parser framework

Walk every nested tool-output value looking for error-like keys and introduce a
generic duplicate-aware parser abstraction.

This was rejected. Provider payloads can contain quoted examples, historical
errors, or domain data named `error`; recursive keyword search would add false
positives. The two YAML grammars are small but structurally different, so a
shared framework would obscure their contracts.

### Adopt a third-party YAML implementation

This was rejected for this phase. It would add a core dependency and broaden
accepted syntax well beyond the deliberately constrained repository formats.

## Failure Evidence Contract

Failure extraction uses evidence in this order:

1. `Trace.error`;
2. each `Trace.tool_calls` item in stored order;
3. each `Trace.tool_outputs` item in stored order.

Non-null top-level `name` and `error` values from tool calls contribute to
classification text, preserving existing behavior. From tool outputs, only a
non-null top-level `error` contributes. Output names may label a symptom when
that same output has a non-empty error, but a successful output name cannot
change classification. Values keep the existing defensive string conversion
behavior.

Classifier keyword precedence does not change. In particular, missing context
continues to outrank an invalid argument, and a failed trace without recognized
evidence continues to fall back to `evaluator_mismatch`.

Symptoms continue to prefer named tool calls. When no tool call has a name,
only a named tool output with a non-empty top-level `error` provides the same
deterministic `tool call failed for ...` summary. A successful named output must
not be described as failed. Root cause continues to prefer `Trace.error`, then
the first tool-call error, and now the first tool-output error. Arbitrary output
fields and nested payload text never affect classification.

## Strict YAML Contract

The failure taxonomy continues to require one non-empty `id` followed by one
non-empty `description`. A repeated ID or a second description for the current
ID raises `ValueError`; no value is silently replaced.

Each lessons YAML record may define a top-level field once. Its `scope` mapping
may define each key once. Exact repeated keys raise `ValueError` with the key in
the message. The complete YAML document is parsed, every `Lesson` is
constructed, and all candidates are validated against a staged lesson catalog
before any record is committed. Duplicate keys, duplicate lesson IDs,
constructor failures, and provenance or lesson-contract failures are therefore
all-or-nothing and leave the Store unchanged.

The accepted YAML shape, scalar conversion, block text handling, provenance
checks, and active-only export remain unchanged.

## Compatibility

Valid canonical and caller-owned files retain their current results. Files
that relied on last-key-wins behavior now fail explicitly; this is an intended
integrity correction. No record model changes, no new package dependency, and
no snapshot or database migration are introduced.

## Phase 69 Supersession

Phase 69 supersedes only the earlier classification rule for tool-call names.
Current classification uses explicit failure text from `Trace.error` and
top-level call/output `error` values; tool names never select a taxonomy entry.
Errored call and output names remain available as symptom labels. Snapshot
version 2, PostgreSQL schema version 1, and packaged resources remain
unchanged.

## Verification

Tests must cover:

- tool-output-only invalid argument classification;
- failure-case symptom and root cause derived from tool outputs;
- existing trace-error and tool-call precedence;
- arbitrary non-error output text not influencing classification;
- duplicate taxonomy descriptions;
- duplicate lesson record and scope keys, including no Store mutation;
- duplicate lesson IDs and later semantic failures with all-or-nothing import;
- canonical taxonomy and active-lessons round trips;
- unchanged snapshot version 2, JSON Schemas, packaged resources, and
  PostgreSQL schema version 1.
