from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import NoReturn, cast

from .event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from .event_v1 import CanonicalEvent
from .gate_session_v3 import GateSession
from .outcome_event_v1 import (
    EVALUATION_AUTHENTICATED_EVENT,
    OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
    OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
    RUN_OUTCOME_RECORDED_EVENT,
    EvaluationAuthenticatedRef,
    OutcomeAttributionProposalRef,
    OutcomeEvaluatorEventContext,
    OutcomeEventV1Error,
    RunOutcomeRecordedRef,
    parse_evaluation_authenticated_event,
    parse_outcome_attribution_proposed_event,
    parse_outcome_attribution_verified_event,
    parse_run_outcome_recorded_event,
)
from .outcome_v3 import (
    OutcomeAttribution,
    RunOutcome,
    parse_outcome_attribution,
    parse_run_outcome,
    verify_run_outcome,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerExecutionError,
)


OUTCOME_CURRENT_REDUCER_ID = "outcome-current"
OUTCOME_CURRENT_PROJECTION_NAME = "outcome_current_v1"
OUTCOME_CURRENT_PROJECTION_SCHEMA_VERSION = 1
OUTCOME_ATTRIBUTION_REDUCER_ID = "outcome-attribution"
OUTCOME_ATTRIBUTION_PROJECTION_NAME = "outcome_attribution_v1"
OUTCOME_ATTRIBUTION_PROJECTION_SCHEMA_VERSION = 1

_OUTCOME_CURRENT_EVENT_TYPES = tuple(
    sorted((EVALUATION_AUTHENTICATED_EVENT, RUN_OUTCOME_RECORDED_EVENT))
)
_OUTCOME_ATTRIBUTION_EVENT_TYPES = tuple(
    sorted(
        (
            RUN_OUTCOME_RECORDED_EVENT,
            OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
            OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
        )
    )
)
_EVENT_SCOPE_FIELDS = (
    "organization_id",
    "tenant_id",
    "repository_id",
    "environment_id",
    "principal_id",
    "agent_client_id",
    "actor_type",
    "actor_id",
)


@dataclass(frozen=True)
class OutcomeProjectionAuthority:
    outcome: RunOutcome
    completed_session: GateSession
    evaluator: OutcomeEvaluatorEventContext

    def __post_init__(self) -> None:
        if type(self.outcome) is not RunOutcome:
            _reject("outcome must be exactly RunOutcome")
        if type(self.completed_session) is not GateSession:
            _reject("completed_session must be exactly GateSession")
        if type(self.evaluator) is not OutcomeEvaluatorEventContext:
            _reject("evaluator must be exactly OutcomeEvaluatorEventContext")
        try:
            verify_run_outcome(self.outcome, self.completed_session)
        except Exception as error:
            raise ReducerExecutionError(
                "TBM_OUTCOME_REDUCER_AUTHORITY_INVALID",
                "outcome authority input is inconsistent",
            ) from error
        if (
            self.evaluator.evaluator_id != self.outcome.evaluator_id
            or self.evaluator.evaluator_version
            != self.outcome.evaluator_version
        ):
            _reject("outcome evaluator authority input is inconsistent")


def build_outcome_current_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=OUTCOME_CURRENT_REDUCER_ID,
        reducer_version=1,
        input_event_types=_OUTCOME_CURRENT_EVENT_TYPES,
        output_projection=OUTCOME_CURRENT_PROJECTION_NAME,
        output_schema_version=OUTCOME_CURRENT_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "outcome-current",
                "algorithm_version": 1,
                "validation": [
                    "typed-payload",
                    "evaluation-before-outcome",
                    "trusted-evaluator-provenance",
                    "exact-outcome-linkage",
                    "single-outcome-stream",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={
            event_type: 1 for event_type in _OUTCOME_CURRENT_EVENT_TYPES
        },
    )

    def initial() -> Mapping[str, object]:
        return {"evaluations": {}, "outcomes": {}, "heads": {}}

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        _validate_typed(reducer_event, "outcome")
        source = reducer_event.source_event
        evaluations = _mapping_copy(state.get("evaluations"), "evaluations")
        outcomes = _mapping_copy(state.get("outcomes"), "outcomes")
        heads = _mapping_copy(state.get("heads"), "heads")

        if source.event_type == EVALUATION_AUTHENTICATED_EVENT:
            evaluation = _parse_evaluation(source)
            if (
                evaluation.run_outcome_id in evaluations
                or evaluation.run_outcome_id in outcomes
                or source.stream_id in heads
            ):
                _reject("EvaluationAuthenticated appears more than once")
            evaluations[evaluation.run_outcome_id] = {
                "evaluation": evaluation.to_dict(),
                **_event_metadata(source),
            }
            heads[source.stream_id] = _head(source)
        elif source.event_type == RUN_OUTCOME_RECORDED_EVENT:
            record = _parse_outcome(source)
            outcome_id = record.outcome.run_outcome_id
            evaluation_projection = _mapping_copy(
                evaluations.get(outcome_id),
                "EvaluationAuthenticated parent",
            )
            evaluation_payload = _mapping_copy(
                evaluation_projection.get("evaluation"),
                "EvaluationAuthenticated payload",
            )
            head = _mapping_copy(
                heads.get(source.stream_id),
                "outcome stream head",
            )
            if (
                outcome_id in outcomes
                or not _head_extends(head, source)
                or evaluation_projection.get("event_id")
                != record.evaluation_event_id
                or not _projection_scope_matches_event(
                    evaluation_projection,
                    source,
                    include_authorization=True,
                )
                or not _evaluation_matches_outcome(
                    evaluation_payload,
                    record,
                )
            ):
                _reject("RunOutcomeRecorded has no exact evaluation parent")
            outcomes[outcome_id] = {
                "record": record.to_dict(),
                **_event_metadata(source),
            }
            heads[source.stream_id] = _head(source)
        else:
            _reject("outcome projection received an unrelated event")
        return {
            "evaluations": evaluations,
            "outcomes": outcomes,
            "heads": heads,
        }

    return FunctionalReducer(descriptor, initial, transition)


def build_outcome_attribution_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=OUTCOME_ATTRIBUTION_REDUCER_ID,
        reducer_version=1,
        input_event_types=_OUTCOME_ATTRIBUTION_EVENT_TYPES,
        output_projection=OUTCOME_ATTRIBUTION_PROJECTION_NAME,
        output_schema_version=OUTCOME_ATTRIBUTION_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "outcome-attribution",
                "algorithm_version": 1,
                "validation": [
                    "typed-payload",
                    "recorded-outcome-parent",
                    "association-proposal-only",
                    "causal-independent-verification",
                    "linear-attribution-stream",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={
            event_type: 1 for event_type in _OUTCOME_ATTRIBUTION_EVENT_TYPES
        },
    )

    def initial() -> Mapping[str, object]:
        return {
            "outcome_events": {},
            "proposals": {},
            "attributions": {},
            "heads": {},
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        _validate_typed(reducer_event, "outcome attribution")
        source = reducer_event.source_event
        outcome_events = _mapping_copy(
            state.get("outcome_events"),
            "outcome_events",
        )
        proposals = _mapping_copy(state.get("proposals"), "proposals")
        attributions = _mapping_copy(
            state.get("attributions"),
            "attributions",
        )
        heads = _mapping_copy(state.get("heads"), "heads")

        if source.event_type == RUN_OUTCOME_RECORDED_EVENT:
            record = _parse_outcome(source)
            outcome_id = record.outcome.run_outcome_id
            if outcome_id in outcome_events:
                _reject("RunOutcomeRecorded appears more than once")
            outcome_events[outcome_id] = {
                "record": record.to_dict(),
                **_event_metadata(source),
            }
        elif source.event_type == OUTCOME_ATTRIBUTION_PROPOSED_EVENT:
            proposal = _parse_proposal(source)
            outcome_projection = _mapping_copy(
                outcome_events.get(proposal.run_outcome_id),
                "RunOutcomeRecorded parent",
            )
            outcome_record = _mapping_copy(
                outcome_projection.get("record"),
                "RunOutcomeRecorded record",
            )
            outcome_payload = _mapping_copy(
                outcome_record.get("outcome"),
                "RunOutcomeRecorded outcome",
            )
            if (
                proposal.attribution_id in proposals
                or proposal.attribution_id in attributions
                or source.stream_id in heads
                or outcome_projection.get("event_id")
                != proposal.run_outcome_event_id
                or outcome_projection.get("global_position")
                is None
                or type(outcome_projection["global_position"]) is not int
                or cast(int, outcome_projection["global_position"])
                >= source.global_position
                or source.causation_id != proposal.run_outcome_event_id
                or outcome_payload.get("run_outcome_id")
                != proposal.run_outcome_id
                or outcome_payload.get("usage_decision_id")
                != proposal.usage_decision_id
                or not _projection_scope_matches_event(
                    outcome_projection,
                    source,
                    include_authorization=False,
                )
            ):
                _reject("OutcomeAttributionProposed has no exact outcome parent")
            proposals[proposal.attribution_id] = {
                "proposal": proposal.to_dict(),
                **_event_metadata(source),
            }
            if proposal.claim_strength == "association":
                attributions[proposal.attribution_id] = {
                    "attribution": proposal.to_attribution().to_dict(),
                    "proposal_event_id": source.event_id,
                    "verification_event_id": None,
                    **_event_metadata(source),
                }
            heads[source.stream_id] = _head(source)
        elif source.event_type == OUTCOME_ATTRIBUTION_VERIFIED_EVENT:
            verified = _parse_verified(source)
            attribution = verified.attribution
            proposal_projection = _mapping_copy(
                proposals.get(attribution.attribution_id),
                "OutcomeAttributionProposed parent",
            )
            proposal_payload = _mapping_copy(
                proposal_projection.get("proposal"),
                "OutcomeAttributionProposed payload",
            )
            head = _mapping_copy(
                heads.get(source.stream_id),
                "outcome attribution stream head",
            )
            if (
                attribution.attribution_id in attributions
                or not _head_extends(head, source)
                or proposal_projection.get("event_id")
                != verified.proposal_event_id
                or not _projection_scope_matches_event(
                    proposal_projection,
                    source,
                    include_authorization=True,
                )
                or not _proposal_matches_attribution(
                    proposal_payload,
                    attribution,
                )
            ):
                _reject("OutcomeAttributionVerified has no exact proposal parent")
            attributions[attribution.attribution_id] = {
                "attribution": attribution.to_dict(),
                "proposal_event_id": verified.proposal_event_id,
                "verification_event_id": source.event_id,
                **_event_metadata(source),
            }
            heads[source.stream_id] = _head(source)
        else:
            _reject("outcome attribution projection received an unrelated event")
        return {
            "outcome_events": outcome_events,
            "proposals": proposals,
            "attributions": attributions,
            "heads": heads,
        }

    return FunctionalReducer(descriptor, initial, transition)


def projected_run_outcome(
    state: Mapping[str, object],
    run_outcome_id: str,
) -> RunOutcome:
    outcomes = state.get("outcomes")
    if not isinstance(outcomes, Mapping) or run_outcome_id not in outcomes:
        _reject("RunOutcome is absent from projection state")
    projection = _mapping_copy(outcomes[run_outcome_id], "RunOutcome projection")
    record = _mapping_copy(projection.get("record"), "RunOutcome record")
    payload = _mapping_copy(record.get("outcome"), "RunOutcome payload")
    try:
        return parse_run_outcome(payload)
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_OUTCOME_REDUCER_STATE_INVALID",
            "projected RunOutcome payload is invalid",
        ) from error


def projected_outcome_attribution(
    state: Mapping[str, object],
    attribution_id: str,
) -> OutcomeAttribution:
    attributions = state.get("attributions")
    if not isinstance(attributions, Mapping) or attribution_id not in attributions:
        _reject("OutcomeAttribution is absent from projection state")
    projection = _mapping_copy(
        attributions[attribution_id],
        "OutcomeAttribution projection",
    )
    payload = _mapping_copy(
        projection.get("attribution"),
        "OutcomeAttribution payload",
    )
    try:
        return parse_outcome_attribution(payload)
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_OUTCOME_REDUCER_STATE_INVALID",
            "projected OutcomeAttribution payload is invalid",
        ) from error


def verify_outcome_projection_parity(
    state: Mapping[str, object],
    authorities: tuple[OutcomeProjectionAuthority, ...],
    events: tuple[CanonicalEvent, ...],
) -> None:
    if type(authorities) is not tuple or any(
        type(item) is not OutcomeProjectionAuthority for item in authorities
    ):
        _reject("outcome parity authority input is invalid")
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _reject("outcome parity event input is invalid")
    expected = {item.outcome.run_outcome_id: item for item in authorities}
    if len(expected) != len(authorities):
        _reject("outcome parity authority input has duplicates")
    evaluations: dict[str, CanonicalEvent] = {}
    recorded: dict[str, CanonicalEvent] = {}
    for event in events:
        if event.event_type == EVALUATION_AUTHENTICATED_EVENT:
            reference = _parse_evaluation(event)
            if reference.run_outcome_id in evaluations:
                _reject("outcome parity has duplicate evaluation events")
            evaluations[reference.run_outcome_id] = event
        elif event.event_type == RUN_OUTCOME_RECORDED_EVENT:
            reference = _parse_outcome(event)
            outcome_id = reference.outcome.run_outcome_id
            if outcome_id in recorded:
                _reject("outcome parity has duplicate outcome events")
            recorded[outcome_id] = event
        else:
            _reject("outcome parity contains an unrelated event")
    if set(evaluations) != set(expected) or set(recorded) != set(expected):
        _reject("outcome event identities differ from authority rows")
    for outcome_id, authority in expected.items():
        evaluation_event = evaluations[outcome_id]
        evaluation = _parse_evaluation(evaluation_event)
        record = parse_run_outcome_recorded_event(
            recorded[outcome_id],
            evaluation_event=evaluation_event,
            completed_session=authority.completed_session,
        )
        if (
            evaluation.evaluator != authority.evaluator
            or record.outcome != authority.outcome
            or projected_run_outcome(state, outcome_id) != authority.outcome
        ):
            _reject("outcome projection differs from exact authority rows")
    expected_state = _reduce_events(build_outcome_current_reducer(), events)
    if _plain_json(state) != _plain_json(expected_state):
        _reject("outcome projection differs from events and authority rows")


def verify_outcome_attribution_projection_parity(
    state: Mapping[str, object],
    outcomes: tuple[RunOutcome, ...],
    attributions: tuple[OutcomeAttribution, ...],
    events: tuple[CanonicalEvent, ...],
) -> None:
    if type(outcomes) is not tuple or any(
        type(outcome) is not RunOutcome for outcome in outcomes
    ):
        _reject("outcome attribution parity outcome input is invalid")
    if type(attributions) is not tuple or any(
        type(attribution) is not OutcomeAttribution
        for attribution in attributions
    ):
        _reject("outcome attribution parity authority input is invalid")
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _reject("outcome attribution parity event input is invalid")
    expected_outcomes = {item.run_outcome_id: item for item in outcomes}
    expected_attributions = {
        item.attribution_id: item for item in attributions
    }
    if len(expected_outcomes) != len(outcomes) or len(expected_attributions) != len(
        attributions
    ):
        _reject("outcome attribution parity input has duplicates")
    recorded_outcomes: dict[str, RunOutcome] = {}
    proposal_ids: set[str] = set()
    verified_ids: set[str] = set()
    for event in events:
        if event.event_type == RUN_OUTCOME_RECORDED_EVENT:
            outcome = _parse_outcome(event).outcome
            if outcome.run_outcome_id in recorded_outcomes:
                _reject("attribution parity has duplicate outcome events")
            recorded_outcomes[outcome.run_outcome_id] = outcome
        elif event.event_type == OUTCOME_ATTRIBUTION_PROPOSED_EVENT:
            proposal = _parse_proposal(event)
            if proposal.attribution_id in proposal_ids:
                _reject("attribution parity has duplicate proposal events")
            proposal_ids.add(proposal.attribution_id)
        elif event.event_type == OUTCOME_ATTRIBUTION_VERIFIED_EVENT:
            attribution = _parse_verified(event).attribution
            if attribution.attribution_id in verified_ids:
                _reject("attribution parity has duplicate verified events")
            verified_ids.add(attribution.attribution_id)
        else:
            _reject("outcome attribution parity contains an unrelated event")
    if recorded_outcomes != expected_outcomes:
        _reject("attribution outcome events differ from authority rows")
    if proposal_ids != set(expected_attributions):
        _reject("attribution proposal identities differ from authority rows")
    expected_verified = {
        attribution.attribution_id
        for attribution in attributions
        if attribution.claim_strength == "causal"
    }
    if verified_ids != expected_verified:
        _reject("causal verification identities differ from authority rows")
    for attribution_id, authority in expected_attributions.items():
        if projected_outcome_attribution(state, attribution_id) != authority:
            _reject("OutcomeAttribution projection differs from authority row")
    expected_state = _reduce_events(build_outcome_attribution_reducer(), events)
    if _plain_json(state) != _plain_json(expected_state):
        _reject("attribution projection differs from events and authority rows")


def _reduce_events(
    reducer: FunctionalReducer,
    events: tuple[CanonicalEvent, ...],
) -> Mapping[str, object]:
    ordered = tuple(sorted(events, key=lambda event: event.global_position))
    if len({event.global_position for event in ordered}) != len(ordered):
        _reject("projection parity events have duplicate global positions")
    state = reducer.initial_state()
    for event in ordered:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def _validate_typed(reducer_event: ReducerEvent, description: str) -> None:
    typed = reducer_event.typed_event
    source = reducer_event.source_event
    if typed is None:
        _reject(f"typed {description} event is required")
    if (
        typed.target_version != 1
        or typed.event_type != source.event_type
        or _plain_json(typed.payload) != _plain_json(source.payload)
    ):
        _reject(f"typed {description} event is invalid")


def _parse_evaluation(event: CanonicalEvent) -> EvaluationAuthenticatedRef:
    try:
        return parse_evaluation_authenticated_event(event)
    except OutcomeEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_OUTCOME_REDUCER_EVENT_INVALID",
            "EvaluationAuthenticated cannot update the projection",
        ) from error


def _parse_outcome(event: CanonicalEvent) -> RunOutcomeRecordedRef:
    try:
        return parse_run_outcome_recorded_event(event)
    except OutcomeEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_OUTCOME_REDUCER_EVENT_INVALID",
            "RunOutcomeRecorded cannot update the projection",
        ) from error


def _parse_proposal(event: CanonicalEvent) -> OutcomeAttributionProposalRef:
    try:
        return parse_outcome_attribution_proposed_event(event)
    except OutcomeEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_OUTCOME_REDUCER_EVENT_INVALID",
            "OutcomeAttributionProposed cannot update the projection",
        ) from error


def _parse_verified(event: CanonicalEvent):
    try:
        return parse_outcome_attribution_verified_event(event)
    except OutcomeEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_OUTCOME_REDUCER_EVENT_INVALID",
            "OutcomeAttributionVerified cannot update the projection",
        ) from error


def _evaluation_matches_outcome(
    evaluation: Mapping[str, object],
    record: RunOutcomeRecordedRef,
) -> bool:
    evaluator = evaluation.get("evaluator")
    outcome = record.outcome
    return (
        isinstance(evaluator, Mapping)
        and evaluation.get("run_outcome_id") == outcome.run_outcome_id
        and evaluation.get("session_id") == outcome.session_id
        and evaluation.get("trace_id") == outcome.trace_id
        and evaluation.get("run_id") == outcome.run_id
        and evaluation.get("usage_decision_id") == outcome.usage_decision_id
        and evaluator.get("evaluator_id") == outcome.evaluator_id
        and evaluator.get("evaluator_version") == outcome.evaluator_version
    )


def _proposal_matches_attribution(
    proposal: Mapping[str, object],
    attribution: OutcomeAttribution,
) -> bool:
    expected = attribution.to_dict()
    expected.pop("verifier_id")
    actual = dict(proposal)
    actual.pop("contract_version", None)
    actual.pop("run_outcome_event_id", None)
    expected.pop("contract_version", None)
    return _plain_json(actual) == _plain_json(expected)


def _head_extends(head: Mapping[str, object], event: CanonicalEvent) -> bool:
    return (
        head.get("stream_version") == event.stream_version - 1
        and head.get("event_id") == event.causation_id
        and head.get("event_sha256") == event.previous_stream_event_sha256
        and type(head.get("global_position")) is int
        and cast(int, head["global_position"]) < event.global_position
        and _projection_scope_matches_event(
            head,
            event,
            include_authorization=True,
        )
    )


def _head(event: CanonicalEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_sha256": event.event_sha256,
        "global_position": event.global_position,
        "stream_version": event.stream_version,
        "authorization_decision_id": event.authorization_decision_id,
        **_event_scope(event),
    }


def _event_metadata(event: CanonicalEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_sha256": event.event_sha256,
        "global_position": event.global_position,
        "stream_id": event.stream_id,
        "stream_version": event.stream_version,
        "authorization_decision_id": event.authorization_decision_id,
        **_event_scope(event),
    }


def _event_scope(event: CanonicalEvent) -> dict[str, object]:
    return {name: getattr(event, name) for name in _EVENT_SCOPE_FIELDS}


def _projection_scope_matches_event(
    projection: Mapping[str, object],
    event: CanonicalEvent,
    *,
    include_authorization: bool,
) -> bool:
    if any(
        projection.get(name) != getattr(event, name)
        for name in _EVENT_SCOPE_FIELDS
    ):
        return False
    return not include_authorization or (
        projection.get("authorization_decision_id")
        == event.authorization_decision_id
    )


def _mapping_copy(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject(f"{name} projection state is invalid")
    copied = _plain_json(value)
    if type(copied) is not dict:
        _reject(f"{name} projection state is invalid")
    return cast(dict[str, object], copied)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(
        domain + _canonical_json_bytes(value)
    ).hexdigest()


def _reject(message: str) -> NoReturn:
    raise ReducerExecutionError(
        "TBM_OUTCOME_REDUCER_EVENT_INVALID",
        message,
    )


__all__ = [
    "OUTCOME_ATTRIBUTION_PROJECTION_NAME",
    "OUTCOME_ATTRIBUTION_PROJECTION_SCHEMA_VERSION",
    "OUTCOME_ATTRIBUTION_REDUCER_ID",
    "OUTCOME_CURRENT_PROJECTION_NAME",
    "OUTCOME_CURRENT_PROJECTION_SCHEMA_VERSION",
    "OUTCOME_CURRENT_REDUCER_ID",
    "OutcomeProjectionAuthority",
    "build_outcome_attribution_reducer",
    "build_outcome_current_reducer",
    "projected_outcome_attribution",
    "projected_run_outcome",
    "verify_outcome_attribution_projection_parity",
    "verify_outcome_projection_parity",
]
