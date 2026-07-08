-- Minimal schema for trace-backed memory.

CREATE TABLE traces (
  trace_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  branch TEXT,
  prompt_version TEXT,
  tool_schema_version TEXT,
  model TEXT,
  eval_result TEXT CHECK (eval_result IN ('pass', 'fail', 'error', 'unknown')),
  trace_uri TEXT,
  input_hash TEXT,
  output_hash TEXT,
  error TEXT,
  latency_ms INTEGER,
  cost_usd NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE failure_cases (
  case_id TEXT PRIMARY KEY,
  source_trace_id TEXT REFERENCES traces(trace_id),
  commit_sha TEXT NOT NULL,
  failure_type TEXT NOT NULL,
  symptom TEXT NOT NULL,
  root_cause TEXT,
  fix TEXT,
  fix_commit_sha TEXT,
  status TEXT NOT NULL CHECK (status IN ('draft', 'verified', 'obsolete')),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE lessons (
  lesson_id TEXT PRIMARY KEY,
  source_case_id TEXT REFERENCES failure_cases(case_id),
  lesson_text TEXT NOT NULL,
  memory_type TEXT NOT NULL CHECK (memory_type IN ('procedural', 'semantic', 'episodic', 'policy')),
  scope_json JSONB NOT NULL,
  confidence NUMERIC DEFAULT 0.0,
  status TEXT NOT NULL CHECK (status IN ('active', 'obsolete')),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE memory_usage_decisions (
  decision_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('debug', 'repair', 'regression', 'planning', 'eval', 'production')),
  candidate_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  used_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  blocked_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk TEXT CHECK (risk IN ('none', 'low', 'medium', 'high')),
  reason TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_traces_commit_sha ON traces(commit_sha);
CREATE INDEX idx_failure_cases_failure_type ON failure_cases(failure_type);
CREATE INDEX idx_failure_cases_status ON failure_cases(status);
CREATE INDEX idx_lessons_status ON lessons(status);
CREATE INDEX idx_lessons_scope_json ON lessons USING GIN (scope_json);
