BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    revision_version integer;
    publication_version integer;
    publication_contract text;
    actual_relations text[];
    actual_functions text[];
    actual_triggers text[];
    unsupported_count bigint;
    public_privilege_count bigint;
BEGIN
    SELECT schema_version INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;
    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication rollback requires active schema version 2';
    END IF;

    SELECT schema_version INTO revision_version
    FROM trace_backed_memory_v3_memory_revision.schema_metadata
    WHERE singleton = 1
    FOR UPDATE;
    IF revision_version IS NULL OR revision_version <> 1 THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication rollback dependency mismatch';
    END IF;

    SELECT schema_version, contract_version
    INTO publication_version, publication_contract
    FROM trace_backed_memory_v3_memory_publication.schema_metadata
    WHERE singleton = 1
    FOR UPDATE;
    IF publication_version IS NULL
       OR publication_version <> 1
       OR publication_contract <> 'tbm.memory-publication.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_memory_publication.schema_metadata,
        trace_backed_memory_v3_memory_publication.
            v3_memory_revision_approvals,
        trace_backed_memory_v3_memory_publication.
            v3_memory_revision_activations,
        trace_backed_memory_v3_memory_publication.
            v3_memory_revision_activation_heads
        IN ACCESS EXCLUSIVE MODE;

    SELECT count(*) INTO unsupported_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_publication'
      AND class.relkind NOT IN ('r', 'i', 'p');
    IF unsupported_count <> 0
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_policy AS policy
            JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname =
                'trace_backed_memory_v3_memory_publication'
       )
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_rewrite AS rule
            JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname =
                'trace_backed_memory_v3_memory_publication'
              AND rule.rulename <> '_RETURN'
       ) THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication rollback unsupported catalog object';
    END IF;

    SELECT pg_catalog.array_agg(class.relname ORDER BY class.relname)
    INTO actual_relations
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_publication'
      AND class.relkind IN ('r', 'i', 'p');
    IF actual_relations IS DISTINCT FROM ARRAY[
        'schema_metadata',
        'schema_metadata_pkey',
        'v3_memory_revision_activation_heads',
        'v3_memory_revision_activation_heads_current_activation_id_key',
        'v3_memory_revision_activation_heads_pkey',
        'v3_memory_revision_activation_tenant_id_repository_id_key_m_key',
        'v3_memory_revision_activations',
        'v3_memory_revision_activations_approval_id_key',
        'v3_memory_revision_activations_pkey',
        'v3_memory_revision_activations_revision_id_key',
        'v3_memory_revision_approvals',
        'v3_memory_revision_approvals_pkey',
        'v3_memory_revision_approvals_revision_id_key'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication rollback relation mismatch: %',
            actual_relations;
    END IF;

    SELECT pg_catalog.array_agg(procedure.proname ORDER BY procedure.proname)
    INTO actual_functions
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_publication';
    IF actual_functions IS DISTINCT FROM ARRAY[
        'reject_immutable_change',
        'validate_activation',
        'validate_approval',
        'validate_head_advance',
        'validate_head_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication rollback function mismatch';
    END IF;

    SELECT pg_catalog.array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO actual_triggers
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_publication'
      AND NOT trigger.tgisinternal;
    IF actual_triggers IS DISTINCT FROM ARRAY[
        'memory_publication_activation_immutable',
        'memory_publication_activation_no_truncate',
        'memory_publication_activation_validate',
        'memory_publication_approval_immutable',
        'memory_publication_approval_no_truncate',
        'memory_publication_approval_validate',
        'memory_publication_head_advance',
        'memory_publication_head_insert',
        'memory_publication_head_no_delete',
        'memory_publication_head_no_truncate',
        'memory_publication_metadata_immutable',
        'memory_publication_metadata_no_truncate'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication rollback trigger mismatch';
    END IF;

    SELECT count(*) INTO public_privilege_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_memory_publication'
      AND class.relkind IN ('r', 'p')
      AND (
          pg_catalog.has_table_privilege('public', class.oid, 'INSERT')
          OR pg_catalog.has_table_privilege('public', class.oid, 'UPDATE')
          OR pg_catalog.has_table_privilege('public', class.oid, 'DELETE')
          OR pg_catalog.has_table_privilege('public', class.oid, 'TRUNCATE')
      );
    IF public_privilege_count <> 0
       OR pg_catalog.has_schema_privilege(
            'public',
            'trace_backed_memory_v3_memory_publication',
            'USAGE'
       )
       OR pg_catalog.has_schema_privilege(
            'public',
            'trace_backed_memory_v3_memory_publication',
            'CREATE'
       )
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname =
                'trace_backed_memory_v3_memory_publication'
              AND NOT trigger.tgisinternal
              AND trigger.tgenabled <> 'O'
       )
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname =
                'trace_backed_memory_v3_memory_publication'
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
            'PostgreSQL memory publication rollback security mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_memory_publication.
        v3_memory_revision_activation_heads,
    trace_backed_memory_v3_memory_publication.
        v3_memory_revision_activations,
    trace_backed_memory_v3_memory_publication.
        v3_memory_revision_approvals,
    trace_backed_memory_v3_memory_publication.schema_metadata;

DROP FUNCTION
    trace_backed_memory_v3_memory_publication.validate_head_advance(),
    trace_backed_memory_v3_memory_publication.validate_head_insert(),
    trace_backed_memory_v3_memory_publication.validate_activation(),
    trace_backed_memory_v3_memory_publication.validate_approval(),
    trace_backed_memory_v3_memory_publication.reject_immutable_change();

DROP SCHEMA trace_backed_memory_v3_memory_publication RESTRICT;

COMMIT;
