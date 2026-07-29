from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .authorization_v3 import (
    AuthorizationDecision,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    verify_authorization_decision,
)
from .contracts_v3 import AuthorizationScope, canonical_sha256
from .evidence_v3 import StructuredRegressionEvidence
from .fix_evidence_v3 import FixEvidence
from .memory_revision_v3 import (
    MemoryRevision,
    verify_memory_revision_evidence_bundle,
)
from .replay_v3 import verify_artifact_content


MEMORY_REVISION_APPROVAL_VERSION = "tbm.memory-revision-approval.v3"
MEMORY_REVISION_ACTIVATION_VERSION = "tbm.memory-revision-activation.v3"
MEMORY_PUBLICATION_JSON_MAX_BYTES = 1024 * 1024
MEMORY_PUBLICATION_JSON_MAX_DEPTH = 32
MEMORY_PUBLICATION_JSON_MAX_NODES = 10_000

_IDENTIFIER_MAX_CHARS = 128
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION_ID_RE = re.compile(r"memory_revision_sha256_[0-9a-f]{64}")
_APPROVAL_ID_RE = re.compile(r"memory_approval_sha256_[0-9a-f]{64}")
_ACTIVATION_ID_RE = re.compile(r"memory_activation_sha256_[0-9a-f]{64}")
_AUTHORIZATION_EVENT_ID_RE = re.compile(r"authz_sha256_[0-9a-f]{64}")
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])"
)
_APPROVAL_FIELDS = frozenset(
    {
        "contract_version",
        "approval_id",
        "revision_id",
        "memory_id",
        "revision_number",
        "previous_revision_id",
        "tenant_id",
        "repository_id",
        "artifact_content_sha256",
        "evidence_bundle_sha256",
        "approved_by",
        "approved_via_client_id",
        "authorization_event_id",
        "authorization_request_sha256",
        "authorization_policy_sha256",
        "approved_at",
        "approval_attestation_sha256",
    }
)
_ACTIVATION_FIELDS = frozenset(
    {
        "contract_version",
        "activation_id",
        "revision_id",
        "approval_id",
        "memory_id",
        "revision_number",
        "previous_revision_id",
        "activation_sequence",
        "previous_activation_id",
        "tenant_id",
        "repository_id",
        "activated_by",
        "activated_via_client_id",
        "authorization_event_id",
        "authorization_request_sha256",
        "authorization_policy_sha256",
        "activated_at",
        "activation_attestation_sha256",
    }
)


class MemoryPublicationContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MemoryRevisionApproval:
    approval_id: str
    revision_id: str
    memory_id: str
    revision_number: int
    previous_revision_id: str | None
    tenant_id: str
    repository_id: str | None
    artifact_content_sha256: str
    evidence_bundle_sha256: str
    approved_by: str
    approved_via_client_id: str
    authorization_event_id: str
    authorization_request_sha256: str
    authorization_policy_sha256: str
    approved_at: str
    approval_attestation_sha256: str
    contract_version: str = MEMORY_REVISION_APPROVAL_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != MEMORY_REVISION_APPROVAL_VERSION:
            _invalid("approval contract_version is not supported")
        _derived_id(self.approval_id, _APPROVAL_ID_RE, "approval_id")
        _derived_id(self.revision_id, _REVISION_ID_RE, "revision_id")
        _identifier(self.memory_id, "memory_id")
        _revision_lineage_shape(
            self.revision_number,
            self.previous_revision_id,
        )
        _target(self.tenant_id, self.repository_id)
        _digest(self.artifact_content_sha256, "artifact_content_sha256")
        _digest(self.evidence_bundle_sha256, "evidence_bundle_sha256")
        _identifier(self.approved_by, "approved_by")
        _identifier(self.approved_via_client_id, "approved_via_client_id")
        _derived_id(
            self.authorization_event_id,
            _AUTHORIZATION_EVENT_ID_RE,
            "authorization_event_id",
        )
        _digest(
            self.authorization_request_sha256,
            "authorization_request_sha256",
        )
        _digest(
            self.authorization_policy_sha256,
            "authorization_policy_sha256",
        )
        approved_at = _timestamp(self.approved_at, "approved_at")
        object.__setattr__(self, "approved_at", approved_at)
        _digest(
            self.approval_attestation_sha256,
            "approval_attestation_sha256",
        )
        if self.approval_id != memory_revision_approval_id(
            self._unsigned_dict()
        ):
            _mismatch("approval_id does not match approval content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "revision_id": self.revision_id,
            "memory_id": self.memory_id,
            "revision_number": self.revision_number,
            "previous_revision_id": self.previous_revision_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "artifact_content_sha256": self.artifact_content_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "approved_by": self.approved_by,
            "approved_via_client_id": self.approved_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "authorization_request_sha256": self.authorization_request_sha256,
            "authorization_policy_sha256": self.authorization_policy_sha256,
            "approved_at": self.approved_at,
            "approval_attestation_sha256": self.approval_attestation_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {"approval_id": self.approval_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class MemoryRevisionActivation:
    activation_id: str
    revision_id: str
    approval_id: str
    memory_id: str
    revision_number: int
    previous_revision_id: str | None
    activation_sequence: int
    previous_activation_id: str | None
    tenant_id: str
    repository_id: str | None
    activated_by: str
    activated_via_client_id: str
    authorization_event_id: str
    authorization_request_sha256: str
    authorization_policy_sha256: str
    activated_at: str
    activation_attestation_sha256: str
    contract_version: str = MEMORY_REVISION_ACTIVATION_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != MEMORY_REVISION_ACTIVATION_VERSION:
            _invalid("activation contract_version is not supported")
        _derived_id(self.activation_id, _ACTIVATION_ID_RE, "activation_id")
        _derived_id(self.revision_id, _REVISION_ID_RE, "revision_id")
        _derived_id(self.approval_id, _APPROVAL_ID_RE, "approval_id")
        _identifier(self.memory_id, "memory_id")
        _revision_lineage_shape(
            self.revision_number,
            self.previous_revision_id,
        )
        if (
            type(self.activation_sequence) is not int
            or self.activation_sequence != self.revision_number
        ):
            _invalid("activation_sequence must equal revision_number")
        if self.activation_sequence == 1:
            if self.previous_activation_id is not None:
                _invalid("first activation forbids previous_activation_id")
        else:
            _derived_id(
                self.previous_activation_id,
                _ACTIVATION_ID_RE,
                "previous_activation_id",
            )
        _target(self.tenant_id, self.repository_id)
        _identifier(self.activated_by, "activated_by")
        _identifier(
            self.activated_via_client_id,
            "activated_via_client_id",
        )
        _derived_id(
            self.authorization_event_id,
            _AUTHORIZATION_EVENT_ID_RE,
            "authorization_event_id",
        )
        _digest(
            self.authorization_request_sha256,
            "authorization_request_sha256",
        )
        _digest(
            self.authorization_policy_sha256,
            "authorization_policy_sha256",
        )
        activated_at = _timestamp(self.activated_at, "activated_at")
        object.__setattr__(self, "activated_at", activated_at)
        _digest(
            self.activation_attestation_sha256,
            "activation_attestation_sha256",
        )
        if self.activation_id != memory_revision_activation_id(
            self._unsigned_dict()
        ):
            _mismatch("activation_id does not match activation content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "revision_id": self.revision_id,
            "approval_id": self.approval_id,
            "memory_id": self.memory_id,
            "revision_number": self.revision_number,
            "previous_revision_id": self.previous_revision_id,
            "activation_sequence": self.activation_sequence,
            "previous_activation_id": self.previous_activation_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "activated_by": self.activated_by,
            "activated_via_client_id": self.activated_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "authorization_request_sha256": self.authorization_request_sha256,
            "authorization_policy_sha256": self.authorization_policy_sha256,
            "activated_at": self.activated_at,
            "activation_attestation_sha256": (
                self.activation_attestation_sha256
            ),
        }

    def to_dict(self) -> dict[str, object]:
        return {"activation_id": self.activation_id, **self._unsigned_dict()}


def memory_revision_approval_id(content: Mapping[str, object]) -> str:
    return "memory_approval_sha256_" + canonical_sha256(content).removeprefix(
        "sha256:"
    )


def memory_revision_activation_id(content: Mapping[str, object]) -> str:
    return "memory_activation_sha256_" + canonical_sha256(
        content
    ).removeprefix("sha256:")


def approve_memory_revision(
    *,
    revision: MemoryRevision,
    previous_revision: MemoryRevision | None,
    content: bytes,
    fix_evidence_by_id: Mapping[str, FixEvidence],
    regression_evidence_by_id: Mapping[
        str,
        StructuredRegressionEvidence,
    ],
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    approved_by: str,
    approved_via_client_id: str,
    approved_at: str,
    approval_attestation_sha256: str,
) -> MemoryRevisionApproval:
    _exact(revision, MemoryRevision, "revision")
    _lineage(revision, previous_revision)
    if type(content) is not bytes:
        _invalid("content must be bytes")
    _mapping(fix_evidence_by_id, "fix_evidence_by_id")
    _mapping(regression_evidence_by_id, "regression_evidence_by_id")
    if not verify_artifact_content(revision.content_artifact, content):
        _mismatch("content bytes do not match revision artifact")
    try:
        verify_memory_revision_evidence_bundle(
            revision,
            fix_evidence_by_id,
            regression_evidence_by_id,
        )
    except ValueError as error:
        raise MemoryPublicationContractError(
            "TBM_MEMORY_PUBLICATION_EVIDENCE_INVALID",
            "memory revision evidence bundle failed verification",
        ) from error
    _identifier(approved_by, "approved_by")
    _identifier(approved_via_client_id, "approved_via_client_id")
    if approved_by == revision.proposed_by:
        _invalid("revision proposer must not approve the revision")
    _require_independent_evidence_actor(
        revision,
        approved_by,
        fix_evidence_by_id,
        regression_evidence_by_id,
        role="approver",
    )
    approved_time = _timestamp(approved_at, "approved_at")
    if parse_rfc3339(approved_time) < parse_rfc3339(revision.proposed_at):
        _invalid("approved_at must not precede proposed_at")
    tenant_id, repository_id = _scope_target(revision.scope)
    _publication_authorization(
        policy=policy,
        request=request,
        decision=decision,
        permission="memory:review",
        actor_id=approved_by,
        client_id=approved_via_client_id,
        tenant_id=tenant_id,
        repository_id=repository_id,
        event_at=approved_time,
    )
    evidence_digest = memory_revision_evidence_bundle_sha256(
        revision,
        fix_evidence_by_id,
        regression_evidence_by_id,
    )
    values = {
        "contract_version": MEMORY_REVISION_APPROVAL_VERSION,
        "revision_id": revision.revision_id,
        "memory_id": revision.memory_id,
        "revision_number": revision.revision_number,
        "previous_revision_id": revision.previous_revision_id,
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "artifact_content_sha256": revision.content_artifact.content_sha256,
        "evidence_bundle_sha256": evidence_digest,
        "approved_by": approved_by,
        "approved_via_client_id": approved_via_client_id,
        "authorization_event_id": decision.authorization_event_id,
        "authorization_request_sha256": decision.request_sha256,
        "authorization_policy_sha256": decision.policy_sha256,
        "approved_at": approved_time,
        "approval_attestation_sha256": approval_attestation_sha256,
    }
    _digest(
        approval_attestation_sha256,
        "approval_attestation_sha256",
    )
    return MemoryRevisionApproval(
        approval_id=memory_revision_approval_id(values),
        **values,
    )


def activate_memory_revision(
    *,
    revision: MemoryRevision,
    approval: MemoryRevisionApproval,
    previous_revision: MemoryRevision | None,
    content: bytes,
    fix_evidence_by_id: Mapping[str, FixEvidence],
    regression_evidence_by_id: Mapping[
        str,
        StructuredRegressionEvidence,
    ],
    approval_policy: AuthorizationPolicyBundle,
    approval_request: AuthorizationRequest,
    approval_decision: AuthorizationDecision,
    previous_activation: MemoryRevisionActivation | None,
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    activated_by: str,
    activated_via_client_id: str,
    activated_at: str,
    activation_attestation_sha256: str,
) -> MemoryRevisionActivation:
    _exact(revision, MemoryRevision, "revision")
    _exact(approval, MemoryRevisionApproval, "approval")
    verify_memory_revision_approval(
        approval,
        revision=revision,
        previous_revision=previous_revision,
        content=content,
        fix_evidence_by_id=fix_evidence_by_id,
        regression_evidence_by_id=regression_evidence_by_id,
        policy=approval_policy,
        request=approval_request,
        decision=approval_decision,
    )
    tenant_id, repository_id = _scope_target(revision.scope)
    if (
        approval.revision_id != revision.revision_id
        or approval.memory_id != revision.memory_id
        or approval.revision_number != revision.revision_number
        or approval.previous_revision_id != revision.previous_revision_id
        or approval.tenant_id != tenant_id
        or approval.repository_id != repository_id
        or approval.artifact_content_sha256
        != revision.content_artifact.content_sha256
    ):
        _mismatch("approval does not match revision")
    _activation_lineage(revision, previous_activation)
    _identifier(activated_by, "activated_by")
    _identifier(
        activated_via_client_id,
        "activated_via_client_id",
    )
    if activated_by in {revision.proposed_by, approval.approved_by}:
        _invalid("activator must be independent of proposer and approver")
    _require_independent_evidence_actor(
        revision,
        activated_by,
        fix_evidence_by_id,
        regression_evidence_by_id,
        role="activator",
    )
    activated_time = _timestamp(activated_at, "activated_at")
    if parse_rfc3339(activated_time) < parse_rfc3339(approval.approved_at):
        _invalid("activated_at must not precede approved_at")
    _publication_authorization(
        policy=policy,
        request=request,
        decision=decision,
        permission="memory:activate",
        actor_id=activated_by,
        client_id=activated_via_client_id,
        tenant_id=tenant_id,
        repository_id=repository_id,
        event_at=activated_time,
    )
    previous_activation_id = (
        previous_activation.activation_id
        if previous_activation is not None
        else None
    )
    values = {
        "contract_version": MEMORY_REVISION_ACTIVATION_VERSION,
        "revision_id": revision.revision_id,
        "approval_id": approval.approval_id,
        "memory_id": revision.memory_id,
        "revision_number": revision.revision_number,
        "previous_revision_id": revision.previous_revision_id,
        "activation_sequence": revision.revision_number,
        "previous_activation_id": previous_activation_id,
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "activated_by": activated_by,
        "activated_via_client_id": activated_via_client_id,
        "authorization_event_id": decision.authorization_event_id,
        "authorization_request_sha256": decision.request_sha256,
        "authorization_policy_sha256": decision.policy_sha256,
        "activated_at": activated_time,
        "activation_attestation_sha256": activation_attestation_sha256,
    }
    _digest(
        activation_attestation_sha256,
        "activation_attestation_sha256",
    )
    return MemoryRevisionActivation(
        activation_id=memory_revision_activation_id(values),
        **values,
    )


def verify_memory_revision_approval(
    approval: MemoryRevisionApproval,
    *,
    revision: MemoryRevision,
    previous_revision: MemoryRevision | None,
    content: bytes,
    fix_evidence_by_id: Mapping[str, FixEvidence],
    regression_evidence_by_id: Mapping[
        str,
        StructuredRegressionEvidence,
    ],
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
) -> None:
    _exact(approval, MemoryRevisionApproval, "approval")
    expected = approve_memory_revision(
        revision=revision,
        previous_revision=previous_revision,
        content=content,
        fix_evidence_by_id=fix_evidence_by_id,
        regression_evidence_by_id=regression_evidence_by_id,
        policy=policy,
        request=request,
        decision=decision,
        approved_by=approval.approved_by,
        approved_via_client_id=approval.approved_via_client_id,
        approved_at=approval.approved_at,
        approval_attestation_sha256=approval.approval_attestation_sha256,
    )
    if expected != approval:
        _mismatch("approval does not match publication inputs")


def verify_memory_revision_activation(
    activation: MemoryRevisionActivation,
    *,
    revision: MemoryRevision,
    approval: MemoryRevisionApproval,
    previous_revision: MemoryRevision | None,
    content: bytes,
    fix_evidence_by_id: Mapping[str, FixEvidence],
    regression_evidence_by_id: Mapping[
        str,
        StructuredRegressionEvidence,
    ],
    approval_policy: AuthorizationPolicyBundle,
    approval_request: AuthorizationRequest,
    approval_decision: AuthorizationDecision,
    previous_activation: MemoryRevisionActivation | None,
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
) -> None:
    _exact(activation, MemoryRevisionActivation, "activation")
    expected = activate_memory_revision(
        revision=revision,
        approval=approval,
        previous_revision=previous_revision,
        content=content,
        fix_evidence_by_id=fix_evidence_by_id,
        regression_evidence_by_id=regression_evidence_by_id,
        approval_policy=approval_policy,
        approval_request=approval_request,
        approval_decision=approval_decision,
        previous_activation=previous_activation,
        policy=policy,
        request=request,
        decision=decision,
        activated_by=activation.activated_by,
        activated_via_client_id=activation.activated_via_client_id,
        activated_at=activation.activated_at,
        activation_attestation_sha256=(
            activation.activation_attestation_sha256
        ),
    )
    if expected != activation:
        _mismatch("activation does not match publication inputs")


def memory_revision_evidence_bundle_sha256(
    revision: MemoryRevision,
    fix_evidence_by_id: Mapping[str, FixEvidence],
    regression_evidence_by_id: Mapping[
        str,
        StructuredRegressionEvidence,
    ],
) -> str:
    _exact(revision, MemoryRevision, "revision")
    _mapping(fix_evidence_by_id, "fix_evidence_by_id")
    _mapping(regression_evidence_by_id, "regression_evidence_by_id")
    if revision.memory_kind == "project_policy":
        return canonical_sha256(
            {
                "revision_id": revision.revision_id,
                "fix_evidence": None,
                "regression_evidence": [],
            }
        )
    fix = fix_evidence_by_id.get(cast(str, revision.fix_evidence_id))
    if type(fix) is not FixEvidence:
        _invalid("fix evidence is missing from evidence bundle")
    regressions: list[StructuredRegressionEvidence] = []
    for evidence_id in revision.regression_evidence_ids:
        evidence = regression_evidence_by_id.get(evidence_id)
        if type(evidence) is not StructuredRegressionEvidence:
            _invalid("regression evidence is missing from evidence bundle")
        regressions.append(evidence)
    return canonical_sha256(
        {
            "revision_id": revision.revision_id,
            "fix_evidence": fix.to_dict(),
            "regression_evidence": [
                item.to_dict()
                for item in sorted(
                    regressions,
                    key=lambda item: item.evidence_id,
                )
            ],
        }
    )


def dumps_memory_revision_approval(
    approval: MemoryRevisionApproval,
) -> str:
    _exact(approval, MemoryRevisionApproval, "approval")
    return _canonical_json(approval.to_dict())


def dumps_memory_revision_activation(
    activation: MemoryRevisionActivation,
) -> str:
    _exact(activation, MemoryRevisionActivation, "activation")
    return _canonical_json(activation.to_dict())


def loads_memory_revision_approval(
    source: str | bytes,
) -> MemoryRevisionApproval:
    return parse_memory_revision_approval(
        _loads_json(source, "memory revision approval")
    )


def loads_memory_revision_activation(
    source: str | bytes,
) -> MemoryRevisionActivation:
    return parse_memory_revision_activation(
        _loads_json(source, "memory revision activation")
    )


def parse_memory_revision_approval(
    payload: Mapping[str, object],
) -> MemoryRevisionApproval:
    item = _strict_object(payload, _APPROVAL_FIELDS, "approval")
    return MemoryRevisionApproval(
        contract_version=_string(item, "contract_version"),
        approval_id=_string(item, "approval_id"),
        revision_id=_string(item, "revision_id"),
        memory_id=_string(item, "memory_id"),
        revision_number=_integer(item, "revision_number"),
        previous_revision_id=_optional_string(
            item,
            "previous_revision_id",
        ),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_optional_string(item, "repository_id"),
        artifact_content_sha256=_string(
            item,
            "artifact_content_sha256",
        ),
        evidence_bundle_sha256=_string(
            item,
            "evidence_bundle_sha256",
        ),
        approved_by=_string(item, "approved_by"),
        approved_via_client_id=_string(
            item,
            "approved_via_client_id",
        ),
        authorization_event_id=_string(
            item,
            "authorization_event_id",
        ),
        authorization_request_sha256=_string(
            item,
            "authorization_request_sha256",
        ),
        authorization_policy_sha256=_string(
            item,
            "authorization_policy_sha256",
        ),
        approved_at=_string(item, "approved_at"),
        approval_attestation_sha256=_string(
            item,
            "approval_attestation_sha256",
        ),
    )


def parse_memory_revision_activation(
    payload: Mapping[str, object],
) -> MemoryRevisionActivation:
    item = _strict_object(payload, _ACTIVATION_FIELDS, "activation")
    return MemoryRevisionActivation(
        contract_version=_string(item, "contract_version"),
        activation_id=_string(item, "activation_id"),
        revision_id=_string(item, "revision_id"),
        approval_id=_string(item, "approval_id"),
        memory_id=_string(item, "memory_id"),
        revision_number=_integer(item, "revision_number"),
        previous_revision_id=_optional_string(
            item,
            "previous_revision_id",
        ),
        activation_sequence=_integer(item, "activation_sequence"),
        previous_activation_id=_optional_string(
            item,
            "previous_activation_id",
        ),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_optional_string(item, "repository_id"),
        activated_by=_string(item, "activated_by"),
        activated_via_client_id=_string(
            item,
            "activated_via_client_id",
        ),
        authorization_event_id=_string(
            item,
            "authorization_event_id",
        ),
        authorization_request_sha256=_string(
            item,
            "authorization_request_sha256",
        ),
        authorization_policy_sha256=_string(
            item,
            "authorization_policy_sha256",
        ),
        activated_at=_string(item, "activated_at"),
        activation_attestation_sha256=_string(
            item,
            "activation_attestation_sha256",
        ),
    )


def _publication_authorization(
    *,
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    permission: str,
    actor_id: str,
    client_id: str,
    tenant_id: str,
    repository_id: str | None,
    event_at: str,
) -> None:
    _exact(policy, AuthorizationPolicyBundle, "policy")
    _exact(request, AuthorizationRequest, "request")
    _exact(decision, AuthorizationDecision, "decision")
    try:
        verify_authorization_decision(policy, request, decision)
    except ValueError as error:
        raise MemoryPublicationContractError(
            "TBM_MEMORY_PUBLICATION_AUTHORIZATION_INVALID",
            "authorization decision failed exact verification",
        ) from error
    if not decision.allowed:
        raise MemoryPublicationContractError(
            "TBM_MEMORY_PUBLICATION_AUTHORIZATION_DENIED",
            "publication authorization was denied",
        )
    if (
        request.permission != permission
        or decision.permission != permission
        or request.principal_id != actor_id
        or decision.principal_id != actor_id
        or request.agent_client_id != client_id
        or decision.agent_client_id != client_id
        or request.tenant_id != tenant_id
        or decision.tenant_id != tenant_id
        or decision.repository_id != repository_id
        or canonical_rfc3339(decision.decided_at) != event_at
    ):
        _mismatch("authorization does not match publication event")
    if repository_id is None:
        if request.repository_reference is not None:
            _mismatch("tenant publication authorization named a repository")
    elif request.repository_reference is None:
        _mismatch("repository publication authorization omitted repository")


def _lineage(
    revision: MemoryRevision,
    previous_revision: MemoryRevision | None,
) -> None:
    if revision.revision_number == 1:
        if previous_revision is not None:
            _invalid("first revision forbids previous revision")
        return
    _exact(previous_revision, MemoryRevision, "previous_revision")
    previous = cast(MemoryRevision, previous_revision)
    if (
        revision.previous_revision_id != previous.revision_id
        or revision.memory_id != previous.memory_id
        or revision.memory_kind != previous.memory_kind
        or revision.revision_number != previous.revision_number + 1
        or revision.scope != previous.scope
    ):
        _mismatch("previous revision does not establish linear lineage")
    if parse_rfc3339(revision.proposed_at) < parse_rfc3339(
        previous.proposed_at
    ):
        _invalid("revision proposal time precedes previous revision")


def _activation_lineage(
    revision: MemoryRevision,
    previous_activation: MemoryRevisionActivation | None,
) -> None:
    if revision.revision_number == 1:
        if previous_activation is not None:
            _invalid("first activation forbids previous activation")
        return
    _exact(
        previous_activation,
        MemoryRevisionActivation,
        "previous_activation",
    )
    previous = cast(MemoryRevisionActivation, previous_activation)
    if (
        previous.revision_id != revision.previous_revision_id
        or previous.memory_id != revision.memory_id
        or previous.revision_number + 1 != revision.revision_number
        or previous.activation_sequence + 1 != revision.revision_number
        or previous.tenant_id != revision.scope.tenant_id
        or previous.repository_id != revision.scope.repository_id
    ):
        _mismatch("previous activation does not establish linear predecessor")


def _scope_target(scope: AuthorizationScope) -> tuple[str, str | None]:
    _exact(scope, AuthorizationScope, "scope")
    if scope.kind == "global":
        _invalid("MemoryRevision publication forbids global scope")
    tenant_id = scope.tenant_id
    if tenant_id is None:
        _invalid("publication scope requires tenant_id")
    return tenant_id, scope.repository_id


def _require_independent_evidence_actor(
    revision: MemoryRevision,
    actor_id: str,
    fix_evidence_by_id: Mapping[str, FixEvidence],
    regression_evidence_by_id: Mapping[
        str,
        StructuredRegressionEvidence,
    ],
    *,
    role: str,
) -> None:
    if revision.memory_kind != "lesson":
        return
    fix = fix_evidence_by_id.get(cast(str, revision.fix_evidence_id))
    if type(fix) is not FixEvidence:
        _invalid("fix evidence is missing")
    actors = {
        fix.submitter_id,
        fix.reviewer_id,
        fix.source_to_fix.verified_by,
    }
    for evidence_id in revision.regression_evidence_ids:
        evidence = regression_evidence_by_id.get(evidence_id)
        if type(evidence) is not StructuredRegressionEvidence:
            _invalid("regression evidence is missing")
        actors.update({evidence.submitter_id, evidence.verifier_id})
    if actor_id in actors:
        _invalid(f"{role} must be independent of evidence actors")


def _revision_lineage_shape(
    revision_number: object,
    previous_revision_id: object,
) -> None:
    if type(revision_number) is not int or revision_number < 1:
        _invalid("revision_number must be a positive integer")
    if revision_number == 1:
        if previous_revision_id is not None:
            _invalid("first revision forbids previous_revision_id")
    else:
        _derived_id(
            previous_revision_id,
            _REVISION_ID_RE,
            "previous_revision_id",
        )


def _target(tenant_id: object, repository_id: object) -> None:
    _identifier(tenant_id, "tenant_id")
    if repository_id is not None:
        _identifier(repository_id, "repository_id")


def _loads_json(source: str | bytes, label: str) -> dict[str, object]:
    try:
        if type(source) is bytes:
            raw = decode_bounded_utf8(
                source,
                max_bytes=MEMORY_PUBLICATION_JSON_MAX_BYTES,
                description=label,
            )
        elif type(source) is str:
            raw = decode_bounded_utf8(
                source.encode("utf-8"),
                max_bytes=MEMORY_PUBLICATION_JSON_MAX_BYTES,
                description=label,
            )
        else:
            raise TypeError
        parsed = parse_bounded_json(
            raw,
            description=label,
            max_nodes=MEMORY_PUBLICATION_JSON_MAX_NODES,
            max_depth=MEMORY_PUBLICATION_JSON_MAX_DEPTH,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise MemoryPublicationContractError(
            "TBM_MEMORY_PUBLICATION_INVALID_JSON",
            f"{label} must be bounded strict JSON",
        ) from error
    if type(parsed) is not dict:
        _invalid(f"{label} must be an object")
    return cast(dict[str, object], parsed)


def _strict_object(
    value: object,
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or any(
        type(key) is not str for key in value
    ):
        _invalid(f"{label} must be an object with string keys")
    item = cast(dict[str, object], value)
    if set(item) != fields:
        _invalid(f"{label} fields do not match contract")
    return item


def _string(item: dict[str, object], name: str) -> str:
    value = item[name]
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return cast(str, value)


def _optional_string(
    item: dict[str, object],
    name: str,
) -> str | None:
    value = item[name]
    if value is not None and type(value) is not str:
        _invalid(f"{name} must be a string or null")
    return cast(str | None, value)


def _integer(item: dict[str, object], name: str) -> int:
    value = item[name]
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    return cast(int, value)


def _timestamp(value: object, name: str) -> str:
    try:
        if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
            raise ValueError
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise MemoryPublicationContractError(
            "TBM_MEMORY_PUBLICATION_INVALID",
            f"{name} must be a timezone-aware RFC 3339 timestamp",
        ) from error


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _IDENTIFIER_MAX_CHARS
    ):
        _invalid(f"{name} must be a non-empty bounded identifier")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise MemoryPublicationContractError(
            "TBM_MEMORY_PUBLICATION_INVALID",
            f"{name} must be valid UTF-8",
        ) from error


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a sha256 digest")


def _derived_id(value: object, pattern: re.Pattern[str], name: str) -> None:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _invalid(f"{name} has invalid content-derived form")


def _exact(value: object, expected: type[object], name: str) -> None:
    if type(value) is not expected:
        _invalid(f"{name} must be exactly {expected.__name__}")


def _mapping(value: object, name: str) -> None:
    if not isinstance(value, Mapping):
        _invalid(f"{name} must be a mapping")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise MemoryPublicationContractError(
            "TBM_MEMORY_PUBLICATION_INVALID",
            "publication event cannot be encoded as canonical JSON",
        ) from error


def _invalid(message: str) -> NoReturn:
    raise MemoryPublicationContractError(
        "TBM_MEMORY_PUBLICATION_INVALID",
        message,
    )


def _mismatch(message: str) -> NoReturn:
    raise MemoryPublicationContractError(
        "TBM_MEMORY_PUBLICATION_MISMATCH",
        message,
    )
