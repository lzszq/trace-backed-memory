-- Minimal schema for trace-backed memory.

CREATE TABLE traces (
  trace_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  repo TEXT,
  tenant TEXT,
  branch TEXT,
  dirty BOOLEAN NOT NULL DEFAULT false,
  prompt_version TEXT,
  prompt_family TEXT,
  tool_schema_version TEXT,
  model TEXT,
  eval_suite TEXT,
  eval_result TEXT NOT NULL DEFAULT 'unknown' CHECK (eval_result IN ('pass', 'fail', 'error', 'unknown')),
  trace_uri TEXT,
  input_hash TEXT,
  output_hash TEXT,
  retrieved_context JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(retrieved_context) = 'array'
    AND NOT jsonb_path_exists(retrieved_context, '$[*] ? (@.type() != "object")')
  ),
  tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(tool_calls) = 'array'
    AND NOT jsonb_path_exists(tool_calls, '$[*] ? (@.type() != "object")')
  ),
  tool_outputs JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(tool_outputs) = 'array'
    AND NOT jsonb_path_exists(tool_outputs, '$[*] ? (@.type() != "object")')
  ),
  error TEXT,
  latency_ms INTEGER,
  cost_usd NUMERIC,
  CHECK (btrim(trace_id) <> ''),
  CHECK (btrim(run_id) <> ''),
  CHECK (btrim(commit_sha) <> ''),
  UNIQUE (trace_id, commit_sha),
  UNIQUE (trace_id, run_id),
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE memory_ids (
  memory_id TEXT PRIMARY KEY,
  memory_kind TEXT NOT NULL CHECK (memory_kind IN ('failure_case', 'lesson', 'project_policy')),
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE failure_cases (
  case_id TEXT PRIMARY KEY,
  source_trace_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  failure_type TEXT NOT NULL,
  symptom TEXT NOT NULL,
  root_cause TEXT,
  reviewed_by TEXT,
  review_notes TEXT,
  reviewed_at TIMESTAMPTZ,
  fix TEXT,
  fix_commit_sha TEXT,
  regression_passed BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'verified', 'obsolete')),
  CHECK (btrim(case_id) <> ''),
  CHECK (btrim(source_trace_id) <> ''),
  CHECK (btrim(commit_sha) <> ''),
  CHECK (btrim(failure_type) <> ''),
  CHECK (btrim(symptom) <> ''),
  CHECK (fix IS NULL OR btrim(fix) <> ''),
  CHECK (fix_commit_sha IS NULL OR btrim(fix_commit_sha) <> ''),
  CHECK (
    status != 'verified'
    OR (
      fix IS NOT NULL AND btrim(fix) <> ''
      AND fix_commit_sha IS NOT NULL AND btrim(fix_commit_sha) <> ''
      AND regression_passed
    )
  ),
  FOREIGN KEY (source_trace_id, commit_sha) REFERENCES traces(trace_id, commit_sha),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE FUNCTION valid_memory_scope_json(value JSONB) RETURNS BOOLEAN AS $$
  SELECT jsonb_typeof(value) = 'object'
    AND value != '{}'::jsonb
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_each(value) AS entry(scope_key, scope_value)
      WHERE entry.scope_key NOT IN (
        'repo',
        'tenant',
        'branch',
        'prompt_version',
        'prompt_family',
        'tool',
        'tool_schema_version',
        'model',
        'model_family',
        'eval_suite',
        'task_type',
        'failure_type'
      )
        OR jsonb_typeof(entry.scope_value) != 'string'
        OR btrim(entry.scope_value #>> '{}') = ''
    );
$$ LANGUAGE SQL IMMUTABLE;

CREATE TABLE lessons (
  lesson_id TEXT PRIMARY KEY,
  source_case_id TEXT NOT NULL REFERENCES failure_cases(case_id),
  lesson_text TEXT NOT NULL,
  memory_type TEXT NOT NULL CHECK (memory_type IN ('procedural', 'semantic', 'episodic', 'policy')),
  scope_json JSONB NOT NULL CHECK (valid_memory_scope_json(scope_json)),
  confidence NUMERIC NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
  sensitive BOOLEAN NOT NULL DEFAULT false,
  eval_leaking BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'obsolete')),
  CHECK (btrim(lesson_id) <> ''),
  CHECK (btrim(source_case_id) <> ''),
  CHECK (btrim(lesson_text) <> ''),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE project_policies (
  policy_id TEXT PRIMARY KEY,
  policy_text TEXT NOT NULL,
  scope_json JSONB NOT NULL CHECK (valid_memory_scope_json(scope_json)),
  confidence NUMERIC NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
  sensitive BOOLEAN NOT NULL DEFAULT false,
  eval_leaking BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'obsolete')),
  CHECK (btrim(policy_id) <> ''),
  CHECK (btrim(policy_text) <> ''),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE FUNCTION register_runtime_memory_id() RETURNS trigger AS $$
DECLARE
  runtime_memory_id TEXT;
  runtime_memory_kind TEXT;
BEGIN
  IF TG_TABLE_NAME = 'failure_cases' THEN
    runtime_memory_id := NEW.case_id;
    runtime_memory_kind := 'failure_case';
  ELSIF TG_TABLE_NAME = 'lessons' THEN
    runtime_memory_id := NEW.lesson_id;
    runtime_memory_kind := 'lesson';
  ELSE
    runtime_memory_id := NEW.policy_id;
    runtime_memory_kind := 'project_policy';
  END IF;

  INSERT INTO memory_ids(memory_id, memory_kind)
  VALUES (runtime_memory_id, runtime_memory_kind);
  RETURN NEW;
EXCEPTION WHEN unique_violation THEN
  RAISE EXCEPTION 'duplicate runtime memory_id: %', runtime_memory_id;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION require_verified_lesson_source_case() RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM failure_cases
    WHERE case_id = NEW.source_case_id
      AND status = 'verified'
      AND regression_passed
  ) THEN
    RAISE EXCEPTION 'lesson source_case_id must reference a verified regression-backed failure case: %',
      NEW.source_case_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER failure_cases_register_runtime_memory_id
BEFORE INSERT ON failure_cases
FOR EACH ROW EXECUTE FUNCTION register_runtime_memory_id();

CREATE TRIGGER lessons_register_runtime_memory_id
BEFORE INSERT ON lessons
FOR EACH ROW EXECUTE FUNCTION register_runtime_memory_id();

CREATE TRIGGER lessons_require_verified_source_case
BEFORE INSERT OR UPDATE OF source_case_id ON lessons
FOR EACH ROW EXECUTE FUNCTION require_verified_lesson_source_case();

CREATE TRIGGER project_policies_register_runtime_memory_id
BEFORE INSERT ON project_policies
FOR EACH ROW EXECUTE FUNCTION register_runtime_memory_id();

CREATE FUNCTION jsonb_text_array_has_duplicates(value JSONB) RETURNS BOOLEAN AS $$
  SELECT CASE
    WHEN jsonb_typeof(value) != 'array' THEN true
    ELSE EXISTS (
      SELECT 1
      FROM jsonb_array_elements_text(value) AS item(memory_id)
      GROUP BY item.memory_id
      HAVING COUNT(*) > 1
    )
  END;
$$ LANGUAGE SQL IMMUTABLE;

CREATE FUNCTION valid_non_empty_text_object(value JSONB) RETURNS BOOLEAN AS $$
  SELECT jsonb_typeof(value) = 'object'
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_each(value) AS entry(object_key, object_value)
      WHERE btrim(entry.object_key) = ''
        OR jsonb_typeof(entry.object_value) != 'string'
        OR btrim(entry.object_value #>> '{}') = ''
    );
$$ LANGUAGE SQL IMMUTABLE;

CREATE FUNCTION valid_candidate_memory_statuses(value JSONB) RETURNS BOOLEAN AS $$
  SELECT jsonb_typeof(value) = 'object'
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_each(value) AS entry(memory_id, status)
      WHERE btrim(entry.memory_id) = ''
        OR jsonb_typeof(entry.status) != 'string'
        OR entry.status #>> '{}' NOT IN ('draft', 'verified', 'active', 'obsolete')
    );
$$ LANGUAGE SQL IMMUTABLE;

CREATE TABLE memory_usage_decisions (
  decision_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('debug', 'repair', 'regression', 'planning', 'eval', 'production')),
  candidate_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(candidate_memory_ids) = 'array'
    AND NOT jsonb_path_exists(candidate_memory_ids, '$[*] ? (@.type() != "string")')
    AND NOT jsonb_path_exists(candidate_memory_ids, '$[*] ? (@ == "")')
    AND NOT jsonb_text_array_has_duplicates(candidate_memory_ids)
  ),
  used_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(used_memory_ids) = 'array'
    AND NOT jsonb_path_exists(used_memory_ids, '$[*] ? (@.type() != "string")')
    AND NOT jsonb_path_exists(used_memory_ids, '$[*] ? (@ == "")')
    AND NOT jsonb_text_array_has_duplicates(used_memory_ids)
  ),
  blocked_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(blocked_memory_ids) = 'array'
    AND NOT jsonb_path_exists(blocked_memory_ids, '$[*] ? (@.type() != "string")')
    AND NOT jsonb_path_exists(blocked_memory_ids, '$[*] ? (@ == "")')
    AND NOT jsonb_text_array_has_duplicates(blocked_memory_ids)
  ),
  risk TEXT NOT NULL CHECK (risk IN ('none', 'low', 'medium', 'high')),
  reason TEXT NOT NULL,
  recommended_injection TEXT NOT NULL CHECK (
    recommended_injection IN ('none', 'short_summary', 'full_case_summary', 'pointer_only')
  ),
  eval_result TEXT CHECK (eval_result IN ('pass', 'fail', 'error', 'unknown')),
  memory_caused_failure BOOLEAN NOT NULL DEFAULT false,
  context JSONB NOT NULL CHECK (valid_non_empty_text_object(context)),
  candidate_memory_statuses JSONB NOT NULL CHECK (valid_candidate_memory_statuses(candidate_memory_statuses)),
  system_blocked_reasons JSONB NOT NULL CHECK (valid_non_empty_text_object(system_blocked_reasons)),
  CHECK (btrim(decision_id) <> ''),
  CHECK (btrim(run_id) <> ''),
  CHECK (btrim(trace_id) <> ''),
  CHECK (btrim(reason) <> ''),
  CHECK (
    (jsonb_array_length(used_memory_ids) = 0 AND recommended_injection = 'none')
    OR (jsonb_array_length(used_memory_ids) > 0 AND recommended_injection != 'none')
  ),
  CHECK (
    NOT memory_caused_failure
    OR (
      jsonb_array_length(used_memory_ids) > 0
      AND eval_result IS NOT NULL
      AND eval_result IN ('fail', 'error')
    )
  ),
  FOREIGN KEY (trace_id, run_id) REFERENCES traces(trace_id, run_id),
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE FUNCTION require_usage_trace_context() RETURNS trigger AS $$
DECLARE
  trace_record traces%ROWTYPE;
BEGIN
  SELECT * INTO trace_record
  FROM traces
  WHERE trace_id = NEW.trace_id
    AND run_id = NEW.run_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'usage trace_id and run_id must reference one trace: %, %',
      NEW.trace_id, NEW.run_id;
  END IF;

  IF NOT (NEW.context ? 'mode')
    OR NOT (NEW.context ? 'repo')
    OR NOT (NEW.context ? 'commit_sha') THEN
    RAISE EXCEPTION 'usage context requires mode, repo, and commit_sha evidence';
  END IF;

  IF NEW.context ->> 'mode' IS DISTINCT FROM NEW.mode THEN
    RAISE EXCEPTION 'usage context mode conflicts with decision mode';
  END IF;
  IF NEW.context ->> 'repo' IS DISTINCT FROM trace_record.repo THEN
    RAISE EXCEPTION 'usage context repo conflicts with trace';
  END IF;
  IF NEW.context ->> 'commit_sha' IS DISTINCT FROM trace_record.commit_sha THEN
    RAISE EXCEPTION 'usage context commit_sha conflicts with trace';
  END IF;

  IF trace_record.tenant IS NOT NULL THEN
    IF NOT (NEW.context ? 'tenant')
      OR NEW.context ->> 'tenant' IS DISTINCT FROM trace_record.tenant THEN
      RAISE EXCEPTION 'usage context tenant conflicts with trace';
    END IF;
  ELSIF NEW.context ? 'tenant' THEN
    RAISE EXCEPTION 'usage context tenant conflicts with trace';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER memory_usage_decisions_require_trace_context
BEFORE INSERT OR UPDATE OF trace_id, run_id, mode, context
ON memory_usage_decisions
FOR EACH ROW EXECUTE FUNCTION require_usage_trace_context();

CREATE FUNCTION require_known_usage_memory_ids() RETURNS trigger AS $$
DECLARE
  unknown_id TEXT;
  missing_used_id TEXT;
  missing_blocked_id TEXT;
  missing_status_id TEXT;
  extra_status_id TEXT;
  extra_block_reason_id TEXT;
  overlapping_id TEXT;
BEGIN
  SELECT refs.memory_id INTO unknown_id
  FROM (
    SELECT jsonb_array_elements_text(NEW.candidate_memory_ids) AS memory_id
    UNION
    SELECT jsonb_array_elements_text(NEW.used_memory_ids) AS memory_id
    UNION
    SELECT jsonb_array_elements_text(NEW.blocked_memory_ids) AS memory_id
  ) AS refs
  WHERE NOT EXISTS (
    SELECT 1 FROM memory_ids WHERE memory_ids.memory_id = refs.memory_id
  )
  LIMIT 1;
  IF unknown_id IS NOT NULL THEN
    RAISE EXCEPTION 'usage log references unknown memory IDs: %', unknown_id;
  END IF;

  SELECT used_ids.memory_id INTO missing_used_id
  FROM jsonb_array_elements_text(NEW.used_memory_ids) AS used_ids(memory_id)
  WHERE NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(NEW.candidate_memory_ids) AS candidate_ids(memory_id)
    WHERE candidate_ids.memory_id = used_ids.memory_id
  )
  LIMIT 1;
  IF missing_used_id IS NOT NULL THEN
    RAISE EXCEPTION 'used memory ids must be present in candidates: %', missing_used_id;
  END IF;

  SELECT blocked_ids.memory_id INTO missing_blocked_id
  FROM jsonb_array_elements_text(NEW.blocked_memory_ids) AS blocked_ids(memory_id)
  WHERE NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(NEW.candidate_memory_ids) AS candidate_ids(memory_id)
    WHERE candidate_ids.memory_id = blocked_ids.memory_id
  )
  LIMIT 1;
  IF missing_blocked_id IS NOT NULL THEN
    RAISE EXCEPTION 'blocked memory ids must be present in candidates: %', missing_blocked_id;
  END IF;

  SELECT candidate_ids.memory_id INTO missing_status_id
  FROM jsonb_array_elements_text(NEW.candidate_memory_ids) AS candidate_ids(memory_id)
  WHERE NOT EXISTS (
    SELECT 1
    FROM jsonb_object_keys(NEW.candidate_memory_statuses) AS status_ids(memory_id)
    WHERE status_ids.memory_id = candidate_ids.memory_id
  )
  LIMIT 1;
  IF missing_status_id IS NOT NULL THEN
    RAISE EXCEPTION 'candidate status evidence must include every candidate: %', missing_status_id;
  END IF;

  SELECT status_ids.memory_id INTO extra_status_id
  FROM jsonb_object_keys(NEW.candidate_memory_statuses) AS status_ids(memory_id)
  WHERE NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(NEW.candidate_memory_ids) AS candidate_ids(memory_id)
    WHERE candidate_ids.memory_id = status_ids.memory_id
  )
  LIMIT 1;
  IF extra_status_id IS NOT NULL THEN
    RAISE EXCEPTION 'candidate status evidence must not include non-candidates: %', extra_status_id;
  END IF;

  SELECT blocked_reason_ids.memory_id INTO extra_block_reason_id
  FROM jsonb_object_keys(NEW.system_blocked_reasons) AS blocked_reason_ids(memory_id)
  WHERE NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(NEW.candidate_memory_ids) AS candidate_ids(memory_id)
    WHERE candidate_ids.memory_id = blocked_reason_ids.memory_id
  )
  LIMIT 1;
  IF extra_block_reason_id IS NOT NULL THEN
    RAISE EXCEPTION 'system block reason must reference a candidate: %', extra_block_reason_id;
  END IF;

  SELECT used_ids.memory_id INTO overlapping_id
  FROM jsonb_array_elements_text(NEW.used_memory_ids) AS used_ids(memory_id)
  JOIN jsonb_array_elements_text(NEW.blocked_memory_ids) AS blocked_ids(memory_id)
    ON blocked_ids.memory_id = used_ids.memory_id
  LIMIT 1;
  IF overlapping_id IS NOT NULL THEN
    RAISE EXCEPTION 'memory ids cannot be both used and blocked: %', overlapping_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER memory_usage_decisions_require_known_memory_ids
BEFORE INSERT OR UPDATE OF candidate_memory_ids, used_memory_ids, blocked_memory_ids, candidate_memory_statuses, system_blocked_reasons
ON memory_usage_decisions
FOR EACH ROW EXECUTE FUNCTION require_known_usage_memory_ids();

CREATE INDEX idx_traces_commit_sha ON traces(commit_sha);
CREATE INDEX idx_traces_repo ON traces(repo);
CREATE INDEX idx_traces_eval_suite ON traces(eval_suite);
CREATE INDEX idx_failure_cases_failure_type ON failure_cases(failure_type);
CREATE INDEX idx_failure_cases_status ON failure_cases(status);
CREATE INDEX idx_lessons_status ON lessons(status);
CREATE INDEX idx_lessons_scope_json ON lessons USING GIN (scope_json);
CREATE INDEX idx_project_policies_status ON project_policies(status);
CREATE INDEX idx_project_policies_scope_json ON project_policies USING GIN (scope_json);
