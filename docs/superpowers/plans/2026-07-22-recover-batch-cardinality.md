# Recover Batch Argument Cardinality Plan

## Scope

Bound submitted `recover-batch` decision IDs and repeated attribution options
before snapshot loading or batch collection construction.

## Steps

1. Add failing CLI tests for the exact boundary and one-item overflow of each
   argument list.
2. Prove overflow returns structured input exit code 2 before snapshot loading
   and cannot write the source file.
3. Add one private 10,000-item recovery-batch limit and a preload cardinality
   validator.
4. Leave accepted-batch uniqueness, strict attribution parsing, Store
   validation, ordering, dry-run, and atomic publication paths unchanged.
5. Publish Phase 44 behavior in README, architecture, usage policy, product,
   roadmap, and documentation contract tests.
6. Run focused and full tests, build and verify wheel/sdist artifacts, obtain
   an independent review, merge, push, and require every CI job to pass.

## Compatibility

- Valid batches at or below 10,000 submitted values retain current behavior.
- Oversized decision-ID or attribution lists now fail before snapshot loading.
- Public APIs, dependencies, snapshot version 2, JSON Schemas, active-lessons
  YAML, 18 packaged resources, PostgreSQL DDL, and schema version 1 do not
  change.
