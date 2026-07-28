PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_semantic_gate_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'tbm.semantic-gate-attempt.v3')
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_schema_requires_evidence
BEFORE INSERT ON trace_backed_memory_v3_semantic_gate_schema
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM trace_backed_memory_v3_gate_evidence_schema
    WHERE singleton = 1 AND schema_version = 1
)
BEGIN
    SELECT RAISE(
        ABORT,
        'SQLite semantic Gate v3 requires gate evidence v3'
    );
END;

INSERT OR IGNORE INTO trace_backed_memory_v3_semantic_gate_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.semantic-gate-attempt.v3');

CREATE TABLE IF NOT EXISTS v3_semantic_gate_attempt_heads (
    system_gate_evaluation_id TEXT PRIMARY KEY
        REFERENCES v3_system_gate_evaluations(evaluation_id),
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    retrieval_snapshot_id TEXT NOT NULL
        REFERENCES v3_retrieval_snapshots(snapshot_id),
    current_sequence INTEGER NOT NULL
        CHECK (current_sequence >= 0 AND current_sequence <= 100),
    current_attempt_id TEXT,
    CHECK (
        (current_sequence = 0 AND current_attempt_id IS NULL)
        OR (current_sequence > 0 AND current_attempt_id IS NOT NULL)
    ),
    FOREIGN KEY (system_gate_evaluation_id, current_attempt_id)
        REFERENCES v3_semantic_gate_attempts (
            system_gate_evaluation_id,
            attempt_id
        )
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_semantic_gate_attempts (
    attempt_id TEXT PRIMARY KEY
        CHECK (
            length(attempt_id) = 88
            AND substr(attempt_id, 1, 24) = 'semantic_attempt_sha256_'
            AND substr(attempt_id, 25) NOT GLOB '*[^0-9a-f]*'
        ),
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    retrieval_snapshot_id TEXT NOT NULL
        REFERENCES v3_retrieval_snapshots(snapshot_id),
    system_gate_evaluation_id TEXT NOT NULL
        REFERENCES v3_system_gate_evaluations(evaluation_id),
    sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 100),
    previous_attempt_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    started_at TEXT NOT NULL
        CHECK (length(started_at) BETWEEN 20 AND 32),
    finished_at TEXT NOT NULL
        CHECK (length(finished_at) BETWEEN 20 AND 32),
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
        ),
    UNIQUE (system_gate_evaluation_id, sequence),
    UNIQUE (system_gate_evaluation_id, attempt_id),
    FOREIGN KEY (system_gate_evaluation_id, previous_attempt_id)
        REFERENCES v3_semantic_gate_attempts (
            system_gate_evaluation_id,
            attempt_id
        )
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_semantic_gate_attempts_session
ON v3_semantic_gate_attempts(session_id, system_gate_evaluation_id, sequence);

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_heads_parent_scope
BEFORE INSERT ON v3_semantic_gate_attempt_heads
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM v3_system_gate_evaluations AS evaluation
    JOIN v3_retrieval_snapshots AS snapshot
      ON snapshot.snapshot_id = evaluation.retrieval_snapshot_id
    WHERE evaluation.evaluation_id = NEW.system_gate_evaluation_id
      AND evaluation.session_id = NEW.session_id
      AND evaluation.retrieval_snapshot_id = NEW.retrieval_snapshot_id
      AND snapshot.session_id = NEW.session_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate head parent scope does not match'
    );
END;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_heads_identity_immutable
BEFORE UPDATE ON v3_semantic_gate_attempt_heads
FOR EACH ROW
WHEN NEW.system_gate_evaluation_id <> OLD.system_gate_evaluation_id
  OR NEW.session_id <> OLD.session_id
  OR NEW.retrieval_snapshot_id <> OLD.retrieval_snapshot_id
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate head identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_heads_immutable_insert_conflict
BEFORE INSERT ON v3_semantic_gate_attempt_heads
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM v3_semantic_gate_attempt_heads
    WHERE system_gate_evaluation_id = NEW.system_gate_evaluation_id
)
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate heads are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_heads_advance
BEFORE UPDATE OF current_sequence, current_attempt_id
ON v3_semantic_gate_attempt_heads
FOR EACH ROW
WHEN NEW.current_sequence <> OLD.current_sequence + 1
  OR NOT EXISTS (
      SELECT 1
      FROM v3_semantic_gate_attempts AS attempt
      WHERE attempt.attempt_id = NEW.current_attempt_id
        AND attempt.system_gate_evaluation_id =
            NEW.system_gate_evaluation_id
        AND attempt.session_id = NEW.session_id
        AND attempt.retrieval_snapshot_id = NEW.retrieval_snapshot_id
        AND attempt.sequence = NEW.current_sequence
        AND attempt.previous_attempt_id IS OLD.current_attempt_id
  )
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate head advance is invalid');
END;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_heads_immutable_delete
BEFORE DELETE ON v3_semantic_gate_attempt_heads
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate heads are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_attempts_parent_scope
BEFORE INSERT ON v3_semantic_gate_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM v3_system_gate_evaluations AS evaluation
    JOIN v3_retrieval_snapshots AS snapshot
      ON snapshot.snapshot_id = evaluation.retrieval_snapshot_id
    WHERE evaluation.evaluation_id = NEW.system_gate_evaluation_id
      AND evaluation.session_id = NEW.session_id
      AND evaluation.retrieval_snapshot_id = NEW.retrieval_snapshot_id
      AND snapshot.session_id = NEW.session_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate attempt parent scope does not match'
    );
END;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_attempts_extend_head
BEFORE INSERT ON v3_semantic_gate_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM v3_semantic_gate_attempt_heads AS head
    WHERE head.system_gate_evaluation_id =
        NEW.system_gate_evaluation_id
      AND head.session_id = NEW.session_id
      AND head.retrieval_snapshot_id = NEW.retrieval_snapshot_id
      AND head.current_sequence = NEW.sequence - 1
      AND head.current_attempt_id IS NEW.previous_attempt_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate attempt does not extend the current head'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_attempts_immutable_insert_conflict
BEFORE INSERT ON v3_semantic_gate_attempts
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM v3_semantic_gate_attempts
    WHERE attempt_id = NEW.attempt_id
       OR (
           system_gate_evaluation_id = NEW.system_gate_evaluation_id
           AND sequence = NEW.sequence
       )
)
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate attempts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_attempts_immutable_update
BEFORE UPDATE ON v3_semantic_gate_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate attempts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_semantic_gate_attempts_immutable_delete
BEFORE DELETE ON v3_semantic_gate_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate attempts are immutable');
END;

COMMIT;
