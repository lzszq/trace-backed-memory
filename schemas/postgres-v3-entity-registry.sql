BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL THEN
        RAISE EXCEPTION
            'PostgreSQL entity registry v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL entity registry v3 requires active schema version 2, found %',
            active_version;
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_entity_registry;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_entity_registry FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_entity_registry.schema_metadata (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.entity-registry.v3')
);

INSERT INTO trace_backed_memory_v3_entity_registry.schema_metadata (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.entity-registry.v3');

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots (
    registry_sha256 text COLLATE "C" PRIMARY KEY
        CHECK (
            registry_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    registry_version text COLLATE "C" NOT NULL UNIQUE
        CHECK (char_length(registry_version) BETWEEN 1 AND 512),
    policy_sha256 text COLLATE "C" NOT NULL
        CHECK (
            policy_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 2 AND 1048576)
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_organizations (
    registry_sha256 text COLLATE "C" NOT NULL
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    organization_id text COLLATE "C" NOT NULL
        CHECK (char_length(organization_id) BETWEEN 1 AND 128),
    display_name text COLLATE "C" NOT NULL
        CHECK (char_length(display_name) BETWEEN 1 AND 512),
    status text COLLATE "C" NOT NULL
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, organization_id)
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_tenants (
    registry_sha256 text COLLATE "C" NOT NULL,
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    organization_id text COLLATE "C" NOT NULL
        CHECK (char_length(organization_id) BETWEEN 1 AND 128),
    display_name text COLLATE "C" NOT NULL
        CHECK (char_length(display_name) BETWEEN 1 AND 512),
    status text COLLATE "C" NOT NULL
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, tenant_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, organization_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_organizations (
            registry_sha256, organization_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_repositories (
    registry_sha256 text COLLATE "C" NOT NULL,
    repository_id text COLLATE "C" NOT NULL
        CHECK (char_length(repository_id) BETWEEN 1 AND 128),
    provider text COLLATE "C" NOT NULL
        CHECK (
            provider IN (
                'local', 'github', 'gitlab', 'bitbucket',
                'azure_devops', 'other'
            )
        ),
    provider_repository_id text COLLATE "C" NOT NULL
        CHECK (char_length(provider_repository_id) BETWEEN 1 AND 512),
    canonical_locator_hash text COLLATE "C" NOT NULL
        CHECK (
            canonical_locator_hash ~ '^sha256:[0-9a-f]{64}$'
        ),
    display_name text COLLATE "C" NOT NULL
        CHECK (char_length(display_name) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, repository_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_repository_tenants (
    registry_sha256 text COLLATE "C" NOT NULL,
    repository_id text COLLATE "C" NOT NULL
        CHECK (char_length(repository_id) BETWEEN 1 AND 128),
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    PRIMARY KEY (registry_sha256, repository_id),
    UNIQUE (registry_sha256, repository_id, tenant_id),
    FOREIGN KEY (registry_sha256, repository_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_repositories (
            registry_sha256, repository_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_repository_legacy_aliases (
    registry_sha256 text COLLATE "C" NOT NULL,
    repository_id text COLLATE "C" NOT NULL
        CHECK (char_length(repository_id) BETWEEN 1 AND 128),
    alias text COLLATE "C" NOT NULL
        CHECK (char_length(alias) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, repository_id, alias),
    FOREIGN KEY (registry_sha256, repository_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_repositories (
            registry_sha256, repository_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_repository_aliases (
    registry_sha256 text COLLATE "C" NOT NULL,
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    alias text COLLATE "C" NOT NULL
        CHECK (char_length(alias) BETWEEN 1 AND 512),
    repository_id text COLLATE "C" NOT NULL
        CHECK (char_length(repository_id) BETWEEN 1 AND 128),
    source text COLLATE "C" NOT NULL
        CHECK (char_length(source) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, tenant_id, alias),
    FOREIGN KEY (registry_sha256, repository_id, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_repository_tenants (
            registry_sha256, repository_id, tenant_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_principals (
    registry_sha256 text COLLATE "C" NOT NULL,
    principal_id text COLLATE "C" NOT NULL
        CHECK (char_length(principal_id) BETWEEN 1 AND 128),
    issuer text COLLATE "C" NOT NULL
        CHECK (char_length(issuer) BETWEEN 1 AND 512),
    subject_hash text COLLATE "C" NOT NULL
        CHECK (
            subject_hash ~ '^sha256:[0-9a-f]{64}$'
        ),
    tenant_id text COLLATE "C"
        CHECK (
            tenant_id IS NULL OR char_length(tenant_id) BETWEEN 1 AND 128
        ),
    status text COLLATE "C" NOT NULL
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, principal_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_agent_clients (
    registry_sha256 text COLLATE "C" NOT NULL,
    agent_client_id text COLLATE "C" NOT NULL
        CHECK (char_length(agent_client_id) BETWEEN 1 AND 128),
    tenant_id text COLLATE "C"
        CHECK (
            tenant_id IS NULL OR char_length(tenant_id) BETWEEN 1 AND 128
        ),
    client_kind text COLLATE "C" NOT NULL
        CHECK (
            client_kind IN ('local_agent', 'service', 'sdk', 'mcp', 'worker')
        ),
    status text COLLATE "C" NOT NULL
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, agent_client_id),
    FOREIGN KEY (registry_sha256)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots (registry_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_environments (
    registry_sha256 text COLLATE "C" NOT NULL,
    environment_id text COLLATE "C" NOT NULL
        CHECK (char_length(environment_id) BETWEEN 1 AND 128),
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    repository_id text COLLATE "C"
        CHECK (
            repository_id IS NULL
            OR char_length(repository_id) BETWEEN 1 AND 128
        ),
    environment_kind text COLLATE "C" NOT NULL
        CHECK (
            environment_kind IN (
                'development', 'test', 'staging',
                'production', 'ci', 'other'
            )
        ),
    display_name text COLLATE "C" NOT NULL
        CHECK (char_length(display_name) BETWEEN 1 AND 512),
    status text COLLATE "C" NOT NULL
        CHECK (status IN ('active', 'disabled')),
    PRIMARY KEY (registry_sha256, environment_id),
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, repository_id, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_repository_tenants (
            registry_sha256, repository_id, tenant_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_environment_attributes (
    registry_sha256 text COLLATE "C" NOT NULL,
    environment_id text COLLATE "C" NOT NULL
        CHECK (char_length(environment_id) BETWEEN 1 AND 128),
    attribute_name text COLLATE "C" NOT NULL
        CHECK (char_length(attribute_name) BETWEEN 1 AND 512),
    attribute_value text COLLATE "C" NOT NULL
        CHECK (char_length(attribute_value) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, environment_id, attribute_name),
    FOREIGN KEY (registry_sha256, environment_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_environments (
            registry_sha256, environment_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_role_bindings (
    registry_sha256 text COLLATE "C" NOT NULL,
    binding_id text COLLATE "C" NOT NULL
        CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    principal_id text COLLATE "C" NOT NULL
        CHECK (char_length(principal_id) BETWEEN 1 AND 128),
    agent_client_id text COLLATE "C" NOT NULL
        CHECK (char_length(agent_client_id) BETWEEN 1 AND 128),
    role_name text COLLATE "C" NOT NULL
        CHECK (char_length(role_name) BETWEEN 1 AND 512),
    scope_kind text COLLATE "C" NOT NULL
        CHECK (scope_kind IN ('global', 'tenant', 'repository')),
    tenant_id text COLLATE "C"
        CHECK (
            tenant_id IS NULL OR char_length(tenant_id) BETWEEN 1 AND 128
        ),
    repository_id text COLLATE "C"
        CHECK (
            repository_id IS NULL
            OR char_length(repository_id) BETWEEN 1 AND 128
        ),
    status text COLLATE "C" NOT NULL
        CHECK (status IN ('active', 'revoked')),
    valid_from text COLLATE "C" NOT NULL
        CHECK (
            char_length(valid_from) BETWEEN 20 AND 32
            AND substr(valid_from, 11, 1) = 'T'
        ),
    expires_at text COLLATE "C"
        CHECK (
            expires_at IS NULL
            OR (
                char_length(expires_at) BETWEEN 20 AND 32
                AND substr(expires_at, 11, 1) = 'T'
            )
        ),
    PRIMARY KEY (registry_sha256, binding_id),
    FOREIGN KEY (registry_sha256, principal_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_principals (
            registry_sha256, principal_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, agent_client_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_agent_clients (
            registry_sha256, agent_client_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_tenants (registry_sha256, tenant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (registry_sha256, repository_id, tenant_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_repository_tenants (
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

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_binding_permissions (
    registry_sha256 text COLLATE "C" NOT NULL,
    binding_id text COLLATE "C" NOT NULL
        CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    permission text COLLATE "C" NOT NULL
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
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_role_bindings (
            registry_sha256, binding_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE trace_backed_memory_v3_entity_registry.v3_entity_registry_scope_attributes (
    registry_sha256 text COLLATE "C" NOT NULL,
    binding_id text COLLATE "C" NOT NULL
        CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    attribute_name text COLLATE "C" NOT NULL
        CHECK (char_length(attribute_name) BETWEEN 1 AND 512),
    attribute_value text COLLATE "C" NOT NULL
        CHECK (char_length(attribute_value) BETWEEN 1 AND 512),
    PRIMARY KEY (registry_sha256, binding_id, attribute_name),
    FOREIGN KEY (registry_sha256, binding_id)
        REFERENCES trace_backed_memory_v3_entity_registry.v3_entity_registry_role_bindings (
            registry_sha256, binding_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE INDEX v3_entity_registry_snapshots_policy
ON trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots (policy_sha256, registry_sha256);

CREATE INDEX v3_entity_registry_tenants_organization
ON trace_backed_memory_v3_entity_registry.v3_entity_registry_tenants (
    registry_sha256, organization_id, tenant_id
);

CREATE INDEX v3_entity_registry_environments_tenant
ON trace_backed_memory_v3_entity_registry.v3_entity_registry_environments (
    registry_sha256, tenant_id, environment_id
);

CREATE FUNCTION
trace_backed_memory_v3_entity_registry.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL entity registry v3 records are immutable';
END
$$;

REVOKE ALL ON FUNCTION
trace_backed_memory_v3_entity_registry.reject_immutable_change()
FROM PUBLIC;

DO $$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT *
        FROM (
            VALUES
                ('metadata', 'schema_metadata'),
                ('snapshots', 'v3_entity_registry_snapshots'),
                ('organizations', 'v3_entity_registry_organizations'),
                ('tenants', 'v3_entity_registry_tenants'),
                ('repositories', 'v3_entity_registry_repositories'),
                ('repository_tenants', 'v3_entity_registry_repository_tenants'),
                ('legacy_aliases', 'v3_entity_registry_repository_legacy_aliases'),
                ('aliases', 'v3_entity_registry_repository_aliases'),
                ('principals', 'v3_entity_registry_principals'),
                ('clients', 'v3_entity_registry_agent_clients'),
                ('environments', 'v3_entity_registry_environments'),
                ('environment_attrs', 'v3_entity_registry_environment_attributes'),
                ('bindings', 'v3_entity_registry_role_bindings'),
                ('permissions', 'v3_entity_registry_binding_permissions'),
                ('scope_attrs', 'v3_entity_registry_scope_attributes')
        ) AS tables(short_name, relation_name)
    LOOP
        EXECUTE pg_catalog.format(
            'CREATE TRIGGER entity_%I_immutable '
            'BEFORE UPDATE OR DELETE ON '
            'trace_backed_memory_v3_entity_registry.%I '
            'FOR EACH ROW EXECUTE FUNCTION '
            'trace_backed_memory_v3_entity_registry.reject_immutable_change()',
            target.short_name,
            target.relation_name
        );
        EXECUTE pg_catalog.format(
            'CREATE TRIGGER entity_%I_no_truncate '
            'BEFORE TRUNCATE ON '
            'trace_backed_memory_v3_entity_registry.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION '
            'trace_backed_memory_v3_entity_registry.reject_immutable_change()',
            target.short_name,
            target.relation_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON '
            'trace_backed_memory_v3_entity_registry.%I FROM PUBLIC',
            target.relation_name
        );
    END LOOP;
END
$$;

COMMIT;
