from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, NoReturn, Protocol

from .authorization_v3 import AuthorizationPermission
from ._timestamps import parse_rfc3339
from .completion_outbox_v3 import (
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    verify_completion_outbox_event,
)
from .durable_finalization_v3 import (
    DurableFinalizationResult,
    DurableFinalizationV3Error,
)
from .gate_completion_v3 import (
    GateCompletionRequest,
    GateCompletionResult,
)
from .gate_service_v3 import GateSessionWriter
from .gate_session_v3 import GATE_SESSION_MAX_LEASE_SECONDS, GateSession
from .outcome_v3 import RunOutcome
from .replay_v3 import DecisionReplayManifest, InjectionArtifact
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizedRetrievalScope,
)
from .usage_decision_v3 import UsageDecision


DURABLE_EXECUTION_CONTRACT_VERSION = "tbm.durable-execution.v3"
OutcomeEvaluatorStatus = Literal["active", "disabled"]


class DurableExecutionV3Error(RuntimeError):
    """Stable, sanitized failure at the durable execution boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class FinalizedReplayReader(Protocol):
    def replay(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        session_id: str,
    ) -> DurableFinalizationResult: ...


class CompletionOutboxWrite(Protocol):
    completion: GateCompletionResult
    event: CompletionOutboxEvent
    delivery: CompletionOutboxDelivery
    event_inserted: bool


class CompletionOutboxAuthority(Protocol):
    @property
    def gate_sessions(self) -> GateSessionWriter: ...

    def complete_session(
        self,
        request: GateCompletionRequest,
    ) -> CompletionOutboxWrite: ...

    def get_event(self, event_id: str) -> CompletionOutboxEvent: ...

    def get_delivery(self, event_id: str) -> CompletionOutboxDelivery: ...


class OutcomeEvaluatorAuthenticator(Protocol):
    """Trusted boundary that authenticates the live evaluator transport."""

    def __call__(
        self,
        context: AuthenticatedOutcomeEvaluatorContext,
    ) -> TrustedOutcomeEvaluator: ...


@dataclass(frozen=True)
class TrustedOutcomeEvaluator:
    """Server-owned evaluator registration, never a caller assertion."""

    evaluator_id: str
    evaluator_version: str
    authenticator_id: str
    credential_id: str = field(repr=False)
    status: OutcomeEvaluatorStatus = "active"

    def __post_init__(self) -> None:
        for value in (
            self.evaluator_id,
            self.evaluator_version,
            self.authenticator_id,
            self.credential_id,
        ):
            _identifier(value, "outcome evaluator registration")
        if self.status not in {"active", "disabled"}:
            _invalid("outcome evaluator status is invalid")


@dataclass(frozen=True)
class AuthenticatedOutcomeEvaluatorContext:
    """Credential identity established by a trusted evaluator authenticator."""

    evaluator_id: str
    authenticator_id: str
    credential_id: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (
            self.evaluator_id,
            self.authenticator_id,
            self.credential_id,
        ):
            _identifier(value, "authenticated outcome evaluator context")


@dataclass(frozen=True)
class DurableExecutionStartRequest:
    session_id: str
    expected_session_version: int
    contract_version: str = DURABLE_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _request_revision(
            self.contract_version,
            self.session_id,
            self.expected_session_version,
        )


@dataclass(frozen=True)
class DurableExecutionResumeRequest:
    session_id: str
    expected_session_version: int
    lease_seconds: int = 1_800
    contract_version: str = DURABLE_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _request_identity(
            self.contract_version,
            self.session_id,
            self.expected_session_version,
            self.lease_seconds,
        )


@dataclass(frozen=True)
class DurableExecutionAbandonRequest:
    session_id: str
    expected_session_version: int
    reason: str
    contract_version: str = DURABLE_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != DURABLE_EXECUTION_CONTRACT_VERSION
            or not _is_identifier(self.session_id)
            or type(self.expected_session_version) is not int
            or self.expected_session_version < 1
            or type(self.reason) is not str
            or not self.reason
            or self.reason.strip() != self.reason
            or len(self.reason) > 512
        ):
            _invalid("durable execution abandonment request is invalid")


@dataclass(frozen=True)
class DurableExecutionStartResult:
    session: GateSession
    usage_decision: UsageDecision
    injection: InjectionArtifact
    manifest: DecisionReplayManifest
    transition_authorization_event_id: str
    snippet: str | None = field(repr=False)
    execution_required: bool
    replayed: bool


@dataclass(frozen=True)
class DurableExecutionAbandonResult:
    session: GateSession
    transition_authorization_event_id: str
    replayed: bool


@dataclass(frozen=True)
class DurableExecutionCompletionResult:
    session: GateSession
    outcome: RunOutcome
    event: CompletionOutboxEvent
    delivery: CompletionOutboxDelivery
    transition_authorization_event_id: str
    inserted: bool
    event_inserted: bool
    replayed: bool


class DurableExecutionService:
    """Authorize exact injection replay, execution, measurement, and emission."""

    def __init__(
        self,
        *,
        authorization_service: AuthenticatedRetrievalService,
        session_writer: GateSessionWriter,
        finalization_reader: FinalizedReplayReader,
        completion_authority: CompletionOutboxAuthority,
        evaluator_authenticator: OutcomeEvaluatorAuthenticator,
        clock: Callable[[], str],
    ) -> None:
        if type(authorization_service) is not AuthenticatedRetrievalService:
            raise TypeError(
                "authorization_service must be AuthenticatedRetrievalService"
            )
        if not all(
            callable(getattr(session_writer, name, None))
            for name in ("get", "renew_lease", "transition")
        ):
            raise TypeError("session_writer must satisfy GateSessionWriter")
        if not callable(getattr(finalization_reader, "replay", None)):
            raise TypeError("finalization_reader is invalid")
        if not all(
            callable(getattr(completion_authority, name, None))
            for name in ("complete_session", "get_event", "get_delivery")
        ):
            raise TypeError("completion_authority is invalid")
        if getattr(completion_authority, "gate_sessions", None) is not session_writer:
            raise TypeError(
                "completion_authority must share the GateSession authority"
            )
        if not callable(evaluator_authenticator):
            raise TypeError("evaluator_authenticator must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._authorization_service = authorization_service
        self._session_writer = session_writer
        self._finalization_reader = finalization_reader
        self._completion_authority = completion_authority
        self._evaluator_authenticator = evaluator_authenticator
        self._clock = clock

    def start(
        self,
        context: AuthenticatedServiceContext,
        retrieval_scope: AuthorizedRetrievalScope,
        transition_scope: AuthorizedRetrievalScope,
        request: DurableExecutionStartRequest,
    ) -> DurableExecutionStartResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(retrieval_scope) is not AuthorizedRetrievalScope
            or type(transition_scope) is not AuthorizedRetrievalScope
            or type(request) is not DurableExecutionStartRequest
        ):
            _invalid("durable execution start input is invalid")
        self._verify_scopes(context, retrieval_scope, transition_scope)
        finalized = self._replay_finalization(
            context,
            retrieval_scope,
            request.session_id,
        )
        current = finalized.session
        self._verify_session_scope(current, context, transition_scope)
        if current.status == "finalized":
            if current.version != request.expected_session_version:
                _changed("GateSession does not match the execution start revision")
            self._require_live_execution(current)
            executing = self._transition_executing(current)
            return self._start_result(
                finalized,
                executing,
                transition_scope,
                snippet=finalized.snippet,
                execution_required=True,
                replayed=False,
            )
        if (
            current.status == "executing"
            and current.version == request.expected_session_version + 1
        ):
            self._require_live_execution(current)
            return self._start_result(
                finalized,
                current,
                transition_scope,
                snippet=finalized.snippet,
                execution_required=True,
                replayed=True,
            )
        if (
            current.status in {"completed", "abandoned"}
            and current.version == request.expected_session_version + 2
        ):
            return self._start_result(
                finalized,
                current,
                transition_scope,
                snippet=None,
                execution_required=False,
                replayed=True,
            )
        _changed("GateSession execution start cannot be replayed exactly")

    def resume(
        self,
        context: AuthenticatedServiceContext,
        retrieval_scope: AuthorizedRetrievalScope,
        transition_scope: AuthorizedRetrievalScope,
        request: DurableExecutionResumeRequest,
    ) -> DurableExecutionStartResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(retrieval_scope) is not AuthorizedRetrievalScope
            or type(transition_scope) is not AuthorizedRetrievalScope
            or type(request) is not DurableExecutionResumeRequest
        ):
            _invalid("durable execution resume input is invalid")
        self._verify_scopes(context, retrieval_scope, transition_scope)
        finalized = self._replay_finalization(
            context,
            retrieval_scope,
            request.session_id,
        )
        current = finalized.session
        self._verify_session_scope(current, context, transition_scope)
        if current.status != "executing":
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_STATUS_INVALID",
                "GateSession is not executing",
            )
        if current.version != request.expected_session_version:
            _changed("GateSession does not match the execution resume revision")
        self._require_live_execution(current)
        try:
            renewed = self._session_writer.renew_lease(
                current.session_id,
                expected_version=current.version,
                lease_seconds=request.lease_seconds,
            )
        except Exception as error:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_RESUME_FAILED",
                "durable execution lease could not be renewed",
            ) from error
        self._verify_renewal(current, renewed)
        return self._start_result(
            finalized,
            renewed,
            transition_scope,
            snippet=finalized.snippet,
            execution_required=True,
            replayed=True,
        )

    def abandon(
        self,
        context: AuthenticatedServiceContext,
        transition_scope: AuthorizedRetrievalScope,
        request: DurableExecutionAbandonRequest,
    ) -> DurableExecutionAbandonResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(transition_scope) is not AuthorizedRetrievalScope
            or type(request) is not DurableExecutionAbandonRequest
        ):
            _invalid("durable execution abandonment input is invalid")
        self._verify_scope(
            context,
            transition_scope,
            permission="gate_session:transition",
        )
        current = self._load_session(request.session_id)
        self._verify_session_scope(current, context, transition_scope)
        if (
            current.status == "abandoned"
            and current.version == request.expected_session_version + 1
            and current.terminal_reason == request.reason
        ):
            return DurableExecutionAbandonResult(
                current,
                transition_scope.authorization_event_id,
                True,
            )
        if current.status != "executing":
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_STATUS_INVALID",
                "GateSession is not executing",
            )
        if current.version != request.expected_session_version:
            _changed("GateSession does not match the abandonment revision")
        self._require_live_execution(current)
        try:
            abandoned = self._session_writer.transition(
                current.session_id,
                "abandoned",
                expected_version=current.version,
                terminal_reason=request.reason,
            )
        except Exception as error:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_ABANDON_FAILED",
                "durable execution could not be abandoned",
            ) from error
        if (
            type(abandoned) is not GateSession
            or abandoned.status != "abandoned"
            or abandoned.version != current.version + 1
            or abandoned.terminal_reason != request.reason
            or self._load_session(current.session_id) != abandoned
        ):
            _receipt_invalid("abandonment")
        return DurableExecutionAbandonResult(
            abandoned,
            transition_scope.authorization_event_id,
            False,
        )

    def complete(
        self,
        context: AuthenticatedServiceContext,
        transition_scope: AuthorizedRetrievalScope,
        evaluator_context: AuthenticatedOutcomeEvaluatorContext,
        request: GateCompletionRequest,
    ) -> DurableExecutionCompletionResult:
        if (
            type(context) is not AuthenticatedServiceContext
            or type(transition_scope) is not AuthorizedRetrievalScope
            or type(evaluator_context) is not AuthenticatedOutcomeEvaluatorContext
            or type(request) is not GateCompletionRequest
        ):
            _invalid("durable execution completion input is invalid")
        self._verify_scope(
            context,
            transition_scope,
            permission="gate_session:transition",
        )
        current = self._load_session(request.session_id)
        self._verify_session_scope(current, context, transition_scope)
        self._authenticate_evaluator(evaluator_context, request)
        if current.status not in {"executing", "completed"}:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_STATUS_INVALID",
                "GateSession cannot be completed from its current status",
            )
        if current.status == "executing":
            if current.version != request.expected_version:
                _changed(
                    "GateSession does not match the execution completion revision"
                )
            self._require_live_execution(current)
        elif current.version != request.expected_version + 1:
            _changed(
                "GateSession does not match the completion replay parent"
            )
        try:
            write = self._completion_authority.complete_session(request)
        except Exception as error:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_COMPLETION_FAILED",
                "durable execution completion and outbox emission failed",
            ) from error
        result = replace(
            self._verify_completion_write(request, write),
            transition_authorization_event_id=(
                transition_scope.authorization_event_id
            ),
        )
        retained_session = self._load_session(request.session_id)
        try:
            retained_event = self._completion_authority.get_event(
                result.event.event_id
            )
            retained_delivery = self._completion_authority.get_delivery(
                result.event.event_id
            )
        except Exception as error:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_READBACK_FAILED",
                "completion outbox records could not be read back",
            ) from error
        if (
            retained_session != result.session
            or type(retained_event) is not CompletionOutboxEvent
            or retained_event != result.event
            or type(retained_delivery) is not CompletionOutboxDelivery
            or retained_delivery.event_id != result.event.event_id
            or retained_delivery.version < result.delivery.version
            or (
                retained_delivery.version == result.delivery.version
                and retained_delivery != result.delivery
            )
        ):
            _receipt_invalid("completion read-back")
        return replace(result, delivery=retained_delivery)

    def _transition_executing(
        self,
        finalized: GateSession,
    ) -> GateSession:
        try:
            executing = self._session_writer.transition(
                finalized.session_id,
                "executing",
                expected_version=finalized.version,
            )
        except Exception:
            current = self._load_session(finalized.session_id)
            if (
                current.status == "executing"
                and current.version == finalized.version + 1
            ):
                self._require_live_execution(current)
                return current
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_START_FAILED",
                "GateSession could not enter executing state",
            ) from None
        if (
            type(executing) is not GateSession
            or executing.status != "executing"
            or executing.version != finalized.version + 1
            or executing.session_id != finalized.session_id
            or executing.lease_expires_at is None
            or replace(
                executing,
                status=finalized.status,
                version=finalized.version,
                updated_at=finalized.updated_at,
                lease_expires_at=finalized.lease_expires_at,
            )
            != finalized
            or self._load_session(finalized.session_id) != executing
        ):
            _receipt_invalid("execution start")
        return executing

    def _verify_completion_write(
        self,
        request: GateCompletionRequest,
        write: CompletionOutboxWrite,
    ) -> DurableExecutionCompletionResult:
        completion = getattr(write, "completion", None)
        event = getattr(write, "event", None)
        delivery = getattr(write, "delivery", None)
        event_inserted = getattr(write, "event_inserted", None)
        if (
            type(completion) is not GateCompletionResult
            or type(event) is not CompletionOutboxEvent
            or type(delivery) is not CompletionOutboxDelivery
            or type(event_inserted) is not bool
            or type(completion.inserted) is not bool
            or event_inserted != completion.inserted
            or completion.session.session_id != request.session_id
            or completion.session.status != "completed"
            or (
                completion.inserted
                and completion.session.version != request.expected_version + 1
            )
            or not _outcome_matches_request(completion.outcome, request)
            or delivery.event_id != event.event_id
            or delivery.status not in {
                "pending",
                "leased",
                "retry_wait",
                "delivered",
                "dead_letter",
            }
        ):
            _receipt_invalid("completion")
        try:
            verify_completion_outbox_event(
                event,
                completion.outcome,
                completion.session,
            )
        except Exception:
            _receipt_invalid("completion outbox")
        return DurableExecutionCompletionResult(
            session=completion.session,
            outcome=completion.outcome,
            event=event,
            delivery=delivery,
            transition_authorization_event_id=(
                ""  # replaced by complete() after validation
            ),
            inserted=completion.inserted,
            event_inserted=event_inserted,
            replayed=not completion.inserted,
        )

    def _authenticate_evaluator(
        self,
        context: AuthenticatedOutcomeEvaluatorContext,
        request: GateCompletionRequest,
    ) -> None:
        try:
            trusted = self._evaluator_authenticator(context)
        except Exception:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_EVALUATOR_REJECTED",
                "outcome evaluator authentication was rejected",
            ) from None
        if (
            type(trusted) is not TrustedOutcomeEvaluator
            or trusted.status != "active"
            or trusted.evaluator_version != request.evaluator_version
            or trusted.evaluator_id != request.evaluator_id
            or trusted.evaluator_id != context.evaluator_id
            or trusted.authenticator_id != context.authenticator_id
            or trusted.credential_id != context.credential_id
        ):
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_EVALUATOR_REJECTED",
                "outcome evaluator authentication was rejected",
            )

    def _verify_scopes(
        self,
        context: AuthenticatedServiceContext,
        retrieval_scope: AuthorizedRetrievalScope,
        transition_scope: AuthorizedRetrievalScope,
    ) -> None:
        self._verify_scope(
            context,
            retrieval_scope,
            permission="memory:retrieve",
        )
        self._verify_scope(
            context,
            transition_scope,
            permission="gate_session:transition",
        )
        if (
            retrieval_scope.principal_id != transition_scope.principal_id
            or retrieval_scope.agent_client_id
            != transition_scope.agent_client_id
            or retrieval_scope.tenant_id != transition_scope.tenant_id
            or retrieval_scope.repository_id != transition_scope.repository_id
            or retrieval_scope.environment_id != transition_scope.environment_id
        ):
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_SCOPE_INVALID",
                "retrieval and transition scopes do not identify one owner",
            )

    def _verify_scope(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        *,
        permission: AuthorizationPermission,
    ) -> None:
        try:
            self._authorization_service.verify_authorized_scope(
                context,
                scope,
                permission=permission,
            )
        except AuthenticatedServiceV3Error as error:
            raise DurableExecutionV3Error(error.code, str(error)) from None
        except Exception:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_AUTHORIZATION_FAILED",
                "durable execution authorization could not be verified",
            ) from None

    def _replay_finalization(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        session_id: str,
    ) -> DurableFinalizationResult:
        try:
            result = self._finalization_reader.replay(
                context,
                scope,
                session_id,
            )
        except DurableFinalizationV3Error as error:
            raise DurableExecutionV3Error(error.code, str(error)) from None
        except Exception:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_REPLAY_FAILED",
                "finalized injection replay could not be verified",
            ) from None
        if (
            type(result) is not DurableFinalizationResult
            or result.session.session_id != session_id
            or not result.replayed
        ):
            _receipt_invalid("finalization replay")
        return result

    def _load_session(self, session_id: str) -> GateSession:
        try:
            session = self._session_writer.get(session_id)
        except Exception:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_SESSION_UNAVAILABLE",
                "GateSession is unavailable",
            ) from None
        if type(session) is not GateSession or session.session_id != session_id:
            _receipt_invalid("GateSession")
        return session

    @staticmethod
    def _verify_session_scope(
        session: GateSession,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
    ) -> None:
        if (
            session.tenant_id != scope.tenant_id
            or session.repository_id != scope.repository_id
            or session.principal_id != scope.principal_id
            or session.agent_client_id != scope.agent_client_id
            or scope.principal_id != context.principal.principal_id
            or scope.agent_client_id != context.agent_client.agent_client_id
            or scope.tenant_id != context.tenant_id
            or scope.environment_id != context.environment_id
        ):
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_SCOPE_INVALID",
                "GateSession is outside the authorized execution scope",
            )

    def _require_live_execution(self, session: GateSession) -> None:
        self._require_unexpired_session(session)
        lease = session.lease_expires_at
        if lease is None or self._trusted_now() >= parse_rfc3339(lease):
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_RECOVERY_REQUIRED",
                "execution lease expired and requires explicit recovery",
            )

    def _require_unexpired_session(self, session: GateSession) -> None:
        if self._trusted_now() >= parse_rfc3339(session.expires_at):
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_RECOVERY_REQUIRED",
                "GateSession expired and requires explicit recovery",
            )

    def _trusted_now(self) -> datetime:
        try:
            value = self._clock()
            if type(value) is not str:
                raise ValueError
            return parse_rfc3339(value)
        except Exception:
            raise DurableExecutionV3Error(
                "TBM_DURABLE_EXECUTION_CLOCK_INVALID",
                "trusted execution clock returned an invalid timestamp",
            ) from None

    def _verify_renewal(
        self,
        current: GateSession,
        renewed: GateSession,
    ) -> None:
        if (
            type(renewed) is not GateSession
            or renewed.status != "executing"
            or renewed.version != current.version + 1
            or renewed.session_id != current.session_id
            or renewed.lease_expires_at is None
            or replace(
                renewed,
                version=current.version,
                updated_at=current.updated_at,
                lease_expires_at=current.lease_expires_at,
            )
            != current
            or self._load_session(current.session_id) != renewed
        ):
            _receipt_invalid("execution lease renewal")

    @staticmethod
    def _start_result(
        finalized: DurableFinalizationResult,
        session: GateSession,
        scope: AuthorizedRetrievalScope,
        *,
        snippet: str | None,
        execution_required: bool,
        replayed: bool,
    ) -> DurableExecutionStartResult:
        return DurableExecutionStartResult(
            session=session,
            usage_decision=finalized.usage_decision,
            injection=finalized.injection,
            manifest=finalized.manifest,
            transition_authorization_event_id=scope.authorization_event_id,
            snippet=snippet,
            execution_required=execution_required,
            replayed=replayed,
        )


def _request_identity(
    contract_version: str,
    session_id: str,
    expected_session_version: int,
    lease_seconds: int,
) -> None:
    if (
        contract_version != DURABLE_EXECUTION_CONTRACT_VERSION
        or not _is_identifier(session_id)
        or type(expected_session_version) is not int
        or expected_session_version < 1
        or type(lease_seconds) is not int
        or lease_seconds < 1
        or lease_seconds > GATE_SESSION_MAX_LEASE_SECONDS
    ):
        _invalid("durable execution request is invalid")


def _request_revision(
    contract_version: str,
    session_id: str,
    expected_session_version: int,
) -> None:
    if (
        contract_version != DURABLE_EXECUTION_CONTRACT_VERSION
        or not _is_identifier(session_id)
        or type(expected_session_version) is not int
        or expected_session_version < 1
    ):
        _invalid("durable execution request is invalid")


def _identifier(value: object, label: str) -> None:
    if not _is_identifier(value):
        _invalid(f"{label} must contain bounded identifiers")


def _is_identifier(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.strip() == value
        and len(value) <= 128
    )


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


def _invalid(message: str) -> NoReturn:
    raise DurableExecutionV3Error(
        "TBM_DURABLE_EXECUTION_REQUEST_INVALID",
        message,
    )


def _changed(message: str) -> NoReturn:
    raise DurableExecutionV3Error(
        "TBM_DURABLE_EXECUTION_SESSION_CHANGED",
        message,
    )


def _receipt_invalid(label: str) -> NoReturn:
    raise DurableExecutionV3Error(
        "TBM_DURABLE_EXECUTION_RECEIPT_INVALID",
        f"{label} authority returned an invalid receipt",
    )


__all__ = [
    "DURABLE_EXECUTION_CONTRACT_VERSION",
    "AuthenticatedOutcomeEvaluatorContext",
    "CompletionOutboxAuthority",
    "CompletionOutboxWrite",
    "DurableExecutionAbandonRequest",
    "DurableExecutionAbandonResult",
    "DurableExecutionCompletionResult",
    "DurableExecutionResumeRequest",
    "DurableExecutionService",
    "DurableExecutionStartRequest",
    "DurableExecutionStartResult",
    "DurableExecutionV3Error",
    "FinalizedReplayReader",
    "OutcomeEvaluatorAuthenticator",
    "OutcomeEvaluatorStatus",
    "TrustedOutcomeEvaluator",
]
