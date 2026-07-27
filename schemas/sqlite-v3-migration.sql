PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_migration_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_migration_schema (
    singleton,
    schema_version
) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS v3_migration_bundles (
    bundle_id TEXT PRIMARY KEY
        CHECK (
            length(bundle_id) = 71
            AND substr(bundle_id, 1, 7) = 'sha256:'
            AND substr(bundle_id, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    bundle_version TEXT NOT NULL
        CHECK (bundle_version = 'tbm.snapshot.v2-to-v3.bundle.v1'),
    state TEXT NOT NULL CHECK (state IN ('blocked', 'ready')),
    source_snapshot_sha256 TEXT NOT NULL
        CHECK (
            length(source_snapshot_sha256) = 71
            AND substr(source_snapshot_sha256, 1, 7) = 'sha256:'
            AND substr(source_snapshot_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    normalized_source_snapshot_sha256 TEXT NOT NULL
        CHECK (
            length(normalized_source_snapshot_sha256) = 71
            AND substr(normalized_source_snapshot_sha256, 1, 7) = 'sha256:'
            AND substr(normalized_source_snapshot_sha256, 8)
                NOT GLOB '*[^0-9a-f]*'
        ),
    mapping_sha256 TEXT NOT NULL
        CHECK (
            length(mapping_sha256) = 71
            AND substr(mapping_sha256, 1, 7) = 'sha256:'
            AND substr(mapping_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    plan_sha256 TEXT NOT NULL
        CHECK (
            length(plan_sha256) = 71
            AND substr(plan_sha256, 1, 7) = 'sha256:'
            AND substr(plan_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    payload TEXT NOT NULL
        CHECK (
            length(CAST(payload AS BLOB)) > 0
            AND length(CAST(payload AS BLOB)) <= 134217728
        )
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS v3_migration_bundles_immutable_update
BEFORE UPDATE ON v3_migration_bundles
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration bundles are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_migration_bundles_immutable_delete
BEFORE DELETE ON v3_migration_bundles
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration bundles are immutable');
END;

COMMIT;
