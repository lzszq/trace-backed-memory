# Required PostgreSQL and Windows CI Coverage Plan

## Scope

Close the CI false-green gap without changing local optional-database behavior
or any product persistence contract.

## Steps

1. Add focused tests for optional versus required PostgreSQL runtime failures.
2. Route the fixture's two environmental skip paths through one required-mode
   helper.
3. Add dedicated PostgreSQL and Windows jobs to the GitHub Actions workflow.
4. Publish Phase 37 in the README, architecture, product document, and roadmap.
5. Run focused tests, full pytest, compile checks, YAML parsing, and
   compatibility checks.
6. Merge and push only after the remote workflow proves every new job.

## Compatibility

- No source package API or dependency changes.
- No snapshot, JSON Schema, active-lessons YAML, resource, or SQL changes.
- Snapshot version remains 2.
- PostgreSQL schema version remains 1.
- Developers without PostgreSQL retain the current pytest skip behavior unless
  they explicitly set `TBM_REQUIRE_POSTGRES=1`.

