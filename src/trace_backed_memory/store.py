from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any

from .lifecycle import (
    memory_item_from_failure_case,
    memory_item_from_lesson,
    memory_item_from_project_policy,
    obsolete_failure_case as transition_failure_case_to_obsolete,
    obsolete_lesson as transition_lesson_to_obsolete,
    obsolete_project_policy as transition_project_policy_to_obsolete,
    review_failure_case as transition_review_failure_case,
    validate_lesson_contract,
    verify_failure_case as transition_verify_failure_case,
)
from .models import (
    EvalResult,
    FailureCase,
    GatedMemoryResult,
    Lesson,
    MemoryContext,
    MemoryDecision,
    MemoryGateRequest,
    MemoryItem,
    MemoryMetrics,
    PRCaseProvenance,
    MemoryUsageLog,
    PRMemoryReport,
    ProjectPolicy,
    Trace,
)
from .policy import (
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
    apply_llm_gate_decision,
    build_injection_snippet,
    build_llm_gate_prompt,
    parse_memory_decision,
    system_gate,
    validate_memory_context,
)

Snapshot = dict[str, Any]
EVAL_RESULTS = {"pass", "fail", "error", "unknown"}
FAILURE_CASE_STATUSES = {"draft", "verified", "obsolete"}
LESSON_STATUSES = {"active", "obsolete"}
MEMORY_TYPES = {"procedural", "semantic", "episodic", "policy"}
MODES = {"debug", "repair", "regression", "planning", "eval", "production"}
DECISION_RISKS = {"none", "low", "medium", "high"}
RECOMMENDED_INJECTIONS = {"none", "short_summary", "full_case_summary", "pointer_only"}
SNAPSHOT_VERSION = 2
SNAPSHOT_COLLECTION_KEYS = frozenset(
    {"traces", "failure_cases", "lessons", "project_policies", "usage_logs"}
)
SNAPSHOT_V2_KEYS = SNAPSHOT_COLLECTION_KEYS.union({"snapshot_version"})


def _synchronized(method):
    @wraps(method)
    def synchronized(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return synchronized


class TraceBackedMemoryStore:
    """Small in-memory MVP store for trace-backed memory workflows."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._traces: dict[str, Trace] = {}
        self._failure_cases: dict[str, FailureCase] = {}
        self._lessons: dict[str, Lesson] = {}
        self._project_policies: dict[str, ProjectPolicy] = {}
        self._usage_logs: list[MemoryUsageLog] = []
        self._pending_gate_requests: dict[str, MemoryGateRequest] = {}
        self._finalized_gate_request_ids: set[str] = set()
        self._store_token = object()
        self._next_gate_request_number = 1

    @property
    @_synchronized
    def traces(self) -> Mapping[str, Trace]:
        return MappingProxyType(deepcopy(self._traces))

    @property
    @_synchronized
    def failure_cases(self) -> Mapping[str, FailureCase]:
        return MappingProxyType(deepcopy(self._failure_cases))

    @property
    @_synchronized
    def lessons(self) -> Mapping[str, Lesson]:
        return MappingProxyType(deepcopy(self._lessons))

    @property
    @_synchronized
    def project_policies(self) -> Mapping[str, ProjectPolicy]:
        return MappingProxyType(deepcopy(self._project_policies))

    @property
    @_synchronized
    def usage_logs(self) -> list[MemoryUsageLog]:
        return deepcopy(self._usage_logs)

    @_synchronized
    def to_snapshot(self) -> Snapshot:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "traces": [asdict(self._traces[trace_id]) for trace_id in sorted(self._traces)],
            "failure_cases": [
                asdict(self._failure_cases[case_id]) for case_id in sorted(self._failure_cases)
            ],
            "lessons": [asdict(self._lessons[lesson_id]) for lesson_id in sorted(self._lessons)],
            "project_policies": [
                asdict(self._project_policies[policy_id])
                for policy_id in sorted(self._project_policies)
            ],
            "usage_logs": [
                asdict(log) for log in sorted(self._usage_logs, key=lambda log: log.decision_id)
            ],
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "TraceBackedMemoryStore":
        if not isinstance(data, Mapping):
            raise ValueError("memory store snapshot must be a JSON object")
        is_v2 = _validate_snapshot_envelope(data)
        store = cls()
        for trace_data in _snapshot_records(data, "traces"):
            store.record_trace(_snapshot_record_instance(Trace, trace_data, "trace"))
        for case_data in _snapshot_records(data, "failure_cases"):
            store.add_failure_case(
                _snapshot_record_instance(FailureCase, case_data, "failure case")
            )
        for lesson_data in _snapshot_records(data, "lessons"):
            store.add_lesson(_snapshot_record_instance(Lesson, lesson_data, "lesson"))
        for policy_data in _snapshot_records(data, "project_policies"):
            store.add_project_policy(
                _snapshot_record_instance(
                    ProjectPolicy, policy_data, "project policy"
                )
            )
        for log_data in _snapshot_records(data, "usage_logs"):
            if is_v2:
                _validate_v2_usage_log_record(log_data)
            else:
                log_data = store._migrate_legacy_usage_log(log_data)
            log = _snapshot_record_instance(
                MemoryUsageLog, log_data, "usage log"
            )
            _validate_usage_log(log)
            store._validate_usage_log_memory_ids(log)
            store._validate_usage_log_trace(log)
            if any(existing.decision_id == log.decision_id for existing in store._usage_logs):
                raise ValueError(f"duplicate usage log decision_id: {log.decision_id}")
            store._usage_logs.append(deepcopy(log))
        return store

    @_synchronized
    def save_json(self, path: str | Path) -> None:
        target = Path(path)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            ) as temporary_file:
                temp_path = Path(temporary_file.name)
                json.dump(
                    self.to_snapshot(),
                    temporary_file,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                temporary_file.write("\n")
                temporary_file.flush()
            os.replace(temp_path, target)
        except BaseException:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    @classmethod
    def load_json(cls, path: str | Path) -> "TraceBackedMemoryStore":
        data = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
        if not isinstance(data, Mapping):
            raise ValueError("memory store snapshot must be a JSON object")
        return cls.from_snapshot(data)

    @_synchronized
    def save_lessons_yaml(self, path: str | Path) -> None:
        active_lessons = [lesson for lesson in self._lessons.values() if lesson.status == "active"]
        Path(path).write_text(_lessons_to_yaml(active_lessons), encoding="utf-8")

    @_synchronized
    def load_lessons_yaml(self, path: str | Path) -> list[Lesson]:
        lesson_records = _lessons_from_yaml(Path(path).read_text(encoding="utf-8"))
        return [self.add_lesson(Lesson(**record)) for record in lesson_records]

    @_synchronized
    def record_trace(self, trace: Trace) -> Trace:
        stored_trace = deepcopy(trace)
        _validate_trace(stored_trace)
        if stored_trace.trace_id in self._traces:
            raise ValueError(f"duplicate trace_id: {stored_trace.trace_id}")
        self._traces[stored_trace.trace_id] = stored_trace
        return deepcopy(stored_trace)

    @_synchronized
    def add_failure_case(self, case: FailureCase) -> FailureCase:
        stored_case = deepcopy(case)
        _validate_failure_case(stored_case)
        if stored_case.case_id in self._failure_cases:
            raise ValueError(f"duplicate case_id: {stored_case.case_id}")
        if stored_case.case_id in self._lessons or stored_case.case_id in self._project_policies:
            raise ValueError(f"duplicate memory_id across memory types: {stored_case.case_id}")
        source_trace = self._traces.get(stored_case.source_trace_id)
        if source_trace is None:
            raise ValueError(f"missing source_trace_id: {stored_case.source_trace_id}")
        if stored_case.commit_sha != source_trace.commit_sha:
            raise ValueError(
                f"failure case commit_sha does not match source trace: {stored_case.case_id}"
            )
        self._failure_cases[stored_case.case_id] = stored_case
        return deepcopy(stored_case)

    @_synchronized
    def review_failure_case(
        self,
        case_id: str,
        *,
        reviewed_by: str,
        root_cause: str,
        failure_type: str | None = None,
        symptom: str | None = None,
        review_notes: str | None = None,
        reviewed_at: str | None = None,
    ) -> FailureCase:
        current = _stored_record(
            self._failure_cases, case_id, "failure case"
        )
        reviewed = transition_review_failure_case(
            current,
            reviewed_by=reviewed_by,
            root_cause=root_cause,
            failure_type=failure_type,
            symptom=symptom,
            review_notes=review_notes,
            reviewed_at=reviewed_at,
        )
        _validate_failure_case(reviewed)
        self._failure_cases[case_id] = reviewed
        return deepcopy(reviewed)

    @_synchronized
    def verify_failure_case(
        self,
        case_id: str,
        *,
        fix: str,
        fix_commit_sha: str,
        regression_passed: bool,
    ) -> FailureCase:
        current = _stored_record(
            self._failure_cases, case_id, "failure case"
        )
        verified = transition_verify_failure_case(
            current,
            fix=fix,
            fix_commit_sha=fix_commit_sha,
            regression_passed=regression_passed,
        )
        _validate_failure_case(verified)
        self._failure_cases[case_id] = verified
        return deepcopy(verified)

    @_synchronized
    def obsolete_failure_case(self, case_id: str) -> FailureCase:
        current = _stored_record(
            self._failure_cases, case_id, "failure case"
        )
        if current.status == "obsolete":
            return deepcopy(current)

        obsolete_case = transition_failure_case_to_obsolete(current)
        obsolete_lessons = {
            lesson_id: transition_lesson_to_obsolete(lesson)
            for lesson_id, lesson in self._lessons.items()
            if lesson.source_case_id == case_id and lesson.status == "active"
        }
        _validate_failure_case(obsolete_case)
        for lesson in obsolete_lessons.values():
            _validate_lesson_record(lesson)
            validate_lesson_contract(
                lesson_text=lesson.lesson_text,
                scope=lesson.scope,
                confidence=lesson.confidence,
            )

        self._failure_cases[case_id] = obsolete_case
        self._lessons.update(obsolete_lessons)
        return deepcopy(obsolete_case)

    @_synchronized
    def add_lesson(self, lesson: Lesson) -> Lesson:
        stored_lesson = deepcopy(lesson)
        _validate_lesson_record(stored_lesson)
        if stored_lesson.lesson_id in self._lessons:
            raise ValueError(f"duplicate lesson_id: {stored_lesson.lesson_id}")
        if (
            stored_lesson.lesson_id in self._failure_cases
            or stored_lesson.lesson_id in self._project_policies
        ):
            raise ValueError(
                f"duplicate memory_id across memory types: {stored_lesson.lesson_id}"
            )
        validate_lesson_contract(
            lesson_text=stored_lesson.lesson_text,
            scope=stored_lesson.scope,
            confidence=stored_lesson.confidence,
        )
        source_case = self._failure_cases.get(stored_lesson.source_case_id)
        if source_case is None:
            raise ValueError(f"missing source_case_id: {stored_lesson.source_case_id}")
        if (
            source_case.status == "draft"
            or not source_case.regression_passed
            or (stored_lesson.status == "active" and source_case.status != "verified")
        ):
            raise ValueError(
                f"lesson requires verified source case: {stored_lesson.source_case_id}"
            )
        source_trace = self._traces.get(source_case.source_trace_id)
        if source_trace is None:
            raise ValueError(f"missing source_trace_id: {source_case.source_trace_id}")
        for field_name in ("repo", "tenant"):
            source_value = getattr(source_trace, field_name)
            if source_value is not None and stored_lesson.scope.get(field_name) != source_value:
                raise ValueError(
                    f"lesson scope must preserve source {field_name}: {source_value}"
                )
        self._lessons[stored_lesson.lesson_id] = stored_lesson
        return deepcopy(stored_lesson)

    @_synchronized
    def obsolete_lesson(self, lesson_id: str) -> Lesson:
        current = _stored_record(self._lessons, lesson_id, "lesson")
        if current.status == "obsolete":
            return deepcopy(current)

        obsolete = transition_lesson_to_obsolete(current)
        _validate_lesson_record(obsolete)
        validate_lesson_contract(
            lesson_text=obsolete.lesson_text,
            scope=obsolete.scope,
            confidence=obsolete.confidence,
        )
        self._lessons[lesson_id] = obsolete
        return deepcopy(obsolete)

    @_synchronized
    def add_project_policy(self, policy: ProjectPolicy) -> ProjectPolicy:
        stored_policy = deepcopy(policy)
        _validate_project_policy(stored_policy)
        if stored_policy.policy_id in self._project_policies:
            raise ValueError(f"duplicate policy_id: {stored_policy.policy_id}")
        if stored_policy.policy_id in self._failure_cases or stored_policy.policy_id in self._lessons:
            raise ValueError(
                f"duplicate memory_id across memory types: {stored_policy.policy_id}"
            )
        validate_lesson_contract(
            lesson_text=stored_policy.policy_text,
            scope=stored_policy.scope,
            confidence=stored_policy.confidence,
        )
        self._project_policies[stored_policy.policy_id] = stored_policy
        return deepcopy(stored_policy)

    @_synchronized
    def obsolete_project_policy(self, policy_id: str) -> ProjectPolicy:
        current = _stored_record(
            self._project_policies, policy_id, "project policy"
        )
        if current.status == "obsolete":
            return deepcopy(current)

        obsolete = transition_project_policy_to_obsolete(current)
        _validate_project_policy(obsolete)
        validate_lesson_contract(
            lesson_text=obsolete.policy_text,
            scope=obsolete.scope,
            confidence=obsolete.confidence,
        )
        self._project_policies[policy_id] = obsolete
        return deepcopy(obsolete)

    @_synchronized
    def candidate_memories(self, context: MemoryContext, *, query: str | None = None) -> list[MemoryItem]:
        validate_memory_context(context)
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be a string or None")
        context_values = _context_values(context)
        candidates: list[MemoryItem] = []

        for lesson in self._lessons.values():
            if _has_metadata_match(lesson.scope, context_values):
                candidates.append(memory_item_from_lesson(lesson))
        for policy in self._project_policies.values():
            if _has_metadata_match(policy.scope, context_values):
                candidates.append(memory_item_from_project_policy(policy))
        if context.mode in {"debug", "repair"}:
            for case in self._failure_cases.values():
                trace = self._traces.get(case.source_trace_id)
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

        return sorted(candidates, key=lambda memory: memory.memory_id)

    @_synchronized
    def prepare_memory(
        self,
        context: MemoryContext,
        *,
        task: str,
        query: str | None = None,
        context_summary: str = "",
    ) -> MemoryGateRequest:
        validate_memory_context(context)
        candidates = self.candidate_memories(context, query=query)
        system_allowed, system_blocked = system_gate(context, candidates)
        request = MemoryGateRequest(
            request_id=f"gate_request_{self._next_gate_request_number:06d}",
            context=context,
            candidate_memory_ids=tuple(memory.memory_id for memory in candidates),
            system_allowed_memory_ids=tuple(
                memory.memory_id for memory in system_allowed
            ),
            system_blocked=tuple(system_blocked.items()),
            prompt=build_llm_gate_prompt(
                context,
                system_allowed,
                task=task,
                context_summary=context_summary,
            ),
            _store_token=self._store_token,
        )
        self._next_gate_request_number += 1
        self._pending_gate_requests[request.request_id] = deepcopy(
            request, {id(self._store_token): self._store_token}
        )
        return request

    @_synchronized
    def finalize_memory(
        self,
        request: MemoryGateRequest,
        decision_payload: str | Mapping[str, Any],
        *,
        trace_id: str,
        eval_result: EvalResult | None = None,
        memory_caused_failure: bool = False,
    ) -> GatedMemoryResult:
        if not isinstance(request, MemoryGateRequest) or request._store_token is not self._store_token:
            raise ValueError("gate request does not belong to this store")
        _validate_required_string(
            request.request_id,
            "request_id",
            "gate request records require",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        validate_memory_context(request.context)
        _validate_required_string(
            trace_id,
            "trace_id",
            "finalization requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        _validate_runtime_outcome(eval_result, memory_caused_failure)
        if request.request_id in self._finalized_gate_request_ids:
            raise ValueError(f"gate request already finalized: {request.request_id}")
        pending = self._pending_gate_requests.get(request.request_id)
        if pending is None or request != pending:
            raise ValueError("gate request does not belong to this store")

        trace = self._traces.get(trace_id)
        if trace is None:
            raise ValueError(f"unknown trace_id: {trace_id}")
        _validate_trace_context(trace, request.context)

        candidates = self._memory_items(request.candidate_memory_ids)
        system_allowed, system_blocked = system_gate(request.context, candidates)
        decision = parse_memory_decision(decision_payload)
        final_allowed, final_decision = apply_llm_gate_decision(
            system_allowed, system_blocked, decision
        )
        snippet = build_injection_snippet(final_allowed, decision=final_decision)
        log = self._new_usage_log(
            trace=trace,
            context=request.context,
            candidates=candidates,
            decision=final_decision,
            system_blocked=system_blocked,
            eval_result=eval_result,
            memory_caused_failure=memory_caused_failure,
        )

        self._usage_logs.append(log)
        self._pending_gate_requests.pop(request.request_id)
        self._finalized_gate_request_ids.add(request.request_id)
        return GatedMemoryResult(
            request_id=request.request_id,
            trace_id=trace.trace_id,
            decision_id=log.decision_id,
            use_memory=final_decision.use_memory,
            allowed_memory_ids=tuple(final_decision.allowed_memory_ids),
            blocked_memory_ids=tuple(final_decision.blocked_memory_ids),
            reason=final_decision.reason,
            risk=final_decision.risk,
            recommended_injection=final_decision.recommended_injection,
            snippet=snippet,
        )

    @_synchronized
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
        _validate_required_string(
            run_id,
            "run_id",
            "usage logging requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        validate_memory_context(context)
        _validate_memory_id_list(candidate_memory_ids, "candidate_memory_ids")
        if not isinstance(decision, MemoryDecision):
            raise ValueError("decision must be a MemoryDecision")
        _validate_runtime_outcome(eval_result, memory_caused_failure)
        trace = self._trace_for_run_id(run_id)
        _validate_trace_context(trace, context)
        candidates = self._memory_items(candidate_memory_ids)
        system_allowed, system_blocked = system_gate(context, candidates)
        validated_decision = parse_memory_decision(asdict(decision))
        overlapping_ids = sorted(
            set(validated_decision.allowed_memory_ids).intersection(
                validated_decision.blocked_memory_ids
            )
        )
        if overlapping_ids:
            raise ValueError(
                "memory ids cannot be both allowed and blocked: "
                + ", ".join(overlapping_ids)
            )
        _final_allowed, final_decision = apply_llm_gate_decision(
            system_allowed, system_blocked, validated_decision
        )
        log = self._new_usage_log(
            trace=trace,
            context=context,
            candidates=candidates,
            decision=final_decision,
            system_blocked=system_blocked,
            eval_result=eval_result,
            memory_caused_failure=memory_caused_failure,
        )
        self._usage_logs.append(log)
        return deepcopy(log)

    def _trace_for_run_id(self, run_id: str) -> Trace:
        _validate_required_string(
            run_id,
            "run_id",
            "trace lookup requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        matches = [trace for trace in self._traces.values() if trace.run_id == run_id]
        if not matches:
            raise ValueError(f"unknown run_id: {run_id}")
        if len(matches) > 1:
            raise ValueError(f"run_id does not resolve to one trace: {run_id}")
        return matches[0]

    def _memory_items(self, memory_ids: tuple[str, ...] | list[str]) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        unknown_ids: list[str] = []
        for memory_id in memory_ids:
            if memory_id in self._lessons:
                items.append(memory_item_from_lesson(self._lessons[memory_id]))
            elif memory_id in self._project_policies:
                items.append(memory_item_from_project_policy(self._project_policies[memory_id]))
            elif memory_id in self._failure_cases:
                case = self._failure_cases[memory_id]
                trace = self._traces[case.source_trace_id]
                memory = memory_item_from_failure_case(
                    replace(case, status="verified", regression_passed=True), trace
                )
                items.append(replace(memory, status=case.status))
            else:
                unknown_ids.append(memory_id)
        if unknown_ids:
            raise ValueError(
                f"usage log references unknown memory IDs: {', '.join(sorted(unknown_ids))}"
            )
        return items

    def _new_usage_log(
        self,
        *,
        trace: Trace,
        context: MemoryContext,
        candidates: list[MemoryItem],
        decision: MemoryDecision,
        system_blocked: dict[str, str],
        eval_result: EvalResult | None,
        memory_caused_failure: bool,
    ) -> MemoryUsageLog:
        candidate_memory_ids = [memory.memory_id for memory in candidates]
        log = MemoryUsageLog(
            decision_id=_next_decision_id(self._usage_logs),
            run_id=trace.run_id,
            mode=context.mode,
            candidate_memory_ids=candidate_memory_ids,
            used_memory_ids=list(
                decision.allowed_memory_ids if decision.use_memory else []
            ),
            blocked_memory_ids=list(decision.blocked_memory_ids),
            reason=decision.reason,
            risk=decision.risk,
            recommended_injection=decision.recommended_injection,
            eval_result=eval_result,
            memory_caused_failure=memory_caused_failure,
            trace_id=trace.trace_id,
            context=_context_evidence(context),
            candidate_memory_statuses={
                memory.memory_id: memory.status for memory in candidates
            },
            system_blocked_reasons=dict(system_blocked),
            created_at=_utc_timestamp(),
        )
        _validate_usage_log(log)
        self._validate_usage_log_memory_ids(log)
        if any(existing.decision_id == log.decision_id for existing in self._usage_logs):
            raise ValueError(f"duplicate usage log decision_id: {log.decision_id}")
        return log

    def _validate_usage_log_memory_ids(self, log: MemoryUsageLog) -> None:
        known_memory_ids = set(self._failure_cases).union(
            self._lessons, self._project_policies
        )
        referenced_ids = set(log.candidate_memory_ids).union(log.used_memory_ids, log.blocked_memory_ids)
        unknown_ids = sorted(referenced_ids.difference(known_memory_ids))
        if unknown_ids:
            raise ValueError(f"usage log references unknown memory IDs: {', '.join(unknown_ids)}")

    def _validate_usage_log_trace(self, log: MemoryUsageLog) -> None:
        if not isinstance(log.trace_id, str) or not log.trace_id:
            raise ValueError("usage log records require trace_id")
        trace = self._traces.get(log.trace_id)
        if trace is None:
            raise ValueError(f"unknown trace_id: {log.trace_id}")
        if log.run_id != trace.run_id:
            raise ValueError(
                f"usage log run_id does not match trace: {log.trace_id}"
            )
        if trace.repo is None:
            raise ValueError(
                f"usage log trace repo is required for context evidence: {log.trace_id}"
            )

        expected_context = {
            "mode": log.mode,
            "repo": trace.repo,
            "commit_sha": trace.commit_sha,
        }
        if trace.tenant is not None:
            expected_context["tenant"] = trace.tenant

        for field_name, expected_value in expected_context.items():
            if log.context.get(field_name) != expected_value:
                raise ValueError(
                    f"usage log context {field_name} does not match trace or mode: {log.trace_id}"
                )
        if trace.tenant is None and "tenant" in log.context:
            raise ValueError(
                f"usage log context tenant does not match trace or mode: {log.trace_id}"
            )

    def _migrate_legacy_usage_log(self, log_data: dict[str, Any]) -> dict[str, Any]:
        legacy_log = _snapshot_record_instance(
            MemoryUsageLog, log_data, "usage log"
        )
        _validate_memory_id_list(
            legacy_log.candidate_memory_ids, "candidate_memory_ids"
        )
        supplied_trace_id = log_data.get("trace_id")
        if supplied_trace_id not in (None, ""):
            if not isinstance(supplied_trace_id, str):
                raise ValueError("usage log trace_id must be a non-empty string")
            trace = self._traces.get(supplied_trace_id)
            if trace is None:
                raise ValueError(f"unknown trace_id: {supplied_trace_id}")
        else:
            trace = self._trace_for_run_id(legacy_log.run_id)
        if legacy_log.run_id != trace.run_id:
            raise ValueError(
                f"usage log run_id does not match trace: {trace.trace_id}"
            )
        if trace.repo is None:
            raise ValueError(
                f"legacy usage log trace repo is required for context evidence: {trace.trace_id}"
            )

        if "context" in log_data and log_data["context"] != {}:
            context = log_data["context"]
        else:
            context = {
                "mode": legacy_log.mode,
                "repo": trace.repo,
                "commit_sha": trace.commit_sha,
            }
            if trace.tenant is not None:
                context["tenant"] = trace.tenant

        if (
            legacy_log.candidate_memory_ids
            and (
                "candidate_memory_statuses" not in log_data
                or log_data["candidate_memory_statuses"] == {}
            )
        ):
            candidate_memory_statuses = {
                memory.memory_id: memory.status
                for memory in self._memory_items(legacy_log.candidate_memory_ids)
            }
        else:
            candidate_memory_statuses = log_data.get(
                "candidate_memory_statuses", {}
            )

        system_blocked_reasons = log_data.get("system_blocked_reasons", {})

        migrated = dict(log_data)
        migrated.update(
            {
                "trace_id": trace.trace_id,
                "context": context,
                "candidate_memory_statuses": candidate_memory_statuses,
                "system_blocked_reasons": system_blocked_reasons,
            }
        )
        return migrated

    @_synchronized
    def metrics(self) -> MemoryMetrics:
        candidate_memory_count = sum(len(log.candidate_memory_ids) for log in self._usage_logs)
        used_memory_count = sum(len(log.used_memory_ids) for log in self._usage_logs)
        blocked_memory_count = sum(len(log.blocked_memory_ids) for log in self._usage_logs)
        obsolete_attempts = sum(
            1
            for log in self._usage_logs
            for status in log.candidate_memory_statuses.values()
            if status == "obsolete"
        )
        average_confidence = 0.0
        if self._lessons:
            average_confidence = sum(
                lesson.confidence for lesson in self._lessons.values()
            ) / len(self._lessons)

        with_memory_results = [
            log.eval_result
            for log in self._usage_logs
            if log.used_memory_ids and log.eval_result
        ]
        without_memory_results = [
            log.eval_result
            for log in self._usage_logs
            if not log.used_memory_ids and log.eval_result
        ]

        return MemoryMetrics(
            decision_count=len(self._usage_logs),
            candidate_memory_count=candidate_memory_count,
            used_memory_count=used_memory_count,
            blocked_memory_count=blocked_memory_count,
            obsolete_memory_usage_attempts=obsolete_attempts,
            average_lesson_confidence=average_confidence,
            pass_rate_with_memory=_pass_rate(with_memory_results),
            pass_rate_without_memory=_pass_rate(without_memory_results),
            wrong_memory_failure_count=sum(
                1 for log in self._usage_logs if log.memory_caused_failure
            ),
        )

    @_synchronized
    def pr_memory_report(self, context: MemoryContext, *, changed_fields: list[str]) -> PRMemoryReport:
        related_case_records: list[tuple[FailureCase, Trace]] = []
        for case in self._failure_cases.values():
            trace = self._traces.get(case.source_trace_id)
            if trace and _case_matches_context(case, trace, context):
                related_case_records.append((case, trace))
        related_case_records.sort(key=lambda record: record[0].case_id)

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


def _stored_record(records: Mapping[str, Any], record_id: str, record_label: str) -> Any:
    record = records.get(record_id)
    if record is None:
        raise ValueError(f"unknown {record_label} ID: {record_id}")
    return record


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


def _context_evidence(context: MemoryContext) -> dict[str, str]:
    return {
        key: value
        for key, value in asdict(context).items()
        if isinstance(value, str)
    }


def _validate_trace_context(trace: Trace, context: MemoryContext) -> None:
    validate_memory_context(context)
    for field_name in ("repo", "commit_sha", "tenant"):
        if getattr(trace, field_name) != getattr(context, field_name):
            raise ValueError(
                f"trace {field_name} does not match memory context: {trace.trace_id}"
            )


def _snapshot_records(data: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = data[key]
    if not isinstance(value, list):
        raise ValueError(f"snapshot field {key!r} must be a list")
    if any(not isinstance(record, dict) for record in value):
        raise ValueError(f"snapshot field {key!r} must contain JSON objects")
    return [dict(record) for record in value]


def _snapshot_record_instance(
    record_type: type[Any], data: dict[str, Any], record_label: str
) -> Any:
    try:
        return record_type(**data)
    except TypeError as exc:
        raise ValueError(f"invalid {record_label} record: {exc}") from exc


def _validate_snapshot_envelope(data: Mapping[str, Any]) -> bool:
    keys = set(data)
    if keys == SNAPSHOT_COLLECTION_KEYS:
        return False
    if keys != SNAPSHOT_V2_KEYS:
        raise ValueError("snapshot envelope must be exact legacy v1 or version 2")
    if type(data["snapshot_version"]) is not int or data["snapshot_version"] != SNAPSHOT_VERSION:
        raise ValueError("snapshot envelope requires snapshot_version 2")
    return True


def _validate_v2_usage_log_record(log_data: dict[str, Any]) -> None:
    required_audit_fields = {
        "trace_id",
        "context",
        "candidate_memory_statuses",
        "system_blocked_reasons",
    }
    missing_fields = sorted(required_audit_fields.difference(log_data))
    if missing_fields:
        raise ValueError(
            "v2 usage log requires audit fields: " + ", ".join(missing_fields)
        )


def _next_decision_id(logs: list[MemoryUsageLog]) -> str:
    max_suffix = 0
    for log in logs:
        match = re.fullmatch(r"decision_(\d+)", log.decision_id)
        if match is not None:
            max_suffix = max(max_suffix, int(match.group(1)))
    return f"decision_{max_suffix + 1:06d}"


def _validate_trace(trace: Trace) -> None:
    for field_name in ("trace_id", "run_id"):
        _validate_required_string(
            getattr(trace, field_name),
            field_name,
            "trace records require",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
    _validate_required_string(
        trace.commit_sha,
        "commit_sha",
        "trace records require",
        max_chars=METADATA_VALUE_MAX_CHARS,
    )
    for field_name in (
        "repo",
        "tenant",
        "branch",
        "prompt_version",
        "prompt_family",
        "tool_schema_version",
        "model",
        "eval_suite",
        "input_hash",
        "output_hash",
        "error",
        "trace_uri",
    ):
        _validate_optional_string(
            getattr(trace, field_name),
            field_name,
            "trace",
            max_chars=(
                None if field_name == "error" else METADATA_VALUE_MAX_CHARS
            ),
        )
    if not isinstance(trace.eval_result, str) or trace.eval_result not in EVAL_RESULTS:
        raise ValueError("eval_result must be one of: error, fail, pass, unknown")
    if type(trace.dirty) is not bool:
        raise ValueError("dirty must be a boolean")
    if trace.latency_ms is not None and type(trace.latency_ms) is not int:
        raise ValueError("latency_ms must be an integer or None")
    if trace.cost_usd is not None and (
        isinstance(trace.cost_usd, bool)
        or not isinstance(trace.cost_usd, (int, float))
        or not math.isfinite(trace.cost_usd)
    ):
        raise ValueError("cost_usd must be a finite number or None")
    _validate_json_object_list(trace.retrieved_context, "retrieved_context")
    _validate_json_object_list(trace.tool_calls, "tool_calls")
    _validate_json_object_list(trace.tool_outputs, "tool_outputs")
    _validate_optional_rfc3339(trace.created_at, "created_at", "trace")


def _validate_failure_case(case: FailureCase) -> None:
    for field_name in ("case_id", "source_trace_id"):
        _validate_required_string(
            getattr(case, field_name),
            field_name,
            "failure case records require",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
    for field_name in ("commit_sha", "failure_type"):
        _validate_required_string(
            getattr(case, field_name),
            field_name,
            "failure case records require",
            max_chars=METADATA_VALUE_MAX_CHARS,
        )
    _validate_required_string(
        case.symptom, "symptom", "failure case records require"
    )
    for field_name in (
        "root_cause",
        "fix",
        "fix_commit_sha",
        "reviewed_by",
        "review_notes",
    ):
        _validate_optional_string(
            getattr(case, field_name),
            field_name,
            "failure case",
            max_chars=(
                METADATA_VALUE_MAX_CHARS
                if field_name in {"fix_commit_sha", "reviewed_by"}
                else None
            ),
        )
    if not isinstance(case.status, str) or case.status not in FAILURE_CASE_STATUSES:
        raise ValueError("failure case status must be one of: draft, obsolete, verified")
    if type(case.regression_passed) is not bool:
        raise ValueError("regression_passed must be a boolean")
    if case.status == "verified" and (not case.fix or not case.fix_commit_sha or not case.regression_passed):
        raise ValueError("verified failure cases require fix, fix_commit_sha, and passing regression")
    _validate_optional_rfc3339(case.reviewed_at, "reviewed_at", "failure case")
    _validate_optional_rfc3339(case.created_at, "created_at", "failure case")


def _validate_lesson_record(lesson: Lesson) -> None:
    for field_name in ("lesson_id", "source_case_id"):
        _validate_required_string(
            getattr(lesson, field_name),
            field_name,
            "lesson records require",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
    if not isinstance(lesson.lesson_text, str) or not lesson.lesson_text.strip():
        raise ValueError("lesson records require lesson_text")
    if not isinstance(lesson.memory_type, str) or lesson.memory_type not in MEMORY_TYPES:
        raise ValueError("lesson memory_type must be one of: episodic, policy, procedural, semantic")
    if not isinstance(lesson.status, str) or lesson.status not in LESSON_STATUSES:
        raise ValueError("lesson status must be one of: active, obsolete")
    if type(lesson.sensitive) is not bool:
        raise ValueError("sensitive must be a boolean")
    if type(lesson.eval_leaking) is not bool:
        raise ValueError("eval_leaking must be a boolean")
    _validate_optional_rfc3339(lesson.created_at, "created_at", "lesson")


def _validate_project_policy(policy: ProjectPolicy) -> None:
    _validate_required_string(
        policy.policy_id,
        "policy_id",
        "project policy records require",
        max_chars=MEMORY_ID_MAX_CHARS,
    )
    if not isinstance(policy.policy_text, str) or not policy.policy_text.strip():
        raise ValueError("project policy records require policy_text")
    if not isinstance(policy.status, str) or policy.status not in LESSON_STATUSES:
        raise ValueError("project policy status must be one of: active, obsolete")
    if type(policy.sensitive) is not bool:
        raise ValueError("sensitive must be a boolean")
    if type(policy.eval_leaking) is not bool:
        raise ValueError("eval_leaking must be a boolean")
    _validate_optional_rfc3339(policy.created_at, "created_at", "project policy")


def _validate_usage_log(log: MemoryUsageLog) -> None:
    for field_name in ("decision_id", "run_id"):
        _validate_required_string(
            getattr(log, field_name),
            field_name,
            "usage log records require",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
    if not isinstance(log.mode, str) or log.mode not in MODES:
        raise ValueError("usage log mode must be one of: debug, eval, planning, production, regression, repair")
    _validate_memory_id_list(log.candidate_memory_ids, "candidate_memory_ids")
    _validate_memory_id_list(log.used_memory_ids, "used_memory_ids")
    _validate_memory_id_list(log.blocked_memory_ids, "blocked_memory_ids")
    if not isinstance(log.reason, str):
        raise ValueError("usage log reason must be a string")
    if not log.reason.strip():
        raise ValueError("usage log reason must be nonblank")
    if not isinstance(log.risk, str) or log.risk not in DECISION_RISKS:
        raise ValueError("usage log risk must be one of: high, low, medium, none")
    if (
        not isinstance(log.recommended_injection, str)
        or log.recommended_injection not in RECOMMENDED_INJECTIONS
    ):
        raise ValueError(
            "usage log recommended_injection must be one of: full_case_summary, none, pointer_only, short_summary"
        )
    if log.eval_result is not None and (
        not isinstance(log.eval_result, str) or log.eval_result not in EVAL_RESULTS
    ):
        raise ValueError("usage log eval_result must be one of: error, fail, pass, unknown")
    if type(log.memory_caused_failure) is not bool:
        raise ValueError("memory_caused_failure must be a boolean")
    if log.trace_id is not None and (
        not isinstance(log.trace_id, str) or not log.trace_id
    ):
        raise ValueError("usage log trace_id must be a non-empty string")
    if log.trace_id is not None and len(log.trace_id) > MEMORY_ID_MAX_CHARS:
        raise ValueError(
            f"usage log trace_id must be at most {MEMORY_ID_MAX_CHARS} characters"
        )
    _validate_string_mapping(log.context, "context")
    _validate_status_mapping(
        log.candidate_memory_statuses, log.candidate_memory_ids
    )
    _validate_string_mapping(log.system_blocked_reasons, "system_blocked_reasons")
    unknown_blocked_reasons = sorted(
        set(log.system_blocked_reasons).difference(log.candidate_memory_ids)
    )
    if unknown_blocked_reasons:
        raise ValueError(
            "usage log system_blocked_reasons must reference candidates: "
            + ", ".join(unknown_blocked_reasons)
        )
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
    _validate_optional_rfc3339(log.created_at, "created_at", "usage log")


def _validate_runtime_outcome(
    eval_result: EvalResult | None, memory_caused_failure: bool
) -> None:
    if eval_result is not None and (
        not isinstance(eval_result, str) or eval_result not in EVAL_RESULTS
    ):
        raise ValueError("eval_result must be one of: error, fail, pass, unknown, or None")
    if type(memory_caused_failure) is not bool:
        raise ValueError("memory_caused_failure must be a boolean")


def _validate_required_string(
    value: Any,
    field_name: str,
    message_prefix: str,
    *,
    max_chars: int | None = None,
) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{message_prefix} {field_name}")
    if max_chars is not None and len(value) > max_chars:
        raise ValueError(f"{field_name} must be at most {max_chars} characters")


def _validate_optional_string(
    value: Any,
    field_name: str,
    record_label: str,
    *,
    max_chars: int | None = None,
) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(
            f"{record_label} {field_name} must be None or a non-empty string"
        )
    if value is not None and max_chars is not None and len(value) > max_chars:
        raise ValueError(
            f"{record_label} {field_name} must be at most {max_chars} characters"
        )


def _validate_optional_rfc3339(
    value: Any, field_name: str, record_label: str
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ) is None:
        raise ValueError(
            f"{record_label} {field_name} must be None or a timezone-aware RFC 3339 date-time string"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{record_label} {field_name} must be None or a timezone-aware RFC 3339 date-time string"
        ) from exc
    if parsed.utcoffset() is None:
        raise ValueError(
            f"{record_label} {field_name} must be None or a timezone-aware RFC 3339 date-time string"
        )


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_json_object_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"trace {field_name} must be a list of JSON objects")


def _validate_memory_id_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"usage log {field_name} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"usage log {field_name} must not contain duplicate memory IDs")
    if any(len(item) > MEMORY_ID_MAX_CHARS for item in value):
        raise ValueError(
            f"usage log {field_name} entries must be at most "
            f"{MEMORY_ID_MAX_CHARS} characters"
        )


def _validate_string_mapping(value: Any, field_name: str) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, str)
        or not item
        for key, item in value.items()
    ):
        raise ValueError(
            f"usage log {field_name} must map non-empty strings to non-empty strings"
        )
    if field_name == "context" and any(
        len(item) > METADATA_VALUE_MAX_CHARS for item in value.values()
    ):
        raise ValueError(
            f"usage log context values must be at most "
            f"{METADATA_VALUE_MAX_CHARS} characters"
        )
    if field_name == "system_blocked_reasons" and any(
        len(key) > MEMORY_ID_MAX_CHARS for key in value
    ):
        raise ValueError(
            f"usage log system_blocked_reasons keys must be at most "
            f"{MEMORY_ID_MAX_CHARS} characters"
        )


def _validate_status_mapping(value: Any, candidate_memory_ids: list[str]) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(memory_id, str)
        or not memory_id
        or not isinstance(status, str)
        or status not in FAILURE_CASE_STATUSES.union(LESSON_STATUSES)
        for memory_id, status in value.items()
    ):
        raise ValueError("usage log candidate_memory_statuses contains an invalid status")
    if any(len(memory_id) > MEMORY_ID_MAX_CHARS for memory_id in value):
        raise ValueError(
            f"usage log candidate_memory_statuses keys must be at most "
            f"{MEMORY_ID_MAX_CHARS} characters"
        )
    if set(value) != set(candidate_memory_ids):
        missing_statuses = sorted(set(candidate_memory_ids).difference(value))
        extra_statuses = sorted(set(value).difference(candidate_memory_ids))
        raise ValueError(
            "usage log candidate_memory_statuses must match candidates"
            + (f"; missing: {', '.join(missing_statuses)}" if missing_statuses else "")
            + (f"; extra: {', '.join(extra_statuses)}" if extra_statuses else "")
        )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


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
