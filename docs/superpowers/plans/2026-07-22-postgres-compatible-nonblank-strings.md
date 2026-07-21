# PostgreSQL-Compatible Nonblank Strings Plan

## Scope

Reject whitespace-only persisted identity, linkage, scope, context, and audit
strings before PostgreSQL synchronization, without normalizing accepted data or
changing database DDL.

## Steps

1. Add failing Store and snapshot tests for every PostgreSQL-constrained text
   field, Failure Case optional fix fields, scope values, and usage-audit map
   keys/values; prove failed writes do not mutate existing state.
2. Add failing policy/lifecycle tests for whitespace-only Memory Context and
   memory scope values while preserving strings containing real content.
3. Add failing canonical/package Schema tests for `pattern: "\\S"` on the six
   affected record/input Schemas and unchanged memory-ID arrays.
4. Add a live PostgreSQL regression that locks the existing `btrim` behavior
   and confirms no DDL or schema-version migration is required.
5. Make the shared required-string validator nonblank, add a targeted optional
   nonblank mode for `fix`/`fix_commit_sha`, and align scope/context/usage map
   validators without trimming accepted values.
6. Update both copies of the six affected Schemas; leave snapshot Schema,
   Memory Decision Schema, and both PostgreSQL DDL copies unchanged.
7. Publish Phase 49 behavior and compatibility in README, architecture, usage
   policy, product, roadmap, and executable documentation contracts.
8. Run focused and full tests, build and verify wheel/sdist artifacts, obtain
   independent review, merge, push, and require every CI job to pass.

## Compatibility

- Whitespace-only values in the covered fields become invalid before database
  sync; accepted values retain their exact bytes and are never stripped.
- Optional Trace metadata, unrelated Failure Case narrative fields, and usage
  memory-ID arrays retain their current behavior.
- Public APIs, models, dependencies, snapshot version 2, active-lessons YAML,
  packaged resource paths/count, PostgreSQL DDL, and schema version 1 remain
  unchanged. Six canonical/package JSON Schema byte pairs change.
