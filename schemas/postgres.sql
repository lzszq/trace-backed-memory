-- Minimal schema for trace-backed memory.

BEGIN;
SET LOCAL search_path = public, pg_catalog;

CREATE TABLE public.trace_backed_memory_schema (
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
  schema_version INTEGER NOT NULL CHECK (schema_version > 0)
);

INSERT INTO public.trace_backed_memory_schema(singleton, schema_version)
VALUES (true, 2);

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
ON public.trace_backed_memory_schema FROM PUBLIC;

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
  latency_ms INTEGER CONSTRAINT traces_latency_ms_non_negative
    CHECK (latency_ms >= 0),
  cost_usd NUMERIC CHECK (
    cost_usd NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
  ),
  CHECK (btrim(trace_id) <> ''),
  CHECK (btrim(run_id) <> ''),
  CHECK (btrim(commit_sha) <> ''),
  UNIQUE (trace_id, commit_sha),
  UNIQUE (trace_id, run_id),
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE FUNCTION protect_trace_record() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'trace records cannot be deleted: %', OLD.trace_id;
  END IF;

  IF ROW(
    NEW.trace_id, NEW.run_id, NEW.commit_sha, NEW.repo, NEW.tenant,
    NEW.branch, NEW.dirty, NEW.prompt_version, NEW.prompt_family,
    NEW.tool_schema_version, NEW.model, NEW.eval_suite, NEW.input_hash,
    NEW.retrieved_context, NEW.tool_calls, NEW.created_at
  ) IS DISTINCT FROM ROW(
    OLD.trace_id, OLD.run_id, OLD.commit_sha, OLD.repo, OLD.tenant,
    OLD.branch, OLD.dirty, OLD.prompt_version, OLD.prompt_family,
    OLD.tool_schema_version, OLD.model, OLD.eval_suite, OLD.input_hash,
    OLD.retrieved_context, OLD.tool_calls, OLD.created_at
  ) THEN
    RAISE EXCEPTION 'trace provenance fields are immutable: %', OLD.trace_id;
  END IF;

  IF ROW(
    NEW.output_hash, NEW.tool_outputs, NEW.eval_result, NEW.latency_ms,
    NEW.cost_usd, NEW.error, NEW.trace_uri
  ) IS NOT DISTINCT FROM ROW(
    OLD.output_hash, OLD.tool_outputs, OLD.eval_result, OLD.latency_ms,
    OLD.cost_usd, OLD.error, OLD.trace_uri
  ) THEN
    RETURN NEW;
  END IF;

  IF OLD.eval_result != 'unknown'
    OR NEW.eval_result NOT IN ('pass', 'fail', 'error') THEN
    RAISE EXCEPTION 'trace completion must move forward exactly once: %',
      OLD.trace_id;
  END IF;

  IF (OLD.output_hash IS NOT NULL AND NEW.output_hash IS DISTINCT FROM OLD.output_hash)
    OR (OLD.tool_outputs != '[]'::jsonb AND NEW.tool_outputs IS DISTINCT FROM OLD.tool_outputs)
    OR (OLD.latency_ms IS NOT NULL AND NEW.latency_ms IS DISTINCT FROM OLD.latency_ms)
    OR (OLD.cost_usd IS NOT NULL AND NEW.cost_usd IS DISTINCT FROM OLD.cost_usd)
    OR (OLD.error IS NOT NULL AND NEW.error IS DISTINCT FROM OLD.error)
    OR (OLD.trace_uri IS NOT NULL AND NEW.trace_uri IS DISTINCT FROM OLD.trace_uri) THEN
    RAISE EXCEPTION 'trace completion cannot overwrite populated fields: %',
      OLD.trace_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE TRIGGER traces_protect_record
BEFORE UPDATE OR DELETE ON traces
FOR EACH ROW EXECUTE FUNCTION protect_trace_record();

REVOKE DELETE ON traces FROM PUBLIC;

CREATE TABLE memory_ids (
  memory_id TEXT PRIMARY KEY,
  memory_kind TEXT NOT NULL CHECK (memory_kind IN ('failure_case', 'lesson', 'project_policy')),
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE FUNCTION protect_memory_id_registry() RETURNS trigger AS $$
BEGIN
  IF TG_OP != 'INSERT' OR pg_trigger_depth() < 2 THEN
    RAISE EXCEPTION 'memory_ids registry does not allow direct %', TG_OP;
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE TRIGGER memory_ids_reject_direct_dml
BEFORE INSERT OR UPDATE OR DELETE ON memory_ids
FOR EACH STATEMENT EXECUTE FUNCTION protect_memory_id_registry();

REVOKE INSERT, UPDATE, DELETE ON memory_ids FROM PUBLIC;

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
  CHECK (root_cause IS NULL OR btrim(root_cause) <> ''),
  CHECK (reviewed_by IS NULL OR btrim(reviewed_by) <> ''),
  CHECK (review_notes IS NULL OR btrim(review_notes) <> ''),
  CHECK (fix IS NULL OR btrim(fix) <> ''),
  CHECK (fix_commit_sha IS NULL OR btrim(fix_commit_sha) <> ''),
  CHECK (
    status != 'verified'
    OR (
      fix IS NOT NULL AND btrim(fix) <> ''
      AND fix_commit_sha IS NOT NULL AND btrim(fix_commit_sha) <> ''
      AND regression_passed
      AND root_cause IS NOT NULL AND btrim(root_cause) <> ''
      AND reviewed_by IS NOT NULL AND btrim(reviewed_by) <> ''
      AND reviewed_at IS NOT NULL
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
$$ LANGUAGE SQL IMMUTABLE
SET search_path = pg_catalog;

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

CREATE FUNCTION reject_runtime_memory_truncate() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'runtime memory table does not allow TRUNCATE: %',
    TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE TRIGGER memory_ids_reject_truncate
BEFORE TRUNCATE ON memory_ids
FOR EACH STATEMENT EXECUTE FUNCTION reject_runtime_memory_truncate();

CREATE TRIGGER traces_reject_truncate
BEFORE TRUNCATE ON traces
FOR EACH STATEMENT EXECUTE FUNCTION reject_runtime_memory_truncate();

CREATE TRIGGER failure_cases_reject_truncate
BEFORE TRUNCATE ON failure_cases
FOR EACH STATEMENT EXECUTE FUNCTION reject_runtime_memory_truncate();

CREATE TRIGGER lessons_reject_truncate
BEFORE TRUNCATE ON lessons
FOR EACH STATEMENT EXECUTE FUNCTION reject_runtime_memory_truncate();

CREATE TRIGGER project_policies_reject_truncate
BEFORE TRUNCATE ON project_policies
FOR EACH STATEMENT EXECUTE FUNCTION reject_runtime_memory_truncate();

REVOKE TRUNCATE ON memory_ids, traces, failure_cases, lessons, project_policies FROM PUBLIC;

CREATE FUNCTION register_runtime_memory_id() RETURNS trigger AS $$
DECLARE
  runtime_memory_id TEXT;
  runtime_memory_kind TEXT;
BEGIN
  IF TG_RELID = 'public.failure_cases'::regclass THEN
    runtime_memory_id := NEW.case_id;
    runtime_memory_kind := 'failure_case';
  ELSIF TG_RELID = 'public.lessons'::regclass THEN
    runtime_memory_id := NEW.lesson_id;
    runtime_memory_kind := 'lesson';
  ELSIF TG_RELID = 'public.project_policies'::regclass THEN
    runtime_memory_id := NEW.policy_id;
    runtime_memory_kind := 'project_policy';
  ELSE
    RAISE EXCEPTION 'runtime memory registration is limited to approved source tables';
  END IF;

  INSERT INTO public.memory_ids(memory_id, memory_kind)
  VALUES (runtime_memory_id, runtime_memory_kind);
  RETURN NEW;
EXCEPTION WHEN unique_violation THEN
  RAISE EXCEPTION 'duplicate runtime memory_id: %', runtime_memory_id;
END;
$$ LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog;

REVOKE ALL ON FUNCTION register_runtime_memory_id() FROM PUBLIC;

CREATE FUNCTION protect_runtime_memory_identity() RETURNS trigger AS $$
DECLARE
  old_runtime_memory_id TEXT;
  new_runtime_memory_id TEXT;
BEGIN
  old_runtime_memory_id := to_jsonb(OLD) ->> TG_ARGV[0];

  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'runtime memory records cannot be deleted: %',
      old_runtime_memory_id;
  END IF;

  new_runtime_memory_id := to_jsonb(NEW) ->> TG_ARGV[0];
  IF new_runtime_memory_id IS DISTINCT FROM old_runtime_memory_id THEN
    RAISE EXCEPTION 'runtime memory IDs are immutable: % -> %',
      old_runtime_memory_id, new_runtime_memory_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE FUNCTION protect_failure_case_source() RETURNS trigger AS $$
BEGIN
  IF ROW(NEW.source_trace_id, NEW.commit_sha)
    IS DISTINCT FROM ROW(OLD.source_trace_id, OLD.commit_sha) THEN
    RAISE EXCEPTION 'failure case source provenance is immutable: %',
      OLD.case_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE FUNCTION protect_lesson_source() RETURNS trigger AS $$
BEGIN
  IF NEW.source_case_id IS DISTINCT FROM OLD.source_case_id THEN
    RAISE EXCEPTION 'lesson source provenance is immutable: %',
      OLD.lesson_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE FUNCTION require_verified_lesson_source_case() RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'active' THEN
    PERFORM 1
    FROM public.failure_cases AS source_case
    JOIN public.traces AS source_trace
      ON source_trace.trace_id = source_case.source_trace_id
      AND source_trace.commit_sha = source_case.commit_sha
    WHERE source_case.case_id = NEW.source_case_id
      AND source_case.status = 'verified'
      AND source_case.regression_passed
      AND NOT source_trace.dirty
    FOR SHARE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'lesson source_case_id must reference a verified regression-backed failure case: %',
        NEW.source_case_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE FUNCTION require_failure_case_source_trace() RETURNS trigger AS $$
BEGIN
  PERFORM 1
  FROM public.traces
  WHERE trace_id = NEW.source_trace_id
    AND commit_sha = NEW.commit_sha
    AND eval_result IN ('fail', 'error')
  FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'failure case requires a failed or errored source trace: %',
      NEW.case_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE FUNCTION enforce_failure_case_status_transition() RETURNS trigger AS $$
BEGIN
  IF (OLD.status = 'verified' AND NEW.status = 'draft')
    OR (OLD.status = 'obsolete' AND NEW.status != 'obsolete') THEN
    RAISE EXCEPTION 'failure case status transition is not allowed: % -> %',
      OLD.status, NEW.status;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE FUNCTION enforce_active_obsolete_status_transition() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'obsolete' AND NEW.status != 'obsolete' THEN
    RAISE EXCEPTION 'runtime memory status transition is not allowed: % -> %',
      OLD.status, NEW.status;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE FUNCTION enforce_failure_case_lesson_lifecycle() RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'obsolete' AND OLD.status IS DISTINCT FROM 'obsolete' THEN
    UPDATE public.lessons
    SET status = 'obsolete', updated_at = now()
    WHERE source_case_id = OLD.case_id
      AND status = 'active';
  ELSIF (
    NEW.status IS DISTINCT FROM 'verified'
    OR NOT NEW.regression_passed
  ) AND EXISTS (
    SELECT 1
    FROM public.lessons
    WHERE source_case_id = OLD.case_id
      AND status = 'active'
  ) THEN
    RAISE EXCEPTION 'active lessons require a verified regression-backed source case: %',
      OLD.case_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE TRIGGER failure_cases_register_runtime_memory_id
BEFORE INSERT ON failure_cases
FOR EACH ROW EXECUTE FUNCTION register_runtime_memory_id();

CREATE TRIGGER failure_cases_require_failed_source_trace
BEFORE INSERT OR UPDATE OF source_trace_id, commit_sha ON failure_cases
FOR EACH ROW EXECUTE FUNCTION require_failure_case_source_trace();

CREATE TRIGGER failure_cases_protect_source_provenance
BEFORE UPDATE OF source_trace_id, commit_sha ON failure_cases
FOR EACH ROW EXECUTE FUNCTION protect_failure_case_source();

CREATE TRIGGER failure_cases_protect_runtime_memory_identity
BEFORE UPDATE OF case_id OR DELETE ON failure_cases
FOR EACH ROW EXECUTE FUNCTION protect_runtime_memory_identity('case_id');

CREATE TRIGGER failure_cases_enforce_forward_status
BEFORE UPDATE OF status ON failure_cases
FOR EACH ROW EXECUTE FUNCTION enforce_failure_case_status_transition();

CREATE TRIGGER failure_cases_enforce_lesson_lifecycle
BEFORE UPDATE OF status, regression_passed ON failure_cases
FOR EACH ROW EXECUTE FUNCTION enforce_failure_case_lesson_lifecycle();

CREATE TRIGGER lessons_register_runtime_memory_id
BEFORE INSERT ON lessons
FOR EACH ROW EXECUTE FUNCTION register_runtime_memory_id();

CREATE TRIGGER lessons_protect_runtime_memory_identity
BEFORE UPDATE OF lesson_id OR DELETE ON lessons
FOR EACH ROW EXECUTE FUNCTION protect_runtime_memory_identity('lesson_id');

CREATE TRIGGER lessons_protect_source_provenance
BEFORE UPDATE OF source_case_id ON lessons
FOR EACH ROW EXECUTE FUNCTION protect_lesson_source();

CREATE TRIGGER lessons_enforce_forward_status
BEFORE UPDATE OF status ON lessons
FOR EACH ROW EXECUTE FUNCTION enforce_active_obsolete_status_transition();

CREATE TRIGGER lessons_require_verified_source_case
BEFORE INSERT OR UPDATE OF source_case_id, status ON lessons
FOR EACH ROW EXECUTE FUNCTION require_verified_lesson_source_case();

CREATE TRIGGER project_policies_register_runtime_memory_id
BEFORE INSERT ON project_policies
FOR EACH ROW EXECUTE FUNCTION register_runtime_memory_id();

CREATE TRIGGER project_policies_protect_runtime_memory_identity
BEFORE UPDATE OF policy_id OR DELETE ON project_policies
FOR EACH ROW EXECUTE FUNCTION protect_runtime_memory_identity('policy_id');

CREATE TRIGGER project_policies_enforce_forward_status
BEFORE UPDATE OF status ON project_policies
FOR EACH ROW EXECUTE FUNCTION enforce_active_obsolete_status_transition();

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
$$ LANGUAGE SQL IMMUTABLE
SET search_path = pg_catalog;

CREATE FUNCTION valid_non_empty_text_object(value JSONB) RETURNS BOOLEAN AS $$
  SELECT jsonb_typeof(value) = 'object'
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_each(value) AS entry(object_key, object_value)
      WHERE btrim(entry.object_key) = ''
        OR jsonb_typeof(entry.object_value) != 'string'
        OR btrim(entry.object_value #>> '{}') = ''
    );
$$ LANGUAGE SQL IMMUTABLE
SET search_path = pg_catalog;

CREATE FUNCTION valid_candidate_memory_statuses(value JSONB) RETURNS BOOLEAN AS $$
  SELECT jsonb_typeof(value) = 'object'
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_each(value) AS entry(memory_id, status)
      WHERE btrim(entry.memory_id) = ''
        OR jsonb_typeof(entry.status) != 'string'
        OR entry.status #>> '{}' NOT IN ('draft', 'verified', 'active', 'obsolete')
    );
$$ LANGUAGE SQL IMMUTABLE
SET search_path = pg_catalog;

CREATE TABLE memory_usage_decisions (
  decision_id TEXT PRIMARY KEY,
  request_id TEXT,
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
  CHECK (
    request_id IS NULL
    OR (
      pg_catalog.btrim(request_id) <> ''
      AND pg_catalog.char_length(request_id) <= 128
    )
  ),
  CHECK (btrim(run_id) <> ''),
  CHECK (btrim(trace_id) <> ''),
  CHECK (reason ~ '[^[:space:]]'),
  CHECK (char_length(reason) <= 2000),
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

CREATE FUNCTION protect_usage_decision_record() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'usage decision records cannot be deleted: %',
      OLD.decision_id;
  END IF;

  IF ROW(
    NEW.decision_id, NEW.request_id, NEW.run_id, NEW.trace_id, NEW.mode,
    NEW.candidate_memory_ids, NEW.used_memory_ids, NEW.blocked_memory_ids,
    NEW.risk, NEW.reason, NEW.recommended_injection, NEW.context,
    NEW.candidate_memory_statuses, NEW.system_blocked_reasons, NEW.created_at
  ) IS DISTINCT FROM ROW(
    OLD.decision_id, OLD.request_id, OLD.run_id, OLD.trace_id, OLD.mode,
    OLD.candidate_memory_ids, OLD.used_memory_ids, OLD.blocked_memory_ids,
    OLD.risk, OLD.reason, OLD.recommended_injection, OLD.context,
    OLD.candidate_memory_statuses, OLD.system_blocked_reasons, OLD.created_at
  ) THEN
    RAISE EXCEPTION 'usage decision audit fields are immutable: %',
      OLD.decision_id;
  END IF;

  IF ROW(NEW.eval_result, NEW.memory_caused_failure)
    IS NOT DISTINCT FROM
    ROW(OLD.eval_result, OLD.memory_caused_failure) THEN
    RETURN NEW;
  END IF;

  IF COALESCE(OLD.eval_result, 'unknown') != 'unknown'
    OR NEW.eval_result IS NULL
    OR NEW.eval_result NOT IN ('pass', 'fail', 'error') THEN
    RAISE EXCEPTION 'usage decision outcome must move forward exactly once: %',
      OLD.decision_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE TRIGGER memory_usage_decisions_protect_record
BEFORE UPDATE OR DELETE ON memory_usage_decisions
FOR EACH ROW EXECUTE FUNCTION protect_usage_decision_record();

CREATE TRIGGER memory_usage_decisions_reject_truncate
BEFORE TRUNCATE ON memory_usage_decisions
FOR EACH STATEMENT EXECUTE FUNCTION reject_runtime_memory_truncate();

REVOKE DELETE, TRUNCATE ON memory_usage_decisions FROM PUBLIC;

CREATE FUNCTION require_usage_trace_context() RETURNS trigger AS $$
DECLARE
  trace_record public.traces%ROWTYPE;
BEGIN
  SELECT * INTO trace_record
  FROM public.traces
  WHERE trace_id = NEW.trace_id
    AND run_id = NEW.run_id
  FOR SHARE;

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
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

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
    SELECT 1
    FROM public.memory_ids
    WHERE public.memory_ids.memory_id = refs.memory_id
      AND (
        (
          public.memory_ids.memory_kind = 'failure_case'
          AND EXISTS (
            SELECT 1 FROM public.failure_cases
            WHERE public.failure_cases.case_id = refs.memory_id
          )
        )
        OR (
          public.memory_ids.memory_kind = 'lesson'
          AND EXISTS (
            SELECT 1 FROM public.lessons
            WHERE public.lessons.lesson_id = refs.memory_id
          )
        )
        OR (
          public.memory_ids.memory_kind = 'project_policy'
          AND EXISTS (
            SELECT 1 FROM public.project_policies
            WHERE public.project_policies.policy_id = refs.memory_id
          )
        )
      )
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
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

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

COMMIT;
