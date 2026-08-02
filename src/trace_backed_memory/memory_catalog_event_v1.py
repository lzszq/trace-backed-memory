from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, Protocol, cast

from ._timestamps import canonical_rfc3339, parse_rfc3339
from ._ingestion import parse_bounded_json
from .activated_revision_v3 import (
    ActivatedRevisionCandidate,
    ActivatedRevisionV3Error,
)
from .authorization_v3 import (
    AuthorizationPermission,
    AuthorizationRequest,
    parse_authorization_decision,
    parse_authorization_policy,
)
from .contracts_v3 import canonical_sha256
from .evidence_v3 import (
    StructuredRegressionEvidence,
    loads_structured_regression_evidence,
)
from .event_registry_v1 import EventPayloadRegistration, EventTypeRegistry
from .event_v1 import CanonicalEvent, build_canonical_event, verify_event_parent
from .fix_evidence_v3 import FixEvidence, loads_fix_evidence
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerConflictError,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerIdempotency,
    verify_ledger_append_receipt,
)
from .memory_publication_v3 import (
    MemoryRevisionActivation,
    MemoryRevisionApproval,
    StoredMemoryRevisionActivationPublication,
    StoredMemoryRevisionApprovalPublication,
    loads_memory_revision_activation,
    loads_memory_revision_approval,
    memory_revision_evidence_bundle_sha256,
)
from .memory_revision_v3 import (
    MemoryRevision,
    loads_memory_revision,
    verify_memory_revision_evidence_bundle,
)
from .models import Lesson
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerV1Error,
    execute_reducer_step,
    initial_reducer_state,
)
from .retrieval_preparation_v3 import ActivatedRevisionRetrievalSource
from .service_v3 import AuthenticatedServiceContext, AuthorizedRetrievalScope


MEMORY_CATALOG_EVENT_PROTOCOL_VERSION = "tbm.memory-catalog-event.v1"
MEMORY_CATALOG_EVENT_STREAM_TYPE = "memory_catalog"
MEMORY_CATALOG_EVENT_PROJECTION = "memory_catalog_current_v1"
MEMORY_CATALOG_EVENT_REDUCER_ID = "memory-catalog-current"
MEMORY_CATALOG_EVENT_MAX_BATCH = 32
MEMORY_CATALOG_EVENT_MAX_STREAM_EVENTS = 10_000
MEMORY_CATALOG_EVENT_MAX_REBUILD_STREAMS = 10_000
MEMORY_CATALOG_EVENT_MAX_REBUILD_EVENTS = 100_000
MEMORY_CATALOG_EVENT_MAX_REBUILD_SCAN_EVENTS = 1_000_000
MEMORY_CATALOG_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "memory_catalog_event_payload_registry_v1.schema.json"
)

MEMORY_REVISION_PROPOSED = "tbm.memory.revision_proposed"
MEMORY_REVISION_REVIEWED = "tbm.memory.revision_reviewed"
MEMORY_REVISION_REJECTED = "tbm.memory.revision_rejected"
MEMORY_FIX_EVIDENCE_RECORDED = "tbm.memory.fix_evidence_recorded"
MEMORY_REGRESSION_EVIDENCE_RECORDED = (
    "tbm.memory.regression_evidence_recorded"
)
MEMORY_REVISION_APPROVED = "tbm.memory.revision_approved"
MEMORY_REVISION_ACTIVATED = "tbm.memory.revision_activated"
MEMORY_REVISION_SUSPENDED = "tbm.memory.revision_suspended"
MEMORY_REVISION_SUPERSEDED = "tbm.memory.revision_superseded"
MEMORY_REVISION_OBSOLETED = "tbm.memory.revision_obsoleted"
MEMORY_RELATIONSHIP_RECORDED = "tbm.memory.relationship_recorded"
MEMORY_COUNTEREXAMPLE_RECORDED = "tbm.memory.counterexample_recorded"

MEMORY_CATALOG_EVENT_TYPES = tuple(
    sorted(
        (
            MEMORY_REVISION_PROPOSED,
            MEMORY_REVISION_REVIEWED,
            MEMORY_REVISION_REJECTED,
            MEMORY_FIX_EVIDENCE_RECORDED,
            MEMORY_REGRESSION_EVIDENCE_RECORDED,
            MEMORY_REVISION_APPROVED,
            MEMORY_REVISION_ACTIVATED,
            MEMORY_REVISION_SUSPENDED,
            MEMORY_REVISION_SUPERSEDED,
            MEMORY_REVISION_OBSOLETED,
            MEMORY_RELATIONSHIP_RECORDED,
            MEMORY_COUNTEREXAMPLE_RECORDED,
        )
    )
)

_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in MEMORY_CATALOG_EVENT_TYPES
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVIEW_ID_RE = re.compile(r"^memory_review_sha256_[0-9a-f]{64}$")
_STATE_CHANGE_ID_RE = re.compile(
    r"^memory_state_change_sha256_[0-9a-f]{64}$"
)
_RELATIONSHIP_ID_RE = re.compile(
    r"^memory_relationship_sha256_[0-9a-f]{64}$"
)
_COUNTEREXAMPLE_ID_RE = re.compile(
    r"^memory_counterexample_sha256_[0-9a-f]{64}$"
)
_REVIEW_FIELDS = frozenset(
    {
        "review_id",
        "contract_version",
        "tenant_id",
        "repository_id",
        "memory_id",
        "revision_id",
        "decision",
        "reviewed_by",
        "reviewed_at",
        "rationale_sha256",
        "review_attestation_sha256",
    }
)
_EVIDENCE_RECORD_FIELDS = frozenset(
    {
        "contract_version",
        "tenant_id",
        "repository_id",
        "memory_id",
        "revision_id",
        "evidence_kind",
        "evidence",
    }
)
_STATE_CHANGE_FIELDS = frozenset(
    {
        "state_change_id",
        "contract_version",
        "tenant_id",
        "repository_id",
        "memory_id",
        "revision_id",
        "activation_id",
        "change",
        "replacement_revision_id",
        "changed_by",
        "changed_at",
        "reason_sha256",
        "change_attestation_sha256",
    }
)
_RELATIONSHIP_FIELDS = frozenset(
    {
        "relationship_id",
        "contract_version",
        "tenant_id",
        "repository_id",
        "memory_id",
        "from_revision_id",
        "to_revision_id",
        "relationship",
        "recorded_by",
        "recorded_at",
        "evidence_sha256",
        "relationship_attestation_sha256",
    }
)
_COUNTEREXAMPLE_FIELDS = frozenset(
    {
        "counterexample_id",
        "contract_version",
        "tenant_id",
        "repository_id",
        "memory_id",
        "revision_id",
        "evidence",
        "recorded_by",
        "recorded_at",
        "counterexample_attestation_sha256",
    }
)
_PUBLICATION_RECORD_FIELDS = frozenset(
    {
        "contract_version",
        "publication",
        "policy",
        "request",
        "decision",
        "attestation_verified_by",
    }
)
_AUTHORIZATION_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "principal_id",
        "agent_client_id",
        "tenant_id",
        "repository_reference",
        "permission",
        "requested_at",
    }
)

MemoryReviewDecision = Literal["accepted", "rejected"]
MemoryStateChangeKind = Literal["suspended", "superseded", "obsoleted"]
MemoryRelationshipKind = Literal["supersedes", "related", "contradicts"]
MemoryCatalogRevisionStatus = Literal[
    "proposed",
    "reviewed",
    "rejected",
    "approved",
    "active",
    "suspended",
    "superseded",
    "obsoleted",
]


class MemoryCatalogEventV1Error(ReducerV1Error):
    """Stable failure for MemoryCatalog event production and replay."""


@dataclass(frozen=True)
class MemoryRevisionReview:
    review_id: str
    tenant_id: str
    repository_id: str
    memory_id: str
    revision_id: str
    decision: MemoryReviewDecision
    reviewed_by: str
    reviewed_at: str
    rationale_sha256: str
    review_attestation_sha256: str
    contract_version: str = "tbm.memory-revision-review.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.memory-revision-review.v1":
            _record_invalid("review contract_version is unsupported")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.memory_id, "memory_id"),
            (self.revision_id, "revision_id"),
            (self.reviewed_by, "reviewed_by"),
        ):
            _identifier(value, name)
        if self.decision not in {"accepted", "rejected"}:
            _record_invalid("review decision is unsupported")
        object.__setattr__(
            self, "reviewed_at", _timestamp(self.reviewed_at, "reviewed_at")
        )
        _digest(self.rationale_sha256, "rationale_sha256")
        _digest(self.review_attestation_sha256, "review_attestation_sha256")
        if _REVIEW_ID_RE.fullmatch(self.review_id) is None:
            _record_invalid("review_id is invalid")
        if self.review_id != _derived_id(
            "memory_review_sha256_", self._unsigned_dict()
        ):
            _record_invalid("review_id does not match review content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "memory_id": self.memory_id,
            "revision_id": self.revision_id,
            "decision": self.decision,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "rationale_sha256": self.rationale_sha256,
            "review_attestation_sha256": self.review_attestation_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {"review_id": self.review_id, **self._unsigned_dict()}


def build_memory_revision_review(
    *,
    tenant_id: str,
    repository_id: str,
    memory_id: str,
    revision_id: str,
    decision: MemoryReviewDecision,
    reviewed_by: str,
    reviewed_at: str,
    rationale_sha256: str,
    review_attestation_sha256: str,
) -> MemoryRevisionReview:
    values = {
        "contract_version": "tbm.memory-revision-review.v1",
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "memory_id": memory_id,
        "revision_id": revision_id,
        "decision": decision,
        "reviewed_by": reviewed_by,
        "reviewed_at": _timestamp(reviewed_at, "reviewed_at"),
        "rationale_sha256": rationale_sha256,
        "review_attestation_sha256": review_attestation_sha256,
    }
    return MemoryRevisionReview(
        review_id=_derived_id("memory_review_sha256_", values),
        tenant_id=tenant_id,
        repository_id=repository_id,
        memory_id=memory_id,
        revision_id=revision_id,
        decision=decision,
        reviewed_by=reviewed_by,
        reviewed_at=cast(str, values["reviewed_at"]),
        rationale_sha256=rationale_sha256,
        review_attestation_sha256=review_attestation_sha256,
    )


@dataclass(frozen=True)
class MemoryCatalogEvidenceRecord:
    tenant_id: str
    repository_id: str
    memory_id: str
    revision_id: str
    evidence: FixEvidence | StructuredRegressionEvidence
    contract_version: str = "tbm.memory-catalog-evidence-record.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.memory-catalog-evidence-record.v1":
            _record_invalid("evidence record contract_version is unsupported")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.memory_id, "memory_id"),
            (self.revision_id, "revision_id"),
        ):
            _identifier(value, name)
        if type(self.evidence) not in {
            FixEvidence,
            StructuredRegressionEvidence,
        }:
            _record_invalid("evidence must be an exact v3 evidence record")

    @property
    def evidence_kind(self) -> Literal["fix", "regression"]:
        return "fix" if type(self.evidence) is FixEvidence else "regression"

    @property
    def evidence_id(self) -> str:
        return self.evidence.evidence_id

    @property
    def actor_id(self) -> str:
        if type(self.evidence) is FixEvidence:
            return self.evidence.reviewer_id
        return cast(StructuredRegressionEvidence, self.evidence).verifier_id

    @property
    def occurred_at(self) -> str:
        if type(self.evidence) is FixEvidence:
            return self.evidence.reviewed_at
        return cast(StructuredRegressionEvidence, self.evidence).verified_at

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "memory_id": self.memory_id,
            "revision_id": self.revision_id,
            "evidence_kind": self.evidence_kind,
            "evidence": self.evidence.to_dict(),
        }


@dataclass(frozen=True)
class MemoryRevisionStateChange:
    state_change_id: str
    tenant_id: str
    repository_id: str
    memory_id: str
    revision_id: str
    activation_id: str | None
    change: MemoryStateChangeKind
    replacement_revision_id: str | None
    changed_by: str
    changed_at: str
    reason_sha256: str
    change_attestation_sha256: str
    contract_version: str = "tbm.memory-revision-state-change.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.memory-revision-state-change.v1":
            _record_invalid("state change contract_version is unsupported")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.memory_id, "memory_id"),
            (self.revision_id, "revision_id"),
            (self.changed_by, "changed_by"),
        ):
            _identifier(value, name)
        if self.activation_id is not None:
            _identifier(self.activation_id, "activation_id")
        if self.change not in {"suspended", "superseded", "obsoleted"}:
            _record_invalid("state change is unsupported")
        if self.change == "superseded":
            _identifier(self.replacement_revision_id, "replacement_revision_id")
            if self.replacement_revision_id == self.revision_id:
                _record_invalid("a revision cannot supersede itself")
        elif self.replacement_revision_id is not None:
            _record_invalid("only superseded may name a replacement revision")
        object.__setattr__(
            self, "changed_at", _timestamp(self.changed_at, "changed_at")
        )
        _digest(self.reason_sha256, "reason_sha256")
        _digest(self.change_attestation_sha256, "change_attestation_sha256")
        if _STATE_CHANGE_ID_RE.fullmatch(self.state_change_id) is None:
            _record_invalid("state_change_id is invalid")
        if self.state_change_id != _derived_id(
            "memory_state_change_sha256_", self._unsigned_dict()
        ):
            _record_invalid("state_change_id does not match state change content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "memory_id": self.memory_id,
            "revision_id": self.revision_id,
            "activation_id": self.activation_id,
            "change": self.change,
            "replacement_revision_id": self.replacement_revision_id,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
            "reason_sha256": self.reason_sha256,
            "change_attestation_sha256": self.change_attestation_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {"state_change_id": self.state_change_id, **self._unsigned_dict()}


def build_memory_revision_state_change(
    *,
    tenant_id: str,
    repository_id: str,
    memory_id: str,
    revision_id: str,
    activation_id: str | None,
    change: MemoryStateChangeKind,
    replacement_revision_id: str | None,
    changed_by: str,
    changed_at: str,
    reason_sha256: str,
    change_attestation_sha256: str,
) -> MemoryRevisionStateChange:
    values = {
        "contract_version": "tbm.memory-revision-state-change.v1",
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "memory_id": memory_id,
        "revision_id": revision_id,
        "activation_id": activation_id,
        "change": change,
        "replacement_revision_id": replacement_revision_id,
        "changed_by": changed_by,
        "changed_at": _timestamp(changed_at, "changed_at"),
        "reason_sha256": reason_sha256,
        "change_attestation_sha256": change_attestation_sha256,
    }
    return MemoryRevisionStateChange(
        state_change_id=_derived_id("memory_state_change_sha256_", values),
        tenant_id=tenant_id,
        repository_id=repository_id,
        memory_id=memory_id,
        revision_id=revision_id,
        activation_id=activation_id,
        change=change,
        replacement_revision_id=replacement_revision_id,
        changed_by=changed_by,
        changed_at=cast(str, values["changed_at"]),
        reason_sha256=reason_sha256,
        change_attestation_sha256=change_attestation_sha256,
    )


@dataclass(frozen=True)
class MemoryRevisionRelationship:
    relationship_id: str
    tenant_id: str
    repository_id: str
    memory_id: str
    from_revision_id: str
    to_revision_id: str
    relationship: MemoryRelationshipKind
    recorded_by: str
    recorded_at: str
    evidence_sha256: str
    relationship_attestation_sha256: str
    contract_version: str = "tbm.memory-revision-relationship.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.memory-revision-relationship.v1":
            _record_invalid("relationship contract_version is unsupported")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.memory_id, "memory_id"),
            (self.from_revision_id, "from_revision_id"),
            (self.to_revision_id, "to_revision_id"),
            (self.recorded_by, "recorded_by"),
        ):
            _identifier(value, name)
        if self.from_revision_id == self.to_revision_id:
            _record_invalid("a relationship cannot reference one revision twice")
        if self.relationship not in {"supersedes", "related", "contradicts"}:
            _record_invalid("relationship is unsupported")
        object.__setattr__(
            self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
        )
        _digest(self.evidence_sha256, "evidence_sha256")
        _digest(
            self.relationship_attestation_sha256,
            "relationship_attestation_sha256",
        )
        if _RELATIONSHIP_ID_RE.fullmatch(self.relationship_id) is None:
            _record_invalid("relationship_id is invalid")
        if self.relationship_id != _derived_id(
            "memory_relationship_sha256_", self._unsigned_dict()
        ):
            _record_invalid("relationship_id does not match relationship content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "memory_id": self.memory_id,
            "from_revision_id": self.from_revision_id,
            "to_revision_id": self.to_revision_id,
            "relationship": self.relationship,
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at,
            "evidence_sha256": self.evidence_sha256,
            "relationship_attestation_sha256": (
                self.relationship_attestation_sha256
            ),
        }

    def to_dict(self) -> dict[str, object]:
        return {"relationship_id": self.relationship_id, **self._unsigned_dict()}


def build_memory_revision_relationship(
    *,
    tenant_id: str,
    repository_id: str,
    memory_id: str,
    from_revision_id: str,
    to_revision_id: str,
    relationship: MemoryRelationshipKind,
    recorded_by: str,
    recorded_at: str,
    evidence_sha256: str,
    relationship_attestation_sha256: str,
) -> MemoryRevisionRelationship:
    values = {
        "contract_version": "tbm.memory-revision-relationship.v1",
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "memory_id": memory_id,
        "from_revision_id": from_revision_id,
        "to_revision_id": to_revision_id,
        "relationship": relationship,
        "recorded_by": recorded_by,
        "recorded_at": _timestamp(recorded_at, "recorded_at"),
        "evidence_sha256": evidence_sha256,
        "relationship_attestation_sha256": relationship_attestation_sha256,
    }
    return MemoryRevisionRelationship(
        relationship_id=_derived_id("memory_relationship_sha256_", values),
        tenant_id=tenant_id,
        repository_id=repository_id,
        memory_id=memory_id,
        from_revision_id=from_revision_id,
        to_revision_id=to_revision_id,
        relationship=relationship,
        recorded_by=recorded_by,
        recorded_at=cast(str, values["recorded_at"]),
        evidence_sha256=evidence_sha256,
        relationship_attestation_sha256=relationship_attestation_sha256,
    )


@dataclass(frozen=True)
class MemoryRevisionCounterexample:
    counterexample_id: str
    tenant_id: str
    repository_id: str
    memory_id: str
    revision_id: str
    evidence: StructuredRegressionEvidence
    recorded_by: str
    recorded_at: str
    counterexample_attestation_sha256: str
    contract_version: str = "tbm.memory-revision-counterexample.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.memory-revision-counterexample.v1":
            _record_invalid("counterexample contract_version is unsupported")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.memory_id, "memory_id"),
            (self.revision_id, "revision_id"),
            (self.recorded_by, "recorded_by"),
        ):
            _identifier(value, name)
        if type(self.evidence) is not StructuredRegressionEvidence:
            _record_invalid("counterexample evidence must be exact structured evidence")
        if self.evidence.result == "pass":
            _record_invalid("counterexample evidence must fail or error")
        object.__setattr__(
            self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
        )
        _digest(
            self.counterexample_attestation_sha256,
            "counterexample_attestation_sha256",
        )
        if _COUNTEREXAMPLE_ID_RE.fullmatch(self.counterexample_id) is None:
            _record_invalid("counterexample_id is invalid")
        if self.counterexample_id != _derived_id(
            "memory_counterexample_sha256_", self._unsigned_dict()
        ):
            _record_invalid("counterexample_id does not match content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "memory_id": self.memory_id,
            "revision_id": self.revision_id,
            "evidence": self.evidence.to_dict(),
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at,
            "counterexample_attestation_sha256": (
                self.counterexample_attestation_sha256
            ),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "counterexample_id": self.counterexample_id,
            **self._unsigned_dict(),
        }


def build_memory_revision_counterexample(
    *,
    tenant_id: str,
    repository_id: str,
    memory_id: str,
    revision_id: str,
    evidence: StructuredRegressionEvidence,
    recorded_by: str,
    recorded_at: str,
    counterexample_attestation_sha256: str,
) -> MemoryRevisionCounterexample:
    if type(evidence) is not StructuredRegressionEvidence:
        _record_invalid("counterexample evidence must be exact structured evidence")
    values = {
        "contract_version": "tbm.memory-revision-counterexample.v1",
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "memory_id": memory_id,
        "revision_id": revision_id,
        "evidence": evidence.to_dict(),
        "recorded_by": recorded_by,
        "recorded_at": _timestamp(recorded_at, "recorded_at"),
        "counterexample_attestation_sha256": (
            counterexample_attestation_sha256
        ),
    }
    return MemoryRevisionCounterexample(
        counterexample_id=_derived_id("memory_counterexample_sha256_", values),
        tenant_id=tenant_id,
        repository_id=repository_id,
        memory_id=memory_id,
        revision_id=revision_id,
        evidence=evidence,
        recorded_by=recorded_by,
        recorded_at=cast(str, values["recorded_at"]),
        counterexample_attestation_sha256=(
            counterexample_attestation_sha256
        ),
    )


MemoryCatalogRecord = (
    MemoryRevision
    | MemoryRevisionReview
    | MemoryCatalogEvidenceRecord
    | StoredMemoryRevisionApprovalPublication
    | StoredMemoryRevisionActivationPublication
    | MemoryRevisionStateChange
    | MemoryRevisionRelationship
    | MemoryRevisionCounterexample
)


def memory_catalog_stream_id(memory_id: str) -> str:
    _identifier(memory_id, "memory_id")
    digest = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()
    return "memory_catalog_" + digest


def build_memory_catalog_event_batch(
    access: LedgerAccessContext,
    records: tuple[MemoryCatalogRecord, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if type(access) is not LedgerAccessContext:
        _fail("TBM_MEMORY_CATALOG_EVENT_ACCESS_INVALID", "access is invalid")
    if (
        type(records) is not tuple
        or not 1 <= len(records) <= MEMORY_CATALOG_EVENT_MAX_BATCH
    ):
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_BATCH_INVALID",
            "records must be a bounded non-empty tuple",
        )
    if type(expected_stream_version) is not int or expected_stream_version < 0:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_BATCH_INVALID",
            "expected_stream_version is invalid",
        )
    if type(next_global_position) is not int or next_global_position < 1:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_BATCH_INVALID",
            "next_global_position is invalid",
        )
    canonical_recorded_at = _timestamp(recorded_at, "recorded_at")
    descriptors = tuple(_record_descriptor(record) for record in records)
    memory_id = descriptors[0][1]
    if any(item[1] != memory_id for item in descriptors):
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_BATCH_INVALID",
            "records must belong to one memory stream",
        )
    stream_id = memory_catalog_stream_id(memory_id)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_MEMORY_CATALOG_EVENT_BATCH_INVALID",
                "a nonzero stream version requires its parent event",
            )
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_BATCH_INVALID",
            "previous event does not match the expected stream head",
        )
    command_value = {
        "protocol_version": MEMORY_CATALOG_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "records": [item[4] for item in descriptors],
    }
    command_sha256 = _domain_sha256(
        b"tbm.memory-catalog-event-command.v1\x00", command_value
    )
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.memory-catalog-event-idempotency.v1\x00", command_value
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    parent = previous_event
    events: list[CanonicalEvent] = []
    trusted_context = access.event_trusted_context()
    for offset, descriptor in enumerate(descriptors):
        (
            event_type,
            _,
            revision_id,
            occurred_at,
            record_dict,
            actor_id,
            tenant_id,
            repository_id,
        ) = descriptor
        _verify_record_access(
            access,
            tenant_id=tenant_id,
            repository_id=repository_id,
            actor_id=actor_id,
        )
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        record_json = _canonical_json(record_dict)
        payload = {
            "memory_id": memory_id,
            "revision_id": revision_id,
            "record_type": event_type,
            "record_sha256": canonical_sha256(record_dict),
            "record_json": record_json,
        }
        event = build_canonical_event(
            event_id="evt_mc_" + event_digest,
            event_type=event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=MEMORY_CATALOG_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_mc_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_mc_" + stream_id[-32:],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=occurred_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_memory_catalog_runtime",
            producer_version="f4-v1",
            payload_schema=_PAYLOAD_SCHEMAS[event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification="internal",
            retention_policy_id="retention_memory_catalog_events",
            artifact_refs=(),
            payload=payload,
        )
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_memory_catalog_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    schemas = _payload_json_schemas()
    for event_type in MEMORY_CATALOG_EVENT_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="domain",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=schemas[event_type],
            )
        )
    return registry.seal()


def memory_catalog_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_memory_catalog_event_registry().dispatch_schema()
    schema["$id"] = MEMORY_CATALOG_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory MemoryCatalog event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed MemoryCatalog event registry; exact domain "
        "record parsers remain authoritative during replay."
    )
    return schema


def dumps_memory_catalog_event_payload_dispatch_schema() -> str:
    return json.dumps(
        memory_catalog_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


@dataclass(frozen=True)
class ActivatedMemoryHead:
    tenant_id: str
    repository_id: str
    memory_id: str
    current_revision_number: int
    current_revision_id: str
    current_approval_id: str
    current_activation_id: str
    applicability_sha256: str
    content_artifact_id: str
    content_sha256: str
    evidence_bundle_sha256: str
    approval_authorization_event_id: str
    activation_authorization_event_id: str
    approval_attestation_verified_by: str
    activation_attestation_verified_by: str
    activated_at: str
    source_event_sha256: str
    head_sha256: str
    contract_version: str = "tbm.activated-memory-head.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.activated-memory-head.v1":
            _projection_invalid("ActivatedMemoryHead contract_version is unsupported")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.memory_id, "memory_id"),
            (self.current_revision_id, "current_revision_id"),
            (self.current_approval_id, "current_approval_id"),
            (self.current_activation_id, "current_activation_id"),
            (self.content_artifact_id, "content_artifact_id"),
            (
                self.approval_authorization_event_id,
                "approval_authorization_event_id",
            ),
            (
                self.activation_authorization_event_id,
                "activation_authorization_event_id",
            ),
            (
                self.approval_attestation_verified_by,
                "approval_attestation_verified_by",
            ),
            (
                self.activation_attestation_verified_by,
                "activation_attestation_verified_by",
            ),
        ):
            _identifier(value, name)
        for value, name in (
            (self.applicability_sha256, "applicability_sha256"),
            (self.content_sha256, "content_sha256"),
            (self.evidence_bundle_sha256, "evidence_bundle_sha256"),
        ):
            _digest(value, name)
        if (
            type(self.current_revision_number) is not int
            or self.current_revision_number < 1
        ):
            _projection_invalid("current_revision_number is invalid")
        object.__setattr__(
            self, "activated_at", _timestamp(self.activated_at, "activated_at")
        )
        _digest(self.source_event_sha256, "source_event_sha256")
        _digest(self.head_sha256, "head_sha256")
        if self.head_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("head_sha256 does not match ActivatedMemoryHead")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "memory_id": self.memory_id,
            "current_revision_number": self.current_revision_number,
            "current_revision_id": self.current_revision_id,
            "current_approval_id": self.current_approval_id,
            "current_activation_id": self.current_activation_id,
            "applicability_sha256": self.applicability_sha256,
            "content_artifact_id": self.content_artifact_id,
            "content_sha256": self.content_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "approval_authorization_event_id": (
                self.approval_authorization_event_id
            ),
            "activation_authorization_event_id": (
                self.activation_authorization_event_id
            ),
            "approval_attestation_verified_by": (
                self.approval_attestation_verified_by
            ),
            "activation_attestation_verified_by": (
                self.activation_attestation_verified_by
            ),
            "activated_at": self.activated_at,
            "source_event_sha256": self.source_event_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {"head_sha256": self.head_sha256, **self._unsigned_dict()}


@dataclass(frozen=True)
class MemoryCatalogRevisionView:
    revision: MemoryRevision
    status: MemoryCatalogRevisionStatus
    review: MemoryRevisionReview | None
    fix_evidence: FixEvidence | None
    regression_evidence: tuple[StructuredRegressionEvidence, ...]
    approval: MemoryRevisionApproval | None
    activation: MemoryRevisionActivation | None
    activation_event_sha256: str | None
    state_changes: tuple[MemoryRevisionStateChange, ...]
    relationships: tuple[MemoryRevisionRelationship, ...]
    counterexamples: tuple[MemoryRevisionCounterexample, ...]

    def __post_init__(self) -> None:
        if type(self.revision) is not MemoryRevision:
            _projection_invalid("revision view requires exact MemoryRevision")
        if self.status not in {
            "proposed",
            "reviewed",
            "rejected",
            "approved",
            "active",
            "suspended",
            "superseded",
            "obsoleted",
        }:
            _projection_invalid("revision view status is unsupported")
        if self.review is not None and type(self.review) is not MemoryRevisionReview:
            _projection_invalid("revision review is invalid")
        if self.fix_evidence is not None and type(self.fix_evidence) is not FixEvidence:
            _projection_invalid("revision fix evidence is invalid")
        if type(self.regression_evidence) is not tuple or any(
            type(item) is not StructuredRegressionEvidence
            for item in self.regression_evidence
        ):
            _projection_invalid("revision regression evidence is invalid")
        if self.approval is not None and type(self.approval) is not MemoryRevisionApproval:
            _projection_invalid("revision approval is invalid")
        if self.activation is not None and type(self.activation) is not MemoryRevisionActivation:
            _projection_invalid("revision activation is invalid")
        if self.activation_event_sha256 is not None:
            _digest(self.activation_event_sha256, "activation_event_sha256")
        if (self.activation is None) != (self.activation_event_sha256 is None):
            _projection_invalid("activation record and source event must coexist")
        if type(self.state_changes) is not tuple or any(
            type(item) is not MemoryRevisionStateChange for item in self.state_changes
        ):
            _projection_invalid("revision state changes are invalid")
        if type(self.relationships) is not tuple or any(
            type(item) is not MemoryRevisionRelationship for item in self.relationships
        ):
            _projection_invalid("revision relationships are invalid")
        if type(self.counterexamples) is not tuple or any(
            type(item) is not MemoryRevisionCounterexample
            for item in self.counterexamples
        ):
            _projection_invalid("revision counterexamples are invalid")

    @property
    def eligible_for_retrieval(self) -> bool:
        return self.status == "active" and self.activation is not None


@dataclass(frozen=True)
class MemoryCatalogProjection:
    tenant_id: str
    repository_id: str
    memory_id: str
    revisions: tuple[MemoryCatalogRevisionView, ...]
    activated_head: ActivatedMemoryHead | None
    last_event_sha256: str | None
    last_global_position: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.memory_id, "memory_id"),
        ):
            _identifier(value, name)
        if type(self.revisions) is not tuple or any(
            type(item) is not MemoryCatalogRevisionView for item in self.revisions
        ):
            _projection_invalid("catalog revisions are invalid")
        numbers = tuple(item.revision.revision_number for item in self.revisions)
        if numbers != tuple(sorted(numbers)) or len(numbers) != len(set(numbers)):
            _projection_invalid("catalog revision numbers are not canonical")
        if any(
            item.revision.memory_id != self.memory_id
            or item.revision.scope.tenant_id != self.tenant_id
            or item.revision.scope.repository_id != self.repository_id
            for item in self.revisions
        ):
            _projection_invalid("catalog revisions cross the projection partition")
        if self.activated_head is not None:
            if type(self.activated_head) is not ActivatedMemoryHead:
                _projection_invalid("catalog activated head is invalid")
            matches = [
                item
                for item in self.revisions
                if item.revision.revision_id
                == self.activated_head.current_revision_id
            ]
            if len(matches) != 1 or not matches[0].eligible_for_retrieval:
                _projection_invalid("catalog activated head is not an active revision")
            if (
                matches[0].activation_event_sha256
                != self.activated_head.source_event_sha256
            ):
                _projection_invalid("catalog head is not bound to activation event")
            if (
                self.activated_head.tenant_id != self.tenant_id
                or self.activated_head.repository_id != self.repository_id
                or self.activated_head.memory_id != self.memory_id
            ):
                _projection_invalid("catalog head crosses the projection partition")
        if self.last_event_sha256 is not None:
            _digest(self.last_event_sha256, "last_event_sha256")
        if type(self.last_global_position) is not int or self.last_global_position < 0:
            _projection_invalid("last_global_position is invalid")

    def get_revision(self, revision_id: str) -> MemoryCatalogRevisionView:
        matches = [
            item for item in self.revisions if item.revision.revision_id == revision_id
        ]
        if len(matches) != 1:
            _fail(
                "TBM_MEMORY_CATALOG_REVISION_NOT_FOUND",
                "memory revision is not present in the catalog",
            )
        return matches[0]


@dataclass(frozen=True)
class MemoryCatalog:
    projections: tuple[MemoryCatalogProjection, ...]

    def __post_init__(self) -> None:
        if type(self.projections) is not tuple or any(
            type(item) is not MemoryCatalogProjection for item in self.projections
        ):
            _projection_invalid("MemoryCatalog projections are invalid")
        keys = tuple(
            (item.tenant_id, item.repository_id, item.memory_id)
            for item in self.projections
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            _projection_invalid("MemoryCatalog projection keys are not canonical")

    def load_head(
        self, *, tenant_id: str, repository_id: str, memory_id: str
    ) -> ActivatedMemoryHead:
        matches = [
            item
            for item in self.projections
            if (
                item.tenant_id,
                item.repository_id,
                item.memory_id,
            )
            == (tenant_id, repository_id, memory_id)
        ]
        if len(matches) != 1 or matches[0].activated_head is None:
            _fail(
                "TBM_MEMORY_CATALOG_HEAD_NOT_FOUND",
                "activated memory head is not present",
            )
        return cast(ActivatedMemoryHead, matches[0].activated_head)

    def verify_head(self, head: ActivatedMemoryHead) -> None:
        if type(head) is not ActivatedMemoryHead:
            _projection_invalid("head verification requires ActivatedMemoryHead")
        actual = self.load_head(
            tenant_id=head.tenant_id,
            repository_id=head.repository_id,
            memory_id=head.memory_id,
        )
        if actual != head:
            _fail(
                "TBM_MEMORY_CATALOG_HEAD_STALE",
                "ActivatedMemoryHead differs from event-rebuilt catalog",
            )


@dataclass(frozen=True)
class MemoryCatalogAppendResult:
    receipt: LedgerAppendReceipt
    projection: MemoryCatalogProjection

    def __post_init__(self) -> None:
        if type(self.receipt) is not LedgerAppendReceipt:
            _projection_invalid("MemoryCatalog append receipt is invalid")
        if type(self.projection) is not MemoryCatalogProjection:
            _projection_invalid("MemoryCatalog append projection is invalid")


@dataclass(frozen=True)
class DurableMemoryCatalogSnapshot:
    catalog: MemoryCatalog
    partition_sha256: str
    reducer_descriptor_sha256: str
    reducer_configuration_sha256: str
    event_high_watermark: int
    source_event_count: int
    snapshot_sha256: str
    contract_version: str = "tbm.durable-memory-catalog-snapshot.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.durable-memory-catalog-snapshot.v1":
            _projection_invalid("durable catalog snapshot version is unsupported")
        if type(self.catalog) is not MemoryCatalog:
            _projection_invalid("durable catalog snapshot requires MemoryCatalog")
        _digest(self.partition_sha256, "partition_sha256")
        _digest(self.reducer_descriptor_sha256, "reducer_descriptor_sha256")
        _digest(
            self.reducer_configuration_sha256,
            "reducer_configuration_sha256",
        )
        if type(self.event_high_watermark) is not int or self.event_high_watermark < 0:
            _projection_invalid("event_high_watermark is invalid")
        if type(self.source_event_count) is not int or not (
            0 <= self.source_event_count <= MEMORY_CATALOG_EVENT_MAX_REBUILD_EVENTS
        ):
            _projection_invalid("source_event_count is invalid")
        _digest(self.snapshot_sha256, "snapshot_sha256")
        if self.snapshot_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("durable catalog snapshot digest does not match")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "partition_sha256": self.partition_sha256,
            "reducer_descriptor_sha256": self.reducer_descriptor_sha256,
            "reducer_configuration_sha256": (
                self.reducer_configuration_sha256
            ),
            "event_high_watermark": self.event_high_watermark,
            "source_event_count": self.source_event_count,
            "catalog": _memory_catalog_digest_value(self.catalog),
        }

    def load_head(
        self, *, tenant_id: str, repository_id: str, memory_id: str
    ) -> ActivatedMemoryHead:
        return self.catalog.load_head(
            tenant_id=tenant_id,
            repository_id=repository_id,
            memory_id=memory_id,
        )

    def verify_head(self, head: ActivatedMemoryHead) -> None:
        self.catalog.verify_head(head)


def build_memory_catalog_reducer(
    *, trusted_attestation_verifier_ids: tuple[str, ...]
) -> FunctionalReducer:
    trusted_verifiers = _trusted_verifier_set(
        trusted_attestation_verifier_ids
    )
    descriptor = ReducerDescriptor(
        reducer_id=MEMORY_CATALOG_EVENT_REDUCER_ID,
        reducer_version=1,
        input_event_types=MEMORY_CATALOG_EVENT_TYPES,
        output_projection=MEMORY_CATALOG_EVENT_PROJECTION,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "memory-catalog-current",
                "algorithm_version": 1,
                "event_types": list(MEMORY_CATALOG_EVENT_TYPES),
                "head_source": "revision_activated",
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {
                "configuration": "trusted-attestation-verifiers",
                "trusted_attestation_verifier_ids": sorted(
                    trusted_verifiers
                ),
                "version": 1,
            },
        ),
        target_event_versions={
            event_type: 1 for event_type in MEMORY_CATALOG_EVENT_TYPES
        },
    )

    def initial() -> Mapping[str, object]:
        return {
            "tenant_id": None,
            "repository_id": None,
            "memory_id": None,
            "revisions": {},
            "head": None,
            "relationships": [],
            "last_event_sha256": None,
            "last_global_position": 0,
        }

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        payload = _typed_payload(reducer_event)
        event = reducer_event.source_event
        memory_id = cast(str, payload["memory_id"])
        current_memory_id = state.get("memory_id")
        if current_memory_id is not None and current_memory_id != memory_id:
            _transition_invalid("MemoryCatalog stream contains another memory")
        if state.get("tenant_id") not in {None, event.tenant_id} or state.get(
            "repository_id"
        ) not in {None, event.repository_id}:
            _transition_invalid("MemoryCatalog stream crossed a ledger partition")
        revisions = _state_mapping(state, "revisions")
        relationships = _state_list(state, "relationships")
        head = _optional_state_mapping(state.get("head"), "head")
        record = _load_record(event.event_type, cast(str, payload["record_json"]))
        _verify_loaded_record(payload, record, event)
        if event.event_type == MEMORY_REVISION_PROPOSED:
            revision = cast(MemoryRevision, record)
            if revision.revision_id in revisions:
                _transition_invalid("MemoryRevision proposal is duplicated")
            if revision.revision_number == 1:
                if revisions:
                    _transition_invalid("first MemoryRevision must begin an empty stream")
            else:
                previous = revisions.get(cast(str, revision.previous_revision_id))
                if previous is None:
                    _transition_invalid("MemoryRevision predecessor is missing")
                previous_revision = loads_memory_revision(
                    cast(str, previous["revision_json"])
                )
                if (
                    previous_revision.memory_id != revision.memory_id
                    or previous_revision.revision_number + 1
                    != revision.revision_number
                ):
                    _transition_invalid("MemoryRevision lineage is not contiguous")
            revisions[revision.revision_id] = _new_revision_state(revision)
        elif event.event_type in {
            MEMORY_REVISION_REVIEWED,
            MEMORY_REVISION_REJECTED,
        }:
            review = cast(MemoryRevisionReview, record)
            item = _revision_state(revisions, review.revision_id)
            revision = loads_memory_revision(cast(str, item["revision_json"]))
            if item.get("status") != "proposed" or item.get("review_json") is not None:
                _transition_invalid("MemoryRevision review transition is invalid")
            if review.reviewed_by == revision.proposed_by:
                _transition_invalid("MemoryRevision proposer cannot review the revision")
            if parse_rfc3339(review.reviewed_at) < parse_rfc3339(revision.proposed_at):
                _transition_invalid("MemoryRevision review precedes the proposal")
            item["review_json"] = _canonical_json(review.to_dict())
            item["status"] = (
                "reviewed" if review.decision == "accepted" else "rejected"
            )
        elif event.event_type in {
            MEMORY_FIX_EVIDENCE_RECORDED,
            MEMORY_REGRESSION_EVIDENCE_RECORDED,
        }:
            evidence_record = cast(MemoryCatalogEvidenceRecord, record)
            item = _revision_state(revisions, evidence_record.revision_id)
            revision = loads_memory_revision(cast(str, item["revision_json"]))
            if revision.memory_kind != "lesson":
                _transition_invalid("project policy revisions forbid regression evidence")
            if evidence_record.evidence_kind == "fix":
                evidence = cast(FixEvidence, evidence_record.evidence)
                if item.get("fix_evidence_json") is not None:
                    _transition_invalid("MemoryRevision fix evidence is duplicated")
                if evidence.evidence_id != revision.fix_evidence_id:
                    _transition_invalid("fix evidence is not referenced by revision")
                item["fix_evidence_json"] = _canonical_json(evidence.to_dict())
            else:
                evidence = cast(StructuredRegressionEvidence, evidence_record.evidence)
                evidence_items = _state_list(item, "regression_evidence_json")
                if evidence.evidence_id not in revision.regression_evidence_ids:
                    _transition_invalid("regression evidence is not referenced by revision")
                if any(
                    loads_structured_regression_evidence(value).evidence_id
                    == evidence.evidence_id
                    for value in evidence_items
                    if type(value) is str
                ):
                    _transition_invalid("regression evidence is duplicated")
                evidence_items.append(_canonical_json(evidence.to_dict()))
                item["regression_evidence_json"] = evidence_items
        elif event.event_type == MEMORY_REVISION_APPROVED:
            approval_record = cast(
                StoredMemoryRevisionApprovalPublication, record
            )
            approval = approval_record.approval
            if approval_record.attestation_verified_by not in trusted_verifiers:
                _transition_invalid("approval attestation verifier is not trusted")
            item = _revision_state(revisions, approval.revision_id)
            revision = loads_memory_revision(cast(str, item["revision_json"]))
            if item.get("status") != "reviewed" or item.get("approval_json") is not None:
                _transition_invalid("MemoryRevision approval transition is invalid")
            review_json = item.get("review_json")
            if type(review_json) is not str:
                _transition_invalid("MemoryRevision approval lacks accepted review")
            review = loads_memory_revision_review(review_json)
            _verify_approval_links(revision, approval_record, event)
            if parse_rfc3339(approval.approved_at) < parse_rfc3339(
                review.reviewed_at
            ):
                _transition_invalid("MemoryRevision approval precedes review")
            _verify_reducer_evidence_bundle(revision, item)
            if approval.evidence_bundle_sha256 != _evidence_bundle_sha256(
                revision, item
            ):
                _transition_invalid("approval evidence digest does not match replay")
            item["approval_json"] = _canonical_json(approval.to_dict())
            item["approval_publication_json"] = _canonical_json(
                _stored_approval_dict(approval_record)
            )
            item["status"] = "approved"
        elif event.event_type == MEMORY_REVISION_ACTIVATED:
            activation_record = cast(
                StoredMemoryRevisionActivationPublication, record
            )
            activation = activation_record.activation
            if activation_record.attestation_verified_by not in trusted_verifiers:
                _transition_invalid("activation attestation verifier is not trusted")
            item = _revision_state(revisions, activation.revision_id)
            revision = loads_memory_revision(cast(str, item["revision_json"]))
            approval_json = item.get("approval_json")
            if item.get("status") != "approved" or type(approval_json) is not str:
                _transition_invalid("MemoryRevision activation transition is invalid")
            approval = loads_memory_revision_approval(approval_json)
            _verify_activation_links(
                revision, approval, activation_record, event
            )
            if activation.activated_by in {
                revision.proposed_by,
                approval.approved_by,
            }:
                _transition_invalid("activator is not independent")
            if head is not None:
                _transition_invalid("a prior ActivatedMemoryHead must be superseded first")
            if revision.revision_number > 1:
                previous = _revision_state(
                    revisions, cast(str, revision.previous_revision_id)
                )
                previous_activation_json = previous.get("activation_json")
                if (
                    previous.get("status") != "superseded"
                    or type(previous_activation_json) is not str
                    or loads_memory_revision_activation(
                        previous_activation_json
                    ).activation_id
                    != activation.previous_activation_id
                ):
                    _transition_invalid("activation predecessor is not superseded")
            item["activation_json"] = _canonical_json(activation.to_dict())
            item["activation_event_sha256"] = event.event_sha256
            item["activation_publication_json"] = _canonical_json(
                _stored_activation_dict(activation_record)
            )
            item["status"] = "active"
            approval_publication_json = item.get("approval_publication_json")
            if type(approval_publication_json) is not str:
                _transition_invalid("activation lacks exact approval publication")
            approval_record = loads_memory_catalog_approval_publication(
                approval_publication_json
            )
            head = _head_state(
                revision,
                approval,
                activation,
                event,
                approval_attestation_verified_by=(
                    approval_record.attestation_verified_by
                ),
                activation_attestation_verified_by=(
                    activation_record.attestation_verified_by
                ),
            )
        elif event.event_type in {
            MEMORY_REVISION_SUSPENDED,
            MEMORY_REVISION_SUPERSEDED,
            MEMORY_REVISION_OBSOLETED,
        }:
            change = cast(MemoryRevisionStateChange, record)
            item = _revision_state(revisions, change.revision_id)
            status = cast(str, item.get("status"))
            if status == "obsoleted":
                _transition_invalid("obsoleted MemoryRevision is terminal")
            activation_json = item.get("activation_json")
            if change.activation_id is not None and (
                type(activation_json) is not str
                or loads_memory_revision_activation(activation_json).activation_id
                != change.activation_id
            ):
                _transition_invalid("state change activation does not match revision")
            if parse_rfc3339(change.changed_at) < parse_rfc3339(
                _latest_revision_time(item)
            ):
                _transition_invalid("state change precedes revision lifecycle")
            if change.change in {"suspended", "superseded"}:
                if status != "active" or head is None:
                    _transition_invalid("state change requires current active revision")
                if head.get("current_revision_id") != change.revision_id:
                    _transition_invalid("state change does not target current head")
                if change.change == "superseded":
                    replacement = _revision_state(
                        revisions, cast(str, change.replacement_revision_id)
                    )
                    replacement_revision = loads_memory_revision(
                        cast(str, replacement["revision_json"])
                    )
                    if replacement_revision.previous_revision_id != change.revision_id:
                        _transition_invalid("superseding revision does not follow current head")
                    if not _has_supersedes_relationship(
                        relationships,
                        from_revision_id=replacement_revision.revision_id,
                        to_revision_id=change.revision_id,
                    ):
                        _transition_invalid("superseded transition lacks relationship evidence")
                head = None
            elif status == "active":
                if head is None or head.get("current_revision_id") != change.revision_id:
                    _transition_invalid("obsolescence does not target current head")
                head = None
            item["status"] = change.change
            changes = _state_list(item, "state_change_json")
            changes.append(_canonical_json(change.to_dict()))
            item["state_change_json"] = changes
        elif event.event_type == MEMORY_RELATIONSHIP_RECORDED:
            relationship = cast(MemoryRevisionRelationship, record)
            source = _revision_state(revisions, relationship.from_revision_id)
            target = _revision_state(revisions, relationship.to_revision_id)
            if relationship.relationship == "supersedes":
                source_revision = loads_memory_revision(
                    cast(str, source["revision_json"])
                )
                if source_revision.previous_revision_id != relationship.to_revision_id:
                    _transition_invalid("supersedes relationship contradicts lineage")
            source_revision = loads_memory_revision(
                cast(str, source["revision_json"])
            )
            target_revision = loads_memory_revision(
                cast(str, target["revision_json"])
            )
            if parse_rfc3339(relationship.recorded_at) < max(
                parse_rfc3339(source_revision.proposed_at),
                parse_rfc3339(target_revision.proposed_at),
            ):
                _transition_invalid("relationship precedes a referenced revision")
            if any(
                loads_memory_revision_relationship(value).relationship_id
                == relationship.relationship_id
                for value in relationships
                if type(value) is str
            ):
                _transition_invalid("MemoryRevision relationship is duplicated")
            relationships.append(_canonical_json(relationship.to_dict()))
            for item in (source, target):
                item_relationships = _state_list(item, "relationship_json")
                item_relationships.append(_canonical_json(relationship.to_dict()))
                item["relationship_json"] = item_relationships
        elif event.event_type == MEMORY_COUNTEREXAMPLE_RECORDED:
            counterexample = cast(MemoryRevisionCounterexample, record)
            item = _revision_state(revisions, counterexample.revision_id)
            revision = loads_memory_revision(cast(str, item["revision_json"]))
            if (
                revision.memory_kind != "lesson"
                or counterexample.evidence.case_id != revision.source_case_id
            ):
                _transition_invalid("counterexample does not match lesson source case")
            if parse_rfc3339(counterexample.recorded_at) < max(
                parse_rfc3339(revision.proposed_at),
                parse_rfc3339(counterexample.evidence.verified_at),
            ):
                _transition_invalid("counterexample precedes its revision or evidence")
            counterexamples = _state_list(item, "counterexample_json")
            if any(
                loads_memory_revision_counterexample(value).counterexample_id
                == counterexample.counterexample_id
                for value in counterexamples
                if type(value) is str
            ):
                _transition_invalid("MemoryRevision counterexample is duplicated")
            counterexamples.append(_canonical_json(counterexample.to_dict()))
            item["counterexample_json"] = counterexamples
        else:  # pragma: no cover - sealed registry and descriptor prevent this
            _transition_invalid("MemoryCatalog event type is unsupported")
        return {
            "tenant_id": event.tenant_id,
            "repository_id": event.repository_id,
            "memory_id": memory_id,
            "revisions": revisions,
            "head": head,
            "relationships": relationships,
            "last_event_sha256": event.event_sha256,
            "last_global_position": event.global_position,
        }

    return FunctionalReducer(descriptor, initial, transition)


def reduce_memory_catalog_events(
    events: tuple[CanonicalEvent, ...],
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
    event_registry: EventTypeRegistry | None = None,
) -> MemoryCatalogProjection:
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_SEQUENCE_INVALID",
            "events must be a tuple of CanonicalEvent values",
        )
    if not events or len(events) > MEMORY_CATALOG_EVENT_MAX_STREAM_EVENTS:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_SEQUENCE_INVALID",
            "MemoryCatalog stream must have a bounded non-empty event set",
        )
    registry = (
        build_memory_catalog_event_registry()
        if event_registry is None
        else event_registry
    )
    if type(registry) is not EventTypeRegistry or not registry.sealed:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_REGISTRY_INVALID",
            "event registry must be a sealed EventTypeRegistry",
        )
    reducer = build_memory_catalog_reducer(
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids
    )
    state = initial_reducer_state(reducer)
    parent: CanonicalEvent | None = None
    stream_id = events[0].stream_id
    for event in events:
        try:
            verify_event_parent(event, parent)
        except ValueError as error:
            raise MemoryCatalogEventV1Error(
                "TBM_MEMORY_CATALOG_EVENT_SEQUENCE_INVALID",
                "MemoryCatalog event chain is invalid",
            ) from error
        if (
            event.stream_type != MEMORY_CATALOG_EVENT_STREAM_TYPE
            or event.stream_id != stream_id
            or event.event_type not in MEMORY_CATALOG_EVENT_TYPES
        ):
            _fail(
                "TBM_MEMORY_CATALOG_EVENT_SEQUENCE_INVALID",
                "MemoryCatalog stream identity is invalid",
            )
        typed = registry.consume(event, target_version=1)
        state = execute_reducer_step(reducer, state.state, ReducerEvent(event, typed))
        parent = event
    return _hydrate_projection(state.state)


def rebuild_memory_catalog(
    event_streams: tuple[tuple[CanonicalEvent, ...], ...],
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
) -> MemoryCatalog:
    if type(event_streams) is not tuple or any(
        type(stream) is not tuple for stream in event_streams
    ):
        _fail(
            "TBM_MEMORY_CATALOG_REBUILD_INVALID",
            "event_streams must be a tuple of event tuples",
        )
    if (
        len(event_streams) > MEMORY_CATALOG_EVENT_MAX_REBUILD_STREAMS
        or sum(len(stream) for stream in event_streams)
        > MEMORY_CATALOG_EVENT_MAX_REBUILD_EVENTS
    ):
        _fail(
            "TBM_MEMORY_CATALOG_REBUILD_INVALID",
            "MemoryCatalog rebuild input exceeds bounded limits",
        )
    projections = tuple(
        sorted(
            (
                reduce_memory_catalog_events(
                    stream,
                    trusted_attestation_verifier_ids=(
                        trusted_attestation_verifier_ids
                    ),
                )
                for stream in event_streams
            ),
            key=lambda item: (item.tenant_id, item.repository_id, item.memory_id),
        )
    )
    return MemoryCatalog(projections)


def append_memory_catalog_records(
    ledger: EventLedgerPort,
    records: tuple[MemoryCatalogRecord, ...],
    *,
    recorded_at: str,
    trusted_attestation_verifier_ids: tuple[str, ...],
) -> MemoryCatalogAppendResult:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global", "verify_stream")
    ):
        _fail(
            "TBM_MEMORY_CATALOG_LEDGER_INVALID",
            "append requires an access-bound EventLedgerPort",
        )
    if type(records) is not tuple or not records:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_BATCH_INVALID",
            "records must be a non-empty tuple",
        )
    descriptor = _record_descriptor(records[0])
    stream_id = memory_catalog_stream_id(descriptor[1])
    retained = _read_memory_catalog_stream(ledger, stream_id)
    verification = ledger.verify_stream(stream_id)
    if (
        not verification.valid
        or verification.verified_stream_version != len(retained)
        or verification.head_event_sha256
        != (None if not retained else retained[-1].event_sha256)
    ):
        _fail(
            "TBM_MEMORY_CATALOG_LEDGER_VERIFICATION_FAILED",
            "retained MemoryCatalog stream failed ledger verification",
        )
    expected_version = len(retained)
    parent = None if not retained else retained[-1]
    events: tuple[CanonicalEvent, ...] | None = None
    idempotency: LedgerIdempotency | None = None
    predicted: MemoryCatalogProjection | None = None
    for attempt in range(8):
        high_watermark = ledger.read_global(
            after_position=0, limit=1
        ).high_watermark_global_position
        events, idempotency = build_memory_catalog_event_batch(
            access,
            records,
            expected_stream_version=expected_version,
            next_global_position=high_watermark + 1,
            previous_event=parent,
            recorded_at=recorded_at,
        )
        predicted = reduce_memory_catalog_events(
            (*retained, *events),
            trusted_attestation_verifier_ids=(
                trusted_attestation_verifier_ids
            ),
        )
        try:
            receipt = ledger.append(
                stream_id, expected_version, events, idempotency
            )
            break
        except EventLedgerConflictError as error:
            if (
                error.code != "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"
                or attempt == 7
            ):
                raise
    else:  # pragma: no cover - bounded loop always breaks or raises
        raise AssertionError("MemoryCatalog append retry loop did not terminate")
    request = LedgerAppendRequest(
        access=access,
        stream_id=stream_id,
        expected_stream_version=expected_version,
        events=cast(tuple[CanonicalEvent, ...], events),
        idempotency=cast(LedgerIdempotency, idempotency),
    )
    verify_ledger_append_receipt(request, receipt)
    rebuilt = reduce_memory_catalog_events(
        _read_memory_catalog_stream(ledger, stream_id),
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
    )
    if rebuilt != predicted:
        _fail(
            "TBM_MEMORY_CATALOG_PROJECTION_MISMATCH",
            "durable MemoryCatalog replay differs from pre-append projection",
        )
    return MemoryCatalogAppendResult(receipt=receipt, projection=rebuilt)


def rebuild_memory_catalog_from_ledger(
    ledger: EventLedgerPort,
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
) -> DurableMemoryCatalogSnapshot:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not callable(
        getattr(ledger, "read_global", None)
    ):
        _fail(
            "TBM_MEMORY_CATALOG_LEDGER_INVALID",
            "rebuild requires an access-bound readable EventLedgerPort",
        )
    cursor = 0
    scanned = 0
    frozen_high_watermark: int | None = None
    streams: dict[str, list[CanonicalEvent]] = {}
    while True:
        page = ledger.read_global(
            after_position=cursor,
            limit=EVENT_LEDGER_MAX_READ_PAGE,
        )
        if frozen_high_watermark is None:
            frozen_high_watermark = page.high_watermark_global_position
        for event in page.events:
            if event.global_position > frozen_high_watermark:
                break
            scanned += 1
            if scanned > MEMORY_CATALOG_EVENT_MAX_REBUILD_SCAN_EVENTS:
                _fail(
                    "TBM_MEMORY_CATALOG_REBUILD_INVALID",
                    "ledger scan exceeds the bounded rebuild limit",
                )
            if event.event_type in MEMORY_CATALOG_EVENT_TYPES:
                streams.setdefault(event.stream_id, []).append(event)
        if (
            not page.has_more
            or page.next_global_position is None
            or page.next_global_position > frozen_high_watermark
        ):
            break
        cursor = page.next_global_position - 1
    event_streams = tuple(
        tuple(items) for _, items in sorted(streams.items())
    )
    catalog = rebuild_memory_catalog(
        event_streams,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
    )
    reducer_descriptor = build_memory_catalog_reducer(
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids
    ).descriptor
    values = {
        "contract_version": "tbm.durable-memory-catalog-snapshot.v1",
        "partition_sha256": access.partition.partition_sha256,
        "reducer_descriptor_sha256": reducer_descriptor.descriptor_sha256,
        "reducer_configuration_sha256": (
            reducer_descriptor.configuration_sha256
        ),
        "event_high_watermark": 0
        if frozen_high_watermark is None
        else frozen_high_watermark,
        "source_event_count": sum(len(items) for items in event_streams),
        "catalog": _memory_catalog_digest_value(catalog),
    }
    return DurableMemoryCatalogSnapshot(
        catalog=catalog,
        partition_sha256=access.partition.partition_sha256,
        reducer_descriptor_sha256=reducer_descriptor.descriptor_sha256,
        reducer_configuration_sha256=(
            reducer_descriptor.configuration_sha256
        ),
        event_high_watermark=cast(int, values["event_high_watermark"]),
        source_event_count=cast(int, values["source_event_count"]),
        snapshot_sha256=canonical_sha256(values),
    )


class ActivatedMemoryHeadReader(Protocol):
    def load_head(
        self, *, tenant_id: str, repository_id: str, memory_id: str
    ) -> ActivatedMemoryHead: ...

    def verify_head(self, head: ActivatedMemoryHead) -> None: ...


class EventActivatedMemoryHeadSource:
    """Require an event-projected head around exact v3 candidate verification."""

    def __init__(
        self,
        *,
        head_reader: ActivatedMemoryHeadReader,
        verified_source: ActivatedRevisionRetrievalSource,
    ) -> None:
        if not callable(getattr(head_reader, "load_head", None)) or not callable(
            getattr(head_reader, "verify_head", None)
        ):
            raise TypeError("head_reader must provide load_head and verify_head")
        if not callable(getattr(verified_source, "load_authorized", None)) or not callable(
            getattr(verified_source, "verify_current", None)
        ):
            raise TypeError("verified_source must implement activated retrieval")
        self._head_reader = head_reader
        self._verified_source = verified_source

    def load_authorized(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        *,
        memory_id: str,
    ) -> ActivatedRevisionCandidate:
        if type(context) is not AuthenticatedServiceContext or type(
            scope
        ) is not AuthorizedRetrievalScope:
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_INPUT_INVALID",
                "authenticated retrieval input is invalid",
            )
        if not _IDENTIFIER_RE.fullmatch(memory_id):
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_INPUT_INVALID",
                "memory_id is invalid",
            )
        before = self._load_head(scope, memory_id)
        candidate = self._verified_source.load_authorized(
            context, scope, memory_id=memory_id
        )
        self._verify_candidate_head(scope, candidate, before)
        after = self._load_head(scope, memory_id)
        if after != before:
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_STALE",
                "ActivatedMemoryHead changed during candidate verification",
            )
        return candidate

    def verify_current(
        self,
        scope: AuthorizedRetrievalScope,
        candidate: ActivatedRevisionCandidate,
    ) -> None:
        if type(scope) is not AuthorizedRetrievalScope or type(
            candidate
        ) is not ActivatedRevisionCandidate:
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_INPUT_INVALID",
                "current-head verification input is invalid",
            )
        before = self._load_head(scope, candidate.revision.memory_id)
        self._verify_candidate_head(scope, candidate, before)
        self._verified_source.verify_current(scope, candidate)
        after = self._load_head(scope, candidate.revision.memory_id)
        if after != before:
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_STALE",
                "ActivatedMemoryHead changed during current verification",
            )

    def _load_head(
        self, scope: AuthorizedRetrievalScope, memory_id: str
    ) -> ActivatedMemoryHead:
        try:
            head = self._head_reader.load_head(
                tenant_id=scope.tenant_id,
                repository_id=scope.repository_id,
                memory_id=memory_id,
            )
        except ActivatedRevisionV3Error:
            raise
        except Exception:
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_HEAD_UNAVAILABLE",
                "ActivatedMemoryHead could not be loaded",
            )
        if type(head) is not ActivatedMemoryHead:
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_HEAD_INVALID",
                "ActivatedMemoryHead reader returned invalid data",
            )
        try:
            self._head_reader.verify_head(head)
        except ActivatedRevisionV3Error:
            raise
        except Exception:
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_HEAD_INVALID",
                "ActivatedMemoryHead failed event-source verification",
            )
        return head

    @staticmethod
    def _verify_candidate_head(
        scope: AuthorizedRetrievalScope,
        candidate: ActivatedRevisionCandidate,
        head: ActivatedMemoryHead,
    ) -> None:
        revision = candidate.revision
        if (
            head.tenant_id != scope.tenant_id
            or head.repository_id != scope.repository_id
            or head.memory_id != revision.memory_id
            or head.current_revision_id != revision.revision_id
            or head.current_revision_number != revision.revision_number
            or head.current_approval_id != candidate.approval.approval_id
            or head.current_activation_id != candidate.activation.activation_id
            or head.applicability_sha256
            != canonical_sha256(revision.scope.to_dict())
            or head.content_artifact_id
            != revision.content_artifact.artifact_id
            or head.content_sha256 != revision.content_artifact.content_sha256
            or head.evidence_bundle_sha256
            != candidate.approval.evidence_bundle_sha256
            or head.approval_authorization_event_id
            != candidate.approval.authorization_event_id
            or head.activation_authorization_event_id
            != candidate.activation.authorization_event_id
            or head.approval_attestation_verified_by
            != candidate.approval_attestation_verified_by
            or head.activation_attestation_verified_by
            != candidate.activation_attestation_verified_by
            or head.activated_at != candidate.activation.activated_at
            or revision.scope.tenant_id != scope.tenant_id
            or revision.scope.repository_id != scope.repository_id
        ):
            _source_reject(
                "TBM_MEMORY_CATALOG_SOURCE_HEAD_MISMATCH",
                "candidate does not match the formal ActivatedMemoryHead",
            )


@dataclass(frozen=True)
class LegacyLessonCompatibilityProjection:
    """Explicit, non-authoritative view for compatibility Lesson records."""

    tenant_id: str
    repository_id: str
    lesson_id: str
    source_case_id: str
    lesson_text: str
    memory_type: str
    scope: tuple[tuple[str, str], ...]
    confidence: float
    sensitive: bool
    eval_leaking: bool
    legacy_status: str
    compatibility_sha256: str
    projection_version: str = "tbm.legacy-lesson-compatibility.v1"
    eligible_for_activated_head: bool = False

    def __post_init__(self) -> None:
        if self.projection_version != "tbm.legacy-lesson-compatibility.v1":
            _projection_invalid("legacy projection version is unsupported")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.lesson_id, "lesson_id"),
            (self.source_case_id, "source_case_id"),
        ):
            _identifier(value, name)
        if type(self.lesson_text) is not str or not self.lesson_text:
            _projection_invalid("legacy lesson text is invalid")
        if self.eligible_for_activated_head is not False:
            _projection_invalid("legacy Lesson cannot become ActivatedMemoryHead")
        _digest(self.compatibility_sha256, "compatibility_sha256")
        if self.compatibility_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("legacy compatibility digest does not match")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "projection_version": self.projection_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "lesson_id": self.lesson_id,
            "source_case_id": self.source_case_id,
            "lesson_text": self.lesson_text,
            "memory_type": self.memory_type,
            "scope": dict(self.scope),
            "confidence": self.confidence,
            "sensitive": self.sensitive,
            "eval_leaking": self.eval_leaking,
            "legacy_status": self.legacy_status,
            "eligible_for_activated_head": False,
        }


def project_legacy_lesson(
    lesson: Lesson, *, tenant_id: str, repository_id: str
) -> LegacyLessonCompatibilityProjection:
    if type(lesson) is not Lesson:
        _fail(
            "TBM_MEMORY_CATALOG_LEGACY_INPUT_INVALID",
            "lesson must be exactly the compatibility Lesson record",
        )
    scope = tuple(sorted(lesson.scope.items()))
    values = {
        "projection_version": "tbm.legacy-lesson-compatibility.v1",
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "lesson_id": lesson.lesson_id,
        "source_case_id": lesson.source_case_id,
        "lesson_text": lesson.lesson_text,
        "memory_type": lesson.memory_type,
        "scope": dict(scope),
        "confidence": lesson.confidence,
        "sensitive": lesson.sensitive,
        "eval_leaking": lesson.eval_leaking,
        "legacy_status": lesson.status,
        "eligible_for_activated_head": False,
    }
    return LegacyLessonCompatibilityProjection(
        tenant_id=tenant_id,
        repository_id=repository_id,
        lesson_id=lesson.lesson_id,
        source_case_id=lesson.source_case_id,
        lesson_text=lesson.lesson_text,
        memory_type=lesson.memory_type,
        scope=scope,
        confidence=lesson.confidence,
        sensitive=lesson.sensitive,
        eval_leaking=lesson.eval_leaking,
        legacy_status=lesson.status,
        compatibility_sha256=canonical_sha256(values),
    )


def loads_memory_revision_review(document: str | bytes) -> MemoryRevisionReview:
    item = _loads_record(document, "MemoryRevisionReview")
    _require_fields(item, _REVIEW_FIELDS, "MemoryRevisionReview")
    return MemoryRevisionReview(
        review_id=_string(item, "review_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        memory_id=_string(item, "memory_id"),
        revision_id=_string(item, "revision_id"),
        decision=cast(MemoryReviewDecision, _string(item, "decision")),
        reviewed_by=_string(item, "reviewed_by"),
        reviewed_at=_string(item, "reviewed_at"),
        rationale_sha256=_string(item, "rationale_sha256"),
        review_attestation_sha256=_string(item, "review_attestation_sha256"),
        contract_version=_string(item, "contract_version"),
    )


def loads_memory_catalog_evidence_record(
    document: str | bytes,
) -> MemoryCatalogEvidenceRecord:
    item = _loads_record(document, "MemoryCatalogEvidenceRecord")
    _require_fields(
        item, _EVIDENCE_RECORD_FIELDS, "MemoryCatalogEvidenceRecord"
    )
    evidence_value = item.get("evidence")
    if type(evidence_value) is not dict:
        _record_invalid("evidence record is missing exact evidence")
    evidence_json = _canonical_json(evidence_value)
    kind = _string(item, "evidence_kind")
    if kind == "fix":
        evidence: FixEvidence | StructuredRegressionEvidence = loads_fix_evidence(
            evidence_json
        )
    elif kind == "regression":
        evidence = loads_structured_regression_evidence(evidence_json)
    else:
        _record_invalid("evidence_kind is unsupported")
    return MemoryCatalogEvidenceRecord(
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        memory_id=_string(item, "memory_id"),
        revision_id=_string(item, "revision_id"),
        evidence=evidence,
        contract_version=_string(item, "contract_version"),
    )


def loads_memory_revision_state_change(
    document: str | bytes,
) -> MemoryRevisionStateChange:
    item = _loads_record(document, "MemoryRevisionStateChange")
    _require_fields(
        item, _STATE_CHANGE_FIELDS, "MemoryRevisionStateChange"
    )
    return MemoryRevisionStateChange(
        state_change_id=_string(item, "state_change_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        memory_id=_string(item, "memory_id"),
        revision_id=_string(item, "revision_id"),
        activation_id=_optional_string(item, "activation_id"),
        change=cast(MemoryStateChangeKind, _string(item, "change")),
        replacement_revision_id=_optional_string(
            item, "replacement_revision_id"
        ),
        changed_by=_string(item, "changed_by"),
        changed_at=_string(item, "changed_at"),
        reason_sha256=_string(item, "reason_sha256"),
        change_attestation_sha256=_string(
            item, "change_attestation_sha256"
        ),
        contract_version=_string(item, "contract_version"),
    )


def loads_memory_revision_relationship(
    document: str | bytes,
) -> MemoryRevisionRelationship:
    item = _loads_record(document, "MemoryRevisionRelationship")
    _require_fields(
        item, _RELATIONSHIP_FIELDS, "MemoryRevisionRelationship"
    )
    return MemoryRevisionRelationship(
        relationship_id=_string(item, "relationship_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        memory_id=_string(item, "memory_id"),
        from_revision_id=_string(item, "from_revision_id"),
        to_revision_id=_string(item, "to_revision_id"),
        relationship=cast(
            MemoryRelationshipKind, _string(item, "relationship")
        ),
        recorded_by=_string(item, "recorded_by"),
        recorded_at=_string(item, "recorded_at"),
        evidence_sha256=_string(item, "evidence_sha256"),
        relationship_attestation_sha256=_string(
            item, "relationship_attestation_sha256"
        ),
        contract_version=_string(item, "contract_version"),
    )


def loads_memory_revision_counterexample(
    document: str | bytes,
) -> MemoryRevisionCounterexample:
    item = _loads_record(document, "MemoryRevisionCounterexample")
    _require_fields(
        item, _COUNTEREXAMPLE_FIELDS, "MemoryRevisionCounterexample"
    )
    evidence_value = item.get("evidence")
    if type(evidence_value) is not dict:
        _record_invalid("counterexample is missing structured evidence")
    evidence = loads_structured_regression_evidence(
        _canonical_json(evidence_value)
    )
    return MemoryRevisionCounterexample(
        counterexample_id=_string(item, "counterexample_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        memory_id=_string(item, "memory_id"),
        revision_id=_string(item, "revision_id"),
        evidence=evidence,
        recorded_by=_string(item, "recorded_by"),
        recorded_at=_string(item, "recorded_at"),
        counterexample_attestation_sha256=_string(
            item, "counterexample_attestation_sha256"
        ),
        contract_version=_string(item, "contract_version"),
    )


def loads_memory_catalog_approval_publication(
    document: str | bytes,
) -> StoredMemoryRevisionApprovalPublication:
    item = _loads_record(document, "MemoryCatalogApprovalPublication")
    _require_fields(
        item, _PUBLICATION_RECORD_FIELDS, "MemoryCatalogApprovalPublication"
    )
    if item.get("contract_version") != "tbm.memory-catalog-approval-record.v1":
        _record_invalid("approval publication record version is unsupported")
    publication = item.get("publication")
    policy = item.get("policy")
    request = item.get("request")
    decision = item.get("decision")
    if any(type(value) is not dict for value in (publication, policy, request, decision)):
        _record_invalid("approval publication record is malformed")
    return StoredMemoryRevisionApprovalPublication(
        approval=loads_memory_revision_approval(
            _canonical_json(cast(dict[str, object], publication))
        ),
        policy=parse_authorization_policy(cast(dict[str, object], policy)),
        request=_parse_authorization_request(cast(dict[str, object], request)),
        decision=parse_authorization_decision(cast(dict[str, object], decision)),
        attestation_verified_by=_string(item, "attestation_verified_by"),
    )


def loads_memory_catalog_activation_publication(
    document: str | bytes,
) -> StoredMemoryRevisionActivationPublication:
    item = _loads_record(document, "MemoryCatalogActivationPublication")
    _require_fields(
        item, _PUBLICATION_RECORD_FIELDS, "MemoryCatalogActivationPublication"
    )
    if item.get("contract_version") != "tbm.memory-catalog-activation-record.v1":
        _record_invalid("activation publication record version is unsupported")
    publication = item.get("publication")
    policy = item.get("policy")
    request = item.get("request")
    decision = item.get("decision")
    if any(type(value) is not dict for value in (publication, policy, request, decision)):
        _record_invalid("activation publication record is malformed")
    return StoredMemoryRevisionActivationPublication(
        activation=loads_memory_revision_activation(
            _canonical_json(cast(dict[str, object], publication))
        ),
        policy=parse_authorization_policy(cast(dict[str, object], policy)),
        request=_parse_authorization_request(cast(dict[str, object], request)),
        decision=parse_authorization_decision(cast(dict[str, object], decision)),
        attestation_verified_by=_string(item, "attestation_verified_by"),
    )


def _stored_approval_dict(
    record: StoredMemoryRevisionApprovalPublication,
) -> dict[str, object]:
    return {
        "contract_version": "tbm.memory-catalog-approval-record.v1",
        "publication": record.approval.to_dict(),
        "policy": record.policy.to_dict(),
        "request": record.request.to_dict(),
        "decision": record.decision.to_dict(),
        "attestation_verified_by": record.attestation_verified_by,
    }


def _stored_activation_dict(
    record: StoredMemoryRevisionActivationPublication,
) -> dict[str, object]:
    return {
        "contract_version": "tbm.memory-catalog-activation-record.v1",
        "publication": record.activation.to_dict(),
        "policy": record.policy.to_dict(),
        "request": record.request.to_dict(),
        "decision": record.decision.to_dict(),
        "attestation_verified_by": record.attestation_verified_by,
    }


def _record_descriptor(
    record: MemoryCatalogRecord,
) -> tuple[str, str, str, str, dict[str, object], str, str, str]:
    if type(record) is MemoryRevision:
        tenant_id = record.scope.tenant_id
        repository_id = record.scope.repository_id
        if tenant_id is None or repository_id is None:
            _record_invalid(
                "MemoryCatalog v1 requires a repository-scoped MemoryRevision"
            )
        return (
            MEMORY_REVISION_PROPOSED,
            record.memory_id,
            record.revision_id,
            record.proposed_at,
            record.to_dict(),
            record.proposed_by,
            tenant_id,
            repository_id,
        )
    if type(record) is MemoryRevisionReview:
        return (
            (
                MEMORY_REVISION_REVIEWED
                if record.decision == "accepted"
                else MEMORY_REVISION_REJECTED
            ),
            record.memory_id,
            record.revision_id,
            record.reviewed_at,
            record.to_dict(),
            record.reviewed_by,
            record.tenant_id,
            record.repository_id,
        )
    if type(record) is MemoryCatalogEvidenceRecord:
        return (
            (
                MEMORY_FIX_EVIDENCE_RECORDED
                if record.evidence_kind == "fix"
                else MEMORY_REGRESSION_EVIDENCE_RECORDED
            ),
            record.memory_id,
            record.revision_id,
            record.occurred_at,
            record.to_dict(),
            record.actor_id,
            record.tenant_id,
            record.repository_id,
        )
    if type(record) is StoredMemoryRevisionApprovalPublication:
        approval = record.approval
        if approval.repository_id is None:
            _record_invalid(
                "MemoryCatalog v1 requires a repository-scoped approval"
            )
        return (
            MEMORY_REVISION_APPROVED,
            approval.memory_id,
            approval.revision_id,
            approval.approved_at,
            _stored_approval_dict(record),
            approval.approved_by,
            approval.tenant_id,
            approval.repository_id,
        )
    if type(record) is StoredMemoryRevisionActivationPublication:
        activation = record.activation
        if activation.repository_id is None:
            _record_invalid(
                "MemoryCatalog v1 requires a repository-scoped activation"
            )
        return (
            MEMORY_REVISION_ACTIVATED,
            activation.memory_id,
            activation.revision_id,
            activation.activated_at,
            _stored_activation_dict(record),
            activation.activated_by,
            activation.tenant_id,
            activation.repository_id,
        )
    if type(record) is MemoryRevisionStateChange:
        event_type = {
            "suspended": MEMORY_REVISION_SUSPENDED,
            "superseded": MEMORY_REVISION_SUPERSEDED,
            "obsoleted": MEMORY_REVISION_OBSOLETED,
        }[record.change]
        return (
            event_type,
            record.memory_id,
            record.revision_id,
            record.changed_at,
            record.to_dict(),
            record.changed_by,
            record.tenant_id,
            record.repository_id,
        )
    if type(record) is MemoryRevisionRelationship:
        return (
            MEMORY_RELATIONSHIP_RECORDED,
            record.memory_id,
            record.from_revision_id,
            record.recorded_at,
            record.to_dict(),
            record.recorded_by,
            record.tenant_id,
            record.repository_id,
        )
    if type(record) is MemoryRevisionCounterexample:
        return (
            MEMORY_COUNTEREXAMPLE_RECORDED,
            record.memory_id,
            record.revision_id,
            record.recorded_at,
            record.to_dict(),
            record.recorded_by,
            record.tenant_id,
            record.repository_id,
        )
    _record_invalid("MemoryCatalog record type is unsupported")


def _read_memory_catalog_stream(
    ledger: EventLedgerPort, stream_id: str
) -> tuple[CanonicalEvent, ...]:
    events: list[CanonicalEvent] = []
    from_version = 1
    while True:
        page = ledger.read_stream(
            stream_id,
            from_version=from_version,
            limit=EVENT_LEDGER_MAX_READ_PAGE,
        )
        events.extend(page.events)
        if len(events) > MEMORY_CATALOG_EVENT_MAX_STREAM_EVENTS:
            _fail(
                "TBM_MEMORY_CATALOG_EVENT_SEQUENCE_INVALID",
                "durable MemoryCatalog stream exceeds the event limit",
            )
        if not page.has_more:
            break
        if page.next_stream_version is None:
            _fail(
                "TBM_MEMORY_CATALOG_LEDGER_READ_FAILED",
                "durable MemoryCatalog page lacks its next cursor",
            )
        from_version = page.next_stream_version
    return tuple(events)


def _verify_record_access(
    access: LedgerAccessContext,
    *,
    tenant_id: str,
    repository_id: str,
    actor_id: str,
) -> None:
    if (
        access.partition.tenant_id != tenant_id
        or access.partition.repository_id != repository_id
    ):
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_SCOPE_DENIED",
            "record target is outside the ledger partition",
        )
    if access.actor_id != actor_id:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_ACTOR_MISMATCH",
            "record actor does not match trusted ledger access",
        )
    if not access.classification_filter.allows("internal"):
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_CLASSIFICATION_DENIED",
            "ledger access does not allow internal MemoryCatalog events",
        )


def _typed_payload(reducer_event: ReducerEvent) -> dict[str, object]:
    typed = reducer_event.typed_event
    if typed is None:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_TYPED_INPUT_REQUIRED",
            "MemoryCatalog reducer requires typed input",
        )
    payload = _thaw_json(typed.payload)
    if type(payload) is not dict:
        _fail(
            "TBM_MEMORY_CATALOG_EVENT_PAYLOAD_INVALID",
            "MemoryCatalog payload must be an object",
        )
    event = reducer_event.source_event
    memory_id = payload.get("memory_id")
    if (
        type(memory_id) is not str
        or event.stream_id != memory_catalog_stream_id(memory_id)
        or payload.get("record_type") != event.event_type
    ):
        _transition_invalid("MemoryCatalog payload does not match its stream")
    return cast(dict[str, object], payload)


def _load_record(event_type: str, record_json: str) -> MemoryCatalogRecord:
    try:
        if event_type == MEMORY_REVISION_PROPOSED:
            return loads_memory_revision(record_json)
        if event_type in {MEMORY_REVISION_REVIEWED, MEMORY_REVISION_REJECTED}:
            return loads_memory_revision_review(record_json)
        if event_type in {
            MEMORY_FIX_EVIDENCE_RECORDED,
            MEMORY_REGRESSION_EVIDENCE_RECORDED,
        }:
            return loads_memory_catalog_evidence_record(record_json)
        if event_type == MEMORY_REVISION_APPROVED:
            return loads_memory_catalog_approval_publication(record_json)
        if event_type == MEMORY_REVISION_ACTIVATED:
            return loads_memory_catalog_activation_publication(record_json)
        if event_type in {
            MEMORY_REVISION_SUSPENDED,
            MEMORY_REVISION_SUPERSEDED,
            MEMORY_REVISION_OBSOLETED,
        }:
            return loads_memory_revision_state_change(record_json)
        if event_type == MEMORY_RELATIONSHIP_RECORDED:
            return loads_memory_revision_relationship(record_json)
        if event_type == MEMORY_COUNTEREXAMPLE_RECORDED:
            return loads_memory_revision_counterexample(record_json)
    except ValueError as error:
        raise MemoryCatalogEventV1Error(
            "TBM_MEMORY_CATALOG_EVENT_RECORD_INVALID",
            "MemoryCatalog event contains an invalid exact record",
        ) from error
    _transition_invalid("MemoryCatalog event record type is unsupported")


def _verify_loaded_record(
    payload: Mapping[str, object],
    record: MemoryCatalogRecord,
    event: CanonicalEvent,
) -> None:
    descriptor = _record_descriptor(record)
    if (
        payload.get("record_type") != descriptor[0]
        or payload.get("memory_id") != descriptor[1]
        or payload.get("revision_id") != descriptor[2]
        or payload.get("record_sha256") != canonical_sha256(descriptor[4])
        or payload.get("record_json") != _canonical_json(descriptor[4])
        or event.actor_id != descriptor[5]
        or event.tenant_id != descriptor[6]
        or event.repository_id != descriptor[7]
        or event.occurred_at != descriptor[3]
    ):
        _transition_invalid("MemoryCatalog record binding does not match payload")


def _verify_approval_links(
    revision: MemoryRevision,
    approval_record: StoredMemoryRevisionApprovalPublication,
    event: CanonicalEvent,
) -> None:
    approval = approval_record.approval
    if (
        approval.revision_id != revision.revision_id
        or approval.memory_id != revision.memory_id
        or approval.revision_number != revision.revision_number
        or approval.previous_revision_id != revision.previous_revision_id
        or approval.tenant_id != revision.scope.tenant_id
        or approval.repository_id != revision.scope.repository_id
        or approval.artifact_content_sha256
        != revision.content_artifact.content_sha256
        or approval.approved_by != event.actor_id
        or approval.authorization_event_id != event.authorization_decision_id
        or approval_record.decision.authorization_event_id
        != event.authorization_decision_id
    ):
        _transition_invalid("MemoryRevision approval does not match proposal or event")


def _verify_activation_links(
    revision: MemoryRevision,
    approval: MemoryRevisionApproval,
    activation_record: StoredMemoryRevisionActivationPublication,
    event: CanonicalEvent,
) -> None:
    activation = activation_record.activation
    if (
        activation.revision_id != revision.revision_id
        or activation.approval_id != approval.approval_id
        or activation.memory_id != revision.memory_id
        or activation.revision_number != revision.revision_number
        or activation.previous_revision_id != revision.previous_revision_id
        or activation.tenant_id != revision.scope.tenant_id
        or activation.repository_id != revision.scope.repository_id
        or activation.activated_by != event.actor_id
        or activation.authorization_event_id != event.authorization_decision_id
        or activation_record.decision.authorization_event_id
        != event.authorization_decision_id
        or parse_rfc3339(activation.activated_at)
        < parse_rfc3339(approval.approved_at)
    ):
        _transition_invalid("MemoryRevision activation does not match approval or event")


def _verify_reducer_evidence_bundle(
    revision: MemoryRevision, item: Mapping[str, object]
) -> None:
    fix_by_id: dict[str, FixEvidence] = {}
    fix_json = item.get("fix_evidence_json")
    if type(fix_json) is str:
        fix = loads_fix_evidence(fix_json)
        fix_by_id[fix.evidence_id] = fix
    regression_by_id: dict[str, StructuredRegressionEvidence] = {}
    for value in _state_list(item, "regression_evidence_json"):
        if type(value) is not str:
            _transition_invalid("regression evidence state is invalid")
        evidence = loads_structured_regression_evidence(value)
        regression_by_id[evidence.evidence_id] = evidence
    try:
        verify_memory_revision_evidence_bundle(
            revision, fix_by_id, regression_by_id
        )
    except ValueError as error:
        raise MemoryCatalogEventV1Error(
            "TBM_MEMORY_CATALOG_EVENT_EVIDENCE_INVALID",
            "MemoryRevision evidence bundle failed exact verification",
        ) from error


def _evidence_bundle_sha256(
    revision: MemoryRevision, item: Mapping[str, object]
) -> str:
    fix_by_id: dict[str, FixEvidence] = {}
    fix_json = item.get("fix_evidence_json")
    if type(fix_json) is str:
        fix = loads_fix_evidence(fix_json)
        fix_by_id[fix.evidence_id] = fix
    regression_by_id: dict[str, StructuredRegressionEvidence] = {}
    for value in _state_list(item, "regression_evidence_json"):
        if type(value) is not str:
            _transition_invalid("regression evidence state is invalid")
        evidence = loads_structured_regression_evidence(value)
        regression_by_id[evidence.evidence_id] = evidence
    return memory_revision_evidence_bundle_sha256(
        revision, fix_by_id, regression_by_id
    )


def _new_revision_state(revision: MemoryRevision) -> dict[str, object]:
    return {
        "revision_json": _canonical_json(revision.to_dict()),
        "status": "proposed",
        "review_json": None,
        "fix_evidence_json": None,
        "regression_evidence_json": [],
        "approval_json": None,
        "approval_publication_json": None,
        "activation_json": None,
        "activation_event_sha256": None,
        "activation_publication_json": None,
        "state_change_json": [],
        "relationship_json": [],
        "counterexample_json": [],
    }


def _head_state(
    revision: MemoryRevision,
    approval: MemoryRevisionApproval,
    activation: MemoryRevisionActivation,
    event: CanonicalEvent,
    *,
    approval_attestation_verified_by: str,
    activation_attestation_verified_by: str,
) -> dict[str, object]:
    values = {
        "contract_version": "tbm.activated-memory-head.v1",
        "tenant_id": activation.tenant_id,
        "repository_id": activation.repository_id,
        "memory_id": activation.memory_id,
        "current_revision_number": activation.revision_number,
        "current_revision_id": activation.revision_id,
        "current_approval_id": activation.approval_id,
        "current_activation_id": activation.activation_id,
        "applicability_sha256": canonical_sha256(revision.scope.to_dict()),
        "content_artifact_id": revision.content_artifact.artifact_id,
        "content_sha256": revision.content_artifact.content_sha256,
        "evidence_bundle_sha256": approval.evidence_bundle_sha256,
        "approval_authorization_event_id": approval.authorization_event_id,
        "activation_authorization_event_id": (
            activation.authorization_event_id
        ),
        "approval_attestation_verified_by": (
            approval_attestation_verified_by
        ),
        "activation_attestation_verified_by": (
            activation_attestation_verified_by
        ),
        "activated_at": activation.activated_at,
        "source_event_sha256": event.event_sha256,
    }
    if activation.repository_id is None:
        _transition_invalid("ActivatedMemoryHead requires repository scope")
    return {"head_sha256": canonical_sha256(values), **values}


def _has_supersedes_relationship(
    relationships: list[object],
    *,
    from_revision_id: str,
    to_revision_id: str,
) -> bool:
    for value in relationships:
        if type(value) is not str:
            _transition_invalid("relationship state is invalid")
        relationship = loads_memory_revision_relationship(value)
        if (
            relationship.relationship == "supersedes"
            and relationship.from_revision_id == from_revision_id
            and relationship.to_revision_id == to_revision_id
        ):
            return True
    return False


def _latest_revision_time(item: Mapping[str, object]) -> str:
    revision = loads_memory_revision(cast(str, item["revision_json"]))
    values = [revision.proposed_at]
    review_json = item.get("review_json")
    if type(review_json) is str:
        values.append(loads_memory_revision_review(review_json).reviewed_at)
    approval_json = item.get("approval_json")
    if type(approval_json) is str:
        values.append(loads_memory_revision_approval(approval_json).approved_at)
    activation_json = item.get("activation_json")
    if type(activation_json) is str:
        values.append(
            loads_memory_revision_activation(activation_json).activated_at
        )
    for value in _state_list(item, "state_change_json"):
        if type(value) is not str:
            _projection_invalid("state change history is invalid")
        values.append(loads_memory_revision_state_change(value).changed_at)
    return max(values, key=parse_rfc3339)


def _hydrate_projection(state: Mapping[str, object]) -> MemoryCatalogProjection:
    tenant_id = state.get("tenant_id")
    repository_id = state.get("repository_id")
    memory_id = state.get("memory_id")
    if any(type(value) is not str for value in (tenant_id, repository_id, memory_id)):
        _projection_invalid("MemoryCatalog projection identity is missing")
    revisions_state = _state_mapping(state, "revisions")
    views: list[MemoryCatalogRevisionView] = []
    for value in revisions_state.values():
        if type(value) is not dict:
            _projection_invalid("MemoryCatalog revision state is invalid")
        revision = loads_memory_revision(cast(str, value["revision_json"]))
        review_json = value.get("review_json")
        fix_json = value.get("fix_evidence_json")
        approval_json = value.get("approval_json")
        activation_json = value.get("activation_json")
        views.append(
            MemoryCatalogRevisionView(
                revision=revision,
                status=cast(MemoryCatalogRevisionStatus, value["status"]),
                review=(
                    None
                    if review_json is None
                    else loads_memory_revision_review(cast(str, review_json))
                ),
                fix_evidence=(
                    None
                    if fix_json is None
                    else loads_fix_evidence(cast(str, fix_json))
                ),
                regression_evidence=tuple(
                    sorted(
                        (
                            loads_structured_regression_evidence(cast(str, item))
                            for item in _state_list(
                                value, "regression_evidence_json"
                            )
                        ),
                        key=lambda item: item.evidence_id,
                    )
                ),
                approval=(
                    None
                    if approval_json is None
                    else loads_memory_revision_approval(cast(str, approval_json))
                ),
                activation=(
                    None
                    if activation_json is None
                    else loads_memory_revision_activation(
                        cast(str, activation_json)
                    )
                ),
                activation_event_sha256=cast(
                    str | None, value.get("activation_event_sha256")
                ),
                state_changes=tuple(
                    loads_memory_revision_state_change(cast(str, item))
                    for item in _state_list(value, "state_change_json")
                ),
                relationships=tuple(
                    loads_memory_revision_relationship(cast(str, item))
                    for item in _state_list(value, "relationship_json")
                ),
                counterexamples=tuple(
                    loads_memory_revision_counterexample(cast(str, item))
                    for item in _state_list(value, "counterexample_json")
                ),
            )
        )
    views.sort(key=lambda item: item.revision.revision_number)
    head_state = _optional_state_mapping(state.get("head"), "head")
    head = None
    if head_state is not None:
        head = ActivatedMemoryHead(
            tenant_id=cast(str, head_state["tenant_id"]),
            repository_id=cast(str, head_state["repository_id"]),
            memory_id=cast(str, head_state["memory_id"]),
            current_revision_number=cast(int, head_state["current_revision_number"]),
            current_revision_id=cast(str, head_state["current_revision_id"]),
            current_approval_id=cast(str, head_state["current_approval_id"]),
            current_activation_id=cast(str, head_state["current_activation_id"]),
            applicability_sha256=cast(str, head_state["applicability_sha256"]),
            content_artifact_id=cast(str, head_state["content_artifact_id"]),
            content_sha256=cast(str, head_state["content_sha256"]),
            evidence_bundle_sha256=cast(
                str, head_state["evidence_bundle_sha256"]
            ),
            approval_authorization_event_id=cast(
                str, head_state["approval_authorization_event_id"]
            ),
            activation_authorization_event_id=cast(
                str, head_state["activation_authorization_event_id"]
            ),
            approval_attestation_verified_by=cast(
                str, head_state["approval_attestation_verified_by"]
            ),
            activation_attestation_verified_by=cast(
                str, head_state["activation_attestation_verified_by"]
            ),
            activated_at=cast(str, head_state["activated_at"]),
            source_event_sha256=cast(str, head_state["source_event_sha256"]),
            head_sha256=cast(str, head_state["head_sha256"]),
            contract_version=cast(str, head_state["contract_version"]),
        )
    return MemoryCatalogProjection(
        tenant_id=cast(str, tenant_id),
        repository_id=cast(str, repository_id),
        memory_id=cast(str, memory_id),
        revisions=tuple(views),
        activated_head=head,
        last_event_sha256=cast(str | None, state.get("last_event_sha256")),
        last_global_position=cast(int, state.get("last_global_position")),
    )


def _payload_json_schemas() -> dict[str, Mapping[str, object]]:
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    record_json = {"type": "string", "minLength": 2, "maxLength": 262144}
    result: dict[str, Mapping[str, object]] = {}
    for event_type in MEMORY_CATALOG_EVENT_TYPES:
        properties = {
            "memory_id": identifier,
            "revision_id": identifier,
            "record_type": {"const": event_type},
            "record_sha256": digest,
            "record_json": record_json,
        }
        result[event_type] = {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }
    return result


def _loads_record(document: str | bytes, description: str) -> dict[str, object]:
    if type(document) is bytes:
        try:
            source = document.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise MemoryCatalogEventV1Error(
                "TBM_MEMORY_CATALOG_RECORD_INVALID_JSON",
                f"{description} must be strict UTF-8 JSON",
            ) from error
    elif type(document) is str:
        source = document
    else:
        _record_invalid(f"{description} must be JSON text")
    try:
        encoded_size = len(source.encode("utf-8"))
    except UnicodeError as error:
        raise MemoryCatalogEventV1Error(
            "TBM_MEMORY_CATALOG_RECORD_INVALID_JSON",
            f"{description} must be strict UTF-8 JSON",
        ) from error
    if encoded_size > 262_144:
        _record_invalid(f"{description} exceeds the byte limit")
    try:
        value = parse_bounded_json(
            source,
            description=description,
            max_nodes=8192,
            max_depth=32,
        )
    except (TypeError, ValueError) as error:
        raise MemoryCatalogEventV1Error(
            "TBM_MEMORY_CATALOG_RECORD_INVALID_JSON",
            f"{description} must be bounded strict JSON",
        ) from error
    if type(value) is not dict:
        _record_invalid(f"{description} must be an object")
    return cast(dict[str, object], value)


def _require_fields(
    item: Mapping[str, object], expected: frozenset[str], description: str
) -> None:
    if frozenset(item) != expected:
        _record_invalid(f"{description} fields do not match the contract")


def _state_mapping(state: Mapping[str, object], name: str) -> dict[str, object]:
    value = _thaw_json(state.get(name))
    if type(value) is not dict or any(type(key) is not str for key in value):
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(dict[str, object], value)


def _optional_state_mapping(
    value: object, name: str
) -> dict[str, object] | None:
    thawed = _thaw_json(value)
    if thawed is None:
        return None
    if type(thawed) is not dict:
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(dict[str, object], thawed)


def _state_list(state: Mapping[str, object], name: str) -> list[object]:
    value = _thaw_json(state.get(name))
    if type(value) is not list:
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(list[object], value)


def _revision_state(
    revisions: dict[str, object], revision_id: str
) -> dict[str, object]:
    value = revisions.get(revision_id)
    if type(value) is not dict:
        _transition_invalid("MemoryRevision is missing from catalog")
    return cast(dict[str, object], value)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise MemoryCatalogEventV1Error(
            "TBM_MEMORY_CATALOG_RECORD_INVALID",
            "MemoryCatalog record is not canonical JSON",
        ) from error


def _memory_catalog_digest_value(catalog: MemoryCatalog) -> dict[str, object]:
    return {
        "projections": [
            {
                "tenant_id": projection.tenant_id,
                "repository_id": projection.repository_id,
                "memory_id": projection.memory_id,
                "last_event_sha256": projection.last_event_sha256,
                "last_global_position": projection.last_global_position,
                "activated_head": (
                    None
                    if projection.activated_head is None
                    else projection.activated_head.to_dict()
                ),
                "revisions": [
                    {
                        "revision": item.revision.to_dict(),
                        "status": item.status,
                        "review": (
                            None if item.review is None else item.review.to_dict()
                        ),
                        "fix_evidence": (
                            None
                            if item.fix_evidence is None
                            else item.fix_evidence.to_dict()
                        ),
                        "regression_evidence": [
                            evidence.to_dict()
                            for evidence in item.regression_evidence
                        ],
                        "approval": (
                            None
                            if item.approval is None
                            else item.approval.to_dict()
                        ),
                        "activation": (
                            None
                            if item.activation is None
                            else item.activation.to_dict()
                        ),
                        "activation_event_sha256": (
                            item.activation_event_sha256
                        ),
                        "state_changes": [
                            change.to_dict() for change in item.state_changes
                        ],
                        "relationships": [
                            relationship.to_dict()
                            for relationship in item.relationships
                        ],
                        "counterexamples": [
                            counterexample.to_dict()
                            for counterexample in item.counterexamples
                        ],
                    }
                    for item in projection.revisions
                ],
            }
            for projection in catalog.projections
        ]
    }


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    if type(value) is list:
        return [_thaw_json(item) for item in value]
    return value


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(
        domain + _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _trusted_verifier_set(values: tuple[str, ...]) -> frozenset[str]:
    if (
        type(values) is not tuple
        or not values
        or len(values) > 32
        or len(values) != len(set(values))
    ):
        _fail(
            "TBM_MEMORY_CATALOG_VERIFIER_CONFIGURATION_INVALID",
            "trusted attestation verifier IDs must be a bounded unique tuple",
        )
    for value in values:
        _identifier(value, "trusted_attestation_verifier_id")
    return frozenset(values)


def _derived_id(prefix: str, value: Mapping[str, object]) -> str:
    return prefix + canonical_sha256(value).removeprefix("sha256:")


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _record_invalid(f"{name} must be a bounded identifier")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _record_invalid(f"{name} must be sha256:<64 lowercase hex>")


def _timestamp(value: object, name: str) -> str:
    try:
        if type(value) is not str:
            raise ValueError("timestamp must be a string")
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise MemoryCatalogEventV1Error(
            "TBM_MEMORY_CATALOG_RECORD_INVALID",
            f"{name} must be canonical RFC3339",
        ) from error


def _string(item: Mapping[str, object], name: str) -> str:
    value = item.get(name)
    if type(value) is not str:
        _record_invalid(f"{name} must be a string")
    return value


def _parse_authorization_request(
    item: Mapping[str, object],
) -> AuthorizationRequest:
    _require_fields(
        item, _AUTHORIZATION_REQUEST_FIELDS, "AuthorizationRequest"
    )
    return AuthorizationRequest(
        request_id=_string(item, "request_id"),
        principal_id=_string(item, "principal_id"),
        agent_client_id=_string(item, "agent_client_id"),
        tenant_id=_optional_string(item, "tenant_id"),
        repository_reference=_optional_string(
            item, "repository_reference"
        ),
        permission=cast(
            AuthorizationPermission, _string(item, "permission")
        ),
        requested_at=_string(item, "requested_at"),
    )


def _optional_string(item: Mapping[str, object], name: str) -> str | None:
    value = item.get(name)
    if value is not None and type(value) is not str:
        _record_invalid(f"{name} must be a string or null")
    return cast(str | None, value)


def _record_invalid(message: str) -> NoReturn:
    _fail("TBM_MEMORY_CATALOG_RECORD_INVALID", message)


def _transition_invalid(message: str) -> NoReturn:
    _fail("TBM_MEMORY_CATALOG_TRANSITION_INVALID", message)


def _projection_invalid(message: str) -> NoReturn:
    _fail("TBM_MEMORY_CATALOG_PROJECTION_INVALID", message)


def _source_reject(code: str, message: str) -> NoReturn:
    raise ActivatedRevisionV3Error(code, message)


def _fail(code: str, message: str) -> NoReturn:
    raise MemoryCatalogEventV1Error(code, message)
