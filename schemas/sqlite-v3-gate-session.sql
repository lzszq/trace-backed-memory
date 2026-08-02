PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_gate_session_schema (
    singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'tbm.gate-session.v3')
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_v3_gate_session_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (1, 1, 'tbm.gate-session.v3');

CREATE TABLE IF NOT EXISTS gate_session_heads (
    session_id TEXT PRIMARY KEY
        CHECK (length(session_id) BETWEEN 1 AND 128),
    tenant_id TEXT NOT NULL
        CHECK (length(tenant_id) BETWEEN 1 AND 128),
    repository_id TEXT NOT NULL
        CHECK (length(repository_id) BETWEEN 1 AND 128),
    principal_id TEXT NOT NULL
        CHECK (length(principal_id) BETWEEN 1 AND 128),
    agent_client_id TEXT NOT NULL
        CHECK (length(agent_client_id) BETWEEN 1 AND 128),
    trace_id TEXT NOT NULL
        CHECK (length(trace_id) BETWEEN 1 AND 128),
    run_id TEXT NOT NULL
        CHECK (length(run_id) BETWEEN 1 AND 128),
    request_fingerprint TEXT NOT NULL
        CHECK (
            length(request_fingerprint) = 71
            AND substr(request_fingerprint, 1, 7) = 'sha256:'
            AND substr(request_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    idempotency_key TEXT NOT NULL
        CHECK (length(idempotency_key) BETWEEN 1 AND 512),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    UNIQUE (
        tenant_id,
        repository_id,
        principal_id,
        agent_client_id,
        idempotency_key
    )
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS gate_session_revisions (
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'created',
            'prepared',
            'awaiting_decision',
            'decided',
            'finalized',
            'executing',
            'completed',
            'canceled',
            'expired',
            'abandoned'
        )
    ),
    updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 20 AND 32),
    expires_at TEXT NOT NULL CHECK (length(expires_at) BETWEEN 20 AND 32),
    lease_expires_at TEXT
        CHECK (
            lease_expires_at IS NULL
            OR length(lease_expires_at) BETWEEN 20 AND 32
        ),
    payload TEXT NOT NULL CHECK (
        length(CAST(payload AS BLOB)) > 0
        AND length(CAST(payload AS BLOB)) <= 1048576
    ),
    PRIMARY KEY (session_id, version),
    FOREIGN KEY (session_id)
        REFERENCES gate_session_heads (session_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CHECK (
        (
            status IN (
                'prepared',
                'awaiting_decision',
                'decided',
                'finalized',
                'executing'
            )
            AND lease_expires_at IS NOT NULL
        )
        OR (
            status NOT IN (
                'prepared',
                'awaiting_decision',
                'decided',
                'finalized',
                'executing'
            )
            AND lease_expires_at IS NULL
        )
    )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS gate_session_revisions_due
ON gate_session_revisions (status, expires_at, lease_expires_at, session_id)
WHERE status IN (
    'prepared',
    'awaiting_decision',
    'decided',
    'finalized',
    'executing'
);

CREATE TRIGGER IF NOT EXISTS gate_session_heads_identity_immutable
BEFORE UPDATE ON gate_session_heads
FOR EACH ROW
WHEN
    NEW.session_id <> OLD.session_id
    OR NEW.tenant_id <> OLD.tenant_id
    OR NEW.repository_id <> OLD.repository_id
    OR NEW.principal_id <> OLD.principal_id
    OR NEW.agent_client_id <> OLD.agent_client_id
    OR NEW.trace_id <> OLD.trace_id
    OR NEW.run_id <> OLD.run_id
    OR NEW.request_fingerprint <> OLD.request_fingerprint
    OR NEW.idempotency_key <> OLD.idempotency_key
BEGIN
    SELECT RAISE(ABORT, 'GateSession identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS gate_session_heads_version_forward
BEFORE UPDATE OF current_version ON gate_session_heads
FOR EACH ROW
WHEN
    NEW.current_version <> OLD.current_version + 1
    OR NOT EXISTS (
        SELECT 1
        FROM gate_session_revisions AS revision
        WHERE revision.session_id = OLD.session_id
          AND revision.version = NEW.current_version
    )
BEGIN
    SELECT RAISE(ABORT, 'GateSession head must advance by one revision');
END;

CREATE TRIGGER IF NOT EXISTS gate_session_heads_immutable_delete
BEFORE DELETE ON gate_session_heads
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'GateSession head is immutable');
END;

CREATE TRIGGER IF NOT EXISTS gate_session_revisions_immutable_update
BEFORE UPDATE ON gate_session_revisions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'GateSession revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS gate_session_revisions_validate_insert
BEFORE INSERT ON gate_session_revisions
FOR EACH ROW
WHEN
    NEW.version <> COALESCE(
        (
            SELECT MAX(previous.version) + 1
            FROM gate_session_revisions AS previous
            WHERE previous.session_id = NEW.session_id
        ),
        1
    )
    OR (
        NEW.version = 1
        AND NEW.status <> 'created'
    )
    OR (
        NEW.version > 1
        AND NOT EXISTS (
            SELECT 1
            FROM gate_session_revisions AS previous
            JOIN gate_session_heads AS head
              ON head.session_id = previous.session_id
            WHERE previous.session_id = NEW.session_id
              AND previous.version = NEW.version - 1
              AND head.current_version = previous.version
              AND (
                  (
                      length(previous.updated_at) = 20
                      AND substr(previous.updated_at, 20, 1) = 'Z'
                  )
                  OR (
                      length(previous.updated_at) = 27
                      AND substr(previous.updated_at, 20, 1) = '.'
                      AND substr(previous.updated_at, 21, 6)
                          NOT GLOB '*[^0-9]*'
                      AND substr(previous.updated_at, 27, 1) = 'Z'
                  )
              )
              AND (
                  (
                      length(NEW.updated_at) = 20
                      AND substr(NEW.updated_at, 20, 1) = 'Z'
                  )
                  OR (
                      length(NEW.updated_at) = 27
                      AND substr(NEW.updated_at, 20, 1) = '.'
                      AND substr(NEW.updated_at, 21, 6)
                          NOT GLOB '*[^0-9]*'
                      AND substr(NEW.updated_at, 27, 1) = 'Z'
                  )
              )
              AND (
                  substr(previous.updated_at, 1, 19)
                      < substr(NEW.updated_at, 1, 19)
                  OR (
                      substr(previous.updated_at, 1, 19)
                          = substr(NEW.updated_at, 1, 19)
                      AND CASE
                          WHEN length(previous.updated_at) = 20 THEN 0
                          ELSE CAST(
                              substr(previous.updated_at, 21, 6) AS INTEGER
                          )
                      END < CASE
                          WHEN length(NEW.updated_at) = 20 THEN 0
                          ELSE CAST(
                              substr(NEW.updated_at, 21, 6) AS INTEGER
                          )
                      END
                  )
              )
              AND previous.expires_at = NEW.expires_at
              AND (
                  (previous.status = 'created'
                      AND NEW.status IN ('prepared', 'canceled'))
                  OR (previous.status = 'prepared'
                      AND NEW.status IN (
                          'awaiting_decision',
                          'canceled',
                          'expired'
                      ))
                  OR (previous.status = 'awaiting_decision'
                      AND NEW.status IN (
                          'decided',
                          'canceled',
                          'expired'
                      ))
                  OR (previous.status = 'decided'
                      AND NEW.status = 'finalized')
                  OR (previous.status = 'finalized'
                      AND NEW.status = 'executing')
                  OR (previous.status = 'executing'
                      AND NEW.status IN ('completed', 'abandoned'))
                  OR (
                      previous.status IN (
                          'prepared',
                          'awaiting_decision',
                          'decided',
                          'finalized',
                          'executing'
                      )
                      AND NEW.status = previous.status
                  )
              )
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid GateSession revision transition');
END;

CREATE TRIGGER IF NOT EXISTS gate_session_revisions_immutable_delete
BEFORE DELETE ON gate_session_revisions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'GateSession revision is immutable');
END;

COMMIT;
