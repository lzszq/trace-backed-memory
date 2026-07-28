BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    gate_schema_version integer;
    gate_contract_version text;
    outcome_schema_version integer;
    outcome_contract_version text;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution v3 requires active schema version 2';
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
            'PostgreSQL OutcomeAttribution v3 requires GateSession schema version 1';
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
            'PostgreSQL OutcomeAttribution v3 requires RunOutcome schema version 1';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_outcome_attribution;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_outcome_attribution FROM PUBLIC;

CREATE TABLE
trace_backed_memory_v3_outcome_attribution.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.outcome-attribution.v3')
);

INSERT INTO
trace_backed_memory_v3_outcome_attribution.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (TRUE, 1, 'tbm.outcome-attribution.v3');

CREATE TABLE
trace_backed_memory_v3_outcome_attribution.outcome_attributions (
    attribution_id text COLLATE "C" PRIMARY KEY CHECK (
        attribution_id
            ~ '^outcome_attribution_sha256_[0-9a-f]{64}$'
    ),
    run_outcome_id text COLLATE "C" NOT NULL CHECK (
        run_outcome_id ~ '^run_outcome_sha256_[0-9a-f]{64}$'
    ),
    usage_decision_id text COLLATE "C" NOT NULL CHECK (
        char_length(usage_decision_id) BETWEEN 1 AND 128
    ),
    memory_revision_ids_json text COLLATE "C" NOT NULL CHECK (
        octet_length(memory_revision_ids_json) BETWEEN 1 AND 8192
    ),
    claim_strength text COLLATE "C" NOT NULL CHECK (
        claim_strength IN ('association', 'causal')
    ),
    effect text COLLATE "C" NOT NULL CHECK (
        effect IN ('helped', 'harmed', 'neutral', 'unknown')
    ),
    method text COLLATE "C" NOT NULL CHECK (
        method IN (
            'runtime_observation',
            'controlled_experiment',
            'manual_review',
            'external_evaluation'
        )
    ),
    evaluator_id text COLLATE "C" NOT NULL CHECK (
        char_length(evaluator_id) BETWEEN 1 AND 128
    ),
    evaluator_version text COLLATE "C" NOT NULL CHECK (
        char_length(evaluator_version) BETWEEN 1 AND 128
    ),
    verifier_id text COLLATE "C" CHECK (
        verifier_id IS NULL
        OR char_length(verifier_id) BETWEEN 1 AND 128
    ),
    evidence_artifact_sha256s_json text COLLATE "C" NOT NULL CHECK (
        octet_length(evidence_artifact_sha256s_json) BETWEEN 1 AND 8192
    ),
    confidence_json text COLLATE "C" NOT NULL CHECK (
        octet_length(confidence_json) BETWEEN 1 AND 128
    ),
    reason text COLLATE "C" NOT NULL CHECK (
        char_length(reason) BETWEEN 1 AND 1024
    ),
    recorded_at timestamp(6) with time zone NOT NULL,
    descriptor text COLLATE "C" NOT NULL CHECK (
        octet_length(descriptor) BETWEEN 1 AND 1048576
    ),
    CONSTRAINT outcome_attributions_run_outcome_fkey
        FOREIGN KEY (run_outcome_id)
        REFERENCES trace_backed_memory_v3_outcome.run_outcomes (
            run_outcome_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE INDEX outcome_attributions_by_outcome
ON trace_backed_memory_v3_outcome_attribution.outcome_attributions (
    run_outcome_id,
    recorded_at,
    attribution_id
);

CREATE FUNCTION
trace_backed_memory_v3_outcome_attribution.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'PostgreSQL OutcomeAttribution records are immutable';
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_outcome_attribution.validate_attribution_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    revisions jsonb;
    evidence jsonb;
    confidence jsonb;
    parsed_descriptor jsonb;
    canonical_revisions text;
    canonical_evidence text;
    canonical_confidence text;
    canonical_recorded_at text;
    canonical_payload text;
    canonical_descriptor text;
    expected_attribution_id text;
    fractional_microseconds bigint;
    linked_outcome record;
    linked_evidence jsonb;
    linked_cost jsonb;
    linked_descriptor jsonb;
    linked_canonical_evidence text;
    linked_canonical_cost text;
    linked_canonical_measured_at text;
    linked_canonical_payload text;
    linked_canonical_descriptor text;
    linked_expected_id text;
    linked_fractional_microseconds bigint;
    linked_revision_payload jsonb;
    strip_characters text :=
        U&'\0009\000A\000B\000C\000D' ||
        U&'\001C\001D\001E\001F \0085\00A0\1680' ||
        U&'\2000\2001\2002\2003\2004\2005\2006' ||
        U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000';
BEGIN
    BEGIN
        revisions := NEW.memory_revision_ids_json::jsonb;
        evidence := NEW.evidence_artifact_sha256s_json::jsonb;
        confidence := NEW.confidence_json::jsonb;
        parsed_descriptor := NEW.descriptor::jsonb;
    EXCEPTION
        WHEN others THEN
            RAISE EXCEPTION
                'invalid PostgreSQL OutcomeAttribution record';
    END;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(
            ARRAY[
                NEW.usage_decision_id,
                NEW.evaluator_id,
                NEW.evaluator_version,
                NEW.verifier_id
            ]
        ) AS identifier(value)
        WHERE identifier.value IS NOT NULL
          AND (
                identifier.value IS DISTINCT FROM
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
          )
    ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution identifier';
    END IF;

    IF NEW.reason IS DISTINCT FROM
            pg_catalog.btrim(NEW.reason, strip_characters)
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.regexp_split_to_table(
                NEW.reason,
                ''
            ) AS character(value)
            WHERE pg_catalog.ascii(character.value) < 32
              AND pg_catalog.ascii(character.value) NOT IN (9, 10)
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution reason';
    END IF;

    IF pg_catalog.jsonb_typeof(revisions) <> 'array'
       OR pg_catalog.jsonb_array_length(revisions) NOT BETWEEN 1 AND 50
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(revisions)
                AS revision(value)
            WHERE pg_catalog.jsonb_typeof(revision.value) <> 'string'
               OR trim(both '"' from revision.value::text)
                  !~ '^memory_revision_sha256_[0-9a-f]{64}$'
       )
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_array_elements_text(revisions)
                AS revision(value)
       ) <> (
            SELECT pg_catalog.count(DISTINCT revision.value)
            FROM pg_catalog.jsonb_array_elements_text(revisions)
                AS revision(value)
       )
       OR (
            SELECT pg_catalog.array_agg(
                revision.value ORDER BY revision.ordinality
            )
            FROM pg_catalog.jsonb_array_elements_text(revisions)
                WITH ORDINALITY AS revision(value, ordinality)
       ) <> (
            SELECT pg_catalog.array_agg(
                revision.value ORDER BY revision.value COLLATE "C"
            )
            FROM pg_catalog.jsonb_array_elements_text(revisions)
                AS revision(value)
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution revisions';
    END IF;

    SELECT
        '[' ||
        pg_catalog.string_agg(
            pg_catalog.to_json(revision.value)::text,
            ','
            ORDER BY revision.ordinality
        ) ||
        ']'
    INTO canonical_revisions
    FROM pg_catalog.jsonb_array_elements_text(revisions)
        WITH ORDINALITY AS revision(value, ordinality);

    IF NEW.memory_revision_ids_json
       IS DISTINCT FROM canonical_revisions THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution revisions are not canonical';
    END IF;

    IF pg_catalog.jsonb_typeof(evidence) <> 'array'
       OR pg_catalog.jsonb_array_length(evidence) NOT BETWEEN 1 AND 64
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(evidence)
                AS artifact(value)
            WHERE pg_catalog.jsonb_typeof(artifact.value) <> 'string'
               OR trim(both '"' from artifact.value::text)
                  !~ '^sha256:[0-9a-f]{64}$'
       )
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_array_elements_text(evidence)
                AS artifact(value)
       ) <> (
            SELECT pg_catalog.count(DISTINCT artifact.value)
            FROM pg_catalog.jsonb_array_elements_text(evidence)
                AS artifact(value)
       )
       OR (
            SELECT pg_catalog.array_agg(
                artifact.value ORDER BY artifact.ordinality
            )
            FROM pg_catalog.jsonb_array_elements_text(evidence)
                WITH ORDINALITY AS artifact(value, ordinality)
       ) <> (
            SELECT pg_catalog.array_agg(
                artifact.value ORDER BY artifact.value COLLATE "C"
            )
            FROM pg_catalog.jsonb_array_elements_text(evidence)
                AS artifact(value)
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution evidence';
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
            'PostgreSQL OutcomeAttribution evidence is not canonical';
    END IF;

    IF pg_catalog.jsonb_typeof(confidence) <> 'number'
       OR (confidence #>> '{}')::double precision < 0
       OR (confidence #>> '{}')::double precision > 1
       OR (confidence #>> '{}')::double precision
            >= 'Infinity'::double precision THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution confidence';
    END IF;
    canonical_confidence :=
        pg_catalog.to_json(
            (confidence #>> '{}')::double precision
        )::text;
    IF canonical_confidence !~ '[.e]' THEN
        canonical_confidence := canonical_confidence || '.0';
    END IF;
    IF NEW.confidence_json IS DISTINCT FROM canonical_confidence THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution confidence is not canonical';
    END IF;

    IF NEW.claim_strength = 'association'
       AND (
            NEW.method <> 'runtime_observation'
            OR NEW.verifier_id IS NOT NULL
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution association';
    END IF;
    IF NEW.claim_strength = 'causal'
       AND (
            NEW.method NOT IN (
                'controlled_experiment',
                'manual_review',
                'external_evaluation'
            )
            OR NEW.verifier_id IS NULL
            OR NEW.verifier_id = NEW.evaluator_id
            OR NEW.effect = 'unknown'
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution causal claim';
    END IF;

    IF pg_catalog.date_part(
        'year',
        NEW.recorded_at AT TIME ZONE 'UTC'
    ) NOT BETWEEN 1 AND 9999 THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution timestamp';
    END IF;
    fractional_microseconds :=
        pg_catalog.mod(
            pg_catalog.date_part(
                'microseconds',
                NEW.recorded_at
            )::bigint,
            1000000
        );
    canonical_recorded_at :=
        pg_catalog.to_char(
            NEW.recorded_at AT TIME ZONE 'UTC',
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
        '{"claim_strength":' ||
        pg_catalog.to_json(NEW.claim_strength)::text ||
        ',"confidence":' || canonical_confidence ||
        ',"contract_version":"tbm.outcome-attribution.v3"' ||
        ',"effect":' || pg_catalog.to_json(NEW.effect)::text ||
        ',"evaluator_id":' ||
        pg_catalog.to_json(NEW.evaluator_id)::text ||
        ',"evaluator_version":' ||
        pg_catalog.to_json(NEW.evaluator_version)::text ||
        ',"evidence_artifact_sha256s":' || canonical_evidence ||
        ',"memory_revision_ids":' || canonical_revisions ||
        ',"method":' || pg_catalog.to_json(NEW.method)::text ||
        ',"reason":' || pg_catalog.to_json(NEW.reason)::text ||
        ',"recorded_at":' ||
        pg_catalog.to_json(canonical_recorded_at)::text ||
        ',"run_outcome_id":' ||
        pg_catalog.to_json(NEW.run_outcome_id)::text ||
        ',"usage_decision_id":' ||
        pg_catalog.to_json(NEW.usage_decision_id)::text ||
        ',"verifier_id":' ||
        COALESCE(pg_catalog.to_json(NEW.verifier_id)::text, 'null') ||
        '}';
    expected_attribution_id :=
        'outcome_attribution_sha256_' ||
        pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(canonical_payload, 'UTF8')
            ),
            'hex'
        );
    canonical_descriptor :=
        '{"attribution_id":' ||
        pg_catalog.to_json(NEW.attribution_id)::text ||
        ',"claim_strength":' ||
        pg_catalog.to_json(NEW.claim_strength)::text ||
        ',"confidence":' || canonical_confidence ||
        ',"contract_version":"tbm.outcome-attribution.v3"' ||
        ',"effect":' || pg_catalog.to_json(NEW.effect)::text ||
        ',"evaluator_id":' ||
        pg_catalog.to_json(NEW.evaluator_id)::text ||
        ',"evaluator_version":' ||
        pg_catalog.to_json(NEW.evaluator_version)::text ||
        ',"evidence_artifact_sha256s":' || canonical_evidence ||
        ',"memory_revision_ids":' || canonical_revisions ||
        ',"method":' || pg_catalog.to_json(NEW.method)::text ||
        ',"reason":' || pg_catalog.to_json(NEW.reason)::text ||
        ',"recorded_at":' ||
        pg_catalog.to_json(canonical_recorded_at)::text ||
        ',"run_outcome_id":' ||
        pg_catalog.to_json(NEW.run_outcome_id)::text ||
        ',"usage_decision_id":' ||
        pg_catalog.to_json(NEW.usage_decision_id)::text ||
        ',"verifier_id":' ||
        COALESCE(pg_catalog.to_json(NEW.verifier_id)::text, 'null') ||
        '}';

    IF NEW.attribution_id IS DISTINCT FROM expected_attribution_id
       OR NEW.descriptor IS DISTINCT FROM canonical_descriptor
       OR pg_catalog.jsonb_typeof(parsed_descriptor)
            IS DISTINCT FROM 'object'
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(parsed_descriptor)
       ) <> 15 THEN
        RAISE EXCEPTION
            'invalid PostgreSQL OutcomeAttribution descriptor';
    END IF;

    SELECT outcome.*
    INTO linked_outcome
    FROM trace_backed_memory_v3_outcome.run_outcomes AS outcome
    WHERE outcome.run_outcome_id = NEW.run_outcome_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution RunOutcome was not found';
    END IF;

    BEGIN
        linked_evidence :=
            linked_outcome.evidence_artifact_sha256s_json::jsonb;
        linked_cost := linked_outcome.cost_usd_json::jsonb;
        linked_descriptor := linked_outcome.descriptor::jsonb;
    EXCEPTION
        WHEN others THEN
            RAISE EXCEPTION
                'invalid linked PostgreSQL RunOutcome record';
    END;

    IF linked_outcome.run_outcome_id
            !~ '^run_outcome_sha256_[0-9a-f]{64}$'
       OR linked_outcome.result NOT IN ('pass', 'fail', 'error')
       OR char_length(linked_outcome.session_id) NOT BETWEEN 1 AND 128
       OR char_length(linked_outcome.trace_id) NOT BETWEEN 1 AND 128
       OR char_length(linked_outcome.run_id) NOT BETWEEN 1 AND 128
       OR char_length(linked_outcome.usage_decision_id)
            NOT BETWEEN 1 AND 128
       OR char_length(linked_outcome.evaluator_id) NOT BETWEEN 1 AND 128
       OR char_length(linked_outcome.evaluator_version)
            NOT BETWEEN 1 AND 128
       OR (
            linked_outcome.output_sha256 IS NULL
            AND linked_outcome.tool_outputs_sha256 IS NULL
       )
       OR (
            linked_outcome.output_sha256 IS NOT NULL
            AND linked_outcome.output_sha256
                !~ '^sha256:[0-9a-f]{64}$'
       )
       OR (
            linked_outcome.tool_outputs_sha256 IS NOT NULL
            AND linked_outcome.tool_outputs_sha256
                !~ '^sha256:[0-9a-f]{64}$'
       )
       OR (
            linked_outcome.result = 'error'
            AND linked_outcome.error_code IS NULL
       )
       OR (
            linked_outcome.result <> 'error'
            AND linked_outcome.error_code IS NOT NULL
       )
       OR (
            linked_outcome.latency_ms IS NOT NULL
            AND linked_outcome.latency_ms NOT BETWEEN 0 AND 2147483647
       )
       OR octet_length(linked_outcome.descriptor)
            NOT BETWEEN 1 AND 1048576
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.unnest(
                ARRAY[
                    linked_outcome.session_id,
                    linked_outcome.trace_id,
                    linked_outcome.run_id,
                    linked_outcome.usage_decision_id,
                    linked_outcome.evaluator_id,
                    linked_outcome.evaluator_version
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
       )
       OR (
            linked_outcome.error_code IS NOT NULL
            AND (
                char_length(linked_outcome.error_code)
                    NOT BETWEEN 1 AND 1024
                OR linked_outcome.error_code IS DISTINCT FROM
                    pg_catalog.btrim(
                        linked_outcome.error_code,
                        strip_characters
                    )
                OR EXISTS (
                    SELECT 1
                    FROM pg_catalog.regexp_split_to_table(
                        linked_outcome.error_code,
                        ''
                    ) AS character(value)
                    WHERE pg_catalog.ascii(character.value) < 32
                      AND pg_catalog.ascii(character.value) NOT IN (9, 10)
                )
            )
       ) THEN
        RAISE EXCEPTION
            'invalid linked PostgreSQL RunOutcome shape';
    END IF;

    IF pg_catalog.jsonb_typeof(linked_evidence) <> 'array'
       OR pg_catalog.jsonb_array_length(linked_evidence)
            NOT BETWEEN 1 AND 64
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(linked_evidence)
                AS artifact(value)
            WHERE pg_catalog.jsonb_typeof(artifact.value) <> 'string'
               OR trim(both '"' from artifact.value::text)
                  !~ '^sha256:[0-9a-f]{64}$'
       )
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_array_elements_text(linked_evidence)
                AS artifact(value)
       ) <> (
            SELECT pg_catalog.count(DISTINCT artifact.value)
            FROM pg_catalog.jsonb_array_elements_text(linked_evidence)
                AS artifact(value)
       )
       OR (
            SELECT pg_catalog.array_agg(
                artifact.value ORDER BY artifact.ordinality
            )
            FROM pg_catalog.jsonb_array_elements_text(linked_evidence)
                WITH ORDINALITY AS artifact(value, ordinality)
       ) <> (
            SELECT pg_catalog.array_agg(
                artifact.value ORDER BY artifact.value COLLATE "C"
            )
            FROM pg_catalog.jsonb_array_elements_text(linked_evidence)
                AS artifact(value)
       ) THEN
        RAISE EXCEPTION
            'invalid linked PostgreSQL RunOutcome evidence';
    END IF;

    SELECT
        '[' ||
        pg_catalog.string_agg(
            pg_catalog.to_json(artifact.value)::text,
            ','
            ORDER BY artifact.ordinality
        ) ||
        ']'
    INTO linked_canonical_evidence
    FROM pg_catalog.jsonb_array_elements_text(linked_evidence)
        WITH ORDINALITY AS artifact(value, ordinality);

    IF linked_outcome.evidence_artifact_sha256s_json
            IS DISTINCT FROM linked_canonical_evidence
       OR NOT (
            linked_outcome.cost_usd_json = 'null'
            OR (
                pg_catalog.jsonb_typeof(linked_cost) = 'number'
                AND (linked_cost #>> '{}')::double precision >= 0
                AND (linked_cost #>> '{}')::double precision
                    < 'Infinity'::double precision
            )
       ) THEN
        RAISE EXCEPTION
            'invalid linked PostgreSQL RunOutcome numeric evidence';
    END IF;

    IF linked_outcome.cost_usd_json = 'null' THEN
        linked_canonical_cost := 'null';
    ELSE
        linked_canonical_cost :=
            pg_catalog.to_json(
                (linked_cost #>> '{}')::double precision
            )::text;
        IF linked_canonical_cost !~ '[.e]' THEN
            linked_canonical_cost := linked_canonical_cost || '.0';
        END IF;
    END IF;

    IF linked_outcome.cost_usd_json
       IS DISTINCT FROM linked_canonical_cost THEN
        RAISE EXCEPTION
            'linked PostgreSQL RunOutcome cost is not canonical';
    END IF;

    IF pg_catalog.date_part(
        'year',
        linked_outcome.measured_at AT TIME ZONE 'UTC'
    ) NOT BETWEEN 1 AND 9999 THEN
        RAISE EXCEPTION
            'invalid linked PostgreSQL RunOutcome timestamp';
    END IF;
    linked_fractional_microseconds :=
        pg_catalog.mod(
            pg_catalog.date_part(
                'microseconds',
                linked_outcome.measured_at
            )::bigint,
            1000000
        );
    linked_canonical_measured_at :=
        pg_catalog.to_char(
            linked_outcome.measured_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS'
        ) ||
        CASE
            WHEN linked_fractional_microseconds = 0 THEN ''
            ELSE '.' ||
                pg_catalog.lpad(
                    linked_fractional_microseconds::text,
                    6,
                    '0'
                )
        END ||
        'Z';

    linked_canonical_payload :=
        '{"contract_version":"tbm.run-outcome.v3"' ||
        ',"cost_usd":' || linked_canonical_cost ||
        ',"error_code":' ||
        COALESCE(
            pg_catalog.to_json(linked_outcome.error_code)::text,
            'null'
        ) ||
        ',"evaluator_id":' ||
        pg_catalog.to_json(linked_outcome.evaluator_id)::text ||
        ',"evaluator_version":' ||
        pg_catalog.to_json(linked_outcome.evaluator_version)::text ||
        ',"evidence_artifact_sha256s":' ||
        linked_canonical_evidence ||
        ',"latency_ms":' ||
        COALESCE(linked_outcome.latency_ms::text, 'null') ||
        ',"measured_at":' ||
        pg_catalog.to_json(linked_canonical_measured_at)::text ||
        ',"output_sha256":' ||
        COALESCE(
            pg_catalog.to_json(linked_outcome.output_sha256)::text,
            'null'
        ) ||
        ',"result":' ||
        pg_catalog.to_json(linked_outcome.result)::text ||
        ',"run_id":' ||
        pg_catalog.to_json(linked_outcome.run_id)::text ||
        ',"session_id":' ||
        pg_catalog.to_json(linked_outcome.session_id)::text ||
        ',"tool_outputs_sha256":' ||
        COALESCE(
            pg_catalog.to_json(
                linked_outcome.tool_outputs_sha256
            )::text,
            'null'
        ) ||
        ',"trace_id":' ||
        pg_catalog.to_json(linked_outcome.trace_id)::text ||
        ',"usage_decision_id":' ||
        pg_catalog.to_json(linked_outcome.usage_decision_id)::text ||
        '}';
    linked_expected_id :=
        'run_outcome_sha256_' ||
        pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(
                    linked_canonical_payload,
                    'UTF8'
                )
            ),
            'hex'
        );
    linked_canonical_descriptor :=
        '{"contract_version":"tbm.run-outcome.v3"' ||
        ',"cost_usd":' || linked_canonical_cost ||
        ',"error_code":' ||
        COALESCE(
            pg_catalog.to_json(linked_outcome.error_code)::text,
            'null'
        ) ||
        ',"evaluator_id":' ||
        pg_catalog.to_json(linked_outcome.evaluator_id)::text ||
        ',"evaluator_version":' ||
        pg_catalog.to_json(linked_outcome.evaluator_version)::text ||
        ',"evidence_artifact_sha256s":' ||
        linked_canonical_evidence ||
        ',"latency_ms":' ||
        COALESCE(linked_outcome.latency_ms::text, 'null') ||
        ',"measured_at":' ||
        pg_catalog.to_json(linked_canonical_measured_at)::text ||
        ',"output_sha256":' ||
        COALESCE(
            pg_catalog.to_json(linked_outcome.output_sha256)::text,
            'null'
        ) ||
        ',"result":' ||
        pg_catalog.to_json(linked_outcome.result)::text ||
        ',"run_id":' ||
        pg_catalog.to_json(linked_outcome.run_id)::text ||
        ',"run_outcome_id":' ||
        pg_catalog.to_json(linked_outcome.run_outcome_id)::text ||
        ',"session_id":' ||
        pg_catalog.to_json(linked_outcome.session_id)::text ||
        ',"tool_outputs_sha256":' ||
        COALESCE(
            pg_catalog.to_json(
                linked_outcome.tool_outputs_sha256
            )::text,
            'null'
        ) ||
        ',"trace_id":' ||
        pg_catalog.to_json(linked_outcome.trace_id)::text ||
        ',"usage_decision_id":' ||
        pg_catalog.to_json(linked_outcome.usage_decision_id)::text ||
        '}';

    IF linked_outcome.run_outcome_id
            IS DISTINCT FROM linked_expected_id
       OR linked_outcome.descriptor
            IS DISTINCT FROM linked_canonical_descriptor
       OR pg_catalog.jsonb_typeof(linked_descriptor)
            IS DISTINCT FROM 'object'
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(linked_descriptor)
       ) <> 16 THEN
        RAISE EXCEPTION
            'invalid linked PostgreSQL RunOutcome descriptor';
    END IF;

    SELECT revision.payload::jsonb
    INTO linked_revision_payload
    FROM
        trace_backed_memory_v3_gate_session.gate_session_heads AS head
    JOIN
        trace_backed_memory_v3_gate_session.gate_session_revisions
            AS revision
      ON revision.session_id = head.session_id
     AND revision.version = head.current_version
    WHERE head.session_id = linked_outcome.session_id
      AND head.trace_id = linked_outcome.trace_id
      AND head.run_id = linked_outcome.run_id
      AND revision.status = 'completed'
      AND revision.updated_at = linked_outcome.measured_at
    FOR SHARE OF head, revision;

    IF NOT FOUND
       OR linked_revision_payload ->> 'usage_decision_id'
            IS DISTINCT FROM linked_outcome.usage_decision_id
       OR linked_revision_payload ->> 'run_outcome_id'
            IS DISTINCT FROM linked_outcome.run_outcome_id
       OR NEW.usage_decision_id
            IS DISTINCT FROM linked_outcome.usage_decision_id
       OR NEW.recorded_at < linked_outcome.measured_at
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements_text(revisions)
                AS claimed(value)
            WHERE NOT EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements_text(
                    linked_revision_payload
                        -> 'final_memory_revision_ids'
                ) AS finalized(value)
                WHERE finalized.value = claimed.value
            )
       ) THEN
        RAISE EXCEPTION
            'PostgreSQL OutcomeAttribution linkage is invalid';
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER attribution_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_outcome_attribution.schema_metadata
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome_attribution.reject_immutable_change();

CREATE TRIGGER attribution_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_outcome_attribution.schema_metadata
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome_attribution.reject_immutable_change();

CREATE TRIGGER outcome_attributions_validate_insert
BEFORE INSERT
ON trace_backed_memory_v3_outcome_attribution.outcome_attributions
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome_attribution.validate_attribution_insert();

CREATE TRIGGER outcome_attributions_immutable_change
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_outcome_attribution.outcome_attributions
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome_attribution.reject_immutable_change();

CREATE TRIGGER outcome_attributions_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_outcome_attribution.outcome_attributions
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_outcome_attribution.reject_immutable_change();

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_outcome_attribution.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_outcome_attribution.validate_attribution_insert()
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_outcome_attribution.schema_metadata
FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_outcome_attribution.outcome_attributions
FROM PUBLIC;

COMMIT;
