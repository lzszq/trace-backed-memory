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
            'PostgreSQL replay v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL replay v3 requires active schema version 2, found %',
            active_version;
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_replay;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_replay FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_replay.schema_metadata (
    singleton boolean
        CONSTRAINT replay_schema_metadata_pkey PRIMARY KEY
        DEFAULT true
        CONSTRAINT replay_schema_metadata_singleton_check CHECK (singleton),
    schema_version integer NOT NULL
        CONSTRAINT replay_schema_metadata_schema_version_check
        CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CONSTRAINT replay_schema_metadata_contract_version_check
        CHECK (contract_version = 'tbm.replay.v3')
);

INSERT INTO trace_backed_memory_v3_replay.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (true, 1, 'tbm.replay.v3');

CREATE TABLE trace_backed_memory_v3_replay.replay_artifacts (
    artifact_id text COLLATE "C"
        CONSTRAINT replay_artifacts_pkey PRIMARY KEY
        CONSTRAINT replay_artifacts_artifact_id_check CHECK (
            artifact_id ~ '^artifact_sha256_[0-9a-f]{64}$'
        )
        CONSTRAINT replay_artifacts_derived_id_check CHECK (
            artifact_id =
                'artifact_sha256_' ||
                pg_catalog.substr(content_sha256, 8)
        ),
    content_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT replay_artifacts_content_sha256_key UNIQUE
        CONSTRAINT replay_artifacts_content_sha256_check CHECK (
            content_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    size_bytes integer NOT NULL
        CONSTRAINT replay_artifacts_size_bytes_check CHECK (
            size_bytes BETWEEN 0 AND 67108864
        ),
    media_type text COLLATE "C" NOT NULL
        CONSTRAINT replay_artifacts_media_type_check CHECK (
            char_length(media_type) BETWEEN 1 AND 512
        ),
    classification text COLLATE "C" NOT NULL
        CONSTRAINT replay_artifacts_classification_check CHECK (
            classification IN ('public', 'internal')
        ),
    created_at text COLLATE "C" NOT NULL
        CONSTRAINT replay_artifacts_created_at_check CHECK (
            char_length(created_at) BETWEEN 1 AND 64
        ),
    encryption_key_id text COLLATE "C"
        CONSTRAINT replay_artifacts_encryption_key_id_check CHECK (
            encryption_key_id IS NULL
        ),
    redaction_policy_id text COLLATE "C"
        CONSTRAINT replay_artifacts_redaction_policy_id_check CHECK (
            redaction_policy_id IS NULL
            OR char_length(redaction_policy_id) BETWEEN 1 AND 128
        ),
    content bytea NOT NULL
        CONSTRAINT replay_artifacts_content_check CHECK (
            octet_length(content) = size_bytes
            AND octet_length(content) <= 67108864
        )
);

CREATE TABLE trace_backed_memory_v3_replay.replay_injections (
    artifact_id text COLLATE "C"
        CONSTRAINT replay_injections_pkey PRIMARY KEY,
    session_id text COLLATE "C" NOT NULL
        CONSTRAINT replay_injections_session_id_check CHECK (
            char_length(session_id) BETWEEN 1 AND 128
        ),
    decision_id text COLLATE "C" NOT NULL
        CONSTRAINT replay_injections_decision_id_check CHECK (
            char_length(decision_id) BETWEEN 1 AND 128
        ),
    usage_decision_id text COLLATE "C" NOT NULL
        CONSTRAINT replay_injections_usage_decision_id_check CHECK (
            char_length(usage_decision_id) BETWEEN 1 AND 128
        ),
    descriptor text COLLATE "C" NOT NULL
        CONSTRAINT replay_injections_descriptor_check CHECK (
            octet_length(descriptor) BETWEEN 1 AND 1048576
        ),
    CONSTRAINT replay_injections_linkage_key UNIQUE (
        artifact_id,
        session_id,
        decision_id,
        usage_decision_id
    ),
    CONSTRAINT replay_injections_artifact_fkey
        FOREIGN KEY (artifact_id)
        REFERENCES trace_backed_memory_v3_replay.replay_artifacts (
            artifact_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE INDEX replay_injections_decision
ON trace_backed_memory_v3_replay.replay_injections (
    session_id,
    decision_id,
    usage_decision_id
);

CREATE TABLE trace_backed_memory_v3_replay.replay_manifests (
    manifest_sha256 text COLLATE "C"
        CONSTRAINT replay_manifests_pkey PRIMARY KEY
        CONSTRAINT replay_manifests_manifest_sha256_check CHECK (
            manifest_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    session_id text COLLATE "C" NOT NULL
        CONSTRAINT replay_manifests_session_id_check CHECK (
            char_length(session_id) BETWEEN 1 AND 128
        ),
    decision_id text COLLATE "C" NOT NULL
        CONSTRAINT replay_manifests_decision_id_check CHECK (
            char_length(decision_id) BETWEEN 1 AND 128
        ),
    usage_decision_id text COLLATE "C" NOT NULL
        CONSTRAINT replay_manifests_usage_decision_id_check CHECK (
            char_length(usage_decision_id) BETWEEN 1 AND 128
        ),
    injection_artifact_id text COLLATE "C",
    completeness text COLLATE "C" NOT NULL
        CONSTRAINT replay_manifests_completeness_check CHECK (
            completeness IN ('complete', 'legacy_partial')
        ),
    descriptor text COLLATE "C" NOT NULL
        CONSTRAINT replay_manifests_descriptor_check CHECK (
            octet_length(descriptor) BETWEEN 1 AND 1048576
        ),
    CONSTRAINT replay_manifests_injection_fkey
        FOREIGN KEY (
            injection_artifact_id,
            session_id,
            decision_id,
            usage_decision_id
        )
        REFERENCES trace_backed_memory_v3_replay.replay_injections (
            artifact_id,
            session_id,
            decision_id,
            usage_decision_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT replay_manifests_injection_shape CHECK (
        (completeness = 'complete' AND injection_artifact_id IS NOT NULL)
        OR
        (completeness = 'legacy_partial')
    )
);

CREATE INDEX replay_manifests_decision
ON trace_backed_memory_v3_replay.replay_manifests (
    session_id,
    decision_id,
    usage_decision_id
);

CREATE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL replay v3 records are immutable';
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_replay.validate_injection_artifact()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    artifact_size integer;
    artifact_media_type text;
BEGIN
    SELECT size_bytes, media_type
    INTO artifact_size, artifact_media_type
    FROM trace_backed_memory_v3_replay.replay_artifacts
    WHERE artifact_id = NEW.artifact_id;

    IF artifact_size IS NOT NULL
       AND (
           artifact_size > 1048576
           OR artifact_media_type <> 'text/plain; charset=utf-8'
       ) THEN
        RAISE EXCEPTION
            'PostgreSQL replay injection artifact shape is invalid';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER replay_schema_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_replay.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

CREATE TRIGGER replay_schema_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_replay.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

CREATE TRIGGER replay_artifacts_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_replay.replay_artifacts
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

CREATE TRIGGER replay_artifacts_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_replay.replay_artifacts
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

CREATE TRIGGER replay_injections_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_replay.replay_injections
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

CREATE TRIGGER replay_injections_validate_artifact
BEFORE INSERT
ON trace_backed_memory_v3_replay.replay_injections
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_replay.validate_injection_artifact();

CREATE TRIGGER replay_injections_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_replay.replay_injections
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

CREATE TRIGGER replay_manifests_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_replay.replay_manifests
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

CREATE TRIGGER replay_manifests_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_replay.replay_manifests
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_replay.reject_immutable_change();

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_replay.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_replay.validate_injection_artifact()
    FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_replay.schema_metadata
    FROM PUBLIC;

COMMIT;
