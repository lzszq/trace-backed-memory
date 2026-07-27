BEGIN;

SET LOCAL search_path = pg_catalog;

DO $rollback$
DECLARE
    current_version INTEGER;
    staging_version INTEGER;
    source_version INTEGER;
    target_version INTEGER;
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
            'v3 staging rollback requires PostgreSQL schema version 2, found %',
            current_version;
    END IF;

    SELECT
        staging_schema_version,
        source_postgres_schema_version,
        target_snapshot_version
      INTO
        staging_version,
        source_version,
        target_version
      FROM trace_backed_memory_v3_staging.schema_metadata
     WHERE singleton
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'v3 staging metadata row is missing';
    END IF;
    IF staging_version <> 1
       OR source_version <> 2
       OR target_version <> 3 THEN
        RAISE EXCEPTION
            'v3 staging metadata is incompatible with this rollback';
    END IF;
END
$rollback$ LANGUAGE plpgsql;

DROP TABLE trace_backed_memory_v3_staging.migration_bundles;
DROP TABLE trace_backed_memory_v3_staging.schema_metadata;
DROP FUNCTION trace_backed_memory_v3_staging.reject_staging_mutation();
DROP SCHEMA trace_backed_memory_v3_staging RESTRICT;

COMMIT;
