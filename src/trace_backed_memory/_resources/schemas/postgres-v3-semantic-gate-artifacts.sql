BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    semantic_version integer;
    semantic_contract text;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    SELECT schema_version, contract_version
    INTO semantic_version, semantic_contract
    FROM trace_backed_memory_v3_semantic_gate.schema_metadata
    WHERE singleton = 1
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifacts require active schema version 2';
    END IF;
    IF semantic_version IS NULL
       OR semantic_version <> 1
       OR semantic_contract <> 'tbm.semantic-gate-attempt.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifacts require semantic Gate v3';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_semantic_gate_artifacts;
REVOKE ALL ON SCHEMA
    trace_backed_memory_v3_semantic_gate_artifacts FROM PUBLIC;

CREATE TABLE
trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata (
    singleton boolean
        CONSTRAINT semantic_gate_artifact_schema_metadata_pkey PRIMARY KEY
        DEFAULT true
        CONSTRAINT semantic_gate_artifact_schema_metadata_singleton_check
        CHECK (singleton),
    schema_version integer NOT NULL
        CONSTRAINT semantic_gate_artifact_schema_version_check
        CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CONSTRAINT semantic_gate_artifact_contract_version_check
        CHECK (contract_version = 'tbm.semantic-gate-artifact.v3')
);

INSERT INTO
trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (true, 1, 'tbm.semantic-gate-artifact.v3');

CREATE TABLE
trace_backed_memory_v3_semantic_gate_artifacts.semantic_gate_artifacts (
    artifact_id text COLLATE "C"
        CONSTRAINT semantic_gate_artifacts_pkey PRIMARY KEY
        CONSTRAINT semantic_gate_artifacts_artifact_id_check CHECK (
            artifact_id ~ '^artifact_sha256_[0-9a-f]{64}$'
        )
        CONSTRAINT semantic_gate_artifacts_derived_id_check CHECK (
            artifact_id =
                'artifact_sha256_' ||
                pg_catalog.substr(content_sha256, 8)
        ),
    content_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT semantic_gate_artifacts_content_sha256_key UNIQUE
        CONSTRAINT semantic_gate_artifacts_content_sha256_check CHECK (
            content_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    size_bytes integer NOT NULL
        CONSTRAINT semantic_gate_artifacts_size_bytes_check CHECK (
            size_bytes BETWEEN 1 AND 128000
        ),
    media_type text COLLATE "C" NOT NULL
        CONSTRAINT semantic_gate_artifacts_media_type_check CHECK (
            pg_catalog.char_length(media_type) BETWEEN 1 AND 512
        ),
    classification text COLLATE "C" NOT NULL
        CONSTRAINT semantic_gate_artifacts_classification_check CHECK (
            classification IN ('public', 'internal')
        ),
    created_at text COLLATE "C" NOT NULL
        CONSTRAINT semantic_gate_artifacts_created_at_check CHECK (
            pg_catalog.char_length(created_at) BETWEEN 1 AND 64
        ),
    encryption_key_id text COLLATE "C"
        CONSTRAINT semantic_gate_artifacts_encryption_key_id_check CHECK (
            encryption_key_id IS NULL
        ),
    redaction_policy_id text COLLATE "C"
        CONSTRAINT semantic_gate_artifacts_redaction_policy_id_check CHECK (
            redaction_policy_id IS NULL
            OR pg_catalog.char_length(redaction_policy_id) BETWEEN 1 AND 128
        ),
    content bytea NOT NULL
        CONSTRAINT semantic_gate_artifacts_content_check CHECK (
            pg_catalog.octet_length(content) = size_bytes
            AND pg_catalog.octet_length(content) <= 128000
        ),
    CONSTRAINT semantic_gate_artifacts_identity_key
        UNIQUE (artifact_id, content_sha256)
);

CREATE TABLE
trace_backed_memory_v3_semantic_gate_artifacts.semantic_gate_artifact_bindings (
    attempt_id text COLLATE "C" NOT NULL,
    artifact_role text COLLATE "C" NOT NULL
        CONSTRAINT semantic_gate_artifact_bindings_role_check CHECK (
            artifact_role IN ('prompt', 'response')
        ),
    artifact_id text COLLATE "C" NOT NULL,
    content_sha256 text COLLATE "C" NOT NULL,
    descriptor text COLLATE "C" NOT NULL
        CONSTRAINT semantic_gate_artifact_bindings_descriptor_check CHECK (
            pg_catalog.octet_length(descriptor) BETWEEN 1 AND 1048576
        ),
    CONSTRAINT semantic_gate_artifact_bindings_pkey
        PRIMARY KEY (attempt_id, artifact_role),
    CONSTRAINT semantic_gate_artifact_bindings_artifact_key
        UNIQUE (artifact_id, content_sha256, attempt_id, artifact_role),
    CONSTRAINT semantic_gate_artifact_bindings_attempt_fkey
        FOREIGN KEY (attempt_id)
        REFERENCES trace_backed_memory_v3_semantic_gate.
            v3_semantic_gate_attempts (attempt_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT semantic_gate_artifact_bindings_artifact_fkey
        FOREIGN KEY (artifact_id, content_sha256)
        REFERENCES trace_backed_memory_v3_semantic_gate_artifacts.
            semantic_gate_artifacts (artifact_id, content_sha256)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE INDEX semantic_gate_artifact_bindings_artifact
ON trace_backed_memory_v3_semantic_gate_artifacts.
    semantic_gate_artifact_bindings (artifact_id, content_sha256);

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'PostgreSQL Semantic Gate artifact records are immutable';
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.verify_artifact_content()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    actual_sha256 text;
    parsed_created_at timestamptz;
    canonical_created_at text;
    fractional_microseconds bigint;
BEGIN
    actual_sha256 :=
        'sha256:' ||
        pg_catalog.encode(pg_catalog.sha256(NEW.content), 'hex');
    IF NEW.content_sha256 IS DISTINCT FROM actual_sha256
       OR NEW.artifact_id IS DISTINCT FROM
            'artifact_sha256_' || pg_catalog.substr(actual_sha256, 8) THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact bytes do not match identity';
    END IF;
    BEGIN
        parsed_created_at := NEW.created_at::timestamptz;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact created_at is invalid';
    END;
    fractional_microseconds :=
        pg_catalog.mod(
            pg_catalog.date_part(
                'microseconds',
                parsed_created_at
            )::bigint,
            1000000
        );
    canonical_created_at :=
        pg_catalog.to_char(
            parsed_created_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS'
        ) ||
        CASE
            WHEN fractional_microseconds = 0 THEN ''
            ELSE '.' ||
                pg_catalog.lpad(
                    fractional_microseconds::text,
                    6,
                    '0'
                )
        END ||
        'Z';
    IF NEW.created_at IS DISTINCT FROM canonical_created_at THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact created_at is not canonical';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.verify_artifact_binding()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    attempt_status text;
    prompt_digest text;
    response_digest text;
    artifact_size integer;
    artifact_media_type text;
    artifact_classification text;
    artifact_created_at text;
    artifact_encryption_key_id text;
    artifact_redaction_policy_id text;
    canonical_descriptor text;
    parsed jsonb;
BEGIN
    SELECT status,
           descriptor::jsonb ->> 'prompt_artifact_sha256',
           descriptor::jsonb ->> 'response_artifact_sha256'
    INTO attempt_status, prompt_digest, response_digest
    FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
    WHERE attempt_id = NEW.attempt_id;

    SELECT size_bytes, media_type, classification, created_at,
           encryption_key_id, redaction_policy_id
    INTO artifact_size, artifact_media_type, artifact_classification,
         artifact_created_at, artifact_encryption_key_id,
         artifact_redaction_policy_id
    FROM trace_backed_memory_v3_semantic_gate_artifacts.
        semantic_gate_artifacts
    WHERE artifact_id = NEW.artifact_id
      AND content_sha256 = NEW.content_sha256;

    IF attempt_status IS NULL OR artifact_size IS NULL THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact binding target is missing';
    END IF;
    IF NEW.artifact_role = 'prompt' AND (
        prompt_digest IS DISTINCT FROM NEW.content_sha256
        OR artifact_media_type IS DISTINCT FROM
            'text/plain; charset=utf-8'
        OR artifact_size > 128000
    ) THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate prompt artifact does not match attempt';
    END IF;
    IF NEW.artifact_role = 'response' AND (
        attempt_status IS DISTINCT FROM 'succeeded'
        OR response_digest IS DISTINCT FROM NEW.content_sha256
        OR artifact_size > 65536
    ) THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate response artifact does not match attempt';
    END IF;

    BEGIN
        parsed := NEW.descriptor::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact descriptor is invalid';
    END;
    canonical_descriptor :=
        '{"artifact":{"artifact_id":' ||
        pg_catalog.to_json(NEW.artifact_id)::text ||
        ',"classification":' ||
        pg_catalog.to_json(artifact_classification)::text ||
        ',"content_sha256":' ||
        pg_catalog.to_json(NEW.content_sha256)::text ||
        ',"created_at":' ||
        pg_catalog.to_json(artifact_created_at)::text ||
        ',"encryption_key_id":' ||
        COALESCE(
            pg_catalog.to_json(artifact_encryption_key_id)::text,
            'null'
        ) ||
        ',"media_type":' ||
        pg_catalog.to_json(artifact_media_type)::text ||
        ',"redaction_policy_id":' ||
        COALESCE(
            pg_catalog.to_json(artifact_redaction_policy_id)::text,
            'null'
        ) ||
        ',"size_bytes":' || artifact_size::text ||
        '},"artifact_kind":"semantic_gate","artifact_role":' ||
        pg_catalog.to_json(NEW.artifact_role)::text ||
        ',"attempt_id":' || pg_catalog.to_json(NEW.attempt_id)::text ||
        ',"contract_version":"tbm.semantic-gate-artifact.v3"}';
    IF NEW.descriptor IS DISTINCT FROM canonical_descriptor THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact descriptor is not canonical';
    END IF;
    IF pg_catalog.jsonb_typeof(parsed) IS DISTINCT FROM 'object'
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(parsed)
       ) <> 5
       OR parsed ->> 'contract_version' IS DISTINCT FROM
            'tbm.semantic-gate-artifact.v3'
       OR parsed ->> 'artifact_kind' IS DISTINCT FROM 'semantic_gate'
       OR parsed ->> 'attempt_id' IS DISTINCT FROM NEW.attempt_id
       OR parsed ->> 'artifact_role' IS DISTINCT FROM NEW.artifact_role
       OR pg_catalog.jsonb_typeof(parsed -> 'artifact')
            IS DISTINCT FROM 'object'
       OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(parsed -> 'artifact')
       ) <> 8
       OR parsed -> 'artifact' ->> 'artifact_id'
            IS DISTINCT FROM NEW.artifact_id
       OR parsed -> 'artifact' ->> 'content_sha256'
            IS DISTINCT FROM NEW.content_sha256
       OR parsed -> 'artifact' ->> 'size_bytes' IS DISTINCT FROM
            artifact_size::text
       OR parsed -> 'artifact' ->> 'media_type'
            IS DISTINCT FROM artifact_media_type
       OR parsed -> 'artifact' ->> 'classification' IS DISTINCT FROM
            artifact_classification
       OR parsed -> 'artifact' ->> 'created_at'
            IS DISTINCT FROM artifact_created_at
       OR (
            artifact_encryption_key_id IS NULL
            AND parsed -> 'artifact' -> 'encryption_key_id'
                IS DISTINCT FROM 'null'::jsonb
       )
       OR (
            artifact_encryption_key_id IS NOT NULL
            AND parsed -> 'artifact' ->> 'encryption_key_id'
                IS DISTINCT FROM
                artifact_encryption_key_id
       )
       OR (
            artifact_redaction_policy_id IS NULL
            AND parsed -> 'artifact' -> 'redaction_policy_id'
                IS DISTINCT FROM 'null'::jsonb
       )
       OR (
            artifact_redaction_policy_id IS NOT NULL
            AND parsed -> 'artifact' ->> 'redaction_policy_id'
                IS DISTINCT FROM
                artifact_redaction_policy_id
       ) THEN
        RAISE EXCEPTION
            'PostgreSQL Semantic Gate artifact descriptor does not match';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER semantic_gate_artifact_schema_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.reject_immutable_change();
CREATE TRIGGER semantic_gate_artifact_schema_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.reject_immutable_change();

CREATE TRIGGER semantic_gate_artifacts_verify_content
BEFORE INSERT
ON trace_backed_memory_v3_semantic_gate_artifacts.semantic_gate_artifacts
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.verify_artifact_content();
CREATE TRIGGER semantic_gate_artifacts_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_semantic_gate_artifacts.semantic_gate_artifacts
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.reject_immutable_change();
CREATE TRIGGER semantic_gate_artifacts_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_semantic_gate_artifacts.semantic_gate_artifacts
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.reject_immutable_change();

CREATE TRIGGER semantic_gate_artifact_bindings_verify
BEFORE INSERT
ON trace_backed_memory_v3_semantic_gate_artifacts.
    semantic_gate_artifact_bindings
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.verify_artifact_binding();
CREATE TRIGGER semantic_gate_artifact_bindings_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_semantic_gate_artifacts.
    semantic_gate_artifact_bindings
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.reject_immutable_change();
CREATE TRIGGER semantic_gate_artifact_bindings_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_semantic_gate_artifacts.
    semantic_gate_artifact_bindings
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_semantic_gate_artifacts.reject_immutable_change();

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate_artifacts.
        reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate_artifacts.
        verify_artifact_content()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate_artifacts.
        verify_artifact_binding()
    FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_semantic_gate_artifacts.schema_metadata,
       trace_backed_memory_v3_semantic_gate_artifacts.
           semantic_gate_artifacts,
       trace_backed_memory_v3_semantic_gate_artifacts.
           semantic_gate_artifact_bindings
    FROM PUBLIC;

COMMIT;
