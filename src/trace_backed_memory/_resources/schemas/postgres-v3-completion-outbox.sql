BEGIN;

SET LOCAL search_path = pg_catalog;

DO $$
DECLARE
    active_version integer;
    gate_schema_version integer;
    gate_contract_version text;
    outcome_schema_version integer;
    outcome_contract_version text;
BEGIN
    SELECT schema_version
    INTO active_version
    FROM public.trace_backed_memory_schema
    WHERE singleton
    FOR UPDATE;

    IF active_version IS NULL OR active_version <> 2 THEN
        RAISE EXCEPTION
            'PostgreSQL completion outbox v3 requires active schema version 2';
    END IF;

    SELECT schema_version, contract_version
    INTO gate_schema_version, gate_contract_version
    FROM trace_backed_memory_v3_gate_session.schema_metadata
    WHERE singleton
    FOR UPDATE;

    IF gate_schema_version IS NULL
       OR gate_schema_version <> 1
       OR gate_contract_version <> 'tbm.gate-session.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL completion outbox v3 requires GateSession schema version 1';
    END IF;

    SELECT schema_version, contract_version
    INTO outcome_schema_version, outcome_contract_version
    FROM trace_backed_memory_v3_outcome.schema_metadata
    WHERE singleton
    FOR UPDATE;

    IF outcome_schema_version IS NULL
       OR outcome_schema_version <> 1
       OR outcome_contract_version <> 'tbm.run-outcome.v3' THEN
        RAISE EXCEPTION
            'PostgreSQL completion outbox v3 requires RunOutcome schema version 1';
    END IF;
END
$$;

CREATE SCHEMA trace_backed_memory_v3_completion_outbox;
REVOKE ALL ON SCHEMA trace_backed_memory_v3_completion_outbox FROM PUBLIC;

CREATE TABLE trace_backed_memory_v3_completion_outbox.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    contract_version text COLLATE "C" NOT NULL
        CHECK (contract_version = 'tbm.completion-outbox.v3')
);

INSERT INTO trace_backed_memory_v3_completion_outbox.schema_metadata (
    singleton,
    schema_version,
    contract_version
) VALUES (TRUE, 1, 'tbm.completion-outbox.v3');

CREATE TABLE trace_backed_memory_v3_completion_outbox.events (
    event_id text COLLATE "C" PRIMARY KEY CHECK (
        event_id ~ '^completion_outbox_event_sha256_[0-9a-f]{64}$'
    ),
    event_type text COLLATE "C" NOT NULL
        CHECK (event_type = 'execution_completed'),
    tenant_id text COLLATE "C" NOT NULL
        CHECK (char_length(tenant_id) BETWEEN 1 AND 128),
    repository_id text COLLATE "C" NOT NULL
        CHECK (char_length(repository_id) BETWEEN 1 AND 128),
    session_id text COLLATE "C" NOT NULL
        CHECK (char_length(session_id) BETWEEN 1 AND 128),
    trace_id text COLLATE "C" NOT NULL
        CHECK (char_length(trace_id) BETWEEN 1 AND 128),
    run_id text COLLATE "C" NOT NULL
        CHECK (char_length(run_id) BETWEEN 1 AND 128),
    usage_decision_id text COLLATE "C" NOT NULL
        CHECK (char_length(usage_decision_id) BETWEEN 1 AND 128),
    run_outcome_id text COLLATE "C" NOT NULL UNIQUE CHECK (
        run_outcome_id ~ '^run_outcome_sha256_[0-9a-f]{64}$'
    ),
    outcome_descriptor_sha256 text COLLATE "C" NOT NULL CHECK (
        outcome_descriptor_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    occurred_at timestamp(6) with time zone NOT NULL,
    descriptor text COLLATE "C" NOT NULL CHECK (
        octet_length(descriptor) BETWEEN 1 AND 1048576
    ),
    CONSTRAINT events_session_fkey
        FOREIGN KEY (session_id)
        REFERENCES
            trace_backed_memory_v3_gate_session.gate_session_heads (
                session_id
            )
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT events_outcome_fkey
        FOREIGN KEY (run_outcome_id)
        REFERENCES trace_backed_memory_v3_outcome.run_outcomes (
            run_outcome_id
        )
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE
trace_backed_memory_v3_completion_outbox.delivery_revisions (
    event_id text COLLATE "C" NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    delivery_revision_id text COLLATE "C" NOT NULL UNIQUE CHECK (
        delivery_revision_id
            ~ '^completion_outbox_delivery_sha256_[0-9a-f]{64}$'
    ),
    status text COLLATE "C" NOT NULL CHECK (
        status IN (
            'pending', 'leased', 'retry_wait', 'delivered', 'dead_letter'
        )
    ),
    attempt_count integer NOT NULL CHECK (
        attempt_count BETWEEN 0 AND 1000
    ),
    updated_at timestamp(6) with time zone NOT NULL,
    available_at timestamp(6) with time zone,
    worker_id text COLLATE "C" CHECK (
        worker_id IS NULL OR char_length(worker_id) BETWEEN 1 AND 128
    ),
    lease_expires_at timestamp(6) with time zone,
    delivered_at timestamp(6) with time zone,
    last_error_code text COLLATE "C" CHECK (
        last_error_code IS NULL
        OR char_length(last_error_code) BETWEEN 1 AND 256
    ),
    response_sha256 text COLLATE "C" CHECK (
        response_sha256 IS NULL
        OR response_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    descriptor text COLLATE "C" NOT NULL CHECK (
        octet_length(descriptor) BETWEEN 1 AND 1048576
    ),
    PRIMARY KEY (event_id, version),
    CONSTRAINT delivery_revisions_event_fkey
        FOREIGN KEY (event_id)
        REFERENCES trace_backed_memory_v3_completion_outbox.events (
            event_id
        )
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT delivery_revisions_state_shape CHECK (
        (
            status = 'pending'
            AND version = 1
            AND attempt_count = 0
            AND available_at IS NOT NULL
            AND worker_id IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND last_error_code IS NULL
            AND response_sha256 IS NULL
        )
        OR (
            status = 'leased'
            AND attempt_count >= 1
            AND available_at IS NULL
            AND worker_id IS NOT NULL
            AND lease_expires_at > updated_at
            AND lease_expires_at <= updated_at + interval '1 day'
            AND delivered_at IS NULL
            AND last_error_code IS NULL
            AND response_sha256 IS NULL
        )
        OR (
            status = 'retry_wait'
            AND attempt_count >= 1
            AND available_at > updated_at
            AND available_at <= updated_at + interval '7 days'
            AND worker_id IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND last_error_code IS NOT NULL
            AND response_sha256 IS NULL
        )
        OR (
            status = 'delivered'
            AND attempt_count >= 1
            AND available_at IS NULL
            AND worker_id IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at = updated_at
            AND last_error_code IS NULL
        )
        OR (
            status = 'dead_letter'
            AND attempt_count >= 1
            AND available_at IS NULL
            AND worker_id IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND last_error_code IS NOT NULL
            AND response_sha256 IS NULL
        )
    )
);

CREATE TABLE trace_backed_memory_v3_completion_outbox.delivery_heads (
    event_id text COLLATE "C" PRIMARY KEY,
    current_version integer NOT NULL CHECK (current_version >= 1),
    CONSTRAINT delivery_heads_revision_fkey
        FOREIGN KEY (event_id, current_version)
        REFERENCES
            trace_backed_memory_v3_completion_outbox.delivery_revisions (
                event_id, version
            )
        ON UPDATE RESTRICT ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX completion_outbox_due
ON trace_backed_memory_v3_completion_outbox.delivery_revisions (
    status,
    available_at,
    lease_expires_at,
    event_id
);

CREATE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'PostgreSQL completion outbox records are immutable';
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_event_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    parsed jsonb;
    canonical_occurred_at text;
    canonical_payload text;
    canonical_descriptor text;
    expected_event_id text;
    fractional_microseconds bigint;
    outcome_descriptor text;
    gate_tenant_id text;
    gate_repository_id text;
    gate_trace_id text;
    gate_run_id text;
    gate_usage_decision_id text;
    gate_outcome_id text;
    gate_status text;
    gate_updated_at timestamp(6) with time zone;
    strip_characters text :=
        U&'\0009\000A\000B\000C\000D' ||
        U&'\001C\001D\001E\001F \0085\00A0\1680' ||
        U&'\2000\2001\2002\2003\2004\2005\2006' ||
        U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000';
BEGIN
    BEGIN
        parsed := NEW.descriptor::jsonb;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'invalid PostgreSQL completion outbox event';
    END;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(
            ARRAY[
                NEW.tenant_id,
                NEW.repository_id,
                NEW.session_id,
                NEW.trace_id,
                NEW.run_id,
                NEW.usage_decision_id
            ]
        ) AS identifier(value)
        WHERE identifier.value IS DISTINCT FROM
            pg_catalog.btrim(identifier.value, strip_characters)
           OR EXISTS (
                SELECT 1
                FROM pg_catalog.regexp_split_to_table(
                    identifier.value, ''
                ) AS character(value)
                WHERE pg_catalog.ascii(character.value) < 32
                   OR pg_catalog.ascii(character.value) = 127
           )
    ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL completion outbox event identifier';
    END IF;

    fractional_microseconds :=
        pg_catalog.mod(
            pg_catalog.date_part('microseconds', NEW.occurred_at)::bigint,
            1000000
        );
    IF pg_catalog.date_part(
        'year', NEW.occurred_at AT TIME ZONE 'UTC'
    ) NOT BETWEEN 1 AND 9999 THEN
        RAISE EXCEPTION 'invalid PostgreSQL completion outbox timestamp';
    END IF;
    canonical_occurred_at :=
        pg_catalog.to_char(
            NEW.occurred_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS'
        ) ||
        CASE
            WHEN fractional_microseconds = 0 THEN ''
            ELSE '.' || pg_catalog.lpad(
                fractional_microseconds::text, 6, '0'
            )
        END || 'Z';
    canonical_payload :=
        '{"contract_version":"tbm.completion-outbox-event.v3"' ||
        ',"event_type":' || pg_catalog.to_json(NEW.event_type)::text ||
        ',"occurred_at":' ||
            pg_catalog.to_json(canonical_occurred_at)::text ||
        ',"outcome_descriptor_sha256":' ||
            pg_catalog.to_json(NEW.outcome_descriptor_sha256)::text ||
        ',"repository_id":' ||
            pg_catalog.to_json(NEW.repository_id)::text ||
        ',"run_id":' || pg_catalog.to_json(NEW.run_id)::text ||
        ',"run_outcome_id":' ||
            pg_catalog.to_json(NEW.run_outcome_id)::text ||
        ',"session_id":' || pg_catalog.to_json(NEW.session_id)::text ||
        ',"tenant_id":' || pg_catalog.to_json(NEW.tenant_id)::text ||
        ',"trace_id":' || pg_catalog.to_json(NEW.trace_id)::text ||
        ',"usage_decision_id":' ||
            pg_catalog.to_json(NEW.usage_decision_id)::text ||
        '}';
    expected_event_id :=
        'completion_outbox_event_sha256_' ||
        pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(canonical_payload, 'UTF8')
            ),
            'hex'
        );
    canonical_descriptor :=
        '{"contract_version":"tbm.completion-outbox-event.v3"' ||
        ',"event_id":' || pg_catalog.to_json(NEW.event_id)::text ||
        ',"event_type":' || pg_catalog.to_json(NEW.event_type)::text ||
        ',"occurred_at":' ||
            pg_catalog.to_json(canonical_occurred_at)::text ||
        ',"outcome_descriptor_sha256":' ||
            pg_catalog.to_json(NEW.outcome_descriptor_sha256)::text ||
        ',"repository_id":' ||
            pg_catalog.to_json(NEW.repository_id)::text ||
        ',"run_id":' || pg_catalog.to_json(NEW.run_id)::text ||
        ',"run_outcome_id":' ||
            pg_catalog.to_json(NEW.run_outcome_id)::text ||
        ',"session_id":' || pg_catalog.to_json(NEW.session_id)::text ||
        ',"tenant_id":' || pg_catalog.to_json(NEW.tenant_id)::text ||
        ',"trace_id":' || pg_catalog.to_json(NEW.trace_id)::text ||
        ',"usage_decision_id":' ||
            pg_catalog.to_json(NEW.usage_decision_id)::text ||
        '}';

    IF parsed IS NULL
       OR pg_catalog.jsonb_typeof(parsed) <> 'object'
       OR NEW.event_id IS DISTINCT FROM expected_event_id
       OR NEW.descriptor IS DISTINCT FROM canonical_descriptor THEN
        RAISE EXCEPTION 'invalid PostgreSQL completion outbox event';
    END IF;

    SELECT outcome.descriptor
    INTO outcome_descriptor
    FROM trace_backed_memory_v3_outcome.run_outcomes AS outcome
    WHERE outcome.run_outcome_id = NEW.run_outcome_id;

    SELECT head.tenant_id,
           head.repository_id,
           head.trace_id,
           head.run_id,
           revision.status,
           revision.updated_at,
           revision.payload::jsonb ->> 'usage_decision_id',
           revision.payload::jsonb ->> 'run_outcome_id'
    INTO gate_tenant_id,
         gate_repository_id,
         gate_trace_id,
         gate_run_id,
         gate_status,
         gate_updated_at,
         gate_usage_decision_id,
         gate_outcome_id
    FROM trace_backed_memory_v3_gate_session.gate_session_heads AS head
    JOIN trace_backed_memory_v3_gate_session.gate_session_revisions AS revision
      ON revision.session_id = head.session_id
     AND revision.version = head.current_version
    WHERE head.session_id = NEW.session_id;

    IF outcome_descriptor IS NULL
       OR gate_status IS DISTINCT FROM 'completed'
       OR gate_tenant_id IS DISTINCT FROM NEW.tenant_id
       OR gate_repository_id IS DISTINCT FROM NEW.repository_id
       OR gate_trace_id IS DISTINCT FROM NEW.trace_id
       OR gate_run_id IS DISTINCT FROM NEW.run_id
       OR gate_usage_decision_id IS DISTINCT FROM NEW.usage_decision_id
       OR gate_outcome_id IS DISTINCT FROM NEW.run_outcome_id
       OR gate_updated_at IS DISTINCT FROM NEW.occurred_at
       OR NEW.outcome_descriptor_sha256 IS DISTINCT FROM
            'sha256:' || pg_catalog.encode(
                pg_catalog.sha256(
                    pg_catalog.convert_to(outcome_descriptor, 'UTF8')
                ),
                'hex'
            ) THEN
        RAISE EXCEPTION
            'PostgreSQL completion outbox event linkage is invalid';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_delivery_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    parsed jsonb;
    canonical_updated_at text;
    canonical_available_at text;
    canonical_lease_expires_at text;
    canonical_delivered_at text;
    canonical_payload text;
    canonical_descriptor text;
    expected_delivery_id text;
    fractional_microseconds bigint;
    strip_characters text :=
        U&'\0009\000A\000B\000C\000D' ||
        U&'\001C\001D\001E\001F \0085\00A0\1680' ||
        U&'\2000\2001\2002\2003\2004\2005\2006' ||
        U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000';
    previous
        trace_backed_memory_v3_completion_outbox.delivery_revisions%ROWTYPE;
    head_version integer;
BEGIN
    BEGIN
        parsed := NEW.descriptor::jsonb;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'invalid PostgreSQL completion outbox delivery';
    END;

    IF (
        NEW.worker_id IS NOT NULL
        AND (
            NEW.worker_id IS DISTINCT FROM
                pg_catalog.btrim(NEW.worker_id, strip_characters)
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.regexp_split_to_table(
                    NEW.worker_id, ''
                ) AS character(value)
                WHERE pg_catalog.ascii(character.value) < 32
                   OR pg_catalog.ascii(character.value) = 127
            )
        )
    ) OR (
        NEW.last_error_code IS NOT NULL
        AND (
            NEW.last_error_code IS DISTINCT FROM
                pg_catalog.btrim(NEW.last_error_code, strip_characters)
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.regexp_split_to_table(
                    NEW.last_error_code, ''
                ) AS character(value)
                WHERE pg_catalog.ascii(character.value) < 32
                   OR pg_catalog.ascii(character.value) = 127
            )
        )
    ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL completion outbox delivery identifier';
    END IF;

    fractional_microseconds := pg_catalog.mod(
        pg_catalog.date_part('microseconds', NEW.updated_at)::bigint,
        1000000
    );
    IF pg_catalog.date_part(
        'year', NEW.updated_at AT TIME ZONE 'UTC'
    ) NOT BETWEEN 1 AND 9999
       OR (
            NEW.available_at IS NOT NULL
            AND pg_catalog.date_part(
                'year', NEW.available_at AT TIME ZONE 'UTC'
            ) NOT BETWEEN 1 AND 9999
       )
       OR (
            NEW.lease_expires_at IS NOT NULL
            AND pg_catalog.date_part(
                'year', NEW.lease_expires_at AT TIME ZONE 'UTC'
            ) NOT BETWEEN 1 AND 9999
       )
       OR (
            NEW.delivered_at IS NOT NULL
            AND pg_catalog.date_part(
                'year', NEW.delivered_at AT TIME ZONE 'UTC'
            ) NOT BETWEEN 1 AND 9999
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL completion outbox delivery timestamp';
    END IF;
    canonical_updated_at := pg_catalog.to_char(
        NEW.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS'
    ) || CASE WHEN fractional_microseconds = 0 THEN '' ELSE
        '.' || pg_catalog.lpad(fractional_microseconds::text, 6, '0')
    END || 'Z';

    IF NEW.available_at IS NOT NULL THEN
        fractional_microseconds := pg_catalog.mod(
            pg_catalog.date_part('microseconds', NEW.available_at)::bigint,
            1000000
        );
        canonical_available_at := pg_catalog.to_char(
            NEW.available_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS'
        ) || CASE WHEN fractional_microseconds = 0 THEN '' ELSE
            '.' || pg_catalog.lpad(fractional_microseconds::text, 6, '0')
        END || 'Z';
    END IF;
    IF NEW.lease_expires_at IS NOT NULL THEN
        fractional_microseconds := pg_catalog.mod(
            pg_catalog.date_part(
                'microseconds', NEW.lease_expires_at
            )::bigint,
            1000000
        );
        canonical_lease_expires_at := pg_catalog.to_char(
            NEW.lease_expires_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS'
        ) || CASE WHEN fractional_microseconds = 0 THEN '' ELSE
            '.' || pg_catalog.lpad(fractional_microseconds::text, 6, '0')
        END || 'Z';
    END IF;
    IF NEW.delivered_at IS NOT NULL THEN
        fractional_microseconds := pg_catalog.mod(
            pg_catalog.date_part('microseconds', NEW.delivered_at)::bigint,
            1000000
        );
        canonical_delivered_at := pg_catalog.to_char(
            NEW.delivered_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS'
        ) || CASE WHEN fractional_microseconds = 0 THEN '' ELSE
            '.' || pg_catalog.lpad(fractional_microseconds::text, 6, '0')
        END || 'Z';
    END IF;

    canonical_payload :=
        '{"attempt_count":' || NEW.attempt_count::text ||
        ',"available_at":' ||
            COALESCE(pg_catalog.to_json(canonical_available_at)::text, 'null') ||
        ',"contract_version":"tbm.completion-outbox-delivery.v3"' ||
        ',"delivered_at":' ||
            COALESCE(pg_catalog.to_json(canonical_delivered_at)::text, 'null') ||
        ',"event_id":' || pg_catalog.to_json(NEW.event_id)::text ||
        ',"last_error_code":' ||
            COALESCE(pg_catalog.to_json(NEW.last_error_code)::text, 'null') ||
        ',"lease_expires_at":' ||
            COALESCE(
                pg_catalog.to_json(canonical_lease_expires_at)::text,
                'null'
            ) ||
        ',"response_sha256":' ||
            COALESCE(pg_catalog.to_json(NEW.response_sha256)::text, 'null') ||
        ',"status":' || pg_catalog.to_json(NEW.status)::text ||
        ',"updated_at":' ||
            pg_catalog.to_json(canonical_updated_at)::text ||
        ',"version":' || NEW.version::text ||
        ',"worker_id":' ||
            COALESCE(pg_catalog.to_json(NEW.worker_id)::text, 'null') ||
        '}';
    expected_delivery_id :=
        'completion_outbox_delivery_sha256_' ||
        pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(canonical_payload, 'UTF8')
            ),
            'hex'
        );
    canonical_descriptor :=
        '{"attempt_count":' || NEW.attempt_count::text ||
        ',"available_at":' ||
            COALESCE(pg_catalog.to_json(canonical_available_at)::text, 'null') ||
        ',"contract_version":"tbm.completion-outbox-delivery.v3"' ||
        ',"delivered_at":' ||
            COALESCE(pg_catalog.to_json(canonical_delivered_at)::text, 'null') ||
        ',"delivery_revision_id":' ||
            pg_catalog.to_json(NEW.delivery_revision_id)::text ||
        ',"event_id":' || pg_catalog.to_json(NEW.event_id)::text ||
        ',"last_error_code":' ||
            COALESCE(pg_catalog.to_json(NEW.last_error_code)::text, 'null') ||
        ',"lease_expires_at":' ||
            COALESCE(
                pg_catalog.to_json(canonical_lease_expires_at)::text,
                'null'
            ) ||
        ',"response_sha256":' ||
            COALESCE(pg_catalog.to_json(NEW.response_sha256)::text, 'null') ||
        ',"status":' || pg_catalog.to_json(NEW.status)::text ||
        ',"updated_at":' ||
            pg_catalog.to_json(canonical_updated_at)::text ||
        ',"version":' || NEW.version::text ||
        ',"worker_id":' ||
            COALESCE(pg_catalog.to_json(NEW.worker_id)::text, 'null') ||
        '}';

    IF parsed IS NULL
       OR pg_catalog.jsonb_typeof(parsed) <> 'object'
       OR NEW.delivery_revision_id IS DISTINCT FROM expected_delivery_id
       OR NEW.descriptor IS DISTINCT FROM canonical_descriptor THEN
        RAISE EXCEPTION 'invalid PostgreSQL completion outbox delivery';
    END IF;

    IF NEW.version = 1 THEN
        IF NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'invalid initial completion outbox delivery';
        END IF;
        RETURN NEW;
    END IF;

    SELECT current_version
    INTO head_version
    FROM trace_backed_memory_v3_completion_outbox.delivery_heads
    WHERE event_id = NEW.event_id
    FOR UPDATE;
    IF head_version IS NULL OR head_version <> NEW.version - 1 THEN
        RAISE EXCEPTION
            'completion outbox delivery does not follow the current head';
    END IF;

    SELECT *
    INTO previous
    FROM trace_backed_memory_v3_completion_outbox.delivery_revisions
    WHERE event_id = NEW.event_id
      AND version = NEW.version - 1;

    IF previous.event_id IS NULL
       OR NEW.updated_at < previous.updated_at
       OR previous.status IN ('delivered', 'dead_letter')
       OR (
            previous.status IN ('pending', 'retry_wait')
            AND (
                NEW.status <> 'leased'
                OR NEW.attempt_count <> previous.attempt_count + 1
            )
       )
       OR (
            previous.status = 'leased'
            AND (
                NEW.status NOT IN (
                    'leased', 'retry_wait', 'delivered', 'dead_letter'
                )
                OR (
                    NEW.status = 'leased'
                    AND NEW.attempt_count <> previous.attempt_count + 1
                )
                OR (
                    NEW.status <> 'leased'
                    AND NEW.attempt_count <> previous.attempt_count
                )
            )
       ) THEN
        RAISE EXCEPTION
            'invalid PostgreSQL completion outbox delivery transition';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_delivery_head_consistency()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_completion_outbox.delivery_heads
        WHERE event_id = NEW.event_id
          AND current_version = NEW.version
    ) THEN
        RAISE EXCEPTION
            'completion outbox delivery revision is not the committed head';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_head_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.current_version <> 1 THEN
        RAISE EXCEPTION 'invalid completion outbox delivery head';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_completion_outbox.delivery_revisions
        WHERE event_id = NEW.event_id
          AND version = 1
          AND status = 'pending'
    ) THEN
        RAISE EXCEPTION 'completion outbox delivery head has no initial revision';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_head_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.current_version <> OLD.current_version + 1 THEN
        RAISE EXCEPTION 'invalid completion outbox delivery head advance';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM trace_backed_memory_v3_completion_outbox.delivery_revisions
        WHERE event_id = NEW.event_id
          AND version = NEW.current_version
    ) THEN
        RAISE EXCEPTION 'completion outbox delivery head revision is missing';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER completion_outbox_metadata_immutable
BEFORE UPDATE OR DELETE ON
trace_backed_memory_v3_completion_outbox.schema_metadata
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();
CREATE TRIGGER completion_outbox_metadata_no_truncate
BEFORE TRUNCATE ON
trace_backed_memory_v3_completion_outbox.schema_metadata
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();

CREATE TRIGGER completion_outbox_events_validate_insert
BEFORE INSERT ON trace_backed_memory_v3_completion_outbox.events
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_event_insert();
CREATE TRIGGER completion_outbox_events_immutable
BEFORE UPDATE OR DELETE ON
trace_backed_memory_v3_completion_outbox.events
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();
CREATE TRIGGER completion_outbox_events_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_completion_outbox.events
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();

CREATE TRIGGER completion_outbox_delivery_validate_insert
BEFORE INSERT ON
trace_backed_memory_v3_completion_outbox.delivery_revisions
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_delivery_insert();
CREATE CONSTRAINT TRIGGER completion_outbox_delivery_head_consistency
AFTER INSERT ON
trace_backed_memory_v3_completion_outbox.delivery_revisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_delivery_head_consistency();
CREATE TRIGGER completion_outbox_delivery_immutable
BEFORE UPDATE OR DELETE ON
trace_backed_memory_v3_completion_outbox.delivery_revisions
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();
CREATE TRIGGER completion_outbox_delivery_no_truncate
BEFORE TRUNCATE ON
trace_backed_memory_v3_completion_outbox.delivery_revisions
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();

CREATE TRIGGER completion_outbox_heads_validate_insert
BEFORE INSERT ON trace_backed_memory_v3_completion_outbox.delivery_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_head_insert();
CREATE TRIGGER completion_outbox_heads_validate_update
BEFORE UPDATE ON trace_backed_memory_v3_completion_outbox.delivery_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.validate_head_update();
CREATE TRIGGER completion_outbox_heads_no_delete
BEFORE DELETE ON trace_backed_memory_v3_completion_outbox.delivery_heads
FOR EACH ROW EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();
CREATE TRIGGER completion_outbox_heads_no_truncate
BEFORE TRUNCATE ON trace_backed_memory_v3_completion_outbox.delivery_heads
FOR EACH STATEMENT EXECUTE FUNCTION
trace_backed_memory_v3_completion_outbox.reject_immutable_change();

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA
    trace_backed_memory_v3_completion_outbox FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON ALL TABLES IN SCHEMA trace_backed_memory_v3_completion_outbox
FROM PUBLIC;

COMMIT;
