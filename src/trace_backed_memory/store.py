from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Iterator, TextIO, cast

from ._ingestion import (
    LESSONS_YAML_FILE_MAX_BYTES,
    LESSONS_YAML_MAX_RECORDS,
    SNAPSHOT_FILE_MAX_BYTES,
    SNAPSHOT_MAX_RECORDS_PER_COLLECTION,
    SNAPSHOT_MAX_TOTAL_RECORDS,
    read_bounded_utf8,
    unique_json_object_pairs,
    validate_non_negative_limit,
    validate_snapshot_record_count,
    validate_snapshot_total_record_count,
)
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
    CommitAncestryEvidence,
    EvalResult,
    FailureCase,
    GatedMemoryResult,
    Lesson,
    MemoryContext,
    MemoryDecision,
    MemoryGateRequest,
    MemoryItem,
    MemoryObsolescenceRequest,
    MeasuredEvalResult,
    MemoryMetrics,
    MemoryOutcomeMetrics,
    MemoryRunAudit,
    MemoryRunAuditStatus,
    MemoryRunCompletion,
    MemoryRunMetrics,
    MemoryRunRemediation,
    MemoryRunRemediationAction,
    MemoryRunResult,
    PRChangeEndpoint,
    PRCaseProvenance,
    MemoryUsageLog,
    PRMemoryReport,
    PRChangeSet,
    ProjectPolicy,
    Trace,
)
from .policy import (
    LLM_GATE_MAX_CANDIDATES,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
    apply_llm_gate_decision,
    build_injection_snippet,
    build_llm_gate_prompt,
    is_finite_number,
    parse_memory_decision,
    system_gate,
    validate_memory_context,
)

Snapshot = dict[str, Any]
_MeasuredEvalResult = MeasuredEvalResult
_MemoryRunCompletionRow = tuple[
    int,
    str,
    _MeasuredEvalResult,
    bool,
    dict[str, Any],
]
_UNSET = object()
EVAL_RESULTS = {"pass", "fail", "error", "unknown"}
EVALUATED_RESULTS = {"pass", "fail", "error"}
FAILURE_CASE_STATUSES = {"draft", "verified", "obsolete"}
LESSON_STATUSES = {"active", "obsolete"}
MEMORY_TYPES = {"procedural", "semantic", "episodic", "policy"}
MODES = {"debug", "repair", "regression", "planning", "eval", "production"}
DECISION_RISKS = {"none", "low", "medium", "high"}
RECOMMENDED_INJECTIONS = {"none", "short_summary", "full_case_summary", "pointer_only"}
MEMORY_KINDS = {"failure_case", "lesson", "project_policy"}
PR_CHANGE_SET_FIELDS = (
    "prompt_version",
    "prompt_family",
    "tool",
    "tool_schema_version",
    "model",
    "eval_suite",
)
PR_CHANGE_SET_MAX_FIELDS = len(PR_CHANGE_SET_FIELDS)
LEGACY_PR_WARNING_FIELDS = frozenset(
    {
        "prompt_version",
        "prompt_family",
        "tool_schema_version",
        "tool",
        "model",
        "model_family",
        "eval_suite",
    }
)
DECLARED_TRACE_CONTEXT_FIELDS = (
    "branch",
    "prompt_version",
    "prompt_family",
    "tool_schema_version",
    "model",
    "eval_suite",
)
MEMORY_SOURCE_IDENTITY_CONTEXT_FIELDS = frozenset(
    {"source_eval_suite", "source_input_hash"}
)
SNAPSHOT_VERSION = 2
TRACE_JSON_MAX_DEPTH = 100
TRACE_LATENCY_MAX_MS = 2_147_483_647
SNAPSHOT_COLLECTION_NAMES = (
    "traces",
    "failure_cases",
    "lessons",
    "project_policies",
    "usage_logs",
)
SNAPSHOT_COLLECTION_KEYS = frozenset(SNAPSHOT_COLLECTION_NAMES)
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
        self._trace_ids_by_run_id: dict[str, list[str]] = {}
        self._failure_cases: dict[str, FailureCase] = {}
        self._lessons: dict[str, Lesson] = {}
        self._project_policies: dict[str, ProjectPolicy] = {}
        self._usage_logs: list[MemoryUsageLog] = []
        self._usage_log_indexes: dict[str, int] = {}
        self._next_decision_number: int = 1
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
    def from_snapshot(
        cls,
        data: Mapping[str, Any],
        *,
        max_records_per_collection: int | None = (
            SNAPSHOT_MAX_RECORDS_PER_COLLECTION
        ),
        max_total_records: int | None = SNAPSHOT_MAX_TOTAL_RECORDS,
    ) -> "TraceBackedMemoryStore":
        if not isinstance(data, Mapping):
            raise ValueError("memory store snapshot must be a JSON object")
        is_v2 = _validate_snapshot_envelope(data)
        _validate_snapshot_collection_limits(
            data,
            max_records_per_collection=max_records_per_collection,
            max_total_records=max_total_records,
        )
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
        usage_log_records = _snapshot_records(data, "usage_logs")
        known_memory_ids = set(store._failure_cases).union(
            store._lessons,
            store._project_policies,
        )
        legacy_traces_by_run_id: dict[str, list[Trace]] = {}
        if not is_v2:
            for trace in store._traces.values():
                legacy_traces_by_run_id.setdefault(trace.run_id, []).append(
                    trace
                )
        trace_tool_names_by_id: dict[str, set[str]] = {}
        for log_data in usage_log_records:
            if is_v2:
                _validate_v2_usage_log_record(log_data)
            else:
                log_data = store._migrate_legacy_usage_log(
                    log_data,
                    traces_by_run_id=legacy_traces_by_run_id,
                )
            log = _snapshot_record_instance(
                MemoryUsageLog, log_data, "usage log"
            )
            _validate_usage_log(log)
            store._validate_usage_log_memory_ids(
                log,
                known_memory_ids=known_memory_ids,
            )
            store._validate_usage_log_trace(
                log,
                trace_tool_names_by_id=trace_tool_names_by_id,
            )
            store._append_usage_log(deepcopy(log))
        return store

    @_synchronized
    def save_json(self, path: str | Path) -> None:
        target = Path(path)
        with _atomic_utf8_writer(target) as temporary_file:
            json.dump(
                self.to_snapshot(),
                temporary_file,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            temporary_file.write("\n")

    @classmethod
    def load_json(
        cls,
        path: str | Path,
        *,
        max_bytes: int | None = SNAPSHOT_FILE_MAX_BYTES,
        max_records_per_collection: int | None = (
            SNAPSHOT_MAX_RECORDS_PER_COLLECTION
        ),
        max_total_records: int | None = SNAPSHOT_MAX_TOTAL_RECORDS,
    ) -> "TraceBackedMemoryStore":
        source = read_bounded_utf8(
            path,
            max_bytes=max_bytes,
            description="memory store snapshot",
        )
        try:
            data = json.loads(
                source,
                parse_constant=_reject_json_constant,
                object_pairs_hook=lambda pairs: unique_json_object_pairs(
                    pairs,
                    description="memory store snapshot",
                ),
            )
        except RecursionError as exc:
            raise ValueError("invalid memory store snapshot JSON nesting") from exc
        if not isinstance(data, Mapping):
            raise ValueError("memory store snapshot must be a JSON object")
        return cls.from_snapshot(
            data,
            max_records_per_collection=max_records_per_collection,
            max_total_records=max_total_records,
        )

    @_synchronized
    def save_lessons_yaml(
        self,
        path: str | Path,
        *,
        overwrite: bool = True,
    ) -> None:
        active_lessons = [
            lesson
            for lesson in self._lessons.values()
            if lesson.status == "active"
        ]
        target = Path(path)
        with _atomic_utf8_writer(
            target,
            overwrite=overwrite,
        ) as temporary_file:
            temporary_file.write(_lessons_to_yaml(active_lessons))

    @_synchronized
    def load_lessons_yaml(
        self,
        path: str | Path,
        *,
        max_bytes: int | None = LESSONS_YAML_FILE_MAX_BYTES,
        max_lessons: int | None = LESSONS_YAML_MAX_RECORDS,
    ) -> list[Lesson]:
        try:
            lesson_records = _lessons_from_yaml(
                read_bounded_utf8(
                    path,
                    max_bytes=max_bytes,
                    description="active lessons YAML",
                ),
                max_lessons=max_lessons,
            )
        except (AttributeError, OverflowError, TypeError) as exc:
            raise ValueError(f"invalid lessons YAML: {exc}") from exc
        lessons = [
            _snapshot_record_instance(Lesson, record, "lesson")
            for record in lesson_records
        ]
        staged_lessons = dict(self._lessons)
        stored_lessons: list[Lesson] = []
        for lesson in lessons:
            stored_lesson = self._validated_lesson_candidate(
                lesson,
                staged_lessons,
            )
            if stored_lesson.status != "active":
                raise ValueError(
                    "active lessons YAML requires status 'active': "
                    f"{stored_lesson.lesson_id}"
                )
            staged_lessons[stored_lesson.lesson_id] = stored_lesson
            stored_lessons.append(stored_lesson)

        returned_lessons = deepcopy(stored_lessons)
        self._lessons.update(
            {lesson.lesson_id: lesson for lesson in stored_lessons}
        )
        return returned_lessons

    @_synchronized
    def record_trace(self, trace: Trace) -> Trace:
        _require_exact_record(trace, Trace, "trace")
        _validate_trace(trace)
        stored_trace = _copy_trace_for_storage(trace)
        _validate_trace(stored_trace)
        if stored_trace.trace_id in self._traces:
            raise ValueError(f"duplicate trace_id: {stored_trace.trace_id}")
        trace_ids = self._trace_ids_by_run_id.get(stored_trace.run_id)
        self._traces[stored_trace.trace_id] = stored_trace
        try:
            if trace_ids is None:
                self._trace_ids_by_run_id[stored_trace.run_id] = [
                    stored_trace.trace_id
                ]
            else:
                trace_ids.append(stored_trace.trace_id)
        except BaseException:
            self._traces.pop(stored_trace.trace_id, None)
            if trace_ids is None:
                self._trace_ids_by_run_id.pop(stored_trace.run_id, None)
            elif trace_ids and trace_ids[-1] == stored_trace.trace_id:
                trace_ids.pop()
            raise
        return deepcopy(stored_trace)

    @_synchronized
    def complete_trace(
        self,
        trace_id: str,
        *,
        eval_result: _MeasuredEvalResult,
        output_hash: str | None = cast(Any, _UNSET),
        tool_outputs: list[dict[str, object]] = cast(Any, _UNSET),
        latency_ms: int | None = cast(Any, _UNSET),
        cost_usd: float | None = cast(Any, _UNSET),
        error: str | None = cast(Any, _UNSET),
        trace_uri: str | None = cast(Any, _UNSET),
    ) -> Trace:
        """Complete one pending Trace with measured execution evidence."""
        _validate_required_string(
            trace_id,
            "trace_id",
            "trace completion requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        _validate_measured_eval_result(eval_result, "trace completion")
        current = self._traces.get(trace_id)
        if current is None:
            raise ValueError(f"unknown trace_id: {trace_id}")

        completed = _completed_trace_candidate(
            current,
            eval_result,
            _provided_trace_completion_values(
                output_hash=output_hash,
                tool_outputs=tool_outputs,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                error=error,
                trace_uri=trace_uri,
            ),
        )

        self._traces[trace_id] = completed
        return deepcopy(completed)

    @_synchronized
    def complete_memory_run(
        self,
        *,
        trace_id: str,
        decision_id: str,
        eval_result: _MeasuredEvalResult,
        memory_caused_failure: bool = False,
        output_hash: str | None = cast(Any, _UNSET),
        tool_outputs: list[dict[str, object]] = cast(Any, _UNSET),
        latency_ms: int | None = cast(Any, _UNSET),
        cost_usd: float | None = cast(Any, _UNSET),
        error: str | None = cast(Any, _UNSET),
        trace_uri: str | None = cast(Any, _UNSET),
    ) -> MemoryRunCompletion:
        """Atomically complete a Trace and its linked memory decision."""
        _validate_required_string(
            trace_id,
            "trace_id",
            "memory run completion requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        _validate_required_string(
            decision_id,
            "decision_id",
            "memory run completion requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        _validate_measured_outcome(eval_result, memory_caused_failure)

        current_trace = self._traces.get(trace_id)
        if current_trace is None:
            raise ValueError(f"unknown trace_id: {trace_id}")
        log_index = self._usage_log_index(decision_id)
        current_log = self._usage_logs[log_index]
        if (
            current_log.trace_id != trace_id
            or current_log.run_id != current_trace.run_id
        ):
            raise ValueError(
                f"decision_id {decision_id} does not belong to trace_id {trace_id}"
            )

        completed_trace = _completed_trace_candidate(
            current_trace,
            eval_result,
            _provided_trace_completion_values(
                output_hash=output_hash,
                tool_outputs=tool_outputs,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                error=error,
                trace_uri=trace_uri,
            ),
        )
        sealed_log = _sealed_usage_outcome_candidate(
            current_log,
            eval_result,
            memory_caused_failure,
        )
        self._validate_usage_log_memory_ids(sealed_log)
        self._validate_usage_log_trace(sealed_log)

        self._traces[trace_id] = completed_trace
        self._usage_logs[log_index] = sealed_log
        return MemoryRunCompletion(
            trace=deepcopy(completed_trace),
            usage_log=deepcopy(sealed_log),
        )

    @_synchronized
    def complete_memory_runs(
        self,
        results: tuple[MemoryRunResult, ...],
    ) -> tuple[MemoryRunCompletion, ...]:
        """Atomically complete a batch of newly measured memory runs."""
        validated_results = _validated_memory_run_results(results)
        completion_rows: list[_MemoryRunCompletionRow] = []
        for result in validated_results:
            log_index = self._usage_log_indexes.get(result.decision_id)
            if log_index is None:
                raise ValueError(f"unknown decision_id: {result.decision_id}")
            current_log = self._usage_logs[log_index]
            completion_rows.append(
                (
                    log_index,
                    cast(str, current_log.trace_id),
                    result.eval_result,
                    result.memory_caused_failure,
                    _memory_run_result_completion_values(result),
                )
            )

        completed_traces, sealed_logs, completions = (
            self._stage_memory_run_completions(completion_rows)
        )
        self._traces.update(completed_traces)
        for log_index, sealed_log in sealed_logs.items():
            self._usage_logs[log_index] = sealed_log
        return completions

    @_synchronized
    def recover_memory_run(
        self,
        decision_id: str,
        *,
        memory_caused_failure: bool = cast(Any, _UNSET),
        output_hash: str | None = cast(Any, _UNSET),
        tool_outputs: list[dict[str, object]] = cast(Any, _UNSET),
        latency_ms: int | None = cast(Any, _UNSET),
        cost_usd: float | None = cast(Any, _UNSET),
        error: str | None = cast(Any, _UNSET),
        trace_uri: str | None = cast(Any, _UNSET),
    ) -> MemoryRunCompletion:
        """Recover a one-sided memory run from its existing measured result."""
        _validate_required_string(
            decision_id,
            "decision_id",
            "memory run recovery requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        if (
            memory_caused_failure is not _UNSET
            and type(memory_caused_failure) is not bool
        ):
            raise ValueError("memory_caused_failure must be a boolean")

        log_index = self._usage_log_index(decision_id)
        current_log = self._usage_logs[log_index]
        trace_id = cast(str, current_log.trace_id)
        current_trace = self._traces[trace_id]
        _status, eval_result, resolved_memory_caused_failure = (
            _resolved_memory_run_recovery(
                current_trace,
                current_log,
                memory_caused_failure,
            )
        )

        return self.complete_memory_run(
            trace_id=trace_id,
            decision_id=decision_id,
            eval_result=eval_result,
            memory_caused_failure=resolved_memory_caused_failure,
            output_hash=output_hash,
            tool_outputs=tool_outputs,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            error=error,
            trace_uri=trace_uri,
        )

    @_synchronized
    def recover_memory_runs(
        self,
        decision_ids: tuple[str, ...],
        *,
        memory_caused_failures: Mapping[str, bool] | None = None,
    ) -> tuple[MemoryRunCompletion, ...]:
        """Atomically recover a batch of already eligible memory runs."""
        validated_ids = _validated_batch_recovery_decision_ids(decision_ids)
        attributions = _validated_batch_recovery_attributions(
            memory_caused_failures,
            validated_ids,
        )

        recovery_rows: list[_MemoryRunCompletionRow] = []
        for decision_id in validated_ids:
            log_index = self._usage_log_indexes.get(decision_id)
            if log_index is None:
                raise ValueError(f"unknown decision_id: {decision_id}")
            current_log = self._usage_logs[log_index]
            trace_id = cast(str, current_log.trace_id)
            current_trace = self._traces[trace_id]
            _status, eval_result, memory_caused_failure = (
                _resolved_memory_run_recovery(
                    current_trace,
                    current_log,
                    attributions.get(decision_id, _UNSET),
                )
            )
            recovery_rows.append(
                (
                    log_index,
                    trace_id,
                    eval_result,
                    memory_caused_failure,
                    {},
                )
            )

        completed_traces, sealed_logs, completions = (
            self._stage_memory_run_completions(recovery_rows)
        )
        self._traces.update(completed_traces)
        for log_index, sealed_log in sealed_logs.items():
            self._usage_logs[log_index] = sealed_log
        return completions

    @_synchronized
    def recover_ready_memory_runs(self) -> tuple[MemoryRunCompletion, ...]:
        """Atomically recover every decision currently ready without input."""
        decision_ids = tuple(
            item.decision_id
            for item in self.memory_run_remediations()
            if item.action == "recover"
        )
        if not decision_ids:
            return ()
        return self.recover_memory_runs(decision_ids)

    def _stage_memory_run_completions(
        self,
        rows: list[_MemoryRunCompletionRow],
    ) -> tuple[
        dict[str, Trace],
        dict[int, MemoryUsageLog],
        tuple[MemoryRunCompletion, ...],
    ]:
        """Build batch candidates without mutation while the caller holds the lock."""
        trace_results: dict[str, _MeasuredEvalResult] = {}
        trace_completion_values: dict[str, dict[str, Any]] = {}
        for (
            _log_index,
            trace_id,
            eval_result,
            _memory_caused_failure,
            completion_values,
        ) in rows:
            existing_result = trace_results.get(trace_id)
            if existing_result is not None and existing_result != eval_result:
                raise ValueError(
                    f"shared trace has conflicting outcomes: {trace_id}"
                )
            trace_results[trace_id] = eval_result

            normalized_trace = _completed_trace_candidate(
                self._traces[trace_id],
                eval_result,
                completion_values,
            )
            merged_values = trace_completion_values.setdefault(trace_id, {})
            for field_name in completion_values:
                normalized_value = deepcopy(
                    getattr(normalized_trace, field_name)
                )
                if (
                    field_name in merged_values
                    and merged_values[field_name] != normalized_value
                ):
                    raise ValueError(
                        "shared trace has conflicting completion evidence "
                        f"for {field_name}: {trace_id}"
                    )
                merged_values[field_name] = normalized_value

        completed_traces = {
            trace_id: _completed_trace_candidate(
                self._traces[trace_id],
                eval_result,
                trace_completion_values[trace_id],
            )
            for trace_id, eval_result in trace_results.items()
        }
        sealed_logs: dict[int, MemoryUsageLog] = {}
        for (
            log_index,
            _trace_id,
            eval_result,
            memory_caused_failure,
            _completion_values,
        ) in rows:
            sealed_log = _sealed_usage_outcome_candidate(
                self._usage_logs[log_index],
                eval_result,
                memory_caused_failure,
            )
            self._validate_usage_log_memory_ids(sealed_log)
            self._validate_usage_log_trace(sealed_log)
            sealed_logs[log_index] = sealed_log

        completions = tuple(
            MemoryRunCompletion(
                trace=deepcopy(completed_traces[trace_id]),
                usage_log=deepcopy(sealed_logs[log_index]),
            )
            for (
                log_index,
                trace_id,
                _eval_result,
                _failure,
                _completion_values,
            ) in rows
        )
        return completed_traces, sealed_logs, completions

    @_synchronized
    def add_failure_case(self, case: FailureCase) -> FailureCase:
        _require_exact_record(case, FailureCase, "failure case")
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
        stored_lesson = self._validated_lesson_candidate(
            lesson,
            self._lessons,
        )
        self._lessons[stored_lesson.lesson_id] = stored_lesson
        return deepcopy(stored_lesson)

    def _validated_lesson_candidate(
        self,
        lesson: Lesson,
        existing_lessons: Mapping[str, Lesson],
    ) -> Lesson:
        _require_exact_record(lesson, Lesson, "lesson")
        stored_lesson = deepcopy(lesson)
        _validate_lesson_record(stored_lesson)
        if stored_lesson.lesson_id in existing_lessons:
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
        return stored_lesson

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
        _require_exact_record(policy, ProjectPolicy, "project policy")
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
    def obsolete_memories(
        self,
        requests: tuple[MemoryObsolescenceRequest, ...],
    ) -> tuple[FailureCase | Lesson | ProjectPolicy, ...]:
        """Atomically obsolete explicit memories and all required dependents."""
        validated = _validated_memory_obsolescence_requests(requests)

        requested_case_ids: set[str] = set()
        requested_lesson_ids: set[str] = set()
        requested_policy_ids: set[str] = set()
        for request in validated:
            if request.memory_kind == "failure_case":
                _stored_record(
                    self._failure_cases,
                    request.memory_id,
                    "failure case",
                )
                requested_case_ids.add(request.memory_id)
            elif request.memory_kind == "lesson":
                _stored_record(self._lessons, request.memory_id, "lesson")
                requested_lesson_ids.add(request.memory_id)
            else:
                _stored_record(
                    self._project_policies,
                    request.memory_id,
                    "project policy",
                )
                requested_policy_ids.add(request.memory_id)

        cascading_case_ids = {
            case_id
            for case_id in requested_case_ids
            if self._failure_cases[case_id].status != "obsolete"
        }
        obsolete_cases = {
            case_id: (
                current
                if current.status == "obsolete"
                else transition_failure_case_to_obsolete(current)
            )
            for case_id, current in self._failure_cases.items()
            if case_id in requested_case_ids
        }
        obsolete_lessons = {
            lesson_id: (
                current
                if current.status == "obsolete"
                else transition_lesson_to_obsolete(current)
            )
            for lesson_id, current in self._lessons.items()
            if lesson_id in requested_lesson_ids
            or (
                current.source_case_id in cascading_case_ids
                and current.status == "active"
            )
        }
        obsolete_policies = {
            policy_id: (
                current
                if current.status == "obsolete"
                else transition_project_policy_to_obsolete(current)
            )
            for policy_id, current in self._project_policies.items()
            if policy_id in requested_policy_ids
        }

        for case in obsolete_cases.values():
            _validate_failure_case(case)
        for lesson in obsolete_lessons.values():
            _validate_lesson_record(lesson)
            validate_lesson_contract(
                lesson_text=lesson.lesson_text,
                scope=lesson.scope,
                confidence=lesson.confidence,
            )
        for policy in obsolete_policies.values():
            _validate_project_policy(policy)
            validate_lesson_contract(
                lesson_text=policy.policy_text,
                scope=policy.scope,
                confidence=policy.confidence,
            )

        self._failure_cases.update(obsolete_cases)
        self._lessons.update(obsolete_lessons)
        self._project_policies.update(obsolete_policies)
        result_collections: dict[
            str,
            Mapping[str, FailureCase | Lesson | ProjectPolicy],
        ] = {
            "failure_case": obsolete_cases,
            "lesson": obsolete_lessons,
            "project_policy": obsolete_policies,
        }
        return tuple(
            deepcopy(result_collections[request.memory_kind][request.memory_id])
            for request in validated
        )

    @_synchronized
    def candidate_memories(
        self,
        context: MemoryContext,
        *,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
        commit_ancestry: CommitAncestryEvidence | None = None,
    ) -> list[MemoryItem]:
        validate_memory_context(context)
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be a string or None")
        validated_semantic_scores = _validated_semantic_scores(
            semantic_scores,
            query=query,
            max_candidates=max_candidates,
            minimum_score=minimum_score,
            stored_memory_ids=set(self._failure_cases).union(
                self._lessons, self._project_policies
            ),
        )
        commit_relations = _validated_commit_ancestry(context, commit_ancestry)
        candidates = self._metadata_candidates(context)
        if commit_relations is not None:
            candidates = self._filter_candidates_by_ancestry(
                candidates, commit_relations
            )

        if validated_semantic_scores is not None:
            candidates = [
                memory
                for memory in candidates
                if memory.memory_id in validated_semantic_scores
                and (
                    minimum_score is None
                    or validated_semantic_scores[memory.memory_id] >= minimum_score
                )
            ]
            candidates.sort(
                key=lambda memory: (
                    -validated_semantic_scores[memory.memory_id],
                    memory.memory_id,
                )
            )
            return candidates[:max_candidates]

        query_tokens = _tokens(query or "")
        if query_tokens:
            candidates = [
                memory
                for memory in candidates
                if query_tokens.intersection(_memory_tokens(memory))
            ]

        return sorted(candidates, key=lambda memory: memory.memory_id)

    def _metadata_candidates(self, context: MemoryContext) -> list[MemoryItem]:
        context_values = _context_values(context)
        candidates: list[MemoryItem] = []

        for lesson in self._lessons.values():
            if _has_metadata_match(lesson.scope, context_values):
                candidates.append(self._memory_item_from_lesson(lesson))
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
        return candidates

    @_synchronized
    def candidate_commit_anchors(
        self, context: MemoryContext
    ) -> tuple[str, ...]:
        validate_memory_context(context)
        return tuple(
            sorted(
                {
                    anchor
                    for memory in self._metadata_candidates(context)
                    if (anchor := self._commit_anchor(memory.memory_id)) is not None
                }
            )
        )

    def _commit_anchor(self, memory_id: str) -> str | None:
        lesson = self._lessons.get(memory_id)
        if lesson is not None:
            anchor = self._failure_cases[lesson.source_case_id].fix_commit_sha
            if not isinstance(anchor, str) or not anchor:
                raise ValueError(
                    f"lesson source case lacks fix_commit_sha: {memory_id}"
                )
            return anchor
        case = self._failure_cases.get(memory_id)
        if case is not None:
            return case.commit_sha
        return None

    def _filter_candidates_by_ancestry(
        self,
        candidates: list[MemoryItem],
        relations: dict[str, bool],
    ) -> list[MemoryItem]:
        anchors = {
            memory.memory_id: self._commit_anchor(memory.memory_id)
            for memory in candidates
        }
        _require_commit_relations(anchors.values(), relations)
        return [
            memory
            for memory in candidates
            if anchors[memory.memory_id] is None
            or relations[anchors[memory.memory_id]]
        ]

    @_synchronized
    def prepare_memory(
        self,
        context: MemoryContext,
        *,
        task: str,
        query: str | None = None,
        semantic_scores: Mapping[str, float] | None = None,
        max_candidates: int | None = None,
        minimum_score: float | None = None,
        context_summary: str = "",
        commit_ancestry: CommitAncestryEvidence | None = None,
    ) -> MemoryGateRequest:
        validate_memory_context(context)
        candidates = self.candidate_memories(
            context,
            query=query,
            semantic_scores=semantic_scores,
            max_candidates=max_candidates,
            minimum_score=minimum_score,
            commit_ancestry=commit_ancestry,
        )
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
        snippet = build_injection_snippet(
            final_allowed,
            decision=final_decision,
            context=request.context,
        )
        log = self._new_usage_log(
            trace=trace,
            context=request.context,
            candidates=candidates,
            decision=final_decision,
            system_blocked=system_blocked,
            eval_result=eval_result,
            memory_caused_failure=memory_caused_failure,
        )

        self._append_usage_log(log)
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
        self._append_usage_log(log)
        return deepcopy(log)

    @_synchronized
    def record_decision_outcome(
        self,
        decision_id: str,
        eval_result: _MeasuredEvalResult,
        *,
        memory_caused_failure: bool = False,
    ) -> MemoryUsageLog:
        """Seal one previously unevaluated decision with a measured outcome."""
        _validate_required_string(
            decision_id,
            "decision_id",
            "decision outcome requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        _validate_measured_outcome(eval_result, memory_caused_failure)

        log_index = self._usage_log_index(decision_id)
        current = self._usage_logs[log_index]
        sealed = _sealed_usage_outcome_candidate(
            current,
            eval_result,
            memory_caused_failure,
        )
        self._validate_usage_log_memory_ids(sealed)
        self._validate_usage_log_trace(sealed)
        self._usage_logs[log_index] = sealed
        return deepcopy(sealed)

    def _usage_log_index(self, decision_id: str) -> int:
        log_index = self._usage_log_indexes.get(decision_id)
        if log_index is None:
            raise ValueError(f"unknown decision_id: {decision_id}")
        return log_index

    def _append_usage_log(self, log: MemoryUsageLog) -> None:
        if log.decision_id in self._usage_log_indexes:
            raise ValueError(
                f"duplicate usage log decision_id: {log.decision_id}"
            )
        match = re.fullmatch(r"decision_(\d+)", log.decision_id)
        next_decision_number = self._next_decision_number
        if match is not None:
            next_decision_number = max(
                next_decision_number,
                int(match.group(1)) + 1,
            )

        log_index = len(self._usage_logs)
        self._usage_logs.append(log)
        try:
            self._usage_log_indexes[log.decision_id] = log_index
        except BaseException:
            self._usage_logs.pop()
            raise
        self._next_decision_number = next_decision_number

    def _trace_for_run_id(self, run_id: str) -> Trace:
        _validate_required_string(
            run_id,
            "run_id",
            "trace lookup requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        trace_ids = self._trace_ids_by_run_id.get(run_id)
        if not trace_ids:
            raise ValueError(f"unknown run_id: {run_id}")
        if len(trace_ids) > 1:
            raise ValueError(f"run_id does not resolve to one trace: {run_id}")
        return self._traces[trace_ids[0]]

    def _memory_items(self, memory_ids: tuple[str, ...] | list[str]) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        unknown_ids: list[str] = []
        for memory_id in memory_ids:
            if memory_id in self._lessons:
                items.append(self._memory_item_from_lesson(self._lessons[memory_id]))
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

    def _memory_item_from_lesson(self, lesson: Lesson) -> MemoryItem:
        source_case = self._failure_cases[lesson.source_case_id]
        source_trace = self._traces[source_case.source_trace_id]
        return memory_item_from_lesson(lesson, source_trace=source_trace)

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
            decision_id=f"decision_{self._next_decision_number:06d}",
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
        self._validate_usage_log_trace(log)
        return log

    def _validate_usage_log_memory_ids(
        self,
        log: MemoryUsageLog,
        *,
        known_memory_ids: set[str] | None = None,
    ) -> None:
        referenced_ids = set(log.candidate_memory_ids).union(
            log.used_memory_ids,
            log.blocked_memory_ids,
        )
        if known_memory_ids is None:
            unknown_ids = sorted(
                memory_id
                for memory_id in referenced_ids
                if memory_id not in self._failure_cases
                and memory_id not in self._lessons
                and memory_id not in self._project_policies
            )
        else:
            unknown_ids = sorted(
                referenced_ids.difference(known_memory_ids)
            )
        if unknown_ids:
            raise ValueError(
                "usage log references unknown memory IDs: "
                + ", ".join(unknown_ids)
            )

    def _validate_usage_log_trace(
        self,
        log: MemoryUsageLog,
        *,
        trace_tool_names_by_id: dict[str, set[str]] | None = None,
    ) -> None:
        if not isinstance(log.trace_id, str) or not log.trace_id.strip():
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
        for field_name in DECLARED_TRACE_CONTEXT_FIELDS:
            if (
                field_name in log.context
                and log.context[field_name] != getattr(trace, field_name)
            ):
                raise ValueError(
                    f"usage log context {field_name} does not match trace: {log.trace_id}"
                )
        if "tool" in log.context:
            if trace_tool_names_by_id is None:
                trace_tool_names = _trace_change_set_tool_names(trace)
            else:
                if trace.trace_id not in trace_tool_names_by_id:
                    trace_tool_names_by_id[
                        trace.trace_id
                    ] = _trace_change_set_tool_names(trace)
                trace_tool_names = trace_tool_names_by_id[trace.trace_id]
            if log.context["tool"] not in trace_tool_names:
                raise ValueError(
                    f"usage log context tool does not match trace: {log.trace_id}"
                )
        if "input_hash" in log.context:
            if "eval_suite" not in log.context:
                raise ValueError(
                    f"usage log context input_hash requires eval_suite: {log.trace_id}"
                )
            if log.context["input_hash"] != trace.input_hash:
                raise ValueError(
                    f"usage log context input_hash does not match trace: {log.trace_id}"
                )

    def _migrate_legacy_usage_log(
        self,
        log_data: dict[str, Any],
        *,
        traces_by_run_id: Mapping[str, list[Trace]],
    ) -> dict[str, Any]:
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
            _validate_required_string(
                legacy_log.run_id,
                "run_id",
                "trace lookup requires",
                max_chars=MEMORY_ID_MAX_CHARS,
            )
            matching_traces = traces_by_run_id.get(legacy_log.run_id, [])
            if not matching_traces:
                raise ValueError(f"unknown run_id: {legacy_log.run_id}")
            if len(matching_traces) > 1:
                raise ValueError(
                    "run_id does not resolve to one trace: "
                    f"{legacy_log.run_id}"
                )
            trace = matching_traces[0]
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
        candidate_memory_count = 0
        used_memory_count = 0
        blocked_memory_count = 0
        obsolete_attempts = 0
        evaluated_with_memory_count = 0
        passed_with_memory_count = 0
        evaluated_without_memory_count = 0
        passed_without_memory_count = 0
        unevaluated_decision_count = 0
        wrong_memory_failure_count = 0
        for log in self._usage_logs:
            candidate_memory_count += len(log.candidate_memory_ids)
            used_memory_count += len(log.used_memory_ids)
            blocked_memory_count += len(log.blocked_memory_ids)
            obsolete_attempts += sum(
                1
                for status in log.candidate_memory_statuses.values()
                if status == "obsolete"
            )
            if log.memory_caused_failure:
                wrong_memory_failure_count += 1
            if log.eval_result not in EVALUATED_RESULTS:
                unevaluated_decision_count += 1
            elif log.used_memory_ids:
                evaluated_with_memory_count += 1
                if log.eval_result == "pass":
                    passed_with_memory_count += 1
            else:
                evaluated_without_memory_count += 1
                if log.eval_result == "pass":
                    passed_without_memory_count += 1

        average_confidence = 0.0
        if self._lessons:
            average_confidence = sum(
                lesson.confidence for lesson in self._lessons.values()
            ) / len(self._lessons)

        return MemoryMetrics(
            decision_count=len(self._usage_logs),
            candidate_memory_count=candidate_memory_count,
            used_memory_count=used_memory_count,
            blocked_memory_count=blocked_memory_count,
            obsolete_memory_usage_attempts=obsolete_attempts,
            average_lesson_confidence=average_confidence,
            pass_rate_with_memory=_pass_rate(
                passed_with_memory_count,
                evaluated_with_memory_count,
            ),
            pass_rate_without_memory=_pass_rate(
                passed_without_memory_count,
                evaluated_without_memory_count,
            ),
            wrong_memory_failure_count=wrong_memory_failure_count,
            evaluated_with_memory_count=evaluated_with_memory_count,
            evaluated_without_memory_count=evaluated_without_memory_count,
            unevaluated_decision_count=unevaluated_decision_count,
        )

    def _memory_run_audit(self, log: MemoryUsageLog) -> MemoryRunAudit:
        trace_id = cast(str, log.trace_id)
        trace = self._traces[trace_id]
        return MemoryRunAudit(
            decision_id=log.decision_id,
            trace_id=trace_id,
            run_id=log.run_id,
            status=_memory_run_audit_status(
                trace.eval_result,
                log.eval_result,
            ),
            trace_eval_result=trace.eval_result,
            decision_eval_result=log.eval_result,
            memory_caused_failure=log.memory_caused_failure,
        )

    @_synchronized
    def memory_run_audits(self) -> tuple[MemoryRunAudit, ...]:
        """Classify completion consistency for every trace-linked decision."""
        audits: list[MemoryRunAudit] = []
        for log in sorted(self._usage_logs, key=lambda item: item.decision_id):
            audits.append(self._memory_run_audit(log))
        return tuple(audits)

    @_synchronized
    def memory_run_remediations(self) -> tuple[MemoryRunRemediation, ...]:
        """Map every memory-run audit to its safe next action."""
        return tuple(
            _memory_run_remediation(audit)
            for audit in self.memory_run_audits()
        )

    @_synchronized
    def memory_run_metrics(self) -> MemoryRunMetrics:
        """Summarize completion consistency for every memory-run decision."""
        decision_count = 0
        pending_count = 0
        trace_only_count = 0
        decision_only_count = 0
        complete_count = 0
        conflict_count = 0
        auto_recoverable_count = 0
        attribution_required_count = 0

        for log in self._usage_logs:
            audit = self._memory_run_audit(log)
            decision_count += 1
            if audit.status == "pending":
                pending_count += 1
            elif audit.status == "trace_only":
                trace_only_count += 1
            elif audit.status == "decision_only":
                decision_only_count += 1
            elif audit.status == "complete":
                complete_count += 1
            else:
                conflict_count += 1

            action = _memory_run_remediation(audit).action
            if action == "recover":
                auto_recoverable_count += 1
            elif action == "recover_with_attribution":
                attribution_required_count += 1

        return MemoryRunMetrics(
            decision_count=decision_count,
            pending_count=pending_count,
            trace_only_count=trace_only_count,
            decision_only_count=decision_only_count,
            complete_count=complete_count,
            conflict_count=conflict_count,
            recoverable_count=(
                auto_recoverable_count + attribution_required_count
            ),
            auto_recoverable_count=auto_recoverable_count,
            attribution_required_count=attribution_required_count,
        )

    @_synchronized
    def memory_outcome_metrics(self) -> tuple[MemoryOutcomeMetrics, ...]:
        candidate_counts: Counter[str] = Counter()
        used_counts: Counter[str] = Counter()
        blocked_counts: Counter[str] = Counter()
        passed_use_counts: Counter[str] = Counter()
        failed_or_errored_use_counts: Counter[str] = Counter()
        unevaluated_use_counts: Counter[str] = Counter()

        for log in self._usage_logs:
            candidate_counts.update(log.candidate_memory_ids)
            used_counts.update(log.used_memory_ids)
            blocked_counts.update(log.blocked_memory_ids)
            for memory_id in log.used_memory_ids:
                if log.eval_result == "pass":
                    passed_use_counts[memory_id] += 1
                elif log.eval_result in {"fail", "error"}:
                    failed_or_errored_use_counts[memory_id] += 1
                else:
                    unevaluated_use_counts[memory_id] += 1

        known_memory_ids = sorted(
            set(self._failure_cases).union(
                self._lessons,
                self._project_policies,
            )
        )
        metrics: list[MemoryOutcomeMetrics] = []
        for memory_id in known_memory_ids:
            passed_use_count = passed_use_counts[memory_id]
            failed_or_errored_use_count = failed_or_errored_use_counts[memory_id]
            evaluated_use_count = passed_use_count + failed_or_errored_use_count
            metrics.append(
                MemoryOutcomeMetrics(
                    memory_id=memory_id,
                    candidate_count=candidate_counts[memory_id],
                    used_count=used_counts[memory_id],
                    blocked_count=blocked_counts[memory_id],
                    evaluated_use_count=evaluated_use_count,
                    passed_use_count=passed_use_count,
                    failed_or_errored_use_count=failed_or_errored_use_count,
                    unevaluated_use_count=unevaluated_use_counts[memory_id],
                    observed_pass_rate=(
                        passed_use_count / evaluated_use_count
                        if evaluated_use_count
                        else None
                    ),
                )
            )
        return tuple(metrics)

    def _pr_related_case_records(
        self,
        context: MemoryContext,
        *,
        changes: tuple[tuple[str, str | None, str | None], ...] | None = None,
    ) -> list[tuple[FailureCase, Trace, PRChangeEndpoint | None]]:
        changed_fields = frozenset(entry[0] for entry in changes or ())
        records: list[tuple[FailureCase, Trace, PRChangeEndpoint | None]] = []
        for case in self._failure_cases.values():
            trace = self._traces.get(case.source_trace_id)
            if trace is None or not _case_matches_context(
                case,
                trace,
                context,
                changed_fields,
                change_set_active=changes is not None,
            ):
                continue
            endpoint = (
                _matched_pr_change_endpoint(trace, changes)
                if changes is not None
                else None
            )
            if changes is None or endpoint is not None:
                records.append((case, trace, endpoint))
        return records

    @_synchronized
    def pr_report_commit_anchors(
        self,
        context: MemoryContext,
        *,
        change_set: PRChangeSet | None = None,
    ) -> tuple[str, ...]:
        validate_memory_context(context)
        changes = None
        if change_set is not None:
            changes = _validated_pr_change_set(context, change_set)
        return tuple(
            sorted(
                {
                    case.commit_sha
                    for case, _trace, _endpoint in self._pr_related_case_records(
                        context, changes=changes
                    )
                }
            )
        )

    @_synchronized
    def pr_memory_report(
        self,
        context: MemoryContext,
        *,
        changed_fields: list[str] | None = None,
        change_set: PRChangeSet | None = None,
        commit_ancestry: CommitAncestryEvidence | None = None,
    ) -> PRMemoryReport:
        validate_memory_context(context)
        if (changed_fields is None) == (change_set is None):
            raise ValueError(
                "exactly one of changed_fields or change_set must be provided"
            )
        changes = None
        if changed_fields is not None:
            warning_fields = _validated_legacy_pr_warning_fields(changed_fields)
        else:
            changes = _validated_pr_change_set(context, change_set)
            warning_fields = tuple(
                field_name for field_name, _old, _new in changes
            )
        ancestry_relations = _validated_commit_ancestry(
            context, commit_ancestry
        )
        related_case_records = self._pr_related_case_records(context, changes=changes)
        if ancestry_relations is not None:
            _require_commit_relations(
                (case.commit_sha for case, _trace, _endpoint in related_case_records),
                ancestry_relations,
            )
            related_case_records = [
                record
                for record in related_case_records
                if ancestry_relations[record[0].commit_sha]
            ]
        related_case_records.sort(key=lambda record: record[0].case_id)

        related_cases = [case for case, _trace, _endpoint in related_case_records]
        related_case_ids = [case.case_id for case in related_cases]
        suggested_regression_tests = [_regression_suggestion(case, context) for case in related_cases]
        warnings = [
            _change_warning(case, context, changed_field)
            for case in related_cases
            for changed_field in warning_fields
        ]

        return PRMemoryReport(
            related_case_ids=related_case_ids,
            suggested_regression_tests=_unique(suggested_regression_tests),
            warnings=_unique(warnings),
            related_case_provenance=[
                _case_provenance(case, trace, endpoint)
                for case, trace, endpoint in related_case_records
            ],
        )


def _stored_record(records: Mapping[str, Any], record_id: str, record_label: str) -> Any:
    if not isinstance(record_id, str) or not record_id:
        raise ValueError(f"{record_label} ID must be a non-empty string")
    if len(record_id) > MEMORY_ID_MAX_CHARS:
        raise ValueError(
            f"{record_label} ID must be at most {MEMORY_ID_MAX_CHARS} characters"
        )
    record = records.get(record_id)
    if record is None:
        raise ValueError(f"unknown {record_label} ID: {record_id}")
    return record


def _validated_memory_obsolescence_requests(
    requests: Any,
) -> tuple[MemoryObsolescenceRequest, ...]:
    if type(requests) is not tuple or not requests:
        raise ValueError(
            "memory batch obsolescence requires a non-empty "
            "MemoryObsolescenceRequest tuple"
        )

    memory_ids: list[str] = []
    for request in requests:
        _require_exact_record(
            request,
            MemoryObsolescenceRequest,
            "memory obsolescence request",
        )
        if (
            type(request.memory_kind) is not str
            or request.memory_kind not in MEMORY_KINDS
        ):
            raise ValueError(
                "memory_kind must be one of failure_case, lesson, "
                "or project_policy"
            )
        _validate_required_string(
            request.memory_id,
            "memory_id",
            "memory obsolescence requests require",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        memory_ids.append(request.memory_id)

    if len(set(memory_ids)) != len(memory_ids):
        raise ValueError(
            "memory batch obsolescence requires unique memory_ids"
        )
    return requests


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


def _validated_legacy_pr_warning_fields(
    changed_fields: object,
) -> tuple[str, ...]:
    if not isinstance(changed_fields, list):
        raise ValueError("changed_fields must be a list of non-empty strings")

    validated: list[str] = []
    seen: set[str] = set()
    for field_name in changed_fields:
        if not isinstance(field_name, str) or not field_name.strip():
            raise ValueError("changed_fields must be a list of non-empty strings")
        if field_name in LEGACY_PR_WARNING_FIELDS and field_name not in seen:
            seen.add(field_name)
            validated.append(field_name)
    return tuple(validated)


def _validated_pr_change_set(
    context: MemoryContext,
    change_set: PRChangeSet,
) -> tuple[tuple[str, str | None, str | None], ...]:
    validate_memory_context(context)
    if type(change_set) is not PRChangeSet:
        raise ValueError("change_set must be a PRChangeSet")

    field_changes = change_set.field_changes
    if type(field_changes) is not tuple or not field_changes:
        raise ValueError("change_set.field_changes must be a non-empty tuple")
    if len(field_changes) > PR_CHANGE_SET_MAX_FIELDS:
        raise ValueError(
            "change_set.field_changes accepts at most "
            f"{PR_CHANGE_SET_MAX_FIELDS} entries"
        )
    if any(type(entry) is not tuple or len(entry) != 3 for entry in field_changes):
        raise ValueError("change_set entries must be 3-item tuples")

    seen_fields: set[str] = set()
    duplicate_fields: set[str] = set()
    unsupported_fields: set[str] = set()
    for entry in field_changes:
        field_name = entry[0]
        if type(field_name) is not str:
            raise ValueError("change_set field names must be strings")
        if field_name in seen_fields:
            duplicate_fields.add(field_name)
        else:
            seen_fields.add(field_name)
        if field_name not in PR_CHANGE_SET_FIELDS:
            unsupported_fields.add(field_name)
    if unsupported_fields:
        raise ValueError(
            "unsupported change_set fields: " + ", ".join(sorted(unsupported_fields))
        )

    if duplicate_fields:
        raise ValueError(
            "duplicate change_set fields: "
            + ", ".join(sorted(duplicate_fields))
        )

    validated: list[tuple[str, str | None, str | None]] = []
    for field_name, old_value, new_value in field_changes:
        for endpoint_value in (old_value, new_value):
            if endpoint_value is not None and type(endpoint_value) is not str:
                raise ValueError(
                    f"change_set {field_name} endpoint values must be None or strings"
                )
            if endpoint_value is not None and (
                not endpoint_value or not endpoint_value.strip()
            ):
                raise ValueError(
                    f"change_set {field_name} endpoint values must be non-empty, "
                    "non-whitespace strings or None"
                )
            if endpoint_value is not None and len(endpoint_value) > METADATA_VALUE_MAX_CHARS:
                raise ValueError(
                    f"change_set {field_name} endpoint values must be at most "
                    f"{METADATA_VALUE_MAX_CHARS} characters"
                )
        if old_value == new_value:
            raise ValueError(
                f"change_set {field_name} old and new values must differ"
            )
        if new_value != getattr(context, field_name):
            raise ValueError(f"change_set {field_name} new value must match context")
        validated.append((field_name, old_value, new_value))

    return tuple(sorted(validated, key=lambda entry: entry[0]))


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
    for field_name in DECLARED_TRACE_CONTEXT_FIELDS:
        context_value = getattr(context, field_name)
        if context_value is not None and getattr(trace, field_name) != context_value:
            raise ValueError(
                f"trace {field_name} does not match memory context: {trace.trace_id}"
            )
    if (
        context.tool is not None
        and context.tool not in _trace_change_set_tool_names(trace)
    ):
        raise ValueError(
            f"trace tool does not match memory context: {trace.trace_id}"
        )
    if context.input_hash is not None:
        if trace.input_hash != context.input_hash:
            raise ValueError(
                f"trace input_hash does not match memory context: {trace.trace_id}"
            )


def _snapshot_records(data: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = data[key]
    if not isinstance(value, list):
        raise ValueError(f"snapshot field {key!r} must be a list")
    if any(not isinstance(record, dict) for record in value):
        raise ValueError(f"snapshot field {key!r} must contain JSON objects")
    return [dict(record) for record in value]


def _validate_snapshot_collection_limits(
    data: Mapping[str, Any],
    *,
    max_records_per_collection: int | None,
    max_total_records: int | None,
) -> None:
    per_collection_limit = validate_non_negative_limit(
        max_records_per_collection,
        "max_records_per_collection",
    )
    total_limit = validate_non_negative_limit(
        max_total_records,
        "max_total_records",
    )
    total_records = 0
    for key in SNAPSHOT_COLLECTION_NAMES:
        value = data[key]
        if not isinstance(value, list):
            raise ValueError(f"snapshot field {key!r} must be a list")
        record_count = validate_snapshot_record_count(
            key,
            len(value),
            max_records_per_collection=per_collection_limit,
        )
        total_records += record_count
    validate_snapshot_total_record_count(
        total_records,
        max_total_records=total_limit,
    )


def _snapshot_record_instance(
    record_type: type[Any], data: dict[str, Any], record_label: str
) -> Any:
    try:
        return record_type(**data)
    except TypeError as exc:
        raise ValueError(f"invalid {record_label} record: {exc}") from exc


def _require_exact_record(value: Any, record_type: type[Any], record_label: str) -> None:
    if type(value) is not record_type:
        raise ValueError(
            f"{record_label} must be exactly a {record_type.__name__} record"
        )


def _copy_trace_for_storage(trace: Trace) -> Trace:
    try:
        return deepcopy(trace)
    except RecursionError as exc:
        raise ValueError("trace changed while being copied") from exc
    except RuntimeError as exc:
        if "changed size during iteration" not in str(exc):
            raise
        raise ValueError("trace changed while being copied") from exc


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
    if trace.latency_ms is not None:
        _validate_json_integer(trace.latency_ms, "latency_ms")
        if trace.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if trace.latency_ms > TRACE_LATENCY_MAX_MS:
            raise ValueError(
                f"latency_ms must be at most {TRACE_LATENCY_MAX_MS}"
            )
    if trace.cost_usd is not None:
        if not is_finite_number(trace.cost_usd):
            raise ValueError("cost_usd must be a finite number or None")
        if type(trace.cost_usd) is int:
            try:
                _validate_json_integer(trace.cost_usd, "cost_usd")
            except ValueError as exc:
                raise ValueError(
                    "cost_usd must be a finite number or None; "
                    "integer exceeds JSON serialization limits"
                ) from exc
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
            nonblank=field_name in {"fix", "fix_commit_sha"},
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
        not isinstance(log.trace_id, str) or not log.trace_id.strip()
    ):
        raise ValueError("usage log trace_id must be a non-empty string")
    if log.trace_id is not None and len(log.trace_id) > MEMORY_ID_MAX_CHARS:
        raise ValueError(
            f"usage log trace_id must be at most {MEMORY_ID_MAX_CHARS} characters"
        )
    _validate_string_mapping(log.context, "context")
    if MEMORY_SOURCE_IDENTITY_CONTEXT_FIELDS.intersection(log.context):
        raise ValueError(
            "usage log context must not persist memory source identity"
        )
    candidate_memory_id_set = set(log.candidate_memory_ids)
    _validate_status_mapping(
        log.candidate_memory_statuses,
        log.candidate_memory_ids,
        candidate_memory_id_set=candidate_memory_id_set,
    )
    _validate_string_mapping(log.system_blocked_reasons, "system_blocked_reasons")
    unknown_blocked_reasons = sorted(
        set(log.system_blocked_reasons).difference(candidate_memory_id_set)
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
        memory_id
        for memory_id in log.used_memory_ids
        if memory_id not in candidate_memory_id_set
    ]
    if missing_used_ids:
        raise ValueError(f"used memory ids must be present in candidates: {', '.join(missing_used_ids)}")
    missing_blocked_ids = [
        memory_id
        for memory_id in log.blocked_memory_ids
        if memory_id not in candidate_memory_id_set
    ]
    if missing_blocked_ids:
        raise ValueError(f"blocked memory ids must be present in candidates: {', '.join(missing_blocked_ids)}")
    blocked_memory_id_set = set(log.blocked_memory_ids)
    used_and_blocked = [
        memory_id
        for memory_id in log.used_memory_ids
        if memory_id in blocked_memory_id_set
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


def _memory_run_audit_status(
    trace_eval_result: EvalResult,
    decision_eval_result: EvalResult | None,
) -> MemoryRunAuditStatus:
    trace_measured = trace_eval_result in EVALUATED_RESULTS
    decision_measured = decision_eval_result in EVALUATED_RESULTS
    if not trace_measured and not decision_measured:
        return "pending"
    if trace_measured and not decision_measured:
        return "trace_only"
    if not trace_measured:
        return "decision_only"
    if trace_eval_result == decision_eval_result:
        return "complete"
    return "conflict"


def _memory_run_remediation(audit: MemoryRunAudit) -> MemoryRunRemediation:
    action: MemoryRunRemediationAction
    resolved_eval_result: _MeasuredEvalResult | None = None
    resolved_memory_caused_failure: bool | None = None

    if audit.status == "pending":
        action = "measure"
    elif audit.status == "trace_only":
        resolved_eval_result = cast(
            _MeasuredEvalResult,
            audit.trace_eval_result,
        )
        if resolved_eval_result == "pass":
            action = "recover"
            resolved_memory_caused_failure = False
        else:
            action = "recover_with_attribution"
    elif audit.status == "decision_only":
        action = "recover"
        resolved_eval_result = cast(
            _MeasuredEvalResult,
            audit.decision_eval_result,
        )
        resolved_memory_caused_failure = audit.memory_caused_failure
    elif audit.status == "complete":
        action = "none"
        resolved_eval_result = cast(
            _MeasuredEvalResult,
            audit.trace_eval_result,
        )
        resolved_memory_caused_failure = audit.memory_caused_failure
    else:
        action = "investigate"

    return MemoryRunRemediation(
        decision_id=audit.decision_id,
        trace_id=audit.trace_id,
        run_id=audit.run_id,
        status=audit.status,
        action=action,
        trace_eval_result=audit.trace_eval_result,
        decision_eval_result=audit.decision_eval_result,
        memory_caused_failure=audit.memory_caused_failure,
        resolved_eval_result=resolved_eval_result,
        resolved_memory_caused_failure=resolved_memory_caused_failure,
    )


def _resolved_memory_run_recovery(
    current_trace: Trace,
    current_log: MemoryUsageLog,
    memory_caused_failure: Any,
) -> tuple[MemoryRunAuditStatus, _MeasuredEvalResult, bool]:
    if (
        memory_caused_failure is not _UNSET
        and type(memory_caused_failure) is not bool
    ):
        raise ValueError("memory_caused_failure must be a boolean")

    status = _memory_run_audit_status(
        current_trace.eval_result,
        current_log.eval_result,
    )
    if status == "pending":
        raise ValueError(
            f"memory run has no measured outcome: {current_log.decision_id}"
        )
    if status == "conflict":
        raise ValueError(
            f"memory run has conflicting outcomes: {current_log.decision_id}"
        )

    if status == "trace_only":
        eval_result = cast(_MeasuredEvalResult, current_trace.eval_result)
        if memory_caused_failure is _UNSET:
            if eval_result in {"fail", "error"}:
                raise ValueError(
                    "failed or errored trace requires explicit "
                    "memory_caused_failure"
                )
            resolved_memory_caused_failure = False
        else:
            resolved_memory_caused_failure = memory_caused_failure
    else:
        eval_result = cast(_MeasuredEvalResult, current_log.eval_result)
        resolved_memory_caused_failure = (
            current_log.memory_caused_failure
            if memory_caused_failure is _UNSET
            else memory_caused_failure
        )

    _validate_measured_outcome(eval_result, resolved_memory_caused_failure)
    return status, eval_result, resolved_memory_caused_failure


def _validated_memory_run_results(
    results: Any,
) -> tuple[MemoryRunResult, ...]:
    if type(results) is not tuple or not results:
        raise ValueError(
            "memory run batch completion requires a non-empty "
            "MemoryRunResult tuple"
        )

    decision_ids: list[str] = []
    for result in results:
        _require_exact_record(result, MemoryRunResult, "memory run result")
        _validate_required_string(
            result.decision_id,
            "decision_id",
            "memory run results require",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
        _validate_measured_outcome(
            result.eval_result,
            result.memory_caused_failure,
        )
        if (
            result.tool_outputs is not None
            and type(result.tool_outputs) is not tuple
        ):
            raise ValueError("memory run result tool_outputs must be a tuple or None")
        decision_ids.append(result.decision_id)

    if len(set(decision_ids)) != len(decision_ids):
        raise ValueError("memory run batch completion requires unique decision_ids")
    return results


def _memory_run_result_completion_values(
    result: MemoryRunResult,
) -> dict[str, Any]:
    completion_values = {
        field_name: getattr(result, field_name)
        for field_name in (
            "output_hash",
            "latency_ms",
            "cost_usd",
            "error",
            "trace_uri",
        )
        if getattr(result, field_name) is not None
    }
    if result.tool_outputs is not None:
        completion_values["tool_outputs"] = list(result.tool_outputs)
    return completion_values


def _validated_batch_recovery_decision_ids(
    decision_ids: Any,
) -> tuple[str, ...]:
    if type(decision_ids) is not tuple or not decision_ids:
        raise ValueError(
            "memory run batch recovery requires a non-empty decision_id tuple"
        )
    for decision_id in decision_ids:
        _validate_required_string(
            decision_id,
            "decision_id",
            "memory run batch recovery requires",
            max_chars=MEMORY_ID_MAX_CHARS,
        )
    if len(set(decision_ids)) != len(decision_ids):
        raise ValueError("memory run batch recovery requires unique decision_ids")
    return decision_ids


def _validated_batch_recovery_attributions(
    memory_caused_failures: Any,
    decision_ids: tuple[str, ...],
) -> dict[str, bool]:
    if memory_caused_failures is None:
        return {}
    if not isinstance(memory_caused_failures, Mapping):
        raise ValueError("memory_caused_failures must be a mapping or None")

    attributions: dict[str, bool] = {}
    for decision_id, attribution in memory_caused_failures.items():
        if (
            not isinstance(decision_id, str)
            or not decision_id
            or len(decision_id) > MEMORY_ID_MAX_CHARS
        ):
            raise ValueError(
                "memory_caused_failures must contain decision_id keys"
            )
        if type(attribution) is not bool:
            raise ValueError(
                "memory_caused_failures must contain boolean values"
            )
        attributions[decision_id] = attribution

    requested_ids = set(decision_ids)
    unexpected_ids = sorted(set(attributions).difference(requested_ids))
    if unexpected_ids:
        raise ValueError(
            "memory_caused_failures contains unrequested decision_id: "
            + ", ".join(unexpected_ids)
        )
    return attributions


def _provided_trace_completion_values(
    *,
    output_hash: Any,
    tool_outputs: Any,
    latency_ms: Any,
    cost_usd: Any,
    error: Any,
    trace_uri: Any,
) -> dict[str, Any]:
    supplied_values = {
        "output_hash": output_hash,
        "tool_outputs": tool_outputs,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "error": error,
        "trace_uri": trace_uri,
    }
    return {
        field_name: value
        for field_name, value in supplied_values.items()
        if value is not _UNSET
    }


def _completed_trace_candidate(
    current: Trace,
    eval_result: _MeasuredEvalResult,
    completion_values: Mapping[str, Any],
) -> Trace:
    candidate = replace(
        current,
        eval_result=eval_result,
        **completion_values,
    )
    _validate_trace(candidate)
    completed = _copy_trace_for_storage(candidate)
    _validate_trace(completed)

    if current.eval_result in EVALUATED_RESULTS:
        if completed == current:
            return current
        raise ValueError(
            f"trace execution already completed: {current.trace_id}"
        )

    for field_name in completion_values:
        current_value = getattr(current, field_name)
        completed_value = getattr(completed, field_name)
        if (
            not _trace_completion_slot_is_empty(field_name, current_value)
            and current_value != completed_value
        ):
            raise ValueError(
                "trace completion cannot rewrite "
                f"{field_name}: {current.trace_id}"
            )
    return completed


def _sealed_usage_outcome_candidate(
    current: MemoryUsageLog,
    eval_result: _MeasuredEvalResult,
    memory_caused_failure: bool,
) -> MemoryUsageLog:
    if current.eval_result in EVALUATED_RESULTS:
        if (
            current.eval_result == eval_result
            and current.memory_caused_failure is memory_caused_failure
        ):
            _validate_usage_log(current)
            return current
        raise ValueError(
            f"decision outcome already sealed: {current.decision_id}"
        )

    sealed = replace(
        current,
        eval_result=eval_result,
        memory_caused_failure=memory_caused_failure,
    )
    _validate_usage_log(sealed)
    return sealed


def _validate_measured_outcome(
    eval_result: _MeasuredEvalResult, memory_caused_failure: bool
) -> None:
    _validate_measured_eval_result(eval_result, "decision outcome")
    if type(memory_caused_failure) is not bool:
        raise ValueError("memory_caused_failure must be a boolean")
    if memory_caused_failure and eval_result not in {"fail", "error"}:
        raise ValueError(
            "memory_caused_failure requires eval_result fail or error"
        )


def _validate_measured_eval_result(
    eval_result: Any, record_label: str
) -> None:
    if not isinstance(eval_result, str) or eval_result not in EVALUATED_RESULTS:
        raise ValueError(
            f"{record_label} requires measured eval_result: error, fail, or pass"
        )


def _trace_completion_slot_is_empty(field_name: str, value: Any) -> bool:
    if field_name == "tool_outputs":
        return value == []
    return value is None


def _validate_required_string(
    value: Any,
    field_name: str,
    message_prefix: str,
    *,
    max_chars: int | None = None,
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{message_prefix} {field_name}")
    if max_chars is not None and len(value) > max_chars:
        raise ValueError(f"{field_name} must be at most {max_chars} characters")


def _validated_commit_ancestry(
    context: MemoryContext,
    evidence: CommitAncestryEvidence | None,
) -> dict[str, bool] | None:
    if evidence is None:
        return None
    if type(evidence) is not CommitAncestryEvidence:
        raise ValueError(
            "commit_ancestry must be a CommitAncestryEvidence or None"
        )
    _validate_required_string(
        evidence.current_commit_sha,
        "current_commit_sha",
        "commit ancestry evidence requires",
        max_chars=METADATA_VALUE_MAX_CHARS,
    )
    if evidence.current_commit_sha != context.commit_sha:
        raise ValueError(
            "commit ancestry current_commit_sha does not match context commit_sha"
        )
    if not isinstance(evidence.commit_relations, tuple):
        raise ValueError("commit ancestry commit_relations must be a tuple")

    relations: dict[str, bool] = {}
    for relation in evidence.commit_relations:
        if type(relation) is not tuple or len(relation) != 2:
            raise ValueError(
                "commit ancestry relations must be two-item tuples"
            )
        anchor, is_ancestor = relation
        _validate_required_string(
            anchor,
            "anchor commit",
            "commit ancestry relations require",
            max_chars=METADATA_VALUE_MAX_CHARS,
        )
        if type(is_ancestor) is not bool:
            raise ValueError("commit ancestry relation values must be booleans")
        if anchor in relations:
            raise ValueError(f"duplicate commit ancestry relation: {anchor}")
        relations[anchor] = is_ancestor
    return relations


def _require_commit_relations(
    anchors: Iterable[str | None], relations: Mapping[str, bool]
) -> None:
    missing = sorted(
        {
            anchor
            for anchor in anchors
            if anchor is not None and anchor not in relations
        }
    )
    if missing:
        raise ValueError(
            "commit ancestry evidence is missing anchors: " + ", ".join(missing)
        )


def _validate_optional_string(
    value: Any,
    field_name: str,
    record_label: str,
    *,
    nonblank: bool = False,
    max_chars: int | None = None,
) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not value
        or (nonblank and not value.strip())
    ):
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
    if not isinstance(value, list) or any(type(item) is not dict for item in value):
        raise ValueError(f"trace {field_name} must be a list of JSON objects")
    for index, item in enumerate(value):
        _validate_json_value(item, f"{field_name}[{index}]")


def _validate_json_value(value: Any, root_path: str) -> None:
    stack: list[tuple[Any, str, int, bool]] = [(value, root_path, 0, False)]
    active_container_ids: set[int] = set()

    while stack:
        current, path, depth, leaving = stack.pop()
        if leaving:
            active_container_ids.remove(id(current))
            continue

        if current is None or type(current) in {bool, str}:
            continue
        if type(current) is int:
            _validate_json_integer(current, path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise ValueError(f"trace {path} must be a finite JSON number")
            continue
        if type(current) not in {dict, list}:
            raise ValueError(
                f"trace {path} must contain only JSON semantic values"
            )
        if depth >= TRACE_JSON_MAX_DEPTH:
            raise ValueError(
                f"trace {path} exceeds maximum nesting depth "
                f"{TRACE_JSON_MAX_DEPTH}"
            )

        container_id = id(current)
        if container_id in active_container_ids:
            raise ValueError(f"trace {path} contains a reference cycle")
        active_container_ids.add(container_id)
        stack.append((current, path, depth, True))

        if type(current) is list:
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], f"{path}[{index}]", depth + 1, False))
            continue

        items = list(current.items())
        for key, child in reversed(items):
            if not isinstance(key, str):
                raise ValueError(f"trace {path} object keys must be strings")
            child_path = (
                f"{path}.{key}"
                if key.isidentifier()
                else f"{path}[{json.dumps(key)}]"
            )
            stack.append((child, child_path, depth + 1, False))


def _validate_json_integer(value: int, path: str) -> None:
    try:
        str(value)
    except ValueError as exc:
        raise ValueError(
            f"trace {path} integer exceeds JSON serialization limits"
        ) from exc


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
        or not key.strip()
        or not isinstance(item, str)
        or not item.strip()
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


def _validate_status_mapping(
    value: Any,
    candidate_memory_ids: list[str],
    *,
    candidate_memory_id_set: set[str] | None = None,
) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(memory_id, str)
        or not memory_id.strip()
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
    if candidate_memory_id_set is None:
        candidate_memory_id_set = set(candidate_memory_ids)
    status_memory_ids = set(value)
    if status_memory_ids != candidate_memory_id_set:
        missing_statuses = sorted(candidate_memory_id_set.difference(value))
        extra_statuses = sorted(
            status_memory_ids.difference(candidate_memory_id_set)
        )
        raise ValueError(
            "usage log candidate_memory_statuses must match candidates"
            + (f"; missing: {', '.join(missing_statuses)}" if missing_statuses else "")
            + (f"; extra: {', '.join(extra_statuses)}" if extra_statuses else "")
        )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@contextmanager
def _atomic_utf8_writer(
    target: Path,
    *,
    overwrite: bool = True,
) -> Iterator[TextIO]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            yield temporary_file
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if overwrite:
            os.replace(temporary_path, target)
        else:
            os.link(temporary_path, target)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


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
        lines.append("    lesson_text: |")
        for text_line in lesson.lesson_text.split("\n"):
            lines.append(f"      {text_line}")
    return "\n".join(lines) + "\n"


def _lessons_from_yaml(
    text: str,
    *,
    max_lessons: int | None = LESSONS_YAML_MAX_RECORDS,
) -> list[dict[str, Any]]:
    lesson_limit = validate_non_negative_limit(max_lessons, "max_lessons")
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
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
            current[block_key] = "\n".join(block_lines)
        block_key = None
        block_lines = []

    def finish_lesson() -> None:
        finish_block()
        if current is not None:
            if lesson_limit is not None and len(lessons) >= lesson_limit:
                raise ValueError(
                    f"lessons YAML contains more than {lesson_limit} records"
                )
            lessons.append(current)

    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if block_key is not None and not stripped:
            block_lines.append(
                raw_line[6:] if raw_line.startswith("      ") else ""
            )
            continue

        if block_key is not None and indent >= 6:
            block_lines.append(
                raw_line[6:] if raw_line.startswith("      ") else stripped
            )
            continue

        if not stripped:
            continue

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

        if indent == 4:
            finish_block()
            key, value = _yaml_key_value(stripped)
            if key in current:
                raise ValueError(f"duplicate lesson field: {key}")
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
            scope = current["scope"]
            if type(scope) is not dict:
                raise TypeError("lesson scope must be a mapping")
            if key in scope:
                raise ValueError(f"duplicate lesson scope field: {key}")
            scope[key] = _parse_yaml_scalar(value)
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


def _pass_rate(passed_count: int, evaluated_count: int) -> float | None:
    if evaluated_count == 0:
        return None
    return passed_count / evaluated_count


def _validated_semantic_scores(
    semantic_scores: Mapping[str, float] | None,
    *,
    query: str | None,
    max_candidates: int | None,
    minimum_score: float | None,
    stored_memory_ids: set[str],
) -> dict[str, int | float] | None:
    if semantic_scores is None:
        if max_candidates is not None:
            raise ValueError("max_candidates requires semantic_scores")
        if minimum_score is not None:
            raise ValueError("minimum_score requires semantic_scores")
        return None
    if not isinstance(semantic_scores, Mapping):
        raise ValueError("semantic_scores must be a mapping or None")
    if query is not None:
        raise ValueError("query and semantic_scores are mutually exclusive")
    if max_candidates is None:
        raise ValueError("max_candidates is required with semantic_scores")
    if type(max_candidates) is not int or not 1 <= max_candidates <= LLM_GATE_MAX_CANDIDATES:
        raise ValueError(
            "max_candidates must be an integer from 1 through "
            f"{LLM_GATE_MAX_CANDIDATES}"
        )
    if minimum_score is not None and not is_finite_number(minimum_score):
        raise ValueError("minimum_score must be a finite number")

    validated: dict[str, int | float] = {}
    for memory_id, score in semantic_scores.items():
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("semantic score memory IDs must be non-empty strings")
        if len(memory_id) > MEMORY_ID_MAX_CHARS:
            raise ValueError(
                "semantic score memory IDs must be at most "
                f"{MEMORY_ID_MAX_CHARS} characters"
            )
        if not is_finite_number(score):
            raise ValueError(
                f"semantic score for {memory_id!r} must be a finite number"
            )
        validated[memory_id] = score

    unknown_ids = sorted(set(validated).difference(stored_memory_ids))
    if unknown_ids:
        raise ValueError(
            "semantic_scores references unknown memory IDs: "
            + ", ".join(unknown_ids)
        )
    return validated


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 2}


def _memory_tokens(memory: MemoryItem) -> set[str]:
    scope_text = " ".join(f"{key} {value}" for key, value in memory.scope.items())
    return _tokens(f"{memory.memory_id} {memory.text} {scope_text}")


def _case_matches_context(
    case: FailureCase,
    trace: Trace,
    context: MemoryContext,
    changed_fields: frozenset[str] = frozenset(),
    *,
    change_set_active: bool = False,
) -> bool:
    if case.status != "verified" or not case.regression_passed:
        return False
    if trace.repo != context.repo:
        return False
    if trace.tenant != context.tenant:
        return False
    if context.failure_type is not None and case.failure_type != context.failure_type:
        return False
    if "tool" not in changed_fields and context.tool is not None:
        tool_names = (
            _trace_change_set_tool_names(trace)
            if change_set_active
            else _trace_tool_names(trace)
        )
        if context.tool not in tool_names:
            return False
    for field_name in [
        "prompt_version",
        "prompt_family",
        "tool_schema_version",
        "model",
        "eval_suite",
    ]:
        if field_name in changed_fields:
            continue
        context_value = getattr(context, field_name)
        if context_value is not None and getattr(trace, field_name) != context_value:
            return False
    return True


def _trace_tool_names(trace: Trace) -> set[str]:
    return {str(call["name"]) for call in trace.tool_calls if call.get("name")}


def _trace_change_set_tool_names(trace: Trace) -> set[str]:
    tool_names: set[str] = set()
    for call in trace.tool_calls:
        name = call.get("name")
        if type(name) is str and name:
            tool_names.add(name)
    return tool_names


def _trace_matches_change_value(
    trace: Trace, field_name: str, expected: str | None
) -> bool:
    if field_name == "tool":
        tool_names = _trace_change_set_tool_names(trace)
        return not tool_names if expected is None else expected in tool_names
    return getattr(trace, field_name) == expected


def _matched_pr_change_endpoint(
    trace: Trace,
    changes: tuple[tuple[str, str | None, str | None], ...],
) -> PRChangeEndpoint | None:
    old_matches = all(
        _trace_matches_change_value(trace, field_name, old_value)
        for field_name, old_value, _new_value in changes
    )
    new_matches = all(
        _trace_matches_change_value(trace, field_name, new_value)
        for field_name, _old_value, new_value in changes
    )
    if old_matches and new_matches:
        return "both"
    if old_matches:
        return "old"
    if new_matches:
        return "new"
    return None


def _regression_suggestion(case: FailureCase, context: MemoryContext) -> str:
    tool = context.tool or "affected tool"
    return f"Run {case.failure_type} regression for tool {tool} before merging."


def _case_provenance(
    case: FailureCase,
    trace: Trace,
    endpoint: PRChangeEndpoint | None = None,
) -> PRCaseProvenance:
    return PRCaseProvenance(
        case_id=case.case_id,
        source_trace_id=case.source_trace_id,
        commit_sha=case.commit_sha,
        fix_commit_sha=case.fix_commit_sha,
        trace_uri=trace.trace_uri,
        failure_type=case.failure_type,
        matched_change_endpoint=endpoint,
    )


def _change_warning(case: FailureCase, context: MemoryContext, changed_field: str) -> str:
    target = context.tool or "known failure area"
    return f"{changed_field} change touches known failure case {case.case_id} for {target}."


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
