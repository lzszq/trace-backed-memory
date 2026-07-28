PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_gate_evidence_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_gate_evidence_schema (
    singleton,
    schema_version
) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS v3_retrieval_snapshots (
    snapshot_id TEXT PRIMARY KEY
        CHECK (
            length(snapshot_id) = 90
            AND substr(snapshot_id, 1, 26) = 'retrieval_snapshot_sha256_'
            AND substr(snapshot_id, 27) NOT GLOB '*[^0-9a-f]*'
        ),
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    authorization_event_id TEXT NOT NULL
        CHECK (
            length(authorization_event_id) = 77
            AND substr(authorization_event_id, 1, 13) = 'authz_sha256_'
            AND substr(authorization_event_id, 14) NOT GLOB '*[^0-9a-f]*'
        ),
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
        )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_retrieval_snapshots_session
ON v3_retrieval_snapshots(session_id, authorization_event_id);

CREATE TABLE IF NOT EXISTS v3_system_gate_evaluations (
    evaluation_id TEXT PRIMARY KEY
        CHECK (
            length(evaluation_id) = 83
            AND substr(evaluation_id, 1, 19) = 'system_gate_sha256_'
            AND substr(evaluation_id, 20) NOT GLOB '*[^0-9a-f]*'
        ),
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    retrieval_snapshot_id TEXT NOT NULL UNIQUE
        REFERENCES v3_retrieval_snapshots(snapshot_id),
    authorization_event_id TEXT NOT NULL
        CHECK (
            length(authorization_event_id) = 77
            AND substr(authorization_event_id, 1, 13) = 'authz_sha256_'
            AND substr(authorization_event_id, 14) NOT GLOB '*[^0-9a-f]*'
        ),
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
        )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_system_gate_evaluations_session
ON v3_system_gate_evaluations(session_id, authorization_event_id);

CREATE TRIGGER IF NOT EXISTS v3_system_gate_evaluations_parent_match
BEFORE INSERT ON v3_system_gate_evaluations
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM v3_retrieval_snapshots AS snapshot
    WHERE snapshot.snapshot_id = NEW.retrieval_snapshot_id
      AND snapshot.session_id = NEW.session_id
      AND snapshot.authorization_event_id = NEW.authorization_event_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 System Gate evaluation parent scope does not match'
    );
END;

CREATE TRIGGER IF NOT EXISTS v3_retrieval_snapshots_immutable_update
BEFORE UPDATE ON v3_retrieval_snapshots
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 retrieval snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_retrieval_snapshots_immutable_delete
BEFORE DELETE ON v3_retrieval_snapshots
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 retrieval snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_system_gate_evaluations_immutable_update
BEFORE UPDATE ON v3_system_gate_evaluations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 System Gate evaluations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_system_gate_evaluations_immutable_delete
BEFORE DELETE ON v3_system_gate_evaluations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 System Gate evaluations are immutable');
END;

COMMIT;
