BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    evidence_version integer;
    evidence_contract text;
    semantic_version integer;
    semantic_contract text;
    policy_count bigint;
    rule_count bigint;
    unsupported_relation_count bigint;
    actual_relations text[];
    actual_functions text[];
    actual_triggers text[];
    catalog_sha256 text;
BEGIN
    SELECT active.schema_version,
           evidence.schema_version,
           evidence.contract_version,
           semantic.schema_version,
           semantic.contract_version
    INTO active_version,
         evidence_version,
         evidence_contract,
         semantic_version,
         semantic_contract
    FROM public.trace_backed_memory_schema AS active
    CROSS JOIN trace_backed_memory_v3_gate_evidence.schema_metadata
        AS evidence
    CROSS JOIN trace_backed_memory_v3_semantic_gate.schema_metadata
        AS semantic
    WHERE active.singleton
      AND evidence.singleton = 1
      AND semantic.singleton = 1
    FOR UPDATE OF active, evidence, semantic;

    IF active_version IS DISTINCT FROM 2
       OR evidence_version IS DISTINCT FROM 1
       OR evidence_contract IS DISTINCT FROM 'tbm.gate-evidence.v3'
       OR semantic_version IS DISTINCT FROM 1
       OR semantic_contract IS DISTINCT FROM
          'tbm.semantic-gate-attempt.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_semantic_gate.schema_metadata,
        trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads,
        trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
        IN ACCESS EXCLUSIVE MODE;

    SELECT count(*)
    INTO policy_count
    FROM pg_catalog.pg_policy AS policy
    JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
              'trace_backed_memory_v3_semantic_gate';

    SELECT count(*)
    INTO rule_count
    FROM pg_catalog.pg_rewrite AS rule
    JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
              'trace_backed_memory_v3_semantic_gate'
      AND rule.rulename <> '_RETURN';

    SELECT count(*)
    INTO unsupported_relation_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
              'trace_backed_memory_v3_semantic_gate'
      AND class.relkind NOT IN ('r', 'i', 'p');

    IF policy_count IS DISTINCT FROM 0
       OR rule_count IS DISTINCT FROM 0
       OR unsupported_relation_count IS DISTINCT FROM 0 THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 rollback unsupported catalog object';
    END IF;

    SELECT pg_catalog.array_agg(class.relname ORDER BY class.relname)
    INTO actual_relations
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
              'trace_backed_memory_v3_semantic_gate'
      AND class.relkind IN ('r', 'i', 'p');

    IF actual_relations IS DISTINCT FROM ARRAY[
        'schema_metadata',
        'schema_metadata_pkey',
        'v3_semantic_gate_attempt_heads',
        'v3_semantic_gate_attempt_heads_pkey',
        'v3_semantic_gate_attempts',
        'v3_semantic_gate_attempts_pkey',
        'v3_semantic_gate_attempts_session',
        'v3_semantic_gate_attempts_system_gate_evaluation_id_attempt_key',
        'v3_semantic_gate_attempts_system_gate_evaluation_id_sequenc_key'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 rollback relation catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        procedure.proname ORDER BY procedure.proname
    )
    INTO actual_functions
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname =
              'trace_backed_memory_v3_semantic_gate';

    IF actual_functions IS DISTINCT FROM ARRAY[
        'protect_head_update',
        'reject_immutable_change',
        'validate_attempt_insert',
        'validate_chain_consistency',
        'validate_head_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 rollback function catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO actual_triggers
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname =
              'trace_backed_memory_v3_semantic_gate'
      AND NOT trigger.tgisinternal;

    IF actual_triggers IS DISTINCT FROM ARRAY[
        'semantic_gate_attempt_consistency',
        'semantic_gate_attempt_immutable',
        'semantic_gate_attempt_insert',
        'semantic_gate_attempt_no_truncate',
        'semantic_gate_head_consistency',
        'semantic_gate_head_immutable_delete',
        'semantic_gate_head_insert',
        'semantic_gate_head_no_truncate',
        'semantic_gate_head_update',
        'semantic_gate_metadata_immutable',
        'semantic_gate_metadata_no_truncate'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 rollback trigger catalog mismatch';
    END IF;

    WITH descriptors AS (
    SELECT 'schema|' || namespace.nspname || '|' ||
           (namespace.nspowner <> 0)::text || '|' ||
           (namespace.nspowner = (
               SELECT active_class.relowner
               FROM pg_catalog.pg_class AS active_class
               JOIN pg_catalog.pg_namespace AS active_namespace
                 ON active_namespace.oid = active_class.relnamespace
               WHERE active_namespace.nspname = 'public'
                 AND active_class.relname = 'trace_backed_memory_schema'
                 AND active_class.relkind IN ('r', 'p')
           ))::text || '|' ||
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
                           ',' ORDER BY acl.grantee, acl.privilege_type
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
                  'trace_backed_memory_v3_semantic_gate'
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
                                           WHEN class.relkind = 'S' THEN 'S'
                                           ELSE 'r'
                                       END::"char",
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
                  'trace_backed_memory_v3_semantic_gate'
          AND class.relkind IN ('r', 'i', 'p')
        UNION ALL
        SELECT 'column|' || class.relname || '|' ||
               attribute.attname || '|' ||
               attribute.attnum::text || '|' ||
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
                           ',' ORDER BY acl.grantee, acl.privilege_type
                       )
                       FROM pg_catalog.aclexplode(attribute.attacl) AS acl
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
                  'trace_backed_memory_v3_semantic_gate'
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
                   'trace_backed_memory_v3_semantic_gate.',
                   ''
               )
        FROM pg_catalog.pg_constraint AS constraint_record
        JOIN pg_catalog.pg_class AS class
          ON class.oid = constraint_record.conrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = constraint_record.connamespace
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_semantic_gate'
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
                   'trace_backed_memory_v3_semantic_gate.',
                   ''
               )
        FROM pg_catalog.pg_index AS index_record
        JOIN pg_catalog.pg_class AS index_class
          ON index_class.oid = index_record.indexrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = index_class.relnamespace
        WHERE namespace.nspname =
                  'trace_backed_memory_v3_semantic_gate'
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
                           ',' ORDER BY acl.grantee, acl.privilege_type
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
                  'trace_backed_memory_v3_semantic_gate'
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
                       'trace_backed_memory_v3_semantic_gate.',
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
                  'trace_backed_memory_v3_semantic_gate'
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
       'fdb61aaf2a5c295b3d578eec2981dd09a9609c9aff1208fa59024daf641d66b4'
    THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 rollback catalog fingerprint mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads,
    trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts,
    trace_backed_memory_v3_semantic_gate.schema_metadata;

DROP FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_chain_consistency(),
    trace_backed_memory_v3_semantic_gate.validate_attempt_insert(),
    trace_backed_memory_v3_semantic_gate.protect_head_update(),
    trace_backed_memory_v3_semantic_gate.validate_head_insert(),
    trace_backed_memory_v3_semantic_gate.reject_immutable_change();

DROP SCHEMA trace_backed_memory_v3_semantic_gate RESTRICT;

COMMIT;
