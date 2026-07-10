from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .models import MemoryContext, MemoryDecision, MemoryItem

CONTEXT_MODES = {"debug", "repair", "regression", "planning", "eval", "production"}
CONTEXT_REQUIRED_FIELDS = {"mode", "repo", "commit_sha"}
CONTEXT_STRING_FIELDS = {
    "mode",
    "repo",
    "tenant",
    "branch",
    "commit_sha",
    "prompt_version",
    "prompt_family",
    "tool",
    "tool_schema_version",
    "model",
    "model_family",
    "eval_suite",
    "task_type",
    "failure_type",
}
ALLOWED_STATUSES = {"active", "verified"}
ALLOWED_MEMORY_TYPES = {"procedural", "semantic", "episodic", "policy"}
PRODUCTION_ALLOWED_TYPES = {"procedural", "policy"}
EVAL_ALLOWED_TYPES = {"policy"}
DECISION_RISKS = {"none", "low", "medium", "high"}
RECOMMENDED_INJECTIONS = {"none", "short_summary", "full_case_summary", "pointer_only"}
INJECTION_TEXT_MAX_CHARS = 500
DECISION_REQUIRED_FIELDS = {
    "use_memory",
    "allowed_memory_ids",
    "blocked_memory_ids",
    "reason",
    "risk",
    "recommended_injection",
}


def _scope_matches(context: MemoryContext, memory: MemoryItem) -> bool:
    """Return True when all declared memory scope fields match the current context.

    Missing context fields do not match explicit memory scope fields. This is intentionally strict.
    """
    context_values = {
        "repo": context.repo,
        "tenant": context.tenant,
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
    context_error = _context_contract_error(context)
    if context_error is not None:
        return context_error
    contract_error = _memory_item_contract_error(memory)
    if contract_error is not None:
        return contract_error
    if memory.status not in ALLOWED_STATUSES:
        return f"status {memory.status!r} is not allowed"
    if memory.memory_type not in ALLOWED_MEMORY_TYPES:
        return f"memory_type {memory.memory_type!r} is not allowed"
    if not (memory.source_case_id or memory.source_trace_id or memory.source_policy_id):
        return "missing source_case_id, source_trace_id, or source_policy_id"
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


def _memory_item_contract_error(memory: MemoryItem) -> str | None:
    if not isinstance(memory.memory_id, str) or not memory.memory_id:
        return "memory_id must be a non-empty string"
    if not isinstance(memory.scope, dict):
        return "scope must be a mapping of known non-empty string fields"
    for key, value in memory.scope.items():
        if key not in CONTEXT_STRING_FIELDS or key in {"mode", "commit_sha"}:
            return f"scope field {key!r} is not allowed"
        if not isinstance(value, str) or not value:
            return f"scope field {key!r} must be a non-empty string"
    source_values = [memory.source_case_id, memory.source_trace_id, memory.source_policy_id]
    for source in source_values:
        if source is not None and (not isinstance(source, str) or not source):
            return "source identifiers must be non-empty strings"
    if type(memory.sensitive) is not bool:
        return "sensitive must be a boolean"
    if type(memory.eval_leaking) is not bool:
        return "eval_leaking must be a boolean"
    if isinstance(memory.confidence, bool) or not isinstance(memory.confidence, (int, float)) or not 0.0 < memory.confidence <= 1.0:
        return "confidence must be greater than 0.0 and at most 1.0"
    return None


def _context_contract_error(context: MemoryContext) -> str | None:
    for field_name in CONTEXT_REQUIRED_FIELDS:
        value = getattr(context, field_name)
        if not isinstance(value, str) or not value:
            return f"context {field_name} must be a non-empty string"
    for field_name in CONTEXT_STRING_FIELDS - CONTEXT_REQUIRED_FIELDS:
        value = getattr(context, field_name)
        if value is not None and (not isinstance(value, str) or not value):
            return f"context {field_name} must be a non-empty string"
    if context.mode not in CONTEXT_MODES:
        return "context mode must be one of: debug, eval, planning, production, regression, repair"
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


def build_llm_gate_prompt(
    context: MemoryContext,
    candidates: list[MemoryItem],
    *,
    task: str,
    context_summary: str = "",
) -> str:
    """Build the semantic applicability prompt for system-approved memory."""
    _system_allowed, system_blocked = system_gate(context, candidates)
    if system_blocked:
        blocked_details = ", ".join(
            f"{memory_id}: {reason}" for memory_id, reason in system_blocked.items()
        )
        raise ValueError(f"LLM gate candidates must pass System Gate: {blocked_details}")
    context_fields = {
        "mode": context.mode,
        "repo": context.repo,
        "tenant": context.tenant,
        "branch": context.branch,
        "commit_sha": context.commit_sha,
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
    context_lines = [f"- {key}: {_json_scalar(value)}" for key, value in context_fields.items() if value is not None]

    memory_lines: list[str] = []
    for memory in candidates:
        source = memory.source_case_id or memory.source_trace_id or memory.source_policy_id or "unknown"
        scope = json.dumps(dict(sorted(memory.scope.items())), sort_keys=True)
        memory_lines.extend(
            [
                f"- id: {_json_scalar(memory.memory_id)}",
                f"  type: {_json_scalar(memory.memory_type)}",
                f"  status: {_json_scalar(memory.status)}",
                f"  scope: {scope}",
                f"  source: {_json_scalar(source)}",
                f"  confidence: {memory.confidence}",
                f"  text: {json.dumps(_cap_injected_text(memory.text))}",
            ]
        )

    return "\n".join(
        [
            "You are deciding whether retrieved memory should be used for the current LLM/agent task.",
            "",
            "Current task:",
            json.dumps(_cap_injected_text(task)),
            "",
            "Current context:",
            *(context_lines or ["- none"]),
            "",
            "Context summary:",
            json.dumps(_cap_injected_text(context_summary)) if context_summary else "none",
            "",
            "Candidate memory:",
            *(memory_lines or ["- none"]),
            "",
            "Rules:",
            "1. Use memory only if it is directly relevant to the current task.",
            "2. Do not use memory if it is obsolete, draft, low-confidence, or missing source.",
            "3. Do not use memory if its scope does not match the current repo, tenant, tool, prompt family, model family, or eval suite.",
            "4. Do not use memory if it may leak benchmark answers, evaluator reasoning, private user data, secrets, or sensitive tool output.",
            "5. In eval mode, use only project policy, prompt contract, and tool schema policy.",
            "6. In production mode, use only active, verified, scope-matched, short procedural memory.",
            "",
            "Return only JSON with use_memory, allowed_memory_ids, blocked_memory_ids, reason, risk, and recommended_injection.",
        ]
    )


def apply_llm_gate_decision(
    system_allowed: list[MemoryItem],
    system_blocked: dict[str, str],
    decision: MemoryDecision,
) -> tuple[list[MemoryItem], MemoryDecision]:
    """Apply an LLM applicability decision without letting it override System Gate."""
    allowed_by_id = {memory.memory_id: memory for memory in system_allowed}
    llm_blocked_ids = set(decision.blocked_memory_ids)
    memory_requested = decision.use_memory and decision.recommended_injection != "none"
    requested_allowed_ids = [
        memory_id
        for memory_id in (decision.allowed_memory_ids if memory_requested else [])
        if memory_id not in llm_blocked_ids
    ]
    final_allowed_ids = [memory_id for memory_id in requested_allowed_ids if memory_id in allowed_by_id]
    use_memory = memory_requested and bool(final_allowed_ids)
    if not use_memory:
        final_allowed_ids = []
    final_allowed = [allowed_by_id[memory_id] for memory_id in final_allowed_ids]

    blocked_ids: list[str] = []
    for memory_id in [*system_blocked.keys(), *decision.blocked_memory_ids]:
        if memory_id not in blocked_ids:
            blocked_ids.append(memory_id)
    for memory_id in decision.allowed_memory_ids:
        if memory_id not in allowed_by_id and memory_id not in blocked_ids:
            blocked_ids.append(memory_id)

    final_decision = MemoryDecision(
        use_memory=use_memory,
        allowed_memory_ids=final_allowed_ids,
        blocked_memory_ids=blocked_ids,
        reason=decision.reason,
        risk=decision.risk,
        recommended_injection=decision.recommended_injection if use_memory else "none",
    )

    return final_allowed, final_decision


def parse_memory_context(payload: str | Mapping[str, Any]) -> MemoryContext:
    """Parse and validate a MemoryContext payload from JSON or a mapping."""
    data = _json_object(payload, "memory context")

    missing = sorted(CONTEXT_REQUIRED_FIELDS.difference(data))
    if missing:
        raise ValueError(f"memory context missing required fields: {', '.join(missing)}")

    context_values: dict[str, str] = {}
    for field_name in CONTEXT_STRING_FIELDS:
        if field_name not in data:
            continue
        value = data[field_name]
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        if not value:
            raise ValueError(f"{field_name} must be a non-empty string")
        context_values[field_name] = value

    if context_values["mode"] not in CONTEXT_MODES:
        raise ValueError("mode must be one of: debug, eval, planning, production, regression, repair")

    return MemoryContext(**context_values)


def parse_memory_decision(payload: str | Mapping[str, Any]) -> MemoryDecision:
    """Parse and validate the JSON shape returned by the LLM applicability gate."""
    data = _json_object(payload, "memory decision")

    missing = sorted(DECISION_REQUIRED_FIELDS.difference(data))
    if missing:
        raise ValueError(f"memory decision missing required fields: {', '.join(missing)}")
    unknown = sorted(set(data).difference(DECISION_REQUIRED_FIELDS))
    if unknown:
        raise ValueError(f"memory decision has unknown fields: {', '.join(unknown)}")

    use_memory = data["use_memory"]
    if not isinstance(use_memory, bool):
        raise ValueError("use_memory must be a boolean")

    allowed_memory_ids = _string_list(data["allowed_memory_ids"], "allowed_memory_ids")
    blocked_memory_ids = _string_list(data["blocked_memory_ids"], "blocked_memory_ids")

    reason = data["reason"]
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")

    risk = data["risk"]
    if risk not in DECISION_RISKS:
        raise ValueError("risk must be one of: high, low, medium, none")

    recommended_injection = data["recommended_injection"]
    if recommended_injection not in RECOMMENDED_INJECTIONS:
        raise ValueError(
            "recommended_injection must be one of: full_case_summary, none, pointer_only, short_summary"
        )
    consistency_error = _memory_decision_consistency_error(
        use_memory=use_memory,
        allowed_memory_ids=allowed_memory_ids,
        recommended_injection=recommended_injection,
    )
    if consistency_error is not None:
        raise ValueError(consistency_error)

    return MemoryDecision(
        use_memory=use_memory,
        allowed_memory_ids=allowed_memory_ids,
        blocked_memory_ids=blocked_memory_ids,
        reason=reason,
        risk=risk,
        recommended_injection=recommended_injection,
    )


def build_injection_snippet(
    memories: list[MemoryItem],
    *,
    recommended_injection: str | None = None,
    decision: MemoryDecision | None = None,
) -> str:
    """Build a short prompt-safe memory snippet from approved memory items."""
    injection_mode = recommended_injection or (decision.recommended_injection if decision else "short_summary")
    if injection_mode not in RECOMMENDED_INJECTIONS:
        raise ValueError(
            "recommended_injection must be one of: full_case_summary, none, pointer_only, short_summary"
        )
    _validate_injection_inputs(memories, decision, recommended_injection)

    if injection_mode == "none" or not memories:
        return ""

    lines = ["Relevant verified memory:"]
    for i, memory in enumerate(memories, start=1):
        source = memory.source_case_id or memory.source_trace_id or memory.source_policy_id or "unknown"
        scope = json.dumps(dict(sorted(memory.scope.items())), sort_keys=True)
        lines.append(
            f"\n{i}. [type: {_json_scalar(memory.memory_type)}][id: {_json_scalar(memory.memory_id)}]"
            f"[source: {_json_scalar(source)}]"
        )
        lines.append(f"Scope: {scope}")
        if injection_mode != "pointer_only":
            lines.append(f"Rule: {json.dumps(_cap_injected_text(memory.text))}")
    return "\n".join(lines)


def _validate_injection_inputs(
    memories: list[MemoryItem],
    decision: MemoryDecision | None,
    requested_injection: str | None,
) -> None:
    for memory in memories:
        guardrail_error = _snippet_guardrail_error(memory)
        if guardrail_error is not None:
            raise ValueError(f"memory {memory.memory_id!r} cannot be injected: {guardrail_error}")

    if decision is None:
        if memories:
            raise ValueError("memory decision is required before injecting memory")
        return
    consistency_error = _memory_decision_consistency_error(
        use_memory=decision.use_memory,
        allowed_memory_ids=decision.allowed_memory_ids,
        recommended_injection=decision.recommended_injection,
    )
    if consistency_error is not None:
        raise ValueError(consistency_error)
    if requested_injection is not None and requested_injection != decision.recommended_injection:
        raise ValueError("recommended_injection must match the memory decision")
    if not decision.use_memory:
        if memories:
            raise ValueError("memory decision does not allow injection")
        return

    memory_ids = [memory.memory_id for memory in memories]
    if len(set(memory_ids)) != len(memory_ids):
        raise ValueError("injection memories must not contain duplicate memory IDs")
    allowed_ids = set(decision.allowed_memory_ids)
    blocked_ids = set(decision.blocked_memory_ids)
    if any(memory_id not in allowed_ids for memory_id in memory_ids):
        raise ValueError("injection memories must be listed in decision.allowed_memory_ids")
    if any(memory_id in blocked_ids for memory_id in memory_ids):
        raise ValueError("injection memories must not include decision.blocked_memory_ids")


def _snippet_guardrail_error(memory: MemoryItem) -> str | None:
    contract_error = _memory_item_contract_error(memory)
    if contract_error is not None:
        return contract_error
    if memory.status not in ALLOWED_STATUSES:
        return f"status {memory.status!r} is not allowed"
    if memory.memory_type not in ALLOWED_MEMORY_TYPES:
        return f"memory_type {memory.memory_type!r} is not allowed"
    if not (memory.source_case_id or memory.source_trace_id or memory.source_policy_id):
        return "missing source_case_id, source_trace_id, or source_policy_id"
    if memory.sensitive:
        return "memory is marked sensitive"
    if memory.eval_leaking:
        return "memory is marked eval_leaking"
    return None


def _memory_decision_consistency_error(
    *,
    use_memory: bool,
    allowed_memory_ids: list[str],
    recommended_injection: str,
) -> str | None:
    if use_memory:
        if not allowed_memory_ids:
            return "use_memory requires at least one allowed_memory_ids entry"
        if recommended_injection == "none":
            return "recommended_injection cannot be 'none' when use_memory is true"
    else:
        if allowed_memory_ids:
            return "use_memory false requires empty allowed_memory_ids"
        if recommended_injection != "none":
            return "recommended_injection must be 'none' when use_memory is false"
    return None


def _cap_injected_text(text: str) -> str:
    if len(text) <= INJECTION_TEXT_MAX_CHARS:
        return text
    return text[: INJECTION_TEXT_MAX_CHARS - 3].rstrip() + "..."


def _json_scalar(value: str) -> str:
    return json.dumps(value)


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must not contain duplicate memory IDs")
    return list(value)


def _json_object(payload: str | Mapping[str, Any], label: str) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid {label} JSON: {exc.msg}") from exc
    else:
        if not isinstance(payload, Mapping):
            raise ValueError(f"{label} payload must be a JSON object")
        data = dict(payload)

    if not isinstance(data, dict):
        raise ValueError(f"{label} payload must be a JSON object")
    return data
