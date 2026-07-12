import json
import os
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory.store as store_module
from trace_backed_memory import (
    FailureCase,
    Lesson,
    MemoryContext,
    MemoryDecision,
    MemoryUsageLog,
    PRCaseProvenance,
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


def allow_decision(memory_id: str) -> dict[str, object]:
    return {
        "use_memory": True,
        "allowed_memory_ids": [memory_id],
        "blocked_memory_ids": [],
        "reason": "direct match",
        "risk": "low",
        "recommended_injection": "short_summary",
    }


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
        ({"semantic_scores": {"lesson_001": False}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
        ({"semantic_scores": {"lesson_001": float("inf")}, "max_candidates": 1}, "semantic score for 'lesson_001' must be a finite number"),
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
    [None, "tool", [""], ["   "], [1], ["tool", None]],
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
