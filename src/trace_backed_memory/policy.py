from __future__ import annotations

from .models import MemoryContext, MemoryItem

ALLOWED_STATUSES = {"active", "verified"}
PRODUCTION_ALLOWED_TYPES = {"procedural", "policy"}
EVAL_ALLOWED_TYPES = {"policy"}


def _scope_matches(context: MemoryContext, memory: MemoryItem) -> bool:
    """Return True when all declared memory scope fields match the current context.

    Missing context fields do not match explicit memory scope fields. This is intentionally strict.
    """
    context_values = {
        "repo": context.repo,
        "branch": context.branch,
        "prompt_version": context.prompt_version,
        "prompt_family": context.prompt_family,
        "tool": context.tool,
        "tool_schema_version": context.tool_schema_version,
        "model": context.model,
        "model_family": context.model_family,
        "eval_suite": context.eval_suite,
        "task_type": context.task_type,
        "failure_type": context.failure_type,
    }

    if not memory.scope:
        return False

    for key, value in memory.scope.items():
        if context_values.get(key) != value:
            return False
    return True


def _blocked_reason(context: MemoryContext, memory: MemoryItem) -> str | None:
    if memory.status not in ALLOWED_STATUSES:
        return f"status {memory.status!r} is not allowed"
    if not (memory.source_case_id or memory.source_trace_id):
        return "missing source_case_id or source_trace_id"
    if memory.sensitive:
        return "memory is marked sensitive"
    if memory.eval_leaking:
        return "memory may leak eval data"
    if not _scope_matches(context, memory):
        return "scope does not match current context"
    if context.mode == "eval" and memory.memory_type not in EVAL_ALLOWED_TYPES:
        return "eval mode only allows policy memory"
    if context.mode == "production" and memory.memory_type not in PRODUCTION_ALLOWED_TYPES:
        return "production mode only allows procedural or policy memory"
    return None


def system_gate(context: MemoryContext, candidates: list[MemoryItem]) -> tuple[list[MemoryItem], dict[str, str]]:
    """Deterministically filter candidate memory before any LLM gate.

    Returns:
        allowed: memory items that passed system policy.
        blocked: mapping of memory_id -> blocked reason.
    """
    allowed: list[MemoryItem] = []
    blocked: dict[str, str] = {}

    for memory in candidates:
        reason = _blocked_reason(context, memory)
        if reason is None:
            allowed.append(memory)
        else:
            blocked[memory.memory_id] = reason

    return allowed, blocked


def build_injection_snippet(memories: list[MemoryItem]) -> str:
    """Build a short prompt-safe memory snippet from approved memory items."""
    if not memories:
        return ""

    lines = ["Relevant verified memory:"]
    for i, memory in enumerate(memories, start=1):
        source = memory.source_case_id or memory.source_trace_id or "unknown"
        scope = ", ".join(f"{k}={v}" for k, v in sorted(memory.scope.items()))
        lines.append(f"\n{i}. [{memory.memory_type}][{memory.memory_id}][source: {source}]")
        lines.append(f"Scope: {scope}")
        lines.append(f"Rule: {memory.text}")
    return "\n".join(lines)
