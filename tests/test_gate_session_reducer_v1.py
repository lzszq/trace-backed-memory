from __future__ import annotations

from collections.abc import Mapping

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import CanonicalEvent, EventTrustedContext
from trace_backed_memory.reducer import (
    ReducerEvent,
    ReducerExecutionError,
    execute_reducer_step,
)
from trace_backed_memory.reducer_registry import DEFAULT_REDUCER_REGISTRY


HASH = "sha256:" + "a" * 64


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain(item) for item in value]
    return value


def _sessions() -> tuple[tbm.GateSession, ...]:
    created = tbm.create_gate_session(
        session_id="gate_session_reducer_001",
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
    prepared = tbm.transition_gate_session(
        created,
        "prepared",
        expected_version=1,
        updated_at="2026-08-01T00:01:00Z",
        lease_expires_at="2026-08-01T00:20:00Z",
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id="system_gate_001",
    )
    awaiting = tbm.transition_gate_session(
        prepared,
        "awaiting_decision",
        expected_version=2,
        updated_at="2026-08-01T00:02:00Z",
    )
    decided = tbm.transition_gate_session(
        awaiting,
        "decided",
        expected_version=3,
        updated_at="2026-08-01T00:03:00Z",
        semantic_gate_attempt_ids=("semantic_gate_attempt_001",),
        decision_id="decision_001",
    )
    finalized = tbm.transition_gate_session(
        decided,
        "finalized",
        expected_version=4,
        updated_at="2026-08-01T00:04:00Z",
        final_memory_revision_ids=("memory_revision_001",),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_decision_001",
    )
    executing = tbm.transition_gate_session(
        finalized,
        "executing",
        expected_version=5,
        updated_at="2026-08-01T00:05:00Z",
    )
    completed = tbm.transition_gate_session(
        executing,
        "completed",
        expected_version=6,
        updated_at="2026-08-01T00:06:00Z",
        run_outcome_id="run_outcome_001",
    )
    return created, prepared, awaiting, decided, finalized, executing, completed


def _events(sessions: tuple[tbm.GateSession, ...]) -> tuple[CanonicalEvent, ...]:
    context = EventTrustedContext(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_local",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id="authorization_decision_001",
    )
    built: list[CanonicalEvent] = []
    for index, session in enumerate(sessions):
        built.append(
            tbm.build_gate_session_event(
                session,
                previous_session=None if index == 0 else sessions[index - 1],
                parent_event=None if index == 0 else built[index - 1],
                global_position=index + 1,
                trusted_context=context,
            )
        )
    return tuple(built)


def _reduce(
    events: tuple[CanonicalEvent, ...],
) -> tuple[object, dict[str, object]]:
    reducer = tbm.build_gate_session_reducer()
    state: object = reducer.initial_state()
    for event in events:
        result = execute_reducer_step(
            reducer,
            state,  # type: ignore[arg-type]
            ReducerEvent(
                event,
                DEFAULT_EVENT_TYPE_REGISTRY.consume(event, target_version=1),
            ),
        )
        state = result.state
    return reducer, dict(state)  # type: ignore[arg-type]


def test_gate_session_reducer_rebuilds_exact_current_projection() -> None:
    sessions = _sessions()
    events = _events(sessions)
    reducer, state = _reduce(events)

    descriptor = reducer.descriptor  # type: ignore[attr-defined]
    assert descriptor.reducer_id == tbm.GATE_SESSION_REDUCER_ID
    assert descriptor.input_event_types == tbm.GATE_SESSION_EVENT_TYPES
    assert descriptor.envelope_only is False
    assert DEFAULT_REDUCER_REGISTRY.resolve(
        tbm.GATE_SESSION_REDUCER_ID,
        1,
    ).descriptor == descriptor
    assert tbm.projected_gate_session(
        state,
        sessions[-1].session_id,
    ) == sessions[-1]
    tbm.verify_gate_session_projection_parity(state, (sessions[-1],))

    head = state["heads"][sessions[-1].session_id]  # type: ignore[index]
    assert head == {
        "session_version": sessions[-1].version,
        "event_id": events[-1].event_id,
        "event_sha256": events[-1].event_sha256,
        "global_position": events[-1].global_position,
        "organization_id": events[-1].organization_id,
        "tenant_id": events[-1].tenant_id,
        "repository_id": events[-1].repository_id,
        "environment_id": events[-1].environment_id,
        "authorization_decision_id": events[-1].authorization_decision_id,
    }


def test_gate_session_reducer_rejects_out_of_order_and_corrupt_heads() -> None:
    sessions = _sessions()[:2]
    events = _events(sessions)
    reducer, first_state = _reduce(events[:1])
    corrupt_states = []
    for field, value in (
        ("event_id", "evt_wrong_parent"),
        ("event_sha256", "sha256:" + "f" * 64),
        ("global_position", events[1].global_position),
        ("environment_id", "environment_other"),
    ):
        state = _plain(first_state)
        assert type(state) is dict
        state["heads"][sessions[0].session_id][field] = value  # type: ignore[index]
        corrupt_states.append(state)
    extra = _plain(first_state)
    assert type(extra) is dict
    extra["heads"][sessions[0].session_id]["unexpected"] = True  # type: ignore[index]
    corrupt_states.append(extra)

    typed = DEFAULT_EVENT_TYPE_REGISTRY.consume(events[1], target_version=1)
    for state in corrupt_states:
        with pytest.raises(ReducerExecutionError) as raised:
            execute_reducer_step(
                reducer,  # type: ignore[arg-type]
                state,
                ReducerEvent(events[1], typed),
            )
        assert raised.value.code == "TBM_GATE_SESSION_REDUCER_EVENT_INVALID"


def test_gate_session_projection_parity_fails_closed() -> None:
    sessions = _sessions()
    _, state = _reduce(_events(sessions))

    with pytest.raises(ReducerExecutionError) as missing:
        tbm.verify_gate_session_projection_parity(state, ())
    assert missing.value.code == "TBM_GATE_SESSION_REDUCER_EVENT_INVALID"

    altered = _plain(state)
    assert type(altered) is dict
    altered["sessions"][sessions[-1].session_id]["decision_id"] = "other"  # type: ignore[index]
    with pytest.raises(ReducerExecutionError):
        tbm.verify_gate_session_projection_parity(altered, (sessions[-1],))
