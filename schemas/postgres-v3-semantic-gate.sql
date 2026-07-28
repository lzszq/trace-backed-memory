BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    evidence_version integer;
    evidence_contract text;
BEGIN
    SELECT active.schema_version,
           evidence.schema_version,
           evidence.contract_version
    INTO active_version, evidence_version, evidence_contract
    FROM public.trace_backed_memory_schema AS active
    CROSS JOIN trace_backed_memory_v3_gate_evidence.schema_metadata
        AS evidence
    WHERE active.singleton AND evidence.singleton = 1
    FOR UPDATE OF active, evidence;

    IF active_version IS NULL THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 requires active schema metadata';
    END IF;
    IF active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 requires active schema version 2, found %',
            active_version;
    END IF;
    IF evidence_version IS DISTINCT FROM 1
       OR evidence_contract IS DISTINCT FROM 'tbm.gate-evidence.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate v3 requires gate evidence v3';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_semantic_gate;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_semantic_gate FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_semantic_gate.schema_metadata (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.semantic-gate-attempt.v3')
);

INSERT INTO trace_backed_memory_v3_semantic_gate.schema_metadata (
    singleton, schema_version, contract_version
) VALUES (1, 1, 'tbm.semantic-gate-attempt.v3');

CREATE TABLE trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts (
    attempt_id text COLLATE "C" PRIMARY KEY
        CHECK (
            attempt_id ~ '^semantic_attempt_sha256_[0-9a-f]{64}$'
        ),
    session_id text COLLATE "C" NOT NULL
        CHECK (char_length(session_id) BETWEEN 1 AND 128),
    retrieval_snapshot_id text COLLATE "C" NOT NULL
        REFERENCES trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots (
            snapshot_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    system_gate_evaluation_id text COLLATE "C" NOT NULL
        REFERENCES trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations (
            evaluation_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 100),
    previous_attempt_id text COLLATE "C",
    status text COLLATE "C" NOT NULL
        CHECK (status IN ('succeeded', 'failed')),
    started_at text COLLATE "C" NOT NULL
        CHECK (char_length(started_at) BETWEEN 20 AND 32),
    finished_at text COLLATE "C" NOT NULL
        CHECK (char_length(finished_at) BETWEEN 20 AND 32),
    descriptor text COLLATE "C" NOT NULL
        CHECK (octet_length(descriptor) BETWEEN 1 AND 1048576),
    UNIQUE (system_gate_evaluation_id, sequence),
    UNIQUE (system_gate_evaluation_id, attempt_id),
    FOREIGN KEY (system_gate_evaluation_id, previous_attempt_id)
        REFERENCES
        trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts (
            system_gate_evaluation_id,
            attempt_id
        )
        ON UPDATE RESTRICT ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX v3_semantic_gate_attempts_session
    ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts (
        session_id, system_gate_evaluation_id, sequence
    );

CREATE TABLE
trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads (
    system_gate_evaluation_id text COLLATE "C" PRIMARY KEY
        REFERENCES trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations (
            evaluation_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    session_id text COLLATE "C" NOT NULL
        CHECK (char_length(session_id) BETWEEN 1 AND 128),
    retrieval_snapshot_id text COLLATE "C" NOT NULL
        REFERENCES trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots (
            snapshot_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    current_sequence integer NOT NULL
        CHECK (current_sequence BETWEEN 0 AND 100),
    current_attempt_id text COLLATE "C",
    CHECK (
        (current_sequence = 0 AND current_attempt_id IS NULL)
        OR (current_sequence > 0 AND current_attempt_id IS NOT NULL)
    ),
    FOREIGN KEY (system_gate_evaluation_id, current_attempt_id)
        REFERENCES
        trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts (
            system_gate_evaluation_id,
            attempt_id
        )
        ON UPDATE RESTRICT ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'PostgreSQL semantic Gate v3 records are immutable';
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate.validate_head_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    snapshot_session_id text;
    evaluation_session_id text;
    evaluation_snapshot_id text;
BEGIN
    SELECT snapshot.session_id
    INTO snapshot_session_id
    FROM trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots
        AS snapshot
    WHERE snapshot.snapshot_id = NEW.retrieval_snapshot_id
    FOR SHARE;

    SELECT evaluation.session_id, evaluation.retrieval_snapshot_id
    INTO evaluation_session_id, evaluation_snapshot_id
    FROM trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations
        AS evaluation
    WHERE evaluation.evaluation_id = NEW.system_gate_evaluation_id
    FOR SHARE;

    IF NEW.current_sequence <> 0
       OR NEW.current_attempt_id IS NOT NULL
       OR snapshot_session_id IS NULL
       OR evaluation_session_id IS NULL
       OR snapshot_session_id <> NEW.session_id
       OR evaluation_session_id <> NEW.session_id
       OR evaluation_snapshot_id <> NEW.retrieval_snapshot_id THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate head parent scope mismatch';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate.protect_head_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    attempt_session_id text;
    attempt_snapshot_id text;
    attempt_sequence integer;
    attempt_previous_id text;
BEGIN
    IF NEW.system_gate_evaluation_id <> OLD.system_gate_evaluation_id
       OR NEW.session_id <> OLD.session_id
       OR NEW.retrieval_snapshot_id <> OLD.retrieval_snapshot_id
       OR NEW.current_sequence <> OLD.current_sequence + 1
       OR NEW.current_attempt_id IS NULL THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate head advance is invalid';
    END IF;

    SELECT attempt.session_id,
           attempt.retrieval_snapshot_id,
           attempt.sequence,
           attempt.previous_attempt_id
    INTO attempt_session_id,
         attempt_snapshot_id,
         attempt_sequence,
         attempt_previous_id
    FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
        AS attempt
    WHERE attempt.system_gate_evaluation_id =
              NEW.system_gate_evaluation_id
      AND attempt.attempt_id = NEW.current_attempt_id
    FOR SHARE;

    IF attempt_sequence IS NULL
       OR attempt_session_id <> NEW.session_id
       OR attempt_snapshot_id <> NEW.retrieval_snapshot_id
       OR attempt_sequence <> NEW.current_sequence
       OR attempt_previous_id IS DISTINCT FROM OLD.current_attempt_id THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate head advance is invalid';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate.validate_attempt_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    snapshot_session_id text;
    evaluation_session_id text;
    evaluation_snapshot_id text;
    head_session_id text;
    head_snapshot_id text;
    head_sequence integer;
    head_attempt_id text;
BEGIN
    SELECT snapshot.session_id
    INTO snapshot_session_id
    FROM trace_backed_memory_v3_gate_evidence.v3_retrieval_snapshots
        AS snapshot
    WHERE snapshot.snapshot_id = NEW.retrieval_snapshot_id
    FOR SHARE;

    SELECT evaluation.session_id, evaluation.retrieval_snapshot_id
    INTO evaluation_session_id, evaluation_snapshot_id
    FROM trace_backed_memory_v3_gate_evidence.v3_system_gate_evaluations
        AS evaluation
    WHERE evaluation.evaluation_id = NEW.system_gate_evaluation_id
    FOR SHARE;

    SELECT head.session_id,
           head.retrieval_snapshot_id,
           head.current_sequence,
           head.current_attempt_id
    INTO head_session_id,
         head_snapshot_id,
         head_sequence,
         head_attempt_id
    FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
        AS head
    WHERE head.system_gate_evaluation_id =
              NEW.system_gate_evaluation_id
    FOR UPDATE;

    IF snapshot_session_id IS NULL
       OR evaluation_session_id IS NULL
       OR head_sequence IS NULL
       OR snapshot_session_id <> NEW.session_id
       OR evaluation_session_id <> NEW.session_id
       OR head_session_id <> NEW.session_id
       OR evaluation_snapshot_id <> NEW.retrieval_snapshot_id
       OR head_snapshot_id <> NEW.retrieval_snapshot_id
       OR NEW.sequence <> head_sequence + 1
       OR NEW.previous_attempt_id IS DISTINCT FROM head_attempt_id THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate attempt does not extend current head';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_semantic_gate.validate_chain_consistency()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    target_evaluation_id text;
    head_sequence integer;
    head_attempt_id text;
    attempt_count integer;
    chain_count integer;
    invalid_link_count integer;
BEGIN
    target_evaluation_id := NEW.system_gate_evaluation_id;

    SELECT head.current_sequence, head.current_attempt_id
    INTO head_sequence, head_attempt_id
    FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
        AS head
    WHERE head.system_gate_evaluation_id = target_evaluation_id;

    SELECT count(*)
    INTO attempt_count
    FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
        AS attempt
    WHERE attempt.system_gate_evaluation_id = target_evaluation_id;

    WITH RECURSIVE chain AS (
        SELECT attempt.attempt_id,
               attempt.previous_attempt_id,
               attempt.sequence,
               ARRAY[attempt.attempt_id]::text[] AS path
        FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
            AS attempt
        WHERE attempt.system_gate_evaluation_id = target_evaluation_id
          AND attempt.attempt_id = head_attempt_id
        UNION ALL
        SELECT parent.attempt_id,
               parent.previous_attempt_id,
               parent.sequence,
               child.path || parent.attempt_id
        FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
            AS parent
        JOIN chain AS child
          ON parent.system_gate_evaluation_id = target_evaluation_id
         AND parent.attempt_id = child.previous_attempt_id
        WHERE NOT parent.attempt_id = ANY(child.path)
    )
    SELECT count(*)
    INTO chain_count
    FROM chain;

    SELECT count(*)
    INTO invalid_link_count
    FROM trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
        AS child
    LEFT JOIN
        trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
        AS parent
      ON parent.system_gate_evaluation_id =
             child.system_gate_evaluation_id
     AND parent.attempt_id = child.previous_attempt_id
    WHERE child.system_gate_evaluation_id = target_evaluation_id
      AND (
          (child.sequence = 1 AND child.previous_attempt_id IS NOT NULL)
          OR (
              child.sequence > 1
              AND (
                  parent.attempt_id IS NULL
                  OR parent.sequence <> child.sequence - 1
              )
          )
      );

    IF head_sequence IS NULL
       OR head_sequence < 1
       OR head_attempt_id IS NULL
       OR attempt_count <> head_sequence
       OR chain_count <> head_sequence
       OR invalid_link_count <> 0 THEN
        RAISE EXCEPTION
            'PostgreSQL semantic Gate chain consistency mismatch';
    END IF;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate.reject_immutable_change()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_head_insert()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate.protect_head_update()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_attempt_insert()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_chain_consistency()
    FROM PUBLIC;

CREATE TRIGGER semantic_gate_metadata_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_semantic_gate.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.reject_immutable_change();

CREATE TRIGGER semantic_gate_metadata_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_semantic_gate.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.reject_immutable_change();

CREATE TRIGGER semantic_gate_head_insert
BEFORE INSERT
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_head_insert();

CREATE TRIGGER semantic_gate_head_update
BEFORE UPDATE
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.protect_head_update();

CREATE TRIGGER semantic_gate_head_immutable_delete
BEFORE DELETE
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.reject_immutable_change();

CREATE TRIGGER semantic_gate_head_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.reject_immutable_change();

CREATE CONSTRAINT TRIGGER semantic_gate_head_consistency
AFTER INSERT OR UPDATE
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_chain_consistency();

CREATE TRIGGER semantic_gate_attempt_insert
BEFORE INSERT
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_attempt_insert();

CREATE TRIGGER semantic_gate_attempt_immutable
BEFORE UPDATE OR DELETE
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.reject_immutable_change();

CREATE TRIGGER semantic_gate_attempt_no_truncate
BEFORE TRUNCATE
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
FOR EACH STATEMENT EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.reject_immutable_change();

CREATE CONSTRAINT TRIGGER semantic_gate_attempt_consistency
AFTER INSERT
ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
    trace_backed_memory_v3_semantic_gate.validate_chain_consistency();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_semantic_gate.schema_metadata
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempt_heads
    FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON trace_backed_memory_v3_semantic_gate.v3_semantic_gate_attempts
    FROM PUBLIC;

COMMIT;
