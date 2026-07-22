import json
from dataclasses import replace

import pytest

import trace_backed_memory as tbm


def _execution_store(
    **current_trace_changes: object,
) -> tuple[
    tbm.TraceBackedMemoryStore,
    tbm.Trace,
    tbm.Lesson,
    tbm.MemoryContext,
]:
    store = tbm.TraceBackedMemoryStore()
    source = store.record_trace(
        tbm.Trace(
            trace_id="trace_execution_source",
            run_id="run_execution_source",
            commit_sha="commit_execution",
            repo="repo",
            tenant="tenant_a",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = store.add_failure_case(
        tbm.verify_failure_case(
            tbm.review_failure_case(
                tbm.draft_failure_case(
                    source,
                    case_id="case_execution",
                    failure_type="invalid_tool_argument",
                    symptom="search_docs received an empty query",
                ),
                reviewed_by="test-reviewer",
                root_cause="the prompt omitted the query contract",
                reviewed_at="2026-07-22T00:00:00Z",
            ),
            fix="require a non-empty query",
            fix_commit_sha="commit_execution_fix",
            regression_passed=True,
        )
    )
    lesson = store.add_lesson(
        tbm.lesson_from_failure_case(
            case,
            lesson_id="lesson_execution",
            lesson_text="Always pass a non-empty query to search_docs.",
            memory_type="procedural",
            scope={"repo": "repo", "tenant": "tenant_a", "tool": "search_docs"},
        )
    )
    changes = {
        "trace_id": "trace_execution_current",
        "run_id": "run_execution_current",
        "eval_result": "unknown",
        "output_hash": None,
        "tool_outputs": [],
        "latency_ms": None,
        "cost_usd": None,
        "error": None,
        "trace_uri": None,
        **current_trace_changes,
    }
    current = store.record_trace(replace(source, **changes))
    context = tbm.MemoryContext(
        mode="repair",
        repo="repo",
        tenant="tenant_a",
        commit_sha="commit_execution",
        tool="search_docs",
    )
    return store, current, lesson, context


def _allow(memory_id: str) -> dict[str, object]:
    return {
        "use_memory": True,
        "allowed_memory_ids": [memory_id],
        "blocked_memory_ids": [],
        "reason": "the verified lesson directly applies",
        "risk": "low",
        "recommended_injection": "short_summary",
    }


def _decline_json() -> str:
    return json.dumps(
        {
            "use_memory": False,
            "allowed_memory_ids": [],
            "blocked_memory_ids": [],
            "reason": "memory is not needed",
            "risk": "none",
            "recommended_injection": "none",
        }
    )


def test_run_memory_execution_orders_callbacks_and_completes_store_records():
    store, current, lesson, context = _execution_store()
    events: list[tuple[str, object]] = []

    def decide(request):
        events.append(("decision", request))
        assert isinstance(request, tbm.MemoryGateRequest)
        assert request.candidate_memory_ids == (lesson.lesson_id,)
        assert lesson.lesson_id in request.prompt
        return _allow(lesson.lesson_id)

    def execute(gated):
        events.append(("execution", gated))
        assert isinstance(gated, tbm.GatedMemoryResult)
        assert gated.trace_id == current.trace_id
        assert gated.allowed_memory_ids == (lesson.lesson_id,)
        assert "Always pass a non-empty query" in gated.snippet
        return tbm.MemoryRunMeasurement(
            eval_result="pass",
            output_hash="sha256:execution-output",
            tool_outputs=({"documents": 3},),
            latency_ms=0,
            cost_usd=0.0,
            trace_uri="memory://trace/execution",
        )

    completion = tbm.run_memory_execution(
        store,
        context=context,
        trace_id=current.trace_id,
        task="repair the search_docs call",
        decide=decide,
        execute=execute,
        query="search_docs empty query",
    )

    assert [event for event, _value in events] == ["decision", "execution"]
    request = events[0][1]
    gated = events[1][1]
    assert gated.request_id == request.request_id
    assert completion.trace.eval_result == "pass"
    assert completion.trace.output_hash == "sha256:execution-output"
    assert completion.trace.tool_outputs == [{"documents": 3}]
    assert completion.trace.latency_ms == 0
    assert completion.trace.cost_usd == 0.0
    assert completion.trace.trace_uri == "memory://trace/execution"
    assert completion.usage_log.decision_id == gated.decision_id
    assert completion.usage_log.trace_id == current.trace_id
    assert completion.usage_log.used_memory_ids == [lesson.lesson_id]
    assert completion.usage_log.eval_result == "pass"
    assert store.memory_run_audits()[0].status == "complete"


def test_run_memory_execution_supports_declined_memory_and_json_decision():
    store, current, _lesson, context = _execution_store()
    observed: list[tbm.GatedMemoryResult] = []

    def execute(gated):
        observed.append(gated)
        return tbm.MemoryRunMeasurement(eval_result="pass")

    completion = tbm.run_memory_execution(
        store,
        context=context,
        trace_id=current.trace_id,
        task="run without historical memory",
        decide=lambda _request: _decline_json(),
        execute=execute,
    )

    assert observed[0].use_memory is False
    assert observed[0].allowed_memory_ids == ()
    assert observed[0].snippet == ""
    assert completion.usage_log.used_memory_ids == []
    assert completion.usage_log.eval_result == "pass"


def test_run_memory_execution_forwards_failure_attribution_and_all_evidence():
    store, current, lesson, context = _execution_store()

    completion = tbm.run_memory_execution(
        store,
        context=context,
        trace_id=current.trace_id,
        task="execute a failing repair",
        decide=lambda _request: _allow(lesson.lesson_id),
        execute=lambda _gated: tbm.MemoryRunMeasurement(
            eval_result="error",
            memory_caused_failure=True,
            output_hash="sha256:error-output",
            tool_outputs=({"tool": "search_docs", "status": "error"},),
            latency_ms=17,
            cost_usd=0.01,
            error="search_docs timed out",
            trace_uri="memory://trace/error",
        ),
    )

    assert completion.trace.eval_result == "error"
    assert completion.trace.error == "search_docs timed out"
    assert completion.trace.tool_outputs == [
        {"tool": "search_docs", "status": "error"}
    ]
    assert completion.usage_log.eval_result == "error"
    assert completion.usage_log.memory_caused_failure is True


def test_measurement_none_preserves_existing_trace_evidence():
    store, current, _lesson, context = _execution_store(
        output_hash="sha256:existing",
        tool_outputs=[{"existing": True}],
        latency_ms=9,
        error="existing diagnostic",
    )

    completion = tbm.run_memory_execution(
        store,
        context=context,
        trace_id=current.trace_id,
        task="preserve prior execution evidence",
        decide=lambda _request: _decline_json(),
        execute=lambda _gated: tbm.MemoryRunMeasurement(eval_result="pass"),
    )

    assert completion.trace.output_hash == "sha256:existing"
    assert completion.trace.tool_outputs == [{"existing": True}]
    assert completion.trace.latency_ms == 9
    assert completion.trace.error == "existing diagnostic"


def test_empty_tool_outputs_is_explicit_and_completion_failure_is_atomic():
    store, current, _lesson, context = _execution_store(
        tool_outputs=[{"existing": True}],
    )
    before_completion: list[dict[str, object]] = []

    def conflicting_measurement(_gated):
        before_completion.append(store.to_snapshot())
        return tbm.MemoryRunMeasurement(
            eval_result="pass",
            tool_outputs=(),
        )

    with pytest.raises(tbm.MemoryRunExecutionError) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="reject conflicting explicit evidence",
            decide=lambda _request: _decline_json(),
            execute=conflicting_measurement,
        )

    error = raised.value
    assert error.phase == "completion"
    assert error.request_id == error.request.request_id
    assert error.decision_id == error.gated_result.decision_id
    assert isinstance(error.__cause__, ValueError)
    assert "cannot rewrite tool_outputs" in str(error.__cause__)
    assert store.to_snapshot() == before_completion[0]
    assert store.traces[current.trace_id].eval_result == "unknown"
    assert store.usage_logs[0].eval_result is None
    assert store.usage_logs[0].memory_caused_failure is False


def test_decision_callback_error_preserves_pending_request_for_retry():
    store, current, lesson, context = _execution_store()

    class DecisionFailure(RuntimeError):
        pass

    failure = DecisionFailure("LLM unavailable")

    def reject(_request):
        raise failure

    with pytest.raises(tbm.MemoryRunExecutionError) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="retry the decision callback",
            decide=reject,
            execute=lambda _gated: pytest.fail("execute must not run"),
        )

    error = raised.value
    assert error.phase == "decision"
    assert error.trace_id == current.trace_id
    assert error.request_id == error.request.request_id
    assert error.gated_result is None
    assert error.decision_id is None
    assert error.__cause__ is failure
    assert store.usage_logs == []
    assert store.traces[current.trace_id].eval_result == "unknown"

    retried = store.finalize_memory(
        error.request,
        _allow(lesson.lesson_id),
        trace_id=current.trace_id,
    )
    assert retried.decision_id == "decision_000001"


def test_rerunning_one_shot_helper_prepares_a_distinct_recoverable_request():
    store, current, _lesson, context = _execution_store()
    errors: list[tbm.MemoryRunExecutionError] = []

    def reject(request):
        raise RuntimeError(f"decision failed for {request.request_id}")

    for _attempt in range(2):
        with pytest.raises(tbm.MemoryRunExecutionError) as raised:
            tbm.run_memory_execution(
                store,
                context=context,
                trace_id=current.trace_id,
                task="demonstrate non-idempotent one-shot retry",
                decide=reject,
                execute=lambda _gated: pytest.fail("execute must not run"),
            )
        errors.append(raised.value)

    assert errors[0].request_id != errors[1].request_id
    assert store.usage_logs == []

    recovered = [
        store.finalize_memory(
            error.request,
            _decline_json(),
            trace_id=error.trace_id,
        )
        for error in errors
    ]
    assert [result.decision_id for result in recovered] == [
        "decision_000001",
        "decision_000002",
    ]


def test_execution_callback_error_exposes_decision_for_explicit_completion():
    store, current, lesson, context = _execution_store()

    class ExecutionFailure(RuntimeError):
        pass

    failure = ExecutionFailure("harness interrupted")

    def reject(_gated):
        raise failure

    with pytest.raises(tbm.MemoryRunExecutionError) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="retain recoverable execution context",
            decide=lambda _request: _allow(lesson.lesson_id),
            execute=reject,
        )

    error = raised.value
    assert error.phase == "execution"
    assert error.trace_id == current.trace_id
    assert error.request_id == error.request.request_id
    assert error.gated_result is not None
    assert error.decision_id == error.gated_result.decision_id
    assert error.__cause__ is failure
    assert store.memory_run_audits()[0].status == "pending"

    completion = store.complete_memory_run(
        trace_id=error.trace_id,
        decision_id=error.decision_id,
        eval_result="error",
        error="harness interrupted",
    )
    assert completion.trace.eval_result == "error"
    assert completion.usage_log.eval_result == "error"


def test_wrong_execution_callback_return_has_recoverable_context():
    store, current, _lesson, context = _execution_store()

    with pytest.raises(tbm.MemoryRunExecutionError) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="reject an invalid callback result",
            decide=lambda _request: _decline_json(),
            execute=lambda _gated: object(),
        )

    error = raised.value
    assert error.phase == "execution"
    assert error.decision_id == store.usage_logs[0].decision_id
    assert isinstance(error.__cause__, TypeError)
    assert "MemoryRunMeasurement" in str(error.__cause__)
    assert store.memory_run_audits()[0].status == "pending"


def test_prepare_error_is_raw_and_finalization_error_exposes_request_for_retry():
    store, current, _lesson, context = _execution_store()
    callback_calls: list[str] = []

    with pytest.raises(ValueError, match="task must be a non-empty string"):
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="",
            decide=lambda _request: callback_calls.append("decision"),
            execute=lambda _gated: callback_calls.append("execution"),
        )

    assert callback_calls == []
    assert store.usage_logs == []

    def invalid_decision(_request):
        return {"use_memory": True}

    with pytest.raises(tbm.MemoryRunExecutionError) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="reject invalid decision payload",
            decide=invalid_decision,
            execute=lambda _gated: pytest.fail("execute must not run"),
        )

    error = raised.value
    assert error.phase == "finalization"
    assert error.trace_id == current.trace_id
    assert error.request_id == error.request.request_id
    assert error.gated_result is None
    assert error.decision_id is None
    assert isinstance(error.__cause__, ValueError)
    assert "memory decision missing required fields" in str(error.__cause__)
    assert store.usage_logs == []
    retried = store.finalize_memory(
        error.request,
        _decline_json(),
        trace_id=current.trace_id,
    )
    assert retried.decision_id == "decision_000001"


def test_completion_error_exposes_decision_and_keeps_both_records_pending():
    store, current, _lesson, context = _execution_store()

    with pytest.raises(tbm.MemoryRunExecutionError) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="reject invalid measured outcome",
            decide=lambda _request: _decline_json(),
            execute=lambda _gated: tbm.MemoryRunMeasurement(
                eval_result="unknown"
            ),
        )

    error = raised.value
    assert error.phase == "completion"
    assert error.trace_id == current.trace_id
    assert error.request_id == error.request.request_id
    assert error.gated_result is not None
    assert error.decision_id == error.gated_result.decision_id
    assert isinstance(error.__cause__, ValueError)
    assert "requires measured eval_result" in str(error.__cause__)
    assert store.traces[current.trace_id].eval_result == "unknown"
    assert store.usage_logs[0].eval_result is None

    completion = store.complete_memory_run(
        trace_id=error.trace_id,
        decision_id=error.decision_id,
        eval_result="pass",
    )
    assert completion.trace.eval_result == "pass"
    assert completion.usage_log.eval_result == "pass"


@pytest.mark.parametrize(
    ("latency_ms", "message"),
    [
        (-1, "latency_ms must be non-negative"),
        (2_147_483_648, "latency_ms must be at most 2147483647"),
    ],
)
def test_invalid_latency_completion_error_keeps_execution_pending(
    latency_ms: int,
    message: str,
):
    store, current, _lesson, context = _execution_store()

    with pytest.raises(tbm.MemoryRunExecutionError) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="reject impossible latency evidence",
            decide=lambda _request: _decline_json(),
            execute=lambda _gated: tbm.MemoryRunMeasurement(
                eval_result="pass",
                latency_ms=latency_ms,
            ),
        )

    error = raised.value
    assert error.phase == "completion"
    assert error.trace_id == current.trace_id
    assert error.decision_id == error.gated_result.decision_id
    assert isinstance(error.__cause__, ValueError)
    assert str(error.__cause__) == message
    assert store.traces[current.trace_id].eval_result == "unknown"
    assert store.usage_logs[0].eval_result is None
    assert store.memory_run_audits()[0].status == "pending"


@pytest.mark.parametrize(
    ("phase", "raised_error"),
    [("decision", KeyboardInterrupt()), ("execution", SystemExit(9))],
)
def test_process_control_exceptions_pass_through(phase, raised_error):
    store, current, _lesson, context = _execution_store()
    requests: list[tbm.MemoryGateRequest] = []
    gated_results: list[tbm.GatedMemoryResult] = []

    def decide(request):
        requests.append(request)
        if phase == "decision":
            raise raised_error
        return _decline_json()

    def execute(gated):
        gated_results.append(gated)
        raise raised_error

    with pytest.raises(type(raised_error)) as raised:
        tbm.run_memory_execution(
            store,
            context=context,
            trace_id=current.trace_id,
            task="preserve process control",
            decide=decide,
            execute=execute,
        )

    assert raised.value is raised_error
    assert len(store.usage_logs) == (0 if phase == "decision" else 1)
    assert store.traces[current.trace_id].eval_result == "unknown"
    if phase == "decision":
        retried = store.finalize_memory(
            requests[0],
            _decline_json(),
            trace_id=current.trace_id,
        )
        assert retried.decision_id == "decision_000001"
    else:
        assert store.memory_run_audits()[0].status == "pending"
        completed = store.complete_memory_run(
            trace_id=current.trace_id,
            decision_id=gated_results[0].decision_id,
            eval_result="error",
        )
        assert completed.usage_log.eval_result == "error"


def test_execution_interface_is_public_and_measurement_is_ephemeral():
    expected_names = {
        "MemoryDecisionCallback",
        "MemoryExecutionCallback",
        "MemoryRunExecutionError",
        "MemoryRunMeasurement",
        "run_memory_execution",
    }

    assert expected_names <= set(tbm.__all__)
    for name in expected_names:
        assert getattr(tbm, name) is not None

    measurement = tbm.MemoryRunMeasurement(eval_result="pass")
    assert "decision_id" not in measurement.__dataclass_fields__
    snapshot = tbm.TraceBackedMemoryStore().to_snapshot()
    assert snapshot["snapshot_version"] == 2
    assert "memory_run_measurements" not in snapshot


def test_memory_run_execution_round_trips_through_existing_snapshot_records():
    store, current, _lesson, context = _execution_store()

    completion = tbm.run_memory_execution(
        store,
        context=context,
        trace_id=current.trace_id,
        task="round trip completed execution",
        decide=lambda _request: _decline_json(),
        execute=lambda _gated: tbm.MemoryRunMeasurement(
            eval_result="pass",
            output_hash="sha256:round-trip",
        ),
    )

    snapshot = store.to_snapshot()
    serialized = json.dumps(snapshot, sort_keys=True)
    restored = tbm.TraceBackedMemoryStore.from_snapshot(snapshot)

    assert snapshot["snapshot_version"] == 2
    assert "MemoryRunMeasurement" not in serialized
    assert "MemoryRunExecutionError" not in serialized
    assert restored.traces[current.trace_id] == completion.trace
    assert restored.usage_logs == [completion.usage_log]
    assert restored.memory_run_audits()[0].status == "complete"
