PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_event_ledger_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL CHECK (
        contract_version = 'tbm.event-ledger-port.v1'
    )
);

INSERT OR IGNORE INTO trace_backed_memory_v3_event_ledger_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.event-ledger-port.v1');

CREATE TABLE IF NOT EXISTS v3_event_ledger_global_head (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    current_global_position INTEGER NOT NULL CHECK (
        current_global_position >= 0
    ),
    current_event_id TEXT,
    current_event_sha256 TEXT,
    CHECK (
        (current_global_position = 0 AND current_event_id IS NULL
            AND current_event_sha256 IS NULL)
        OR
        (current_global_position > 0 AND current_event_id IS NOT NULL
            AND current_event_sha256 IS NOT NULL)
    )
);

INSERT OR IGNORE INTO v3_event_ledger_global_head (
    singleton,
    current_global_position,
    current_event_id,
    current_event_sha256
) VALUES (1, 0, NULL, NULL);

CREATE TABLE IF NOT EXISTS v3_event_ledger_stream_heads (
    partition_sha256 TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    current_stream_version INTEGER NOT NULL CHECK (
        current_stream_version >= 0
    ),
    current_event_id TEXT,
    current_event_sha256 TEXT,
    PRIMARY KEY (partition_sha256, stream_id),
    CHECK (
        (current_stream_version = 0 AND current_event_id IS NULL
            AND current_event_sha256 IS NULL)
        OR
        (current_stream_version > 0 AND current_event_id IS NOT NULL
            AND current_event_sha256 IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS v3_event_ledger_events (
    event_id TEXT PRIMARY KEY,
    event_sha256 TEXT NOT NULL UNIQUE,
    partition_sha256 TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_version INTEGER NOT NULL CHECK (stream_version >= 1),
    global_position INTEGER NOT NULL UNIQUE CHECK (global_position >= 1),
    previous_stream_event_sha256 TEXT,
    classification TEXT NOT NULL CHECK (
        classification IN ('public', 'internal', 'confidential', 'restricted')
    ),
    artifact_ref_count INTEGER NOT NULL CHECK (
        artifact_ref_count BETWEEN 0 AND 128
    ),
    canonical_event TEXT NOT NULL COLLATE BINARY CHECK (
        length(CAST(canonical_event AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(canonical_event)
    ),
    UNIQUE (partition_sha256, stream_id, stream_version),
    FOREIGN KEY (partition_sha256, stream_id)
        REFERENCES v3_event_ledger_stream_heads (
            partition_sha256,
            stream_id
        )
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS v3_event_ledger_events_partition_global
ON v3_event_ledger_events (partition_sha256, global_position);

CREATE INDEX IF NOT EXISTS v3_event_ledger_events_partition_stream
ON v3_event_ledger_events (
    partition_sha256,
    stream_id,
    stream_version
);

CREATE TABLE IF NOT EXISTS v3_event_ledger_artifacts (
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    artifact_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (
        size_bytes BETWEEN 0 AND 67108864
    ),
    classification TEXT NOT NULL CHECK (
        classification IN ('public', 'internal', 'confidential', 'restricted')
    ),
    retention_policy_id TEXT NOT NULL,
    encryption_key_id TEXT,
    availability TEXT NOT NULL CHECK (
        availability IN ('available', 'erased', 'external', 'unavailable')
    ),
    descriptor TEXT NOT NULL COLLATE BINARY CHECK (
        length(CAST(descriptor AS BLOB)) BETWEEN 2 AND 16384
        AND json_valid(descriptor)
    ),
    PRIMARY KEY (event_id, ordinal),
    UNIQUE (event_id, artifact_id),
    FOREIGN KEY (event_id)
        REFERENCES v3_event_ledger_events (event_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS v3_event_ledger_idempotency (
    partition_sha256 TEXT NOT NULL,
    idempotency_key_sha256 TEXT NOT NULL,
    command_sha256 TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    previous_stream_version INTEGER NOT NULL CHECK (
        previous_stream_version >= 0
    ),
    current_stream_version INTEGER NOT NULL CHECK (
        current_stream_version > previous_stream_version
    ),
    first_global_position INTEGER NOT NULL CHECK (
        first_global_position >= 1
    ),
    last_global_position INTEGER NOT NULL CHECK (
        last_global_position >= first_global_position
    ),
    event_sha256s_json TEXT NOT NULL COLLATE BINARY CHECK (
        length(CAST(event_sha256s_json AS BLOB)) BETWEEN 4 AND 16384
        AND json_valid(event_sha256s_json)
    ),
    receipt_sha256 TEXT NOT NULL,
    PRIMARY KEY (partition_sha256, idempotency_key_sha256)
);

CREATE TABLE IF NOT EXISTS v3_event_ledger_checkpoints (
    projection_name TEXT NOT NULL CHECK (
        length(CAST(projection_name AS BLOB)) BETWEEN 1 AND 128
    ),
    projection_version INTEGER NOT NULL CHECK (projection_version >= 1),
    partition_sha256 TEXT NOT NULL CHECK (
        length(partition_sha256) = 71
        AND substr(partition_sha256, 1, 7) = 'sha256:'
        AND substr(partition_sha256, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    global_position INTEGER NOT NULL CHECK (global_position >= 0),
    state_sha256 TEXT NOT NULL CHECK (
        length(state_sha256) = 71
        AND substr(state_sha256, 1, 7) = 'sha256:'
        AND substr(state_sha256, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    descriptor TEXT NOT NULL COLLATE BINARY CHECK (
        length(CAST(descriptor AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(descriptor)
    ),
    PRIMARY KEY (
        projection_name,
        projection_version,
        partition_sha256,
        global_position
    )
);

CREATE TABLE IF NOT EXISTS v3_event_ledger_projection_activations (
    projection_name TEXT NOT NULL CHECK (
        length(CAST(projection_name AS BLOB)) BETWEEN 1 AND 128
    ),
    partition_sha256 TEXT NOT NULL CHECK (
        length(partition_sha256) = 71
        AND substr(partition_sha256, 1, 7) = 'sha256:'
        AND substr(partition_sha256, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    head_version INTEGER NOT NULL CHECK (head_version >= 1),
    target_build_id TEXT NOT NULL CHECK (
        length(target_build_id) = 71
        AND substr(target_build_id, 1, 7) = 'sha256:'
        AND substr(target_build_id, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    previous_build_id TEXT CHECK (
        previous_build_id IS NULL OR (
            length(previous_build_id) = 71
            AND substr(previous_build_id, 1, 7) = 'sha256:'
            AND substr(previous_build_id, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    operation TEXT NOT NULL CHECK (operation IN ('activate', 'rollback')),
    activation_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(activation_sha256) = 71
        AND substr(activation_sha256, 1, 7) = 'sha256:'
        AND substr(activation_sha256, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    descriptor TEXT NOT NULL COLLATE BINARY CHECK (
        length(CAST(descriptor AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(descriptor)
    ),
    PRIMARY KEY (projection_name, partition_sha256, head_version)
);

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_schema_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_event_ledger_schema
BEGIN
    SELECT RAISE(ABORT, 'event ledger schema metadata is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_schema_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_event_ledger_schema
BEGIN
    SELECT RAISE(ABORT, 'event ledger schema metadata cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_projection_activations_validate_insert
BEFORE INSERT ON v3_event_ledger_projection_activations
WHEN (
    NEW.head_version = 1
    AND (
        NEW.previous_build_id IS NOT NULL
        OR EXISTS (
            SELECT 1 FROM v3_event_ledger_projection_activations
            WHERE projection_name = NEW.projection_name
              AND partition_sha256 = NEW.partition_sha256
        )
    )
) OR (
    NEW.head_version > 1
    AND NOT EXISTS (
        SELECT 1 FROM v3_event_ledger_projection_activations
        WHERE projection_name = NEW.projection_name
          AND partition_sha256 = NEW.partition_sha256
          AND head_version = NEW.head_version - 1
          AND target_build_id = NEW.previous_build_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'projection activation does not advance its head');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_projection_activations_immutable_update
BEFORE UPDATE ON v3_event_ledger_projection_activations
BEGIN
    SELECT RAISE(ABORT, 'projection activation rows are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_projection_activations_immutable_delete
BEFORE DELETE ON v3_event_ledger_projection_activations
BEGIN
    SELECT RAISE(ABORT, 'projection activation rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_global_head_validate_insert
BEFORE INSERT ON v3_event_ledger_global_head
WHEN NEW.singleton <> 1 OR NEW.current_global_position <> 0
    OR NEW.current_event_id IS NOT NULL
    OR NEW.current_event_sha256 IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'event ledger global head must start empty');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_global_head_advance
BEFORE UPDATE ON v3_event_ledger_global_head
BEGIN
    SELECT CASE WHEN
        NEW.singleton <> OLD.singleton
        OR NEW.current_global_position <> OLD.current_global_position + 1
        OR NOT EXISTS (
            SELECT 1
            FROM v3_event_ledger_events AS event
            WHERE event.global_position = NEW.current_global_position
              AND event.event_id = NEW.current_event_id
              AND event.event_sha256 = NEW.current_event_sha256
        )
    THEN RAISE(ABORT, 'event ledger global head advance is invalid') END;
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_global_head_no_delete
BEFORE DELETE ON v3_event_ledger_global_head
BEGIN
    SELECT RAISE(ABORT, 'event ledger global head cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_stream_heads_validate_insert
BEFORE INSERT ON v3_event_ledger_stream_heads
WHEN NEW.current_stream_version <> 0 OR NEW.current_event_id IS NOT NULL
    OR NEW.current_event_sha256 IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'event ledger stream head must start empty');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_stream_heads_identity_immutable
BEFORE UPDATE ON v3_event_ledger_stream_heads
WHEN NEW.partition_sha256 <> OLD.partition_sha256
    OR NEW.stream_id <> OLD.stream_id
    OR NEW.organization_id <> OLD.organization_id
    OR NEW.tenant_id <> OLD.tenant_id
    OR NEW.repository_id <> OLD.repository_id
    OR NEW.environment_id <> OLD.environment_id
BEGIN
    SELECT RAISE(ABORT, 'event ledger stream identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_stream_heads_advance
BEFORE UPDATE ON v3_event_ledger_stream_heads
BEGIN
    SELECT CASE WHEN
        NEW.current_stream_version <> OLD.current_stream_version + 1
        OR NOT EXISTS (
            SELECT 1
            FROM v3_event_ledger_events AS event
            WHERE event.partition_sha256 = NEW.partition_sha256
              AND event.stream_id = NEW.stream_id
              AND event.stream_version = NEW.current_stream_version
              AND event.event_id = NEW.current_event_id
              AND event.event_sha256 = NEW.current_event_sha256
        )
    THEN RAISE(ABORT, 'event ledger stream head advance is invalid') END;
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_stream_heads_no_delete
BEFORE DELETE ON v3_event_ledger_stream_heads
BEGIN
    SELECT RAISE(ABORT, 'event ledger stream head cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_events_validate_insert
BEFORE INSERT ON v3_event_ledger_events
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM v3_event_ledger_stream_heads AS head
        WHERE head.partition_sha256 = NEW.partition_sha256
          AND head.stream_id = NEW.stream_id
          AND head.organization_id = NEW.organization_id
          AND head.tenant_id = NEW.tenant_id
          AND head.repository_id = NEW.repository_id
          AND head.environment_id = NEW.environment_id
          AND head.current_stream_version = NEW.stream_version - 1
          AND head.current_event_sha256 IS NEW.previous_stream_event_sha256
    ) THEN RAISE(ABORT, 'event does not extend the current stream head') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM v3_event_ledger_global_head AS head
        WHERE head.singleton = 1
          AND head.current_global_position = NEW.global_position - 1
    ) THEN RAISE(ABORT, 'event does not extend the global ledger head') END;
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_events_immutable_update
BEFORE UPDATE ON v3_event_ledger_events
BEGIN
    SELECT RAISE(ABORT, 'event ledger events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_events_immutable_delete
BEFORE DELETE ON v3_event_ledger_events
BEGIN
    SELECT RAISE(ABORT, 'event ledger events cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_artifacts_validate_insert
BEFORE INSERT ON v3_event_ledger_artifacts
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM v3_event_ledger_events AS event
        WHERE event.event_id = NEW.event_id
          AND NEW.ordinal < event.artifact_ref_count
    ) THEN RAISE(ABORT, 'event artifact ordinal is outside the event descriptor') END;
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_artifacts_immutable_update
BEFORE UPDATE ON v3_event_ledger_artifacts
BEGIN
    SELECT RAISE(ABORT, 'event artifact descriptors are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_artifacts_immutable_delete
BEFORE DELETE ON v3_event_ledger_artifacts
BEGIN
    SELECT RAISE(ABORT, 'event artifact descriptors cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_idempotency_immutable_update
BEFORE UPDATE ON v3_event_ledger_idempotency
BEGIN
    SELECT RAISE(ABORT, 'event ledger idempotency records are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_idempotency_immutable_delete
BEFORE DELETE ON v3_event_ledger_idempotency
BEGIN
    SELECT RAISE(ABORT, 'event ledger idempotency records cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_checkpoints_immutable_update
BEFORE UPDATE ON v3_event_ledger_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'event ledger checkpoints are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_event_ledger_checkpoints_immutable_delete
BEFORE DELETE ON v3_event_ledger_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'event ledger checkpoints cannot be deleted');
END;

COMMIT;
