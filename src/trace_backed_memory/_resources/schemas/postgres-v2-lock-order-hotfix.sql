-- Apply the lesson/source-case lock-order fix to an existing version-2 schema.
-- This migration is atomic, version-gated, and safe to apply more than once.

BEGIN;
SET LOCAL search_path = public, pg_catalog;

DO $hotfix$
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
  IF current_version != 2 THEN
    RAISE EXCEPTION
      'trace-backed memory lock-order hotfix requires schema version 2, found %',
      current_version;
  END IF;
END;
$hotfix$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION public.require_verified_lesson_source_case()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE'
    AND NEW.source_case_id IS NOT DISTINCT FROM OLD.source_case_id
    AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

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

COMMIT;
