BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
BEGIN
    SELECT schema_version INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;
    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL event ledger v1 requires active schema version 2';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_event_ledger;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_event_ledger FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_event_ledger.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL CHECK (
        contract_version = 'tbm.event-ledger-port.v1'
    )
);

INSERT INTO trace_backed_memory_v3_event_ledger.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (TRUE, 1, 'tbm.event-ledger-port.v1');

CREATE TABLE trace_backed_memory_v3_event_ledger.global_head (
    singleton boolean DEFAULT TRUE CHECK (singleton),
    current_global_position bigint NOT NULL CHECK (
        current_global_position >= 0
    ),
    current_event_id text COLLATE "C",
    current_event_sha256 text COLLATE "C",
    CONSTRAINT event_ledger_global_head_pkey PRIMARY KEY (singleton),
    CONSTRAINT event_ledger_global_head_shape CHECK (
        (
            current_global_position = 0
            AND current_event_id IS NULL
            AND current_event_sha256 IS NULL
        )
        OR (
            current_global_position > 0
            AND current_event_id IS NOT NULL
            AND current_event_sha256 IS NOT NULL
            AND current_event_sha256 ~ '^sha256:[0-9a-f]{64}$'
        )
    )
);

INSERT INTO trace_backed_memory_v3_event_ledger.global_head (
    singleton,
    current_global_position,
    current_event_id,
    current_event_sha256
) VALUES (TRUE, 0, NULL, NULL);

CREATE TABLE trace_backed_memory_v3_event_ledger.stream_heads (
    partition_sha256 text COLLATE "C" NOT NULL CHECK (
        partition_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    stream_id text COLLATE "C" NOT NULL CHECK (
        char_length(stream_id) BETWEEN 1 AND 256
    ),
    organization_id text COLLATE "C" NOT NULL CHECK (
        char_length(organization_id) BETWEEN 1 AND 128
    ),
    tenant_id text COLLATE "C" NOT NULL CHECK (
        char_length(tenant_id) BETWEEN 1 AND 128
    ),
    repository_id text COLLATE "C" NOT NULL CHECK (
        char_length(repository_id) BETWEEN 1 AND 128
    ),
    environment_id text COLLATE "C" NOT NULL CHECK (
        char_length(environment_id) BETWEEN 1 AND 128
    ),
    current_stream_version integer NOT NULL CHECK (
        current_stream_version >= 0
    ),
    current_event_id text COLLATE "C",
    current_event_sha256 text COLLATE "C",
    CONSTRAINT event_ledger_stream_heads_pkey PRIMARY KEY (
        partition_sha256,
        stream_id
    ),
    CONSTRAINT event_ledger_stream_head_shape CHECK (
        (
            current_stream_version = 0
            AND current_event_id IS NULL
            AND current_event_sha256 IS NULL
        )
        OR (
            current_stream_version > 0
            AND current_event_id IS NOT NULL
            AND current_event_sha256 ~ '^sha256:[0-9a-f]{64}$'
        )
    )
);

CREATE TABLE trace_backed_memory_v3_event_ledger.events (
    event_id text COLLATE "C" NOT NULL,
    event_sha256 text COLLATE "C" NOT NULL CHECK (
        event_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    partition_sha256 text COLLATE "C" NOT NULL CHECK (
        partition_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    organization_id text COLLATE "C" NOT NULL,
    tenant_id text COLLATE "C" NOT NULL,
    repository_id text COLLATE "C" NOT NULL,
    environment_id text COLLATE "C" NOT NULL,
    stream_id text COLLATE "C" NOT NULL,
    stream_version integer NOT NULL CHECK (stream_version >= 1),
    global_position bigint NOT NULL CHECK (global_position >= 1),
    previous_stream_event_sha256 text COLLATE "C" CHECK (
        previous_stream_event_sha256 IS NULL
        OR previous_stream_event_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    classification text COLLATE "C" NOT NULL CHECK (
        classification IN ('public', 'internal', 'confidential', 'restricted')
    ),
    artifact_ref_count integer NOT NULL CHECK (
        artifact_ref_count BETWEEN 0 AND 128
    ),
    canonical_event text COLLATE "C" NOT NULL CHECK (
        octet_length(canonical_event) BETWEEN 2 AND 1048576
    ),
    CONSTRAINT event_ledger_events_pkey PRIMARY KEY (event_id),
    CONSTRAINT event_ledger_events_sha256_key UNIQUE (event_sha256),
    CONSTRAINT event_ledger_events_global_key UNIQUE (global_position),
    CONSTRAINT event_ledger_events_stream_version_key UNIQUE (
        partition_sha256,
        stream_id,
        stream_version
    ),
    CONSTRAINT event_ledger_events_stream_fkey FOREIGN KEY (
        partition_sha256,
        stream_id
    ) REFERENCES trace_backed_memory_v3_event_ledger.stream_heads (
        partition_sha256,
        stream_id
    ) ON UPDATE RESTRICT ON DELETE RESTRICT
      DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX event_ledger_events_partition_global
ON trace_backed_memory_v3_event_ledger.events (
    partition_sha256,
    global_position
);

CREATE INDEX event_ledger_events_partition_stream
ON trace_backed_memory_v3_event_ledger.events (
    partition_sha256,
    stream_id,
    stream_version
);

CREATE TABLE trace_backed_memory_v3_event_ledger.artifacts (
    event_id text COLLATE "C" NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    artifact_id text COLLATE "C" NOT NULL,
    content_sha256 text COLLATE "C" NOT NULL CHECK (
        content_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    media_type text COLLATE "C" NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 67108864),
    classification text COLLATE "C" NOT NULL CHECK (
        classification IN ('public', 'internal', 'confidential', 'restricted')
    ),
    retention_policy_id text COLLATE "C" NOT NULL,
    encryption_key_id text COLLATE "C",
    availability text COLLATE "C" NOT NULL CHECK (
        availability IN ('available', 'erased', 'external', 'unavailable')
    ),
    descriptor text COLLATE "C" NOT NULL CHECK (
        octet_length(descriptor) BETWEEN 2 AND 16384
    ),
    CONSTRAINT event_ledger_artifacts_pkey PRIMARY KEY (event_id, ordinal),
    CONSTRAINT event_ledger_artifacts_event_artifact_key UNIQUE (
        event_id,
        artifact_id
    ),
    CONSTRAINT event_ledger_artifacts_event_fkey FOREIGN KEY (event_id)
        REFERENCES trace_backed_memory_v3_event_ledger.events (event_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE trace_backed_memory_v3_event_ledger.idempotency (
    partition_sha256 text COLLATE "C" NOT NULL,
    idempotency_key_sha256 text COLLATE "C" NOT NULL CHECK (
        idempotency_key_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    command_sha256 text COLLATE "C" NOT NULL CHECK (
        command_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    request_sha256 text COLLATE "C" NOT NULL CHECK (
        request_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    stream_id text COLLATE "C" NOT NULL,
    previous_stream_version integer NOT NULL CHECK (
        previous_stream_version >= 0
    ),
    current_stream_version integer NOT NULL CHECK (
        current_stream_version > previous_stream_version
    ),
    first_global_position bigint NOT NULL CHECK (
        first_global_position >= 1
    ),
    last_global_position bigint NOT NULL CHECK (
        last_global_position >= first_global_position
    ),
    event_sha256s_json text COLLATE "C" NOT NULL CHECK (
        octet_length(event_sha256s_json) BETWEEN 4 AND 16384
    ),
    receipt_sha256 text COLLATE "C" NOT NULL CHECK (
        receipt_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT event_ledger_idempotency_pkey PRIMARY KEY (
        partition_sha256,
        idempotency_key_sha256
    )
);

CREATE INDEX event_ledger_idempotency_stream
ON trace_backed_memory_v3_event_ledger.idempotency (
    partition_sha256,
    stream_id,
    current_stream_version
);

CREATE TABLE trace_backed_memory_v3_event_ledger.checkpoints (
    projection_name text COLLATE "C" NOT NULL,
    projection_version integer NOT NULL CHECK (projection_version >= 1),
    partition_sha256 text COLLATE "C" NOT NULL CHECK (
        partition_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    global_position bigint NOT NULL CHECK (global_position >= 0),
    state_sha256 text COLLATE "C" NOT NULL CHECK (
        state_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    descriptor text COLLATE "C" NOT NULL CHECK (
        octet_length(descriptor) BETWEEN 2 AND 1048576
    ),
    CONSTRAINT event_ledger_checkpoints_pkey PRIMARY KEY (
        projection_name,
        projection_version,
        partition_sha256,
        global_position
    )
);

CREATE TABLE trace_backed_memory_v3_event_ledger.projection_activations (
    projection_name text COLLATE "C" NOT NULL,
    partition_sha256 text COLLATE "C" NOT NULL CHECK (
        partition_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    head_version bigint NOT NULL CHECK (head_version >= 1),
    target_build_id text COLLATE "C" NOT NULL CHECK (
        target_build_id ~ '^sha256:[0-9a-f]{64}$'
    ),
    previous_build_id text COLLATE "C" CHECK (
        previous_build_id IS NULL
        OR previous_build_id ~ '^sha256:[0-9a-f]{64}$'
    ),
    operation text COLLATE "C" NOT NULL CHECK (
        operation IN ('activate', 'rollback')
    ),
    activation_sha256 text COLLATE "C" NOT NULL UNIQUE CHECK (
        activation_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    descriptor text COLLATE "C" NOT NULL CHECK (
        octet_length(descriptor) BETWEEN 2 AND 1048576
    ),
    CONSTRAINT event_ledger_projection_activations_pkey PRIMARY KEY (
        projection_name,
        partition_sha256,
        head_version
    )
);

CREATE FUNCTION trace_backed_memory_v3_event_ledger.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL event ledger records are immutable';
END
$$;

CREATE FUNCTION trace_backed_memory_v3_event_ledger.validate_projection_activation_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.head_version = 1 THEN
        IF NEW.previous_build_id IS NOT NULL
           OR EXISTS (
                SELECT 1
                FROM trace_backed_memory_v3_event_ledger.projection_activations
                WHERE projection_name = NEW.projection_name
                  AND partition_sha256 = NEW.partition_sha256
           ) THEN
            RAISE EXCEPTION 'projection activation does not start its head';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_event_ledger.projection_activations
        WHERE projection_name = NEW.projection_name
          AND partition_sha256 = NEW.partition_sha256
          AND head_version = NEW.head_version - 1
          AND target_build_id = NEW.previous_build_id
    ) THEN
        RAISE EXCEPTION 'projection activation does not advance its head';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_event_ledger.validate_global_head_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT NEW.singleton
       OR NEW.current_global_position <> 0
       OR NEW.current_event_id IS NOT NULL
       OR NEW.current_event_sha256 IS NOT NULL THEN
        RAISE EXCEPTION 'PostgreSQL event ledger global head must start empty';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_event_ledger.validate_global_head_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.singleton <> OLD.singleton
       OR NEW.current_global_position <> OLD.current_global_position + 1
       OR NOT EXISTS (
            SELECT 1
            FROM trace_backed_memory_v3_event_ledger.events AS event
            WHERE event.global_position = NEW.current_global_position
              AND event.event_id = NEW.current_event_id
              AND event.event_sha256 = NEW.current_event_sha256
       ) THEN
        RAISE EXCEPTION 'PostgreSQL event ledger global head advance is invalid';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_event_ledger.validate_stream_head_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.current_stream_version <> 0
       OR NEW.current_event_id IS NOT NULL
       OR NEW.current_event_sha256 IS NOT NULL THEN
        RAISE EXCEPTION 'PostgreSQL event ledger stream head must start empty';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_event_ledger.validate_stream_head_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.partition_sha256 <> OLD.partition_sha256
       OR NEW.stream_id <> OLD.stream_id
       OR NEW.organization_id <> OLD.organization_id
       OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.repository_id <> OLD.repository_id
       OR NEW.environment_id <> OLD.environment_id
       OR NEW.current_stream_version <> OLD.current_stream_version + 1
       OR NOT EXISTS (
            SELECT 1
            FROM trace_backed_memory_v3_event_ledger.events AS event
            WHERE event.partition_sha256 = NEW.partition_sha256
              AND event.stream_id = NEW.stream_id
              AND event.stream_version = NEW.current_stream_version
              AND event.event_id = NEW.current_event_id
              AND event.event_sha256 = NEW.current_event_sha256
       ) THEN
        RAISE EXCEPTION 'PostgreSQL event ledger stream head advance is invalid';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_event_ledger.validate_event_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_event_ledger.stream_heads AS head
        WHERE head.partition_sha256 = NEW.partition_sha256
          AND head.stream_id = NEW.stream_id
          AND head.organization_id = NEW.organization_id
          AND head.tenant_id = NEW.tenant_id
          AND head.repository_id = NEW.repository_id
          AND head.environment_id = NEW.environment_id
          AND head.current_stream_version = NEW.stream_version - 1
          AND head.current_event_sha256 IS NOT DISTINCT FROM
              NEW.previous_stream_event_sha256
    ) OR NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_event_ledger.global_head AS head
        WHERE head.singleton
          AND head.current_global_position = NEW.global_position - 1
    ) THEN
        RAISE EXCEPTION 'PostgreSQL event does not extend current ledger heads';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_event_ledger.validate_artifact_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_event_ledger.events AS event
        WHERE event.event_id = NEW.event_id
          AND NEW.ordinal < event.artifact_ref_count
    ) THEN
        RAISE EXCEPTION 'PostgreSQL event artifact ordinal is invalid';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER event_ledger_schema_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_event_ledger.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_schema_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

CREATE TRIGGER event_ledger_global_head_initial
BEFORE INSERT ON trace_backed_memory_v3_event_ledger.global_head
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.validate_global_head_insert();
CREATE TRIGGER event_ledger_global_head_advance
BEFORE UPDATE ON trace_backed_memory_v3_event_ledger.global_head
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.validate_global_head_update();
CREATE TRIGGER event_ledger_global_head_no_delete
BEFORE DELETE ON trace_backed_memory_v3_event_ledger.global_head
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_global_head_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.global_head
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

CREATE TRIGGER event_ledger_stream_heads_initial
BEFORE INSERT ON trace_backed_memory_v3_event_ledger.stream_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.validate_stream_head_insert();
CREATE TRIGGER event_ledger_stream_heads_advance
BEFORE UPDATE ON trace_backed_memory_v3_event_ledger.stream_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.validate_stream_head_update();
CREATE TRIGGER event_ledger_stream_heads_no_delete
BEFORE DELETE ON trace_backed_memory_v3_event_ledger.stream_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_stream_heads_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.stream_heads
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

CREATE TRIGGER event_ledger_events_validate_insert
BEFORE INSERT ON trace_backed_memory_v3_event_ledger.events
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.validate_event_insert();
CREATE TRIGGER event_ledger_events_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_event_ledger.events
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_events_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.events
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

CREATE TRIGGER event_ledger_artifacts_validate_insert
BEFORE INSERT ON trace_backed_memory_v3_event_ledger.artifacts
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.validate_artifact_insert();
CREATE TRIGGER event_ledger_artifacts_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_event_ledger.artifacts
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_artifacts_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.artifacts
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

CREATE TRIGGER event_ledger_idempotency_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_event_ledger.idempotency
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_idempotency_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.idempotency
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

CREATE TRIGGER event_ledger_checkpoints_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_event_ledger.checkpoints
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_checkpoints_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.checkpoints
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

CREATE TRIGGER event_ledger_projection_activations_validate_insert
BEFORE INSERT ON trace_backed_memory_v3_event_ledger.projection_activations
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.validate_projection_activation_insert();
CREATE TRIGGER event_ledger_projection_activations_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_event_ledger.projection_activations
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();
CREATE TRIGGER event_ledger_projection_activations_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_event_ledger.projection_activations
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_event_ledger.reject_immutable_change();

REVOKE ALL ON ALL TABLES IN SCHEMA trace_backed_memory_v3_event_ledger
FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA trace_backed_memory_v3_event_ledger
FROM PUBLIC;

COMMIT;
