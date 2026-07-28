PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

BEGIN IMMEDIATE;

CREATE TABLE trace_backed_memory_v3_entity_registry_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL COLLATE BINARY
        CHECK (contract_version = 'tbm.entity-registry.v3')
);

INSERT INTO trace_backed_memory_v3_entity_registry_schema (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.entity-registry.v3');

CREATE TABLE v3_entity_registry_snapshots (
    registry_sha256 TEXT PRIMARY KEY COLLATE BINARY
        CHECK (
            registry_sha256 GLOB 'sha256:*'
            AND length(registry_sha256) = 71
            AND substr(registry_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    registry_version TEXT NOT NULL UNIQUE COLLATE BINARY
        CHECK (length(registry_version) BETWEEN 1 AND 512),
    policy_sha256 TEXT NOT NULL COLLATE BINARY
        CHECK (
            policy_sha256 GLOB 'sha256:*'
            AND length(policy_sha256) = 71
            AND substr(policy_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    descriptor TEXT NOT NULL COLLATE BINARY
        CHECK (length(CAST(descriptor AS BLOB)) BETWEEN 2 AND 1048576)
);

CREATE TABLE v3_entity_registry_organizations (
    registry_sha256 TEXT NOT NULL COLLATE BINARY
        REFERENCES v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    organization_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(organization_id) BETWEEN 1 AND 128),
    display_name TEXT NOT NULL COLLATE BINARY
        CHECK (length(display_name) BETWEEN 1 AND 512),
    status TEXT NOT NULL COLLATE BINARY
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, organization_id)
);

CREATE TABLE v3_entity_registry_tenants (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    tenant_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(tenant_id) BETWEEN 1 AND 128),
    organization_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(organization_id) BETWEEN 1 AND 128),
    display_name TEXT NOT NULL COLLATE BINARY
        CHECK (length(display_name) BETWEEN 1 AND 512),
    status TEXT NOT NULL COLLATE BINARY
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, tenant_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, organization_id)
        REFERENCES v3_entity_registry_organizations (
            registry_sha256, organization_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_repositories (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    repository_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(repository_id) BETWEEN 1 AND 128),
    provider TEXT NOT NULL COLLATE BINARY
        CHECK (
            provider IN (
                'local', 'github', 'gitlab', 'bitbucket',
                'azure_devops', 'other'
            )
        ),
    provider_repository_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(provider_repository_id) BETWEEN 1 AND 512),
    canonical_locator_hash TEXT NOT NULL COLLATE BINARY
        CHECK (
            canonical_locator_hash GLOB 'sha256:*'
            AND length(canonical_locator_hash) = 71
            AND substr(canonical_locator_hash, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    display_name TEXT NOT NULL COLLATE BINARY
        CHECK (length(display_name) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, repository_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_repository_tenants (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    repository_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(repository_id) BETWEEN 1 AND 128),
    tenant_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(tenant_id) BETWEEN 1 AND 128),
    PRIMARY KEY (registry_sha256, repository_id),
    UNIQUE (registry_sha256, repository_id, tenant_id),
    FOREIGN KEY (registry_sha256, repository_id)
        REFERENCES v3_entity_registry_repositories (
            registry_sha256, repository_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_repository_legacy_aliases (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    repository_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(repository_id) BETWEEN 1 AND 128),
    alias TEXT NOT NULL COLLATE BINARY
        CHECK (length(alias) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, repository_id, alias),
    FOREIGN KEY (registry_sha256, repository_id)
        REFERENCES v3_entity_registry_repositories (
            registry_sha256, repository_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_repository_aliases (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    tenant_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(tenant_id) BETWEEN 1 AND 128),
    alias TEXT NOT NULL COLLATE BINARY
        CHECK (length(alias) BETWEEN 1 AND 512),
    repository_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(repository_id) BETWEEN 1 AND 128),
    source TEXT NOT NULL COLLATE BINARY
        CHECK (length(source) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, tenant_id, alias),
    FOREIGN KEY (registry_sha256, repository_id, tenant_id)
        REFERENCES v3_entity_registry_repository_tenants (
            registry_sha256, repository_id, tenant_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_principals (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    principal_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(principal_id) BETWEEN 1 AND 128),
    issuer TEXT NOT NULL COLLATE BINARY
        CHECK (length(issuer) BETWEEN 1 AND 512),
    subject_hash TEXT NOT NULL COLLATE BINARY
        CHECK (
            subject_hash GLOB 'sha256:*'
            AND length(subject_hash) = 71
            AND substr(subject_hash, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT COLLATE BINARY
        CHECK (
            tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 128
        ),
    status TEXT NOT NULL COLLATE BINARY
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, principal_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_agent_clients (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    agent_client_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(agent_client_id) BETWEEN 1 AND 128),
    tenant_id TEXT COLLATE BINARY
        CHECK (
            tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 128
        ),
    client_kind TEXT NOT NULL COLLATE BINARY
        CHECK (
            client_kind IN ('local_agent', 'service', 'sdk', 'mcp', 'worker')
        ),
    status TEXT NOT NULL COLLATE BINARY
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, agent_client_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_environments (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    environment_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(environment_id) BETWEEN 1 AND 128),
    tenant_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(tenant_id) BETWEEN 1 AND 128),
    repository_id TEXT COLLATE BINARY
        CHECK (
            repository_id IS NULL
            OR length(repository_id) BETWEEN 1 AND 128
        ),
    environment_kind TEXT NOT NULL COLLATE BINARY
        CHECK (
            environment_kind IN (
                'development', 'test', 'staging',
                'production', 'ci', 'other'
            )
        ),
    display_name TEXT NOT NULL COLLATE BINARY
        CHECK (length(display_name) BETWEEN 1 AND 512),
    status TEXT NOT NULL COLLATE BINARY
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, environment_id),
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, repository_id, tenant_id)
        REFERENCES v3_entity_registry_repository_tenants (
            registry_sha256, repository_id, tenant_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_environment_attributes (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    environment_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(environment_id) BETWEEN 1 AND 128),
    attribute_name TEXT NOT NULL COLLATE BINARY
        CHECK (length(attribute_name) BETWEEN 1 AND 512),
    attribute_value TEXT NOT NULL COLLATE BINARY
        CHECK (length(attribute_value) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, environment_id, attribute_name),
    FOREIGN KEY (registry_sha256, environment_id)
        REFERENCES v3_entity_registry_environments (
            registry_sha256, environment_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_role_bindings (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    binding_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(binding_id) BETWEEN 1 AND 128),
    principal_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(principal_id) BETWEEN 1 AND 128),
    agent_client_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(agent_client_id) BETWEEN 1 AND 128),
    role_name TEXT NOT NULL COLLATE BINARY
        CHECK (length(role_name) BETWEEN 1 AND 512),
    scope_kind TEXT NOT NULL COLLATE BINARY
        CHECK (scope_kind IN ('global', 'tenant', 'repository')),
    tenant_id TEXT COLLATE BINARY
        CHECK (
            tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 128
        ),
    repository_id TEXT COLLATE BINARY
        CHECK (
            repository_id IS NULL
            OR length(repository_id) BETWEEN 1 AND 128
        ),
    status TEXT NOT NULL COLLATE BINARY
        CHECK (status IN ('active', 'revoked')),
    valid_from TEXT NOT NULL COLLATE BINARY
        CHECK (
            length(valid_from) BETWEEN 20 AND 32
            AND substr(valid_from, 11, 1) = 'T'
        ),
    expires_at TEXT COLLATE BINARY
        CHECK (
            expires_at IS NULL
            OR (
                length(expires_at) BETWEEN 20 AND 32
                AND substr(expires_at, 11, 1) = 'T'
            )
        ),
    PRIMARY KEY (registry_sha256, binding_id),
    FOREIGN KEY (registry_sha256, principal_id)
        REFERENCES v3_entity_registry_principals (
            registry_sha256, principal_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, agent_client_id)
        REFERENCES v3_entity_registry_agent_clients (
            registry_sha256, agent_client_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, repository_id, tenant_id)
        REFERENCES v3_entity_registry_repository_tenants (
            registry_sha256, repository_id, tenant_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK (
        (scope_kind = 'global' AND tenant_id IS NULL AND repository_id IS NULL)
        OR (scope_kind = 'tenant' AND tenant_id IS NOT NULL
            AND repository_id IS NULL)
        OR (scope_kind = 'repository' AND tenant_id IS NOT NULL
            AND repository_id IS NOT NULL)
    )
);

CREATE TABLE v3_entity_registry_binding_permissions (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    binding_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(binding_id) BETWEEN 1 AND 128),
    permission TEXT NOT NULL COLLATE BINARY
        CHECK (
            permission IN (
                'artifact:read', 'artifact:write',
                'gate_session:create', 'gate_session:transition',
                'memory:activate', 'memory:create', 'memory:inject',
                'memory:retrieve', 'memory:review', 'memory:verify',
                'platform:admin', 'platform:audit_read',
                'policy:approve_global', 'policy:create_global',
                'tenant:audit_read'
            )
        ),
    PRIMARY KEY (registry_sha256, binding_id, permission),
    FOREIGN KEY (registry_sha256, binding_id)
        REFERENCES v3_entity_registry_role_bindings (
            registry_sha256, binding_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE v3_entity_registry_scope_attributes (
    registry_sha256 TEXT NOT NULL COLLATE BINARY,
    binding_id TEXT NOT NULL COLLATE BINARY
        CHECK (length(binding_id) BETWEEN 1 AND 128),
    attribute_name TEXT NOT NULL COLLATE BINARY
        CHECK (length(attribute_name) BETWEEN 1 AND 512),
    attribute_value TEXT NOT NULL COLLATE BINARY
        CHECK (length(attribute_value) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, binding_id, attribute_name),
    FOREIGN KEY (registry_sha256, binding_id)
        REFERENCES v3_entity_registry_role_bindings (
            registry_sha256, binding_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE INDEX v3_entity_registry_snapshots_policy
ON v3_entity_registry_snapshots (policy_sha256, registry_sha256);

CREATE INDEX v3_entity_registry_tenants_organization
ON v3_entity_registry_tenants (
    registry_sha256, organization_id, tenant_id
);

CREATE INDEX v3_entity_registry_environments_tenant
ON v3_entity_registry_environments (
    registry_sha256, tenant_id, environment_id
);

CREATE TRIGGER v3_entity_registry_schema_identity_guard
BEFORE INSERT ON trace_backed_memory_v3_entity_registry_schema
WHEN EXISTS (SELECT 1 FROM trace_backed_memory_v3_entity_registry_schema)
BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 identity is immutable');
END;

CREATE TRIGGER v3_entity_registry_schema_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_entity_registry_schema
BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;

CREATE TRIGGER v3_entity_registry_schema_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_entity_registry_schema
BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;

CREATE TRIGGER v3_entity_registry_snapshots_identity_guard
BEFORE INSERT ON v3_entity_registry_snapshots
WHEN EXISTS (
    SELECT 1 FROM v3_entity_registry_snapshots
    WHERE registry_sha256 = NEW.registry_sha256
       OR registry_version = NEW.registry_version
)
BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 identity is immutable');
END;

CREATE TRIGGER v3_entity_registry_snapshots_immutable_update
BEFORE UPDATE ON v3_entity_registry_snapshots
BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;

CREATE TRIGGER v3_entity_registry_snapshots_immutable_delete
BEFORE DELETE ON v3_entity_registry_snapshots
BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;

CREATE TRIGGER v3_entity_registry_rows_organizations_update
BEFORE UPDATE ON v3_entity_registry_organizations BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_organizations_delete
BEFORE DELETE ON v3_entity_registry_organizations BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_tenants_update
BEFORE UPDATE ON v3_entity_registry_tenants BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_tenants_delete
BEFORE DELETE ON v3_entity_registry_tenants BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_repositories_update
BEFORE UPDATE ON v3_entity_registry_repositories BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_repositories_delete
BEFORE DELETE ON v3_entity_registry_repositories BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_repository_tenants_update
BEFORE UPDATE ON v3_entity_registry_repository_tenants BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_repository_tenants_delete
BEFORE DELETE ON v3_entity_registry_repository_tenants BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_legacy_aliases_update
BEFORE UPDATE ON v3_entity_registry_repository_legacy_aliases BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_legacy_aliases_delete
BEFORE DELETE ON v3_entity_registry_repository_legacy_aliases BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_aliases_update
BEFORE UPDATE ON v3_entity_registry_repository_aliases BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_aliases_delete
BEFORE DELETE ON v3_entity_registry_repository_aliases BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_principals_update
BEFORE UPDATE ON v3_entity_registry_principals BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_principals_delete
BEFORE DELETE ON v3_entity_registry_principals BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_clients_update
BEFORE UPDATE ON v3_entity_registry_agent_clients BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_clients_delete
BEFORE DELETE ON v3_entity_registry_agent_clients BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_environments_update
BEFORE UPDATE ON v3_entity_registry_environments BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_environments_delete
BEFORE DELETE ON v3_entity_registry_environments BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_environment_attributes_update
BEFORE UPDATE ON v3_entity_registry_environment_attributes BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_environment_attributes_delete
BEFORE DELETE ON v3_entity_registry_environment_attributes BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_bindings_update
BEFORE UPDATE ON v3_entity_registry_role_bindings BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_bindings_delete
BEFORE DELETE ON v3_entity_registry_role_bindings BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_permissions_update
BEFORE UPDATE ON v3_entity_registry_binding_permissions BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_permissions_delete
BEFORE DELETE ON v3_entity_registry_binding_permissions BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_scope_attributes_update
BEFORE UPDATE ON v3_entity_registry_scope_attributes BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;
CREATE TRIGGER v3_entity_registry_rows_scope_attributes_delete
BEFORE DELETE ON v3_entity_registry_scope_attributes BEGIN
    SELECT RAISE(ABORT, 'SQLite entity registry v3 rows are immutable');
END;

COMMIT;
