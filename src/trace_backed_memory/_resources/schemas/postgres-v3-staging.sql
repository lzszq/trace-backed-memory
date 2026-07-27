BEGIN;

SET LOCAL search_path = pg_catalog;

DO $migration$
DECLARE
    current_version INTEGER;
BEGIN
    SELECT schema_version
      INTO current_version
      FROM public.trace_backed_memory_schema
     WHERE singleton
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'trace-backed-memory schema metadata row is missing';
    END IF;
    IF current_version <> 2 THEN
        RAISE EXCEPTION
            'v3 staging requires PostgreSQL schema version 2, found %',
            current_version;
    END IF;
END
$migration$ LANGUAGE plpgsql;

CREATE SCHEMA trace_backed_memory_v3_staging;

CREATE TABLE trace_backed_memory_v3_staging.schema_metadata (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    staging_schema_version INTEGER NOT NULL
        CHECK (staging_schema_version = 1),
    source_postgres_schema_version INTEGER NOT NULL
        CHECK (source_postgres_schema_version = 2),
    target_snapshot_version INTEGER NOT NULL
        CHECK (target_snapshot_version = 3)
);

INSERT INTO trace_backed_memory_v3_staging.schema_metadata (
    singleton,
    staging_schema_version,
    source_postgres_schema_version,
    target_snapshot_version
) VALUES (TRUE, 1, 2, 3);

CREATE FUNCTION trace_backed_memory_v3_staging.reject_staging_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'v3 staging records are immutable';
END
$function$;

CREATE TRIGGER schema_metadata_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_staging.schema_metadata
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_staging.reject_staging_mutation();

CREATE TRIGGER schema_metadata_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_staging.schema_metadata
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_staging.reject_staging_mutation();

CREATE TRIGGER schema_metadata_immutable_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_staging.schema_metadata
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_staging.reject_staging_mutation();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_staging.schema_metadata
  FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_staging.migration_bundles (
    bundle_id TEXT PRIMARY KEY
        CHECK (bundle_id ~ '^sha256:[0-9a-f]{64}$'),
    bundle_version TEXT NOT NULL
        CHECK (
            bundle_version = 'tbm.snapshot.v2-to-v3.bundle.v1'
        ),
    state TEXT NOT NULL CHECK (state IN ('blocked', 'ready')),
    source_snapshot_sha256 TEXT NOT NULL
        CHECK (source_snapshot_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    normalized_source_snapshot_sha256 TEXT NOT NULL
        CHECK (
            normalized_source_snapshot_sha256
                ~ '^sha256:[0-9a-f]{64}$'
        ),
    mapping_sha256 TEXT NOT NULL
        CHECK (mapping_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    plan_sha256 TEXT NOT NULL
        CHECK (plan_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    payload TEXT NOT NULL
        CHECK (
            octet_length(payload) > 0
            AND octet_length(payload) <= 134217728
        )
);

CREATE TRIGGER migration_bundles_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_staging.migration_bundles
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_staging.reject_staging_mutation();

CREATE TRIGGER migration_bundles_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_staging.migration_bundles
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_staging.reject_staging_mutation();

CREATE TRIGGER migration_bundles_immutable_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_staging.migration_bundles
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_staging.reject_staging_mutation();

REVOKE UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_staging.migration_bundles
  FROM PUBLIC;

COMMIT;
