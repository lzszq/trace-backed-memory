# Bounded Local Document Ingestion Design

## Problem

Caller-owned snapshots, active-lessons YAML, custom failure taxonomies, and CLI
JSON evidence are read completely before their existing semantic validators run.
A very large local file can therefore consume unbounded memory and validation
time even though record content, JSON depth in Trace fields, and mutation
atomicity are otherwise strict.

Bound every caller-owned local document before decoding and add collection or
JSON traversal budgets before record construction. Keep trusted large migration
work possible through explicit Python API overrides; CLI commands always use the
safe defaults.

## Single-Handle UTF-8 Reader

Add one private ingestion module with a binary reader that:

1. validates a positive byte limit or explicit `None`;
2. opens the path once;
3. reads at most `limit + 1` bytes from that handle;
4. rejects an over-limit file before UTF-8 decoding;
5. decodes with strict UTF-8.

This avoids a `stat()` then read time-of-check/time-of-use race. Path and decode
exceptions retain their current public types. Oversize input raises `ValueError`
with the document label, configured byte limit, and path. A byte-decoding helper
applies the same budget to the fixed packaged taxonomy without treating
packaged resources as caller paths.

## Default Budgets

The defaults are intentionally far above current canonical resources and initial
examples while placing finite ceilings on allocation and validation work:

- snapshot JSON: 64 MiB, 100,000 records per collection, 250,000 total;
- active-lessons YAML: 8 MiB and 10,000 lesson records;
- failure-taxonomy YAML: 1 MiB and 1,000 failure types;
- CLI measurements/tool-output JSON: 8 MiB, 10,000 top-level items,
  100,000 total JSON nodes, and depth 100.

The five snapshot collections are counted before constructing a Store. Lesson
and taxonomy parsers stop when an appended record would exceed their budget.
The CLI performs an iterative JSON traversal, extending its existing finite
number scan so deeply nested input cannot consume Python recursion.

## Python API Overrides

`TraceBackedMemoryStore.load_json()` adds keyword-only `max_bytes`,
`max_records_per_collection`, and `max_total_records`.
`TraceBackedMemoryStore.from_snapshot()` accepts the two record limits for
already-decoded mappings. `load_lessons_yaml()` adds `max_bytes` and
`max_lessons`; `load_failure_taxonomy()` adds `max_bytes` and
`max_failure_types`.

Each defaults to the safe budget. A non-negative record limit, positive byte
limit, or `None` is accepted; booleans and invalid values are rejected.
Explicit `None` disables only that named budget and is intended for trusted
offline migrations. All existing positional calls remain compatible.

## Error And Atomicity Contract

Store and taxonomy APIs report oversize or over-count inputs as `ValueError`.
Unreadable paths and invalid UTF-8 retain `OSError` and `UnicodeDecodeError`.
CLI evidence/measurement limit failures become `CLIInputError`, preserving
structured input errors and exit code 2. Snapshot limit failures already map to
CLI input errors through the existing load boundary.

All limits are checked before Store mutation. Lesson imports keep their staged
all-or-nothing commit. A rejected CLI manifest never reaches completion or
snapshot save. No partially parsed Store is returned.

## Compatibility And Non-Goals

This phase bounds file ingestion, not in-memory object construction, database
result size, network transport, or persisted model field lengths. It adds no
record, schema, or persisted limit metadata. Snapshot version 2, JSON Schemas,
active-lessons YAML shape, packaged resource bytes, `schemas/postgres.sql`, and
PostgreSQL schema version 1 remain unchanged.

## Verification

Tests cover exact byte/count boundaries, one-over rejection, invalid limit
arguments, explicit trusted overrides, UTF-8 byte rather than character size,
snapshot per-collection and total counts, lesson/taxonomy early stops, CLI item,
node, and depth budgets, stable error classification, unchanged destination
snapshots, canonical packaged taxonomy, normal round trips, documentation, the
full suite, and built wheel/sdist smoke tests.
