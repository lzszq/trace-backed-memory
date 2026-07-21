from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory import Trace, classify_failure_type, draft_failure_case_from_trace


def test_classifies_invalid_tool_argument_from_tool_call_error():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_calls=[
            {
                "name": "search_docs",
                "arguments": {"query": None},
                "error": "Invalid argument: query is required",
            }
        ],
    )

    assert classify_failure_type(trace) == "invalid_tool_argument"


def test_classifies_invalid_tool_argument_from_tool_output_error():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_outputs=[
            {
                "name": "search_docs",
                "error": "Invalid argument: query is required",
            }
        ],
    )

    assert classify_failure_type(trace) == "invalid_tool_argument"


@pytest.mark.parametrize(
    "error",
    [
        "Missing required argument: query",
        "Required parameter 'query' was not supplied",
        "Required field: query",
        "'query' is a required property",
    ],
)
@pytest.mark.parametrize("record_field", ["tool_calls", "tool_outputs"])
def test_classifies_only_explicit_required_tool_argument_markers(
    error: str, record_field: str
):
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="error",
        **{record_field: [{"name": "search_docs", "error": error}]},
    )

    assert classify_failure_type(trace) == "invalid_tool_argument"


@pytest.mark.parametrize(
    ("eval_result", "error", "expected"),
    [
        ("fail", "Required permission denied", "evaluator_mismatch"),
        ("error", "Authentication is required", "unknown"),
    ],
)
@pytest.mark.parametrize("record_field", ["tool_calls", "tool_outputs"])
def test_required_non_argument_tool_errors_use_existing_fallbacks(
    eval_result: str, error: str, expected: str, record_field: str
):
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result=eval_result,
        **{record_field: [{"name": "secure_lookup", "error": error}]},
    )

    assert classify_failure_type(trace) == expected


def test_required_argument_marker_does_not_span_separate_tool_errors():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="error",
        tool_calls=[
            {"name": "search_docs", "error": "Additional approval is required"}
        ],
        tool_outputs=[
            {"name": "audit_log", "error": "Argument logging failed"}
        ],
    )

    assert classify_failure_type(trace) == "unknown"


def test_non_error_tool_output_content_does_not_influence_classification():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_outputs=[
            {
                "name": "stale_result_reader",
                "result": {
                    "example": "Invalid argument: query is required",
                },
            }
        ],
    )

    assert classify_failure_type(trace) == "evaluator_mismatch"
    case = draft_failure_case_from_trace(trace, case_id="case_001")
    assert case.symptom == "evaluator_mismatch: trace trace_001 failed"
    assert case.root_cause is None


def test_classifies_missing_required_context_from_empty_retrieval():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        retrieved_context=[],
        error="The agent answered without retrieving required context.",
    )

    assert classify_failure_type(trace) == "missing_required_context"


def test_missing_required_context_takes_precedence_over_required_tool_call_text():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": "billing policy"}}],
        error="Required context is missing before answering.",
    )

    assert classify_failure_type(trace) == "missing_required_context"


def test_trace_error_precedence_is_preserved_for_tool_output_errors():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        error="Required context is missing before answering.",
        tool_outputs=[
            {
                "name": "search_docs",
                "error": "Required field: query",
            }
        ],
    )

    assert classify_failure_type(trace) == "missing_required_context"
    case = draft_failure_case_from_trace(trace, case_id="case_001")
    assert case.symptom == (
        "missing_required_context: tool call failed for search_docs"
    )
    assert case.root_cause == "Required context is missing before answering."


def test_classifier_covers_stale_context_with_repository_taxonomy():
    taxonomy = tbm.load_failure_taxonomy(Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml")
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        error="The answer relied on old retrieved context from a previous commit.",
    )

    assert classify_failure_type(trace, taxonomy=taxonomy) == "stale_context"


def test_classifier_covers_prompt_contract_violation_with_repository_taxonomy():
    taxonomy = tbm.load_failure_taxonomy(Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml")
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        error="The response ignored the required JSON output contract in the prompt.",
    )

    assert classify_failure_type(trace, taxonomy=taxonomy) == "prompt_contract_violation"


def test_classifier_covers_hallucinated_enum_value_with_repository_taxonomy():
    taxonomy = tbm.load_failure_taxonomy(Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml")
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        error="The model invented status value 'approved' outside the schema options.",
    )

    assert classify_failure_type(trace, taxonomy=taxonomy) == "hallucinated_enum_value"


def test_classifier_covers_evaluator_mismatch_with_repository_taxonomy():
    taxonomy = tbm.load_failure_taxonomy(Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml")
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        error="The evaluator rejected the output due to rubric mismatch.",
    )

    assert classify_failure_type(trace, taxonomy=taxonomy) == "evaluator_mismatch"


def test_drafts_failure_case_from_failed_trace():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_calls=[
            {
                "name": "search_docs",
                "arguments": {"query": None},
                "error": "Invalid argument: query is required",
            }
        ],
    )

    case = draft_failure_case_from_trace(trace, case_id="case_001")

    assert case.case_id == "case_001"
    assert case.source_trace_id == "trace_001"
    assert case.failure_type == "invalid_tool_argument"
    assert case.status == "draft"
    assert "search_docs" in case.symptom
    assert "Invalid argument" in case.root_cause


def test_drafts_failure_case_from_tool_output_evidence():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_outputs=[
            {
                "name": "search_docs",
                "error": "Invalid argument: query is required",
            }
        ],
    )

    case = draft_failure_case_from_trace(trace, case_id="case_001")

    assert case.failure_type == "invalid_tool_argument"
    assert case.symptom == "invalid_tool_argument: tool call failed for search_docs"
    assert case.root_cause == "Invalid argument: query is required"


def test_successful_named_tool_call_does_not_replace_trace_error_symptom():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="error",
        error="Model provider timed out.",
        tool_calls=[
            {
                "name": "search_docs",
                "arguments": {"query": "billing policy"},
            }
        ],
    )

    case = draft_failure_case_from_trace(trace, case_id="case_001")

    assert case.failure_type == "unknown"
    assert case.symptom == "unknown: Model provider timed out."
    assert case.root_cause == "Model provider timed out."


def test_successful_named_tool_call_without_error_uses_trace_fallback_symptom():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_calls=[
            {
                "name": "search_docs",
                "arguments": {"query": "billing policy"},
            }
        ],
    )

    case = draft_failure_case_from_trace(trace, case_id="case_001")

    assert case.failure_type == "evaluator_mismatch"
    assert case.symptom == "evaluator_mismatch: trace trace_001 failed"
    assert case.root_cause is None


def test_failed_tool_output_names_symptom_after_successful_named_call():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="error",
        tool_calls=[
            {
                "name": "search_docs",
                "arguments": {"query": "billing policy"},
            }
        ],
        tool_outputs=[
            {
                "name": "write_report",
                "error": "Required field: destination",
            }
        ],
    )

    case = draft_failure_case_from_trace(trace, case_id="case_001")

    assert case.failure_type == "invalid_tool_argument"
    assert case.symptom == (
        "invalid_tool_argument: tool call failed for write_report"
    )
    assert case.root_cause == "Required field: destination"


def test_tool_call_evidence_precedes_tool_output_evidence_in_drafts():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_calls=[
            {
                "name": "search_docs",
                "error": "Invalid argument from tool call",
            }
        ],
        tool_outputs=[
            {
                "name": "fallback_search",
                "error": "Invalid argument from tool output",
            }
        ],
    )

    case = draft_failure_case_from_trace(trace, case_id="case_001")

    assert case.symptom == "invalid_tool_argument: tool call failed for search_docs"
    assert case.root_cause == "Invalid argument from tool call"


def test_draft_from_passing_trace_is_rejected():
    trace = Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="pass")

    try:
        draft_failure_case_from_trace(trace, case_id="case_001")
    except ValueError as exc:
        assert "failed or errored" in str(exc)
    else:
        raise AssertionError("passing traces must not produce failure cases")


def test_loads_failure_taxonomy_from_repository_yaml():
    path = Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml"
    taxonomy = tbm.load_failure_taxonomy(path)

    assert "invalid_tool_argument" in taxonomy
    assert taxonomy["invalid_tool_argument"] == "Tool call arguments do not match the tool schema."
    assert "hallucinated_enum_value" in taxonomy


def test_loads_the_same_failure_taxonomy_from_installed_resources_by_default():
    path = Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml"

    assert tbm.load_failure_taxonomy() == tbm.load_failure_taxonomy(path)


def test_failure_taxonomy_rejects_duplicate_descriptions(tmp_path):
    path = tmp_path / "duplicate-description.yaml"
    path.write_text(
        (
            "failure_types:\n"
            "  - id: invalid_tool_argument\n"
            "    description: first description\n"
            "    description: replacement description\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="duplicate failure taxonomy description: invalid_tool_argument",
    ):
        tbm.load_failure_taxonomy(path)


def test_classifier_can_require_taxonomy_membership():
    trace = Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha="abc123",
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "error": "Invalid argument: query is required"}],
    )

    try:
        classify_failure_type(trace, taxonomy={"missing_required_context": "Only this type is allowed."})
    except ValueError as exc:
        assert "taxonomy" in str(exc)
        assert "invalid_tool_argument" in str(exc)
    else:
        raise AssertionError("taxonomy validation must reject classifier outputs not present in taxonomy")


def test_failure_taxonomy_enforces_byte_and_record_budgets():
    path = Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml"
    byte_count = len(path.read_bytes())
    expected = tbm.load_failure_taxonomy()

    assert tbm.load_failure_taxonomy(
        path,
        max_bytes=byte_count,
        max_failure_types=len(expected),
    ) == expected
    assert tbm.load_failure_taxonomy(
        max_bytes=byte_count,
        max_failure_types=len(expected),
    ) == expected

    with pytest.raises(ValueError, match="failure taxonomy YAML file exceeds"):
        tbm.load_failure_taxonomy(path, max_bytes=byte_count - 1)
    with pytest.raises(ValueError, match="more than .* failure types"):
        tbm.load_failure_taxonomy(
            path,
            max_failure_types=len(expected) - 1,
        )


@pytest.mark.parametrize("limit", [True, -1, 1.5, "1"])
def test_failure_taxonomy_rejects_invalid_record_budgets(limit):
    with pytest.raises(
        ValueError,
        match="max_failure_types must be a non-negative integer or None",
    ):
        tbm.load_failure_taxonomy(max_failure_types=limit)
