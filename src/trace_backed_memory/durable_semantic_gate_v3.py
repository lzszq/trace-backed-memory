from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import NoReturn

from .gate_evaluation_v3 import (
    SemanticGateAttempt,
    SystemGateEvaluation,
)
from .gate_service_v3 import GateSessionWriter
from .gate_session_v3 import GATE_SESSION_MAX_LEASE_SECONDS, GateSession
from .policy import MEMORY_ID_MAX_CHARS
from .retrieval_v3 import RetrievalSnapshot
from .semantic_gate_artifact_v3 import StoredSemanticGateAttemptArtifacts
from .semantic_gate_service_v3 import (
    AuthenticatedSemanticGateService,
    AuthenticatedSemanticProviderContext,
    SemanticGateInvocationRequest,
    SemanticGateAttemptAuthority,
    SemanticGateEvidenceReader,
    SemanticGateServiceResult,
    SemanticGateServiceV3Error,
    SemanticProviderCall,
    SemanticProviderEffectRecoveryRequiredError,
    SemanticProviderInvocationFailedError,
    SemanticProviderResult,
)


DURABLE_SEMANTIC_GATE_CONTRACT_VERSION = "tbm.durable-semantic-gate.v3"
_DUMMY_SYSTEM_GATE_ID = "system_gate_sha256_" + ("0" * 64)


class DurableSemanticGateV3Error(RuntimeError):
    """Stable, sanitized failure at the durable Semantic Gate boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DurableSemanticGateRequest:
    """One version-checked mutable attempt or content-checked decided replay."""

    session_id: str
    expected_session_version: int
    prompt: bytes
    expected_previous_attempt_id: str | None = None
    lease_seconds: int = 1_800
    contract_version: str = DURABLE_SEMANTIC_GATE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != DURABLE_SEMANTIC_GATE_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if (
            not _is_identifier(self.session_id)
            or (
                self.expected_previous_attempt_id is not None
                and not _is_identifier(self.expected_previous_attempt_id)
            )
            or type(self.expected_session_version) is not int
            or self.expected_session_version < 1
            or type(self.lease_seconds) is not int
            or self.lease_seconds < 1
            or self.lease_seconds > GATE_SESSION_MAX_LEASE_SECONDS
        ):
            _invalid("durable Semantic Gate request is invalid")
        try:
            SemanticGateInvocationRequest(
                system_gate_evaluation_id=_DUMMY_SYSTEM_GATE_ID,
                prompt=self.prompt,
                expected_previous_attempt_id=(
                    self.expected_previous_attempt_id
                ),
            )
        except SemanticGateServiceV3Error:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_INVALID",
                "durable Semantic Gate request is invalid",
            ) from None

    def invocation_request(
        self,
        system_gate_evaluation_id: str,
    ) -> SemanticGateInvocationRequest:
        return SemanticGateInvocationRequest(
            system_gate_evaluation_id=system_gate_evaluation_id,
            prompt=self.prompt,
            expected_previous_attempt_id=self.expected_previous_attempt_id,
        )


@dataclass(frozen=True)
class DurableSemanticGateResult:
    """Exact durable session and attempt-artifact read-back receipt."""

    session: GateSession
    semantic_gate: SemanticGateServiceResult
    replayed: bool


class DurableSemanticGateProviderFailedError(DurableSemanticGateV3Error):
    """A failed provider attempt was retained and the session stays awaiting."""

    def __init__(
        self,
        session: GateSession,
        attempt: SemanticGateAttempt,
    ) -> None:
        self.session = session
        self.attempt = attempt
        super().__init__(
            "TBM_DURABLE_SEMANTIC_GATE_PROVIDER_FAILED",
            "semantic provider failure was retained; the session remains awaiting",
        )


class DurableSemanticGateRecoveryRequiredError(DurableSemanticGateV3Error):
    """An immutable attempt exists but session state needs explicit recovery."""

    def __init__(
        self,
        session: GateSession,
        attempt: SemanticGateAttempt | None,
    ) -> None:
        self.session = session
        self.attempt = attempt
        super().__init__(
            "TBM_DURABLE_SEMANTIC_GATE_RECOVERY_REQUIRED",
            "durable Semantic Gate state requires explicit recovery",
        )


class DurableSemanticGateProviderEffectRecoveryRequiredError(
    DurableSemanticGateV3Error
):
    """A remote provider effect may exist and must be reconciled first."""

    def __init__(self, session: GateSession, effect_id: str) -> None:
        self.session = session
        self.effect_id = effect_id
        super().__init__(
            "TBM_DURABLE_SEMANTIC_PROVIDER_EFFECT_RECOVERY_REQUIRED",
            "semantic provider effect requires explicit reconciliation",
        )


class AuthenticatedSemanticGateSessionService:
    """Advance PREPARED through AWAITING_DECISION to DECIDED."""

    def __init__(
        self,
        *,
        semantic_gate_service: AuthenticatedSemanticGateService,
        session_writer: GateSessionWriter,
    ) -> None:
        if type(semantic_gate_service) is not AuthenticatedSemanticGateService:
            raise TypeError(
                "semantic_gate_service must be AuthenticatedSemanticGateService"
            )
        if not all(
            callable(getattr(session_writer, name, None))
            for name in ("get", "renew_lease", "transition")
        ):
            raise TypeError("session_writer must satisfy GateSessionWriter")
        self._semantic_gate_service = semantic_gate_service
        self._session_writer = session_writer

    @property
    def session_authority(self) -> GateSessionWriter:
        """Return the exact durable GateSession authority."""

        return self._session_writer

    @property
    def evidence_authority(self) -> SemanticGateEvidenceReader:
        """Return the exact deterministic Gate evidence authority."""

        return self._semantic_gate_service.evidence_authority

    @property
    def semantic_authority(self) -> SemanticGateAttemptAuthority:
        """Return the exact Semantic Gate attempt/artifact authority."""

        return self._semantic_gate_service.semantic_authority

    def decide(
        self,
        context: AuthenticatedSemanticProviderContext,
        request: DurableSemanticGateRequest,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
    ) -> DurableSemanticGateResult:
        if type(context) is not AuthenticatedSemanticProviderContext:
            _invalid("authenticated semantic provider context is invalid")
        if type(request) is not DurableSemanticGateRequest:
            _invalid("durable Semantic Gate request is invalid")
        if not callable(call_provider):
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_CALLBACK_INVALID",
                "semantic provider callback is invalid",
            )

        self._semantic_gate_service._authenticate(context)  # noqa: SLF001
        session = self._load_session(request.session_id)
        if session.status == "decided":
            return self._replay_decided(context, request, session)
        if session.status not in {"prepared", "awaiting_decision"}:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_STATUS_INVALID",
                "GateSession is not prepared or awaiting a semantic decision",
            )
        if session.version != request.expected_session_version:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_CHANGED",
                "GateSession does not match the expected revision",
            )
        evaluation_id = session.system_gate_evaluation_id
        if evaluation_id is None:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_LINKAGE_INVALID",
                "GateSession is missing prepared Gate evidence",
            )
        invocation = request.invocation_request(evaluation_id)
        evaluation, snapshot, chain = (
            self._semantic_gate_service._load_verified_state(  # noqa: SLF001
                context,
                evaluation_id,
            )
        )
        self._verify_state_linkage(session, evaluation, snapshot)

        recovered = self._recoverable_success(
            request,
            session,
            chain,
        )
        if recovered is not None:
            if session.status != "awaiting_decision":
                raise DurableSemanticGateRecoveryRequiredError(
                    session,
                    recovered.attempt,
                )
            decided = self._publish_decided(
                context,
                request,
                session,
                chain,
                recovered,
            )
            return DurableSemanticGateResult(
                decided,
                recovered,
                replayed=True,
            )
        self._verify_expected_parent(request, chain)

        awaiting = session
        if session.status == "prepared":
            if chain:
                raise DurableSemanticGateRecoveryRequiredError(
                    session,
                    chain[-1],
                )
            awaiting = self._transition_awaiting(session)
        awaiting = self._renew_awaiting(awaiting, request.lease_seconds)

        try:
            semantic_result = self._semantic_gate_service.invoke(
                context,
                invocation,
                call_provider,
            )
        except SemanticProviderEffectRecoveryRequiredError as error:
            current = self._load_session(awaiting.session_id)
            if not self._same_awaiting_state(current, awaiting):
                raise DurableSemanticGateRecoveryRequiredError(
                    current,
                    None,
                ) from None
            raise DurableSemanticGateProviderEffectRecoveryRequiredError(
                current,
                error.effect_id,
            ) from None
        except SemanticProviderInvocationFailedError as error:
            self._raise_provider_failure(
                context,
                request,
                awaiting,
                error,
            )
        except SemanticGateServiceV3Error as error:
            recovered = self._recover_after_invoke_error(
                context,
                request,
                awaiting,
            )
            if recovered is not None:
                return recovered
            raise error from None

        evaluation, snapshot, retained_chain = (
            self._semantic_gate_service._load_verified_state(  # noqa: SLF001
                context,
                evaluation_id,
            )
        )
        self._verify_state_linkage(awaiting, evaluation, snapshot)
        self._verify_success_result(
            awaiting,
            retained_chain,
            semantic_result,
        )
        decided = self._publish_decided(
            context,
            request,
            awaiting,
            retained_chain,
            semantic_result,
        )
        return DurableSemanticGateResult(
            decided,
            semantic_result,
            replayed=False,
        )

    def _replay_decided(
        self,
        context: AuthenticatedSemanticProviderContext,
        request: DurableSemanticGateRequest,
        session: GateSession,
    ) -> DurableSemanticGateResult:
        evaluation_id = session.system_gate_evaluation_id
        if evaluation_id is None:
            raise DurableSemanticGateRecoveryRequiredError(session, None)
        evaluation, snapshot, chain = (
            self._semantic_gate_service._load_verified_state(  # noqa: SLF001
                context,
                evaluation_id,
            )
        )
        self._verify_state_linkage(session, evaluation, snapshot)
        result = self._recoverable_success(request, session, chain)
        attempt = None if not chain else chain[-1]
        if (
            result is None
            or session.semantic_gate_attempt_ids
            != tuple(item.attempt_id for item in chain)
            or session.decision_id != result.attempt.decision_id
        ):
            raise DurableSemanticGateRecoveryRequiredError(session, attempt)
        if self._load_session(session.session_id) != session:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_CHANGED",
                "GateSession changed during exact replay",
            )
        return DurableSemanticGateResult(session, result, replayed=True)

    def _recoverable_success(
        self,
        request: DurableSemanticGateRequest,
        session: GateSession,
        chain: tuple[SemanticGateAttempt, ...],
    ) -> SemanticGateServiceResult | None:
        succeeded = tuple(
            attempt for attempt in chain if attempt.status == "succeeded"
        )
        if not succeeded:
            return None
        terminal = chain[-1]
        if len(succeeded) != 1 or terminal is not succeeded[0]:
            raise DurableSemanticGateRecoveryRequiredError(session, terminal)
        if (
            request.expected_previous_attempt_id
            != terminal.previous_attempt_id
        ):
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_REPLAY_CONFLICT",
                "durable Semantic Gate replay does not match the retained attempt",
            )
        try:
            result = self._load_semantic_result(terminal)
        except DurableSemanticGateV3Error:
            raise DurableSemanticGateRecoveryRequiredError(
                session,
                terminal,
            ) from None
        if result.artifacts.prompt.content != request.prompt:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_REPLAY_CONFLICT",
                "durable Semantic Gate replay does not match the retained prompt",
            )
        return result

    @staticmethod
    def _verify_expected_parent(
        request: DurableSemanticGateRequest,
        chain: tuple[SemanticGateAttempt, ...],
    ) -> None:
        parent_id = None if not chain else chain[-1].attempt_id
        if request.expected_previous_attempt_id != parent_id:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_CHAIN_CHANGED",
                "semantic attempt chain does not match the expected parent",
            )

    def _transition_awaiting(self, prepared: GateSession) -> GateSession:
        try:
            awaiting = self._session_writer.transition(
                prepared.session_id,
                "awaiting_decision",
                expected_version=prepared.version,
            )
        except Exception:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_CHANGED",
                "GateSession could not enter awaiting_decision",
            ) from None
        if (
            type(awaiting) is not GateSession
            or awaiting.session_id != prepared.session_id
            or awaiting.version != prepared.version + 1
            or awaiting.status != "awaiting_decision"
            or replace(
                awaiting,
                status=prepared.status,
                version=prepared.version,
                updated_at=prepared.updated_at,
            )
            != prepared
            or self._load_session(awaiting.session_id) != awaiting
        ):
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_RECEIPT_INVALID",
                "GateSession authority returned an invalid awaiting receipt",
            )
        return awaiting

    def _renew_awaiting(
        self,
        awaiting: GateSession,
        lease_seconds: int,
    ) -> GateSession:
        try:
            renewed = self._session_writer.renew_lease(
                awaiting.session_id,
                expected_version=awaiting.version,
                lease_seconds=lease_seconds,
            )
        except Exception:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_CHANGED",
                "GateSession could not claim a live decision lease",
            ) from None
        if (
            type(renewed) is not GateSession
            or renewed.session_id != awaiting.session_id
            or renewed.version != awaiting.version + 1
            or renewed.status != "awaiting_decision"
            or renewed.lease_expires_at == awaiting.lease_expires_at
            or replace(
                renewed,
                version=awaiting.version,
                updated_at=awaiting.updated_at,
                lease_expires_at=awaiting.lease_expires_at,
            )
            != awaiting
            or self._load_session(renewed.session_id) != renewed
        ):
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_RECEIPT_INVALID",
                "GateSession authority returned an invalid lease receipt",
            )
        return renewed

    def _publish_decided(
        self,
        context: AuthenticatedSemanticProviderContext,
        request: DurableSemanticGateRequest,
        awaiting: GateSession,
        chain: tuple[SemanticGateAttempt, ...],
        semantic_result: SemanticGateServiceResult,
    ) -> GateSession:
        attempt = semantic_result.attempt
        attempt_ids = tuple(item.attempt_id for item in chain)
        try:
            decided = self._transition_decided(
                awaiting,
                attempt_ids,
                attempt.decision_id,
            )
        except Exception:
            current = self._load_session(awaiting.session_id)
            if current.status == "decided":
                try:
                    return self._replay_decided(
                        context,
                        request,
                        current,
                    ).session
                except DurableSemanticGateV3Error:
                    raise DurableSemanticGateRecoveryRequiredError(
                        current,
                        attempt,
                    ) from None
            if (
                current.status == "awaiting_decision"
                and self._same_awaiting_state(current, awaiting)
            ):
                try:
                    return self._transition_decided(
                        current,
                        attempt_ids,
                        attempt.decision_id,
                    )
                except Exception:
                    latest = self._load_session(awaiting.session_id)
                    raise DurableSemanticGateRecoveryRequiredError(
                        latest,
                        attempt,
                    ) from None
            raise DurableSemanticGateRecoveryRequiredError(
                current,
                attempt,
            ) from None
        return decided

    def _recover_after_invoke_error(
        self,
        context: AuthenticatedSemanticProviderContext,
        request: DurableSemanticGateRequest,
        awaiting: GateSession,
    ) -> DurableSemanticGateResult | None:
        current = self._load_session(awaiting.session_id)
        if current.status == "decided":
            try:
                return self._replay_decided(context, request, current)
            except DurableSemanticGateV3Error:
                raise DurableSemanticGateRecoveryRequiredError(
                    current,
                    None,
                ) from None
        attempt: SemanticGateAttempt | None = None
        evaluation_id = awaiting.system_gate_evaluation_id
        if evaluation_id is not None:
            try:
                _, _, chain = (
                    self._semantic_gate_service._load_verified_state(  # noqa: SLF001
                        context,
                        evaluation_id,
                    )
                )
                attempt = None if not chain else chain[-1]
            except Exception:
                raise DurableSemanticGateRecoveryRequiredError(
                    current,
                    None,
                ) from None
        if current == awaiting:
            current_parent_id = None if attempt is None else attempt.attempt_id
            if current_parent_id == request.expected_previous_attempt_id:
                return None
        raise DurableSemanticGateRecoveryRequiredError(
            current,
            attempt,
        ) from None

    def _transition_decided(
        self,
        awaiting: GateSession,
        attempt_ids: tuple[str, ...],
        decision_id: str | None,
    ) -> GateSession:
        if decision_id is None:
            raise DurableSemanticGateRecoveryRequiredError(
                awaiting,
                None,
            )
        decided = self._session_writer.transition(
            awaiting.session_id,
            "decided",
            expected_version=awaiting.version,
            semantic_gate_attempt_ids=attempt_ids,
            decision_id=decision_id,
        )
        if (
            type(decided) is not GateSession
            or decided.session_id != awaiting.session_id
            or decided.version != awaiting.version + 1
            or decided.status != "decided"
            or decided.semantic_gate_attempt_ids != attempt_ids
            or decided.decision_id != decision_id
            or replace(
                decided,
                status=awaiting.status,
                version=awaiting.version,
                updated_at=awaiting.updated_at,
                semantic_gate_attempt_ids=awaiting.semantic_gate_attempt_ids,
                decision_id=awaiting.decision_id,
            )
            != awaiting
            or self._load_session(decided.session_id) != decided
        ):
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_RECEIPT_INVALID",
                "GateSession authority returned an invalid decided receipt",
            )
        return decided

    def _raise_provider_failure(
        self,
        context: AuthenticatedSemanticProviderContext,
        request: DurableSemanticGateRequest,
        awaiting: GateSession,
        error: SemanticProviderInvocationFailedError,
    ) -> NoReturn:
        attempt = error.result.attempt
        try:
            evaluation_id = awaiting.system_gate_evaluation_id
            if evaluation_id is None:
                raise ValueError("missing evaluation")
            evaluation, snapshot, chain = (
                self._semantic_gate_service._load_verified_state(  # noqa: SLF001
                    context,
                    evaluation_id,
                )
            )
            self._verify_state_linkage(awaiting, evaluation, snapshot)
            if not chain or chain[-1] != attempt or attempt.status != "failed":
                raise ValueError("failed attempt was not retained as the chain head")
            current = self._load_session(awaiting.session_id)
            if current.status != "awaiting_decision":
                raise ValueError("session no longer awaits a decision")
            if not self._same_awaiting_state(current, awaiting):
                raise ValueError("session linkage changed")
            if (
                request.expected_previous_attempt_id
                != attempt.previous_attempt_id
            ):
                raise ValueError("failed attempt parent changed")
        except Exception:
            current = self._load_session(awaiting.session_id)
            raise DurableSemanticGateRecoveryRequiredError(
                current,
                attempt,
            ) from None
        raise DurableSemanticGateProviderFailedError(current, attempt) from None

    def _load_semantic_result(
        self,
        attempt: SemanticGateAttempt,
    ) -> SemanticGateServiceResult:
        try:
            artifacts = (
                self._semantic_gate_service._authority  # noqa: SLF001
                .load_attempt_with_artifacts(attempt.attempt_id)
            )
        except Exception:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_ATTEMPT_UNAVAILABLE",
                "semantic attempt artifacts are unavailable",
            ) from None
        if (
            type(artifacts) is not StoredSemanticGateAttemptArtifacts
            or artifacts.attempt != attempt
        ):
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_ATTEMPT_INVALID",
                "semantic attempt artifact receipt is invalid",
            )
        return SemanticGateServiceResult(artifacts)

    def _load_session(self, session_id: str) -> GateSession:
        try:
            session = self._session_writer.get(session_id)
        except Exception:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_UNAVAILABLE",
                "GateSession is unavailable",
            ) from None
        if type(session) is not GateSession or session.session_id != session_id:
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_SESSION_RECEIPT_INVALID",
                "GateSession authority returned an invalid receipt",
            )
        return session

    @staticmethod
    def _verify_state_linkage(
        session: GateSession,
        evaluation: SystemGateEvaluation,
        snapshot: RetrievalSnapshot,
    ) -> None:
        if (
            type(evaluation) is not SystemGateEvaluation
            or type(snapshot) is not RetrievalSnapshot
            or session.retrieval_snapshot_id != snapshot.snapshot_id
            or session.system_gate_evaluation_id != evaluation.evaluation_id
            or evaluation.retrieval_snapshot_id != snapshot.snapshot_id
            or evaluation.session_id != session.session_id
            or snapshot.session_id != session.session_id
            or snapshot.trace_id != session.trace_id
            or snapshot.run_id != session.run_id
        ):
            raise DurableSemanticGateV3Error(
                "TBM_DURABLE_SEMANTIC_GATE_LINKAGE_INVALID",
                "GateSession and Semantic Gate evidence linkage is invalid",
            )

    @staticmethod
    def _verify_success_result(
        session: GateSession,
        chain: tuple[SemanticGateAttempt, ...],
        result: SemanticGateServiceResult,
    ) -> None:
        succeeded = tuple(
            attempt for attempt in chain if attempt.status == "succeeded"
        )
        if (
            not chain
            or chain[-1] != result.attempt
            or result.attempt.status != "succeeded"
            or len(succeeded) != 1
            or succeeded[0] != chain[-1]
        ):
            raise DurableSemanticGateRecoveryRequiredError(
                session,
                result.attempt,
            )

    @staticmethod
    def _same_awaiting_state(
        current: GateSession,
        previous: GateSession,
    ) -> bool:
        return (
            current.status == "awaiting_decision"
            and replace(
                current,
                version=previous.version,
                updated_at=previous.updated_at,
                lease_expires_at=previous.lease_expires_at,
            )
            == previous
        )


def _invalid(message: str) -> NoReturn:
    raise DurableSemanticGateV3Error(
        "TBM_DURABLE_SEMANTIC_GATE_INVALID",
        message,
    )


def _is_identifier(value: object) -> bool:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_ID_MAX_CHARS
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


__all__ = [
    "DURABLE_SEMANTIC_GATE_CONTRACT_VERSION",
    "AuthenticatedSemanticGateSessionService",
    "DurableSemanticGateProviderEffectRecoveryRequiredError",
    "DurableSemanticGateProviderFailedError",
    "DurableSemanticGateRecoveryRequiredError",
    "DurableSemanticGateRequest",
    "DurableSemanticGateResult",
    "DurableSemanticGateV3Error",
]
