PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_audit_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'tbm.audit-event.v3')
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_audit_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.audit-event.v3');

CREATE TABLE IF NOT EXISTS v3_audit_stream_heads (
    stream_id TEXT PRIMARY KEY
        CHECK (length(stream_id) > 0 AND length(stream_id) <= 128),
    tenant_id TEXT NOT NULL
        CHECK (length(tenant_id) > 0 AND length(tenant_id) <= 128),
    repository_id TEXT NOT NULL
        CHECK (length(repository_id) > 0 AND length(repository_id) <= 128),
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    trace_id TEXT NOT NULL
        CHECK (length(trace_id) > 0 AND length(trace_id) <= 128),
    run_id TEXT NOT NULL
        CHECK (length(run_id) > 0 AND length(run_id) <= 128),
    current_sequence INTEGER NOT NULL
        CHECK (
            current_sequence >= 0
            AND current_sequence <= 2147483647
        ),
    current_event_id TEXT
        CHECK (
            current_event_id IS NULL
            OR (
                length(current_event_id) = 83
                AND substr(current_event_id, 1, 19) =
                    'audit_event_sha256_'
                AND substr(current_event_id, 20)
                    NOT GLOB '*[^0-9a-f]*'
            )
        ),
    CHECK (
        (current_sequence = 0 AND current_event_id IS NULL)
        OR (current_sequence > 0 AND current_event_id IS NOT NULL)
    )
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS v3_audit_events (
    event_id TEXT PRIMARY KEY
        CHECK (
            length(event_id) = 83
            AND substr(event_id, 1, 19) = 'audit_event_sha256_'
            AND substr(event_id, 20) NOT GLOB '*[^0-9a-f]*'
        ),
    stream_id TEXT NOT NULL
        REFERENCES v3_audit_stream_heads(stream_id),
    sequence INTEGER NOT NULL
        CHECK (sequence >= 1 AND sequence <= 2147483647),
    previous_event_id TEXT,
    tenant_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    actor_type TEXT NOT NULL
        CHECK (actor_type IN ('principal', 'service', 'worker')),
    actor_id TEXT NOT NULL
        CHECK (length(actor_id) > 0 AND length(actor_id) <= 128),
    event_type TEXT NOT NULL
        CHECK (
            event_type IN (
                'session_created',
                'session_transitioned',
                'authorization_evaluated',
                'retrieval_recorded',
                'system_gate_evaluated',
                'semantic_gate_attempted',
                'decision_finalized',
                'injection_created',
                'execution_completed',
                'outcome_attributed',
                'recovery_succeeded',
                'recovery_failed',
                'session_canceled',
                'session_expired',
                'session_abandoned'
            )
        ),
    recovery_action_id TEXT
        CHECK (
            recovery_action_id IS NULL
            OR (
                length(recovery_action_id) = 87
                AND substr(recovery_action_id, 1, 23) =
                    'recovery_action_sha256_'
                AND substr(recovery_action_id, 24)
                    NOT GLOB '*[^0-9a-f]*'
            )
        ),
    reason_code TEXT NOT NULL
        CHECK (length(reason_code) > 0 AND length(reason_code) <= 256),
    payload_sha256 TEXT NOT NULL
        CHECK (
            length(payload_sha256) = 71
            AND substr(payload_sha256, 1, 7) = 'sha256:'
            AND substr(payload_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    occurred_at TEXT NOT NULL
        CHECK (length(occurred_at) > 0 AND length(occurred_at) <= 64),
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
    ),
    UNIQUE (stream_id, sequence),
    UNIQUE (stream_id, event_id),
    UNIQUE (recovery_action_id),
    UNIQUE (recovery_action_id, event_id),
    CHECK (
        (
            event_type IN ('recovery_succeeded', 'recovery_failed')
            AND recovery_action_id IS NOT NULL
        )
        OR (
            event_type NOT IN ('recovery_succeeded', 'recovery_failed')
            AND recovery_action_id IS NULL
        )
    ),
    FOREIGN KEY (stream_id, previous_event_id)
        REFERENCES v3_audit_events(stream_id, event_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (recovery_action_id)
        REFERENCES v3_recovery_actions(recovery_action_id)
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_audit_events_session
ON v3_audit_events(session_id, sequence);

CREATE INDEX IF NOT EXISTS v3_audit_events_type
ON v3_audit_events(event_type, occurred_at);

CREATE TABLE IF NOT EXISTS v3_recovery_actions (
    recovery_action_id TEXT PRIMARY KEY
        CHECK (
            length(recovery_action_id) = 87
            AND substr(recovery_action_id, 1, 23) =
                'recovery_action_sha256_'
            AND substr(recovery_action_id, 24)
                NOT GLOB '*[^0-9a-f]*'
        ),
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL
        CHECK (length(session_id) > 0 AND length(session_id) <= 128),
    request_sha256 TEXT NOT NULL
        CHECK (
            length(request_sha256) = 71
            AND substr(request_sha256, 1, 7) = 'sha256:'
            AND substr(request_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    descriptor TEXT NOT NULL
        CHECK (
            length(CAST(descriptor AS BLOB)) > 0
            AND length(CAST(descriptor AS BLOB)) <= 1048576
        ),
    UNIQUE (session_id, request_sha256),
    FOREIGN KEY (recovery_action_id, event_id)
        REFERENCES v3_audit_events(recovery_action_id, event_id)
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS v3_recovery_actions_session
ON v3_recovery_actions(session_id, recovery_action_id);

CREATE TRIGGER IF NOT EXISTS v3_audit_stream_heads_identity_immutable
BEFORE UPDATE ON v3_audit_stream_heads
FOR EACH ROW
WHEN
    NEW.stream_id <> OLD.stream_id
    OR NEW.tenant_id <> OLD.tenant_id
    OR NEW.repository_id <> OLD.repository_id
    OR NEW.session_id <> OLD.session_id
    OR NEW.trace_id <> OLD.trace_id
    OR NEW.run_id <> OLD.run_id
BEGIN
    SELECT RAISE(ABORT, 'v3 audit stream identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_audit_stream_heads_initial
BEFORE INSERT ON v3_audit_stream_heads
FOR EACH ROW
WHEN NEW.current_sequence <> 0 OR NEW.current_event_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'v3 audit stream head must begin empty');
END;

CREATE TRIGGER IF NOT EXISTS v3_audit_stream_heads_advance
BEFORE UPDATE ON v3_audit_stream_heads
FOR EACH ROW
WHEN
    NEW.current_sequence <> OLD.current_sequence + 1
    OR NEW.current_event_id IS NULL
    OR NOT EXISTS (
        SELECT 1
        FROM v3_audit_events AS event
        WHERE event.stream_id = OLD.stream_id
          AND event.sequence = NEW.current_sequence
          AND event.event_id = NEW.current_event_id
          AND (
              (
                  OLD.current_sequence = 0
                  AND event.previous_event_id IS NULL
              )
              OR event.previous_event_id = OLD.current_event_id
          )
    )
BEGIN
    SELECT RAISE(ABORT, 'v3 audit stream head must advance exactly once');
END;

CREATE TRIGGER IF NOT EXISTS v3_audit_stream_heads_immutable_delete
BEFORE DELETE ON v3_audit_stream_heads
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 audit stream heads cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_audit_events_append
BEFORE INSERT ON v3_audit_events
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM v3_audit_stream_heads AS head
    WHERE head.stream_id = NEW.stream_id
      AND head.tenant_id = NEW.tenant_id
      AND head.repository_id = NEW.repository_id
      AND head.session_id = NEW.session_id
      AND head.trace_id = NEW.trace_id
      AND head.run_id = NEW.run_id
      AND NEW.sequence = head.current_sequence + 1
      AND (
          (
              head.current_sequence = 0
              AND NEW.previous_event_id IS NULL
          )
          OR NEW.previous_event_id = head.current_event_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'v3 audit event is not the next stream event');
END;

CREATE TRIGGER IF NOT EXISTS v3_audit_events_immutable_update
BEFORE UPDATE ON v3_audit_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 audit events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_audit_events_immutable_delete
BEFORE DELETE ON v3_audit_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 audit events cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS v3_recovery_actions_immutable_update
BEFORE UPDATE ON v3_recovery_actions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 recovery actions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_recovery_actions_immutable_delete
BEFORE DELETE ON v3_recovery_actions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'v3 recovery actions cannot be deleted');
END;

COMMIT;
