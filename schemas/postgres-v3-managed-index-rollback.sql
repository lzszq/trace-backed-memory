BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    managed_schema_version integer;
    managed_contract_version text;
    relation_names text[];
    function_names text[];
    trigger_names text[];
    constraint_names text[];
    constraint_descriptors text[];
    column_descriptors text[];
    policy_count bigint;
    rule_count bigint;
    unsupported_relation_count bigint;
    schema_acl_exact boolean;
    relation_acl_exact boolean;
    function_acl_exact boolean;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO managed_schema_version, managed_contract_version
    FROM trace_backed_memory_v3_managed_index.schema_metadata
    WHERE singleton = 1
    FOR UPDATE;

    IF managed_schema_version IS NULL
       OR managed_schema_version <> 1
       OR managed_contract_version <> 'tbm.managed-index-bundle.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_managed_index.schema_metadata,
        trace_backed_memory_v3_managed_index.v3_managed_index_bundles,
        trace_backed_memory_v3_managed_index.v3_managed_index_heads
        IN ACCESS EXCLUSIVE MODE;

    SELECT count(*)
    INTO policy_count
    FROM pg_catalog.pg_policy AS policy
    JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index';

    SELECT count(*)
    INTO rule_count
    FROM pg_catalog.pg_rewrite AS rule
    JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
      AND rule.rulename <> '_RETURN';

    SELECT count(*)
    INTO unsupported_relation_count
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
      AND class.relkind NOT IN ('r', 'i');

    IF policy_count <> 0
       OR rule_count <> 0
       OR unsupported_relation_count <> 0 THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback unsupported catalog object';
    END IF;

    SELECT pg_catalog.array_agg(class.relname ORDER BY class.relname)
    INTO relation_names
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
      AND class.relkind IN ('r', 'i');

    IF relation_names IS DISTINCT FROM ARRAY[
        'managed_index_bundles_pkey',
        'managed_index_bundles_scope_key',
        'managed_index_heads_pkey',
        'managed_index_schema_metadata_pkey',
        'schema_metadata',
        'v3_managed_index_bundles',
        'v3_managed_index_bundles_scope',
        'v3_managed_index_heads'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback relation catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        routine.proname || '|' ||
        pg_catalog.pg_get_function_identity_arguments(routine.oid) || '|' ||
        routine.prorettype::pg_catalog.regtype::text || '|' ||
        routine.prokind::text || '|' || routine.pronargs::text
        ORDER BY routine.proname,
                 pg_catalog.pg_get_function_identity_arguments(routine.oid)
    )
    INTO function_names
    FROM pg_catalog.pg_proc AS routine
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = routine.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index';

    IF function_names IS DISTINCT FROM ARRAY[
        'reject_immutable_change||trigger|f|0',
        'validate_head_advance||trigger|f|0'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback function catalog mismatch';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS routine
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = routine.pronamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
          AND (
              routine.prosecdef
              OR routine.proconfig IS DISTINCT FROM ARRAY[
                  'search_path=pg_catalog'
              ]::text[]
              OR routine.provolatile <> 'v'
              OR pg_catalog.btrim(pg_catalog.regexp_replace(
                  routine.prosrc,
                  '[[:space:]]+',
                  ' ',
                  'g'
              )) IS DISTINCT FROM CASE routine.proname
                  WHEN 'reject_immutable_change' THEN
                      'BEGIN RAISE EXCEPTION ''managed index immutable relation cannot be changed''; END;'
                  WHEN 'validate_head_advance' THEN
                      'BEGIN IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.repository_id IS DISTINCT FROM OLD.repository_id OR NEW.environment_id IS DISTINCT FROM OLD.environment_id OR NEW.head_version IS DISTINCT FROM OLD.head_version + 1 OR NEW.bundle_id IS NOT DISTINCT FROM OLD.bundle_id THEN RAISE EXCEPTION ''managed index head update must be one CAS advance''; END IF; RETURN NEW; END;'
                  ELSE NULL
              END
          )
    ) THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback function security mismatch';
    END IF;

    SELECT pg_catalog.array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO trigger_names
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class
      ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
      AND NOT trigger.tgisinternal;

    IF trigger_names IS DISTINCT FROM ARRAY[
        'managed_index_bundles_immutable',
        'managed_index_bundles_no_truncate',
        'managed_index_heads_cas',
        'managed_index_heads_no_delete',
        'managed_index_heads_no_truncate',
        'managed_index_schema_immutable',
        'managed_index_schema_no_truncate'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback trigger catalog mismatch';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_trigger AS trigger
        JOIN pg_catalog.pg_class AS class
          ON class.oid = trigger.tgrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = class.relnamespace
        WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
          AND NOT trigger.tgisinternal
          AND trigger.tgenabled <> 'O'
    ) THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback disabled trigger';
    END IF;

    SELECT pg_catalog.array_agg(constraint_record.conname
                                ORDER BY constraint_record.conname)
    INTO constraint_names
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
      AND constraint_record.contype <> 'n';

    IF constraint_names IS DISTINCT FROM ARRAY[
        'managed_index_bundle_catalog_digest_check',
        'managed_index_bundle_environment_check',
        'managed_index_bundle_id_check',
        'managed_index_bundle_payload_check',
        'managed_index_bundle_repository_check',
        'managed_index_bundle_retriever_check',
        'managed_index_bundle_retriever_version_check',
        'managed_index_bundle_tenant_check',
        'managed_index_bundles_pkey',
        'managed_index_bundles_scope_key',
        'managed_index_head_environment_check',
        'managed_index_head_repository_check',
        'managed_index_head_tenant_check',
        'managed_index_head_version_check',
        'managed_index_heads_bundle_fkey',
        'managed_index_heads_pkey',
        'managed_index_schema_contract_check',
        'managed_index_schema_metadata_pkey',
        'managed_index_schema_singleton_check',
        'managed_index_schema_version_check'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback constraint catalog mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        constraint_record.conname || '|' ||
        constraint_record.contype::text || '|' ||
        constraint_record.condeferrable::text || '|' ||
        constraint_record.condeferred::text || '|' ||
        constraint_record.convalidated::text || '|' ||
        pg_catalog.replace(
            pg_catalog.pg_get_constraintdef(
                constraint_record.oid,
                true
            ),
            'trace_backed_memory_v3_managed_index.',
            ''
        )
        ORDER BY constraint_record.conname
    )
    INTO constraint_descriptors
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
      AND constraint_record.contype <> 'n';

    IF constraint_descriptors IS DISTINCT FROM ARRAY[
        'managed_index_bundle_catalog_digest_check|c|false|false|true|CHECK (source_catalog_sha256 ~ ''^sha256:[0-9a-f]{64}$''::text)',
        'managed_index_bundle_environment_check|c|false|false|true|CHECK (char_length(environment_id) >= 1 AND char_length(environment_id) <= 128 AND btrim(environment_id) <> ''''::text)',
        'managed_index_bundle_id_check|c|false|false|true|CHECK (bundle_id ~ ''^managed_index_bundle_sha256_[0-9a-f]{64}$''::text)',
        'managed_index_bundle_payload_check|c|false|false|true|CHECK (octet_length(payload_utf8) >= 2 AND octet_length(payload_utf8) <= 67108864)',
        'managed_index_bundle_repository_check|c|false|false|true|CHECK (char_length(repository_id) >= 1 AND char_length(repository_id) <= 128 AND btrim(repository_id) <> ''''::text)',
        'managed_index_bundle_retriever_check|c|false|false|true|CHECK (char_length(retriever_id) >= 1 AND char_length(retriever_id) <= 128 AND btrim(retriever_id) <> ''''::text)',
        'managed_index_bundle_retriever_version_check|c|false|false|true|CHECK (char_length(retriever_version) >= 1 AND char_length(retriever_version) <= 128 AND btrim(retriever_version) <> ''''::text)',
        'managed_index_bundle_tenant_check|c|false|false|true|CHECK (char_length(tenant_id) >= 1 AND char_length(tenant_id) <= 128 AND btrim(tenant_id) <> ''''::text)',
        'managed_index_bundles_pkey|p|false|false|true|PRIMARY KEY (bundle_id)',
        'managed_index_bundles_scope_key|u|false|false|true|UNIQUE (tenant_id, repository_id, environment_id, bundle_id)',
        'managed_index_head_environment_check|c|false|false|true|CHECK (char_length(environment_id) >= 1 AND char_length(environment_id) <= 128 AND btrim(environment_id) <> ''''::text)',
        'managed_index_head_repository_check|c|false|false|true|CHECK (char_length(repository_id) >= 1 AND char_length(repository_id) <= 128 AND btrim(repository_id) <> ''''::text)',
        'managed_index_head_tenant_check|c|false|false|true|CHECK (char_length(tenant_id) >= 1 AND char_length(tenant_id) <= 128 AND btrim(tenant_id) <> ''''::text)',
        'managed_index_head_version_check|c|false|false|true|CHECK (head_version >= 1)',
        'managed_index_heads_bundle_fkey|f|false|false|true|FOREIGN KEY (tenant_id, repository_id, environment_id, bundle_id) REFERENCES v3_managed_index_bundles(tenant_id, repository_id, environment_id, bundle_id) ON UPDATE RESTRICT ON DELETE RESTRICT',
        'managed_index_heads_pkey|p|false|false|true|PRIMARY KEY (tenant_id, repository_id, environment_id)',
        'managed_index_schema_contract_check|c|false|false|true|CHECK (contract_version = ''tbm.managed-index-bundle.v3''::text)',
        'managed_index_schema_metadata_pkey|p|false|false|true|PRIMARY KEY (singleton)',
        'managed_index_schema_singleton_check|c|false|false|true|CHECK (singleton = 1)',
        'managed_index_schema_version_check|c|false|false|true|CHECK (schema_version = 1)'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback constraint definition mismatch';
    END IF;

    SELECT pg_catalog.array_agg(
        class.relname || '|' || attribute.attnum::text || '|' ||
        attribute.attname || '|' ||
        pg_catalog.format_type(
            attribute.atttypid,
            attribute.atttypmod
        ) || '|' || attribute.attnotnull::text || '|' ||
        COALESCE(
            collation_namespace.nspname || '.' ||
            collation_record.collname,
            '-'
        )
        ORDER BY class.relname, attribute.attnum
    )
    INTO column_descriptors
    FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS class
      ON class.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    LEFT JOIN pg_catalog.pg_collation AS collation_record
      ON collation_record.oid = attribute.attcollation
    LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
      ON collation_namespace.oid = collation_record.collnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index'
      AND class.relkind = 'r'
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped;

    IF column_descriptors IS DISTINCT FROM ARRAY[
        'schema_metadata|1|singleton|integer|true|-',
        'schema_metadata|2|schema_version|integer|true|-',
        'schema_metadata|3|contract_version|text|true|pg_catalog.C',
        'v3_managed_index_bundles|1|bundle_id|text|true|pg_catalog.C',
        'v3_managed_index_bundles|2|tenant_id|text|true|pg_catalog.C',
        'v3_managed_index_bundles|3|repository_id|text|true|pg_catalog.C',
        'v3_managed_index_bundles|4|environment_id|text|true|pg_catalog.C',
        'v3_managed_index_bundles|5|retriever_id|text|true|pg_catalog.C',
        'v3_managed_index_bundles|6|retriever_version|text|true|pg_catalog.C',
        'v3_managed_index_bundles|7|source_catalog_sha256|text|true|pg_catalog.C',
        'v3_managed_index_bundles|8|payload_utf8|bytea|true|-',
        'v3_managed_index_bundles|9|appended_at|timestamp with time zone|true|-',
        'v3_managed_index_heads|1|tenant_id|text|true|pg_catalog.C',
        'v3_managed_index_heads|2|repository_id|text|true|pg_catalog.C',
        'v3_managed_index_heads|3|environment_id|text|true|pg_catalog.C',
        'v3_managed_index_heads|4|bundle_id|text|true|pg_catalog.C',
        'v3_managed_index_heads|5|head_version|bigint|true|-'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback column catalog mismatch';
    END IF;

    IF pg_catalog.has_schema_privilege(
        'public',
        'trace_backed_memory_v3_managed_index',
        'USAGE'
    ) OR pg_catalog.has_schema_privilege(
        'public',
        'trace_backed_memory_v3_managed_index',
        'CREATE'
    ) THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback schema ACL mismatch';
    END IF;

    SELECT
        (
            SELECT pg_catalog.array_agg(
                CASE
                    WHEN acl.grantee = namespace.nspowner THEN 'owner'
                    ELSE 'other:' || acl.grantee::text
                END || ':' ||
                CASE
                    WHEN acl.grantor = namespace.nspowner THEN 'owner'
                    ELSE 'other:' || acl.grantor::text
                END || ':' || acl.privilege_type || ':' ||
                acl.is_grantable::text
                ORDER BY acl.grantee, acl.privilege_type
            )
            FROM pg_catalog.aclexplode(
                COALESCE(
                    namespace.nspacl,
                    pg_catalog.acldefault(
                        'n'::"char",
                        namespace.nspowner
                    )
                )
            ) AS acl
        ) = (
            SELECT pg_catalog.array_agg(
                'owner:owner:' || acl.privilege_type || ':' ||
                acl.is_grantable::text
                ORDER BY acl.privilege_type
            )
            FROM pg_catalog.aclexplode(
                pg_catalog.acldefault(
                    'n'::"char",
                    namespace.nspowner
                )
            ) AS acl
            WHERE acl.grantee = namespace.nspowner
              AND acl.grantor = namespace.nspowner
        ),
        NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS relation_namespace
              ON relation_namespace.oid = class.relnamespace
            WHERE relation_namespace.nspname =
                      'trace_backed_memory_v3_managed_index'
              AND class.relkind IN ('r', 'i')
              AND (
                  class.relowner <> relation_namespace.nspowner
                  OR (class.relkind = 'i' AND class.relacl IS NOT NULL)
                  OR (
                      class.relkind = 'r'
                      AND (
                          SELECT pg_catalog.array_agg(
                              CASE
                                  WHEN acl.grantee = class.relowner
                                      THEN 'owner'
                                  ELSE 'other:' || acl.grantee::text
                              END || ':' ||
                              CASE
                                  WHEN acl.grantor = class.relowner
                                      THEN 'owner'
                                  ELSE 'other:' || acl.grantor::text
                              END || ':' || acl.privilege_type || ':' ||
                              acl.is_grantable::text
                              ORDER BY acl.grantee, acl.privilege_type
                          )
                          FROM pg_catalog.aclexplode(
                              COALESCE(
                                  class.relacl,
                                  pg_catalog.acldefault(
                                      'r'::"char",
                                      class.relowner
                                  )
                              )
                          ) AS acl
                      ) IS DISTINCT FROM (
                          SELECT pg_catalog.array_agg(
                              'owner:owner:' || acl.privilege_type || ':' ||
                              acl.is_grantable::text
                              ORDER BY acl.privilege_type
                          )
                          FROM pg_catalog.aclexplode(
                              pg_catalog.acldefault(
                                  'r'::"char",
                                  class.relowner
                              )
                          ) AS acl
                          WHERE acl.grantee = class.relowner
                            AND acl.grantor = class.relowner
                      )
                  )
              )
        ),
        NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS routine
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = routine.pronamespace
            WHERE function_namespace.nspname =
                      'trace_backed_memory_v3_managed_index'
              AND (
                  routine.proowner <> function_namespace.nspowner
                  OR (
                      SELECT pg_catalog.array_agg(
                          CASE
                              WHEN acl.grantee = routine.proowner
                                  THEN 'owner'
                              ELSE 'other:' || acl.grantee::text
                          END || ':' ||
                          CASE
                              WHEN acl.grantor = routine.proowner
                                  THEN 'owner'
                              ELSE 'other:' || acl.grantor::text
                          END || ':' || acl.privilege_type || ':' ||
                          acl.is_grantable::text
                          ORDER BY acl.grantee, acl.privilege_type
                      )
                      FROM pg_catalog.aclexplode(
                          COALESCE(
                              routine.proacl,
                              pg_catalog.acldefault(
                                  'f'::"char",
                                  routine.proowner
                              )
                          )
                      ) AS acl
                  ) IS DISTINCT FROM (
                      SELECT pg_catalog.array_agg(
                          'owner:owner:' || acl.privilege_type || ':' ||
                          acl.is_grantable::text
                          ORDER BY acl.privilege_type
                      )
                      FROM pg_catalog.aclexplode(
                          pg_catalog.acldefault(
                              'f'::"char",
                              routine.proowner
                          )
                      ) AS acl
                      WHERE acl.grantee = routine.proowner
                        AND acl.grantor = routine.proowner
                  )
              )
        )
    INTO schema_acl_exact, relation_acl_exact, function_acl_exact
    FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_managed_index';

    IF schema_acl_exact IS DISTINCT FROM TRUE
       OR relation_acl_exact IS DISTINCT FROM TRUE
       OR function_acl_exact IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 rollback object ACL mismatch';
    END IF;
END
$$;

DROP TABLE trace_backed_memory_v3_managed_index.v3_managed_index_heads;
DROP TABLE trace_backed_memory_v3_managed_index.v3_managed_index_bundles;
DROP TABLE trace_backed_memory_v3_managed_index.schema_metadata;
DROP FUNCTION
    trace_backed_memory_v3_managed_index.validate_head_advance();
DROP FUNCTION
    trace_backed_memory_v3_managed_index.reject_immutable_change();
DROP SCHEMA trace_backed_memory_v3_managed_index RESTRICT;

COMMIT;
