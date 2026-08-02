from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, Protocol, TypeVar, cast

from .durable_composition_v3 import (
    DurableCompositionV3Error,
    DurableServiceBundle,
)
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
from .gate_service_v3 import (
    AuthenticatedPreparedGateResult,
    bind_authority_event_context,
    bind_gate_session_event_context,
)
from .gate_session_v3 import GateSession
from .replay_export_v3 import (
    REPLAY_EXPORT_MAX_CONTENT_BYTES,
    ReplayBundleExport,
    ReplayExportError,
    ReplayExportReader,
    export_replay_bundle as export_authorized_replay_bundle,
)
from .replay_v3 import DataClassification, DecisionReplayManifest
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
DURABLE_REPLAY_EXPORT_CONTRACT_VERSION = "tbm.durable-replay-export.v3"
_Result = TypeVar("_Result")

__all__ = [
    "DURABLE_AGENT_CONTRACT_VERSION",
    "DURABLE_REPLAY_EXPORT_CONTRACT_VERSION",
    "AuthenticatedDurableAgentMemory",
    "DurableAgentCancelRequest",
    "DurableAgentCancelResult",
    "DurableAgentEvidenceReader",
    "DurableAgentReplayExportReader",
    "DurableAgentSessionAuthority",
    "DurableReplayExportRequest",
    "DurableReplayExportResult",
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


class DurableAgentReplayExportReader(ReplayExportReader, Protocol):
    def load_manifest_for_session(
        self,
        session_id: str,
        decision_id: str,
        usage_decision_id: str,
        injection_artifact_id: str,
    ) -> DecisionReplayManifest: ...


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


@dataclass(frozen=True)
class DurableReplayExportRequest:
    session_id: str
    expected_session_version: int
    allowed_classifications: tuple[DataClassification, ...]
    max_content_bytes: int = REPLAY_EXPORT_MAX_CONTENT_BYTES
    contract_version: str = DURABLE_REPLAY_EXPORT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != DURABLE_REPLAY_EXPORT_CONTRACT_VERSION
            or not _is_identifier(self.session_id)
            or type(self.expected_session_version) is not int
            or self.expected_session_version < 1
            or type(self.allowed_classifications) is not tuple
            or not self.allowed_classifications
            or len(self.allowed_classifications) > 4
            or any(
                type(classification) is not str
                or classification
                not in {
                    "public",
                    "internal",
                    "confidential",
                    "restricted",
                }
                for classification in self.allowed_classifications
            )
            or len(set(self.allowed_classifications))
            != len(self.allowed_classifications)
            or type(self.max_content_bytes) is not int
            or self.max_content_bytes < 1
            or self.max_content_bytes > REPLAY_EXPORT_MAX_CONTENT_BYTES
        ):
            _invalid("durable replay export request is invalid")


@dataclass(frozen=True)
class DurableReplayExportResult:
    session: GateSession
    bundle: ReplayBundleExport
    read_authorization_event_id: str
    retrieval_authorization_event_id: str


class AuthenticatedDurableAgentMemory:
    """One authenticated facade over the complete durable Gate lifecycle."""

    def __init__(
        self,
        *,
        service_bundle: DurableServiceBundle | None = None,
        authorization_service: AuthenticatedRetrievalService | None = None,
        preparation_service: DurableRetrievalPreparationService | None = None,
        semantic_service: AuthenticatedSemanticGateSessionService | None = None,
        finalization_service: DurableFinalizationService | None = None,
        execution_service: DurableExecutionService | None = None,
    ) -> None:
        legacy_services = (
            authorization_service,
            preparation_service,
            semantic_service,
            finalization_service,
            execution_service,
        )
        if service_bundle is None:
            if (
                type(authorization_service)
                is not AuthenticatedRetrievalService
                or type(preparation_service)
                is not DurableRetrievalPreparationService
                or type(semantic_service)
                is not AuthenticatedSemanticGateSessionService
                or type(finalization_service) is not DurableFinalizationService
                or type(execution_service) is not DurableExecutionService
            ):
                raise TypeError(
                    "service_bundle or the complete legacy service set is required"
                )
            service_bundle = DurableServiceBundle.from_services(
                preparation_service=preparation_service,
                semantic_service=semantic_service,
                finalization_service=finalization_service,
                execution_service=execution_service,
            )
            if (
                service_bundle.authority_graph.authorization_service
                is not authorization_service
            ):
                raise DurableCompositionV3Error(
                    "TBM_DURABLE_AUTHORITY_GRAPH_MISMATCH",
                    "durable services do not share the configured authority graph",
                )
        elif (
            type(service_bundle) is not DurableServiceBundle
            or any(value is not None for value in legacy_services)
        ):
            raise TypeError(
                "service_bundle cannot be combined with legacy service arguments"
            )

        graph = service_bundle.authority_graph
        authorization_service = graph.authorization_service
        preparation_service = service_bundle.preparation_service
        semantic_service = service_bundle.semantic_service
        finalization_service = service_bundle.finalization_service
        execution_service = service_bundle.execution_service
        session_authority = graph.session_authority
        evidence_reader = graph.evidence_authority
        replay_export_reader = graph.replay_export_reader
        if not all(
            callable(getattr(session_authority, name, None))
            for name in ("get", "transition")
        ):
            raise TypeError(
                "GateSession authority must support current reads and transitions"
            )
        if not callable(getattr(evidence_reader, "load_snapshot", None)):
            raise TypeError("Gate evidence authority must support snapshot reads")
        if not all(
            callable(getattr(replay_export_reader, name, None))
            for name in (
                "load_manifest",
                "load_manifest_for_session",
                "load_injection",
                "load_artifact_descriptor",
                "load_artifact",
            )
        ):
            raise TypeError(
                "replay authority must support authorized bundle export reads"
            )

        self._service_bundle = service_bundle
        self._authorization_service = authorization_service
        self._preparation_service = preparation_service
        self._semantic_service = semantic_service
        self._finalization_service = finalization_service
        self._execution_service = execution_service
        self._session_authority: DurableAgentSessionAuthority = session_authority
        self._semantic_authority = graph.semantic_authority
        self._evidence_reader: DurableAgentEvidenceReader = evidence_reader
        self._replay_export_reader = cast(
            DurableAgentReplayExportReader,
            replay_export_reader,
        )

    @property
    def service_bundle(self) -> DurableServiceBundle:
        """Return the validated service bundle used by this facade."""

        return self._service_bundle

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

    def export_replay_bundle(
        self,
        context: AuthenticatedServiceContext,
        request: DurableReplayExportRequest,
    ) -> DurableReplayExportResult:
        """Authorize and export the exact bundle linked to one durable session."""

        if (
            type(context) is not AuthenticatedServiceContext
            or type(request) is not DurableReplayExportRequest
        ):
            _invalid("durable replay export input is invalid")
        session, retrieval_scope = self._recover_retrieval_scope(
            context,
            request.session_id,
        )
        self._verify_expected_session_version(
            session,
            request.expected_session_version,
        )
        return self._authorize_replay_read(
            context,
            lambda read_scope: self._export_replay_authorized(
                context,
                request,
                session,
                retrieval_scope,
                read_scope,
            ),
        )

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
            with bind_gate_session_event_context(
                self._session_authority,
                scope,
            ), bind_authority_event_context(
                self._semantic_authority,
                scope,
            ), bind_authority_event_context(
                self._finalization_service.replay_authority,
                scope,
            ):
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

    def _authorize_replay_read(
        self,
        context: AuthenticatedServiceContext,
        operation: Callable[[AuthorizedRetrievalScope], _Result],
    ) -> _Result:
        def verified_operation(scope: AuthorizedRetrievalScope) -> _Result:
            self._verify_replay_read_scope(context, scope)
            with bind_authority_event_context(
                self._replay_export_reader,
                scope,
            ):
                return operation(scope)

        try:
            authorized = self._authorization_service.authorize_permission(
                context,
                permission="artifact:read",
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
                        DurableFinalizationV3Error,
                    ),
                )
            ):
                raise cause
            raise
        return authorized.value

    def _export_replay_authorized(
        self,
        context: AuthenticatedServiceContext,
        request: DurableReplayExportRequest,
        expected_session: GateSession,
        expected_retrieval_scope: AuthorizedRetrievalScope,
        read_scope: AuthorizedRetrievalScope,
    ) -> DurableReplayExportResult:
        self._verify_replay_read_scope(context, read_scope)
        session, retrieval_scope = self._recover_retrieval_scope(
            context,
            request.session_id,
        )
        if (
            session != expected_session
            or retrieval_scope != expected_retrieval_scope
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SESSION_CHANGED",
                "GateSession changed before replay export",
            )
        self._verify_session_scope(session, read_scope)
        self._verify_expected_session_version(
            session,
            request.expected_session_version,
        )
        if (
            session.status
            not in {
                "finalized",
                "executing",
                "completed",
                "abandoned",
            }
            or session.decision_id is None
            or session.usage_decision_id is None
            or session.injection_artifact_id is None
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_REPLAY_UNAVAILABLE",
                "GateSession has no exportable replay bundle",
            )
        try:
            manifest = self._replay_export_reader.load_manifest_for_session(
                session.session_id,
                session.decision_id,
                session.usage_decision_id,
                session.injection_artifact_id,
            )
        except Exception as error:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_REPLAY_UNAVAILABLE",
                "durable replay manifest could not be resolved",
            ) from error
        if (
            type(manifest) is not DecisionReplayManifest
            or manifest.session_id != session.session_id
            or manifest.decision_id != session.decision_id
            or manifest.usage_decision_id != session.usage_decision_id
            or manifest.injection_artifact_id
            != session.injection_artifact_id
            or manifest.completeness != "complete"
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_REPLAY_LINKAGE_INVALID",
                "durable replay manifest linkage is invalid",
            )
        self._verify_replay_read_scope(context, read_scope)
        try:
            bundle = export_authorized_replay_bundle(
                self._replay_export_reader,
                manifest.manifest_sha256,
                allowed_classifications=frozenset(
                    request.allowed_classifications
                ),
                max_content_bytes=request.max_content_bytes,
            )
        except ReplayExportError as error:
            raise DurableAgentV3Error(error.code, str(error)) from None
        except Exception as error:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_REPLAY_UNAVAILABLE",
                "durable replay bundle could not be loaded",
            ) from error
        if bundle.manifest != manifest:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_REPLAY_LINKAGE_INVALID",
                "durable replay export manifest linkage is invalid",
            )
        self._verify_replay_read_scope(context, read_scope)
        retained_session, retained_scope = self._recover_retrieval_scope(
            context,
            request.session_id,
        )
        if (
            retained_session != expected_session
            or retained_scope != expected_retrieval_scope
        ):
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SESSION_CHANGED",
                "GateSession changed during replay export",
            )
        self._verify_session_scope(retained_session, read_scope)
        return DurableReplayExportResult(
            session=retained_session,
            bundle=bundle,
            read_authorization_event_id=read_scope.authorization_event_id,
            retrieval_authorization_event_id=(
                retained_scope.authorization_event_id
            ),
        )

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

    def _verify_replay_read_scope(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
    ) -> None:
        self._authorization_service.verify_authorized_scope(
            context,
            scope,
            permission="artifact:read",
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
    def _verify_expected_session_version(
        session: GateSession,
        expected_version: int,
    ) -> None:
        if session.version != expected_version:
            raise DurableAgentV3Error(
                "TBM_DURABLE_AGENT_SESSION_CHANGED",
                "GateSession does not match the expected replay revision",
            )

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
