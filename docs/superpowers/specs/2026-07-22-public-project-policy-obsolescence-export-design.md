# Public Project-Policy Obsolescence Export Design

## Summary

The lifecycle module implements `obsolete_project_policy()`, and the README
and architecture name it beside the failure-case and lesson obsolescence
helpers. The package root exports only the other two helpers. A documented
`from trace_backed_memory import obsolete_project_policy` therefore fails for
source and installed-package users even though the implementation exists.

Phase 46 closes this public-surface gap. It does not introduce a new lifecycle
transition or change Store behavior.

## Public Surface

Import the existing lifecycle function in `trace_backed_memory.__init__` and
include the same name in `__all__`. Do not add a wrapper, alias, or second
implementation. The root export and `trace_backed_memory.lifecycle` must refer
to the same function object, preserving its current signature and behavior:
return a replaced `ProjectPolicy` with `status="obsolete"` without mutating the
input object.

Update the executable README API example to import and invoke all three
low-level obsolescence helpers. The CLI and Store continue to own their existing
atomic, forward-only orchestration; the low-level helper remains a pure record
transition.

## Compatibility

This is an additive public export. Existing imports and runtime behavior remain
unchanged. Models, dependencies, command-line behavior, snapshot version 2,
every JSON Schema, active-lessons YAML, all 18 packaged resource names and
bytes, PostgreSQL DDL, and PostgreSQL schema version 1 remain unchanged.

Because `__init__.py` is ordinary package code, wheel and source distributions
pick up the export without package-data or metadata changes.

## Tests

- A root-package test imports `obsolete_project_policy`, requires membership in
  `__all__`, and verifies identity with the lifecycle implementation.
- The executable README API test invokes the root export and checks the
  obsolete result while retaining the original policy.
- Documentation contract coverage publishes the completed Phase 46 surface and
  unchanged compatibility boundaries.
- Distribution verification and an isolated-wheel import smoke prove the fix is
  present outside the source checkout.
