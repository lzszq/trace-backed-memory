# Apply and roll back a local SQLite v3 migration

**English** | [简体中文](sqlite-v3-apply.zh-CN.md)

This guide covers the explicit local migration from a validated snapshot
version 2 or SQLite schema version 1 source into a separate SQLite v3 target.
It does not mutate the source. PostgreSQL cutover is not part of this command.

## Before applying

Create a migration mapping, run the preflight, and freeze a ready bundle:

```text
tbm migration plan-v3 SNAPSHOT_V2 MAPPING_JSON
tbm migration bundle-v3 SNAPSHOT_V2 MAPPING_JSON > migration-bundle.json
tbm migration verify-v3-bundle migration-bundle.json
```

Add `--repository-root REPOSITORY_ID=PATH` to all three commands when the
mapping requires trusted Git ancestry verification. A blocked bundle cannot be
applied.

## Apply a snapshot source

```text
tbm migration apply-v3 migration-bundle.json project.snapshot.json \
  --source-kind snapshot \
  --target sqlite \
  --database .tbm/durable.sqlite3 \
  --backup .tbm/project.snapshot.v2.bak
```

## Apply an SQLite v1 source

```text
tbm migration apply-v3 migration-bundle.json legacy.sqlite3 \
  --source-kind sqlite \
  --target sqlite \
  --database .tbm/durable.sqlite3 \
  --backup .tbm/legacy.sqlite3.v1.bak
```

The source must be an existing single-link regular file. The target and backup
must be distinct output paths; if either already exists, it must also be a
single-link regular file. The target must not contain unrelated data. A
consistent SQLite backup is used for a SQLite source, so committed WAL state
is included.

Apply performs these checks before publishing the target:

1. exactly replay the bundle preflight;
2. require `state=ready`;
3. strictly load the source and match its normalized snapshot digest;
4. create or revalidate the source backup;
5. construct a temporary SQLite v1 compatibility copy;
6. install and fingerprint the complete 16-component SQLite v3 bundle;
7. store the immutable migration bundle, application, record dispositions,
   and initial `durable-v3` profile event;
8. verify the schema, source copy, backup, dispositions, and SQLite integrity;
9. publish without replacing an existing file.

The same command is idempotent when the target already contains the exact
application. A failed attempt may leave a valid backup, which the next attempt
revalidates and reuses. An unpublished temporary target is never treated as a
successful migration. Temporary payloads live in exclusive private
directories (`0700` on POSIX), retain their inode identity through
publication, and pass single-link regular-file checks; publication never
follows or replaces a symbolic link. SQLite writers use `synchronous=FULL`,
published files are synced, and parent directory entries are synced on
platforms that expose directory `fsync`.

## Legacy evidence treatment

Migration preserves all v2 records in the compatibility tables, but does not
turn old status fields into v3 authorization or publication:

| v2 record | v3 migration disposition |
|---|---|
| clean Trace | `legacy_trace`, `retained_legacy` |
| dirty Trace | `legacy_dirty_trace`, `retained_legacy` |
| FailureCase with mapped regression preflight | `mapped_regression_preflight`, `retained_legacy` |
| FailureCase without mapped proof | `legacy_unverified`, `retained_legacy` |
| Lesson or ProjectPolicy | `unpublished_v3`, even when v2 status was `active` |
| MemoryUsageLog | `legacy_partial_replay`, `legacy_partial` |

The original status, including `obsolete`, is preserved. The migration does
not fabricate independent verifier attestations, authorization decisions,
artifact encryption metadata, replay bytes, approval events, activation
events, tenant IDs, repository IDs, or scope. ActivatedRevision publication
and active v3 retrieval remain separate work.

## Verify

```text
tbm migration verify-v3 .tbm/durable.sqlite3
```

Verification fails closed on schema/catalog drift, bundle replay differences,
changed compatibility payloads, a missing or changed backup, changed record
dispositions, invalid profile history, a changed rollback database, or failed
SQLite integrity checks. The command reports the current `durable-v3` or
`compat-v2` profile and deterministic evidence/status counts.

## Roll back to compat-v2

Stop writers, then materialize a separate SQLite v1 compatibility database:

```text
tbm migration rollback-v3 .tbm/durable.sqlite3 \
  --compat-database .tbm/compat-v2.sqlite3
```

Rollback reconstructs the exact normalized v2 Store, verifies it, and appends
an immutable `compat-v2` profile event to the durable target. It does not
delete the durable database or its v3 evidence. A durable runtime refuses to
open a migrated target after this event; point the compatibility client at the
reported SQLite v1 database instead. Repeating the same rollback is
idempotent. The verification snapshot, compatibility publication, and profile
event are protected by one SQLite `BEGIN IMMEDIATE` writer boundary; stopping
external writers remains the operator prerequisite for cutover.

## Python API

```python
from trace_backed_memory import (
    apply_sqlite_v3_migration,
    load_snapshot_v3_migration_bundle,
    rollback_sqlite_v3_migration,
    verify_sqlite_v3_migration,
)

bundle = load_snapshot_v3_migration_bundle("migration-bundle.json")
applied = apply_sqlite_v3_migration(
    bundle,
    source="legacy.sqlite3",
    source_kind="sqlite",
    target_database=".tbm/durable.sqlite3",
    backup=".tbm/legacy.sqlite3.v1.bak",
)
verified = verify_sqlite_v3_migration(".tbm/durable.sqlite3")
rolled_back = rollback_sqlite_v3_migration(
    ".tbm/durable.sqlite3",
    compatibility_database=".tbm/compat-v2.sqlite3",
)
```

When the bundle uses required ancestry, pass the same trusted
`commit_relation_verifier` to apply, verify, and rollback.
