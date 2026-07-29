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

    IF active_version IS DISTINCT FROM 2 THEN
        RAISE EXCEPTION
            'PostgreSQL Artifact Authority requires active schema version 2';
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS trace_backed_memory_v3_artifacts;

REVOKE ALL ON SCHEMA trace_backed_memory_v3_artifacts FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_artifacts.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.artifact-authority.v3')
);

INSERT INTO trace_backed_memory_v3_artifacts.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (
    true,
    1,
    'tbm.artifact-authority.v3'
);

CREATE TABLE trace_backed_memory_v3_artifacts.encrypted_artifacts (
    artifact_id text COLLATE "C" PRIMARY KEY
        CONSTRAINT encrypted_artifacts_artifact_id_check CHECK (
            artifact_id ~ '^artifact_sha256_[0-9a-f]{64}$'
        ),
    content_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_content_sha256_key UNIQUE
        CONSTRAINT encrypted_artifacts_content_sha256_check CHECK (
            content_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    size_bytes bigint NOT NULL
        CONSTRAINT encrypted_artifacts_size_bytes_check CHECK (
            size_bytes BETWEEN 0 AND 67108864
        ),
    media_type text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_media_type_check CHECK (
            length(media_type) BETWEEN 1 AND 512
        ),
    classification text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_classification_check CHECK (
            classification IN (
                'public',
                'internal',
                'confidential',
                'restricted'
            )
        ),
    created_at text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_created_at_check CHECK (
            length(created_at) BETWEEN 1 AND 64
        ),
    redaction_policy_id text COLLATE "C"
        CONSTRAINT encrypted_artifacts_redaction_policy_id_check CHECK (
            redaction_policy_id IS NULL
            OR length(redaction_policy_id) BETWEEN 1 AND 128
        ),
    tenant_id text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_tenant_id_check CHECK (
            length(tenant_id) BETWEEN 1 AND 128
        ),
    repository_id text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_repository_id_check CHECK (
            length(repository_id) BETWEEN 1 AND 128
        ),
    environment_id text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_environment_id_check CHECK (
            length(environment_id) BETWEEN 1 AND 128
        ),
    write_authorization_event_id text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_write_authorization_event_id_check
        CHECK (
            write_authorization_event_id
                ~ '^authz_sha256_[0-9a-f]{64}$'
        ),
    encryption_provider_id text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_encryption_provider_id_check CHECK (
            length(encryption_provider_id) BETWEEN 1 AND 128
        ),
    encryption_algorithm text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_encryption_algorithm_check CHECK (
            length(encryption_algorithm) BETWEEN 1 AND 128
        ),
    artifact_encryption_key_id text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_artifact_key_id_check CHECK (
            length(artifact_encryption_key_id) BETWEEN 1 AND 128
        ),
    encryption_key_id text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_envelope_key_id_check CHECK (
            length(encryption_key_id) BETWEEN 1 AND 128
        ),
    nonce bytea NOT NULL
        CONSTRAINT encrypted_artifacts_nonce_size_check CHECK (
            octet_length(nonce) BETWEEN 1 AND 1024
        ),
    ciphertext bytea NOT NULL
        CONSTRAINT encrypted_artifacts_ciphertext_size_check CHECK (
            octet_length(ciphertext) BETWEEN 1 AND 67174400
        ),
    ciphertext_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_ciphertext_sha256_check CHECK (
            ciphertext_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    retain_until text COLLATE "C"
        CONSTRAINT encrypted_artifacts_retain_until_check CHECK (
            retain_until IS NULL
            OR length(retain_until) BETWEEN 1 AND 64
        ),
    legal_hold boolean NOT NULL,
    stored_at text COLLATE "C" NOT NULL
        CONSTRAINT encrypted_artifacts_stored_at_check CHECK (
            length(stored_at) BETWEEN 1 AND 64
        ),
    CONSTRAINT encrypted_artifacts_artifact_content_check CHECK (
        artifact_id =
            'artifact_sha256_' || substr(content_sha256, 8)
    ),
    CONSTRAINT encrypted_artifacts_encryption_key_match CHECK (
        artifact_encryption_key_id = encryption_key_id
    )
);

CREATE INDEX encrypted_artifacts_scope
ON trace_backed_memory_v3_artifacts.encrypted_artifacts (
    tenant_id,
    repository_id,
    environment_id,
    artifact_id
);

CREATE FUNCTION
    trace_backed_memory_v3_artifacts.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'PostgreSQL Artifact Authority records are immutable';
END
$$;

CREATE FUNCTION
    trace_backed_memory_v3_artifacts.verify_encrypted_artifact()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    actual_ciphertext_sha256 text;
BEGIN
    actual_ciphertext_sha256 :=
        'sha256:' ||
        pg_catalog.encode(pg_catalog.sha256(NEW.ciphertext), 'hex');
    IF NEW.ciphertext_sha256 IS DISTINCT FROM
       actual_ciphertext_sha256 THEN
        RAISE EXCEPTION
            'PostgreSQL Artifact Authority ciphertext digest mismatch';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER artifact_schema_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_artifacts.schema_metadata
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_artifacts.reject_immutable_change();

CREATE TRIGGER artifact_schema_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_artifacts.schema_metadata
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_artifacts.reject_immutable_change();

CREATE TRIGGER encrypted_artifacts_verify
BEFORE INSERT
ON trace_backed_memory_v3_artifacts.encrypted_artifacts
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_artifacts.verify_encrypted_artifact();

CREATE TRIGGER encrypted_artifacts_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_artifacts.encrypted_artifacts
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_artifacts.reject_immutable_change();

CREATE TRIGGER encrypted_artifacts_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_artifacts.encrypted_artifacts
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_artifacts.reject_immutable_change();

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_artifacts.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_artifacts.verify_encrypted_artifact()
    FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_artifacts.schema_metadata,
   trace_backed_memory_v3_artifacts.encrypted_artifacts
FROM PUBLIC;

COMMIT;
