PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

BEGIN IMMEDIATE;

CREATE TABLE trace_backed_memory_v3_outcome_attribution_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL CHECK (
        contract_version = 'tbm.outcome-attribution.v3'
    )
);

INSERT INTO trace_backed_memory_v3_outcome_attribution_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.outcome-attribution.v3');

CREATE TABLE v3_outcome_attributions (
    attribution_id TEXT PRIMARY KEY CHECK (
        length(attribution_id) = 91
        AND substr(attribution_id, 1, 27)
            = 'outcome_attribution_sha256_'
        AND substr(attribution_id, 28) NOT GLOB '*[^0-9a-f]*'
    ),
    run_outcome_id TEXT NOT NULL CHECK (
        length(run_outcome_id) = 83
        AND substr(run_outcome_id, 1, 19) = 'run_outcome_sha256_'
        AND substr(run_outcome_id, 20) NOT GLOB '*[^0-9a-f]*'
    ),
    usage_decision_id TEXT NOT NULL CHECK (
        length(usage_decision_id) BETWEEN 1 AND 128
    ),
    memory_revision_ids_json TEXT NOT NULL CHECK (
        length(CAST(memory_revision_ids_json AS BLOB)) BETWEEN 1 AND 8192
        AND json_valid(memory_revision_ids_json)
        AND json_type(memory_revision_ids_json) = 'array'
        AND json_array_length(memory_revision_ids_json) BETWEEN 1 AND 50
    ),
    claim_strength TEXT NOT NULL CHECK (
        claim_strength IN ('association', 'causal')
    ),
    effect TEXT NOT NULL CHECK (
        effect IN ('helped', 'harmed', 'neutral', 'unknown')
    ),
    method TEXT NOT NULL CHECK (
        method IN (
            'runtime_observation',
            'controlled_experiment',
            'manual_review',
            'external_evaluation'
        )
    ),
    evaluator_id TEXT NOT NULL CHECK (
        length(evaluator_id) BETWEEN 1 AND 128
    ),
    evaluator_version TEXT NOT NULL CHECK (
        length(evaluator_version) BETWEEN 1 AND 128
    ),
    verifier_id TEXT CHECK (
        verifier_id IS NULL OR length(verifier_id) BETWEEN 1 AND 128
    ),
    evidence_artifact_sha256s_json TEXT NOT NULL CHECK (
        length(CAST(evidence_artifact_sha256s_json AS BLOB))
            BETWEEN 1 AND 8192
        AND json_valid(evidence_artifact_sha256s_json)
        AND json_type(evidence_artifact_sha256s_json) = 'array'
        AND json_array_length(evidence_artifact_sha256s_json)
            BETWEEN 1 AND 64
    ),
    confidence_json TEXT NOT NULL CHECK (
        length(confidence_json) BETWEEN 1 AND 128
        AND json_valid(confidence_json)
        AND json_type(confidence_json) = 'real'
        AND CAST(confidence_json AS REAL) BETWEEN 0.0 AND 1.0
    ),
    reason TEXT NOT NULL CHECK (
        length(reason) BETWEEN 1 AND 1024
    ),
    recorded_at TEXT NOT NULL CHECK (
        length(recorded_at) BETWEEN 20 AND 32
    ),
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) BETWEEN 1 AND 1048576
        AND json_valid(descriptor)
    ),
    FOREIGN KEY (run_outcome_id)
        REFERENCES v3_run_outcomes (run_outcome_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
) WITHOUT ROWID;

CREATE INDEX v3_outcome_attributions_by_outcome
ON v3_outcome_attributions (
    run_outcome_id,
    recorded_at,
    attribution_id
);

CREATE TRIGGER v3_outcome_attribution_schema_immutable_insert
BEFORE INSERT ON trace_backed_memory_v3_outcome_attribution_schema
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM trace_backed_memory_v3_outcome_attribution_schema
)
BEGIN
    SELECT RAISE(ABORT, 'OutcomeAttribution schema metadata is immutable');
END;

CREATE TRIGGER v3_outcome_attribution_schema_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_outcome_attribution_schema
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OutcomeAttribution schema metadata is immutable');
END;

CREATE TRIGGER v3_outcome_attribution_schema_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_outcome_attribution_schema
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OutcomeAttribution schema metadata is immutable');
END;

CREATE TRIGGER v3_outcome_attributions_immutable_insert
BEFORE INSERT ON v3_outcome_attributions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM v3_outcome_attributions
    WHERE attribution_id = NEW.attribution_id
)
BEGIN
    SELECT RAISE(ABORT, 'OutcomeAttribution is immutable');
END;

CREATE TRIGGER v3_outcome_attributions_validate_insert
BEFORE INSERT ON v3_outcome_attributions
FOR EACH ROW
WHEN
    tbm_validate_outcome_attribution(NEW.descriptor) <> 1
    OR tbm_validate_outcome_attribution_row(
        NEW.attribution_id,
        NEW.run_outcome_id,
        NEW.usage_decision_id,
        NEW.memory_revision_ids_json,
        NEW.claim_strength,
        NEW.effect,
        NEW.method,
        NEW.evaluator_id,
        NEW.evaluator_version,
        NEW.verifier_id,
        NEW.evidence_artifact_sha256s_json,
        NEW.confidence_json,
        NEW.reason,
        NEW.recorded_at,
        NEW.descriptor
    ) <> 1
    OR EXISTS (
        SELECT 1
        FROM json_each(NEW.memory_revision_ids_json) AS revision
        WHERE revision.type <> 'text'
           OR length(revision.value) <> 87
           OR substr(revision.value, 1, 23)
              <> 'memory_revision_sha256_'
           OR substr(revision.value, 24) GLOB '*[^0-9a-f]*'
    )
    OR (
        SELECT COUNT(*)
        FROM json_each(NEW.memory_revision_ids_json)
    ) <> (
        SELECT COUNT(DISTINCT revision.value)
        FROM json_each(NEW.memory_revision_ids_json) AS revision
    )
    OR EXISTS (
        SELECT 1
        FROM json_each(NEW.memory_revision_ids_json) AS current
        JOIN json_each(NEW.memory_revision_ids_json) AS previous
          ON CAST(previous.key AS INTEGER)
             = CAST(current.key AS INTEGER) - 1
        WHERE current.value <= previous.value
    )
    OR EXISTS (
        SELECT 1
        FROM json_each(NEW.evidence_artifact_sha256s_json) AS artifact
        WHERE artifact.type <> 'text'
           OR length(artifact.value) <> 71
           OR substr(artifact.value, 1, 7) <> 'sha256:'
           OR substr(artifact.value, 8) GLOB '*[^0-9a-f]*'
    )
    OR (
        SELECT COUNT(*)
        FROM json_each(NEW.evidence_artifact_sha256s_json)
    ) <> (
        SELECT COUNT(DISTINCT artifact.value)
        FROM json_each(NEW.evidence_artifact_sha256s_json) AS artifact
    )
    OR EXISTS (
        SELECT 1
        FROM json_each(NEW.evidence_artifact_sha256s_json) AS current
        JOIN json_each(NEW.evidence_artifact_sha256s_json) AS previous
          ON CAST(previous.key AS INTEGER)
             = CAST(current.key AS INTEGER) - 1
        WHERE current.value <= previous.value
    )
    OR (
        NEW.claim_strength = 'association'
        AND (
            NEW.method <> 'runtime_observation'
            OR NEW.verifier_id IS NOT NULL
        )
    )
    OR (
        NEW.claim_strength = 'causal'
        AND (
            NEW.method NOT IN (
                'controlled_experiment',
                'manual_review',
                'external_evaluation'
            )
            OR NEW.verifier_id IS NULL
            OR NEW.verifier_id = NEW.evaluator_id
            OR NEW.effect = 'unknown'
        )
    )
    OR NEW.descriptor <> (
        '{"attribution_id":' || json_quote(NEW.attribution_id)
        || ',"claim_strength":' || json_quote(NEW.claim_strength)
        || ',"confidence":' || NEW.confidence_json
        || ',"contract_version":"tbm.outcome-attribution.v3"'
        || ',"effect":' || json_quote(NEW.effect)
        || ',"evaluator_id":' || json_quote(NEW.evaluator_id)
        || ',"evaluator_version":' || json_quote(NEW.evaluator_version)
        || ',"evidence_artifact_sha256s":'
        || NEW.evidence_artifact_sha256s_json
        || ',"memory_revision_ids":' || NEW.memory_revision_ids_json
        || ',"method":' || json_quote(NEW.method)
        || ',"reason":' || json_quote(NEW.reason)
        || ',"recorded_at":' || json_quote(NEW.recorded_at)
        || ',"run_outcome_id":' || json_quote(NEW.run_outcome_id)
        || ',"usage_decision_id":' || json_quote(NEW.usage_decision_id)
        || ',"verifier_id":' || json_quote(NEW.verifier_id)
        || '}'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM v3_run_outcomes AS outcome
        JOIN gate_session_heads AS head
          ON head.session_id = outcome.session_id
        JOIN gate_session_revisions AS revision
          ON revision.session_id = head.session_id
         AND revision.version = head.current_version
        WHERE outcome.run_outcome_id = NEW.run_outcome_id
          AND tbm_validate_run_outcome_row(
              outcome.run_outcome_id,
              outcome.session_id,
              outcome.trace_id,
              outcome.run_id,
              outcome.usage_decision_id,
              outcome.result,
              outcome.evaluator_id,
              outcome.evaluator_version,
              outcome.output_sha256,
              outcome.tool_outputs_sha256,
              outcome.evidence_artifact_sha256s_json,
              outcome.latency_ms,
              outcome.cost_usd_json,
              outcome.error_code,
              outcome.measured_at,
              outcome.descriptor
          ) = 1
          AND outcome.usage_decision_id = NEW.usage_decision_id
          AND revision.status = 'completed'
          AND json_extract(revision.payload, '$.run_outcome_id')
              = outcome.run_outcome_id
          AND json_extract(revision.payload, '$.usage_decision_id')
              = outcome.usage_decision_id
          AND tbm_outcome_attribution_time_not_before(
              NEW.recorded_at,
              outcome.measured_at
          ) = 1
          AND NOT EXISTS (
              SELECT 1
              FROM json_each(NEW.memory_revision_ids_json) AS claimed
              WHERE NOT EXISTS (
                  SELECT 1
                  FROM json_each(
                      revision.payload,
                      '$.final_memory_revision_ids'
                  ) AS finalized
                  WHERE finalized.value = claimed.value
              )
          )
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid OutcomeAttribution record');
END;

CREATE TRIGGER v3_outcome_attributions_immutable_update
BEFORE UPDATE ON v3_outcome_attributions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OutcomeAttribution is immutable');
END;

CREATE TRIGGER v3_outcome_attributions_immutable_delete
BEFORE DELETE ON v3_outcome_attributions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OutcomeAttribution is immutable');
END;

COMMIT;
