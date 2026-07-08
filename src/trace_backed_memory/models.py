from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["debug", "repair", "regression", "planning", "eval", "production"]
Status = Literal["draft", "verified", "active", "obsolete"]
MemoryType = Literal["procedural", "semantic", "episodic", "policy"]


@dataclass(frozen=True)
class MemoryContext:
    mode: Mode
    repo: str
    commit_sha: str
    branch: str | None = None
    prompt_version: str | None = None
    prompt_family: str | None = None
    tool: str | None = None
    tool_schema_version: str | None = None
    model: str | None = None
    model_family: str | None = None
    eval_suite: str | None = None
    task_type: str | None = None
    failure_type: str | None = None


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    status: Status
    memory_type: MemoryType
    scope: dict[str, str] = field(default_factory=dict)
    text: str = ""
    source_trace_id: str | None = None
    source_case_id: str | None = None
    confidence: float = 1.0
    sensitive: bool = False
    eval_leaking: bool = False


@dataclass(frozen=True)
class MemoryDecision:
    use_memory: bool
    allowed_memory_ids: list[str]
    blocked_memory_ids: list[str]
    reason: str
    risk: Literal["none", "low", "medium", "high"]
    recommended_injection: Literal["none", "short_summary", "full_case_summary", "pointer_only"]
