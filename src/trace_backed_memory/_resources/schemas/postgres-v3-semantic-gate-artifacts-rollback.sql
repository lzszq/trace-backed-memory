BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    semantic_version integer;
    semantic_contract text;
    artifact_version integer;
    artifact_contract text;
    policy_count bigint;
    rule_count bigint;
    unsupported_relation_count bigint;
    catalog_sha256 text;
BEGIN
    SELECT active.schema_version,
           semantic.schema_version,
           semantic.contract_version,
           artifact.schema_version,
           artifact.contract_version
    INTO active_version,
         semantic_version,
         semantic_contract,
         artifact_version,
         artifact_contract
    FROM public.trace_backed_memory_schema AS active
    CROSS JOIN trace_backed_memory_v3_semantic_gate.schema_metadata
        AS semantic
    CROSS JOIN
        trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata
        AS artifact
    WHERE active.singleton
      AND semantic.singleton = 1
      AND artifact.singleton
    FOR UPDATE OF active, semantic, artifact;

    IF active_version IS DISTINCT FROM 2
       OR semantic_version IS DISTINCT FROM 1
       OR semantic_contract IS DISTINCT FROM
          'tbm.semantic-gate-attempt.v3'
       OR artifact_version IS DISTINCT FROM 1
       OR artifact_contract IS DISTINCT FROM
          'tbm.semantic-gate-artifact.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata,
        trace_backed_memory_v3_semantic_gate_artifacts.
            semantic_gate_artifacts,
        trace_backed_memory_v3_semantic_gate_artifacts.
            semantic_gate_artifact_bindings
        IN ACCESS EXCLUSIVE MODE;

    SELECT pg_catalog.count(*)
    INTO policy_count
    FROM pg_catalog.pg_policy AS policy
    JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
        'trace_backed_memory_v3_semantic_gate_artifacts';

    SELECT pg_catalog.count(*)
    INTO rule_count
    FROM pg_catalog.pg_rewrite AS rule
    JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
        'trace_backed_memory_v3_semantic_gate_artifacts'
      AND rule.rulename <> '_RETURN';

    SELECT pg_catalog.count(*)
    INTO unsupported_relation_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
        'trace_backed_memory_v3_semantic_gate_artifacts'
      AND class.relkind NOT IN ('r', 'i', 'p');

    IF policy_count IS DISTINCT FROM 0
       OR rule_count IS DISTINCT FROM 0
       OR unsupported_relation_count IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact rollback unsupported object';
    END IF;

    WITH descriptors AS (
        SELECT
            'schema|' || namespace.nspname || '|' ||
            (namespace.nspowner = active_class.relowner)::text || '|' ||
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
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_semantic_gate_artifacts'
          AND active_namespace.nspname = 'public'
          AND active_class.relkind IN ('r', 'p')
        UNION ALL
        SELECT
            'relation|' || class.relname || '|' ||
            class.relkind::text || '|' ||
            (class.relowner = namespace.nspowner)::text || '|' ||
            class.relrowsecurity::text || '|' ||
            class.relforcerowsecurity::text || '|' ||
            COALESCE(
                (
                    SELECT pg_catalog.string_agg(
                        CASE
                            WHEN acl.grantee = 0 THEN 'public'
                            WHEN acl.grantee = class.relowner THEN 'owner'
                            ELSE 'other'
                        END || ':' || acl.privilege_type || ':' ||
                        acl.is_grantable::text,
                        ',' ORDER BY acl.grantee, acl.privilege_type
                    )
                    FROM (
                        SELECT exploded.grantor,
                               exploded.grantee,
                               exploded.privilege_type,
                               exploded.is_grantable
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                class.relacl,
                                pg_catalog.acldefault(
                                    CASE
                                        WHEN class.relkind = 'S'
                                            THEN 'S'::"char"
                                        ELSE 'r'::"char"
                                    END,
                                    class.relowner
                                )
                            )
                        ) AS exploded
                        UNION ALL
                        SELECT class.relowner,
                               class.relowner,
                               'MAINTAIN'::text,
                               false
                        WHERE pg_catalog.current_setting(
                            'server_version_num'
                        )::integer < 170000
                    ) AS acl
                ),
                '-'
            )
        FROM pg_catalog.pg_class AS class
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_semantic_gate_artifacts'
          AND class.relkind IN ('r', 'i', 'p')
        UNION ALL
        SELECT
            'column|' || class.relname || '|' ||
            attribute.attnum::text || '|' || attribute.attname || '|' ||
            pg_catalog.format_type(
                attribute.atttypid, attribute.atttypmod
            ) || '|' || attribute.attnotnull::text || '|' ||
            COALESCE(
                pg_catalog.pg_get_expr(
                    default_value.adbin, default_value.adrelid
                ),
                '-'
            ) || '|' ||
            COALESCE(collation_value.collname, '-')
        FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_class AS class
          ON class.oid = attribute.attrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        LEFT JOIN pg_catalog.pg_collation AS collation_value
          ON collation_value.oid = attribute.attcollation
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_semantic_gate_artifacts'
          AND class.relkind IN ('r', 'p')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        UNION ALL
        SELECT
            'constraint|' || constraint_value.conname || '|' ||
            class.relname || '|' || constraint_value.contype::text || '|' ||
            constraint_value.condeferrable::text || '|' ||
            constraint_value.condeferred::text || '|' ||
            constraint_value.convalidated::text || '|' ||
            pg_catalog.pg_get_constraintdef(
                constraint_value.oid, true
            )
        FROM pg_catalog.pg_constraint AS constraint_value
        JOIN pg_catalog.pg_class AS class
          ON class.oid = constraint_value.conrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_semantic_gate_artifacts'
          AND constraint_value.contype <> 'n'
        UNION ALL
        SELECT
            'function|' || procedure.proname || '|' ||
            language.lanname || '|' || procedure.provolatile::text || '|' ||
            procedure.prosecdef::text || '|' ||
            (procedure.proowner = namespace.nspowner)::text || '|' ||
            pg_catalog.has_function_privilege(
                'public', procedure.oid, 'EXECUTE'
            )::text || '|' ||
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
                  'trace_backed_memory_v3_semantic_gate_artifacts'
        UNION ALL
        SELECT
            'trigger|' || trigger.tgname || '|' || class.relname || '|' ||
            function_namespace.nspname || '.' || procedure.proname || '|' ||
            trigger.tgtype::text || '|' || trigger.tgenabled::text || '|' ||
            trigger.tgdeferrable::text || '|' ||
            trigger.tginitdeferred::text || '|' ||
            pg_catalog.encode(trigger.tgargs, 'hex') || '|' ||
            COALESCE(
                pg_catalog.pg_get_expr(trigger.tgqual, trigger.tgrelid),
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
                  'trace_backed_memory_v3_semantic_gate_artifacts'
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
       '8d0e4d3726909c9e4a74d9bab2328396666c0f0c2a8e8ab0588f4d4887040a4e'
    THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact rollback catalog mismatch: %',
            catalog_sha256;
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_semantic_gate_artifacts.
        semantic_gate_artifact_bindings,
    trace_backed_memory_v3_semantic_gate_artifacts.semantic_gate_artifacts,
    trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata
    RESTRICT;

DROP FUNCTION
    trace_backed_memory_v3_semantic_gate_artifacts.
        verify_artifact_binding(),
    trace_backed_memory_v3_semantic_gate_artifacts.
        verify_artifact_content(),
    trace_backed_memory_v3_semantic_gate_artifacts.
        reject_immutable_change()
    RESTRICT;

DROP SCHEMA
    trace_backed_memory_v3_semantic_gate_artifacts
    RESTRICT;

COMMIT;
