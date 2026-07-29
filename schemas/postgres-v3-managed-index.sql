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
            'PostgreSQL managed index v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL managed index v3 requires active schema version 2, found %',
            active_version;
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_managed_index;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_managed_index FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_managed_index.schema_metadata (
    singleton integer
        CONSTRAINT managed_index_schema_metadata_pkey PRIMARY KEY
        CONSTRAINT managed_index_schema_singleton_check CHECK (singleton = 1),
    schema_version integer NOT NULL
        CONSTRAINT managed_index_schema_version_check
        CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_schema_contract_check
        CHECK (contract_version = 'tbm.managed-index-bundle.v3')
);

INSERT INTO trace_backed_memory_v3_managed_index.schema_metadata (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.managed-index-bundle.v3');

CREATE TABLE trace_backed_memory_v3_managed_index.v3_managed_index_bundles (
    bundle_id text COLLATE "C"
        CONSTRAINT managed_index_bundles_pkey PRIMARY KEY
        CONSTRAINT managed_index_bundle_id_check CHECK (
            bundle_id ~ '^managed_index_bundle_sha256_[0-9a-f]{64}$'
        ),
    tenant_id text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_bundle_tenant_check
        CHECK (
            char_length(tenant_id) BETWEEN 1 AND 128
            AND btrim(tenant_id) <> ''
        ),
    repository_id text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_bundle_repository_check
        CHECK (
            char_length(repository_id) BETWEEN 1 AND 128
            AND btrim(repository_id) <> ''
        ),
    environment_id text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_bundle_environment_check
        CHECK (
            char_length(environment_id) BETWEEN 1 AND 128
            AND btrim(environment_id) <> ''
        ),
    retriever_id text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_bundle_retriever_check
        CHECK (
            char_length(retriever_id) BETWEEN 1 AND 128
            AND btrim(retriever_id) <> ''
        ),
    retriever_version text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_bundle_retriever_version_check
        CHECK (
            char_length(retriever_version) BETWEEN 1 AND 128
            AND btrim(retriever_version) <> ''
        ),
    source_catalog_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_bundle_catalog_digest_check
        CHECK (source_catalog_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    payload_utf8 bytea NOT NULL
        CONSTRAINT managed_index_bundle_payload_check
        CHECK (octet_length(payload_utf8) BETWEEN 2 AND 67108864),
    appended_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CONSTRAINT managed_index_bundles_scope_key UNIQUE (
        tenant_id,
        repository_id,
        environment_id,
        bundle_id
    )
);

CREATE TABLE trace_backed_memory_v3_managed_index.v3_managed_index_heads (
    tenant_id text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_head_tenant_check
        CHECK (
            char_length(tenant_id) BETWEEN 1 AND 128
            AND btrim(tenant_id) <> ''
        ),
    repository_id text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_head_repository_check
        CHECK (
            char_length(repository_id) BETWEEN 1 AND 128
            AND btrim(repository_id) <> ''
        ),
    environment_id text COLLATE "C" NOT NULL
        CONSTRAINT managed_index_head_environment_check
        CHECK (
            char_length(environment_id) BETWEEN 1 AND 128
            AND btrim(environment_id) <> ''
        ),
    bundle_id text COLLATE "C" NOT NULL,
    head_version bigint NOT NULL
        CONSTRAINT managed_index_head_version_check CHECK (head_version >= 1),
    CONSTRAINT managed_index_heads_pkey PRIMARY KEY (
        tenant_id,
        repository_id,
        environment_id
    ),
    CONSTRAINT managed_index_heads_bundle_fkey FOREIGN KEY (
        tenant_id,
        repository_id,
        environment_id,
        bundle_id
    ) REFERENCES trace_backed_memory_v3_managed_index.v3_managed_index_bundles (
        tenant_id,
        repository_id,
        environment_id,
        bundle_id
    ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE INDEX v3_managed_index_bundles_scope
    ON trace_backed_memory_v3_managed_index.v3_managed_index_bundles (
        tenant_id COLLATE "C",
        repository_id COLLATE "C",
        environment_id COLLATE "C",
        bundle_id COLLATE "C"
    );

CREATE FUNCTION trace_backed_memory_v3_managed_index.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'managed index immutable relation cannot be changed';
END;
$$;

CREATE FUNCTION trace_backed_memory_v3_managed_index.validate_head_advance()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.repository_id IS DISTINCT FROM OLD.repository_id
       OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
       OR NEW.head_version IS DISTINCT FROM OLD.head_version + 1
       OR NEW.bundle_id IS NOT DISTINCT FROM OLD.bundle_id THEN
        RAISE EXCEPTION
            'managed index head update must be one CAS advance';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER managed_index_schema_immutable
BEFORE UPDATE OR DELETE ON
    trace_backed_memory_v3_managed_index.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_managed_index.reject_immutable_change();

CREATE TRIGGER managed_index_schema_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_managed_index.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_managed_index.reject_immutable_change();

CREATE TRIGGER managed_index_bundles_immutable
BEFORE UPDATE OR DELETE ON
    trace_backed_memory_v3_managed_index.v3_managed_index_bundles
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_managed_index.reject_immutable_change();

CREATE TRIGGER managed_index_bundles_no_truncate
BEFORE TRUNCATE ON
    trace_backed_memory_v3_managed_index.v3_managed_index_bundles
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_managed_index.reject_immutable_change();

CREATE TRIGGER managed_index_heads_no_delete
BEFORE DELETE ON trace_backed_memory_v3_managed_index.v3_managed_index_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_managed_index.reject_immutable_change();

CREATE TRIGGER managed_index_heads_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_managed_index.v3_managed_index_heads
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_managed_index.reject_immutable_change();

CREATE TRIGGER managed_index_heads_cas
BEFORE UPDATE ON trace_backed_memory_v3_managed_index.v3_managed_index_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_managed_index.validate_head_advance();

REVOKE ALL ON ALL TABLES IN SCHEMA
    trace_backed_memory_v3_managed_index FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA
    trace_backed_memory_v3_managed_index FROM PUBLIC;

COMMIT;
