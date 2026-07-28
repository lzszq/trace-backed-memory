BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    revision_schema_version integer;
    revision_contract_version text;
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
            'PostgreSQL memory revision v3 rollback requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO revision_schema_version, revision_contract_version
    FROM trace_backed_memory_v3_memory_revision.schema_metadata
    WHERE singleton = 1
    FOR UPDATE;

    IF revision_schema_version IS NULL
       OR revision_schema_version <> 1
       OR revision_contract_version <> 'tbm.memory-revision.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL memory revision v3 rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_memory_revision.schema_metadata,
        trace_backed_memory_v3_memory_revision.v3_fix_evidence,
        trace_backed_memory_v3_memory_revision.v3_regression_evidence,
        trace_backed_memory_v3_memory_revision.v3_memory_revision_proposals,
        trace_backed_memory_v3_memory_revision.
            v3_memory_revision_regression_evidence
        IN ACCESS EXCLUSIVE MODE;

    SELECT count(*)
    INTO policy_count
    FROM pg_catalog.pg_policy AS policy
    JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision';

    SELECT count(*)
    INTO rule_count
    FROM pg_catalog.pg_rewrite AS rule
    JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision'
      AND rule.rulename <> '_RETURN';

    SELECT count(*)
    INTO unsupported_relation_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision'
      AND class.relkind NOT IN ('r', 'i', 'p');

    IF policy_count IS DISTINCT FROM 0
       OR rule_count IS DISTINCT FROM 0
       OR unsupported_relation_count IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION
            'PostgreSQL memory revision v3 rollback unsupported catalog object';
    END IF;

    SELECT pg_catalog.array_agg(class.relname ORDER BY class.relname)
    INTO actual_relations
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision'
      AND class.relkind IN ('r', 'i', 'p');

    IF actual_relations IS DISTINCT FROM ARRAY[
        'schema_metadata',
        'schema_metadata_pkey',
        'v3_fix_evidence',
        'v3_fix_evidence_pkey',
        'v3_memory_revision_proposals',
        'v3_memory_revision_proposals_memory_id_revision_number_key',
        'v3_memory_revision_proposals_pkey',
        'v3_memory_revision_regression_evidence',
        'v3_memory_revision_regression_evidence_pkey',
        'v3_memory_revision_regression_evidence_revision_id_ordinal_key',
        'v3_regression_evidence',
        'v3_regression_evidence_pkey'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL memory revision v3 rollback relation catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(procedure.proname ORDER BY procedure.proname)
    INTO actual_functions
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision';

    IF actual_functions IS DISTINCT FROM ARRAY[
        'reject_immutable_change',
        'validate_revision_parent'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL memory revision v3 rollback function catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO actual_triggers
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision'
      AND NOT trigger.tgisinternal;

    IF actual_triggers IS DISTINCT FROM ARRAY[
        'memory_revision_fix_immutable',
        'memory_revision_fix_no_truncate',
        'memory_revision_link_immutable',
        'memory_revision_link_no_truncate',
        'memory_revision_metadata_immutable',
        'memory_revision_metadata_no_truncate',
        'memory_revision_proposal_immutable',
        'memory_revision_proposal_no_truncate',
        'memory_revision_proposal_parent',
        'memory_revision_regression_immutable',
        'memory_revision_regression_no_truncate'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL memory revision v3 rollback trigger catalog mismatch';
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
                              'trace_backed_memory_v3_memory_revision'
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
                              'trace_backed_memory_v3_memory_revision'
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
                              'trace_backed_memory_v3_memory_revision'
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
                              'trace_backed_memory_v3_memory_revision'
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
                              'trace_backed_memory_v3_memory_revision'
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
                              'trace_backed_memory_v3_memory_revision'
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
                              'trace_backed_memory_v3_memory_revision'
                      AND NOT trigger.tgisinternal
                ), '-'),
                'UTF8'
            )
        ),
        'hex'
    )
    INTO catalog_sha256;

    IF catalog_sha256 IS DISTINCT FROM
       'c3d5a2cd2844a511da55db890935b610b142932c56f2ca32fb8f41cdbe2e8a8c'
    THEN
        RAISE EXCEPTION
            'PostgreSQL memory revision v3 rollback catalog fingerprint mismatch';
    END IF;

    SELECT count(*)
    INTO drift_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision'
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
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_revision'
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
           'trace_backed_memory_v3_memory_revision',
           'USAGE'
       )
       OR pg_catalog.has_schema_privilege(
           'public',
           'trace_backed_memory_v3_memory_revision',
           'CREATE'
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_trigger AS trigger
           JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = class.relnamespace
           WHERE namespace.nspname =
                     'trace_backed_memory_v3_memory_revision'
             AND NOT trigger.tgisinternal
             AND trigger.tgenabled <> 'O'
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS procedure
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = procedure.pronamespace
           WHERE namespace.nspname =
                     'trace_backed_memory_v3_memory_revision'
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
            'PostgreSQL memory revision v3 rollback catalog security mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_memory_revision.
        v3_memory_revision_regression_evidence,
    trace_backed_memory_v3_memory_revision.v3_memory_revision_proposals,
    trace_backed_memory_v3_memory_revision.v3_regression_evidence,
    trace_backed_memory_v3_memory_revision.v3_fix_evidence,
    trace_backed_memory_v3_memory_revision.schema_metadata;

DROP FUNCTION
    trace_backed_memory_v3_memory_revision.validate_revision_parent(),
    trace_backed_memory_v3_memory_revision.reject_immutable_change();

DROP SCHEMA trace_backed_memory_v3_memory_revision RESTRICT;

COMMIT;
