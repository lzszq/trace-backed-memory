from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import NoReturn, Protocol

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError
from .gate_evaluation_v3 import (
    GATE_EVALUATION_MAX_DECISIONS,
    RecommendedInjection,
    SemanticGateAttempt,
    SemanticRisk,
    SystemGateEvaluation,
    build_semantic_gate_attempt,
    verify_semantic_gate_attempt,
    verify_semantic_gate_attempt_parent,
    verify_system_gate_evaluation,
)
from .policy import LLM_GATE_PROMPT_MAX_CHARS
from .replay_v3 import DataClassification, create_content_addressed_artifact
from .retrieval_v3 import RetrievalSnapshot
from .semantic_gate_artifact_v3 import (
    SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES,
    SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
    SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES,
    StoredSemanticGateArtifact,
    StoredSemanticGateAttemptArtifacts,
    create_semantic_gate_artifact_binding,
)


_IDENTIFIER_MAX_CHARS = 128
_TEXT_MAX_CHARS = 4_096
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SYSTEM_GATE_ID_RE = re.compile(r"^system_gate_sha256_[0-9a-f]{64}$")
_SEMANTIC_ATTEMPT_ID_RE = re.compile(r"^semantic_attempt_sha256_[0-9a-f]{64}$")
_MEMORY_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_PROVIDER_ERROR_CODES = frozenset(
    {
        "provider_authentication_failed",
        "provider_content_rejected",
        "provider_error",
        "provider_rate_limited",
        "provider_response_invalid",
        "provider_timeout",
        "provider_unavailable",
    }
)


class SemanticGateServiceV3Error(V3ContractError):
    """Stable, sanitized failure at the trusted Semantic Gate boundary."""


class SemanticGateEvidenceReader(Protocol):
    def load_snapshot(self, snapshot_id: str) -> RetrievalSnapshot: ...

    def load_evaluation(self, evaluation_id: str) -> SystemGateEvaluation: ...


class SemanticGateAttemptAuthority(Protocol):
    def load_attempt_chain(
        self, evaluation_id: str
    ) -> tuple[SemanticGateAttempt, ...]: ...

    def store_attempt_with_artifacts(
        self,
        attempt: SemanticGateAttempt,
        prompt: StoredSemanticGateArtifact,
        response: StoredSemanticGateArtifact | None,
    ) -> object: ...

    def load_attempt_with_artifacts(
        self, attempt_id: str
    ) -> StoredSemanticGateAttemptArtifacts: ...


@dataclass(frozen=True)
class AuthenticatedSemanticProviderContext:
    """Provider facts produced by a trusted transport authenticator."""

    provider_id: str
    authenticator_id: str
    credential_id: str

    def __post_init__(self) -> None:
        if any(
            not _is_identifier(value)
            for value in (
                self.provider_id,
                self.authenticator_id,
                self.credential_id,
            )
        ):
            _authentication_failed()


@dataclass(frozen=True)
class TrustedSemanticProvider:
    """Server-owned provider, credential, model, and endpoint registration."""

    provider_id: str
    authenticator_id: str
    credential_id: str
    model_id: str
    model_version: str
    endpoint_id: str

    def __post_init__(self) -> None:
        if any(
            not _is_identifier(value)
            for value in (
                self.provider_id,
                self.authenticator_id,
                self.credential_id,
                self.model_id,
                self.model_version,
                self.endpoint_id,
            )
        ):
            _invalid_configuration()


@dataclass(frozen=True)
class SemanticGateServiceConfiguration:
    """Server-owned prompt, generation, and artifact policy metadata."""

    prompt_template_id: str
    prompt_template_version: str
    generation_config_sha256: str
    response_media_type: str
    classification: DataClassification = "internal"
    redaction_policy_id: str | None = None

    def __post_init__(self) -> None:
        if not _is_identifier(self.prompt_template_id) or not _is_identifier(
            self.prompt_template_version
        ):
            _invalid_configuration()
        if (
            type(self.generation_config_sha256) is not str
            or _DIGEST_RE.fullmatch(self.generation_config_sha256) is None
        ):
            _invalid_configuration()
        if not _is_identifier(self.response_media_type):
            _invalid_configuration()
        if self.classification not in {"public", "internal"}:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_ENCRYPTION_REQUIRED",
                "the configured artifact authority cannot retain sensitive bytes",
            )
        if self.redaction_policy_id is not None and not _is_identifier(
            self.redaction_policy_id
        ):
            _invalid_configuration()


@dataclass(frozen=True)
class SemanticGateInvocationRequest:
    system_gate_evaluation_id: str
    prompt: bytes
    expected_previous_attempt_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.system_gate_evaluation_id) is not str
            or _SYSTEM_GATE_ID_RE.fullmatch(self.system_gate_evaluation_id) is None
            or type(self.prompt) is not bytes
            or not self.prompt
            or len(self.prompt) > SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES
        ):
            _invalid_request()
        try:
            prompt_text = self.prompt.decode("utf-8", errors="strict")
        except UnicodeError:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_REQUEST_INVALID",
                "semantic gate invocation request is invalid",
            ) from None
        if len(prompt_text) > LLM_GATE_PROMPT_MAX_CHARS:
            _invalid_request()
        if (
            self.expected_previous_attempt_id is not None
            and _SEMANTIC_ATTEMPT_ID_RE.fullmatch(self.expected_previous_attempt_id)
            is None
        ):
            _invalid_request()


@dataclass(frozen=True)
class SemanticProviderCall:
    """Bounded request passed to the authenticated provider adapter."""

    provider_id: str
    model_id: str
    model_version: str
    endpoint_id: str
    prompt: bytes


@dataclass(frozen=True)
class SemanticProviderResult:
    """Successful provider response before trusted provenance construction."""

    response: bytes
    provider_request_id: str
    decision_id: str
    final_allowed_revision_ids: tuple[str, ...]
    final_blocked_revision_ids: tuple[str, ...]
    reason: str
    risk: SemanticRisk
    recommended_injection: RecommendedInjection
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.response) is not bytes
            or not self.response
            or len(self.response) > SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES
        ):
            _invalid_provider_result()
        if not _is_identifier(self.provider_request_id) or not _is_identifier(
            self.decision_id
        ):
            _invalid_provider_result()
        if (
            type(self.final_allowed_revision_ids) is not tuple
            or type(self.final_blocked_revision_ids) is not tuple
            or len(self.final_allowed_revision_ids) > GATE_EVALUATION_MAX_DECISIONS
            or len(self.final_blocked_revision_ids) > GATE_EVALUATION_MAX_DECISIONS
            or any(
                type(value) is not str
                or _MEMORY_REVISION_ID_RE.fullmatch(value) is None
                for value in (
                    *self.final_allowed_revision_ids,
                    *self.final_blocked_revision_ids,
                )
            )
            or type(self.reason) is not str
            or not self.reason.strip()
            or len(self.reason) > _TEXT_MAX_CHARS
            or not _is_canonical_ids(self.final_allowed_revision_ids)
            or not _is_canonical_ids(self.final_blocked_revision_ids)
            or bool(
                set(self.final_allowed_revision_ids).intersection(
                    self.final_blocked_revision_ids
                )
            )
            or self.risk not in {"low", "medium", "high", "unknown"}
            or self.recommended_injection not in {"none", "summary", "full"}
        ):
            _invalid_provider_result()
        try:
            self.reason.encode("utf-8")
        except UnicodeError:
            _invalid_provider_result()
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and (
                type(value) is not int or value < 0 or value > 2_147_483_647
            ):
                _invalid_provider_result()


class SemanticProviderCallError(RuntimeError):
    """Sanitized provider failure suitable for durable provenance."""

    def __init__(
        self,
        error_code: str,
        *,
        provider_request_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        if error_code not in _PROVIDER_ERROR_CODES:
            raise ValueError("provider error code is not supported")
        if provider_request_id is not None and not _is_identifier(provider_request_id):
            raise ValueError("provider request ID must be a bounded identifier")
        for value in (input_tokens, output_tokens):
            if value is not None and (
                type(value) is not int or value < 0 or value > 2_147_483_647
            ):
                raise ValueError("provider token counts must be nonnegative integers")
        self.error_code = error_code
        self.provider_request_id = provider_request_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        super().__init__("semantic provider call failed")


@dataclass(frozen=True)
class SemanticGateServiceResult:
    artifacts: StoredSemanticGateAttemptArtifacts

    @property
    def attempt(self) -> SemanticGateAttempt:
        return self.artifacts.attempt


class SemanticProviderInvocationFailedError(SemanticGateServiceV3Error):
    """Provider failure was durably recorded without exposing raw details."""

    def __init__(self, result: SemanticGateServiceResult) -> None:
        self.result = result
        super().__init__(
            "TBM_SEMANTIC_SERVICE_PROVIDER_FAILED",
            "semantic provider failure was durably recorded",
        )


class AuthenticatedSemanticGateService:
    """Authenticate, time, construct, and atomically retain one model attempt."""

    def __init__(
        self,
        *,
        provider: TrustedSemanticProvider,
        configuration: SemanticGateServiceConfiguration,
        evidence_reader: SemanticGateEvidenceReader,
        authority: SemanticGateAttemptAuthority,
        clock: Callable[[], str],
    ) -> None:
        if type(provider) is not TrustedSemanticProvider:
            raise TypeError("provider must be exactly TrustedSemanticProvider")
        if type(configuration) is not SemanticGateServiceConfiguration:
            raise TypeError(
                "configuration must be exactly SemanticGateServiceConfiguration"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._provider = provider
        self._configuration = configuration
        self._evidence_reader = evidence_reader
        self._authority = authority
        self._clock = clock

    def invoke(
        self,
        context: AuthenticatedSemanticProviderContext,
        request: SemanticGateInvocationRequest,
        call_provider: Callable[[SemanticProviderCall], SemanticProviderResult],
    ) -> SemanticGateServiceResult:
        if type(context) is not AuthenticatedSemanticProviderContext:
            _authentication_failed()
        if type(request) is not SemanticGateInvocationRequest:
            _invalid_request()
        if not callable(call_provider):
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_CALLBACK_INVALID",
                "semantic provider callback is invalid",
            )
        self._authenticate(context)
        evaluation, snapshot = self._load_evidence(request.system_gate_evaluation_id)
        chain = self._load_chain(evaluation.evaluation_id)
        parent = None if not chain else chain[-1]
        parent_id = None if parent is None else parent.attempt_id
        if request.expected_previous_attempt_id != parent_id:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_CHAIN_CHANGED",
                "semantic gate attempt chain does not match the expected parent",
            )
        sequence = len(chain) + 1
        if sequence > GATE_EVALUATION_MAX_DECISIONS:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_CHAIN_FULL",
                "semantic gate attempt chain reached its sequence limit",
            )

        started_at = self._trusted_time("started_at")
        provider_call = SemanticProviderCall(
            provider_id=self._provider.provider_id,
            model_id=self._provider.model_id,
            model_version=self._provider.model_version,
            endpoint_id=self._provider.endpoint_id,
            prompt=request.prompt,
        )
        provider_result: SemanticProviderResult | None = None
        failure = SemanticProviderCallError("provider_error")
        try:
            returned = call_provider(provider_call)
            if type(returned) is not SemanticProviderResult:
                failure = SemanticProviderCallError("provider_response_invalid")
            else:
                provider_result = returned
        except SemanticProviderCallError as error:
            failure = error
        except Exception:
            pass
        finished_at = self._trusted_time("finished_at")
        latency_ms = self._latency_ms(started_at, finished_at)

        try:
            result = self._persist(
                request=request,
                evaluation=evaluation,
                snapshot=snapshot,
                parent=parent,
                sequence=sequence,
                previous_attempt_id=parent_id,
                started_at=started_at,
                finished_at=finished_at,
                latency_ms=latency_ms,
                provider_result=provider_result,
                failure=failure,
            )
        except SemanticGateServiceV3Error:
            raise
        except Exception:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_PROVIDER_RESULT_INVALID",
                "semantic provider returned an invalid result",
            ) from None
        if provider_result is None:
            raised = SemanticProviderInvocationFailedError(result)
            raise raised from None
        return result

    def _authenticate(self, context: AuthenticatedSemanticProviderContext) -> None:
        if (
            context.provider_id != self._provider.provider_id
            or context.authenticator_id != self._provider.authenticator_id
            or context.credential_id != self._provider.credential_id
        ):
            _authentication_failed()

    def _load_evidence(
        self, evaluation_id: str
    ) -> tuple[SystemGateEvaluation, RetrievalSnapshot]:
        try:
            evaluation = self._evidence_reader.load_evaluation(evaluation_id)
            if (
                type(evaluation) is not SystemGateEvaluation
                or evaluation.evaluation_id != evaluation_id
            ):
                raise ValueError("invalid evaluation receipt")
            snapshot = self._evidence_reader.load_snapshot(
                evaluation.retrieval_snapshot_id
            )
            verify_system_gate_evaluation(evaluation, snapshot)
            return evaluation, snapshot
        except Exception:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_EVIDENCE_INVALID",
                "semantic gate evidence is missing or invalid",
            ) from None

    def _load_chain(self, evaluation_id: str) -> tuple[SemanticGateAttempt, ...]:
        try:
            chain = self._authority.load_attempt_chain(evaluation_id)
        except Exception:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_CHAIN_READ_FAILED",
                "semantic gate attempt chain could not be read",
            ) from None
        if type(chain) is not tuple or any(
            type(attempt) is not SemanticGateAttempt for attempt in chain
        ):
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_CHAIN_READ_FAILED",
                "semantic gate authority returned an invalid attempt chain",
            )
        return chain

    def _persist(
        self,
        *,
        request: SemanticGateInvocationRequest,
        evaluation: SystemGateEvaluation,
        snapshot: RetrievalSnapshot,
        parent: SemanticGateAttempt | None,
        sequence: int,
        previous_attempt_id: str | None,
        started_at: str,
        finished_at: str,
        latency_ms: int,
        provider_result: SemanticProviderResult | None,
        failure: SemanticProviderCallError,
    ) -> SemanticGateServiceResult:
        prompt_descriptor = create_content_addressed_artifact(
            request.prompt,
            media_type=SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
            classification=self._configuration.classification,
            created_at=started_at,
            redaction_policy_id=self._configuration.redaction_policy_id,
        )
        response_descriptor = (
            None
            if provider_result is None
            else create_content_addressed_artifact(
                provider_result.response,
                media_type=self._configuration.response_media_type,
                classification=self._configuration.classification,
                created_at=finished_at,
                redaction_policy_id=self._configuration.redaction_policy_id,
            )
        )
        attempt = build_semantic_gate_attempt(
            session_id=evaluation.session_id,
            retrieval_snapshot_id=evaluation.retrieval_snapshot_id,
            system_gate_evaluation_id=evaluation.evaluation_id,
            sequence=sequence,
            previous_attempt_id=previous_attempt_id,
            provider_id=self._provider.provider_id,
            model_id=self._provider.model_id,
            model_version=self._provider.model_version,
            endpoint_id=self._provider.endpoint_id,
            prompt_template_id=self._configuration.prompt_template_id,
            prompt_template_version=self._configuration.prompt_template_version,
            prompt_artifact_sha256=prompt_descriptor.content_sha256,
            response_artifact_sha256=(
                None
                if response_descriptor is None
                else response_descriptor.content_sha256
            ),
            generation_config_sha256=self._configuration.generation_config_sha256,
            provider_request_id=(
                provider_result.provider_request_id
                if provider_result is not None
                else failure.provider_request_id
            ),
            status="succeeded" if provider_result is not None else "failed",
            decision_id=(
                None if provider_result is None else provider_result.decision_id
            ),
            final_allowed_revision_ids=(
                ()
                if provider_result is None
                else provider_result.final_allowed_revision_ids
            ),
            final_blocked_revision_ids=(
                ()
                if provider_result is None
                else provider_result.final_blocked_revision_ids
            ),
            reason=None if provider_result is None else provider_result.reason,
            risk=None if provider_result is None else provider_result.risk,
            recommended_injection=(
                None
                if provider_result is None
                else provider_result.recommended_injection
            ),
            error_code=None if provider_result is not None else failure.error_code,
            input_tokens=(
                provider_result.input_tokens
                if provider_result is not None
                else failure.input_tokens
            ),
            output_tokens=(
                provider_result.output_tokens
                if provider_result is not None
                else failure.output_tokens
            ),
            latency_ms=latency_ms,
            started_at=started_at,
            finished_at=finished_at,
        )
        verify_semantic_gate_attempt(attempt, evaluation, snapshot)
        verify_semantic_gate_attempt_parent(attempt, parent)
        prompt = StoredSemanticGateArtifact(
            create_semantic_gate_artifact_binding(
                attempt,
                request.prompt,
                artifact_role="prompt",
                media_type=SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
                classification=self._configuration.classification,
                created_at=started_at,
                redaction_policy_id=self._configuration.redaction_policy_id,
            ),
            request.prompt,
        )
        response = (
            None
            if provider_result is None
            else StoredSemanticGateArtifact(
                create_semantic_gate_artifact_binding(
                    attempt,
                    provider_result.response,
                    artifact_role="response",
                    media_type=self._configuration.response_media_type,
                    classification=self._configuration.classification,
                    created_at=finished_at,
                    redaction_policy_id=self._configuration.redaction_policy_id,
                ),
                provider_result.response,
            )
        )
        artifacts = StoredSemanticGateAttemptArtifacts(attempt, prompt, response)
        try:
            self._authority.store_attempt_with_artifacts(attempt, prompt, response)
            retained = self._authority.load_attempt_with_artifacts(attempt.attempt_id)
        except Exception:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_PERSISTENCE_FAILED",
                "semantic gate attempt could not be durably retained",
            ) from None
        if retained != artifacts:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_RECEIPT_INVALID",
                "semantic gate authority returned an invalid read-back receipt",
            )
        return SemanticGateServiceResult(retained)

    def _trusted_time(self, label: str) -> str:
        try:
            value = self._clock()
            if type(value) is not str:
                raise ValueError(f"{label} is not text")
            canonical = canonical_rfc3339(value)
            parse_rfc3339(canonical)
            return canonical
        except Exception:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_CLOCK_INVALID",
                "trusted semantic gate clock returned an invalid timestamp",
            ) from None

    @staticmethod
    def _latency_ms(started_at: str, finished_at: str) -> int:
        elapsed = parse_rfc3339(finished_at) - parse_rfc3339(started_at)
        milliseconds = int(elapsed.total_seconds() * 1000)
        if milliseconds < 0 or milliseconds > 2_147_483_647:
            raise SemanticGateServiceV3Error(
                "TBM_SEMANTIC_SERVICE_CLOCK_INVALID",
                "trusted semantic gate clock moved outside the latency bound",
            )
        return milliseconds


def _is_identifier(value: object) -> bool:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _IDENTIFIER_MAX_CHARS
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _is_canonical_ids(values: tuple[str, ...]) -> bool:
    return len(set(values)) == len(values) and values == tuple(sorted(values))


def _authentication_failed() -> NoReturn:
    raise SemanticGateServiceV3Error(
        "TBM_SEMANTIC_SERVICE_AUTHENTICATION_FAILED",
        "semantic provider authentication failed",
    )


def _invalid_configuration() -> NoReturn:
    raise SemanticGateServiceV3Error(
        "TBM_SEMANTIC_SERVICE_CONFIGURATION_INVALID",
        "semantic gate service configuration is invalid",
    )


def _invalid_request() -> NoReturn:
    raise SemanticGateServiceV3Error(
        "TBM_SEMANTIC_SERVICE_REQUEST_INVALID",
        "semantic gate invocation request is invalid",
    )


def _invalid_provider_result() -> NoReturn:
    raise SemanticGateServiceV3Error(
        "TBM_SEMANTIC_SERVICE_PROVIDER_RESULT_INVALID",
        "semantic provider returned an invalid result",
    )


__all__ = [
    "AuthenticatedSemanticGateService",
    "AuthenticatedSemanticProviderContext",
    "SemanticGateInvocationRequest",
    "SemanticGateServiceConfiguration",
    "SemanticGateServiceResult",
    "SemanticGateServiceV3Error",
    "SemanticProviderCall",
    "SemanticProviderCallError",
    "SemanticProviderInvocationFailedError",
    "SemanticProviderResult",
    "TrustedSemanticProvider",
]
