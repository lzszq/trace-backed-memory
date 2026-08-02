from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Generic, Never, Protocol, TypeVar

from .authorization_v3 import AuthorizationDecision
from ._timestamps import parse_rfc3339
from .event_v1 import EventTrustedContext
from .gate_session_v3 import GateSession
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizedRetrievalScope,
)


_PreparedValue = TypeVar("_PreparedValue")


class AuthenticatedGateServiceV3Error(AuthenticatedServiceV3Error):
    """Stable failure while creating an authenticated durable GateSession."""


class GateSessionCreateReceipt(Protocol):
    session: GateSession
    inserted: bool


class GateSessionWriter(Protocol):
    def create_or_get(
        self,
        *,
        session_id: str,
        tenant_id: str,
        repository_id: str,
        principal_id: str,
        agent_client_id: str,
        trace_id: str,
        run_id: str,
        request_fingerprint: str,
        idempotency_key: str,
        expires_in_seconds: int,
    ) -> GateSessionCreateReceipt: ...

    def get(self, session_id: str) -> GateSession: ...

    def find_by_idempotency(
        self,
        *,
        tenant_id: str,
        repository_id: str,
        principal_id: str,
        agent_client_id: str,
        idempotency_key: str,
    ) -> GateSession | None: ...

    def renew_lease(
        self,
        session_id: str,
        *,
        expected_version: int,
        lease_seconds: int,
    ) -> GateSession: ...

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


@contextmanager
def bind_authority_event_context(
    authority: object,
    scope: AuthorizedRetrievalScope,
) -> Iterator[None]:
    if type(scope) is not AuthorizedRetrievalScope:
        raise TypeError("scope must be exactly AuthorizedRetrievalScope")
    binder = getattr(authority, "bind_event_context", None)
    if not callable(binder):
        yield
        return
    trusted_context = EventTrustedContext(
        organization_id=scope.organization_id,
        tenant_id=scope.tenant_id,
        repository_id=scope.repository_id,
        environment_id=scope.environment_id,
        principal_id=scope.principal_id,
        agent_client_id=scope.agent_client_id,
        actor_type="agent_client",
        actor_id=scope.agent_client_id,
        authorization_decision_id=scope.authorization_event_id,
    )
    with binder(trusted_context):
        yield


@contextmanager
def bind_gate_session_event_context(
    session_writer: GateSessionWriter,
    scope: AuthorizedRetrievalScope,
) -> Iterator[None]:
    with bind_authority_event_context(session_writer, scope):
        yield


@dataclass(frozen=True)
class GatePreparationRequest:
    trace_id: str
    run_id: str
    request_fingerprint: str
    idempotency_key: str
    expires_in_seconds: int
    lease_seconds: int

    def __post_init__(self) -> None:
        for value in (
            self.trace_id,
            self.run_id,
            self.request_fingerprint,
            self.idempotency_key,
        ):
            if type(value) is not str or not value:
                _invalid_request()
        for value in (self.expires_in_seconds, self.lease_seconds):
            if type(value) is not int or value <= 0:
                _invalid_request()


@dataclass(frozen=True)
class PreparedGateEvidence(Generic[_PreparedValue]):
    retrieval_snapshot_id: str
    system_gate_evaluation_id: str
    value: _PreparedValue

    def __post_init__(self) -> None:
        if (
            type(self.retrieval_snapshot_id) is not str
            or not self.retrieval_snapshot_id
            or type(self.system_gate_evaluation_id) is not str
            or not self.system_gate_evaluation_id
        ):
            raise AuthenticatedGateServiceV3Error(
                "TBM_GATE_SERVICE_EVIDENCE_INVALID",
                "prepared gate evidence is invalid",
            )


@dataclass(frozen=True)
class AuthenticatedPreparedGateResult(Generic[_PreparedValue]):
    authorization: AuthorizationDecision
    scope: AuthorizedRetrievalScope
    session: GateSession
    value: _PreparedValue


class GateSessionReplayError(AuthenticatedGateServiceV3Error):
    """Exact idempotent session already exists; retrieval was not repeated."""

    def __init__(self, session: GateSession) -> None:
        self.session = session
        super().__init__(
            "TBM_GATE_SERVICE_SESSION_EXISTS",
            "an exact durable gate session already exists",
        )


class GatePreparationFailedError(AuthenticatedGateServiceV3Error):
    """Preparation failed and the newly created session was canceled."""

    def __init__(self, session: GateSession) -> None:
        self.session = session
        super().__init__(
            "TBM_GATE_SERVICE_PREPARATION_FAILED",
            "gate preparation failed and the durable session was canceled",
        )


class GatePreparationRecoveryRequiredError(AuthenticatedGateServiceV3Error):
    """Preparation failed and durable state requires explicit recovery."""

    def __init__(self, session: GateSession) -> None:
        self.session = session
        super().__init__(
            "TBM_GATE_SERVICE_RECOVERY_REQUIRED",
            "gate preparation requires explicit durable-session recovery",
        )


class AuthenticatedGateSessionService:
    """Create and prepare a durable GateSession behind authorization."""

    def __init__(
        self,
        *,
        authorization_service: AuthenticatedRetrievalService,
        session_writer: GateSessionWriter,
        session_id_factory: Callable[[], str],
        evidence_verifier: Callable[
            [AuthorizedRetrievalScope, GateSession, PreparedGateEvidence[object]],
            None,
        ],
    ) -> None:
        if type(authorization_service) is not AuthenticatedRetrievalService:
            raise TypeError(
                "authorization_service must be AuthenticatedRetrievalService"
            )
        if not callable(session_id_factory):
            raise TypeError("session_id_factory must be callable")
        if not callable(evidence_verifier):
            raise TypeError("evidence_verifier must be callable")
        self._authorization_service = authorization_service
        self._session_writer = session_writer
        self._session_id_factory = session_id_factory
        self._evidence_verifier = evidence_verifier

    @property
    def authorization_service(self) -> AuthenticatedRetrievalService:
        """Return the exact authorization service bound to this composition."""

        return self._authorization_service

    @property
    def session_authority(self) -> GateSessionWriter:
        """Return the exact durable GateSession authority."""

        return self._session_writer

    def prepare(
        self,
        context: AuthenticatedServiceContext,
        request: GatePreparationRequest,
        prepare: Callable[
            [AuthorizedRetrievalScope, GateSession],
            PreparedGateEvidence[_PreparedValue],
        ],
    ) -> AuthenticatedPreparedGateResult[_PreparedValue]:
        if type(request) is not GatePreparationRequest:
            _invalid_request()
        if not callable(prepare):
            raise AuthenticatedGateServiceV3Error(
                "TBM_GATE_SERVICE_CALLBACK_INVALID",
                "gate preparation callback is invalid",
            )

        try:
            authorized = self._authorization_service.authorize_retrieval(
                context,
                lambda scope: self._prepare_authorized(
                    scope,
                    request,
                    prepare,
                ),
            )
        except AuthenticatedServiceV3Error as error:
            cause = error.__cause__
            if (
                error.code == "TBM_SERVICE_RETRIEVAL_FAILED"
                and isinstance(cause, AuthenticatedGateServiceV3Error)
            ):
                raise cause
            raise
        session, evidence = authorized.value
        return AuthenticatedPreparedGateResult(
            authorization=authorized.decision,
            scope=authorized.scope,
            session=session,
            value=evidence.value,
        )

    def _prepare_authorized(
        self,
        scope: AuthorizedRetrievalScope,
        request: GatePreparationRequest,
        prepare: Callable[
            [AuthorizedRetrievalScope, GateSession],
            PreparedGateEvidence[_PreparedValue],
        ],
    ) -> tuple[GateSession, PreparedGateEvidence[_PreparedValue]]:
        with bind_gate_session_event_context(self._session_writer, scope):
            return self._prepare_authorized_bound(scope, request, prepare)

    def _prepare_authorized_bound(
        self,
        scope: AuthorizedRetrievalScope,
        request: GatePreparationRequest,
        prepare: Callable[
            [AuthorizedRetrievalScope, GateSession],
            PreparedGateEvidence[_PreparedValue],
        ],
    ) -> tuple[GateSession, PreparedGateEvidence[_PreparedValue]]:
        try:
            session_id = self._session_id_factory()
            receipt = self._session_writer.create_or_get(
                session_id=session_id,
                tenant_id=scope.tenant_id,
                repository_id=scope.repository_id,
                principal_id=scope.principal_id,
                agent_client_id=scope.agent_client_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                request_fingerprint=request.request_fingerprint,
                idempotency_key=request.idempotency_key,
                expires_in_seconds=request.expires_in_seconds,
            )
            session = self._verify_create_receipt(
                receipt,
                scope,
                request,
                session_id,
            )
            if self._session_writer.get(session.session_id) != session:
                raise AuthenticatedGateServiceV3Error(
                    "TBM_GATE_SERVICE_SESSION_RECEIPT_INVALID",
                    "gate-session create receipt was not durably retained",
                )
        except AuthenticatedGateServiceV3Error:
            raise
        except Exception as error:
            raise AuthenticatedGateServiceV3Error(
                "TBM_GATE_SERVICE_SESSION_CREATE_FAILED",
                "durable gate session could not be created",
            ) from error
        if not receipt.inserted:
            raise GateSessionReplayError(session)

        try:
            evidence = prepare(scope, session)
            if type(evidence) is not PreparedGateEvidence:
                raise AuthenticatedGateServiceV3Error(
                    "TBM_GATE_SERVICE_EVIDENCE_INVALID",
                    "gate preparation returned invalid evidence",
                )
            self._evidence_verifier(
                scope,
                session,
                evidence,  # type: ignore[arg-type]
            )
            prepared = self._session_writer.transition(
                session.session_id,
                "prepared",
                expected_version=session.version,
                lease_seconds=request.lease_seconds,
                retrieval_snapshot_id=evidence.retrieval_snapshot_id,
                system_gate_evaluation_id=evidence.system_gate_evaluation_id,
            )
            self._verify_prepared(prepared, session, evidence)
            if self._session_writer.get(prepared.session_id) != prepared:
                raise AuthenticatedGateServiceV3Error(
                    "TBM_GATE_SERVICE_PREPARED_RECEIPT_INVALID",
                    "prepared GateSession receipt was not durably retained",
                )
            return prepared, evidence
        except Exception as error:
            self._raise_compensated_failure(session, error)

    @staticmethod
    def _verify_create_receipt(
        receipt: GateSessionCreateReceipt,
        scope: AuthorizedRetrievalScope,
        request: GatePreparationRequest,
        proposed_session_id: str,
    ) -> GateSession:
        session = getattr(receipt, "session", None)
        inserted = getattr(receipt, "inserted", None)
        if (
            type(session) is not GateSession
            or type(inserted) is not bool
            or session.tenant_id != scope.tenant_id
            or session.repository_id != scope.repository_id
            or session.principal_id != scope.principal_id
            or session.agent_client_id != scope.agent_client_id
            or session.trace_id != request.trace_id
            or session.run_id != request.run_id
            or session.request_fingerprint != request.request_fingerprint
            or session.idempotency_key != request.idempotency_key
            or (
                inserted
                and (
                    session.status != "created"
                    or session.version != 1
                    or session.session_id != proposed_session_id
                )
            )
        ):
            raise AuthenticatedGateServiceV3Error(
                "TBM_GATE_SERVICE_SESSION_RECEIPT_INVALID",
                "gate-session authority returned an invalid create receipt",
            )
        return session

    @staticmethod
    def _verify_prepared(
        prepared: GateSession,
        created: GateSession,
        evidence: PreparedGateEvidence[object],
    ) -> None:
        if (
            type(prepared) is not GateSession
            or prepared.session_id != created.session_id
            or prepared.version != created.version + 1
            or prepared.status != "prepared"
            or parse_rfc3339(prepared.updated_at)
            <= parse_rfc3339(created.updated_at)
            or prepared.retrieval_snapshot_id
            != evidence.retrieval_snapshot_id
            or prepared.system_gate_evaluation_id
            != evidence.system_gate_evaluation_id
            or replace(
                prepared,
                version=created.version,
                status=created.status,
                updated_at=created.updated_at,
                lease_expires_at=created.lease_expires_at,
                retrieval_snapshot_id=created.retrieval_snapshot_id,
                system_gate_evaluation_id=(
                    created.system_gate_evaluation_id
                ),
            )
            != created
        ):
            raise AuthenticatedGateServiceV3Error(
                "TBM_GATE_SERVICE_PREPARED_RECEIPT_INVALID",
                "gate-session authority returned an invalid prepared revision",
            )

    def _raise_compensated_failure(
        self,
        created: GateSession,
        error: Exception,
    ) -> Never:
        try:
            canceled = self._session_writer.transition(
                created.session_id,
                "canceled",
                expected_version=created.version,
                terminal_reason="prepare_failed",
            )
        except Exception:
            try:
                current = self._session_writer.get(created.session_id)
            except Exception:
                current = created
            raise GatePreparationRecoveryRequiredError(current) from error
        if (
            type(canceled) is not GateSession
            or canceled.session_id != created.session_id
            or canceled.version != created.version + 1
            or canceled.status != "canceled"
            or canceled.terminal_reason != "prepare_failed"
            or replace(
                canceled,
                version=created.version,
                status=created.status,
                updated_at=created.updated_at,
                terminal_reason=created.terminal_reason,
            )
            != created
        ):
            try:
                current = self._session_writer.get(created.session_id)
            except Exception:
                current = created
            raise GatePreparationRecoveryRequiredError(current) from error
        try:
            retained = self._session_writer.get(created.session_id)
        except Exception:
            retained = created
        if retained != canceled:
            raise GatePreparationRecoveryRequiredError(retained) from error
        raise GatePreparationFailedError(canceled) from error


def _invalid_request() -> None:
    raise AuthenticatedGateServiceV3Error(
        "TBM_GATE_SERVICE_REQUEST_INVALID",
        "gate preparation request is invalid",
    )
