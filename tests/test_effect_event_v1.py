from __future__ import annotations

from dataclasses import replace

import pytest

from trace_backed_memory.completion_outbox_v3 import (
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    acknowledge_completion_outbox_delivery,
    build_completion_outbox_event,
    build_initial_completion_outbox_delivery,
    claim_completion_outbox_delivery,
    fail_completion_outbox_delivery,
)
from trace_backed_memory.effect_event_v1 import (
    EFFECT_COMPENSATED_EVENT,
    EFFECT_COMPENSATION_REQUESTED_EVENT,
    EFFECT_DEAD_LETTERED_EVENT,
    EFFECT_FAILED_EVENT,
    EFFECT_REQUESTED_EVENT,
    EFFECT_RETRY_SCHEDULED_EVENT,
    EFFECT_STARTED_EVENT,
    EffectCompensationRequestedRef,
    EffectContract,
    EffectEventV1Error,
    EffectRequestedRef,
    build_completion_effect_requested_event,
    build_effect_compensated_event,
    build_effect_compensation_requested_event,
    build_effect_delivery_event_batch,
    build_effect_requested_event,
    completion_effect_contract,
    parse_effect_compensated_event,
    parse_effect_compensation_requested_event,
    parse_effect_delivery_event,
    parse_effect_requested_event,
)
from trace_backed_memory.effect_reducer_v1 import (
    EffectProjectionAuthority,
    build_effect_queue_reducer,
    projected_completion_delivery,
    projected_completion_outbox_event,
    projected_effect_contract,
    projected_effect_status,
    verify_effect_projection_parity,
)
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import CanonicalEvent, EventTrustedContext
from trace_backed_memory.gate_session_event_v1 import build_gate_session_event
from trace_backed_memory.gate_session_v3 import (
    GateSession,
    create_gate_session,
    transition_gate_session,
)
from trace_backed_memory.outcome_v3 import build_run_outcome
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


def _worker_trusted(
    trusted: EventTrustedContext,
    worker_id: str,
) -> EventTrustedContext:
    return replace(trusted, actor_type="worker", actor_id=worker_id)


def _completed_fixture() -> tuple[
    CanonicalEvent,
    CompletionOutboxEvent,
    CompletionOutboxDelivery,
    EventTrustedContext,
]:
    created = create_gate_session(
        session_id="gate_session_effect_events_001",
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
    outcome = build_run_outcome(
        session_id=executing.session_id,
        trace_id=executing.trace_id,
        run_id=executing.run_id,
        usage_decision_id="usage_decision_001",
        result="pass",
        evaluator_id="outcome_evaluator",
        evaluator_version="v1",
        output_sha256=DIGEST_A,
        evidence_artifact_sha256s=(DIGEST_B,),
        measured_at="2026-08-01T00:06:00Z",
    )
    completed = transition_gate_session(
        executing,
        "completed",
        expected_version=6,
        updated_at=outcome.measured_at,
        run_outcome_id=outcome.run_outcome_id,
    )
    sessions = (
        created,
        prepared,
        awaiting,
        decided,
        finalized,
        executing,
        completed,
    )
    previous_session: GateSession | None = None
    parent_event: CanonicalEvent | None = None
    events: list[CanonicalEvent] = []
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
    outbox_event = build_completion_outbox_event(outcome, completed)
    initial = build_initial_completion_outbox_delivery(outbox_event)
    return events[-1], outbox_event, initial, _trusted(completed)


def _reduce(events: tuple[CanonicalEvent, ...]):
    reducer = build_effect_queue_reducer()
    state = reducer.initial_state()
    for event in events:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def _dead_letter_history() -> tuple[
    tuple[CanonicalEvent, ...],
    CompletionOutboxEvent,
    tuple[CompletionOutboxDelivery, ...],
]:
    completed_event, outbox_event, pending, trusted = _completed_fixture()
    requested = build_completion_effect_requested_event(
        outbox_event,
        pending,
        completed_event=completed_event,
        global_position=8,
        trusted_context=trusted,
    )
    first_lease = claim_completion_outbox_delivery(
        pending,
        worker_id="worker_001",
        claimed_at="2026-08-01T00:07:00Z",
        lease_seconds=60,
    )
    started = build_effect_delivery_event_batch(
        pending,
        first_lease,
        parent_event=requested,
        first_global_position=9,
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    retry = fail_completion_outbox_delivery(
        first_lease,
        worker_id="worker_001",
        failed_at="2026-08-01T00:07:30Z",
        error_code="consumer_unavailable",
        retry_delay_seconds=60,
        max_attempts=2,
    )
    retry_events = build_effect_delivery_event_batch(
        first_lease,
        retry,
        parent_event=started[-1],
        first_global_position=10,
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    second_lease = claim_completion_outbox_delivery(
        retry,
        worker_id="worker_002",
        claimed_at="2026-08-01T00:09:00Z",
        lease_seconds=60,
    )
    restarted = build_effect_delivery_event_batch(
        retry,
        second_lease,
        parent_event=retry_events[-1],
        first_global_position=12,
        trusted_context=_worker_trusted(trusted, "worker_002"),
    )
    dead_letter = fail_completion_outbox_delivery(
        second_lease,
        worker_id="worker_002",
        failed_at="2026-08-01T00:09:30Z",
        error_code="consumer_rejected",
        retry_delay_seconds=60,
        max_attempts=2,
    )
    dead_events = build_effect_delivery_event_batch(
        second_lease,
        dead_letter,
        parent_event=restarted[-1],
        first_global_position=13,
        trusted_context=_worker_trusted(trusted, "worker_002"),
    )
    return (
        (requested, *started, *retry_events, *restarted, *dead_events),
        outbox_event,
        (pending, first_lease, retry, second_lease, dead_letter),
    )


def test_effect_queue_replays_delivery_history_retry_and_dead_letter():
    events, outbox_event, delivery_history = _dead_letter_history()
    assert tuple(event.event_type for event in events) == (
        EFFECT_REQUESTED_EVENT,
        EFFECT_STARTED_EVENT,
        EFFECT_FAILED_EVENT,
        EFFECT_RETRY_SCHEDULED_EVENT,
        EFFECT_STARTED_EVENT,
        EFFECT_FAILED_EVENT,
        EFFECT_DEAD_LETTERED_EVENT,
    )
    assert parse_effect_requested_event(events[0]).outbox_event == outbox_event
    for parent, event in zip(events, events[1:], strict=False):
        if event.event_type in {
            EFFECT_STARTED_EVENT,
            EFFECT_FAILED_EVENT,
            EFFECT_RETRY_SCHEDULED_EVENT,
            EFFECT_DEAD_LETTERED_EVENT,
        }:
            parse_effect_delivery_event(event, parent_event=parent)
    for event in events:
        assert DEFAULT_EVENT_TYPE_REGISTRY.consume(event).event_type == (
            event.event_type
        )

    state = _reduce(events)
    effect_id = outbox_event.event_id
    assert projected_effect_status(state, effect_id) == "dead_letter"
    assert projected_completion_outbox_event(state, effect_id) == outbox_event
    assert projected_completion_delivery(state, effect_id) == delivery_history[-1]
    assert projected_effect_contract(state, effect_id).effect_type == (
        "completion_notification"
    )
    verify_effect_projection_parity(
        state,
        (EffectProjectionAuthority(outbox_event, delivery_history),),
        events,
    )


def test_effect_queue_requires_failed_event_before_retry_disposition():
    events, _outbox_event, _delivery_history = _dead_letter_history()
    reducer = build_effect_queue_reducer()
    state = reducer.initial_state()
    for event in events[:2]:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    retry_without_failure = events[3]
    with pytest.raises(ReducerExecutionError) as error:
        reducer.transition(
            state,
            ReducerEvent(
                retry_without_failure,
                DEFAULT_EVENT_TYPE_REGISTRY.consume(retry_without_failure),
            ),
        )
    assert error.value.code == "TBM_EFFECT_REDUCER_EVENT_INVALID"


def test_compensation_is_a_new_effect_and_original_is_forward_only():
    completed_event, outbox_event, pending, trusted = _completed_fixture()
    original_contract = replace(
        completion_effect_contract(outbox_event, completed_event),
        compensation_supported=True,
    )
    requested = build_effect_requested_event(
        EffectRequestedRef(original_contract, outbox_event, pending),
        requested_by_event=completed_event,
        global_position=8,
        trusted_context=trusted,
    )
    lease = claim_completion_outbox_delivery(
        pending,
        worker_id="worker_001",
        claimed_at="2026-08-01T00:07:00Z",
        lease_seconds=60,
    )
    started = build_effect_delivery_event_batch(
        pending,
        lease,
        parent_event=requested,
        first_global_position=9,
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    delivered = acknowledge_completion_outbox_delivery(
        lease,
        worker_id="worker_001",
        acknowledged_at="2026-08-01T00:07:30Z",
        response_sha256=DIGEST_B,
    )
    succeeded = build_effect_delivery_event_batch(
        lease,
        delivered,
        parent_event=started[-1],
        first_global_position=10,
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    compensation_contract = EffectContract(
        effect_id="effect_compensation_001",
        effect_type="completion_notification_retraction",
        idempotency_key=f"{outbox_event.event_id}:compensation",
        requested_by_event_id=succeeded[-1].event_id,
        input_artifact_sha256=outbox_event.outcome_descriptor_sha256,
        authorization_event_id=trusted.authorization_decision_id,
        compensation_supported=False,
    )
    compensation_reference = EffectCompensationRequestedRef(
        original_effect_id=outbox_event.event_id,
        original_terminal_event_id=succeeded[-1].event_id,
        compensation_effect=compensation_contract,
    )
    compensation_requested = build_effect_compensation_requested_event(
        compensation_reference,
        original_requested_event=requested,
        original_terminal_event=succeeded[-1],
        global_position=11,
        occurred_at="2026-08-01T00:08:00Z",
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    compensated = build_effect_compensated_event(
        compensation_requested,
        global_position=12,
        occurred_at="2026-08-01T00:09:00Z",
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    assert parse_effect_compensation_requested_event(
        compensation_requested
    ) == compensation_reference
    assert parse_effect_compensated_event(
        compensated,
        compensation_request_event=compensation_requested,
    ).compensation_effect_id == compensation_contract.effect_id
    assert compensation_requested.event_type == EFFECT_COMPENSATION_REQUESTED_EVENT
    assert compensated.event_type == EFFECT_COMPENSATED_EVENT

    state = _reduce(
        (
            requested,
            *started,
            *succeeded,
            compensation_requested,
            compensated,
        )
    )
    assert projected_effect_status(state, outbox_event.event_id) == "compensated"
    assert projected_effect_status(state, compensation_contract.effect_id) == (
        "succeeded"
    )
    assert projected_effect_contract(
        state,
        compensation_contract.effect_id,
    ) == compensation_contract


def test_non_compensable_completion_effect_rejects_compensation_request():
    completed_event, outbox_event, pending, trusted = _completed_fixture()
    requested = build_completion_effect_requested_event(
        outbox_event,
        pending,
        completed_event=completed_event,
        global_position=8,
        trusted_context=trusted,
    )
    lease = claim_completion_outbox_delivery(
        pending,
        worker_id="worker_001",
        claimed_at="2026-08-01T00:07:00Z",
        lease_seconds=60,
    )
    started = build_effect_delivery_event_batch(
        pending,
        lease,
        parent_event=requested,
        first_global_position=9,
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    delivered = acknowledge_completion_outbox_delivery(
        lease,
        worker_id="worker_001",
        acknowledged_at="2026-08-01T00:07:30Z",
    )
    succeeded = build_effect_delivery_event_batch(
        lease,
        delivered,
        parent_event=started[-1],
        first_global_position=10,
        trusted_context=_worker_trusted(trusted, "worker_001"),
    )
    compensation = EffectContract(
        effect_id="effect_compensation_002",
        effect_type="completion_notification_retraction",
        idempotency_key=f"{outbox_event.event_id}:compensation",
        requested_by_event_id=succeeded[-1].event_id,
        input_artifact_sha256=outbox_event.outcome_descriptor_sha256,
        authorization_event_id=trusted.authorization_decision_id,
        compensation_supported=False,
    )
    with pytest.raises(EffectEventV1Error) as error:
        build_effect_compensation_requested_event(
            EffectCompensationRequestedRef(
                outbox_event.event_id,
                succeeded[-1].event_id,
                compensation,
            ),
            original_requested_event=requested,
            original_terminal_event=succeeded[-1],
            global_position=11,
            occurred_at="2026-08-01T00:08:00Z",
            trusted_context=_worker_trusted(trusted, "worker_001"),
        )
    assert error.value.code == "TBM_EFFECT_EVENT_INVALID"
