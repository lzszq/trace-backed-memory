# Recover Attribution Delimiter Design

## Summary

`recover-batch --attribution` accepts `DECISION_ID=true|false`, while the
Store, JSON Schema, and PostgreSQL contracts all permit `=` inside a decision
ID. The CLI currently splits at the first `=`, so an otherwise valid ID such as
`decision=regional` cannot be attributed or recovered through the CLI.

Phase 70 parses the final `=` as the attribution delimiter. The complete
prefix is the decision ID and the suffix must still be exactly `true` or
`false`.

## Parsing Contract

For each attribution option:

1. split on the final `=`;
2. require a non-empty decision-ID prefix;
3. require a non-empty suffix and parse only exact lowercase `true` or `false`;
4. require the preserved decision ID to be requested exactly once;
5. reject duplicate attribution entries for that complete decision ID.

Missing delimiters, empty IDs, empty suffixes, invalid boolean suffixes,
unrequested IDs, and duplicate attributions remain `CLIInputError` failures.
They retain structured input errors and exit code 2. Accepted IDs are not
trimmed or normalized.

## Ordering and Atomicity

The change is local to attribution parsing. Recover-batch cardinality checks,
snapshot loading, requested-ID ordering, Store-owned eligibility and recovery
validation, dry-run behavior, the shared snapshot write lock, and
all-or-nothing publication remain unchanged.

## Compatibility

Every previously valid attribution string retains the same result. Strings
that contain `=` in the decision-ID prefix become usable consistently with the
existing 128-character nonblank Store and persistence contract. No decision ID
character is newly forbidden.

Public APIs, CLI output shapes, models, dependencies, snapshot version 2, JSON
Schemas, the 18 packaged resources, PostgreSQL DDL, and PostgreSQL schema
version 1 remain unchanged.

## Tests

- End-to-end `recover-batch` accepts an existing decision ID containing one or
  multiple `=` characters and preserves it in output and the written snapshot.
- A decision ID whose suffix-like segment is `=true` still uses the final
  delimiter for the actual attribution value.
- Empty and malformed suffixes continue to return input exit code 2 without
  replacing the snapshot.
- Existing ordinary attributions, request ordering, strict booleans, and full
  repository tests remain green.
