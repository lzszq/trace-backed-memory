PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_replay_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_replay_schema (
    singleton,
    schema_version
) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS v3_replay_artifacts (
    artifact_id TEXT PRIMARY KEY
        CHECK (
            length(artifact_id) = 80
            AND substr(artifact_id, 1, 16) = 'artifact_sha256_'
            AND substr(artifact_id, 17) NOT GLOB '*[^0-9a-f]*'
        ),
    content_sha256 TEXT NOT NULL UNIQUE
        CHECK (
            length(content_sha256) = 71
            AND substr(content_sha256, 1, 7) = 'sha256:'
            AND substr(content_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    size_bytes INTEGER NOT NULL
        CHECK (size_bytes >= 0 AND size_bytes <= 67108864),
    media_type TEXT NOT NULL
        CHECK (length(media_type) > 0 AND length(media_type) <= 512),
    classification TEXT NOT NULL
        CHECK (
            classification IN (
                'public',
                'internal'
            )
        ),
    created_at TEXT NOT NULL
        CHECK (length(created_at) > 0 AND length(created_at) <= 64),
    encryption_key_id TEXT
        CHECK (
            encryption_key_id IS NULL
            OR (
                length(encryption_key_id) > 0
                AND length(encryption_key_id) <= 128
            )
        ),
    redaction_policy_id TEXT
        CHECK (
            redaction_policy_id IS NULL
            OR (
                length(redaction_policy_id) > 0
                AND length(redaction_policy_id) <= 128
            )
        ),
    content BLOB NOT NULL
        CHECK (
            length(content) = size_bytes
            AND length(content) <= 67108864
        ),
    CHECK (encryption_key_id IS NULL)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_replay_injections (
    artifact_id TEXT PRIMARY KEY
        REFERENCES v3_replay_artifacts(artifact_id),
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    decision_id TEXT NOT NULL
        CHECK (length(decision_id) > 0 AND length(decision_id) <= 128),
    usage_decision_id TEXT NOT NULL
        CHECK (
            length(usage_decision_id) > 0
            AND length(usage_decision_id) <= 128
        ),
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
        )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_replay_injections_decision
ON v3_replay_injections(session_id, decision_id, usage_decision_id);

CREATE TABLE IF NOT EXISTS v3_replay_manifests (
    manifest_sha256 TEXT PRIMARY KEY
        CHECK (
            length(manifest_sha256) = 71
            AND substr(manifest_sha256, 1, 7) = 'sha256:'
            AND substr(manifest_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    decision_id TEXT NOT NULL
        CHECK (length(decision_id) > 0 AND length(decision_id) <= 128),
    usage_decision_id TEXT NOT NULL
        CHECK (
            length(usage_decision_id) > 0
            AND length(usage_decision_id) <= 128
        ),
    injection_artifact_id TEXT
        REFERENCES v3_replay_injections(artifact_id),
    completeness TEXT NOT NULL
        CHECK (completeness IN ('complete', 'legacy_partial')),
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
        )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_replay_manifests_decision
ON v3_replay_manifests(session_id, decision_id, usage_decision_id);

CREATE TRIGGER IF NOT EXISTS v3_replay_artifacts_immutable_update
BEFORE UPDATE ON v3_replay_artifacts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 replay artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_replay_artifacts_immutable_delete
BEFORE DELETE ON v3_replay_artifacts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 replay artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_replay_injections_immutable_update
BEFORE UPDATE ON v3_replay_injections
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 replay injections are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_replay_injections_immutable_delete
BEFORE DELETE ON v3_replay_injections
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 replay injections are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_replay_manifests_immutable_update
BEFORE UPDATE ON v3_replay_manifests
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 replay manifests are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_replay_manifests_immutable_delete
BEFORE DELETE ON v3_replay_manifests
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 replay manifests are immutable');
END;

COMMIT;
