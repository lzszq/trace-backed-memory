PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_memory_publication_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);
INSERT OR IGNORE INTO trace_backed_memory_v3_memory_publication_schema
    (singleton, schema_version) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS v3_memory_revision_approvals (
    approval_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    repository_id TEXT,
    repository_id_key TEXT NOT NULL CHECK (
        repository_id_key = COALESCE(repository_id, '')
    ),
    memory_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) <= 1048576 AND json_valid(descriptor)
    ),
    authorization_policy_descriptor TEXT NOT NULL CHECK (
        length(CAST(authorization_policy_descriptor AS BLOB)) <= 1048576
        AND json_valid(authorization_policy_descriptor)
    ),
    authorization_request_descriptor TEXT NOT NULL CHECK (
        length(CAST(authorization_request_descriptor AS BLOB)) <= 1048576
        AND json_valid(authorization_request_descriptor)
    ),
    authorization_decision_descriptor TEXT NOT NULL CHECK (
        length(CAST(authorization_decision_descriptor AS BLOB)) <= 1048576
        AND json_valid(authorization_decision_descriptor)
    ),
    attestation_verified_by TEXT NOT NULL,
    FOREIGN KEY (revision_id)
        REFERENCES v3_memory_revision_proposals(revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS v3_memory_revision_activations (
    activation_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    revision_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    repository_id TEXT,
    repository_id_key TEXT NOT NULL CHECK (
        repository_id_key = COALESCE(repository_id, '')
    ),
    memory_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    previous_activation_id TEXT,
    descriptor TEXT NOT NULL CHECK (
        length(CAST(descriptor AS BLOB)) <= 1048576 AND json_valid(descriptor)
    ),
    authorization_policy_descriptor TEXT NOT NULL CHECK (
        length(CAST(authorization_policy_descriptor AS BLOB)) <= 1048576
        AND json_valid(authorization_policy_descriptor)
    ),
    authorization_request_descriptor TEXT NOT NULL CHECK (
        length(CAST(authorization_request_descriptor AS BLOB)) <= 1048576
        AND json_valid(authorization_request_descriptor)
    ),
    authorization_decision_descriptor TEXT NOT NULL CHECK (
        length(CAST(authorization_decision_descriptor AS BLOB)) <= 1048576
        AND json_valid(authorization_decision_descriptor)
    ),
    attestation_verified_by TEXT NOT NULL,
    UNIQUE (
        tenant_id,
        repository_id_key,
        memory_id,
        revision_number
    ),
    FOREIGN KEY (approval_id)
        REFERENCES v3_memory_revision_approvals(approval_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (revision_id)
        REFERENCES v3_memory_revision_proposals(revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (previous_activation_id)
        REFERENCES v3_memory_revision_activations(activation_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS v3_memory_revision_activation_heads (
    tenant_id TEXT NOT NULL,
    repository_id TEXT,
    repository_id_key TEXT NOT NULL CHECK (
        repository_id_key = COALESCE(repository_id, '')
    ),
    memory_id TEXT NOT NULL,
    current_revision_number INTEGER NOT NULL CHECK (
        current_revision_number >= 0
    ),
    current_revision_id TEXT,
    current_activation_id TEXT UNIQUE,
    PRIMARY KEY (tenant_id, repository_id_key, memory_id),
    FOREIGN KEY (current_revision_id)
        REFERENCES v3_memory_revision_proposals(revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (current_activation_id)
        REFERENCES v3_memory_revision_activations(activation_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK (
        (
            current_revision_number = 0
            AND current_revision_id IS NULL
            AND current_activation_id IS NULL
        )
        OR (
            current_revision_number >= 1
            AND current_revision_id IS NOT NULL
            AND current_activation_id IS NOT NULL
        )
    )
);

CREATE TRIGGER IF NOT EXISTS v3_memory_revision_approvals_validate_insert
BEFORE INSERT ON v3_memory_revision_approvals
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM v3_memory_revision_proposals AS proposal
        WHERE proposal.revision_id = NEW.revision_id
          AND proposal.memory_id = NEW.memory_id
          AND proposal.revision_number = NEW.revision_number
    ) THEN RAISE(ABORT, 'approval proposal mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS v3_memory_revision_activations_validate_insert
BEFORE INSERT ON v3_memory_revision_activations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM v3_memory_revision_approvals AS approval
        WHERE approval.approval_id = NEW.approval_id
          AND approval.revision_id = NEW.revision_id
          AND approval.tenant_id = NEW.tenant_id
          AND approval.repository_id_key = NEW.repository_id_key
          AND approval.memory_id = NEW.memory_id
          AND approval.revision_number = NEW.revision_number
    ) THEN RAISE(ABORT, 'activation approval mismatch') END;
    SELECT CASE
        WHEN NEW.revision_number = 1
         AND NEW.previous_activation_id IS NOT NULL
        THEN RAISE(ABORT, 'first activation must not have a predecessor')
        WHEN NEW.revision_number > 1 AND NOT EXISTS (
            SELECT 1
            FROM v3_memory_revision_activation_heads AS head
            WHERE head.tenant_id = NEW.tenant_id
              AND head.repository_id_key = NEW.repository_id_key
              AND head.memory_id = NEW.memory_id
              AND head.current_revision_number = NEW.revision_number - 1
              AND head.current_activation_id = NEW.previous_activation_id
        )
        THEN RAISE(ABORT, 'activation predecessor is not current head')
    END;
END;

CREATE TRIGGER IF NOT EXISTS v3_memory_revision_heads_validate_insert
BEFORE INSERT ON v3_memory_revision_activation_heads
BEGIN
    SELECT CASE WHEN
        NEW.current_revision_number <> 0
        OR NEW.current_revision_id IS NOT NULL
        OR NEW.current_activation_id IS NOT NULL
    THEN RAISE(ABORT, 'activation head must start empty') END;
END;

CREATE TRIGGER IF NOT EXISTS v3_memory_revision_heads_validate_advance
BEFORE UPDATE ON v3_memory_revision_activation_heads
BEGIN
    SELECT CASE WHEN
        NEW.tenant_id <> OLD.tenant_id
        OR NEW.repository_id_key <> OLD.repository_id_key
        OR NEW.repository_id IS NOT OLD.repository_id
        OR NEW.memory_id <> OLD.memory_id
        OR NEW.current_revision_number <> OLD.current_revision_number + 1
        OR NOT EXISTS (
            SELECT 1
            FROM v3_memory_revision_activations AS activation
            WHERE activation.activation_id = NEW.current_activation_id
              AND activation.revision_id = NEW.current_revision_id
              AND activation.tenant_id = NEW.tenant_id
              AND activation.repository_id_key = NEW.repository_id_key
              AND activation.memory_id = NEW.memory_id
              AND activation.revision_number = NEW.current_revision_number
              AND activation.previous_activation_id
                  IS OLD.current_activation_id
        )
    THEN RAISE(ABORT, 'activation head advance mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS v3_memory_revision_approvals_immutable_update
BEFORE UPDATE ON v3_memory_revision_approvals
BEGIN
    SELECT RAISE(ABORT, 'memory revision approval is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_approvals_immutable_delete
BEFORE DELETE ON v3_memory_revision_approvals
BEGIN
    SELECT RAISE(ABORT, 'memory revision approval is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_activations_immutable_update
BEFORE UPDATE ON v3_memory_revision_activations
BEGIN
    SELECT RAISE(ABORT, 'memory revision activation is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_activations_immutable_delete
BEFORE DELETE ON v3_memory_revision_activations
BEGIN
    SELECT RAISE(ABORT, 'memory revision activation is immutable');
END;
CREATE TRIGGER IF NOT EXISTS v3_memory_revision_heads_no_delete
BEFORE DELETE ON v3_memory_revision_activation_heads
BEGIN
    SELECT RAISE(ABORT, 'memory revision activation head cannot be deleted');
END;
