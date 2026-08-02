from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from .contracts_v3 import V3ContractError
from .gate_session_v3 import GateSession, GateSessionStatus


GateSessionRecoveryOutcome = Literal[
    "expired",
    "recovery_required",
    "superseded",
]


class GateSessionRecoveryWorkerError(V3ContractError):
    """Stable failure while scanning durable GateSession recovery work."""


class GateSessionDueRepository(Protocol):
    def list_due(self, *, limit: int = 100) -> tuple[GateSession, ...]: ...

    def get(self, session_id: str) -> GateSession: ...

    def transition(
        self,
        session_id: str,
        target_status: str,
        *,
        expected_version: int,
        lease_seconds: int | None = None,
        retrieval_snapshot_id: str | None = None,
        system_gate_evaluation_id: str | None = None,
        semantic_gate_attempt_ids: tuple[str, ...] | None = None,
        decision_id: str | None = None,
        final_memory_revision_ids: tuple[str, ...] | None = None,
        injection_artifact_id: str | None = None,
        usage_decision_id: str | None = None,
        run_outcome_id: str | None = None,
        terminal_reason: str | None = None,
    ) -> GateSession: ...


@dataclass(frozen=True)
class GateSessionRecoveryResult:
    session_id: str
    observed_version: int
    observed_status: GateSessionStatus
    outcome: GateSessionRecoveryOutcome
    current: GateSession


class GateSessionRecoveryWorker:
    """Perform one bounded, CAS-safe pass over unlocked due candidates."""

    def __init__(self, repository: GateSessionDueRepository) -> None:
        self._repository = repository

    def run_once(
        self,
        *,
        limit: int = 100,
    ) -> tuple[GateSessionRecoveryResult, ...]:
        if type(limit) is not int or limit < 1 or limit > 10_000:
            raise GateSessionRecoveryWorkerError(
                "TBM_GATE_WORKER_LIMIT_INVALID",
                "GateSession recovery limit is invalid",
            )
        try:
            candidates = self._repository.list_due(limit=limit)
        except Exception as error:
            raise GateSessionRecoveryWorkerError(
                "TBM_GATE_WORKER_DISCOVERY_FAILED",
                "GateSession due discovery failed",
            ) from error
        if type(candidates) is not tuple or len(candidates) > limit:
            _invalid_candidates()
        seen: set[str] = set()
        for candidate in candidates:
            if (
                type(candidate) is not GateSession
                or candidate.session_id in seen
                or candidate.status
                not in {
                    "prepared",
                    "awaiting_decision",
                    "decided",
                    "finalized",
                    "executing",
                }
            ):
                _invalid_candidates()
            seen.add(candidate.session_id)
        return tuple(self._recover(candidate) for candidate in candidates)

    def _recover(
        self,
        candidate: GateSession,
    ) -> GateSessionRecoveryResult:
        if candidate.status in {"prepared", "awaiting_decision"}:
            try:
                binder = getattr(
                    self._repository,
                    "bind_recovery_event_context",
                    None,
                )
                binding = (
                    binder(candidate) if callable(binder) else nullcontext()
                )
                with binding:
                    expired = self._repository.transition(
                        candidate.session_id,
                        "expired",
                        expected_version=candidate.version,
                        terminal_reason="session_expired",
                    )
                self._verify_expired(candidate, expired)
                if self._repository.get(candidate.session_id) != expired:
                    raise GateSessionRecoveryWorkerError(
                        "TBM_GATE_WORKER_RECEIPT_INVALID",
                        "expired GateSession receipt was not durably retained",
                    )
                return self._result(candidate, "expired", expired)
            except GateSessionRecoveryWorkerError:
                raise
            except Exception:
                return self._classify_after_failure(candidate)
        return self._classify_current(candidate)

    def _classify_after_failure(
        self,
        candidate: GateSession,
    ) -> GateSessionRecoveryResult:
        try:
            current = self._repository.get(candidate.session_id)
        except Exception as error:
            raise GateSessionRecoveryWorkerError(
                "TBM_GATE_WORKER_RECOVERY_READ_FAILED",
                "GateSession recovery state could not be read",
            ) from error
        self._validate_current(candidate, current)
        outcome: GateSessionRecoveryOutcome = (
            "superseded"
            if current.version != candidate.version
            or current.status != candidate.status
            else "recovery_required"
        )
        return self._result(candidate, outcome, current)

    def _classify_current(
        self,
        candidate: GateSession,
    ) -> GateSessionRecoveryResult:
        try:
            current = self._repository.get(candidate.session_id)
        except Exception as error:
            raise GateSessionRecoveryWorkerError(
                "TBM_GATE_WORKER_RECOVERY_READ_FAILED",
                "GateSession recovery state could not be read",
            ) from error
        self._validate_current(candidate, current)
        outcome: GateSessionRecoveryOutcome = (
            "superseded"
            if current.version != candidate.version
            or current.status != candidate.status
            else "recovery_required"
        )
        return self._result(candidate, outcome, current)

    @staticmethod
    def _validate_current(
        candidate: GateSession,
        current: GateSession,
    ) -> None:
        if (
            type(current) is not GateSession
            or current.session_id != candidate.session_id
            or current.contract_version != candidate.contract_version
            or current.tenant_id != candidate.tenant_id
            or current.repository_id != candidate.repository_id
            or current.principal_id != candidate.principal_id
            or current.agent_client_id != candidate.agent_client_id
            or current.trace_id != candidate.trace_id
            or current.run_id != candidate.run_id
            or current.request_fingerprint != candidate.request_fingerprint
            or current.idempotency_key != candidate.idempotency_key
            or current.created_at != candidate.created_at
            or current.expires_at != candidate.expires_at
            or current.version < candidate.version
            or (
                current.version == candidate.version
                and current != candidate
            )
        ):
            raise GateSessionRecoveryWorkerError(
                "TBM_GATE_WORKER_CURRENT_INVALID",
                "GateSession authority returned invalid current state",
            )

    @staticmethod
    def _verify_expired(
        candidate: GateSession,
        expired: GateSession,
    ) -> None:
        if (
            type(expired) is not GateSession
            or expired.session_id != candidate.session_id
            or expired.version != candidate.version + 1
            or expired.status != "expired"
            or expired.terminal_reason != "session_expired"
            or replace(
                expired,
                version=candidate.version,
                status=candidate.status,
                updated_at=candidate.updated_at,
                lease_expires_at=candidate.lease_expires_at,
                terminal_reason=candidate.terminal_reason,
            )
            != candidate
        ):
            raise GateSessionRecoveryWorkerError(
                "TBM_GATE_WORKER_RECEIPT_INVALID",
                "GateSession authority returned an invalid expiry receipt",
            )

    @staticmethod
    def _result(
        candidate: GateSession,
        outcome: GateSessionRecoveryOutcome,
        current: GateSession,
    ) -> GateSessionRecoveryResult:
        return GateSessionRecoveryResult(
            session_id=candidate.session_id,
            observed_version=candidate.version,
            observed_status=candidate.status,
            outcome=outcome,
            current=current,
        )


def _invalid_candidates() -> None:
    raise GateSessionRecoveryWorkerError(
        "TBM_GATE_WORKER_CANDIDATES_INVALID",
        "GateSession due discovery returned invalid candidates",
    )
