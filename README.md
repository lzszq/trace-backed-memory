# Trace-backed Memory

A provenance-backed memory layer for LLM / agent harness engineering.

## One-liner

Trace-backed Memory turns immutable agent traces, eval results, and git commits into verified, scoped, auditable memory that can be used selectively during debug, repair, regression analysis, planning, and production runtime.

## What this is

This project is not generic chatbot memory. It is a harness-oriented memory system:

```text
trace -> failure case -> verified lesson -> gated runtime memory
```

The system is designed around five rules:

1. Trace is the source of truth.
2. Memory is a curated projection derived from trace, eval, and git history.
3. Raw trace should not be injected into prompts by default.
4. Memory must pass both system gate and LLM applicability gate before use.
5. Every memory item must have source, scope, status, and usage logs.

## Core concepts

| Concept | Purpose | LLM-visible by default |
|---|---|---:|
| Trace | Immutable run provenance: prompts, tool calls, outputs, eval, commit | No |
| Failure Case | Structured postmortem of a failed run | Debug / repair only |
| Verified Lesson | Validated reusable rule derived from a case | Yes, if scoped and gated |
| Project Policy | Manually maintained prompt/tool/eval policy | Yes, if relevant |
| Memory Decision | Audit record of why memory was used or blocked | No |

## MVP architecture

```text
Git commit / PR / CI
        ↓
Harness run
        ↓
Trace store
        ↓
Eval result
        ↓
Failure detection
        ↓
Failure case draft
        ↓
Verification / regression
        ↓
Verified lesson
        ↓
Memory index
        ↓
System gate
        ↓
LLM applicability gate
        ↓
Runtime injection
        ↓
Memory usage log
```

## Suggested initial API

```python
from trace_backed_memory import MemoryContext, MemoryItem, system_gate

context = MemoryContext(
    mode="repair",
    repo="agent-harness",
    branch="main",
    commit_sha="abc123",
    prompt_family="planner",
    tool="search_docs",
    tool_schema_version="search_docs_v2",
    eval_suite="tool_calling_regression",
    failure_type="invalid_tool_argument",
)

candidates = [
    MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tool": "search_docs", "prompt_family": "planner"},
        text="When calling search_docs, always provide a non-empty natural-language query.",
        source_case_id="case_001",
    )
]

allowed, blocked = system_gate(context, candidates)
```

## Repository layout

```text
.
├── docs/
│   ├── architecture.md
│   ├── usage-policy.md
│   └── mvp-roadmap.md
├── examples/
│   ├── trace.example.json
│   ├── failure_case.example.json
│   ├── lesson.example.json
│   └── memory_decision.example.json
├── memory/
│   ├── lessons.example.yaml
│   └── failure_taxonomy.yaml
├── schemas/
│   ├── postgres.sql
│   ├── memory_context.schema.json
│   └── memory_decision.schema.json
├── src/trace_backed_memory/
│   ├── __init__.py
│   ├── models.py
│   └── policy.py
└── tests/
    └── test_policy.py
```
