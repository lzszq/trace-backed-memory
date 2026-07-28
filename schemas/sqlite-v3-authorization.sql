PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

BEGIN IMMEDIATE;

CREATE TABLE trace_backed_memory_v3_authorization_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL COLLATE BINARY
        CHECK (contract_version = 'tbm.authorization.v3')
);

INSERT INTO trace_backed_memory_v3_authorization_schema (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.authorization.v3');

CREATE TABLE v3_authorization_policies (
    policy_sha256 TEXT PRIMARY KEY COLLATE BINARY
        CHECK (
            policy_sha256 GLOB 'sha256:*'
            AND length(policy_sha256) = 71
            AND substr(policy_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    policy_version TEXT NOT NULL UNIQUE COLLATE BINARY
        CHECK (length(policy_version) BETWEEN 1 AND 128),
    descriptor TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(descriptor AS BLOB)) BETWEEN 2 AND 1048576)
);

CREATE TABLE v3_authorization_decisions (
    authorization_event_id TEXT PRIMARY KEY COLLATE BINARY
        CHECK (
            authorization_event_id GLOB 'authz_sha256_*'
            AND length(authorization_event_id) = 77
            AND substr(authorization_event_id, 14)
                NOT GLOB '*[^0-9a-f]*'
        ),
    request_id TEXT NOT NULL UNIQUE COLLATE BINARY
        CHECK (length(request_id) BETWEEN 1 AND 128),
    request_sha256 TEXT NOT NULL COLLATE BINARY
        CHECK (
            request_sha256 GLOB 'sha256:*'
            AND length(request_sha256) = 71
            AND substr(request_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    policy_sha256 TEXT NOT NULL COLLATE BINARY
        REFERENCES v3_authorization_policies (policy_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    principal_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(principal_id) BETWEEN 1 AND 128),
    agent_client_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(agent_client_id) BETWEEN 1 AND 128),
    tenant_id TEXT COLLATE BINARY
        CHECK (tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 128),
    repository_id TEXT COLLATE BINARY
        CHECK (
            repository_id IS NULL
            OR length(repository_id) BETWEEN 1 AND 128
        ),
    permission TEXT NOT NULL COLLATE BINARY,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    reason TEXT NOT NULL COLLATE BINARY,
    decided_at TEXT NOT NULL COLLATE BINARY,
    descriptor TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(descriptor AS BLOB)) BETWEEN 2 AND 1048576)
);

CREATE INDEX v3_authorization_decisions_policy
ON v3_authorization_decisions (policy_sha256, decided_at, authorization_event_id);

CREATE INDEX v3_authorization_decisions_principal
ON v3_authorization_decisions (principal_id, decided_at, authorization_event_id);

CREATE TRIGGER v3_authorization_schema_identity_guard
BEFORE INSERT ON trace_backed_memory_v3_authorization_schema
WHEN EXISTS (
    SELECT 1 FROM trace_backed_memory_v3_authorization_schema
)
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 identity is immutable');
END;

CREATE TRIGGER v3_authorization_schema_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_authorization_schema
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 rows are immutable');
END;

CREATE TRIGGER v3_authorization_schema_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_authorization_schema
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 rows are immutable');
END;

CREATE TRIGGER v3_authorization_policies_immutable_update
BEFORE UPDATE ON v3_authorization_policies
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 rows are immutable');
END;

CREATE TRIGGER v3_authorization_policies_identity_guard
BEFORE INSERT ON v3_authorization_policies
WHEN EXISTS (
    SELECT 1
    FROM v3_authorization_policies
    WHERE policy_sha256 = NEW.policy_sha256
       OR policy_version = NEW.policy_version
)
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 identity is immutable');
END;

CREATE TRIGGER v3_authorization_policies_immutable_delete
BEFORE DELETE ON v3_authorization_policies
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 rows are immutable');
END;

CREATE TRIGGER v3_authorization_decisions_immutable_update
BEFORE UPDATE ON v3_authorization_decisions
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 rows are immutable');
END;

CREATE TRIGGER v3_authorization_decisions_identity_guard
BEFORE INSERT ON v3_authorization_decisions
WHEN EXISTS (
    SELECT 1
    FROM v3_authorization_decisions
    WHERE authorization_event_id = NEW.authorization_event_id
       OR request_id = NEW.request_id
)
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 identity is immutable');
END;

CREATE TRIGGER v3_authorization_decisions_immutable_delete
BEFORE DELETE ON v3_authorization_decisions
BEGIN
    SELECT RAISE(ABORT, 'SQLite authorization v3 rows are immutable');
END;

COMMIT;
