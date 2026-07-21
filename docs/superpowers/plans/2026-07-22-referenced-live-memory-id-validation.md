# Referenced Live Memory-ID Validation Plan

## Scope

Remove full memory-catalog key copying from live usage-log validation while
preserving snapshot-local indexing and exact diagnostics.

## Steps

1. Add deterministic tests proving live decision logging and direct unknown-ID
   validation do not iterate the memory dictionaries.
2. Replace the live full-catalog union with per-referenced-ID membership in the
   three authoritative Store dictionaries.
3. Preserve the existing snapshot `known_memory_ids` set reuse, relationship
   validation order, deduplication, and sorted unknown-ID error.
4. Cover decision creation, outcome/completion regressions, and catalog sizes
   that would expose a reintroduced scan.
5. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 54.
6. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Add no persistent or derived Store field.
- Preserve public APIs, exact errors, snapshot version 2, every JSON Schema,
  active-lessons YAML, all packaged resources, PostgreSQL DDL, and PostgreSQL
  schema version 1.
