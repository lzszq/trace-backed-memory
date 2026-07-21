# Public Project-Policy Obsolescence Export Plan

## Scope

Make the already implemented and documented `obsolete_project_policy()` helper
available from the package root, matching the other obsolescence helpers.

## Steps

1. Add a failing package-root import and `__all__` test that also checks the
   export is the existing lifecycle function rather than a wrapper.
2. Extend the executable README API test with the missing policy transition and
   input immutability assertion.
3. Re-export `obsolete_project_policy` from `trace_backed_memory.__init__` and
   add it to `__all__`.
4. Update the README example, architecture, product document, roadmap, and
   documentation contract for the completed public surface.
5. Run focused and full tests, build and verify wheel/sdist artifacts, import
   the helper from an isolated wheel, obtain independent review, merge, push,
   and require every CI job to pass.

## Compatibility

- The change is an additive root-package export of an existing function; its
  implementation, signature, and lifecycle semantics do not change.
- CLI and Store behavior, models, dependencies, snapshot version 2, every JSON
  Schema, active-lessons YAML, packaged resource names/bytes, PostgreSQL DDL,
  and PostgreSQL schema version 1 remain unchanged.
