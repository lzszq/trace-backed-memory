BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    evidence_schema_version integer;
    evidence_contract_version text;
    policy_count bigint;
    rule_count bigint;
    unsupported_relation_count bigint;
    actual_relations text[];
    actual_functions text[];
    actual_triggers text[];
    public_privilege_count bigint;
    drift_count bigint;
    catalog_sha256 text;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO evidence_schema_version, evidence_contract_version
    FROM trace_backed_memory_v3_gate_evidence.schema_metadata
    WHERE singleton = 1
    FOR UPDATE;

    IF evidence_schema_version IS NULL
       OR evidence_schema_version <> 1
       OR evidence_contract_version <> 'tbm.gate-evidence.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_gate_evidence.schema_metadata,
        trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots,
        trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations
        IN ACCESS EXCLUSIVE MODE;

    SELECT count(*)
    INTO policy_count
    FROM pg_catalog.pg_policy AS policy
    JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence';

    SELECT count(*)
    INTO rule_count
    FROM pg_catalog.pg_rewrite AS rule
    JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence'
      AND rule.rulename <> '_RETURN';

    SELECT count(*)
    INTO unsupported_relation_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence'
      AND class.relkind NOT IN ('r', 'i', 'p');

    IF policy_count IS DISTINCT FROM 0
       OR rule_count IS DISTINCT FROM 0
       OR unsupported_relation_count IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback unsupported catalog object';
    END IF;

    SELECT pg_catalog.array_agg(class.relname ORDER BY class.relname)
    INTO actual_relations
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence'
      AND class.relkind IN ('r', 'i', 'p');

    IF actual_relations IS DISTINCT FROM ARRAY[
        'schema_metadata',
        'schema_metadata_pkey',
        'v3_retrieval_snapshots',
        'v3_retrieval_snapshots_pkey',
        'v3_retrieval_snapshots_session',
        'v3_system_gate_evaluations',
        'v3_system_gate_evaluations_pkey',
        'v3_system_gate_evaluations_retrieval_snapshot_id_key',
        'v3_system_gate_evaluations_session'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback relation catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(procedure.proname ORDER BY procedure.proname)
    INTO actual_functions
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence';

    IF actual_functions IS DISTINCT FROM ARRAY[
        'reject_immutable_change',
        'validate_evaluation_parent'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback function catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO actual_triggers
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence'
      AND NOT trigger.tgisinternal;

    IF actual_triggers IS DISTINCT FROM ARRAY[
        'gate_evidence_evaluation_immutable',
        'gate_evidence_evaluation_no_truncate',
        'gate_evidence_evaluation_parent',
        'gate_evidence_metadata_immutable',
        'gate_evidence_metadata_no_truncate',
        'gate_evidence_snapshot_immutable',
        'gate_evidence_snapshot_no_truncate'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback trigger catalog mismatch';
    END IF;

    SELECT pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                COALESCE((
                    SELECT pg_catalog.string_agg(
                        pg_catalog.concat_ws(
                            '|',
                            'schema',
                            namespace.nspname,
                            (namespace.nspowner <> 0)::text,
                            COALESCE(namespace.nspacl::text, '-'),
                            pg_catalog.has_schema_privilege(
                                'public', namespace.oid, 'USAGE'
                            )::text,
                            pg_catalog.has_schema_privilege(
                                'public', namespace.oid, 'CREATE'
                            )::text
                        ),
                        E'\n' ORDER BY namespace.nspname
                    )
                    FROM pg_catalog.pg_namespace AS namespace
                    WHERE namespace.nspname =
                              'trace_backed_memory_v3_gate_evidence'
                ), '-') || E'\n' ||
                COALESCE((
                    SELECT pg_catalog.string_agg(
                        pg_catalog.concat_ws(
                            '|',
                            'relation',
                            class.relname,
                            class.relkind::text,
                            class.relpersistence::text,
                            COALESCE(access_method.amname, '-'),
                            class.relrowsecurity::text,
                            class.relforcerowsecurity::text,
                            class.relreplident::text,
                            class.relispartition::text,
                            (class.relowner = namespace.nspowner)::text,
                            COALESCE(class.relacl::text, '-'),
                            COALESCE(class.reloptions::text, '-')
                        ),
                        E'\n' ORDER BY class.relname
                    )
                    FROM pg_catalog.pg_class AS class
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = class.relnamespace
                    LEFT JOIN pg_catalog.pg_am AS access_method
                      ON access_method.oid = class.relam
                    WHERE namespace.nspname =
                              'trace_backed_memory_v3_gate_evidence'
                      AND class.relkind IN ('r', 'i', 'p')
                ), '-') || E'\n' ||
                COALESCE((
                    SELECT pg_catalog.string_agg(
                        pg_catalog.concat_ws(
                            '|',
                            'column',
                            class.relname,
                            attribute.attname,
                            attribute.attnum::text,
                            pg_catalog.format_type(
                                attribute.atttypid, attribute.atttypmod
                            ),
                            attribute.attnotnull::text,
                            attribute.attidentity::text,
                            attribute.attgenerated::text,
                            attribute.attstorage::text,
                            COALESCE(attribute.attacl::text, '-'),
                            COALESCE(
                                collation_namespace.nspname || '.' ||
                                collation_record.collname,
                                '-'
                            ),
                            COALESCE(
                                pg_catalog.pg_get_expr(
                                    default_record.adbin,
                                    default_record.adrelid
                                ),
                                '-'
                            )
                        ),
                        E'\n' ORDER BY class.relname, attribute.attnum
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
                      ON collation_namespace.oid =
                         collation_record.collnamespace
                    WHERE namespace.nspname =
                              'trace_backed_memory_v3_gate_evidence'
                      AND class.relkind IN ('r', 'p')
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                ), '-') || E'\n' ||
                COALESCE((
                    SELECT pg_catalog.string_agg(
                        pg_catalog.concat_ws(
                            '|',
                            'constraint',
                            class.relname,
                            constraint_record.conname,
                            constraint_record.contype::text,
                            constraint_record.condeferrable::text,
                            constraint_record.condeferred::text,
                            constraint_record.convalidated::text,
                            pg_catalog.pg_get_constraintdef(
                                constraint_record.oid, true
                            )
                        ),
                        E'\n' ORDER BY
                            class.relname,
                            constraint_record.conname
                    )
                    FROM pg_catalog.pg_constraint AS constraint_record
                    JOIN pg_catalog.pg_class AS class
                      ON class.oid = constraint_record.conrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = constraint_record.connamespace
                    WHERE namespace.nspname =
                              'trace_backed_memory_v3_gate_evidence'
                      AND constraint_record.contype <> 'n'
                ), '-') || E'\n' ||
                COALESCE((
                    SELECT pg_catalog.string_agg(
                        pg_catalog.concat_ws(
                            '|',
                            'index',
                            index_class.relname,
                            index_record.indisunique::text,
                            index_record.indisprimary::text,
                            index_record.indisexclusion::text,
                            index_record.indimmediate::text,
                            index_record.indisvalid::text,
                            index_record.indisready::text,
                            index_record.indislive::text,
                            index_record.indisreplident::text,
                            index_record.indnkeyatts::text,
                            index_record.indnatts::text,
                            pg_catalog.pg_get_indexdef(
                                index_record.indexrelid
                            )
                        ),
                        E'\n' ORDER BY index_class.relname
                    )
                    FROM pg_catalog.pg_index AS index_record
                    JOIN pg_catalog.pg_class AS index_class
                      ON index_class.oid = index_record.indexrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = index_class.relnamespace
                    WHERE namespace.nspname =
                              'trace_backed_memory_v3_gate_evidence'
                ), '-') || E'\n' ||
                COALESCE((
                    SELECT pg_catalog.string_agg(
                        pg_catalog.concat_ws(
                            '|',
                            'function',
                            procedure.proname,
                            procedure.prokind::text,
                            language.lanname,
                            procedure.prorettype::pg_catalog.regtype::text,
                            procedure.prosecdef::text,
                            procedure.proleakproof::text,
                            procedure.proisstrict::text,
                            procedure.provolatile::text,
                            procedure.proparallel::text,
                            procedure.pronargs::text,
                            procedure.pronargdefaults::text,
                            procedure.proargtypes::text,
                            COALESCE(procedure.proacl::text, '-'),
                            COALESCE(procedure.proconfig::text, '-'),
                            pg_catalog.btrim(procedure.prosrc)
                        ),
                        E'\n' ORDER BY procedure.proname
                    )
                    FROM pg_catalog.pg_proc AS procedure
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = procedure.pronamespace
                    JOIN pg_catalog.pg_language AS language
                      ON language.oid = procedure.prolang
                    WHERE namespace.nspname =
                              'trace_backed_memory_v3_gate_evidence'
                ), '-') || E'\n' ||
                COALESCE((
                    SELECT pg_catalog.string_agg(
                        pg_catalog.concat_ws(
                            '|',
                            'trigger',
                            trigger.tgname,
                            class.relname,
                            function_namespace.nspname || '.' ||
                                procedure.proname,
                            trigger.tgtype::text,
                            trigger.tgenabled::text,
                            trigger.tgdeferrable::text,
                            trigger.tginitdeferred::text,
                            pg_catalog.encode(trigger.tgargs, 'hex'),
                            pg_catalog.pg_get_triggerdef(trigger.oid, true)
                        ),
                        E'\n' ORDER BY trigger.tgname
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
                              'trace_backed_memory_v3_gate_evidence'
                      AND NOT trigger.tgisinternal
                ), '-'),
                'UTF8'
            )
        ),
        'hex'
    )
    INTO catalog_sha256;

    IF catalog_sha256 IS DISTINCT FROM (
       CASE
           WHEN pg_catalog.current_setting(
               'server_version_num'
           )::integer < 170000 THEN
               '3c48490b1699b302e2d057fbdca632f9edd005cd09f8c36901e2d1df92111e71'
           ELSE
               'e8387b20d5a9762e90694e07d4851a251bbbe900904272e12a6ba123454c3cce'
       END
    )
    THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback catalog fingerprint mismatch';
    END IF;

    SELECT count(*)
    INTO drift_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence'
      AND class.relkind IN ('r', 'p')
      AND (
          class.relrowsecurity
          OR class.relforcerowsecurity
          OR class.relpersistence <> 'p'
          OR class.reloptions IS NOT NULL
      );

    SELECT count(*)
    INTO public_privilege_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_gate_evidence'
      AND class.relkind IN ('r', 'p')
      AND (
          pg_catalog.has_table_privilege('public', class.oid, 'INSERT')
          OR pg_catalog.has_table_privilege('public', class.oid, 'UPDATE')
          OR pg_catalog.has_table_privilege('public', class.oid, 'DELETE')
          OR pg_catalog.has_table_privilege('public', class.oid, 'TRUNCATE')
      );

    IF drift_count IS DISTINCT FROM 0
       OR public_privilege_count IS DISTINCT FROM 0
       OR pg_catalog.has_schema_privilege(
           'public',
           'trace_backed_memory_v3_gate_evidence',
           'USAGE'
       )
       OR pg_catalog.has_schema_privilege(
           'public',
           'trace_backed_memory_v3_gate_evidence',
           'CREATE'
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_trigger AS trigger
           JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = class.relnamespace
           WHERE namespace.nspname =
                     'trace_backed_memory_v3_gate_evidence'
             AND NOT trigger.tgisinternal
             AND trigger.tgenabled <> 'O'
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS procedure
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = procedure.pronamespace
           WHERE namespace.nspname =
                     'trace_backed_memory_v3_gate_evidence'
             AND (
                 procedure.prosecdef
                 OR procedure.proconfig IS DISTINCT FROM
                    ARRAY['search_path=pg_catalog']::text[]
                 OR pg_catalog.has_function_privilege(
                     'public', procedure.oid, 'EXECUTE'
                 )
             )
       ) THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 rollback catalog security mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations,
    trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots,
    trace_backed_memory_v3_gate_evidence.schema_metadata;

DROP FUNCTION
    trace_backed_memory_v3_gate_evidence.validate_evaluation_parent(),
    trace_backed_memory_v3_gate_evidence.reject_immutable_change();

DROP SCHEMA trace_backed_memory_v3_gate_evidence RESTRICT;

COMMIT;
