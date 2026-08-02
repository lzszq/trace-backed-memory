from __future__ import annotations

from collections.abc import Mapping

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
)


HASH = "sha256:" + "a" * 64


def _created(session_id: str = "gate_session_events_001") -> tbm.GateSession:
    return tbm.create_gate_session(
        session_id=session_id,
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=HASH,
        idempotency_key="request-001",
        created_at="2026-08-01T00:00:00Z",
        expires_at="2026-08-01T01:00:00Z",
    )


def _prepared(previous: tbm.GateSession) -> tbm.GateSession:
    return tbm.transition_gate_session(
        previous,
        "prepared",
        expected_version=previous.version,
        updated_at="2026-08-01T00:01:00Z",
        lease_expires_at="2026-08-01T00:20:00Z",
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id="system_gate_001",
    )


def _awaiting(previous: tbm.GateSession) -> tbm.GateSession:
    return tbm.transition_gate_session(
        previous,
        "awaiting_decision",
        expected_version=previous.version,
        updated_at="2026-08-01T00:02:00Z",
    )


def _decided(previous: tbm.GateSession) -> tbm.GateSession:
    return tbm.transition_gate_session(
        previous,
        "decided",
        expected_version=previous.version,
        updated_at="2026-08-01T00:03:00Z",
        semantic_gate_attempt_ids=("semantic_gate_attempt_001",),
        decision_id="decision_001",
    )


def _finalized(previous: tbm.GateSession) -> tbm.GateSession:
    return tbm.transition_gate_session(
        previous,
        "finalized",
        expected_version=previous.version,
        updated_at="2026-08-01T00:04:00Z",
        final_memory_revision_ids=("memory_revision_001",),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_decision_001",
    )


def _executing(previous: tbm.GateSession) -> tbm.GateSession:
    return tbm.transition_gate_session(
        previous,
        "executing",
        expected_version=previous.version,
        updated_at="2026-08-01T00:05:00Z",
    )


def _completed(previous: tbm.GateSession) -> tbm.GateSession:
    return tbm.transition_gate_session(
        previous,
        "completed",
        expected_version=previous.version,
        updated_at="2026-08-01T00:06:00Z",
        run_outcome_id="run_outcome_001",
    )


def _main_sessions(
    session_id: str = "gate_session_events_001",
) -> tuple[tbm.GateSession, ...]:
    created = _created(session_id)
    prepared = _prepared(created)
    awaiting = _awaiting(prepared)
    decided = _decided(awaiting)
    finalized = _finalized(decided)
    executing = _executing(finalized)
    return (
        created,
        prepared,
        awaiting,
        decided,
        finalized,
        executing,
        _completed(executing),
    )


def _trusted(session: tbm.GateSession) -> EventTrustedContext:
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


def _events(
    sessions: tuple[tbm.GateSession, ...],
    *,
    first_position: int = 1,
) -> tuple[CanonicalEvent, ...]:
    built: list[CanonicalEvent] = []
    previous_session: tbm.GateSession | None = None
    parent_event: CanonicalEvent | None = None
    for offset, session in enumerate(sessions):
        event = tbm.build_gate_session_event(
            session,
            previous_session=previous_session,
            parent_event=parent_event,
            global_position=first_position + offset,
            trusted_context=_trusted(session),
        )
        built.append(event)
        previous_session = session
        parent_event = event
    return tuple(built)


def _clone_event(event: CanonicalEvent, **overrides: object) -> CanonicalEvent:
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
    trusted_context = EventTrustedContext(
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
        trusted_context=trusted_context,
        **values,  # type: ignore[arg-type]
    )


def test_all_gate_session_revision_event_types_are_exact_and_registered() -> None:
    main = _main_sessions()
    events = list(_events(main))

    canceled_created = _created("gate_session_canceled_001")
    canceled = tbm.transition_gate_session(
        canceled_created,
        "canceled",
        expected_version=1,
        updated_at="2026-08-01T00:01:00Z",
        terminal_reason="caller canceled",
    )
    events.append(_events((canceled_created, canceled), first_position=20)[-1])

    expired_sessions = _main_sessions("gate_session_expired_001")[:3]
    expired = tbm.transition_gate_session(
        expired_sessions[-1],
        "expired",
        expected_version=3,
        updated_at="2026-08-01T01:00:01Z",
        terminal_reason="deadline elapsed",
    )
    events.append(_events((*expired_sessions, expired), first_position=30)[-1])

    abandoned_sessions = _main_sessions("gate_session_abandoned_001")[:6]
    abandoned = tbm.transition_gate_session(
        abandoned_sessions[-1],
        "abandoned",
        expected_version=6,
        updated_at="2026-08-01T00:06:00Z",
        terminal_reason="execution lease lost",
    )
    events.append(_events((*abandoned_sessions, abandoned), first_position=40)[-1])

    renewal_created = _created("gate_session_renewed_001")
    renewal_prepared = _prepared(renewal_created)
    renewed = tbm.renew_gate_session_lease(
        renewal_prepared,
        expected_version=2,
        updated_at="2026-08-01T00:10:00Z",
        lease_expires_at="2026-08-01T00:30:00Z",
    )
    events.append(
        _events(
            (renewal_created, renewal_prepared, renewed),
            first_position=50,
        )[-1]
    )

    assert {event.event_type for event in events} == set(
        tbm.GATE_SESSION_EVENT_TYPES
    )
    for event in events:
        typed = DEFAULT_EVENT_TYPE_REGISTRY.consume(event, target_version=1)
        assert typed.source_event is event
        assert typed.event_type == event.event_type
        assert typed.target_version == 1


def test_gate_session_event_roundtrip_is_deterministic_and_parent_bound() -> None:
    sessions = _main_sessions()
    events = _events(sessions)

    rebuilt = _events(sessions)
    assert events == rebuilt
    for index, (session, event) in enumerate(zip(sessions, events, strict=True)):
        previous = None if index == 0 else sessions[index - 1]
        parent = None if index == 0 else events[index - 1]
        assert tbm.parse_gate_session_event(
            event,
            previous_session=previous,
            parent_event=parent,
        ) == session
        payload = event.to_dict()["payload"]
        assert isinstance(payload, Mapping)
        assert payload["session_sha256"] == tbm.gate_session_revision_sha256(
            session
        )
        assert payload["transition_authorization_event_id"] == (
            event.authorization_decision_id
        )

    bogus_parent = _clone_event(events[0], payload={"bogus": "parent"})
    with pytest.raises(tbm.GateSessionEventV1Error) as bogus:
        tbm.build_gate_session_event(
            sessions[1],
            previous_session=sessions[0],
            parent_event=bogus_parent,
            global_position=2,
            trusted_context=_trusted(sessions[1]),
        )
    assert bogus.value.code == "TBM_GATE_SESSION_EVENT_INVALID"

    with pytest.raises(tbm.GateSessionEventV1Error) as position:
        tbm.build_gate_session_event(
            sessions[1],
            previous_session=sessions[0],
            parent_event=events[0],
            global_position=1,
            trusted_context=_trusted(sessions[1]),
        )
    assert position.value.code == "TBM_GATE_SESSION_EVENT_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", "evt_gate_forged"),
        ("request_id", "gate_request_forged"),
        ("idempotency_key_sha256", "sha256:" + "b" * 64),
        ("request_sha256", "sha256:" + "c" * 64),
        ("correlation_id", "gate_correlation_forged"),
        ("producer", "forged_producer"),
        ("producer_version", "9.9.9"),
    ],
)
def test_gate_session_event_rejects_deterministic_envelope_tampering(
    field: str,
    value: object,
) -> None:
    session = _created()
    event = _events((session,))[0]
    tampered = _clone_event(event, **{field: value})

    with pytest.raises(tbm.GateSessionEventV1Error) as raised:
        tbm.parse_gate_session_event(tampered, previous_session=None)
    assert raised.value.code == "TBM_GATE_SESSION_EVENT_INVALID"


def test_gate_session_event_rejects_payload_and_causation_tampering() -> None:
    sessions = _main_sessions()[:2]
    events = _events(sessions)
    payload = dict(events[1].to_dict()["payload"])
    payload["session_sha256"] = "sha256:" + "f" * 64
    tampered_payload = _clone_event(events[1], payload=payload)
    tampered_causation = _clone_event(events[1], causation_id="evt_forged_parent")

    for tampered in (tampered_payload, tampered_causation):
        with pytest.raises(tbm.GateSessionEventV1Error) as raised:
            tbm.parse_gate_session_event(
                tampered,
                previous_session=sessions[0],
            )
        assert raised.value.code == "TBM_GATE_SESSION_EVENT_INVALID"
