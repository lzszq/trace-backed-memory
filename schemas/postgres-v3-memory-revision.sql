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

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL memory revision v3 requires active schema version 2';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_memory_revision;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_memory_revision FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_memory_revision.schema_metadata (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.memory-revision.v3')
);

INSERT INTO trace_backed_memory_v3_memory_revision.schema_metadata (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.memory-revision.v3');

CREATE TABLE trace_backed_memory_v3_memory_revision.v3_fix_evidence (
    evidence_id text COLLATE "C" PRIMARY KEY
        CHECK (evidence_id ~ '^fix_evidence_sha256_[0-9a-f]{64}$'),
    case_id text COLLATE "C" NOT NULL
        CHECK (char_length(case_id) BETWEEN 1 AND 128),
    source_trace_id text COLLATE "C" NOT NULL
        CHECK (char_length(source_trace_id) BETWEEN 1 AND 128),
    source_commit_sha text COLLATE "C" NOT NULL
        CHECK (char_length(source_commit_sha) BETWEEN 1 AND 128),
    fix_commit_sha text COLLATE "C" NOT NULL
        CHECK (char_length(fix_commit_sha) BETWEEN 1 AND 128),
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576)
);

CREATE TABLE trace_backed_memory_v3_memory_revision.v3_regression_evidence (
    evidence_id text COLLATE "C" PRIMARY KEY
        CHECK (evidence_id ~ '^regression_sha256_[0-9a-f]{64}$'),
    case_id text COLLATE "C" NOT NULL
        CHECK (char_length(case_id) BETWEEN 1 AND 128),
    source_trace_id text COLLATE "C" NOT NULL
        CHECK (char_length(source_trace_id) BETWEEN 1 AND 128),
    source_commit_sha text COLLATE "C" NOT NULL
        CHECK (char_length(source_commit_sha) BETWEEN 1 AND 128),
    fix_commit_sha text COLLATE "C" NOT NULL
        CHECK (char_length(fix_commit_sha) BETWEEN 1 AND 128),
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576)
);

CREATE TABLE trace_backed_memory_v3_memory_revision.v3_memory_revision_proposals (
    revision_id text COLLATE "C" PRIMARY KEY
        CHECK (revision_id ~ '^memory_revision_sha256_[0-9a-f]{64}$'),
    memory_id text COLLATE "C" NOT NULL
        CHECK (char_length(memory_id) BETWEEN 1 AND 128),
    revision_number integer NOT NULL CHECK (revision_number >= 1),
    previous_revision_id text COLLATE "C"
        REFERENCES trace_backed_memory_v3_memory_revision.
            v3_memory_revision_proposals (revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    fix_evidence_id text COLLATE "C"
        REFERENCES trace_backed_memory_v3_memory_revision.v3_fix_evidence (
            evidence_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576),
    UNIQUE (memory_id, revision_number)
);

CREATE TABLE
    trace_backed_memory_v3_memory_revision.
        v3_memory_revision_regression_evidence (
    revision_id text COLLATE "C" NOT NULL
        REFERENCES trace_backed_memory_v3_memory_revision.
            v3_memory_revision_proposals (revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    evidence_id text COLLATE "C" NOT NULL
        REFERENCES trace_backed_memory_v3_memory_revision.
            v3_regression_evidence (evidence_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (revision_id, evidence_id),
    UNIQUE (revision_id, ordinal)
);

CREATE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'PostgreSQL memory revision v3 records are immutable';
END
$$;

CREATE FUNCTION
    trace_backed_memory_v3_memory_revision.validate_revision_parent()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    parent_memory_id text;
    parent_revision_number integer;
BEGIN
    IF NEW.revision_number = 1 THEN
        IF NEW.previous_revision_id IS NOT NULL THEN
            RAISE EXCEPTION 'first revision must not have a parent';
        END IF;
        RETURN NEW;
    END IF;

    SELECT parent.memory_id, parent.revision_number
    INTO parent_memory_id, parent_revision_number
    FROM trace_backed_memory_v3_memory_revision.
        v3_memory_revision_proposals AS parent
    WHERE parent.revision_id = NEW.previous_revision_id
    FOR SHARE;

    IF parent_memory_id IS NULL
       OR parent_memory_id <> NEW.memory_id
       OR parent_revision_number <> NEW.revision_number - 1 THEN
        RAISE EXCEPTION 'revision parent continuity mismatch';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_memory_revision.validate_revision_parent()
    FROM PUBLIC;

CREATE TRIGGER memory_revision_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_revision.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();
CREATE TRIGGER memory_revision_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_revision.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();

CREATE TRIGGER memory_revision_fix_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_revision.v3_fix_evidence
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();
CREATE TRIGGER memory_revision_fix_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_revision.v3_fix_evidence
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();

CREATE TRIGGER memory_revision_regression_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_revision.v3_regression_evidence
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();
CREATE TRIGGER memory_revision_regression_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_revision.v3_regression_evidence
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();

CREATE TRIGGER memory_revision_proposal_parent
BEFORE INSERT
ON trace_backed_memory_v3_memory_revision.v3_memory_revision_proposals
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.validate_revision_parent();
CREATE TRIGGER memory_revision_proposal_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_revision.v3_memory_revision_proposals
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();
CREATE TRIGGER memory_revision_proposal_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_revision.v3_memory_revision_proposals
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();

CREATE TRIGGER memory_revision_link_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_revision.
    v3_memory_revision_regression_evidence
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();
CREATE TRIGGER memory_revision_link_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_revision.
    v3_memory_revision_regression_evidence
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_revision.reject_immutable_change();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_memory_revision.schema_metadata
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_memory_revision.v3_fix_evidence
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_memory_revision.v3_regression_evidence
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_memory_revision.v3_memory_revision_proposals
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_memory_revision.
        v3_memory_revision_regression_evidence
    FROM PUBLIC;

COMMIT;
