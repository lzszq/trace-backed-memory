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
            'PostgreSQL GateSession v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession v3 requires active schema version 2, found %',
            active_version;
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_gate_session;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_gate_session FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_gate_session.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.gate-session.v3')
);

INSERT INTO trace_backed_memory_v3_gate_session.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (true, 1, 'tbm.gate-session.v3');

CREATE TABLE trace_backed_memory_v3_gate_session.gate_session_heads (
    session_id text COLLATE "C" PRIMARY KEY
        CHECK (char_length(session_id) BETWEEN 1 AND 128),
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    repository_id text COLLATE "C" NOT NULL
        CHECK (char_length(repository_id) BETWEEN 1 AND 128),
    principal_id text COLLATE "C" NOT NULL
        CHECK (char_length(principal_id) BETWEEN 1 AND 128),
    agent_client_id text COLLATE "C" NOT NULL
        CHECK (char_length(agent_client_id) BETWEEN 1 AND 128),
    trace_id text COLLATE "C" NOT NULL
        CHECK (char_length(trace_id) BETWEEN 1 AND 128),
    run_id text COLLATE "C" NOT NULL
        CHECK (char_length(run_id) BETWEEN 1 AND 128),
    request_fingerprint text COLLATE "C" NOT NULL
        CHECK (
            request_fingerprint ~
                '^sha256:[0-9a-f]{64}$'
        ),
    idempotency_key text COLLATE "C" NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 1 AND 512),
    current_version integer NOT NULL CHECK (current_version >= 1),
    CONSTRAINT gate_session_heads_idempotency_key UNIQUE (
        tenant_id,
        repository_id,
        principal_id,
        agent_client_id,
        idempotency_key
    )
);

CREATE TABLE trace_backed_memory_v3_gate_session.gate_session_revisions (
    session_id text COLLATE "C" NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    status text COLLATE "C" NOT NULL CHECK (
        status IN (
            'created',
            'prepared',
            'awaiting_decision',
            'decided',
            'finalized',
            'executing',
            'completed',
            'canceled',
            'expired',
            'abandoned'
        )
    ),
    updated_at timestamp(6) with time zone NOT NULL,
    expires_at timestamp(6) with time zone NOT NULL,
    lease_expires_at timestamp(6) with time zone,
    payload text COLLATE "C" NOT NULL CHECK (
        octet_length(payload) BETWEEN 1 AND 1048576
    ),
    CONSTRAINT gate_session_revisions_pkey
        PRIMARY KEY (session_id, version),
    CONSTRAINT gate_session_revisions_head_fkey
        FOREIGN KEY (session_id)
        REFERENCES trace_backed_memory_v3_gate_session.gate_session_heads (
            session_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT gate_session_revisions_lease_shape CHECK (
        (
            status IN (
                'prepared',
                'awaiting_decision',
                'decided',
                'finalized',
                'executing'
            )
            AND lease_expires_at IS NOT NULL
            AND lease_expires_at > updated_at
            AND lease_expires_at <= expires_at
        )
        OR (
            status NOT IN (
                'prepared',
                'awaiting_decision',
                'decided',
                'finalized',
                'executing'
            )
            AND lease_expires_at IS NULL
        )
    ),
    CONSTRAINT gate_session_revisions_expiry_shape CHECK (
        (
            status = 'expired'
            AND updated_at >= expires_at
        )
        OR (
            status <> 'expired'
            AND (
                status IN ('completed', 'canceled', 'abandoned')
                OR updated_at <= expires_at
            )
        )
    )
);

CREATE INDEX gate_session_revisions_due
ON trace_backed_memory_v3_gate_session.gate_session_revisions (
    status,
    expires_at,
    lease_expires_at,
    session_id
)
WHERE status IN (
    'prepared',
    'awaiting_decision',
    'decided',
    'finalized',
    'executing'
);

CREATE FUNCTION
trace_backed_memory_v3_gate_session.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL GateSession records are immutable';
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_gate_session.protect_head_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.repository_id IS DISTINCT FROM OLD.repository_id
       OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
       OR NEW.agent_client_id IS DISTINCT FROM OLD.agent_client_id
       OR NEW.trace_id IS DISTINCT FROM OLD.trace_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'PostgreSQL GateSession identity is immutable';
    END IF;

    IF NEW.current_version <> OLD.current_version + 1
       OR NOT EXISTS (
           SELECT 1
           FROM
               trace_backed_memory_v3_gate_session.gate_session_revisions
                   AS revision
           WHERE revision.session_id = OLD.session_id
             AND revision.version = NEW.current_version
       ) THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession head must advance by one revision';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_gate_session.validate_revision_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    previous_status text;
    previous_updated_at timestamp(6) with time zone;
    previous_expires_at timestamp(6) with time zone;
    head_version integer;
    expected_version integer;
BEGIN
    SELECT
        previous.status,
        previous.updated_at,
        previous.expires_at,
        head.current_version
    INTO
        previous_status,
        previous_updated_at,
        previous_expires_at,
        head_version
    FROM trace_backed_memory_v3_gate_session.gate_session_heads AS head
    LEFT JOIN
        trace_backed_memory_v3_gate_session.gate_session_revisions
            AS previous
      ON previous.session_id = head.session_id
     AND previous.version = NEW.version - 1
    WHERE head.session_id = NEW.session_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'PostgreSQL GateSession head is missing';
    END IF;

    SELECT COALESCE(MAX(revision.version) + 1, 1)
    INTO expected_version
    FROM trace_backed_memory_v3_gate_session.gate_session_revisions AS revision
    WHERE revision.session_id = NEW.session_id;

    IF NEW.version <> expected_version THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession revision version is not contiguous';
    END IF;

    IF NEW.version = 1 THEN
        IF NEW.status <> 'created' OR head_version <> 1 THEN
            RAISE EXCEPTION
                'PostgreSQL GateSession first revision must be created';
        END IF;
        RETURN NEW;
    END IF;

    IF previous_status IS NULL
       OR head_version <> NEW.version - 1
       OR NEW.updated_at <= previous_updated_at
       OR NEW.expires_at IS DISTINCT FROM previous_expires_at THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession revision does not extend current head';
    END IF;

    IF NOT (
        (previous_status = 'created'
            AND NEW.status IN ('prepared', 'canceled'))
        OR (previous_status = 'prepared'
            AND NEW.status IN (
                'prepared',
                'awaiting_decision',
                'canceled',
                'expired'
            ))
        OR (previous_status = 'awaiting_decision'
            AND NEW.status IN (
                'awaiting_decision',
                'decided',
                'canceled',
                'expired'
            ))
        OR (previous_status = 'decided'
            AND NEW.status IN ('decided', 'finalized'))
        OR (previous_status = 'finalized'
            AND NEW.status IN ('finalized', 'executing'))
        OR (previous_status = 'executing'
            AND NEW.status IN ('executing', 'completed', 'abandoned'))
    ) THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession revision transition is invalid';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_gate_session.validate_head_revision_consistency()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    head_version integer;
    maximum_revision integer;
BEGIN
    SELECT head.current_version
    INTO head_version
    FROM trace_backed_memory_v3_gate_session.gate_session_heads AS head
    WHERE head.session_id = NEW.session_id;

    SELECT MAX(revision.version)
    INTO maximum_revision
    FROM trace_backed_memory_v3_gate_session.gate_session_revisions AS revision
    WHERE revision.session_id = NEW.session_id;

    IF head_version IS NULL
       OR maximum_revision IS NULL
       OR head_version <> maximum_revision THEN
        RAISE EXCEPTION
            'PostgreSQL GateSession head and revision are inconsistent';
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER gate_session_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_gate_session.schema_metadata
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change();

CREATE TRIGGER gate_session_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_gate_session.schema_metadata
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change();

CREATE TRIGGER gate_session_heads_protect_update
BEFORE UPDATE
ON trace_backed_memory_v3_gate_session.gate_session_heads
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.protect_head_update();

CREATE TRIGGER gate_session_heads_immutable_delete
BEFORE DELETE
ON trace_backed_memory_v3_gate_session.gate_session_heads
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change();

CREATE TRIGGER gate_session_heads_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_gate_session.gate_session_heads
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change();

CREATE CONSTRAINT TRIGGER gate_session_heads_insert_consistent_revision
AFTER INSERT
ON trace_backed_memory_v3_gate_session.gate_session_heads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session
        .validate_head_revision_consistency();

CREATE CONSTRAINT TRIGGER gate_session_heads_update_consistent_revision
AFTER UPDATE OF current_version
ON trace_backed_memory_v3_gate_session.gate_session_heads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session
        .validate_head_revision_consistency();

CREATE TRIGGER gate_session_revisions_validate_insert
BEFORE INSERT
ON trace_backed_memory_v3_gate_session.gate_session_revisions
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.validate_revision_insert();

CREATE CONSTRAINT TRIGGER gate_session_revisions_consistent_head
AFTER INSERT
ON trace_backed_memory_v3_gate_session.gate_session_revisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session
        .validate_head_revision_consistency();

CREATE TRIGGER gate_session_revisions_immutable_change
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_gate_session.gate_session_revisions
FOR EACH ROW
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change();

CREATE TRIGGER gate_session_revisions_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_gate_session.gate_session_revisions
FOR EACH STATEMENT
EXECUTE FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change();

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_gate_session.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_gate_session.protect_head_update()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_gate_session.validate_revision_insert()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_gate_session.validate_head_revision_consistency()
    FROM PUBLIC;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON trace_backed_memory_v3_gate_session.schema_metadata
FROM PUBLIC;

COMMIT;
