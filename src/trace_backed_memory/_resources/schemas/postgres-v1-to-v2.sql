-- Upgrade trace-backed memory PostgreSQL schema version 1 to version 2.
-- The migration is atomic and requires the exact version-1 metadata row.

BEGIN;
SET LOCAL search_path = public, pg_catalog;

DO $migration$
DECLARE
  current_version INTEGER;
BEGIN
  SELECT schema_version INTO current_version
  FROM public.trace_backed_memory_schema
  WHERE singleton
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'trace-backed memory schema metadata row is missing';
  END IF;
  IF current_version != 1 THEN
    RAISE EXCEPTION
      'trace-backed memory migration requires schema version 1, found %',
      current_version;
  END IF;
END;
$migration$ LANGUAGE plpgsql;

ALTER TABLE public.memory_usage_decisions
ADD COLUMN request_id TEXT;

ALTER TABLE public.memory_usage_decisions
ADD CONSTRAINT memory_usage_decisions_request_id_valid CHECK (
  request_id IS NULL
  OR (
    pg_catalog.btrim(request_id) <> ''
    AND pg_catalog.char_length(request_id) <= 128
  )
);

CREATE FUNCTION public.protect_failure_case_source() RETURNS trigger AS $$
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

CREATE FUNCTION public.protect_lesson_source() RETURNS trigger AS $$
BEGIN
  IF NEW.source_case_id IS DISTINCT FROM OLD.source_case_id THEN
    RAISE EXCEPTION 'lesson source provenance is immutable: %',
      OLD.lesson_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = pg_catalog;

CREATE TRIGGER failure_cases_protect_source_provenance
BEFORE UPDATE OF source_trace_id, commit_sha ON public.failure_cases
FOR EACH ROW EXECUTE FUNCTION public.protect_failure_case_source();

CREATE TRIGGER lessons_protect_source_provenance
BEFORE UPDATE OF source_case_id ON public.lessons
FOR EACH ROW EXECUTE FUNCTION public.protect_lesson_source();

CREATE FUNCTION public.protect_trace_record() RETURNS trigger AS $$
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
BEFORE UPDATE OR DELETE ON public.traces
FOR EACH ROW EXECUTE FUNCTION public.protect_trace_record();

CREATE TRIGGER traces_reject_truncate
BEFORE TRUNCATE ON public.traces
FOR EACH STATEMENT
EXECUTE FUNCTION public.reject_runtime_memory_truncate();

REVOKE DELETE, TRUNCATE ON public.traces FROM PUBLIC;

CREATE FUNCTION public.protect_usage_decision_record() RETURNS trigger AS $$
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
BEFORE UPDATE OR DELETE ON public.memory_usage_decisions
FOR EACH ROW
EXECUTE FUNCTION public.protect_usage_decision_record();

CREATE TRIGGER memory_usage_decisions_reject_truncate
BEFORE TRUNCATE ON public.memory_usage_decisions
FOR EACH STATEMENT
EXECUTE FUNCTION public.reject_runtime_memory_truncate();

REVOKE DELETE, TRUNCATE
ON public.memory_usage_decisions FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.require_usage_trace_context()
RETURNS trigger AS $$
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

UPDATE public.trace_backed_memory_schema
SET schema_version = 2
WHERE singleton;

COMMIT;
