from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .lifecycle import (
    memory_item_from_failure_case,
    memory_item_from_lesson,
    memory_item_from_project_policy,
    validate_lesson_contract,
)
from .models import (
    EvalResult,
    FailureCase,
    Lesson,
    MemoryContext,
    MemoryDecision,
    MemoryItem,
    MemoryMetrics,
    PRCaseProvenance,
    MemoryUsageLog,
    PRMemoryReport,
    ProjectPolicy,
    Trace,
)

Snapshot = dict[str, list[dict[str, Any]]]
EVAL_RESULTS = {"pass", "fail", "error", "unknown"}
FAILURE_CASE_STATUSES = {"draft", "verified", "obsolete"}
LESSON_STATUSES = {"active", "obsolete"}
MEMORY_TYPES = {"procedural", "semantic", "episodic", "policy"}
MODES = {"debug", "repair", "regression", "planning", "eval", "production"}
DECISION_RISKS = {"none", "low", "medium", "high"}
RECOMMENDED_INJECTIONS = {"none", "short_summary", "full_case_summary", "pointer_only"}


class TraceBackedMemoryStore:
    """Small in-memory MVP store for trace-backed memory workflows."""

    def __init__(self) -> None:
        self.traces: dict[str, Trace] = {}
        self.failure_cases: dict[str, FailureCase] = {}
        self.lessons: dict[str, Lesson] = {}
        self.project_policies: dict[str, ProjectPolicy] = {}
        self.usage_logs: list[MemoryUsageLog] = []

    def to_snapshot(self) -> Snapshot:
        return {
            "traces": [asdict(trace) for trace in self.traces.values()],
            "failure_cases": [asdict(case) for case in self.failure_cases.values()],
            "lessons": [asdict(lesson) for lesson in self.lessons.values()],
            "project_policies": [asdict(policy) for policy in self.project_policies.values()],
            "usage_logs": [asdict(log) for log in self.usage_logs],
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "TraceBackedMemoryStore":
        store = cls()
        for trace_data in _snapshot_records(data, "traces"):
            store.record_trace(Trace(**trace_data))
        for case_data in _snapshot_records(data, "failure_cases"):
            store.add_failure_case(FailureCase(**case_data))
        for lesson_data in _snapshot_records(data, "lessons"):
            store.add_lesson(Lesson(**lesson_data))
        for policy_data in _snapshot_records(data, "project_policies"):
            store.add_project_policy(ProjectPolicy(**policy_data))
        for log_data in _snapshot_records(data, "usage_logs"):
            log = MemoryUsageLog(**log_data)
            _validate_usage_log(log)
            store._validate_usage_log_memory_ids(log)
            if any(existing.decision_id == log.decision_id for existing in store.usage_logs):
                raise ValueError(f"duplicate usage log decision_id: {log.decision_id}")
            store.usage_logs.append(log)
        return store

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_snapshot(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "TraceBackedMemoryStore":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("memory store snapshot must be a JSON object")
        return cls.from_snapshot(data)

    def save_lessons_yaml(self, path: str | Path) -> None:
        active_lessons = [lesson for lesson in self.lessons.values() if lesson.status == "active"]
        Path(path).write_text(_lessons_to_yaml(active_lessons), encoding="utf-8")

    def load_lessons_yaml(self, path: str | Path) -> list[Lesson]:
        lesson_records = _lessons_from_yaml(Path(path).read_text(encoding="utf-8"))
        return [self.add_lesson(Lesson(**record)) for record in lesson_records]

    def record_trace(self, trace: Trace) -> Trace:
        _validate_trace(trace)
        if trace.trace_id in self.traces:
            raise ValueError(f"duplicate trace_id: {trace.trace_id}")
        self.traces[trace.trace_id] = trace
        return trace

    def add_failure_case(self, case: FailureCase) -> FailureCase:
        _validate_failure_case(case)
        if case.case_id in self.failure_cases:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        if case.case_id in self.lessons or case.case_id in self.project_policies:
            raise ValueError(f"duplicate memory_id across memory types: {case.case_id}")
        source_trace = self.traces.get(case.source_trace_id)
        if source_trace is None:
            raise ValueError(f"missing source_trace_id: {case.source_trace_id}")
        if case.commit_sha != source_trace.commit_sha:
            raise ValueError(f"failure case commit_sha does not match source trace: {case.case_id}")
        self.failure_cases[case.case_id] = case
        return case

    def add_lesson(self, lesson: Lesson) -> Lesson:
        _validate_lesson_record(lesson)
        if lesson.lesson_id in self.lessons:
            raise ValueError(f"duplicate lesson_id: {lesson.lesson_id}")
        if lesson.lesson_id in self.failure_cases or lesson.lesson_id in self.project_policies:
            raise ValueError(f"duplicate memory_id across memory types: {lesson.lesson_id}")
        validate_lesson_contract(
            lesson_text=lesson.lesson_text,
            scope=lesson.scope,
            confidence=lesson.confidence,
        )
        source_case = self.failure_cases.get(lesson.source_case_id)
        if source_case is None:
            raise ValueError(f"missing source_case_id: {lesson.source_case_id}")
        if source_case.status != "verified" or not source_case.regression_passed:
            raise ValueError(f"lesson requires verified source case: {lesson.source_case_id}")
        source_trace = self.traces.get(source_case.source_trace_id)
        if source_trace is None:
            raise ValueError(f"missing source_trace_id: {source_case.source_trace_id}")
        for field_name in ("repo", "tenant"):
            source_value = getattr(source_trace, field_name)
            if source_value is not None and lesson.scope.get(field_name) != source_value:
                raise ValueError(
                    f"lesson scope must preserve source {field_name}: {source_value}"
                )
        self.lessons[lesson.lesson_id] = lesson
        return lesson

    def add_project_policy(self, policy: ProjectPolicy) -> ProjectPolicy:
        _validate_project_policy(policy)
        if policy.policy_id in self.project_policies:
            raise ValueError(f"duplicate policy_id: {policy.policy_id}")
        if policy.policy_id in self.failure_cases or policy.policy_id in self.lessons:
            raise ValueError(f"duplicate memory_id across memory types: {policy.policy_id}")
        validate_lesson_contract(
            lesson_text=policy.policy_text,
            scope=policy.scope,
            confidence=policy.confidence,
        )
        self.project_policies[policy.policy_id] = policy
        return policy

    def candidate_memories(self, context: MemoryContext, *, query: str | None = None) -> list[MemoryItem]:
        context_values = _context_values(context)
        candidates: list[MemoryItem] = []

        for lesson in self.lessons.values():
            if _has_metadata_match(lesson.scope, context_values):
                candidates.append(memory_item_from_lesson(lesson))
        for policy in self.project_policies.values():
            if _has_metadata_match(policy.scope, context_values):
                candidates.append(memory_item_from_project_policy(policy))
        if context.mode in {"debug", "repair"}:
            for case in self.failure_cases.values():
                trace = self.traces.get(case.source_trace_id)
                if trace is None or case.status != "verified" or not case.regression_passed:
                    continue
                memory = memory_item_from_failure_case(case, trace)
                trace_tools = _trace_tool_names(trace)
                if context.tool is not None and trace_tools and context.tool not in trace_tools:
                    continue
                if _has_metadata_match(memory.scope, context_values):
                    candidates.append(memory)

        query_tokens = _tokens(query or "")
        if query_tokens:
            candidates = [
                memory
                for memory in candidates
                if query_tokens.intersection(_memory_tokens(memory))
            ]

        return candidates

    def log_decision(
        self,
        run_id: str,
        context: MemoryContext,
        candidate_memory_ids: list[str],
        decision: MemoryDecision,
        *,
        eval_result: EvalResult | None = None,
        memory_caused_failure: bool = False,
    ) -> MemoryUsageLog:
        used_memory_ids = list(decision.allowed_memory_ids if decision.use_memory else [])
        log = MemoryUsageLog(
            decision_id=_next_decision_id(self.usage_logs),
            run_id=run_id,
            mode=context.mode,
            candidate_memory_ids=list(candidate_memory_ids),
            used_memory_ids=used_memory_ids,
            blocked_memory_ids=list(decision.blocked_memory_ids),
            reason=decision.reason,
            risk=decision.risk,
            recommended_injection=decision.recommended_injection,
            eval_result=eval_result,
            memory_caused_failure=memory_caused_failure,
        )
        _validate_usage_log(log)
        self._validate_usage_log_memory_ids(log)
        if any(existing.decision_id == log.decision_id for existing in self.usage_logs):
            raise ValueError(f"duplicate usage log decision_id: {log.decision_id}")
        self.usage_logs.append(log)
        return log

    def _validate_usage_log_memory_ids(self, log: MemoryUsageLog) -> None:
        known_memory_ids = set(self.failure_cases).union(self.lessons, self.project_policies)
        referenced_ids = set(log.candidate_memory_ids).union(log.used_memory_ids, log.blocked_memory_ids)
        unknown_ids = sorted(referenced_ids.difference(known_memory_ids))
        if unknown_ids:
            raise ValueError(f"usage log references unknown memory IDs: {', '.join(unknown_ids)}")

    def metrics(self) -> MemoryMetrics:
        candidate_memory_count = sum(len(log.candidate_memory_ids) for log in self.usage_logs)
        used_memory_count = sum(len(log.used_memory_ids) for log in self.usage_logs)
        blocked_memory_count = sum(len(log.blocked_memory_ids) for log in self.usage_logs)
        obsolete_attempts = sum(
            1
            for log in self.usage_logs
            for memory_id in log.candidate_memory_ids
            if _is_obsolete_memory(memory_id, self.failure_cases, self.lessons, self.project_policies)
        )
        average_confidence = 0.0
        if self.lessons:
            average_confidence = sum(lesson.confidence for lesson in self.lessons.values()) / len(self.lessons)

        with_memory_results = [log.eval_result for log in self.usage_logs if log.used_memory_ids and log.eval_result]
        without_memory_results = [
            log.eval_result for log in self.usage_logs if not log.used_memory_ids and log.eval_result
        ]

        return MemoryMetrics(
            decision_count=len(self.usage_logs),
            candidate_memory_count=candidate_memory_count,
            used_memory_count=used_memory_count,
            blocked_memory_count=blocked_memory_count,
            obsolete_memory_usage_attempts=obsolete_attempts,
            average_lesson_confidence=average_confidence,
            pass_rate_with_memory=_pass_rate(with_memory_results),
            pass_rate_without_memory=_pass_rate(without_memory_results),
            wrong_memory_failure_count=sum(1 for log in self.usage_logs if log.memory_caused_failure),
        )

    def pr_memory_report(self, context: MemoryContext, *, changed_fields: list[str]) -> PRMemoryReport:
        related_case_records: list[tuple[FailureCase, Trace]] = []
        for case in self.failure_cases.values():
            trace = self.traces.get(case.source_trace_id)
            if trace and _case_matches_context(case, trace, context):
                related_case_records.append((case, trace))

        related_cases = [case for case, _trace in related_case_records]
        related_case_ids = [case.case_id for case in related_cases]
        suggested_regression_tests = [_regression_suggestion(case, context) for case in related_cases]
        warnings = [
            _change_warning(case, context, changed_field)
            for case in related_cases
            for changed_field in changed_fields
            if changed_field
            in {"prompt_version", "prompt_family", "tool_schema_version", "tool", "model", "model_family", "eval_suite"}
        ]

        return PRMemoryReport(
            related_case_ids=related_case_ids,
            suggested_regression_tests=_unique(suggested_regression_tests),
            warnings=_unique(warnings),
            related_case_provenance=[
                _case_provenance(case, trace) for case, trace in related_case_records
            ],
        )


def _context_values(context: MemoryContext) -> dict[str, str | None]:
    return {
        "repo": context.repo,
        "tenant": context.tenant,
        "branch": context.branch,
        "prompt_version": context.prompt_version,
        "prompt_family": context.prompt_family,
        "tool": context.tool,
        "tool_schema_version": context.tool_schema_version,
        "model": context.model,
        "model_family": context.model_family,
        "eval_suite": context.eval_suite,
        "task_type": context.task_type,
        "failure_type": context.failure_type,
    }


def _snapshot_records(data: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"snapshot field {key!r} must be a list")
    if any(not isinstance(record, dict) for record in value):
        raise ValueError(f"snapshot field {key!r} must contain JSON objects")
    return [dict(record) for record in value]


def _is_obsolete_memory(
    memory_id: str,
    failure_cases: Mapping[str, FailureCase],
    lessons: Mapping[str, Lesson],
    project_policies: Mapping[str, ProjectPolicy],
) -> bool:
    case = failure_cases.get(memory_id)
    if case is not None:
        return case.status == "obsolete"
    lesson = lessons.get(memory_id)
    if lesson is not None:
        return lesson.status == "obsolete"
    policy = project_policies.get(memory_id)
    if policy is not None:
        return policy.status == "obsolete"
    return False


def _next_decision_id(logs: list[MemoryUsageLog]) -> str:
    max_suffix = 0
    for log in logs:
        match = re.fullmatch(r"decision_(\d+)", log.decision_id)
        if match is not None:
            max_suffix = max(max_suffix, int(match.group(1)))
    return f"decision_{max_suffix + 1:06d}"


def _validate_trace(trace: Trace) -> None:
    if not trace.trace_id:
        raise ValueError("trace records require trace_id")
    if not trace.run_id:
        raise ValueError("trace records require run_id")
    if not trace.commit_sha:
        raise ValueError("trace records require commit_sha")
    if trace.eval_result not in EVAL_RESULTS:
        raise ValueError("eval_result must be one of: error, fail, pass, unknown")
    if type(trace.dirty) is not bool:
        raise ValueError("dirty must be a boolean")
    _validate_json_object_list(trace.retrieved_context, "retrieved_context")
    _validate_json_object_list(trace.tool_calls, "tool_calls")
    _validate_json_object_list(trace.tool_outputs, "tool_outputs")


def _validate_failure_case(case: FailureCase) -> None:
    if not case.case_id:
        raise ValueError("failure case records require case_id")
    if not case.source_trace_id:
        raise ValueError("failure case records require source_trace_id")
    if not case.commit_sha:
        raise ValueError("failure case records require commit_sha")
    if not case.failure_type:
        raise ValueError("failure case records require failure_type")
    if not case.symptom:
        raise ValueError("failure case records require symptom")
    if case.status not in FAILURE_CASE_STATUSES:
        raise ValueError("failure case status must be one of: draft, obsolete, verified")
    if type(case.regression_passed) is not bool:
        raise ValueError("regression_passed must be a boolean")
    if case.status == "verified" and (not case.fix or not case.fix_commit_sha or not case.regression_passed):
        raise ValueError("verified failure cases require fix, fix_commit_sha, and passing regression")


def _validate_lesson_record(lesson: Lesson) -> None:
    if not lesson.lesson_id:
        raise ValueError("lesson records require lesson_id")
    if not lesson.source_case_id:
        raise ValueError("lesson records require source_case_id")
    if lesson.memory_type not in MEMORY_TYPES:
        raise ValueError("lesson memory_type must be one of: episodic, policy, procedural, semantic")
    if lesson.status not in LESSON_STATUSES:
        raise ValueError("lesson status must be one of: active, obsolete")
    if type(lesson.sensitive) is not bool:
        raise ValueError("sensitive must be a boolean")
    if type(lesson.eval_leaking) is not bool:
        raise ValueError("eval_leaking must be a boolean")


def _validate_project_policy(policy: ProjectPolicy) -> None:
    if not policy.policy_id:
        raise ValueError("project policy records require policy_id")
    if not policy.policy_text.strip():
        raise ValueError("project policy records require policy_text")
    if policy.status not in LESSON_STATUSES:
        raise ValueError("project policy status must be one of: active, obsolete")
    if type(policy.sensitive) is not bool:
        raise ValueError("sensitive must be a boolean")
    if type(policy.eval_leaking) is not bool:
        raise ValueError("eval_leaking must be a boolean")


def _validate_usage_log(log: MemoryUsageLog) -> None:
    if not log.decision_id:
        raise ValueError("usage log records require decision_id")
    if not log.run_id:
        raise ValueError("usage log records require run_id")
    if log.mode not in MODES:
        raise ValueError("usage log mode must be one of: debug, eval, planning, production, regression, repair")
    _validate_memory_id_list(log.candidate_memory_ids, "candidate_memory_ids")
    _validate_memory_id_list(log.used_memory_ids, "used_memory_ids")
    _validate_memory_id_list(log.blocked_memory_ids, "blocked_memory_ids")
    if not isinstance(log.reason, str):
        raise ValueError("usage log reason must be a string")
    if log.risk not in DECISION_RISKS:
        raise ValueError("usage log risk must be one of: high, low, medium, none")
    if log.recommended_injection not in RECOMMENDED_INJECTIONS:
        raise ValueError(
            "usage log recommended_injection must be one of: full_case_summary, none, pointer_only, short_summary"
        )
    if log.eval_result is not None and log.eval_result not in EVAL_RESULTS:
        raise ValueError("usage log eval_result must be one of: error, fail, pass, unknown")
    if type(log.memory_caused_failure) is not bool:
        raise ValueError("memory_caused_failure must be a boolean")
    if log.used_memory_ids and log.recommended_injection == "none":
        raise ValueError("usage log recommended_injection cannot be 'none' when memory was used")
    if not log.used_memory_ids and log.recommended_injection != "none":
        raise ValueError("usage log recommended_injection must be 'none' when no memory was used")
    if log.memory_caused_failure and (not log.used_memory_ids or log.eval_result not in {"fail", "error"}):
        raise ValueError("usage log memory_caused_failure requires failed or errored memory use")
    missing_used_ids = [
        memory_id for memory_id in log.used_memory_ids if memory_id not in log.candidate_memory_ids
    ]
    if missing_used_ids:
        raise ValueError(f"used memory ids must be present in candidates: {', '.join(missing_used_ids)}")
    missing_blocked_ids = [
        memory_id for memory_id in log.blocked_memory_ids if memory_id not in log.candidate_memory_ids
    ]
    if missing_blocked_ids:
        raise ValueError(f"blocked memory ids must be present in candidates: {', '.join(missing_blocked_ids)}")
    used_and_blocked = [
        memory_id for memory_id in log.used_memory_ids if memory_id in log.blocked_memory_ids
    ]
    if used_and_blocked:
        raise ValueError(f"memory ids cannot be both used and blocked: {', '.join(used_and_blocked)}")


def _validate_json_object_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"trace {field_name} must be a list of JSON objects")


def _validate_memory_id_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"usage log {field_name} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"usage log {field_name} must not contain duplicate memory IDs")


def _lessons_to_yaml(lessons: list[Lesson]) -> str:
    if not lessons:
        return "lessons: []\n"

    lines = ["lessons:"]
    for lesson in lessons:
        lines.extend(
            [
                f"  - lesson_id: {_yaml_scalar(lesson.lesson_id)}",
                f"    source_case_id: {_yaml_scalar(lesson.source_case_id)}",
                f"    memory_type: {_yaml_scalar(lesson.memory_type)}",
                f"    status: {_yaml_scalar(lesson.status)}",
                f"    confidence: {lesson.confidence}",
                f"    sensitive: {_yaml_bool(lesson.sensitive)}",
                f"    eval_leaking: {_yaml_bool(lesson.eval_leaking)}",
                "    scope:",
            ]
        )
        for key, value in lesson.scope.items():
            lines.append(f"      {key}: {_yaml_scalar(value)}")
        lines.append("    lesson_text: >")
        for text_line in lesson.lesson_text.splitlines() or [""]:
            lines.append(f"      {text_line}")
    return "\n".join(lines) + "\n"


def _lessons_from_yaml(text: str) -> list[dict[str, Any]]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("lessons YAML must not be empty")
    if lines[0] == "lessons: []":
        return []
    if lines[0] != "lessons:":
        raise ValueError("lessons YAML must start with 'lessons:'")

    lessons: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    block_key: str | None = None
    block_lines: list[str] = []

    def finish_block() -> None:
        nonlocal block_key, block_lines
        if current is not None and block_key is not None:
            current[block_key] = "\n".join(block_lines).strip()
        block_key = None
        block_lines = []

    def finish_lesson() -> None:
        finish_block()
        if current is not None:
            lessons.append(current)

    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if stripped.startswith("- "):
            finish_lesson()
            current = {}
            block_key = None
            item = stripped[2:]
            if item:
                key, value = _yaml_key_value(item)
                current[key] = _parse_yaml_scalar(value)
            continue

        if current is None:
            raise ValueError("lesson record must start with '-'")

        if block_key is not None and indent >= 6:
            block_lines.append(raw_line[6:] if raw_line.startswith("      ") else stripped)
            continue

        if indent == 4:
            finish_block()
            key, value = _yaml_key_value(stripped)
            if key == "scope" and value == "":
                current["scope"] = {}
                continue
            if key == "lesson_text" and value in {">", "|"}:
                block_key = "lesson_text"
                block_lines = []
                continue
            current[key] = _parse_yaml_scalar(value)
            continue

        if indent == 6 and "scope" in current:
            key, value = _yaml_key_value(stripped)
            current["scope"][key] = _parse_yaml_scalar(value)
            continue

        raise ValueError(f"unsupported lessons YAML line: {raw_line}")

    finish_lesson()
    return lessons


def _yaml_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"expected YAML key/value line: {text}")
    key, value = text.split(":", 1)
    return key.strip(), value.strip()


def _parse_yaml_scalar(value: str) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    try:
        return float(value)
    except ValueError:
        return value


def _yaml_scalar(value: str) -> str:
    return json.dumps(value)


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def _has_metadata_match(scope: dict[str, str], context_values: dict[str, str | None]) -> bool:
    return all(context_values.get(key) == value for key, value in scope.items())


def _pass_rate(results: list[EvalResult]) -> float | None:
    if not results:
        return None
    return sum(1 for result in results if result == "pass") / len(results)


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 2}


def _memory_tokens(memory: MemoryItem) -> set[str]:
    scope_text = " ".join(f"{key} {value}" for key, value in memory.scope.items())
    return _tokens(f"{memory.memory_id} {memory.text} {scope_text}")


def _case_matches_context(case: FailureCase, trace: Trace, context: MemoryContext) -> bool:
    if case.status != "verified" or not case.regression_passed:
        return False
    if trace.repo != context.repo:
        return False
    if trace.tenant != context.tenant:
        return False
    if context.failure_type is not None and case.failure_type != context.failure_type:
        return False
    if context.tool is not None and context.tool not in _trace_tool_names(trace):
        return False
    for field_name in ["prompt_version", "prompt_family", "tool_schema_version", "model", "eval_suite"]:
        context_value = getattr(context, field_name)
        if context_value is not None and getattr(trace, field_name) != context_value:
            return False
    return True


def _trace_tool_names(trace: Trace) -> set[str]:
    return {str(call["name"]) for call in trace.tool_calls if call.get("name")}


def _regression_suggestion(case: FailureCase, context: MemoryContext) -> str:
    tool = context.tool or "affected tool"
    return f"Run {case.failure_type} regression for tool {tool} before merging."


def _case_provenance(case: FailureCase, trace: Trace) -> PRCaseProvenance:
    return PRCaseProvenance(
        case_id=case.case_id,
        source_trace_id=case.source_trace_id,
        commit_sha=case.commit_sha,
        fix_commit_sha=case.fix_commit_sha,
        trace_uri=trace.trace_uri,
        failure_type=case.failure_type,
    )


def _change_warning(case: FailureCase, context: MemoryContext, changed_field: str) -> str:
    target = context.tool or "known failure area"
    return f"{changed_field} change touches known failure case {case.case_id} for {target}."


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
