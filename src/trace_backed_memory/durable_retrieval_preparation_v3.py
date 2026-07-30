from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn, Protocol

from .contracts_v3 import canonical_sha256
from .gate_evidence_v3 import DurablePreparedGateEvidenceVerifier
from .gate_evaluation_v3 import SystemGateEvaluation
from .gate_service_v3 import (
    AuthenticatedGateServiceV3Error,
    AuthenticatedGateSessionService,
    AuthenticatedPreparedGateResult,
    GatePreparationRequest,
    GateSessionReplayError,
    GateSessionWriter,
    PreparedGateEvidence,
)
from .gate_session_v3 import GateSession
from .retrieval_preparation_v3 import (
    ActivatedRevisionRetrievalSource,
    AuthenticatedRetrievalPreparationService,
    PreparedRetrievalEvidence,
    RetrievalPreparationContext,
    RetrievalPreparationRequest,
    RetrievalPreparationV3Error,
    SemanticQueryVector,
)
from .retrieval_v3 import RetrievalMode, RetrievalSnapshot
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthorizedRetrievalScope,
)


DURABLE_RETRIEVAL_PREPARATION_CONTRACT_VERSION = "tbm.durable-retrieval-preparation.v3"
_FINGERPRINT_SESSION_ID = "durable_retrieval_fingerprint"


class DurableRetrievalPreparationV3Error(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GateEvidenceStoreReceipt(Protocol):
    snapshot_id: str
    snapshot_inserted: bool
    evaluation_id: str
    evaluation_inserted: bool


class GateEvidenceAuthority(Protocol):
    def store_bundle(
        self,
        snapshot: RetrievalSnapshot,
        evaluation: SystemGateEvaluation,
    ) -> GateEvidenceStoreReceipt: ...

    def load_snapshot(self, snapshot_id: str) -> RetrievalSnapshot: ...

    def load_evaluation(
        self,
        evaluation_id: str,
    ) -> SystemGateEvaluation: ...


@dataclass(frozen=True)
class DurableRetrievalPreparationRequest:
    request_id: str
    trace_id: str
    run_id: str
    context: RetrievalPreparationContext
    retrieval_mode: RetrievalMode
    retriever_id: str
    retriever_version: str
    top_k: int
    idempotency_key: str
    expires_in_seconds: int
    lease_seconds: int
    query: bytes | None = field(default=None, repr=False)
    semantic_query: SemanticQueryVector | None = field(
        default=None,
        repr=False,
    )
    contract_version: str = DURABLE_RETRIEVAL_PREPARATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != DURABLE_RETRIEVAL_PREPARATION_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        try:
            self.retrieval_request(_FINGERPRINT_SESSION_ID)
            self.gate_request()
        except DurableRetrievalPreparationV3Error:
            raise
        except (
            AuthenticatedGateServiceV3Error,
            RetrievalPreparationV3Error,
            ValueError,
        ) as error:
            raise DurableRetrievalPreparationV3Error(
                "TBM_DURABLE_RETRIEVAL_INVALID",
                "durable retrieval request is invalid",
            ) from error

    @property
    def request_fingerprint(self) -> str:
        request = self.retrieval_request(_FINGERPRINT_SESSION_ID)
        semantic_query = request.semantic_query
        return canonical_sha256(
            {
                "contract_version": self.contract_version,
                "request_id": request.request_id,
                "trace_id": request.trace_id,
                "run_id": request.run_id,
                "context": request.context.to_dict(),
                "retrieval_mode": request.retrieval_mode,
                "retriever_id": request.retriever_id,
                "retriever_version": request.retriever_version,
                "top_k": request.top_k,
                "query_sha256": request.query_sha256,
                "semantic_query": (
                    None
                    if semantic_query is None
                    else {
                        "provider_id": semantic_query.provider_id,
                        "provider_version": semantic_query.provider_version,
                        "vector": list(semantic_query.vector),
                    }
                ),
                "expires_in_seconds": self.expires_in_seconds,
                "lease_seconds": self.lease_seconds,
            }
        )

    def gate_request(self) -> GatePreparationRequest:
        return GatePreparationRequest(
            trace_id=self.trace_id,
            run_id=self.run_id,
            request_fingerprint=self.request_fingerprint,
            idempotency_key=self.idempotency_key,
            expires_in_seconds=self.expires_in_seconds,
            lease_seconds=self.lease_seconds,
        )

    def retrieval_request(self, session_id: str) -> RetrievalPreparationRequest:
        return RetrievalPreparationRequest(
            session_id=session_id,
            request_id=self.request_id,
            trace_id=self.trace_id,
            run_id=self.run_id,
            context=self.context,
            retrieval_mode=self.retrieval_mode,
            retriever_id=self.retriever_id,
            retriever_version=self.retriever_version,
            top_k=self.top_k,
            query=self.query,
            semantic_query=self.semantic_query,
        )


class DurableRetrievalPreparationService:
    """Attach authorized retrieval/System Gate evidence to a durable GateSession."""

    def __init__(
        self,
        *,
        gate_session_service: AuthenticatedGateSessionService,
        retrieval_service: AuthenticatedRetrievalPreparationService,
        evidence_authority: GateEvidenceAuthority,
    ) -> None:
        if type(gate_session_service) is not AuthenticatedGateSessionService:
            raise TypeError(
                "gate_session_service must be AuthenticatedGateSessionService"
            )
        if type(retrieval_service) is not AuthenticatedRetrievalPreparationService:
            raise TypeError(
                "retrieval_service must be AuthenticatedRetrievalPreparationService"
            )
        if (
            gate_session_service.authorization_service
            is not retrieval_service.authorization_service
        ):
            raise TypeError(
                "gate and retrieval services must share one authorization service"
            )
        if not all(
            callable(getattr(evidence_authority, name, None))
            for name in ("store_bundle", "load_snapshot", "load_evaluation")
        ):
            raise TypeError("evidence_authority must satisfy GateEvidenceAuthority")
        self._gate_session_service = gate_session_service
        self._retrieval_service = retrieval_service
        self._evidence_authority = evidence_authority
        self._evidence_verifier = DurablePreparedGateEvidenceVerifier(
            evidence_authority
        )

    @property
    def authorization_service(self) -> AuthenticatedRetrievalService:
        """Return the shared authorization service."""

        return self._gate_session_service.authorization_service

    @property
    def session_authority(self) -> GateSessionWriter:
        """Return the shared durable GateSession authority."""

        return self._gate_session_service.session_authority

    @property
    def evidence_authority(self) -> GateEvidenceAuthority:
        """Return the exact retrieval/System-Gate evidence authority."""

        return self._evidence_authority

    @property
    def revision_source(self) -> ActivatedRevisionRetrievalSource:
        """Return the exact activated-revision source."""

        return self._retrieval_service.revision_source

    def prepare(
        self,
        context: AuthenticatedServiceContext,
        request: DurableRetrievalPreparationRequest,
    ) -> AuthenticatedPreparedGateResult[PreparedRetrievalEvidence]:
        if type(context) is not AuthenticatedServiceContext:
            _invalid("authenticated service context is invalid")
        if type(request) is not DurableRetrievalPreparationRequest:
            _invalid("durable retrieval request is invalid")

        finder = getattr(
            self._gate_session_service.session_authority,
            "find_by_idempotency",
            None,
        )
        if callable(finder):
            try:
                existing = finder(
                    tenant_id=context.tenant_id,
                    repository_id=request.context.repository_id,
                    principal_id=context.principal.principal_id,
                    agent_client_id=context.agent_client.agent_client_id,
                    idempotency_key=request.idempotency_key,
                )
            except Exception as error:
                raise DurableRetrievalPreparationV3Error(
                    "TBM_DURABLE_RETRIEVAL_REPLAY_LOOKUP_FAILED",
                    "durable retrieval replay lookup failed",
                ) from error
            if existing is not None:
                return self._recover_exact_replay(
                    context,
                    request,
                    existing,
                )

        def prepare_authorized(
            scope: AuthorizedRetrievalScope,
            session: GateSession,
        ) -> PreparedGateEvidence[PreparedRetrievalEvidence]:
            retrieval_request = request.retrieval_request(session.session_id)
            evidence = self._retrieval_service.prepare_for_authorized_scope(
                context,
                scope,
                retrieval_request,
            )
            prepared = PreparedGateEvidence(
                retrieval_snapshot_id=evidence.snapshot.snapshot_id,
                system_gate_evaluation_id=(
                    evidence.system_gate_evaluation.evaluation_id
                ),
                value=evidence,
            )
            try:
                receipt = self._evidence_authority.store_bundle(
                    evidence.snapshot,
                    evidence.system_gate_evaluation,
                )
            except Exception as error:
                raise DurableRetrievalPreparationV3Error(
                    "TBM_DURABLE_RETRIEVAL_EVIDENCE_STORE_FAILED",
                    "durable retrieval evidence could not be stored",
                ) from error
            self._verify_store_receipt(receipt, prepared)
            self._evidence_verifier(scope, session, prepared)
            return prepared

        try:
            return self._gate_session_service.prepare(
                context,
                request.gate_request(),
                prepare_authorized,
            )
        except GateSessionReplayError as error:
            return self._recover_exact_replay(
                context,
                request,
                error.session,
            )

    def _recover_exact_replay(
        self,
        context: AuthenticatedServiceContext,
        request: DurableRetrievalPreparationRequest,
        session: GateSession,
    ) -> AuthenticatedPreparedGateResult[PreparedRetrievalEvidence]:
        if (
            session.status != "prepared"
            or session.request_fingerprint != request.request_fingerprint
            or session.idempotency_key != request.idempotency_key
            or session.retrieval_snapshot_id is None
            or session.system_gate_evaluation_id is None
        ):
            raise GateSessionReplayError(session)
        try:
            snapshot = self._evidence_authority.load_snapshot(
                session.retrieval_snapshot_id
            )
            evaluation = self._evidence_authority.load_evaluation(
                session.system_gate_evaluation_id
            )
            scope = self.authorization_service.recover_authorized_scope(
                context,
                snapshot.authorization_event_id,
                permission="memory:retrieve",
            )
            decision = self.authorization_service.verify_authorized_scope(
                context,
                scope,
                permission="memory:retrieve",
            )
            evidence = self._retrieval_service.recover_persisted_evidence(
                context,
                scope,
                snapshot,
                evaluation,
            )
            prepared = PreparedGateEvidence(
                retrieval_snapshot_id=snapshot.snapshot_id,
                system_gate_evaluation_id=evaluation.evaluation_id,
                value=evidence,
            )
            self._evidence_verifier(scope, session, prepared)
        except Exception as replay_error:
            raise DurableRetrievalPreparationV3Error(
                "TBM_DURABLE_RETRIEVAL_REPLAY_INVALID",
                "durable retrieval replay could not be verified",
            ) from replay_error
        return AuthenticatedPreparedGateResult(
            authorization=decision,
            scope=scope,
            session=session,
            value=evidence,
        )

    @staticmethod
    def _verify_store_receipt(
        receipt: GateEvidenceStoreReceipt,
        evidence: PreparedGateEvidence[PreparedRetrievalEvidence],
    ) -> None:
        if (
            getattr(receipt, "snapshot_id", None) != evidence.retrieval_snapshot_id
            or type(getattr(receipt, "snapshot_inserted", None)) is not bool
            or getattr(receipt, "evaluation_id", None)
            != evidence.system_gate_evaluation_id
            or type(getattr(receipt, "evaluation_inserted", None)) is not bool
        ):
            raise DurableRetrievalPreparationV3Error(
                "TBM_DURABLE_RETRIEVAL_EVIDENCE_RECEIPT_INVALID",
                "durable retrieval evidence receipt is invalid",
            )


def _invalid(message: str) -> NoReturn:
    raise DurableRetrievalPreparationV3Error(
        "TBM_DURABLE_RETRIEVAL_INVALID",
        message,
    )


__all__ = [
    "DURABLE_RETRIEVAL_PREPARATION_CONTRACT_VERSION",
    "DurableRetrievalPreparationRequest",
    "DurableRetrievalPreparationService",
    "DurableRetrievalPreparationV3Error",
    "GateEvidenceAuthority",
    "GateEvidenceStoreReceipt",
]
