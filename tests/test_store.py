import json
import os
import threading
import time
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.store as store_module
from trace_backed_memory import (
    CommitAncestryEvidence,
    FailureCase,
    GatedMemoryResult,
    Lesson,
    MemoryContext,
    MemoryDecision,
    MemoryMetrics,
    MemoryUsageLog,
    METADATA_VALUE_MAX_CHARS,
    PRCaseProvenance,
    PRChangeEndpoint,
    PRChangeSet,
    ProjectPolicy,
    Trace,
    TraceBackedMemoryStore,
    apply_llm_gate_decision,
    draft_failure_case,
    lesson_from_failure_case,
    obsolete_failure_case,
    obsolete_lesson,
    review_failure_case,
    system_gate,
    verify_failure_case,
)


class IntLike(int):
    pass


def store_with_verified_case(
    *, repo: str = "repo", tenant: str | None = "tenant_a"
) -> tuple[TraceBackedMemoryStore, Trace, FailureCase]:
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_contract",
            run_id="run_contract",
            commit_sha="abc",
            repo=repo,
            tenant=tenant,
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_contract",
            failure_type="invalid_tool_argument",
            symptom="search_docs received an empty query",
        ),
        fix="require a non-empty query",
        fix_commit_sha="def",
        regression_passed=True,
    )
    store.add_failure_case(case)
    return store, trace, case


def store_with_records_in_order(trace_ids: list[str]) -> TraceBackedMemoryStore:
    store = TraceBackedMemoryStore()
    for trace_id in trace_ids:
        store.record_trace(
            Trace(
                trace_id=trace_id,
                run_id=f"run_{trace_id}",
                commit_sha=f"commit_{trace_id}",
                repo="repo",
                eval_result="unknown",
            )
        )
    return store


def pending_execution_trace(
    *,
    trace_id: str = "trace_pending_execution",
    run_id: str = "run_pending_execution",
    **changes: object,
) -> Trace:
    return replace(
        Trace(
            trace_id=trace_id,
            run_id=run_id,
            commit_sha="commit_pending",
            repo="repo",
            tenant="tenant_a",
            branch="main",
            prompt_version="planner_v3",
            prompt_family="planner",
            tool_schema_version="search_docs_v2",
            model="gpt-test",
            eval_suite="tool_regression",
            input_hash="sha256:pending-input",
            retrieved_context=[{"source": "docs", "rank": 1}],
            tool_calls=[{"name": "search_docs", "arguments": {"query": "memory"}}],
            eval_result="unknown",
            created_at="2025-07-13T08:00:00Z",
        ),
        **changes,
    )


def store_with_retrieval_records_in_order(
    suffixes: list[str],
) -> TraceBackedMemoryStore:
    store = TraceBackedMemoryStore()
    for suffix in suffixes:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_{suffix}",
                run_id=f"run_{suffix}",
                commit_sha=f"commit_{suffix}",
                repo="repo",
                tenant="tenant",
                eval_result="fail",
            )
        )
        case = store.add_failure_case(
            FailureCase(
                case_id=f"case_{suffix}",
                source_trace_id=trace.trace_id,
                commit_sha=trace.commit_sha,
                failure_type="invalid_tool_argument",
                symptom=f"symptom {suffix}",
                fix=f"fix {suffix}",
                fix_commit_sha=f"fix_commit_{suffix}",
                regression_passed=True,
                status="verified",
            )
        )
        store.add_lesson(
            Lesson(
                lesson_id=f"lesson_{suffix}",
                source_case_id=case.case_id,
                lesson_text=f"lesson {suffix}",
                memory_type="procedural",
                scope={"repo": "repo", "tenant": "tenant"},
            )
        )
        store.add_project_policy(
            ProjectPolicy(
                policy_id=f"policy_{suffix}",
                policy_text=f"policy {suffix}",
                scope={"repo": "repo", "tenant": "tenant"},
            )
        )
    return store


def ancestry_evidence(current: str, **relations: bool) -> CommitAncestryEvidence:
    return CommitAncestryEvidence(
        current_commit_sha=current,
        commit_relations=tuple(relations.items()),
    )


def ancestry_context() -> MemoryContext:
    return MemoryContext(
        mode="debug",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        failure_type="invalid_tool_argument",
    )


def store_with_active_lesson() -> tuple[
    TraceBackedMemoryStore, Trace, FailureCase, Lesson
]:
    store, trace, case = store_with_verified_case()
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
    )
    store.add_lesson(lesson)
    return store, trace, case, lesson


def matching_context(trace: Trace) -> MemoryContext:
    return MemoryContext(
        mode="repair",
        repo=trace.repo or "repo",
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
    )


def store_with_usage_decision(
    *,
    eval_result: str | None = None,
    use_memory: bool = True,
) -> tuple[TraceBackedMemoryStore, MemoryUsageLog, Lesson]:
    store, trace, _case, lesson = store_with_active_lesson()
    log = store.log_decision(
        trace.run_id,
        matching_context(trace),
        [lesson.lesson_id],
        MemoryDecision(
            use_memory=use_memory,
            allowed_memory_ids=[lesson.lesson_id] if use_memory else [],
            blocked_memory_ids=[],
            reason="directly relevant" if use_memory else "not needed",
            risk="low" if use_memory else "none",
            recommended_injection="short_summary" if use_memory else "none",
        ),
        eval_result=eval_result,
    )
    return store, log, lesson


def store_with_pending_memory_run(
    *,
    use_memory: bool = True,
    current_trace_changes: dict[str, object] | None = None,
) -> tuple[TraceBackedMemoryStore, Trace, GatedMemoryResult, Lesson]:
    store, source_trace, _case, lesson = store_with_active_lesson()
    current = store.record_trace(
        replace(
            source_trace,
            trace_id="trace_pending_memory_run",
            run_id="run_pending_memory_run",
            eval_result="unknown",
            **(current_trace_changes or {}),
        )
    )
    request = store.prepare_memory(matching_context(current), task="repair")
    payload = (
        allow_decision(lesson.lesson_id)
        if use_memory
        else {
            "use_memory": False,
            "allowed_memory_ids": [],
            "blocked_memory_ids": [],
            "reason": "memory not needed",
            "risk": "none",
            "recommended_injection": "none",
        }
    )
    result = store.finalize_memory(
        request,
        payload,
        trace_id=current.trace_id,
    )
    return store, current, result, lesson


def add_pending_memory_run(
    store: TraceBackedMemoryStore,
    source_trace: Trace,
    lesson: Lesson,
    *,
    suffix: str,
) -> tuple[Trace, GatedMemoryResult]:
    current = store.record_trace(
        replace(
            source_trace,
            trace_id=f"trace_audit_{suffix}",
            run_id=f"run_audit_{suffix}",
            output_hash=None,
            tool_outputs=[],
            eval_result="unknown",
            latency_ms=None,
            cost_usd=None,
            error=None,
            trace_uri=None,
        )
    )
    request = store.prepare_memory(matching_context(current), task="audit run")
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current.trace_id,
    )
    return current, result


def store_with_declared_trace_provenance() -> tuple[
    TraceBackedMemoryStore,
    Trace,
    Lesson,
    MemoryContext,
]:
    store = TraceBackedMemoryStore()
    source_trace = store.record_trace(
        Trace(
            trace_id="trace_provenance_source",
            run_id="run_provenance_source",
            commit_sha="abc",
            repo="repo",
            tenant="tenant_a",
            branch="main",
            prompt_version="planner_v3",
            prompt_family="planner",
            tool_schema_version="search_docs_v2",
            model="gpt-test",
            eval_suite="tool_regression",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                source_trace,
                case_id="case_provenance",
                failure_type="invalid_tool_argument",
                symptom="search_docs received an empty query",
            ),
            fix="require a non-empty query",
            fix_commit_sha="def",
            regression_passed=True,
        )
    )
    lesson = store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_provenance",
            lesson_text="Always pass a non-empty query to search_docs.",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a"},
        )
    )
    current_trace = store.record_trace(
        replace(
            source_trace,
            trace_id="trace_provenance_current",
            run_id="run_provenance_current",
            eval_result="unknown",
        )
    )
    context = MemoryContext(
        mode="production",
        repo="repo",
        tenant="tenant_a",
        commit_sha="abc",
        branch="main",
        prompt_version="planner_v3",
        prompt_family="planner",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
        model="gpt-test",
        model_family="gpt",
        eval_suite="tool_regression",
        task_type="tool_repair",
        failure_type="invalid_tool_argument",
    )
    return store, current_trace, lesson, context


def allow_decision(memory_id: str) -> dict[str, object]:
    return {
        "use_memory": True,
        "allowed_memory_ids": [memory_id],
        "blocked_memory_ids": [],
        "reason": "direct match",
        "risk": "low",
        "recommended_injection": "short_summary",
    }


BENCHMARK_BLOCK_REASON = "memory originates from current benchmark example"


class _BenchmarkIdentityString(str):
    def __str__(self) -> str:
        return "spoofed-by-subclass"


def store_with_benchmark_source_identity(
    *,
    eval_suite: str | None = "benchmark-suite",
    input_hash: str | None = "sha256:source-example",
) -> tuple[TraceBackedMemoryStore, Trace, FailureCase, Lesson, ProjectPolicy]:
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_benchmark_source",
            run_id="run_benchmark_source",
            commit_sha="abc",
            repo="repo",
            tenant="tenant_a",
            eval_suite=eval_suite,
            input_hash=input_hash,
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                trace,
                case_id="case_benchmark_source",
                failure_type="invalid_tool_argument",
                symptom="search_docs received an empty query",
            ),
            fix="require a non-empty query",
            fix_commit_sha="def",
            regression_passed=True,
        )
    )
    lesson = store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_benchmark_source",
            lesson_text="Always pass a non-empty query to search_docs.",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
        )
    )
    policy = store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_project",
            policy_text="Tool calls must follow their declared schema.",
            scope={"repo": "repo", "tenant": "tenant_a"},
        )
    )
    return store, trace, case, lesson, policy


def benchmark_context(
    trace: Trace,
    *,
    mode: str = "repair",
    eval_suite: str = "benchmark-suite",
    input_hash: str = "sha256:source-example",
) -> MemoryContext:
    return MemoryContext(
        mode=mode,  # type: ignore[arg-type]
        repo=trace.repo or "repo",
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
        eval_suite=eval_suite,
        input_hash=input_hash,
        failure_type="invalid_tool_argument",
    )


@pytest.mark.parametrize("mode", ["debug", "repair"])
def test_candidate_memories_enrich_source_identity_for_benchmark_example(
    mode: str,
):
    store, trace, case, lesson, policy = store_with_benchmark_source_identity()

    candidates = store.candidate_memories(benchmark_context(trace, mode=mode))
    by_id = {memory.memory_id: memory for memory in candidates}

    assert set(by_id) == {case.case_id, lesson.lesson_id, policy.policy_id}
    for memory_id in (case.case_id, lesson.lesson_id):
        assert (
            by_id[memory_id].source_eval_suite,
            by_id[memory_id].source_input_hash,
        ) == ("benchmark-suite", "sha256:source-example")
    assert (
        by_id[policy.policy_id].source_eval_suite,
        by_id[policy.policy_id].source_input_hash,
    ) == (None, None)


def test_candidate_memories_omit_incomplete_source_identity_pair():
    store, trace, case, lesson, policy = store_with_benchmark_source_identity(
        input_hash=None
    )

    candidates = store.candidate_memories(
        benchmark_context(trace, input_hash="sha256:current-example")
    )
    by_id = {memory.memory_id: memory for memory in candidates}

    assert set(by_id) == {case.case_id, lesson.lesson_id, policy.policy_id}
    assert all(
        (memory.source_eval_suite, memory.source_input_hash) == (None, None)
        for memory in candidates
    )


def test_string_subclass_trace_identity_is_normalized_before_same_example_gate():
    store, trace, case, lesson, _policy = store_with_benchmark_source_identity(
        eval_suite=_BenchmarkIdentityString("benchmark-suite"),
        input_hash=_BenchmarkIdentityString("sha256:source-example"),
    )

    request = store.prepare_memory(
        benchmark_context(trace),
        task="repair failed tool call",
    )

    assert dict(request.system_blocked) == {
        case.case_id: BENCHMARK_BLOCK_REASON,
        lesson.lesson_id: BENCHMARK_BLOCK_REASON,
    }


def test_prepare_and_finalize_audit_current_benchmark_example_blocks():
    store, trace, case, lesson, policy = store_with_benchmark_source_identity()
    context = benchmark_context(trace)

    request = store.prepare_memory(context, task="repair failed tool call")

    assert set(request.candidate_memory_ids) == {
        case.case_id,
        lesson.lesson_id,
        policy.policy_id,
    }
    assert request.system_allowed_memory_ids == (policy.policy_id,)
    assert dict(request.system_blocked) == {
        case.case_id: BENCHMARK_BLOCK_REASON,
        lesson.lesson_id: BENCHMARK_BLOCK_REASON,
    }
    assert case.case_id not in request.prompt
    assert lesson.lesson_id not in request.prompt
    assert policy.policy_id in request.prompt
    assert trace.input_hash not in request.prompt

    result = store.finalize_memory(
        request,
        allow_decision(policy.policy_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert result.allowed_memory_ids == (policy.policy_id,)
    log = store.usage_logs[-1]
    assert log.context["eval_suite"] == trace.eval_suite
    assert log.context["input_hash"] == trace.input_hash
    assert set(log.candidate_memory_ids) == {
        case.case_id,
        lesson.lesson_id,
        policy.policy_id,
    }
    assert log.candidate_memory_statuses == {
        case.case_id: "verified",
        lesson.lesson_id: "active",
        policy.policy_id: "active",
    }
    assert log.system_blocked_reasons == {
        case.case_id: BENCHMARK_BLOCK_REASON,
        lesson.lesson_id: BENCHMARK_BLOCK_REASON,
    }
    snapshot_text = json.dumps(store.to_snapshot(), sort_keys=True)
    assert "source_eval_suite" not in snapshot_text
    assert "source_input_hash" not in snapshot_text


def test_benchmark_identity_audit_round_trips_through_snapshot_v2_without_source_fields():
    store, trace, case, lesson, policy = store_with_benchmark_source_identity()
    context = benchmark_context(trace)
    request = store.prepare_memory(context, task="repair failed tool call")
    store.finalize_memory(
        request,
        allow_decision(policy.policy_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    snapshot = store.to_snapshot()
    serialized = json.dumps(snapshot, sort_keys=True)
    restored = TraceBackedMemoryStore.from_snapshot(snapshot)
    restored_log = restored.usage_logs[-1]

    assert snapshot["snapshot_version"] == 2
    assert restored.traces[trace.trace_id].input_hash == trace.input_hash
    assert restored_log.context["input_hash"] == trace.input_hash
    assert restored_log.system_blocked_reasons == {
        case.case_id: BENCHMARK_BLOCK_REASON,
        lesson.lesson_id: BENCHMARK_BLOCK_REASON,
    }
    assert "source_eval_suite" not in serialized
    assert "source_input_hash" not in serialized

    restored_candidates = {
        memory.memory_id: memory
        for memory in restored.candidate_memories(context)
    }
    assert (
        restored_candidates[lesson.lesson_id].source_eval_suite,
        restored_candidates[lesson.lesson_id].source_input_hash,
    ) == (trace.eval_suite, trace.input_hash)


def test_different_benchmark_example_allows_lesson_and_omits_hashes_from_snippet():
    store, source_trace, _case, lesson, _policy = store_with_benchmark_source_identity()
    current_trace = store.record_trace(
        replace(
            source_trace,
            trace_id="trace_current_example",
            run_id="run_current_example",
            input_hash="sha256:current-example",
            eval_result="unknown",
        )
    )
    context = benchmark_context(
        current_trace, input_hash="sha256:current-example"
    )
    request = store.prepare_memory(context, task="repair failed tool call")

    assert lesson.lesson_id in request.system_allowed_memory_ids
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current_trace.trace_id,
    )

    assert result.allowed_memory_ids == (lesson.lesson_id,)
    assert "Always pass a non-empty query" in result.snippet
    assert "sha256:source-example" not in result.snippet
    assert "sha256:current-example" not in result.snippet


@pytest.mark.parametrize(
    ("trace_changes", "expected_message"),
    [
        ({"eval_suite": "other-suite"}, "trace eval_suite does not match memory context"),
        ({"input_hash": "sha256:other-example"}, "trace input_hash does not match memory context"),
    ],
    ids=["eval-suite", "input-hash"],
)
def test_finalize_binds_benchmark_example_before_pending_request_consumption(
    trace_changes: dict[str, str], expected_message: str
):
    store, source_trace, _case, _lesson, policy = store_with_benchmark_source_identity()
    mismatched_trace = store.record_trace(
        replace(
            source_trace,
            trace_id="trace_mismatched_example",
            run_id="run_mismatched_example",
            **trace_changes,
        )
    )
    request = store.prepare_memory(
        benchmark_context(source_trace), task="repair failed tool call"
    )

    with pytest.raises(ValueError, match=expected_message):
        store.finalize_memory(
            request,
            allow_decision(policy.policy_id),
            trace_id=mismatched_trace.trace_id,
        )

    assert store.usage_logs == []
    result = store.finalize_memory(
        request,
        allow_decision(policy.policy_id),
        trace_id=source_trace.trace_id,
    )
    assert result.allowed_memory_ids == (policy.policy_id,)
    assert len(store.usage_logs) == 1


def benchmark_input_hash_audit_snapshot() -> dict[str, object]:
    store, trace, _case, lesson, _policy = store_with_benchmark_source_identity()
    store.log_decision(
        trace.run_id,
        benchmark_context(trace),
        [lesson.lesson_id],
        MemoryDecision(
            use_memory=False,
            allowed_memory_ids=[],
            blocked_memory_ids=[],
            reason="current benchmark example is blocked",
            risk="none",
            recommended_injection="none",
        ),
    )
    return store.to_snapshot()


@pytest.mark.parametrize(
    ("context_changes", "expected_message"),
    [
        ({"eval_suite": None}, "input_hash requires eval_suite"),
        ({"eval_suite": "other-suite"}, "context eval_suite does not match trace"),
        ({"input_hash": "sha256:other-example"}, "context input_hash does not match trace"),
    ],
    ids=["missing-pair", "eval-suite-mismatch", "input-hash-mismatch"],
)
def test_imported_usage_log_validates_input_hash_audit_identity(
    context_changes: dict[str, str | None], expected_message: str
):
    snapshot = benchmark_input_hash_audit_snapshot()
    usage_log = _snapshot_record(snapshot, "usage_logs")
    context = usage_log["context"]
    assert isinstance(context, dict)
    for field_name, value in context_changes.items():
        if value is None:
            context.pop(field_name)
        else:
            context[field_name] = value

    with pytest.raises(ValueError, match=expected_message):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_imported_usage_log_without_input_hash_audit_remains_compatible():
    snapshot = benchmark_input_hash_audit_snapshot()
    usage_log = _snapshot_record(snapshot, "usage_logs")
    context = usage_log["context"]
    assert isinstance(context, dict)
    context.pop("input_hash")

    restored = TraceBackedMemoryStore.from_snapshot(snapshot)

    assert "input_hash" not in restored.usage_logs[0].context
    assert restored.usage_logs[0].context["eval_suite"] == "benchmark-suite"


@pytest.mark.parametrize(
    "reserved_key",
    ["source_eval_suite", "source_input_hash"],
)
def test_imported_usage_log_rejects_persisted_memory_source_identity(
    reserved_key: str,
):
    snapshot = benchmark_input_hash_audit_snapshot()
    usage_log = _snapshot_record(snapshot, "usage_logs")
    context = usage_log["context"]
    assert isinstance(context, dict)
    context[reserved_key] = "forbidden-source-identity"

    with pytest.raises(
        ValueError,
        match="usage log context must not persist memory source identity",
    ):
        TraceBackedMemoryStore.from_snapshot(snapshot)


DECLARED_PROVENANCE_MISMATCHES = [
    ("branch", {"branch": "other"}, "trace branch does not match memory context"),
    (
        "prompt-version",
        {"prompt_version": "planner_other"},
        "trace prompt_version does not match memory context",
    ),
    (
        "prompt-family",
        {"prompt_family": "other"},
        "trace prompt_family does not match memory context",
    ),
    (
        "tool-schema-version",
        {"tool_schema_version": "search_docs_other"},
        "trace tool_schema_version does not match memory context",
    ),
    ("model", {"model": "other"}, "trace model does not match memory context"),
    (
        "eval-suite",
        {"eval_suite": "other"},
        "trace eval_suite does not match memory context",
    ),
    (
        "tool",
        {"tool_calls": [{"name": "other"}]},
        "trace tool does not match memory context",
    ),
    (
        "non-string-tool",
        {"tool_calls": [{"name": 7}]},
        "trace tool does not match memory context",
    ),
]


@pytest.mark.parametrize(
    ("case_id", "trace_changes", "expected_message"),
    DECLARED_PROVENANCE_MISMATCHES,
    ids=[entry[0] for entry in DECLARED_PROVENANCE_MISMATCHES],
)
def test_finalize_binds_every_declared_trace_provenance_field_before_consumption(
    case_id: str,
    trace_changes: dict[str, object],
    expected_message: str,
):
    store, current_trace, lesson, context = store_with_declared_trace_provenance()
    mismatched_trace = store.record_trace(
        replace(
            current_trace,
            trace_id=f"trace_provenance_mismatch_{case_id}",
            run_id=f"run_provenance_mismatch_{case_id}",
            **trace_changes,
        )
    )
    request = store.prepare_memory(context, task="answer with verified memory")

    with pytest.raises(ValueError, match=expected_message):
        store.finalize_memory(
            request,
            allow_decision(lesson.lesson_id),
            trace_id=mismatched_trace.trace_id,
        )

    assert store.usage_logs == []
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current_trace.trace_id,
    )
    assert result.allowed_memory_ids == (lesson.lesson_id,)
    assert len(store.usage_logs) == 1


def test_finalize_allows_richer_trace_when_optional_context_provenance_is_omitted():
    store, current_trace, lesson, _context = store_with_declared_trace_provenance()
    broad_context = MemoryContext(
        mode="production",
        repo=current_trace.repo or "repo",
        tenant=current_trace.tenant,
        commit_sha=current_trace.commit_sha,
    )
    request = store.prepare_memory(
        broad_context,
        task="answer with broad trace provenance",
    )

    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current_trace.trace_id,
    )

    assert result.allowed_memory_ids == (lesson.lesson_id,)


@pytest.mark.parametrize(
    ("case_id", "trace_changes", "expected_message"),
    [DECLARED_PROVENANCE_MISMATCHES[1], DECLARED_PROVENANCE_MISMATCHES[6]],
    ids=["prompt-version", "tool"],
)
def test_low_level_logging_binds_declared_provenance_before_append(
    case_id: str,
    trace_changes: dict[str, object],
    expected_message: str,
):
    store, current_trace, lesson, context = store_with_declared_trace_provenance()
    mismatched_trace = store.record_trace(
        replace(
            current_trace,
            trace_id=f"trace_log_mismatch_{case_id}",
            run_id=f"run_log_mismatch_{case_id}",
            **trace_changes,
        )
    )
    decision = MemoryDecision(
        use_memory=True,
        allowed_memory_ids=[lesson.lesson_id],
        blocked_memory_ids=[],
        reason="directly relevant",
        risk="low",
        recommended_injection="short_summary",
    )

    with pytest.raises(ValueError, match=expected_message):
        store.log_decision(
            mismatched_trace.run_id,
            context,
            [lesson.lesson_id],
            decision,
        )

    assert store.usage_logs == []
    store.log_decision(
        current_trace.run_id,
        context,
        [lesson.lesson_id],
        decision,
    )
    assert len(store.usage_logs) == 1


def snapshot_with_declared_trace_provenance_log() -> dict[str, object]:
    store, current_trace, lesson, context = store_with_declared_trace_provenance()
    store.log_decision(
        current_trace.run_id,
        context,
        [lesson.lesson_id],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=[lesson.lesson_id],
            blocked_memory_ids=[],
            reason="directly relevant",
            risk="low",
            recommended_injection="short_summary",
        ),
        eval_result="pass",
    )
    return store.to_snapshot()


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("branch", "other"),
        ("prompt_version", "planner_other"),
        ("prompt_family", "other"),
        ("tool_schema_version", "search_docs_other"),
        ("model", "other"),
        ("eval_suite", "other"),
        ("tool", "other"),
    ],
)
def test_imported_usage_log_binds_present_declared_trace_provenance(
    field_name: str,
    replacement: str,
):
    snapshot = snapshot_with_declared_trace_provenance_log()
    log = _snapshot_record(snapshot, "usage_logs")
    context = log["context"]
    assert isinstance(context, dict)
    context[field_name] = replacement

    with pytest.raises(
        ValueError,
        match=rf"context {field_name} does not match trace",
    ):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_imported_usage_log_allows_omitted_optional_trace_provenance():
    snapshot = snapshot_with_declared_trace_provenance_log()
    log = _snapshot_record(snapshot, "usage_logs")
    context = log["context"]
    assert isinstance(context, dict)
    for field_name in (
        "branch",
        "prompt_version",
        "prompt_family",
        "tool",
        "tool_schema_version",
        "model",
        "eval_suite",
    ):
        context.pop(field_name)

    restored = TraceBackedMemoryStore.from_snapshot(snapshot)

    assert restored.usage_logs[0].context == context


def test_legacy_usage_log_rejects_mismatched_supplied_trace_provenance():
    legacy = snapshot_with_declared_trace_provenance_log()
    legacy.pop("snapshot_version")
    log = _snapshot_record(legacy, "usage_logs")
    context = log["context"]
    assert isinstance(context, dict)
    context["model"] = "other"

    with pytest.raises(ValueError, match="context model does not match trace"):
        TraceBackedMemoryStore.from_snapshot(legacy)


@pytest.mark.parametrize(
    ("initial_result", "sealed_result"),
    [(None, "pass"), ("unknown", "fail"), (None, "error")],
    ids=["missing-to-pass", "unknown-to-fail", "missing-to-error"],
)
def test_record_decision_outcome_seals_unevaluated_log_and_round_trips(
    initial_result: str | None,
    sealed_result: str,
):
    store, log, _lesson = store_with_usage_decision(eval_result=initial_result)

    sealed = store.record_decision_outcome(log.decision_id, sealed_result)

    assert sealed.eval_result == sealed_result
    assert sealed.memory_caused_failure is False
    assert store.usage_logs[0] == sealed
    restored = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    assert restored.usage_logs[0] == sealed

    sealed.context["repo"] = "mutated-return-value"
    sealed.candidate_memory_ids.clear()
    assert store.usage_logs[0].context["repo"] == "repo"
    assert store.usage_logs[0].candidate_memory_ids == ["lesson_001"]


def test_record_decision_outcome_moves_global_and_per_memory_metrics():
    store, log, lesson = store_with_usage_decision()

    before = store.metrics()
    before_memory = {
        item.memory_id: item for item in store.memory_outcome_metrics()
    }[lesson.lesson_id]
    assert before.unevaluated_decision_count == 1
    assert before.evaluated_with_memory_count == 0
    assert before.pass_rate_with_memory is None
    assert before_memory.unevaluated_use_count == 1
    assert before_memory.evaluated_use_count == 0

    store.record_decision_outcome(log.decision_id, "pass")

    after = store.metrics()
    after_memory = {
        item.memory_id: item for item in store.memory_outcome_metrics()
    }[lesson.lesson_id]
    assert after.unevaluated_decision_count == 0
    assert after.evaluated_with_memory_count == 1
    assert after.pass_rate_with_memory == 1.0
    assert after_memory.unevaluated_use_count == 0
    assert after_memory.evaluated_use_count == 1
    assert after_memory.passed_use_count == 1
    assert after_memory.observed_pass_rate == 1.0


def test_finalize_then_record_decision_outcome_uses_returned_decision_id():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
    )

    assert store.usage_logs[0].eval_result is None
    sealed = store.record_decision_outcome(result.decision_id, "pass")

    assert sealed.decision_id == result.decision_id
    assert sealed.eval_result == "pass"
    assert store.usage_logs[0] == sealed


def test_record_decision_outcome_exact_replay_is_idempotent_and_conflicts_fail():
    store, log, _lesson = store_with_usage_decision()
    sealed = store.record_decision_outcome(
        log.decision_id,
        "fail",
        memory_caused_failure=True,
    )
    after_seal = store.to_snapshot()

    replayed = store.record_decision_outcome(
        log.decision_id,
        "fail",
        memory_caused_failure=True,
    )

    assert replayed == sealed
    assert store.to_snapshot() == after_seal
    for eval_result, caused_failure in (("error", True), ("fail", False)):
        with pytest.raises(ValueError, match="decision outcome already sealed"):
            store.record_decision_outcome(
                log.decision_id,
                eval_result,
                memory_caused_failure=caused_failure,
            )
        assert store.to_snapshot() == after_seal


@pytest.mark.parametrize("invalid_result", [None, "unknown", "pending", True])
def test_record_decision_outcome_requires_measured_result_without_mutation(
    invalid_result: object,
):
    store, log, _lesson = store_with_usage_decision()
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="measured eval_result"):
        store.record_decision_outcome(log.decision_id, invalid_result)

    assert store.to_snapshot() == before


def test_record_decision_outcome_rejects_invalid_failure_attribution_atomically():
    store, log, _lesson = store_with_usage_decision()
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="memory_caused_failure requires eval_result fail or error"):
        store.record_decision_outcome(
            log.decision_id,
            "pass",
            memory_caused_failure=True,
        )
    with pytest.raises(ValueError, match="memory_caused_failure must be a boolean"):
        store.record_decision_outcome(
            log.decision_id,
            "fail",
            memory_caused_failure=1,
        )
    assert store.to_snapshot() == before

    no_use_store, no_use_log, _lesson = store_with_usage_decision(use_memory=False)
    no_use_before = no_use_store.to_snapshot()
    with pytest.raises(ValueError, match="requires failed or errored memory use"):
        no_use_store.record_decision_outcome(
            no_use_log.decision_id,
            "fail",
            memory_caused_failure=True,
        )
    assert no_use_store.to_snapshot() == no_use_before


def test_record_decision_outcome_rejects_unknown_or_invalid_decision_id():
    store, _log, _lesson = store_with_usage_decision()
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="unknown decision_id: decision_missing"):
        store.record_decision_outcome("decision_missing", "pass")
    with pytest.raises(ValueError, match="decision outcome requires decision_id"):
        store.record_decision_outcome("", "pass")

    assert store.to_snapshot() == before


def test_concurrent_conflicting_decision_outcome_seals_exactly_once():
    store, log, _lesson = store_with_usage_decision()
    start = threading.Barrier(3)
    outcomes: list[object] = []

    def seal(eval_result: str) -> None:
        start.wait()
        try:
            outcomes.append(
                store.record_decision_outcome(log.decision_id, eval_result)
            )
        except ValueError as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=seal, args=(eval_result,))
        for eval_result in ("pass", "fail")
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    successes = [
        outcome for outcome in outcomes if isinstance(outcome, MemoryUsageLog)
    ]
    failures = [outcome for outcome in outcomes if isinstance(outcome, ValueError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "decision outcome already sealed" in str(failures[0])
    assert store.usage_logs[0].eval_result == successes[0].eval_result


def test_prepare_and_finalize_memory_is_trace_linked_and_audited():
    store, trace, _case, lesson = store_with_active_lesson()
    context = MemoryContext(
        mode="repair",
        repo=trace.repo,
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
    )
    request = store.prepare_memory(context, task="repair failed call")
    result = store.finalize_memory(
        request,
        {
            "use_memory": True,
            "allowed_memory_ids": [lesson.lesson_id],
            "blocked_memory_ids": [],
            "reason": "direct match",
            "risk": "low",
            "recommended_injection": "short_summary",
        },
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert result.allowed_memory_ids == (lesson.lesson_id,)
    assert "Relevant verified memory" in result.snippet
    log = store.usage_logs[-1]
    assert log.trace_id == trace.trace_id
    assert log.context["tenant"] == trace.tenant
    assert log.candidate_memory_statuses == {lesson.lesson_id: "active"}
    assert log.created_at.endswith("Z")


def test_prepare_memory_keeps_semantic_rank_and_system_gate_audit_boundaries():
    store, trace, _case, safe_lesson = store_with_active_lesson()
    sensitive_lesson = replace(
        safe_lesson,
        lesson_id="lesson_sensitive",
        lesson_text="Sensitive guidance must remain blocked.",
        sensitive=True,
    )
    obsolete_lesson = replace(
        safe_lesson,
        lesson_id="lesson_obsolete",
        lesson_text="Obsolete guidance must remain blocked.",
        status="obsolete",
    )
    store.add_lesson(sensitive_lesson)
    store.add_lesson(obsolete_lesson)
    scores = {
        sensitive_lesson.lesson_id: 1.0,
        obsolete_lesson.lesson_id: 0.9,
        safe_lesson.lesson_id: 0.8,
    }

    request = store.prepare_memory(
        matching_context(trace),
        task="repair failed call",
        semantic_scores=scores,
        max_candidates=3,
    )
    scores.clear()

    assert request.candidate_memory_ids == (
        sensitive_lesson.lesson_id,
        obsolete_lesson.lesson_id,
        safe_lesson.lesson_id,
    )
    assert request.system_allowed_memory_ids == (safe_lesson.lesson_id,)
    assert dict(request.system_blocked) == {
        sensitive_lesson.lesson_id: "memory is marked sensitive",
        obsolete_lesson.lesson_id: "status 'obsolete' is not allowed",
    }
    assert sensitive_lesson.lesson_id not in request.prompt
    assert obsolete_lesson.lesson_id not in request.prompt

    result = store.finalize_memory(
        request,
        allow_decision(safe_lesson.lesson_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert result.allowed_memory_ids == (safe_lesson.lesson_id,)
    log = store.usage_logs[-1]
    assert log.candidate_memory_ids == [
        sensitive_lesson.lesson_id,
        obsolete_lesson.lesson_id,
        safe_lesson.lesson_id,
    ]
    assert log.candidate_memory_statuses == {
        sensitive_lesson.lesson_id: "active",
        obsolete_lesson.lesson_id: "obsolete",
        safe_lesson.lesson_id: "active",
    }
    assert log.system_blocked_reasons == dict(request.system_blocked)


def test_semantic_scores_are_not_persisted_by_prepare_or_finalize():
    store, trace, _case, lesson = store_with_active_lesson()
    before_prepare = store.to_snapshot()
    raw_score = 0.3141592653589793

    request = store.prepare_memory(
        matching_context(trace),
        task="repair failed call",
        semantic_scores={lesson.lesson_id: raw_score},
        max_candidates=1,
        minimum_score=0.25,
    )

    assert store.to_snapshot() == before_prepare

    store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )
    snapshot = store.to_snapshot()
    usage_log = snapshot["usage_logs"][-1]
    assert usage_log["candidate_memory_ids"] == [lesson.lesson_id]
    assert "semantic_scores" not in usage_log
    assert "max_candidates" not in usage_log
    assert "minimum_score" not in usage_log
    assert str(raw_score) not in json.dumps(snapshot, sort_keys=True)


def test_invalid_semantic_prepare_does_not_consume_request_id():
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc")

    with pytest.raises(
        ValueError,
        match="max_candidates must be an integer from 1 through 50",
    ):
        store.prepare_memory(
            context,
            task="repair",
            semantic_scores={},
            max_candidates=0,
        )

    request = store.prepare_memory(context, task="repair")
    assert request.request_id == "gate_request_000001"


def test_prepare_memory_validates_context_before_empty_candidate_registration():
    store = TraceBackedMemoryStore()
    invalid_context = MemoryContext(
        mode="repair", repo="", commit_sha="abc"
    )

    with pytest.raises(ValueError, match="context repo"):
        store.prepare_memory(invalid_context, task="repair")

    request = store.prepare_memory(
        MemoryContext(mode="repair", repo="repo", commit_sha="abc"),
        task="repair",
    )
    assert request.request_id == "gate_request_000001"


def test_gate_request_cannot_cross_stores_or_be_replayed():
    first, trace, _case, lesson = store_with_active_lesson()
    second = TraceBackedMemoryStore.from_snapshot(first.to_snapshot())
    context = matching_context(trace)
    request = first.prepare_memory(context, task="repair")
    payload = allow_decision(lesson.lesson_id)
    with pytest.raises(ValueError, match="does not belong"):
        second.finalize_memory(request, payload, trace_id=trace.trace_id)
    first.finalize_memory(request, payload, trace_id=trace.trace_id)
    with pytest.raises(ValueError, match="already finalized"):
        first.finalize_memory(request, payload, trace_id=trace.trace_id)


def test_finalize_rechecks_memory_obsoleted_after_prepare():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    store.obsolete_lesson(lesson.lesson_id)
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id
    )
    assert result.use_memory is False
    assert result.snippet == ""
    assert lesson.lesson_id in result.blocked_memory_ids


def test_finalize_rechecks_semantically_selected_memory_obsoleted_after_prepare():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(
        matching_context(trace),
        task="repair",
        semantic_scores={lesson.lesson_id: 1.0},
        max_candidates=1,
    )
    store.obsolete_lesson(lesson.lesson_id)

    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
    )

    assert result.use_memory is False
    assert result.snippet == ""
    assert lesson.lesson_id in result.blocked_memory_ids


def test_failed_finalize_does_not_consume_request_or_append_log():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    before = store.usage_logs
    with pytest.raises(ValueError, match="unknown trace_id"):
        store.finalize_memory(request, allow_decision(lesson.lesson_id), trace_id="wrong")
    assert store.usage_logs == before
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id
    )
    assert result.use_memory is True


def test_concurrent_finalization_consumes_request_exactly_once(monkeypatch):
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    payload = allow_decision(lesson.lesson_id)
    original_parser = store_module.parse_memory_decision

    def sleeping_parser(decision_payload):
        time.sleep(0.05)
        return original_parser(decision_payload)

    monkeypatch.setattr(store_module, "parse_memory_decision", sleeping_parser)
    start = threading.Barrier(3)
    outcomes: list[object] = []

    def finalize() -> None:
        start.wait()
        try:
            outcomes.append(
                store.finalize_memory(
                    request, payload, trace_id=trace.trace_id
                )
            )
        except ValueError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=finalize) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    successes = [outcome for outcome in outcomes if not isinstance(outcome, ValueError)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, ValueError)]
    assert len(successes) == 1
    assert [str(failure) for failure in failures] == [
        f"gate request already finalized: {request.request_id}"
    ]
    assert len(store.usage_logs) == 1
    with pytest.raises(ValueError, match="already finalized"):
        store.finalize_memory(request, payload, trace_id=trace.trace_id)


def test_decision_validation_failure_leaves_gate_request_pending():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")

    with pytest.raises(ValueError, match="memory decision"):
        store.finalize_memory(request, {}, trace_id=trace.trace_id)

    assert store.usage_logs == []
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id
    )
    assert result.use_memory is True


def test_obsolete_attempt_metric_does_not_reclassify_old_decisions():
    store, trace, _case, lesson = store_with_active_lesson()
    request = store.prepare_memory(matching_context(trace), task="repair")
    store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id
    )
    assert store.metrics().obsolete_memory_usage_attempts == 0
    store.obsolete_lesson(lesson.lesson_id)
    assert store.metrics().obsolete_memory_usage_attempts == 0


def store_with_cascade_lessons() -> tuple[
    TraceBackedMemoryStore, FailureCase, tuple[Lesson, Lesson], Lesson
]:
    store, _trace, case, first_lesson = store_with_active_lesson()
    second_lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_002",
        lesson_text="Validate the search query before calling search_docs.",
        memory_type="procedural",
        scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
    )
    store.add_lesson(second_lesson)

    unrelated_trace = store.record_trace(
        Trace(
            trace_id="trace_unrelated",
            run_id="run_unrelated",
            commit_sha="unrelated",
            repo="repo",
            tenant="tenant_a",
            eval_result="fail",
        )
    )
    unrelated_case = verify_failure_case(
        draft_failure_case(
            unrelated_trace,
            case_id="case_unrelated",
            failure_type="other_failure",
            symptom="unrelated failure",
        ),
        fix="unrelated fix",
        fix_commit_sha="unrelated_fix",
        regression_passed=True,
    )
    store.add_failure_case(unrelated_case)
    unrelated_lesson = lesson_from_failure_case(
        unrelated_case,
        lesson_id="lesson_unrelated",
        lesson_text="Unrelated active guidance.",
        memory_type="semantic",
        scope={"repo": "repo", "tenant": "tenant_a", "tool": "other_tool"},
    )
    store.add_lesson(unrelated_lesson)
    return store, case, (first_lesson, second_lesson), unrelated_lesson


def test_store_can_review_and_verify_a_persisted_draft():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc",
            repo="repo",
            tenant="tenant_a",
            eval_result="fail",
        )
    )
    store.add_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="bad_tool",
            symptom="bad call",
        )
    )

    reviewed = store.review_failure_case(
        "case_001", reviewed_by="reviewer", root_cause="missing contract"
    )
    verified = store.verify_failure_case(
        "case_001",
        fix="add contract",
        fix_commit_sha="def",
        regression_passed=True,
    )

    assert reviewed.reviewed_by == "reviewer"
    assert verified.status == "verified"
    assert store.failure_cases["case_001"] == verified


@pytest.mark.parametrize(
    ("operation", "record_type", "missing_id"),
    [
        ("review_failure_case", "failure case", "missing_case"),
        ("verify_failure_case", "failure case", "missing_case"),
        ("obsolete_failure_case", "failure case", "missing_case"),
        ("obsolete_lesson", "lesson", "missing_lesson"),
        ("obsolete_project_policy", "project policy", "missing_policy"),
    ],
)
def test_store_lifecycle_unknown_ids_raise_stable_value_errors(
    operation: str, record_type: str, missing_id: str
):
    store = TraceBackedMemoryStore()
    if operation == "review_failure_case":
        call = lambda: store.review_failure_case(
            missing_id, reviewed_by="reviewer", root_cause="cause"
        )
    elif operation == "verify_failure_case":
        call = lambda: store.verify_failure_case(
            missing_id,
            fix="fix",
            fix_commit_sha="def",
            regression_passed=True,
        )
    else:
        call = lambda: getattr(store, operation)(missing_id)

    with pytest.raises(
        ValueError, match=rf"unknown {record_type} ID: {missing_id}"
    ):
        call()


@pytest.mark.parametrize("invalid_boolean", ["true", 1])
def test_store_verify_rejects_non_boolean_regression_result(invalid_boolean: object):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc",
            eval_result="fail",
        )
    )
    store.add_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="bad_tool",
            symptom="bad call",
        )
    )

    with pytest.raises(ValueError, match="passing regression"):
        store.verify_failure_case(
            "case_001",
            fix="add contract",
            fix_commit_sha="def",
            regression_passed=invalid_boolean,  # type: ignore[arg-type]
        )


def test_obsoleting_source_case_cascades_to_active_lessons():
    store, _trace, case, lesson = store_with_active_lesson()

    obsolete = store.obsolete_failure_case(case.case_id)

    assert obsolete.status == "obsolete"
    assert store.lessons[lesson.lesson_id].status == "obsolete"


def test_obsolete_failure_case_cascade_updates_all_dependents_only():
    store, case, dependent_lessons, unrelated_lesson = store_with_cascade_lessons()

    store.obsolete_failure_case(case.case_id)

    assert store.failure_cases[case.case_id].status == "obsolete"
    assert [store.lessons[lesson.lesson_id].status for lesson in dependent_lessons] == [
        "obsolete",
        "obsolete",
    ]
    assert store.lessons[unrelated_lesson.lesson_id].status == "active"


def test_obsolete_failure_case_cascade_is_atomic_on_second_lesson_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    store, case, dependent_lessons, _unrelated_lesson = store_with_cascade_lessons()
    before_snapshot = store.to_snapshot()
    before_bytes = json.dumps(before_snapshot, sort_keys=True).encode("utf-8")
    before_cases = store.failure_cases
    before_lessons = store.lessons
    original_validator = store_module._validate_lesson_record
    validated_lesson_ids: list[str] = []

    def fail_for_second_dependent(lesson: Lesson) -> None:
        validated_lesson_ids.append(lesson.lesson_id)
        if lesson.lesson_id == dependent_lessons[1].lesson_id:
            raise ValueError("injected second dependent validation failure")
        original_validator(lesson)

    monkeypatch.setattr(store_module, "_validate_lesson_record", fail_for_second_dependent)

    with pytest.raises(ValueError, match="second dependent validation failure"):
        store.obsolete_failure_case(case.case_id)

    assert validated_lesson_ids == [lesson.lesson_id for lesson in dependent_lessons]
    assert store.failure_cases == before_cases
    assert store.lessons == before_lessons
    assert json.dumps(store.to_snapshot(), sort_keys=True).encode("utf-8") == before_bytes


def test_obsolete_case_cascade_round_trips_through_snapshot():
    store, _trace, case, lesson = store_with_active_lesson()

    store.obsolete_failure_case(case.case_id)
    loaded = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())

    assert loaded.failure_cases[case.case_id].status == "obsolete"
    assert loaded.lessons[lesson.lesson_id].status == "obsolete"


def test_recorded_trace_is_isolated_from_caller_nested_mutation():
    calls = [{"name": "search_docs", "arguments": {"query": "before"}}]
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc",
        repo="repo",
        eval_result="fail",
        tool_calls=calls,
    )
    store = TraceBackedMemoryStore()
    store.record_trace(trace)

    calls[0]["arguments"]["query"] = "after"

    assert store.traces["trace_001"].tool_calls[0]["arguments"]["query"] == "before"


def test_record_trace_validates_caller_and_copied_trace_before_insertion(
    monkeypatch: pytest.MonkeyPatch,
):
    trace = Trace(
        trace_id="trace_double_validation",
        run_id="run_double_validation",
        commit_sha="abc",
        tool_calls=[{"name": "search", "arguments": {"query": "before"}}],
    )
    validated: list[Trace] = []
    real_validate = store_module._validate_trace

    def tracking_validate(value: Trace) -> None:
        validated.append(value)
        real_validate(value)

    monkeypatch.setattr(store_module, "_validate_trace", tracking_validate)
    store = TraceBackedMemoryStore()
    stored = store.record_trace(trace)

    assert len(validated) == 2
    assert validated[0] is trace
    assert validated[1] is not trace
    trace.tool_calls[0]["arguments"]["query"] = "after"
    assert stored.tool_calls[0]["arguments"]["query"] == "before"
    assert store.traces[trace.trace_id].tool_calls[0]["arguments"]["query"] == "before"


def test_record_trace_rejects_coordinated_mutation_between_validation_and_copy(
    monkeypatch: pytest.MonkeyPatch,
):
    trace = Trace(
        trace_id="trace_copy_race",
        run_id="run_copy_race",
        commit_sha="abc",
        tool_calls=[{"name": "search", "arguments": {"query": "before"}}],
    )
    copy_started = threading.Event()
    mutation_finished = threading.Event()
    errors: list[BaseException] = []
    real_deepcopy = store_module.deepcopy

    def coordinated_deepcopy(value, *args, **kwargs):
        if value is trace:
            copy_started.set()
            if not mutation_finished.wait(timeout=2):
                raise AssertionError("coordinated mutation did not finish")
        return real_deepcopy(value, *args, **kwargs)

    monkeypatch.setattr(store_module, "deepcopy", coordinated_deepcopy)
    store = TraceBackedMemoryStore()

    def record() -> None:
        try:
            store.record_trace(trace)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=record)
    worker.start()
    assert copy_started.wait(timeout=2)
    arguments = trace.tool_calls[0]["arguments"]
    assert isinstance(arguments, dict)
    arguments["query"] = {"not-json"}
    mutation_finished.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "tool_calls[0].arguments.query" in str(errors[0])
    assert store.traces == {}


def test_record_trace_normalizes_expected_copy_mutation_error(
    monkeypatch: pytest.MonkeyPatch,
):
    trace = Trace("trace_copy_error", "run_copy_error", "abc")

    def fail_copy(_value, *_args, **_kwargs):
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(store_module, "deepcopy", fail_copy)

    with pytest.raises(ValueError, match="trace changed while being copied"):
        TraceBackedMemoryStore().record_trace(trace)


def test_record_trace_normalizes_copy_recursion_from_concurrent_depth_change(
    monkeypatch: pytest.MonkeyPatch,
):
    trace = Trace("trace_copy_depth", "run_copy_depth", "abc")

    def fail_copy(_value, *_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(store_module, "deepcopy", fail_copy)

    with pytest.raises(ValueError, match="trace changed while being copied"):
        TraceBackedMemoryStore().record_trace(trace)


def test_record_trace_does_not_swallow_unrelated_copy_programming_error(
    monkeypatch: pytest.MonkeyPatch,
):
    trace = Trace("trace_copy_bug", "run_copy_bug", "abc")

    def fail_copy(_value, *_args, **_kwargs):
        raise RuntimeError("copy implementation bug")

    monkeypatch.setattr(store_module, "deepcopy", fail_copy)

    with pytest.raises(RuntimeError, match="copy implementation bug"):
        TraceBackedMemoryStore().record_trace(trace)


def test_complete_trace_fills_execution_evidence_and_round_trips():
    store = TraceBackedMemoryStore()
    pending = store.record_trace(pending_execution_trace())
    tool_outputs = [{"documents": 3, "cached": False}]

    completed = store.complete_trace(
        pending.trace_id,
        eval_result="pass",
        output_hash="sha256:completed-output",
        tool_outputs=tool_outputs,
        latency_ms=125,
        cost_usd=0.0025,
        error=None,
        trace_uri="trace://runs/pending-execution",
    )

    assert completed.eval_result == "pass"
    assert completed.output_hash == "sha256:completed-output"
    assert completed.tool_outputs == [{"documents": 3, "cached": False}]
    assert completed.latency_ms == 125
    assert completed.cost_usd == 0.0025
    assert completed.error is None
    assert completed.trace_uri == "trace://runs/pending-execution"
    for field_name in (
        "trace_id",
        "run_id",
        "commit_sha",
        "repo",
        "tenant",
        "branch",
        "prompt_version",
        "prompt_family",
        "tool_schema_version",
        "model",
        "eval_suite",
        "input_hash",
        "retrieved_context",
        "tool_calls",
        "created_at",
    ):
        assert getattr(completed, field_name) == getattr(pending, field_name)

    restored = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    assert restored.traces[pending.trace_id] == completed
    tool_outputs[0]["documents"] = 98
    assert store.traces[pending.trace_id].tool_outputs[0]["documents"] == 3
    completed.tool_outputs[0]["documents"] = 99
    assert store.traces[pending.trace_id].tool_outputs[0]["documents"] == 3


def test_complete_trace_preserves_omitted_and_equal_prefilled_evidence():
    store = TraceBackedMemoryStore()
    pending = store.record_trace(
        pending_execution_trace(
            output_hash="sha256:prefilled",
            tool_outputs=[{"status": "prefilled"}],
            latency_ms=80,
            cost_usd=0.03,
            error="prefilled error",
            trace_uri="trace://prefilled",
        )
    )

    completed = store.complete_trace(
        pending.trace_id,
        eval_result="error",
        output_hash="sha256:prefilled",
    )

    assert completed.output_hash == "sha256:prefilled"
    assert completed.tool_outputs == [{"status": "prefilled"}]
    assert completed.trace_uri == "trace://prefilled"
    assert completed.latency_ms == 80
    assert completed.cost_usd == 0.03
    assert completed.error == "prefilled error"


TRACE_COMPLETION_REWRITES = [
    ("output_hash", {"output_hash": "sha256:other"}),
    ("tool_outputs", {"tool_outputs": [{"status": "other"}]}),
    ("latency_ms", {"latency_ms": 999}),
    ("cost_usd", {"cost_usd": 9.99}),
    ("error", {"error": "other error"}),
    ("trace_uri", {"trace_uri": "trace://other"}),
]


@pytest.mark.parametrize(
    ("field_name", "completion_changes"),
    TRACE_COMPLETION_REWRITES,
    ids=[entry[0] for entry in TRACE_COMPLETION_REWRITES],
)
def test_complete_trace_rejects_rewriting_prefilled_execution_evidence(
    field_name: str,
    completion_changes: dict[str, object],
):
    store = TraceBackedMemoryStore()
    pending = store.record_trace(
        pending_execution_trace(
            output_hash="sha256:prefilled",
            tool_outputs=[{"status": "prefilled"}],
            latency_ms=10,
            cost_usd=0.01,
            error="prefilled error",
            trace_uri="trace://prefilled",
        )
    )
    before = store.to_snapshot()

    with pytest.raises(
        ValueError,
        match=rf"trace completion cannot rewrite {field_name}",
    ):
        store.complete_trace(
            pending.trace_id,
            eval_result="error",
            **completion_changes,
        )

    assert store.to_snapshot() == before


def test_complete_trace_exact_replay_is_idempotent_and_sealed_conflicts_fail():
    store = TraceBackedMemoryStore()
    pending = store.record_trace(pending_execution_trace())
    completed = store.complete_trace(
        pending.trace_id,
        eval_result="pass",
        output_hash="sha256:completed",
        tool_outputs=[{"status": "ok"}],
        latency_ms=25,
    )
    after_completion = store.to_snapshot()

    replayed = store.complete_trace(
        pending.trace_id,
        eval_result="pass",
        output_hash="sha256:completed",
        tool_outputs=[{"status": "ok"}],
        latency_ms=25,
    )

    assert replayed == completed
    assert store.to_snapshot() == after_completion
    with pytest.raises(ValueError, match="trace execution already completed"):
        store.complete_trace(pending.trace_id, eval_result="fail")
    with pytest.raises(ValueError, match="trace execution already completed"):
        store.complete_trace(
            pending.trace_id,
            eval_result="pass",
            output_hash="sha256:other",
        )
    assert store.to_snapshot() == after_completion


def test_complete_trace_accepts_only_exact_replay_for_directly_measured_trace():
    store = TraceBackedMemoryStore()
    measured = store.record_trace(
        pending_execution_trace(
            eval_result="fail",
            error="evaluation failed",
            latency_ms=50,
        )
    )

    assert store.complete_trace(measured.trace_id, eval_result="fail") == measured
    with pytest.raises(ValueError, match="trace execution already completed"):
        store.complete_trace(measured.trace_id, eval_result="error")


@pytest.mark.parametrize("invalid_result", [None, "unknown", "pending", True])
def test_complete_trace_requires_measured_result_without_mutation(
    invalid_result: object,
):
    store = TraceBackedMemoryStore()
    pending = store.record_trace(pending_execution_trace())
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="trace completion requires measured eval_result"):
        store.complete_trace(pending.trace_id, eval_result=invalid_result)

    assert store.to_snapshot() == before


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("output_hash", ""),
        ("tool_outputs", None),
        ("tool_outputs", [1]),
        ("latency_ms", True),
        ("cost_usd", float("inf")),
        ("error", 7),
        ("trace_uri", ""),
    ],
)
def test_complete_trace_reuses_trace_validation_atomically(
    field_name: str,
    invalid_value: object,
):
    store = TraceBackedMemoryStore()
    pending = store.record_trace(pending_execution_trace())
    before = store.to_snapshot()

    with pytest.raises(ValueError, match=field_name):
        store.complete_trace(
            pending.trace_id,
            eval_result="error",
            **{field_name: invalid_value},
        )

    assert store.to_snapshot() == before


def test_complete_trace_rejects_unknown_or_invalid_trace_id():
    store = TraceBackedMemoryStore()
    store.record_trace(pending_execution_trace())
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="unknown trace_id: trace_missing"):
        store.complete_trace("trace_missing", eval_result="pass")
    with pytest.raises(ValueError, match="trace completion requires trace_id"):
        store.complete_trace("", eval_result="pass")

    assert store.to_snapshot() == before


def test_concurrent_conflicting_trace_completion_succeeds_exactly_once():
    store = TraceBackedMemoryStore()
    pending = store.record_trace(pending_execution_trace())
    start = threading.Barrier(3)
    outcomes: list[object] = []

    def complete(eval_result: str) -> None:
        start.wait()
        try:
            outcomes.append(
                store.complete_trace(pending.trace_id, eval_result=eval_result)
            )
        except ValueError as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=complete, args=(eval_result,))
        for eval_result in ("pass", "fail")
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    successes = [outcome for outcome in outcomes if isinstance(outcome, Trace)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, ValueError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "trace execution already completed" in str(failures[0])
    assert store.traces[pending.trace_id].eval_result == successes[0].eval_result


def test_trace_and_decision_can_be_completed_after_memory_execution():
    store, source_trace, _case, lesson = store_with_active_lesson()
    current = store.record_trace(
        replace(
            source_trace,
            trace_id="trace_current_execution",
            run_id="run_current_execution",
            eval_result="unknown",
        )
    )
    request = store.prepare_memory(matching_context(current), task="repair")
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current.trace_id,
    )

    completed_trace = store.complete_trace(
        current.trace_id,
        eval_result="pass",
        output_hash="sha256:current-output",
        tool_outputs=[{"status": "ok"}],
        latency_ms=20,
    )
    assert store.usage_logs[0].eval_result is None
    completed_decision = store.record_decision_outcome(
        result.decision_id,
        "pass",
    )

    assert store.traces[source_trace.trace_id].eval_result == "fail"
    assert completed_trace.eval_result == "pass"
    assert completed_decision.eval_result == "pass"
    assert store.traces[current.trace_id].eval_result == "pass"
    assert store.metrics().pass_rate_with_memory == 1.0


def test_complete_memory_run_atomically_completes_both_records_and_round_trips():
    store, current, result, lesson = store_with_pending_memory_run()

    completion = store.complete_memory_run(
        trace_id=current.trace_id,
        decision_id=result.decision_id,
        eval_result="pass",
        output_hash="sha256:atomic-output",
        tool_outputs=[{"status": "ok"}],
        latency_ms=30,
        cost_usd=0.003,
        trace_uri="trace://atomic-run",
    )

    assert isinstance(completion, tbm.MemoryRunCompletion)
    assert "MemoryRunCompletion" in tbm.__all__
    with pytest.raises(FrozenInstanceError):
        completion.trace = current
    assert completion.trace.eval_result == "pass"
    assert completion.trace.output_hash == "sha256:atomic-output"
    assert completion.usage_log.eval_result == "pass"
    assert completion.usage_log.decision_id == result.decision_id
    assert store.traces[current.trace_id] == completion.trace
    assert store.usage_logs[0] == completion.usage_log
    assert store.metrics().pass_rate_with_memory == 1.0
    assert {
        item.memory_id: item for item in store.memory_outcome_metrics()
    }[lesson.lesson_id].observed_pass_rate == 1.0

    restored = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    assert restored.traces[current.trace_id] == completion.trace
    assert restored.usage_logs[0] == completion.usage_log

    completion.trace.tool_outputs[0]["status"] = "mutated"
    completion.usage_log.context["repo"] = "mutated"
    assert store.traces[current.trace_id].tool_outputs == [{"status": "ok"}]
    assert store.usage_logs[0].context["repo"] == "repo"


def test_complete_memory_run_exact_replay_is_idempotent_and_conflicts_are_atomic():
    store, current, result, _lesson = store_with_pending_memory_run()
    completed = store.complete_memory_run(
        trace_id=current.trace_id,
        decision_id=result.decision_id,
        eval_result="pass",
        output_hash="sha256:atomic-output",
    )
    after_completion = store.to_snapshot()

    replayed = store.complete_memory_run(
        trace_id=current.trace_id,
        decision_id=result.decision_id,
        eval_result="pass",
    )

    assert replayed == completed
    assert store.to_snapshot() == after_completion
    conflicting_calls = [
        {"eval_result": "fail"},
        {"eval_result": "pass", "output_hash": "sha256:other"},
        {"eval_result": "pass", "memory_caused_failure": True},
    ]
    for changes in conflicting_calls:
        with pytest.raises(ValueError):
            store.complete_memory_run(
                trace_id=current.trace_id,
                decision_id=result.decision_id,
                **changes,
            )
        assert store.to_snapshot() == after_completion


def test_complete_memory_run_requires_exact_decision_trace_linkage():
    store, current, result, _lesson = store_with_pending_memory_run()
    other_trace = store.record_trace(
        replace(
            current,
            trace_id="trace_other_memory_run",
            run_id="run_other_memory_run",
        )
    )
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="decision_id .* does not belong to trace_id"):
        store.complete_memory_run(
            trace_id=other_trace.trace_id,
            decision_id=result.decision_id,
            eval_result="pass",
        )
    with pytest.raises(ValueError, match="unknown decision_id: decision_missing"):
        store.complete_memory_run(
            trace_id=current.trace_id,
            decision_id="decision_missing",
            eval_result="pass",
        )

    assert store.to_snapshot() == before


def test_complete_memory_run_trace_candidate_failure_leaves_both_pending():
    store, current, result, _lesson = store_with_pending_memory_run(
        current_trace_changes={"output_hash": "sha256:prefilled"}
    )
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="trace completion cannot rewrite output_hash"):
        store.complete_memory_run(
            trace_id=current.trace_id,
            decision_id=result.decision_id,
            eval_result="pass",
            output_hash="sha256:other",
        )

    assert store.to_snapshot() == before
    assert store.traces[current.trace_id].eval_result == "unknown"
    assert store.usage_logs[0].eval_result is None


def test_complete_memory_run_usage_candidate_failure_leaves_trace_pending():
    store, current, result, _lesson = store_with_pending_memory_run(
        use_memory=False
    )
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="requires failed or errored memory use"):
        store.complete_memory_run(
            trace_id=current.trace_id,
            decision_id=result.decision_id,
            eval_result="fail",
            memory_caused_failure=True,
            output_hash="sha256:would-be-valid",
        )

    assert store.to_snapshot() == before
    assert store.traces[current.trace_id].eval_result == "unknown"
    assert store.usage_logs[0].eval_result is None


def test_complete_memory_run_recovers_either_matching_partial_state():
    trace_first, trace, trace_result, _lesson = store_with_pending_memory_run()
    trace_first.complete_trace(
        trace.trace_id,
        eval_result="pass",
        output_hash="sha256:trace-first",
    )

    recovered_trace_first = trace_first.complete_memory_run(
        trace_id=trace.trace_id,
        decision_id=trace_result.decision_id,
        eval_result="pass",
    )

    assert recovered_trace_first.trace.output_hash == "sha256:trace-first"
    assert recovered_trace_first.usage_log.eval_result == "pass"

    decision_first, trace, decision_result, _lesson = store_with_pending_memory_run()
    decision_first.record_decision_outcome(decision_result.decision_id, "error")

    recovered_decision_first = decision_first.complete_memory_run(
        trace_id=trace.trace_id,
        decision_id=decision_result.decision_id,
        eval_result="error",
        error="executor failed",
    )

    assert recovered_decision_first.trace.eval_result == "error"
    assert recovered_decision_first.trace.error == "executor failed"
    assert recovered_decision_first.usage_log.eval_result == "error"


def test_complete_memory_run_rejects_conflicting_partial_states_atomically():
    store, current, result, _lesson = store_with_pending_memory_run()
    store.complete_trace(current.trace_id, eval_result="pass")
    store.record_decision_outcome(result.decision_id, "error")
    before = store.to_snapshot()

    for eval_result in ("pass", "error"):
        with pytest.raises(ValueError):
            store.complete_memory_run(
                trace_id=current.trace_id,
                decision_id=result.decision_id,
                eval_result=eval_result,
            )
        assert store.to_snapshot() == before


def test_concurrent_conflicting_memory_run_completion_has_one_consistent_winner():
    store, current, result, _lesson = store_with_pending_memory_run()
    start = threading.Barrier(3)
    outcomes: list[object] = []

    def complete(eval_result: str) -> None:
        start.wait()
        try:
            outcomes.append(
                store.complete_memory_run(
                    trace_id=current.trace_id,
                    decision_id=result.decision_id,
                    eval_result=eval_result,
                )
            )
        except ValueError as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=complete, args=(eval_result,))
        for eval_result in ("pass", "fail")
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    successes = [
        outcome
        for outcome in outcomes
        if type(outcome).__name__ == "MemoryRunCompletion"
    ]
    failures = [outcome for outcome in outcomes if isinstance(outcome, ValueError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert store.traces[current.trace_id].eval_result == store.usage_logs[0].eval_result
    assert store.traces[current.trace_id].eval_result == successes[0].trace.eval_result


def test_memory_run_result_is_exported_frozen_and_uses_immutable_tool_tuple():
    result = tbm.MemoryRunResult(
        decision_id="decision_000001",
        eval_result="pass",
        output_hash="sha256:output",
        tool_outputs=({"documents": 3},),
        latency_ms=25,
    )

    assert result.tool_outputs == ({"documents": 3},)
    assert "MeasuredEvalResult" in tbm.__all__
    assert "MemoryRunResult" in tbm.__all__
    with pytest.raises(FrozenInstanceError):
        result.eval_result = "error"


def test_complete_memory_runs_atomically_records_mixed_results_and_evidence():
    store, source_trace, _case, lesson = store_with_active_lesson()
    pass_trace, pass_decision = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_pass"
    )
    fail_trace, fail_decision = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_fail"
    )
    error_trace, error_decision = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_error"
    )
    results = (
        tbm.MemoryRunResult(
            decision_id=error_decision.decision_id,
            eval_result="error",
            cost_usd=0.03,
            error="executor failed",
            trace_uri="trace://error",
        ),
        tbm.MemoryRunResult(
            decision_id=pass_decision.decision_id,
            eval_result="pass",
            output_hash="sha256:pass-output",
            tool_outputs=({"documents": 3},),
            latency_ms=25,
        ),
        tbm.MemoryRunResult(
            decision_id=fail_decision.decision_id,
            eval_result="fail",
            memory_caused_failure=True,
            output_hash="sha256:fail-output",
            error="memory instruction was wrong",
        ),
    )

    completions = store.complete_memory_runs(results)

    assert isinstance(completions, tuple)
    assert tuple(item.usage_log.decision_id for item in completions) == tuple(
        result.decision_id for result in results
    )
    assert [item.trace.trace_id for item in completions] == [
        error_trace.trace_id,
        pass_trace.trace_id,
        fail_trace.trace_id,
    ]
    assert [item.trace.eval_result for item in completions] == [
        "error",
        "pass",
        "fail",
    ]
    assert completions[0].trace.cost_usd == 0.03
    assert completions[0].trace.error == "executor failed"
    assert completions[0].trace.trace_uri == "trace://error"
    assert completions[1].trace.output_hash == "sha256:pass-output"
    assert completions[1].trace.tool_outputs == [{"documents": 3}]
    assert completions[1].trace.latency_ms == 25
    assert completions[2].usage_log.memory_caused_failure is True
    assert [audit.status for audit in store.memory_run_audits()] == [
        "complete",
        "complete",
        "complete",
    ]
    assert store.memory_run_metrics().complete_count == 3
    assert store.metrics().evaluated_with_memory_count == 3
    completed_snapshot = store.to_snapshot()
    assert "memory_run_results" not in completed_snapshot
    assert TraceBackedMemoryStore.from_snapshot(
        completed_snapshot
    ).memory_run_audits() == store.memory_run_audits()

    replayed = store.complete_memory_runs(results)
    assert replayed == completions
    assert store.to_snapshot() == completed_snapshot

    completions[1].trace.tool_outputs.append({"mutated": True})
    completions[1].usage_log.candidate_memory_ids.append("lesson_spoofed")
    assert store.traces[pass_trace.trace_id].tool_outputs == [{"documents": 3}]
    assert "lesson_spoofed" not in next(
        log
        for log in store.usage_logs
        if log.decision_id == pass_decision.decision_id
    ).candidate_memory_ids


def test_complete_memory_runs_supports_matching_partial_and_complete_states():
    store, source_trace, _case, lesson = store_with_active_lesson()
    trace_only_trace, trace_only_decision = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_trace_only"
    )
    decision_only_trace, decision_only_decision = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_decision_only"
    )
    complete_trace, complete_decision = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_replay"
    )
    store.complete_trace(
        trace_only_trace.trace_id,
        eval_result="pass",
        output_hash="sha256:trace-first",
        tool_outputs=[{"prefilled": True}],
    )
    store.record_decision_outcome(
        decision_only_decision.decision_id,
        "error",
        memory_caused_failure=True,
    )
    store.complete_memory_run(
        trace_id=complete_trace.trace_id,
        decision_id=complete_decision.decision_id,
        eval_result="fail",
        error="already complete",
    )

    completions = store.complete_memory_runs(
        (
            tbm.MemoryRunResult(
                decision_id=decision_only_decision.decision_id,
                eval_result="error",
                memory_caused_failure=True,
                trace_uri="trace://decision-first",
                tool_outputs=(),
            ),
            tbm.MemoryRunResult(
                decision_id=trace_only_decision.decision_id,
                eval_result="pass",
            ),
            tbm.MemoryRunResult(
                decision_id=complete_decision.decision_id,
                eval_result="fail",
            ),
        )
    )

    assert [item.trace.eval_result for item in completions] == [
        "error",
        "pass",
        "fail",
    ]
    assert completions[0].trace.trace_id == decision_only_trace.trace_id
    assert completions[0].trace.trace_uri == "trace://decision-first"
    assert completions[0].trace.tool_outputs == []
    assert completions[1].trace.output_hash == "sha256:trace-first"
    assert completions[1].trace.tool_outputs == [{"prefilled": True}]
    assert completions[2].trace.error == "already complete"
    assert all(
        audit.status == "complete" for audit in store.memory_run_audits()
    )


def test_complete_memory_runs_merges_compatible_shared_trace_evidence():
    store, source_trace, _case, lesson = store_with_active_lesson()
    current, first = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_shared"
    )
    request = store.prepare_memory(
        matching_context(current), task="second shared completion"
    )
    second = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current.trace_id,
    )

    completions = store.complete_memory_runs(
        (
            tbm.MemoryRunResult(
                decision_id=second.decision_id,
                eval_result="pass",
                output_hash="sha256:shared",
                tool_outputs=({"documents": 2},),
            ),
            tbm.MemoryRunResult(
                decision_id=first.decision_id,
                eval_result="pass",
                output_hash="sha256:shared",
                latency_ms=40,
                cost_usd=0.01,
            ),
        )
    )

    assert tuple(item.usage_log.decision_id for item in completions) == (
        second.decision_id,
        first.decision_id,
    )
    assert completions[0].trace == completions[1].trace
    assert completions[0].trace.output_hash == "sha256:shared"
    assert completions[0].trace.tool_outputs == [{"documents": 2}]
    assert completions[0].trace.latency_ms == 40
    assert completions[0].trace.cost_usd == 0.01
    assert store.traces[current.trace_id] == completions[0].trace


@pytest.mark.parametrize(
    ("first_result", "second_result", "message"),
    [
        (
            {"eval_result": "pass", "output_hash": "sha256:first"},
            {"eval_result": "pass", "output_hash": "sha256:second"},
            "shared trace has conflicting completion evidence",
        ),
        (
            {"eval_result": "pass"},
            {"eval_result": "error"},
            "shared trace has conflicting outcomes",
        ),
    ],
)
def test_complete_memory_runs_rejects_shared_trace_disagreement_atomically(
    first_result: dict[str, object],
    second_result: dict[str, object],
    message: str,
):
    store, source_trace, _case, lesson = store_with_active_lesson()
    current, first = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_shared_conflict"
    )
    request = store.prepare_memory(
        matching_context(current), task="second conflicting completion"
    )
    second = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current.trace_id,
    )
    before = store.to_snapshot()

    with pytest.raises(ValueError, match=message):
        store.complete_memory_runs(
            (
                tbm.MemoryRunResult(
                    decision_id=first.decision_id,
                    **first_result,
                ),
                tbm.MemoryRunResult(
                    decision_id=second.decision_id,
                    **second_result,
                ),
            )
        )
    assert store.to_snapshot() == before


def test_complete_memory_runs_validates_inputs_without_partial_mutation():
    store, source_trace, _case, lesson = store_with_active_lesson()
    _first_trace, first = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_valid"
    )
    _second_trace, second = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_invalid"
    )
    valid = tbm.MemoryRunResult(decision_id=first.decision_id, eval_result="pass")
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="non-empty MemoryRunResult tuple"):
        store.complete_memory_runs([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty MemoryRunResult tuple"):
        store.complete_memory_runs(())
    with pytest.raises(ValueError, match="exactly a MemoryRunResult"):
        store.complete_memory_runs((valid, {"decision_id": second.decision_id}))
    with pytest.raises(ValueError, match="unique decision_ids"):
        store.complete_memory_runs((valid, valid))
    with pytest.raises(ValueError, match="unknown decision_id: decision_missing"):
        store.complete_memory_runs(
            (
                valid,
                tbm.MemoryRunResult(
                    decision_id="decision_missing",
                    eval_result="pass",
                ),
            )
        )
    with pytest.raises(ValueError, match="requires measured eval_result"):
        store.complete_memory_runs(
            (
                valid,
                tbm.MemoryRunResult(
                    decision_id=second.decision_id,
                    eval_result="unknown",  # type: ignore[arg-type]
                ),
            )
        )
    with pytest.raises(ValueError, match="memory_caused_failure must be a boolean"):
        store.complete_memory_runs(
            (
                valid,
                tbm.MemoryRunResult(
                    decision_id=second.decision_id,
                    eval_result="error",
                    memory_caused_failure=None,  # type: ignore[arg-type]
                ),
            )
        )
    with pytest.raises(ValueError, match="tool_outputs must be a tuple or None"):
        store.complete_memory_runs(
            (
                valid,
                tbm.MemoryRunResult(
                    decision_id=second.decision_id,
                    eval_result="pass",
                    tool_outputs=[],  # type: ignore[arg-type]
                ),
            )
        )
    with pytest.raises(ValueError):
        store.complete_memory_runs(
            (
                valid,
                tbm.MemoryRunResult(
                    decision_id=second.decision_id,
                    eval_result="pass",
                    tool_outputs=({1: "invalid key"},),  # type: ignore[dict-item]
                ),
            )
        )
    with pytest.raises(TypeError, match="unexpected keyword argument 'trace_id'"):
        tbm.MemoryRunResult(
            decision_id=second.decision_id,
            eval_result="pass",
            trace_id="trace_spoofed",  # type: ignore[call-arg]
        )
    assert store.to_snapshot() == before


def test_complete_memory_runs_rolls_back_on_later_candidate_validation_failure(
    monkeypatch,
):
    store, source_trace, _case, lesson = store_with_active_lesson()
    _first_trace, first = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_candidate_first"
    )
    _second_trace, second = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete_candidate_second"
    )
    before = store.to_snapshot()
    original_validate = store._validate_usage_log_trace

    def reject_second(log):
        original_validate(log)
        if log.decision_id == second.decision_id:
            raise ValueError("injected second completion candidate failure")

    monkeypatch.setattr(store, "_validate_usage_log_trace", reject_second)

    with pytest.raises(
        ValueError, match="injected second completion candidate failure"
    ):
        store.complete_memory_runs(
            (
                tbm.MemoryRunResult(
                    decision_id=first.decision_id,
                    eval_result="pass",
                ),
                tbm.MemoryRunResult(
                    decision_id=second.decision_id,
                    eval_result="pass",
                ),
            )
        )
    assert store.to_snapshot() == before


def test_memory_run_audits_classify_every_state_and_round_trip_stably():
    store, source_trace, _case, lesson = store_with_active_lesson()
    pending_trace, pending_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="pending"
    )
    trace_only_trace, trace_only_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="trace_only"
    )
    decision_only_trace, decision_only_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="decision_only"
    )
    complete_trace, complete_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="complete"
    )
    conflict_trace, conflict_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="conflict"
    )

    store.complete_trace(trace_only_trace.trace_id, eval_result="pass")
    store.record_decision_outcome(decision_only_result.decision_id, "error")
    store.complete_memory_run(
        trace_id=complete_trace.trace_id,
        decision_id=complete_result.decision_id,
        eval_result="fail",
        memory_caused_failure=True,
    )
    store.complete_trace(conflict_trace.trace_id, eval_result="pass")
    store.record_decision_outcome(conflict_result.decision_id, "error")
    before = store.to_snapshot()

    audits = store.memory_run_audits()

    assert isinstance(audits, tuple)
    assert all(isinstance(audit, tbm.MemoryRunAudit) for audit in audits)
    assert "MemoryRunAudit" in tbm.__all__
    assert "MemoryRunAuditStatus" in tbm.__all__
    assert [audit.decision_id for audit in audits] == sorted(
        audit.decision_id for audit in audits
    )
    by_decision = {audit.decision_id: audit for audit in audits}
    expected = {
        pending_result.decision_id: (
            "pending",
            pending_trace.trace_id,
            "unknown",
            None,
            False,
        ),
        trace_only_result.decision_id: (
            "trace_only",
            trace_only_trace.trace_id,
            "pass",
            None,
            False,
        ),
        decision_only_result.decision_id: (
            "decision_only",
            decision_only_trace.trace_id,
            "unknown",
            "error",
            False,
        ),
        complete_result.decision_id: (
            "complete",
            complete_trace.trace_id,
            "fail",
            "fail",
            True,
        ),
        conflict_result.decision_id: (
            "conflict",
            conflict_trace.trace_id,
            "pass",
            "error",
            False,
        ),
    }
    assert {
        decision_id: (
            audit.status,
            audit.trace_id,
            audit.trace_eval_result,
            audit.decision_eval_result,
            audit.memory_caused_failure,
        )
        for decision_id, audit in by_decision.items()
    } == expected
    assert all(
        audit.run_id == store.traces[audit.trace_id].run_id for audit in audits
    )
    with pytest.raises(FrozenInstanceError):
        audits[0].status = "conflict"
    assert store.to_snapshot() == before

    reversed_snapshot = deepcopy(before)
    reversed_snapshot["usage_logs"].reverse()
    restored = TraceBackedMemoryStore.from_snapshot(reversed_snapshot)
    assert restored.memory_run_audits() == audits


def test_memory_run_audits_are_decision_oriented_and_empty_store_is_empty():
    assert TraceBackedMemoryStore().memory_run_audits() == ()
    store, source_trace, _case, lesson = store_with_active_lesson()
    current, first = add_pending_memory_run(
        store, source_trace, lesson, suffix="shared_trace"
    )
    second_request = store.prepare_memory(
        matching_context(current), task="second decision"
    )
    second = store.finalize_memory(
        second_request,
        allow_decision(lesson.lesson_id),
        trace_id=current.trace_id,
        eval_result="unknown",
    )

    audits = store.memory_run_audits()

    assert [audit.decision_id for audit in audits] == [
        first.decision_id,
        second.decision_id,
    ]
    assert [audit.trace_id for audit in audits] == [
        current.trace_id,
        current.trace_id,
    ]
    assert [audit.status for audit in audits] == ["pending", "pending"]
    assert [audit.decision_eval_result for audit in audits] == [None, "unknown"]
    assert source_trace.trace_id not in {audit.trace_id for audit in audits}


def test_memory_run_remediations_map_every_state_to_an_action():
    store, source_trace, _case, lesson = store_with_active_lesson()
    pending_trace, pending_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="remediation_pending"
    )
    passing_trace, passing_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="remediation_trace_pass"
    )
    failed_trace, failed_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="remediation_trace_fail"
    )
    _decision_trace, decision_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="remediation_decision"
    )
    complete_trace, complete_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="remediation_complete"
    )
    conflict_trace, conflict_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="remediation_conflict"
    )

    store.complete_trace(passing_trace.trace_id, eval_result="pass")
    store.complete_trace(failed_trace.trace_id, eval_result="error")
    store.record_decision_outcome(
        decision_result.decision_id,
        "fail",
        memory_caused_failure=True,
    )
    store.complete_memory_run(
        trace_id=complete_trace.trace_id,
        decision_id=complete_result.decision_id,
        eval_result="error",
        memory_caused_failure=False,
    )
    store.complete_trace(conflict_trace.trace_id, eval_result="pass")
    store.record_decision_outcome(
        conflict_result.decision_id,
        "error",
        memory_caused_failure=True,
    )
    before = store.to_snapshot()

    remediations = store.memory_run_remediations()

    assert isinstance(remediations, tuple)
    assert all(
        isinstance(remediation, tbm.MemoryRunRemediation)
        for remediation in remediations
    )
    assert "MemoryRunRemediation" in tbm.__all__
    assert "MemoryRunRemediationAction" in tbm.__all__
    assert set(get_args(tbm.MemoryRunRemediationAction)) == {
        "measure",
        "recover",
        "recover_with_attribution",
        "investigate",
        "none",
    }
    assert [item.decision_id for item in remediations] == sorted(
        item.decision_id for item in remediations
    )
    by_decision = {item.decision_id: item for item in remediations}
    assert {
        decision_id: (
            item.status,
            item.action,
            item.trace_eval_result,
            item.decision_eval_result,
            item.memory_caused_failure,
            item.resolved_eval_result,
            item.resolved_memory_caused_failure,
        )
        for decision_id, item in by_decision.items()
    } == {
        pending_result.decision_id: (
            "pending",
            "measure",
            "unknown",
            None,
            False,
            None,
            None,
        ),
        passing_result.decision_id: (
            "trace_only",
            "recover",
            "pass",
            None,
            False,
            "pass",
            False,
        ),
        failed_result.decision_id: (
            "trace_only",
            "recover_with_attribution",
            "error",
            None,
            False,
            "error",
            None,
        ),
        decision_result.decision_id: (
            "decision_only",
            "recover",
            "unknown",
            "fail",
            True,
            "fail",
            True,
        ),
        complete_result.decision_id: (
            "complete",
            "none",
            "error",
            "error",
            False,
            "error",
            False,
        ),
        conflict_result.decision_id: (
            "conflict",
            "investigate",
            "pass",
            "error",
            True,
            None,
            None,
        ),
    }
    assert by_decision[pending_result.decision_id].trace_id == pending_trace.trace_id
    assert all(
        item.run_id == store.traces[item.trace_id].run_id
        for item in remediations
    )
    with pytest.raises(FrozenInstanceError):
        remediations[0].action = "none"
    assert store.to_snapshot() == before
    assert "memory_run_remediations" not in before
    assert (
        TraceBackedMemoryStore.from_snapshot(before).memory_run_remediations()
        == remediations
    )


def test_memory_run_remediations_are_decision_oriented_and_advisory():
    assert TraceBackedMemoryStore().memory_run_remediations() == ()
    store, source_trace, _case, lesson = store_with_active_lesson()
    current, first = add_pending_memory_run(
        store, source_trace, lesson, suffix="remediation_shared"
    )
    request = store.prepare_memory(
        matching_context(current), task="second remediation decision"
    )
    second = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current.trace_id,
    )
    store.complete_trace(current.trace_id, eval_result="pass")

    planned = store.memory_run_remediations()

    assert [item.decision_id for item in planned] == [
        first.decision_id,
        second.decision_id,
    ]
    assert [item.trace_id for item in planned] == [
        current.trace_id,
        current.trace_id,
    ]
    assert [item.action for item in planned] == ["recover", "recover"]

    store.record_decision_outcome(first.decision_id, "error")
    with pytest.raises(ValueError, match="memory run has conflicting outcomes"):
        store.recover_memory_run(first.decision_id)
    assert planned[0].action == "recover"
    assert {
        item.decision_id: item.action
        for item in store.memory_run_remediations()
    } == {
        first.decision_id: "investigate",
        second.decision_id: "recover",
    }

    disagreeing, source_trace, _case, lesson = store_with_active_lesson()
    shared, first = add_pending_memory_run(
        disagreeing,
        source_trace,
        lesson,
        suffix="remediation_shared_disagreement",
    )
    request = disagreeing.prepare_memory(
        matching_context(shared), task="second disagreeing decision"
    )
    second = disagreeing.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=shared.trace_id,
    )
    disagreeing.record_decision_outcome(first.decision_id, "pass")
    disagreeing.record_decision_outcome(second.decision_id, "error")
    disagreement_plan = disagreeing.memory_run_remediations()
    before = disagreeing.to_snapshot()

    assert [item.action for item in disagreement_plan] == [
        "recover",
        "recover",
    ]
    assert [item.resolved_eval_result for item in disagreement_plan] == [
        "pass",
        "error",
    ]
    with pytest.raises(ValueError, match="shared trace has conflicting outcomes"):
        disagreeing.recover_memory_runs(
            tuple(item.decision_id for item in disagreement_plan)
        )
    assert disagreeing.to_snapshot() == before


def test_recover_ready_memory_runs_recovers_only_automatic_actions():
    store, source_trace, _case, lesson = store_with_active_lesson()
    _pending_trace, pending_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_pending"
    )
    passing_trace, passing_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_passing_trace"
    )
    failed_trace, failed_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_failed_trace"
    )
    _decision_trace, decision_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_decision"
    )
    complete_trace, complete_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_complete"
    )
    conflict_trace, conflict_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_conflict"
    )

    store.complete_trace(passing_trace.trace_id, eval_result="pass")
    store.complete_trace(failed_trace.trace_id, eval_result="error")
    store.record_decision_outcome(
        decision_result.decision_id,
        "fail",
        memory_caused_failure=True,
    )
    store.complete_memory_run(
        trace_id=complete_trace.trace_id,
        decision_id=complete_result.decision_id,
        eval_result="pass",
    )
    store.complete_trace(conflict_trace.trace_id, eval_result="pass")
    store.record_decision_outcome(conflict_result.decision_id, "error")
    before_metrics = store.memory_run_metrics()

    completions = store.recover_ready_memory_runs()

    expected_ids = (
        passing_result.decision_id,
        decision_result.decision_id,
    )
    assert isinstance(completions, tuple)
    assert tuple(item.usage_log.decision_id for item in completions) == expected_ids
    assert [item.trace.eval_result for item in completions] == ["pass", "fail"]
    assert [item.usage_log.memory_caused_failure for item in completions] == [
        False,
        True,
    ]
    assert before_metrics.auto_recoverable_count == len(completions) == 2
    assert {
        item.decision_id: item.action
        for item in store.memory_run_remediations()
    } == {
        pending_result.decision_id: "measure",
        passing_result.decision_id: "none",
        failed_result.decision_id: "recover_with_attribution",
        decision_result.decision_id: "none",
        complete_result.decision_id: "none",
        conflict_result.decision_id: "investigate",
    }
    completions[0].usage_log.candidate_memory_ids.append("lesson_spoofed")
    assert "lesson_spoofed" not in next(
        log
        for log in store.usage_logs
        if log.decision_id == passing_result.decision_id
    ).candidate_memory_ids
    after = store.to_snapshot()
    assert store.recover_ready_memory_runs() == ()
    assert store.to_snapshot() == after
    assert store.memory_run_metrics().auto_recoverable_count == 0
    assert store.memory_run_metrics().attribution_required_count == 1
    restored = TraceBackedMemoryStore.from_snapshot(after)
    assert restored.memory_run_remediations() == store.memory_run_remediations()
    assert restored.recover_ready_memory_runs() == ()


def test_recover_ready_memory_runs_has_no_caller_selected_inputs():
    store = TraceBackedMemoryStore()

    assert store.recover_ready_memory_runs() == ()
    with pytest.raises(TypeError, match="takes 1 positional argument"):
        store.recover_ready_memory_runs(("decision_000001",))
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        store.recover_ready_memory_runs(
            memory_caused_failures={"decision_000001": False}
        )


def test_recover_ready_memory_runs_preserves_shared_trace_batch_validation():
    def shared_decision_only_store():
        store, source_trace, _case, lesson = store_with_active_lesson()
        current, first = add_pending_memory_run(
            store, source_trace, lesson, suffix="ready_sweep_shared"
        )
        request = store.prepare_memory(
            matching_context(current), task="second ready shared decision"
        )
        second = store.finalize_memory(
            request,
            allow_decision(lesson.lesson_id),
            trace_id=current.trace_id,
        )
        return store, current, first, second

    matching, current, first, second = shared_decision_only_store()
    matching.record_decision_outcome(first.decision_id, "pass")
    matching.record_decision_outcome(second.decision_id, "pass")

    completions = matching.recover_ready_memory_runs()

    assert tuple(item.usage_log.decision_id for item in completions) == (
        first.decision_id,
        second.decision_id,
    )
    assert matching.traces[current.trace_id].eval_result == "pass"
    assert all(
        item.status == "complete" for item in matching.memory_run_audits()
    )

    conflicting, _trace, first, second = shared_decision_only_store()
    conflicting.record_decision_outcome(first.decision_id, "pass")
    conflicting.record_decision_outcome(second.decision_id, "error")
    before = conflicting.to_snapshot()

    with pytest.raises(ValueError, match="shared trace has conflicting outcomes"):
        conflicting.recover_ready_memory_runs()
    assert conflicting.to_snapshot() == before


def test_recover_ready_memory_runs_rolls_back_later_candidate_failure(
    monkeypatch,
):
    store, source_trace, _case, lesson = store_with_active_lesson()
    first_trace, first = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_candidate_first"
    )
    second_trace, second = add_pending_memory_run(
        store, source_trace, lesson, suffix="ready_sweep_candidate_second"
    )
    store.complete_trace(first_trace.trace_id, eval_result="pass")
    store.complete_trace(second_trace.trace_id, eval_result="pass")
    before = store.to_snapshot()
    original_validate = store._validate_usage_log_trace

    def reject_second(log):
        original_validate(log)
        if log.decision_id == second.decision_id:
            raise ValueError("injected ready sweep candidate failure")

    monkeypatch.setattr(store, "_validate_usage_log_trace", reject_second)

    with pytest.raises(ValueError, match="injected ready sweep candidate failure"):
        store.recover_ready_memory_runs()
    assert store.to_snapshot() == before
    assert first.decision_id != second.decision_id


def test_concurrent_ready_memory_run_sweeps_commit_once():
    store, current, result, _lesson = store_with_pending_memory_run()
    store.complete_trace(current.trace_id, eval_result="pass")
    start = threading.Barrier(3)
    outcomes = []

    def recover_ready():
        start.wait()
        try:
            outcomes.append(store.recover_ready_memory_runs())
        except Exception as exc:  # pragma: no cover - diagnostic capture
            outcomes.append(exc)

    threads = [threading.Thread(target=recover_ready) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert all(isinstance(outcome, tuple) for outcome in outcomes)
    assert sorted(len(outcome) for outcome in outcomes) == [0, 1]
    completion = next(outcome[0] for outcome in outcomes if outcome)
    assert completion.usage_log.decision_id == result.decision_id
    assert store.memory_run_audits()[0].status == "complete"


def test_memory_run_metrics_empty_store_is_frozen_and_exported():
    metrics = TraceBackedMemoryStore().memory_run_metrics()

    assert metrics == tbm.MemoryRunMetrics(
        decision_count=0,
        pending_count=0,
        trace_only_count=0,
        decision_only_count=0,
        complete_count=0,
        conflict_count=0,
        recoverable_count=0,
    )
    assert "MemoryRunMetrics" in tbm.__all__
    assert metrics.auto_recoverable_count == 0
    assert metrics.attribution_required_count == 0
    with pytest.raises(FrozenInstanceError):
        metrics.pending_count = 1


def test_memory_run_metrics_count_every_decision_and_follow_recovery():
    store, source_trace, _case, lesson = store_with_active_lesson()
    pending_trace, _pending_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="metrics_pending"
    )
    trace_only_trace, trace_only_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="metrics_trace_only"
    )
    _decision_only_trace, decision_only_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="metrics_decision_only"
    )
    complete_trace, complete_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="metrics_complete"
    )
    conflict_trace, conflict_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="metrics_conflict"
    )
    shared_request = store.prepare_memory(
        matching_context(pending_trace), task="second decision on one trace"
    )
    store.finalize_memory(
        shared_request,
        allow_decision(lesson.lesson_id),
        trace_id=pending_trace.trace_id,
    )

    store.complete_trace(trace_only_trace.trace_id, eval_result="pass")
    store.record_decision_outcome(decision_only_result.decision_id, "error")
    store.complete_memory_run(
        trace_id=complete_trace.trace_id,
        decision_id=complete_result.decision_id,
        eval_result="fail",
        memory_caused_failure=True,
    )
    store.complete_trace(conflict_trace.trace_id, eval_result="pass")
    store.record_decision_outcome(conflict_result.decision_id, "error")
    before = store.to_snapshot()

    metrics = store.memory_run_metrics()

    assert metrics == tbm.MemoryRunMetrics(
        decision_count=6,
        pending_count=2,
        trace_only_count=1,
        decision_only_count=1,
        complete_count=1,
        conflict_count=1,
        recoverable_count=2,
        auto_recoverable_count=2,
        attribution_required_count=0,
    )
    assert metrics.decision_count == (
        metrics.pending_count
        + metrics.trace_only_count
        + metrics.decision_only_count
        + metrics.complete_count
        + metrics.conflict_count
    )
    assert metrics.recoverable_count == (
        metrics.trace_only_count + metrics.decision_only_count
    )
    assert metrics.recoverable_count == (
        metrics.auto_recoverable_count + metrics.attribution_required_count
    )
    assert store.to_snapshot() == before
    assert "memory_run_metrics" not in before
    restored = TraceBackedMemoryStore.from_snapshot(before)
    assert restored.memory_run_metrics() == metrics

    store.recover_memory_run(trace_only_result.decision_id)
    after_trace_recovery = store.memory_run_metrics()
    assert after_trace_recovery.trace_only_count == 0
    assert after_trace_recovery.complete_count == 2
    assert after_trace_recovery.recoverable_count == 1
    assert after_trace_recovery.auto_recoverable_count == 1
    assert after_trace_recovery.attribution_required_count == 0

    store.recover_memory_run(decision_only_result.decision_id)
    after_both_recoveries = store.memory_run_metrics()
    assert after_both_recoveries.decision_only_count == 0
    assert after_both_recoveries.complete_count == 3
    assert after_both_recoveries.recoverable_count == 0
    assert after_both_recoveries.auto_recoverable_count == 0
    assert after_both_recoveries.attribution_required_count == 0


def test_memory_run_metrics_expose_recovery_attribution_work():
    store, current, result, _lesson = store_with_pending_memory_run()
    store.complete_trace(current.trace_id, eval_result="fail")

    metrics = store.memory_run_metrics()

    assert metrics.recoverable_count == 1
    assert metrics.auto_recoverable_count == 0
    assert metrics.attribution_required_count == 1
    assert (
        metrics.recoverable_count
        == metrics.auto_recoverable_count + metrics.attribution_required_count
    )

    store.recover_memory_run(
        result.decision_id,
        memory_caused_failure=False,
    )
    recovered = store.memory_run_metrics()
    assert recovered.recoverable_count == 0
    assert recovered.auto_recoverable_count == 0
    assert recovered.attribution_required_count == 0


def test_recover_memory_run_completes_decision_only_and_preserves_attribution():
    store, current, result, _lesson = store_with_pending_memory_run()
    sealed = store.record_decision_outcome(
        result.decision_id,
        "error",
        memory_caused_failure=True,
    )
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="decision outcome already sealed"):
        store.recover_memory_run(
            result.decision_id,
            memory_caused_failure=False,
        )
    assert store.to_snapshot() == before

    recovered = store.recover_memory_run(
        result.decision_id,
        output_hash="sha256:recovered-output",
        tool_outputs=[{"status": "failed"}],
        error="executor failed",
    )

    assert isinstance(recovered, tbm.MemoryRunCompletion)
    assert recovered.trace.trace_id == current.trace_id
    assert recovered.trace.eval_result == "error"
    assert recovered.trace.output_hash == "sha256:recovered-output"
    assert recovered.trace.error == "executor failed"
    assert recovered.usage_log == sealed
    assert recovered.usage_log.memory_caused_failure is True
    assert store.memory_run_audits()[0].status == "complete"
    restored = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    assert restored.memory_run_audits() == store.memory_run_audits()
    assert restored.traces[current.trace_id] == recovered.trace


def test_recover_memory_run_completes_trace_only_pass_without_attribution():
    store, current, result, _lesson = store_with_pending_memory_run()
    completed = store.complete_trace(
        current.trace_id,
        eval_result="pass",
        output_hash="sha256:trace-first",
    )
    before = store.to_snapshot()

    with pytest.raises(
        ValueError,
        match="memory_caused_failure requires eval_result fail or error",
    ):
        store.recover_memory_run(
            result.decision_id,
            memory_caused_failure=True,
        )
    assert store.to_snapshot() == before

    recovered = store.recover_memory_run(result.decision_id)

    assert recovered.trace == completed
    assert recovered.usage_log.eval_result == "pass"
    assert recovered.usage_log.memory_caused_failure is False
    assert store.memory_run_audits()[0].status == "complete"


@pytest.mark.parametrize(
    ("eval_result", "memory_caused_failure"),
    [("fail", True), ("error", False)],
)
def test_recover_memory_run_requires_explicit_failed_trace_attribution(
    eval_result: str,
    memory_caused_failure: bool,
):
    store, current, result, _lesson = store_with_pending_memory_run()
    completed = store.complete_trace(
        current.trace_id,
        eval_result=eval_result,  # type: ignore[arg-type]
    )
    before = store.to_snapshot()

    with pytest.raises(
        ValueError,
        match="failed or errored trace requires explicit memory_caused_failure",
    ):
        store.recover_memory_run(result.decision_id)
    assert store.to_snapshot() == before

    recovered = store.recover_memory_run(
        result.decision_id,
        memory_caused_failure=memory_caused_failure,
    )

    assert recovered.trace == completed
    assert recovered.usage_log.eval_result == eval_result
    assert (
        recovered.usage_log.memory_caused_failure is memory_caused_failure
    )


def test_recover_memory_run_rejects_pending_and_conflicting_states_atomically():
    pending_store, _trace, pending_result, _lesson = store_with_pending_memory_run()
    pending_before = pending_store.to_snapshot()

    with pytest.raises(ValueError, match="memory run has no measured outcome"):
        pending_store.recover_memory_run(pending_result.decision_id)
    assert pending_store.to_snapshot() == pending_before

    conflict_store, trace, conflict_result, _lesson = store_with_pending_memory_run()
    conflict_store.complete_trace(trace.trace_id, eval_result="pass")
    conflict_store.record_decision_outcome(conflict_result.decision_id, "error")
    conflict_before = conflict_store.to_snapshot()

    with pytest.raises(ValueError, match="memory run has conflicting outcomes"):
        conflict_store.recover_memory_run(conflict_result.decision_id)
    assert conflict_store.to_snapshot() == conflict_before


def test_recover_memory_run_replays_complete_state_and_rejects_evidence_rewrite():
    store, current, result, _lesson = store_with_pending_memory_run()
    completed = store.complete_memory_run(
        trace_id=current.trace_id,
        decision_id=result.decision_id,
        eval_result="pass",
        output_hash="sha256:complete",
    )
    before = store.to_snapshot()

    assert store.recover_memory_run(result.decision_id) == completed
    assert store.to_snapshot() == before
    with pytest.raises(ValueError, match="trace execution already completed"):
        store.recover_memory_run(
            result.decision_id,
            output_hash="sha256:other",
        )
    assert store.to_snapshot() == before


def test_recover_memory_run_rejects_invalid_decision_ids_and_attribution_types():
    store, _current, result, _lesson = store_with_pending_memory_run()
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="memory run recovery requires decision_id"):
        store.recover_memory_run("")
    with pytest.raises(ValueError, match="unknown decision_id: decision_missing"):
        store.recover_memory_run("decision_missing")
    with pytest.raises(ValueError, match="memory_caused_failure must be a boolean"):
        store.recover_memory_run(
            result.decision_id,
            memory_caused_failure=None,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="unexpected keyword argument 'trace_id'"):
        store.recover_memory_run(
            result.decision_id,
            trace_id="trace_spoofed",  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError, match="unexpected keyword argument 'eval_result'"):
        store.recover_memory_run(
            result.decision_id,
            eval_result="pass",  # type: ignore[call-arg]
        )
    assert store.to_snapshot() == before


def test_recover_memory_runs_atomically_recovers_mixed_states_in_input_order():
    store, source_trace, _case, lesson = store_with_active_lesson()
    trace_only_trace, trace_only_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_trace_only"
    )
    failed_trace, failed_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_failed_trace_only"
    )
    decision_only_trace, decision_only_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_decision_only"
    )
    complete_trace, complete_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_complete"
    )
    store.complete_trace(trace_only_trace.trace_id, eval_result="pass")
    store.complete_trace(failed_trace.trace_id, eval_result="error")
    store.record_decision_outcome(
        decision_only_result.decision_id,
        "fail",
        memory_caused_failure=True,
    )
    store.complete_memory_run(
        trace_id=complete_trace.trace_id,
        decision_id=complete_result.decision_id,
        eval_result="pass",
        output_hash="sha256:already-complete",
    )
    decision_ids = (
        decision_only_result.decision_id,
        trace_only_result.decision_id,
        failed_result.decision_id,
        complete_result.decision_id,
    )

    completions = store.recover_memory_runs(
        decision_ids,
        memory_caused_failures={failed_result.decision_id: False},
    )

    assert isinstance(completions, tuple)
    assert all(isinstance(item, tbm.MemoryRunCompletion) for item in completions)
    assert tuple(item.usage_log.decision_id for item in completions) == decision_ids
    assert [item.trace.eval_result for item in completions] == [
        "fail",
        "pass",
        "error",
        "pass",
    ]
    assert [item.usage_log.memory_caused_failure for item in completions] == [
        True,
        False,
        False,
        False,
    ]
    assert completions[0].trace.trace_id == decision_only_trace.trace_id
    assert [audit.status for audit in store.memory_run_audits()] == [
        "complete",
        "complete",
        "complete",
        "complete",
    ]
    assert store.memory_run_metrics() == tbm.MemoryRunMetrics(
        decision_count=4,
        pending_count=0,
        trace_only_count=0,
        decision_only_count=0,
        complete_count=4,
        conflict_count=0,
        recoverable_count=0,
        auto_recoverable_count=0,
        attribution_required_count=0,
    )
    completed_snapshot = store.to_snapshot()
    assert TraceBackedMemoryStore.from_snapshot(
        completed_snapshot
    ).memory_run_metrics() == store.memory_run_metrics()

    replayed = store.recover_memory_runs(
        decision_ids,
        memory_caused_failures={failed_result.decision_id: False},
    )
    assert replayed == completions
    assert store.to_snapshot() == completed_snapshot

    completions[0].trace.tool_outputs.append({"mutated": True})
    completions[0].usage_log.candidate_memory_ids.append("lesson_spoofed")
    assert store.traces[decision_only_trace.trace_id].tool_outputs == []
    assert "lesson_spoofed" not in next(
        log
        for log in store.usage_logs
        if log.decision_id == decision_only_result.decision_id
    ).candidate_memory_ids


def test_recover_memory_runs_rejects_any_ineligible_item_without_mutation():
    store, source_trace, _case, lesson = store_with_active_lesson()
    recoverable_trace, recoverable_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_recoverable"
    )
    _pending_trace, pending_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_pending"
    )
    conflict_trace, conflict_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_conflict"
    )
    failed_trace, failed_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_missing_attribution"
    )
    store.complete_trace(recoverable_trace.trace_id, eval_result="pass")
    store.complete_trace(conflict_trace.trace_id, eval_result="pass")
    store.record_decision_outcome(conflict_result.decision_id, "error")
    store.complete_trace(failed_trace.trace_id, eval_result="fail")
    before = store.to_snapshot()

    invalid_batches = [
        (
            (recoverable_result.decision_id, pending_result.decision_id),
            {},
            "no measured outcome",
        ),
        (
            (recoverable_result.decision_id, conflict_result.decision_id),
            {},
            "conflicting outcomes",
        ),
        (
            (recoverable_result.decision_id, failed_result.decision_id),
            {},
            "requires explicit memory_caused_failure",
        ),
        (
            (recoverable_result.decision_id,),
            {recoverable_result.decision_id: True},
            "memory_caused_failure requires eval_result fail or error",
        ),
    ]
    for decision_ids, attributions, error in invalid_batches:
        with pytest.raises(ValueError, match=error):
            store.recover_memory_runs(
                decision_ids,
                memory_caused_failures=attributions,
            )
        assert store.to_snapshot() == before


def test_recover_memory_runs_rolls_back_on_later_candidate_validation_failure(
    monkeypatch,
):
    store, source_trace, _case, lesson = store_with_active_lesson()
    first_trace, first_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_candidate_first"
    )
    second_trace, second_result = add_pending_memory_run(
        store, source_trace, lesson, suffix="batch_candidate_second"
    )
    store.complete_trace(first_trace.trace_id, eval_result="pass")
    store.complete_trace(second_trace.trace_id, eval_result="pass")
    before = store.to_snapshot()
    original_validate = store._validate_usage_log_trace

    def reject_second(log):
        original_validate(log)
        if log.decision_id == second_result.decision_id:
            raise ValueError("injected second candidate failure")

    monkeypatch.setattr(store, "_validate_usage_log_trace", reject_second)

    with pytest.raises(ValueError, match="injected second candidate failure"):
        store.recover_memory_runs(
            (first_result.decision_id, second_result.decision_id)
        )
    assert store.to_snapshot() == before


def test_recover_memory_runs_validates_batch_inputs_before_mutation():
    store, current, result, _lesson = store_with_pending_memory_run()
    store.complete_trace(current.trace_id, eval_result="pass")
    before = store.to_snapshot()

    with pytest.raises(ValueError, match="non-empty decision_id tuple"):
        store.recover_memory_runs([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty decision_id tuple"):
        store.recover_memory_runs(())
    with pytest.raises(ValueError, match="unique decision_ids"):
        store.recover_memory_runs((result.decision_id, result.decision_id))
    with pytest.raises(ValueError, match="unknown decision_id: decision_missing"):
        store.recover_memory_runs((result.decision_id, "decision_missing"))
    with pytest.raises(ValueError, match="must be a mapping"):
        store.recover_memory_runs(
            (result.decision_id,),
            memory_caused_failures=[],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="must contain decision_id keys"):
        store.recover_memory_runs(
            (result.decision_id,),
            memory_caused_failures={1: False},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="must contain boolean values"):
        store.recover_memory_runs(
            (result.decision_id,),
            memory_caused_failures={result.decision_id: None},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="unrequested decision_id"):
        store.recover_memory_runs(
            (result.decision_id,),
            memory_caused_failures={"decision_other": False},
        )
    with pytest.raises(TypeError, match="unexpected keyword argument 'trace_id'"):
        store.recover_memory_runs(
            (result.decision_id,),
            trace_id=current.trace_id,  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError, match="unexpected keyword argument 'eval_result'"):
        store.recover_memory_runs(
            (result.decision_id,),
            eval_result="pass",  # type: ignore[call-arg]
        )
    assert store.to_snapshot() == before


def test_recover_memory_runs_handles_shared_traces_consistently_from_entry_state():
    def shared_pending_store():
        store, source_trace, _case, lesson = store_with_active_lesson()
        current, first = add_pending_memory_run(
            store, source_trace, lesson, suffix="batch_shared"
        )
        request = store.prepare_memory(
            matching_context(current), task="second shared decision"
        )
        second = store.finalize_memory(
            request,
            allow_decision(lesson.lesson_id),
            trace_id=current.trace_id,
        )
        return store, current, first, second

    matching, current, first, second = shared_pending_store()
    matching.record_decision_outcome(first.decision_id, "pass")
    matching.record_decision_outcome(second.decision_id, "pass")

    completions = matching.recover_memory_runs(
        (second.decision_id, first.decision_id)
    )

    assert tuple(item.usage_log.decision_id for item in completions) == (
        second.decision_id,
        first.decision_id,
    )
    assert matching.traces[current.trace_id].eval_result == "pass"
    assert [audit.status for audit in matching.memory_run_audits()] == [
        "complete",
        "complete",
    ]

    conflicting, _trace, first, second = shared_pending_store()
    conflicting.record_decision_outcome(first.decision_id, "pass")
    conflicting.record_decision_outcome(second.decision_id, "error")
    conflicting_before = conflicting.to_snapshot()
    with pytest.raises(ValueError, match="shared trace has conflicting outcomes"):
        conflicting.recover_memory_runs((first.decision_id, second.decision_id))
    assert conflicting.to_snapshot() == conflicting_before

    pending, _trace, first, second = shared_pending_store()
    pending.record_decision_outcome(first.decision_id, "pass")
    pending_before = pending.to_snapshot()
    with pytest.raises(ValueError, match="no measured outcome"):
        pending.recover_memory_runs((first.decision_id, second.decision_id))
    assert pending.to_snapshot() == pending_before


def test_mutating_public_collection_copy_does_not_change_store():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc",
            eval_result="fail",
        )
    )
    public = dict(store.traces)

    public["trace_001"].tool_calls.append({"name": "mutated"})

    assert store.traces["trace_001"].tool_calls == []


@pytest.mark.parametrize(
    ("collection_name", "record_id"),
    [
        ("failure_cases", "case_contract"),
        ("lessons", "lesson_001"),
        ("project_policies", "policy_001"),
    ],
)
def test_public_record_mapping_values_are_copy_isolated(
    collection_name: str, record_id: str
):
    store, _trace, _case, _lesson = store_with_active_lesson()
    store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_001",
            policy_text="Always provide a search query.",
            scope={"repo": "repo", "tool": "search_docs"},
        )
    )
    public = dict(getattr(store, collection_name))
    public_record = public[record_id]

    if isinstance(public_record, FailureCase):
        object.__setattr__(public_record, "symptom", "mutated")
        assert store.failure_cases[record_id].symptom == "search_docs received an empty query"
    else:
        public_record.scope["tool"] = "mutated"
        assert getattr(store, collection_name)[record_id].scope["tool"] == "search_docs"


@pytest.mark.parametrize("record_kind", ["lesson", "project_policy"])
def test_store_isolates_caller_owned_scope_of_accepted_records(record_kind: str):
    store, _trace, case = store_with_verified_case()
    scope = {"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"}

    if record_kind == "lesson":
        record = Lesson(
            lesson_id="lesson_scope",
            source_case_id=case.case_id,
            lesson_text="Always provide a search query.",
            memory_type="procedural",
            scope=scope,
        )
        store.add_lesson(record)
        collection_name = "lessons"
        record_id = record.lesson_id
    else:
        record = ProjectPolicy(
            policy_id="policy_scope",
            policy_text="Always provide a search query.",
            scope=scope,
        )
        store.add_project_policy(record)
        collection_name = "project_policies"
        record_id = record.policy_id

    record.scope["tool"] = "mutated"

    assert getattr(store, collection_name)[record_id].scope["tool"] == "search_docs"


def test_mutating_public_usage_logs_copy_does_not_change_store():
    store, _trace, _case, lesson = store_with_active_lesson()
    context = MemoryContext(
        mode="repair",
        repo="repo",
        tenant="tenant_a",
        commit_sha="abc",
        tool="search_docs",
    )
    store.log_decision(
        "run_contract",
        context,
        [lesson.lesson_id],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=[lesson.lesson_id],
            blocked_memory_ids=[],
            reason="relevant",
            risk="low",
            recommended_injection="short_summary",
        ),
    )
    public = store.usage_logs

    public[0].candidate_memory_ids.append("mutated")

    assert store.usage_logs[0].candidate_memory_ids == [lesson.lesson_id]


def test_store_can_obsolete_lesson_and_project_policy_idempotently():
    store, _trace, _case, lesson = store_with_active_lesson()
    policy = store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_001",
            policy_text="Always provide a search query.",
            scope={"repo": "repo", "tool": "search_docs"},
        )
    )

    obsolete_lesson_record = store.obsolete_lesson(lesson.lesson_id)
    obsolete_policy = store.obsolete_project_policy(policy.policy_id)

    assert obsolete_lesson_record.status == "obsolete"
    assert obsolete_policy.status == "obsolete"
    assert store.obsolete_lesson(lesson.lesson_id) == obsolete_lesson_record
    assert store.obsolete_project_policy(policy.policy_id) == obsolete_policy


def test_returned_records_are_isolated_from_store_nested_mutation():
    store, _trace, case, _lesson = store_with_active_lesson()
    returned_lesson = store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_returned",
            lesson_text="Use the stored query contract.",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
        )
    )

    returned_lesson.scope["tool"] = "mutated"

    assert store.lessons["lesson_returned"].scope["tool"] == "search_docs"


def valid_snapshot_dict() -> dict[str, object]:
    store, _trace, case = store_with_verified_case()
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_contract",
            lesson_text="Always pass a non-empty query.",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
        )
    )
    return store.to_snapshot()


def fully_populated_snapshot() -> dict[str, object]:
    store = TraceBackedMemoryStore()
    for suffix in ["a", "b"]:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_{suffix}",
                run_id=f"run_{suffix}",
                commit_sha=f"commit_{suffix}",
                repo="repo",
                tenant="tenant",
                eval_result="pass",
            )
        )
        case = store.add_failure_case(
            FailureCase(
                case_id=f"case_{suffix}",
                source_trace_id=trace.trace_id,
                commit_sha=trace.commit_sha,
                failure_type="invalid_tool_argument",
                symptom=f"symptom {suffix}",
                fix=f"fix {suffix}",
                fix_commit_sha=f"fix_commit_{suffix}",
                regression_passed=True,
                status="verified",
            )
        )
        lesson = store.add_lesson(
            Lesson(
                lesson_id=f"lesson_{suffix}",
                source_case_id=case.case_id,
                lesson_text=f"lesson {suffix}",
                memory_type="procedural",
                scope={"repo": "repo", "tenant": "tenant"},
            )
        )
        policy = store.add_project_policy(
            ProjectPolicy(
                policy_id=f"policy_{suffix}",
                policy_text=f"policy {suffix}",
                scope={"repo": "repo", "tenant": "tenant"},
            )
        )
        store.log_decision(
            trace.run_id,
            MemoryContext(
                mode="repair",
                repo="repo",
                tenant="tenant",
                commit_sha=trace.commit_sha,
            ),
            [lesson.lesson_id, policy.policy_id],
            MemoryDecision(
                use_memory=True,
                allowed_memory_ids=[lesson.lesson_id, policy.policy_id],
                blocked_memory_ids=[],
                reason=f"use records {suffix}",
                risk="low",
                recommended_injection="short_summary",
            ),
            eval_result="pass",
        )
    return store.to_snapshot()


def v2_snapshot_with_usage_log() -> dict[str, object]:
    store, trace, _case, lesson = store_with_active_lesson()
    store.log_decision(
        trace.run_id,
        matching_context(trace),
        [lesson.lesson_id],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=[lesson.lesson_id],
            blocked_memory_ids=[],
            reason="use matching lesson",
            risk="low",
            recommended_injection="short_summary",
        ),
        eval_result="pass",
    )
    return store.to_snapshot()


def _snapshot_record(
    snapshot: dict[str, object], collection_name: str, index: int = 0
) -> dict[str, object]:
    collection = snapshot[collection_name]
    assert isinstance(collection, list)
    record = collection[index]
    assert isinstance(record, dict)
    return record


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("trace_id", 1),
        ("run_id", []),
        ("commit_sha", {}),
        ("repo", 1),
        ("eval_result", []),
        ("dirty", 1),
        ("latency_ms", True),
        ("latency_ms", 1.5),
        ("cost_usd", False),
        ("created_at", "2026-07-10T12:00:00"),
    ],
)
def test_runtime_trace_validation_matches_schema_types(
    field_name: str, invalid_value: object
):
    values: dict[str, object] = {
        "trace_id": "trace_invalid",
        "run_id": "run_invalid",
        "commit_sha": "abc",
        "repo": "repo",
        "eval_result": "unknown",
    }
    values[field_name] = invalid_value

    with pytest.raises(ValueError, match=field_name):
        TraceBackedMemoryStore().record_trace(Trace(**values))  # type: ignore[arg-type]


@pytest.mark.parametrize("cost_usd", [float("nan"), float("inf"), float("-inf")])
def test_runtime_trace_rejects_non_finite_cost(cost_usd: float):
    with pytest.raises(ValueError, match="cost_usd"):
        TraceBackedMemoryStore().record_trace(
            Trace(
                trace_id="trace_non_finite",
                run_id="run_non_finite",
                commit_sha="abc",
                cost_usd=cost_usd,
            )
        )


@pytest.mark.parametrize(
    ("record_kind", "confidence"),
    [
        ("lesson", float("nan")),
        ("lesson", float("inf")),
        ("policy", float("-inf")),
    ],
)
def test_runtime_memory_records_reject_non_finite_confidence(
    record_kind: str, confidence: float
):
    store, _trace, case = store_with_verified_case()
    if record_kind == "lesson":
        insertion = lambda: store.add_lesson(
            Lesson(
                lesson_id="lesson_non_finite",
                source_case_id=case.case_id,
                lesson_text="rule",
                memory_type="procedural",
                scope={"repo": "repo", "tenant": "tenant_a"},
                confidence=confidence,
            )
        )
    else:
        insertion = lambda: store.add_project_policy(
            ProjectPolicy(
                policy_id="policy_non_finite",
                policy_text="rule",
                scope={"repo": "repo"},
                confidence=confidence,
            )
        )

    with pytest.raises(ValueError, match="confidence"):
        insertion()


def test_runtime_trace_error_text_is_not_treated_as_bounded_metadata():
    error_text = "failure detail " * 100
    trace = TraceBackedMemoryStore().record_trace(
        Trace(
            trace_id="trace_error_text",
            run_id="run_error_text",
            commit_sha="abc",
            error=error_text,
        )
    )

    assert trace.error == error_text


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("case_id", 1),
        ("source_trace_id", []),
        ("failure_type", {}),
        ("symptom", 7),
        ("root_cause", 1),
        ("regression_passed", 1),
        ("status", []),
        ("reviewed_at", "2026-07-10T12:00:00"),
        ("created_at", 0),
    ],
)
def test_runtime_failure_case_validation_matches_schema_types(
    field_name: str, invalid_value: object
):
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(trace_id="trace_source", run_id="run_source", commit_sha="abc")
    )
    values: dict[str, object] = {
        "case_id": "case_invalid",
        "source_trace_id": "trace_source",
        "commit_sha": "abc",
        "failure_type": "invalid_tool_argument",
        "symptom": "bad input",
    }
    values[field_name] = invalid_value

    with pytest.raises(ValueError, match=field_name):
        store.add_failure_case(FailureCase(**values))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("record_kind", "field_name", "invalid_value"),
    [
        ("lesson", "lesson_id", 1),
        ("lesson", "source_case_id", []),
        ("lesson", "lesson_text", 7),
        ("lesson", "memory_type", {}),
        ("lesson", "confidence", True),
        ("lesson", "sensitive", 1),
        ("lesson", "created_at", "2026-07-10T12:00:00"),
        ("policy", "policy_id", 1),
        ("policy", "policy_text", []),
        ("policy", "confidence", False),
        ("policy", "eval_leaking", 1),
        ("policy", "status", {}),
        ("policy", "created_at", "not-a-date"),
    ],
)
def test_runtime_memory_record_validation_matches_schema_types(
    record_kind: str, field_name: str, invalid_value: object
):
    store, _trace, case = store_with_verified_case()
    if record_kind == "lesson":
        values: dict[str, object] = {
            "lesson_id": "lesson_invalid",
            "source_case_id": case.case_id,
            "lesson_text": "rule",
            "memory_type": "procedural",
            "scope": {"repo": "repo", "tenant": "tenant_a"},
        }
        values[field_name] = invalid_value
        record = Lesson(**values)  # type: ignore[arg-type]
        insertion = lambda: store.add_lesson(record)
    else:
        values = {
            "policy_id": "policy_invalid",
            "policy_text": "rule",
            "scope": {"repo": "repo"},
        }
        values[field_name] = invalid_value
        record = ProjectPolicy(**values)  # type: ignore[arg-type]
        insertion = lambda: store.add_project_policy(record)

    with pytest.raises(ValueError, match=field_name):
        insertion()


@pytest.mark.parametrize(
    "record_kind",
    [
        "trace_id",
        "failure_case_id",
        "failure_source_id",
        "lesson_id",
        "lesson_source_id",
        "policy_id",
        "usage_memory_id",
    ],
)
def test_store_rejects_oversized_memory_and_source_identifiers(record_kind: str):
    oversized = "x" * 129
    if record_kind == "trace_id":
        with pytest.raises(ValueError, match="at most 128 characters"):
            TraceBackedMemoryStore().record_trace(
                Trace(trace_id=oversized, run_id="run", commit_sha="abc")
            )
        return

    store, trace, case = store_with_verified_case()
    if record_kind == "failure_case_id":
        record = FailureCase(
            case_id=oversized,
            source_trace_id=trace.trace_id,
            commit_sha=trace.commit_sha,
            failure_type="invalid_tool_argument",
            symptom="bad input",
        )
        insertion = lambda: store.add_failure_case(record)
    elif record_kind == "failure_source_id":
        record = FailureCase(
            case_id="case_invalid",
            source_trace_id=oversized,
            commit_sha=trace.commit_sha,
            failure_type="invalid_tool_argument",
            symptom="bad input",
        )
        insertion = lambda: store.add_failure_case(record)
    elif record_kind in {"lesson_id", "lesson_source_id"}:
        record = Lesson(
            lesson_id=oversized if record_kind == "lesson_id" else "lesson_invalid",
            source_case_id=oversized if record_kind == "lesson_source_id" else case.case_id,
            lesson_text="rule",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a"},
        )
        insertion = lambda: store.add_lesson(record)
    elif record_kind == "policy_id":
        record = ProjectPolicy(
            policy_id=oversized,
            policy_text="rule",
            scope={"repo": "repo"},
        )
        insertion = lambda: store.add_project_policy(record)
    else:
        insertion = lambda: store.log_decision(
            trace.run_id,
            matching_context(trace),
            [oversized],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[oversized],
                reason="blocked",
                risk="low",
                recommended_injection="none",
            ),
        )

    with pytest.raises(ValueError, match="at most 128 characters"):
        insertion()


@pytest.mark.parametrize("record_kind", ["lesson", "policy"])
def test_store_rejects_scope_values_over_metadata_budget(record_kind: str):
    store, _trace, case = store_with_verified_case()
    scope = {"repo": "r" * 513, "tenant": "tenant_a"}
    if record_kind == "lesson":
        insertion = lambda: store.add_lesson(
            Lesson(
                lesson_id="lesson_oversized_scope",
                source_case_id=case.case_id,
                lesson_text="rule",
                memory_type="procedural",
                scope=scope,
            )
        )
    else:
        insertion = lambda: store.add_project_policy(
            ProjectPolicy(
                policy_id="policy_oversized_scope",
                policy_text="rule",
                scope=scope,
            )
        )

    with pytest.raises(ValueError, match="at most 512 characters"):
        insertion()


@pytest.mark.parametrize(
    ("collection_name", "field_name", "invalid_value"),
    [
        ("traces", "latency_ms", True),
        ("traces", "cost_usd", False),
        ("failure_cases", "reviewed_by", 1),
        ("failure_cases", "status", []),
        ("lessons", "lesson_text", 1),
        ("lessons", "created_at", "2026-07-10T12:00:00"),
        ("project_policies", "policy_text", {}),
        ("project_policies", "confidence", True),
        ("usage_logs", "mode", []),
        ("usage_logs", "eval_result", True),
        ("usage_logs", "created_at", "2026-07-10T12:00:00"),
    ],
)
def test_v2_snapshot_record_validation_matches_schema_types(
    collection_name: str, field_name: str, invalid_value: object
):
    snapshot = fully_populated_snapshot()
    _snapshot_record(snapshot, collection_name)[field_name] = invalid_value

    with pytest.raises(ValueError, match=field_name):
        TraceBackedMemoryStore.from_snapshot(snapshot)


@pytest.mark.parametrize(
    ("collection_name", "field_name", "invalid_value"),
    [
        ("traces", "cost_usd", float("nan")),
        ("traces", "cost_usd", float("inf")),
        ("lessons", "confidence", float("-inf")),
        ("project_policies", "confidence", float("nan")),
    ],
)
def test_v2_snapshot_rejects_non_finite_numbers(
    collection_name: str, field_name: str, invalid_value: float
):
    snapshot = fully_populated_snapshot()
    _snapshot_record(snapshot, collection_name)[field_name] = invalid_value

    with pytest.raises(ValueError, match=field_name):
        TraceBackedMemoryStore.from_snapshot(snapshot)


@pytest.mark.parametrize(
    ("collection_name", "required_field", "record_label"),
    [
        ("traces", "trace_id", "trace"),
        ("failure_cases", "case_id", "failure case"),
        ("lessons", "lesson_id", "lesson"),
        ("project_policies", "policy_id", "project policy"),
        ("usage_logs", "decision_id", "usage log"),
    ],
)
def test_v2_snapshot_normalizes_record_constructor_type_errors(
    collection_name: str, required_field: str, record_label: str
):
    snapshot = fully_populated_snapshot()
    _snapshot_record(snapshot, collection_name).pop(required_field)

    with pytest.raises(ValueError, match=rf"invalid {record_label} record"):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_v2_snapshot_accepts_rfc3339_z_and_offset_timestamps():
    snapshot = fully_populated_snapshot()
    _snapshot_record(snapshot, "traces")["created_at"] = "2026-07-10T12:00:00Z"
    failure_case = _snapshot_record(snapshot, "failure_cases")
    failure_case["reviewed_at"] = "2026-07-10T20:00:00+08:00"
    failure_case["created_at"] = "2026-07-10T12:00:00.123Z"
    _snapshot_record(snapshot, "lessons")["created_at"] = (
        "2026-07-10T20:00:00+08:00"
    )
    _snapshot_record(snapshot, "project_policies")["created_at"] = (
        "2026-07-10T12:00:00Z"
    )
    _snapshot_record(snapshot, "usage_logs")["created_at"] = (
        "2026-07-10T20:00:00+08:00"
    )

    restored = TraceBackedMemoryStore.from_snapshot(snapshot)

    assert restored.to_snapshot()["usage_logs"]


def test_snapshot_v2_has_exact_versioned_envelope():
    snapshot = TraceBackedMemoryStore().to_snapshot()

    assert snapshot == {
        "snapshot_version": 2,
        "traces": [],
        "failure_cases": [],
        "lessons": [],
        "project_policies": [],
        "usage_logs": [],
    }


@pytest.mark.parametrize("payload", [{}, {"unknown": []}, {"snapshot_version": 2}])
def test_snapshot_rejects_truncated_or_unknown_envelopes(payload):
    with pytest.raises(ValueError, match="snapshot envelope"):
        TraceBackedMemoryStore.from_snapshot(payload)


@pytest.mark.parametrize("payload", [None, [], "snapshot", 7])
def test_snapshot_rejects_non_mapping_payloads_with_stable_error(payload):
    with pytest.raises(
        ValueError, match="memory store snapshot must be a JSON object"
    ):
        TraceBackedMemoryStore.from_snapshot(payload)


@pytest.mark.parametrize("snapshot_version", [True, 2.0, 3])
def test_snapshot_rejects_unsupported_version_values(snapshot_version):
    payload = TraceBackedMemoryStore().to_snapshot()
    payload["snapshot_version"] = snapshot_version

    with pytest.raises(ValueError, match="snapshot envelope"):
        TraceBackedMemoryStore.from_snapshot(payload)


def test_exact_legacy_snapshot_is_migrated():
    legacy = {
        "traces": [],
        "failure_cases": [],
        "lessons": [],
        "project_policies": [],
        "usage_logs": [],
    }

    assert TraceBackedMemoryStore.from_snapshot(legacy).to_snapshot()["snapshot_version"] == 2


@pytest.mark.parametrize(
    "field_name",
    [
        "trace_id",
        "context",
        "candidate_memory_statuses",
        "system_blocked_reasons",
    ],
)
def test_v2_snapshot_rejects_usage_logs_missing_safe_workflow_audit_fields(
    field_name: str,
):
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0].pop(field_name)

    with pytest.raises(ValueError, match=field_name):
        TraceBackedMemoryStore.from_snapshot(snapshot)


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        ({"trace_id": ""}, "trace_id"),
        ({"trace_id": "missing_trace"}, "unknown trace_id"),
        ({"run_id": "wrong_run"}, "run_id"),
        ({"context": {"mode": "repair", "repo": "wrong", "commit_sha": "abc"}}, "context repo"),
    ],
)
def test_v2_snapshot_requires_trace_linked_usage_log_evidence(
    mutation: dict[str, object], expected_message: str,
):
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0].update(mutation)

    with pytest.raises(ValueError, match=expected_message):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_legacy_usage_log_is_migrated_to_complete_audit_evidence():
    legacy = v2_snapshot_with_usage_log()
    legacy.pop("snapshot_version")
    usage_logs = legacy["usage_logs"]
    assert isinstance(usage_logs, list)
    legacy_log = usage_logs[0]
    for field_name in [
        "trace_id",
        "context",
        "candidate_memory_statuses",
        "system_blocked_reasons",
    ]:
        legacy_log.pop(field_name)

    restored = TraceBackedMemoryStore.from_snapshot(legacy)
    migrated_log = restored.usage_logs[0]

    assert migrated_log.trace_id == "trace_contract"
    assert migrated_log.context == {
        "mode": "repair",
        "repo": "repo",
        "tenant": "tenant_a",
        "commit_sha": "abc",
    }
    assert migrated_log.candidate_memory_statuses == {"lesson_001": "active"}
    assert migrated_log.system_blocked_reasons == {}
    metrics = restored.metrics()
    assert metrics.evaluated_with_memory_count == 1
    assert metrics.evaluated_without_memory_count == 0
    assert metrics.unevaluated_decision_count == 0
    assert metrics.pass_rate_with_memory == 1.0


@pytest.mark.parametrize(
    ("used_memory", "include_eval_result", "eval_result"),
    [
        (True, True, "unknown"),
        (True, False, None),
        (False, True, "unknown"),
        (False, False, None),
    ],
    ids=["with-unknown", "with-missing", "without-unknown", "without-missing"],
)
def test_legacy_usage_log_metrics_keep_unknown_and_missing_outcomes_unevaluated(
    used_memory: bool,
    include_eval_result: bool,
    eval_result: str | None,
):
    legacy = v2_snapshot_with_usage_log()
    legacy.pop("snapshot_version")
    legacy_log = _snapshot_record(legacy, "usage_logs")
    for field_name in (
        "trace_id",
        "context",
        "candidate_memory_statuses",
        "system_blocked_reasons",
    ):
        legacy_log.pop(field_name)
    if not used_memory:
        legacy_log["used_memory_ids"] = []
        legacy_log["recommended_injection"] = "none"
    if include_eval_result:
        legacy_log["eval_result"] = eval_result
    else:
        legacy_log.pop("eval_result")

    restored = TraceBackedMemoryStore.from_snapshot(legacy)
    metrics = restored.metrics()

    assert metrics.decision_count == 1
    assert metrics.evaluated_with_memory_count == 0
    assert metrics.evaluated_without_memory_count == 0
    assert metrics.unevaluated_decision_count == 1
    assert metrics.pass_rate_with_memory is None
    assert metrics.pass_rate_without_memory is None
    per_memory = {
        item.memory_id: item
        for item in restored.memory_outcome_metrics()
    }
    lesson_metrics = per_memory["lesson_001"]
    assert lesson_metrics.candidate_count == 1
    assert lesson_metrics.used_count == int(used_memory)
    assert lesson_metrics.evaluated_use_count == 0
    assert lesson_metrics.unevaluated_use_count == int(used_memory)
    assert lesson_metrics.observed_pass_rate is None
    assert per_memory["case_contract"].used_count == 0


def test_legacy_usage_log_prefers_valid_supplied_trace_id_over_ambiguous_run_id():
    legacy = v2_snapshot_with_usage_log()
    legacy.pop("snapshot_version")
    traces = legacy["traces"]
    assert isinstance(traces, list)
    duplicate_trace = deepcopy(traces[0])
    duplicate_trace["trace_id"] = "trace_duplicate"
    traces.append(duplicate_trace)
    usage_logs = legacy["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0].pop("context")
    usage_logs[0].pop("candidate_memory_statuses")
    usage_logs[0].pop("system_blocked_reasons")

    restored = TraceBackedMemoryStore.from_snapshot(legacy)

    assert restored.usage_logs[0].trace_id == "trace_contract"


def test_legacy_usage_log_preserves_supplied_decision_time_evidence_after_obsoletion():
    legacy = v2_snapshot_with_usage_log()
    legacy.pop("snapshot_version")
    lessons = legacy["lessons"]
    assert isinstance(lessons, list)
    lessons[0]["status"] = "obsolete"
    usage_logs = legacy["usage_logs"]
    assert isinstance(usage_logs, list)
    supplied_context = deepcopy(usage_logs[0]["context"])
    usage_logs[0]["candidate_memory_statuses"] = {"lesson_001": "active"}
    usage_logs[0]["system_blocked_reasons"] = {
        "lesson_001": "captured at decision time"
    }

    restored = TraceBackedMemoryStore.from_snapshot(legacy)
    migrated_log = restored.usage_logs[0]

    assert migrated_log.context == supplied_context
    assert migrated_log.candidate_memory_statuses == {"lesson_001": "active"}
    assert migrated_log.system_blocked_reasons == {
        "lesson_001": "captured at decision time"
    }
    assert restored.metrics().obsolete_memory_usage_attempts == 0


def test_legacy_usage_log_rejects_invalid_supplied_trace_id_without_fallback():
    legacy = v2_snapshot_with_usage_log()
    legacy.pop("snapshot_version")
    usage_logs = legacy["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0]["trace_id"] = "missing_trace"
    usage_logs[0].pop("context")
    usage_logs[0].pop("candidate_memory_statuses")
    usage_logs[0].pop("system_blocked_reasons")

    with pytest.raises(ValueError, match="unknown trace_id: missing_trace"):
        TraceBackedMemoryStore.from_snapshot(legacy)


@pytest.mark.parametrize("ambiguous", [False, True], ids=["missing", "ambiguous"])
def test_legacy_usage_log_migration_rejects_unresolvable_run_id(ambiguous: bool):
    legacy = v2_snapshot_with_usage_log()
    legacy.pop("snapshot_version")
    usage_logs = legacy["usage_logs"]
    assert isinstance(usage_logs, list)
    for field_name in [
        "trace_id",
        "context",
        "candidate_memory_statuses",
        "system_blocked_reasons",
    ]:
        usage_logs[0].pop(field_name)

    traces = legacy["traces"]
    assert isinstance(traces, list)
    if ambiguous:
        duplicate_trace = deepcopy(traces[0])
        duplicate_trace["trace_id"] = "trace_duplicate"
        traces.append(duplicate_trace)
    else:
        legacy["traces"] = []
        legacy["failure_cases"] = []
        legacy["lessons"] = []

    with pytest.raises(ValueError, match="run_id"):
        TraceBackedMemoryStore.from_snapshot(legacy)


def test_equivalent_stores_emit_identical_snapshot_json(tmp_path):
    first = store_with_records_in_order(["trace_b", "trace_a"])
    second = store_with_records_in_order(["trace_a", "trace_b"])
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    first.save_json(first_path)
    second.save_json(second_path)

    assert first_path.read_bytes() == second_path.read_bytes()


def test_reversed_v2_collections_emit_identical_snapshot_json(tmp_path):
    snapshot = fully_populated_snapshot()
    reversed_snapshot = deepcopy(snapshot)
    collection_names = [
        "traces",
        "failure_cases",
        "lessons",
        "project_policies",
        "usage_logs",
    ]

    for collection_name in collection_names:
        records = reversed_snapshot[collection_name]
        assert isinstance(records, list)
        assert len(records) >= 2
        records.reverse()

    first = TraceBackedMemoryStore.from_snapshot(snapshot)
    second = TraceBackedMemoryStore.from_snapshot(reversed_snapshot)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    first.save_json(first_path)
    second.save_json(second_path)

    assert first_path.read_bytes() == second_path.read_bytes()
    assert [log["decision_id"] for log in second.to_snapshot()["usage_logs"]] == [
        "decision_000001",
        "decision_000002",
    ]


def test_retrieval_prompts_and_reports_are_independent_of_insertion_order():
    first = store_with_retrieval_records_in_order(["b", "a"])
    second = store_with_retrieval_records_in_order(["a", "b"])
    context = MemoryContext(
        mode="debug",
        repo="repo",
        commit_sha="current",
        tenant="tenant",
        failure_type="invalid_tool_argument",
    )

    first_candidates = first.candidate_memories(context)
    second_candidates = second.candidate_memories(context)
    first_ids = [memory.memory_id for memory in first_candidates]
    second_ids = [memory.memory_id for memory in second_candidates]

    assert first_ids == second_ids == sorted(first_ids)
    assert first.prepare_memory(context, task="repair").prompt == second.prepare_memory(
        context, task="repair"
    ).prompt

    first_report = first.pr_memory_report(
        context, changed_fields=["model", "prompt_version"]
    )
    second_report = second.pr_memory_report(
        context, changed_fields=["model", "prompt_version"]
    )
    assert first_report == second_report
    assert first_report.related_case_ids == ["case_a", "case_b"]
    assert [
        provenance.case_id for provenance in first_report.related_case_provenance
    ] == ["case_a", "case_b"]


def test_save_json_uses_sibling_replace(monkeypatch, tmp_path):
    calls = []
    real_replace = os.replace

    def recording_replace(source, target):
        calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", recording_replace)
    target = tmp_path / "snapshot.json"

    TraceBackedMemoryStore().save_json(target)

    assert calls[0][1] == target
    assert calls[0][0].parent == target.parent


def test_save_json_syncs_temporary_file_before_replace(monkeypatch, tmp_path):
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(file_descriptor):
        events.append("fsync")
        real_fsync(file_descriptor)

    def recording_replace(source, target):
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    TraceBackedMemoryStore().save_json(tmp_path / "snapshot.json")

    assert events == ["fsync", "replace"]


def test_store_text_persistence_uses_canonical_lf_bytes(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"
    lessons_path = tmp_path / "lessons.yaml"
    store = TraceBackedMemoryStore()

    store.save_json(snapshot_path)
    store.save_lessons_yaml(lessons_path)

    assert b"\r\n" not in snapshot_path.read_bytes()
    assert b"\r\n" not in lessons_path.read_bytes()
    assert snapshot_path.read_bytes().endswith(b"\n")
    assert lessons_path.read_bytes() == b"lessons: []\n"


def test_save_json_cleans_temporary_sibling_when_replace_fails(monkeypatch, tmp_path):
    target = tmp_path / "snapshot.json"
    target.write_text("existing snapshot\n", encoding="utf-8")

    def failing_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        TraceBackedMemoryStore().save_json(target)

    assert target.read_text(encoding="utf-8") == "existing snapshot\n"
    assert list(tmp_path.glob(".snapshot.json.*.tmp")) == []


@pytest.mark.parametrize("cost_usd", [float("nan"), float("inf"), float("-inf")])
def test_save_json_rejects_non_finite_numbers_atomically(tmp_path, cost_usd: float):
    target = tmp_path / "snapshot.json"
    target.write_text("existing snapshot\n", encoding="utf-8")
    store = TraceBackedMemoryStore()
    store._traces["trace_non_finite"] = Trace(
        trace_id="trace_non_finite",
        run_id="run_non_finite",
        commit_sha="abc",
        cost_usd=cost_usd,
    )

    with pytest.raises(ValueError, match="JSON"):
        store.save_json(target)

    assert target.read_text(encoding="utf-8") == "existing snapshot\n"
    assert list(tmp_path.glob(".snapshot.json.*.tmp")) == []


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_load_json_rejects_non_standard_numeric_constants(tmp_path, constant: str):
    snapshot_path = tmp_path / "non-standard-number.json"
    snapshot_path.write_text(
        """
        {
          "snapshot_version": 2,
          "traces": [
            {
              "trace_id": "trace_non_finite",
              "run_id": "run_non_finite",
              "commit_sha": "abc",
              "cost_usd": %s
            }
          ],
          "failure_cases": [],
          "lessons": [],
          "project_policies": [],
          "usage_logs": []
        }
        """ % constant,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid JSON constant"):
        TraceBackedMemoryStore.load_json(snapshot_path)


def test_snapshot_rejects_string_boolean_safety_fields():
    snapshot = valid_snapshot_dict()
    snapshot["lessons"][0]["sensitive"] = "false"
    with pytest.raises(ValueError, match="sensitive must be a boolean"):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_store_rejects_lesson_that_omits_source_tenant_scope():
    store, _trace, case = store_with_verified_case(repo="repo", tenant="tenant_a")
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="rule",
        memory_type="procedural",
        scope={"repo": "repo"},
    )
    with pytest.raises(ValueError, match="source tenant"):
        store.add_lesson(lesson)


@pytest.mark.parametrize(
    "scope",
    [
        {"tenant": "tenant_a"},
        {"repo": "other_repo", "tenant": "tenant_a"},
    ],
    ids=["omits_source_repo", "mismatches_source_repo"],
)
def test_store_rejects_lesson_with_invalid_source_repo_scope(scope: dict[str, str]):
    store, _trace, case = store_with_verified_case(repo="repo", tenant="tenant_a")
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="rule",
        memory_type="procedural",
        scope=scope,
    )

    with pytest.raises(ValueError, match="source repo"):
        store.add_lesson(lesson)


@pytest.mark.parametrize("invalid_boolean", ["false", 1])
def test_store_rejects_non_boolean_trace_dirty_exact_boolean(invalid_boolean: object):
    store = TraceBackedMemoryStore()

    with pytest.raises(ValueError, match="dirty must be a boolean"):
        store.record_trace(
            Trace(
                trace_id="trace_001",
                run_id="run_001",
                commit_sha="abc",
                dirty=invalid_boolean,  # type: ignore[arg-type]
            )
        )


@pytest.mark.parametrize("invalid_boolean", ["false", 1])
def test_store_rejects_non_boolean_regression_passed_exact_boolean(invalid_boolean: object):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc"))
    case = FailureCase(
        case_id="case_001",
        source_trace_id=trace.trace_id,
        commit_sha=trace.commit_sha,
        failure_type="invalid_tool_argument",
        symptom="bad query",
        regression_passed=invalid_boolean,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="regression_passed must be a boolean"):
        store.add_failure_case(case)


@pytest.mark.parametrize("invalid_boolean", ["false", 1])
def test_store_rejects_non_boolean_lesson_eval_leaking_exact_boolean(invalid_boolean: object):
    store, _trace, case = store_with_verified_case()
    lesson = Lesson(
        lesson_id="lesson_001",
        source_case_id=case.case_id,
        lesson_text="rule",
        memory_type="procedural",
        scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
        eval_leaking=invalid_boolean,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="eval_leaking must be a boolean"):
        store.add_lesson(lesson)


@pytest.mark.parametrize("field_name", ["sensitive", "eval_leaking"])
@pytest.mark.parametrize("invalid_boolean", ["false", 1])
def test_store_rejects_non_boolean_project_policy_safety_flags_exact_boolean(
    field_name: str, invalid_boolean: object
):
    store = TraceBackedMemoryStore()
    policy = ProjectPolicy(
        policy_id="policy_001",
        policy_text="rule",
        scope={"tool": "search_docs"},
        **{field_name: invalid_boolean},  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match=f"{field_name} must be a boolean"):
        store.add_project_policy(policy)


@pytest.mark.parametrize("invalid_boolean", ["false", 1])
def test_store_rejects_non_boolean_memory_caused_failure_exact_boolean(invalid_boolean: object):
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0]["memory_caused_failure"] = invalid_boolean

    with pytest.raises(ValueError, match="memory_caused_failure must be a boolean"):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_store_retrieves_by_metadata_then_logs_usage_decision():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"repo": "agent-harness", "tool": "search_docs"},
        )
    )

    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    candidates = store.candidate_memories(context)
    decision = MemoryDecision(
        use_memory=True,
        allowed_memory_ids=["lesson_001"],
        blocked_memory_ids=[],
        reason="directly relevant",
        risk="low",
        recommended_injection="short_summary",
    )

    log = store.log_decision("run_001", context, [m.memory_id for m in candidates], decision)

    assert [m.memory_id for m in candidates] == ["lesson_001"]
    assert log.used_memory_ids == ["lesson_001"]
    assert log.candidate_memory_ids == ["lesson_001"]
    assert store.usage_logs == [log]


def test_low_level_logging_reapplies_system_gate_before_persisting_usage():
    store, trace, case = store_with_verified_case()
    lesson = store.add_lesson(
        Lesson(
            lesson_id="lesson_sensitive",
            source_case_id=case.case_id,
            lesson_text="private rule",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a"},
            sensitive=True,
        )
    )
    context = matching_context(trace)

    log = store.log_decision(
        trace.run_id,
        context,
        [lesson.lesson_id],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=[lesson.lesson_id],
            blocked_memory_ids=[],
            reason="caller attempted use",
            risk="low",
            recommended_injection="short_summary",
        ),
    )

    assert log.used_memory_ids == []
    assert log.blocked_memory_ids == [lesson.lesson_id]
    assert log.recommended_injection == "none"
    assert log.system_blocked_reasons == {
        lesson.lesson_id: "memory is marked sensitive"
    }


def test_low_level_logging_requires_nonblank_audit_reason():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_reason",
            run_id="run_reason",
            commit_sha="abc",
            repo="repo",
        )
    )
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc")

    with pytest.raises(ValueError, match="reason must be nonblank"):
        store.log_decision(
            trace.run_id,
            context,
            [],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[],
                reason="   ",
                risk="none",
                recommended_injection="none",
            ),
        )


def test_v2_snapshot_rejects_blank_usage_log_reason():
    snapshot = v2_snapshot_with_usage_log()
    _snapshot_record(snapshot, "usage_logs")["reason"] = "\t \n"

    with pytest.raises(ValueError, match="reason must be nonblank"):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_store_rejects_duplicate_trace_ids():
    store = TraceBackedMemoryStore()
    trace = Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123")

    store.record_trace(trace)

    try:
        store.record_trace(trace)
    except ValueError as exc:
        assert "duplicate trace_id" in str(exc)
    else:
        raise AssertionError("duplicate traces must be rejected")


def test_store_rejects_invalid_trace_records():
    invalid_traces = [
        Trace(trace_id="", run_id="run_001", commit_sha="abc123"),
        Trace(trace_id="trace_001", run_id="", commit_sha="abc123"),
        Trace(trace_id="trace_001", run_id="run_001", commit_sha=""),
        Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="skipped"),
    ]

    for trace in invalid_traces:
        store = TraceBackedMemoryStore()
        try:
            store.record_trace(trace)
        except ValueError as exc:
            assert "trace" in str(exc) or "eval_result" in str(exc)
        else:
            raise AssertionError("invalid trace records must be rejected")


def test_store_rejects_trace_records_with_invalid_json_shapes():
    invalid_traces = [
        Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", retrieved_context={"doc": "one"}),
        Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", tool_calls=["search_docs"]),
        Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", tool_outputs=["result"]),
    ]

    for trace in invalid_traces:
        store = TraceBackedMemoryStore()
        try:
            store.record_trace(trace)
        except ValueError as exc:
            assert "list of JSON objects" in str(exc)
        else:
            raise AssertionError("trace JSON-like collections must be list[dict]")


def test_store_rejects_failure_case_without_stored_source_trace():
    store = TraceBackedMemoryStore()
    trace = Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail")
    case = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
    )

    try:
        store.add_failure_case(case)
    except ValueError as exc:
        assert "source_trace_id" in str(exc)
    else:
        raise AssertionError("failure cases must require a stored source trace")


def test_store_rejects_failure_case_with_mismatched_source_commit():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = FailureCase(
        case_id="case_001",
        source_trace_id=trace.trace_id,
        commit_sha="different_commit",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
    )

    try:
        store.add_failure_case(case)
    except ValueError as exc:
        assert "commit_sha" in str(exc)
    else:
        raise AssertionError("failure cases must match their source trace commit")


def test_store_rejects_invalid_failure_case_records():
    invalid_cases = [
        FailureCase(
            case_id="",
            source_trace_id="trace_001",
            commit_sha="abc123",
            failure_type="invalid_tool_argument",
            symptom="bad query",
        ),
        FailureCase(
            case_id="case_001",
            source_trace_id="trace_001",
            commit_sha="abc123",
            failure_type="",
            symptom="bad query",
        ),
        FailureCase(
            case_id="case_001",
            source_trace_id="trace_001",
            commit_sha="abc123",
            failure_type="invalid_tool_argument",
            symptom="",
        ),
        FailureCase(
            case_id="case_001",
            source_trace_id="trace_001",
            commit_sha="abc123",
            failure_type="invalid_tool_argument",
            symptom="bad query",
            status="unknown",
        ),
        FailureCase(
            case_id="case_001",
            source_trace_id="trace_001",
            commit_sha="abc123",
            failure_type="invalid_tool_argument",
            symptom="bad query",
            status="verified",
            regression_passed=False,
        ),
    ]

    for case in invalid_cases:
        store = TraceBackedMemoryStore()
        store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
        try:
            store.add_failure_case(case)
        except ValueError as exc:
            assert "failure case" in str(exc) or "verified" in str(exc)
        else:
            raise AssertionError("invalid failure case records must be rejected")


def test_candidate_memories_uses_metadata_filter_before_gate():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="matching_lesson",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    )
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="unrelated_lesson",
            lesson_text="Use the invoice parser.",
            memory_type="procedural",
            scope={"tool": "parse_invoice"},
        )
    )

    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    assert [memory.memory_id for memory in store.candidate_memories(context)] == ["matching_lesson"]


def test_candidate_commit_anchors_are_sorted_and_exclude_project_policies():
    store = store_with_retrieval_records_in_order(["b", "a"])

    assert store.candidate_commit_anchors(ancestry_context()) == (
        "commit_a",
        "commit_b",
        "fix_commit_a",
        "fix_commit_b",
    )


def test_pr_report_commit_anchors_are_sorted():
    store = store_with_retrieval_records_in_order(["b", "a"])

    assert store.pr_report_commit_anchors(ancestry_context()) == (
        "commit_a",
        "commit_b",
    )


def test_pr_change_set_is_frozen_and_package_exported():
    change_set = PRChangeSet((("model", "gpt-old", "gpt-new"),))
    package = __import__("trace_backed_memory")

    assert package.PRChangeEndpoint is PRChangeEndpoint
    assert package.PRChangeSet is PRChangeSet
    assert "PRChangeEndpoint" in package.__all__
    assert "PRChangeSet" in package.__all__
    with pytest.raises(FrozenInstanceError):
        change_set.field_changes = ()  # type: ignore[misc]


def test_legacy_pr_case_provenance_defaults_to_no_change_endpoint():
    provenance = PRCaseProvenance(
        case_id="case",
        source_trace_id="trace",
        commit_sha="commit",
        fix_commit_sha=None,
        trace_uri=None,
        failure_type="tool_error",
    )

    assert provenance.matched_change_endpoint is None


@pytest.fixture(
    params=("pr_report_commit_anchors", "pr_memory_report"),
    ids=("commit-anchors", "memory-report"),
)
def pr_change_set_boundary(request):
    def invoke(
        store: TraceBackedMemoryStore,
        context: MemoryContext,
        change_set: object,
    ):
        boundary = getattr(store, request.param)
        return boundary(context, change_set=change_set)

    return invoke


def prevent_pr_case_scan(monkeypatch: pytest.MonkeyPatch, store: TraceBackedMemoryStore):
    def fail_scan(*_args, **_kwargs):
        raise AssertionError("PR case scan ran before change-set validation")

    monkeypatch.setattr(store, "_pr_related_case_records", fail_scan)


@pytest.mark.parametrize(
    ("change_set", "message"),
    [
        (object(), "change_set must be a PRChangeSet"),
        (PRChangeSet([]), "change_set.field_changes must be a non-empty tuple"),
        (PRChangeSet(()), "change_set.field_changes must be a non-empty tuple"),
        (PRChangeSet((("model", "old"),)), "change_set entries must be 3-item tuples"),
        (PRChangeSet(("model",)), "change_set entries must be 3-item tuples"),
        (
            PRChangeSet((["model", "old", "new"],)),
            "change_set entries must be 3-item tuples",
        ),
        (
            PRChangeSet(((1, "old", "new"),)),
            "change_set field names must be strings",
        ),
        (
            PRChangeSet((("model", "same", "same"),)),
            "change_set model old and new values must differ",
        ),
    ],
)
def test_pr_change_set_boundaries_reject_malformed_shape_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
    pr_change_set_boundary,
    change_set: object,
    message: str,
):
    store = TraceBackedMemoryStore()
    prevent_pr_case_scan(monkeypatch, store)
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc", model="new")

    with pytest.raises(ValueError, match=message):
        pr_change_set_boundary(store, context, change_set)


@pytest.mark.parametrize("endpoint_index", [1, 2], ids=("old", "new"))
@pytest.mark.parametrize(
    ("invalid_endpoint", "message"),
    [
        (True, "change_set model endpoint values must be None or strings"),
        (False, "change_set model endpoint values must be None or strings"),
        (1, "change_set model endpoint values must be None or strings"),
        (1.5, "change_set model endpoint values must be None or strings"),
        ([], "change_set model endpoint values must be None or strings"),
        ({}, "change_set model endpoint values must be None or strings"),
        ((), "change_set model endpoint values must be None or strings"),
        (b"new", "change_set model endpoint values must be None or strings"),
        (
            "",
            "change_set model endpoint values must be non-empty, non-whitespace strings or None",
        ),
        (
            "   ",
            "change_set model endpoint values must be non-empty, non-whitespace strings or None",
        ),
        (
            "x" * (METADATA_VALUE_MAX_CHARS + 1),
            f"change_set model endpoint values must be at most {METADATA_VALUE_MAX_CHARS} characters",
        ),
    ],
)
def test_pr_change_set_boundaries_reject_invalid_endpoints_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
    pr_change_set_boundary,
    endpoint_index: int,
    invalid_endpoint: object,
    message: str,
):
    store = TraceBackedMemoryStore()
    prevent_pr_case_scan(monkeypatch, store)
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc", model="new")
    endpoints: list[object] = ["model", "old", "new"]
    endpoints[endpoint_index] = invalid_endpoint

    with pytest.raises(ValueError, match=message):
        pr_change_set_boundary(
            store,
            context,
            PRChangeSet((tuple(endpoints),)),  # type: ignore[arg-type]
        )


def test_pr_change_set_boundaries_reject_duplicate_fields_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
    pr_change_set_boundary,
):
    store = TraceBackedMemoryStore()
    prevent_pr_case_scan(monkeypatch, store)
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc", model="new")
    change_set = PRChangeSet(
        (("model", "old", "new"), ("model", "older", "new"))
    )

    with pytest.raises(ValueError, match="duplicate change_set fields: model"):
        pr_change_set_boundary(store, context, change_set)


@pytest.mark.parametrize("new_value", ["other", None], ids=("different", "none"))
def test_pr_change_set_boundaries_reject_incorrect_context_binding_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
    pr_change_set_boundary,
    new_value: str | None,
):
    store = TraceBackedMemoryStore()
    prevent_pr_case_scan(monkeypatch, store)
    context = MemoryContext(
        mode="repair", repo="repo", commit_sha="abc", model="gpt-new"
    )

    with pytest.raises(
        ValueError, match="change_set model new value must match context"
    ):
        pr_change_set_boundary(
            store,
            context,
            PRChangeSet((("model", "gpt-old", new_value),)),
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "model_family",
        "repo",
        "tenant",
        "branch",
        "commit_sha",
        "arbitrary_unknown",
    ],
)
def test_pr_change_set_boundaries_reject_every_unsupported_field_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
    pr_change_set_boundary,
    field_name: str,
):
    store = TraceBackedMemoryStore()
    prevent_pr_case_scan(monkeypatch, store)
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc")

    with pytest.raises(
        ValueError, match=f"unsupported change_set fields: {field_name}"
    ):
        pr_change_set_boundary(
            store,
            context,
            PRChangeSet(((field_name, "old", "new"),)),
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "prompt_version",
        "prompt_family",
        "tool",
        "tool_schema_version",
        "model",
        "eval_suite",
    ],
)
def test_pr_change_set_boundaries_accept_every_supported_field_bound_to_context(
    pr_change_set_boundary,
    field_name: str,
):
    store = TraceBackedMemoryStore()
    context = MemoryContext(
        mode="repair",
        repo="repo",
        commit_sha="abc",
        **{field_name: "new"},
    )

    pr_change_set_boundary(
        store,
        context,
        PRChangeSet(((field_name, "old", "new"),)),
    )


@pytest.mark.parametrize(
    ("old_value", "new_value", "context_model"),
    [(None, "gpt-new", "gpt-new"), ("gpt-old", None, None)],
)
def test_pr_change_set_boundaries_accept_none_endpoint_context_bindings(
    pr_change_set_boundary,
    old_value: str | None,
    new_value: str | None,
    context_model: str | None,
):
    context = MemoryContext(
        mode="repair", repo="repo", commit_sha="abc", model=context_model
    )

    pr_change_set_boundary(
        TraceBackedMemoryStore(),
        context,
        PRChangeSet((("model", old_value, new_value),)),
    )


def test_pr_change_set_validation_returns_entries_sorted_by_field_name():
    context = MemoryContext(
        mode="repair",
        repo="repo",
        commit_sha="abc",
        prompt_version="prompt-new",
        model="model-new",
    )
    change_set = PRChangeSet(
        (
            ("prompt_version", "prompt-old", "prompt-new"),
            ("model", "model-old", "model-new"),
        )
    )

    assert store_module._validated_pr_change_set(context, change_set) == (
        ("model", "model-old", "model-new"),
        ("prompt_version", "prompt-old", "prompt-new"),
    )


def store_with_pr_tool_name_case(
    raw_name: object,
    *,
    model: str | None = None,
) -> TraceBackedMemoryStore:
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_tool_name",
            run_id="run_tool_name",
            commit_sha="commit_tool_name",
            repo="repo",
            tenant="tenant",
            model=model,
            eval_result="fail",
            tool_calls=[{"name": raw_name}],
        )
    )
    store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                trace,
                case_id="case_tool_name",
                failure_type="tool_error",
                symptom="tool name matching failure",
            ),
            fix="match exact raw string names",
            fix_commit_sha="fix_tool_name",
            regression_passed=True,
        )
    )
    return store


def test_pr_change_set_tool_endpoint_does_not_coerce_raw_non_string_names():
    store = store_with_pr_tool_name_case(7)
    context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        tool="7",
        failure_type="tool_error",
    )
    change_set = PRChangeSet((("tool", "old_tool", "7"),))

    assert store.pr_report_commit_anchors(context, change_set=change_set) == ()
    assert store.pr_memory_report(context, change_set=change_set).related_case_ids == []


def test_pr_change_set_unchanged_tool_context_does_not_coerce_raw_names():
    store = store_with_pr_tool_name_case(7, model="new")
    context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        tool="7",
        model="new",
        failure_type="tool_error",
    )

    change_set_report = store.pr_memory_report(
        context,
        change_set=PRChangeSet((("model", "old", "new"),)),
    )
    legacy_report = store.pr_memory_report(context, changed_fields=["model"])

    assert change_set_report.related_case_ids == []
    assert legacy_report.related_case_ids == ["case_tool_name"]


def test_pr_change_set_exact_string_tool_names_still_match():
    store = store_with_pr_tool_name_case("7")
    context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        tool="7",
        failure_type="tool_error",
    )

    report = store.pr_memory_report(
        context,
        change_set=PRChangeSet((("tool", "old_tool", "7"),)),
    )

    assert report.related_case_ids == ["case_tool_name"]
    assert report.related_case_provenance[0].matched_change_endpoint == "new"


def test_pr_change_set_matches_complete_endpoints_and_reports_provenance():
    store = TraceBackedMemoryStore()

    def add_case(
        suffix: str,
        *,
        prompt_version: str,
        model: str,
        eval_suite: str = "suite",
        repo: str = "repo",
        tenant: str = "tenant",
        failure_type: str = "tool_error",
    ) -> None:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_{suffix}",
                run_id=f"run_{suffix}",
                commit_sha=f"commit_{suffix}",
                repo=repo,
                tenant=tenant,
                prompt_version=prompt_version,
                model=model,
                eval_suite=eval_suite,
                eval_result="fail",
            )
        )
        store.add_failure_case(
            verify_failure_case(
                draft_failure_case(
                    trace,
                    case_id=f"case_{suffix}",
                    failure_type=failure_type,
                    symptom=f"symptom {suffix}",
                ),
                fix=f"fix {suffix}",
                fix_commit_sha=f"fix_{suffix}",
                regression_passed=True,
            )
        )

    add_case("old", prompt_version="prompt-old", model="model-old")
    add_case("new", prompt_version="prompt-new", model="model-new")
    add_case("old_prompt_new_model", prompt_version="prompt-old", model="model-new")
    add_case("new_prompt_old_model", prompt_version="prompt-new", model="model-old")
    add_case("unrelated", prompt_version="other", model="other")
    add_case("other_eval", prompt_version="prompt-new", model="model-new", eval_suite="other")
    add_case("other_repo", prompt_version="prompt-new", model="model-new", repo="other")
    add_case("other_tenant", prompt_version="prompt-new", model="model-new", tenant="other")
    add_case("other_failure", prompt_version="prompt-new", model="model-new", failure_type="timeout")

    context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        prompt_version="prompt-new",
        model="model-new",
        eval_suite="suite",
        failure_type="tool_error",
    )
    change_set = PRChangeSet(
        (
            ("prompt_version", "prompt-old", "prompt-new"),
            ("model", "model-old", "model-new"),
        )
    )

    report = store.pr_memory_report(context, change_set=change_set)
    reversed_report = store.pr_memory_report(
        context, change_set=PRChangeSet(tuple(reversed(change_set.field_changes)))
    )

    assert report == reversed_report
    assert report.related_case_ids == ["case_new", "case_old"]
    assert report.warnings == [
        "model change touches known failure case case_new for known failure area.",
        "prompt_version change touches known failure case case_new for known failure area.",
        "model change touches known failure case case_old for known failure area.",
        "prompt_version change touches known failure case case_old for known failure area.",
    ]
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in report.related_case_provenance
    ] == [("case_new", "new"), ("case_old", "old")]


def test_pr_change_set_anchors_and_ancestry_use_only_endpoint_cases():
    def store_with_endpoint_cases(order: list[str]) -> TraceBackedMemoryStore:
        store = TraceBackedMemoryStore()
        endpoint_values = {
            "old": ("prompt-old", "model-old", "old_endpoint_error"),
            "new": ("prompt-new", "model-new", "new_endpoint_error"),
            "mixed": ("prompt-old", "model-new", "mixed_endpoint_error"),
            "unrelated": ("other", "other", "unrelated_endpoint_error"),
        }
        for suffix in order:
            prompt_version, model, failure_type = endpoint_values[suffix]
            trace = store.record_trace(
                Trace(
                    trace_id=f"trace_{suffix}",
                    run_id=f"run_{suffix}",
                    commit_sha=f"commit_{suffix}",
                    repo="repo",
                    tenant="tenant",
                    prompt_version=prompt_version,
                    model=model,
                    eval_suite="suite",
                    eval_result="fail",
                )
            )
            store.add_failure_case(
                verify_failure_case(
                    draft_failure_case(
                        trace,
                        case_id=f"case_{suffix}",
                        failure_type=failure_type,
                        symptom=f"symptom {suffix}",
                    ),
                    fix=f"fix {suffix}",
                    fix_commit_sha=f"fix_{suffix}",
                    regression_passed=True,
                )
            )
        return store

    context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        prompt_version="prompt-new",
        model="model-new",
        eval_suite="suite",
        failure_type=None,
    )
    change_set = PRChangeSet(
        (
            ("prompt_version", "prompt-old", "prompt-new"),
            ("model", "model-old", "model-new"),
        )
    )
    new_suggestion = (
        "Run new_endpoint_error regression for tool affected tool before merging."
    )
    old_suggestion = (
        "Run old_endpoint_error regression for tool affected tool before merging."
    )
    store = store_with_endpoint_cases(["unrelated", "new", "mixed", "old"])

    assert store.pr_report_commit_anchors(context, change_set=change_set) == (
        "commit_new",
        "commit_old",
    )
    assert store.pr_report_commit_anchors(context) == ("commit_new",)
    reverse_order_anchors = store_with_endpoint_cases(
        ["old", "mixed", "new", "unrelated"]
    ).pr_report_commit_anchors(
        context,
        change_set=PRChangeSet(tuple(reversed(change_set.field_changes))),
    )
    assert reverse_order_anchors == ("commit_new", "commit_old")

    with pytest.raises(
        ValueError,
        match="commit ancestry evidence is missing anchors: commit_new, commit_old",
    ):
        store.pr_memory_report(
            context,
            change_set=change_set,
            commit_ancestry=ancestry_evidence("current"),
        )

    report = store.pr_memory_report(
        context,
        change_set=change_set,
        commit_ancestry=ancestry_evidence(
            "current",
            commit_new=True,
            commit_old=False,
        ),
    )

    assert report.related_case_ids == ["case_new"]
    assert report.suggested_regression_tests == [new_suggestion]
    assert old_suggestion not in report.suggested_regression_tests
    assert report.warnings == [
        "model change touches known failure case case_new for known failure area.",
        "prompt_version change touches known failure case case_new for known failure area.",
    ]
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in report.related_case_provenance
    ] == [("case_new", "new")]

    unfiltered_report = store.pr_memory_report(context, change_set=change_set)
    assert unfiltered_report.related_case_ids == ["case_new", "case_old"]
    assert unfiltered_report.suggested_regression_tests == [
        new_suggestion,
        old_suggestion,
    ]
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in unfiltered_report.related_case_provenance
    ] == [("case_new", "new"), ("case_old", "old")]

    legacy_report = store.pr_memory_report(
        context,
        changed_fields=["model"],
        commit_ancestry=ancestry_evidence("current", commit_new=True),
    )
    assert legacy_report.related_case_ids == ["case_new"]
    assert legacy_report.related_case_provenance[0].matched_change_endpoint is None


def test_pr_change_set_report_does_not_persist_endpoint_metadata():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_old_endpoint",
            run_id="run_old_endpoint",
            commit_sha="commit-old",
            repo="repo",
            tenant="tenant",
            prompt_version="prompt-old",
            model="model-old",
            eval_result="fail",
        )
    )
    store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                trace,
                case_id="case_old_endpoint",
                failure_type="invalid_tool_argument",
                symptom="old endpoint regression",
            ),
            fix="update prompt",
            fix_commit_sha="fix-old",
            regression_passed=True,
        )
    )
    context = MemoryContext(
        mode="repair",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        prompt_version="prompt-new",
        model="model-new",
        failure_type="invalid_tool_argument",
    )
    change_set = PRChangeSet(
        (
            ("prompt_version", "prompt-old", "prompt-new"),
            ("model", "model-old", "model-new"),
        )
    )
    before = store.to_snapshot()
    before_json = json.dumps(before, sort_keys=True)

    assert TraceBackedMemoryStore.from_snapshot(before).to_snapshot() == before

    report = store.pr_memory_report(context, change_set=change_set)
    after = store.to_snapshot()
    after_json = json.dumps(after, sort_keys=True)

    assert report.related_case_provenance[0].matched_change_endpoint == "old"
    assert after == before
    assert TraceBackedMemoryStore.from_snapshot(after).to_snapshot() == after
    for report_only_field in (
        '"matched_change_endpoint"',
        '"change_set"',
        '"field_changes"',
    ):
        assert report_only_field not in before_json
        assert report_only_field not in after_json


def test_pr_change_set_matches_optional_and_tool_endpoints():
    store = TraceBackedMemoryStore()

    def add_case(
        suffix: str,
        *,
        model: str | None = None,
        tool_calls: list[dict[str, object]] | None = None,
        eval_suite: str,
    ) -> None:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_{suffix}",
                run_id=f"run_{suffix}",
                commit_sha=f"commit_{suffix}",
                repo="repo",
                tenant="tenant",
                prompt_version="prompt",
                model=model,
                eval_suite=eval_suite,
                eval_result="fail",
                tool_calls=tool_calls or [],
            )
        )
        store.add_failure_case(
            verify_failure_case(
                draft_failure_case(
                    trace,
                    case_id=f"case_{suffix}",
                    failure_type="tool_error",
                    symptom=f"symptom {suffix}",
                ),
                fix=f"fix {suffix}",
                fix_commit_sha=f"fix_{suffix}",
                regression_passed=True,
            )
        )

    add_case("model_missing", eval_suite="models")
    add_case("model_new", model="model-new", eval_suite="models")
    add_case("model_old", model="model-old", eval_suite="model_removal")
    add_case("model_removed", eval_suite="model_removal")
    add_case("tool_old", tool_calls=[{"name": "old_tool"}], eval_suite="tools")
    add_case("tool_new", tool_calls=[{"name": "new_tool"}], eval_suite="tools")
    add_case(
        "tool_both",
        tool_calls=[{"name": "old_tool"}, {"name": "new_tool"}],
        eval_suite="tools",
    )
    add_case("tool_unnamed", tool_calls=[{"name": ""}], eval_suite="tools")

    addition_context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        prompt_version="prompt",
        model="model-new",
        eval_suite="models",
        failure_type="tool_error",
    )
    addition_report = store.pr_memory_report(
        addition_context,
        change_set=PRChangeSet((("model", None, "model-new"),)),
    )
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in addition_report.related_case_provenance
    ] == [("case_model_missing", "old"), ("case_model_new", "new")]

    removal_model_context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        prompt_version="prompt",
        eval_suite="model_removal",
        failure_type="tool_error",
    )
    removal_model_report = store.pr_memory_report(
        removal_model_context,
        change_set=PRChangeSet((("model", "model-old", None),)),
    )
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in removal_model_report.related_case_provenance
    ] == [("case_model_old", "old"), ("case_model_removed", "new")]

    tool_context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        prompt_version="prompt",
        tool="new_tool",
        eval_suite="tools",
        failure_type="tool_error",
    )
    tool_report = store.pr_memory_report(
        tool_context,
        change_set=PRChangeSet((("tool", "old_tool", "new_tool"),)),
    )
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in tool_report.related_case_provenance
    ] == [
        ("case_tool_both", "both"),
        ("case_tool_new", "new"),
        ("case_tool_old", "old"),
    ]

    removal_context = MemoryContext(
        mode="regression",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        prompt_version="prompt",
        eval_suite="tools",
        failure_type="tool_error",
    )
    removal_report = store.pr_memory_report(
        removal_context,
        change_set=PRChangeSet((("tool", "old_tool", None),)),
    )
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in removal_report.related_case_provenance
    ] == [
        ("case_tool_both", "old"),
        ("case_tool_old", "old"),
        ("case_tool_unnamed", "new"),
    ]


def test_pr_memory_report_change_inputs_are_exclusive_and_legacy_endpoints_are_none():
    store = store_with_retrieval_records_in_order(["a"])
    context = ancestry_context()
    change_set = PRChangeSet((("model", "gpt-old", None),))

    with pytest.raises(
        ValueError, match="exactly one of changed_fields or change_set must be provided"
    ):
        store.pr_memory_report(context)
    with pytest.raises(
        ValueError, match="exactly one of changed_fields or change_set must be provided"
    ):
        store.pr_memory_report(
            context, changed_fields=["model"], change_set=change_set
        )

    report = store.pr_memory_report(context, changed_fields=[])

    assert report.related_case_ids == ["case_a"]
    assert report.warnings == []
    assert all(
        provenance.matched_change_endpoint is None
        for provenance in report.related_case_provenance
    )


def test_pr_memory_report_excludes_unrelated_git_history_everywhere():
    store = TraceBackedMemoryStore()
    for suffix, failure_type in [
        ("a", "invalid_tool_argument"),
        ("b", "tool_timeout"),
    ]:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_{suffix}",
                run_id=f"run_{suffix}",
                commit_sha=f"commit_{suffix}",
                repo="repo",
                tenant="tenant",
                eval_result="fail",
                trace_uri=f"s3://traces/trace_{suffix}.json",
            )
        )
        store.add_failure_case(
            verify_failure_case(
                draft_failure_case(
                    trace,
                    case_id=f"case_{suffix}",
                    failure_type=failure_type,
                    symptom=f"symptom {suffix}",
                ),
                fix=f"fix {suffix}",
                fix_commit_sha=f"fix_commit_{suffix}",
                regression_passed=True,
            )
        )
    evidence = ancestry_evidence("current", commit_a=True, commit_b=False)
    context = MemoryContext(
        mode="debug",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
        failure_type=None,
    )

    report = store.pr_memory_report(
        context,
        changed_fields=["model"],
        commit_ancestry=evidence,
    )

    assert report.related_case_ids == ["case_a"]
    assert report.suggested_regression_tests == [
        "Run invalid_tool_argument regression for tool affected tool before merging."
    ]
    assert (
        "Run tool_timeout regression for tool affected tool before merging."
        not in report.suggested_regression_tests
    )
    assert report.warnings == [
        "model change touches known failure case case_a for known failure area."
    ]
    assert report.related_case_provenance == [
        PRCaseProvenance(
            case_id="case_a",
            source_trace_id="trace_a",
            commit_sha="commit_a",
            fix_commit_sha="fix_commit_a",
            trace_uri="s3://traces/trace_a.json",
            failure_type="invalid_tool_argument",
        )
    ]


def test_pr_memory_report_rejects_missing_ancestry_evidence():
    store = store_with_retrieval_records_in_order(["b", "a"])
    with pytest.raises(
        ValueError,
        match="commit ancestry evidence is missing anchors: commit_b",
    ):
        store.pr_memory_report(
            ancestry_context(),
            changed_fields=["model"],
            commit_ancestry=ancestry_evidence("current", commit_a=True),
        )


def test_pr_memory_report_without_ancestry_preserves_all_related_cases():
    store = store_with_retrieval_records_in_order(["b", "a"])
    legacy_report = store.pr_memory_report(
        ancestry_context(), changed_fields=["model"]
    )
    evidence_report = store.pr_memory_report(
        ancestry_context(),
        changed_fields=["model"],
        commit_ancestry=ancestry_evidence(
            "current", commit_a=True, commit_b=True
        ),
    )

    assert evidence_report == legacy_report
    assert legacy_report.related_case_ids == ["case_a", "case_b"]


def test_candidate_memories_filter_history_but_not_project_policies_by_ancestry():
    store = store_with_retrieval_records_in_order(["b", "a"])
    evidence = ancestry_evidence(
        "current",
        commit_a=True,
        commit_b=False,
        fix_commit_a=True,
        fix_commit_b=False,
        unused_anchor=False,
    )

    candidates = store.candidate_memories(
        ancestry_context(), commit_ancestry=evidence
    )

    assert [memory.memory_id for memory in candidates] == [
        "case_a",
        "lesson_a",
        "policy_a",
        "policy_b",
    ]


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ({}, "commit_ancestry must be a CommitAncestryEvidence or None"),
        (
            CommitAncestryEvidence("", ()),
            "commit ancestry evidence requires current_commit_sha",
        ),
        (CommitAncestryEvidence("other", ()), "does not match context commit_sha"),
        (
            CommitAncestryEvidence("current", []),  # type: ignore[arg-type]
            "commit_relations must be a tuple",
        ),
        (
            CommitAncestryEvidence(
                "current", (["commit_a", True],)  # type: ignore[arg-type]
            ),
            "relations must be two-item tuples",
        ),
        (
            CommitAncestryEvidence("current", (("", True),)),
            "relations require anchor commit",
        ),
        (
            CommitAncestryEvidence(
                "current", (("commit_a", 1),)  # type: ignore[arg-type]
            ),
            "relation values must be booleans",
        ),
        (
            CommitAncestryEvidence(
                "current", (("commit_a", True), ("commit_a", False))
            ),
            "duplicate commit ancestry relation",
        ),
    ],
)
def test_candidate_memories_rejects_malformed_commit_ancestry(
    evidence: object, message: str
):
    store = store_with_retrieval_records_in_order(["a"])
    with pytest.raises(ValueError, match=message):
        store.candidate_memories(
            ancestry_context(), commit_ancestry=evidence  # type: ignore[arg-type]
        )


def test_candidate_memories_rejects_sorted_missing_ancestry_anchors():
    store = store_with_retrieval_records_in_order(["b", "a"])
    evidence = ancestry_evidence("current", commit_a=True)

    with pytest.raises(
        ValueError,
        match=(
            "commit ancestry evidence is missing anchors: "
            "commit_b, fix_commit_a, fix_commit_b"
        ),
    ):
        store.candidate_memories(
            ancestry_context(), query="lesson a", commit_ancestry=evidence
        )


def test_ancestry_filters_before_semantic_ranking():
    store = store_with_retrieval_records_in_order(["a"])
    evidence = ancestry_evidence(
        "current", commit_a=False, fix_commit_a=False
    )

    candidates = store.candidate_memories(
        ancestry_context(),
        semantic_scores={"case_a": 1.0, "lesson_a": 0.9, "policy_a": 0.1},
        max_candidates=3,
        commit_ancestry=evidence,
    )

    assert [memory.memory_id for memory in candidates] == ["policy_a"]


def test_semantic_scores_are_validated_even_for_false_ancestry():
    store = store_with_retrieval_records_in_order(["a"])
    evidence = ancestry_evidence(
        "current", commit_a=False, fix_commit_a=False
    )

    with pytest.raises(
        ValueError,
        match="semantic score for 'lesson_a' must be a finite number",
    ):
        store.candidate_memories(
            ancestry_context(),
            semantic_scores={"lesson_a": "invalid"},  # type: ignore[dict-item]
            max_candidates=1,
            commit_ancestry=evidence,
        )


def test_true_ancestry_does_not_bypass_system_gate():
    store, trace, case, lesson = store_with_active_lesson()
    sensitive = replace(
        lesson,
        lesson_id="lesson_sensitive",
        lesson_text="Sensitive guidance",
        sensitive=True,
    )
    store.add_lesson(sensitive)
    assert case.fix_commit_sha is not None
    evidence = CommitAncestryEvidence(
        trace.commit_sha,
        ((case.fix_commit_sha, True),),
    )

    request = store.prepare_memory(
        matching_context(trace),
        task="repair",
        commit_ancestry=evidence,
    )

    assert sensitive.lesson_id in request.candidate_memory_ids
    assert sensitive.lesson_id not in request.system_allowed_memory_ids
    assert dict(request.system_blocked)[sensitive.lesson_id] == (
        "memory is marked sensitive"
    )


def test_invalid_ancestry_prepare_does_not_consume_request_id():
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="repo", commit_sha="current")

    with pytest.raises(ValueError, match="does not match context commit_sha"):
        store.prepare_memory(
            context,
            task="repair",
            commit_ancestry=CommitAncestryEvidence("other", ()),
        )

    assert store.prepare_memory(context, task="repair").request_id == (
        "gate_request_000001"
    )


def test_prepare_uses_ancestry_without_persisting_evidence():
    store = store_with_retrieval_records_in_order(["a"])
    store.record_trace(
        Trace(
            trace_id="trace_current",
            run_id="run_current",
            commit_sha="current",
            repo="repo",
            tenant="tenant",
        )
    )
    context = ancestry_context()
    evidence = ancestry_evidence(
        "current", commit_a=True, fix_commit_a=True
    )
    before = store.to_snapshot()

    request = store.prepare_memory(
        context, task="repair", commit_ancestry=evidence
    )
    assert store.to_snapshot() == before
    result = store.finalize_memory(
        request,
        allow_decision("lesson_a"),
        trace_id="trace_current",
        eval_result="pass",
    )

    assert result.allowed_memory_ids == ("lesson_a",)
    encoded = json.dumps(store.to_snapshot(), sort_keys=True)
    assert "commit_ancestry" not in encoded
    assert "commit_relations" not in encoded


def test_candidate_memories_ranks_semantic_scores_after_metadata_filter():
    store = store_with_retrieval_records_in_order(["c", "a", "b"])
    store.add_project_policy(
        ProjectPolicy(
            policy_id="wrong_scope",
            policy_text="This record must never enter the current scope.",
            scope={"repo": "other", "tenant": "tenant"},
        )
    )
    context = MemoryContext(
        mode="planning",
        repo="repo",
        tenant="tenant",
        commit_sha="current",
    )
    scores = {
        "wrong_scope": 100,
        "policy_c": 0.9,
        "lesson_b": 0.9,
        "lesson_a": 0.8,
        "lesson_c": 0.7,
        "policy_a": 0.2,
    }

    candidates = store.candidate_memories(
        context,
        semantic_scores=scores,
        max_candidates=3,
        minimum_score=0.5,
    )

    assert [memory.memory_id for memory in candidates] == [
        "lesson_b",
        "policy_c",
        "lesson_a",
    ]


def test_candidate_memories_accepts_an_empty_semantic_score_mapping():
    store, trace, _case, _lesson = store_with_active_lesson()

    assert store.candidate_memories(
        matching_context(trace),
        semantic_scores={},
        max_candidates=1,
    ) == []


def test_candidate_memories_accepts_the_maximum_semantic_candidate_limit():
    store = TraceBackedMemoryStore()
    for index in range(51):
        store.add_project_policy(
            ProjectPolicy(
                policy_id=f"policy_{index:03d}",
                policy_text=f"Policy {index}",
                scope={"repo": "repo"},
            )
        )
    context = MemoryContext(mode="planning", repo="repo", commit_sha="current")
    scores = {f"policy_{index:03d}": 1.0 for index in range(51)}

    candidates = store.candidate_memories(
        context,
        semantic_scores=scores,
        max_candidates=50,
    )

    assert [memory.memory_id for memory in candidates] == [
        f"policy_{index:03d}" for index in range(50)
    ]


@pytest.mark.parametrize(
    ("semantic_kwargs", "message"),
    [
        ({"semantic_scores": [], "max_candidates": 1}, "semantic_scores must be a mapping or None"),
        ({"query": "", "semantic_scores": {}, "max_candidates": 1}, "query and semantic_scores are mutually exclusive"),
        ({"semantic_scores": {}}, "max_candidates is required with semantic_scores"),
        ({"max_candidates": 1}, "max_candidates requires semantic_scores"),
        ({"minimum_score": 0.5}, "minimum_score requires semantic_scores"),
        ({"semantic_scores": {}, "max_candidates": True}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 1.0}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 0}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 51}, "max_candidates must be an integer from 1 through 50"),
        ({"semantic_scores": {}, "max_candidates": 1, "minimum_score": False}, "minimum_score must be a finite number"),
        ({"semantic_scores": {}, "max_candidates": 1, "minimum_score": float("nan")}, "minimum_score must be a finite number"),
        ({"semantic_scores": {}, "max_candidates": 1, "minimum_score": float("inf")}, "minimum_score must be a finite number"),
        ({"semantic_scores": {}, "max_candidates": 1, "minimum_score": float("-inf")}, "minimum_score must be a finite number"),
        ({"semantic_scores": {"lesson_001": False}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {"lesson_001": float("inf")}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {"lesson_001": float("-inf")}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {"lesson_001": "0.5"}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {"lesson_001": Decimal("0.5")}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {"lesson_001": IntLike(1)}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {1: 0.5}, "max_candidates": 1}, "semantic score memory IDs must be non-empty strings"),
        ({"semantic_scores": {"": 0.5}, "max_candidates": 1}, "semantic score memory IDs must be non-empty strings"),
        ({"semantic_scores": {"x" * 129: 0.5}, "max_candidates": 1}, "semantic score memory IDs must be at most 128 characters"),
        ({"semantic_scores": {"missing": 0.5}, "max_candidates": 1}, "semantic_scores references unknown memory IDs: missing"),
    ],
)
def test_candidate_memories_rejects_invalid_semantic_options(
    semantic_kwargs: dict[str, object], message: str
):
    store, trace, _case, _lesson = store_with_active_lesson()

    with pytest.raises(ValueError, match=message):
        store.candidate_memories(
            matching_context(trace),
            **semantic_kwargs,  # type: ignore[arg-type]
        )


def test_candidate_memories_validates_metadata_ineligible_scores():
    store, trace, _case, lesson = store_with_active_lesson()
    store.add_project_policy(
        ProjectPolicy(
            policy_id="wrong_scope",
            policy_text="This policy is outside the current repository scope.",
            scope={"repo": "other"},
        )
    )

    with pytest.raises(
        ValueError,
        match="semantic score for 'wrong_scope' must be a finite number",
    ):
        store.candidate_memories(
            matching_context(trace),
            semantic_scores={lesson.lesson_id: 0.8, "wrong_scope": "invalid"},  # type: ignore[dict-item]
            max_candidates=1,
        )


def test_candidate_memories_requires_all_declared_scope_fields_to_match():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="same_tenant_wrong_tool",
            lesson_text="Use the invoice parser.",
            memory_type="procedural",
            scope={"tenant": "tenant_a", "tool": "parse_invoice"},
        )
    )
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="matching_lesson",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tenant": "tenant_a", "tool": "search_docs"},
        )
    )
    context = MemoryContext(
        mode="repair",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="abc123",
        tool="search_docs",
    )

    assert [memory.memory_id for memory in store.candidate_memories(context)] == ["matching_lesson"]


def test_candidate_memories_can_filter_metadata_matches_by_keyword_query():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="query_lesson",
            lesson_text="When calling search_docs, always provide a non-empty natural language query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    )
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="timeout_lesson",
            lesson_text="Retry search_docs once when the request times out.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    )
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="wrong_tool_lesson",
            lesson_text="Retry parse_invoice when the request times out.",
            memory_type="procedural",
            scope={"tool": "parse_invoice"},
        )
    )

    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    assert [memory.memory_id for memory in store.candidate_memories(context, query="timeout retry")] == [
        "timeout_lesson"
    ]


def test_candidate_memories_short_query_tokens_do_not_drop_metadata_matches():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="ai_lesson",
            lesson_text="AI routing should preserve the v2 search_docs schema.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    )
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    assert [memory.memory_id for memory in store.candidate_memories(context, query="AI v2")] == ["ai_lesson"]


def test_candidate_memories_blank_or_punctuation_query_keeps_metadata_matches():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="query_lesson",
            lesson_text="When calling search_docs, always provide a non-empty natural language query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    )
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    assert [memory.memory_id for memory in store.candidate_memories(context, query="   !!!   ")] == ["query_lesson"]


def test_candidate_memories_include_matching_project_policies():
    store = TraceBackedMemoryStore()
    store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_001",
            policy_text="Planner responses must include a tool-call rationale.",
            scope={"prompt_family": "planner"},
        )
    )
    store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_other",
            policy_text="Invoice parser responses must include currency.",
            scope={"tool": "parse_invoice"},
        )
    )
    context = MemoryContext(
        mode="planning",
        repo="agent-harness",
        commit_sha="abc123",
        prompt_family="planner",
    )

    candidates = store.candidate_memories(context)

    assert [(memory.memory_id, memory.memory_type, memory.source_policy_id) for memory in candidates] == [
        ("policy_001", "policy", "policy_001")
    ]


def test_candidate_memories_include_verified_failure_cases_for_debug_and_repair_only():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_a",
            branch="main",
            prompt_version="planner_v3",
            prompt_family="planner",
            tool_schema_version="search_docs_v2",
            model="gpt-5.5-pro",
            eval_suite="tool_calling_regression",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    verified = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="invalid_tool_argument",
            symptom="planner called search_docs with null query",
            root_cause="prompt omitted the non-empty query contract",
        ),
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(verified)
    draft_trace = store.record_trace(
        Trace(trace_id="trace_draft", run_id="run_draft", commit_sha="draft123", eval_result="fail")
    )
    store.add_failure_case(
        draft_failure_case(
            draft_trace,
            case_id="case_draft",
            failure_type="invalid_tool_argument",
            symptom="draft failure is not ready",
        )
    )
    obsolete_trace = store.record_trace(
        Trace(trace_id="trace_obsolete", run_id="run_obsolete", commit_sha="old123", eval_result="fail")
    )
    store.add_failure_case(
        obsolete_failure_case(
            verify_failure_case(
                draft_failure_case(
                    obsolete_trace,
                    case_id="case_obsolete",
                    failure_type="invalid_tool_argument",
                    symptom="obsolete failure is no longer relevant",
                ),
                fix="old fix",
                fix_commit_sha="oldfix123",
                regression_passed=True,
            )
        )
    )
    context = MemoryContext(
        mode="debug",
        repo="agent-harness",
        tenant="tenant_a",
        branch="main",
        commit_sha="new123",
        prompt_version="planner_v3",
        prompt_family="planner",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
        model="gpt-5.5-pro",
        eval_suite="tool_calling_regression",
        failure_type="invalid_tool_argument",
    )

    debug_candidates = store.candidate_memories(context)
    repair_candidates = store.candidate_memories(MemoryContext(**{**context.__dict__, "mode": "repair"}))
    eval_candidates = store.candidate_memories(MemoryContext(**{**context.__dict__, "mode": "eval"}))

    assert [(memory.memory_id, memory.memory_type, memory.status) for memory in debug_candidates] == [
        ("case_001", "episodic", "verified")
    ]
    assert [memory.memory_id for memory in repair_candidates] == ["case_001"]
    assert eval_candidates == []


def test_candidate_memories_scope_failure_cases_from_source_trace_metadata():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_a",
            branch="main",
            tool_schema_version="search_docs_v2",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                trace,
                case_id="case_001",
                failure_type="invalid_tool_argument",
                symptom="planner called search_docs with null query",
            ),
            fix="fixed prompt",
            fix_commit_sha="def456",
            regression_passed=True,
        )
    )
    matching = MemoryContext(
        mode="debug",
        repo="agent-harness",
        tenant="tenant_a",
        branch="main",
        commit_sha="new123",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
        failure_type="invalid_tool_argument",
    )
    wrong_branch = MemoryContext(**{**matching.__dict__, "branch": "feature"})
    wrong_tool = MemoryContext(**{**matching.__dict__, "tool": "parse_invoice"})
    missing_failure_type = MemoryContext(**{**matching.__dict__, "failure_type": None})

    assert [memory.memory_id for memory in store.candidate_memories(matching)] == ["case_001"]
    assert store.candidate_memories(wrong_branch) == []
    assert store.candidate_memories(wrong_tool) == []
    assert store.candidate_memories(missing_failure_type) == []


def test_candidate_memories_do_not_match_multi_tool_failure_cases_to_unseen_tool():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}, {"name": "lookup_account"}],
        )
    )
    store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                trace,
                case_id="case_001",
                failure_type="invalid_tool_argument",
                symptom="search_docs received a null query after account lookup",
            ),
            fix="fixed prompt",
            fix_commit_sha="def456",
            regression_passed=True,
        )
    )
    context = MemoryContext(
        mode="debug",
        repo="agent-harness",
        commit_sha="new123",
        tool="parse_invoice",
        failure_type="invalid_tool_argument",
    )

    assert store.candidate_memories(context) == []


def test_store_metrics_summarize_usage_logs_and_lesson_confidence():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    active_lesson = lesson_from_failure_case(
        case,
        lesson_id="active_lesson",
        lesson_text="Use a non-empty query.",
        memory_type="procedural",
        scope={"repo": "agent-harness", "tool": "search_docs"},
        confidence=0.8,
    )
    obsolete = obsolete_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="obsolete_lesson",
            lesson_text="Old query guidance.",
            memory_type="procedural",
            scope={"repo": "agent-harness", "tool": "search_docs"},
            confidence=0.2,
        )
    )
    store.add_lesson(active_lesson)
    store.add_lesson(obsolete)

    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    candidates = store.candidate_memories(context)
    system_allowed, system_blocked = system_gate(context, candidates)
    _, final_decision = apply_llm_gate_decision(
        system_allowed,
        system_blocked,
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["active_lesson"],
            blocked_memory_ids=[],
            reason="directly relevant",
            risk="low",
            recommended_injection="short_summary",
        ),
    )

    store.log_decision("run_001", context, [m.memory_id for m in candidates], final_decision)

    metrics = store.metrics()

    assert metrics.decision_count == 1
    assert metrics.candidate_memory_count == 2
    assert metrics.used_memory_count == 1
    assert metrics.blocked_memory_count == 1
    assert metrics.obsolete_memory_usage_attempts == 1
    assert metrics.average_lesson_confidence == 0.5


def test_store_metrics_track_pass_rates_and_wrong_memory_failures():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"repo": "agent-harness", "tool": "search_docs"},
        )
    )
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_002",
            lesson_text="Old guidance that looked relevant.",
            memory_type="procedural",
            scope={"repo": "agent-harness", "tool": "search_docs"},
        )
    )
    for index, run_id in enumerate(
        ["run_with_memory_pass", "run_with_memory_fail", "run_without_memory_pass"],
        start=2,
    ):
        store.record_trace(
            Trace(
                trace_id=f"trace_{index:03d}",
                run_id=run_id,
                commit_sha="abc123",
                repo="agent-harness",
                tool_calls=[{"name": "search_docs"}],
            )
        )
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    store.log_decision(
        "run_with_memory_pass",
        context,
        ["lesson_001"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_001"],
            blocked_memory_ids=[],
            reason="directly relevant",
            risk="low",
            recommended_injection="short_summary",
        ),
        eval_result="pass",
    )
    store.log_decision(
        "run_with_memory_fail",
        context,
        ["lesson_002"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_002"],
            blocked_memory_ids=[],
            reason="looked relevant but was harmful",
            risk="medium",
            recommended_injection="short_summary",
        ),
        eval_result="fail",
        memory_caused_failure=True,
    )
    store.log_decision(
        "run_without_memory_pass",
        context,
        [],
        MemoryDecision(
            use_memory=False,
            allowed_memory_ids=[],
            blocked_memory_ids=[],
            reason="no relevant memory",
            risk="none",
            recommended_injection="none",
        ),
        eval_result="pass",
    )

    metrics = store.metrics()

    assert metrics.pass_rate_with_memory == 0.5
    assert metrics.pass_rate_without_memory == 1.0
    assert metrics.wrong_memory_failure_count == 1


def test_memory_metrics_appends_outcome_counts_without_positional_breakage():
    metrics = MemoryMetrics(1, 2, 3, 4, 5, 0.5, 0.75, 0.25, 6)

    assert metrics.decision_count == 1
    assert metrics.pass_rate_with_memory == 0.75
    assert metrics.pass_rate_without_memory == 0.25
    assert metrics.wrong_memory_failure_count == 6
    assert metrics.evaluated_with_memory_count == 0
    assert metrics.evaluated_without_memory_count == 0
    assert metrics.unevaluated_decision_count == 0


def test_store_metrics_empty_cohorts_have_no_pass_rate():
    metrics = TraceBackedMemoryStore().metrics()

    assert metrics.decision_count == 0
    assert metrics.evaluated_with_memory_count == 0
    assert metrics.evaluated_without_memory_count == 0
    assert metrics.unevaluated_decision_count == 0
    assert metrics.pass_rate_with_memory is None
    assert metrics.pass_rate_without_memory is None
    assert TraceBackedMemoryStore().memory_outcome_metrics() == ()


def test_memory_outcome_metrics_model_is_exported_and_frozen():
    metrics_type = getattr(tbm, "MemoryOutcomeMetrics")
    metrics = metrics_type(
        memory_id="lesson_001",
        candidate_count=1,
        used_count=1,
        blocked_count=0,
        evaluated_use_count=1,
        passed_use_count=1,
        failed_or_errored_use_count=0,
        unevaluated_use_count=0,
        observed_pass_rate=1.0,
    )

    with pytest.raises(FrozenInstanceError):
        metrics.used_count = 2


def test_store_memory_outcome_metrics_are_sorted_complete_and_noncausal():
    store, source_trace, case, first_lesson = store_with_active_lesson()
    second_lesson = store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_002",
            lesson_text="Retry the same tool once after a transient error.",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
        )
    )
    policy = store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_zero",
            policy_text="Use schema-valid tool arguments.",
            scope={"repo": "repo", "tenant": "tenant_a"},
        )
    )
    context = matching_context(source_trace)

    observations = [
        ("shared_pass", [first_lesson.lesson_id, second_lesson.lesson_id], [], "pass"),
        ("first_fail", [first_lesson.lesson_id], [second_lesson.lesson_id], "fail"),
        ("second_error", [second_lesson.lesson_id], [], "error"),
        ("first_unknown", [first_lesson.lesson_id], [], "unknown"),
        ("second_missing", [second_lesson.lesson_id], [], None),
    ]
    for suffix, allowed_ids, blocked_ids, eval_result in observations:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_memory_metrics_{suffix}",
                run_id=f"run_memory_metrics_{suffix}",
                commit_sha=source_trace.commit_sha,
                repo=source_trace.repo,
                tenant=source_trace.tenant,
                tool_calls=[{"name": "search_docs"}],
            )
        )
        candidate_ids = [*allowed_ids, *blocked_ids]
        store.log_decision(
            trace.run_id,
            context,
            candidate_ids,
            MemoryDecision(
                use_memory=True,
                allowed_memory_ids=allowed_ids,
                blocked_memory_ids=blocked_ids,
                reason="per-memory observation",
                risk="low",
                recommended_injection="short_summary",
            ),
            eval_result=eval_result,  # type: ignore[arg-type]
        )

    metrics = store.memory_outcome_metrics()

    assert metrics == (
        tbm.MemoryOutcomeMetrics(
            memory_id=case.case_id,
            candidate_count=0,
            used_count=0,
            blocked_count=0,
            evaluated_use_count=0,
            passed_use_count=0,
            failed_or_errored_use_count=0,
            unevaluated_use_count=0,
            observed_pass_rate=None,
        ),
        tbm.MemoryOutcomeMetrics(
            memory_id=first_lesson.lesson_id,
            candidate_count=3,
            used_count=3,
            blocked_count=0,
            evaluated_use_count=2,
            passed_use_count=1,
            failed_or_errored_use_count=1,
            unevaluated_use_count=1,
            observed_pass_rate=0.5,
        ),
        tbm.MemoryOutcomeMetrics(
            memory_id=second_lesson.lesson_id,
            candidate_count=4,
            used_count=3,
            blocked_count=1,
            evaluated_use_count=2,
            passed_use_count=1,
            failed_or_errored_use_count=1,
            unevaluated_use_count=1,
            observed_pass_rate=0.5,
        ),
        tbm.MemoryOutcomeMetrics(
            memory_id=policy.policy_id,
            candidate_count=0,
            used_count=0,
            blocked_count=0,
            evaluated_use_count=0,
            passed_use_count=0,
            failed_or_errored_use_count=0,
            unevaluated_use_count=0,
            observed_pass_rate=None,
        ),
    )
    assert TraceBackedMemoryStore.from_snapshot(
        store.to_snapshot()
    ).memory_outcome_metrics() == metrics


def test_store_metrics_exclude_unknown_and_missing_outcomes_from_pass_rates():
    store, source_trace, _case, lesson = store_with_active_lesson()
    context = matching_context(source_trace)
    observations = [
        ("with_pass", True, "pass"),
        ("with_fail", True, "fail"),
        ("with_error", True, "error"),
        ("with_unknown", True, "unknown"),
        ("with_missing", True, None),
        ("without_pass", False, "pass"),
        ("without_fail", False, "fail"),
        ("without_unknown", False, "unknown"),
        ("without_missing", False, None),
    ]

    for suffix, use_memory, eval_result in observations:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_metrics_{suffix}",
                run_id=f"run_metrics_{suffix}",
                commit_sha=source_trace.commit_sha,
                repo=source_trace.repo,
                tenant=source_trace.tenant,
                tool_calls=[{"name": "search_docs"}],
            )
        )
        candidate_ids = [lesson.lesson_id] if use_memory else []
        decision = MemoryDecision(
            use_memory=use_memory,
            allowed_memory_ids=candidate_ids,
            blocked_memory_ids=[],
            reason="outcome metrics observation",
            risk="low" if use_memory else "none",
            recommended_injection="short_summary" if use_memory else "none",
        )
        store.log_decision(
            trace.run_id,
            context,
            candidate_ids,
            decision,
            eval_result=eval_result,  # type: ignore[arg-type]
        )

    metrics = store.metrics()

    assert metrics.decision_count == 9
    assert metrics.evaluated_with_memory_count == 3
    assert metrics.evaluated_without_memory_count == 2
    assert metrics.unevaluated_decision_count == 4
    assert (
        metrics.evaluated_with_memory_count
        + metrics.evaluated_without_memory_count
        + metrics.unevaluated_decision_count
        == metrics.decision_count
    )
    assert metrics.pass_rate_with_memory == pytest.approx(1 / 3)
    assert metrics.pass_rate_without_memory == 0.5


def test_store_rejects_usage_log_with_used_memory_outside_candidates():
    store, trace, _case, _lesson = store_with_active_lesson()
    context = matching_context(trace)

    try:
        store.log_decision(
            trace.run_id,
            context,
            ["lesson_001"],
            MemoryDecision(
                use_memory=True,
                allowed_memory_ids=["lesson_missing"],
                blocked_memory_ids=[],
                reason="inconsistent decision",
                risk="medium",
                recommended_injection="short_summary",
            ),
        )
    except ValueError as exc:
        assert "candidate" in str(exc)
    else:
        raise AssertionError("used memory ids must be present in the candidate ids")


def test_store_rejects_usage_log_with_invalid_eval_result():
    store, trace, _case, _lesson = store_with_active_lesson()
    context = matching_context(trace)

    try:
        store.log_decision(
            trace.run_id,
            context,
            [],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[],
                reason="invalid eval result",
                risk="none",
                recommended_injection="none",
            ),
            eval_result="skipped",
        )
    except ValueError as exc:
        assert "eval_result" in str(exc)
    else:
        raise AssertionError("usage logs must reject unsupported eval_result values")


def test_store_rejects_usage_log_with_empty_run_id():
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    try:
        store.log_decision(
            "",
            context,
            [],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[],
                reason="missing run id",
                risk="none",
                recommended_injection="none",
            ),
        )
    except ValueError as exc:
        assert "run_id" in str(exc)
    else:
        raise AssertionError("usage logs must require run_id")


def test_store_rejects_usage_log_with_inconsistent_blocked_memory_ids():
    store, trace, _case, _lesson = store_with_active_lesson()
    context = matching_context(trace)
    invalid_decisions = [
        MemoryDecision(
            use_memory=False,
            allowed_memory_ids=[],
            blocked_memory_ids=["lesson_missing"],
            reason="blocked id was not a candidate",
            risk="low",
            recommended_injection="none",
        ),
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_001"],
            blocked_memory_ids=["lesson_001"],
            reason="same id is both used and blocked",
            risk="medium",
            recommended_injection="short_summary",
        ),
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_001", "lesson_001"],
            blocked_memory_ids=[],
            reason="duplicate used ids",
            risk="medium",
            recommended_injection="short_summary",
        ),
    ]

    for decision in invalid_decisions:
        try:
            store.log_decision(trace.run_id, context, ["lesson_001"], decision)
        except ValueError as exc:
            assert "memory" in str(exc) or "candidate" in str(exc) or "duplicate" in str(exc)
        else:
            raise AssertionError("usage logs must reject inconsistent used/blocked memory IDs")


def test_store_rejects_usage_log_with_empty_memory_ids():
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    try:
        store.log_decision(
            "run_001",
            context,
            [""],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[],
                reason="empty candidate id",
                risk="none",
                recommended_injection="none",
            ),
        )
    except ValueError as exc:
        assert "candidate_memory_ids" in str(exc)
    else:
        raise AssertionError("usage log memory IDs must be non-empty strings")


def test_store_rejects_usage_log_with_unknown_candidate_memory_ids():
    store, trace, _case, _lesson = store_with_active_lesson()
    context = matching_context(trace)

    try:
        store.log_decision(
            trace.run_id,
            context,
            ["missing_memory"],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[],
                reason="ghost candidate",
                risk="none",
                recommended_injection="none",
            ),
        )
    except ValueError as exc:
        assert "unknown memory IDs" in str(exc)
    else:
        raise AssertionError("usage logs must only reference stored runtime memory IDs")


def test_store_rejects_usage_logs_with_impossible_injection_modes():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"repo": "agent-harness", "tool": "search_docs"},
        )
    )
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    invalid_cases = [
        (
            [],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[],
                reason="no memory used",
                risk="none",
                recommended_injection="short_summary",
            ),
            "recommended_injection",
        ),
        (
            ["lesson_001"],
            MemoryDecision(
                use_memory=True,
                allowed_memory_ids=["lesson_001"],
                blocked_memory_ids=[],
                reason="memory used",
                risk="low",
                recommended_injection="none",
            ),
            "recommended_injection",
        ),
    ]

    for candidate_ids, decision, expected_message in invalid_cases:
        try:
            store.log_decision("run_001", context, candidate_ids, decision)
        except ValueError as exc:
            assert expected_message in str(exc)
        else:
            raise AssertionError("usage logs must keep used memory and injection mode consistent")


def test_store_rejects_wrong_memory_failure_without_failed_memory_use():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"repo": "agent-harness", "tool": "search_docs"},
        )
    )
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    invalid_cases = [
        (
            [],
            MemoryDecision(
                use_memory=False,
                allowed_memory_ids=[],
                blocked_memory_ids=[],
                reason="no memory used",
                risk="none",
                recommended_injection="none",
            ),
            "fail",
        ),
        (
            ["lesson_001"],
            MemoryDecision(
                use_memory=True,
                allowed_memory_ids=["lesson_001"],
                blocked_memory_ids=[],
                reason="memory used successfully",
                risk="low",
                recommended_injection="short_summary",
            ),
            "pass",
        ),
    ]

    for candidate_ids, decision, eval_result in invalid_cases:
        try:
            store.log_decision(
                "run_001",
                context,
                candidate_ids,
                decision,
                eval_result=eval_result,
                memory_caused_failure=True,
            )
        except ValueError as exc:
            assert "memory_caused_failure" in str(exc)
        else:
            raise AssertionError("wrong-memory failure logs must require failed or errored memory use")


def test_store_metrics_count_obsolete_project_policy_attempts():
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            prompt_family="planner",
        )
    )
    store.add_project_policy(
        ProjectPolicy(
            policy_id="obsolete_policy",
            policy_text="Old planner policy.",
            scope={"prompt_family": "planner"},
            status="obsolete",
        )
    )
    context = MemoryContext(
        mode="planning",
        repo="agent-harness",
        commit_sha="abc123",
        prompt_family="planner",
    )
    candidates = store.candidate_memories(context)
    system_allowed, system_blocked = system_gate(context, candidates)

    store.log_decision(
        "run_001",
        context,
        [memory.memory_id for memory in candidates],
        MemoryDecision(
            use_memory=False,
            allowed_memory_ids=[memory.memory_id for memory in system_allowed],
            blocked_memory_ids=list(system_blocked),
            reason="blocked obsolete policy",
            risk="none",
            recommended_injection="none",
        ),
    )

    assert system_blocked == {"obsolete_policy": "status 'obsolete' is not allowed"}
    assert store.metrics().obsolete_memory_usage_attempts == 1


def test_store_metrics_count_obsolete_failure_case_attempts():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
        )
    )
    case = obsolete_failure_case(
        verify_failure_case(
            draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
            fix="fixed prompt",
            fix_commit_sha="def456",
            regression_passed=True,
        )
    )
    store.add_failure_case(case)
    context = MemoryContext(mode="debug", repo="agent-harness", commit_sha="abc123")

    store.log_decision(
        "run_001",
        context,
        ["case_001"],
        MemoryDecision(
            use_memory=False,
            allowed_memory_ids=[],
            blocked_memory_ids=["case_001"],
            reason="blocked obsolete failure case",
            risk="none",
            recommended_injection="none",
        ),
    )

    assert store.metrics().obsolete_memory_usage_attempts == 1


def test_store_rejects_lesson_without_stored_source_case():
    store = TraceBackedMemoryStore()
    trace = Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail")
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="Use a non-empty query.",
        memory_type="procedural",
        scope={"tool": "search_docs"},
    )

    try:
        store.add_lesson(lesson)
    except ValueError as exc:
        assert "source_case_id" in str(exc)
    else:
        raise AssertionError("lessons must require a stored source case")


def test_store_rejects_lesson_from_unverified_stored_case():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="bad query",
    )
    store.add_failure_case(draft)
    lesson = lesson_from_failure_case(
        verify_failure_case(draft, fix="fixed prompt", fix_commit_sha="def456", regression_passed=True),
        lesson_id="lesson_001",
        lesson_text="Use a non-empty query.",
        memory_type="procedural",
        scope={"tool": "search_docs"},
    )

    try:
        store.add_lesson(lesson)
    except ValueError as exc:
        assert "verified source case" in str(exc)
    else:
        raise AssertionError("lessons must require a verified stored source case")


def test_store_rejects_lesson_with_empty_scope_or_invalid_confidence():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)

    invalid_lessons = [
        Lesson(
            lesson_id="empty_scope",
            source_case_id="case_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={},
        ),
        Lesson(
            lesson_id="bad_confidence",
            source_case_id="case_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
            confidence=1.5,
        ),
    ]

    for lesson in invalid_lessons:
        try:
            store.add_lesson(lesson)
        except ValueError as exc:
            assert "scope" in str(exc) or "confidence" in str(exc)
        else:
            raise AssertionError("store must validate lesson scope and confidence")


def test_store_rejects_invalid_lesson_records():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)

    invalid_lessons = [
        Lesson(
            lesson_id="",
            source_case_id="case_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        ),
        Lesson(
            lesson_id="lesson_001",
            source_case_id="",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        ),
        Lesson(
            lesson_id="lesson_001",
            source_case_id="case_001",
            lesson_text="Use a non-empty query.",
            memory_type="unsupported",
            scope={"tool": "search_docs"},
        ),
        Lesson(
            lesson_id="lesson_001",
            source_case_id="case_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
            status="draft",
        ),
    ]

    for lesson in invalid_lessons:
        try:
            store.add_lesson(lesson)
        except ValueError as exc:
            assert "lesson" in str(exc)
        else:
            raise AssertionError("invalid lesson records must be rejected")


def test_store_rejects_invalid_project_policy_records():
    invalid_policies = [
        ProjectPolicy(
            policy_id="",
            policy_text="Planner responses must include a rationale.",
            scope={"prompt_family": "planner"},
        ),
        ProjectPolicy(
            policy_id="project_policy_001",
            policy_text="",
            scope={"prompt_family": "planner"},
        ),
        ProjectPolicy(
            policy_id="project_policy_001",
            policy_text="Planner responses must include a rationale.",
            scope={"prompt_family": "planner"},
            status="draft",
        ),
    ]

    for policy in invalid_policies:
        store = TraceBackedMemoryStore()
        try:
            store.add_project_policy(policy)
        except ValueError as exc:
            assert "policy" in str(exc)
        else:
            raise AssertionError("invalid project policy records must be rejected")


def test_store_rejects_memory_id_collisions_across_lessons_and_project_policies():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="memory_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    )

    try:
        store.add_project_policy(
            ProjectPolicy(
                policy_id="memory_001",
                policy_text="Planner responses must include a rationale.",
                scope={"prompt_family": "planner"},
            )
        )
    except ValueError as exc:
        assert "memory_id" in str(exc)
    else:
        raise AssertionError("lesson and project policy memory IDs must be globally unique")


def test_store_rejects_runtime_memory_id_collisions_with_failure_cases():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)

    try:
        store.add_lesson(
            lesson_from_failure_case(
                case,
                lesson_id="case_001",
                lesson_text="Use a non-empty query.",
                memory_type="procedural",
                scope={"tool": "search_docs"},
            )
        )
    except ValueError as exc:
        assert "memory_id" in str(exc)
    else:
        raise AssertionError("lesson IDs must not collide with runtime-visible failure case IDs")

    try:
        store.add_project_policy(
            ProjectPolicy(
                policy_id="case_001",
                policy_text="Planner responses must include a rationale.",
                scope={"prompt_family": "planner"},
            )
        )
    except ValueError as exc:
        assert "memory_id" in str(exc)
    else:
        raise AssertionError("project policy IDs must not collide with runtime-visible failure case IDs")


def test_store_rejects_failure_case_id_collisions_with_existing_runtime_memory():
    store = TraceBackedMemoryStore()
    store.add_project_policy(
        ProjectPolicy(
            policy_id="case_001",
            policy_text="Planner responses must include a rationale.",
            scope={"prompt_family": "planner"},
        )
    )
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )

    try:
        store.add_failure_case(case)
    except ValueError as exc:
        assert "memory_id" in str(exc)
    else:
        raise AssertionError("failure case IDs must not collide with existing runtime memory IDs")


def test_candidate_memories_preserve_lesson_safety_flags_for_system_gate():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        Lesson(
            lesson_id="sensitive_lesson",
            source_case_id="case_001",
            lesson_text="Do not expose private tool output.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
            sensitive=True,
        )
    )
    store.add_lesson(
        Lesson(
            lesson_id="eval_leaking_lesson",
            source_case_id="case_001",
            lesson_text="This contains evaluator-specific answer details.",
            memory_type="policy",
            scope={"tool": "search_docs"},
            eval_leaking=True,
        )
    )
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    candidates = store.candidate_memories(context)
    _, blocked = system_gate(context, candidates)

    assert {memory.memory_id: (memory.sensitive, memory.eval_leaking) for memory in candidates} == {
        "sensitive_lesson": (True, False),
        "eval_leaking_lesson": (False, True),
    }
    assert blocked == {
        "sensitive_lesson": "memory is marked sensitive",
        "eval_leaking_lesson": "memory may leak eval data",
    }


def test_pr_memory_report_surfaces_related_failures_and_regression_suggestions():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_a",
            eval_result="fail",
            trace_uri="s3://traces/trace_001.json",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
            error="Invalid argument: query is required",
        )
    )
    case = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="invalid_tool_argument",
            symptom="search_docs received null query",
        ),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        tool="search_docs",
        failure_type="invalid_tool_argument",
    )

    report = store.pr_memory_report(context, changed_fields=["tool_schema_version"])

    assert report.related_case_ids == ["case_001"]
    assert report.suggested_regression_tests == [
        "Run invalid_tool_argument regression for tool search_docs before merging."
    ]
    assert report.warnings == [
        "tool_schema_version change touches known failure case case_001 for search_docs."
    ]
    assert report.related_case_provenance == [
        PRCaseProvenance(
            case_id="case_001",
            source_trace_id="trace_001",
            commit_sha="abc123",
            fix_commit_sha="def456",
            trace_uri="s3://traces/trace_001.json",
            failure_type="invalid_tool_argument",
        )
    ]


def test_pr_memory_report_ignores_different_tenant_failures():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            tenant="tenant_b",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        tool="search_docs",
    )

    report = store.pr_memory_report(context, changed_fields=["tool_schema_version"])

    assert report.related_case_ids == []
    assert report.suggested_regression_tests == []
    assert report.warnings == []


def test_pr_memory_report_requires_context_tenant_for_tenant_scoped_failures():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_b",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        commit_sha="new123",
        tool="search_docs",
    )

    report = store.pr_memory_report(context, changed_fields=["tool_schema_version"])

    assert report.related_case_ids == []
    assert report.suggested_regression_tests == []
    assert report.warnings == []


def test_pr_memory_report_ignores_failures_without_trace_tenant_when_context_declares_tenant():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        tool="search_docs",
    )

    report = store.pr_memory_report(context, changed_fields=["tool_schema_version"])

    assert report.related_case_ids == []
    assert report.suggested_regression_tests == []
    assert report.warnings == []


def test_pr_memory_report_ignores_different_repo_failures_when_trace_declares_repo():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="other-harness",
            tenant="tenant_a",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        tool="search_docs",
    )

    report = store.pr_memory_report(context, changed_fields=["tool_schema_version"])

    assert report.related_case_ids == []
    assert report.suggested_regression_tests == []
    assert report.warnings == []


def test_pr_memory_report_ignores_failures_without_trace_repo_provenance():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            tenant="tenant_a",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        tool="search_docs",
    )

    report = store.pr_memory_report(context, changed_fields=["tool_schema_version"])

    assert report.related_case_ids == []
    assert report.suggested_regression_tests == []
    assert report.warnings == []


def test_pr_memory_report_ignores_unverified_cases():
    store = TraceBackedMemoryStore()
    draft_trace = store.record_trace(
        Trace(
            trace_id="trace_draft",
            run_id="run_draft",
            commit_sha="abc123",
            tenant="tenant_a",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    store.add_failure_case(
        draft_failure_case(
            draft_trace,
            case_id="case_draft",
            failure_type="invalid_tool_argument",
            symptom="search_docs received a null query",
        )
    )
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        tool="search_docs",
        failure_type="invalid_tool_argument",
    )

    report = store.pr_memory_report(context, changed_fields=["tool_schema_version"])

    assert report.related_case_ids == []
    assert report.suggested_regression_tests == []
    assert report.warnings == []


def test_pr_memory_report_filters_by_eval_suite_when_context_declares_it():
    store = TraceBackedMemoryStore()
    matching_trace = store.record_trace(
        Trace(
            trace_id="trace_matching",
            run_id="run_matching",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_a",
            eval_suite="tool_calling_regression",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    other_trace = store.record_trace(
        Trace(
            trace_id="trace_other",
            run_id="run_other",
            commit_sha="abc124",
            repo="agent-harness",
            tenant="tenant_a",
            eval_suite="retrieval_regression",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                matching_trace,
                case_id="case_matching",
                failure_type="invalid_tool_argument",
                symptom="search_docs received a null query",
            ),
            fix="fixed prompt",
            fix_commit_sha="def456",
            regression_passed=True,
        )
    )
    store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                other_trace,
                case_id="case_other",
                failure_type="invalid_tool_argument",
                symptom="search_docs received a null query",
            ),
            fix="fixed prompt",
            fix_commit_sha="def457",
            regression_passed=True,
        )
    )
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        tool="search_docs",
        failure_type="invalid_tool_argument",
        eval_suite="tool_calling_regression",
    )

    report = store.pr_memory_report(context, changed_fields=["eval_suite"])

    assert report.related_case_ids == ["case_matching"]
    assert report.warnings == [
        "eval_suite change touches known failure case case_matching for search_docs."
    ]


def test_pr_memory_report_filters_by_prompt_tool_schema_and_model_when_context_declares_them():
    store = TraceBackedMemoryStore()
    trace_specs = [
        ("matching", "planner_v3", "planner", "search_docs_v2", "gpt-5.5-pro"),
        ("other_prompt", "planner_v2", "planner", "search_docs_v2", "gpt-5.5-pro"),
        ("other_family", "planner_v3", "summarizer", "search_docs_v2", "gpt-5.5-pro"),
        ("other_schema", "planner_v3", "planner", "search_docs_v1", "gpt-5.5-pro"),
        ("other_model", "planner_v3", "planner", "search_docs_v2", "gpt-4.1"),
    ]
    for suffix, prompt_version, prompt_family, tool_schema_version, model in trace_specs:
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_{suffix}",
                run_id=f"run_{suffix}",
                commit_sha=f"abc_{suffix}",
                repo="agent-harness",
                tenant="tenant_a",
                prompt_version=prompt_version,
                prompt_family=prompt_family,
                tool_schema_version=tool_schema_version,
                model=model,
                eval_result="fail",
                tool_calls=[{"name": "search_docs"}],
            )
        )
        store.add_failure_case(
            verify_failure_case(
                draft_failure_case(
                    trace,
                    case_id=f"case_{suffix}",
                    failure_type="invalid_tool_argument",
                    symptom="search_docs received a null query",
                ),
                fix="fixed prompt",
                fix_commit_sha=f"def_{suffix}",
                regression_passed=True,
            )
        )
    context = MemoryContext(
        mode="regression",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="new123",
        prompt_version="planner_v3",
        prompt_family="planner",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
        model="gpt-5.5-pro",
        failure_type="invalid_tool_argument",
    )

    report = store.pr_memory_report(
        context,
        changed_fields=["prompt_version", "prompt_family", "tool_schema_version", "model"],
    )

    assert report.related_case_ids == ["case_matching"]
    assert report.warnings == [
        "prompt_version change touches known failure case case_matching for search_docs.",
        "prompt_family change touches known failure case case_matching for search_docs.",
        "tool_schema_version change touches known failure case case_matching for search_docs.",
        "model change touches known failure case case_matching for search_docs.",
    ]


def test_store_json_snapshot_round_trips_records_and_usage_logs(tmp_path):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_a",
            eval_suite="tool_calling_regression",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={
                "repo": "agent-harness",
                "tenant": "tenant_a",
                "tool": "search_docs",
            },
        )
    )
    context = MemoryContext(
        mode="repair",
        repo="agent-harness",
        commit_sha="abc123",
        tenant="tenant_a",
        tool="search_docs",
    )
    store.log_decision(
        "run_001",
        context,
        ["lesson_001"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_001"],
            blocked_memory_ids=[],
            reason="directly relevant",
            risk="low",
            recommended_injection="short_summary",
        ),
        eval_result="pass",
    )

    snapshot_path = tmp_path / "memory-store.json"
    store.save_json(snapshot_path)
    loaded = TraceBackedMemoryStore.load_json(snapshot_path)

    assert loaded.traces["trace_001"].eval_suite == "tool_calling_regression"
    assert loaded.traces == store.traces
    assert loaded.failure_cases == store.failure_cases
    assert loaded.lessons == store.lessons
    assert loaded.usage_logs == store.usage_logs
    assert [memory.memory_id for memory in loaded.candidate_memories(context)] == ["lesson_001"]
    assert loaded.metrics().pass_rate_with_memory == 1.0


def test_store_json_snapshot_round_trips_project_policies(tmp_path):
    store = TraceBackedMemoryStore()
    policy = store.add_project_policy(
        ProjectPolicy(
            policy_id="project_policy_001",
            policy_text="Planner responses must include a tool-call rationale.",
            scope={"repo": "agent-harness", "prompt_family": "planner"},
            confidence=0.9,
        )
    )

    snapshot_path = tmp_path / "memory-store.json"
    store.save_json(snapshot_path)
    loaded = TraceBackedMemoryStore.load_json(snapshot_path)
    context = MemoryContext(
        mode="planning",
        repo="agent-harness",
        commit_sha="abc123",
        prompt_family="planner",
    )

    assert loaded.project_policies == {"project_policy_001": policy}
    assert [memory.memory_id for memory in loaded.candidate_memories(context)] == ["project_policy_001"]


def test_store_saves_and_loads_active_lessons_yaml(tmp_path):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    active_lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_active",
        lesson_text="Use a non-empty query.\nNever pass null.",
        memory_type="procedural",
        scope={"tool": "search_docs", "prompt_family": "planner"},
        confidence=0.92,
    )
    store.add_lesson(active_lesson)
    store.add_lesson(
        obsolete_lesson(
            lesson_from_failure_case(
                case,
                lesson_id="lesson_obsolete",
                lesson_text="Old guidance.",
                memory_type="procedural",
                scope={"tool": "search_docs"},
            )
        )
    )

    yaml_path = tmp_path / "lessons.yaml"
    store.save_lessons_yaml(yaml_path)

    yaml_text = yaml_path.read_text(encoding="utf-8")
    assert "lessons:" in yaml_text
    assert "lesson_active" in yaml_text
    assert "lesson_obsolete" not in yaml_text

    loaded = TraceBackedMemoryStore()
    loaded.record_trace(trace)
    loaded.add_failure_case(case)
    loaded_lessons = loaded.load_lessons_yaml(yaml_path)

    assert loaded_lessons == [active_lesson]
    assert loaded.lessons == {"lesson_active": active_lesson}


def test_save_lessons_yaml_syncs_and_replaces_with_a_sibling(
    monkeypatch,
    tmp_path,
):
    store, _trace, _case, _lesson = store_with_active_lesson()
    target = tmp_path / "lessons.yaml"
    events = []
    replacements = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(file_descriptor):
        events.append("fsync")
        real_fsync(file_descriptor)

    def recording_replace(source, destination):
        events.append("replace")
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    store.save_lessons_yaml(target)

    assert events == ["fsync", "replace"]
    assert replacements[0][0].parent == target.parent
    assert replacements[0][1] == target
    assert list(tmp_path.glob(".lessons.yaml.*.tmp")) == []


def test_save_lessons_yaml_can_publish_without_replacing(
    monkeypatch,
    tmp_path,
):
    store, _trace, _case, _lesson = store_with_active_lesson()
    target = tmp_path / "lessons.yaml"
    links = []
    real_link = os.link

    def recording_link(source, destination):
        links.append((Path(source), Path(destination)))
        real_link(source, destination)

    monkeypatch.setattr(os, "link", recording_link)

    store.save_lessons_yaml(target, overwrite=False)

    assert len(links) == 1
    assert links[0][0].parent == target.parent
    assert links[0][1] == target
    assert b"lesson_001" in target.read_bytes()
    assert list(tmp_path.glob(".lessons.yaml.*.tmp")) == []


def test_save_lessons_yaml_no_replace_preserves_existing_destination(
    tmp_path,
):
    store, _trace, _case, _lesson = store_with_active_lesson()
    target = tmp_path / "lessons.yaml"
    original = b"caller-owned lessons\n"
    target.write_bytes(original)

    with pytest.raises(FileExistsError):
        store.save_lessons_yaml(target, overwrite=False)

    assert target.read_bytes() == original
    assert list(tmp_path.glob(".lessons.yaml.*.tmp")) == []


def test_save_lessons_yaml_no_replace_link_failure_cleans_temporary_file(
    monkeypatch,
    tmp_path,
):
    store, _trace, _case, _lesson = store_with_active_lesson()
    target = tmp_path / "lessons.yaml"

    def fail_link(_source, _destination):
        raise OSError("injected lesson link failure")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(OSError, match="injected lesson link failure"):
        store.save_lessons_yaml(target, overwrite=False)

    assert not target.exists()
    assert list(tmp_path.glob(".lessons.yaml.*.tmp")) == []


@pytest.mark.parametrize("failure_point", ["serialize", "fsync", "replace"])
def test_save_lessons_yaml_failure_preserves_existing_file_and_cleans_temp(
    monkeypatch,
    tmp_path,
    failure_point,
):
    store, _trace, _case, _lesson = store_with_active_lesson()
    target = tmp_path / "lessons.yaml"
    original = b"caller-owned lessons\n"
    target.write_bytes(original)

    if failure_point == "serialize":
        def fail_serialize(_lessons):
            raise ValueError("injected lesson serialize failure")

        monkeypatch.setattr(store_module, "_lessons_to_yaml", fail_serialize)
    elif failure_point == "fsync":
        def fail_fsync(_file_descriptor):
            raise OSError("injected lesson fsync failure")

        monkeypatch.setattr(os, "fsync", fail_fsync)
    else:
        def fail_replace(_source, _destination):
            raise OSError("injected lesson replace failure")

        monkeypatch.setattr(os, "replace", fail_replace)

    expected_error = ValueError if failure_point == "serialize" else OSError
    with pytest.raises(
        expected_error,
        match=f"injected lesson {failure_point} failure",
    ):
        store.save_lessons_yaml(target)

    assert target.read_bytes() == original
    assert list(tmp_path.glob(".lessons.yaml.*.tmp")) == []


def test_store_lessons_yaml_preserves_exact_lf_delimited_lesson_text(tmp_path):
    store, trace, case = store_with_verified_case()
    lesson_text = "\nFirst paragraph.  \n\n  Indented second paragraph.\n"
    lesson = store.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_multiline",
            lesson_text=lesson_text,
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a"},
        )
    )
    path = tmp_path / "multiline-lessons.yaml"

    store.save_lessons_yaml(path)

    serialized = path.read_text(encoding="utf-8")
    assert "    lesson_text: |\n" in serialized
    assert "      First paragraph.  \n      \n" in serialized

    restored = TraceBackedMemoryStore()
    restored.record_trace(trace)
    restored.add_failure_case(case)
    loaded = restored.load_lessons_yaml(path)

    assert loaded == [lesson]
    assert loaded[0].lesson_text == lesson_text


def test_store_lessons_yaml_preserves_literal_lines_in_legacy_block(
    tmp_path,
):
    store, _trace, case = store_with_verified_case()
    path = tmp_path / "legacy-folded-lessons.yaml"
    path.write_text(
        (
            "lessons:\n"
            '  - lesson_id: "lesson_legacy_block"\n'
            f'    source_case_id: "{case.case_id}"\n'
            '    memory_type: "procedural"\n'
            '    status: "active"\n'
            "    confidence: 1.0\n"
            "    sensitive: false\n"
            "    eval_leaking: false\n"
            "    scope:\n"
            '      repo: "repo"\n'
            '      tenant: "tenant_a"\n'
            "    lesson_text: >\n"
            "      First paragraph.\n"
            "      Still the first paragraph.\n"
            "      \n"
            "      Second paragraph.\n"
        ),
        encoding="utf-8",
    )

    (loaded,) = store.load_lessons_yaml(path)

    assert loaded.lesson_text == (
        "First paragraph.\nStill the first paragraph.\n\nSecond paragraph."
    )


def test_store_saves_empty_lessons_yaml_atomically(tmp_path):
    path = tmp_path / "empty-lessons.yaml"

    TraceBackedMemoryStore().save_lessons_yaml(path)

    assert path.read_bytes() == b"lessons: []\n"
    assert list(tmp_path.glob(".empty-lessons.yaml.*.tmp")) == []


def test_store_lessons_yaml_preserves_numeric_looking_strings(tmp_path):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_001",
        lesson_text="Schema version 2 must stay a string.",
        memory_type="procedural",
        scope={"tool_schema_version": "2", "branch": "123"},
    )
    store.add_lesson(lesson)

    yaml_path = tmp_path / "numeric-looking-lessons.yaml"
    store.save_lessons_yaml(yaml_path)

    loaded = TraceBackedMemoryStore()
    loaded.record_trace(trace)
    loaded.add_failure_case(case)
    loaded_lessons = loaded.load_lessons_yaml(yaml_path)

    assert loaded_lessons == [lesson]
    assert loaded.lessons["lesson_001"].scope == {"tool_schema_version": "2", "branch": "123"}


def test_store_loads_lessons_example_yaml_with_provenance_checks():
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)

    loaded_lessons = store.load_lessons_yaml(Path(__file__).resolve().parents[1] / "memory" / "lessons.example.yaml")

    assert [lesson.lesson_id for lesson in loaded_lessons] == ["lesson_001"]
    assert store.lessons["lesson_001"].source_case_id == "case_001"
    assert store.lessons["lesson_001"].scope["tool"] == "search_docs"


@pytest.mark.parametrize(
    ("invalid_record", "message"),
    [
        (
            (
                '  - lesson_id: "lesson_duplicate"\n'
                '    lesson_id: "lesson_replacement"\n'
            ),
            "duplicate lesson field: lesson_id",
        ),
        (
            (
                '  - lesson_id: "lesson_duplicate"\n'
                "    scope:\n"
                '      tool: "search_docs"\n'
                '      tool: "replacement_tool"\n'
            ),
            "duplicate lesson scope field: tool",
        ),
    ],
)
def test_lessons_yaml_rejects_duplicate_keys_before_store_mutation(
    tmp_path,
    invalid_record,
    message,
):
    store, _trace, case = store_with_verified_case()
    path = tmp_path / "duplicate-key-lessons.yaml"
    path.write_text(
        (
            "lessons:\n"
            '  - lesson_id: "lesson_valid"\n'
            f'    source_case_id: "{case.case_id}"\n'
            '    memory_type: "procedural"\n'
            '    status: "active"\n'
            "    confidence: 1.0\n"
            "    sensitive: false\n"
            "    eval_leaking: false\n"
            "    scope:\n"
            '      repo: "repo"\n'
            '      tenant: "tenant_a"\n'
            '      tool: "search_docs"\n'
            "    lesson_text: >\n"
            "      Valid lesson text.\n"
            f"{invalid_record}"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        store.load_lessons_yaml(path)

    assert store.lessons == {}


@pytest.mark.parametrize(
    ("second_id", "second_repo", "message"),
    [
        (
            "lesson_valid",
            "repo",
            "duplicate lesson_id: lesson_valid",
        ),
        (
            "lesson_invalid_scope",
            "other_repo",
            "lesson scope must preserve source repo: repo",
        ),
        (
            "lesson_incomplete",
            None,
            "invalid lesson record",
        ),
    ],
)
def test_lessons_yaml_semantic_failure_is_atomic(
    tmp_path,
    second_id,
    second_repo,
    message,
):
    store, _trace, case = store_with_verified_case()

    def record(lesson_id, repo):
        return (
            f'  - lesson_id: "{lesson_id}"\n'
            f'    source_case_id: "{case.case_id}"\n'
            '    memory_type: "procedural"\n'
            '    status: "active"\n'
            "    confidence: 1.0\n"
            "    sensitive: false\n"
            "    eval_leaking: false\n"
            "    scope:\n"
            f'      repo: "{repo}"\n'
            '      tenant: "tenant_a"\n'
            '      tool: "search_docs"\n'
            "    lesson_text: >\n"
            f"      Lesson text for {lesson_id}.\n"
        )

    path = tmp_path / "atomic-lessons.yaml"
    second_record = (
        f'  - lesson_id: "{second_id}"\n'
        if second_repo is None
        else record(second_id, second_repo)
    )
    path.write_text(
        "lessons:\n"
        + record("lesson_valid", "repo")
        + second_record,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        store.load_lessons_yaml(path)

    assert store.lessons == {}


def test_store_json_snapshot_rejects_lessons_without_stored_source_cases(tmp_path):
    snapshot_path = tmp_path / "bad-memory-store.json"
    snapshot_path.write_text(
        """
        {
          "traces": [],
          "failure_cases": [],
          "lessons": [
            {
              "lesson_id": "lesson_001",
              "source_case_id": "missing_case",
              "lesson_text": "Use a non-empty query.",
              "memory_type": "procedural",
              "scope": {"tool": "search_docs"}
            }
          ],
          "project_policies": [],
          "usage_logs": []
        }
        """,
        encoding="utf-8",
    )

    try:
        TraceBackedMemoryStore.load_json(snapshot_path)
    except ValueError as exc:
        assert "source_case_id" in str(exc)
    else:
        raise AssertionError("loaded lessons must require a stored source case")


def test_store_json_snapshot_rejects_failure_case_without_stored_source_trace(tmp_path):
    snapshot_path = tmp_path / "bad-failure-case-store.json"
    snapshot_path.write_text(
        """
        {
          "traces": [],
          "failure_cases": [
            {
              "case_id": "case_001",
              "source_trace_id": "missing_trace",
              "commit_sha": "abc123",
              "failure_type": "invalid_tool_argument",
              "symptom": "planner called search_docs with null query"
            }
          ],
          "lessons": [],
          "project_policies": [],
          "usage_logs": []
        }
        """,
        encoding="utf-8",
    )

    try:
        TraceBackedMemoryStore.load_json(snapshot_path)
    except ValueError as exc:
        assert "source_trace_id" in str(exc)
    else:
        raise AssertionError("loaded failure cases must require a stored source trace")


def test_store_json_snapshot_rejects_inconsistent_usage_logs(tmp_path):
    snapshot_path = tmp_path / "bad-usage-log-store.json"
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0]["used_memory_ids"] = ["lesson_missing"]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    try:
        TraceBackedMemoryStore.load_json(snapshot_path)
    except ValueError as exc:
        assert "candidate" in str(exc)
    else:
        raise AssertionError("loaded usage logs must require used ids to come from candidates")


def test_store_json_snapshot_rejects_usage_logs_with_unknown_candidate_memory_ids(tmp_path):
    snapshot_path = tmp_path / "ghost-usage-log-store.json"
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0].update(
        {
            "candidate_memory_ids": ["missing_memory"],
            "used_memory_ids": [],
            "risk": "none",
            "recommended_injection": "none",
            "candidate_memory_statuses": {"missing_memory": "active"},
        }
    )
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    try:
        TraceBackedMemoryStore.load_json(snapshot_path)
    except ValueError as exc:
        assert "unknown memory IDs" in str(exc)
    else:
        raise AssertionError("loaded usage logs must only reference stored runtime memory IDs")


def test_store_json_snapshot_rejects_invalid_usage_log_contract(tmp_path):
    invalid_cases = [
        ("empty-decision-id", {"decision_id": ""}, "decision_id"),
        ("invalid-mode", {"mode": "sandbox"}, "mode"),
        ("invalid-risk", {"risk": "severe"}, "risk"),
        ("invalid-injection", {"recommended_injection": "verbose"}, "recommended_injection"),
        ("non-string-candidate-id", {"candidate_memory_ids": [42]}, "candidate_memory_ids"),
    ]

    for name, mutation, expected_message in invalid_cases:
        snapshot_path = tmp_path / f"{name}.json"
        snapshot = v2_snapshot_with_usage_log()
        usage_logs = snapshot["usage_logs"]
        assert isinstance(usage_logs, list)
        usage_logs[0].update(mutation)
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        try:
            TraceBackedMemoryStore.load_json(snapshot_path)
        except ValueError as exc:
            assert expected_message in str(exc)
        else:
            raise AssertionError(f"loaded usage logs must reject invalid {expected_message}")


def test_store_snapshot_rejects_unhashable_candidate_memory_status():
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0]["candidate_memory_statuses"] = {"lesson_001": ["active"]}

    with pytest.raises(
        ValueError, match="candidate_memory_statuses.*status"
    ):
        TraceBackedMemoryStore.from_snapshot(snapshot)


@pytest.mark.parametrize(
    "statuses",
    [
        {},
        {"lesson_001": "active", "extra_memory": "active"},
    ],
    ids=["missing", "extra"],
)
def test_usage_log_candidate_statuses_must_exactly_match_candidates(
    statuses: dict[str, str],
):
    log = MemoryUsageLog(
        decision_id="decision_000001",
        run_id="run_001",
        mode="repair",
        candidate_memory_ids=["lesson_001"],
        used_memory_ids=[],
        blocked_memory_ids=[],
        reason="imported log",
        risk="none",
        recommended_injection="none",
        candidate_memory_statuses=statuses,
    )

    with pytest.raises(ValueError, match="candidate_memory_statuses must match candidates"):
        store_module._validate_usage_log(log)


def test_v2_snapshot_rejects_incomplete_candidate_status_evidence():
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    usage_logs[0]["candidate_memory_statuses"] = {}

    with pytest.raises(ValueError, match="candidate_memory_statuses must match candidates"):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_store_json_snapshot_rejects_duplicate_usage_log_decision_ids(tmp_path):
    snapshot = v2_snapshot_with_usage_log()
    usage_logs = snapshot["usage_logs"]
    assert isinstance(usage_logs, list)
    snapshot_path = tmp_path / "duplicate-usage-log-ids.json"
    snapshot_path.write_text(
        json.dumps({**snapshot, "usage_logs": [usage_logs[0], usage_logs[0]]}),
        encoding="utf-8",
    )

    try:
        TraceBackedMemoryStore.load_json(snapshot_path)
    except ValueError as exc:
        assert "decision_id" in str(exc)
    else:
        raise AssertionError("loaded usage logs must reject duplicate decision_id values")


def test_log_decision_avoids_duplicate_decision_ids_after_sparse_snapshot_import():
    seed = TraceBackedMemoryStore()
    trace = seed.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha="abc123",
            repo="agent-harness",
            eval_result="fail",
        )
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    seed.add_failure_case(case)
    seed.add_lesson(
        lesson_from_failure_case(
            case,
            lesson_id="lesson_001",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"repo": "agent-harness", "tool": "search_docs"},
        )
    )
    seed.record_trace(
        Trace(
            trace_id="trace_004",
            run_id="run_004",
            commit_sha="abc123",
            repo="agent-harness",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    snapshot = seed.to_snapshot()
    snapshot.pop("snapshot_version")
    snapshot["usage_logs"] = [
        {
            "decision_id": "decision_000001",
            "run_id": "run_001",
            "mode": "repair",
            "candidate_memory_ids": ["lesson_001"],
            "used_memory_ids": [],
            "blocked_memory_ids": [],
            "reason": "imported no-use decision",
            "risk": "none",
            "recommended_injection": "none",
        },
        {
            "decision_id": "decision_000003",
            "run_id": "run_004",
            "mode": "repair",
            "candidate_memory_ids": ["lesson_001"],
            "used_memory_ids": [],
            "blocked_memory_ids": [],
            "reason": "imported sparse no-use decision",
            "risk": "none",
            "recommended_injection": "none",
        },
    ]
    store = TraceBackedMemoryStore.from_snapshot(snapshot)
    decision = MemoryDecision(
        use_memory=False,
        allowed_memory_ids=[],
        blocked_memory_ids=[],
        reason="no relevant memory",
        risk="none",
        recommended_injection="none",
    )

    log = store.log_decision(
        "run_004",
        MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs"),
        ["lesson_001"],
        decision,
    )

    assert log.decision_id == "decision_000004"
    assert len({entry.decision_id for entry in store.usage_logs}) == len(store.usage_logs)


def test_store_json_snapshot_round_trips_failure_case_review_metadata(tmp_path):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail"))
    reviewed = review_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="invalid_tool_argument",
            symptom="planner called search_docs with null query",
        ),
        reviewed_by="jason",
        root_cause="planner prompt omitted the search_docs query contract",
        review_notes="Confirmed by manual trace inspection.",
        reviewed_at="2026-07-09T00:00:00Z",
    )
    store.add_failure_case(reviewed)

    snapshot_path = tmp_path / "reviewed-memory-store.json"
    store.save_json(snapshot_path)
    loaded = TraceBackedMemoryStore.load_json(snapshot_path)

    loaded_case = loaded.failure_cases["case_001"]
    assert loaded_case.reviewed_by == "jason"
    assert loaded_case.review_notes == "Confirmed by manual trace inspection."
    assert loaded_case.reviewed_at == "2026-07-09T00:00:00Z"


@pytest.mark.parametrize("invalid_query", [[], {}, (), 0, False, ["query"]])
def test_candidate_memories_requires_string_or_none_query(invalid_query: object):
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc")

    with pytest.raises(ValueError, match="query must be a string or None"):
        store.candidate_memories(
            context,
            query=invalid_query,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "invalid_kwargs",
    [
        {"task": ""},
        {"task": []},
        {"task": "repair", "query": {}},
        {"task": "repair", "context_summary": []},
    ],
)
def test_prepare_memory_rejects_malformed_inputs_without_consuming_request_id(
    invalid_kwargs: dict[str, object],
):
    store = TraceBackedMemoryStore()
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc")

    with pytest.raises(ValueError):
        store.prepare_memory(context, **invalid_kwargs)  # type: ignore[arg-type]

    request = store.prepare_memory(context, task="repair")
    assert request.request_id == "gate_request_000001"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("run_id", []),
        ("context", {}),
        ("decision", {}),
    ],
)
def test_log_decision_rejects_malformed_direct_inputs(
    field_name: str, invalid_value: object
):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc", repo="repo")
    )
    values: dict[str, object] = {
        "run_id": trace.run_id,
        "context": matching_context(trace),
        "decision": MemoryDecision(False, [], [], "not relevant", "none", "none"),
    }
    values[field_name] = invalid_value

    with pytest.raises(ValueError, match=field_name):
        store.log_decision(
            values["run_id"],  # type: ignore[arg-type]
            values["context"],  # type: ignore[arg-type]
            [],
            values["decision"],  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("request_id", []),
        ("context", {}),
        ("trace_id", []),
        ("trace_id", ""),
    ],
)
def test_finalize_memory_rejects_malformed_request_and_trace_identifiers(
    field_name: str, invalid_value: object
):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc", repo="repo")
    )
    request = store.prepare_memory(matching_context(trace), task="repair")
    trace_id: object = trace.trace_id
    if field_name in {"request_id", "context"}:
        request = replace(request, **{field_name: invalid_value})
    else:
        trace_id = invalid_value

    with pytest.raises(ValueError, match=field_name):
        store.finalize_memory(
            request,
            {
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "not relevant",
                "risk": "none",
                "recommended_injection": "none",
            },
            trace_id=trace_id,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [("eval_result", []), ("memory_caused_failure", [])],
)
def test_finalize_memory_rejects_malformed_outcome_fields(
    field_name: str, invalid_value: object
):
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc", repo="repo")
    )
    request = store.prepare_memory(matching_context(trace), task="repair")
    kwargs = {field_name: invalid_value}

    with pytest.raises(ValueError, match=field_name):
        store.finalize_memory(
            request,
            {
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "not relevant",
                "risk": "none",
                "recommended_injection": "none",
            },
            trace_id=trace.trace_id,
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("method_name", "message"),
    [
        ("record_trace", "trace must be exactly a Trace record"),
        ("add_failure_case", "failure case must be exactly a FailureCase record"),
        ("add_lesson", "lesson must be exactly a Lesson record"),
        ("add_project_policy", "project policy must be exactly a ProjectPolicy record"),
    ],
)
def test_record_writers_reject_wrong_record_types_before_field_access(
    method_name: str,
    message: str,
):
    store = TraceBackedMemoryStore()

    with pytest.raises(ValueError, match=message):
        getattr(store, method_name)(object())


def test_record_writers_require_exact_record_classes_not_subclasses():
    class DerivedTrace(Trace):
        pass

    class DerivedFailureCase(FailureCase):
        pass

    class DerivedLesson(Lesson):
        pass

    class DerivedProjectPolicy(ProjectPolicy):
        pass

    records = [
        (
            "record_trace",
            DerivedTrace("trace_derived", "run_derived", "abc"),
            "trace must be exactly a Trace record",
        ),
        (
            "add_failure_case",
            DerivedFailureCase(
                "case_derived", "trace_derived", "abc", "tool_error", "failed"
            ),
            "failure case must be exactly a FailureCase record",
        ),
        (
            "add_lesson",
            DerivedLesson(
                "lesson_derived",
                "case_derived",
                "rule",
                "procedural",
                {"repo": "repo"},
            ),
            "lesson must be exactly a Lesson record",
        ),
        (
            "add_project_policy",
            DerivedProjectPolicy("policy_derived", "rule", {"repo": "repo"}),
            "project policy must be exactly a ProjectPolicy record",
        ),
    ]

    store = TraceBackedMemoryStore()
    for method_name, record, message in records:
        with pytest.raises(ValueError, match=message):
            getattr(store, method_name)(record)


@pytest.mark.parametrize(
    ("method_name", "record_label"),
    [
        ("obsolete_failure_case", "failure case"),
        ("obsolete_lesson", "lesson"),
        ("obsolete_project_policy", "project policy"),
    ],
)
@pytest.mark.parametrize("record_id", [None, 0, "", []])
def test_lifecycle_lookups_reject_falsey_and_unhashable_ids(
    method_name: str,
    record_label: str,
    record_id: object,
):
    store = TraceBackedMemoryStore()

    with pytest.raises(
        ValueError,
        match=rf"{record_label} ID must be a non-empty string",
    ):
        getattr(store, method_name)(record_id)


def test_lessons_yaml_normalizes_constructor_errors_to_value_error(tmp_path: Path):
    path = tmp_path / "malformed-lessons.yaml"
    path.write_text(
        'lessons:\n  - lesson_id: "missing_required_fields"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid lesson record"):
        TraceBackedMemoryStore().load_lessons_yaml(path)


def test_lessons_yaml_normalizes_parser_type_errors_to_value_error(tmp_path: Path):
    path = tmp_path / "malformed-scope-lessons.yaml"
    path.write_text(
        (
            'lessons:\n'
            '  - lesson_id: "malformed_scope"\n'
            '    scope: 1\n'
            '      repo: "repo"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid lessons YAML"):
        TraceBackedMemoryStore().load_lessons_yaml(path)


def test_pr_memory_report_validates_context_before_scanning_empty_store():
    with pytest.raises(ValueError, match="context must be a MemoryContext"):
        TraceBackedMemoryStore().pr_memory_report(
            object(),  # type: ignore[arg-type]
            changed_fields=["tool"],
        )


@pytest.mark.parametrize(
    "changed_fields",
    ["tool", [""], ["   "], [1], ["tool", None]],
)
def test_pr_memory_report_validates_changed_fields_before_scanning_empty_store(
    changed_fields: object,
):
    context = MemoryContext(mode="repair", repo="repo", commit_sha="abc")

    with pytest.raises(
        ValueError,
        match="changed_fields must be a list of non-empty strings",
    ):
        TraceBackedMemoryStore().pr_memory_report(
            context,
            changed_fields=changed_fields,  # type: ignore[arg-type]
        )


def test_trace_accepts_json_serializable_large_integer_cost():
    large_cost = 10**1000
    store = TraceBackedMemoryStore()

    stored = store.record_trace(
        Trace(
            trace_id="trace_large_cost",
            run_id="run_large_cost",
            commit_sha="abc",
            cost_usd=large_cost,
        )
    )

    assert stored.cost_usd == large_cost
    assert store.traces[stored.trace_id].cost_usd == large_cost


def test_large_integer_cost_round_trips_through_snapshot_and_json(tmp_path: Path):
    large_cost = 10**1000
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_large_cost_round_trip",
            run_id="run_large_cost_round_trip",
            commit_sha="abc",
            cost_usd=large_cost,
        )
    )

    snapshot_loaded = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    path = tmp_path / "large-cost-store.json"
    store.save_json(path)
    json_loaded = TraceBackedMemoryStore.load_json(path)

    assert snapshot_loaded.traces["trace_large_cost_round_trip"].cost_usd == large_cost
    assert json_loaded.traces["trace_large_cost_round_trip"].cost_usd == large_cost


@pytest.mark.parametrize("sign", [1, -1], ids=["positive", "negative"])
def test_trace_rejects_huge_integer_cost_without_overflow(sign: int):
    cost_usd = sign * 10**10_000
    with pytest.raises(
        ValueError,
        match="cost_usd.*integer exceeds JSON serialization limits",
    ):
        TraceBackedMemoryStore().record_trace(
            Trace(
                trace_id="trace_huge_cost",
                run_id="run_huge_cost",
                commit_sha="abc",
                cost_usd=cost_usd,
            )
        )


@pytest.mark.parametrize("sign", [1, -1], ids=["positive", "negative"])
def test_trace_rejects_huge_integer_latency_before_json_serialization(sign: int):
    latency_ms = sign * 10**10_000

    with pytest.raises(ValueError, match="latency_ms.*JSON serialization limits"):
        TraceBackedMemoryStore().record_trace(
            Trace(
                trace_id="trace_huge_latency",
                run_id="run_huge_latency",
                commit_sha="abc",
                latency_ms=latency_ms,
            )
        )


@pytest.mark.parametrize(
    "field_name",
    ["retrieved_context", "tool_calls", "tool_outputs"],
)
def test_trace_json_fields_reject_nested_non_json_values(field_name: str):
    values: dict[str, object] = {
        "trace_id": f"trace_invalid_{field_name}",
        "run_id": f"run_invalid_{field_name}",
        "commit_sha": "abc",
        field_name: [{"nested": {"not", "json"}}],
    }

    with pytest.raises(ValueError, match=rf"trace {field_name}\[0\].*nested"):
        TraceBackedMemoryStore().record_trace(Trace(**values))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid_value",
    [
        ("tuple",),
        b"bytes",
        {1: "non-string key"},
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_trace_json_fields_reject_every_nested_non_json_semantic_value(
    invalid_value: object,
):
    with pytest.raises(ValueError, match=r"trace tool_calls\[0\].*payload"):
        TraceBackedMemoryStore().record_trace(
            Trace(
                trace_id="trace_invalid_nested_json",
                run_id="run_invalid_nested_json",
                commit_sha="abc",
                tool_calls=[{"payload": invalid_value}],
            )
        )


def test_trace_json_fields_reject_reference_cycles_with_a_stable_path():
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(
        ValueError,
        match=r"trace tool_outputs\[0\].*self.*reference cycle",
    ):
        TraceBackedMemoryStore().record_trace(
            Trace(
                trace_id="trace_cyclic_json",
                run_id="run_cyclic_json",
                commit_sha="abc",
                tool_outputs=[cyclic],
            )
        )


def test_trace_json_rejects_huge_nested_integer_before_storage():
    huge_integer = 10**10_000

    with pytest.raises(ValueError, match="integer exceeds JSON serialization limits"):
        TraceBackedMemoryStore().record_trace(
            Trace(
                trace_id="trace_huge_nested_integer",
                run_id="run_huge_nested_integer",
                commit_sha="abc",
                tool_outputs=[{"result": huge_integer}],
            )
        )


def test_trace_json_excessive_depth_is_a_value_error_not_recursion_error():
    root: dict[str, object] = {}
    cursor = root
    for _ in range(2_000):
        child: dict[str, object] = {}
        cursor["next"] = child
        cursor = child

    with pytest.raises(ValueError, match="maximum nesting depth"):
        TraceBackedMemoryStore().record_trace(
            Trace(
                trace_id="trace_deep_json",
                run_id="run_deep_json",
                commit_sha="abc",
                retrieved_context=[root],
            )
        )


def test_snapshot_import_rejects_nested_invalid_trace_json():
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(trace_id="trace_snapshot_json", run_id="run_snapshot_json", commit_sha="abc")
    )
    snapshot = store.to_snapshot()
    snapshot["traces"][0]["tool_outputs"] = [{"payload": {"not", "json"}}]

    with pytest.raises(ValueError, match=r"trace tool_outputs\[0\].*payload"):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_valid_nested_trace_json_can_snapshot_save_and_reload(tmp_path: Path):
    payload: dict[str, object] = {"leaf": [None, True, False, "text", 1, 1.5]}
    for index in range(40):
        payload = {f"level_{index}": [payload]}

    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_valid_nested_json",
            run_id="run_valid_nested_json",
            commit_sha="abc",
            retrieved_context=[payload],
            tool_calls=[{"name": "search", "arguments": payload}],
            tool_outputs=[{"result": payload}],
        )
    )

    snapshot = store.to_snapshot()
    path = tmp_path / "nested-store.json"
    store.save_json(path)
    loaded = TraceBackedMemoryStore.load_json(path)

    assert loaded.to_snapshot() == snapshot


def test_load_json_enforces_utf8_byte_budget_and_allows_trusted_override(
    tmp_path: Path,
):
    store, _trace, _case = store_with_verified_case()
    path = tmp_path / "bounded.snapshot.json"
    store.save_json(path)
    byte_count = len(path.read_bytes())

    assert TraceBackedMemoryStore.load_json(
        path,
        max_bytes=byte_count,
    ).to_snapshot() == store.to_snapshot()

    with pytest.raises(ValueError, match="memory store snapshot file exceeds"):
        TraceBackedMemoryStore.load_json(path, max_bytes=byte_count - 1)

    assert TraceBackedMemoryStore.load_json(
        path,
        max_bytes=None,
    ).to_snapshot() == store.to_snapshot()


def test_snapshot_record_budgets_are_checked_before_construction():
    snapshot = valid_snapshot_dict()
    collection_counts = [
        len(snapshot[key])
        for key in (
            "traces",
            "failure_cases",
            "lessons",
            "project_policies",
            "usage_logs",
        )
    ]
    per_collection = max(collection_counts)
    total = sum(collection_counts)

    restored = TraceBackedMemoryStore.from_snapshot(
        snapshot,
        max_records_per_collection=per_collection,
        max_total_records=total,
    )
    assert restored.to_snapshot() == snapshot

    with pytest.raises(ValueError, match="snapshot field .* maximum is 0"):
        TraceBackedMemoryStore.from_snapshot(
            snapshot,
            max_records_per_collection=0,
        )
    with pytest.raises(ValueError, match="snapshot contains .* maximum is"):
        TraceBackedMemoryStore.from_snapshot(
            snapshot,
            max_total_records=total - 1,
        )

    assert TraceBackedMemoryStore.from_snapshot(
        snapshot,
        max_records_per_collection=None,
        max_total_records=None,
    ).to_snapshot() == snapshot


@pytest.mark.parametrize("limit_name", [
    "max_records_per_collection",
    "max_total_records",
])
@pytest.mark.parametrize("limit", [True, -1, 1.5, "1"])
def test_snapshot_rejects_invalid_record_budgets(limit_name, limit):
    with pytest.raises(
        ValueError,
        match="must be a non-negative integer or None",
    ):
        TraceBackedMemoryStore.from_snapshot(
            valid_snapshot_dict(),
            **{limit_name: limit},
        )


def test_lessons_yaml_enforces_byte_and_record_budgets_before_mutation(
    tmp_path: Path,
):
    source, trace, case, lesson = store_with_active_lesson()
    path = tmp_path / "bounded-lessons.yaml"
    source.save_lessons_yaml(path)
    byte_count = len(path.read_bytes())

    target = TraceBackedMemoryStore()
    target.record_trace(trace)
    target.add_failure_case(case)
    before = target.to_snapshot()

    with pytest.raises(ValueError, match="active lessons YAML file exceeds"):
        target.load_lessons_yaml(path, max_bytes=byte_count - 1)
    assert target.to_snapshot() == before

    with pytest.raises(ValueError, match="more than 0 records"):
        target.load_lessons_yaml(path, max_lessons=0)
    assert target.to_snapshot() == before

    assert target.load_lessons_yaml(
        path,
        max_bytes=byte_count,
        max_lessons=1,
    ) == [lesson]


@pytest.mark.parametrize("limit", [True, -1, 1.5, "1"])
def test_lessons_yaml_rejects_invalid_record_budgets(tmp_path: Path, limit):
    source, _trace, _case, _lesson = store_with_active_lesson()
    path = tmp_path / "invalid-budget-lessons.yaml"
    source.save_lessons_yaml(path)

    with pytest.raises(
        ValueError,
        match="max_lessons must be a non-negative integer or None",
    ):
        TraceBackedMemoryStore().load_lessons_yaml(path, max_lessons=limit)
