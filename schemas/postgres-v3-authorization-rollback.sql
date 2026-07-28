BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    authorization_schema_version integer;
    authorization_contract_version text;
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
            'PostgreSQL authorization v3 rollback requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO authorization_schema_version, authorization_contract_version
    FROM trace_backed_memory_v3_authorization.schema_metadata
    WHERE singleton
    FOR UPDATE;

    IF authorization_schema_version IS NULL
       OR authorization_schema_version <> 1
       OR authorization_contract_version <> 'tbm.authorization.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_authorization.schema_metadata,
        trace_backed_memory_v3_authorization.authorization_policies,
        trace_backed_memory_v3_authorization.authorization_decisions
        IN ACCESS EXCLUSIVE MODE;

    SELECT array_agg(class.relname ORDER BY class.relname)
    INTO relation_names
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_authorization'
      AND class.relkind IN ('r', 'i', 'p');
    IF relation_names IS DISTINCT FROM ARRAY[
        'authorization_decisions',
        'authorization_decisions_pkey',
        'authorization_decisions_policy',
        'authorization_decisions_principal',
        'authorization_decisions_request_key',
        'authorization_policies',
        'authorization_policies_pkey',
        'authorization_policies_version_key',
        'authorization_schema_metadata_pkey',
        'schema_metadata'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(procedure.proname ORDER BY procedure.proname)
    INTO function_names
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_authorization';
    IF function_names IS DISTINCT FROM ARRAY[
        'reject_immutable_change'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO trigger_names
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class
      ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_authorization'
      AND NOT trigger.tgisinternal;
    IF trigger_names IS DISTINCT FROM ARRAY[
        'authorization_decisions_immutable',
        'authorization_decisions_no_truncate',
        'authorization_policies_immutable',
        'authorization_policies_no_truncate',
        'authorization_schema_metadata_immutable',
        'authorization_schema_metadata_no_truncate'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        constraint_record.conname
        ORDER BY constraint_record.conname
    )
    INTO constraint_names
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_authorization'
      AND constraint_record.contype <> 'n';
    IF constraint_names IS DISTINCT FROM ARRAY[
        'authorization_decisions_agent_client_id_check',
        'authorization_decisions_decided_at_check',
        'authorization_decisions_descriptor_check',
        'authorization_decisions_event_id_check',
        'authorization_decisions_permission_check',
        'authorization_decisions_pkey',
        'authorization_decisions_policy_fkey',
        'authorization_decisions_principal_id_check',
        'authorization_decisions_reason_check',
        'authorization_decisions_repository_id_check',
        'authorization_decisions_request_id_check',
        'authorization_decisions_request_key',
        'authorization_decisions_request_sha256_check',
        'authorization_decisions_tenant_id_check',
        'authorization_policies_descriptor_check',
        'authorization_policies_pkey',
        'authorization_policies_sha256_check',
        'authorization_policies_version_check',
        'authorization_policies_version_key',
        'authorization_schema_metadata_contract_version_check',
        'authorization_schema_metadata_pkey',
        'authorization_schema_metadata_schema_version_check',
        'authorization_schema_metadata_singleton_check'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        columns.table_name || '.' || columns.column_name
        ORDER BY columns.table_name, columns.ordinal_position
    )
    INTO column_names
    FROM information_schema.columns AS columns
    WHERE columns.table_schema = 'trace_backed_memory_v3_authorization';
    IF column_names IS DISTINCT FROM ARRAY[
        'authorization_decisions.authorization_event_id',
        'authorization_decisions.request_id',
        'authorization_decisions.request_sha256',
        'authorization_decisions.policy_sha256',
        'authorization_decisions.principal_id',
        'authorization_decisions.agent_client_id',
        'authorization_decisions.tenant_id',
        'authorization_decisions.repository_id',
        'authorization_decisions.permission',
        'authorization_decisions.allowed',
        'authorization_decisions.reason',
        'authorization_decisions.decided_at',
        'authorization_decisions.descriptor',
        'authorization_policies.policy_sha256',
        'authorization_policies.policy_version',
        'authorization_policies.descriptor',
        'schema_metadata.singleton',
        'schema_metadata.schema_version',
        'schema_metadata.contract_version'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 rollback catalog mismatch';
    END IF;

    WITH descriptors AS (
        SELECT 'schema|' || namespace.nspname || '|' ||
               (namespace.nspowner <> 0)::text || '|' ||
               COALESCE(
                   (
                       SELECT pg_catalog.string_agg(
                           CASE
                               WHEN acl.grantee = 0 THEN 'public'
                               WHEN acl.grantee = namespace.nspowner
                                   THEN 'owner'
                               ELSE 'other:' || acl.grantee::text
                           END || ':' || acl.privilege_type || ':' ||
                           acl.is_grantable::text,
                           ',' ORDER BY
                               acl.grantee,
                               acl.privilege_type
                       )
                       FROM pg_catalog.aclexplode(
                           COALESCE(
                               namespace.nspacl,
                               pg_catalog.acldefault(
                                   'n'::"char", namespace.nspowner
                               )
                           )
                       ) AS acl
                   ),
                   '-'
               ) || '|' ||
               pg_catalog.has_schema_privilege(
                   'public', namespace.oid, 'USAGE'
               )::text || '|' ||
               pg_catalog.has_schema_privilege(
                   'public', namespace.oid, 'CREATE'
               )::text AS descriptor
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_authorization'
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
               COALESCE(
                   (
                       SELECT pg_catalog.string_agg(
                           CASE
                               WHEN acl.grantee = 0 THEN 'public'
                               WHEN acl.grantee = class.relowner
                                   THEN 'owner'
                               ELSE 'other:' || acl.grantee::text
                           END || ':' || acl.privilege_type || ':' ||
                           acl.is_grantable::text,
                           ',' ORDER BY
                               acl.grantee,
                               acl.privilege_type
                       )
                       FROM pg_catalog.aclexplode(
                           COALESCE(
                               class.relacl,
                               pg_catalog.acldefault(
                                   CASE
                                       WHEN class.relkind = 'S' THEN 'S'
                                       ELSE 'r'
                                   END::"char",
                                   class.relowner
                               )
                           )
                       ) AS acl
                   ),
                   '-'
               ) || '|' ||
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
                  'trace_backed_memory_v3_authorization'
          AND class.relkind IN ('r', 'i', 'p')
        UNION ALL
        SELECT 'column|' || class.relname || '|' ||
               attribute.attname || '|' || attribute.attnum::text || '|' ||
               pg_catalog.format_type(
                   attribute.atttypid, attribute.atttypmod
               ) || '|' || attribute.attnotnull::text || '|' ||
               attribute.attidentity::text || '|' ||
               attribute.attgenerated::text || '|' ||
               attribute.attstorage::text || '|' ||
               attribute.attcompression::text || '|' ||
               COALESCE(
                   (
                       SELECT pg_catalog.string_agg(
                           CASE
                               WHEN acl.grantee = 0 THEN 'public'
                               WHEN acl.grantee = class.relowner
                                   THEN 'owner'
                               ELSE 'other:' || acl.grantee::text
                           END || ':' || acl.privilege_type || ':' ||
                           acl.is_grantable::text,
                           ',' ORDER BY
                               acl.grantee,
                               acl.privilege_type
                       )
                       FROM pg_catalog.aclexplode(
                           attribute.attacl
                       ) AS acl
                   ),
                   '-'
               ) || '|' ||
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
                  'trace_backed_memory_v3_authorization'
          AND class.relkind IN ('r', 'p')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        UNION ALL
        SELECT 'constraint|' || class.relname || '|' ||
               constraint_record.conname || '|' ||
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
                   'trace_backed_memory_v3_authorization.',
                   ''
               ) AS descriptor
        FROM pg_catalog.pg_constraint AS constraint_record
        JOIN pg_catalog.pg_class AS class
          ON class.oid = constraint_record.conrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = constraint_record.connamespace
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_authorization'
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
                   'trace_backed_memory_v3_authorization.',
                   ''
               )
        FROM pg_catalog.pg_index AS index_record
        JOIN pg_catalog.pg_class AS index_class
          ON index_class.oid = index_record.indexrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = index_class.relnamespace
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_authorization'
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
               pg_catalog.has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               )::text || '|' ||
               COALESCE(
                   (
                       SELECT pg_catalog.string_agg(
                           CASE
                               WHEN acl.grantee = 0 THEN 'public'
                               WHEN acl.grantee = procedure.proowner
                                   THEN 'owner'
                               ELSE 'other:' || acl.grantee::text
                           END || ':' || acl.privilege_type || ':' ||
                           acl.is_grantable::text,
                           ',' ORDER BY
                               acl.grantee,
                               acl.privilege_type
                       )
                       FROM pg_catalog.aclexplode(
                           COALESCE(
                               procedure.proacl,
                               pg_catalog.acldefault(
                                   'f'::"char", procedure.proowner
                               )
                           )
                       ) AS acl
                   ),
                   '-'
               ) || '|' ||
               COALESCE(
                   pg_catalog.array_to_string(procedure.proconfig, ','),
                   '-'
               ) || '|' || pg_catalog.btrim(procedure.prosrc)
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_language AS language
          ON language.oid = procedure.prolang
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_authorization'
        UNION ALL
        SELECT 'trigger|' || trigger.tgname || '|' || class.relname ||
               '|' || function_namespace.nspname || '.' ||
               procedure.proname || '|' || trigger.tgtype::text || '|' ||
               trigger.tgenabled::text || '|' ||
               trigger.tgdeferrable::text || '|' ||
               trigger.tginitdeferred::text || '|' ||
               pg_catalog.encode(trigger.tgargs, 'hex') || '|' ||
               COALESCE(
                   pg_catalog.replace(
                       pg_catalog.pg_get_expr(
                           trigger.tgqual, trigger.tgrelid
                       ),
                       'trace_backed_memory_v3_authorization.',
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
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_authorization'
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
       '7050f2ce5f457431c41fdab0fcfacf5a746054a854484dccd6b5195e850c466b'
    THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 rollback catalog fingerprint mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_authorization.authorization_decisions
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_authorization.authorization_policies
    RESTRICT;
DROP TABLE
    trace_backed_memory_v3_authorization.schema_metadata
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_authorization.reject_immutable_change()
    RESTRICT;
DROP SCHEMA trace_backed_memory_v3_authorization RESTRICT;

COMMIT;
