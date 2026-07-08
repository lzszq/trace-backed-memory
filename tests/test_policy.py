from trace_backed_memory import MemoryContext, MemoryItem, build_injection_snippet, system_gate


def test_system_gate_allows_matching_active_lesson():
    context = MemoryContext(
        mode="repair",
        repo="agent-harness",
        commit_sha="abc123",
        prompt_family="planner",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
    )
    memory = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs", "prompt_family": "planner", "tool_schema_version": "search_docs_v2"},
        text="Use a non-empty query.",
        source_case_id="case_001",
    )

    allowed, blocked = system_gate(context, [memory])

    assert [m.memory_id for m in allowed] == ["lesson_001"]
    assert blocked == {}


def test_system_gate_blocks_draft_memory():
    context = MemoryContext(mode="repair", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    memory = MemoryItem(
        memory_id="case_draft",
        status="draft",
        memory_type="episodic",
        scope={"tool": "search_docs"},
        text="Draft root cause.",
        source_trace_id="trace_001",
    )

    allowed, blocked = system_gate(context, [memory])

    assert allowed == []
    assert "case_draft" in blocked


def test_system_gate_blocks_eval_non_policy_memory():
    context = MemoryContext(mode="eval", repo="agent-harness", commit_sha="abc123", tool="search_docs")
    memory = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs"},
        text="Use a non-empty query.",
        source_case_id="case_001",
    )

    allowed, blocked = system_gate(context, [memory])

    assert allowed == []
    assert blocked["lesson_001"] == "eval mode only allows policy memory"


def test_build_injection_snippet():
    memory = MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs"},
        text="Use a non-empty query.",
        source_case_id="case_001",
    )

    snippet = build_injection_snippet([memory])

    assert "Relevant verified memory" in snippet
    assert "lesson_001" in snippet
    assert "Use a non-empty query" in snippet
