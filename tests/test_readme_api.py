from trace_backed_memory import (
    ProjectPolicy,
    MemoryContext,
    MemoryItem,
    PRCaseProvenance,
    Trace,
    TraceMetadataCaptureError,
    TraceBackedMemoryStore,
    apply_llm_gate_decision,
    build_injection_snippet,
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
    parse_memory_decision,
    review_failure_case,
    system_gate,
    verify_failure_case,
)


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
        scope={"tenant": "tenant_a", "tool": "search_docs"},
    )
    store.add_lesson(lesson)

    context = parse_memory_context(
        {
            "mode": "repair",
            "repo": "agent-harness",
            "tenant": "tenant_a",
            "commit_sha": "abc123",
            "tool": "search_docs",
            "failure_type": failure_type,
            "eval_suite": "tool_calling_regression",
        }
    )
    candidates = store.candidate_memories(context, query="search_docs null query")
    system_allowed, system_blocked = system_gate(context, candidates)
    llm_decision = parse_memory_decision(
        {
            "use_memory": True,
            "allowed_memory_ids": ["lesson_001"],
            "blocked_memory_ids": [],
            "reason": "The lesson directly matches the current tool failure.",
            "risk": "low",
            "recommended_injection": "short_summary",
        }
    )
    allowed, final_decision = apply_llm_gate_decision(system_allowed, system_blocked, llm_decision)
    snippet = build_injection_snippet(allowed, decision=final_decision)
    store.log_decision(
        "run_001",
        context,
        [memory.memory_id for memory in candidates],
        final_decision,
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
    assert [memory.memory_id for memory in allowed] == ["lesson_001"]
    assert "When calling search_docs" in snippet
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
