# Snapshot v3 migration preflight

**English** | [简体中文](snapshot-v3-preflight.zh-CN.md)

This document describes the delivered, read-only preflight for the coordinated
schema-version-3 program. It does not change the active compatibility boundary:
runtime snapshots remain version 2, SQLite remains schema version 1,
PostgreSQL remains schema version 2, and `tbm.agent.v1` remains the agent
protocol.

## Command

```text
tbm migration plan-v3 SNAPSHOT_V2 MAPPING_JSON \
  [--repository-root REPOSITORY_ID=PATH]...
```

The command requires an explicit `snapshot_version: 2` envelope, validates it
through
`TraceBackedMemoryStore`, parses a closed and bounded operator mapping, and
prints one deterministic JSON plan. It never changes the snapshot or mapping
file. A syntactically valid plan returns successfully even when `ready` is
false; callers must inspect `ready`, `counts.errors`, and the stable issue
codes.

When the mapping selects `ancestry_policy.mode="required"`, every repository
used by regression evidence must be associated with a trusted local Git object
database through a repeated `--repository-root`. The command verifies both
source-to-fix and fix-to-regression relations with
`git merge-base --is-ancestor`; missing, unrelated, or unavailable objects fail
closed in the plan. No checkout is inferred from the current directory.

The equivalent Python API is:

```python
from trace_backed_memory import (
    parse_v3_migration_mapping,
    plan_snapshot_v3_migration,
)

mapping = parse_v3_migration_mapping(mapping_payload)
plan = plan_snapshot_v3_migration(
    snapshot_payload,
    mapping,
    commit_relation_verifier=trusted_commit_relation_verifier,
)
assert plan.ready
```

`trusted_commit_relation_verifier(repository_id, relation)` is an application
port. It must authenticate the repository evidence and return an exact
`bool`. Omitting it under a required policy produces
`TBM_V3_ANCESTRY_VERIFIER_REQUIRED`; exceptions, non-Boolean results, and
rejected relations are reported as deterministic migration errors.

## Explicit operator mapping

The mapping protocol is `tbm.snapshot.v2-to-v3.mapping.v1`. It requires:

- a canonical repository registry with provider identity, a hashed canonical
  locator, and explicit legacy aliases;
- a canonical tenant registry with explicit legacy aliases;
- one repository/tenant binding for every Trace;
- one `global`, `tenant`, or `repository` authorization scope for every Lesson
  and Project Policy, separate from applicability attributes;
- structured regression evidence for every verified legacy Failure Case,
  including the passing regression Trace/run/evaluator and verified
  source-to-fix and fix-to-regression ancestor relations (a commit is an
  ancestor of itself);
- privileged approval evidence for every global Project Policy;
- an ancestry policy of `required`, or `disabled` with a nonblank audited bypass
  reason.

`disabled` is never silent: the plan contains
`TBM_V3_ANCESTRY_DISABLED`. The mapping's repository locator hashes,
evaluator identities, artifact digests, and global-policy approval records are
operator declarations for migration planning, not an authorization or
attestation service. A ready preflight does not activate migrated memory. The
future writer must bind those declarations to authenticated registries and
preserve insufficiently attested legacy records at a restricted trust state.

Missing `repo` or `tenant` values are never interpreted as global access.
Omitted legacy scope fields are never upgraded to global scope. A mapping may
narrow applicability, but it may not remove an existing repository, tenant, or
metadata constraint.

## Report semantics

The plan protocol is `tbm.snapshot.v2-to-v3.plan.v1`. It binds the report to
algorithm-tagged SHA-256 digests of the explicit source snapshot and canonical
mapping. Registry arrays, aliases, applicability attributes, and artifact
digests are normalized before the mapping hash so semantically equivalent
operator mappings produce the same digest.
Issues are deterministically ordered and classified as:

- `error`: migration is not ready;
- `warning`: migration can proceed later, but the limitation must remain
  explicit.

Existing version-2 usage logs produce `TBM_V3_LEGACY_REPLAY_PARTIAL`. The
warning does not fabricate retriever, model, prompt, ancestry, renderer,
response, or snippet evidence that version 2 never stored.

## Machine-readable resources

- `schemas/snapshot_v3_migration_mapping.schema.json`
- `schemas/snapshot_v3_migration_plan.schema.json`
- `examples/snapshot_v3_migration_mapping.example.json`
- `examples/snapshot_v3_migration_plan.example.json`

All four are canonical packaged resources and remain byte-identical between
the repository, wheel, and source distribution.

## Deliberate boundary

The preflight does not emit or load a version-3 snapshot, alter SQLite or
PostgreSQL, persist Gate sessions, or claim complete decision replay. Those
operations require the remaining coordinated version-3 domain, persistence,
transaction, rollback, and recovery implementation. Keeping the preflight
read-only prevents an incomplete schema from becoming an accidental production
format.
