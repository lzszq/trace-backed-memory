from pathlib import Path

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


def test_draft_from_passing_trace_is_rejected():
    trace = Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="pass")

    try:
        draft_failure_case_from_trace(trace, case_id="case_001")
    except ValueError as exc:
        assert "failed or errored" in str(exc)
    else:
        raise AssertionError("passing traces must not produce failure cases")


def test_loads_failure_taxonomy_from_repository_yaml():
    taxonomy = tbm.load_failure_taxonomy(Path(__file__).resolve().parents[1] / "memory" / "failure_taxonomy.yaml")

    assert "invalid_tool_argument" in taxonomy
    assert taxonomy["invalid_tool_argument"] == "Tool call arguments do not match the tool schema."
    assert "hallucinated_enum_value" in taxonomy


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
