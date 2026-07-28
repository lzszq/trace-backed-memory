BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    gate_schema_version integer;
    gate_contract_version text;
    outcome_schema_version integer;
    outcome_contract_version text;
    relation_names text[];
    function_names text[];
    trigger_names text[];
    constraint_names text[];
    column_names text[];
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome v3 rollback requires active schema version 2';
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
            'PostgreSQL RunOutcome v3 rollback GateSession metadata mismatch';
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
            'PostgreSQL RunOutcome v3 rollback metadata mismatch';
    END IF;

    SELECT array_agg(class.relname ORDER BY class.relname)
    INTO relation_names
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_outcome'
      AND class.relkind IN ('r', 'i', 'p');
    IF relation_names <> ARRAY[
        'run_outcomes',
        'run_outcomes_pkey',
        'run_outcomes_session_id_key',
        'schema_metadata',
        'schema_metadata_pkey'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(procedure.proname ORDER BY procedure.proname)
    INTO function_names
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_outcome';
    IF function_names <> ARRAY[
        'reject_immutable_change',
        'validate_run_outcome_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(trigger.tgname ORDER BY trigger.tgname)
    INTO trigger_names
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class
      ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_outcome'
      AND NOT trigger.tgisinternal;
    IF trigger_names <> ARRAY[
        'outcome_metadata_immutable',
        'outcome_metadata_no_truncate',
        'run_outcomes_immutable_change',
        'run_outcomes_no_truncate',
        'run_outcomes_validate_insert'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        constraint_record.conname
        ORDER BY constraint_record.conname
    )
    INTO constraint_names
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = 'trace_backed_memory_v3_outcome'
      AND constraint_record.contype <> 'n';
    IF constraint_names <> ARRAY[
        'run_outcomes_cost_usd_json_check',
        'run_outcomes_descriptor_check',
        'run_outcomes_error_code_check',
        'run_outcomes_error_shape',
        'run_outcomes_evaluator_id_check',
        'run_outcomes_evaluator_version_check',
        'run_outcomes_evidence_artifact_sha256s_json_check',
        'run_outcomes_latency_ms_check',
        'run_outcomes_output_sha256_check',
        'run_outcomes_output_shape',
        'run_outcomes_pkey',
        'run_outcomes_result_check',
        'run_outcomes_run_id_check',
        'run_outcomes_run_outcome_id_check',
        'run_outcomes_session_fkey',
        'run_outcomes_session_id_key',
        'run_outcomes_tool_outputs_sha256_check',
        'run_outcomes_trace_id_check',
        'run_outcomes_usage_decision_id_check',
        'schema_metadata_contract_version_check',
        'schema_metadata_pkey',
        'schema_metadata_schema_version_check',
        'schema_metadata_singleton_check'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome v3 rollback catalog mismatch';
    END IF;

    SELECT array_agg(
        columns.table_name || '.' || columns.column_name
        ORDER BY columns.table_name, columns.ordinal_position
    )
    INTO column_names
    FROM information_schema.columns AS columns
    WHERE columns.table_schema = 'trace_backed_memory_v3_outcome';
    IF column_names <> ARRAY[
        'run_outcomes.run_outcome_id',
        'run_outcomes.session_id',
        'run_outcomes.trace_id',
        'run_outcomes.run_id',
        'run_outcomes.usage_decision_id',
        'run_outcomes.result',
        'run_outcomes.evaluator_id',
        'run_outcomes.evaluator_version',
        'run_outcomes.output_sha256',
        'run_outcomes.tool_outputs_sha256',
        'run_outcomes.evidence_artifact_sha256s_json',
        'run_outcomes.latency_ms',
        'run_outcomes.cost_usd_json',
        'run_outcomes.error_code',
        'run_outcomes.measured_at',
        'run_outcomes.descriptor',
        'schema_metadata.singleton',
        'schema_metadata.schema_version',
        'schema_metadata.contract_version'
    ]::text[] THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome v3 rollback catalog mismatch';
    END IF;
END
$$;

DROP TABLE trace_backed_memory_v3_outcome.run_outcomes RESTRICT;
DROP TABLE trace_backed_memory_v3_outcome.schema_metadata RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_outcome.validate_run_outcome_insert()
    RESTRICT;
DROP FUNCTION
    trace_backed_memory_v3_outcome.reject_immutable_change()
    RESTRICT;
DROP SCHEMA trace_backed_memory_v3_outcome RESTRICT;

COMMIT;
