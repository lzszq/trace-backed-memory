from pathlib import Path
import re

from trace_backed_memory import (
    FailureCase,
    Lesson,
    ProjectPolicy,
    MemoryContext,
    MemoryItem,
    PostgresMemoryRepository,
    PRCaseProvenance,
    Trace,
    TraceMetadataCaptureError,
    TraceBackedMemoryStore,
    build_llm_gate_prompt,
    capture_trace_metadata,
    classify_failure_type,
    draft_failure_case,
    draft_failure_case_from_trace,
    lesson_from_failure_case,
    load_failure_taxonomy,
    memory_item_from_failure_case,
    memory_item_from_lesson,
    memory_item_from_project_policy,
    obsolete_failure_case,
    obsolete_lesson,
    parse_memory_context,
    review_failure_case,
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
        draft_failure_case(
            trace,
            case_id="case_safe_readme",
            failure_type="invalid_tool_argument",
            symptom="search_docs received an empty query",
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
    store, trace, _case, lesson = readme_store_fixture()
    context = MemoryContext(
        mode="repair", repo=trace.repo, tenant=trace.tenant,
        commit_sha=trace.commit_sha, tool="search_docs",
    )
    request = store.prepare_memory(context, task="repair failed tool call")
    result = store.finalize_memory(
        request, allow_decision(lesson.lesson_id), trace_id=trace.trace_id,
    )
    assert result.use_memory
    assert result.decision_id == store.usage_logs[-1].decision_id


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


def test_readme_implemented_mvp_api_pipeline_still_works(tmp_path):
    def runner(args: list[str], cwd: str | None = None) -> str:
        if args == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return "C:/work/agent-harness\n"
        if args == ["git", "branch", "--show-current"]:
            return "main\n"
        if args == ["git", "status", "--porcelain"]:
            return " M README.md\n"
        raise AssertionError(f"unexpected command: {args}")

    store = TraceBackedMemoryStore()
    metadata = capture_trace_metadata(repo_path=".", runner=runner)
    taxonomy = load_failure_taxonomy("memory/failure_taxonomy.yaml")

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
        draft,
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

    prompt = build_llm_gate_prompt(context, [memory_item_from_lesson(lesson)], task="repair failed tool call")

    assert memory_item_from_failure_case(verified, trace).memory_id == "case_001"
    assert memory_item_from_lesson(lesson).memory_id == "lesson_001"
    assert memory_item_from_project_policy(policy).source_policy_id == "project_policy_001"
    assert obsolete_failure_case(verified).status == "obsolete"
    assert obsolete_lesson(lesson).status == "obsolete"
    assert "Candidate memory" in prompt
