from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import re
from typing import Literal, NoReturn, cast

from ._ingestion import parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .authorization_v3 import (
    AuthorizationDecision,
    AuthorizationPermission,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    parse_authorization_decision,
    parse_authorization_policy,
    verify_authorization_decision,
)
from .contracts_v3 import canonical_sha256
from .event_registry_v1 import EventPayloadRegistration, EventTypeRegistry
from .event_v1 import CanonicalEvent, build_canonical_event, verify_event_parent
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerConflictError,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerGlobalReadRequest,
    LedgerIdempotency,
    LedgerTenantPartition,
    verify_ledger_append_receipt,
    verify_ledger_global_page,
)
from .outcome_effect_event_v1 import (
    OUTCOME_ATTRIBUTION_RECORDED,
    OUTCOME_EFFECT_EVENT_STREAM_TYPE,
    RUN_OUTCOME_RECORDED,
    build_outcome_effect_event_registry,
    outcome_effect_stream_id,
)
from .outcome_v3 import (
    OutcomeAttribution,
    RunOutcome,
    loads_outcome_attribution,
    loads_run_outcome,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerV1Error,
    execute_reducer_step,
    initial_reducer_state,
)


OUTCOME_HARM_EVENT_PROTOCOL_VERSION = "tbm.outcome-harm-event.v1"
OUTCOME_HARM_EVENT_STREAM_TYPE = "outcome_harm_context"
OUTCOME_HARM_EVENT_PROJECTION = "outcome_harm_metrics_v1"
OUTCOME_HARM_EVENT_REDUCER_ID = "outcome-harm-metrics"
OUTCOME_HARM_EVENT_MAX_BATCH = 16
OUTCOME_HARM_EVENT_MAX_CONTEXTS = 10_000
OUTCOME_HARM_EVENT_MAX_SOURCE_EVENTS = 50_000
OUTCOME_HARM_EVENT_MAX_LEDGER_SCAN = 1_000_000
OUTCOME_HARM_JSON_MAX_BYTES = 512 * 1024
OUTCOME_HARM_JSON_MAX_DEPTH = 32
OUTCOME_HARM_JSON_MAX_NODES = 50_000
OUTCOME_HARM_CONTEXT_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "outcome_evaluation_context_v1.schema.json"
)
OUTCOME_HARM_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "outcome_harm_event_payload_registry_v1.schema.json"
)

OUTCOME_EVALUATION_CONTEXT_BOUND = "tbm.outcome.evaluation_context_bound"
OUTCOME_HARM_INPUT_EVENT_TYPES = tuple(
    sorted(
        (
            RUN_OUTCOME_RECORDED,
            OUTCOME_ATTRIBUTION_RECORDED,
            OUTCOME_EVALUATION_CONTEXT_BOUND,
        )
    )
)
_PAYLOAD_SCHEMAS = {
    OUTCOME_EVALUATION_CONTEXT_BOUND: (
        OUTCOME_EVALUATION_CONTEXT_BOUND + ".v1"
    )
}
_ALL_CLASSIFICATIONS = (
    "public",
    "internal",
    "confidential",
    "restricted",
)
_RESULTS = ("pass", "fail", "error")
_EFFECTS = ("helped", "harmed", "neutral", "unknown")
_CAUSAL_METHODS = (
    "controlled_experiment",
    "manual_review",
    "external_evaluation",
)
_COHORT_ARMS = ("observational", "with_memory", "without_memory")
_ASSIGNMENT_METHODS = ("randomized", "matched_control", "manual")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTEXT_ID_RE = re.compile(
    r"^outcome_context_sha256_[0-9a-f]{64}$"
)
_POLICY_ID_RE = re.compile(r"^harm_policy_sha256_[0-9a-f]{64}$")
_SIGNAL_ID_RE = re.compile(r"^harm_signal_sha256_[0-9a-f]{64}$")
_RECOMMENDATION_ID_RE = re.compile(
    r"^suspension_recommendation_sha256_[0-9a-f]{64}$"
)
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")


class OutcomeHarmEventV1Error(ReducerV1Error):
    """Stable outcome/harm projection and context-event failure."""


def _fail(code: str, message: str) -> NoReturn:
    raise OutcomeHarmEventV1Error(code, message)


def _record_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_HARM_RECORD_INVALID", message)


def _transition_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_HARM_TRANSITION_INVALID", message)


def _projection_invalid(message: str) -> NoReturn:
    _fail("TBM_OUTCOME_HARM_PROJECTION_INVALID", message)


@dataclass(frozen=True)
class OutcomeHarmPolicy:
    policy_id: str
    minimum_verified_harmed_claims: int
    minimum_confidence_micros: int
    require_distinct_run_outcomes: bool
    contract_version: str = "tbm.outcome-harm-policy.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.outcome-harm-policy.v1":
            _record_invalid("outcome harm policy version is unsupported")
        if (
            type(self.policy_id) is not str
            or _POLICY_ID_RE.fullmatch(self.policy_id) is None
        ):
            _record_invalid("policy_id is invalid")
        if (
            type(self.minimum_verified_harmed_claims) is not int
            or not 1 <= self.minimum_verified_harmed_claims <= 100
        ):
            _record_invalid("minimum harmed claim count is invalid")
        if (
            type(self.minimum_confidence_micros) is not int
            or not 0 <= self.minimum_confidence_micros <= 1_000_000
        ):
            _record_invalid("minimum confidence is invalid")
        if type(self.require_distinct_run_outcomes) is not bool:
            _record_invalid("require_distinct_run_outcomes must be boolean")
        if self.policy_id != _content_id(
            "harm_policy_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_OUTCOME_HARM_HASH_MISMATCH",
                "harm policy ID does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "minimum_verified_harmed_claims": (
                self.minimum_verified_harmed_claims
            ),
            "minimum_confidence_micros": self.minimum_confidence_micros,
            "require_distinct_run_outcomes": self.require_distinct_run_outcomes,
        }

    def to_dict(self) -> dict[str, object]:
        return {"policy_id": self.policy_id, **self._unsigned_dict()}


def build_outcome_harm_policy(
    *,
    minimum_verified_harmed_claims: int = 1,
    minimum_confidence_micros: int = 800_000,
    require_distinct_run_outcomes: bool = True,
) -> OutcomeHarmPolicy:
    values: dict[str, object] = {
        "contract_version": "tbm.outcome-harm-policy.v1",
        "minimum_verified_harmed_claims": minimum_verified_harmed_claims,
        "minimum_confidence_micros": minimum_confidence_micros,
        "require_distinct_run_outcomes": require_distinct_run_outcomes,
    }
    return OutcomeHarmPolicy(
        policy_id=_content_id("harm_policy_sha256_", values),
        minimum_verified_harmed_claims=minimum_verified_harmed_claims,
        minimum_confidence_micros=minimum_confidence_micros,
        require_distinct_run_outcomes=require_distinct_run_outcomes,
    )


@dataclass(frozen=True)
class OutcomeEvaluationContext:
    context_id: str
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    run_outcome_id: str
    session_id: str
    trace_id: str
    run_id: str
    usage_decision_id: str
    usage_decision_sha256: str
    replay_manifest_sha256: str
    retrieval_snapshot_sha256: str
    injection_artifact_id: str
    memory_revision_ids: tuple[str, ...]
    evaluation_suite: str | None
    evaluation_case: str | None
    experiment_id: str | None
    cohort_id: str | None
    cohort_arm: Literal["observational", "with_memory", "without_memory"]
    assignment_method: Literal["randomized", "matched_control", "manual"] | None
    assignment_evidence_sha256: str | None
    bound_by: str
    bound_via_client_id: str
    authorization_event_id: str
    bound_at: str
    contract_version: str = "tbm.outcome-evaluation-context.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.outcome-evaluation-context.v1":
            _record_invalid("outcome evaluation context version is unsupported")
        if (
            type(self.context_id) is not str
            or _CONTEXT_ID_RE.fullmatch(self.context_id) is None
        ):
            _record_invalid("context_id is invalid")
        _target_partition(self)
        for name in (
            "run_outcome_id",
            "session_id",
            "trace_id",
            "run_id",
            "usage_decision_id",
            "injection_artifact_id",
            "bound_by",
            "bound_via_client_id",
            "authorization_event_id",
        ):
            _identifier(getattr(self, name), name)
        for name in (
            "usage_decision_sha256",
            "replay_manifest_sha256",
            "retrieval_snapshot_sha256",
        ):
            _digest(getattr(self, name), name)
        _revision_ids(self.memory_revision_ids)
        for name in ("evaluation_suite", "evaluation_case"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)
        if (self.evaluation_suite is None) != (self.evaluation_case is None):
            _record_invalid(
                "evaluation_suite and evaluation_case must be paired"
            )
        if self.cohort_arm not in _COHORT_ARMS:
            _record_invalid("cohort_arm is unsupported")
        cohort_values = (
            self.experiment_id,
            self.cohort_id,
            self.assignment_method,
            self.assignment_evidence_sha256,
        )
        if self.cohort_arm == "observational":
            if any(value is not None for value in cohort_values):
                _record_invalid("observational context cannot claim a cohort")
        else:
            if any(value is None for value in cohort_values):
                _record_invalid("experiment cohort metadata is incomplete")
            _identifier(self.experiment_id, "experiment_id")
            _identifier(self.cohort_id, "cohort_id")
            if self.assignment_method not in _ASSIGNMENT_METHODS:
                _record_invalid("assignment_method is unsupported")
            _digest(
                self.assignment_evidence_sha256,
                "assignment_evidence_sha256",
            )
        if self.cohort_arm == "with_memory" and not self.memory_revision_ids:
            _record_invalid("with-memory cohort requires memory revisions")
        if self.cohort_arm == "without_memory" and self.memory_revision_ids:
            _record_invalid("without-memory cohort forbids memory revisions")
        _timestamp(self.bound_at, "bound_at")
        if self.context_id != _content_id(
            "outcome_context_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_OUTCOME_HARM_HASH_MISMATCH",
                "context_id does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "run_outcome_id": self.run_outcome_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "usage_decision_id": self.usage_decision_id,
            "usage_decision_sha256": self.usage_decision_sha256,
            "replay_manifest_sha256": self.replay_manifest_sha256,
            "retrieval_snapshot_sha256": self.retrieval_snapshot_sha256,
            "injection_artifact_id": self.injection_artifact_id,
            "memory_revision_ids": list(self.memory_revision_ids),
            "evaluation_suite": self.evaluation_suite,
            "evaluation_case": self.evaluation_case,
            "experiment_id": self.experiment_id,
            "cohort_id": self.cohort_id,
            "cohort_arm": self.cohort_arm,
            "assignment_method": self.assignment_method,
            "assignment_evidence_sha256": self.assignment_evidence_sha256,
            "bound_by": self.bound_by,
            "bound_via_client_id": self.bound_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "bound_at": self.bound_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"context_id": self.context_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class StoredOutcomeEvaluationContext:
    context: OutcomeEvaluationContext
    policy: AuthorizationPolicyBundle
    request: AuthorizationRequest
    decision: AuthorizationDecision
    attestation_verified_by: str

    def __post_init__(self) -> None:
        if type(self.context) is not OutcomeEvaluationContext:
            _record_invalid("stored outcome context is invalid")
        if (
            type(self.policy) is not AuthorizationPolicyBundle
            or type(self.request) is not AuthorizationRequest
            or type(self.decision) is not AuthorizationDecision
        ):
            _record_invalid("stored authorization records are invalid")
        _identifier(self.attestation_verified_by, "attestation_verified_by")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": "tbm.stored-outcome-evaluation-context.v1",
            "context": self.context.to_dict(),
            "policy": self.policy.to_dict(),
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "attestation_verified_by": self.attestation_verified_by,
        }


@dataclass(frozen=True)
class OutcomeHarmSourceEvent:
    event_type: str
    event_sha256: str
    global_position: int
    subject_id: str
    occurred_at: str

    def __post_init__(self) -> None:
        if self.event_type not in OUTCOME_HARM_INPUT_EVENT_TYPES:
            _projection_invalid("source event type is unsupported")
        _digest(self.event_sha256, "event_sha256")
        if type(self.global_position) is not int or self.global_position < 1:
            _projection_invalid("source event position is invalid")
        _identifier(self.subject_id, "subject_id", projection=True)
        _timestamp(self.occurred_at, "occurred_at", projection=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "event_sha256": self.event_sha256,
            "global_position": self.global_position,
            "subject_id": self.subject_id,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class MemoryAssociationMetrics:
    memory_revision_id: str
    attribution_ids: tuple[str, ...]
    run_outcome_ids: tuple[str, ...]
    helped_count: int
    harmed_count: int
    neutral_count: int
    unknown_count: int

    def __post_init__(self) -> None:
        _revision_id(self.memory_revision_id, projection=True)
        _canonical_identifiers(
            self.attribution_ids, "attribution_ids", projection=True
        )
        _canonical_identifiers(
            self.run_outcome_ids, "run_outcome_ids", projection=True
        )
        counts = (
            self.helped_count,
            self.harmed_count,
            self.neutral_count,
            self.unknown_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            _projection_invalid("association effect counts are invalid")
        if sum(counts) != len(self.attribution_ids):
            _projection_invalid("association counts do not match claims")

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_revision_id": self.memory_revision_id,
            "attribution_ids": list(self.attribution_ids),
            "run_outcome_ids": list(self.run_outcome_ids),
            "helped_count": self.helped_count,
            "harmed_count": self.harmed_count,
            "neutral_count": self.neutral_count,
            "unknown_count": self.unknown_count,
        }


@dataclass(frozen=True)
class ExperimentCohortMetrics:
    experiment_id: str
    cohort_id: str
    cohort_arm: Literal["with_memory", "without_memory"]
    memory_revision_ids: tuple[str, ...]
    run_outcome_ids: tuple[str, ...]
    evaluated_count: int
    unevaluated_count: int
    pass_count: int
    fail_count: int
    error_count: int

    def __post_init__(self) -> None:
        _identifier(self.experiment_id, "experiment_id", projection=True)
        _identifier(self.cohort_id, "cohort_id", projection=True)
        if self.cohort_arm not in {"with_memory", "without_memory"}:
            _projection_invalid("experiment cohort arm is invalid")
        _revision_ids(self.memory_revision_ids, projection=True)
        _canonical_identifiers(
            self.run_outcome_ids, "run_outcome_ids", projection=True
        )
        counts = (
            self.evaluated_count,
            self.unevaluated_count,
            self.pass_count,
            self.fail_count,
            self.error_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            _projection_invalid("experiment cohort counts are invalid")
        if self.evaluated_count + self.unevaluated_count != len(
            self.run_outcome_ids
        ):
            _projection_invalid("cohort evaluation counts are inconsistent")
        if self.pass_count + self.fail_count + self.error_count != len(
            self.run_outcome_ids
        ):
            _projection_invalid("cohort result counts are inconsistent")
        if self.cohort_arm == "with_memory" and not self.memory_revision_ids:
            _projection_invalid("with-memory cohort lacks memory revisions")
        if self.cohort_arm == "without_memory" and self.memory_revision_ids:
            _projection_invalid("without-memory cohort contains revisions")

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "cohort_id": self.cohort_id,
            "cohort_arm": self.cohort_arm,
            "memory_revision_ids": list(self.memory_revision_ids),
            "run_outcome_ids": list(self.run_outcome_ids),
            "evaluated_count": self.evaluated_count,
            "unevaluated_count": self.unevaluated_count,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "error_count": self.error_count,
        }


@dataclass(frozen=True)
class VerifiedCausalClaim:
    attribution_id: str
    run_outcome_id: str
    memory_revision_ids: tuple[str, ...]
    effect: Literal["helped", "harmed", "neutral"]
    method: Literal[
        "controlled_experiment", "manual_review", "external_evaluation"
    ]
    verifier_id: str
    confidence_micros: int
    source_event_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.attribution_id, "attribution_id", projection=True)
        _identifier(self.run_outcome_id, "run_outcome_id", projection=True)
        _revision_ids(self.memory_revision_ids, projection=True)
        if self.effect not in {"helped", "harmed", "neutral"}:
            _projection_invalid("causal claim effect is invalid")
        if self.method not in _CAUSAL_METHODS:
            _projection_invalid("causal claim method is invalid")
        _identifier(self.verifier_id, "verifier_id", projection=True)
        _confidence_micros(self.confidence_micros, projection=True)
        _digest(self.source_event_sha256, "source_event_sha256", projection=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "attribution_id": self.attribution_id,
            "run_outcome_id": self.run_outcome_id,
            "memory_revision_ids": list(self.memory_revision_ids),
            "effect": self.effect,
            "method": self.method,
            "verifier_id": self.verifier_id,
            "confidence_micros": self.confidence_micros,
            "source_event_sha256": self.source_event_sha256,
        }


@dataclass(frozen=True)
class HarmfulMemorySignal:
    signal_id: str
    policy_id: str
    memory_revision_id: str
    attribution_ids: tuple[str, ...]
    run_outcome_ids: tuple[str, ...]
    minimum_observed_confidence_micros: int
    detected_at: str

    def __post_init__(self) -> None:
        if type(self.signal_id) is not str or _SIGNAL_ID_RE.fullmatch(
            self.signal_id
        ) is None:
            _projection_invalid("harm signal ID is invalid")
        if type(self.policy_id) is not str or _POLICY_ID_RE.fullmatch(
            self.policy_id
        ) is None:
            _projection_invalid("harm signal policy ID is invalid")
        _revision_id(self.memory_revision_id, projection=True)
        _canonical_identifiers(
            self.attribution_ids, "attribution_ids", projection=True
        )
        _canonical_identifiers(
            self.run_outcome_ids, "run_outcome_ids", projection=True
        )
        _confidence_micros(
            self.minimum_observed_confidence_micros, projection=True
        )
        _timestamp(self.detected_at, "detected_at", projection=True)
        if self.signal_id != _content_id(
            "harm_signal_sha256_", self._unsigned_dict()
        ):
            _projection_invalid("harm signal ID does not match content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": "tbm.harmful-memory-signal.v1",
            "policy_id": self.policy_id,
            "memory_revision_id": self.memory_revision_id,
            "attribution_ids": list(self.attribution_ids),
            "run_outcome_ids": list(self.run_outcome_ids),
            "minimum_observed_confidence_micros": (
                self.minimum_observed_confidence_micros
            ),
            "detected_at": self.detected_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"signal_id": self.signal_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class SuspensionRecommendation:
    recommendation_id: str
    signal_id: str
    memory_revision_id: str
    action: Literal["suspend"]
    reason: Literal["verified_causal_harm_threshold_met"]
    recommended_at: str

    def __post_init__(self) -> None:
        if type(self.recommendation_id) is not str or (
            _RECOMMENDATION_ID_RE.fullmatch(self.recommendation_id) is None
        ):
            _projection_invalid("suspension recommendation ID is invalid")
        if type(self.signal_id) is not str or _SIGNAL_ID_RE.fullmatch(
            self.signal_id
        ) is None:
            _projection_invalid("recommendation signal ID is invalid")
        _revision_id(self.memory_revision_id, projection=True)
        if self.action != "suspend" or (
            self.reason != "verified_causal_harm_threshold_met"
        ):
            _projection_invalid("suspension recommendation is invalid")
        _timestamp(self.recommended_at, "recommended_at", projection=True)
        if self.recommendation_id != _content_id(
            "suspension_recommendation_sha256_", self._unsigned_dict()
        ):
            _projection_invalid("recommendation ID does not match content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": "tbm.suspension-recommendation.v1",
            "signal_id": self.signal_id,
            "memory_revision_id": self.memory_revision_id,
            "action": self.action,
            "reason": self.reason,
            "recommended_at": self.recommended_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "recommendation_id": self.recommendation_id,
            **self._unsigned_dict(),
        }


@dataclass(frozen=True)
class OutcomeHarmProjection:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    policy: OutcomeHarmPolicy
    source_events: tuple[OutcomeHarmSourceEvent, ...]
    contexts: tuple[OutcomeEvaluationContext, ...]
    run_outcomes: tuple[RunOutcome, ...]
    attributions: tuple[OutcomeAttribution, ...]
    evaluated_run_outcome_ids: tuple[str, ...]
    unevaluated_run_outcome_ids: tuple[str, ...]
    observed_associations: tuple[MemoryAssociationMetrics, ...]
    experiment_cohorts: tuple[ExperimentCohortMetrics, ...]
    verified_causal_claims: tuple[VerifiedCausalClaim, ...]
    harmful_memory_signals: tuple[HarmfulMemorySignal, ...]
    suspension_recommendations: tuple[SuspensionRecommendation, ...]
    last_global_position: int

    def __post_init__(self) -> None:
        _target_partition(self)
        if type(self.policy) is not OutcomeHarmPolicy:
            _projection_invalid("outcome harm projection policy is invalid")
        _typed_tuple(
            self.source_events, OutcomeHarmSourceEvent, "source_events"
        )
        _typed_tuple(self.contexts, OutcomeEvaluationContext, "contexts")
        _typed_tuple(self.run_outcomes, RunOutcome, "run_outcomes")
        _typed_tuple(self.attributions, OutcomeAttribution, "attributions")
        _canonical_identifiers(
            self.evaluated_run_outcome_ids,
            "evaluated_run_outcome_ids",
            projection=True,
            allow_empty=True,
        )
        _canonical_identifiers(
            self.unevaluated_run_outcome_ids,
            "unevaluated_run_outcome_ids",
            projection=True,
            allow_empty=True,
        )
        if set(self.evaluated_run_outcome_ids) & set(
            self.unevaluated_run_outcome_ids
        ):
            _projection_invalid("evaluated and unevaluated outcomes overlap")
        _typed_tuple(
            self.observed_associations,
            MemoryAssociationMetrics,
            "observed_associations",
        )
        _typed_tuple(
            self.experiment_cohorts,
            ExperimentCohortMetrics,
            "experiment_cohorts",
        )
        _typed_tuple(
            self.verified_causal_claims,
            VerifiedCausalClaim,
            "verified_causal_claims",
        )
        _typed_tuple(
            self.harmful_memory_signals,
            HarmfulMemorySignal,
            "harmful_memory_signals",
        )
        _typed_tuple(
            self.suspension_recommendations,
            SuspensionRecommendation,
            "suspension_recommendations",
        )
        positions = tuple(item.global_position for item in self.source_events)
        if positions != tuple(sorted(set(positions))):
            _projection_invalid("source event positions are not canonical")
        if type(self.last_global_position) is not int or (
            self.last_global_position != (0 if not positions else positions[-1])
        ):
            _projection_invalid("projection high watermark is inconsistent")
        signal_by_id = {
            item.signal_id: item for item in self.harmful_memory_signals
        }
        if len(signal_by_id) != len(self.harmful_memory_signals):
            _projection_invalid("harm signals are duplicated")
        for item in self.suspension_recommendations:
            signal = signal_by_id.get(item.signal_id)
            if signal is None or signal.memory_revision_id != item.memory_revision_id:
                _projection_invalid("recommendation is not linked to its signal")

    def to_dict(self) -> dict[str, object]:
        return _projection_digest_value(self)


@dataclass(frozen=True)
class DurableOutcomeHarmSnapshot:
    projection: OutcomeHarmProjection
    partition_sha256: str
    reducer_descriptor_sha256: str
    reducer_configuration_sha256: str
    event_high_watermark: int
    source_event_count: int
    snapshot_sha256: str
    contract_version: str = "tbm.durable-outcome-harm-snapshot.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.durable-outcome-harm-snapshot.v1":
            _projection_invalid("durable outcome harm snapshot version is invalid")
        if type(self.projection) is not OutcomeHarmProjection:
            _projection_invalid("durable outcome harm projection is invalid")
        for name in (
            "partition_sha256",
            "reducer_descriptor_sha256",
            "reducer_configuration_sha256",
            "snapshot_sha256",
        ):
            _digest(getattr(self, name), name, projection=True)
        if type(self.event_high_watermark) is not int or (
            self.event_high_watermark < self.projection.last_global_position
        ):
            _projection_invalid("snapshot event watermark is invalid")
        if type(self.source_event_count) is not int or (
            self.source_event_count != len(self.projection.source_events)
        ):
            _projection_invalid("snapshot event count is invalid")
        if self.snapshot_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("snapshot digest does not match content")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "partition_sha256": self.partition_sha256,
            "reducer_descriptor_sha256": self.reducer_descriptor_sha256,
            "reducer_configuration_sha256": self.reducer_configuration_sha256,
            "event_high_watermark": self.event_high_watermark,
            "source_event_count": self.source_event_count,
            "projection": self.projection.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {"snapshot_sha256": self.snapshot_sha256, **self._unsigned_dict()}


@dataclass(frozen=True)
class OutcomeHarmAppendResult:
    receipt: LedgerAppendReceipt
    snapshot: DurableOutcomeHarmSnapshot

    def __post_init__(self) -> None:
        if type(self.receipt) is not LedgerAppendReceipt:
            _record_invalid("outcome harm append receipt is invalid")
        if type(self.snapshot) is not DurableOutcomeHarmSnapshot:
            _record_invalid("outcome harm append snapshot is invalid")


def build_outcome_evaluation_context(
    *,
    organization_id: str,
    tenant_id: str,
    repository_id: str,
    environment_id: str,
    run_outcome_id: str,
    session_id: str,
    trace_id: str,
    run_id: str,
    usage_decision_id: str,
    usage_decision_sha256: str,
    replay_manifest_sha256: str,
    retrieval_snapshot_sha256: str,
    injection_artifact_id: str,
    memory_revision_ids: tuple[str, ...],
    evaluation_suite: str | None,
    evaluation_case: str | None,
    cohort_arm: Literal["observational", "with_memory", "without_memory"],
    bound_by: str,
    bound_via_client_id: str,
    authorization_event_id: str,
    bound_at: str,
    experiment_id: str | None = None,
    cohort_id: str | None = None,
    assignment_method: Literal[
        "randomized", "matched_control", "manual"
    ]
    | None = None,
    assignment_evidence_sha256: str | None = None,
) -> OutcomeEvaluationContext:
    values: dict[str, object] = {
        "contract_version": "tbm.outcome-evaluation-context.v1",
        "organization_id": organization_id,
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "environment_id": environment_id,
        "run_outcome_id": run_outcome_id,
        "session_id": session_id,
        "trace_id": trace_id,
        "run_id": run_id,
        "usage_decision_id": usage_decision_id,
        "usage_decision_sha256": usage_decision_sha256,
        "replay_manifest_sha256": replay_manifest_sha256,
        "retrieval_snapshot_sha256": retrieval_snapshot_sha256,
        "injection_artifact_id": injection_artifact_id,
        "memory_revision_ids": list(memory_revision_ids),
        "evaluation_suite": evaluation_suite,
        "evaluation_case": evaluation_case,
        "experiment_id": experiment_id,
        "cohort_id": cohort_id,
        "cohort_arm": cohort_arm,
        "assignment_method": assignment_method,
        "assignment_evidence_sha256": assignment_evidence_sha256,
        "bound_by": bound_by,
        "bound_via_client_id": bound_via_client_id,
        "authorization_event_id": authorization_event_id,
        "bound_at": canonical_rfc3339(bound_at),
    }
    return OutcomeEvaluationContext(
        context_id=_content_id("outcome_context_sha256_", values),
        organization_id=organization_id,
        tenant_id=tenant_id,
        repository_id=repository_id,
        environment_id=environment_id,
        run_outcome_id=run_outcome_id,
        session_id=session_id,
        trace_id=trace_id,
        run_id=run_id,
        usage_decision_id=usage_decision_id,
        usage_decision_sha256=usage_decision_sha256,
        replay_manifest_sha256=replay_manifest_sha256,
        retrieval_snapshot_sha256=retrieval_snapshot_sha256,
        injection_artifact_id=injection_artifact_id,
        memory_revision_ids=memory_revision_ids,
        evaluation_suite=evaluation_suite,
        evaluation_case=evaluation_case,
        experiment_id=experiment_id,
        cohort_id=cohort_id,
        cohort_arm=cohort_arm,
        assignment_method=assignment_method,
        assignment_evidence_sha256=assignment_evidence_sha256,
        bound_by=bound_by,
        bound_via_client_id=bound_via_client_id,
        authorization_event_id=authorization_event_id,
        bound_at=canonical_rfc3339(bound_at),
    )


def outcome_harm_context_stream_id(
    partition: LedgerTenantPartition,
) -> str:
    if type(partition) is not LedgerTenantPartition:
        _record_invalid("outcome harm partition is invalid")
    return "outcome_harm_context_" + partition.partition_sha256.removeprefix(
        "sha256:"
    )


def dumps_outcome_evaluation_context(
    context: OutcomeEvaluationContext,
) -> str:
    if type(context) is not OutcomeEvaluationContext:
        _record_invalid("context must be OutcomeEvaluationContext")
    return _canonical_json(context.to_dict())


def loads_outcome_evaluation_context(
    document: str | bytes,
) -> OutcomeEvaluationContext:
    return _parse_context(
        _loads_record(document, "outcome evaluation context")
    )


def dumps_stored_outcome_evaluation_context(
    stored: StoredOutcomeEvaluationContext,
) -> str:
    if type(stored) is not StoredOutcomeEvaluationContext:
        _record_invalid("stored context is invalid")
    return _canonical_json(stored.to_dict())


def loads_stored_outcome_evaluation_context(
    document: str | bytes,
) -> StoredOutcomeEvaluationContext:
    return _parse_stored_context(
        _loads_record(document, "stored outcome evaluation context")
    )


def outcome_evaluation_context_schema() -> dict[str, object]:
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    nullable_identifier = {"oneOf": [{"type": "null"}, identifier]}
    nullable_digest = {"oneOf": [{"type": "null"}, digest]}
    properties: dict[str, object] = {
        "context_id": {
            "type": "string",
            "pattern": r"^outcome_context_sha256_[0-9a-f]{64}$",
        },
        "contract_version": {"const": "tbm.outcome-evaluation-context.v1"},
        "organization_id": identifier,
        "tenant_id": identifier,
        "repository_id": identifier,
        "environment_id": identifier,
        "run_outcome_id": identifier,
        "session_id": identifier,
        "trace_id": identifier,
        "run_id": identifier,
        "usage_decision_id": identifier,
        "usage_decision_sha256": digest,
        "replay_manifest_sha256": digest,
        "retrieval_snapshot_sha256": digest,
        "injection_artifact_id": identifier,
        "memory_revision_ids": {
            "type": "array",
            "maxItems": 256,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "pattern": r"^memory_revision_sha256_[0-9a-f]{64}$",
            },
        },
        "evaluation_suite": nullable_identifier,
        "evaluation_case": nullable_identifier,
        "experiment_id": nullable_identifier,
        "cohort_id": nullable_identifier,
        "cohort_arm": {"enum": list(_COHORT_ARMS)},
        "assignment_method": {
            "oneOf": [{"type": "null"}, {"enum": list(_ASSIGNMENT_METHODS)}]
        },
        "assignment_evidence_sha256": nullable_digest,
        "bound_by": identifier,
        "bound_via_client_id": identifier,
        "authorization_event_id": identifier,
        "bound_at": {"type": "string", "minLength": 20, "maxLength": 64},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": OUTCOME_HARM_CONTEXT_SCHEMA_ID,
        "title": "Trace-backed Memory outcome evaluation context v1",
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def build_outcome_harm_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    schema = _context_payload_json_schema()
    registry.register(
        EventPayloadRegistration(
            event_type=OUTCOME_EVALUATION_CONTEXT_BOUND,
            event_version=1,
            event_kind="domain",
            payload_schema=_PAYLOAD_SCHEMAS[OUTCOME_EVALUATION_CONTEXT_BOUND],
            schema=schema,
        )
    )
    return registry.seal()


def outcome_harm_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_outcome_harm_event_registry().dispatch_schema()
    schema["$id"] = OUTCOME_HARM_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory outcome harm event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed context-event registry. Outcome source "
        "events retain the outcome/effect v1 registry contract."
    )
    return schema


def dumps_outcome_harm_event_payload_dispatch_schema() -> str:
    return json.dumps(
        outcome_harm_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_outcome_harm_event_batch(
    access: LedgerAccessContext,
    records: tuple[StoredOutcomeEvaluationContext, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if type(access) is not LedgerAccessContext:
        _fail("TBM_OUTCOME_HARM_ACCESS_INVALID", "access is invalid")
    if (
        type(records) is not tuple
        or not 1 <= len(records) <= OUTCOME_HARM_EVENT_MAX_BATCH
        or any(type(item) is not StoredOutcomeEvaluationContext for item in records)
    ):
        _fail(
            "TBM_OUTCOME_HARM_BATCH_INVALID",
            "records must be a bounded non-empty tuple",
        )
    if type(expected_stream_version) is not int or expected_stream_version < 0:
        _fail("TBM_OUTCOME_HARM_BATCH_INVALID", "stream version is invalid")
    if type(next_global_position) is not int or next_global_position < 1:
        _fail("TBM_OUTCOME_HARM_BATCH_INVALID", "global position is invalid")
    canonical_recorded_at = _timestamp(recorded_at, "recorded_at")
    stream_id = outcome_harm_context_stream_id(access.partition)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail("TBM_OUTCOME_HARM_BATCH_INVALID", "stream parent is missing")
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail("TBM_OUTCOME_HARM_BATCH_INVALID", "stream parent is invalid")
    record_values = tuple(item.to_dict() for item in records)
    command_value = {
        "protocol_version": OUTCOME_HARM_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "records": list(record_values),
    }
    command_sha256 = _domain_sha256(
        b"tbm.outcome-harm-event-command.v1\x00", command_value
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=_domain_sha256(
            b"tbm.outcome-harm-event-idempotency.v1\x00", command_value
        ),
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    parent = previous_event
    events: list[CanonicalEvent] = []
    for offset, (stored, record_value) in enumerate(zip(records, record_values)):
        context = stored.context
        _verify_context_access(access, context)
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        payload = {
            "subject_id": context.run_outcome_id,
            "record_type": OUTCOME_EVALUATION_CONTEXT_BOUND,
            "record_sha256": canonical_sha256(record_value),
            "record_json": _canonical_json(record_value),
        }
        event = build_canonical_event(
            event_id="evt_oh_" + event_digest,
            event_type=OUTCOME_EVALUATION_CONTEXT_BOUND,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=OUTCOME_HARM_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_oh_" + event_digest[:32],
            idempotency_key_sha256=idempotency.idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_oh_" + stream_id[-32:],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=context.bound_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_outcome_harm_runtime",
            producer_version="f4-v1",
            payload_schema=_PAYLOAD_SCHEMAS[OUTCOME_EVALUATION_CONTEXT_BOUND],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification="internal",
            retention_policy_id="retention_outcome_harm_events",
            artifact_refs=(),
            payload=payload,
        )
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_outcome_harm_reducer(
    *,
    policy: OutcomeHarmPolicy,
    trusted_attestation_verifier_ids: tuple[str, ...],
) -> FunctionalReducer:
    if type(policy) is not OutcomeHarmPolicy:
        _record_invalid("outcome harm policy is invalid")
    trusted_verifiers = _trusted_verifier_set(
        trusted_attestation_verifier_ids
    )
    descriptor = ReducerDescriptor(
        reducer_id=OUTCOME_HARM_EVENT_REDUCER_ID,
        reducer_version=1,
        input_event_types=OUTCOME_HARM_INPUT_EVENT_TYPES,
        output_projection=OUTCOME_HARM_EVENT_PROJECTION,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "outcome-harm-metrics",
                "algorithm_version": 1,
                "event_types": list(OUTCOME_HARM_INPUT_EVENT_TYPES),
                "association_is_not_causation": True,
                "suspension_is_recommendation_only": True,
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {
                "policy": policy.to_dict(),
                "trusted_attestation_verifier_ids": sorted(
                    trusted_verifiers
                ),
                "version": 1,
            },
        ),
        target_event_versions={
            event_type: 1 for event_type in OUTCOME_HARM_INPUT_EVENT_TYPES
        },
    )

    def initial() -> Mapping[str, object]:
        return {
            "organization_id": None,
            "tenant_id": None,
            "repository_id": None,
            "environment_id": None,
            "contexts": [],
            "run_outcomes": [],
            "attributions": [],
            "source_events": [],
            "last_global_position": 0,
        }

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        event = reducer_event.source_event
        partition = _event_partition(event)
        for name in (
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
        ):
            if state.get(name) not in {None, getattr(partition, name)}:
                _transition_invalid("outcome harm events crossed a partition")
        contexts = _state_list(state, "contexts")
        outcomes = _state_list(state, "run_outcomes")
        attributions = _state_list(state, "attributions")
        source_events = _state_list(state, "source_events")
        payload = _typed_payload(reducer_event)
        if event.event_type == OUTCOME_EVALUATION_CONTEXT_BOUND:
            stored = _load_stored_context_payload(payload, event)
            _verify_stored_context_authorization(stored, event=event)
            if stored.attestation_verified_by not in trusted_verifiers:
                _transition_invalid("context attestation verifier is not trusted")
            context = stored.context
            if any(
                isinstance(item, Mapping)
                and item.get("run_outcome_id") == context.run_outcome_id
                for item in contexts
            ):
                _transition_invalid("run outcome context is duplicated")
            contexts.append(context.to_dict())
            subject_id = context.run_outcome_id
        elif event.event_type == RUN_OUTCOME_RECORDED:
            outcome = _load_run_outcome_payload(payload, event)
            if any(
                isinstance(item, Mapping)
                and item.get("run_outcome_id") == outcome.run_outcome_id
                for item in outcomes
            ):
                _transition_invalid("RunOutcome source event is duplicated")
            outcomes.append(
                {
                    "run_outcome_id": outcome.run_outcome_id,
                    "record_json": _canonical_json(outcome.to_dict()),
                }
            )
            subject_id = outcome.run_outcome_id
        elif event.event_type == OUTCOME_ATTRIBUTION_RECORDED:
            attribution = _load_attribution_payload(payload, event)
            if any(
                isinstance(item, Mapping)
                and item.get("attribution_id") == attribution.attribution_id
                for item in attributions
            ):
                _transition_invalid("OutcomeAttribution source event is duplicated")
            attributions.append(
                {
                    "attribution_id": attribution.attribution_id,
                    "record_json": _canonical_json(attribution.to_dict()),
                }
            )
            subject_id = attribution.attribution_id
        else:  # pragma: no cover - descriptor and registry guard this
            raise AssertionError("unsupported outcome harm reducer event")
        source_events.append(
            OutcomeHarmSourceEvent(
                event_type=event.event_type,
                event_sha256=event.event_sha256,
                global_position=event.global_position,
                subject_id=subject_id,
                occurred_at=event.occurred_at,
            ).to_dict()
        )
        return {
            "organization_id": partition.organization_id,
            "tenant_id": partition.tenant_id,
            "repository_id": partition.repository_id,
            "environment_id": partition.environment_id,
            "contexts": contexts,
            "run_outcomes": outcomes,
            "attributions": attributions,
            "source_events": source_events,
            "last_global_position": event.global_position,
        }

    return FunctionalReducer(descriptor, initial, transition)


def reduce_outcome_harm_events(
    events: tuple[CanonicalEvent, ...],
    *,
    policy: OutcomeHarmPolicy,
    trusted_attestation_verifier_ids: tuple[str, ...],
    context_event_registry: EventTypeRegistry | None = None,
) -> OutcomeHarmProjection:
    if (
        type(events) is not tuple
        or not events
        or len(events) > OUTCOME_HARM_EVENT_MAX_SOURCE_EVENTS
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _fail(
            "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
            "events must be a bounded non-empty CanonicalEvent tuple",
        )
    positions = tuple(event.global_position for event in events)
    if positions != tuple(sorted(set(positions))):
        _fail(
            "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
            "events must have strictly increasing global positions",
        )
    context_registry = (
        build_outcome_harm_event_registry()
        if context_event_registry is None
        else context_event_registry
    )
    source_registry = build_outcome_effect_event_registry()
    if (
        type(context_registry) is not EventTypeRegistry
        or not context_registry.sealed
        or type(source_registry) is not EventTypeRegistry
        or not source_registry.sealed
    ):
        _fail(
            "TBM_OUTCOME_HARM_EVENT_REGISTRY_INVALID",
            "event registries must be sealed",
        )
    reducer = build_outcome_harm_reducer(
        policy=policy,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
    )
    state = initial_reducer_state(reducer)
    context_parent: CanonicalEvent | None = None
    partition = _event_partition(events[0])
    for event in events:
        if _event_partition(event) != partition:
            _fail(
                "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
                "events crossed a ledger partition",
            )
        if event.event_type == OUTCOME_EVALUATION_CONTEXT_BOUND:
            _verify_context_event_envelope(event)
            try:
                verify_event_parent(event, context_parent)
            except ValueError as error:
                raise OutcomeHarmEventV1Error(
                    "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
                    "context event stream chain is invalid",
                ) from error
            typed = context_registry.consume(event, target_version=1)
            context_parent = event
        else:
            _verify_outcome_source_event_envelope(event)
            typed = source_registry.consume(event, target_version=1)
        state = execute_reducer_step(
            reducer, state.state, ReducerEvent(event, typed)
        )
    return _hydrate_projection(state.state, policy=policy)


def append_outcome_evaluation_contexts(
    ledger: EventLedgerPort,
    records: tuple[StoredOutcomeEvaluationContext, ...],
    *,
    policy: OutcomeHarmPolicy,
    trusted_attestation_verifier_ids: tuple[str, ...],
    recorded_at: str,
) -> OutcomeHarmAppendResult:
    access = _require_ledger(ledger)
    stream_id = outcome_harm_context_stream_id(access.partition)
    for attempt in range(8):
        retained = _read_context_stream(ledger, stream_id)
        _verify_context_stream(ledger, stream_id, retained)
        expected_version = len(retained)
        parent = None if not retained else retained[-1]
        high_watermark = _ledger_high_watermark(ledger)
        events, idempotency = build_outcome_harm_event_batch(
            access,
            records,
            expected_stream_version=expected_version,
            next_global_position=high_watermark + 1,
            previous_event=parent,
            recorded_at=recorded_at,
        )
        try:
            receipt = ledger.append(
                stream_id,
                expected_version,
                events,
                idempotency,
            )
            break
        except EventLedgerConflictError as error:
            if (
                error.code != "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"
                or attempt == 7
            ):
                raise
    else:  # pragma: no cover
        raise AssertionError("outcome harm append retry did not terminate")
    request = LedgerAppendRequest(
        access=access,
        stream_id=stream_id,
        expected_stream_version=expected_version,
        events=cast(tuple[CanonicalEvent, ...], events),
        idempotency=cast(LedgerIdempotency, idempotency),
    )
    verify_ledger_append_receipt(request, receipt)
    durable = _read_context_stream(ledger, stream_id)
    _verify_context_stream(ledger, stream_id, durable)
    if tuple(item.event_sha256 for item in durable[-len(events) :]) != tuple(
        item.event_sha256 for item in events
    ):
        _fail(
            "TBM_OUTCOME_HARM_LEDGER_VERIFICATION_FAILED",
            "appended context events failed exact read-back",
        )
    snapshot = rebuild_outcome_harm_from_ledger(
        ledger,
        policy=policy,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
    )
    appended_sha256s = {item.event_sha256 for item in events}
    if not appended_sha256s.issubset(
        {item.event_sha256 for item in snapshot.projection.source_events}
    ):
        _fail(
            "TBM_OUTCOME_HARM_PROJECTION_MISMATCH",
            "durable outcome harm projection omits appended contexts",
        )
    return OutcomeHarmAppendResult(receipt=receipt, snapshot=snapshot)


def rebuild_outcome_harm_from_ledger(
    ledger: EventLedgerPort,
    *,
    policy: OutcomeHarmPolicy,
    trusted_attestation_verifier_ids: tuple[str, ...],
) -> DurableOutcomeHarmSnapshot:
    access = _require_ledger(ledger)
    events, high_watermark = _scan_outcome_harm_events(ledger)
    _verify_source_streams(ledger, events)
    if events:
        projection = reduce_outcome_harm_events(
            events,
            policy=policy,
            trusted_attestation_verifier_ids=(
                trusted_attestation_verifier_ids
            ),
        )
    else:
        projection = _empty_projection(access.partition, policy)
    repeated, _ = _scan_outcome_harm_events(
        ledger, event_high_watermark=high_watermark
    )
    if repeated != events:
        _fail(
            "TBM_OUTCOME_HARM_REBUILD_SUPERSEDED",
            "outcome harm inputs changed during fixed-watermark rebuild",
        )
    _verify_source_streams(ledger, repeated)
    reducer = build_outcome_harm_reducer(
        policy=policy,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
    )
    values = {
        "contract_version": "tbm.durable-outcome-harm-snapshot.v1",
        "partition_sha256": access.partition.partition_sha256,
        "reducer_descriptor_sha256": reducer.descriptor.descriptor_sha256,
        "reducer_configuration_sha256": (
            reducer.descriptor.configuration_sha256
        ),
        "event_high_watermark": high_watermark,
        "source_event_count": len(events),
        "projection": projection.to_dict(),
    }
    return DurableOutcomeHarmSnapshot(
        projection=projection,
        partition_sha256=access.partition.partition_sha256,
        reducer_descriptor_sha256=reducer.descriptor.descriptor_sha256,
        reducer_configuration_sha256=(
            reducer.descriptor.configuration_sha256
        ),
        event_high_watermark=high_watermark,
        source_event_count=len(events),
        snapshot_sha256=canonical_sha256(values),
    )


def _hydrate_projection(
    state: Mapping[str, object], *, policy: OutcomeHarmPolicy
) -> OutcomeHarmProjection:
    source_state = _state_list(state, "source_events")
    context_state = _state_list(state, "contexts")
    outcome_state = _state_list(state, "run_outcomes")
    attribution_state = _state_list(state, "attributions")
    if len(context_state) > OUTCOME_HARM_EVENT_MAX_CONTEXTS:
        _projection_invalid("outcome harm context limit is exceeded")
    source_events = tuple(
        _parse_source_event(_as_mapping(item, "source event"))
        for item in source_state
    )
    contexts = tuple(
        _parse_context(_as_mapping(item, "outcome context"))
        for item in context_state
    )
    outcomes = tuple(
        loads_run_outcome(
            _string(_as_mapping(item, "RunOutcome"), "record_json")
        )
        for item in outcome_state
    )
    attributions = tuple(
        loads_outcome_attribution(
            _string(_as_mapping(item, "OutcomeAttribution"), "record_json")
        )
        for item in attribution_state
    )
    context_by_outcome = {item.run_outcome_id: item for item in contexts}
    outcome_by_id = {item.run_outcome_id: item for item in outcomes}
    if len(context_by_outcome) != len(contexts):
        _projection_invalid("outcome contexts are duplicated")
    if len(outcome_by_id) != len(outcomes):
        _projection_invalid("RunOutcome records are duplicated")
    attribution_by_id = {item.attribution_id: item for item in attributions}
    if len(attribution_by_id) != len(attributions):
        _projection_invalid("OutcomeAttribution records are duplicated")
    partition = LedgerTenantPartition(
        organization_id=cast(str, state.get("organization_id")),
        tenant_id=cast(str, state.get("tenant_id")),
        repository_id=cast(str, state.get("repository_id")),
        environment_id=cast(str, state.get("environment_id")),
    )
    for context in contexts:
        if _target_partition(context) != partition:
            _projection_invalid("outcome context crossed its partition")
        outcome = outcome_by_id.get(context.run_outcome_id)
        if outcome is None:
            _projection_invalid("outcome context target is missing")
        if (
            outcome.session_id != context.session_id
            or outcome.trace_id != context.trace_id
            or outcome.run_id != context.run_id
            or outcome.usage_decision_id != context.usage_decision_id
            or parse_rfc3339(context.bound_at)
            < parse_rfc3339(outcome.measured_at)
        ):
            _projection_invalid("outcome context does not match RunOutcome")
    linked_attributions: list[OutcomeAttribution] = []
    for attribution in attributions:
        outcome = outcome_by_id.get(attribution.run_outcome_id)
        if outcome is None:
            _projection_invalid("attribution RunOutcome is missing")
        if (
            attribution.usage_decision_id != outcome.usage_decision_id
            or parse_rfc3339(attribution.recorded_at)
            < parse_rfc3339(outcome.measured_at)
        ):
            _projection_invalid("attribution does not match RunOutcome")
        context = context_by_outcome.get(attribution.run_outcome_id)
        if context is None:
            continue
        if context.cohort_arm == "without_memory":
            _projection_invalid("without-memory cohort has a memory attribution")
        if attribution.usage_decision_id != context.usage_decision_id or not set(
            attribution.memory_revision_ids
        ).issubset(context.memory_revision_ids):
            _projection_invalid("attribution does not match bound memory usage")
        if (
            attribution.claim_strength == "causal"
            and attribution.method == "controlled_experiment"
            and context.cohort_arm != "with_memory"
        ):
            _projection_invalid(
                "controlled-experiment claim lacks with-memory cohort evidence"
            )
        linked_attributions.append(attribution)
    evaluated_ids = tuple(
        sorted(
            context.run_outcome_id
            for context in contexts
            if context.evaluation_suite is not None
        )
    )
    unevaluated_ids = tuple(
        sorted(set(outcome_by_id) - set(evaluated_ids))
    )
    source_by_subject = {
        (item.event_type, item.subject_id): item for item in source_events
    }
    associations = _association_metrics(tuple(linked_attributions))
    cohorts = _cohort_metrics(contexts, outcome_by_id)
    causal_claims = _causal_claims(
        tuple(linked_attributions), source_by_subject
    )
    signals = _harm_signals(
        causal_claims, attribution_by_id, policy=policy
    )
    recommendations = tuple(
        _build_suspension_recommendation(signal) for signal in signals
    )
    return OutcomeHarmProjection(
        organization_id=partition.organization_id,
        tenant_id=partition.tenant_id,
        repository_id=partition.repository_id,
        environment_id=partition.environment_id,
        policy=policy,
        source_events=source_events,
        contexts=tuple(sorted(contexts, key=lambda item: item.context_id)),
        run_outcomes=tuple(
            sorted(outcomes, key=lambda item: item.run_outcome_id)
        ),
        attributions=tuple(
            sorted(attributions, key=lambda item: item.attribution_id)
        ),
        evaluated_run_outcome_ids=evaluated_ids,
        unevaluated_run_outcome_ids=unevaluated_ids,
        observed_associations=associations,
        experiment_cohorts=cohorts,
        verified_causal_claims=causal_claims,
        harmful_memory_signals=signals,
        suspension_recommendations=recommendations,
        last_global_position=cast(int, state.get("last_global_position")),
    )


def _association_metrics(
    attributions: tuple[OutcomeAttribution, ...],
) -> tuple[MemoryAssociationMetrics, ...]:
    grouped: dict[str, list[OutcomeAttribution]] = {}
    for attribution in attributions:
        if attribution.claim_strength != "association":
            continue
        for revision_id in attribution.memory_revision_ids:
            grouped.setdefault(revision_id, []).append(attribution)
    result: list[MemoryAssociationMetrics] = []
    for revision_id, claims in sorted(grouped.items()):
        counts = {effect: 0 for effect in _EFFECTS}
        for claim in claims:
            counts[claim.effect] += 1
        result.append(
            MemoryAssociationMetrics(
                memory_revision_id=revision_id,
                attribution_ids=tuple(
                    sorted(claim.attribution_id for claim in claims)
                ),
                run_outcome_ids=tuple(
                    sorted({claim.run_outcome_id for claim in claims})
                ),
                helped_count=counts["helped"],
                harmed_count=counts["harmed"],
                neutral_count=counts["neutral"],
                unknown_count=counts["unknown"],
            )
        )
    return tuple(result)


def _cohort_metrics(
    contexts: tuple[OutcomeEvaluationContext, ...],
    outcome_by_id: Mapping[str, RunOutcome],
) -> tuple[ExperimentCohortMetrics, ...]:
    grouped: dict[
        tuple[str, str, Literal["with_memory", "without_memory"]],
        list[OutcomeEvaluationContext],
    ] = {}
    for context in contexts:
        if context.cohort_arm == "observational":
            continue
        key = (
            cast(str, context.experiment_id),
            cast(str, context.cohort_id),
            context.cohort_arm,
        )
        grouped.setdefault(key, []).append(context)
    result: list[ExperimentCohortMetrics] = []
    for (experiment_id, cohort_id, arm), members in sorted(grouped.items()):
        revision_sets = {member.memory_revision_ids for member in members}
        if len(revision_sets) != 1:
            _projection_invalid("experiment cohort changes its memory set")
        outcomes = [outcome_by_id[item.run_outcome_id] for item in members]
        result.append(
            ExperimentCohortMetrics(
                experiment_id=experiment_id,
                cohort_id=cohort_id,
                cohort_arm=arm,
                memory_revision_ids=members[0].memory_revision_ids,
                run_outcome_ids=tuple(
                    sorted(item.run_outcome_id for item in outcomes)
                ),
                evaluated_count=sum(
                    item.evaluation_suite is not None for item in members
                ),
                unevaluated_count=sum(
                    item.evaluation_suite is None for item in members
                ),
                pass_count=sum(item.result == "pass" for item in outcomes),
                fail_count=sum(item.result == "fail" for item in outcomes),
                error_count=sum(item.result == "error" for item in outcomes),
            )
        )
    return tuple(result)


def _causal_claims(
    attributions: tuple[OutcomeAttribution, ...],
    source_by_subject: Mapping[tuple[str, str], OutcomeHarmSourceEvent],
) -> tuple[VerifiedCausalClaim, ...]:
    result: list[VerifiedCausalClaim] = []
    for attribution in attributions:
        if attribution.claim_strength != "causal":
            continue
        source = source_by_subject.get(
            (OUTCOME_ATTRIBUTION_RECORDED, attribution.attribution_id)
        )
        if source is None or attribution.verifier_id is None:
            _projection_invalid("causal attribution source evidence is missing")
        result.append(
            VerifiedCausalClaim(
                attribution_id=attribution.attribution_id,
                run_outcome_id=attribution.run_outcome_id,
                memory_revision_ids=attribution.memory_revision_ids,
                effect=cast(
                    Literal["helped", "harmed", "neutral"],
                    attribution.effect,
                ),
                method=cast(
                    Literal[
                        "controlled_experiment",
                        "manual_review",
                        "external_evaluation",
                    ],
                    attribution.method,
                ),
                verifier_id=attribution.verifier_id,
                confidence_micros=_float_confidence_micros(
                    attribution.confidence
                ),
                source_event_sha256=source.event_sha256,
            )
        )
    return tuple(sorted(result, key=lambda item: item.attribution_id))


def _harm_signals(
    claims: tuple[VerifiedCausalClaim, ...],
    attribution_by_id: Mapping[str, OutcomeAttribution],
    *,
    policy: OutcomeHarmPolicy,
) -> tuple[HarmfulMemorySignal, ...]:
    grouped: dict[str, list[VerifiedCausalClaim]] = {}
    for claim in claims:
        if (
            claim.effect != "harmed"
            or claim.confidence_micros < policy.minimum_confidence_micros
        ):
            continue
        for revision_id in claim.memory_revision_ids:
            grouped.setdefault(revision_id, []).append(claim)
    result: list[HarmfulMemorySignal] = []
    for revision_id, harmful_claims in sorted(grouped.items()):
        attribution_ids = tuple(
            sorted(claim.attribution_id for claim in harmful_claims)
        )
        outcome_ids = tuple(
            sorted({claim.run_outcome_id for claim in harmful_claims})
        )
        if len(attribution_ids) < policy.minimum_verified_harmed_claims:
            continue
        if policy.require_distinct_run_outcomes and len(outcome_ids) < (
            policy.minimum_verified_harmed_claims
        ):
            continue
        detected_at = max(
            attribution_by_id[claim.attribution_id].recorded_at
            for claim in harmful_claims
        )
        values = {
            "contract_version": "tbm.harmful-memory-signal.v1",
            "policy_id": policy.policy_id,
            "memory_revision_id": revision_id,
            "attribution_ids": list(attribution_ids),
            "run_outcome_ids": list(outcome_ids),
            "minimum_observed_confidence_micros": min(
                claim.confidence_micros for claim in harmful_claims
            ),
            "detected_at": detected_at,
        }
        result.append(
            HarmfulMemorySignal(
                signal_id=_content_id("harm_signal_sha256_", values),
                policy_id=policy.policy_id,
                memory_revision_id=revision_id,
                attribution_ids=attribution_ids,
                run_outcome_ids=outcome_ids,
                minimum_observed_confidence_micros=cast(
                    int, values["minimum_observed_confidence_micros"]
                ),
                detected_at=detected_at,
            )
        )
    return tuple(result)


def _build_suspension_recommendation(
    signal: HarmfulMemorySignal,
) -> SuspensionRecommendation:
    values = {
        "contract_version": "tbm.suspension-recommendation.v1",
        "signal_id": signal.signal_id,
        "memory_revision_id": signal.memory_revision_id,
        "action": "suspend",
        "reason": "verified_causal_harm_threshold_met",
        "recommended_at": signal.detected_at,
    }
    return SuspensionRecommendation(
        recommendation_id=_content_id(
            "suspension_recommendation_sha256_", values
        ),
        signal_id=signal.signal_id,
        memory_revision_id=signal.memory_revision_id,
        action="suspend",
        reason="verified_causal_harm_threshold_met",
        recommended_at=signal.detected_at,
    )


def _typed_payload(reducer_event: ReducerEvent) -> dict[str, object]:
    if reducer_event.typed_event is None:
        _fail(
            "TBM_OUTCOME_HARM_TYPED_INPUT_REQUIRED",
            "outcome harm reducer requires typed input",
        )
    payload = _thaw_json(reducer_event.typed_event.payload)
    if type(payload) is not dict:
        _transition_invalid("outcome harm payload must be an object")
    return cast(dict[str, object], payload)


def _load_run_outcome_payload(
    payload: Mapping[str, object], event: CanonicalEvent
) -> RunOutcome:
    raw = payload.get("record_json")
    if type(raw) is not str:
        _transition_invalid("RunOutcome event lacks exact record JSON")
    try:
        outcome = loads_run_outcome(raw)
    except ValueError as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_SOURCE_RECORD_INVALID",
            "RunOutcome source record is invalid",
        ) from error
    if (
        outcome.run_outcome_id != payload.get("run_outcome_id")
        or outcome.session_id != payload.get("session_id")
        or canonical_sha256(outcome.to_dict()) != payload.get("record_sha256")
        or event.stream_id != outcome_effect_stream_id(outcome.session_id)
        or event.occurred_at != outcome.measured_at
    ):
        _transition_invalid("RunOutcome source descriptor does not match event")
    return outcome


def _load_attribution_payload(
    payload: Mapping[str, object], event: CanonicalEvent
) -> OutcomeAttribution:
    raw = payload.get("record_json")
    if type(raw) is not str:
        _transition_invalid("OutcomeAttribution event lacks exact record JSON")
    try:
        attribution = loads_outcome_attribution(raw)
    except ValueError as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_SOURCE_RECORD_INVALID",
            "OutcomeAttribution source record is invalid",
        ) from error
    if (
        attribution.attribution_id != payload.get("attribution_id")
        or attribution.run_outcome_id != payload.get("run_outcome_id")
        or attribution.claim_strength != payload.get("claim_strength")
        or attribution.recorded_at != payload.get("recorded_at")
        or canonical_sha256(attribution.to_dict())
        != payload.get("record_sha256")
        or event.stream_id
        != outcome_effect_stream_id(cast(str, payload.get("session_id")))
        or event.occurred_at != attribution.recorded_at
    ):
        _transition_invalid(
            "OutcomeAttribution source descriptor does not match event"
        )
    return attribution


def _load_stored_context_payload(
    payload: Mapping[str, object], event: CanonicalEvent
) -> StoredOutcomeEvaluationContext:
    raw = payload.get("record_json")
    if type(raw) is not str:
        _transition_invalid("context event lacks exact record JSON")
    try:
        stored = loads_stored_outcome_evaluation_context(raw)
    except ValueError as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_EVENT_RECORD_INVALID",
            "stored outcome context is invalid",
        ) from error
    record_value = stored.to_dict()
    context = stored.context
    if (
        payload.get("record_type") != OUTCOME_EVALUATION_CONTEXT_BOUND
        or payload.get("subject_id") != context.run_outcome_id
        or payload.get("record_sha256") != canonical_sha256(record_value)
        or payload.get("record_json") != _canonical_json(record_value)
        or event.occurred_at != context.bound_at
        or event.actor_type != "principal"
        or event.actor_id != context.bound_by
        or event.principal_id != context.bound_by
        or event.agent_client_id != context.bound_via_client_id
        or event.authorization_decision_id != context.authorization_event_id
        or _event_partition(event) != _target_partition(context)
    ):
        _transition_invalid("stored outcome context does not match event")
    return stored


def _verify_stored_context_authorization(
    stored: StoredOutcomeEvaluationContext, *, event: CanonicalEvent
) -> None:
    try:
        verify_authorization_decision(
            stored.policy, stored.request, stored.decision
        )
    except ValueError as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_AUTHORIZATION_INVALID",
            "stored context authorization is invalid",
        ) from error
    context = stored.context
    request = stored.request
    decision = stored.decision
    if (
        not decision.allowed
        or request.permission != "memory:verify"
        or decision.permission != "memory:verify"
        or request.tenant_id != context.tenant_id
        or request.repository_reference is None
        or decision.tenant_id != context.tenant_id
        or decision.repository_id != context.repository_id
        or request.principal_id != context.bound_by
        or request.agent_client_id != context.bound_via_client_id
        or decision.principal_id != context.bound_by
        or decision.agent_client_id != context.bound_via_client_id
        or decision.authorization_event_id != context.authorization_event_id
        or event.authorization_decision_id != context.authorization_event_id
        or parse_rfc3339(decision.decided_at)
        > parse_rfc3339(context.bound_at)
    ):
        _transition_invalid("stored authorization does not match context event")


def _verify_context_event_envelope(event: CanonicalEvent) -> None:
    if (
        event.event_type != OUTCOME_EVALUATION_CONTEXT_BOUND
        or event.stream_type != OUTCOME_HARM_EVENT_STREAM_TYPE
        or event.stream_id
        != outcome_harm_context_stream_id(_event_partition(event))
        or event.classification != "internal"
        or event.producer != "tbm_outcome_harm_runtime"
        or event.producer_version != "f4-v1"
        or event.retention_policy_id != "retention_outcome_harm_events"
    ):
        _fail(
            "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
            "context event envelope is invalid",
        )


def _verify_outcome_source_event_envelope(event: CanonicalEvent) -> None:
    if (
        event.event_type
        not in {RUN_OUTCOME_RECORDED, OUTCOME_ATTRIBUTION_RECORDED}
        or event.stream_type != OUTCOME_EFFECT_EVENT_STREAM_TYPE
        or event.classification != "internal"
        or event.producer != "tbm_outcome_effect_adapter"
        or event.producer_version != "f2-v1"
        or event.retention_policy_id != "retention_outcome_effect_events"
    ):
        _fail(
            "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
            "outcome source event envelope is invalid",
        )


def _verify_context_access(
    access: LedgerAccessContext, context: OutcomeEvaluationContext
) -> None:
    if access.partition != _target_partition(context):
        _fail(
            "TBM_OUTCOME_HARM_SCOPE_DENIED",
            "context target is outside the ledger partition",
        )
    if (
        access.actor_type != "principal"
        or access.actor_id != context.bound_by
        or access.principal_id != context.bound_by
        or access.agent_client_id != context.bound_via_client_id
        or access.authorization_decision_id
        != context.authorization_event_id
    ):
        _fail(
            "TBM_OUTCOME_HARM_ACTOR_MISMATCH",
            "context provenance differs from trusted ledger access",
        )
    if access.classification_filter.allowed != _ALL_CLASSIFICATIONS:
        _fail(
            "TBM_OUTCOME_HARM_CLASSIFICATION_VIEW_INCOMPLETE",
            "outcome harm operations require every classification",
        )


def _scan_outcome_harm_events(
    ledger: EventLedgerPort,
    *,
    event_high_watermark: int | None = None,
) -> tuple[tuple[CanonicalEvent, ...], int]:
    access = _require_ledger(ledger)
    cursor = 0
    scanned = 0
    target = event_high_watermark
    relevant: list[CanonicalEvent] = []
    while True:
        request = LedgerGlobalReadRequest(
            access=access,
            after_position=cursor,
            limit=EVENT_LEDGER_MAX_READ_PAGE,
        )
        page = ledger.read_global(
            after_position=cursor, limit=EVENT_LEDGER_MAX_READ_PAGE
        )
        verify_ledger_global_page(request, page)
        if target is None:
            target = page.high_watermark_global_position
        elif page.high_watermark_global_position < target:
            _fail(
                "TBM_OUTCOME_HARM_LEDGER_READ_FAILED",
                "ledger high watermark moved backwards",
            )
        for event in page.events:
            if event.global_position > target:
                break
            scanned += 1
            if scanned > OUTCOME_HARM_EVENT_MAX_LEDGER_SCAN:
                _fail(
                    "TBM_OUTCOME_HARM_LEDGER_SCAN_LIMIT",
                    "outcome harm ledger scan exceeds its bound",
                )
            if event.event_type in OUTCOME_HARM_INPUT_EVENT_TYPES:
                relevant.append(event)
                if len(relevant) > OUTCOME_HARM_EVENT_MAX_SOURCE_EVENTS:
                    _fail(
                        "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
                        "outcome harm source event limit is exceeded",
                    )
        if target == 0 or not page.has_more:
            break
        next_cursor = page.next_global_position
        if next_cursor is None or next_cursor <= cursor:
            _fail(
                "TBM_OUTCOME_HARM_LEDGER_READ_FAILED",
                "global ledger page lacks a forward cursor",
            )
        cursor = next_cursor
        if cursor >= target:
            break
    return tuple(relevant), cast(int, target)


def _ledger_high_watermark(ledger: EventLedgerPort) -> int:
    access = _require_ledger(ledger)
    request = LedgerGlobalReadRequest(access=access, after_position=0, limit=1)
    page = ledger.read_global(after_position=0, limit=1)
    verify_ledger_global_page(request, page)
    return page.high_watermark_global_position


def _read_context_stream(
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
        if page.events and page.events[0].stream_version != from_version:
            _fail(
                "TBM_OUTCOME_HARM_LEDGER_READ_FAILED",
                "context stream page skipped or repeated its cursor",
            )
        events.extend(page.events)
        if len(events) > OUTCOME_HARM_EVENT_MAX_CONTEXTS:
            _fail(
                "TBM_OUTCOME_HARM_EVENT_SEQUENCE_INVALID",
                "context stream exceeds its event limit",
            )
        if not page.has_more:
            break
        if (
            page.next_stream_version is None
            or page.next_stream_version <= from_version
        ):
            _fail(
                "TBM_OUTCOME_HARM_LEDGER_READ_FAILED",
                "context stream page lacks a forward cursor",
            )
        from_version = page.next_stream_version
    return tuple(events)


def _verify_context_stream(
    ledger: EventLedgerPort,
    stream_id: str,
    events: tuple[CanonicalEvent, ...],
) -> None:
    verification = ledger.verify_stream(stream_id)
    expected_head = None if not events else events[-1].event_sha256
    if (
        not verification.valid
        or verification.verified_stream_version != len(events)
        or verification.head_event_sha256 != expected_head
    ):
        _fail(
            "TBM_OUTCOME_HARM_LEDGER_VERIFICATION_FAILED",
            "retained context stream failed verification",
        )


def _verify_source_streams(
    ledger: EventLedgerPort, events: tuple[CanonicalEvent, ...]
) -> None:
    for stream_id in sorted({event.stream_id for event in events}):
        verification = ledger.verify_stream(stream_id)
        if not verification.valid:
            _fail(
                "TBM_OUTCOME_HARM_LEDGER_VERIFICATION_FAILED",
                "an outcome harm source stream failed verification",
            )


def _require_ledger(ledger: EventLedgerPort) -> LedgerAccessContext:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global", "verify_stream")
    ):
        _fail(
            "TBM_OUTCOME_HARM_LEDGER_INVALID",
            "operation requires an access-bound EventLedgerPort",
        )
    if access.classification_filter.allowed != _ALL_CLASSIFICATIONS:
        _fail(
            "TBM_OUTCOME_HARM_CLASSIFICATION_VIEW_INCOMPLETE",
            "ledger access must include every classification",
        )
    return access


def _parse_context(item: Mapping[str, object]) -> OutcomeEvaluationContext:
    _require_fields(
        item,
        {
            "context_id",
            "contract_version",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "run_outcome_id",
            "session_id",
            "trace_id",
            "run_id",
            "usage_decision_id",
            "usage_decision_sha256",
            "replay_manifest_sha256",
            "retrieval_snapshot_sha256",
            "injection_artifact_id",
            "memory_revision_ids",
            "evaluation_suite",
            "evaluation_case",
            "experiment_id",
            "cohort_id",
            "cohort_arm",
            "assignment_method",
            "assignment_evidence_sha256",
            "bound_by",
            "bound_via_client_id",
            "authorization_event_id",
            "bound_at",
        },
        "OutcomeEvaluationContext",
    )
    revision_values = item.get("memory_revision_ids")
    if type(revision_values) is not list or any(
        type(value) is not str for value in revision_values
    ):
        _record_invalid("context memory_revision_ids are invalid")
    return OutcomeEvaluationContext(
        context_id=_string(item, "context_id"),
        contract_version=_string(item, "contract_version"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        run_outcome_id=_string(item, "run_outcome_id"),
        session_id=_string(item, "session_id"),
        trace_id=_string(item, "trace_id"),
        run_id=_string(item, "run_id"),
        usage_decision_id=_string(item, "usage_decision_id"),
        usage_decision_sha256=_string(item, "usage_decision_sha256"),
        replay_manifest_sha256=_string(item, "replay_manifest_sha256"),
        retrieval_snapshot_sha256=_string(item, "retrieval_snapshot_sha256"),
        injection_artifact_id=_string(item, "injection_artifact_id"),
        memory_revision_ids=tuple(cast(list[str], revision_values)),
        evaluation_suite=_optional_string(item, "evaluation_suite"),
        evaluation_case=_optional_string(item, "evaluation_case"),
        experiment_id=_optional_string(item, "experiment_id"),
        cohort_id=_optional_string(item, "cohort_id"),
        cohort_arm=cast(
            Literal["observational", "with_memory", "without_memory"],
            _string(item, "cohort_arm"),
        ),
        assignment_method=cast(
            Literal["randomized", "matched_control", "manual"] | None,
            _optional_string(item, "assignment_method"),
        ),
        assignment_evidence_sha256=_optional_string(
            item, "assignment_evidence_sha256"
        ),
        bound_by=_string(item, "bound_by"),
        bound_via_client_id=_string(item, "bound_via_client_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        bound_at=_string(item, "bound_at"),
    )


def _parse_stored_context(
    item: Mapping[str, object],
) -> StoredOutcomeEvaluationContext:
    _require_fields(
        item,
        {
            "contract_version",
            "context",
            "policy",
            "request",
            "decision",
            "attestation_verified_by",
        },
        "StoredOutcomeEvaluationContext",
    )
    if _string(item, "contract_version") != (
        "tbm.stored-outcome-evaluation-context.v1"
    ):
        _record_invalid("stored context version is unsupported")
    return StoredOutcomeEvaluationContext(
        context=_parse_context(_mapping(item, "context")),
        policy=parse_authorization_policy(_mapping(item, "policy")),
        request=_parse_authorization_request(_mapping(item, "request")),
        decision=parse_authorization_decision(_mapping(item, "decision")),
        attestation_verified_by=_string(item, "attestation_verified_by"),
    )


def _parse_authorization_request(
    item: Mapping[str, object],
) -> AuthorizationRequest:
    _require_fields(
        item,
        {
            "request_id",
            "principal_id",
            "agent_client_id",
            "tenant_id",
            "repository_reference",
            "permission",
            "requested_at",
        },
        "AuthorizationRequest",
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


def _parse_source_event(item: Mapping[str, object]) -> OutcomeHarmSourceEvent:
    _require_fields(
        item,
        {
            "event_type",
            "event_sha256",
            "global_position",
            "subject_id",
            "occurred_at",
        },
        "OutcomeHarmSourceEvent",
    )
    return OutcomeHarmSourceEvent(
        event_type=_string(item, "event_type"),
        event_sha256=_string(item, "event_sha256"),
        global_position=_integer(item, "global_position"),
        subject_id=_string(item, "subject_id"),
        occurred_at=_string(item, "occurred_at"),
    )


def _context_payload_json_schema() -> Mapping[str, object]:
    properties = {
        "subject_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
        },
        "record_type": {"const": OUTCOME_EVALUATION_CONTEXT_BOUND},
        "record_sha256": {
            "type": "string",
            "pattern": r"^sha256:[0-9a-f]{64}$",
        },
        "record_json": {
            "type": "string",
            "minLength": 2,
            "maxLength": OUTCOME_HARM_JSON_MAX_BYTES,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _projection_digest_value(
    projection: OutcomeHarmProjection,
) -> dict[str, object]:
    return {
        "organization_id": projection.organization_id,
        "tenant_id": projection.tenant_id,
        "repository_id": projection.repository_id,
        "environment_id": projection.environment_id,
        "policy": projection.policy.to_dict(),
        "source_events": [item.to_dict() for item in projection.source_events],
        "contexts": [item.to_dict() for item in projection.contexts],
        "run_outcomes": [item.to_dict() for item in projection.run_outcomes],
        "attributions": [item.to_dict() for item in projection.attributions],
        "evaluated_run_outcome_ids": list(
            projection.evaluated_run_outcome_ids
        ),
        "unevaluated_run_outcome_ids": list(
            projection.unevaluated_run_outcome_ids
        ),
        "observed_associations": [
            item.to_dict() for item in projection.observed_associations
        ],
        "experiment_cohorts": [
            item.to_dict() for item in projection.experiment_cohorts
        ],
        "verified_causal_claims": [
            item.to_dict() for item in projection.verified_causal_claims
        ],
        "harmful_memory_signals": [
            item.to_dict() for item in projection.harmful_memory_signals
        ],
        "suspension_recommendations": [
            item.to_dict()
            for item in projection.suspension_recommendations
        ],
        "last_global_position": projection.last_global_position,
    }


def _empty_projection(
    partition: LedgerTenantPartition, policy: OutcomeHarmPolicy
) -> OutcomeHarmProjection:
    return OutcomeHarmProjection(
        organization_id=partition.organization_id,
        tenant_id=partition.tenant_id,
        repository_id=partition.repository_id,
        environment_id=partition.environment_id,
        policy=policy,
        source_events=(),
        contexts=(),
        run_outcomes=(),
        attributions=(),
        evaluated_run_outcome_ids=(),
        unevaluated_run_outcome_ids=(),
        observed_associations=(),
        experiment_cohorts=(),
        verified_causal_claims=(),
        harmful_memory_signals=(),
        suspension_recommendations=(),
        last_global_position=0,
    )


def _target_partition(value: object) -> LedgerTenantPartition:
    try:
        return LedgerTenantPartition(
            organization_id=cast(str, getattr(value, "organization_id")),
            tenant_id=cast(str, getattr(value, "tenant_id")),
            repository_id=cast(str, getattr(value, "repository_id")),
            environment_id=cast(str, getattr(value, "environment_id")),
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_RECORD_INVALID",
            "outcome harm target partition is invalid",
        ) from error


def _event_partition(event: CanonicalEvent) -> LedgerTenantPartition:
    return LedgerTenantPartition(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
    )


def _trusted_verifier_set(values: tuple[str, ...]) -> frozenset[str]:
    if type(values) is not tuple or not values or len(values) > 64:
        _record_invalid(
            "trusted attestation verifier IDs must be a bounded tuple"
        )
    if any(type(value) is not str for value in values):
        _record_invalid("trusted attestation verifier IDs must be strings")
    if len(values) != len(set(values)):
        _record_invalid("trusted attestation verifier IDs must be unique")
    for value in values:
        _identifier(value, "trusted_attestation_verifier_id")
    return frozenset(values)


def _content_id(prefix: str, value: Mapping[str, object]) -> str:
    return prefix + canonical_sha256(value).removeprefix("sha256:")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(
        domain + _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_RECORD_INVALID",
            "outcome harm value is not canonical JSON",
        ) from error


def _loads_record(document: str | bytes, description: str) -> dict[str, object]:
    if type(document) is bytes:
        try:
            source = document.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise OutcomeHarmEventV1Error(
                "TBM_OUTCOME_HARM_JSON_INVALID",
                f"{description} must be strict UTF-8 JSON",
            ) from error
    elif type(document) is str:
        source = document
    else:
        _record_invalid(f"{description} must be JSON text")
    try:
        size = len(source.encode("utf-8"))
    except UnicodeError as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_JSON_INVALID",
            f"{description} must be strict UTF-8 JSON",
        ) from error
    if size > OUTCOME_HARM_JSON_MAX_BYTES:
        _record_invalid(f"{description} exceeds the byte limit")
    try:
        value = parse_bounded_json(
            source,
            max_depth=OUTCOME_HARM_JSON_MAX_DEPTH,
            max_nodes=OUTCOME_HARM_JSON_MAX_NODES,
            description=description,
        )
    except (TypeError, ValueError) as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_JSON_INVALID", f"{description} is invalid"
        ) from error
    if type(value) is not dict:
        _record_invalid(f"{description} must be an object")
    return cast(dict[str, object], value)


def _identifier(
    value: object,
    name: str,
    *,
    projection: bool = False,
) -> str:
    invalid = _projection_invalid if projection else _record_invalid
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        invalid(f"{name} is invalid")
    return value


def _digest(
    value: object,
    name: str,
    *,
    projection: bool = False,
) -> str:
    invalid = _projection_invalid if projection else _record_invalid
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        invalid(f"{name} is invalid")
    return value


def _timestamp(
    value: object,
    name: str,
    *,
    projection: bool = False,
) -> str:
    invalid = _projection_invalid if projection else _record_invalid
    try:
        if type(value) is not str:
            raise ValueError
        canonical = canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        try:
            invalid(f"{name} is invalid")
        except OutcomeHarmEventV1Error as wrapped:
            raise wrapped from error
    return canonical


def _revision_id(
    value: object, *, projection: bool = False
) -> str:
    invalid = _projection_invalid if projection else _record_invalid
    if type(value) is not str or _REVISION_ID_RE.fullmatch(value) is None:
        invalid("memory revision ID is invalid")
    return value


def _revision_ids(
    values: object, *, projection: bool = False
) -> tuple[str, ...]:
    invalid = _projection_invalid if projection else _record_invalid
    if (
        type(values) is not tuple
        or len(values) > 256
        or any(type(value) is not str for value in values)
    ):
        invalid("memory_revision_ids must be a bounded tuple")
    typed = cast(tuple[str, ...], values)
    if typed != tuple(sorted(set(typed))):
        invalid("memory_revision_ids must be canonical and unique")
    for value in typed:
        _revision_id(value, projection=projection)
    return typed


def _canonical_identifiers(
    values: object,
    name: str,
    *,
    projection: bool,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    invalid = _projection_invalid if projection else _record_invalid
    if (
        type(values) is not tuple
        or (not values and not allow_empty)
        or len(values) > OUTCOME_HARM_EVENT_MAX_SOURCE_EVENTS
        or any(type(value) is not str for value in values)
    ):
        invalid(f"{name} must be a bounded tuple")
    typed = cast(tuple[str, ...], values)
    if typed != tuple(sorted(set(typed))):
        invalid(f"{name} must be canonical and unique")
    for value in typed:
        _identifier(value, name, projection=projection)
    return typed


def _confidence_micros(value: object, *, projection: bool) -> int:
    invalid = _projection_invalid if projection else _record_invalid
    if type(value) is not int or not 0 <= value <= 1_000_000:
        invalid("confidence_micros is invalid")
    return value


def _float_confidence_micros(value: float) -> int:
    try:
        decimal = Decimal(str(value)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_EVEN
        )
    except (InvalidOperation, ValueError) as error:
        raise OutcomeHarmEventV1Error(
            "TBM_OUTCOME_HARM_SOURCE_RECORD_INVALID",
            "attribution confidence is invalid",
        ) from error
    micros = int(decimal * Decimal(1_000_000))
    _confidence_micros(micros, projection=True)
    return micros


def _typed_tuple(
    values: object, expected: type[object], name: str
) -> tuple[object, ...]:
    if type(values) is not tuple or any(type(value) is not expected for value in values):
        _projection_invalid(f"{name} contains invalid records")
    return cast(tuple[object, ...], values)


def _state_list(state: Mapping[str, object], name: str) -> list[object]:
    value = _thaw_json(state.get(name))
    if type(value) is not list:
        _projection_invalid(f"reducer state {name} is invalid")
    return cast(list[object], value)


def _as_mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        type(key) is not str for key in value
    ):
        _projection_invalid(f"{description} state is invalid")
    return cast(Mapping[str, object], value)


def _require_fields(
    item: Mapping[str, object], fields: set[str], description: str
) -> None:
    if any(type(key) is not str for key in item):
        _record_invalid(f"{description} must have string fields")
    actual = set(item)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        detail = missing[0] if missing else extra[0]
        _record_invalid(f"{description} has invalid field: {detail}")


def _string(item: Mapping[str, object], name: str) -> str:
    value = item.get(name)
    if type(value) is not str:
        _record_invalid(f"{name} must be a string")
    return value


def _optional_string(item: Mapping[str, object], name: str) -> str | None:
    value = item.get(name)
    if value is not None and type(value) is not str:
        _record_invalid(f"{name} must be a string or null")
    return cast(str | None, value)


def _integer(item: Mapping[str, object], name: str) -> int:
    value = item.get(name)
    if type(value) is not int:
        _record_invalid(f"{name} must be an integer")
    return value


def _mapping(item: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = item.get(name)
    if not isinstance(value, Mapping) or any(
        type(key) is not str for key in value
    ):
        _record_invalid(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    if type(value) is list:
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "DurableOutcomeHarmSnapshot",
    "ExperimentCohortMetrics",
    "HarmfulMemorySignal",
    "MemoryAssociationMetrics",
    "OUTCOME_EVALUATION_CONTEXT_BOUND",
    "OUTCOME_HARM_CONTEXT_SCHEMA_ID",
    "OUTCOME_HARM_EVENT_PAYLOAD_SCHEMA_ID",
    "OUTCOME_HARM_EVENT_PROTOCOL_VERSION",
    "OUTCOME_HARM_INPUT_EVENT_TYPES",
    "OutcomeEvaluationContext",
    "OutcomeHarmAppendResult",
    "OutcomeHarmEventV1Error",
    "OutcomeHarmPolicy",
    "OutcomeHarmProjection",
    "OutcomeHarmSourceEvent",
    "StoredOutcomeEvaluationContext",
    "SuspensionRecommendation",
    "VerifiedCausalClaim",
    "append_outcome_evaluation_contexts",
    "build_outcome_evaluation_context",
    "build_outcome_harm_event_batch",
    "build_outcome_harm_event_registry",
    "build_outcome_harm_policy",
    "build_outcome_harm_reducer",
    "dumps_outcome_evaluation_context",
    "dumps_outcome_harm_event_payload_dispatch_schema",
    "dumps_stored_outcome_evaluation_context",
    "loads_outcome_evaluation_context",
    "loads_stored_outcome_evaluation_context",
    "outcome_evaluation_context_schema",
    "outcome_harm_context_stream_id",
    "outcome_harm_event_payload_dispatch_schema",
    "rebuild_outcome_harm_from_ledger",
    "reduce_outcome_harm_events",
]
