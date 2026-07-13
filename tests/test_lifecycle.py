from datetime import datetime

import pytest
import trace_backed_memory as tbm
from trace_backed_memory import (
    FailureCase,
    Trace,
    draft_failure_case,
    lesson_from_failure_case,
    memory_item_from_failure_case,
    memory_item_from_lesson,
    obsolete_failure_case,
    obsolete_lesson,
    review_failure_case,
    verify_failure_case,
)


def test_failed_trace_can_become_verified_lesson_memory():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        branch="main",
        tool_schema_version="search_docs_v2",
        model="gpt-5.5-pro",
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        error="Invalid argument: query is required",
    )

    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
        root_cause="prompt omitted the non-empty query contract",
    )
    verified = verify_failure_case(
        draft,
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    lesson = lesson_from_failure_case(
        verified,
        lesson_id="lesson_001",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={"tool": "search_docs", "tool_schema_version": "search_docs_v2"},
        confidence=0.92,
    )

    memory = memory_item_from_lesson(lesson)

    assert draft.status == "draft"
    assert draft.source_trace_id == "trace_001"
    assert verified.status == "verified"
    assert verified.regression_passed is True
    assert memory.memory_id == "lesson_001"
    assert memory.status == "active"
    assert memory.source_case_id == "case_001"
    assert memory.scope == {"tool": "search_docs", "tool_schema_version": "search_docs_v2"}


@pytest.mark.parametrize("sign", [1, -1], ids=["positive", "negative"])
def test_lesson_contract_rejects_huge_integer_confidence_without_overflow(
    sign: int,
):
    confidence = sign * 10**10_000
    case = FailureCase(
        case_id="case_huge_confidence",
        source_trace_id="trace_huge_confidence",
        commit_sha="abc123",
        failure_type="tool_error",
        symptom="failed",
        fix="fixed",
        fix_commit_sha="def456",
        regression_passed=True,
        status="verified",
    )

    with pytest.raises(ValueError, match="confidence"):
        lesson_from_failure_case(
            case,
            lesson_id="lesson_huge_confidence",
            lesson_text="Use a bounded confidence value.",
            memory_type="procedural",
            scope={"repo": "repo"},
            confidence=confidence,
        )


def test_lesson_contract_rejects_json_serializable_large_integer_confidence():
    case = FailureCase(
        case_id="case_large_confidence",
        source_trace_id="trace_large_confidence",
        commit_sha="abc123",
        failure_type="tool_error",
        symptom="failed",
        fix="fixed",
        fix_commit_sha="def456",
        regression_passed=True,
        status="verified",
    )

    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        lesson_from_failure_case(
            case,
            lesson_id="lesson_large_confidence",
            lesson_text="Keep confidence bounded.",
            memory_type="procedural",
            scope={"repo": "repo"},
            confidence=10**1000,
        )


def test_lesson_requires_verified_failure_case():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
    )

    try:
        lesson_from_failure_case(
            draft,
            lesson_id="lesson_001",
            lesson_text="Always pass a non-empty query to search_docs.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    except ValueError as exc:
        assert "verified" in str(exc)
    else:
        raise AssertionError("draft cases must not produce active lessons")


def test_lesson_requires_verified_case_with_regression_pass():
    inconsistent_case = FailureCase(
        case_id="case_001",
        source_trace_id="trace_001",
        commit_sha="abc123",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
        status="verified",
        regression_passed=False,
    )

    try:
        lesson_from_failure_case(
            inconsistent_case,
            lesson_id="lesson_001",
            lesson_text="Always pass a non-empty query to search_docs.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
        )
    except ValueError as exc:
        assert "regression" in str(exc)
    else:
        raise AssertionError("lessons must require regression-backed verified cases")


def test_lesson_confidence_must_be_between_zero_and_one():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    verified = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="invalid_tool_argument",
            symptom="planner called search_docs with null query",
        ),
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )

    for confidence in [-0.1, 1.1]:
        try:
            lesson_from_failure_case(
                verified,
                lesson_id=f"lesson_{confidence}",
                lesson_text="Always pass a non-empty query to search_docs.",
                memory_type="procedural",
                scope={"tool": "search_docs"},
                confidence=confidence,
            )
        except ValueError as exc:
            assert "confidence" in str(exc)
        else:
            raise AssertionError("lesson confidence must be bounded between 0 and 1")


def test_lesson_confidence_must_be_numeric_not_boolean():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    case = verify_failure_case(
        draft_failure_case(trace, case_id="case_001", failure_type="invalid_tool_argument", symptom="bad query"),
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )

    try:
        lesson_from_failure_case(
            case,
            lesson_id="lesson_bool_confidence",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"tool": "search_docs"},
            confidence=True,
        )
    except ValueError as exc:
        assert "confidence" in str(exc)
    else:
        raise AssertionError("lesson confidence must reject boolean values")


def test_lesson_scope_fields_must_be_known_non_empty_strings():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    verified = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="invalid_tool_argument",
            symptom="planner called search_docs with null query",
        ),
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )

    invalid_scopes = [
        {"unknown_field": "search_docs"},
        {"tool": ""},
        {"tool": ["search_docs"]},
    ]

    for scope in invalid_scopes:
        try:
            lesson_from_failure_case(
                verified,
                lesson_id=f"lesson_{len(str(scope))}",
                lesson_text="Always pass a non-empty query to search_docs.",
                memory_type="procedural",
                scope=scope,
            )
        except ValueError as exc:
            assert "scope" in str(exc)
        else:
            raise AssertionError("lesson scope fields must be known non-empty strings")


def test_verification_requires_regression_pass():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
    )

    try:
        verify_failure_case(
            draft,
            fix="added schema example",
            fix_commit_sha="def456",
            regression_passed=False,
        )
    except ValueError as exc:
        assert "regression" in str(exc)
    else:
        raise AssertionError("verified cases must require a passing regression")


def test_only_draft_failure_cases_can_be_verified():
    for status in ["verified", "obsolete"]:
        case = FailureCase(
            case_id=f"case_{status}",
            source_trace_id="trace_001",
            commit_sha="abc123",
            failure_type="invalid_tool_argument",
            symptom="planner called search_docs with null query",
            status=status,
        )

        try:
            verify_failure_case(
                case,
                fix="added schema example",
                fix_commit_sha="def456",
                regression_passed=True,
            )
        except ValueError as exc:
            assert "draft" in str(exc)
        else:
            raise AssertionError("only draft failure cases should be verified")


def test_draft_failure_case_can_be_manually_reviewed_before_verification():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="evaluator_mismatch",
        symptom="unclear failure",
    )

    reviewed = review_failure_case(
        draft,
        reviewed_by="jason",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
        root_cause="planner prompt omitted the search_docs query contract",
        review_notes="Confirmed by inspecting the failed tool call arguments.",
        reviewed_at="2026-07-09T00:00:00Z",
    )
    verified = verify_failure_case(
        reviewed,
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )

    assert reviewed.status == "draft"
    assert reviewed.failure_type == "invalid_tool_argument"
    assert reviewed.root_cause == "planner prompt omitted the search_docs query contract"
    assert reviewed.reviewed_by == "jason"
    assert reviewed.review_notes == "Confirmed by inspecting the failed tool call arguments."
    assert reviewed.reviewed_at == "2026-07-09T00:00:00Z"
    assert verified.status == "verified"
    assert verified.reviewed_by == "jason"


def test_manual_review_defaults_review_timestamp_to_utc():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="evaluator_mismatch",
        symptom="unclear failure",
    )

    reviewed = review_failure_case(
        draft,
        reviewed_by="jason",
        root_cause="planner prompt omitted the search_docs query contract",
    )

    assert reviewed.reviewed_at is not None
    assert reviewed.reviewed_at.endswith("Z")
    datetime.fromisoformat(reviewed.reviewed_at.replace("Z", "+00:00"))


def test_only_draft_failure_cases_can_be_manually_reviewed():
    case = FailureCase(
        case_id="case_001",
        source_trace_id="trace_001",
        commit_sha="abc123",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
        status="verified",
        regression_passed=True,
    )

    try:
        review_failure_case(case, reviewed_by="jason", root_cause="prompt omitted contract")
    except ValueError as exc:
        assert "draft" in str(exc)
    else:
        raise AssertionError("only draft cases should be reviewable")


def test_manual_review_requires_reviewer_and_root_cause():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
    )

    for kwargs in [
        {"reviewed_by": "", "root_cause": "prompt omitted contract"},
        {"reviewed_by": "jason", "root_cause": ""},
    ]:
        try:
            review_failure_case(draft, **kwargs)
        except ValueError as exc:
            assert "review" in str(exc) or "root_cause" in str(exc)
        else:
            raise AssertionError("manual review must require reviewer and root cause")


def test_failure_case_and_lesson_can_be_marked_obsolete():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    verified = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_001",
            failure_type="invalid_tool_argument",
            symptom="planner called search_docs with null query",
        ),
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    lesson = lesson_from_failure_case(
        verified,
        lesson_id="lesson_001",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={"tool": "search_docs"},
    )

    obsolete_case = obsolete_failure_case(verified)
    obsolete_memory = memory_item_from_lesson(obsolete_lesson(lesson))

    assert obsolete_case.status == "obsolete"
    assert obsolete_memory.status == "obsolete"


def test_project_policy_can_become_policy_memory_item():
    policy = tbm.ProjectPolicy(
        policy_id="project_policy_001",
        policy_text="Planner responses must include a tool-call rationale.",
        scope={"prompt_family": "planner"},
        confidence=0.88,
        sensitive=True,
    )

    memory = tbm.memory_item_from_project_policy(policy)

    assert memory.memory_id == "project_policy_001"
    assert memory.memory_type == "policy"
    assert memory.status == "active"
    assert memory.source_policy_id == "project_policy_001"
    assert memory.text == "Planner responses must include a tool-call rationale."
    assert memory.confidence == 0.88
    assert memory.sensitive is True


def test_verified_failure_case_can_become_runtime_memory_item_with_trace_scope():
    trace = Trace(
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
    case = verify_failure_case(
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

    memory = memory_item_from_failure_case(case, trace)

    assert memory.memory_id == "case_001"
    assert memory.status == "verified"
    assert memory.memory_type == "episodic"
    assert memory.source_case_id == "case_001"
    assert memory.source_trace_id == "trace_001"
    assert memory.scope == {
        "repo": "agent-harness",
        "tenant": "tenant_a",
        "branch": "main",
        "prompt_version": "planner_v3",
        "prompt_family": "planner",
        "tool": "search_docs",
        "tool_schema_version": "search_docs_v2",
        "model": "gpt-5.5-pro",
        "eval_suite": "tool_calling_regression",
        "failure_type": "invalid_tool_argument",
    }
    assert "planner called search_docs with null query" in memory.text
    assert "prompt omitted the non-empty query contract" in memory.text
    assert "added schema example" in memory.text


@pytest.mark.parametrize(
    ("eval_suite", "input_hash", "expected_identity"),
    [
        ("benchmark-suite", "sha256:example", ("benchmark-suite", "sha256:example")),
        ("benchmark-suite", None, (None, None)),
        (None, "sha256:example", (None, None)),
        ("", "sha256:example", (None, None)),
        ("benchmark-suite", "", (None, None)),
        (True, "sha256:example", (None, None)),
        ("benchmark-suite", True, (None, None)),
        ("x" * 513, "sha256:example", (None, None)),
        ("benchmark-suite", "x" * 513, (None, None)),
    ],
)
def test_failure_case_memory_propagates_only_complete_raw_trace_source_identity(
    eval_suite: object,
    input_hash: object,
    expected_identity: tuple[str | None, str | None],
):
    trace = Trace(
        trace_id="trace_source_identity",
        run_id="run_source_identity",
        commit_sha="abc123",
        eval_result="fail",
        eval_suite=eval_suite,  # type: ignore[arg-type]
        input_hash=input_hash,  # type: ignore[arg-type]
    )
    case = verify_failure_case(
        draft_failure_case(
            trace,
            case_id="case_source_identity",
            failure_type="tool_error",
            symptom="failed",
        ),
        fix="fixed",
        fix_commit_sha="def456",
        regression_passed=True,
    )

    memory = memory_item_from_failure_case(case, trace)

    assert (memory.source_eval_suite, memory.source_input_hash) == expected_identity


def test_lesson_memory_optionally_propagates_complete_trace_source_identity():
    lesson = tbm.Lesson(
        lesson_id="lesson_source_identity",
        source_case_id="case_source_identity",
        lesson_text="Use a non-empty query.",
        memory_type="procedural",
        scope={"repo": "repo"},
    )
    complete_trace = Trace(
        trace_id="trace_complete",
        run_id="run_complete",
        commit_sha="abc123",
        eval_suite="benchmark-suite",
        input_hash="sha256:example",
    )
    incomplete_trace = Trace(
        trace_id="trace_incomplete",
        run_id="run_incomplete",
        commit_sha="abc123",
        eval_suite="benchmark-suite",
    )

    legacy_memory = memory_item_from_lesson(lesson)
    complete_memory = memory_item_from_lesson(lesson, source_trace=complete_trace)
    incomplete_memory = memory_item_from_lesson(lesson, source_trace=incomplete_trace)

    assert (legacy_memory.source_eval_suite, legacy_memory.source_input_hash) == (None, None)
    assert (complete_memory.source_eval_suite, complete_memory.source_input_hash) == (
        "benchmark-suite",
        "sha256:example",
    )
    assert (incomplete_memory.source_eval_suite, incomplete_memory.source_input_hash) == (
        None,
        None,
    )

    policy_memory = tbm.memory_item_from_project_policy(
        tbm.ProjectPolicy(
            policy_id="policy_source_identity",
            policy_text="Use an approved prompt contract.",
            scope={"repo": "repo"},
        )
    )
    assert (policy_memory.source_eval_suite, policy_memory.source_input_hash) == (
        None,
        None,
    )


def test_draft_failure_case_rejects_empty_required_fields():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
    )
    invalid_cases = [
        (
            {"case_id": "", "failure_type": "invalid_tool_argument", "symptom": "null query"},
            "case_id",
        ),
        (
            {"case_id": "case_001", "failure_type": "", "symptom": "null query"},
            "failure_type",
        ),
        (
            {"case_id": "case_001", "failure_type": "invalid_tool_argument", "symptom": ""},
            "symptom",
        ),
    ]

    for kwargs, expected_field in invalid_cases:
        try:
            draft_failure_case(trace, **kwargs)
        except ValueError as exc:
            assert expected_field in str(exc)
        else:
            raise AssertionError(f"draft failure cases must reject empty {expected_field}")


def test_lesson_from_failure_case_rejects_invalid_public_fields():
    case = FailureCase(
        case_id="case_001",
        source_trace_id="trace_001",
        commit_sha="abc123",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
        status="verified",
        regression_passed=True,
    )
    invalid_cases = [
        (
            {"lesson_id": "", "memory_type": "procedural"},
            "lesson_id",
        ),
        (
            {"lesson_id": "lesson_001", "memory_type": "unsupported"},
            "memory_type",
        ),
    ]

    for kwargs, expected_field in invalid_cases:
        try:
            lesson_from_failure_case(
                case,
                lesson_text="Always pass a non-empty query to search_docs.",
                scope={"tool": "search_docs"},
                **kwargs,
            )
        except ValueError as exc:
            assert expected_field in str(exc)
        else:
            raise AssertionError(f"lessons must reject invalid {expected_field}")


def test_memory_item_from_project_policy_rejects_invalid_public_fields():
    invalid_policies = [
        (
            tbm.ProjectPolicy(
                policy_id="",
                policy_text="Planner responses must include a tool-call rationale.",
                scope={"prompt_family": "planner"},
            ),
            "policy_id",
        ),
        (
            tbm.ProjectPolicy(
                policy_id="project_policy_001",
                policy_text="Planner responses must include a tool-call rationale.",
                scope={"prompt_family": "planner"},
                status="unsupported",
            ),
            "status",
        ),
    ]

    for policy, expected_field in invalid_policies:
        try:
            tbm.memory_item_from_project_policy(policy)
        except ValueError as exc:
            assert expected_field in str(exc)
        else:
            raise AssertionError(f"project policies must reject invalid {expected_field}")
