import json
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
    snapshot = {
        "traces": [],
        "failure_cases": [],
        "lessons": [],
        "project_policies": [],
        "usage_logs": [
            MemoryUsageLog(
                decision_id="decision_001",
                run_id="run_001",
                mode="repair",
                candidate_memory_ids=[],
                used_memory_ids=[],
                blocked_memory_ids=[],
                reason="no memory",
                risk="none",
                recommended_injection="none",
                memory_caused_failure=invalid_boolean,  # type: ignore[arg-type]
            ).__dict__
        ],
    }

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
    snapshot_path.write_text(
        """
        {
          "traces": [],
          "failure_cases": [],
          "lessons": [],
          "project_policies": [],
          "usage_logs": [
            {
              "decision_id": "decision_000001",
              "run_id": "run_001",
              "mode": "repair",
              "candidate_memory_ids": ["lesson_001"],
              "used_memory_ids": ["lesson_missing"],
              "blocked_memory_ids": [],
              "reason": "inconsistent imported log",
              "risk": "medium",
              "recommended_injection": "short_summary",
              "eval_result": "pass"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    try:
        TraceBackedMemoryStore.load_json(snapshot_path)
    except ValueError as exc:
        assert "candidate" in str(exc)
    else:
        raise AssertionError("loaded usage logs must require used ids to come from candidates")


def test_store_json_snapshot_rejects_usage_logs_with_unknown_candidate_memory_ids(tmp_path):
    snapshot_path = tmp_path / "ghost-usage-log-store.json"
    snapshot_path.write_text(
        """
        {
          "traces": [],
          "failure_cases": [],
          "lessons": [],
          "project_policies": [],
          "usage_logs": [
            {
              "decision_id": "decision_000001",
              "run_id": "run_001",
              "mode": "repair",
              "candidate_memory_ids": ["missing_memory"],
              "used_memory_ids": [],
              "blocked_memory_ids": [],
              "reason": "ghost candidate",
              "risk": "none",
              "recommended_injection": "none",
              "eval_result": "unknown"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    try:
        TraceBackedMemoryStore.load_json(snapshot_path)
    except ValueError as exc:
        assert "unknown memory IDs" in str(exc)
    else:
        raise AssertionError("loaded usage logs must only reference stored runtime memory IDs")


def test_store_json_snapshot_rejects_invalid_usage_log_contract(tmp_path):
    valid_log = {
        "decision_id": "decision_000001",
        "run_id": "run_001",
        "mode": "repair",
        "candidate_memory_ids": ["lesson_001"],
        "used_memory_ids": [],
        "blocked_memory_ids": [],
        "reason": "imported log",
        "risk": "low",
        "recommended_injection": "none",
        "eval_result": "unknown",
    }
    invalid_cases = [
        ("empty-decision-id", {"decision_id": ""}, "decision_id"),
        ("invalid-mode", {"mode": "sandbox"}, "mode"),
        ("invalid-risk", {"risk": "severe"}, "risk"),
        ("invalid-injection", {"recommended_injection": "verbose"}, "recommended_injection"),
        ("non-string-candidate-id", {"candidate_memory_ids": [42]}, "candidate_memory_ids"),
    ]

    for name, mutation, expected_message in invalid_cases:
        snapshot_path = tmp_path / f"{name}.json"
        invalid_log = {**valid_log, **mutation}
        snapshot_path.write_text(
            json.dumps(
                {
                    "traces": [],
                    "failure_cases": [],
                    "lessons": [],
                    "project_policies": [],
                    "usage_logs": [invalid_log],
                }
            ),
            encoding="utf-8",
        )

        try:
            TraceBackedMemoryStore.load_json(snapshot_path)
        except ValueError as exc:
            assert expected_message in str(exc)
        else:
            raise AssertionError(f"loaded usage logs must reject invalid {expected_message}")


def test_store_snapshot_rejects_unhashable_candidate_memory_status():
    snapshot = {
        "traces": [],
        "failure_cases": [],
        "lessons": [],
        "project_policies": [],
        "usage_logs": [
            {
                "decision_id": "decision_000001",
                "run_id": "run_001",
                "mode": "repair",
                "candidate_memory_ids": ["lesson_001"],
                "used_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "malformed imported status evidence",
                "risk": "low",
                "recommended_injection": "none",
                "candidate_memory_statuses": {"lesson_001": ["active"]},
            }
        ],
    }

    with pytest.raises(
        ValueError, match="candidate_memory_statuses.*status"
    ):
        TraceBackedMemoryStore.from_snapshot(snapshot)


def test_store_json_snapshot_rejects_duplicate_usage_log_decision_ids(tmp_path):
    usage_log = {
        "decision_id": "decision_000001",
        "run_id": "run_001",
        "mode": "repair",
        "candidate_memory_ids": [],
        "used_memory_ids": [],
        "blocked_memory_ids": [],
        "reason": "imported log",
        "risk": "none",
        "recommended_injection": "none",
    }
    snapshot_path = tmp_path / "duplicate-usage-log-ids.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "traces": [],
                "failure_cases": [],
                "lessons": [],
                "project_policies": [],
                "usage_logs": [usage_log, usage_log],
            }
        ),
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
            "run_id": "run_003",
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
