BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    artifact_version integer;
    artifact_contract text;
    policy_count bigint;
    rule_count bigint;
    unsupported_relation_count bigint;
    catalog_sha256 text;
BEGIN
    SELECT active.schema_version,
           artifact.schema_version,
           artifact.contract_version
    INTO active_version,
         artifact_version,
         artifact_contract
    FROM public.trace_backed_memory_schema AS active
    CROSS JOIN trace_backed_memory_v3_artifacts.schema_metadata
        AS artifact
    WHERE active.singleton
      AND artifact.singleton
    FOR UPDATE OF active, artifact;

    IF active_version IS DISTINCT FROM 2
       OR artifact_version IS DISTINCT FROM 1
       OR artifact_contract IS DISTINCT FROM
          'tbm.artifact-authority.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL Artifact Authority rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_artifacts.schema_metadata,
        trace_backed_memory_v3_artifacts.encrypted_artifacts
        IN ACCESS EXCLUSIVE MODE;

    SELECT pg_catalog.count(*)
    INTO policy_count
    FROM pg_catalog.pg_policy AS policy
    JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts';

    SELECT pg_catalog.count(*)
    INTO rule_count
    FROM pg_catalog.pg_rewrite AS rule
    JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
      AND rule.rulename <> '_RETURN';

    SELECT pg_catalog.count(*)
    INTO unsupported_relation_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
      AND class.relkind NOT IN ('r', 'i', 'p');

    IF policy_count IS DISTINCT FROM 0
       OR rule_count IS DISTINCT FROM 0
       OR unsupported_relation_count IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION
            'PostgreSQL Artifact Authority rollback unsupported object';
    END IF;

    WITH descriptors AS (
        SELECT 'schema|' || namespace.nspname || '|' ||
               (namespace.nspowner <> 0)::text || '|' ||
               (namespace.nspowner = active_class.relowner)::text || '|' ||
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
        JOIN pg_catalog.pg_class AS active_class
          ON active_class.relname = 'trace_backed_memory_schema'
        JOIN pg_catalog.pg_namespace AS active_namespace
          ON active_namespace.oid = active_class.relnamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
          AND active_namespace.nspname = 'public'
          AND active_class.relkind IN ('r', 'p')
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
        WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
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
        WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
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
                   'trace_backed_memory_v3_artifacts.',
                   ''
               ) AS descriptor
        FROM pg_catalog.pg_constraint AS constraint_record
        JOIN pg_catalog.pg_class AS class
          ON class.oid = constraint_record.conrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = constraint_record.connamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
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
                   'trace_backed_memory_v3_artifacts.',
                   ''
               )
        FROM pg_catalog.pg_index AS index_record
        JOIN pg_catalog.pg_class AS index_class
          ON index_class.oid = index_record.indexrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = index_class.relnamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
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
               (procedure.proowner = namespace.nspowner)::text || '|' ||
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
        WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
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
                       'trace_backed_memory_v3_artifacts.',
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
        WHERE namespace.nspname = 'trace_backed_memory_v3_artifacts'
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
       '26f1ce0e8e2d9e0b61149a49bfd9f2c1d0d4516034e775b8593b85ac24047b6b'
    THEN
        RAISE EXCEPTION
            'PostgreSQL Artifact Authority rollback catalog mismatch: %',
            catalog_sha256;
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_artifacts.encrypted_artifacts,
    trace_backed_memory_v3_artifacts.schema_metadata
    RESTRICT;

DROP FUNCTION
    trace_backed_memory_v3_artifacts.verify_encrypted_artifact(),
    trace_backed_memory_v3_artifacts.reject_immutable_change()
    RESTRICT;

DROP SCHEMA trace_backed_memory_v3_artifacts RESTRICT;

COMMIT;
