from __future__ import annotations

import math
from datetime import datetime, timezone
from dataclasses import replace
from typing import get_args

from .models import FailureCase, Lesson, LessonStatus, MemoryItem, MemoryType, ProjectPolicy, Trace
from .policy import METADATA_VALUE_MAX_CHARS

SCOPE_FIELDS = {
    "repo",
    "tenant",
    "branch",
    "prompt_version",
    "prompt_family",
    "tool",
    "tool_schema_version",
    "model",
    "model_family",
    "eval_suite",
    "task_type",
    "failure_type",
}
_SUPPORTED_MEMORY_TYPES = set(get_args(MemoryType))
_SUPPORTED_POLICY_STATUSES = set(get_args(LessonStatus))


def _require_non_empty_string(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_supported_value(value: str, field_name: str, supported_values: set[str]) -> None:
    if not isinstance(value, str) or value not in supported_values:
        raise ValueError(f"{field_name} is not supported: {value}")


def validate_lesson_contract(*, lesson_text: str, scope: dict[str, str], confidence: float) -> None:
    if not isinstance(scope, dict) or not scope:
        raise ValueError("lessons require non-empty scope")
    for key, value in scope.items():
        if key not in SCOPE_FIELDS:
            raise ValueError(f"lesson scope field is not supported: {key}")
        if not isinstance(value, str) or not value:
            raise ValueError(f"lesson scope field {key!r} must be a non-empty string")
        if len(value) > METADATA_VALUE_MAX_CHARS:
            raise ValueError(
                f"lesson scope field {key!r} must be at most "
                f"{METADATA_VALUE_MAX_CHARS} characters"
            )
    if not isinstance(lesson_text, str) or not lesson_text.strip():
        raise ValueError("lessons require lesson_text")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("lesson confidence must be a number between 0 and 1")
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        raise ValueError("lesson confidence must be between 0 and 1")


def draft_failure_case(
    trace: Trace,
    *,
    case_id: str,
    failure_type: str,
    symptom: str,
    root_cause: str | None = None,
    fix: str | None = None,
) -> FailureCase:
    if trace.eval_result not in {"fail", "error"}:
        raise ValueError("failure cases can only be drafted from failed or errored traces")
    _require_non_empty_string(case_id, "case_id")
    _require_non_empty_string(failure_type, "failure_type")
    _require_non_empty_string(symptom, "symptom")

    return FailureCase(
        case_id=case_id,
        source_trace_id=trace.trace_id,
        commit_sha=trace.commit_sha,
        failure_type=failure_type,
        symptom=symptom,
        root_cause=root_cause,
        fix=fix,
        status="draft",
    )


def verify_failure_case(
    case: FailureCase,
    *,
    fix: str,
    fix_commit_sha: str,
    regression_passed: bool,
) -> FailureCase:
    if case.status != "draft":
        raise ValueError("only draft failure cases can be verified")
    if not fix:
        raise ValueError("verified failure cases require a fix")
    if not fix_commit_sha:
        raise ValueError("verified failure cases require fix_commit_sha")
    if type(regression_passed) is not bool or not regression_passed:
        raise ValueError("verified failure cases require a passing regression")

    return replace(
        case,
        status="verified",
        fix=fix,
        fix_commit_sha=fix_commit_sha,
        regression_passed=True,
    )


def review_failure_case(
    case: FailureCase,
    *,
    reviewed_by: str,
    root_cause: str,
    failure_type: str | None = None,
    symptom: str | None = None,
    review_notes: str | None = None,
    reviewed_at: str | None = None,
) -> FailureCase:
    if case.status != "draft":
        raise ValueError("only draft failure cases can be manually reviewed")
    if not reviewed_by:
        raise ValueError("manual review requires reviewed_by")
    if not root_cause:
        raise ValueError("manual review requires root_cause")

    return replace(
        case,
        failure_type=failure_type or case.failure_type,
        symptom=symptom or case.symptom,
        root_cause=root_cause,
        reviewed_by=reviewed_by,
        review_notes=review_notes,
        reviewed_at=reviewed_at or _utc_timestamp(),
    )


def obsolete_failure_case(case: FailureCase) -> FailureCase:
    return replace(case, status="obsolete")


def lesson_from_failure_case(
    case: FailureCase,
    *,
    lesson_id: str,
    lesson_text: str,
    memory_type: MemoryType,
    scope: dict[str, str],
    confidence: float = 1.0,
) -> Lesson:
    if case.status != "verified":
        raise ValueError("active lessons require a verified failure case")
    if not case.regression_passed:
        raise ValueError("active lessons require a regression-backed failure case")
    _require_non_empty_string(lesson_id, "lesson_id")
    _require_supported_value(memory_type, "memory_type", _SUPPORTED_MEMORY_TYPES)
    validate_lesson_contract(lesson_text=lesson_text, scope=scope, confidence=confidence)

    return Lesson(
        lesson_id=lesson_id,
        source_case_id=case.case_id,
        lesson_text=lesson_text,
        memory_type=memory_type,
        scope=dict(scope),
        confidence=confidence,
        status="active",
    )


def memory_item_from_lesson(lesson: Lesson) -> MemoryItem:
    return MemoryItem(
        memory_id=lesson.lesson_id,
        status=lesson.status,
        memory_type=lesson.memory_type,
        scope=dict(lesson.scope),
        text=lesson.lesson_text,
        source_case_id=lesson.source_case_id,
        confidence=lesson.confidence,
        sensitive=lesson.sensitive,
        eval_leaking=lesson.eval_leaking,
    )


def memory_item_from_failure_case(case: FailureCase, trace: Trace) -> MemoryItem:
    if case.status != "verified" or not case.regression_passed:
        raise ValueError("failure case memory requires a verified regression-backed case")
    if case.source_trace_id != trace.trace_id:
        raise ValueError("failure case source_trace_id must match trace")
    if case.commit_sha != trace.commit_sha:
        raise ValueError("failure case commit_sha must match trace")

    return MemoryItem(
        memory_id=case.case_id,
        status=case.status,
        memory_type="episodic",
        scope=_failure_case_scope(case, trace),
        text=_failure_case_text(case),
        source_trace_id=trace.trace_id,
        source_case_id=case.case_id,
        confidence=1.0,
    )


def memory_item_from_project_policy(policy: ProjectPolicy) -> MemoryItem:
    _require_non_empty_string(policy.policy_id, "policy_id")
    _require_supported_value(policy.status, "status", _SUPPORTED_POLICY_STATUSES)
    validate_lesson_contract(
        lesson_text=policy.policy_text,
        scope=policy.scope,
        confidence=policy.confidence,
    )
    return MemoryItem(
        memory_id=policy.policy_id,
        status=policy.status,
        memory_type="policy",
        scope=dict(policy.scope),
        text=policy.policy_text,
        source_policy_id=policy.policy_id,
        confidence=policy.confidence,
        sensitive=policy.sensitive,
        eval_leaking=policy.eval_leaking,
    )


def obsolete_lesson(lesson: Lesson) -> Lesson:
    return replace(lesson, status="obsolete")


def obsolete_project_policy(policy: ProjectPolicy) -> ProjectPolicy:
    return replace(policy, status="obsolete")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _failure_case_scope(case: FailureCase, trace: Trace) -> dict[str, str]:
    scope: dict[str, str] = {}
    for key in [
        "repo",
        "tenant",
        "branch",
        "prompt_version",
        "prompt_family",
        "tool_schema_version",
        "model",
        "eval_suite",
    ]:
        value = getattr(trace, key)
        if value:
            scope[key] = value

    tool_names = {str(call["name"]) for call in trace.tool_calls if call.get("name")}
    if len(tool_names) == 1:
        scope["tool"] = next(iter(tool_names))

    scope["failure_type"] = case.failure_type
    return scope


def _failure_case_text(case: FailureCase) -> str:
    lines = [f"Failure: {case.symptom}"]
    if case.root_cause:
        lines.append(f"Root cause: {case.root_cause}")
    if case.fix:
        lines.append(f"Fix: {case.fix}")
    return "\n".join(lines)
