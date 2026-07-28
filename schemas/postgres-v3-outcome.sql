BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    gate_schema_version integer;
    gate_contract_version text;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome v3 requires active schema version 2';
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
            'PostgreSQL RunOutcome v3 requires GateSession schema version 1';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_outcome;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_outcome FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_outcome.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.run-outcome.v3')
);

INSERT INTO trace_backed_memory_v3_outcome.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (TRUE, 1, 'tbm.run-outcome.v3');

CREATE TABLE trace_backed_memory_v3_outcome.run_outcomes (
    run_outcome_id text COLLATE "C" PRIMARY KEY CHECK (
        run_outcome_id ~ '^run_outcome_sha256_[0-9a-f]{64}$'
    ),
    session_id text COLLATE "C" NOT NULL,
    trace_id text COLLATE "C" NOT NULL
        CHECK (char_length(trace_id) BETWEEN 1 AND 128),
    run_id text COLLATE "C" NOT NULL
        CHECK (char_length(run_id) BETWEEN 1 AND 128),
    usage_decision_id text COLLATE "C" NOT NULL
        CHECK (char_length(usage_decision_id) BETWEEN 1 AND 128),
    result text COLLATE "C" NOT NULL
        CHECK (result IN ('pass', 'fail', 'error')),
    evaluator_id text COLLATE "C" NOT NULL
        CHECK (char_length(evaluator_id) BETWEEN 1 AND 128),
    evaluator_version text COLLATE "C" NOT NULL
        CHECK (char_length(evaluator_version) BETWEEN 1 AND 128),
    output_sha256 text COLLATE "C" CHECK (
        output_sha256 IS NULL
        OR output_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    tool_outputs_sha256 text COLLATE "C" CHECK (
        tool_outputs_sha256 IS NULL
        OR tool_outputs_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    evidence_artifact_sha256s_json text COLLATE "C" NOT NULL CHECK (
        octet_length(evidence_artifact_sha256s_json) BETWEEN 1 AND 8192
    ),
    latency_ms integer CHECK (
        latency_ms IS NULL OR latency_ms BETWEEN 0 AND 2147483647
    ),
    cost_usd_json text COLLATE "C" NOT NULL CHECK (
        octet_length(cost_usd_json) BETWEEN 1 AND 128
    ),
    error_code text COLLATE "C" CHECK (
        error_code IS NULL OR char_length(error_code) BETWEEN 1 AND 1024
    ),
    measured_at timestamp(6) with time zone NOT NULL,
    descriptor text COLLATE "C" NOT NULL CHECK (
        octet_length(descriptor) BETWEEN 1 AND 1048576
    ),
    CONSTRAINT run_outcomes_session_id_key UNIQUE (session_id),
    CONSTRAINT run_outcomes_output_shape CHECK (
        output_sha256 IS NOT NULL OR tool_outputs_sha256 IS NOT NULL
    ),
    CONSTRAINT run_outcomes_error_shape CHECK (
        (result = 'error' AND error_code IS NOT NULL)
        OR (result <> 'error' AND error_code IS NULL)
    ),
    CONSTRAINT run_outcomes_session_fkey
        FOREIGN KEY (session_id)
        REFERENCES
            trace_backed_memory_v3_gate_session.gate_session_heads (
                session_id
            )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE FUNCTION
trace_backed_memory_v3_outcome.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL RunOutcome records are immutable';
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_outcome.validate_run_outcome_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    evidence jsonb;
    cost jsonb;
    parsed_descriptor jsonb;
    canonical_evidence text;
    canonical_cost text;
    canonical_measured_at text;
    canonical_payload text;
    canonical_descriptor text;
    expected_outcome_id text;
    fractional_microseconds bigint;
    strip_characters text :=
        U&'\0009\000A\000B\000C\000D' ||
        U&'\001C\001D\001E\001F \0085\00A0\1680' ||
        U&'\2000\2001\2002\2003\2004\2005\2006' ||
        U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000';
BEGIN
    BEGIN
        evidence := NEW.evidence_artifact_sha256s_json::jsonb;
        cost := NEW.cost_usd_json::jsonb;
        parsed_descriptor := NEW.descriptor::jsonb;
    EXCEPTION
        WHEN others THEN
            RAISE EXCEPTION 'invalid PostgreSQL RunOutcome record';
    END;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(
            ARRAY[
                NEW.session_id,
                NEW.trace_id,
                NEW.run_id,
                NEW.usage_decision_id,
                NEW.evaluator_id,
                NEW.evaluator_version
            ]
        ) AS identifier(value)
        WHERE identifier.value IS DISTINCT FROM
            pg_catalog.btrim(identifier.value, strip_characters)
           OR EXISTS (
                SELECT 1
                FROM pg_catalog.regexp_split_to_table(
                    identifier.value,
                    ''
                ) AS character(value)
                WHERE pg_catalog.ascii(character.value) < 32
                   OR pg_catalog.ascii(character.value) = 127
           )
    ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL RunOutcome identifier';
    END IF;

    IF NEW.error_code IS NOT NULL
       AND (
            NEW.error_code IS DISTINCT FROM
                pg_catalog.btrim(NEW.error_code, strip_characters)
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.regexp_split_to_table(
                    NEW.error_code,
                    ''
                ) AS character(value)
                WHERE pg_catalog.ascii(character.value) < 32
                  AND pg_catalog.ascii(character.value) NOT IN (9, 10)
            )
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL RunOutcome error text';
    END IF;

    IF jsonb_typeof(evidence) <> 'array'
       OR jsonb_array_length(evidence) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'invalid PostgreSQL RunOutcome evidence';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(evidence) AS artifact(value)
        WHERE jsonb_typeof(artifact.value) <> 'string'
           OR trim(both '"' from artifact.value::text)
              !~ '^sha256:[0-9a-f]{64}$'
    ) OR (
        SELECT count(*)
        FROM jsonb_array_elements_text(evidence) AS artifact(value)
    ) <> (
        SELECT count(DISTINCT artifact.value)
        FROM jsonb_array_elements_text(evidence) AS artifact(value)
    ) OR (
        SELECT array_agg(artifact.value ORDER BY artifact.ordinality)
        FROM jsonb_array_elements_text(evidence)
             WITH ORDINALITY AS artifact(value, ordinality)
    ) <> (
        SELECT array_agg(artifact.value ORDER BY artifact.value)
        FROM jsonb_array_elements_text(evidence) AS artifact(value)
    ) THEN
        RAISE EXCEPTION 'invalid PostgreSQL RunOutcome evidence';
    END IF;

    SELECT
        '[' ||
        pg_catalog.string_agg(
            pg_catalog.to_json(artifact.value)::text,
            ','
            ORDER BY artifact.ordinality
        ) ||
        ']'
    INTO canonical_evidence
    FROM pg_catalog.jsonb_array_elements_text(evidence)
         WITH ORDINALITY AS artifact(value, ordinality);

    IF NEW.evidence_artifact_sha256s_json
       IS DISTINCT FROM canonical_evidence THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome evidence is not canonical';
    END IF;

    IF NOT (
        NEW.cost_usd_json = 'null'
        OR (
            jsonb_typeof(cost) = 'number'
            AND (cost #>> '{}')::double precision >= 0
            AND (cost #>> '{}')::double precision
                < 'Infinity'::double precision
        )
    ) THEN
        RAISE EXCEPTION 'invalid PostgreSQL RunOutcome cost';
    END IF;
    IF NEW.cost_usd_json = 'null' THEN
        canonical_cost := 'null';
    ELSE
        canonical_cost :=
            pg_catalog.to_json(
                (cost #>> '{}')::double precision
            )::text;
        IF canonical_cost !~ '[.e]' THEN
            canonical_cost := canonical_cost || '.0';
        END IF;
    END IF;
    IF NEW.cost_usd_json IS DISTINCT FROM canonical_cost THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome cost is not canonical';
    END IF;

    fractional_microseconds :=
        pg_catalog.mod(
            pg_catalog.date_part(
                'microseconds',
                NEW.measured_at
            )::bigint,
            1000000
        );
    IF pg_catalog.date_part(
        'year',
        NEW.measured_at AT TIME ZONE 'UTC'
    ) NOT BETWEEN 1 AND 9999 THEN
        RAISE EXCEPTION
            'invalid PostgreSQL RunOutcome timestamp';
    END IF;
    canonical_measured_at :=
        pg_catalog.to_char(
            NEW.measured_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS'
        ) ||
        CASE
            WHEN fractional_microseconds = 0 THEN ''
            ELSE '.' ||
                pg_catalog.lpad(
                    fractional_microseconds::text,
                    6,
                    '0'
                )
        END ||
        'Z';

    canonical_payload :=
        '{"contract_version":"tbm.run-outcome.v3"' ||
        ',"cost_usd":' || canonical_cost ||
        ',"error_code":' ||
        COALESCE(pg_catalog.to_json(NEW.error_code)::text, 'null') ||
        ',"evaluator_id":' ||
        pg_catalog.to_json(NEW.evaluator_id)::text ||
        ',"evaluator_version":' ||
        pg_catalog.to_json(NEW.evaluator_version)::text ||
        ',"evidence_artifact_sha256s":' || canonical_evidence ||
        ',"latency_ms":' ||
        COALESCE(NEW.latency_ms::text, 'null') ||
        ',"measured_at":' ||
        pg_catalog.to_json(canonical_measured_at)::text ||
        ',"output_sha256":' ||
        COALESCE(pg_catalog.to_json(NEW.output_sha256)::text, 'null') ||
        ',"result":' || pg_catalog.to_json(NEW.result)::text ||
        ',"run_id":' || pg_catalog.to_json(NEW.run_id)::text ||
        ',"session_id":' || pg_catalog.to_json(NEW.session_id)::text ||
        ',"tool_outputs_sha256":' ||
        COALESCE(
            pg_catalog.to_json(NEW.tool_outputs_sha256)::text,
            'null'
        ) ||
        ',"trace_id":' || pg_catalog.to_json(NEW.trace_id)::text ||
        ',"usage_decision_id":' ||
        pg_catalog.to_json(NEW.usage_decision_id)::text ||
        '}';
    expected_outcome_id :=
        'run_outcome_sha256_' ||
        pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(canonical_payload, 'UTF8')
            ),
            'hex'
        );
    canonical_descriptor :=
        '{"contract_version":"tbm.run-outcome.v3"' ||
        ',"cost_usd":' || canonical_cost ||
        ',"error_code":' ||
        COALESCE(pg_catalog.to_json(NEW.error_code)::text, 'null') ||
        ',"evaluator_id":' ||
        pg_catalog.to_json(NEW.evaluator_id)::text ||
        ',"evaluator_version":' ||
        pg_catalog.to_json(NEW.evaluator_version)::text ||
        ',"evidence_artifact_sha256s":' || canonical_evidence ||
        ',"latency_ms":' ||
        COALESCE(NEW.latency_ms::text, 'null') ||
        ',"measured_at":' ||
        pg_catalog.to_json(canonical_measured_at)::text ||
        ',"output_sha256":' ||
        COALESCE(pg_catalog.to_json(NEW.output_sha256)::text, 'null') ||
        ',"result":' || pg_catalog.to_json(NEW.result)::text ||
        ',"run_id":' || pg_catalog.to_json(NEW.run_id)::text ||
        ',"run_outcome_id":' ||
        pg_catalog.to_json(NEW.run_outcome_id)::text ||
        ',"session_id":' || pg_catalog.to_json(NEW.session_id)::text ||
        ',"tool_outputs_sha256":' ||
        COALESCE(
            pg_catalog.to_json(NEW.tool_outputs_sha256)::text,
            'null'
        ) ||
        ',"trace_id":' || pg_catalog.to_json(NEW.trace_id)::text ||
        ',"usage_decision_id":' ||
        pg_catalog.to_json(NEW.usage_decision_id)::text ||
        '}';

    IF NEW.run_outcome_id IS DISTINCT FROM expected_outcome_id
       OR NEW.descriptor IS DISTINCT FROM canonical_descriptor
       OR pg_catalog.jsonb_typeof(parsed_descriptor) IS DISTINCT FROM 'object'
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(parsed_descriptor)
       ) <> 16 THEN
        RAISE EXCEPTION 'invalid PostgreSQL RunOutcome descriptor';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM
            trace_backed_memory_v3_gate_session.gate_session_heads AS head
        JOIN
            trace_backed_memory_v3_gate_session.gate_session_revisions
                AS revision
          ON revision.session_id = head.session_id
         AND revision.version = head.current_version
        WHERE head.session_id = NEW.session_id
          AND head.trace_id = NEW.trace_id
          AND head.run_id = NEW.run_id
          AND revision.status = 'completed'
          AND revision.updated_at = NEW.measured_at
          AND revision.payload::jsonb ->> 'usage_decision_id'
              = NEW.usage_decision_id
          AND revision.payload::jsonb ->> 'run_outcome_id'
              = NEW.run_outcome_id
    ) THEN
        RAISE EXCEPTION
            'PostgreSQL RunOutcome does not match completed GateSession';
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER outcome_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_outcome.schema_metadata
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome.reject_immutable_change();

CREATE TRIGGER outcome_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_outcome.schema_metadata
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome.reject_immutable_change();

CREATE TRIGGER run_outcomes_validate_insert
BEFORE INSERT
ON trace_backed_memory_v3_outcome.run_outcomes
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome.validate_run_outcome_insert();

CREATE TRIGGER run_outcomes_immutable_change
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_outcome.run_outcomes
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome.reject_immutable_change();

CREATE TRIGGER run_outcomes_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_outcome.run_outcomes
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome.reject_immutable_change();

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_outcome.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_outcome.validate_run_outcome_insert()
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_outcome.schema_metadata
FROM PUBLIC;

COMMIT;
