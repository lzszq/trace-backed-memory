from dataclasses import replace
from pathlib import Path
import re

from trace_backed_memory import (
    FailureCase,
    Lesson,
    ProjectPolicy,
    MemoryContext,
    MemoryItem,
    MemoryObsolescenceRequest,
    PostgresMemoryRepository,
    PRCaseProvenance,
    Trace,
    TraceMetadataCaptureError,
    TraceBackedMemoryStore,
    build_llm_gate_prompt,
    capture_commit_ancestry,
    capture_trace_metadata,
    classify_failure_type,
    draft_failure_case,
    draft_failure_case_from_trace,
    export_packaged_resource,
    lesson_from_failure_case,
    load_failure_taxonomy,
    memory_item_from_failure_case,
    memory_item_from_lesson,
    memory_item_from_project_policy,
    obsolete_failure_case,
    obsolete_lesson,
    obsolete_project_policy,
    packaged_resources,
    parse_memory_context,
    review_failure_case,
    read_packaged_resource,
    system_gate,
    verify_failure_case,
)


def test_readme_postgres_repository_example_stays_executable_without_a_database(monkeypatch):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"```python\n"
        r"(from trace_backed_memory import PostgresMemoryRepository\n\n"
        r"with PostgresMemoryRepository\.connect\(\"postgresql://\.\.\.\"\) as repository:\n"
        r"    result = repository\.sync\(store\)\n"
        r"    restored = repository\.load\(\)\n)"
        r"```",
        readme,
    )
    assert match is not None, "README should include the PostgreSQL repository example"

    store = TraceBackedMemoryStore()
    restored = TraceBackedMemoryStore()

    class FakeRepository:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def sync(self, incoming_store):
            assert incoming_store is store
            return "synced"

        def load(self):
            return restored

    repository = FakeRepository()

    def fake_connect(cls, conninfo):
        assert cls is PostgresMemoryRepository
        assert conninfo == "postgresql://..."
        return repository

    monkeypatch.setattr(
        PostgresMemoryRepository, "connect", classmethod(fake_connect)
    )

    namespace = {"store": store}
    exec(match.group(1), namespace)

    assert namespace["result"] == "synced"
    assert namespace["restored"] is restored


def readme_store_fixture() -> tuple[
    TraceBackedMemoryStore, Trace, FailureCase, Lesson
]:
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_safe_readme",
            run_id="run_safe_readme",
            commit_sha="abc123",
            repo="agent-harness",
            tenant="tenant_a",
            eval_result="fail",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        )
    )
    case = verify_failure_case(
        review_failure_case(
            draft_failure_case(
                trace,
                case_id="case_safe_readme",
                failure_type="invalid_tool_argument",
                symptom="search_docs received an empty query",
            ),
            reviewed_by="test-reviewer",
            root_cause="the prompt omitted the query contract",
            reviewed_at="2026-07-22T00:00:00Z",
        ),
        fix="require a non-empty query",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(case)
    lesson = lesson_from_failure_case(
        case,
        lesson_id="lesson_safe_readme",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={
            "repo": "agent-harness",
            "tenant": "tenant_a",
            "tool": "search_docs",
        },
    )
    store.add_lesson(lesson)
    return store, trace, case, lesson


def allow_decision(memory_id: str) -> dict[str, object]:
    return {
        "use_memory": True,
        "allowed_memory_ids": [memory_id],
        "blocked_memory_ids": [],
        "reason": "direct match",
        "risk": "low",
        "recommended_injection": "short_summary",
    }


def test_readme_safe_workflow_example_stays_executable():
    store, source_trace, _case, lesson = readme_store_fixture()
    trace = store.record_trace(
        replace(
            source_trace,
            trace_id="trace_current_readme",
            run_id="run_current_readme",
            eval_result="unknown",
            tool_calls=[
                {"name": "search_docs", "arguments": {"query": "memory"}}
            ],
        )
    )
    context = MemoryContext(
        mode="repair", repo=trace.repo, tenant=trace.tenant,
        commit_sha=trace.commit_sha, tool="search_docs",
    )
    request = store.prepare_memory(context, task="repair failed tool call")
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id,
    )
    completion = store.complete_memory_run(
        trace_id=trace.trace_id,
        decision_id=result.decision_id,
        eval_result="pass",
        tool_outputs=[{"documents": 3}],
        latency_ms=125,
    )
    assert result.use_memory
    assert result.decision_id == store.usage_logs[-1].decision_id
    assert store.traces[source_trace.trace_id].eval_result == "fail"
    assert completion.trace.eval_result == "pass"
    assert completion.usage_log.eval_result == "pass"
    (audit,) = store.memory_run_audits()
    assert audit.trace_id == trace.trace_id
    assert audit.decision_id == result.decision_id
    assert audit.status == "complete"
    run_metrics = store.memory_run_metrics()
    assert run_metrics.decision_count == 1
    assert run_metrics.complete_count == 1
    assert run_metrics.recoverable_count == 0
    assert store.metrics().evaluated_with_memory_count == 1
    assert store.metrics().unevaluated_decision_count == 0


def test_readme_memory_run_recovery_workflow_stays_executable():
    store, source_trace, _case, lesson = readme_store_fixture()
    current = store.record_trace(
        replace(
            source_trace,
            trace_id="trace_recovery_readme",
            run_id="run_recovery_readme",
            eval_result="unknown",
            tool_calls=[
                {"name": "search_docs", "arguments": {"query": "memory"}}
            ],
        )
    )
    context = MemoryContext(
        mode="repair",
        repo=current.repo,
        tenant=current.tenant,
        commit_sha=current.commit_sha,
        tool="search_docs",
    )
    request = store.prepare_memory(context, task="repair failed tool call")
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=current.trace_id,
    )
    store.complete_trace(current.trace_id, eval_result="pass")
    (audit,) = store.memory_run_audits()

    completion = store.recover_memory_run(audit.decision_id)

    assert audit.status == "trace_only"
    assert audit.decision_id == result.decision_id
    assert completion.trace.eval_result == "pass"
    assert completion.usage_log.eval_result == "pass"
    assert store.memory_run_audits()[0].status == "complete"


def test_readme_batch_memory_run_recovery_workflow_stays_executable():
    store, source_trace, _case, lesson = readme_store_fixture()
    decision_ids = []
    for suffix in ("a", "b"):
        current = store.record_trace(
            replace(
                source_trace,
                trace_id=f"trace_batch_recovery_readme_{suffix}",
                run_id=f"run_batch_recovery_readme_{suffix}",
                eval_result="unknown",
                tool_outputs=[],
            )
        )
        context = MemoryContext(
            mode="repair",
            repo=current.repo,
            tenant=current.tenant,
            commit_sha=current.commit_sha,
            tool="search_docs",
        )
        request = store.prepare_memory(context, task="repair failed tool call")
        result = store.finalize_memory(
            request,
            allow_decision(lesson.lesson_id),
            trace_id=current.trace_id,
        )
        store.complete_trace(current.trace_id, eval_result="pass")
        decision_ids.append(result.decision_id)

    recoverable_ids = tuple(
        audit.decision_id
        for audit in store.memory_run_audits()
        if audit.status in {"trace_only", "decision_only"}
    )
    completions = store.recover_memory_runs(recoverable_ids)

    assert recoverable_ids == tuple(decision_ids)
    assert tuple(item.usage_log.decision_id for item in completions) == recoverable_ids
    assert all(item.trace.eval_result == "pass" for item in completions)
    assert store.memory_run_metrics().complete_count == 2
    assert store.memory_run_metrics().recoverable_count == 0


def test_readme_memory_run_remediation_workflow_stays_executable():
    store, source_trace, _case, lesson = readme_store_fixture()
    decisions = []
    for suffix, eval_result in (("pass", "pass"), ("fail", "fail")):
        current = store.record_trace(
            replace(
                source_trace,
                trace_id=f"trace_remediation_readme_{suffix}",
                run_id=f"run_remediation_readme_{suffix}",
                eval_result="unknown",
                tool_outputs=[],
            )
        )
        context = MemoryContext(
            mode="repair",
            repo=current.repo,
            tenant=current.tenant,
            commit_sha=current.commit_sha,
            tool="search_docs",
        )
        request = store.prepare_memory(context, task="repair failed tool call")
        result = store.finalize_memory(
            request,
            allow_decision(lesson.lesson_id),
            trace_id=current.trace_id,
        )
        store.complete_trace(current.trace_id, eval_result=eval_result)
        decisions.append(result.decision_id)

    remediations = store.memory_run_remediations()
    automatic_ids = tuple(
        item.decision_id for item in remediations if item.action == "recover"
    )
    attribution_ids = tuple(
        item.decision_id
        for item in remediations
        if item.action == "recover_with_attribution"
    )

    automatic_completions = store.recover_ready_memory_runs()
    store.recover_memory_runs(
        attribution_ids,
        memory_caused_failures={attribution_ids[0]: False},
    )

    assert automatic_ids == (decisions[0],)
    assert tuple(
        item.usage_log.decision_id for item in automatic_completions
    ) == automatic_ids
    assert attribution_ids == (decisions[1],)
    assert [item.action for item in remediations] == [
        "recover",
        "recover_with_attribution",
    ]
    assert all(
        item.action == "none" for item in store.memory_run_remediations()
    )
    assert store.memory_run_metrics().recoverable_count == 0


def test_readme_batch_memory_run_completion_workflow_stays_executable():
    from trace_backed_memory import MemoryRunResult

    store, source_trace, _case, lesson = readme_store_fixture()
    decisions = []
    for suffix in ("pass", "error"):
        current = store.record_trace(
            replace(
                source_trace,
                trace_id=f"trace_batch_completion_readme_{suffix}",
                run_id=f"run_batch_completion_readme_{suffix}",
                eval_result="unknown",
                tool_outputs=[],
            )
        )
        context = MemoryContext(
            mode="repair",
            repo=current.repo,
            tenant=current.tenant,
            commit_sha=current.commit_sha,
            tool="search_docs",
        )
        request = store.prepare_memory(context, task="repair failed tool call")
        decision = store.finalize_memory(
            request,
            allow_decision(lesson.lesson_id),
            trace_id=current.trace_id,
        )
        decisions.append(decision)

    completions = store.complete_memory_runs(
        (
            MemoryRunResult(
                decision_id=decisions[0].decision_id,
                eval_result="pass",
                tool_outputs=({"documents": 3},),
                latency_ms=125,
            ),
            MemoryRunResult(
                decision_id=decisions[1].decision_id,
                eval_result="error",
                memory_caused_failure=False,
                error="executor failed",
            ),
        )
    )

    assert [item.trace.eval_result for item in completions] == ["pass", "error"]
    assert completions[0].trace.tool_outputs == [{"documents": 3}]
    assert completions[1].trace.error == "executor failed"
    assert store.memory_run_metrics().complete_count == 2


def test_readme_benchmark_safe_workflow_stays_executable():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"```python\n"
        r"(# BENCHMARK_SAFE_WORKFLOW_START\n.*?"
        r"# BENCHMARK_SAFE_WORKFLOW_END\n)"
        r"```",
        readme,
        re.DOTALL,
    )
    assert match is not None, "README should include the benchmark-safe workflow"

    namespace: dict[str, object] = {}
    exec(match.group(1), namespace)

    request = namespace["request"]
    result = namespace["result"]
    store = namespace["store"]
    lesson = namespace["lesson"]
    source_input_hash = namespace["source_input_hash"]
    current_input_hash = namespace["current_input_hash"]
    block_reason = namespace["BENCHMARK_BLOCK_REASON"]

    assert request.candidate_memory_ids == (lesson.lesson_id,)
    assert request.system_allowed_memory_ids == ()
    assert dict(request.system_blocked) == {lesson.lesson_id: block_reason}
    assert lesson.lesson_id not in request.prompt
    assert source_input_hash not in request.prompt
    assert current_input_hash not in request.prompt
    assert result.use_memory is False
    assert result.snippet == ""
    assert store.usage_logs[-1].context["input_hash"] == current_input_hash
    assert store.usage_logs[-1].system_blocked_reasons == {
        lesson.lesson_id: block_reason
    }


def test_readme_semantic_retrieval_example_stays_executable():
    store, trace, _case, lesson = readme_store_fixture()
    context = MemoryContext(
        mode="repair",
        repo=trace.repo,
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
    )
    semantic_scores = {lesson.lesson_id: 0.93}

    request = store.prepare_memory(
        context,
        task="repair failed search_docs call",
        semantic_scores=semantic_scores,
        max_candidates=10,
        minimum_score=0.70,
    )
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert request.candidate_memory_ids == (lesson.lesson_id,)
    assert result.allowed_memory_ids == (lesson.lesson_id,)


def test_readme_git_ancestry_workflow_stays_executable():
    store, trace, _case, lesson = readme_store_fixture()
    context = MemoryContext(
        mode="repair",
        repo=trace.repo,
        tenant=trace.tenant,
        commit_sha=trace.commit_sha,
        tool="search_docs",
    )
    anchors = store.candidate_commit_anchors(context)

    evidence = capture_commit_ancestry(
        context.commit_sha,
        anchors,
        repo_path=".",
        runner=lambda _args, _cwd=None: 0,
    )
    request = store.prepare_memory(
        context,
        task="repair failed search_docs call",
        commit_ancestry=evidence,
    )
    result = store.finalize_memory(
        request,
        allow_decision(lesson.lesson_id),
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    assert result.allowed_memory_ids == (lesson.lesson_id,)


def test_readme_pr_change_set_workflow_stays_executable():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"```python\n"
        r"(# PR_CHANGE_SET_WORKFLOW_START\n.*?# PR_CHANGE_SET_WORKFLOW_END\n)"
        r"```",
        readme,
        re.DOTALL,
    )
    assert match is not None, "README should include the PR change-set workflow"

    namespace: dict[str, object] = {}
    exec(match.group(1), namespace)

    report = namespace["report"]
    assert namespace["anchors"] == ("commit-new", "commit-old")
    assert report.related_case_ids == ["case-new", "case-old"]
    assert [
        (provenance.case_id, provenance.matched_change_endpoint)
        for provenance in report.related_case_provenance
    ] == [("case-new", "new"), ("case-old", "old")]
    assert "case-mixed" not in report.related_case_ids


def test_readme_suggested_initial_api_still_works():
    context = MemoryContext(
        mode="repair",
        repo="agent-harness",
        tenant="tenant_a",
        branch="main",
        commit_sha="abc123",
        prompt_family="planner",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
        eval_suite="tool_calling_regression",
        failure_type="invalid_tool_argument",
    )
    memory = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tenant": "tenant_a", "tool": "search_docs", "prompt_family": "planner"},
        text="When calling search_docs, always provide a non-empty natural-language query.",
        source_case_id="case_001",
    )

    allowed, blocked = system_gate(context, [memory])

    assert [item.memory_id for item in allowed] == ["lesson_001"]
    assert blocked == {}


def test_readme_implemented_public_api_pipeline_still_works(tmp_path):
    def runner(args: list[str], cwd: str | None = None) -> str:
        if args == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return "C:/work/agent-harness\n"
        if args == ["git", "branch", "--show-current"]:
            return "main\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        raise AssertionError(f"unexpected command: {args}")

    store = TraceBackedMemoryStore()
    metadata = capture_trace_metadata(repo_path=".", runner=runner)
    taxonomy = load_failure_taxonomy()

    trace = store.record_trace(
        Trace(
            trace_id="trace_001",
            run_id="run_001",
            commit_sha=metadata.commit_sha,
            repo=metadata.repo,
            tenant="tenant_a",
            branch=metadata.branch,
            dirty=metadata.dirty,
            prompt_family="planner",
            eval_suite="tool_calling_regression",
            eval_result="fail",
            trace_uri="s3://traces/trace_001.json",
            tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
            error="Invalid argument: query is required",
        )
    )

    failure_type = classify_failure_type(trace, taxonomy=taxonomy)
    case = draft_failure_case_from_trace(trace, case_id="case_001", taxonomy=taxonomy)
    reviewed = review_failure_case(
        case,
        reviewed_by="jason",
        root_cause="planner prompt omitted the search_docs query contract",
        review_notes="Confirmed by inspecting failed tool call arguments.",
    )
    verified = verify_failure_case(
        reviewed,
        fix="added schema example",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    store.add_failure_case(verified)

    lesson = lesson_from_failure_case(
        verified,
        lesson_id="lesson_001",
        lesson_text="When calling search_docs, always provide a non-empty query.",
        memory_type="procedural",
        scope={"repo": metadata.repo, "tenant": "tenant_a", "tool": "search_docs"},
    )
    store.add_lesson(lesson)

    context = parse_memory_context(
        {
            "mode": "repair",
            "repo": metadata.repo,
            "tenant": "tenant_a",
            "commit_sha": metadata.commit_sha,
            "tool": "search_docs",
            "failure_type": failure_type,
            "eval_suite": "tool_calling_regression",
        }
    )
    request = store.prepare_memory(
        context,
        task="repair failed search_docs call",
        query="search_docs null query",
    )
    result = store.finalize_memory(
        request,
        {
            "use_memory": True,
            "allowed_memory_ids": ["lesson_001"],
            "blocked_memory_ids": [],
            "reason": "The lesson directly matches the current tool failure.",
            "risk": "low",
            "recommended_injection": "short_summary",
        },
        trace_id=trace.trace_id,
        eval_result="pass",
    )

    metrics = store.metrics()
    memory_metrics = {
        item.memory_id: item for item in store.memory_outcome_metrics()
    }
    snapshot = store.to_snapshot()
    restored = TraceBackedMemoryStore.from_snapshot(snapshot)
    snapshot_path = tmp_path / "memory-store.snapshot.json"
    store.save_json(snapshot_path)
    restored_from_disk = TraceBackedMemoryStore.load_json(snapshot_path)
    lessons_yaml_path = tmp_path / "lessons.active.yaml"
    store.save_lessons_yaml(lessons_yaml_path)
    lesson_only_store = TraceBackedMemoryStore()
    lesson_only_store.record_trace(trace)
    lesson_only_store.add_failure_case(verified)
    loaded_yaml_lessons = lesson_only_store.load_lessons_yaml(lessons_yaml_path)
    pr_report = store.pr_memory_report(context, changed_fields=["tool_schema_version", "eval_suite"])

    assert case.failure_type == failure_type
    assert verified.reviewed_by == "jason"
    assert trace.repo == "agent-harness"
    assert trace.eval_suite == "tool_calling_regression"
    assert result.allowed_memory_ids == ("lesson_001",)
    assert "When calling search_docs" in result.snippet
    assert metrics.decision_count == 1
    assert metrics.pass_rate_with_memory == 1.0
    assert metrics.evaluated_with_memory_count == 1
    assert metrics.evaluated_without_memory_count == 0
    assert metrics.unevaluated_decision_count == 0
    assert memory_metrics["lesson_001"].candidate_count == 1
    assert memory_metrics["lesson_001"].used_count == 1
    assert memory_metrics["lesson_001"].observed_pass_rate == 1.0
    assert memory_metrics["case_001"].candidate_count == 0
    assert restored.lessons == store.lessons
    assert restored_from_disk.usage_logs == store.usage_logs
    assert loaded_yaml_lessons == [lesson]
    assert lesson_only_store.lessons == {"lesson_001": lesson}
    assert pr_report.related_case_ids == ["case_001"]
    assert pr_report.suggested_regression_tests == [
        "Run invalid_tool_argument regression for tool search_docs before merging."
    ]
    assert pr_report.related_case_provenance == [
        PRCaseProvenance(
            case_id="case_001",
            source_trace_id="trace_001",
            commit_sha="abc123",
            fix_commit_sha="def456",
            trace_uri="s3://traces/trace_001.json",
            failure_type="invalid_tool_argument",
        )
    ]
    assert "eval_suite change touches known failure case case_001 for search_docs." in pr_report.warnings
    assert issubclass(TraceMetadataCaptureError, RuntimeError)


def test_readme_primary_pipeline_uses_captured_metadata_values():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )

    assert 'scope={"repo": metadata.repo, "tenant": "tenant_a", "tool": "search_docs"}' in readme
    assert '"repo": metadata.repo' in readme
    assert '"commit_sha": metadata.commit_sha' in readme


def test_readme_describes_postgres_load_return_type():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    contract = " ".join(readme.split())

    assert (
        "`repository.load()` returns a normalized, validated "
        "`TraceBackedMemoryStore`"
        in contract
    )
    assert "`load()` returns a normalized store snapshot" not in contract


def test_readme_additional_public_helpers_still_work():
    trace = Trace(trace_id="trace_001", run_id="run_001", commit_sha="abc123", eval_result="fail")
    draft = draft_failure_case(
        trace,
        case_id="case_001",
        failure_type="invalid_tool_argument",
        symptom="planner called search_docs with null query",
    )
    verified = verify_failure_case(
        review_failure_case(
            draft,
            reviewed_by="test-reviewer",
            root_cause="the prompt omitted the query contract",
            reviewed_at="2026-07-22T00:00:00Z",
        ),
        fix="fixed prompt",
        fix_commit_sha="def456",
        regression_passed=True,
    )
    lesson = lesson_from_failure_case(
        verified,
        lesson_id="lesson_001",
        lesson_text="Use a non-empty query.",
        memory_type="procedural",
        scope={"tool": "search_docs"},
    )
    policy = ProjectPolicy(
        policy_id="project_policy_001",
        policy_text="Planner responses must include a tool-call rationale.",
        scope={"prompt_family": "planner"},
    )
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")

    lesson_memory = memory_item_from_lesson(
        lesson,
        source_trace=trace,
        source_case=verified,
    )
    prompt = build_llm_gate_prompt(
        context,
        [lesson_memory],
        task="repair failed tool call",
    )

    assert memory_item_from_failure_case(verified, trace).memory_id == "case_001"
    assert lesson_memory.memory_id == "lesson_001"
    assert memory_item_from_project_policy(policy).source_policy_id == "project_policy_001"
    assert obsolete_failure_case(verified).status == "obsolete"
    assert obsolete_lesson(lesson).status == "obsolete"
    assert obsolete_project_policy(policy).status == "obsolete"
    assert policy.status == "active"
    request = MemoryObsolescenceRequest("lesson", lesson.lesson_id)
    assert request.memory_kind == "lesson"
    assert request.memory_id == lesson.lesson_id
    assert "Candidate memory" in prompt


def test_readme_publishes_snapshot_operations_cli_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )

    for command in [
        "tbm snapshot validate SNAPSHOT",
        "tbm snapshot stats SNAPSHOT",
        "tbm migration plan-v3 SNAPSHOT_V2 MAPPING_JSON",
        "tbm lessons export SNAPSHOT DESTINATION [--overwrite]",
        "tbm lessons import SNAPSHOT SOURCE_YAML [--write]",
        "tbm obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]",
        "tbm obsolete-batch SNAPSHOT REQUESTS_JSON [--write]",
        "tbm audit SNAPSHOT",
        "tbm metrics SNAPSHOT",
        "tbm remediation SNAPSHOT",
        "tbm pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH",
        "tbm outcome SNAPSHOT DECISION_ID --eval-result {pass,fail,error}",
        "tbm complete SNAPSHOT TRACE_ID DECISION_ID",
        "tbm complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]",
        "tbm recover-ready SNAPSHOT [--write]",
        "tbm recover SNAPSHOT DECISION_ID",
        "tbm recover-batch SNAPSHOT DECISION_ID...",
    ]:
        assert command in readme

    normalized = " ".join(readme.split())
    assert "`python -m trace_backed_memory`" in normalized
    assert "dry-run" in normalized
    assert "`--write`" in normalized
    assert "structured JSON" in normalized
    assert "2,048 characters" in normalized
    assert "snapshot version 2" in normalized


def test_readme_publishes_deferred_outcome_cli_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "tbm outcome SNAPSHOT DECISION_ID --eval-result {pass,fail,error}",
        "--memory-caused-failure true|false",
        "`record_decision_outcome()`",
        "never completes the linked Trace",
        "dry-run",
        "`changed=true`",
        "`changed=false`",
        "previous/current outcome pair",
        "candidate/used/blocked memory IDs",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized


def test_readme_publishes_active_lessons_cli_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "tbm lessons export SNAPSHOT DESTINATION [--overwrite]",
        "tbm lessons import SNAPSHOT SOURCE_YAML [--write]",
        "active lessons only",
        "Store order",
        "8 MiB",
        "10,000-lesson",
        "not an upsert",
        "full validation dry-run",
        "symbolic link",
        "hard link",
        "`load_lessons_yaml()`",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized


def test_readme_publishes_memory_obsolescence_cli_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "tbm obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]",
        "forward-only",
        "`obsolete_failure_case()`",
        "`obsolete_lesson()`",
        "`obsolete_project_policy()`",
        "atomically obsoletes",
        "`cascaded_lesson_ids`",
        "successful no-op",
        "preview by default",
        "cannot reactivate",
        "single-item command",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized


def test_readme_publishes_atomic_batch_obsolescence_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "tbm obsolete-batch SNAPSHOT REQUESTS_JSON [--write]",
        "`MemoryObsolescenceRequest`",
        "`obsolete_memories()`",
        "strict UTF-8 JSON",
        "non-empty array",
        "10,000-item",
        "request order",
        "explicitly requested lesson",
        "`affected_count`",
        "all-or-nothing",
        "dry-run",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized


def test_readme_publishes_measured_completion_cli_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "tbm complete SNAPSHOT TRACE_ID DECISION_ID",
        "--eval-result {pass,fail,error}",
        "--memory-caused-failure true|false",
        "--tool-outputs-file PATH",
        "array of objects",
        "fresh measured result",
        "omitted",
        "dry-run",
        "`--write`",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized


def test_readme_publishes_batch_measured_completion_cli_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "tbm complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]",
        "strict UTF-8 JSON",
        "non-empty array",
        "`MemoryRunResult`",
        "`complete_memory_runs()`",
        "manifest order",
        "duplicate object keys",
        "all-or-nothing",
        "dry-run",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized


def test_readme_publishes_and_executes_packaged_resource_contract(tmp_path):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for command in [
        "tbm resource list",
        "tbm resource read schemas/trace.schema.json",
        "tbm resource export schemas/sqlite.sql sqlite.sql",
        "tbm resource export schemas/postgres.sql postgres.sql",
    ]:
        assert command in readme
    for contract in [
        "`PackagedResource`",
        "`packaged_resources()`",
        "`read_packaged_resource()`",
        "`export_packaged_resource()`",
        "byte-identical",
        "`py.typed`",
        "SHA-256",
        "source-distribution",
    ]:
        assert contract in normalized

    descriptions = packaged_resources()
    assert len(descriptions) == 57
    sqlite_expected = read_packaged_resource("schemas/sqlite.sql")
    sqlite_destination = tmp_path / "sqlite.sql"
    assert export_packaged_resource(
        "schemas/sqlite.sql",
        sqlite_destination,
    ) == sqlite_destination
    assert sqlite_destination.read_bytes() == sqlite_expected
    expected = read_packaged_resource("schemas/postgres.sql")
    destination = tmp_path / "postgres.sql"
    assert export_packaged_resource(
        "schemas/postgres.sql",
        destination,
    ) == destination
    assert destination.read_bytes() == expected
    assert load_failure_taxonomy()["invalid_tool_argument"]


def test_readme_publishes_callback_memory_run_execution_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "`run_memory_execution()`",
        "`MemoryDecisionCallback`",
        "`MemoryExecutionCallback`",
        "`MemoryRunMeasurement`",
        "`MemoryRunExecutionError`",
        "`MemoryGateRequest`",
        "`GatedMemoryResult`",
        "Store-produced `decision_id`",
        "does not infer",
        "advanced callers",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized

    assert "def decide(request):" in readme
    assert "def execute(gated):" in readme
    assert "completion = run_memory_execution(" in readme
    assert "|   |-- execution.py" in readme
    assert "|   |-- postgres.py" in readme
    assert "    |-- test_execution.py" in readme
    assert "    |-- test_postgres_repository.py" in readme
    assert "|   |-- memory_usage_log.example.json" in readme


def test_readme_publishes_atomic_lesson_yaml_persistence_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "`save_lessons_yaml()`",
        "sibling temporary file",
        "`os.fsync()`",
        "`os.replace()`",
        "`lesson_text: |`",
        "blank lines",
        "canonical LF",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract in normalized


def test_readme_publishes_bounded_local_document_ingestion_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "bounded local document ingestion",
        "single file handle",
        "64 MiB",
        "100,000 records per collection",
        "250,000 total records",
        "8 MiB",
        "10,000 lessons",
        "1 MiB",
        "1,000 failure types",
        "100,000 JSON nodes",
        "depth 100",
        "`max_bytes`",
        "`None`",
        "trusted offline migrations",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract.lower() in normalized.lower()


def test_readme_publishes_pr_report_cli_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(readme.split())

    for contract in [
        "tbm pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH",
        "`mode`, `repo`, and `commit_sha`",
        "`field_changes`",
        "`field_name`",
        "`old_value`",
        "`new_value`",
        "`pr_report_commit_anchors()`",
        "`capture_commit_ancestry()`",
        "`pr_memory_report()`",
        "same immutable `PRChangeSet`",
        "`commit_ancestry`",
        "`report`",
        "read-only",
        "exit code 2",
        "exit code 3",
        "`GIT_NO_LAZY_FETCH=1`",
        "option terminator",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]:
        assert contract.lower() in normalized.lower()
