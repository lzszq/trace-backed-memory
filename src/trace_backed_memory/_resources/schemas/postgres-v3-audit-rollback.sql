BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    audit_version integer;
    audit_contract text;
BEGIN
    SELECT schema_version INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;
    SELECT schema_version, contract_version
    INTO audit_version, audit_contract
    FROM trace_backed_memory_v3_audit.schema_metadata
    WHERE singleton
    FOR UPDATE;
    IF active_version <> 2
       OR audit_version <> 1
       OR audit_contract <> 'tbm.audit-event.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 rollback metadata mismatch';
    END IF;
END
$$;

LOCK TABLE
    trace_backed_memory_v3_audit.schema_metadata,
    trace_backed_memory_v3_audit.audit_stream_heads,
    trace_backed_memory_v3_audit.audit_events,
    trace_backed_memory_v3_audit.recovery_actions
IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
    relations text[];
    constraints text[];
    columns text[];
    functions text[];
    triggers text[];
    catalog_sha256 text;
BEGIN
    SELECT pg_catalog.array_agg(class.relname ORDER BY class.relname)
    INTO relations
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
      AND class.relkind IN ('r', 'i', 'p');
    IF relations IS DISTINCT FROM ARRAY[
        'audit_events',
        'audit_events_pkey',
        'audit_events_recovery_action_key',
        'audit_events_recovery_pair_key',
        'audit_events_session',
        'audit_events_stream_event_key',
        'audit_events_stream_sequence_key',
        'audit_events_type',
        'audit_schema_metadata_pkey',
        'audit_stream_heads',
        'audit_stream_heads_pkey',
        'recovery_actions',
        'recovery_actions_event_id_key',
        'recovery_actions_pkey',
        'recovery_actions_request_key',
        'recovery_actions_session',
        'schema_metadata'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 rollback relation catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        constraint_record.conname ORDER BY constraint_record.conname
    )
    INTO constraints
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
      AND constraint_record.contype <> 'n';
    IF constraints IS DISTINCT FROM ARRAY[
        'audit_events_actor_id_check',
        'audit_events_actor_type_check',
        'audit_events_consistent',
        'audit_events_descriptor_check',
        'audit_events_event_id_check',
        'audit_events_event_type_check',
        'audit_events_occurred_at_check',
        'audit_events_parent_fkey',
        'audit_events_payload_sha256_check',
        'audit_events_pkey',
        'audit_events_previous_event_id_check',
        'audit_events_reason_code_check',
        'audit_events_recovery_action_fkey',
        'audit_events_recovery_action_id_check',
        'audit_events_recovery_action_key',
        'audit_events_recovery_pair_key',
        'audit_events_recovery_shape_check',
        'audit_events_repository_id_check',
        'audit_events_run_id_check',
        'audit_events_sequence_check',
        'audit_events_session_id_check',
        'audit_events_stream_event_key',
        'audit_events_stream_fkey',
        'audit_events_stream_sequence_key',
        'audit_events_tenant_id_check',
        'audit_events_trace_id_check',
        'audit_schema_metadata_contract_version_check',
        'audit_schema_metadata_pkey',
        'audit_schema_metadata_schema_version_check',
        'audit_schema_metadata_singleton_check',
        'audit_stream_heads_consistent',
        'audit_stream_heads_event_id_check',
        'audit_stream_heads_pkey',
        'audit_stream_heads_repository_id_check',
        'audit_stream_heads_run_id_check',
        'audit_stream_heads_sequence_check',
        'audit_stream_heads_session_id_check',
        'audit_stream_heads_shape_check',
        'audit_stream_heads_stream_id_check',
        'audit_stream_heads_tenant_id_check',
        'audit_stream_heads_trace_id_check',
        'recovery_actions_descriptor_check',
        'recovery_actions_event_fkey',
        'recovery_actions_event_id_key',
        'recovery_actions_executor_id_check',
        'recovery_actions_finished_at_check',
        'recovery_actions_id_check',
        'recovery_actions_pair',
        'recovery_actions_pkey',
        'recovery_actions_request_key',
        'recovery_actions_request_sha256_check',
        'recovery_actions_result_check',
        'recovery_actions_run_id_check',
        'recovery_actions_session_id_check',
        'recovery_actions_trace_id_check'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 rollback constraint catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        table_name::text || '|' || column_name::text || '|' ||
        data_type::text || '|' || is_nullable::text || '|' ||
        COALESCE(collation_name::text, '-')
        ORDER BY table_name, column_name
    )
    INTO columns
    FROM information_schema.columns
    WHERE table_schema = 'trace_backed_memory_v3_audit';
    IF columns IS DISTINCT FROM ARRAY[
        'audit_events|actor_id|text|NO|C',
        'audit_events|actor_type|text|NO|C',
        'audit_events|descriptor|text|NO|C',
        'audit_events|event_id|text|NO|C',
        'audit_events|event_type|text|NO|C',
        'audit_events|occurred_at|text|NO|C',
        'audit_events|payload_sha256|text|NO|C',
        'audit_events|previous_event_id|text|YES|C',
        'audit_events|reason_code|text|NO|C',
        'audit_events|recovery_action_id|text|YES|C',
        'audit_events|repository_id|text|NO|C',
        'audit_events|run_id|text|NO|C',
        'audit_events|sequence|integer|NO|-',
        'audit_events|session_id|text|NO|C',
        'audit_events|stream_id|text|NO|C',
        'audit_events|tenant_id|text|NO|C',
        'audit_events|trace_id|text|NO|C',
        'audit_stream_heads|current_event_id|text|YES|C',
        'audit_stream_heads|current_sequence|integer|NO|-',
        'audit_stream_heads|repository_id|text|NO|C',
        'audit_stream_heads|run_id|text|NO|C',
        'audit_stream_heads|session_id|text|NO|C',
        'audit_stream_heads|stream_id|text|NO|C',
        'audit_stream_heads|tenant_id|text|NO|C',
        'audit_stream_heads|trace_id|text|NO|C',
        'recovery_actions|descriptor|text|NO|C',
        'recovery_actions|event_id|text|NO|C',
        'recovery_actions|executor_id|text|NO|C',
        'recovery_actions|finished_at|text|NO|C',
        'recovery_actions|recovery_action_id|text|NO|C',
        'recovery_actions|request_sha256|text|NO|C',
        'recovery_actions|result|text|NO|C',
        'recovery_actions|run_id|text|NO|C',
        'recovery_actions|session_id|text|NO|C',
        'recovery_actions|trace_id|text|NO|C',
        'schema_metadata|contract_version|text|NO|C',
        'schema_metadata|schema_version|integer|NO|-',
        'schema_metadata|singleton|boolean|NO|-'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 rollback column catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        procedure.proname || '|' || language.lanname || '|' ||
        procedure.prorettype::pg_catalog.regtype::text || '|' ||
        COALESCE(pg_catalog.array_to_string(procedure.proconfig, ','), '-') ||
        '|' || pg_catalog.md5(pg_catalog.btrim(procedure.prosrc))
        ORDER BY procedure.proname
    )
    INTO functions
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    JOIN pg_catalog.pg_language AS language
      ON language.oid = procedure.prolang
    WHERE namespace.nspname = 'trace_backed_memory_v3_audit';
    IF functions IS DISTINCT FROM ARRAY[
        'reject_immutable_change|plpgsql|trigger|search_path=pg_catalog|68da982c417bae9006eb83257bea82cc',
        'validate_event_insert|plpgsql|trigger|search_path=pg_catalog|7a8b08da28c287a29431090c05b6c8a0',
        'validate_head_insert|plpgsql|trigger|search_path=pg_catalog|811f4d76216f56044d9a36621afa48a6',
        'validate_head_update|plpgsql|trigger|search_path=pg_catalog|f27e941a48b812f8c07779cb0c5574b7',
        'validate_recovery_pair|plpgsql|trigger|search_path=pg_catalog|9a27304adbda19c774e4b9422286b8ab',
        'validate_stream_consistency|plpgsql|trigger|search_path=pg_catalog|b22cbba52a0f1bf6b4db19714ae40458'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 rollback function catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        trigger.tgname || '|' || class.relname || '|' ||
        function_namespace.nspname || '.' || procedure.proname || '|' ||
        trigger.tgtype::text || '|' || trigger.tgenabled::text
        ORDER BY trigger.tgname
    )
    INTO triggers
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    JOIN pg_catalog.pg_proc AS procedure
      ON procedure.oid = trigger.tgfoid
    JOIN pg_catalog.pg_namespace AS function_namespace
      ON function_namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
      AND NOT trigger.tgisinternal;
    IF triggers IS DISTINCT FROM ARRAY[
        'audit_events_append|audit_events|trace_backed_memory_v3_audit.validate_event_insert|7|O',
        'audit_events_consistent|audit_events|trace_backed_memory_v3_audit.validate_stream_consistency|5|O',
        'audit_events_immutable|audit_events|trace_backed_memory_v3_audit.reject_immutable_change|27|O',
        'audit_events_no_truncate|audit_events|trace_backed_memory_v3_audit.reject_immutable_change|34|O',
        'audit_schema_metadata_immutable|schema_metadata|trace_backed_memory_v3_audit.reject_immutable_change|27|O',
        'audit_schema_metadata_no_truncate|schema_metadata|trace_backed_memory_v3_audit.reject_immutable_change|34|O',
        'audit_stream_heads_advance|audit_stream_heads|trace_backed_memory_v3_audit.validate_head_update|19|O',
        'audit_stream_heads_consistent|audit_stream_heads|trace_backed_memory_v3_audit.validate_stream_consistency|21|O',
        'audit_stream_heads_immutable_delete|audit_stream_heads|trace_backed_memory_v3_audit.reject_immutable_change|11|O',
        'audit_stream_heads_initial|audit_stream_heads|trace_backed_memory_v3_audit.validate_head_insert|7|O',
        'audit_stream_heads_no_truncate|audit_stream_heads|trace_backed_memory_v3_audit.reject_immutable_change|34|O',
        'recovery_actions_immutable|recovery_actions|trace_backed_memory_v3_audit.reject_immutable_change|27|O',
        'recovery_actions_no_truncate|recovery_actions|trace_backed_memory_v3_audit.reject_immutable_change|34|O',
        'recovery_actions_pair|recovery_actions|trace_backed_memory_v3_audit.validate_recovery_pair|5|O'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 rollback trigger catalog mismatch';
    END IF;

    WITH descriptors AS (
        SELECT 'schema|' || namespace.nspname || '|' ||
               pg_catalog.has_schema_privilege(
                   'public', namespace.oid, 'USAGE'
               )::text || '|' ||
               pg_catalog.has_schema_privilege(
                   'public', namespace.oid, 'CREATE'
               )::text AS descriptor
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
        UNION ALL
        SELECT 'relation|' || class.relname || '|' ||
               class.relkind::text || '|' ||
               class.relpersistence::text || '|' ||
               COALESCE(access_method.amname, '-') || '|' ||
               class.relrowsecurity::text || '|' ||
               class.relforcerowsecurity::text || '|' ||
               class.relreplident::text || '|' ||
               class.relispartition::text || '|' ||
               (class.relowner = namespace.nspowner)::text || '|' ||
               CASE WHEN class.relkind IN ('r', 'p') THEN
                   pg_catalog.has_table_privilege(
                       'public', class.oid,
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                   )::text
               ELSE 'false' END || '|' ||
               COALESCE(
                   pg_catalog.array_to_string(class.reloptions, ','),
                   '-'
               )
        FROM pg_catalog.pg_class AS class
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        LEFT JOIN pg_catalog.pg_am AS access_method
          ON access_method.oid = class.relam
        WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
          AND class.relkind IN ('r', 'i', 'p')
        UNION ALL
        SELECT 'column|' || class.relname || '|' || attribute.attname || '|' ||
               attribute.attnum::text || '|' ||
               pg_catalog.format_type(
                   attribute.atttypid, attribute.atttypmod
               ) || '|' ||
               attribute.attnotnull::text || '|' ||
               attribute.attidentity::text || '|' ||
               attribute.attgenerated::text || '|' ||
               attribute.attstorage::text || '|' ||
               attribute.attcompression::text || '|' ||
               COALESCE(
                   collation_namespace.nspname || '.' ||
                   collation_record.collname,
                   '-'
               ) || '|' ||
               COALESCE(
                   pg_catalog.pg_get_expr(
                       default_record.adbin, default_record.adrelid
                   ),
                   '-'
               )
        FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_class AS class
          ON class.oid = attribute.attrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef AS default_record
          ON default_record.adrelid = attribute.attrelid
         AND default_record.adnum = attribute.attnum
        LEFT JOIN pg_catalog.pg_collation AS collation_record
          ON collation_record.oid = attribute.attcollation
        LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
          ON collation_namespace.oid = collation_record.collnamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
          AND class.relkind IN ('r', 'p')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        UNION ALL
        SELECT 'constraint|' || constraint_record.conname || '|' ||
               constraint_record.contype::text || '|' ||
               constraint_record.condeferrable::text || '|' ||
               constraint_record.condeferred::text || '|' ||
               constraint_record.convalidated::text || '|' ||
               constraint_record.conislocal::text || '|' ||
               constraint_record.coninhcount::text || '|' ||
               constraint_record.connoinherit::text || '|' ||
               constraint_record.confupdtype::text || '|' ||
               constraint_record.confdeltype::text || '|' ||
               constraint_record.confmatchtype::text || '|' ||
               pg_catalog.replace(
                   pg_catalog.pg_get_constraintdef(
                       constraint_record.oid, true
                   ),
                   'trace_backed_memory_v3_audit.',
                   ''
               )
        FROM pg_catalog.pg_constraint AS constraint_record
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = constraint_record.connamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
          AND constraint_record.contype <> 'n'
        UNION ALL
        SELECT 'index|' || index_class.relname || '|' ||
               index_record.indisunique::text || '|' ||
               index_record.indisprimary::text || '|' ||
               index_record.indisexclusion::text || '|' ||
               index_record.indimmediate::text || '|' ||
               index_record.indisvalid::text || '|' ||
               index_record.indisready::text || '|' ||
               index_record.indislive::text || '|' ||
               index_record.indisreplident::text || '|' ||
               index_record.indnkeyatts::text || '|' ||
               index_record.indnatts::text || '|' ||
               pg_catalog.replace(
                   pg_catalog.pg_get_indexdef(index_record.indexrelid),
                   'trace_backed_memory_v3_audit.',
                   ''
               )
        FROM pg_catalog.pg_index AS index_record
        JOIN pg_catalog.pg_class AS index_class
          ON index_class.oid = index_record.indexrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = index_class.relnamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
        UNION ALL
        SELECT 'function|' || procedure.proname || '|' ||
               procedure.prokind::text || '|' || language.lanname || '|' ||
               procedure.prorettype::pg_catalog.regtype::text || '|' ||
               procedure.prosecdef::text || '|' ||
               procedure.proleakproof::text || '|' ||
               procedure.proisstrict::text || '|' ||
               procedure.provolatile::text || '|' ||
               procedure.proparallel::text || '|' ||
               procedure.pronargs::text || '|' ||
               procedure.pronargdefaults::text || '|' ||
               procedure.proargtypes::text || '|' ||
               COALESCE(procedure.proallargtypes::text, '-') || '|' ||
               COALESCE(procedure.proargmodes::text, '-') || '|' ||
               COALESCE(procedure.proargnames::text, '-') || '|' ||
               pg_catalog.pg_get_function_identity_arguments(
                   procedure.oid
               ) || '|' ||
               (procedure.proowner = namespace.nspowner)::text || '|' ||
               pg_catalog.has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               )::text || '|' ||
               COALESCE(
                   pg_catalog.array_to_string(procedure.proconfig, ','),
                   '-'
               ) || '|' ||
               pg_catalog.replace(
                   pg_catalog.btrim(procedure.prosrc),
                   E'\r\n',
                   E'\n'
               )
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_language AS language
          ON language.oid = procedure.prolang
        WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
        UNION ALL
        SELECT 'trigger|' || trigger.tgname || '|' || class.relname || '|' ||
               function_namespace.nspname || '.' || procedure.proname || '|' ||
               trigger.tgtype::text || '|' ||
               trigger.tgenabled::text || '|' ||
               trigger.tgdeferrable::text || '|' ||
               trigger.tginitdeferred::text || '|' ||
               pg_catalog.encode(trigger.tgargs, 'hex') || '|' ||
               COALESCE(
                   pg_catalog.replace(
                       pg_catalog.pg_get_expr(
                           trigger.tgqual, trigger.tgrelid
                       ),
                       'trace_backed_memory_v3_audit.',
                       ''
                   ),
                   '-'
               )
        FROM pg_catalog.pg_trigger AS trigger
        JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        JOIN pg_catalog.pg_proc AS procedure
          ON procedure.oid = trigger.tgfoid
        JOIN pg_catalog.pg_namespace AS function_namespace
          ON function_namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_audit'
          AND NOT trigger.tgisinternal
    )
    SELECT pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                pg_catalog.string_agg(
                    descriptor, E'\n' ORDER BY descriptor
                ),
                'UTF8'
            )
        ),
        'hex'
    )
    INTO catalog_sha256
    FROM descriptors;
    IF catalog_sha256 IS DISTINCT FROM
       '96c8c201f2a6d7431d1fe547634496a4d458cd7ba0a4b2003a6393d3995a1d41'
    THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 rollback catalog fingerprint mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_audit.schema_metadata,
    trace_backed_memory_v3_audit.audit_stream_heads,
    trace_backed_memory_v3_audit.audit_events,
    trace_backed_memory_v3_audit.recovery_actions
RESTRICT;

DROP FUNCTION
    trace_backed_memory_v3_audit.reject_immutable_change(),
    trace_backed_memory_v3_audit.validate_head_insert(),
    trace_backed_memory_v3_audit.validate_event_insert(),
    trace_backed_memory_v3_audit.validate_head_update(),
    trace_backed_memory_v3_audit.validate_stream_consistency(),
    trace_backed_memory_v3_audit.validate_recovery_pair()
RESTRICT;

DROP SCHEMA trace_backed_memory_v3_audit RESTRICT;

COMMIT;
