PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

BEGIN IMMEDIATE;

CREATE TEMP TABLE tbm_gate_session_timestamp_hotfix_guard (
    valid INTEGER NOT NULL CHECK (valid = 1)
);

INSERT INTO tbm_gate_session_timestamp_hotfix_guard (valid)
SELECT CASE WHEN COUNT(*) = 1 THEN 1 ELSE 0 END
FROM trace_backed_memory_v3_bundle_schema AS bundle
JOIN trace_backed_memory_v3_gate_session_schema AS gate_session
  ON gate_session.singleton = bundle.singleton
WHERE bundle.singleton = 1
  AND bundle.schema_version = 1
  AND bundle.contract_version = 'tbm.sqlite-bundle.v3'
  AND bundle.component_set_sha256 IN (
      'sha256:3b845f08d52c83705b55cb369758db23a344d9324a006d6541cae554bf921381',
      'sha256:323d537feef043b0dff54194f419f8b177096fbfe8f72c1f7e66ca5dd98ce42b'
  )
  AND bundle.catalog_sha256 =
      'sha256:0a86379bdf0ddc8db146e410297d5b2d05e418b2983f02a4c9845a8e3c61273b'
  AND gate_session.schema_version = 1
  AND gate_session.contract_version = 'tbm.gate-session.v3';

DROP TRIGGER gate_session_revisions_validate_insert;

CREATE TRIGGER gate_session_revisions_validate_insert
BEFORE INSERT ON gate_session_revisions
FOR EACH ROW
WHEN
    NEW.version <> COALESCE(
        (
            SELECT MAX(previous.version) + 1
            FROM gate_session_revisions AS previous
            WHERE previous.session_id = NEW.session_id
        ),
        1
    )
    OR (
        NEW.version = 1
        AND NEW.status <> 'created'
    )
    OR (
        NEW.version > 1
        AND NOT EXISTS (
            SELECT 1
            FROM gate_session_revisions AS previous
            JOIN gate_session_heads AS head
              ON head.session_id = previous.session_id
            WHERE previous.session_id = NEW.session_id
              AND previous.version = NEW.version - 1
              AND head.current_version = previous.version
              AND (
                  (
                      length(previous.updated_at) = 20
                      AND substr(previous.updated_at, 20, 1) = 'Z'
                  )
                  OR (
                      length(previous.updated_at) = 27
                      AND substr(previous.updated_at, 20, 1) = '.'
                      AND substr(previous.updated_at, 21, 6)
                          NOT GLOB '*[^0-9]*'
                      AND substr(previous.updated_at, 27, 1) = 'Z'
                  )
              )
              AND (
                  (
                      length(NEW.updated_at) = 20
                      AND substr(NEW.updated_at, 20, 1) = 'Z'
                  )
                  OR (
                      length(NEW.updated_at) = 27
                      AND substr(NEW.updated_at, 20, 1) = '.'
                      AND substr(NEW.updated_at, 21, 6)
                          NOT GLOB '*[^0-9]*'
                      AND substr(NEW.updated_at, 27, 1) = 'Z'
                  )
              )
              AND (
                  substr(previous.updated_at, 1, 19)
                      < substr(NEW.updated_at, 1, 19)
                  OR (
                      substr(previous.updated_at, 1, 19)
                          = substr(NEW.updated_at, 1, 19)
                      AND CASE
                          WHEN length(previous.updated_at) = 20 THEN 0
                          ELSE CAST(
                              substr(previous.updated_at, 21, 6) AS INTEGER
                          )
                      END < CASE
                          WHEN length(NEW.updated_at) = 20 THEN 0
                          ELSE CAST(
                              substr(NEW.updated_at, 21, 6) AS INTEGER
                          )
                      END
                  )
              )
              AND previous.expires_at = NEW.expires_at
              AND (
                  (previous.status = 'created'
                      AND NEW.status IN ('prepared', 'canceled'))
                  OR (previous.status = 'prepared'
                      AND NEW.status IN (
                          'awaiting_decision',
                          'canceled',
                          'expired'
                      ))
                  OR (previous.status = 'awaiting_decision'
                      AND NEW.status IN (
                          'decided',
                          'canceled',
                          'expired'
                      ))
                  OR (previous.status = 'decided'
                      AND NEW.status = 'finalized')
                  OR (previous.status = 'finalized'
                      AND NEW.status = 'executing')
                  OR (previous.status = 'executing'
                      AND NEW.status IN ('completed', 'abandoned'))
                  OR (
                      previous.status IN (
                          'prepared',
                          'awaiting_decision',
                          'decided',
                          'finalized',
                          'executing'
                      )
                      AND NEW.status = previous.status
                  )
              )
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid GateSession revision transition');
END;

DROP TRIGGER trace_backed_memory_v3_bundle_schema_immutable_update;

UPDATE trace_backed_memory_v3_bundle_schema
SET component_set_sha256 =
    'sha256:323d537feef043b0dff54194f419f8b177096fbfe8f72c1f7e66ca5dd98ce42b'
WHERE singleton = 1
  AND schema_version = 1
  AND contract_version = 'tbm.sqlite-bundle.v3'
  AND component_set_sha256 IN (
      'sha256:3b845f08d52c83705b55cb369758db23a344d9324a006d6541cae554bf921381',
      'sha256:323d537feef043b0dff54194f419f8b177096fbfe8f72c1f7e66ca5dd98ce42b'
  )
  AND catalog_sha256 =
      'sha256:0a86379bdf0ddc8db146e410297d5b2d05e418b2983f02a4c9845a8e3c61273b';

INSERT INTO tbm_gate_session_timestamp_hotfix_guard (valid)
VALUES (changes());

CREATE TRIGGER trace_backed_memory_v3_bundle_schema_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_bundle_schema
BEGIN
    SELECT RAISE(ABORT, 'SQLite v3 bundle metadata is immutable');
END;

DROP TABLE tbm_gate_session_timestamp_hotfix_guard;

COMMIT;
