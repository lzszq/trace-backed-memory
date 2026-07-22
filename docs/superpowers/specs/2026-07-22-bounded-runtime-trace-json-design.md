# Bounded Runtime Trace JSON Design

**Status:** Accepted for Phase 65 implementation

## Problem

`Trace.retrieved_context`, `Trace.tool_calls`, and `Trace.tool_outputs` accept
caller-owned JSON object lists. The Store currently validates JSON value types,
finite numbers, integer serializability, cycles, and depth, but each top-level
object is checked with a fresh traversal and no aggregate node or text budget.
A direct Python caller can therefore make validation, traversal-stack growth,
`deepcopy()`, snapshot serialization, and PostgreSQL persistence process an
unbounded structured payload even though CLI document ingestion is bounded.

## Runtime Contract

Treat all three structured Trace fields as one validation domain with these
fixed limits:

- `TRACE_JSON_MAX_NODES = 100_000` JSON semantic values in aggregate, including
  the three top-level lists, their object/list containers, and scalar values;
- `TRACE_JSON_MAX_TEXT_BYTES = 8 * 1024 * 1024` aggregate UTF-8 bytes across
  every object key and string value;
- the existing `TRACE_JSON_MAX_DEPTH = 100` per structured value.

The node and text budgets are Store runtime guards, not persisted fields or
caller-configurable settings. They apply to `record_trace()`, completion paths,
snapshot import, and PostgreSQL load because those paths converge on
`_validate_trace()`.

## Validation Order And Resource Bounds

Create one budget for `_validate_trace()` and pass it through the fields in
their persisted order: `retrieved_context`, `tool_calls`, then `tool_outputs`.
Validate the outer field as a list of exact dictionaries before traversing its
children, preserving existing type diagnostics.

Count a node when it is visited. Before pushing a list or dictionary's children
onto the traversal stack, compare its cardinality with the remaining node
budget. This lower-bound check rejects oversized wide containers before a
second proportional stack or `dict.items()` list is allocated. Actual child
visits still consume the shared budget and catch aggregate overflow across
sibling containers and fields.

Count keys before constructing their diagnostic child paths. For each key or
string value, reject immediately when its character count already exceeds the
remaining byte budget, then measure UTF-8 bytes. This keeps encoding allocation
bounded and rejects lone surrogates with a path-specific UTF-8 error before
storage or publication.

## Errors

Overflow raises `ValueError` before caller-owned values are copied:

- `trace JSON contains more than 100000 nodes`
- `trace JSON text exceeds 8388608 UTF-8 bytes at <path>`

Non-UTF-8-encodable keys or string values raise:

- `trace <path> must be UTF-8 encodable`

Existing path-specific errors for type, object-key, finite-number, integer,
cycle, and depth failures remain unchanged.

## Compatibility

This deliberately rejects only Trace structured payloads beyond the new fixed
runtime boundary. Accepted records retain their values, ordering, copy
isolation, and serialized bytes. No public signature, dependency, model field,
snapshot field, JSON Schema, active-lessons YAML, packaged resource,
PostgreSQL DDL, snapshot version 2, or PostgreSQL schema version 1 changes.

## Verification

Tests must pin exact default constants; accept exact aggregate node/text
boundaries; reject one unit above across multiple fields; reject wide
containers before copy; count non-ASCII UTF-8 bytes and object keys; reject
lone surrogates; preserve completion atomicity and snapshot-import behavior;
and keep existing nested JSON, full-suite, distribution, and remote
Windows/POSIX/PostgreSQL coverage green.
