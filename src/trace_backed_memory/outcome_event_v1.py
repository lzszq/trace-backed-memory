from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import NoReturn, cast

from ._timestamps import RFC3339_PATTERN, canonical_rfc3339
from .contracts_v3 import V3ContractError
from .event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
    verify_event_parent,
)
from .gate_session_event_v1 import (
    EXECUTION_STARTED_EVENT,
    GATE_SESSION_EVENT_CONTRACT_VERSION,
    GATE_SESSION_EVENT_TYPES,
    GATE_SESSION_LEASE_RENEWED_EVENT,
    GATE_SESSION_EVENT_RETENTION_POLICY_ID,
    gate_session_event_id,
    gate_session_revision_sha256,
)
from .gate_session_v3 import GateSession, parse_gate_session
from .outcome_v3 import (
    OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
    RUN_OUTCOME_CONTRACT_VERSION,
    OutcomeAttribution,
    RunOutcome,
    parse_outcome_attribution,
    parse_run_outcome,
    verify_outcome_attribution,
    verify_run_outcome,
)


OUTCOME_EVENT_CONTRACT_VERSION = "tbm.outcome-event.v1"
OUTCOME_EVENT_VERSION = 1
OUTCOME_EVENT_PRODUCER = "trace_backed_memory"
OUTCOME_EVENT_PRODUCER_VERSION = "0.1.0"
OUTCOME_EVENT_RETENTION_POLICY_ID = GATE_SESSION_EVENT_RETENTION_POLICY_ID
OUTCOME_EVENT_STREAM_TYPE = "run_outcome"
OUTCOME_ATTRIBUTION_EVENT_STREAM_TYPE = "outcome_attribution"

EVALUATION_AUTHENTICATED_EVENT = "tbm.evaluation.authenticated"
RUN_OUTCOME_RECORDED_EVENT = "tbm.run_outcome.recorded"
OUTCOME_ATTRIBUTION_PROPOSED_EVENT = "tbm.outcome_attribution.proposed"
OUTCOME_ATTRIBUTION_VERIFIED_EVENT = "tbm.outcome_attribution.verified"
OUTCOME_EVENT_TYPES = (
    EVALUATION_AUTHENTICATED_EVENT,
    RUN_OUTCOME_RECORDED_EVENT,
    OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
    OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_OUTCOME_ID_RE = re.compile(r"^run_outcome_sha256_[0-9a-f]{64}$")
_ATTRIBUTION_ID_RE = re.compile(
    r"^outcome_attribution_sha256_[0-9a-f]{64}$"
)
_MEMORY_REVISION_ID_RE = re.compile(
    r"^memory_revision_sha256_[0-9a-f]{64}$"
)


class OutcomeEventV1Error(V3ContractError):
    """Stable failure for canonical outcome and attribution events."""


@dataclass(frozen=True)
class OutcomeEvaluatorEventContext:
    evaluator_id: str
    evaluator_version: str
    authenticator_id: str
    credential_id: str

    def __post_init__(self) -> None:
        for name in (
            "evaluator_id",
            "evaluator_version",
            "authenticator_id",
            "credential_id",
        ):
            _identifier(getattr(self, name), name)

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "authenticator_id": self.authenticator_id,
            "credential_id": self.credential_id,
        }


@dataclass(frozen=True)
class EvaluationAuthenticatedRef:
    session_id: str
    trace_id: str
    run_id: str
    usage_decision_id: str
    run_outcome_id: str
    evaluator: OutcomeEvaluatorEventContext
    execution_event_id: str
    transition_authorization_event_id: str

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "trace_id",
            "run_id",
            "usage_decision_id",
            "run_outcome_id",
            "execution_event_id",
            "transition_authorization_event_id",
        ):
            _identifier(getattr(self, name), name)
        if type(self.evaluator) is not OutcomeEvaluatorEventContext:
            _invalid("evaluator must be exactly OutcomeEvaluatorEventContext")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": OUTCOME_EVENT_CONTRACT_VERSION,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "usage_decision_id": self.usage_decision_id,
            "run_outcome_id": self.run_outcome_id,
            "evaluator": self.evaluator.to_dict(),
            "execution_event_id": self.execution_event_id,
            "transition_authorization_event_id": (
                self.transition_authorization_event_id
            ),
        }


@dataclass(frozen=True)
class RunOutcomeRecordedRef:
    outcome: RunOutcome
    completed_session_sha256: str
    final_memory_revision_ids: tuple[str, ...]
    evaluation_event_id: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not RunOutcome:
            _invalid("outcome must be exactly RunOutcome")
        _digest(self.completed_session_sha256, "completed_session_sha256")
        if (
            type(self.final_memory_revision_ids) is not tuple
            or tuple(sorted(self.final_memory_revision_ids))
            != self.final_memory_revision_ids
            or len(set(self.final_memory_revision_ids))
            != len(self.final_memory_revision_ids)
        ):
            _invalid("final_memory_revision_ids must be sorted and unique")
        for revision_id in self.final_memory_revision_ids:
            _identifier(revision_id, "final_memory_revision_id")
        _identifier(self.evaluation_event_id, "evaluation_event_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": OUTCOME_EVENT_CONTRACT_VERSION,
            "outcome": self.outcome.to_dict(),
            "completed_session_sha256": self.completed_session_sha256,
            "final_memory_revision_ids": list(self.final_memory_revision_ids),
            "evaluation_event_id": self.evaluation_event_id,
        }


@dataclass(frozen=True)
class OutcomeAttributionProposalRef:
    attribution_id: str
    run_outcome_id: str
    usage_decision_id: str
    memory_revision_ids: tuple[str, ...]
    claim_strength: str
    effect: str
    method: str
    evaluator_id: str
    evaluator_version: str
    evidence_artifact_sha256s: tuple[str, ...]
    confidence: float
    reason: str
    recorded_at: str
    run_outcome_event_id: str

    def __post_init__(self) -> None:
        if (
            type(self.attribution_id) is not str
            or _ATTRIBUTION_ID_RE.fullmatch(self.attribution_id) is None
        ):
            _invalid("attribution_id must be a canonical content identifier")
        if (
            type(self.run_outcome_id) is not str
            or _RUN_OUTCOME_ID_RE.fullmatch(self.run_outcome_id) is None
        ):
            _invalid("run_outcome_id must be a canonical content identifier")
        for name in (
            "usage_decision_id",
            "evaluator_id",
            "evaluator_version",
            "run_outcome_event_id",
        ):
            _identifier(getattr(self, name), name)
        if (
            type(self.memory_revision_ids) is not tuple
            or tuple(sorted(self.memory_revision_ids))
            != self.memory_revision_ids
            or len(set(self.memory_revision_ids))
            != len(self.memory_revision_ids)
            or len(self.memory_revision_ids) > 50
        ):
            _invalid("memory_revision_ids must be bounded, sorted, and unique")
        if any(
            type(revision_id) is not str
            or _MEMORY_REVISION_ID_RE.fullmatch(revision_id) is None
            for revision_id in self.memory_revision_ids
        ):
            _invalid("memory_revision_ids must be canonical revision identifiers")
        if self.claim_strength == "association":
            if self.method != "runtime_observation":
                _invalid("association proposals must use runtime_observation")
        elif self.claim_strength == "causal":
            if self.method not in {
                "controlled_experiment",
                "manual_review",
                "external_evaluation",
            }:
                _invalid("causal proposals require a non-observational method")
            if self.effect == "unknown":
                _invalid("causal proposals cannot have an unknown effect")
        else:
            _invalid("claim_strength must be association or causal")
        if self.effect not in {"helped", "harmed", "neutral", "unknown"}:
            _invalid("effect is not supported")
        if (
            type(self.evidence_artifact_sha256s) is not tuple
            or not 1 <= len(self.evidence_artifact_sha256s) <= 64
            or tuple(sorted(self.evidence_artifact_sha256s))
            != self.evidence_artifact_sha256s
            or len(set(self.evidence_artifact_sha256s))
            != len(self.evidence_artifact_sha256s)
        ):
            _invalid(
                "evidence_artifact_sha256s must be bounded, sorted, and unique"
            )
        for digest in self.evidence_artifact_sha256s:
            _digest(digest, "evidence_artifact_sha256")
        if (
            type(self.confidence) is not float
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            _invalid("confidence must be a finite number from 0 to 1")
        if (
            type(self.reason) is not str
            or not self.reason
            or len(self.reason) > 1024
        ):
            _invalid("reason must be bounded non-empty text")
        if (
            type(self.recorded_at) is not str
            or canonical_rfc3339(self.recorded_at) != self.recorded_at
        ):
            _invalid("recorded_at must be a canonical RFC 3339 timestamp")

    @classmethod
    def from_attribution(
        cls,
        attribution: OutcomeAttribution,
        *,
        run_outcome_event_id: str,
    ) -> OutcomeAttributionProposalRef:
        if type(attribution) is not OutcomeAttribution:
            _invalid("attribution must be exactly OutcomeAttribution")
        return cls(
            attribution_id=attribution.attribution_id,
            run_outcome_id=attribution.run_outcome_id,
            usage_decision_id=attribution.usage_decision_id,
            memory_revision_ids=attribution.memory_revision_ids,
            claim_strength=attribution.claim_strength,
            effect=attribution.effect,
            method=attribution.method,
            evaluator_id=attribution.evaluator_id,
            evaluator_version=attribution.evaluator_version,
            evidence_artifact_sha256s=attribution.evidence_artifact_sha256s,
            confidence=attribution.confidence,
            reason=attribution.reason,
            recorded_at=attribution.recorded_at,
            run_outcome_event_id=run_outcome_event_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": OUTCOME_EVENT_CONTRACT_VERSION,
            "attribution_id": self.attribution_id,
            "run_outcome_id": self.run_outcome_id,
            "usage_decision_id": self.usage_decision_id,
            "memory_revision_ids": list(self.memory_revision_ids),
            "claim_strength": self.claim_strength,
            "effect": self.effect,
            "method": self.method,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "evidence_artifact_sha256s": list(
                self.evidence_artifact_sha256s
            ),
            "confidence": self.confidence,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "run_outcome_event_id": self.run_outcome_event_id,
        }

    def to_attribution(self) -> OutcomeAttribution:
        return parse_outcome_attribution(
            {
                "contract_version": OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
                "attribution_id": self.attribution_id,
                "run_outcome_id": self.run_outcome_id,
                "usage_decision_id": self.usage_decision_id,
                "memory_revision_ids": list(self.memory_revision_ids),
                "claim_strength": self.claim_strength,
                "effect": self.effect,
                "method": self.method,
                "evaluator_id": self.evaluator_id,
                "evaluator_version": self.evaluator_version,
                "verifier_id": None,
                "evidence_artifact_sha256s": list(
                    self.evidence_artifact_sha256s
                ),
                "confidence": self.confidence,
                "reason": self.reason,
                "recorded_at": self.recorded_at,
            }
        )


@dataclass(frozen=True)
class OutcomeAttributionVerifiedRef:
    attribution: OutcomeAttribution
    proposal_event_id: str

    def __post_init__(self) -> None:
        if type(self.attribution) is not OutcomeAttribution:
            _invalid("attribution must be exactly OutcomeAttribution")
        if self.attribution.claim_strength != "causal":
            _invalid("only causal attribution requires a verified event")
        _identifier(self.proposal_event_id, "proposal_event_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": OUTCOME_EVENT_CONTRACT_VERSION,
            "attribution": self.attribution.to_dict(),
            "proposal_event_id": self.proposal_event_id,
        }


def build_run_outcome_event_batch(
    outcome: RunOutcome,
    *,
    executing_session: GateSession,
    completed_session: GateSession,
    execution_event: CanonicalEvent,
    evaluator_context: OutcomeEvaluatorEventContext,
    first_global_position: int,
    trusted_context: EventTrustedContext,
) -> tuple[CanonicalEvent, CanonicalEvent]:
    _verify_completion_inputs(
        outcome,
        executing_session,
        completed_session,
        execution_event,
        evaluator_context,
        trusted_context,
    )
    if first_global_position <= execution_event.global_position:
        _invalid("outcome event positions must follow execution evidence")
    evaluation_ref = EvaluationAuthenticatedRef(
        session_id=outcome.session_id,
        trace_id=outcome.trace_id,
        run_id=outcome.run_id,
        usage_decision_id=outcome.usage_decision_id,
        run_outcome_id=outcome.run_outcome_id,
        evaluator=evaluator_context,
        execution_event_id=execution_event.event_id,
        transition_authorization_event_id=(
            trusted_context.authorization_decision_id
        ),
    )
    outcome_stream_id = run_outcome_event_stream_id(outcome.run_outcome_id)
    identity_sha256 = _domain_sha256(
        b"tbm.outcome-command-identity.v1\x00",
        {
            "outcome": outcome.to_dict(),
            "evaluator": evaluator_context.to_dict(),
            "authorization_decision_id": (
                trusted_context.authorization_decision_id
            ),
        },
    )
    request_sha256 = _domain_sha256(
        b"tbm.outcome-command.v1\x00",
        {
            "evaluation": evaluation_ref.to_dict(),
            "outcome": outcome.to_dict(),
            "completed_session_sha256": gate_session_revision_sha256(
                completed_session
            ),
        },
    )
    evaluation_event = build_canonical_event(
        event_id=_outcome_event_id("evaluation", outcome.run_outcome_id),
        event_type=EVALUATION_AUTHENTICATED_EVENT,
        event_version=OUTCOME_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=outcome_stream_id,
        stream_type=OUTCOME_EVENT_STREAM_TYPE,
        stream_version=1,
        global_position=first_global_position,
        trusted_context=trusted_context,
        request_id=_outcome_request_id(outcome.run_outcome_id),
        idempotency_key_sha256=identity_sha256,
        request_sha256=request_sha256,
        correlation_id=_outcome_correlation_id(outcome.session_id),
        causation_id=execution_event.event_id,
        occurred_at=outcome.measured_at,
        recorded_at=outcome.measured_at,
        producer=OUTCOME_EVENT_PRODUCER,
        producer_version=OUTCOME_EVENT_PRODUCER_VERSION,
        payload_schema=f"{EVALUATION_AUTHENTICATED_EVENT}.v1",
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id=OUTCOME_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=evaluation_ref.to_dict(),
    )
    recorded_ref = RunOutcomeRecordedRef(
        outcome=outcome,
        completed_session_sha256=gate_session_revision_sha256(
            completed_session
        ),
        final_memory_revision_ids=completed_session.final_memory_revision_ids,
        evaluation_event_id=evaluation_event.event_id,
    )
    outcome_event = build_canonical_event(
        event_id=_outcome_event_id("recorded", outcome.run_outcome_id),
        event_type=RUN_OUTCOME_RECORDED_EVENT,
        event_version=OUTCOME_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=outcome_stream_id,
        stream_type=OUTCOME_EVENT_STREAM_TYPE,
        stream_version=2,
        global_position=first_global_position + 1,
        trusted_context=trusted_context,
        request_id=_outcome_request_id(outcome.run_outcome_id),
        idempotency_key_sha256=identity_sha256,
        request_sha256=request_sha256,
        correlation_id=_outcome_correlation_id(outcome.session_id),
        causation_id=evaluation_event.event_id,
        occurred_at=outcome.measured_at,
        recorded_at=outcome.measured_at,
        producer=OUTCOME_EVENT_PRODUCER,
        producer_version=OUTCOME_EVENT_PRODUCER_VERSION,
        payload_schema=f"{RUN_OUTCOME_RECORDED_EVENT}.v1",
        previous_stream_event_sha256=evaluation_event.event_sha256,
        classification="internal",
        retention_policy_id=OUTCOME_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=recorded_ref.to_dict(),
    )
    parse_evaluation_authenticated_event(evaluation_event)
    parsed = parse_run_outcome_recorded_event(
        outcome_event,
        evaluation_event=evaluation_event,
        completed_session=completed_session,
    )
    if parsed != recorded_ref:
        raise AssertionError("outcome event did not round-trip")
    return (evaluation_event, outcome_event)


def build_outcome_attribution_event_batch(
    attribution: OutcomeAttribution,
    *,
    outcome_event: CanonicalEvent,
    completed_session: GateSession,
    first_global_position: int,
    trusted_context: EventTrustedContext,
) -> tuple[CanonicalEvent, ...]:
    if type(attribution) is not OutcomeAttribution:
        _invalid("attribution must be exactly OutcomeAttribution")
    if type(completed_session) is not GateSession:
        _invalid("completed_session must be exactly GateSession")
    try:
        verify_outcome_attribution(
            attribution,
            parse_run_outcome_recorded_event(outcome_event).outcome,
            completed_session,
        )
    except Exception as error:
        raise OutcomeEventV1Error(
            "TBM_OUTCOME_EVENT_INVALID",
            "attribution linkage is invalid",
        ) from error
    if outcome_event.event_type != RUN_OUTCOME_RECORDED_EVENT:
        _invalid("attribution parent is not RunOutcomeRecorded")
    _verify_scope(outcome_event, trusted_context, require_authorization=False)
    if first_global_position <= outcome_event.global_position:
        _invalid("attribution event position must follow RunOutcomeRecorded")
    proposal_ref = OutcomeAttributionProposalRef.from_attribution(
        attribution,
        run_outcome_event_id=outcome_event.event_id,
    )
    stream_id = outcome_attribution_event_stream_id(
        attribution.attribution_id
    )
    identity_sha256 = _domain_sha256(
        b"tbm.outcome-attribution-command-identity.v1\x00",
        {
            "attribution_id": attribution.attribution_id,
            "authorization_decision_id": (
                trusted_context.authorization_decision_id
            ),
        },
    )
    request_sha256 = _domain_sha256(
        b"tbm.outcome-attribution-command.v1\x00",
        attribution.to_dict(),
    )
    proposal_event = build_canonical_event(
        event_id=_attribution_event_id("proposed", attribution.attribution_id),
        event_type=OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
        event_version=OUTCOME_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=stream_id,
        stream_type=OUTCOME_ATTRIBUTION_EVENT_STREAM_TYPE,
        stream_version=1,
        global_position=first_global_position,
        trusted_context=trusted_context,
        request_id=_attribution_request_id(attribution.attribution_id),
        idempotency_key_sha256=identity_sha256,
        request_sha256=request_sha256,
        correlation_id=_outcome_correlation_id(completed_session.session_id),
        causation_id=outcome_event.event_id,
        occurred_at=attribution.recorded_at,
        recorded_at=attribution.recorded_at,
        producer=OUTCOME_EVENT_PRODUCER,
        producer_version=OUTCOME_EVENT_PRODUCER_VERSION,
        payload_schema=f"{OUTCOME_ATTRIBUTION_PROPOSED_EVENT}.v1",
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id=OUTCOME_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=proposal_ref.to_dict(),
    )
    parse_outcome_attribution_proposed_event(proposal_event)
    if attribution.claim_strength == "association":
        return (proposal_event,)
    verified_ref = OutcomeAttributionVerifiedRef(
        attribution=attribution,
        proposal_event_id=proposal_event.event_id,
    )
    verified_event = build_canonical_event(
        event_id=_attribution_event_id("verified", attribution.attribution_id),
        event_type=OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
        event_version=OUTCOME_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=stream_id,
        stream_type=OUTCOME_ATTRIBUTION_EVENT_STREAM_TYPE,
        stream_version=2,
        global_position=first_global_position + 1,
        trusted_context=trusted_context,
        request_id=_attribution_request_id(attribution.attribution_id),
        idempotency_key_sha256=identity_sha256,
        request_sha256=request_sha256,
        correlation_id=_outcome_correlation_id(completed_session.session_id),
        causation_id=proposal_event.event_id,
        occurred_at=attribution.recorded_at,
        recorded_at=attribution.recorded_at,
        producer=OUTCOME_EVENT_PRODUCER,
        producer_version=OUTCOME_EVENT_PRODUCER_VERSION,
        payload_schema=f"{OUTCOME_ATTRIBUTION_VERIFIED_EVENT}.v1",
        previous_stream_event_sha256=proposal_event.event_sha256,
        classification="internal",
        retention_policy_id=OUTCOME_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=verified_ref.to_dict(),
    )
    parsed = parse_outcome_attribution_verified_event(
        verified_event,
        proposal_event=proposal_event,
    )
    if parsed != verified_ref:
        raise AssertionError("verified attribution event did not round-trip")
    return (proposal_event, verified_event)


def parse_evaluation_authenticated_event(
    event: CanonicalEvent,
) -> EvaluationAuthenticatedRef:
    _verify_event_shape(event, EVALUATION_AUTHENTICATED_EVENT, stream_version=1)
    payload = _payload(event)
    if set(payload) != {
        "contract_version",
        "session_id",
        "trace_id",
        "run_id",
        "usage_decision_id",
        "run_outcome_id",
        "evaluator",
        "execution_event_id",
        "transition_authorization_event_id",
    } or payload.get("contract_version") != OUTCOME_EVENT_CONTRACT_VERSION:
        _invalid("EvaluationAuthenticated payload is invalid")
    evaluator = payload.get("evaluator")
    if type(evaluator) is not dict or set(evaluator) != {
        "evaluator_id",
        "evaluator_version",
        "authenticator_id",
        "credential_id",
    }:
        _invalid("EvaluationAuthenticated evaluator payload is invalid")
    record = EvaluationAuthenticatedRef(
        session_id=cast(str, payload["session_id"]),
        trace_id=cast(str, payload["trace_id"]),
        run_id=cast(str, payload["run_id"]),
        usage_decision_id=cast(str, payload["usage_decision_id"]),
        run_outcome_id=cast(str, payload["run_outcome_id"]),
        evaluator=OutcomeEvaluatorEventContext(
            evaluator_id=cast(str, evaluator["evaluator_id"]),
            evaluator_version=cast(str, evaluator["evaluator_version"]),
            authenticator_id=cast(str, evaluator["authenticator_id"]),
            credential_id=cast(str, evaluator["credential_id"]),
        ),
        execution_event_id=cast(str, payload["execution_event_id"]),
        transition_authorization_event_id=cast(
            str,
            payload["transition_authorization_event_id"],
        ),
    )
    if (
        record.execution_event_id != event.causation_id
        or record.transition_authorization_event_id
        != event.authorization_decision_id
    ):
        _invalid("EvaluationAuthenticated envelope linkage is invalid")
    return record


def parse_run_outcome_recorded_event(
    event: CanonicalEvent,
    *,
    evaluation_event: CanonicalEvent | None = None,
    completed_session: GateSession | None = None,
) -> RunOutcomeRecordedRef:
    _verify_event_shape(event, RUN_OUTCOME_RECORDED_EVENT, stream_version=2)
    payload = _payload(event)
    if set(payload) != {
        "contract_version",
        "outcome",
        "completed_session_sha256",
        "final_memory_revision_ids",
        "evaluation_event_id",
    } or payload.get("contract_version") != OUTCOME_EVENT_CONTRACT_VERSION:
        _invalid("RunOutcomeRecorded payload is invalid")
    outcome_payload = payload.get("outcome")
    revision_ids = payload.get("final_memory_revision_ids")
    if type(outcome_payload) is not dict or type(revision_ids) is not list:
        _invalid("RunOutcomeRecorded nested payload is invalid")
    try:
        outcome = parse_run_outcome(outcome_payload)
    except Exception as error:
        raise OutcomeEventV1Error(
            "TBM_OUTCOME_EVENT_INVALID",
            "RunOutcomeRecorded outcome is invalid",
        ) from error
    record = RunOutcomeRecordedRef(
        outcome=outcome,
        completed_session_sha256=cast(str, payload["completed_session_sha256"]),
        final_memory_revision_ids=tuple(cast(list[str], revision_ids)),
        evaluation_event_id=cast(str, payload["evaluation_event_id"]),
    )
    if record.evaluation_event_id != event.causation_id:
        _invalid("RunOutcomeRecorded causation is invalid")
    if evaluation_event is not None:
        evaluation = parse_evaluation_authenticated_event(evaluation_event)
        try:
            verify_event_parent(event, evaluation_event)
        except Exception as error:
            raise OutcomeEventV1Error(
                "TBM_OUTCOME_EVENT_INVALID",
                "RunOutcomeRecorded stream parent is invalid",
            ) from error
        if (
            evaluation_event.event_id != record.evaluation_event_id
            or evaluation.run_outcome_id != outcome.run_outcome_id
            or evaluation.session_id != outcome.session_id
            or evaluation.trace_id != outcome.trace_id
            or evaluation.run_id != outcome.run_id
            or evaluation.usage_decision_id != outcome.usage_decision_id
            or evaluation.evaluator.evaluator_id != outcome.evaluator_id
            or evaluation.evaluator.evaluator_version
            != outcome.evaluator_version
            or not _same_scope(evaluation_event, event)
        ):
            _invalid("RunOutcomeRecorded evaluation parent is inconsistent")
    if completed_session is not None:
        if type(completed_session) is not GateSession:
            _invalid("completed_session must be exactly GateSession")
        try:
            verify_run_outcome(outcome, completed_session)
        except Exception as error:
            raise OutcomeEventV1Error(
                "TBM_OUTCOME_EVENT_INVALID",
                "RunOutcomeRecorded completed session is inconsistent",
            ) from error
        if (
            record.completed_session_sha256
            != gate_session_revision_sha256(completed_session)
            or record.final_memory_revision_ids
            != completed_session.final_memory_revision_ids
        ):
            _invalid("RunOutcomeRecorded completed session evidence is inconsistent")
    return record


def parse_outcome_attribution_proposed_event(
    event: CanonicalEvent,
) -> OutcomeAttributionProposalRef:
    _verify_event_shape(
        event,
        OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
        stream_version=1,
    )
    payload = _payload(event)
    expected = {
        "contract_version",
        "attribution_id",
        "run_outcome_id",
        "usage_decision_id",
        "memory_revision_ids",
        "claim_strength",
        "effect",
        "method",
        "evaluator_id",
        "evaluator_version",
        "evidence_artifact_sha256s",
        "confidence",
        "reason",
        "recorded_at",
        "run_outcome_event_id",
    }
    if set(payload) != expected or payload.get("contract_version") != OUTCOME_EVENT_CONTRACT_VERSION:
        _invalid("OutcomeAttributionProposed payload is invalid")
    revisions = payload.get("memory_revision_ids")
    artifacts = payload.get("evidence_artifact_sha256s")
    if type(revisions) is not list or type(artifacts) is not list:
        _invalid("OutcomeAttributionProposed arrays are invalid")
    record = OutcomeAttributionProposalRef(
        attribution_id=cast(str, payload["attribution_id"]),
        run_outcome_id=cast(str, payload["run_outcome_id"]),
        usage_decision_id=cast(str, payload["usage_decision_id"]),
        memory_revision_ids=tuple(cast(list[str], revisions)),
        claim_strength=cast(str, payload["claim_strength"]),
        effect=cast(str, payload["effect"]),
        method=cast(str, payload["method"]),
        evaluator_id=cast(str, payload["evaluator_id"]),
        evaluator_version=cast(str, payload["evaluator_version"]),
        evidence_artifact_sha256s=tuple(cast(list[str], artifacts)),
        confidence=cast(float, payload["confidence"]),
        reason=cast(str, payload["reason"]),
        recorded_at=cast(str, payload["recorded_at"]),
        run_outcome_event_id=cast(str, payload["run_outcome_event_id"]),
    )
    if record.run_outcome_event_id != event.causation_id:
        _invalid("OutcomeAttributionProposed causation is invalid")
    if record.claim_strength == "association":
        record.to_attribution()
    elif record.claim_strength != "causal":
        _invalid("OutcomeAttributionProposed claim strength is invalid")
    return record


def parse_outcome_attribution_verified_event(
    event: CanonicalEvent,
    *,
    proposal_event: CanonicalEvent | None = None,
) -> OutcomeAttributionVerifiedRef:
    _verify_event_shape(
        event,
        OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
        stream_version=2,
    )
    payload = _payload(event)
    if set(payload) != {
        "contract_version",
        "attribution",
        "proposal_event_id",
    } or payload.get("contract_version") != OUTCOME_EVENT_CONTRACT_VERSION:
        _invalid("OutcomeAttributionVerified payload is invalid")
    attribution_payload = payload.get("attribution")
    if type(attribution_payload) is not dict:
        _invalid("OutcomeAttributionVerified attribution is invalid")
    try:
        attribution = parse_outcome_attribution(attribution_payload)
    except Exception as error:
        raise OutcomeEventV1Error(
            "TBM_OUTCOME_EVENT_INVALID",
            "OutcomeAttributionVerified attribution is invalid",
        ) from error
    record = OutcomeAttributionVerifiedRef(
        attribution=attribution,
        proposal_event_id=cast(str, payload["proposal_event_id"]),
    )
    if record.proposal_event_id != event.causation_id:
        _invalid("OutcomeAttributionVerified causation is invalid")
    if proposal_event is not None:
        proposal = parse_outcome_attribution_proposed_event(proposal_event)
        try:
            verify_event_parent(event, proposal_event)
        except Exception as error:
            raise OutcomeEventV1Error(
                "TBM_OUTCOME_EVENT_INVALID",
                "OutcomeAttributionVerified stream parent is invalid",
            ) from error
        if (
            proposal_event.event_id != record.proposal_event_id
            or proposal.attribution_id != attribution.attribution_id
            or proposal.run_outcome_id != attribution.run_outcome_id
            or proposal.usage_decision_id != attribution.usage_decision_id
            or proposal.memory_revision_ids != attribution.memory_revision_ids
            or proposal.claim_strength != attribution.claim_strength
            or proposal.effect != attribution.effect
            or proposal.method != attribution.method
            or proposal.evaluator_id != attribution.evaluator_id
            or proposal.evaluator_version != attribution.evaluator_version
            or proposal.evidence_artifact_sha256s
            != attribution.evidence_artifact_sha256s
            or proposal.confidence != attribution.confidence
            or proposal.reason != attribution.reason
            or proposal.recorded_at != attribution.recorded_at
            or not _same_scope(proposal_event, event)
        ):
            _invalid("OutcomeAttributionVerified proposal is inconsistent")
    return record


def run_outcome_event_stream_id(run_outcome_id: str) -> str:
    _identifier(run_outcome_id, "run_outcome_id")
    return "outcome_stream_sha256_" + _domain_sha256(
        b"tbm.outcome-stream.v1\x00",
        {"run_outcome_id": run_outcome_id},
    ).removeprefix("sha256:")


def outcome_attribution_event_stream_id(attribution_id: str) -> str:
    _identifier(attribution_id, "attribution_id")
    return "attribution_stream_sha256_" + _domain_sha256(
        b"tbm.outcome-attribution-stream.v1\x00",
        {"attribution_id": attribution_id},
    ).removeprefix("sha256:")


def outcome_event_payload_schema(event_type: str) -> dict[str, object]:
    if event_type not in OUTCOME_EVENT_TYPES:
        _invalid("event_type is not an outcome event")
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": _IDENTIFIER_RE.pattern,
    }
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    timestamp = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }
    string_array = {
        "type": "array",
        "items": identifier,
        "maxItems": 64,
        "uniqueItems": True,
    }
    outcome_schema = _run_outcome_schema(identifier, digest, timestamp)
    attribution_schema = _attribution_schema(identifier, digest, timestamp)
    if event_type == EVALUATION_AUTHENTICATED_EVENT:
        properties: dict[str, object] = {
            "contract_version": {"const": OUTCOME_EVENT_CONTRACT_VERSION},
            "session_id": identifier,
            "trace_id": identifier,
            "run_id": identifier,
            "usage_decision_id": identifier,
            "run_outcome_id": identifier,
            "evaluator": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "evaluator_id",
                    "evaluator_version",
                    "authenticator_id",
                    "credential_id",
                ],
                "properties": {
                    "evaluator_id": identifier,
                    "evaluator_version": identifier,
                    "authenticator_id": identifier,
                    "credential_id": identifier,
                },
            },
            "execution_event_id": identifier,
            "transition_authorization_event_id": identifier,
        }
    elif event_type == RUN_OUTCOME_RECORDED_EVENT:
        properties = {
            "contract_version": {"const": OUTCOME_EVENT_CONTRACT_VERSION},
            "outcome": outcome_schema,
            "completed_session_sha256": digest,
            "final_memory_revision_ids": string_array,
            "evaluation_event_id": identifier,
        }
    elif event_type == OUTCOME_ATTRIBUTION_PROPOSED_EVENT:
        properties = {
            "contract_version": {"const": OUTCOME_EVENT_CONTRACT_VERSION},
            "attribution_id": identifier,
            "run_outcome_id": identifier,
            "usage_decision_id": identifier,
            "memory_revision_ids": string_array,
            "claim_strength": {"enum": ["association", "causal"]},
            "effect": {"enum": ["helped", "harmed", "neutral", "unknown"]},
            "method": {
                "enum": [
                    "runtime_observation",
                    "controlled_experiment",
                    "manual_review",
                    "external_evaluation",
                ]
            },
            "evaluator_id": identifier,
            "evaluator_version": identifier,
            "evidence_artifact_sha256s": {
                "type": "array",
                "items": digest,
                "minItems": 1,
                "maxItems": 64,
                "uniqueItems": True,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
            },
            "recorded_at": timestamp,
            "run_outcome_event_id": identifier,
        }
    else:
        properties = {
            "contract_version": {"const": OUTCOME_EVENT_CONTRACT_VERSION},
            "attribution": attribution_schema,
            "proposal_event_id": identifier,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _run_outcome_schema(
    identifier: Mapping[str, object],
    digest: Mapping[str, object],
    timestamp: Mapping[str, object],
) -> dict[str, object]:
    optional_digest = {"oneOf": [{"type": "null"}, digest]}
    optional_integer = {
        "oneOf": [
            {"type": "null"},
            {"type": "integer", "minimum": 0},
        ]
    }
    optional_number = {
        "oneOf": [
            {"type": "null"},
            {"type": "number", "minimum": 0},
        ]
    }
    optional_identifier = {"oneOf": [{"type": "null"}, identifier]}
    properties: dict[str, object] = {
        "contract_version": {"const": RUN_OUTCOME_CONTRACT_VERSION},
        "run_outcome_id": identifier,
        "session_id": identifier,
        "trace_id": identifier,
        "run_id": identifier,
        "usage_decision_id": identifier,
        "result": {"enum": ["pass", "fail", "error"]},
        "evaluator_id": identifier,
        "evaluator_version": identifier,
        "output_sha256": optional_digest,
        "tool_outputs_sha256": optional_digest,
        "evidence_artifact_sha256s": {
            "type": "array",
            "items": digest,
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
        },
        "latency_ms": optional_integer,
        "cost_usd": optional_number,
        "error_code": optional_identifier,
        "measured_at": timestamp,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _attribution_schema(
    identifier: Mapping[str, object],
    digest: Mapping[str, object],
    timestamp: Mapping[str, object],
) -> dict[str, object]:
    optional_identifier = {"oneOf": [{"type": "null"}, identifier]}
    properties: dict[str, object] = {
        "contract_version": {"const": OUTCOME_ATTRIBUTION_CONTRACT_VERSION},
        "attribution_id": identifier,
        "run_outcome_id": identifier,
        "usage_decision_id": identifier,
        "memory_revision_ids": {
            "type": "array",
            "items": identifier,
            "maxItems": 50,
            "uniqueItems": True,
        },
        "claim_strength": {"enum": ["association", "causal"]},
        "effect": {"enum": ["helped", "harmed", "neutral", "unknown"]},
        "method": {
            "enum": [
                "runtime_observation",
                "controlled_experiment",
                "manual_review",
                "external_evaluation",
            ]
        },
        "evaluator_id": identifier,
        "evaluator_version": identifier,
        "verifier_id": optional_identifier,
        "evidence_artifact_sha256s": {
            "type": "array",
            "items": digest,
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1024},
        "recorded_at": timestamp,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _verify_completion_inputs(
    outcome: RunOutcome,
    executing_session: GateSession,
    completed_session: GateSession,
    execution_event: CanonicalEvent,
    evaluator_context: OutcomeEvaluatorEventContext,
    trusted_context: EventTrustedContext,
) -> None:
    if type(outcome) is not RunOutcome:
        _invalid("outcome must be exactly RunOutcome")
    if (
        type(executing_session) is not GateSession
        or type(completed_session) is not GateSession
    ):
        _invalid("completion sessions must be exactly GateSession")
    if type(evaluator_context) is not OutcomeEvaluatorEventContext:
        _invalid("evaluator_context must be exactly OutcomeEvaluatorEventContext")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if (
        executing_session.status != "executing"
        or completed_session.status != "completed"
        or completed_session.version != executing_session.version + 1
        or completed_session.session_id != executing_session.session_id
        or evaluator_context.evaluator_id != outcome.evaluator_id
        or evaluator_context.evaluator_version != outcome.evaluator_version
    ):
        _invalid("outcome completion inputs are inconsistent")
    try:
        verify_run_outcome(outcome, completed_session)
    except Exception as error:
        raise OutcomeEventV1Error(
            "TBM_OUTCOME_EVENT_INVALID",
            "RunOutcome does not match completed GateSession",
        ) from error
    retained_execution = gate_session_from_event(execution_event)
    if (
        execution_event.event_type
        not in {EXECUTION_STARTED_EVENT, GATE_SESSION_LEASE_RENEWED_EVENT}
        or retained_execution != executing_session
        or execution_event.event_id != gate_session_event_id(executing_session)
    ):
        _invalid("execution event is not the exact current GateSession head")
    _verify_scope(execution_event, trusted_context, require_authorization=False)


def gate_session_from_event(event: CanonicalEvent) -> GateSession:
    if (
        type(event) is not CanonicalEvent
        or event.event_type not in GATE_SESSION_EVENT_TYPES
        or event.payload_schema != f"{event.event_type}.v1"
        or event.artifact_refs
    ):
        _invalid("event is not a canonical GateSession event")
    payload = _payload(event)
    if payload.get("contract_version") != GATE_SESSION_EVENT_CONTRACT_VERSION:
        _invalid("GateSession event contract is invalid")
    session_payload = payload.get("session")
    if type(session_payload) is not dict:
        _invalid("GateSession event session is invalid")
    try:
        session = parse_gate_session(session_payload)
    except Exception as error:
        raise OutcomeEventV1Error(
            "TBM_OUTCOME_EVENT_INVALID",
            "GateSession event payload is invalid",
        ) from error
    if (
        payload.get("session_sha256") != gate_session_revision_sha256(session)
        or event.event_id != gate_session_event_id(session)
        or event.stream_id != session.session_id
        or event.stream_version != session.version
    ):
        _invalid("GateSession event identity is invalid")
    return session


def _verify_event_shape(
    event: CanonicalEvent,
    event_type: str,
    *,
    stream_version: int,
) -> None:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if (
        event.event_type != event_type
        or event.event_version != OUTCOME_EVENT_VERSION
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.stream_version != stream_version
        or event.payload_schema != f"{event_type}.v1"
        or event.classification != "internal"
        or event.retention_policy_id != OUTCOME_EVENT_RETENTION_POLICY_ID
        or event.artifact_refs
    ):
        _invalid("outcome event envelope is invalid")


def _verify_scope(
    event: CanonicalEvent,
    trusted_context: EventTrustedContext,
    *,
    require_authorization: bool,
) -> None:
    expected = (
        trusted_context.organization_id,
        trusted_context.tenant_id,
        trusted_context.repository_id,
        trusted_context.environment_id,
        trusted_context.principal_id,
        trusted_context.agent_client_id,
        trusted_context.actor_type,
        trusted_context.actor_id,
    )
    actual = (
        event.organization_id,
        event.tenant_id,
        event.repository_id,
        event.environment_id,
        event.principal_id,
        event.agent_client_id,
        event.actor_type,
        event.actor_id,
    )
    if actual != expected or (
        require_authorization
        and event.authorization_decision_id
        != trusted_context.authorization_decision_id
    ):
        _invalid("event is outside the trusted outcome scope")


def _same_scope(left: CanonicalEvent, right: CanonicalEvent) -> bool:
    fields = (
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "principal_id",
        "agent_client_id",
        "actor_type",
        "actor_id",
        "authorization_decision_id",
    )
    return all(getattr(left, name) == getattr(right, name) for name in fields)


def _payload(event: CanonicalEvent) -> dict[str, object]:
    try:
        payload = event.to_dict()["payload"]
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise OutcomeEventV1Error(
            "TBM_OUTCOME_EVENT_INVALID",
            "outcome event payload is not canonical JSON",
        ) from error
    if type(payload) is not dict:
        _invalid("outcome event payload must be an object")
    return cast(dict[str, object], payload)


def _outcome_event_id(stage: str, run_outcome_id: str) -> str:
    return "evt_outcome_" + _domain_sha256(
        b"tbm.outcome-event-identity.v1\x00",
        {"stage": stage, "run_outcome_id": run_outcome_id},
    ).removeprefix("sha256:")


def _attribution_event_id(stage: str, attribution_id: str) -> str:
    return "evt_attribution_" + _domain_sha256(
        b"tbm.outcome-attribution-event-identity.v1\x00",
        {"stage": stage, "attribution_id": attribution_id},
    ).removeprefix("sha256:")


def _outcome_request_id(run_outcome_id: str) -> str:
    return "outcome_request_" + _domain_sha256(
        b"tbm.outcome-request.v1\x00",
        {"run_outcome_id": run_outcome_id},
    ).removeprefix("sha256:")[:47]


def _attribution_request_id(attribution_id: str) -> str:
    return "attribution_request_" + _domain_sha256(
        b"tbm.outcome-attribution-request.v1\x00",
        {"attribution_id": attribution_id},
    ).removeprefix("sha256:")[:43]


def _outcome_correlation_id(session_id: str) -> str:
    return "outcome_correlation_" + _domain_sha256(
        b"tbm.outcome-correlation.v1\x00",
        {"session_id": session_id},
    ).removeprefix("sha256:")[:43]


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a bounded identifier")
    return value


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a SHA-256 digest")
    return value


def _domain_sha256(domain: bytes, value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise OutcomeEventV1Error(
            "TBM_OUTCOME_EVENT_INVALID",
            "outcome event identity input is not canonical JSON",
        ) from error
    return "sha256:" + hashlib.sha256(domain + payload).hexdigest()


def _invalid(message: str) -> NoReturn:
    raise OutcomeEventV1Error("TBM_OUTCOME_EVENT_INVALID", message)


__all__ = [
    "EVALUATION_AUTHENTICATED_EVENT",
    "OUTCOME_ATTRIBUTION_PROPOSED_EVENT",
    "OUTCOME_ATTRIBUTION_VERIFIED_EVENT",
    "OUTCOME_EVENT_CONTRACT_VERSION",
    "OUTCOME_EVENT_TYPES",
    "OUTCOME_EVENT_VERSION",
    "RUN_OUTCOME_RECORDED_EVENT",
    "EvaluationAuthenticatedRef",
    "OutcomeAttributionProposalRef",
    "OutcomeAttributionVerifiedRef",
    "OutcomeEvaluatorEventContext",
    "OutcomeEventV1Error",
    "RunOutcomeRecordedRef",
    "build_outcome_attribution_event_batch",
    "build_run_outcome_event_batch",
    "gate_session_from_event",
    "outcome_attribution_event_stream_id",
    "outcome_event_payload_schema",
    "parse_evaluation_authenticated_event",
    "parse_outcome_attribution_proposed_event",
    "parse_outcome_attribution_verified_event",
    "parse_run_outcome_recorded_event",
    "run_outcome_event_stream_id",
]
