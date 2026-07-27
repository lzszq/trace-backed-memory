BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    replay_schema_version integer;
    replay_contract_version text;
    relation_names text[];
    function_names text[];
    trigger_names text[];
    constraint_names text[];
    column_names text[];
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 rollback requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO replay_schema_version, replay_contract_version
    FROM trace_backed_memory_v3_replay.schema_metadata
    WHERE singleton
    FOR UPDATE;

    IF replay_schema_version IS NULL
       OR replay_schema_version <> 1
       OR replay_contract_version <> 'tbm.replay.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_replay.schema_metadata,
        trace_backed_memory_v3_replay.replay_artifacts,
        trace_backed_memory_v3_replay.replay_injections,
        trace_backed_memory_v3_replay.replay_manifests
        IN ACCESS EXCLUSIVE MODE;

    SELECT array_agg(class.relname ORDER BY class.relname)
    INTO relation_names
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_replay'
      AND class.relkind IN ('r', 'i', 'p');
    IF relation_names <> ARRAY[
        'replay_artifacts',
        'replay_artifacts_content_sha256_key',
        'replay_artifacts_pkey',
        'replay_injections',
        'replay_injections_decision',
        'replay_injections_linkage_key',
        'replay_injections_pkey',
        'replay_manifests',
        'replay_manifests_decision',
        'replay_manifests_pkey',
        'replay_schema_metadata_pkey',
        'schema_metadata'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(procedure.proname ORDER BY procedure.proname)
    INTO function_names
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_replay';
    IF function_names <> ARRAY[
        'reject_immutable_change',
        'validate_injection_artifact'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO trigger_names
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class
      ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_replay'
      AND NOT trigger.tgisinternal;
    IF trigger_names <> ARRAY[
        'replay_artifacts_immutable',
        'replay_artifacts_no_truncate',
        'replay_injections_immutable',
        'replay_injections_no_truncate',
        'replay_injections_validate_artifact',
        'replay_manifests_immutable',
        'replay_manifests_no_truncate',
        'replay_schema_metadata_immutable',
        'replay_schema_metadata_no_truncate'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        constraint_record.conname
        ORDER BY constraint_record.conname
    )
    INTO constraint_names
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_replay'
      AND constraint_record.contype <> 'n';
    IF constraint_names <> ARRAY[
        'replay_artifacts_artifact_id_check',
        'replay_artifacts_classification_check',
        'replay_artifacts_content_check',
        'replay_artifacts_content_sha256_check',
        'replay_artifacts_content_sha256_key',
        'replay_artifacts_created_at_check',
        'replay_artifacts_derived_id_check',
        'replay_artifacts_encryption_key_id_check',
        'replay_artifacts_media_type_check',
        'replay_artifacts_pkey',
        'replay_artifacts_redaction_policy_id_check',
        'replay_artifacts_size_bytes_check',
        'replay_injections_artifact_fkey',
        'replay_injections_decision_id_check',
        'replay_injections_descriptor_check',
        'replay_injections_linkage_key',
        'replay_injections_pkey',
        'replay_injections_session_id_check',
        'replay_injections_usage_decision_id_check',
        'replay_manifests_completeness_check',
        'replay_manifests_decision_id_check',
        'replay_manifests_descriptor_check',
        'replay_manifests_injection_fkey',
        'replay_manifests_injection_shape',
        'replay_manifests_manifest_sha256_check',
        'replay_manifests_pkey',
        'replay_manifests_session_id_check',
        'replay_manifests_usage_decision_id_check',
        'replay_schema_metadata_contract_version_check',
        'replay_schema_metadata_pkey',
        'replay_schema_metadata_schema_version_check',
        'replay_schema_metadata_singleton_check'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        columns.table_name || '.' || columns.column_name
        ORDER BY columns.table_name, columns.ordinal_position
    )
    INTO column_names
    FROM information_schema.columns AS columns
    WHERE columns.table_schema = 'trace_backed_memory_v3_replay';
    IF column_names <> ARRAY[
        'replay_artifacts.artifact_id',
        'replay_artifacts.content_sha256',
        'replay_artifacts.size_bytes',
        'replay_artifacts.media_type',
        'replay_artifacts.classification',
        'replay_artifacts.created_at',
        'replay_artifacts.encryption_key_id',
        'replay_artifacts.redaction_policy_id',
        'replay_artifacts.content',
        'replay_injections.artifact_id',
        'replay_injections.session_id',
        'replay_injections.decision_id',
        'replay_injections.usage_decision_id',
        'replay_injections.descriptor',
        'replay_manifests.manifest_sha256',
        'replay_manifests.session_id',
        'replay_manifests.decision_id',
        'replay_manifests.usage_decision_id',
        'replay_manifests.injection_artifact_id',
        'replay_manifests.completeness',
        'replay_manifests.descriptor',
        'schema_metadata.singleton',
        'schema_metadata.schema_version',
        'schema_metadata.contract_version'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 rollback catalog mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_replay.replay_manifests
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_replay.replay_injections
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_replay.replay_artifacts
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_replay.schema_metadata
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_replay.validate_injection_artifact()
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_replay.reject_immutable_change()
    RESTRICT;
DROP SCHEMA trace_backed_memory_v3_replay RESTRICT;

COMMIT;
