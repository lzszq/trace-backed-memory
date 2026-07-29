from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Generic, NoReturn, Protocol, TypeVar, cast

from .artifact_service_v3 import AuthorizedArtifactReadResult
from .contracts_v3 import V3ContractError, canonical_sha256
from .evidence_v3 import StructuredRegressionEvidence
from .fix_evidence_v3 import FixEvidence
from .memory_publication_v3 import (
    MemoryRevisionActivation,
    MemoryRevisionApproval,
    StoredMemoryRevisionActivationPublication,
    StoredMemoryRevisionApprovalPublication,
    verify_memory_revision_activation,
)
from .memory_revision_v3 import MemoryRevision
from .replay_v3 import verify_artifact_content
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizedRetrievalScope,
)


ACTIVATED_REVISION_CANDIDATE_VERSION = "tbm.activated-revision-candidate.v3"
_AUTHORIZATION_ID_RE = re.compile(r"authz_sha256_[0-9a-f]{64}")


class ActivatedRevisionV3Error(V3ContractError):
    """Stable, sanitized failure while resolving a current activation head."""


class RevisionProposalBundle(Protocol):
    revision: MemoryRevision
    fix_evidence: FixEvidence | None
    regression_evidence: tuple[StructuredRegressionEvidence, ...]


class RevisionProposalReader(Protocol):
    def load_proposal(self, revision_id: str) -> RevisionProposalBundle: ...


class PublicationHead(Protocol):
    tenant_id: str
    repository_id: str | None
    memory_id: str
    current_revision_number: int
    current_revision_id: str
    current_activation_id: str


class PublicationReader(Protocol):
    def load_head(
        self, *, tenant_id: str, repository_id: str | None, memory_id: str
    ) -> PublicationHead: ...

    def load_approval_bundle(
        self, approval_id: str
    ) -> StoredMemoryRevisionApprovalPublication: ...

    def load_activation_bundle(
        self, activation_id: str
    ) -> StoredMemoryRevisionActivationPublication: ...


class AuthorizedArtifactReader(Protocol):
    def get_with_receipt(
        self, context: AuthenticatedServiceContext, artifact_id: str
    ) -> AuthorizedArtifactReadResult: ...


@dataclass(frozen=True)
class ActivatedRevisionCandidate:
    revision: MemoryRevision
    approval: MemoryRevisionApproval
    activation: MemoryRevisionActivation
    content: bytes = field(repr=False)
    candidate_sha256: str = ""
    retrieval_authorization_event_id: str = ""
    artifact_authorization_event_id: str = ""
    approval_attestation_verified_by: str = ""
    activation_attestation_verified_by: str = ""
    contract_version: str = ACTIVATED_REVISION_CANDIDATE_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != ACTIVATED_REVISION_CANDIDATE_VERSION:
            _invalid("candidate contract_version is unsupported")
        if type(self.revision) is not MemoryRevision:
            _invalid("revision must be exactly MemoryRevision")
        if type(self.approval) is not MemoryRevisionApproval:
            _invalid("approval must be exactly MemoryRevisionApproval")
        if type(self.activation) is not MemoryRevisionActivation:
            _invalid("activation must be exactly MemoryRevisionActivation")
        _validate_publication_links(
            self.revision,
            self.approval,
            self.activation,
        )
        if type(self.content) is not bytes or not verify_artifact_content(
            self.revision.content_artifact, self.content
        ):
            _invalid("content does not match revision artifact")
        for value, name in (
            (
                self.retrieval_authorization_event_id,
                "retrieval_authorization_event_id",
            ),
            (
                self.artifact_authorization_event_id,
                "artifact_authorization_event_id",
            ),
        ):
            if type(value) is not str or _AUTHORIZATION_ID_RE.fullmatch(value) is None:
                _invalid(f"{name} must be an authorization event ID")
        for value, name in (
            (
                self.approval_attestation_verified_by,
                "approval_attestation_verified_by",
            ),
            (
                self.activation_attestation_verified_by,
                "activation_attestation_verified_by",
            ),
        ):
            if not _bounded_identifier(value):
                _invalid(f"{name} must be a bounded identifier")
        expected = activated_revision_candidate_sha256(
            self.revision,
            self.approval,
            self.activation,
            approval_attestation_verified_by=(
                self.approval_attestation_verified_by
            ),
            activation_attestation_verified_by=(
                self.activation_attestation_verified_by
            ),
        )
        if self.candidate_sha256 != expected:
            _invalid("candidate_sha256 does not match publication content")


def activated_revision_candidate_sha256(
    revision: MemoryRevision,
    approval: MemoryRevisionApproval,
    activation: MemoryRevisionActivation,
    *,
    approval_attestation_verified_by: str,
    activation_attestation_verified_by: str,
) -> str:
    if type(revision) is not MemoryRevision:
        _invalid("revision must be exactly MemoryRevision")
    if type(approval) is not MemoryRevisionApproval:
        _invalid("approval must be exactly MemoryRevisionApproval")
    if type(activation) is not MemoryRevisionActivation:
        _invalid("activation must be exactly MemoryRevisionActivation")
    _validate_publication_links(revision, approval, activation)
    if not _bounded_identifier(approval_attestation_verified_by):
        _invalid("approval_attestation_verified_by must be a bounded identifier")
    if not _bounded_identifier(activation_attestation_verified_by):
        _invalid("activation_attestation_verified_by must be a bounded identifier")
    return canonical_sha256(
        {
            "contract_version": ACTIVATED_REVISION_CANDIDATE_VERSION,
            "revision": revision.to_dict(),
            "approval": approval.to_dict(),
            "activation": activation.to_dict(),
            "approval_attestation_verified_by": (
                approval_attestation_verified_by
            ),
            "activation_attestation_verified_by": (
                activation_attestation_verified_by
            ),
        }
    )


_T = TypeVar("_T")


@dataclass(frozen=True)
class _SourceOutcome(Generic[_T]):
    value: _T | None = None
    error: ActivatedRevisionV3Error | None = None


class ActivatedRevisionSource:
    """Resolve and reverify one current published revision for retrieval."""

    def __init__(
        self,
        *,
        authorization_service: AuthenticatedRetrievalService,
        proposal_reader: RevisionProposalReader,
        publication_reader: PublicationReader,
        artifact_reader: AuthorizedArtifactReader,
        trusted_attestation_verifier_ids: tuple[str, ...],
    ) -> None:
        if type(authorization_service) is not AuthenticatedRetrievalService:
            raise TypeError(
                "authorization_service must be AuthenticatedRetrievalService"
            )
        if (
            type(trusted_attestation_verifier_ids) is not tuple
            or not trusted_attestation_verifier_ids
            or len(trusted_attestation_verifier_ids) > 32
            or len(set(trusted_attestation_verifier_ids))
            != len(trusted_attestation_verifier_ids)
            or any(
                not _bounded_identifier(item)
                for item in trusted_attestation_verifier_ids
            )
        ):
            raise ValueError(
                "trusted_attestation_verifier_ids must be unique bounded IDs"
            )
        for reader, methods in (
            (proposal_reader, ("load_proposal",)),
            (
                publication_reader,
                (
                    "load_head",
                    "load_approval_bundle",
                    "load_activation_bundle",
                ),
            ),
            (artifact_reader, ("get_with_receipt",)),
        ):
            if any(not callable(getattr(reader, method, None)) for method in methods):
                raise TypeError("activated revision reader is invalid")
        self._authorization_service = authorization_service
        self._proposal_reader = proposal_reader
        self._publication_reader = publication_reader
        self._artifact_reader = artifact_reader
        self._trusted_verifiers = frozenset(trusted_attestation_verifier_ids)

    def load_current(
        self,
        context: AuthenticatedServiceContext,
        *,
        memory_id: str,
        repository_id: str | None,
    ) -> ActivatedRevisionCandidate:
        if not _bounded_identifier(memory_id):
            _reject("TBM_ACTIVATED_REVISION_INPUT_INVALID", "memory_id is invalid")
        if (
            repository_id is not None
            and not _bounded_identifier(repository_id)
        ):
            _reject(
                "TBM_ACTIVATED_REVISION_INPUT_INVALID",
                "repository_id is invalid",
            )

        def resolve(
            scope: AuthorizedRetrievalScope,
        ) -> _SourceOutcome[ActivatedRevisionCandidate]:
            try:
                return _SourceOutcome(
                    value=self._resolve_authorized(
                        context,
                        scope,
                        memory_id=memory_id,
                        repository_id=repository_id,
                    )
                )
            except ActivatedRevisionV3Error as error:
                return _SourceOutcome(error=error)
            except Exception:
                return _SourceOutcome(
                    error=ActivatedRevisionV3Error(
                        "TBM_ACTIVATED_REVISION_READ_FAILED",
                        "activated revision could not be resolved",
                    )
                )

        try:
            authorized = self._authorization_service.authorize_retrieval(
                context,
                resolve,
            )
            candidate = self._unwrap(authorized.value)
            if (
                candidate.retrieval_authorization_event_id
                != authorized.decision.authorization_event_id
            ):
                _reject(
                    "TBM_ACTIVATED_REVISION_AUTHORIZATION_MISMATCH",
                    "retrieval authorization receipt does not match candidate",
                )
            return candidate
        except ActivatedRevisionV3Error:
            raise
        except AuthenticatedServiceV3Error as error:
            raise ActivatedRevisionV3Error(error.code, str(error)) from None

    def _resolve_authorized(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        *,
        memory_id: str,
        repository_id: str | None,
    ) -> ActivatedRevisionCandidate:
        if repository_id is not None and repository_id != scope.repository_id:
            _reject(
                "TBM_ACTIVATED_REVISION_SCOPE_REJECTED",
                "publication repository is outside authorized scope",
            )
        head = self._load_head(scope, memory_id, repository_id)
        activation_bundle = self._load_activation(head.current_activation_id)
        activation = activation_bundle.activation
        approval_bundle = self._load_approval(activation.approval_id)
        approval = approval_bundle.approval
        proposal = self._load_proposal(head.current_revision_id)
        revision = proposal.revision
        if (
            head.tenant_id != scope.tenant_id
            or head.repository_id != repository_id
            or head.memory_id != memory_id
            or head.current_revision_id != revision.revision_id
            or head.current_revision_number != revision.revision_number
            or head.current_activation_id != activation.activation_id
            or revision.scope.tenant_id != scope.tenant_id
            or revision.scope.repository_id != repository_id
        ):
            _reject(
                "TBM_ACTIVATED_REVISION_HEAD_MISMATCH",
                "publication head does not match revision scope",
            )
        self._require_trusted_verifier(
            approval_bundle.attestation_verified_by,
            role="approval",
        )
        self._require_trusted_verifier(
            activation_bundle.attestation_verified_by,
            role="activation",
        )
        previous_revision: MemoryRevision | None = None
        previous_activation: MemoryRevisionActivation | None = None
        if revision.revision_number > 1:
            if revision.previous_revision_id is None:
                _reject(
                    "TBM_ACTIVATED_REVISION_LINEAGE_INVALID",
                    "revision predecessor is missing",
                )
            previous_revision = self._load_proposal(
                revision.previous_revision_id
            ).revision
            if activation.previous_activation_id is None:
                _reject(
                    "TBM_ACTIVATED_REVISION_LINEAGE_INVALID",
                    "activation predecessor is missing",
                )
            previous_activation_bundle = self._load_activation(
                activation.previous_activation_id
            )
            self._require_trusted_verifier(
                previous_activation_bundle.attestation_verified_by,
                role="previous activation",
            )
            previous_activation = previous_activation_bundle.activation
        artifact_read = self._load_artifact(
            context,
            revision.content_artifact.artifact_id,
        )
        fix_evidence_by_id: dict[str, FixEvidence] = {}
        if proposal.fix_evidence is not None:
            fix_evidence_by_id[proposal.fix_evidence.evidence_id] = (
                proposal.fix_evidence
            )
        regression_evidence_by_id = {
            item.evidence_id: item for item in proposal.regression_evidence
        }
        try:
            verify_memory_revision_activation(
                activation,
                revision=revision,
                approval=approval,
                previous_revision=previous_revision,
                content=artifact_read.content,
                fix_evidence_by_id=fix_evidence_by_id,
                regression_evidence_by_id=regression_evidence_by_id,
                approval_policy=approval_bundle.policy,
                approval_request=approval_bundle.request,
                approval_decision=approval_bundle.decision,
                previous_activation=previous_activation,
                policy=activation_bundle.policy,
                request=activation_bundle.request,
                decision=activation_bundle.decision,
            )
        except ValueError:
            _reject(
                "TBM_ACTIVATED_REVISION_VERIFICATION_FAILED",
                "activated revision failed exact publication verification",
            )
        current = self._load_head(scope, memory_id, repository_id)
        if (
            current.current_activation_id != head.current_activation_id
            or current.current_revision_id != head.current_revision_id
            or current.current_revision_number != head.current_revision_number
        ):
            _reject(
                "TBM_ACTIVATED_REVISION_STALE",
                "publication head changed during activated revision read",
            )
        candidate_sha256 = activated_revision_candidate_sha256(
            revision,
            approval,
            activation,
            approval_attestation_verified_by=(
                approval_bundle.attestation_verified_by
            ),
            activation_attestation_verified_by=(
                activation_bundle.attestation_verified_by
            ),
        )
        return ActivatedRevisionCandidate(
            revision=revision,
            approval=approval,
            activation=activation,
            content=artifact_read.content,
            candidate_sha256=candidate_sha256,
            retrieval_authorization_event_id=scope.authorization_event_id,
            artifact_authorization_event_id=(
                artifact_read.authorization_event_id
            ),
            approval_attestation_verified_by=(
                approval_bundle.attestation_verified_by
            ),
            activation_attestation_verified_by=(
                activation_bundle.attestation_verified_by
            ),
        )

    def _load_head(
        self,
        scope: AuthorizedRetrievalScope,
        memory_id: str,
        repository_id: str | None,
    ) -> PublicationHead:
        try:
            return self._publication_reader.load_head(
                tenant_id=scope.tenant_id,
                repository_id=repository_id,
                memory_id=memory_id,
            )
        except Exception:
            _reject(
                "TBM_ACTIVATED_REVISION_PUBLICATION_READ_FAILED",
                "publication head could not be loaded",
            )

    def _load_proposal(self, revision_id: str) -> RevisionProposalBundle:
        try:
            proposal = self._proposal_reader.load_proposal(revision_id)
        except Exception:
            _reject(
                "TBM_ACTIVATED_REVISION_PROPOSAL_READ_FAILED",
                "memory revision proposal could not be loaded",
            )
        if (
            type(proposal.revision) is not MemoryRevision
            or (
                proposal.fix_evidence is not None
                and type(proposal.fix_evidence) is not FixEvidence
            )
            or type(proposal.regression_evidence) is not tuple
            or any(
                type(item) is not StructuredRegressionEvidence
                for item in proposal.regression_evidence
            )
        ):
            _reject(
                "TBM_ACTIVATED_REVISION_PROPOSAL_INVALID",
                "memory revision proposal reader returned invalid data",
            )
        return proposal

    def _load_approval(
        self, approval_id: str
    ) -> StoredMemoryRevisionApprovalPublication:
        try:
            bundle = self._publication_reader.load_approval_bundle(approval_id)
        except Exception:
            _reject(
                "TBM_ACTIVATED_REVISION_PUBLICATION_READ_FAILED",
                "approval publication could not be loaded",
            )
        if type(bundle) is not StoredMemoryRevisionApprovalPublication:
            _reject(
                "TBM_ACTIVATED_REVISION_PUBLICATION_INVALID",
                "approval publication reader returned invalid data",
            )
        return bundle

    def _load_activation(
        self, activation_id: str
    ) -> StoredMemoryRevisionActivationPublication:
        try:
            bundle = self._publication_reader.load_activation_bundle(
                activation_id
            )
        except Exception:
            _reject(
                "TBM_ACTIVATED_REVISION_PUBLICATION_READ_FAILED",
                "activation publication could not be loaded",
            )
        if type(bundle) is not StoredMemoryRevisionActivationPublication:
            _reject(
                "TBM_ACTIVATED_REVISION_PUBLICATION_INVALID",
                "activation publication reader returned invalid data",
            )
        return bundle

    def _load_artifact(
        self,
        context: AuthenticatedServiceContext,
        artifact_id: str,
    ) -> AuthorizedArtifactReadResult:
        try:
            result = self._artifact_reader.get_with_receipt(
                context, artifact_id
            )
        except Exception:
            _reject(
                "TBM_ACTIVATED_REVISION_ARTIFACT_READ_FAILED",
                "activated revision artifact could not be read",
            )
        if type(result) is not AuthorizedArtifactReadResult:
            _reject(
                "TBM_ACTIVATED_REVISION_ARTIFACT_INVALID",
                "artifact reader returned invalid data",
            )
        return result

    def _require_trusted_verifier(self, verifier_id: str, *, role: str) -> None:
        if verifier_id not in self._trusted_verifiers:
            _reject(
                "TBM_ACTIVATED_REVISION_ATTESTATION_UNTRUSTED",
                f"{role} attestation verifier is not trusted",
            )

    @staticmethod
    def _unwrap(outcome: _SourceOutcome[_T]) -> _T:
        if outcome.error is not None:
            raise outcome.error from None
        return cast(_T, outcome.value)


def _invalid(message: str) -> NoReturn:
    raise ActivatedRevisionV3Error("TBM_ACTIVATED_REVISION_INVALID", message)


def _validate_publication_links(
    revision: MemoryRevision,
    approval: MemoryRevisionApproval,
    activation: MemoryRevisionActivation,
) -> None:
    tenant_id = revision.scope.tenant_id
    repository_id = revision.scope.repository_id
    if (
        approval.revision_id != revision.revision_id
        or approval.memory_id != revision.memory_id
        or approval.revision_number != revision.revision_number
        or approval.previous_revision_id != revision.previous_revision_id
        or approval.tenant_id != tenant_id
        or approval.repository_id != repository_id
        or approval.artifact_content_sha256
        != revision.content_artifact.content_sha256
        or activation.revision_id != revision.revision_id
        or activation.approval_id != approval.approval_id
        or activation.memory_id != revision.memory_id
        or activation.revision_number != revision.revision_number
        or activation.previous_revision_id != revision.previous_revision_id
        or activation.tenant_id != tenant_id
        or activation.repository_id != repository_id
    ):
        _invalid("publication records do not match revision")


def _reject(code: str, message: str) -> NoReturn:
    raise ActivatedRevisionV3Error(code, message) from None


def _bounded_identifier(value: object) -> bool:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > 128
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True
