# Strict JSON Object Key Uniqueness Plan

## Scope

Reject duplicate object keys before caller-owned snapshot, memory-context,
memory-decision, and CLI JSON values are converted to ordinary dictionaries.

## Steps

1. Add failing snapshot tests for duplicate envelope and nested record keys.
2. Add failing policy tests for duplicate context and decision fields.
3. Add one private ordered-pairs helper in `_ingestion.py` with
   description-specific errors.
4. Wire snapshot and policy `json.loads()` calls to the helper.
5. Reuse the same helper behind the CLI's existing `CLIInputError` contract and
   rerun its duplicate-key regression tests.
6. Publish Phase 43 behavior in README, architecture, usage policy, product,
   roadmap, and documentation contract tests.
7. Run focused and full tests, build and verify wheel/sdist artifacts, obtain an
   independent review, merge, push, and require every CI job to pass.

## Compatibility

- Valid JSON and direct Mapping inputs keep their current behavior.
- Duplicate keys now fail deterministically instead of using the last value.
- Public APIs, dependencies, snapshot version 2, JSON Schemas, active-lessons
  YAML, 18 packaged resources, PostgreSQL DDL, and schema version 1 do not
  change.

