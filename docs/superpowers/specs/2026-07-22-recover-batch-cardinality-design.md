# Recover Batch Argument Cardinality Design

## Summary

`tbm recover-batch` accepts decision IDs through an argparse `nargs="+"`
positional and causal attributions through repeated `--attribution` options.
Both lists are currently unbounded. Before the Store validates the batch, the
adapter converts these values to a tuple, set, and dictionary, and it first
loads the complete snapshot. A caller can therefore make the CLI perform
unbounded argument-driven work before any recovery state is examined.

Phase 44 gives both argument lists the same fixed 10,000-item ceiling already
used for CLI JSON batches. Cardinality is checked immediately after argument
parsing and before snapshot loading, collection construction, recovery, or
publication.

## Preload Boundary

Add a private ingestion constant for the `recover-batch` item ceiling and one
CLI preflight helper. For `recover-batch` only, the helper checks:

- no more than 10,000 submitted decision IDs; and
- no more than 10,000 submitted `--attribution` values.

Counts are based on submitted values before duplicate detection. This prevents
duplicates from being used to bypass the work budget. The helper uses only the
already-parsed list lengths and does not build a tuple, set, dictionary, or
Store. Overflow raises `CLIInputError`, preserving the structured `input`
error and exit code 2 contract.

Accepted inputs continue through the existing path. Decision IDs must still
be unique, attribution values must still use exact
`DECISION_ID=true|false` syntax, and the Store remains authoritative for
identity, eligibility, linkage, attribution, and atomic recovery.

## Compatibility and Persistence

Ordinary batches and the exact 10,000-item boundary remain valid. The new
limit is fixed for the CLI and has no opt-out; callers that need trusted
offline bulk processing can partition requests and invoke the Store API under
their own resource controls.

The change adds no public API, model, dependency, persisted limit metadata, or
record. Snapshot version 2, every JSON Schema, active-lessons YAML, all 18
packaged resources, PostgreSQL DDL, and PostgreSQL schema version 1 remain
unchanged.

## Tests

- An exact configured boundary reaches snapshot loading and the normal
  recovery path.
- One excess decision ID fails before snapshot loading with exit code 2.
- One excess attribution fails at the same preload boundary.
- Overflow with `--write` never reads or replaces the snapshot.
- Documentation contract tests publish both ceilings, preload rejection,
  Phase 44 maturity, and the unchanged persistence formats.

