from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Literal, NoReturn, cast

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import canonical_sha256
from .event_registry_v1 import (
    EventPayloadRegistration,
    EventTypeRegistry,
)
from .event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    EventSource,
    build_canonical_event,
    verify_event_parent,
)
from .evidence_v3 import StructuredRegressionEvidence
from .fix_evidence_v3 import FixEvidence
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerIdempotency,
    LedgerTenantPartition,
    verify_ledger_append_receipt,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerV1Error,
    execute_reducer_step,
    initial_reducer_state,
)
from .trace_event_v1 import TRACE_EVENT_TYPES, verify_trace_event


FAILURE_CASE_EVENT_PROTOCOL_VERSION = "tbm.failure-case-event.v1"
FAILURE_CASE_EVENT_STREAM_TYPE = "failure_case"
FAILURE_CASE_EVENT_MAX_EVENTS = 32
FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS = 8_192
FAILURE_CASE_EVENT_MAX_ARTIFACTS = 64
FAILURE_CASE_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "failure_case_event_payload_registry_v1.schema.json"
)

FAILURE_CASE_EXTRACTOR_PROPOSED = "tbm.failure_case.extractor_proposed"
FAILURE_CASE_REVIEWED = "tbm.failure_case.reviewed"
FAILURE_CASE_FIX_EVIDENCE_RECORDED = (
    "tbm.failure_case.fix_evidence_recorded"
)
FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED = (
    "tbm.failure_case.regression_evidence_recorded"
)
FAILURE_CASE_LEGACY_IMPORTED = "tbm.failure_case.legacy_imported"

FAILURE_CASE_EVENT_TYPES = tuple(
    sorted(
        (
            FAILURE_CASE_EXTRACTOR_PROPOSED,
            FAILURE_CASE_REVIEWED,
            FAILURE_CASE_FIX_EVIDENCE_RECORDED,
            FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED,
            FAILURE_CASE_LEGACY_IMPORTED,
        )
    )
)

FAILURE_CASE_REDUCER_ID = "failure-case-current-v1"
FAILURE_CASE_PROJECTION = "tbm.failure-case-projection.v1"

FailureCaseReviewDecision = Literal["accepted", "rejected"]
FailureCaseEvidenceQuality = Literal[
    "none", "legacy_unstructured", "structured_verified"
]
FailureCaseProjectionStatus = Literal[
    "candidate", "reviewed", "rejected", "verified", "legacy_imported"
]

_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in FAILURE_CASE_EVENT_TYPES
}
_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_EVENT_SHA256_RE = _DIGEST_RE
_MAX_POSITION = 9_223_372_036_854_775_807
_DRAFT_PRODUCER_CAPABILITY = object()


class FailureCaseEventV1Error(ReducerV1Error):
    """Stable failure for event-derived FailureCase projections."""


def _fail(code: str, message: str) -> NoReturn:
    raise FailureCaseEventV1Error(code, message)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("TBM_FAILURE_CASE_EVENT_INVALID", f"{field} is invalid")
    return value


def _code(value: object, field: str) -> str:
    if type(value) is not str or _CODE_RE.fullmatch(value) is None:
        _fail("TBM_FAILURE_CASE_EVENT_INVALID", f"{field} is invalid")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail("TBM_FAILURE_CASE_EVENT_INVALID", f"{field} is invalid")
    return value


def _timestamp(value: object, field: str) -> str:
    if type(value) is not str:
        _fail("TBM_FAILURE_CASE_EVENT_INVALID", f"{field} is invalid")
    try:
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise FailureCaseEventV1Error(
            "TBM_FAILURE_CASE_EVENT_INVALID",
            f"{field} is invalid",
        ) from error


def _tuple_of_unique_strings(
    value: object,
    *,
    field: str,
    pattern: re.Pattern[str],
    maximum: int,
    non_empty: bool = False,
    sorted_required: bool = False,
) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or len(value) > maximum
        or (non_empty and not value)
        or any(type(item) is not str or pattern.fullmatch(item) is None for item in value)
        or len(value) != len(set(value))
        or (sorted_required and value != tuple(sorted(value)))
    ):
        _fail("TBM_FAILURE_CASE_EVENT_INVALID", f"{field} is invalid")
    return value


def _artifact_refs(
    value: object,
    *,
    non_empty: bool = False,
) -> tuple[EventArtifactRef, ...]:
    if (
        type(value) is not tuple
        or len(value) > FAILURE_CASE_EVENT_MAX_ARTIFACTS
        or (non_empty and not value)
        or any(type(item) is not EventArtifactRef for item in value)
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
            "artifact references are invalid",
        )
    refs = cast(tuple[EventArtifactRef, ...], value)
    ids = tuple(item.artifact_id for item in refs)
    if len(ids) != len(set(ids)):
        _fail(
            "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
            "artifact references must be unique",
        )
    return tuple(sorted(refs, key=lambda item: item.artifact_id))


@dataclass(frozen=True)
class FailureCaseExtractorProposal:
    proposal_id: str
    case_id: str
    source_trace_id: str
    source_run_id: str
    source_partition_sha256: str
    source_event_sha256s: tuple[str, ...]
    source_artifact_ids: tuple[str, ...]
    proposal_artifact_ids: tuple[str, ...]
    failure_type: str
    extractor_id: str
    extractor_version: str
    extractor_configuration_sha256: str
    proposed_at: str
    candidate_status: Literal["candidate"] = "candidate"

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _identifier(self.source_trace_id, "source_trace_id")
        _identifier(self.source_run_id, "source_run_id")
        _digest(self.source_partition_sha256, "source_partition_sha256")
        _tuple_of_unique_strings(
            self.source_event_sha256s,
            field="source_event_sha256s",
            pattern=_EVENT_SHA256_RE,
            maximum=FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS,
            non_empty=True,
        )
        _tuple_of_unique_strings(
            self.source_artifact_ids,
            field="source_artifact_ids",
            pattern=_ARTIFACT_ID_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            sorted_required=True,
        )
        _tuple_of_unique_strings(
            self.proposal_artifact_ids,
            field="proposal_artifact_ids",
            pattern=_ARTIFACT_ID_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            non_empty=True,
            sorted_required=True,
        )
        if set(self.source_artifact_ids) & set(self.proposal_artifact_ids):
            _fail(
                "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
                "source and proposal artifacts must be distinct",
            )
        _code(self.failure_type, "failure_type")
        _identifier(self.extractor_id, "extractor_id")
        _code(self.extractor_version, "extractor_version")
        _digest(
            self.extractor_configuration_sha256,
            "extractor_configuration_sha256",
        )
        object.__setattr__(self, "proposed_at", _timestamp(self.proposed_at, "proposed_at"))
        if self.candidate_status != "candidate":
            _fail(
                "TBM_FAILURE_CASE_EXTRACTOR_CANNOT_VERIFY",
                "extractor output must remain a candidate",
            )
        expected = failure_case_proposal_id(self._unsigned_dict())
        if self.proposal_id != expected:
            _fail(
                "TBM_FAILURE_CASE_PROPOSAL_HASH_MISMATCH",
                "proposal_id does not match canonical proposal content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "source_trace_id": self.source_trace_id,
            "source_run_id": self.source_run_id,
            "source_partition_sha256": self.source_partition_sha256,
            "source_event_sha256s": list(self.source_event_sha256s),
            "source_artifact_ids": list(self.source_artifact_ids),
            "proposal_artifact_ids": list(self.proposal_artifact_ids),
            "failure_type": self.failure_type,
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
            "extractor_configuration_sha256": self.extractor_configuration_sha256,
            "proposed_at": self.proposed_at,
            "candidate_status": self.candidate_status,
        }

    def to_dict(self) -> dict[str, object]:
        result = self._unsigned_dict()
        result["proposal_id"] = self.proposal_id
        return result


def failure_case_proposal_id(content: Mapping[str, object]) -> str:
    return "failure_proposal_sha256_" + canonical_sha256(content).removeprefix(
        "sha256:"
    )


def build_failure_case_extractor_proposal(
    trace_events: tuple[CanonicalEvent, ...],
    *,
    case_id: str,
    failure_type: str,
    proposal_artifacts: tuple[EventArtifactRef, ...],
    extractor_id: str,
    extractor_version: str,
    extractor_configuration_sha256: str,
    proposed_at: str,
) -> FailureCaseExtractorProposal:
    (
        trace_id,
        run_id,
        source_partition_sha256,
        source_hashes,
        source_artifact_ids,
    ) = _trace_source(trace_events)
    proposal_refs = _artifact_refs(proposal_artifacts, non_empty=True)
    proposal_artifact_ids = tuple(item.artifact_id for item in proposal_refs)
    if set(source_artifact_ids) & set(proposal_artifact_ids):
        _fail(
            "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
            "proposal artifacts cannot reuse source Trace artifacts",
        )
    proposed = _timestamp(proposed_at, "proposed_at")
    unsigned: dict[str, object] = {
        "case_id": case_id,
        "source_trace_id": trace_id,
        "source_run_id": run_id,
        "source_partition_sha256": source_partition_sha256,
        "source_event_sha256s": list(source_hashes),
        "source_artifact_ids": list(source_artifact_ids),
        "proposal_artifact_ids": list(proposal_artifact_ids),
        "failure_type": failure_type,
        "extractor_id": extractor_id,
        "extractor_version": extractor_version,
        "extractor_configuration_sha256": extractor_configuration_sha256,
        "proposed_at": proposed,
        "candidate_status": "candidate",
    }
    return FailureCaseExtractorProposal(
        proposal_id=failure_case_proposal_id(unsigned),
        case_id=case_id,
        source_trace_id=trace_id,
        source_run_id=run_id,
        source_partition_sha256=source_partition_sha256,
        source_event_sha256s=source_hashes,
        source_artifact_ids=source_artifact_ids,
        proposal_artifact_ids=proposal_artifact_ids,
        failure_type=failure_type,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
        extractor_configuration_sha256=extractor_configuration_sha256,
        proposed_at=proposed,
    )


@dataclass(frozen=True)
class _FailureCaseEventDraft:
    event_type: str
    case_id: str
    occurred_at: str
    payload: Mapping[str, object]
    artifact_refs: tuple[EventArtifactRef, ...] = ()
    classification: EventClassification = "internal"
    retention_policy_id: str = "retention_failure_case_events"
    source: EventSource | None = None
    _producer_capability: object = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self._producer_capability is not _DRAFT_PRODUCER_CAPABILITY:
            _fail(
                "TBM_FAILURE_CASE_DRAFT_PRODUCER_REQUIRED",
                "FailureCase drafts must be created by a validated builder",
            )
        if self.event_type not in FAILURE_CASE_EVENT_TYPES:
            _fail(
                "TBM_FAILURE_CASE_EVENT_TYPE_INVALID",
                "FailureCase event type is invalid",
            )
        _identifier(self.case_id, "case_id")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        refs = _artifact_refs(self.artifact_refs)
        object.__setattr__(self, "artifact_refs", refs)
        if self.classification not in _CLASSIFICATION_RANK:
            _fail(
                "TBM_FAILURE_CASE_EVENT_CLASSIFICATION_INVALID",
                "event classification is invalid",
            )
        if any(
            _CLASSIFICATION_RANK[item.classification]
            > _CLASSIFICATION_RANK[self.classification]
            for item in refs
        ):
            _fail(
                "TBM_FAILURE_CASE_EVENT_CLASSIFICATION_INVALID",
                "event classification cannot be lower than an Artifact",
            )
        _identifier(self.retention_policy_id, "retention_policy_id")
        if self.event_type == FAILURE_CASE_LEGACY_IMPORTED:
            if type(self.source) is not EventSource:
                _fail(
                    "TBM_FAILURE_CASE_LEGACY_SOURCE_REQUIRED",
                    "legacy imports require explicit source evidence",
                )
        elif self.source is not None:
            _fail(
                "TBM_FAILURE_CASE_EVENT_INVALID",
                "native FailureCase events cannot claim import source evidence",
            )
        payload = _plain_mapping(self.payload)
        if payload.get("case_id") != self.case_id:
            _fail(
                "TBM_FAILURE_CASE_EVENT_INVALID",
                "event payload case_id does not match its draft",
            )
        object.__setattr__(self, "payload", _freeze_json(payload))
        _validate_draft_payload(self.event_type, payload, refs)

    def command_value(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "case_id": self.case_id,
            "occurred_at": self.occurred_at,
            "payload": _plain_mapping(self.payload),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
            "source": None if self.source is None else self.source.to_dict(),
        }


@dataclass(frozen=True)
class FailureCaseProjection:
    case_id: str
    source_trace_id: str
    source_run_id: str | None
    proposal_id: str | None
    failure_type: str | None
    status: FailureCaseProjectionStatus
    evidence_quality: FailureCaseEvidenceQuality
    extractor_id: str | None
    reviewer_id: str | None
    review_decision: FailureCaseReviewDecision | None
    fix_evidence_id: str | None
    regression_evidence_id: str | None
    regression_result: Literal["pass", "fail", "error"] | None
    eligible_for_new_memory: bool
    source_event_sha256s: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    last_event_sha256: str
    last_global_position: int
    projection_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _identifier(self.source_trace_id, "source_trace_id")
        if self.source_run_id is not None:
            _identifier(self.source_run_id, "source_run_id")
        if self.proposal_id is not None:
            _code(self.proposal_id, "proposal_id")
        if self.failure_type is not None:
            _code(self.failure_type, "failure_type")
        if self.status not in {
            "candidate",
            "reviewed",
            "rejected",
            "verified",
            "legacy_imported",
        }:
            _fail("TBM_FAILURE_CASE_PROJECTION_INVALID", "status is invalid")
        if self.evidence_quality not in {
            "none",
            "legacy_unstructured",
            "structured_verified",
        }:
            _fail(
                "TBM_FAILURE_CASE_PROJECTION_INVALID",
                "evidence quality is invalid",
            )
        _tuple_of_unique_strings(
            self.source_event_sha256s,
            field="source_event_sha256s",
            pattern=_EVENT_SHA256_RE,
            maximum=FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS,
        )
        _tuple_of_unique_strings(
            self.artifact_ids,
            field="artifact_ids",
            pattern=_ARTIFACT_ID_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            sorted_required=True,
        )
        _digest(self.last_event_sha256, "last_event_sha256")
        if type(self.last_global_position) is not int or self.last_global_position < 1:
            _fail(
                "TBM_FAILURE_CASE_PROJECTION_INVALID",
                "last_global_position is invalid",
            )
        if self.eligible_for_new_memory != (
            self.status == "verified"
            and self.evidence_quality == "structured_verified"
            and self.review_decision == "accepted"
            and self.fix_evidence_id is not None
            and self.regression_evidence_id is not None
            and self.regression_result == "pass"
        ):
            _fail(
                "TBM_FAILURE_CASE_PROJECTION_INVALID",
                "new-Memory eligibility is not backed by structured evidence",
            )
        unsigned = self.to_dict(include_digest=False)
        expected = canonical_sha256(unsigned)
        if self.projection_sha256 != expected:
            _fail(
                "TBM_FAILURE_CASE_PROJECTION_HASH_MISMATCH",
                "projection_sha256 does not match projection content",
            )

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "case_id": self.case_id,
            "source_trace_id": self.source_trace_id,
            "source_run_id": self.source_run_id,
            "proposal_id": self.proposal_id,
            "failure_type": self.failure_type,
            "status": self.status,
            "evidence_quality": self.evidence_quality,
            "extractor_id": self.extractor_id,
            "reviewer_id": self.reviewer_id,
            "review_decision": self.review_decision,
            "fix_evidence_id": self.fix_evidence_id,
            "regression_evidence_id": self.regression_evidence_id,
            "regression_result": self.regression_result,
            "eligible_for_new_memory": self.eligible_for_new_memory,
            "source_event_sha256s": list(self.source_event_sha256s),
            "artifact_ids": list(self.artifact_ids),
            "last_event_sha256": self.last_event_sha256,
            "last_global_position": self.last_global_position,
        }
        if include_digest:
            result["projection_sha256"] = self.projection_sha256
        return result


def failure_case_event_stream_id(case_id: str) -> str:
    _identifier(case_id, "case_id")
    return "failure_case_" + hashlib.sha256(case_id.encode("utf-8")).hexdigest()


def build_failure_case_proposal_draft(
    proposal: FailureCaseExtractorProposal,
    *,
    proposal_artifacts: tuple[EventArtifactRef, ...],
    classification: EventClassification = "confidential",
) -> _FailureCaseEventDraft:
    if type(proposal) is not FailureCaseExtractorProposal:
        _fail(
            "TBM_FAILURE_CASE_PROPOSAL_INVALID",
            "proposal must be exactly FailureCaseExtractorProposal",
        )
    refs = _artifact_refs(proposal_artifacts, non_empty=True)
    if tuple(item.artifact_id for item in refs) != proposal.proposal_artifact_ids:
        _fail(
            "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
            "proposal Artifact descriptors do not match the proposal",
        )
    return _FailureCaseEventDraft(
        event_type=FAILURE_CASE_EXTRACTOR_PROPOSED,
        case_id=proposal.case_id,
        occurred_at=proposal.proposed_at,
        payload=proposal.to_dict(),
        artifact_refs=refs,
        classification=classification,
        _producer_capability=_DRAFT_PRODUCER_CAPABILITY,
    )


def build_failure_case_review_draft(
    proposal: FailureCaseExtractorProposal,
    *,
    reviewer_id: str,
    decision: FailureCaseReviewDecision,
    reason_code: str,
    reviewed_at: str,
    attestation_sha256: str,
    review_artifacts: tuple[EventArtifactRef, ...] = (),
    classification: EventClassification = "confidential",
) -> _FailureCaseEventDraft:
    if type(proposal) is not FailureCaseExtractorProposal:
        _fail("TBM_FAILURE_CASE_REVIEW_INVALID", "proposal is invalid")
    _identifier(reviewer_id, "reviewer_id")
    if reviewer_id == proposal.extractor_id:
        _fail(
            "TBM_FAILURE_CASE_REVIEW_INDEPENDENCE_REQUIRED",
            "extractor cannot review its own proposal",
        )
    if decision not in {"accepted", "rejected"}:
        _fail("TBM_FAILURE_CASE_REVIEW_INVALID", "review decision is invalid")
    _code(reason_code, "reason_code")
    reviewed = _timestamp(reviewed_at, "reviewed_at")
    if parse_rfc3339(reviewed) < parse_rfc3339(proposal.proposed_at):
        _fail(
            "TBM_FAILURE_CASE_REVIEW_INVALID",
            "review cannot precede the proposal",
        )
    _digest(attestation_sha256, "attestation_sha256")
    refs = _artifact_refs(review_artifacts)
    payload = {
        "case_id": proposal.case_id,
        "proposal_id": proposal.proposal_id,
        "reviewer_id": reviewer_id,
        "decision": decision,
        "reason_code": reason_code,
        "reviewed_at": reviewed,
        "attestation_sha256": attestation_sha256,
        "review_artifact_ids": [item.artifact_id for item in refs],
    }
    return _FailureCaseEventDraft(
        event_type=FAILURE_CASE_REVIEWED,
        case_id=proposal.case_id,
        occurred_at=reviewed,
        payload=payload,
        artifact_refs=refs,
        classification=classification,
        _producer_capability=_DRAFT_PRODUCER_CAPABILITY,
    )


def build_failure_case_fix_evidence_draft(
    evidence: FixEvidence,
    *,
    occurred_at: str | None = None,
) -> _FailureCaseEventDraft:
    if type(evidence) is not FixEvidence:
        _fail(
            "TBM_FAILURE_CASE_STRUCTURED_EVIDENCE_REQUIRED",
            "fix evidence must be exactly FixEvidence",
        )
    recorded = evidence.reviewed_at if occurred_at is None else _timestamp(
        occurred_at, "occurred_at"
    )
    if parse_rfc3339(recorded) < parse_rfc3339(evidence.reviewed_at):
        _fail(
            "TBM_FAILURE_CASE_EVIDENCE_INVALID",
            "fix evidence event cannot precede evidence review",
        )
    payload = {
        "case_id": evidence.case_id,
        "source_trace_id": evidence.source_trace_id,
        "evidence_id": evidence.evidence_id,
        "evidence_sha256": canonical_sha256(evidence.to_dict()),
        "source_commit_sha": evidence.source_commit_sha,
        "fix_commit_sha": evidence.fix_commit_sha,
        "submitter_id": evidence.submitter_id,
        "reviewer_id": evidence.reviewer_id,
        "reviewed_at": evidence.reviewed_at,
        "attestation_sha256": evidence.attestation_sha256,
        "artifact_hashes": list(evidence.artifact_hashes),
    }
    return _FailureCaseEventDraft(
        event_type=FAILURE_CASE_FIX_EVIDENCE_RECORDED,
        case_id=evidence.case_id,
        occurred_at=recorded,
        payload=payload,
        _producer_capability=_DRAFT_PRODUCER_CAPABILITY,
    )


def build_failure_case_regression_evidence_draft(
    evidence: StructuredRegressionEvidence,
    *,
    occurred_at: str | None = None,
) -> _FailureCaseEventDraft:
    if type(evidence) is not StructuredRegressionEvidence:
        _fail(
            "TBM_FAILURE_CASE_STRUCTURED_EVIDENCE_REQUIRED",
            "regression evidence must be exactly StructuredRegressionEvidence",
        )
    recorded = evidence.verified_at if occurred_at is None else _timestamp(
        occurred_at, "occurred_at"
    )
    if parse_rfc3339(recorded) < parse_rfc3339(evidence.verified_at):
        _fail(
            "TBM_FAILURE_CASE_EVIDENCE_INVALID",
            "regression event cannot precede evidence verification",
        )
    payload = {
        "case_id": evidence.case_id,
        "source_trace_id": evidence.source_trace_id,
        "evidence_id": evidence.evidence_id,
        "evidence_sha256": canonical_sha256(evidence.to_dict()),
        "result": evidence.result,
        "source_commit_sha": evidence.source_commit_sha,
        "fix_commit_sha": evidence.fix_commit_sha,
        "verification_commit_sha": evidence.verification_commit_sha,
        "verification_trace_id": evidence.verification_trace_id,
        "verification_run_id": evidence.verification_run_id,
        "submitter_id": evidence.submitter_id,
        "verifier_id": evidence.verifier_id,
        "verified_at": evidence.verified_at,
        "attestation_sha256": evidence.attestation_sha256,
        "artifact_hashes": list(evidence.artifact_hashes),
    }
    return _FailureCaseEventDraft(
        event_type=FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED,
        case_id=evidence.case_id,
        occurred_at=recorded,
        payload=payload,
        _producer_capability=_DRAFT_PRODUCER_CAPABILITY,
    )


def build_legacy_failure_case_import_draft(
    *,
    case_id: str,
    source_trace_id: str,
    failure_type: str,
    regression_passed: bool,
    imported_at: str,
    source: EventSource,
) -> _FailureCaseEventDraft:
    if type(regression_passed) is not bool:
        _fail(
            "TBM_FAILURE_CASE_LEGACY_INVALID",
            "legacy regression_passed must be exactly bool",
        )
    payload = {
        "case_id": case_id,
        "source_trace_id": source_trace_id,
        "failure_type": failure_type,
        "regression_passed": regression_passed,
        "evidence_quality": (
            "legacy_unstructured" if regression_passed else "none"
        ),
        "eligible_for_new_memory": False,
    }
    return _FailureCaseEventDraft(
        event_type=FAILURE_CASE_LEGACY_IMPORTED,
        case_id=case_id,
        occurred_at=imported_at,
        payload=payload,
        source=source,
        _producer_capability=_DRAFT_PRODUCER_CAPABILITY,
    )


def _trace_source(
    events: tuple[CanonicalEvent, ...],
) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...]]:
    if (
        type(events) is not tuple
        or not 1 <= len(events) <= FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _fail(
            "TBM_FAILURE_CASE_TRACE_SOURCE_INVALID",
            "source Trace events must be a bounded non-empty tuple",
        )
    parent: CanonicalEvent | None = None
    trace_id: str | None = None
    run_id: str | None = None
    artifact_ids: set[str] = set()
    stream_id = events[0].stream_id
    source_partition = LedgerTenantPartition(
        organization_id=events[0].organization_id,
        tenant_id=events[0].tenant_id,
        repository_id=events[0].repository_id,
        environment_id=events[0].environment_id,
    )
    for event in events:
        verify_trace_event(event)
        if event.event_type not in TRACE_EVENT_TYPES or event.stream_id != stream_id:
            _fail(
                "TBM_FAILURE_CASE_TRACE_SOURCE_INVALID",
                "source events must belong to one TraceEvent stream",
            )
        if any(
            getattr(event, name) != getattr(source_partition, name)
            for name in (
                "organization_id",
                "tenant_id",
                "repository_id",
                "environment_id",
            )
        ):
            _fail(
                "TBM_FAILURE_CASE_TRACE_SOURCE_INVALID",
                "source Trace partition changed",
            )
        if parent is None:
            if event.stream_version != 1 or event.previous_stream_event_sha256 is not None:
                _fail(
                    "TBM_FAILURE_CASE_TRACE_SOURCE_INVALID",
                    "source Trace stream must begin at version one",
                )
        else:
            verify_event_parent(event, parent)
        payload = _plain_mapping(event.payload)
        current_trace = cast(str, payload["trace_id"])
        current_run = cast(str, payload["run_id"])
        if trace_id is None:
            trace_id, run_id = current_trace, current_run
        elif current_trace != trace_id or current_run != run_id:
            _fail(
                "TBM_FAILURE_CASE_TRACE_SOURCE_INVALID",
                "source Trace identity changed",
            )
        artifact_ids.update(item.artifact_id for item in event.artifact_refs)
        parent = event
    if trace_id is None or run_id is None:
        raise AssertionError("validated TraceEvent stream lacks identity")
    return (
        trace_id,
        run_id,
        source_partition.partition_sha256,
        tuple(event.event_sha256 for event in events),
        tuple(sorted(artifact_ids)),
    )


def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
    def thaw(item: object) -> object:
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for key, child in item.items():
                if type(key) is not str:
                    _fail(
                        "TBM_FAILURE_CASE_EVENT_INVALID",
                        "event payload keys must be strings",
                    )
                result[key] = thaw(child)
            return result
        if type(item) in {list, tuple}:
            return [thaw(child) for child in cast(list[object] | tuple[object, ...], item)]
        return item

    try:
        thawed = thaw(value)
        copied = json.loads(
            json.dumps(thawed, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
    except (TypeError, ValueError) as error:
        raise FailureCaseEventV1Error(
            "TBM_FAILURE_CASE_EVENT_INVALID",
            "event payload must be finite JSON",
        ) from error
    if type(copied) is not dict:
        _fail("TBM_FAILURE_CASE_EVENT_INVALID", "event payload must be an object")
    return cast(dict[str, object], copied)


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, child in value.items():
            if type(key) is not str:
                _fail(
                    "TBM_FAILURE_CASE_EVENT_INVALID",
                    "event payload keys must be strings",
                )
            frozen[key] = _freeze_json(child)
        return MappingProxyType(frozen)
    if type(value) in {list, tuple}:
        return tuple(
            _freeze_json(child)
            for child in cast(list[object] | tuple[object, ...], value)
        )
    return value


def _exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
) -> None:
    if frozenset(payload) != expected:
        _fail(
            "TBM_FAILURE_CASE_EVENT_INVALID",
            "event payload fields do not match the event contract",
        )


def _payload_string_list(
    payload: Mapping[str, object],
    field: str,
    *,
    pattern: re.Pattern[str],
    maximum: int,
    non_empty: bool = False,
    sorted_required: bool = False,
) -> tuple[str, ...]:
    value = payload.get(field)
    if type(value) not in {list, tuple}:
        _fail("TBM_FAILURE_CASE_EVENT_INVALID", f"{field} must be an array")
    return _tuple_of_unique_strings(
        tuple(cast(list[object] | tuple[object, ...], value)),
        field=field,
        pattern=pattern,
        maximum=maximum,
        non_empty=non_empty,
        sorted_required=sorted_required,
    )


def _validate_draft_payload(
    event_type: str,
    payload: Mapping[str, object],
    refs: tuple[EventArtifactRef, ...],
) -> None:
    _identifier(payload.get("case_id"), "case_id")
    ref_ids = tuple(item.artifact_id for item in refs)
    if event_type == FAILURE_CASE_EXTRACTOR_PROPOSED:
        _exact_fields(
            payload,
            frozenset(
                {
                    "proposal_id",
                    "case_id",
                    "source_trace_id",
                    "source_run_id",
                    "source_partition_sha256",
                    "source_event_sha256s",
                    "source_artifact_ids",
                    "proposal_artifact_ids",
                    "failure_type",
                    "extractor_id",
                    "extractor_version",
                    "extractor_configuration_sha256",
                    "proposed_at",
                    "candidate_status",
                }
            ),
        )
        _code(payload.get("proposal_id"), "proposal_id")
        _identifier(payload.get("source_trace_id"), "source_trace_id")
        _identifier(payload.get("source_run_id"), "source_run_id")
        _digest(payload.get("source_partition_sha256"), "source_partition_sha256")
        _payload_string_list(
            payload,
            "source_event_sha256s",
            pattern=_EVENT_SHA256_RE,
            maximum=FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS,
            non_empty=True,
        )
        _payload_string_list(
            payload,
            "source_artifact_ids",
            pattern=_ARTIFACT_ID_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            sorted_required=True,
        )
        proposal_ids = _payload_string_list(
            payload,
            "proposal_artifact_ids",
            pattern=_ARTIFACT_ID_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            non_empty=True,
            sorted_required=True,
        )
        if proposal_ids != ref_ids:
            _fail(
                "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
                "proposal payload does not match its Artifact descriptors",
            )
        _code(payload.get("failure_type"), "failure_type")
        _identifier(payload.get("extractor_id"), "extractor_id")
        _code(payload.get("extractor_version"), "extractor_version")
        _digest(
            payload.get("extractor_configuration_sha256"),
            "extractor_configuration_sha256",
        )
        _timestamp(payload.get("proposed_at"), "proposed_at")
        if payload.get("candidate_status") != "candidate":
            _fail(
                "TBM_FAILURE_CASE_EXTRACTOR_CANNOT_VERIFY",
                "extractor output must remain a candidate",
            )
        unsigned = dict(payload)
        proposal_id = cast(str, unsigned.pop("proposal_id"))
        if proposal_id != failure_case_proposal_id(unsigned):
            _fail(
                "TBM_FAILURE_CASE_PROPOSAL_HASH_MISMATCH",
                "proposal payload hash is invalid",
            )
        return
    if event_type == FAILURE_CASE_REVIEWED:
        _exact_fields(
            payload,
            frozenset(
                {
                    "case_id",
                    "proposal_id",
                    "reviewer_id",
                    "decision",
                    "reason_code",
                    "reviewed_at",
                    "attestation_sha256",
                    "review_artifact_ids",
                }
            ),
        )
        _code(payload.get("proposal_id"), "proposal_id")
        _identifier(payload.get("reviewer_id"), "reviewer_id")
        if payload.get("decision") not in {"accepted", "rejected"}:
            _fail("TBM_FAILURE_CASE_REVIEW_INVALID", "decision is invalid")
        _code(payload.get("reason_code"), "reason_code")
        _timestamp(payload.get("reviewed_at"), "reviewed_at")
        _digest(payload.get("attestation_sha256"), "attestation_sha256")
        artifact_ids = _payload_string_list(
            payload,
            "review_artifact_ids",
            pattern=_ARTIFACT_ID_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            sorted_required=True,
        )
        if artifact_ids != ref_ids:
            _fail(
                "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
                "review payload does not match its Artifact descriptors",
            )
        return
    if event_type == FAILURE_CASE_FIX_EVIDENCE_RECORDED:
        _exact_fields(
            payload,
            frozenset(
                {
                    "case_id",
                    "source_trace_id",
                    "evidence_id",
                    "evidence_sha256",
                    "source_commit_sha",
                    "fix_commit_sha",
                    "submitter_id",
                    "reviewer_id",
                    "reviewed_at",
                    "attestation_sha256",
                    "artifact_hashes",
                }
            ),
        )
        _identifier(payload.get("source_trace_id"), "source_trace_id")
        _code(payload.get("evidence_id"), "evidence_id")
        _digest(payload.get("evidence_sha256"), "evidence_sha256")
        _code(payload.get("source_commit_sha"), "source_commit_sha")
        _code(payload.get("fix_commit_sha"), "fix_commit_sha")
        _identifier(payload.get("submitter_id"), "submitter_id")
        _identifier(payload.get("reviewer_id"), "reviewer_id")
        _timestamp(payload.get("reviewed_at"), "reviewed_at")
        _digest(payload.get("attestation_sha256"), "attestation_sha256")
        _payload_string_list(
            payload,
            "artifact_hashes",
            pattern=_DIGEST_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            sorted_required=True,
        )
        if refs:
            _fail(
                "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
                "structured evidence events carry hashes, not unbound descriptors",
            )
        return
    if event_type == FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED:
        _exact_fields(
            payload,
            frozenset(
                {
                    "case_id",
                    "source_trace_id",
                    "evidence_id",
                    "evidence_sha256",
                    "result",
                    "source_commit_sha",
                    "fix_commit_sha",
                    "verification_commit_sha",
                    "verification_trace_id",
                    "verification_run_id",
                    "submitter_id",
                    "verifier_id",
                    "verified_at",
                    "attestation_sha256",
                    "artifact_hashes",
                }
            ),
        )
        for field in (
            "source_trace_id",
            "verification_trace_id",
            "verification_run_id",
            "submitter_id",
            "verifier_id",
        ):
            _identifier(payload.get(field), field)
        _code(payload.get("evidence_id"), "evidence_id")
        _digest(payload.get("evidence_sha256"), "evidence_sha256")
        if payload.get("result") not in {"pass", "fail", "error"}:
            _fail("TBM_FAILURE_CASE_EVIDENCE_INVALID", "result is invalid")
        for field in (
            "source_commit_sha",
            "fix_commit_sha",
            "verification_commit_sha",
        ):
            _code(payload.get(field), field)
        _timestamp(payload.get("verified_at"), "verified_at")
        _digest(payload.get("attestation_sha256"), "attestation_sha256")
        _payload_string_list(
            payload,
            "artifact_hashes",
            pattern=_DIGEST_RE,
            maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
            sorted_required=True,
        )
        if refs:
            _fail(
                "TBM_FAILURE_CASE_EVENT_ARTIFACT_INVALID",
                "structured evidence events carry hashes, not unbound descriptors",
            )
        return
    if event_type == FAILURE_CASE_LEGACY_IMPORTED:
        _exact_fields(
            payload,
            frozenset(
                {
                    "case_id",
                    "source_trace_id",
                    "failure_type",
                    "regression_passed",
                    "evidence_quality",
                    "eligible_for_new_memory",
                }
            ),
        )
        _identifier(payload.get("source_trace_id"), "source_trace_id")
        _code(payload.get("failure_type"), "failure_type")
        regression_passed = payload.get("regression_passed")
        if type(regression_passed) is not bool:
            _fail(
                "TBM_FAILURE_CASE_LEGACY_INVALID",
                "regression_passed must be exactly bool",
            )
        expected_quality = "legacy_unstructured" if regression_passed else "none"
        if (
            payload.get("evidence_quality") != expected_quality
            or payload.get("eligible_for_new_memory") is not False
            or refs
        ):
            _fail(
                "TBM_FAILURE_CASE_LEGACY_INVALID",
                "legacy evidence must remain downgraded and ineligible",
            )
        return
    raise AssertionError("unregistered FailureCase event type")


def _string_schema(*, pattern: str, max_length: int = 256) -> dict[str, object]:
    return {"type": "string", "pattern": pattern, "maxLength": max_length}


def _array_schema(
    *,
    pattern: str,
    maximum: int,
    minimum: int = 0,
) -> dict[str, object]:
    return {
        "type": "array",
        "items": _string_schema(pattern=pattern),
        "minItems": minimum,
        "maxItems": maximum,
        "uniqueItems": True,
    }


def _object_schema(
    required: tuple[str, ...],
    properties: Mapping[str, object],
) -> dict[str, object]:
    return {
        "type": "object",
        "required": list(required),
        "additionalProperties": False,
        "properties": dict(properties),
    }


def _payload_json_schemas() -> dict[str, dict[str, object]]:
    identifier = _string_schema(pattern=_IDENTIFIER_RE.pattern, max_length=128)
    code = _string_schema(pattern=_CODE_RE.pattern)
    digest = _string_schema(pattern=_DIGEST_RE.pattern, max_length=71)
    artifact_id = _ARTIFACT_ID_RE.pattern
    timestamp = {
        "type": "string",
        "pattern": (
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
            r"[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
        ),
        "maxLength": 40,
    }
    common = {"case_id": identifier}
    return {
        FAILURE_CASE_EXTRACTOR_PROPOSED: _object_schema(
            (
                "proposal_id",
                "case_id",
                "source_trace_id",
                "source_run_id",
                "source_partition_sha256",
                "source_event_sha256s",
                "source_artifact_ids",
                "proposal_artifact_ids",
                "failure_type",
                "extractor_id",
                "extractor_version",
                "extractor_configuration_sha256",
                "proposed_at",
                "candidate_status",
            ),
            {
                **common,
                "proposal_id": code,
                "source_trace_id": identifier,
                "source_run_id": identifier,
                "source_partition_sha256": digest,
                "source_event_sha256s": _array_schema(
                    pattern=_DIGEST_RE.pattern,
                    maximum=FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS,
                    minimum=1,
                ),
                "source_artifact_ids": _array_schema(
                    pattern=artifact_id,
                    maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
                ),
                "proposal_artifact_ids": _array_schema(
                    pattern=artifact_id,
                    maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
                    minimum=1,
                ),
                "failure_type": code,
                "extractor_id": identifier,
                "extractor_version": code,
                "extractor_configuration_sha256": digest,
                "proposed_at": timestamp,
                "candidate_status": {"const": "candidate"},
            },
        ),
        FAILURE_CASE_REVIEWED: _object_schema(
            (
                "case_id",
                "proposal_id",
                "reviewer_id",
                "decision",
                "reason_code",
                "reviewed_at",
                "attestation_sha256",
                "review_artifact_ids",
            ),
            {
                **common,
                "proposal_id": code,
                "reviewer_id": identifier,
                "decision": {"enum": ["accepted", "rejected"]},
                "reason_code": code,
                "reviewed_at": timestamp,
                "attestation_sha256": digest,
                "review_artifact_ids": _array_schema(
                    pattern=artifact_id,
                    maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
                ),
            },
        ),
        FAILURE_CASE_FIX_EVIDENCE_RECORDED: _object_schema(
            (
                "case_id",
                "source_trace_id",
                "evidence_id",
                "evidence_sha256",
                "source_commit_sha",
                "fix_commit_sha",
                "submitter_id",
                "reviewer_id",
                "reviewed_at",
                "attestation_sha256",
                "artifact_hashes",
            ),
            {
                **common,
                "source_trace_id": identifier,
                "evidence_id": code,
                "evidence_sha256": digest,
                "source_commit_sha": code,
                "fix_commit_sha": code,
                "submitter_id": identifier,
                "reviewer_id": identifier,
                "reviewed_at": timestamp,
                "attestation_sha256": digest,
                "artifact_hashes": _array_schema(
                    pattern=_DIGEST_RE.pattern,
                    maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
                ),
            },
        ),
        FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED: _object_schema(
            (
                "case_id",
                "source_trace_id",
                "evidence_id",
                "evidence_sha256",
                "result",
                "source_commit_sha",
                "fix_commit_sha",
                "verification_commit_sha",
                "verification_trace_id",
                "verification_run_id",
                "submitter_id",
                "verifier_id",
                "verified_at",
                "attestation_sha256",
                "artifact_hashes",
            ),
            {
                **common,
                "source_trace_id": identifier,
                "evidence_id": code,
                "evidence_sha256": digest,
                "result": {"enum": ["pass", "fail", "error"]},
                "source_commit_sha": code,
                "fix_commit_sha": code,
                "verification_commit_sha": code,
                "verification_trace_id": identifier,
                "verification_run_id": identifier,
                "submitter_id": identifier,
                "verifier_id": identifier,
                "verified_at": timestamp,
                "attestation_sha256": digest,
                "artifact_hashes": _array_schema(
                    pattern=_DIGEST_RE.pattern,
                    maximum=FAILURE_CASE_EVENT_MAX_ARTIFACTS,
                ),
            },
        ),
        FAILURE_CASE_LEGACY_IMPORTED: _object_schema(
            (
                "case_id",
                "source_trace_id",
                "failure_type",
                "regression_passed",
                "evidence_quality",
                "eligible_for_new_memory",
            ),
            {
                **common,
                "source_trace_id": identifier,
                "failure_type": code,
                "regression_passed": {"type": "boolean"},
                "evidence_quality": {
                    "enum": ["none", "legacy_unstructured"]
                },
                "eligible_for_new_memory": {"const": False},
            },
        ),
    }


def build_failure_case_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    schemas = _payload_json_schemas()
    for event_type in FAILURE_CASE_EVENT_TYPES:
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


def failure_case_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_failure_case_event_registry().dispatch_schema()
    schema["$id"] = FAILURE_CASE_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory FailureCase event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed FailureCase event registry; extractor "
        "output remains an unverified candidate."
    )
    return schema


def dumps_failure_case_event_payload_dispatch_schema() -> str:
    return json.dumps(
        failure_case_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_failure_case_event_batch(
    drafts: tuple[_FailureCaseEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= EVENT_LEDGER_MAX_APPEND_BATCH
        or any(type(item) is not _FailureCaseEventDraft for item in drafts)
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_BATCH_INVALID",
            "drafts must be a bounded non-empty tuple",
        )
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_FAILURE_CASE_EVENT_ACCESS_INVALID",
            "access must be exactly LedgerAccessContext",
        )
    if (
        type(expected_stream_version) is not int
        or not 0 <= expected_stream_version <= _MAX_POSITION
        or type(next_global_position) is not int
        or not 1 <= next_global_position <= _MAX_POSITION
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_BATCH_INVALID",
            "stream or global position is invalid",
        )
    case_id = drafts[0].case_id
    if any(item.case_id != case_id for item in drafts):
        _fail(
            "TBM_FAILURE_CASE_EVENT_BATCH_INVALID",
            "one batch must target one FailureCase",
        )
    proposals = tuple(
        item for item in drafts if item.event_type == FAILURE_CASE_EXTRACTOR_PROPOSED
    )
    if any(
        item.payload.get("source_partition_sha256")
        != access.partition.partition_sha256
        for item in proposals
    ):
        _fail(
            "TBM_FAILURE_CASE_SOURCE_PARTITION_MISMATCH",
            "source Trace and FailureCase event partitions must match",
        )
    stream_id = failure_case_event_stream_id(case_id)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_FAILURE_CASE_EVENT_BATCH_INVALID",
                "nonzero stream version requires the current parent",
            )
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_BATCH_INVALID",
            "previous event does not match the FailureCase stream",
        )
    canonical_recorded_at = _timestamp(recorded_at, "recorded_at")
    parent = previous_event
    previous_occurred = parent.occurred_at if parent is not None else None
    for draft in drafts:
        if (
            previous_occurred is not None
            and parse_rfc3339(draft.occurred_at)
            < parse_rfc3339(previous_occurred)
        ):
            _fail(
                "TBM_FAILURE_CASE_EVENT_SEQUENCE_INVALID",
                "event timestamps cannot move backwards",
            )
        previous_occurred = draft.occurred_at
    command = {
        "protocol_version": FAILURE_CASE_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "recorded_at": canonical_recorded_at,
        "drafts": [item.command_value() for item in drafts],
    }
    command_sha256 = canonical_sha256(command)
    idempotency_key_sha256 = canonical_sha256(
        {"domain": "tbm.failure-case-command.v1", "command": command}
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    correlation_digest = hashlib.sha256(
        (access.partition.partition_sha256 + "\x00" + case_id).encode("utf-8")
    ).hexdigest()
    events: list[CanonicalEvent] = []
    parent = previous_event
    for offset, draft in enumerate(drafts):
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        imported = draft.event_type == FAILURE_CASE_LEGACY_IMPORTED
        event = build_canonical_event(
            event_id="evt_failure_case_" + event_digest,
            event_type=draft.event_type,
            event_version=1,
            event_kind="domain",
            origin="imported" if imported else "native",
            source=draft.source,
            stream_id=stream_id,
            stream_type=FAILURE_CASE_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_failure_case_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_failure_case_" + correlation_digest[:32],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=draft.occurred_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_failure_case_event_adapter",
            producer_version="f4-v1",
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification=draft.classification,
            retention_policy_id=draft.retention_policy_id,
            artifact_refs=draft.artifact_refs,
            payload=draft.payload,
        )
        verify_failure_case_event(event)
        if parent is not None:
            verify_event_parent(event, parent)
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_failure_case_event_append_request(
    drafts: tuple[_FailureCaseEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> LedgerAppendRequest:
    events, idempotency = build_failure_case_event_batch(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )
    return LedgerAppendRequest(
        access=access,
        stream_id=events[0].stream_id,
        expected_stream_version=expected_stream_version,
        events=events,
        idempotency=idempotency,
    )


def append_failure_case_event_batch(
    ledger: EventLedgerPort,
    drafts: tuple[_FailureCaseEventDraft, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> LedgerAppendReceipt:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not callable(
        getattr(ledger, "append", None)
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_LEDGER_INVALID",
            "append requires an access-bound EventLedgerPort",
        )
    request = build_failure_case_event_append_request(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )
    receipt = ledger.append(
        request.stream_id,
        request.expected_stream_version,
        request.events,
        request.idempotency,
    )
    verify_ledger_append_receipt(request, receipt)
    return receipt


def verify_failure_case_event(event: CanonicalEvent) -> None:
    if type(event) is not CanonicalEvent:
        _fail(
            "TBM_FAILURE_CASE_EVENT_INVALID",
            "event must be exactly CanonicalEvent",
        )
    if (
        event.event_type not in FAILURE_CASE_EVENT_TYPES
        or event.event_version != 1
        or event.event_kind != "domain"
        or event.stream_type != FAILURE_CASE_EVENT_STREAM_TYPE
        or event.payload_schema != _PAYLOAD_SCHEMAS[event.event_type]
        or event.producer != "tbm_failure_case_event_adapter"
        or event.producer_version != "f4-v1"
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_INVALID",
            "event envelope is not the FailureCase protocol",
        )
    payload = build_failure_case_event_registry().consume(
        event, target_version=1
    ).payload
    case_id = _identifier(payload.get("case_id"), "case_id")
    if event.stream_id != failure_case_event_stream_id(case_id):
        _fail(
            "TBM_FAILURE_CASE_EVENT_INVALID",
            "event stream identity does not match case_id",
        )
    _validate_draft_payload(event.event_type, payload, event.artifact_refs)
    imported = event.event_type == FAILURE_CASE_LEGACY_IMPORTED
    if imported != (event.origin == "imported"):
        _fail(
            "TBM_FAILURE_CASE_EVENT_INVALID",
            "legacy origin does not match event type",
        )


def build_failure_case_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=FAILURE_CASE_REDUCER_ID,
        reducer_version=1,
        input_event_types=FAILURE_CASE_EVENT_TYPES,
        output_projection=FAILURE_CASE_PROJECTION,
        output_schema_version=1,
        code_sha256=canonical_sha256(
            {
                "algorithm": "failure-case-current",
                "algorithm_version": 1,
                "candidate_only_extractor": True,
                "legacy_boolean_quality": "legacy_unstructured",
                "structured_verification_requires": [
                    "accepted_independent_review",
                    "fix_evidence",
                    "passing_regression_evidence",
                    "exact_trace_and_commit_links",
                ],
                "input_event_types": list(FAILURE_CASE_EVENT_TYPES),
            }
        ),
        configuration_sha256=canonical_sha256(
            {
                "max_events": FAILURE_CASE_EVENT_MAX_EVENTS,
                "max_source_events": FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS,
                "max_artifacts": FAILURE_CASE_EVENT_MAX_ARTIFACTS,
                "version": 1,
            }
        ),
        target_event_versions={
            event_type: 1 for event_type in FAILURE_CASE_EVENT_TYPES
        },
    )

    def initial() -> Mapping[str, object]:
        return {
            "case_id": None,
            "source_trace_id": None,
            "source_run_id": None,
            "proposal_id": None,
            "failure_type": None,
            "status": None,
            "evidence_quality": "none",
            "extractor_id": None,
            "reviewer_id": None,
            "review_decision": None,
            "fix_evidence_id": None,
            "fix_evidence_sha256": None,
            "source_commit_sha": None,
            "fix_commit_sha": None,
            "regression_evidence_ids": [],
            "regression_evidence_id": None,
            "regression_result": None,
            "eligible_for_new_memory": False,
            "source_event_sha256s": [],
            "artifact_ids": [],
            "last_event_sha256": None,
            "last_global_position": 0,
            "last_stream_version": 0,
            "last_occurred_at": None,
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        typed = reducer_event.typed_event
        if typed is None:
            _fail(
                "TBM_FAILURE_CASE_TYPED_INPUT_REQUIRED",
                "FailureCase reducer requires typed event input",
            )
        event = reducer_event.source_event
        verify_failure_case_event(event)
        payload = _plain_mapping(typed.payload)
        next_state = _plain_mapping(state)
        _apply_failure_case_event(next_state, event, payload)
        return next_state

    return FunctionalReducer(descriptor, initial, transition)


def _apply_failure_case_event(
    state: dict[str, object],
    event: CanonicalEvent,
    payload: Mapping[str, object],
) -> None:
    if event.occurred_at is None:
        _fail(
            "TBM_FAILURE_CASE_EVENT_SEQUENCE_INVALID",
            "FailureCase events require occurrence timestamps",
        )
    previous_occurred = state.get("last_occurred_at")
    if (
        type(previous_occurred) is str
        and parse_rfc3339(event.occurred_at) < parse_rfc3339(previous_occurred)
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_SEQUENCE_INVALID",
            "FailureCase event timestamps cannot move backwards",
        )
    if event.stream_version != cast(int, state["last_stream_version"]) + 1:
        _fail(
            "TBM_FAILURE_CASE_EVENT_SEQUENCE_INVALID",
            "FailureCase stream version is not contiguous",
        )
    event_type = event.event_type
    if event_type == FAILURE_CASE_EXTRACTOR_PROPOSED:
        if state["case_id"] is not None:
            _fail(
                "TBM_FAILURE_CASE_TRANSITION_INVALID",
                "extractor proposal cannot replace an existing case",
            )
        state.update(
            {
                "case_id": payload["case_id"],
                "source_trace_id": payload["source_trace_id"],
                "source_run_id": payload["source_run_id"],
                "proposal_id": payload["proposal_id"],
                "failure_type": payload["failure_type"],
                "status": "candidate",
                "evidence_quality": "none",
                "extractor_id": payload["extractor_id"],
                "source_event_sha256s": payload["source_event_sha256s"],
                "artifact_ids": payload["proposal_artifact_ids"],
            }
        )
    elif event_type == FAILURE_CASE_LEGACY_IMPORTED:
        if state["case_id"] is not None:
            _fail(
                "TBM_FAILURE_CASE_TRANSITION_INVALID",
                "legacy import cannot replace an existing case",
            )
        state.update(
            {
                "case_id": payload["case_id"],
                "source_trace_id": payload["source_trace_id"],
                "failure_type": payload["failure_type"],
                "status": "legacy_imported",
                "evidence_quality": payload["evidence_quality"],
                "eligible_for_new_memory": False,
            }
        )
    elif event_type == FAILURE_CASE_REVIEWED:
        if (
            state["status"] != "candidate"
            or state["review_decision"] is not None
            or payload["proposal_id"] != state["proposal_id"]
        ):
            _fail(
                "TBM_FAILURE_CASE_TRANSITION_INVALID",
                "review requires the exact unreviewed extractor proposal",
            )
        if payload["reviewer_id"] == state["extractor_id"]:
            _fail(
                "TBM_FAILURE_CASE_REVIEW_INDEPENDENCE_REQUIRED",
                "extractor cannot review its own proposal",
            )
        decision = payload["decision"]
        state.update(
            {
                "reviewer_id": payload["reviewer_id"],
                "review_decision": decision,
                "status": "reviewed" if decision == "accepted" else "rejected",
                "artifact_ids": sorted(
                    set(cast(list[str], state["artifact_ids"]))
                    | set(cast(list[str], payload["review_artifact_ids"]))
                ),
            }
        )
    elif event_type == FAILURE_CASE_FIX_EVIDENCE_RECORDED:
        if (
            state["status"] != "reviewed"
            or state["review_decision"] != "accepted"
            or state["fix_evidence_id"] is not None
            or payload["case_id"] != state["case_id"]
            or payload["source_trace_id"] != state["source_trace_id"]
        ):
            _fail(
                "TBM_FAILURE_CASE_TRANSITION_INVALID",
                "FixEvidence requires the accepted matching proposal",
            )
        state.update(
            {
                "fix_evidence_id": payload["evidence_id"],
                "fix_evidence_sha256": payload["evidence_sha256"],
                "source_commit_sha": payload["source_commit_sha"],
                "fix_commit_sha": payload["fix_commit_sha"],
            }
        )
    elif event_type == FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED:
        evidence_ids = cast(list[str], state["regression_evidence_ids"])
        if (
            state["status"] not in {"reviewed"}
            or state["review_decision"] != "accepted"
            or state["fix_evidence_id"] is None
            or payload["case_id"] != state["case_id"]
            or payload["source_trace_id"] != state["source_trace_id"]
            or payload["source_commit_sha"] != state["source_commit_sha"]
            or payload["fix_commit_sha"] != state["fix_commit_sha"]
            or payload["evidence_id"] in evidence_ids
        ):
            _fail(
                "TBM_FAILURE_CASE_TRANSITION_INVALID",
                "regression evidence does not match the reviewed fix",
            )
        if payload["submitter_id"] == payload["verifier_id"] or payload[
            "verifier_id"
        ] in {
            state["extractor_id"],
            state["reviewer_id"],
        }:
            _fail(
                "TBM_FAILURE_CASE_EVIDENCE_INDEPENDENCE_REQUIRED",
                "regression verifier must be independent of extraction and review",
            )
        evidence_ids.append(cast(str, payload["evidence_id"]))
        state["regression_evidence_ids"] = evidence_ids
        state["regression_evidence_id"] = payload["evidence_id"]
        state["regression_result"] = payload["result"]
        if payload["result"] == "pass":
            state["status"] = "verified"
            state["evidence_quality"] = "structured_verified"
            state["eligible_for_new_memory"] = True
    else:
        raise AssertionError("unregistered FailureCase event type")
    state["last_event_sha256"] = event.event_sha256
    state["last_global_position"] = event.global_position
    state["last_stream_version"] = event.stream_version
    state["last_occurred_at"] = event.occurred_at


def reduce_failure_case_events(
    events: tuple[CanonicalEvent, ...],
    *,
    event_registry: EventTypeRegistry | None = None,
) -> FailureCaseProjection | None:
    if (
        type(events) is not tuple
        or len(events) > FAILURE_CASE_EVENT_MAX_EVENTS
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _fail(
            "TBM_FAILURE_CASE_EVENT_SEQUENCE_INVALID",
            "events must be a bounded tuple of CanonicalEvent values",
        )
    if not events:
        return None
    registry = (
        build_failure_case_event_registry()
        if event_registry is None
        else event_registry
    )
    if type(registry) is not EventTypeRegistry or not registry.sealed:
        _fail(
            "TBM_FAILURE_CASE_EVENT_REGISTRY_INVALID",
            "event registry must be sealed",
        )
    reducer = build_failure_case_reducer()
    step = initial_reducer_state(reducer)
    parent: CanonicalEvent | None = None
    stream_id = events[0].stream_id
    for event in events:
        verify_failure_case_event(event)
        if parent is None:
            if event.stream_version != 1 or event.previous_stream_event_sha256 is not None:
                _fail(
                    "TBM_FAILURE_CASE_EVENT_SEQUENCE_INVALID",
                    "FailureCase stream must begin at version one",
                )
        else:
            verify_event_parent(event, parent)
        if event.stream_id != stream_id:
            _fail(
                "TBM_FAILURE_CASE_EVENT_SEQUENCE_INVALID",
                "events must belong to one FailureCase stream",
            )
        typed = registry.consume(event, target_version=1)
        step = execute_reducer_step(
            reducer,
            step.state,
            ReducerEvent(event, typed),
        )
        parent = event
    return _projection_from_state(step.state)


def _projection_from_state(
    state: Mapping[str, object],
) -> FailureCaseProjection:
    item = _plain_mapping(state)
    if item.get("case_id") is None or item.get("last_event_sha256") is None:
        _fail(
            "TBM_FAILURE_CASE_PROJECTION_INVALID",
            "projection state has no FailureCase",
        )
    unsigned: dict[str, object] = {
        "case_id": item["case_id"],
        "source_trace_id": item["source_trace_id"],
        "source_run_id": item["source_run_id"],
        "proposal_id": item["proposal_id"],
        "failure_type": item["failure_type"],
        "status": item["status"],
        "evidence_quality": item["evidence_quality"],
        "extractor_id": item["extractor_id"],
        "reviewer_id": item["reviewer_id"],
        "review_decision": item["review_decision"],
        "fix_evidence_id": item["fix_evidence_id"],
        "regression_evidence_id": item["regression_evidence_id"],
        "regression_result": item["regression_result"],
        "eligible_for_new_memory": item["eligible_for_new_memory"],
        "source_event_sha256s": item["source_event_sha256s"],
        "artifact_ids": item["artifact_ids"],
        "last_event_sha256": item["last_event_sha256"],
        "last_global_position": item["last_global_position"],
    }
    return FailureCaseProjection(
        case_id=cast(str, unsigned["case_id"]),
        source_trace_id=cast(str, unsigned["source_trace_id"]),
        source_run_id=cast(str | None, unsigned["source_run_id"]),
        proposal_id=cast(str | None, unsigned["proposal_id"]),
        failure_type=cast(str | None, unsigned["failure_type"]),
        status=cast(FailureCaseProjectionStatus, unsigned["status"]),
        evidence_quality=cast(
            FailureCaseEvidenceQuality, unsigned["evidence_quality"]
        ),
        extractor_id=cast(str | None, unsigned["extractor_id"]),
        reviewer_id=cast(str | None, unsigned["reviewer_id"]),
        review_decision=cast(
            FailureCaseReviewDecision | None, unsigned["review_decision"]
        ),
        fix_evidence_id=cast(str | None, unsigned["fix_evidence_id"]),
        regression_evidence_id=cast(
            str | None, unsigned["regression_evidence_id"]
        ),
        regression_result=cast(
            Literal["pass", "fail", "error"] | None,
            unsigned["regression_result"],
        ),
        eligible_for_new_memory=cast(
            bool, unsigned["eligible_for_new_memory"]
        ),
        source_event_sha256s=tuple(
            cast(list[str], unsigned["source_event_sha256s"])
        ),
        artifact_ids=tuple(cast(list[str], unsigned["artifact_ids"])),
        last_event_sha256=cast(str, unsigned["last_event_sha256"]),
        last_global_position=cast(int, unsigned["last_global_position"]),
        projection_sha256=canonical_sha256(unsigned),
    )


__all__ = [
    "FAILURE_CASE_EVENT_MAX_ARTIFACTS",
    "FAILURE_CASE_EVENT_MAX_EVENTS",
    "FAILURE_CASE_EVENT_MAX_SOURCE_EVENTS",
    "FAILURE_CASE_EVENT_PAYLOAD_SCHEMA_ID",
    "FAILURE_CASE_EVENT_PROTOCOL_VERSION",
    "FAILURE_CASE_EVENT_STREAM_TYPE",
    "FAILURE_CASE_EVENT_TYPES",
    "FAILURE_CASE_EXTRACTOR_PROPOSED",
    "FAILURE_CASE_FIX_EVIDENCE_RECORDED",
    "FAILURE_CASE_LEGACY_IMPORTED",
    "FAILURE_CASE_PROJECTION",
    "FAILURE_CASE_REDUCER_ID",
    "FAILURE_CASE_REGRESSION_EVIDENCE_RECORDED",
    "FAILURE_CASE_REVIEWED",
    "FailureCaseEventV1Error",
    "FailureCaseEvidenceQuality",
    "FailureCaseExtractorProposal",
    "FailureCaseProjection",
    "FailureCaseProjectionStatus",
    "FailureCaseReviewDecision",
    "append_failure_case_event_batch",
    "build_failure_case_event_append_request",
    "build_failure_case_event_batch",
    "build_failure_case_event_registry",
    "build_failure_case_extractor_proposal",
    "build_failure_case_fix_evidence_draft",
    "build_failure_case_proposal_draft",
    "build_failure_case_reducer",
    "build_failure_case_regression_evidence_draft",
    "build_failure_case_review_draft",
    "build_legacy_failure_case_import_draft",
    "dumps_failure_case_event_payload_dispatch_schema",
    "failure_case_event_payload_dispatch_schema",
    "failure_case_event_stream_id",
    "failure_case_proposal_id",
    "reduce_failure_case_events",
    "verify_failure_case_event",
]
