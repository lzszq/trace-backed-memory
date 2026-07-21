# Conservative Failure Extraction Plan

## Scope

Restore the documented conservative evidence boundary for failure-type and
symptom extraction without changing APIs or persistence.

## Steps

1. Add failing extraction tests for successful named calls coexisting with
   Trace errors and for bare `required` permission/authentication messages.
2. Add positive boundary tests for explicit required argument, parameter,
   field, and property markers.
3. Filter tool-call symptom names by top-level error evidence and replace the
   bare required-word classifier shortcut with explicit markers.
4. Update README, architecture, product, roadmap, and executable documentation
   contracts for Phase 50.
5. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Preserve classifier precedence, taxonomy checking, symptom wording, stored
  order, and root-cause selection.
- Preserve public signatures, dependencies, snapshot version 2, every JSON
  Schema, active-lessons YAML, all packaged resources, PostgreSQL DDL, and
  PostgreSQL schema version 1.
