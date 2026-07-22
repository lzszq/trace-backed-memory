# Active-Only Lesson Imports Plan

## Scope

Make the existing portable lessons YAML import interface enforce its documented
active-only domain without changing general Lesson lifecycle storage.

## Steps

1. Add a Store regression test for a mixed active/obsolete YAML document and
   prove that rejection leaves the Store unchanged.
2. Add a CLI `--write` regression test for structured input exit code 2 and
   unchanged snapshot bytes.
3. Enforce `status == "active"` inside `load_lessons_yaml()` after normal
   candidate validation and before staged insertion.
4. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 58.
5. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Keep `add_lesson()`, snapshot reconstruction, PostgreSQL loading, and
  obsolescence lifecycle support for obsolete records unchanged.
- Keep empty and canonical active-only imports unchanged.
- Preserve snapshot version 2, PostgreSQL schema version 1, and all packaged
  resources.
