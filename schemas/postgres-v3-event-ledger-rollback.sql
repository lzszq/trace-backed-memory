BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    ledger_version integer;
    ledger_contract text;
    relation_names text[];
    function_names text[];
    trigger_names text[];
    triggers_enabled boolean;
    policy_count integer;
    rule_count integer;
BEGIN
    SELECT schema_version INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;
    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL event ledger rollback requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO ledger_version, ledger_contract
    FROM trace_backed_memory_v3_event_ledger.schema_metadata
    WHERE singleton
    FOR UPDATE;
    IF ledger_version IS NULL
       OR ledger_version <> 1
       OR ledger_contract <> 'tbm.event-ledger-port.v1' THEN
        RAISE EXCEPTION
            'PostgreSQL event ledger rollback metadata mismatch';
    END IF;

    LOCK TABLE
        trace_backed_memory_v3_event_ledger.schema_metadata,
        trace_backed_memory_v3_event_ledger.global_head,
        trace_backed_memory_v3_event_ledger.stream_heads,
        trace_backed_memory_v3_event_ledger.events,
        trace_backed_memory_v3_event_ledger.artifacts,
        trace_backed_memory_v3_event_ledger.idempotency,
        trace_backed_memory_v3_event_ledger.checkpoints
        IN ACCESS EXCLUSIVE MODE;

    SELECT array_agg(class.relname ORDER BY class.relname)
    INTO relation_names
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_event_ledger'
      AND class.relkind IN ('r', 'i', 'p');
    IF relation_names <> ARRAY[
        'artifacts',
        'checkpoints',
        'event_ledger_artifacts_event_artifact_key',
        'event_ledger_artifacts_pkey',
        'event_ledger_checkpoints_pkey',
        'event_ledger_events_global_key',
        'event_ledger_events_partition_global',
        'event_ledger_events_partition_stream',
        'event_ledger_events_pkey',
        'event_ledger_events_sha256_key',
        'event_ledger_events_stream_version_key',
        'event_ledger_global_head_pkey',
        'event_ledger_idempotency_pkey',
        'event_ledger_idempotency_stream',
        'event_ledger_stream_heads_pkey',
        'events',
        'global_head',
        'idempotency',
        'schema_metadata',
        'schema_metadata_pkey',
        'stream_heads'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL event ledger rollback relation catalog mismatch';
    END IF;

    SELECT array_agg(procedure.proname ORDER BY procedure.proname)
    INTO function_names
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_event_ledger';
    IF function_names <> ARRAY[
        'reject_immutable_change',
        'validate_artifact_insert',
        'validate_event_insert',
        'validate_global_head_insert',
        'validate_global_head_update',
        'validate_stream_head_insert',
        'validate_stream_head_update'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL event ledger rollback function catalog mismatch';
    END IF;

    SELECT array_agg(trigger.tgname ORDER BY trigger.tgname),
           bool_and(trigger.tgenabled = 'O')
    INTO trigger_names, triggers_enabled
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_event_ledger'
      AND NOT trigger.tgisinternal;
    IF trigger_names <> ARRAY[
        'event_ledger_artifacts_immutable',
        'event_ledger_artifacts_no_truncate',
        'event_ledger_artifacts_validate_insert',
        'event_ledger_checkpoints_immutable',
        'event_ledger_checkpoints_no_truncate',
        'event_ledger_events_immutable',
        'event_ledger_events_no_truncate',
        'event_ledger_events_validate_insert',
        'event_ledger_global_head_advance',
        'event_ledger_global_head_initial',
        'event_ledger_global_head_no_delete',
        'event_ledger_global_head_no_truncate',
        'event_ledger_idempotency_immutable',
        'event_ledger_idempotency_no_truncate',
        'event_ledger_schema_immutable',
        'event_ledger_schema_no_truncate',
        'event_ledger_stream_heads_advance',
        'event_ledger_stream_heads_initial',
        'event_ledger_stream_heads_no_delete',
        'event_ledger_stream_heads_no_truncate'
    ]::text[] OR triggers_enabled IS NOT TRUE THEN
        RAISE EXCEPTION
            'PostgreSQL event ledger rollback trigger catalog mismatch';
    END IF;

    SELECT count(*) INTO policy_count
    FROM pg_catalog.pg_policy AS policy
    JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_event_ledger';
    SELECT count(*) INTO rule_count
    FROM pg_catalog.pg_rewrite AS rewrite
    JOIN pg_catalog.pg_class AS class ON class.oid = rewrite.ev_class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_event_ledger'
      AND rewrite.rulename <> '_RETURN';
    IF policy_count <> 0 OR rule_count <> 0 THEN
        RAISE EXCEPTION
            'PostgreSQL event ledger rollback policy or rule catalog mismatch';
    END IF;
END
$$;

DROP TABLE
    trace_backed_memory_v3_event_ledger.checkpoints,
    trace_backed_memory_v3_event_ledger.idempotency,
    trace_backed_memory_v3_event_ledger.artifacts,
    trace_backed_memory_v3_event_ledger.events,
    trace_backed_memory_v3_event_ledger.stream_heads,
    trace_backed_memory_v3_event_ledger.global_head,
    trace_backed_memory_v3_event_ledger.schema_metadata
RESTRICT;

DROP FUNCTION
    trace_backed_memory_v3_event_ledger.reject_immutable_change(),
    trace_backed_memory_v3_event_ledger.validate_global_head_insert(),
    trace_backed_memory_v3_event_ledger.validate_global_head_update(),
    trace_backed_memory_v3_event_ledger.validate_stream_head_insert(),
    trace_backed_memory_v3_event_ledger.validate_stream_head_update(),
    trace_backed_memory_v3_event_ledger.validate_event_insert(),
    trace_backed_memory_v3_event_ledger.validate_artifact_insert()
RESTRICT;

DROP SCHEMA trace_backed_memory_v3_event_ledger RESTRICT;

COMMIT;
