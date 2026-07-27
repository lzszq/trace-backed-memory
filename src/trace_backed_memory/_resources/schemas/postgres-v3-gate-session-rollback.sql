BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    gate_schema_version integer;
    gate_contract_version text;
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
            'PostgreSQL GateSession v3 rollback requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO gate_schema_version, gate_contract_version
    FROM trace_backed_memory_v3_gate_session.schema_metadata
    WHERE singleton
    FOR UPDATE;

    IF gate_schema_version IS NULL
       OR gate_schema_version <> 1
       OR gate_contract_version <> 'tbm.gate-session.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession v3 rollback metadata mismatch';
    END IF;

    SELECT array_agg(class.relname ORDER BY class.relname)
    INTO relation_names
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_session'
      AND class.relkind IN ('r', 'i', 'p');
    IF relation_names <> ARRAY[
        'gate_session_heads',
        'gate_session_heads_idempotency_key',
        'gate_session_heads_pkey',
        'gate_session_revisions',
        'gate_session_revisions_due',
        'gate_session_revisions_pkey',
        'schema_metadata',
        'schema_metadata_pkey'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(procedure.proname ORDER BY procedure.proname)
    INTO function_names
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_session';
    IF function_names <> ARRAY[
        'protect_head_update',
        'reject_immutable_change',
        'validate_head_revision_consistency',
        'validate_revision_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO trigger_names
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class
      ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_session'
      AND NOT trigger.tgisinternal;
    IF trigger_names <> ARRAY[
        'gate_session_heads_immutable_delete',
        'gate_session_heads_insert_consistent_revision',
        'gate_session_heads_no_truncate',
        'gate_session_heads_protect_update',
        'gate_session_heads_update_consistent_revision',
        'gate_session_metadata_immutable',
        'gate_session_metadata_no_truncate',
        'gate_session_revisions_consistent_head',
        'gate_session_revisions_immutable_change',
        'gate_session_revisions_no_truncate',
        'gate_session_revisions_validate_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        constraint_record.conname
        ORDER BY constraint_record.conname
    )
    INTO constraint_names
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_session'
      AND constraint_record.contype <> 'n';
    IF constraint_names <> ARRAY[
        'gate_session_heads_agent_client_id_check',
        'gate_session_heads_current_version_check',
        'gate_session_heads_idempotency_key',
        'gate_session_heads_idempotency_key_check',
        'gate_session_heads_insert_consistent_revision',
        'gate_session_heads_pkey',
        'gate_session_heads_principal_id_check',
        'gate_session_heads_repository_id_check',
        'gate_session_heads_request_fingerprint_check',
        'gate_session_heads_run_id_check',
        'gate_session_heads_session_id_check',
        'gate_session_heads_tenant_id_check',
        'gate_session_heads_trace_id_check',
        'gate_session_heads_update_consistent_revision',
        'gate_session_revisions_consistent_head',
        'gate_session_revisions_expiry_shape',
        'gate_session_revisions_head_fkey',
        'gate_session_revisions_lease_shape',
        'gate_session_revisions_payload_check',
        'gate_session_revisions_pkey',
        'gate_session_revisions_status_check',
        'gate_session_revisions_version_check',
        'schema_metadata_contract_version_check',
        'schema_metadata_pkey',
        'schema_metadata_schema_version_check',
        'schema_metadata_singleton_check'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        columns.table_name || '.' || columns.column_name
        ORDER BY columns.table_name, columns.ordinal_position
    )
    INTO column_names
    FROM information_schema.columns AS columns
    WHERE columns.table_schema = 'trace_backed_memory_v3_gate_session';
    IF column_names <> ARRAY[
        'gate_session_heads.session_id',
        'gate_session_heads.tenant_id',
        'gate_session_heads.repository_id',
        'gate_session_heads.principal_id',
        'gate_session_heads.agent_client_id',
        'gate_session_heads.trace_id',
        'gate_session_heads.run_id',
        'gate_session_heads.request_fingerprint',
        'gate_session_heads.idempotency_key',
        'gate_session_heads.current_version',
        'gate_session_revisions.session_id',
        'gate_session_revisions.version',
        'gate_session_revisions.status',
        'gate_session_revisions.updated_at',
        'gate_session_revisions.expires_at',
        'gate_session_revisions.lease_expires_at',
        'gate_session_revisions.payload',
        'schema_metadata.singleton',
        'schema_metadata.schema_version',
        'schema_metadata.contract_version'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession v3 rollback catalog mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_gate_session.gate_session_revisions
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_gate_session.gate_session_heads
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_gate_session.schema_metadata
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_gate_session.validate_revision_insert()
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_gate_session
        .validate_head_revision_consistency()
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_gate_session.protect_head_update()
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change()
    RESTRICT;
DROP SCHEMA trace_backed_memory_v3_gate_session RESTRICT;

COMMIT;
