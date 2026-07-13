from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["debug", "repair", "regression", "planning", "eval", "production"]
Status = Literal["draft", "verified", "active", "obsolete"]
MemoryType = Literal["procedural", "semantic", "episodic", "policy"]
EvalResult = Literal["pass", "fail", "error", "unknown"]
FailureCaseStatus = Literal["draft", "verified", "obsolete"]
LessonStatus = Literal["active", "obsolete"]
PRChangeEndpoint = Literal["old", "new", "both"]


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
    tenant: str | None = None
    input_hash: str | None = None


@dataclass(frozen=True)
class Trace:
    trace_id: str
    run_id: str
    commit_sha: str
    repo: str | None = None
    tenant: str | None = None
    branch: str | None = None
    dirty: bool = False
    prompt_version: str | None = None
    prompt_family: str | None = None
    tool_schema_version: str | None = None
    model: str | None = None
    eval_suite: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    retrieved_context: list[dict[str, object]] = field(default_factory=list)
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    tool_outputs: list[dict[str, object]] = field(default_factory=list)
    eval_result: EvalResult = "unknown"
    latency_ms: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    trace_uri: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class TraceMetadata:
    commit_sha: str
    repo: str | None
    branch: str | None
    dirty: bool


@dataclass(frozen=True)
class CommitAncestryEvidence:
    current_commit_sha: str
    commit_relations: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class FailureCase:
    case_id: str
    source_trace_id: str
    commit_sha: str
    failure_type: str
    symptom: str
    root_cause: str | None = None
    fix: str | None = None
    fix_commit_sha: str | None = None
    regression_passed: bool = False
    reviewed_by: str | None = None
    review_notes: str | None = None
    reviewed_at: str | None = None
    status: FailureCaseStatus = "draft"
    created_at: str | None = None


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    source_case_id: str
    lesson_text: str
    memory_type: MemoryType
    scope: dict[str, str]
    confidence: float = 1.0
    sensitive: bool = False
    eval_leaking: bool = False
    status: LessonStatus = "active"
    created_at: str | None = None


@dataclass(frozen=True)
class ProjectPolicy:
    policy_id: str
    policy_text: str
    scope: dict[str, str]
    confidence: float = 1.0
    sensitive: bool = False
    eval_leaking: bool = False
    status: LessonStatus = "active"
    created_at: str | None = None


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
    source_policy_id: str | None = None
    source_eval_suite: str | None = None
    source_input_hash: str | None = None


@dataclass(frozen=True)
class MemoryDecision:
    use_memory: bool
    allowed_memory_ids: list[str]
    blocked_memory_ids: list[str]
    reason: str
    risk: Literal["none", "low", "medium", "high"]
    recommended_injection: Literal["none", "short_summary", "full_case_summary", "pointer_only"]


@dataclass(frozen=True)
class MemoryGateRequest:
    request_id: str
    context: MemoryContext
    candidate_memory_ids: tuple[str, ...]
    system_allowed_memory_ids: tuple[str, ...]
    system_blocked: tuple[tuple[str, str], ...]
    prompt: str
    _store_token: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class GatedMemoryResult:
    request_id: str
    trace_id: str
    decision_id: str
    use_memory: bool
    allowed_memory_ids: tuple[str, ...]
    blocked_memory_ids: tuple[str, ...]
    reason: str
    risk: Literal["none", "low", "medium", "high"]
    recommended_injection: Literal["none", "short_summary", "full_case_summary", "pointer_only"]
    snippet: str


@dataclass(frozen=True)
class MemoryUsageLog:
    decision_id: str
    run_id: str
    mode: Mode
    candidate_memory_ids: list[str]
    used_memory_ids: list[str]
    blocked_memory_ids: list[str]
    reason: str
    risk: Literal["none", "low", "medium", "high"]
    recommended_injection: Literal["none", "short_summary", "full_case_summary", "pointer_only"]
    eval_result: EvalResult | None = None
    memory_caused_failure: bool = False
    trace_id: str | None = None
    context: dict[str, str] = field(default_factory=dict)
    candidate_memory_statuses: dict[str, Status] = field(default_factory=dict)
    system_blocked_reasons: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None


@dataclass(frozen=True)
class MemoryMetrics:
    decision_count: int
    candidate_memory_count: int
    used_memory_count: int
    blocked_memory_count: int
    obsolete_memory_usage_attempts: int
    average_lesson_confidence: float
    pass_rate_with_memory: float | None = None
    pass_rate_without_memory: float | None = None
    wrong_memory_failure_count: int = 0
    evaluated_with_memory_count: int = 0
    evaluated_without_memory_count: int = 0
    unevaluated_decision_count: int = 0


@dataclass(frozen=True)
class MemoryOutcomeMetrics:
    memory_id: str
    candidate_count: int
    used_count: int
    blocked_count: int
    evaluated_use_count: int
    passed_use_count: int
    failed_or_errored_use_count: int
    unevaluated_use_count: int
    observed_pass_rate: float | None


@dataclass(frozen=True)
class PRChangeSet:
    field_changes: tuple[tuple[str, str | None, str | None], ...]


@dataclass(frozen=True)
class PRCaseProvenance:
    case_id: str
    source_trace_id: str
    commit_sha: str
    fix_commit_sha: str | None
    trace_uri: str | None
    failure_type: str
    matched_change_endpoint: PRChangeEndpoint | None = None


@dataclass(frozen=True)
class PRMemoryReport:
    related_case_ids: list[str]
    suggested_regression_tests: list[str]
    warnings: list[str]
    related_case_provenance: list[PRCaseProvenance] = field(default_factory=list)
