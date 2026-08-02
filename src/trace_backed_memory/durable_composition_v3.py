from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from .durable_execution_v3 import (
    CompletionOutboxAuthority,
    DurableExecutionService,
)
from .durable_finalization_v3 import (
    DurableFinalizationService,
    FinalizationReplayAuthority,
)
from .durable_retrieval_preparation_v3 import (
    DurableRetrievalPreparationService,
    GateEvidenceAuthority,
)
from .durable_semantic_gate_v3 import (
    AuthenticatedSemanticGateSessionService,
)
from .gate_service_v3 import GateSessionWriter
from .retrieval_preparation_v3 import ActivatedRevisionRetrievalSource
from .semantic_gate_service_v3 import SemanticGateAttemptAuthority
from .service_v3 import AuthenticatedRetrievalService


DURABLE_COMPOSITION_CONTRACT_VERSION = "tbm.durable-composition.v3"


class DurableCompositionV3Error(RuntimeError):
    """Stable failure while validating the durable service graph."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DurableAuthorityGraph:
    """Explicit identity graph for one durable lifecycle composition."""

    authorization_service: AuthenticatedRetrievalService
    session_authority: GateSessionWriter
    evidence_authority: GateEvidenceAuthority
    semantic_authority: SemanticGateAttemptAuthority
    revision_source: ActivatedRevisionRetrievalSource
    replay_authority: FinalizationReplayAuthority
    completion_authority: CompletionOutboxAuthority
    replay_export_reader: object | None = None
    contract_version: str = DURABLE_COMPOSITION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != DURABLE_COMPOSITION_CONTRACT_VERSION
            or type(self.authorization_service)
            is not AuthenticatedRetrievalService
        ):
            _invalid()
        replay_export_reader = self.replay_export_reader
        if replay_export_reader is None:
            replay_export_reader = self.replay_authority
            object.__setattr__(
                self,
                "replay_export_reader",
                replay_export_reader,
            )
        required = (
            (
                self.session_authority,
                ("get", "renew_lease", "transition"),
            ),
            (
                self.evidence_authority,
                ("store_bundle", "load_snapshot", "load_evaluation"),
            ),
            (
                self.semantic_authority,
                (
                    "load_attempt_chain",
                    "load_attempt_with_artifacts",
                ),
            ),
            (
                self.revision_source,
                ("load_authorized", "verify_current"),
            ),
            (
                self.replay_authority,
                (
                    "store_complete_bundle",
                    "load_artifact",
                    "load_artifact_descriptor",
                    "load_injection",
                    "load_manifest",
                    "load_manifest_for_session",
                ),
            ),
            (
                replay_export_reader,
                (
                    "load_artifact",
                    "load_artifact_descriptor",
                    "load_injection",
                    "load_manifest",
                    "load_manifest_for_session",
                ),
            ),
            (
                self.completion_authority,
                ("complete_session", "get_event", "get_delivery"),
            ),
        )
        if any(
            not all(callable(getattr(value, name, None)) for name in names)
            for value, names in required
        ):
            _invalid()
        if (
            getattr(self.completion_authority, "gate_sessions", None)
            is not self.session_authority
        ):
            _mismatch()


@dataclass(frozen=True)
class DurableServiceBundle:
    """Validated services bound to one explicit authority graph."""

    authority_graph: DurableAuthorityGraph
    preparation_service: DurableRetrievalPreparationService
    semantic_service: AuthenticatedSemanticGateSessionService
    finalization_service: DurableFinalizationService
    execution_service: DurableExecutionService
    contract_version: str = DURABLE_COMPOSITION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != DURABLE_COMPOSITION_CONTRACT_VERSION
            or type(self.authority_graph) is not DurableAuthorityGraph
            or type(self.preparation_service)
            is not DurableRetrievalPreparationService
            or type(self.semantic_service)
            is not AuthenticatedSemanticGateSessionService
            or type(self.finalization_service) is not DurableFinalizationService
            or type(self.execution_service) is not DurableExecutionService
        ):
            _invalid()
        graph = self.authority_graph
        if (
            self.preparation_service.authorization_service
            is not graph.authorization_service
            or self.preparation_service.session_authority
            is not graph.session_authority
            or self.preparation_service.evidence_authority
            is not graph.evidence_authority
            or self.preparation_service.revision_source
            is not graph.revision_source
            or self.semantic_service.session_authority
            is not graph.session_authority
            or self.semantic_service.evidence_authority
            is not graph.evidence_authority
            or self.semantic_service.semantic_authority
            is not graph.semantic_authority
            or self.finalization_service.authorization_service
            is not graph.authorization_service
            or self.finalization_service.session_authority
            is not graph.session_authority
            or self.finalization_service.evidence_authority
            is not graph.evidence_authority
            or self.finalization_service.semantic_authority
            is not graph.semantic_authority
            or self.finalization_service.revision_source
            is not graph.revision_source
            or self.finalization_service.replay_authority
            is not graph.replay_authority
            or self.execution_service.authorization_service
            is not graph.authorization_service
            or self.execution_service.session_authority
            is not graph.session_authority
            or self.execution_service.finalization_reader
            is not self.finalization_service
            or self.execution_service.completion_authority
            is not graph.completion_authority
        ):
            _mismatch()

    @classmethod
    def from_services(
        cls,
        *,
        preparation_service: DurableRetrievalPreparationService,
        semantic_service: AuthenticatedSemanticGateSessionService,
        finalization_service: DurableFinalizationService,
        execution_service: DurableExecutionService,
    ) -> DurableServiceBundle:
        """Build and verify an explicit graph for compatibility callers."""

        if type(preparation_service) is not DurableRetrievalPreparationService:
            _invalid()
        graph = DurableAuthorityGraph(
            authorization_service=preparation_service.authorization_service,
            session_authority=preparation_service.session_authority,
            evidence_authority=preparation_service.evidence_authority,
            semantic_authority=semantic_service.semantic_authority,
            revision_source=preparation_service.revision_source,
            replay_authority=finalization_service.replay_authority,
            completion_authority=execution_service.completion_authority,
        )
        return cls(
            authority_graph=graph,
            preparation_service=preparation_service,
            semantic_service=semantic_service,
            finalization_service=finalization_service,
            execution_service=execution_service,
        )


def _invalid() -> NoReturn:
    raise DurableCompositionV3Error(
        "TBM_DURABLE_COMPOSITION_INVALID",
        "durable service composition is invalid",
    )


def _mismatch() -> NoReturn:
    raise DurableCompositionV3Error(
        "TBM_DURABLE_AUTHORITY_GRAPH_MISMATCH",
        "durable services do not share the configured authority graph",
    )


__all__ = [
    "DURABLE_COMPOSITION_CONTRACT_VERSION",
    "DurableAuthorityGraph",
    "DurableCompositionV3Error",
    "DurableServiceBundle",
]
