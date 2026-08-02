from __future__ import annotations

from dataclasses import replace

import pytest

from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.gate_session_event_v1 import (
    GATE_SESSION_COMPLETED_EVENT,
    build_gate_session_event,
)
from trace_backed_memory.gate_session_v3 import (
    GateSession,
    create_gate_session,
    transition_gate_session,
)
from trace_backed_memory.outcome_event_v1 import (
    EVALUATION_AUTHENTICATED_EVENT,
    OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
    OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
    RUN_OUTCOME_RECORDED_EVENT,
    OutcomeEvaluatorEventContext,
    OutcomeEventV1Error,
    build_outcome_attribution_event_batch,
    build_run_outcome_event_batch,
    parse_evaluation_authenticated_event,
    parse_outcome_attribution_proposed_event,
    parse_outcome_attribution_verified_event,
    parse_run_outcome_recorded_event,
)
from trace_backed_memory.outcome_reducer_v1 import (
    OutcomeProjectionAuthority,
    build_outcome_attribution_reducer,
    build_outcome_current_reducer,
    projected_outcome_attribution,
    projected_run_outcome,
    verify_outcome_attribution_projection_parity,
    verify_outcome_projection_parity,
)
from trace_backed_memory.outcome_v3 import (
    OutcomeAttribution,
    RunOutcome,
    build_outcome_attribution,
    build_run_outcome,
)
from trace_backed_memory.reducer import ReducerEvent, ReducerExecutionError


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64


def _trusted(session: GateSession) -> EventTrustedContext:
    return EventTrustedContext(
        organization_id="organization_001",
        tenant_id=session.tenant_id,
        repository_id=session.repository_id,
        environment_id="environment_local",
        principal_id=session.principal_id,
        agent_client_id=session.agent_client_id,
        actor_type="agent_client",
        actor_id=session.agent_client_id,
        authorization_decision_id="authorization_decision_001",
    )


def _sessions() -> tuple[tuple[GateSession, ...], GateSession]:
    created = create_gate_session(
        session_id="gate_session_outcome_events_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=DIGEST_A,
        idempotency_key="request-001",
        created_at="2026-08-01T00:00:00Z",
        expires_at="2026-08-01T01:00:00Z",
    )
    prepared = transition_gate_session(
        created,
        "prepared",
        expected_version=1,
        updated_at="2026-08-01T00:01:00Z",
        lease_expires_at="2026-08-01T00:20:00Z",
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id="system_gate_001",
    )
    awaiting = transition_gate_session(
        prepared,
        "awaiting_decision",
        expected_version=2,
        updated_at="2026-08-01T00:02:00Z",
    )
    decided = transition_gate_session(
        awaiting,
        "decided",
        expected_version=3,
        updated_at="2026-08-01T00:03:00Z",
        semantic_gate_attempt_ids=("semantic_gate_attempt_001",),
        decision_id="decision_001",
    )
    finalized = transition_gate_session(
        decided,
        "finalized",
        expected_version=4,
        updated_at="2026-08-01T00:04:00Z",
        final_memory_revision_ids=(REVISION_A,),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_decision_001",
    )
    executing = transition_gate_session(
        finalized,
        "executing",
        expected_version=5,
        updated_at="2026-08-01T00:05:00Z",
    )
    outcome = _outcome(session=executing)
    completed = transition_gate_session(
        executing,
        "completed",
        expected_version=6,
        updated_at=outcome.measured_at,
        run_outcome_id=outcome.run_outcome_id,
    )
    return (
        (created, prepared, awaiting, decided, finalized, executing),
        completed,
    )


def _gate_events(sessions: tuple[GateSession, ...]) -> tuple[CanonicalEvent, ...]:
    events: list[CanonicalEvent] = []
    previous_session: GateSession | None = None
    parent_event: CanonicalEvent | None = None
    for global_position, session in enumerate(sessions, start=1):
        event = build_gate_session_event(
            session,
            previous_session=previous_session,
            parent_event=parent_event,
            global_position=global_position,
            trusted_context=_trusted(session),
        )
        events.append(event)
        previous_session = session
        parent_event = event
    return tuple(events)


def _outcome(*, session: GateSession) -> RunOutcome:
    return build_run_outcome(
        session_id=session.session_id,
        trace_id=session.trace_id,
        run_id=session.run_id,
        usage_decision_id="usage_decision_001",
        result="pass",
        evaluator_id="outcome_evaluator",
        evaluator_version="v1",
        output_sha256=DIGEST_A,
        evidence_artifact_sha256s=(DIGEST_B,),
        measured_at="2026-08-01T00:06:00Z",
    )


def _evaluator() -> OutcomeEvaluatorEventContext:
    return OutcomeEvaluatorEventContext(
        evaluator_id="outcome_evaluator",
        evaluator_version="v1",
        authenticator_id="mtls",
        credential_id="credential_outcome_evaluator",
    )


def _fixture() -> tuple[
    GateSession,
    GateSession,
    CanonicalEvent,
    RunOutcome,
    EventTrustedContext,
]:
    sessions, completed = _sessions()
    executing = sessions[-1]
    execution_event = _gate_events(sessions)[-1]
    return (
        executing,
        completed,
        execution_event,
        _outcome(session=executing),
        _trusted(executing),
    )


def _association(outcome: RunOutcome) -> OutcomeAttribution:
    return build_outcome_attribution(
        run_outcome_id_value=outcome.run_outcome_id,
        usage_decision_id=outcome.usage_decision_id,
        memory_revision_ids=(REVISION_A,),
        claim_strength="association",
        effect="unknown",
        method="runtime_observation",
        evaluator_id="outcome_observer",
        evaluator_version="v1",
        evidence_artifact_sha256s=(DIGEST_A,),
        confidence=0.5,
        reason="The revision was present in the completed run.",
        recorded_at="2026-08-01T00:07:00Z",
    )


def _causal(outcome: RunOutcome) -> OutcomeAttribution:
    return build_outcome_attribution(
        run_outcome_id_value=outcome.run_outcome_id,
        usage_decision_id=outcome.usage_decision_id,
        memory_revision_ids=(REVISION_A,),
        claim_strength="causal",
        effect="helped",
        method="controlled_experiment",
        evaluator_id="experiment_evaluator",
        evaluator_version="v2",
        verifier_id="independent_verifier",
        evidence_artifact_sha256s=(DIGEST_A, DIGEST_B),
        confidence=0.9,
        reason="The controlled cohort showed a verified improvement.",
        recorded_at="2026-08-01T00:07:00Z",
    )


def _clone_event(
    event: CanonicalEvent,
    *,
    trusted_context: EventTrustedContext | None = None,
    **overrides: object,
) -> CanonicalEvent:
    values: dict[str, object] = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_version": event.event_version,
        "event_kind": event.event_kind,
        "origin": event.origin,
        "source": event.source,
        "stream_id": event.stream_id,
        "stream_type": event.stream_type,
        "stream_version": event.stream_version,
        "global_position": event.global_position,
        "request_id": event.request_id,
        "idempotency_key_sha256": event.idempotency_key_sha256,
        "request_sha256": event.request_sha256,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "occurred_at": event.occurred_at,
        "recorded_at": event.recorded_at,
        "producer": event.producer,
        "producer_version": event.producer_version,
        "payload_schema": event.payload_schema,
        "previous_stream_event_sha256": event.previous_stream_event_sha256,
        "classification": event.classification,
        "retention_policy_id": event.retention_policy_id,
        "artifact_refs": event.artifact_refs,
        "payload": event.to_dict()["payload"],
    }
    values.update(overrides)
    context = trusted_context or EventTrustedContext(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
        principal_id=event.principal_id,
        agent_client_id=event.agent_client_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        authorization_decision_id=event.authorization_decision_id,
    )
    return build_canonical_event(
        trusted_context=context,
        **values,  # type: ignore[arg-type]
    )


def _outcome_events() -> tuple[
    CanonicalEvent,
    CanonicalEvent,
    GateSession,
    RunOutcome,
    EventTrustedContext,
]:
    executing, completed, execution_event, outcome, trusted = _fixture()
    evaluation, recorded = build_run_outcome_event_batch(
        outcome,
        executing_session=executing,
        completed_session=completed,
        execution_event=execution_event,
        evaluator_context=_evaluator(),
        first_global_position=7,
        trusted_context=trusted,
    )
    return evaluation, recorded, completed, outcome, trusted


def _reduce(reducer, events: tuple[CanonicalEvent, ...]):
    state = reducer.initial_state()
    for event in sorted(events, key=lambda item: item.global_position):
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def test_completion_event_order_and_exact_round_trip() -> None:
    executing, completed, execution_event, outcome, trusted = _fixture()
    evaluation, recorded = build_run_outcome_event_batch(
        outcome,
        executing_session=executing,
        completed_session=completed,
        execution_event=execution_event,
        evaluator_context=_evaluator(),
        first_global_position=7,
        trusted_context=trusted,
    )
    completion = build_gate_session_event(
        completed,
        previous_session=executing,
        parent_event=execution_event,
        global_position=9,
        trusted_context=trusted,
    )

    assert tuple(
        event.event_type for event in (evaluation, recorded, completion)
    ) == (
        EVALUATION_AUTHENTICATED_EVENT,
        RUN_OUTCOME_RECORDED_EVENT,
        GATE_SESSION_COMPLETED_EVENT,
    )
    assert evaluation.causation_id == execution_event.event_id
    assert recorded.causation_id == evaluation.event_id
    assert recorded.previous_stream_event_sha256 == evaluation.event_sha256
    assert parse_evaluation_authenticated_event(evaluation).evaluator == _evaluator()
    assert parse_run_outcome_recorded_event(
        recorded,
        evaluation_event=evaluation,
        completed_session=completed,
    ).outcome == outcome
    assert build_run_outcome_event_batch(
        outcome,
        executing_session=executing,
        completed_session=completed,
        execution_event=execution_event,
        evaluator_context=_evaluator(),
        first_global_position=7,
        trusted_context=trusted,
    ) == (evaluation, recorded)
    for event in (evaluation, recorded):
        assert DEFAULT_EVENT_TYPE_REGISTRY.consume(event).payload == event.payload


def test_outcome_events_reject_scope_evaluator_and_position_mismatches() -> None:
    executing, completed, execution_event, outcome, trusted = _fixture()

    with pytest.raises(OutcomeEventV1Error, match="trusted outcome scope"):
        build_run_outcome_event_batch(
            outcome,
            executing_session=executing,
            completed_session=completed,
            execution_event=execution_event,
            evaluator_context=_evaluator(),
            first_global_position=7,
            trusted_context=replace(trusted, tenant_id="tenant_other"),
        )
    with pytest.raises(OutcomeEventV1Error, match="inputs are inconsistent"):
        build_run_outcome_event_batch(
            outcome,
            executing_session=executing,
            completed_session=completed,
            execution_event=execution_event,
            evaluator_context=replace(_evaluator(), evaluator_version="v2"),
            first_global_position=7,
            trusted_context=trusted,
        )
    with pytest.raises(OutcomeEventV1Error, match="positions"):
        build_run_outcome_event_batch(
            outcome,
            executing_session=executing,
            completed_session=completed,
            execution_event=execution_event,
            evaluator_context=_evaluator(),
            first_global_position=execution_event.global_position,
            trusted_context=trusted,
        )


def test_outcome_recorded_rejects_wrong_parent_and_tampered_payload() -> None:
    evaluation, recorded, _, _, _ = _outcome_events()

    wrong_parent = _clone_event(
        recorded,
        previous_stream_event_sha256=DIGEST_A,
    )
    with pytest.raises(OutcomeEventV1Error, match="stream parent"):
        parse_run_outcome_recorded_event(
            wrong_parent,
            evaluation_event=evaluation,
        )

    payload = dict(recorded.to_dict()["payload"])
    payload["completed_session_sha256"] = DIGEST_A
    tampered = _clone_event(recorded, payload=payload)
    with pytest.raises(OutcomeEventV1Error, match="session evidence"):
        parse_run_outcome_recorded_event(
            tampered,
            completed_session=_outcome_events()[2],
        )

    outcome_payload = dict(payload["outcome"])
    outcome_payload["evaluator_version"] = "v2"
    payload["outcome"] = outcome_payload
    with pytest.raises(OutcomeEventV1Error, match="outcome is invalid"):
        parse_run_outcome_recorded_event(_clone_event(recorded, payload=payload))


def test_association_produces_only_a_proposal_event() -> None:
    _, recorded, completed, outcome, trusted = _outcome_events()
    events = build_outcome_attribution_event_batch(
        _association(outcome),
        outcome_event=recorded,
        completed_session=completed,
        first_global_position=10,
        trusted_context=trusted,
    )

    assert len(events) == 1
    assert events[0].event_type == OUTCOME_ATTRIBUTION_PROPOSED_EVENT
    assert parse_outcome_attribution_proposed_event(events[0]).to_attribution() == (
        _association(outcome)
    )
    assert DEFAULT_EVENT_TYPE_REGISTRY.consume(events[0]).payload == events[0].payload


def test_causal_attribution_requires_independent_verified_event() -> None:
    _, recorded, completed, outcome, trusted = _outcome_events()
    attribution = _causal(outcome)
    proposal, verified = build_outcome_attribution_event_batch(
        attribution,
        outcome_event=recorded,
        completed_session=completed,
        first_global_position=10,
        trusted_context=trusted,
    )

    assert (proposal.event_type, verified.event_type) == (
        OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
        OUTCOME_ATTRIBUTION_VERIFIED_EVENT,
    )
    assert verified.causation_id == proposal.event_id
    assert verified.previous_stream_event_sha256 == proposal.event_sha256
    assert parse_outcome_attribution_proposed_event(proposal).claim_strength == (
        "causal"
    )
    assert parse_outcome_attribution_verified_event(
        verified,
        proposal_event=proposal,
    ).attribution == attribution
    for event in (proposal, verified):
        assert DEFAULT_EVENT_TYPE_REGISTRY.consume(event).payload == event.payload


def test_attribution_events_reject_linkage_and_parent_tampering() -> None:
    evaluation, recorded, completed, outcome, trusted = _outcome_events()
    proposal, verified = build_outcome_attribution_event_batch(
        _causal(outcome),
        outcome_event=recorded,
        completed_session=completed,
        first_global_position=10,
        trusted_context=trusted,
    )

    with pytest.raises(OutcomeEventV1Error, match="position"):
        build_outcome_attribution_event_batch(
            _association(outcome),
            outcome_event=recorded,
            completed_session=completed,
            first_global_position=recorded.global_position,
            trusted_context=trusted,
        )
    with pytest.raises(OutcomeEventV1Error, match="linkage is invalid"):
        build_outcome_attribution_event_batch(
            _association(outcome),
            outcome_event=evaluation,
            completed_session=completed,
            first_global_position=10,
            trusted_context=trusted,
        )

    payload = dict(verified.to_dict()["payload"])
    payload["proposal_event_id"] = "evt_wrong_parent"
    with pytest.raises(OutcomeEventV1Error, match="causation"):
        parse_outcome_attribution_verified_event(
            _clone_event(verified, payload=payload),
            proposal_event=proposal,
        )

    wrong_scope = _clone_event(
        proposal,
        trusted_context=replace(trusted, repository_id="repository_other"),
    )
    with pytest.raises(OutcomeEventV1Error, match="stream parent"):
        parse_outcome_attribution_verified_event(
            verified,
            proposal_event=wrong_scope,
        )


def test_outcome_reducer_matches_exact_authority_rows() -> None:
    evaluation, recorded, completed, outcome, _ = _outcome_events()
    reducer = build_outcome_current_reducer()
    state = _reduce(reducer, (evaluation, recorded))

    assert projected_run_outcome(state, outcome.run_outcome_id) == outcome
    assert _reduce(reducer, (evaluation, recorded)) == state
    verify_outcome_projection_parity(
        state,
        (
            OutcomeProjectionAuthority(
                outcome=outcome,
                completed_session=completed,
                evaluator=_evaluator(),
            ),
        ),
        (evaluation, recorded),
    )


def test_outcome_reducer_rejects_missing_or_wrong_evaluation_parent() -> None:
    evaluation, recorded, _, outcome, _ = _outcome_events()
    reducer = build_outcome_current_reducer()

    with pytest.raises(ReducerExecutionError, match="projection state"):
        _reduce(reducer, (recorded,))

    wrong_parent = _clone_event(
        recorded,
        previous_stream_event_sha256=DIGEST_A,
    )
    with pytest.raises(ReducerExecutionError, match="exact evaluation"):
        _reduce(reducer, (evaluation, wrong_parent))

    state = _reduce(reducer, (evaluation, recorded))
    tampered_state = dict(state)
    tampered_outcomes = dict(tampered_state["outcomes"])
    tampered_projection = dict(tampered_outcomes[outcome.run_outcome_id])
    tampered_record = dict(tampered_projection["record"])
    tampered_record["completed_session_sha256"] = DIGEST_A
    tampered_projection["record"] = tampered_record
    tampered_outcomes[outcome.run_outcome_id] = tampered_projection
    tampered_state["outcomes"] = tampered_outcomes
    with pytest.raises(ReducerExecutionError, match="differs"):
        verify_outcome_projection_parity(
            tampered_state,
            (
                OutcomeProjectionAuthority(
                    outcome=outcome,
                    completed_session=_outcome_events()[2],
                    evaluator=_evaluator(),
                ),
            ),
            (evaluation, recorded),
        )


def test_outcome_attribution_reducer_separates_association_and_causality() -> None:
    _, recorded, _, outcome, trusted = _outcome_events()
    association = _association(outcome)
    causal = _causal(outcome)
    association_events = build_outcome_attribution_event_batch(
        association,
        outcome_event=recorded,
        completed_session=_outcome_events()[2],
        first_global_position=10,
        trusted_context=trusted,
    )
    causal_events = build_outcome_attribution_event_batch(
        causal,
        outcome_event=recorded,
        completed_session=_outcome_events()[2],
        first_global_position=12,
        trusted_context=trusted,
    )
    events = (recorded, *association_events, *causal_events)
    reducer = build_outcome_attribution_reducer()
    state = _reduce(reducer, events)

    assert projected_outcome_attribution(state, association.attribution_id) == (
        association
    )
    assert projected_outcome_attribution(state, causal.attribution_id) == causal
    verify_outcome_attribution_projection_parity(
        state,
        (outcome,),
        (association, causal),
        events,
    )


def test_causal_proposal_is_not_projected_before_verification() -> None:
    _, recorded, completed, outcome, trusted = _outcome_events()
    causal = _causal(outcome)
    proposal, verified = build_outcome_attribution_event_batch(
        causal,
        outcome_event=recorded,
        completed_session=completed,
        first_global_position=10,
        trusted_context=trusted,
    )
    reducer = build_outcome_attribution_reducer()
    proposed_state = _reduce(reducer, (recorded, proposal))

    with pytest.raises(ReducerExecutionError, match="absent"):
        projected_outcome_attribution(proposed_state, causal.attribution_id)
    verified_state = reducer.transition(
        proposed_state,
        ReducerEvent(
            verified,
            DEFAULT_EVENT_TYPE_REGISTRY.consume(verified),
        ),
    )
    assert projected_outcome_attribution(
        verified_state,
        causal.attribution_id,
    ) == causal


def test_attribution_reducer_requires_recorded_outcome_and_linear_parent() -> None:
    _, recorded, completed, outcome, trusted = _outcome_events()
    proposal, verified = build_outcome_attribution_event_batch(
        _causal(outcome),
        outcome_event=recorded,
        completed_session=completed,
        first_global_position=10,
        trusted_context=trusted,
    )
    reducer = build_outcome_attribution_reducer()

    with pytest.raises(ReducerExecutionError, match="projection state"):
        _reduce(reducer, (proposal,))
    wrong_parent = _clone_event(
        verified,
        previous_stream_event_sha256=DIGEST_A,
    )
    with pytest.raises(ReducerExecutionError, match="exact proposal"):
        _reduce(reducer, (recorded, proposal, wrong_parent))
