PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

CREATE TABLE IF NOT EXISTS trace_backed_memory_v3_managed_index_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK (contract_version = 'tbm.managed-index-bundle.v3')
);

INSERT OR IGNORE INTO trace_backed_memory_v3_managed_index_schema (
    singleton,
    schema_version,
    contract_version
) VALUES (
    1,
    1,
    'tbm.managed-index-bundle.v3'
);

CREATE TABLE IF NOT EXISTS v3_managed_index_bundles (
    bundle_id TEXT PRIMARY KEY
        CHECK (
            length(bundle_id) = 92
            AND substr(bundle_id, 1, 28) =
                'managed_index_bundle_sha256_'
            AND substr(bundle_id, 29) NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL
        CHECK (
            length(tenant_id) BETWEEN 1 AND 128
            AND length(trim(tenant_id)) >= 1
        ),
    repository_id TEXT NOT NULL
        CHECK (
            length(repository_id) BETWEEN 1 AND 128
            AND length(trim(repository_id)) >= 1
        ),
    environment_id TEXT NOT NULL
        CHECK (
            length(environment_id) BETWEEN 1 AND 128
            AND length(trim(environment_id)) >= 1
        ),
    retriever_id TEXT NOT NULL
        CHECK (
            length(retriever_id) BETWEEN 1 AND 128
            AND length(trim(retriever_id)) >= 1
        ),
    retriever_version TEXT NOT NULL
        CHECK (
            length(retriever_version) BETWEEN 1 AND 128
            AND length(trim(retriever_version)) >= 1
        ),
    source_catalog_sha256 TEXT NOT NULL
        CHECK (
            length(source_catalog_sha256) = 71
            AND substr(source_catalog_sha256, 1, 7) = 'sha256:'
            AND substr(source_catalog_sha256, 8) NOT GLOB '*[^0-9a-f]*'
        ),
    payload_utf8 BLOB NOT NULL
        CHECK (length(payload_utf8) BETWEEN 2 AND 67108864),
    appended_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (tenant_id, repository_id, environment_id, bundle_id)
);

CREATE TABLE IF NOT EXISTS v3_managed_index_heads (
    tenant_id TEXT NOT NULL
        CHECK (
            length(tenant_id) BETWEEN 1 AND 128
            AND length(trim(tenant_id)) >= 1
        ),
    repository_id TEXT NOT NULL
        CHECK (
            length(repository_id) BETWEEN 1 AND 128
            AND length(trim(repository_id)) >= 1
        ),
    environment_id TEXT NOT NULL
        CHECK (
            length(environment_id) BETWEEN 1 AND 128
            AND length(trim(environment_id)) >= 1
        ),
    bundle_id TEXT NOT NULL,
    head_version INTEGER NOT NULL CHECK (head_version >= 1),
    PRIMARY KEY (tenant_id, repository_id, environment_id),
    FOREIGN KEY (
        tenant_id,
        repository_id,
        environment_id,
        bundle_id
    ) REFERENCES v3_managed_index_bundles (
        tenant_id,
        repository_id,
        environment_id,
        bundle_id
    )
);

CREATE INDEX IF NOT EXISTS v3_managed_index_bundles_scope
    ON v3_managed_index_bundles (
        tenant_id,
        repository_id,
        environment_id,
        bundle_id
    );

CREATE TRIGGER IF NOT EXISTS v3_managed_index_schema_no_update
BEFORE UPDATE ON trace_backed_memory_v3_managed_index_schema
BEGIN
    SELECT RAISE(ABORT, 'managed index schema metadata is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_managed_index_schema_no_delete
BEFORE DELETE ON trace_backed_memory_v3_managed_index_schema
BEGIN
    SELECT RAISE(ABORT, 'managed index schema metadata is immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_managed_index_bundles_no_update
BEFORE UPDATE ON v3_managed_index_bundles
BEGIN
    SELECT RAISE(ABORT, 'managed index bundles are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_managed_index_bundles_no_delete
BEFORE DELETE ON v3_managed_index_bundles
BEGIN
    SELECT RAISE(ABORT, 'managed index bundles are immutable');
END;

CREATE TRIGGER IF NOT EXISTS v3_managed_index_heads_no_delete
BEFORE DELETE ON v3_managed_index_heads
BEGIN
    SELECT RAISE(ABORT, 'managed index heads are append-only');
END;

CREATE TRIGGER IF NOT EXISTS v3_managed_index_heads_cas
BEFORE UPDATE ON v3_managed_index_heads
WHEN
    NEW.tenant_id <> OLD.tenant_id
    OR NEW.repository_id <> OLD.repository_id
    OR NEW.environment_id <> OLD.environment_id
    OR NEW.head_version <> OLD.head_version + 1
    OR NEW.bundle_id = OLD.bundle_id
BEGIN
    SELECT RAISE(ABORT, 'managed index head update must be one CAS advance');
END;
