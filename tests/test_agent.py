import json
from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.agent as agent_module
import trace_backed_memory.cli as cli


def _agent_fixture() -> tuple[
    tbm.TraceBackedMemoryStore,
    tbm.Trace,
    tbm.Lesson,
    tbm.MemoryContext,
]:
    store = tbm.TraceBackedMemoryStore()
    source = store.record_trace(
        tbm.Trace(
            trace_id="trace_agent_source",
            run_id="run_agent_source",
            commit_sha="commit_agent",
            repo="repo",
            tenant="tenant",
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    case = store.add_failure_case(
        tbm.verify_failure_case(
            tbm.review_failure_case(
                tbm.draft_failure_case(
                    source,
                    case_id="case_agent",
                    failure_type="invalid_tool_argument",
                    symptom="search_docs received an empty query",
                ),
                reviewed_by="reviewer",
                reviewed_at="2026-07-27T00:00:00Z",
                root_cause="the query contract was omitted",
            ),
            fix="require a non-empty query",
            fix_commit_sha="commit_agent_fix",
            regression_passed=True,
        )
    )
    lesson = store.add_lesson(
        tbm.lesson_from_failure_case(
            case,
            lesson_id="lesson_agent",
            lesson_text="Always pass a non-empty query to search_docs.",
            memory_type="procedural",
            scope={
                "repo": "repo",
                "tenant": "tenant",
                "tool": "search_docs",
            },
        )
    )
    current = replace(
        source,
        trace_id="trace_agent_current",
        run_id="run_agent_current",
        eval_result="unknown",
        output_hash=None,
        tool_outputs=[],
        latency_ms=None,
        cost_usd=None,
        error=None,
        trace_uri=None,
    )
    context = tbm.MemoryContext(
        mode="repair",
        repo="repo",
        tenant="tenant",
        commit_sha="commit_agent",
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


def _decline() -> dict[str, object]:
    return {
        "use_memory": False,
        "allowed_memory_ids": [],
        "blocked_memory_ids": [],
        "reason": "memory is not required",
        "risk": "none",
        "recommended_injection": "none",
    }


def test_agent_capabilities_are_versioned_and_explicit_about_process_local_state():
    capabilities = tbm.agent_capabilities()

    assert capabilities.protocol_version == "tbm.agent.v1"
    assert capabilities.snapshot_version == 2
    assert capabilities.sqlite_schema_version == 1
    assert capabilities.postgres_schema_version == 2
    assert capabilities.storage_modes == ("memory", "sqlite", "postgres")
    assert capabilities.process_local_records == (
        "pending_gate_requests",
        "finalized_gate_requests",
    )
    assert capabilities.limits["gate_candidates"] == 50
    assert capabilities.limits["finalized_request_replays"] == 10_000
    assert capabilities.to_dict()["protocol_version"] == "tbm.agent.v1"


def test_capabilities_cli_does_not_require_a_snapshot(capsys):
    assert cli.main(["capabilities"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol_version"] == "tbm.agent.v1"
    assert payload["operations"] == [
        "capture",
        "prepare",
        "finalize",
        "complete",
        "cancel",
        "run",
        "flush",
        "health",
    ]


def test_local_agent_memory_hides_store_request_token_and_completes_run():
    store, current, lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)

    prepared = runtime.prepare(
        current,
        context,
        task="repair the failed search_docs call",
    )
    assert prepared.request_id.startswith("gate_request_")
    assert prepared.request_id.endswith("_000001")
    assert prepared.trace_id == current.trace_id
    assert prepared.candidate_memory_ids == (lesson.lesson_id,)
    assert "_store_token" not in prepared.to_dict()

    finalized = runtime.finalize(
        prepared.request_id,
        _allow(lesson.lesson_id),
    )
    assert finalized.allowed_memory_ids == (lesson.lesson_id,)
    assert "Always pass a non-empty query" in finalized.snippet

    completed = runtime.complete(
        finalized.decision_id,
        tbm.MemoryRunMeasurement(
            eval_result="pass",
            output_hash="sha256:agent-output",
            tool_outputs=({"documents": 3},),
            latency_ms=7,
        ),
    )
    assert completed.request_id == prepared.request_id
    assert completed.eval_result == "pass"

    snapshot = runtime.snapshot()
    trace = next(
        row
        for row in snapshot["traces"]
        if row["trace_id"] == current.trace_id
    )
    usage = snapshot["usage_logs"][0]
    assert trace["output_hash"] == "sha256:agent-output"
    assert trace["tool_outputs"] == [{"documents": 3}]
    assert usage["request_id"] == prepared.request_id
    assert usage["eval_result"] == "pass"


def test_finalize_is_idempotent_at_agent_interface_and_rejects_other_decision():
    store, current, lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)
    prepared = runtime.prepare(current, context, task="repair")

    first = runtime.finalize(
        prepared.request_id,
        _allow(lesson.lesson_id),
    )
    replay = runtime.finalize(
        prepared.request_id,
        json.dumps(_allow(lesson.lesson_id)),
    )
    assert replay == first
    assert len(runtime.snapshot()["usage_logs"]) == 1

    with pytest.raises(tbm.AgentMemoryError) as error:
        runtime.finalize(prepared.request_id, _decline())
    assert error.value.code == "TBM_AGENT_DECISION_CONFLICT"
    assert error.value.request_id == prepared.request_id


def test_finalize_replay_cache_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        agent_module,
        "FINALIZED_GATE_REQUEST_MAX_ITEMS",
        1,
    )
    store, current, lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)
    first = runtime.prepare(current, context, task="repair")
    runtime.finalize(first.request_id, _allow(lesson.lesson_id))

    second_trace = replace(
        current,
        trace_id="trace_agent_replay_second",
        run_id="run_agent_replay_second",
    )
    second = runtime.prepare(second_trace, context, task="repair")
    finalized = runtime.finalize(
        second.request_id,
        _allow(lesson.lesson_id),
    )

    with pytest.raises(tbm.AgentMemoryError) as error:
        runtime.finalize(
            first.request_id,
            _allow(lesson.lesson_id),
        )
    assert error.value.code == "TBM_AGENT_REQUEST_NOT_FOUND"
    assert runtime.finalize(
        second.request_id,
        _allow(lesson.lesson_id),
    ) == finalized


def test_finalize_maps_invalid_decision_to_bounded_protocol_error():
    store, current, _lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)
    prepared = runtime.prepare(current, context, task="repair")

    with pytest.raises(tbm.AgentMemoryError) as error:
        runtime.finalize(prepared.request_id, {"use_memory": True})

    assert error.value.code == "TBM_AGENT_INVALID_DECISION"
    assert error.value.category == "input"
    assert error.value.operation == "finalize"
    assert error.value.request_id == prepared.request_id
    assert len(
        tbm.AgentMemoryError(
            "TBM_AGENT_TEST",
            "internal",
            "run",
            "x" * (tbm.AGENT_ERROR_MESSAGE_MAX_CHARS + 1),
        ).to_dict()["error"]["message"]
    ) == tbm.AGENT_ERROR_MESSAGE_MAX_CHARS


def test_agent_entrypoints_reject_malformed_ids_with_schema_safe_errors():
    runtime = tbm.LocalAgentMemory.in_memory()
    measurement = tbm.MemoryRunMeasurement(eval_result="pass")

    calls = (
        lambda: runtime.finalize([], _decline()),  # type: ignore[arg-type]
        lambda: runtime.cancel({}),  # type: ignore[arg-type]
        lambda: runtime.complete([], measurement),  # type: ignore[arg-type]
        lambda: runtime.flush(request_id=[]),  # type: ignore[arg-type]
    )
    for call in calls:
        with pytest.raises(tbm.AgentMemoryError) as error:
            call()
        assert error.value.code == "TBM_AGENT_INVALID_INPUT"
        envelope = error.value.to_dict()
        assert "request_id" not in envelope["error"]
        assert "decision_id" not in envelope["error"]

    direct = tbm.AgentMemoryError(
        "TBM_AGENT_TEST",
        "input",
        "finalize",
        "invalid identifier",
        request_id=[],  # type: ignore[arg-type]
    ).to_dict()
    assert "request_id" not in direct["error"]


def test_cancel_releases_request_and_stable_error_does_not_expose_store_token():
    store, current, _lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)
    prepared = runtime.prepare(current, context, task="repair")

    runtime.cancel(prepared.request_id)
    with pytest.raises(tbm.AgentMemoryError) as error:
        runtime.finalize(prepared.request_id, _decline())

    assert error.value.code == "TBM_AGENT_REQUEST_NOT_FOUND"
    envelope = error.value.to_dict()
    assert envelope["protocol_version"] == "tbm.agent.v1"
    assert envelope["error"]["request_id"] == prepared.request_id
    assert "_store_token" not in json.dumps(envelope)


def test_complete_wraps_malformed_tool_outputs_in_protocol_error():
    store, current, lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)
    prepared = runtime.prepare(current, context, task="repair")
    finalized = runtime.finalize(
        prepared.request_id,
        _allow(lesson.lesson_id),
    )

    with pytest.raises(tbm.AgentMemoryError) as error:
        runtime.complete(
            finalized.decision_id,
            tbm.MemoryRunMeasurement(
                eval_result="pass",
                tool_outputs=("bad",),  # type: ignore[arg-type]
            ),
        )

    assert error.value.code == "TBM_AGENT_COMPLETE_REJECTED"
    assert error.value.request_id == prepared.request_id
    assert error.value.decision_id == finalized.decision_id


def test_run_makes_the_common_agent_path_one_call():
    store, current, lesson, context = _agent_fixture()
    observed: list[str] = []

    result = tbm.LocalAgentMemory.in_memory(store).run(
        current,
        context,
        task="repair",
        decide=lambda prepared: (
            observed.append(f"decide:{prepared.request_id}")
            or _allow(lesson.lesson_id)
        ),
        execute=lambda finalized: (
            observed.append(f"execute:{finalized.decision_id}")
            or tbm.MemoryRunMeasurement(eval_result="pass")
        ),
    )

    assert observed == [
        f"decide:{result.prepared.request_id}",
        f"execute:{result.finalized.decision_id}",
    ]
    assert result.completed.eval_result == "pass"
    assert result.to_dict()["protocol_version"] == "tbm.agent.v1"


def test_run_preserves_pending_and_finalized_recovery_handles_on_callback_errors():
    store, current, _lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)

    with pytest.raises(tbm.AgentMemoryError) as decision_error:
        runtime.run(
            current,
            context,
            task="repair",
            decide=lambda _prepared: (_ for _ in ()).throw(
                RuntimeError("provider unavailable")
            ),
            execute=lambda _finalized: tbm.MemoryRunMeasurement(
                eval_result="pass"
            ),
        )
    assert decision_error.value.code == "TBM_AGENT_DECISION_CALLBACK_FAILED"
    request_id = decision_error.value.request_id
    assert request_id is not None
    finalized = runtime.finalize(request_id, _decline())

    with pytest.raises(tbm.AgentMemoryError) as execution_error:
        runtime.run(
            replace(
                current,
                trace_id="trace_agent_second",
                run_id="run_agent_second",
            ),
            context,
            task="repair",
            decide=lambda _prepared: _decline(),
            execute=lambda _finalized: (_ for _ in ()).throw(
                RuntimeError("executor unavailable")
            ),
        )
    assert execution_error.value.code == "TBM_AGENT_EXECUTION_CALLBACK_FAILED"
    assert execution_error.value.decision_id is not None
    assert finalized.decision_id != execution_error.value.decision_id


def test_sqlite_runtime_persists_each_durable_phase_and_reopens(tmp_path: Path):
    store, current, lesson, context = _agent_fixture()
    database = tmp_path / "agent.sqlite3"
    with tbm.SQLiteMemoryRepository.connect(
        database,
        initialize=True,
    ) as repository:
        repository.sync(store)

    with tbm.LocalAgentMemory.open_sqlite(database) as runtime:
        result = runtime.run(
            current,
            context,
            task="repair",
            decide=lambda _prepared: _allow(lesson.lesson_id),
            execute=lambda _finalized: tbm.MemoryRunMeasurement(
                eval_result="pass",
                trace_uri="memory://agent/sqlite",
            ),
        )

    with tbm.LocalAgentMemory.open_sqlite(database) as reopened:
        second_trace = replace(
            current,
            trace_id="trace_agent_sqlite_second",
            run_id="run_agent_sqlite_second",
        )
        second = reopened.prepare(
            second_trace,
            context,
            task="repair",
        )
        assert second.request_id.endswith("_000002")
        assert second.request_id != result.prepared.request_id
        reopened.cancel(second.request_id)
        snapshot = reopened.snapshot()
    persisted = next(
        row
        for row in snapshot["traces"]
        if row["trace_id"] == current.trace_id
    )
    assert persisted["eval_result"] == "pass"
    assert persisted["trace_uri"] == "memory://agent/sqlite"
    assert snapshot["usage_logs"][0]["decision_id"] == (
        result.finalized.decision_id
    )


def test_sqlite_runtime_stale_request_id_cannot_target_new_session(
    tmp_path: Path,
):
    store, current, lesson, context = _agent_fixture()
    database = tmp_path / "agent-request-session.sqlite3"
    with tbm.SQLiteMemoryRepository.connect(
        database,
        initialize=True,
    ) as repository:
        repository.sync(store)

    with tbm.LocalAgentMemory.open_sqlite(database) as first:
        abandoned = first.prepare(current, context, task="old task")

    with tbm.LocalAgentMemory.open_sqlite(database) as second:
        new_trace = replace(
            current,
            trace_id="trace_agent_new_session",
            run_id="run_agent_new_session",
        )
        prepared = second.prepare(
            new_trace,
            context,
            task="new task",
        )
        assert prepared.request_id != abandoned.request_id
        assert prepared.request_id.endswith("_000001")
        assert abandoned.request_id.endswith("_000001")

        with pytest.raises(tbm.AgentMemoryError) as finalize_error:
            second.finalize(
                abandoned.request_id,
                _allow(lesson.lesson_id),
            )
        assert finalize_error.value.code == "TBM_AGENT_REQUEST_NOT_FOUND"

        with pytest.raises(tbm.AgentMemoryError) as cancel_error:
            second.cancel(abandoned.request_id)
        assert cancel_error.value.code == "TBM_AGENT_REQUEST_NOT_FOUND"

        finalized = second.finalize(
            prepared.request_id,
            _allow(lesson.lesson_id),
        )
        assert finalized.request_id == prepared.request_id
        assert second.snapshot()["usage_logs"][-1]["request_id"] == (
            prepared.request_id
        )


def test_from_repository_does_not_close_borrowed_repository_by_default():
    store, current, _lesson, context = _agent_fixture()

    class Repository:
        def __init__(self):
            self.closed = False
            self.sync_count = 0

        def load(self):
            return tbm.TraceBackedMemoryStore.from_snapshot(
                store.to_snapshot()
            )

        def sync(self, _store):
            self.sync_count += 1

        def close(self):
            self.closed = True

    repository = Repository()
    runtime = tbm.LocalAgentMemory.from_repository(repository)
    runtime.prepare(current, context, task="repair")
    runtime.close()

    assert repository.sync_count == 1
    assert repository.closed is False


def test_from_repository_rejects_invalid_adapter_result_with_stable_error():
    class Repository:
        def load(self):
            return None

        def sync(self, _store):
            raise AssertionError("sync must not be called")

        def close(self):
            raise AssertionError("close must not be called")

    with pytest.raises(tbm.AgentMemoryError) as error:
        tbm.LocalAgentMemory.from_repository(Repository())

    assert error.value.code == "TBM_AGENT_REPOSITORY_LOAD_FAILED"
    assert error.value.operation == "open"
    assert "TraceBackedMemoryStore" not in str(error.value)


def test_repository_connection_errors_use_stable_open_error(monkeypatch):
    def fail_connect(*_args, **_kwargs):
        raise OSError("sensitive adapter detail")

    monkeypatch.setattr(
        agent_module.SQLiteMemoryRepository,
        "connect",
        fail_connect,
    )

    with pytest.raises(tbm.AgentMemoryError) as error:
        tbm.LocalAgentMemory.open_sqlite("unavailable.sqlite3")

    assert error.value.code == "TBM_AGENT_REPOSITORY_CONNECT_FAILED"
    assert error.value.category == "persistence"
    assert error.value.operation == "open"
    assert error.value.retryable is True
    assert "sensitive adapter detail" not in str(error.value)


def test_capture_local_trace_builds_bounded_pending_trace(monkeypatch):
    monkeypatch.setattr(
        agent_module,
        "capture_trace_metadata",
        lambda _path: tbm.TraceMetadata(
            commit_sha="commit_local",
            repo="repo_local",
            branch="main",
            dirty=True,
        ),
    )

    trace = tbm.capture_local_trace(
        ".",
        tenant="tenant",
        retrieved_context=({"document_count": 3},),
        tool_names=("search_docs",),
    )

    assert trace.trace_id.startswith("trace_")
    assert trace.run_id.startswith("run_")
    assert trace.repo == "repo_local"
    assert trace.commit_sha == "commit_local"
    assert trace.branch == "main"
    assert trace.dirty is True
    assert trace.retrieved_context == [{"document_count": 3}]
    assert trace.tool_calls == [{"name": "search_docs"}]
    assert trace.eval_result == "unknown"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"trace_id": ""}, "trace_id must be None or a nonblank string"),
        ({"run_id": " "}, "run_id must be None or a nonblank string"),
        (
            {"retrieved_context": ({"valid": True}, object())},
            "retrieved_context must contain exact JSON object dictionaries",
        ),
        (
            {"tool_names": ("x" * 513,)},
            "tool_names must contain nonblank strings",
        ),
    ],
)
def test_capture_local_trace_rejects_input_before_git(
    monkeypatch,
    values,
    message,
):
    def unexpected_capture(_path):
        raise AssertionError("Git capture must not run")

    monkeypatch.setattr(
        agent_module,
        "capture_trace_metadata",
        unexpected_capture,
    )

    with pytest.raises(tbm.AgentMemoryError, match=message) as error:
        tbm.capture_local_trace(".", **values)

    assert error.value.code == "TBM_AGENT_INVALID_INPUT"
    assert error.value.category == "input"


def test_capture_local_trace_reuses_store_trace_validation(monkeypatch):
    monkeypatch.setattr(
        agent_module,
        "capture_trace_metadata",
        lambda _path: tbm.TraceMetadata(
            commit_sha="commit_local",
            repo="repo_local",
            branch="main",
            dirty=False,
        ),
    )

    with pytest.raises(
        tbm.AgentMemoryError,
        match="contain only JSON semantic values",
    ) as error:
        tbm.capture_local_trace(
            ".",
            retrieved_context=({"invalid": object()},),
        )

    assert error.value.code == "TBM_AGENT_INVALID_INPUT"


def test_capture_local_trace_wraps_git_capture_failure(monkeypatch):
    def fail_capture(_path):
        raise OSError("sensitive repository path")

    monkeypatch.setattr(
        agent_module,
        "capture_trace_metadata",
        fail_capture,
    )

    with pytest.raises(tbm.AgentMemoryError) as error:
        tbm.capture_local_trace(".")

    assert error.value.code == "TBM_AGENT_CAPTURE_FAILED"
    assert error.value.operation == "capture"
    assert error.value.retryable is True
    assert "sensitive repository path" not in str(error.value)


def test_prepare_with_git_ancestry_captures_complete_anchors(monkeypatch):
    store, current, _lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)
    captured = {}

    def capture(current_commit_sha, anchors, *, repo_path):
        captured["current_commit_sha"] = current_commit_sha
        captured["anchors"] = anchors
        captured["repo_path"] = repo_path
        return tbm.CommitAncestryEvidence(
            current_commit_sha=current_commit_sha,
            commit_relations=tuple(
                (anchor, True) for anchor in anchors
            ),
        )

    monkeypatch.setattr(
        agent_module,
        "capture_commit_ancestry",
        capture,
    )

    prepared = runtime.prepare_with_git_ancestry(
        current,
        context,
        repo_path=".",
        task="repair search_docs",
    )

    assert prepared.trace_id == current.trace_id
    assert captured == {
        "current_commit_sha": current.commit_sha,
        "anchors": ("commit_agent_fix",),
        "repo_path": ".",
    }


def test_agent_health_is_non_sensitive_and_tracks_process_local_state():
    store, current, lesson, context = _agent_fixture()
    runtime = tbm.LocalAgentMemory.in_memory(store)

    initial = runtime.health()
    assert initial["protocol_version"] == "tbm.agent.v1"
    assert initial["pending_request_count"] == 0
    assert initial["finalized_request_replay_count"] == 0
    assert initial["memory_metrics"]["decision_count"] == 0

    prepared = runtime.prepare(
        current,
        context,
        task="repair search_docs",
    )
    pending = runtime.health()
    assert pending["pending_request_count"] == 1
    assert "traces" not in pending

    runtime.finalize(prepared.request_id, _allow(lesson.lesson_id))
    finalized = runtime.health()
    assert finalized["pending_request_count"] == 0
    assert finalized["finalized_request_replay_count"] == 1
    assert finalized["memory_run_metrics"]["pending_count"] == 1


def test_agent_public_surface_is_exported():
    expected = {
        "AGENT_PROTOCOL_VERSION",
        "AGENT_ERROR_MESSAGE_MAX_CHARS",
        "AgentCapabilities",
        "AgentCompletedRun",
        "AgentDecisionCallback",
        "AgentExecutionCallback",
        "AgentFinalizedMemory",
        "AgentMemoryError",
        "AgentPreparedMemory",
        "AgentRunResult",
        "LocalAgentMemory",
        "MemoryRepository",
        "agent_capabilities",
        "capture_local_trace",
    }

    assert expected.issubset(set(tbm.__all__))
    for name in expected:
        assert getattr(tbm, name) is not None


def test_agent_protocol_schemas_and_examples_are_versioned_and_packaged():
    root = Path(__file__).resolve().parents[1]
    names = (
        "agent_capabilities",
        "agent_completed",
        "agent_error",
        "agent_finalized",
        "agent_prepared",
    )
    packaged = {item.name for item in tbm.packaged_resources()}

    for name in names:
        schema_path = root / "schemas" / f"{name}.schema.json"
        example_path = root / "examples" / f"{name}.example.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        example = json.loads(example_path.read_text(encoding="utf-8"))

        assert schema["$schema"] == (
            "https://json-schema.org/draft/2020-12/schema"
        )
        assert schema["properties"]["protocol_version"] == {
            "const": "tbm.agent.v1"
        }
        assert example["protocol_version"] == "tbm.agent.v1"
        assert f"schemas/{name}.schema.json" in packaged
        assert f"examples/{name}.example.json" in packaged

    finalized_schema = json.loads(
        (root / "schemas" / "agent_finalized.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert finalized_schema["properties"]["allowed_memory_ids"][
        "maxItems"
    ] == tbm.INJECTION_MAX_MEMORIES
