PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

BEGIN IMMEDIATE;

DROP TRIGGER trace_backed_memory_v3_bundle_schema_immutable_update;

UPDATE trace_backed_memory_v3_bundle_schema
SET component_set_sha256 =
    'sha256:3b845f08d52c83705b55cb369758db23a344d9324a006d6541cae554bf921381'
WHERE singleton = 1;

CREATE TRIGGER trace_backed_memory_v3_bundle_schema_immutable_update
BEFORE UPDATE ON trace_backed_memory_v3_bundle_schema
BEGIN
    SELECT RAISE(ABORT, 'SQLite v3 bundle metadata is immutable');
END;

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
              AND previous.updated_at < NEW.updated_at
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

COMMIT;
