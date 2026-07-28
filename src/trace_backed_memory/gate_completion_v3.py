from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts_v3 import V3ContractError
from .gate_session_v3 import GateSession
from .outcome_v3 import (
    RunOutcome,
    RunResult,
    build_run_outcome,
    verify_run_outcome,
)


class GateCompletionV3Error(V3ContractError):
    """Stable failure at the storage-neutral GateSession completion boundary."""


@dataclass(frozen=True)
class GateCompletionRequest:
    """Server-owned request to measure and complete one executing session."""

    session_id: str
    expected_version: int
    result: RunResult
    evaluator_id: str
    evaluator_version: str
    evidence_artifact_sha256s: tuple[str, ...]
    output_sha256: str | None = None
    tool_outputs_sha256: str | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.session_id) is not str
            or not self.session_id
            or len(self.session_id) > 128
            or self.session_id.strip() != self.session_id
        ):
            _invalid("session_id must be a bounded identifier")
        if type(self.expected_version) is not int or self.expected_version < 1:
            _invalid("expected_version must be a positive integer")
        try:
            validated = build_run_outcome(
                session_id="validation_session",
                trace_id="validation_trace",
                run_id="validation_run",
                usage_decision_id="validation_usage",
                result=self.result,
                evaluator_id=self.evaluator_id,
                evaluator_version=self.evaluator_version,
                evidence_artifact_sha256s=self.evidence_artifact_sha256s,
                measured_at="1970-01-01T00:00:00Z",
                output_sha256=self.output_sha256,
                tool_outputs_sha256=self.tool_outputs_sha256,
                latency_ms=self.latency_ms,
                cost_usd=self.cost_usd,
                error_code=self.error_code,
            )
        except ValueError as error:
            raise GateCompletionV3Error(
                "TBM_GATE_COMPLETION_REQUEST_INVALID",
                "gate completion measurement failed validation",
            ) from error
        object.__setattr__(
            self,
            "evidence_artifact_sha256s",
            validated.evidence_artifact_sha256s,
        )
        object.__setattr__(self, "cost_usd", validated.cost_usd)


@dataclass(frozen=True)
class GateCompletionResult:
    session: GateSession
    outcome: RunOutcome
    inserted: bool


class GateSessionCompletionAuthority(Protocol):
    def complete_session(
        self,
        request: GateCompletionRequest,
    ) -> GateCompletionResult: ...

    def get_session(self, session_id: str) -> GateSession: ...

    def get_outcome(self, run_outcome_id: str) -> RunOutcome: ...


class GateSessionCompletionService:
    """Verify one authority-owned atomic outcome/session completion."""

    def __init__(self, authority: GateSessionCompletionAuthority) -> None:
        self._authority = authority

    def complete(self, request: GateCompletionRequest) -> GateCompletionResult:
        if type(request) is not GateCompletionRequest:
            _invalid("request must be exactly GateCompletionRequest")
        try:
            result = self._authority.complete_session(request)
        except GateCompletionV3Error:
            raise
        except Exception as error:
            raise GateCompletionV3Error(
                "TBM_GATE_COMPLETION_FAILED",
                "durable GateSession completion failed",
            ) from error
        self._verify_result(request, result)
        try:
            retained_session = self._authority.get_session(
                result.session.session_id
            )
            retained_outcome = self._authority.get_outcome(
                result.outcome.run_outcome_id
            )
        except Exception as error:
            raise GateCompletionV3Error(
                "TBM_GATE_COMPLETION_READBACK_FAILED",
                "completed GateSession and outcome could not be read back",
            ) from error
        if retained_session != result.session or retained_outcome != result.outcome:
            raise GateCompletionV3Error(
                "TBM_GATE_COMPLETION_READBACK_INVALID",
                "completed GateSession or outcome read-back changed",
            )
        return result

    @staticmethod
    def _verify_result(
        request: GateCompletionRequest,
        result: GateCompletionResult,
    ) -> None:
        if (
            type(result) is not GateCompletionResult
            or type(result.session) is not GateSession
            or type(result.outcome) is not RunOutcome
            or type(result.inserted) is not bool
            or result.session.session_id != request.session_id
            or result.session.status != "completed"
            or (
                result.inserted
                and result.session.version != request.expected_version + 1
            )
            or not _outcome_matches_request(result.outcome, request)
        ):
            raise GateCompletionV3Error(
                "TBM_GATE_COMPLETION_RECEIPT_INVALID",
                "completion authority returned an invalid receipt",
            )
        try:
            verify_run_outcome(result.outcome, result.session)
        except ValueError as error:
            raise GateCompletionV3Error(
                "TBM_GATE_COMPLETION_RECEIPT_INVALID",
                "completion authority returned inconsistent durable records",
            ) from error


def _outcome_matches_request(
    outcome: RunOutcome,
    request: GateCompletionRequest,
) -> bool:
    return (
        outcome.result == request.result
        and outcome.evaluator_id == request.evaluator_id
        and outcome.evaluator_version == request.evaluator_version
        and outcome.evidence_artifact_sha256s
        == request.evidence_artifact_sha256s
        and outcome.output_sha256 == request.output_sha256
        and outcome.tool_outputs_sha256 == request.tool_outputs_sha256
        and outcome.latency_ms == request.latency_ms
        and outcome.cost_usd == request.cost_usd
        and outcome.error_code == request.error_code
    )


def _invalid(message: str) -> None:
    raise GateCompletionV3Error(
        "TBM_GATE_COMPLETION_REQUEST_INVALID",
        message,
    )


__all__ = [
    "GateCompletionRequest",
    "GateCompletionResult",
    "GateCompletionV3Error",
    "GateSessionCompletionAuthority",
    "GateSessionCompletionService",
]
