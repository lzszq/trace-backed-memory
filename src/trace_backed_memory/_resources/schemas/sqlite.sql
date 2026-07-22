PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS trace_backed_memory_schema (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  schema_version INTEGER NOT NULL CHECK (schema_version > 0)
) WITHOUT ROWID;

INSERT OR IGNORE INTO trace_backed_memory_schema(singleton, schema_version)
VALUES (1, 1);

CREATE TABLE IF NOT EXISTS traces (
  trace_id TEXT PRIMARY KEY
    CHECK (typeof(trace_id) = 'text' AND length(trace_id) BETWEEN 1 AND 128),
  payload TEXT NOT NULL
    CHECK (
      typeof(payload) = 'text'
      AND length(CAST(payload AS BLOB)) <= 67108864
    )
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS failure_cases (
  case_id TEXT PRIMARY KEY
    CHECK (typeof(case_id) = 'text' AND length(case_id) BETWEEN 1 AND 128),
  payload TEXT NOT NULL
    CHECK (
      typeof(payload) = 'text'
      AND length(CAST(payload AS BLOB)) <= 67108864
    )
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS lessons (
  lesson_id TEXT PRIMARY KEY
    CHECK (typeof(lesson_id) = 'text' AND length(lesson_id) BETWEEN 1 AND 128),
  payload TEXT NOT NULL
    CHECK (
      typeof(payload) = 'text'
      AND length(CAST(payload AS BLOB)) <= 67108864
    )
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS project_policies (
  policy_id TEXT PRIMARY KEY
    CHECK (typeof(policy_id) = 'text' AND length(policy_id) BETWEEN 1 AND 128),
  payload TEXT NOT NULL
    CHECK (
      typeof(payload) = 'text'
      AND length(CAST(payload AS BLOB)) <= 67108864
    )
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS memory_usage_decisions (
  decision_id TEXT PRIMARY KEY
    CHECK (typeof(decision_id) = 'text' AND length(decision_id) BETWEEN 1 AND 128),
  payload TEXT NOT NULL
    CHECK (
      typeof(payload) = 'text'
      AND length(CAST(payload AS BLOB)) <= 67108864
    )
) WITHOUT ROWID;

COMMIT;
