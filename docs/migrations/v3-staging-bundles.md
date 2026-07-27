# Version-3 migration bundles and isolated staging

**English** | [简体中文](v3-staging-bundles.zh-CN.md)

This delivery adds durable migration preparation without changing an active
runtime format. Runtime snapshots remain version 2, SQLite remains schema
version 1, PostgreSQL remains schema version 2, and the agent protocol remains
`tbm.agent.v1`.

## Inert bundle contract

`tbm.snapshot.v2-to-v3.bundle.v1` freezes:

- the exact validated version-2 source snapshot;
- a normalized source-snapshot digest;
- the canonical operator mapping;
- the deterministic preflight plan;
- independent SHA-256 digests for all three documents;
- a content-derived `bundle_id`; and
- a `ready` or `blocked` staging state.

The exact and normalized source digests are deliberately separate. The exact
digest detects array-order or byte-materialization changes between preflight
and a future writer. The normalized digest identifies the Store state obtained
after strict version-2 reconstruction.

Create or verify bundles with:

```text
tbm migration bundle-v3 SNAPSHOT_V2 MAPPING_JSON \
  [--repository-root REPOSITORY_ID=PATH]...

tbm migration verify-v3-bundle BUNDLE_JSON \
  [--repository-root REPOSITORY_ID=PATH]...
```

The corresponding Python APIs are:

```python
bundle = create_snapshot_v3_migration_bundle(
    source,
    mapping,
    commit_relation_verifier=trusted_verifier,
)
encoded = dumps_snapshot_v3_migration_bundle(bundle)
parsed = loads_snapshot_v3_migration_bundle(encoded)
verify_snapshot_v3_migration_bundle(
    parsed,
    commit_relation_verifier=trusted_verifier,
)
```

Parsing checks closed fields, bounded UTF-8 JSON, duplicate keys, finite
numbers, Unicode validity, all embedded contracts, all document hashes, plan
readiness, and the derived bundle ID. Verification reruns the complete
preflight and requires an exact plan replay. A bundle created under required
ancestry cannot be reverified without an equivalent trusted verifier.

## SQLite staging repository

`SQLiteV3MigrationRepository` persists immutable bundles through the separate
`schemas/sqlite-v3-migration.sql` schema. It can use its own database or coexist
with the runtime SQLite tables because it has separate metadata and table
names. It never changes `trace_backed_memory_schema`, never loads a bundle as a
runtime Store, and exposes no activation or deletion API.

```python
with SQLiteV3MigrationRepository.connect(
    ".tbm/migrations.sqlite3",
    initialize=True,
) as staging:
    result = staging.stage(
        bundle,
        commit_relation_verifier=trusted_verifier,
    )
    replayed = staging.load(result.bundle_id)
```

Staging is one transaction, exact replay is idempotent, conflicting content is
rejected, and every load revalidates the stored payload and duplicated digest
columns. Every operation compares the installed table and trigger definitions
with the canonical packaged DDL, so a weakened same-version schema fails
closed. Update and delete triggers make bundle rows immutable. When a caller
already owns a SQLite transaction, staging uses a savepoint and therefore
commits or rolls back with that outer transaction. If an internal savepoint
cannot be released after retry, the outer transaction is rolled back rather
than left committable.

This SQLite profile is a local staging ledger. It does not provide
PostgreSQL-style row locks, database-side tenant authorization, or
multi-client isolation.

## PostgreSQL staging and rollback

Two operator resources are provided:

- `schemas/postgres-v3-staging.sql`
- `schemas/postgres-v3-staging-rollback.sql`

The staging script starts one transaction, locks the active public schema
metadata row `FOR UPDATE`, requires exact PostgreSQL schema version 2, and
creates `trace_backed_memory_v3_staging`. It does not change public runtime
tables or the active schema version. Replaying the creation script fails
because the isolated schema already exists. Database triggers reject update,
delete, and truncate operations against both staging metadata and bundle rows,
while those triggers remain installed. The schema owner and database
superusers are trusted operator boundaries because they can alter or disable
database triggers; an application writer role should not own the staging
objects.

The rollback script independently locks and verifies both active and staging
metadata, drops the known tables and function, and removes
`trace_backed_memory_v3_staging` with `RESTRICT`. A missing, modified, or
wrong-version metadata record, an unexpected staging object, or an external
dependency aborts and rolls back the transaction. Neither script is run
automatically by the Python runtime.

The PostgreSQL resource creates isolated storage, not a public bundle-insertion
adapter. Its column checks enforce bounded shape but cannot prove that embedded
JSON agrees with its duplicated digest columns. A future PostgreSQL writer must
run the same Python bundle verification and exact-replay checks before insert.

## Security boundary

A bundle is content-addressed, not signed. Operator-supplied repository
locator hashes, evaluator names, artifact hashes, `verified_by` identities, and
global-policy approval IDs remain declarations until a future writer binds
them to authenticated registries and retained evidence. `ready` means the
migration inputs passed the current preflight; it does not mean:

- memory is activated;
- tenant authorization has been established;
- evaluator or approval attestations are trusted;
- a version-3 runtime database exists; or
- a complete retriever/model/renderer decision replay is available.

Blocked bundles may be staged for diagnosis. Bundles with disabled ancestry
remain explicitly warning-bearing. No API in this delivery can publish either
kind as runtime memory.
