BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    gate_schema_version integer;
    gate_contract_version text;
    outcome_schema_version integer;
    outcome_contract_version text;
    attribution_schema_version integer;
    attribution_contract_version text;
    relation_names text[];
    function_names text[];
    trigger_names text[];
    constraint_names text[];
    column_names text[];
    catalog_sha256 text;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback requires active schema version 2';
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
            'PostgreSQL OutcomeAttribution rollback GateSession metadata mismatch';
    END IF;

    SELECT schema_version, contract_version
    INTO outcome_schema_version, outcome_contract_version
    FROM trace_backed_memory_v3_outcome.schema_metadata
    WHERE singleton
    FOR UPDATE;

    IF outcome_schema_version IS NULL
       OR outcome_schema_version <> 1
       OR outcome_contract_version <> 'tbm.run-outcome.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback RunOutcome metadata mismatch';
    END IF;

    SELECT schema_version, contract_version
    INTO attribution_schema_version, attribution_contract_version
    FROM trace_backed_memory_v3_outcome_attribution.schema_metadata
    WHERE singleton
    FOR UPDATE;

    IF attribution_schema_version IS NULL
       OR attribution_schema_version <> 1
       OR attribution_contract_version
            <> 'tbm.outcome-attribution.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_outcome_attribution.schema_metadata,
        trace_backed_memory_v3_outcome_attribution.outcome_attributions
        IN ACCESS EXCLUSIVE MODE;

    WITH descriptors AS (
        SELECT 'schema|' || namespace.nspname || '|' ||
               pg_catalog.has_schema_privilege(
                   'public', namespace.oid, 'USAGE'
               )::text || '|' ||
               pg_catalog.has_schema_privilege(
                   'public', namespace.oid, 'CREATE'
               )::text AS descriptor
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname =
            'trace_backed_memory_v3_outcome_attribution'
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
        WHERE namespace.nspname =
            'trace_backed_memory_v3_outcome_attribution'
          AND class.relkind IN ('r', 'i', 'p')
        UNION ALL
        SELECT 'column|' || class.relname || '|' ||
               attribute.attname || '|' ||
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
        WHERE namespace.nspname =
            'trace_backed_memory_v3_outcome_attribution'
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
                   'trace_backed_memory_v3_outcome_attribution.',
                   ''
               )
        FROM pg_catalog.pg_constraint AS constraint_record
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = constraint_record.connamespace
        WHERE namespace.nspname =
            'trace_backed_memory_v3_outcome_attribution'
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
                   'trace_backed_memory_v3_outcome_attribution.',
                   ''
               )
        FROM pg_catalog.pg_index AS index_record
        JOIN pg_catalog.pg_class AS index_class
          ON index_class.oid = index_record.indexrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = index_class.relnamespace
        WHERE namespace.nspname =
            'trace_backed_memory_v3_outcome_attribution'
        UNION ALL
        SELECT 'function|' || procedure.proname || '|' ||
               procedure.prokind::text || '|' ||
               language.lanname || '|' ||
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
        WHERE namespace.nspname =
            'trace_backed_memory_v3_outcome_attribution'
        UNION ALL
        SELECT 'trigger|' || trigger.tgname || '|' ||
               class.relname || '|' ||
               function_namespace.nspname || '.' ||
                   procedure.proname || '|' ||
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
                       'trace_backed_memory_v3_outcome_attribution.',
                       ''
                   ),
                   '-'
               )
        FROM pg_catalog.pg_trigger AS trigger
        JOIN pg_catalog.pg_class AS class
          ON class.oid = trigger.tgrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        JOIN pg_catalog.pg_proc AS procedure
          ON procedure.oid = trigger.tgfoid
        JOIN pg_catalog.pg_namespace AS function_namespace
          ON function_namespace.oid = procedure.pronamespace
        WHERE namespace.nspname =
            'trace_backed_memory_v3_outcome_attribution'
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

    IF catalog_sha256
       <> 'c526057d37173b03356901da43c2d0e5e67a62f95d33930cc06b9be1f5c511a1'
    THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback catalog mismatch';
    END IF;

    SELECT array_agg(class.relname ORDER BY class.relname)
    INTO relation_names
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
        'trace_backed_memory_v3_outcome_attribution'
      AND class.relkind IN ('r', 'i', 'p');
    IF relation_names <> ARRAY[
        'outcome_attributions',
        'outcome_attributions_by_outcome',
        'outcome_attributions_pkey',
        'schema_metadata',
        'schema_metadata_pkey'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback catalog mismatch';
    END IF;

    SELECT array_agg(procedure.proname ORDER BY procedure.proname)
    INTO function_names
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname =
        'trace_backed_memory_v3_outcome_attribution';
    IF function_names <> ARRAY[
        'reject_immutable_change',
        'validate_attribution_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback catalog mismatch';
    END IF;

    SELECT array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO trigger_names
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class
      ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
        'trace_backed_memory_v3_outcome_attribution'
      AND NOT trigger.tgisinternal;
    IF trigger_names <> ARRAY[
        'attribution_metadata_immutable',
        'attribution_metadata_no_truncate',
        'outcome_attributions_immutable_change',
        'outcome_attributions_no_truncate',
        'outcome_attributions_validate_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        constraint_record.conname
        ORDER BY constraint_record.conname
    )
    INTO constraint_names
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname =
        'trace_backed_memory_v3_outcome_attribution'
      AND constraint_record.contype <> 'n';
    IF constraint_names <> ARRAY[
        'outcome_attributions_attribution_id_check',
        'outcome_attributions_claim_strength_check',
        'outcome_attributions_confidence_json_check',
        'outcome_attributions_descriptor_check',
        'outcome_attributions_effect_check',
        'outcome_attributions_evaluator_id_check',
        'outcome_attributions_evaluator_version_check',
        'outcome_attributions_evidence_artifact_sha256s_json_check',
        'outcome_attributions_memory_revision_ids_json_check',
        'outcome_attributions_method_check',
        'outcome_attributions_pkey',
        'outcome_attributions_reason_check',
        'outcome_attributions_run_outcome_fkey',
        'outcome_attributions_run_outcome_id_check',
        'outcome_attributions_usage_decision_id_check',
        'outcome_attributions_verifier_id_check',
        'schema_metadata_contract_version_check',
        'schema_metadata_pkey',
        'schema_metadata_schema_version_check',
        'schema_metadata_singleton_check'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        columns.table_name || '.' || columns.column_name
        ORDER BY columns.table_name, columns.ordinal_position
    )
    INTO column_names
    FROM information_schema.columns AS columns
    WHERE columns.table_schema =
        'trace_backed_memory_v3_outcome_attribution';
    IF column_names <> ARRAY[
        'outcome_attributions.attribution_id',
        'outcome_attributions.run_outcome_id',
        'outcome_attributions.usage_decision_id',
        'outcome_attributions.memory_revision_ids_json',
        'outcome_attributions.claim_strength',
        'outcome_attributions.effect',
        'outcome_attributions.method',
        'outcome_attributions.evaluator_id',
        'outcome_attributions.evaluator_version',
        'outcome_attributions.verifier_id',
        'outcome_attributions.evidence_artifact_sha256s_json',
        'outcome_attributions.confidence_json',
        'outcome_attributions.reason',
        'outcome_attributions.recorded_at',
        'outcome_attributions.descriptor',
        'schema_metadata.singleton',
        'schema_metadata.schema_version',
        'schema_metadata.contract_version'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution rollback catalog mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_outcome_attribution.outcome_attributions
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_outcome_attribution.schema_metadata
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_outcome_attribution.validate_attribution_insert()
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_outcome_attribution.reject_immutable_change()
    RESTRICT;
DROP SCHEMA trace_backed_memory_v3_outcome_attribution RESTRICT;

COMMIT;
