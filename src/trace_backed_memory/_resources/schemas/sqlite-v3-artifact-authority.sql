PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_artifact_authority_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_artifact_authority_schema (
    singleton, schema_version
) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS v3_encrypted_artifacts (
    artifact_id TEXT PRIMARY KEY
        CHECK (length(artifact_id) = 80 AND substr(artifact_id, 1, 16) = 'artifact_sha256_'),
    content_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(content_sha256) = 71 AND substr(content_sha256, 1, 7) = 'sha256:'),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 67108864),
    media_type TEXT NOT NULL CHECK (length(media_type) BETWEEN 1 AND 512),
    classification TEXT NOT NULL
        CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
    redaction_policy_id TEXT CHECK (redaction_policy_id IS NULL OR length(redaction_policy_id) BETWEEN 1 AND 128),
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    repository_id TEXT NOT NULL CHECK (length(repository_id) BETWEEN 1 AND 128),
    environment_id TEXT NOT NULL CHECK (length(environment_id) BETWEEN 1 AND 128),
    write_authorization_event_id TEXT NOT NULL CHECK (length(write_authorization_event_id) BETWEEN 1 AND 128),
    encryption_provider_id TEXT NOT NULL CHECK (length(encryption_provider_id) BETWEEN 1 AND 128),
    encryption_algorithm TEXT NOT NULL CHECK (length(encryption_algorithm) BETWEEN 1 AND 128),
    encryption_key_id TEXT NOT NULL CHECK (length(encryption_key_id) BETWEEN 1 AND 128),
    nonce BLOB NOT NULL CHECK (length(nonce) BETWEEN 1 AND 1024),
    ciphertext BLOB NOT NULL CHECK (length(ciphertext) BETWEEN 1 AND 67174400),
    ciphertext_sha256 TEXT NOT NULL
        CHECK (length(ciphertext_sha256) = 71 AND substr(ciphertext_sha256, 1, 7) = 'sha256:'),
    retain_until TEXT CHECK (retain_until IS NULL OR length(retain_until) BETWEEN 1 AND 64),
    legal_hold INTEGER NOT NULL CHECK (legal_hold IN (0, 1)),
    stored_at TEXT NOT NULL CHECK (length(stored_at) BETWEEN 1 AND 64)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_encrypted_artifacts_scope
ON v3_encrypted_artifacts(tenant_id, repository_id, environment_id);

CREATE TRIGGER IF NOT EXISTS v3_encrypted_artifacts_immutable_update
BEFORE UPDATE ON v3_encrypted_artifacts FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 encrypted artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_encrypted_artifacts_immutable_delete
BEFORE DELETE ON v3_encrypted_artifacts FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 encrypted artifacts are immutable');
END;

COMMIT;
