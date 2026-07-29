from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, Protocol, TypeVar

from .durable_execution_v3 import (
    AuthenticatedOutcomeEvaluatorContext,
    DurableExecutionAbandonRequest,
    DurableExecutionAbandonResult,
    DurableExecutionCompletionResult,
    DurableExecutionResumeRequest,
    DurableExecutionService,
    DurableExecutionStartRequest,
    DurableExecutionStartResult,
    DurableExecutionV3Error,
)
from .durable_finalization_v3 import (
    DurableFinalizationRequest,
    DurableFinalizationResult,
    DurableFinalizationService,
    DurableFinalizationV3Error,
)
from .durable_retrieval_preparation_v3 import (
    DurableRetrievalPreparationRequest,
    DurableRetrievalPreparationService,
)
from .durable_semantic_gate_v3 import (
    AuthenticatedSemanticGateSessionService,
    DurableSemanticGateRequest,
    DurableSemanticGateResult,
    DurableSemanticGateV3Error,
)
from .gate_completion_v3 import GateCompletionRequest
from .gate_service_v3 import AuthenticatedPreparedGateResult
from .gate_session_v3 import GateSession
from .retrieval_preparation_v3 import PreparedRetrievalEvidence
from .retrieval_v3 import RetrievalSnapshot
from .semantic_gate_service_v3 import (
    AuthenticatedSemanticProviderContext,
    SemanticProviderCall,
    SemanticProviderResult,
)
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizedRetrievalScope,
)


DURABLE_AGENT_CONTRACT_VERSION = "tbm.durable-agent.v3"
_Result = TypeVar("_Result")

__all__ = [
    "DURABLE_AGENT_CONTRACT_VERSION",
    "AuthenticatedDurableAgentMemory",
    "DurableAgentCancelRequest",
    "DurableAgentCancelResult",
    "DurableAgentEvidenceReader",
    "DurableAgentSessionAuthority",
    "DurableAgentV3Error",
]


class DurableAgentV3Error(RuntimeError):
    """Stable, sanitized failure at the durable Agent composition boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DurableAgentSessionAuthority(Protocol):
    def get(self, session_id: str) -> GateSession: ...

    def transition(
        self,
        session_id: str,
        target_status: str,
        *,
        expected_version: int,
        terminal_reason: str | None = None,
    ) -> GateSession: ...


class DurableAgentEvidenceReader(Protocol):
    def load_snapshot(self, snapshot_id: str) -> RetrievalSnapshot: ...


@dataclass(frozen=True)
class DurableAgentCancelRequest:
    session_id: str
    expected_session_version: int
    reason: str
    contract_version: str = DURABLE_AGENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != DURABLE_AGENT_CONTRACT_VERSION
            or not _is_identifier(self.session_id)
            or type(self.expected_session_version) is not int
            or self.expected_session_version < 1
            or type(self.reason) is not str
            or not self.reason
            or self.reason.strip() != self.reason
            or len(self.reason) > 512
        ):
            _invalid("durable Agent cancellation request is invalid")


@dataclass(frozen=True)
class DurableAgentCancelResult:
    session: GateSession
    transition_authorization_event_id: str
    replayed: bool


class AuthenticatedDurableAgentMemory:
    """One authenticated facade over the complete durable Gate lifecycle."""

    def __init__(
        self,
        *,
        authorization_service: AuthenticatedRetrievalService,
        preparation_service: DurableRetrievalPreparationService,
        semantic_service: AuthenticatedSemanticGateSessionService,
        finalization_service: DurableFinalizationService,
        execution_service: DurableExecutionService,
    ) -> None:
        if type(authorization_service) is not AuthenticatedRetrievalService:
            raise TypeError(
                "authorization_service must be AuthenticatedRetrievalService"
            )
        if type(preparation_service) is not DurableRetrievalPreparationService:
            raise TypeError(
                "preparation_service must be DurableRetrievalPreparationService"
            )
        if type(semantic_service) is not AuthenticatedSemanticGateSessionService:
            raise TypeError(
                "semantic_service must be AuthenticatedSemanticGateSessionService"
            )
        if type(finalization_service) is not DurableFinalizationService:
            raise TypeError(
                "finalization_service must be DurableFinalizationService"
            )
        if type(execution_service) is not DurableExecutionService:
            raise TypeError(
                "execution_service must be DurableExecutionService"
            )

        gate_service = preparation_service._gate_session_service  # noqa: SLF001
        retrieval_service = preparation_service._retrieval_service  # noqa: SLF001
        session_authority = gate_service._session_writer  # noqa: SLF001
        evidence_reader = preparation_service._evidence_authority  # noqa: SLF001
        semantic_gate_service = semantic_service._semantic_gate_service  # noqa: SLF001
        if (
            gate_service._authorization_service is not authorization_service  # noqa: SLF001
            or retrieval_service._authorization_service  # noqa: SLF001
            is not authorization_service
            or finalization_service._authorization_service  # noqa: SLF001
            is not authorization_service
            or execution_service._authorization_service  # noqa: SLF001
            is not authorization_service
        ):
            raise TypeError(
                "durable Agent services must share one authorization service"
            )
        if (
            semantic_service._session_writer is not session_authority  # noqa: SLF001
            or finalization_service._session_writer  # noqa: SLF001
            is not session_authority
            or execution_service._session_writer  # noqa: SLF001
            is not session_authority
        ):
            raise TypeError(
                "durable Agent services must share one GateSession authority"
            )
        if (
            semantic_gate_service._evidence_reader is not evidence_reader  # noqa: SLF001
            or finalization_service._evidence_reader  # noqa: SLF001
            is not evidence_reader
        ):
            raise TypeError(
                "durable Agent services must share one Gate evidence authority"
            )
        if (
            semantic_gate_service._authority  # noqa: SLF001
            is not finalization_service._semantic_authority  # noqa: SLF001
        ):
            raise TypeError(
                "durable Agent services must share one Semantic Gate authority"
            )
        if (
            retrieval_service._revision_source  # noqa: SLF001
            is not finalization_service._revision_source  # noqa: SLF001
        ):
            raise TypeError(
                "durable Agent services must share one activated revision source"
            )
        if (
            execution_service._finalization_reader  # noqa: SLF001
            is not finalization_service
        ):
            raise TypeError(
                "durable execution must replay through the configured finalizer"
            )
        if not all(
            callable(getattr(session_authority, name, None))
            for name in ("get", "transition")
        ):
            raise TypeError(
                "GateSession authority must support current reads and transitions"
            )
        if not callable(getattr(evidence_reader, "load_snapshot", None)):
            raise TypeError("Gate evidence authority must support snapshot reads")

        self._authorization_service = authorization_service
        self._preparation_service = preparation_service
        self._semantic_service = semantic_service
        self._finalization_service = finalization_service
        self._execution_service = execution_service
        self._session_authority: DurableAgentSessionAuthority = session_authority
        self._evidence_reader: DurableAgentEvidenceReader = evidence_reader

    def prepare(
        self,
        context: AuthenticatedServiceContext,
        request: DurableRetrievalPreparationRequest,
    ) -> AuthenticatedPreparedGateResult[PreparedRetrievalEvidence]:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurableRetrievalPreparationRequest
        ):
            _invalid("durable Agent preparation input is invalid")
        return self._preparation_service.prepare(context, request)

    def decide(
        self,
        context: AuthenticatedServiceContext,
        provider_context: AuthenticatedSemanticProviderContext,
        request: DurableSemanticGateRequest,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
    ) -> DurableSemanticGateResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(provider_context) is not AuthenticatedSemanticProviderContext
            or type(request) is not DurableSemanticGateRequest
            or not callable(call_provider)
        ):
            _invalid("durable Agent Semantic Gate input is invalid")
        self._recover_retrieval_scope(context, request.session_id)
        return self._authorize_transition(
            context,
            lambda transition_scope: self._decide_authorized(
                context,
                provider_context,
                request,
                transition_scope,
                call_provider,
            ),
        )

    def finalize(
        self,
        context: AuthenticatedServiceContext,
        request: DurableFinalizationRequest,
    ) -> DurableFinalizationResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurableFinalizationRequest
        ):
            _invalid("durable Agent finalization input is invalid")
        _, scope = self._recover_retrieval_scope(
            context,
            request.session_id,
        )
        return self._authorize_transition(
            context,
            lambda _transition_scope: self._finalization_service.finalize(
                context,
                scope,
                request,
            ),
        )

    def start(
        self,
        context: AuthenticatedServiceContext,
        request: DurableExecutionStartRequest,
    ) -> DurableExecutionStartResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurableExecutionStartRequest
        ):
            _invalid("durable Agent execution start input is invalid")
        _, retrieval_scope = self._recover_retrieval_scope(
            context,
            request.session_id,
        )
        return self._authorize_transition(
            context,
            lambda transition_scope: self._execution_service.start(
                context,
                retrieval_scope,
                transition_scope,
                request,
            ),
        )

    def resume(
        self,
        context: AuthenticatedServiceContext,
        request: DurableExecutionResumeRequest,
    ) -> DurableExecutionStartResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurableExecutionResumeRequest
        ):
            _invalid("durable Agent execution resume input is invalid")
        _, retrieval_scope = self._recover_retrieval_scope(
            context,
            request.session_id,
        )
        return self._authorize_transition(
            context,
            lambda transition_scope: self._execution_service.resume(
                context,
                retrieval_scope,
                transition_scope,
                request,
            ),
        )

    def abandon(
        self,
        context: AuthenticatedServiceContext,
        request: DurableExecutionAbandonRequest,
    ) -> DurableExecutionAbandonResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurableExecutionAbandonRequest
        ):
            _invalid("durable Agent execution abandonment input is invalid")
        self._recover_retrieval_scope(context, request.session_id)
        return self._authorize_transition(
            context,
            lambda transition_scope: self._execution_service.abandon(
                context,
                transition_scope,
                request,
            ),
        )

    def complete(
        self,
        context: AuthenticatedServiceContext,
        evaluator_context: AuthenticatedOutcomeEvaluatorContext,
        request: GateCompletionRequest,
    ) -> DurableExecutionCompletionResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(evaluator_context) is not AuthenticatedOutcomeEvaluatorContext
            or type(request) is not GateCompletionRequest
        ):
            _invalid("durable Agent execution completion input is invalid")
        self._recover_retrieval_scope(context, request.session_id)
        return self._authorize_transition(
            context,
            lambda transition_scope: self._execution_service.complete(
                context,
                transition_scope,
                evaluator_context,
                request,
            ),
        )

    def cancel(
        self,
        context: AuthenticatedServiceContext,
        request: DurableAgentCancelRequest,
    ) -> DurableAgentCancelResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurableAgentCancelRequest
        ):
            _invalid("durable Agent cancellation request is invalid")
        self._recover_retrieval_scope(context, request.session_id)
        return self._authorize_transition(
            context,
            lambda transition_scope: self._cancel_authorized(
                context,
                transition_scope,
                request,
            ),
        )

    def get_session(
        self,
        context: AuthenticatedServiceContext,
        session_id: str,
    ) -> GateSession:
        if (
            type(context) is not AuthenticatedServiceContext
            or not _is_identifier(session_id)
        ):
            _invalid("durable Agent session lookup is invalid")
        session, _ = self._recover_retrieval_scope(context, session_id)
        return session

    def _decide_authorized(
        self,
        context: AuthenticatedServiceContext,
        provider_context: AuthenticatedSemanticProviderContext,
        request: DurableSemanticGateRequest,
        transition_scope: AuthorizedRetrievalScope,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
    ) -> DurableSemanticGateResult:
        def call_rechecked_provider(
            provider_call: SemanticProviderCall,
        ) -> SemanticProviderResult:
            self._recover_retrieval_scope(context, request.session_id)
            result = call_provider(provider_call)
            self._recover_retrieval_scope(context, request.session_id)
            self._verify_transition_scope(context, transition_scope)
            return result

        return self._semantic_service.decide(
            provider_context,
            request,
            call_rechecked_provider,
        )

    def _cancel_authorized(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        request: DurableAgentCancelRequest,
    ) -> DurableAgentCancelResult:
        self._verify_transition_scope(context, scope)
        current = self._load_session(request.session_id)
        self._verify_session_scope(current, scope)
        if (
            current.status == "canceled"
            and current.version == request.expected_session_version + 1
            and current.terminal_reason == request.reason
        ):
            return DurableAgentCancelResult(
                current,
                scope.authorization_event_id,
                True,
            )
        if current.status not in {"prepared", "awaiting_decision"}:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_CANCEL_STATUS_INVALID",
                "GateSession cannot be canceled from its current status",
            )
        if current.version != request.expected_session_version:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SESSION_CHANGED",
                "GateSession does not match the expected cancellation revision",
            )
        try:
            canceled = self._session_authority.transition(
                current.session_id,
                "canceled",
                expected_version=current.version,
                terminal_reason=request.reason,
            )
        except Exception as error:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_CANCEL_FAILED",
                "durable Agent session could not be canceled",
            ) from error
        if (
            type(canceled) is not GateSession
            or canceled.status != "canceled"
            or canceled.version != current.version + 1
            or canceled.terminal_reason != request.reason
            or self._load_session(current.session_id) != canceled
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_CANCEL_RECEIPT_INVALID",
                "durable Agent cancellation receipt is invalid",
            )
        return DurableAgentCancelResult(
            canceled,
            scope.authorization_event_id,
            False,
        )

    def _authorize_transition(
        self,
        context: AuthenticatedServiceContext,
        operation: Callable[[AuthorizedRetrievalScope], _Result],
    ) -> _Result:
        def verified_operation(scope: AuthorizedRetrievalScope) -> _Result:
            self._verify_transition_scope(context, scope)
            return operation(scope)

        try:
            authorized = self._authorization_service.authorize_permission(
                context,
                permission="gate_session:transition",
                operation=verified_operation,
            )
        except AuthenticatedServiceV3Error as error:
            cause = error.__cause__
            if (
                error.code == "TBM_SERVICE_RETRIEVAL_FAILED"
                and isinstance(
                    cause,
                    (
                        AuthenticatedServiceV3Error,
                        DurableAgentV3Error,
                        DurableExecutionV3Error,
                        DurableFinalizationV3Error,
                        DurableSemanticGateV3Error,
                    ),
                )
            ):
                raise cause
            raise
        return authorized.value

    def _verify_transition_scope(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
    ) -> None:
        self._authorization_service.verify_authorized_scope(
            context,
            scope,
            permission="gate_session:transition",
        )

    def _recover_retrieval_scope(
        self,
        context: AuthenticatedServiceContext,
        session_id: str,
    ) -> tuple[GateSession, AuthorizedRetrievalScope]:
        if (
            type(context) is not AuthenticatedServiceContext
            or not _is_identifier(session_id)
        ):
            _invalid("durable Agent session lookup is invalid")
        session = self._load_session(session_id)
        snapshot_id = session.retrieval_snapshot_id
        if snapshot_id is None:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_LINKAGE_INVALID",
                "GateSession is missing retained retrieval evidence",
            )
        try:
            snapshot = self._evidence_reader.load_snapshot(snapshot_id)
        except Exception as error:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_EVIDENCE_UNAVAILABLE",
                "durable Agent retrieval evidence could not be loaded",
            ) from error
        if (
            type(snapshot) is not RetrievalSnapshot
            or snapshot.snapshot_id != snapshot_id
            or snapshot.session_id != session.session_id
            or snapshot.trace_id != session.trace_id
            or snapshot.run_id != session.run_id
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_LINKAGE_INVALID",
                "GateSession retrieval evidence linkage is invalid",
            )
        scope = self._authorization_service.recover_authorized_scope(
            context,
            snapshot.authorization_event_id,
            permission="memory:retrieve",
        )
        if scope.authorization_event_id != snapshot.authorization_event_id:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_LINKAGE_INVALID",
                "GateSession retrieval authorization linkage is invalid",
            )
        self._verify_session_scope(session, scope)
        retained_session = self._load_session(session_id)
        if retained_session != session:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SESSION_CHANGED",
                "GateSession changed during durable continuation",
            )
        return retained_session, scope

    def _load_session(self, session_id: str) -> GateSession:
        try:
            session = self._session_authority.get(session_id)
        except Exception as error:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SESSION_UNAVAILABLE",
                "durable Agent session could not be loaded",
            ) from error
        if (
            type(session) is not GateSession
            or session.session_id != session_id
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SESSION_INVALID",
                "durable Agent session read-back is invalid",
            )
        return session

    @staticmethod
    def _verify_session_scope(
        session: GateSession,
        scope: AuthorizedRetrievalScope,
    ) -> None:
        if (
            session.principal_id != scope.principal_id
            or session.agent_client_id != scope.agent_client_id
            or session.tenant_id != scope.tenant_id
            or session.repository_id != scope.repository_id
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SCOPE_MISMATCH",
                "durable Agent session is outside the authorized scope",
            )


def _is_identifier(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.strip() == value
        and len(value) <= 128
    )


def _invalid(message: str) -> NoReturn:
    raise DurableAgentV3Error(
        "TBM_DURABLE_AGENT_INVALID",
        message,
    )
