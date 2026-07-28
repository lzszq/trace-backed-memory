from __future__ import annotations

import re
from typing import Protocol

from .gate_evaluation_v3 import (
    SystemGateEvaluation,
    verify_system_gate_evaluation,
)
from .gate_service_v3 import PreparedGateEvidence
from .gate_session_v3 import GateSession
from .retrieval_v3 import RetrievalSnapshot
from .service_v3 import AuthorizedRetrievalScope


_SNAPSHOT_ID_RE = re.compile(r"^retrieval_snapshot_sha256_[0-9a-f]{64}$")
_SYSTEM_ID_RE = re.compile(r"^system_gate_sha256_[0-9a-f]{64}$")


class GateEvidenceV3VerificationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GateEvidenceV3Reader(Protocol):
    def load_snapshot(self, snapshot_id: str) -> RetrievalSnapshot: ...

    def load_evaluation(
        self,
        evaluation_id: str,
    ) -> SystemGateEvaluation: ...


class DurablePreparedGateEvidenceVerifier:
    """Verify exact durable evidence before GateSession reaches PREPARED."""

    def __init__(self, reader: GateEvidenceV3Reader) -> None:
        self._reader = reader

    def __call__(
        self,
        scope: AuthorizedRetrievalScope,
        session: GateSession,
        evidence: PreparedGateEvidence[object],
    ) -> None:
        if (
            type(scope) is not AuthorizedRetrievalScope
            or type(session) is not GateSession
            or type(evidence) is not PreparedGateEvidence
        ):
            _invalid("prepared Gate evidence verification input is invalid")
        if (
            type(evidence.retrieval_snapshot_id) is not str
            or _SNAPSHOT_ID_RE.fullmatch(evidence.retrieval_snapshot_id)
            is None
            or type(evidence.system_gate_evaluation_id) is not str
            or _SYSTEM_ID_RE.fullmatch(evidence.system_gate_evaluation_id)
            is None
        ):
            _invalid("prepared Gate evidence identifiers are invalid")
        try:
            snapshot = self._reader.load_snapshot(
                evidence.retrieval_snapshot_id
            )
            evaluation = self._reader.load_evaluation(
                evidence.system_gate_evaluation_id
            )
        except GateEvidenceV3VerificationError:
            raise
        except Exception as error:
            raise GateEvidenceV3VerificationError(
                "TBM_GATE_EVIDENCE_UNAVAILABLE",
                "durable prepared Gate evidence is unavailable",
            ) from error
        if (
            type(snapshot) is not RetrievalSnapshot
            or type(evaluation) is not SystemGateEvaluation
            or snapshot.snapshot_id != evidence.retrieval_snapshot_id
            or evaluation.evaluation_id
            != evidence.system_gate_evaluation_id
        ):
            _invalid("durable evidence authority returned different records")
        try:
            verify_system_gate_evaluation(evaluation, snapshot)
        except ValueError as error:
            raise GateEvidenceV3VerificationError(
                "TBM_GATE_EVIDENCE_LINKAGE_INVALID",
                "retrieval and System Gate evidence linkage is invalid",
            ) from error
        if (
            snapshot.session_id != session.session_id
            or evaluation.session_id != session.session_id
            or snapshot.trace_id != session.trace_id
            or snapshot.run_id != session.run_id
            or snapshot.authorization_event_id
            != scope.authorization_event_id
            or evaluation.authorization_event_id
            != scope.authorization_event_id
            or session.tenant_id != scope.tenant_id
            or session.repository_id != scope.repository_id
            or session.principal_id != scope.principal_id
            or session.agent_client_id != scope.agent_client_id
        ):
            raise GateEvidenceV3VerificationError(
                "TBM_GATE_EVIDENCE_SCOPE_MISMATCH",
                "durable prepared Gate evidence does not match authorized scope",
            )


def _invalid(message: str) -> None:
    raise GateEvidenceV3VerificationError(
        "TBM_GATE_EVIDENCE_INVALID",
        message,
    )
