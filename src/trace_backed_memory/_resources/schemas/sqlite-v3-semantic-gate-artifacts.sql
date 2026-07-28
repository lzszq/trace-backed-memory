PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS
trace_backed_memory_v3_semantic_gate_artifacts_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'tbm.semantic-gate-artifact.v3')
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_schema_requires_attempts
BEFORE INSERT ON trace_backed_memory_v3_semantic_gate_artifacts_schema
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM trace_backed_memory_v3_semantic_gate_schema
    WHERE singleton = 1
      AND schema_version = 1
      AND contract_version = 'tbm.semantic-gate-attempt.v3'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'SQLite semantic Gate artifacts require semantic Gate v3'
    );
END;

INSERT OR IGNORE INTO
trace_backed_memory_v3_semantic_gate_artifacts_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.semantic-gate-artifact.v3');

CREATE TABLE IF NOT EXISTS v3_semantic_gate_artifacts (
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
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 128000),
    media_type TEXT NOT NULL
        CHECK (length(media_type) > 0 AND length(media_type) <= 512),
    classification TEXT NOT NULL
        CHECK (classification IN ('public', 'internal')),
    created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 32),
    encryption_key_id TEXT CHECK (encryption_key_id IS NULL),
    redaction_policy_id TEXT
        CHECK (
            redaction_policy_id IS NULL
            OR (
                length(redaction_policy_id) > 0
                AND length(redaction_policy_id) <= 128
            )
        ),
    content BLOB NOT NULL,
    UNIQUE (artifact_id, content_sha256),
    CHECK (length(content) = size_bytes)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_semantic_gate_artifact_bindings (
    attempt_id TEXT NOT NULL
        REFERENCES v3_semantic_gate_attempts(attempt_id),
    artifact_role TEXT NOT NULL
        CHECK (artifact_role IN ('prompt', 'response')),
    artifact_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
        ),
    PRIMARY KEY (attempt_id, artifact_role),
    FOREIGN KEY (artifact_id, content_sha256)
        REFERENCES v3_semantic_gate_artifacts (
            artifact_id,
            content_sha256
        )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_semantic_gate_artifact_bindings_artifact
ON v3_semantic_gate_artifact_bindings(artifact_id);

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifact_bindings_match_attempt
BEFORE INSERT ON v3_semantic_gate_artifact_bindings
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM v3_semantic_gate_attempts AS attempt
    JOIN v3_semantic_gate_artifacts AS artifact
      ON artifact.artifact_id = NEW.artifact_id
     AND artifact.content_sha256 = NEW.content_sha256
    WHERE attempt.attempt_id = NEW.attempt_id
      AND (
          (
              NEW.artifact_role = 'prompt'
              AND artifact.size_bytes <= 128000
              AND artifact.media_type = 'text/plain; charset=utf-8'
              AND NEW.content_sha256 = json_extract(
                  attempt.descriptor,
                  '$.prompt_artifact_sha256'
              )
          )
          OR (
              NEW.artifact_role = 'response'
              AND attempt.status = 'succeeded'
              AND artifact.size_bytes <= 65536
              AND NEW.content_sha256 = json_extract(
                  attempt.descriptor,
                  '$.response_artifact_sha256'
              )
          )
      )
      AND json_valid(NEW.descriptor)
      AND (
          SELECT count(*) FROM json_each(NEW.descriptor)
      ) = 5
      AND (
          SELECT count(*)
          FROM json_each(NEW.descriptor, '$.artifact')
      ) = 8
      AND json_extract(
          NEW.descriptor,
          '$.contract_version'
      ) = 'tbm.semantic-gate-artifact.v3'
      AND json_extract(
          NEW.descriptor,
          '$.artifact_kind'
      ) = 'semantic_gate'
      AND json_extract(
          NEW.descriptor,
          '$.artifact_role'
      ) = NEW.artifact_role
      AND json_extract(
          NEW.descriptor,
          '$.attempt_id'
      ) = NEW.attempt_id
      AND json_extract(
          NEW.descriptor,
          '$.artifact.artifact_id'
      ) = artifact.artifact_id
      AND json_extract(
          NEW.descriptor,
          '$.artifact.content_sha256'
      ) = artifact.content_sha256
      AND json_extract(
          NEW.descriptor,
          '$.artifact.size_bytes'
      ) = artifact.size_bytes
      AND json_extract(
          NEW.descriptor,
          '$.artifact.media_type'
      ) = artifact.media_type
      AND json_extract(
          NEW.descriptor,
          '$.artifact.classification'
      ) = artifact.classification
      AND json_extract(
          NEW.descriptor,
          '$.artifact.created_at'
      ) = artifact.created_at
      AND json_extract(
          NEW.descriptor,
          '$.artifact.encryption_key_id'
      ) IS artifact.encryption_key_id
      AND json_extract(
          NEW.descriptor,
          '$.artifact.redaction_policy_id'
      ) IS artifact.redaction_policy_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate artifact binding does not match attempt'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_verify_content
BEFORE INSERT ON v3_semantic_gate_artifacts
FOR EACH ROW
WHEN NEW.content_sha256 <> tbm_sha256(NEW.content)
  OR NEW.artifact_id <> (
      'artifact_sha256_' || substr(NEW.content_sha256, 8)
  )
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate artifact bytes do not match identity'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_immutable_insert_conflict
BEFORE INSERT ON v3_semantic_gate_artifacts
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM v3_semantic_gate_artifacts
    WHERE artifact_id = NEW.artifact_id
       OR content_sha256 = NEW.content_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_immutable_update
BEFORE UPDATE ON v3_semantic_gate_artifacts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_immutable_delete
BEFORE DELETE ON v3_semantic_gate_artifacts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 semantic Gate artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifact_bindings_immutable_insert_conflict
BEFORE INSERT ON v3_semantic_gate_artifact_bindings
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM v3_semantic_gate_artifact_bindings
    WHERE attempt_id = NEW.attempt_id
      AND artifact_role = NEW.artifact_role
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate artifact bindings are immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifact_bindings_immutable_update
BEFORE UPDATE ON v3_semantic_gate_artifact_bindings
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate artifact bindings are immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifact_bindings_immutable_delete
BEFORE DELETE ON v3_semantic_gate_artifact_bindings
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'v3 semantic Gate artifact bindings are immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_schema_immutable_insert_conflict
BEFORE INSERT ON trace_backed_memory_v3_semantic_gate_artifacts_schema
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM trace_backed_memory_v3_semantic_gate_artifacts_schema
    WHERE singleton = NEW.singleton
)
BEGIN
    SELECT RAISE(
        ABORT,
        'SQLite semantic Gate artifact schema metadata is immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_schema_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_semantic_gate_artifacts_schema
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'SQLite semantic Gate artifact schema metadata is immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS
v3_semantic_gate_artifacts_schema_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_semantic_gate_artifacts_schema
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'SQLite semantic Gate artifact schema metadata is immutable'
    );
END;

COMMIT;
