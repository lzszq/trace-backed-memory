PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_completion_outbox_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'tbm.completion-outbox.v3')
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_completion_outbox_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.completion-outbox.v3');

CREATE TABLE IF NOT EXISTS v3_completion_outbox_events (
    event_id TEXT PRIMARY KEY CHECK (
        length(event_id) = 95
        AND substr(event_id, 1, 31)
            = 'completion_outbox_event_sha256_'
        AND substr(event_id, 32) NOT GLOB '*[^0-9a-f]*'
    ),
    event_type TEXT NOT NULL CHECK (event_type = 'execution_completed'),
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    repository_id TEXT NOT NULL
        CHECK (length(repository_id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
    trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
    run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
    usage_decision_id TEXT NOT NULL
        CHECK (length(usage_decision_id) BETWEEN 1 AND 128),
    run_outcome_id TEXT NOT NULL UNIQUE CHECK (
        length(run_outcome_id) = 83
        AND substr(run_outcome_id, 1, 19) = 'run_outcome_sha256_'
        AND substr(run_outcome_id, 20) NOT GLOB '*[^0-9a-f]*'
    ),
    outcome_descriptor_sha256 TEXT NOT NULL CHECK (
        length(outcome_descriptor_sha256) = 71
        AND substr(outcome_descriptor_sha256, 1, 7) = 'sha256:'
        AND substr(outcome_descriptor_sha256, 8)
            NOT GLOB '*[^0-9a-f]*'
    ),
    occurred_at TEXT NOT NULL
        CHECK (length(occurred_at) BETWEEN 20 AND 32),
    occurred_at_us INTEGER NOT NULL,
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) BETWEEN 1 AND 1048576
        AND json_valid(descriptor)
    ),
    FOREIGN KEY (run_outcome_id)
        REFERENCES v3_run_outcomes (run_outcome_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    FOREIGN KEY (session_id)
        REFERENCES gate_session_heads (session_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_completion_outbox_delivery_revisions (
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    delivery_revision_id TEXT NOT NULL UNIQUE CHECK (
        length(delivery_revision_id) = 98
        AND substr(delivery_revision_id, 1, 34)
            = 'completion_outbox_delivery_sha256_'
        AND substr(delivery_revision_id, 35) NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'leased',
            'retry_wait',
            'delivered',
            'dead_letter'
        )
    ),
    attempt_count INTEGER NOT NULL
        CHECK (attempt_count BETWEEN 0 AND 1000),
    updated_at TEXT NOT NULL
        CHECK (length(updated_at) BETWEEN 20 AND 32),
    updated_at_us INTEGER NOT NULL,
    available_at TEXT
        CHECK (
            available_at IS NULL
            OR length(available_at) BETWEEN 20 AND 32
        ),
    available_at_us INTEGER,
    worker_id TEXT
        CHECK (
            worker_id IS NULL
            OR length(worker_id) BETWEEN 1 AND 128
        ),
    lease_expires_at TEXT
        CHECK (
            lease_expires_at IS NULL
            OR length(lease_expires_at) BETWEEN 20 AND 32
        ),
    lease_expires_at_us INTEGER,
    delivered_at TEXT
        CHECK (
            delivered_at IS NULL
            OR length(delivered_at) BETWEEN 20 AND 32
        ),
    delivered_at_us INTEGER,
    last_error_code TEXT
        CHECK (
            last_error_code IS NULL
            OR length(last_error_code) BETWEEN 1 AND 256
        ),
    response_sha256 TEXT CHECK (
        response_sha256 IS NULL
        OR (
            length(response_sha256) = 71
            AND substr(response_sha256, 1, 7) = 'sha256:'
            AND substr(response_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) BETWEEN 1 AND 1048576
        AND json_valid(descriptor)
    ),
    PRIMARY KEY (event_id, version),
    FOREIGN KEY (event_id)
        REFERENCES v3_completion_outbox_events (event_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_completion_outbox_delivery_heads (
    event_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    FOREIGN KEY (event_id, current_version)
        REFERENCES v3_completion_outbox_delivery_revisions (
            event_id,
            version
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_completion_outbox_due
ON v3_completion_outbox_delivery_revisions (
    status,
    available_at_us,
    lease_expires_at_us,
    event_id,
    version
)
WHERE status IN ('pending', 'retry_wait', 'leased');

CREATE TRIGGER IF NOT EXISTS v3_completion_outbox_events_validate_insert
BEFORE INSERT ON v3_completion_outbox_events
FOR EACH ROW
WHEN
    tbm_v3_completion_outbox_mutation_allowed() <> 1
    OR tbm_v3_completion_outbox_event_is_canonical(
        NEW.event_id,
        NEW.event_type,
        NEW.tenant_id,
        NEW.repository_id,
        NEW.session_id,
        NEW.trace_id,
        NEW.run_id,
        NEW.usage_decision_id,
        NEW.run_outcome_id,
        NEW.outcome_descriptor_sha256,
        NEW.occurred_at,
        NEW.occurred_at_us,
        NEW.descriptor
    ) <> 1
    OR NOT EXISTS (
        SELECT 1
        FROM v3_run_outcomes AS outcome
        JOIN gate_session_heads AS head
          ON head.session_id = outcome.session_id
        JOIN gate_session_revisions AS revision
          ON revision.session_id = head.session_id
         AND revision.version = head.current_version
        WHERE outcome.run_outcome_id = NEW.run_outcome_id
          AND outcome.session_id = NEW.session_id
          AND outcome.trace_id = NEW.trace_id
          AND outcome.run_id = NEW.run_id
          AND outcome.usage_decision_id = NEW.usage_decision_id
          AND outcome.measured_at = NEW.occurred_at
          AND head.tenant_id = NEW.tenant_id
          AND head.repository_id = NEW.repository_id
          AND revision.status = 'completed'
          AND json_extract(revision.payload, '$.run_outcome_id')
              = NEW.run_outcome_id
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid completion outbox event');
END;

CREATE TRIGGER IF NOT EXISTS v3_completion_outbox_events_immutable_update
BEFORE UPDATE ON v3_completion_outbox_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'completion outbox event is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_completion_outbox_events_immutable_delete
BEFORE DELETE ON v3_completion_outbox_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'completion outbox event is immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_completion_outbox_delivery_revisions_validate_insert
BEFORE INSERT ON v3_completion_outbox_delivery_revisions
FOR EACH ROW
WHEN
    tbm_v3_completion_outbox_mutation_allowed() <> 1
    OR tbm_v3_completion_outbox_delivery_is_canonical(
        NEW.event_id,
        NEW.version,
        NEW.delivery_revision_id,
        NEW.status,
        NEW.attempt_count,
        NEW.updated_at,
        NEW.updated_at_us,
        NEW.available_at,
        NEW.available_at_us,
        NEW.worker_id,
        NEW.lease_expires_at,
        NEW.lease_expires_at_us,
        NEW.delivered_at,
        NEW.delivered_at_us,
        NEW.last_error_code,
        NEW.response_sha256,
        NEW.descriptor
    ) <> 1
    OR (
        NEW.version = 1
        AND (
            NEW.status <> 'pending'
            OR EXISTS (
                SELECT 1
                FROM v3_completion_outbox_delivery_revisions AS existing
                WHERE existing.event_id = NEW.event_id
            )
        )
    )
    OR (
        NEW.version > 1
        AND (
            NOT EXISTS (
                SELECT 1
                FROM v3_completion_outbox_delivery_heads AS head
                WHERE head.event_id = NEW.event_id
                  AND head.current_version = NEW.version - 1
            )
            OR tbm_v3_completion_outbox_transition_is_valid(
                (
                    SELECT previous.descriptor
                    FROM v3_completion_outbox_delivery_revisions AS previous
                    WHERE previous.event_id = NEW.event_id
                      AND previous.version = NEW.version - 1
                ),
                NEW.descriptor
            ) <> 1
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid completion outbox delivery revision');
END;

CREATE TRIGGER IF NOT EXISTS
v3_completion_outbox_delivery_revisions_immutable_update
BEFORE UPDATE ON v3_completion_outbox_delivery_revisions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'completion outbox delivery revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS
v3_completion_outbox_delivery_revisions_immutable_delete
BEFORE DELETE ON v3_completion_outbox_delivery_revisions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'completion outbox delivery revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_completion_outbox_delivery_heads_validate_insert
BEFORE INSERT ON v3_completion_outbox_delivery_heads
FOR EACH ROW
WHEN
    tbm_v3_completion_outbox_mutation_allowed() <> 1
    OR NEW.current_version <> 1
    OR NOT EXISTS (
        SELECT 1
        FROM v3_completion_outbox_delivery_revisions AS revision
        WHERE revision.event_id = NEW.event_id
          AND revision.version = 1
          AND revision.status = 'pending'
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid completion outbox delivery head');
END;

CREATE TRIGGER IF NOT EXISTS v3_completion_outbox_delivery_heads_advance
BEFORE UPDATE ON v3_completion_outbox_delivery_heads
FOR EACH ROW
WHEN
    tbm_v3_completion_outbox_mutation_allowed() <> 1
    OR NEW.event_id <> OLD.event_id
    OR NEW.current_version <> OLD.current_version + 1
    OR NOT EXISTS (
        SELECT 1
        FROM v3_completion_outbox_delivery_revisions AS revision
        WHERE revision.event_id = NEW.event_id
          AND revision.version = NEW.current_version
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid completion outbox head advance');
END;

CREATE TRIGGER IF NOT EXISTS v3_completion_outbox_delivery_heads_no_delete
BEFORE DELETE ON v3_completion_outbox_delivery_heads
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'completion outbox delivery head is immutable');
END;

COMMIT;
