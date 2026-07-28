PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_outcome_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'tbm.run-outcome.v3')
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_outcome_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.run-outcome.v3');

CREATE TABLE IF NOT EXISTS v3_run_outcomes (
    run_outcome_id TEXT PRIMARY KEY CHECK (
        length(run_outcome_id) = 83
        AND substr(run_outcome_id, 1, 19) = 'run_outcome_sha256_'
        AND substr(run_outcome_id, 20) NOT GLOB '*[^0-9a-f]*'
    ),
    session_id TEXT NOT NULL UNIQUE
        CHECK (length(session_id) BETWEEN 1 AND 128),
    trace_id TEXT NOT NULL
        CHECK (length(trace_id) BETWEEN 1 AND 128),
    run_id TEXT NOT NULL
        CHECK (length(run_id) BETWEEN 1 AND 128),
    usage_decision_id TEXT NOT NULL
        CHECK (length(usage_decision_id) BETWEEN 1 AND 128),
    result TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'error')),
    evaluator_id TEXT NOT NULL
        CHECK (length(evaluator_id) BETWEEN 1 AND 128),
    evaluator_version TEXT NOT NULL
        CHECK (length(evaluator_version) BETWEEN 1 AND 128),
    output_sha256 TEXT CHECK (
        output_sha256 IS NULL
        OR (
            length(output_sha256) = 71
            AND substr(output_sha256, 1, 7) = 'sha256:'
            AND substr(output_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    tool_outputs_sha256 TEXT CHECK (
        tool_outputs_sha256 IS NULL
        OR (
            length(tool_outputs_sha256) = 71
            AND substr(tool_outputs_sha256, 1, 7) = 'sha256:'
            AND substr(tool_outputs_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    evidence_artifact_sha256s_json TEXT NOT NULL CHECK (
        length(CAST(evidence_artifact_sha256s_json AS BLOB))
            BETWEEN 1 AND 8192
        AND json_valid(evidence_artifact_sha256s_json)
        AND json_type(evidence_artifact_sha256s_json) = 'array'
        AND json_array_length(evidence_artifact_sha256s_json)
            BETWEEN 1 AND 64
    ),
    latency_ms INTEGER CHECK (
        latency_ms IS NULL
        OR latency_ms BETWEEN 0 AND 2147483647
    ),
    cost_usd_json TEXT NOT NULL CHECK (
        cost_usd_json = 'null'
        OR (
            json_valid(cost_usd_json)
            AND json_type(cost_usd_json) IN ('integer', 'real')
            AND CAST(cost_usd_json AS REAL) >= 0
        )
    ),
    error_code TEXT CHECK (
        error_code IS NULL
        OR length(error_code) BETWEEN 1 AND 1024
    ),
    measured_at TEXT NOT NULL
        CHECK (length(measured_at) BETWEEN 20 AND 32),
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) BETWEEN 1 AND 1048576
        AND json_valid(descriptor)
    ),
    CHECK (output_sha256 IS NOT NULL OR tool_outputs_sha256 IS NOT NULL),
    CHECK (
        (result = 'error' AND error_code IS NOT NULL)
        OR (result <> 'error' AND error_code IS NULL)
    ),
    FOREIGN KEY (session_id)
        REFERENCES gate_session_heads (session_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS v3_run_outcomes_validate_insert
BEFORE INSERT ON v3_run_outcomes
FOR EACH ROW
WHEN
    EXISTS (
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
    OR NEW.descriptor <> (
        '{"contract_version":"tbm.run-outcome.v3"'
        || ',"cost_usd":' || NEW.cost_usd_json
        || ',"error_code":' || json_quote(NEW.error_code)
        || ',"evaluator_id":' || json_quote(NEW.evaluator_id)
        || ',"evaluator_version":' || json_quote(NEW.evaluator_version)
        || ',"evidence_artifact_sha256s":'
        || NEW.evidence_artifact_sha256s_json
        || ',"latency_ms":'
        || COALESCE(CAST(NEW.latency_ms AS TEXT), 'null')
        || ',"measured_at":' || json_quote(NEW.measured_at)
        || ',"output_sha256":' || json_quote(NEW.output_sha256)
        || ',"result":' || json_quote(NEW.result)
        || ',"run_id":' || json_quote(NEW.run_id)
        || ',"run_outcome_id":' || json_quote(NEW.run_outcome_id)
        || ',"session_id":' || json_quote(NEW.session_id)
        || ',"tool_outputs_sha256":'
        || json_quote(NEW.tool_outputs_sha256)
        || ',"trace_id":' || json_quote(NEW.trace_id)
        || ',"usage_decision_id":' || json_quote(NEW.usage_decision_id)
        || '}'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM gate_session_heads AS head
        JOIN gate_session_revisions AS revision
          ON revision.session_id = head.session_id
         AND revision.version = head.current_version
        WHERE head.session_id = NEW.session_id
          AND head.trace_id = NEW.trace_id
          AND head.run_id = NEW.run_id
          AND revision.status = 'completed'
          AND revision.updated_at = NEW.measured_at
          AND json_extract(revision.payload, '$.usage_decision_id')
              = NEW.usage_decision_id
          AND json_extract(revision.payload, '$.run_outcome_id')
              = NEW.run_outcome_id
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid RunOutcome record');
END;

CREATE TRIGGER IF NOT EXISTS v3_run_outcomes_immutable_update
BEFORE UPDATE ON v3_run_outcomes
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'RunOutcome is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_run_outcomes_immutable_delete
BEFORE DELETE ON v3_run_outcomes
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'RunOutcome is immutable');
END;

COMMIT;
