PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_memory_revision_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);
INSERT OR IGNORE INTO trace_backed_memory_v3_memory_revision_schema
    (singleton, schema_version) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS v3_fix_evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    source_trace_id TEXT NOT NULL,
    source_commit_sha TEXT NOT NULL,
    fix_commit_sha TEXT NOT NULL,
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) <= 1048576 AND json_valid(descriptor)
    )
);

CREATE TABLE IF NOT EXISTS v3_regression_evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    source_trace_id TEXT NOT NULL,
    source_commit_sha TEXT NOT NULL,
    fix_commit_sha TEXT NOT NULL,
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) <= 1048576 AND json_valid(descriptor)
    )
);

CREATE TABLE IF NOT EXISTS v3_memory_revision_proposals (
    revision_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    previous_revision_id TEXT,
    fix_evidence_id TEXT,
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) <= 1048576 AND json_valid(descriptor)
    ),
    UNIQUE (memory_id, revision_number),
    FOREIGN KEY (previous_revision_id)
        REFERENCES v3_memory_revision_proposals(revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (fix_evidence_id)
        REFERENCES v3_fix_evidence(evidence_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS v3_memory_revision_regression_evidence (
    revision_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (revision_id, evidence_id),
    UNIQUE (revision_id, ordinal),
    FOREIGN KEY (revision_id)
        REFERENCES v3_memory_revision_proposals(revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (evidence_id)
        REFERENCES v3_regression_evidence(evidence_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS v3_memory_revision_parent_continuity
BEFORE INSERT ON v3_memory_revision_proposals
BEGIN
    SELECT CASE
        WHEN NEW.revision_number = 1 AND NEW.previous_revision_id IS NOT NULL
        THEN RAISE(ABORT, 'first revision must not have a parent')
        WHEN NEW.revision_number > 1 AND NOT EXISTS (
            SELECT 1 FROM v3_memory_revision_proposals AS parent
            WHERE parent.revision_id = NEW.previous_revision_id
              AND parent.memory_id = NEW.memory_id
              AND parent.revision_number = NEW.revision_number - 1
        )
        THEN RAISE(ABORT, 'revision parent continuity mismatch')
    END;
END;

CREATE TRIGGER IF NOT EXISTS v3_fix_evidence_immutable_update
BEFORE UPDATE ON v3_fix_evidence BEGIN
    SELECT RAISE(ABORT, 'fix evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_fix_evidence_immutable_delete
BEFORE DELETE ON v3_fix_evidence BEGIN
    SELECT RAISE(ABORT, 'fix evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_regression_evidence_immutable_update
BEFORE UPDATE ON v3_regression_evidence BEGIN
    SELECT RAISE(ABORT, 'regression evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_regression_evidence_immutable_delete
BEFORE DELETE ON v3_regression_evidence BEGIN
    SELECT RAISE(ABORT, 'regression evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_proposals_immutable_update
BEFORE UPDATE ON v3_memory_revision_proposals BEGIN
    SELECT RAISE(ABORT, 'memory revision proposal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_proposals_immutable_delete
BEFORE DELETE ON v3_memory_revision_proposals BEGIN
    SELECT RAISE(ABORT, 'memory revision proposal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_links_immutable_update
BEFORE UPDATE ON v3_memory_revision_regression_evidence BEGIN
    SELECT RAISE(ABORT, 'memory revision evidence links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_links_immutable_delete
BEFORE DELETE ON v3_memory_revision_regression_evidence BEGIN
    SELECT RAISE(ABORT, 'memory revision evidence links are immutable');
END;
