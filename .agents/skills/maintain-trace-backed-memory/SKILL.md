---
name: maintain-trace-backed-memory
description: Safely modify the trace-backed-memory repository while preserving evidence provenance, gate monotonicity, atomic persistence, schema compatibility, packaged-resource parity, and bilingual documentation. Use for changes to domain models, lifecycle, policy, Store behavior, agent APIs, CLI, SQLite/PostgreSQL adapters, schemas, migrations, packaging, tests, or architecture documentation.
---

# Maintain Trace-backed Memory

Follow the repository's evidence-first contracts while making changes.

## Workflow

1. Read the root `AGENTS.md`.
2. Read `references/invariants.md` before changing lifecycle, policy,
   persistence, protocols, or schemas.
3. Read `references/architecture-map.md` to locate all affected layers.
4. Read the exact implementation code to be modified.
5. Design the smallest coherent change that keeps policy in the kernel and
   adapters thin.
6. Update every affected representation in the same change.
7. Run focused tests, then `python tools/verify.py --fast`.
8. Run `python tools/verify.py --full` before release handoff.

## Stored-contract changes

When adding or changing persisted data, inspect all of:

- domain record and validation;
- Store insertion, reconstruction, and canonical serialization;
- snapshot JSON Schema and example;
- SQLite schema/repository;
- PostgreSQL schema/repository and lock order;
- migrations and compatibility documentation;
- packaged byte-identical resource copy;
- unit, rejection, replay, rollback, and distribution tests.

Do not bump snapshot or database versions without an explicit migration and
verification path. Do not silently broaden missing tenant, repository, or
scope values.

## Runtime changes

Preserve this order:

```text
retrieve -> System Gate -> semantic narrowing -> stale-state recheck
-> render -> usage audit -> measured completion
```

Keep pending Store tokens private. An external adapter may hold an opaque
request ID, but it may not reconstruct candidates, reopen blocked memory, or
render caller-supplied content.

## Documentation and resources

Update English and Simplified Chinese reference documents together. Preserve
explicitly labeled historical phase baselines. Author canonical resource files
at the repository root, then mirror their exact bytes into the installed
resource tree and run full distribution verification.
