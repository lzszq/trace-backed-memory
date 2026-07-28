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
            'PostgreSQL gate evidence v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence v3 requires active schema version 2, found %',
            active_version;
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_gate_evidence;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_gate_evidence FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_gate_evidence.schema_metadata (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.gate-evidence.v3')
);

INSERT INTO trace_backed_memory_v3_gate_evidence.schema_metadata (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.gate-evidence.v3');

CREATE TABLE trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots (
    snapshot_id text COLLATE "C" PRIMARY KEY
        CHECK (
            snapshot_id ~ '^retrieval_snapshot_sha256_[0-9a-f]{64}$'
        ),
    session_id text COLLATE "C" NOT NULL
        CHECK (char_length(session_id) BETWEEN 1 AND 128),
    authorization_event_id text COLLATE "C" NOT NULL
        CHECK (
            authorization_event_id ~ '^authz_sha256_[0-9a-f]{64}$'
        ),
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576)
);

CREATE INDEX v3_retrieval_snapshots_session
    ON trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots (
        session_id, authorization_event_id
    );

CREATE TABLE trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations (
    evaluation_id text COLLATE "C" PRIMARY KEY
        CHECK (
            evaluation_id ~ '^system_gate_sha256_[0-9a-f]{64}$'
        ),
    session_id text COLLATE "C" NOT NULL
        CHECK (char_length(session_id) BETWEEN 1 AND 128),
    retrieval_snapshot_id text COLLATE "C" NOT NULL UNIQUE
        REFERENCES trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots (
            snapshot_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    authorization_event_id text COLLATE "C" NOT NULL
        CHECK (
            authorization_event_id ~ '^authz_sha256_[0-9a-f]{64}$'
        ),
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576)
);

CREATE INDEX v3_system_gate_evaluations_session
    ON trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations (
        session_id, authorization_event_id
    );

CREATE FUNCTION trace_backed_memory_v3_gate_evidence.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL gate evidence v3 records are immutable';
END
$$;

CREATE FUNCTION trace_backed_memory_v3_gate_evidence.validate_evaluation_parent()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    parent_session_id text;
    parent_authorization_event_id text;
BEGIN
    SELECT snapshot.session_id, snapshot.authorization_event_id
    INTO parent_session_id, parent_authorization_event_id
    FROM trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots AS snapshot
    WHERE snapshot.snapshot_id = NEW.retrieval_snapshot_id
    FOR SHARE;

    IF parent_session_id IS NULL
       OR parent_session_id <> NEW.session_id
       OR parent_authorization_event_id <> NEW.authorization_event_id THEN
        RAISE EXCEPTION
            'PostgreSQL gate evidence evaluation parent mismatch';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_gate_evidence.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_gate_evidence.validate_evaluation_parent()
    FROM PUBLIC;

CREATE TRIGGER gate_evidence_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_gate_evidence.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_gate_evidence.reject_immutable_change();

CREATE TRIGGER gate_evidence_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_gate_evidence.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_gate_evidence.reject_immutable_change();

CREATE TRIGGER gate_evidence_snapshot_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_gate_evidence.reject_immutable_change();

CREATE TRIGGER gate_evidence_snapshot_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_gate_evidence.reject_immutable_change();

CREATE TRIGGER gate_evidence_evaluation_parent
BEFORE INSERT
ON trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_gate_evidence.validate_evaluation_parent();

CREATE TRIGGER gate_evidence_evaluation_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_gate_evidence.reject_immutable_change();

CREATE TRIGGER gate_evidence_evaluation_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_gate_evidence.reject_immutable_change();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_gate_evidence.schema_metadata
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations
    FROM PUBLIC;

COMMIT;
