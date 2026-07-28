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
            'PostgreSQL audit v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL audit v3 requires active schema version 2, found %',
            active_version;
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_audit;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_audit FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_audit.schema_metadata (
    singleton boolean
        CONSTRAINT audit_schema_metadata_pkey PRIMARY KEY
        DEFAULT true
        CONSTRAINT audit_schema_metadata_singleton_check CHECK (singleton),
    schema_version integer NOT NULL
        CONSTRAINT audit_schema_metadata_schema_version_check
        CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CONSTRAINT audit_schema_metadata_contract_version_check
        CHECK (contract_version = 'tbm.audit-event.v3')
);

INSERT INTO trace_backed_memory_v3_audit.schema_metadata (
    singleton, schema_version, contract_version
) VALUES (true, 1, 'tbm.audit-event.v3');

CREATE TABLE trace_backed_memory_v3_audit.audit_stream_heads (
    stream_id text COLLATE "C"
        CONSTRAINT audit_stream_heads_pkey PRIMARY KEY
        CONSTRAINT audit_stream_heads_stream_id_check CHECK (
            char_length(stream_id) BETWEEN 1 AND 128
        ),
    tenant_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_stream_heads_tenant_id_check CHECK (
            char_length(tenant_id) BETWEEN 1 AND 128
        ),
    repository_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_stream_heads_repository_id_check CHECK (
            char_length(repository_id) BETWEEN 1 AND 128
        ),
    session_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_stream_heads_session_id_check CHECK (
            char_length(session_id) BETWEEN 1 AND 128
        ),
    trace_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_stream_heads_trace_id_check CHECK (
            char_length(trace_id) BETWEEN 1 AND 128
        ),
    run_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_stream_heads_run_id_check CHECK (
            char_length(run_id) BETWEEN 1 AND 128
        ),
    current_sequence integer NOT NULL DEFAULT 0
        CONSTRAINT audit_stream_heads_sequence_check CHECK (
            current_sequence BETWEEN 0 AND 2147483647
        ),
    current_event_id text COLLATE "C"
        CONSTRAINT audit_stream_heads_event_id_check CHECK (
            current_event_id IS NULL
            OR current_event_id ~ '^audit_event_sha256_[0-9a-f]{64}$'
        ),
    CONSTRAINT audit_stream_heads_shape_check CHECK (
        (current_sequence = 0 AND current_event_id IS NULL)
        OR (current_sequence > 0 AND current_event_id IS NOT NULL)
    )
);

CREATE TABLE trace_backed_memory_v3_audit.audit_events (
    event_id text COLLATE "C"
        CONSTRAINT audit_events_pkey PRIMARY KEY
        CONSTRAINT audit_events_event_id_check CHECK (
            event_id ~ '^audit_event_sha256_[0-9a-f]{64}$'
        ),
    stream_id text COLLATE "C" NOT NULL,
    sequence integer NOT NULL
        CONSTRAINT audit_events_sequence_check CHECK (
            sequence BETWEEN 1 AND 2147483647
        ),
    previous_event_id text COLLATE "C"
        CONSTRAINT audit_events_previous_event_id_check CHECK (
            previous_event_id IS NULL
            OR previous_event_id ~ '^audit_event_sha256_[0-9a-f]{64}$'
        ),
    tenant_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_tenant_id_check CHECK (
            char_length(tenant_id) BETWEEN 1 AND 128
        ),
    repository_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_repository_id_check CHECK (
            char_length(repository_id) BETWEEN 1 AND 128
        ),
    session_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_session_id_check CHECK (
            char_length(session_id) BETWEEN 1 AND 128
        ),
    trace_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_trace_id_check CHECK (
            char_length(trace_id) BETWEEN 1 AND 128
        ),
    run_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_run_id_check CHECK (
            char_length(run_id) BETWEEN 1 AND 128
        ),
    actor_type text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_actor_type_check CHECK (
            actor_type IN ('principal', 'service', 'worker')
        ),
    actor_id text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_actor_id_check CHECK (
            char_length(actor_id) BETWEEN 1 AND 128
        ),
    event_type text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_event_type_check CHECK (
            event_type IN (
                'session_created', 'session_transitioned',
                'authorization_evaluated', 'retrieval_recorded',
                'system_gate_evaluated', 'semantic_gate_attempted',
                'decision_finalized', 'injection_created',
                'execution_completed', 'outcome_attributed',
                'recovery_succeeded', 'recovery_failed',
                'session_canceled', 'session_expired', 'session_abandoned'
            )
        ),
    recovery_action_id text COLLATE "C"
        CONSTRAINT audit_events_recovery_action_id_check CHECK (
            recovery_action_id IS NULL
            OR recovery_action_id ~ '^recovery_action_sha256_[0-9a-f]{64}$'
        ),
    reason_code text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_reason_code_check CHECK (
            char_length(reason_code) BETWEEN 1 AND 256
        ),
    payload_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_payload_sha256_check CHECK (
            payload_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    occurred_at text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_occurred_at_check CHECK (
            char_length(occurred_at) BETWEEN 1 AND 64
        ),
    descriptor text COLLATE "C" NOT NULL
        CONSTRAINT audit_events_descriptor_check CHECK (
            octet_length(descriptor) BETWEEN 1 AND 1048576
        ),
    CONSTRAINT audit_events_stream_sequence_key UNIQUE (stream_id, sequence),
    CONSTRAINT audit_events_stream_event_key UNIQUE (stream_id, event_id),
    CONSTRAINT audit_events_recovery_action_key UNIQUE (recovery_action_id),
    CONSTRAINT audit_events_recovery_pair_key
        UNIQUE (recovery_action_id, event_id),
    CONSTRAINT audit_events_recovery_shape_check CHECK (
        (
            event_type IN ('recovery_succeeded', 'recovery_failed')
            AND recovery_action_id IS NOT NULL
        )
        OR (
            event_type NOT IN ('recovery_succeeded', 'recovery_failed')
            AND recovery_action_id IS NULL
        )
    ),
    CONSTRAINT audit_events_stream_fkey
        FOREIGN KEY (stream_id)
        REFERENCES trace_backed_memory_v3_audit.audit_stream_heads (stream_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT audit_events_parent_fkey
        FOREIGN KEY (stream_id, previous_event_id)
        REFERENCES trace_backed_memory_v3_audit.audit_events (
            stream_id, event_id
        )
        ON UPDATE RESTRICT ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX audit_events_session
ON trace_backed_memory_v3_audit.audit_events (session_id, sequence);
CREATE INDEX audit_events_type
ON trace_backed_memory_v3_audit.audit_events (event_type, occurred_at);

CREATE TABLE trace_backed_memory_v3_audit.recovery_actions (
    recovery_action_id text COLLATE "C"
        CONSTRAINT recovery_actions_pkey PRIMARY KEY
        CONSTRAINT recovery_actions_id_check CHECK (
            recovery_action_id ~ '^recovery_action_sha256_[0-9a-f]{64}$'
        ),
    event_id text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_event_id_key UNIQUE,
    session_id text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_session_id_check CHECK (
            char_length(session_id) BETWEEN 1 AND 128
        ),
    trace_id text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_trace_id_check CHECK (
            char_length(trace_id) BETWEEN 1 AND 128
        ),
    run_id text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_run_id_check CHECK (
            char_length(run_id) BETWEEN 1 AND 128
        ),
    result text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_result_check CHECK (
            result IN ('succeeded', 'failed')
        ),
    executor_id text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_executor_id_check CHECK (
            char_length(executor_id) BETWEEN 1 AND 128
        ),
    request_sha256 text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_request_sha256_check CHECK (
            request_sha256 ~ '^sha256:[0-9a-f]{64}$'
        ),
    finished_at text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_finished_at_check CHECK (
            char_length(finished_at) BETWEEN 1 AND 64
        ),
    descriptor text COLLATE "C" NOT NULL
        CONSTRAINT recovery_actions_descriptor_check CHECK (
            octet_length(descriptor) BETWEEN 1 AND 1048576
        ),
    CONSTRAINT recovery_actions_request_key
        UNIQUE (session_id, request_sha256),
    CONSTRAINT recovery_actions_event_fkey
        FOREIGN KEY (recovery_action_id, event_id)
        REFERENCES trace_backed_memory_v3_audit.audit_events (
            recovery_action_id, event_id
        )
        ON UPDATE RESTRICT ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE trace_backed_memory_v3_audit.audit_events
ADD CONSTRAINT audit_events_recovery_action_fkey
FOREIGN KEY (recovery_action_id)
REFERENCES trace_backed_memory_v3_audit.recovery_actions (
    recovery_action_id
)
ON UPDATE RESTRICT ON DELETE RESTRICT
DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX recovery_actions_session
ON trace_backed_memory_v3_audit.recovery_actions (
    session_id, recovery_action_id
);

CREATE FUNCTION trace_backed_memory_v3_audit.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL audit v3 records are immutable';
END
$$;

CREATE FUNCTION trace_backed_memory_v3_audit.validate_head_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.current_sequence <> 0 OR NEW.current_event_id IS NOT NULL THEN
        RAISE EXCEPTION 'PostgreSQL audit stream head must begin empty';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_audit.validate_event_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    head trace_backed_memory_v3_audit.audit_stream_heads%ROWTYPE;
BEGIN
    SELECT * INTO head
    FROM trace_backed_memory_v3_audit.audit_stream_heads
    WHERE stream_id = NEW.stream_id
    FOR UPDATE;
    IF NOT FOUND
       OR head.tenant_id <> NEW.tenant_id
       OR head.repository_id <> NEW.repository_id
       OR head.session_id <> NEW.session_id
       OR head.trace_id <> NEW.trace_id
       OR head.run_id <> NEW.run_id
       OR NEW.sequence <> head.current_sequence + 1
       OR (
           head.current_sequence = 0
           AND NEW.previous_event_id IS NOT NULL
       )
       OR (
           head.current_sequence > 0
           AND NEW.previous_event_id IS DISTINCT FROM head.current_event_id
       ) THEN
        RAISE EXCEPTION 'PostgreSQL audit event is not the next stream event';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_audit.validate_head_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.stream_id <> OLD.stream_id
       OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.repository_id <> OLD.repository_id
       OR NEW.session_id <> OLD.session_id
       OR NEW.trace_id <> OLD.trace_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.current_sequence <> OLD.current_sequence + 1
       OR NEW.current_event_id IS NULL
       OR NOT EXISTS (
           SELECT 1
           FROM trace_backed_memory_v3_audit.audit_events AS event
           WHERE event.stream_id = OLD.stream_id
             AND event.sequence = NEW.current_sequence
             AND event.event_id = NEW.current_event_id
             AND (
                 (
                     OLD.current_sequence = 0
                     AND event.previous_event_id IS NULL
                 )
                 OR event.previous_event_id = OLD.current_event_id
             )
       ) THEN
        RAISE EXCEPTION
            'PostgreSQL audit stream head must advance exactly once';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_audit.validate_stream_consistency()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    event_sequence integer;
    head_sequence integer;
    head_event_id text;
BEGIN
    IF TG_TABLE_NAME = 'audit_events' THEN
        event_sequence := NEW.sequence;
        SELECT current_sequence, current_event_id
        INTO head_sequence, head_event_id
        FROM trace_backed_memory_v3_audit.audit_stream_heads
        WHERE stream_id = NEW.stream_id;
        IF head_sequence IS NULL
           OR head_sequence < event_sequence
           OR (
               head_sequence = event_sequence
               AND head_event_id <> NEW.event_id
           ) THEN
            RAISE EXCEPTION
                'PostgreSQL audit event was not committed to stream head';
        END IF;
    ELSE
        IF NEW.current_sequence > 0 AND NOT EXISTS (
            SELECT 1
            FROM trace_backed_memory_v3_audit.audit_events AS event
            WHERE event.stream_id = NEW.stream_id
              AND event.sequence = NEW.current_sequence
              AND event.event_id = NEW.current_event_id
        ) THEN
            RAISE EXCEPTION
                'PostgreSQL audit stream head references a missing event';
        END IF;
    END IF;
    RETURN NULL;
END
$$;

CREATE FUNCTION trace_backed_memory_v3_audit.validate_recovery_pair()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    event trace_backed_memory_v3_audit.audit_events%ROWTYPE;
BEGIN
    SELECT * INTO event
    FROM trace_backed_memory_v3_audit.audit_events
    WHERE recovery_action_id = NEW.recovery_action_id
      AND event_id = NEW.event_id;
    IF NOT FOUND
       OR event.event_type <> (
           CASE NEW.result
               WHEN 'succeeded' THEN 'recovery_succeeded'
               ELSE 'recovery_failed'
           END
       )
       OR event.actor_id <> NEW.executor_id
       OR event.session_id <> NEW.session_id
       OR event.trace_id <> NEW.trace_id
       OR event.run_id <> NEW.run_id
       OR event.payload_sha256 <> NEW.request_sha256
       OR event.occurred_at::timestamptz < NEW.finished_at::timestamptz THEN
        RAISE EXCEPTION
            'PostgreSQL recovery action and audit event linkage differs';
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER audit_schema_metadata_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_audit.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();
CREATE TRIGGER audit_schema_metadata_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_audit.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();

CREATE TRIGGER audit_stream_heads_initial
BEFORE INSERT ON trace_backed_memory_v3_audit.audit_stream_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.validate_head_insert();
CREATE TRIGGER audit_stream_heads_advance
BEFORE UPDATE ON trace_backed_memory_v3_audit.audit_stream_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.validate_head_update();
CREATE TRIGGER audit_stream_heads_immutable_delete
BEFORE DELETE ON trace_backed_memory_v3_audit.audit_stream_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();
CREATE TRIGGER audit_stream_heads_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_audit.audit_stream_heads
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();
CREATE CONSTRAINT TRIGGER audit_stream_heads_consistent
AFTER INSERT OR UPDATE ON trace_backed_memory_v3_audit.audit_stream_heads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.validate_stream_consistency();

CREATE TRIGGER audit_events_append
BEFORE INSERT ON trace_backed_memory_v3_audit.audit_events
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.validate_event_insert();
CREATE TRIGGER audit_events_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_audit.audit_events
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();
CREATE TRIGGER audit_events_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_audit.audit_events
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();
CREATE CONSTRAINT TRIGGER audit_events_consistent
AFTER INSERT ON trace_backed_memory_v3_audit.audit_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.validate_stream_consistency();

CREATE TRIGGER recovery_actions_immutable
BEFORE UPDATE OR DELETE ON trace_backed_memory_v3_audit.recovery_actions
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();
CREATE TRIGGER recovery_actions_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_audit.recovery_actions
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_audit.reject_immutable_change();
CREATE CONSTRAINT TRIGGER recovery_actions_pair
AFTER INSERT ON trace_backed_memory_v3_audit.recovery_actions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_audit.validate_recovery_pair();

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA trace_backed_memory_v3_audit
FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE
ON ALL TABLES IN SCHEMA trace_backed_memory_v3_audit
FROM PUBLIC;

COMMIT;
