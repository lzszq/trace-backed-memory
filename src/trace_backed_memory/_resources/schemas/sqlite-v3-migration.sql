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

CREATE TABLE IF NOT EXISTS v3_migration_applications (
    application_id TEXT PRIMARY KEY
        CHECK (
            length(application_id) = 71
            AND substr(application_id, 1, 7) = 'sha256:'
            AND substr(application_id, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    bundle_id TEXT NOT NULL UNIQUE
        REFERENCES v3_migration_bundles(bundle_id),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('snapshot', 'sqlite')),
    backup_path TEXT NOT NULL COLLATE BINARY
        CHECK (
            length(CAST(backup_path AS BLOB)) BETWEEN 1 AND 16384
        ),
    backup_sha256 TEXT NOT NULL
        CHECK (
            length(backup_sha256) = 71
            AND substr(backup_sha256, 1, 7) = 'sha256:'
            AND substr(backup_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    normalized_source_snapshot_sha256 TEXT NOT NULL
        CHECK (
            length(normalized_source_snapshot_sha256) = 71
            AND substr(normalized_source_snapshot_sha256, 1, 7) = 'sha256:'
            AND substr(normalized_source_snapshot_sha256, 8)
                NOT GLOB '*[^0-9a-f]*'
        ),
    disposition_sha256 TEXT NOT NULL
        CHECK (
            length(disposition_sha256) = 71
            AND substr(disposition_sha256, 1, 7) = 'sha256:'
            AND substr(disposition_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    record_count INTEGER NOT NULL
        CHECK (record_count BETWEEN 0 AND 1000000),
    applied_at TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(applied_at AS BLOB)) BETWEEN 20 AND 64)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_migration_record_dispositions (
    application_id TEXT NOT NULL
        REFERENCES v3_migration_applications(application_id),
    record_kind TEXT NOT NULL
        CHECK (
            record_kind IN (
                'trace',
                'failure_case',
                'lesson',
                'project_policy',
                'usage_log'
            )
        ),
    record_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(record_id AS BLOB)) BETWEEN 1 AND 1024),
    source_status TEXT COLLATE BINARY
        CHECK (
            source_status IS NULL
            OR length(CAST(source_status AS BLOB)) BETWEEN 1 AND 128
        ),
    evidence_status TEXT NOT NULL
        CHECK (
            evidence_status IN (
                'legacy_trace',
                'legacy_dirty_trace',
                'legacy_unverified',
                'mapped_regression_preflight',
                'legacy_partial_replay'
            )
        ),
    target_status TEXT NOT NULL
        CHECK (
            target_status IN (
                'retained_legacy',
                'unpublished_v3',
                'legacy_partial'
            )
        ),
    reason_code TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(reason_code AS BLOB)) BETWEEN 1 AND 128),
    PRIMARY KEY (application_id, record_kind, record_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_migration_profile_events (
    application_id TEXT NOT NULL
        REFERENCES v3_migration_applications(application_id),
    sequence INTEGER NOT NULL CHECK (sequence IN (1, 2)),
    profile TEXT NOT NULL
        CHECK (profile IN ('durable-v3', 'compat-v2')),
    compatibility_path TEXT COLLATE BINARY
        CHECK (
            compatibility_path IS NULL
            OR length(CAST(compatibility_path AS BLOB))
                BETWEEN 1 AND 16384
        ),
    compatibility_sha256 TEXT
        CHECK (
            compatibility_sha256 IS NULL
            OR (
                length(compatibility_sha256) = 71
                AND substr(compatibility_sha256, 1, 7) = 'sha256:'
                AND substr(compatibility_sha256, 8)
                    NOT GLOB '*[^0-9a-f]*'
            )
        ),
    occurred_at TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(occurred_at AS BLOB)) BETWEEN 20 AND 64),
    reason_code TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(reason_code AS BLOB)) BETWEEN 1 AND 128),
    PRIMARY KEY (application_id, sequence),
    UNIQUE (application_id, profile),
    CHECK (
        (
            sequence = 1
            AND profile = 'durable-v3'
            AND compatibility_path IS NULL
            AND compatibility_sha256 IS NULL
        )
        OR (
            sequence = 2
            AND profile = 'compat-v2'
            AND compatibility_path IS NOT NULL
            AND compatibility_sha256 IS NOT NULL
        )
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

CREATE TRIGGER IF NOT EXISTS v3_migration_applications_immutable_update
BEFORE UPDATE ON v3_migration_applications
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration applications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_migration_applications_immutable_delete
BEFORE DELETE ON v3_migration_applications
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration applications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_migration_record_dispositions_immutable_update
BEFORE UPDATE ON v3_migration_record_dispositions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration record dispositions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_migration_record_dispositions_immutable_delete
BEFORE DELETE ON v3_migration_record_dispositions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration record dispositions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_migration_profile_events_immutable_update
BEFORE UPDATE ON v3_migration_profile_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration profile events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_migration_profile_events_immutable_delete
BEFORE DELETE ON v3_migration_profile_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 migration profile events are immutable');
END;

COMMIT;
