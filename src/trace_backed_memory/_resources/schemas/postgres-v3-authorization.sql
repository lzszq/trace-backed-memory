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
            'PostgreSQL authorization v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL authorization v3 requires active schema version 2, found %',
            active_version;
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_authorization;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_authorization FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_authorization.schema_metadata (
    singleton boolean
        CONSTRAINT authorization_schema_metadata_pkey PRIMARY KEY
        DEFAULT true
        CONSTRAINT authorization_schema_metadata_singleton_check CHECK (singleton),
    schema_version integer NOT NULL
        CONSTRAINT authorization_schema_metadata_schema_version_check
        CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CONSTRAINT authorization_schema_metadata_contract_version_check
        CHECK (contract_version = 'tbm.authorization.v3')
);

INSERT INTO trace_backed_memory_v3_authorization.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (true, 1, 'tbm.authorization.v3');

CREATE TABLE trace_backed_memory_v3_authorization.authorization_policies (
    policy_sha256 text COLLATE "C"
        CONSTRAINT authorization_policies_pkey PRIMARY KEY
        CONSTRAINT authorization_policies_sha256_check CHECK (
            policy_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    policy_version text COLLATE "C" NOT NULL
        CONSTRAINT authorization_policies_version_key UNIQUE
        CONSTRAINT authorization_policies_version_check CHECK (
            char_length(policy_version) BETWEEN 1 AND 128
        ),
    descriptor text COLLATE "C" NOT NULL
        CONSTRAINT authorization_policies_descriptor_check CHECK (
            octet_length(descriptor) BETWEEN 2 AND 1048576
        )
);

CREATE TABLE trace_backed_memory_v3_authorization.authorization_decisions (
    authorization_event_id text COLLATE "C"
        CONSTRAINT authorization_decisions_pkey PRIMARY KEY
        CONSTRAINT authorization_decisions_event_id_check CHECK (
            authorization_event_id ~ '^authz_sha256_[0-9a-f]{64}$'
        ),
    request_id text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_request_key UNIQUE
        CONSTRAINT authorization_decisions_request_id_check CHECK (
            char_length(request_id) BETWEEN 1 AND 128
        ),
    request_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_request_sha256_check CHECK (
            request_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    policy_sha256 text COLLATE "C" NOT NULL,
    principal_id text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_principal_id_check CHECK (
            char_length(principal_id) BETWEEN 1 AND 128
        ),
    agent_client_id text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_agent_client_id_check CHECK (
            char_length(agent_client_id) BETWEEN 1 AND 128
        ),
    tenant_id text COLLATE "C"
        CONSTRAINT authorization_decisions_tenant_id_check CHECK (
            tenant_id IS NULL
            OR char_length(tenant_id) BETWEEN 1 AND 128
        ),
    repository_id text COLLATE "C"
        CONSTRAINT authorization_decisions_repository_id_check CHECK (
            repository_id IS NULL
            OR char_length(repository_id) BETWEEN 1 AND 128
        ),
    permission text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_permission_check CHECK (
            char_length(permission) BETWEEN 1 AND 128
        ),
    allowed boolean NOT NULL,
    reason text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_reason_check CHECK (
            char_length(reason) BETWEEN 1 AND 128
        ),
    decided_at text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_decided_at_check CHECK (
            char_length(decided_at) BETWEEN 1 AND 64
        ),
    descriptor text COLLATE "C" NOT NULL
        CONSTRAINT authorization_decisions_descriptor_check CHECK (
            octet_length(descriptor) BETWEEN 2 AND 1048576
        ),
    CONSTRAINT authorization_decisions_policy_fkey
        FOREIGN KEY (policy_sha256)
        REFERENCES trace_backed_memory_v3_authorization.authorization_policies (
            policy_sha256
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE INDEX authorization_decisions_policy
ON trace_backed_memory_v3_authorization.authorization_decisions (
    policy_sha256,
    decided_at,
    authorization_event_id
);

CREATE INDEX authorization_decisions_principal
ON trace_backed_memory_v3_authorization.authorization_decisions (
    principal_id,
    decided_at,
    authorization_event_id
);

CREATE FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL authorization v3 records are immutable';
END
$$;

REVOKE ALL ON FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change()
FROM PUBLIC;

CREATE TRIGGER authorization_schema_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_authorization.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change();

CREATE TRIGGER authorization_schema_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_authorization.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change();

CREATE TRIGGER authorization_policies_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_authorization.authorization_policies
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change();

CREATE TRIGGER authorization_policies_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_authorization.authorization_policies
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change();

CREATE TRIGGER authorization_decisions_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_authorization.authorization_decisions
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change();

CREATE TRIGGER authorization_decisions_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_authorization.authorization_decisions
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_authorization.reject_immutable_change();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_authorization.schema_metadata
FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_authorization.authorization_policies
FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_authorization.authorization_decisions
FROM PUBLIC;

COMMIT;
