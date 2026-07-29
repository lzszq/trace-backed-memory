BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    revision_version integer;
    revision_contract text;
BEGIN
    SELECT schema_version INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;
    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication v3 requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO revision_version, revision_contract
    FROM trace_backed_memory_v3_memory_revision.schema_metadata
    WHERE singleton = 1
    FOR UPDATE;
    IF revision_version IS NULL
       OR revision_version <> 1
       OR revision_contract <> 'tbm.memory-revision.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL memory publication v3 requires memory revision v3';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_memory_publication;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_memory_publication FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_memory_publication.schema_metadata (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.memory-publication.v3')
);
INSERT INTO trace_backed_memory_v3_memory_publication.schema_metadata (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.memory-publication.v3');

CREATE TABLE
    trace_backed_memory_v3_memory_publication.v3_memory_revision_approvals (
    approval_id text COLLATE "C" PRIMARY KEY
        CHECK (approval_id ~ '^memory_approval_sha256_[0-9a-f]{64}$'),
    revision_id text COLLATE "C" NOT NULL UNIQUE
        REFERENCES trace_backed_memory_v3_memory_revision.
            v3_memory_revision_proposals (revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    repository_id text COLLATE "C",
    repository_id_key text COLLATE "C" NOT NULL
        CHECK (repository_id_key = COALESCE(repository_id, '')),
    memory_id text COLLATE "C" NOT NULL
        CHECK (char_length(memory_id) BETWEEN 1 AND 128),
    revision_number integer NOT NULL CHECK (revision_number >= 1),
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576),
    authorization_policy_descriptor text COLLATE "C" NOT NULL
        CHECK (
            octet_length(authorization_policy_descriptor)
            BETWEEN 1 AND 1048576
        ),
    authorization_request_descriptor text COLLATE "C" NOT NULL
        CHECK (
            octet_length(authorization_request_descriptor)
            BETWEEN 1 AND 1048576
        ),
    authorization_decision_descriptor text COLLATE "C" NOT NULL
        CHECK (
            octet_length(authorization_decision_descriptor)
            BETWEEN 1 AND 1048576
        ),
    attestation_verified_by text COLLATE "C" NOT NULL
        CHECK (char_length(attestation_verified_by) BETWEEN 1 AND 128)
);

CREATE TABLE
    trace_backed_memory_v3_memory_publication.v3_memory_revision_activations (
    activation_id text COLLATE "C" PRIMARY KEY
        CHECK (activation_id ~ '^memory_activation_sha256_[0-9a-f]{64}$'),
    approval_id text COLLATE "C" NOT NULL UNIQUE
        REFERENCES trace_backed_memory_v3_memory_publication.
            v3_memory_revision_approvals (approval_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    revision_id text COLLATE "C" NOT NULL UNIQUE
        REFERENCES trace_backed_memory_v3_memory_revision.
            v3_memory_revision_proposals (revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    repository_id text COLLATE "C",
    repository_id_key text COLLATE "C" NOT NULL
        CHECK (repository_id_key = COALESCE(repository_id, '')),
    memory_id text COLLATE "C" NOT NULL
        CHECK (char_length(memory_id) BETWEEN 1 AND 128),
    revision_number integer NOT NULL CHECK (revision_number >= 1),
    previous_activation_id text COLLATE "C"
        REFERENCES trace_backed_memory_v3_memory_publication.
            v3_memory_revision_activations (activation_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576),
    authorization_policy_descriptor text COLLATE "C" NOT NULL
        CHECK (
            octet_length(authorization_policy_descriptor)
            BETWEEN 1 AND 1048576
        ),
    authorization_request_descriptor text COLLATE "C" NOT NULL
        CHECK (
            octet_length(authorization_request_descriptor)
            BETWEEN 1 AND 1048576
        ),
    authorization_decision_descriptor text COLLATE "C" NOT NULL
        CHECK (
            octet_length(authorization_decision_descriptor)
            BETWEEN 1 AND 1048576
        ),
    attestation_verified_by text COLLATE "C" NOT NULL
        CHECK (char_length(attestation_verified_by) BETWEEN 1 AND 128),
    UNIQUE (
        tenant_id,
        repository_id_key,
        memory_id,
        revision_number
    )
);

CREATE TABLE
    trace_backed_memory_v3_memory_publication.
        v3_memory_revision_activation_heads (
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    repository_id text COLLATE "C",
    repository_id_key text COLLATE "C" NOT NULL
        CHECK (repository_id_key = COALESCE(repository_id, '')),
    memory_id text COLLATE "C" NOT NULL
        CHECK (char_length(memory_id) BETWEEN 1 AND 128),
    current_revision_number integer NOT NULL
        CHECK (current_revision_number >= 0),
    current_revision_id text COLLATE "C"
        REFERENCES trace_backed_memory_v3_memory_revision.
            v3_memory_revision_proposals (revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    current_activation_id text COLLATE "C" UNIQUE
        REFERENCES trace_backed_memory_v3_memory_publication.
            v3_memory_revision_activations (activation_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    PRIMARY KEY (tenant_id, repository_id_key, memory_id),
    CHECK (
        (
            current_revision_number = 0
            AND current_revision_id IS NULL
            AND current_activation_id IS NULL
        )
        OR (
            current_revision_number >= 1
            AND current_revision_id IS NOT NULL
            AND current_activation_id IS NOT NULL
        )
    )
);

CREATE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL memory publication v3 record is immutable';
END
$$;

CREATE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_approval()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_memory_revision.
            v3_memory_revision_proposals AS proposal
        WHERE proposal.revision_id = NEW.revision_id
          AND proposal.memory_id = NEW.memory_id
          AND proposal.revision_number = NEW.revision_number
        FOR SHARE
    ) THEN
        RAISE EXCEPTION 'approval proposal mismatch';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_memory_publication.
            v3_memory_revision_approvals AS approval
        WHERE approval.approval_id = NEW.approval_id
          AND approval.revision_id = NEW.revision_id
          AND approval.tenant_id = NEW.tenant_id
          AND approval.repository_id_key = NEW.repository_id_key
          AND approval.memory_id = NEW.memory_id
          AND approval.revision_number = NEW.revision_number
        FOR SHARE
    ) THEN
        RAISE EXCEPTION 'activation approval mismatch';
    END IF;
    IF NEW.revision_number = 1 THEN
        IF NEW.previous_activation_id IS NOT NULL THEN
            RAISE EXCEPTION 'first activation must not have a predecessor';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_memory_publication.
            v3_memory_revision_activation_heads AS head
        WHERE head.tenant_id = NEW.tenant_id
          AND head.repository_id_key = NEW.repository_id_key
          AND head.memory_id = NEW.memory_id
          AND head.current_revision_number = NEW.revision_number - 1
          AND head.current_activation_id = NEW.previous_activation_id
        FOR UPDATE
    ) THEN
        RAISE EXCEPTION 'activation predecessor is not current head';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_head_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.current_revision_number <> 0
       OR NEW.current_revision_id IS NOT NULL
       OR NEW.current_activation_id IS NOT NULL THEN
        RAISE EXCEPTION 'activation head must start empty';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_head_advance()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id
       OR NEW.repository_id_key <> OLD.repository_id_key
       OR NEW.repository_id IS DISTINCT FROM OLD.repository_id
       OR NEW.memory_id <> OLD.memory_id
       OR NEW.current_revision_number <> OLD.current_revision_number + 1
       OR NOT EXISTS (
            SELECT 1
            FROM trace_backed_memory_v3_memory_publication.
                v3_memory_revision_activations AS activation
            WHERE activation.activation_id = NEW.current_activation_id
              AND activation.revision_id = NEW.current_revision_id
              AND activation.tenant_id = NEW.tenant_id
              AND activation.repository_id_key = NEW.repository_id_key
              AND activation.memory_id = NEW.memory_id
              AND activation.revision_number = NEW.current_revision_number
              AND activation.previous_activation_id
                  IS NOT DISTINCT FROM OLD.current_activation_id
            FOR SHARE
       ) THEN
        RAISE EXCEPTION 'activation head advance mismatch';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON ALL FUNCTIONS
    IN SCHEMA trace_backed_memory_v3_memory_publication FROM PUBLIC;

CREATE TRIGGER memory_publication_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_publication.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();
CREATE TRIGGER memory_publication_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_publication.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();

CREATE TRIGGER memory_publication_approval_validate
BEFORE INSERT
ON trace_backed_memory_v3_memory_publication.v3_memory_revision_approvals
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_approval();
CREATE TRIGGER memory_publication_approval_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_publication.v3_memory_revision_approvals
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();
CREATE TRIGGER memory_publication_approval_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_publication.v3_memory_revision_approvals
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();

CREATE TRIGGER memory_publication_activation_validate
BEFORE INSERT
ON trace_backed_memory_v3_memory_publication.v3_memory_revision_activations
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_activation();
CREATE TRIGGER memory_publication_activation_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_memory_publication.v3_memory_revision_activations
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();
CREATE TRIGGER memory_publication_activation_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_publication.v3_memory_revision_activations
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();

CREATE TRIGGER memory_publication_head_insert
BEFORE INSERT
ON trace_backed_memory_v3_memory_publication.
    v3_memory_revision_activation_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_head_insert();
CREATE TRIGGER memory_publication_head_advance
BEFORE UPDATE
ON trace_backed_memory_v3_memory_publication.
    v3_memory_revision_activation_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.validate_head_advance();
CREATE TRIGGER memory_publication_head_no_delete
BEFORE DELETE
ON trace_backed_memory_v3_memory_publication.
    v3_memory_revision_activation_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();
CREATE TRIGGER memory_publication_head_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_memory_publication.
    v3_memory_revision_activation_heads
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_memory_publication.reject_immutable_change();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON ALL TABLES IN SCHEMA trace_backed_memory_v3_memory_publication
    FROM PUBLIC;

COMMIT;
