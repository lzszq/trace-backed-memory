from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.gate_evidence_event_v1 import (
    RETRIEVAL_PREPARED_EVENT,
    SYSTEM_GATE_EVALUATED_EVENT,
    GateEvidenceEventV1Error,
    build_retrieval_prepared_event,
    build_system_gate_evaluated_event,
    parse_gate_evidence_event,
)
from trace_backed_memory.gate_evidence_reducer_v1 import (
    build_gate_evidence_reducer,
    verify_gate_evidence_projection_parity,
)
from trace_backed_memory.reducer import (
    ReducerEvent,
    ReducerExecutionError,
    execute_reducer_step,
)


ROOT = Path(__file__).resolve().parents[1]


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain(item) for item in value]
    return value


def _records() -> tuple[tbm.RetrievalSnapshot, tbm.SystemGateEvaluation]:
    snapshot = tbm.loads_retrieval_snapshot(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
    )
    evaluation = tbm.loads_system_gate_evaluation(
        (
            ROOT / "examples" / "system_gate_evaluation_v3.example.json"
        ).read_bytes()
    )
    return snapshot, evaluation


def _trusted(authorization_event_id: str) -> EventTrustedContext:
    return EventTrustedContext(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_local",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id=authorization_event_id,
    )


def _events():
    snapshot, evaluation = _records()
    trusted = _trusted(snapshot.authorization_event_id)
    retrieval = build_retrieval_prepared_event(
        snapshot,
        global_position=1,
        trusted_context=trusted,
    )
    system_gate = build_system_gate_evaluated_event(
        evaluation,
        retrieval_event=retrieval,
        global_position=2,
        trusted_context=trusted,
    )
    return snapshot, evaluation, retrieval, system_gate


def _with_causation(event: CanonicalEvent, causation_id: str) -> CanonicalEvent:
    payload = dict(event.payload)
    payload["causation_event_id"] = causation_id
    trusted = EventTrustedContext(
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
        event_id=event.event_id,
        event_type=event.event_type,
        event_version=event.event_version,
        event_kind=event.event_kind,
        origin=event.origin,
        source=event.source,
        stream_id=event.stream_id,
        stream_type=event.stream_type,
        stream_version=event.stream_version,
        global_position=event.global_position,
        trusted_context=trusted,
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256="sha256:" + "f" * 64,
        correlation_id=event.correlation_id,
        causation_id=causation_id,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        artifact_refs=event.artifact_refs,
        payload=payload,
    )


def test_gate_evidence_events_bind_exact_artifact_descriptors() -> None:
    snapshot, evaluation, retrieval, system_gate = _events()
    retrieval_bytes = tbm.dumps_retrieval_snapshot(snapshot).encode("utf-8")
    system_gate_bytes = tbm.dumps_system_gate_evaluation(evaluation).encode("utf-8")

    assert retrieval.event_type == RETRIEVAL_PREPARED_EVENT
    assert system_gate.event_type == SYSTEM_GATE_EVALUATED_EVENT
    assert retrieval.stream_id == snapshot.snapshot_id
    assert system_gate.stream_id == evaluation.evaluation_id
    assert system_gate.causation_id == retrieval.event_id
    assert retrieval.artifact_refs[0].content_sha256 == (
        "sha256:" + hashlib.sha256(retrieval_bytes).hexdigest()
    )
    assert system_gate.artifact_refs[0].content_sha256 == (
        "sha256:" + hashlib.sha256(system_gate_bytes).hexdigest()
    )
    assert retrieval.artifact_refs[0].size_bytes == len(retrieval_bytes)
    assert system_gate.artifact_refs[0].size_bytes == len(system_gate_bytes)
    assert parse_gate_evidence_event(retrieval).record_id == snapshot.snapshot_id
    assert (
        parse_gate_evidence_event(system_gate).retrieval_snapshot_id
        == snapshot.snapshot_id
    )
    assert DEFAULT_EVENT_TYPE_REGISTRY.consume(retrieval).payload == retrieval.payload
    assert DEFAULT_EVENT_TYPE_REGISTRY.consume(system_gate).payload == system_gate.payload


def test_gate_evidence_reducer_rebuilds_exact_current_linkage() -> None:
    snapshot, evaluation, retrieval, system_gate = _events()
    reducer = build_gate_evidence_reducer()
    state = reducer.initial_state()
    for event in (retrieval, system_gate):
        state = execute_reducer_step(
            reducer,
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        ).state

    verify_gate_evidence_projection_parity(
        state,
        (snapshot,),
        (evaluation,),
    )
    assert state["sessions"][snapshot.session_id] == {  # type: ignore[index]
        "authorization_event_id": snapshot.authorization_event_id,
        "retrieval_snapshot_id": snapshot.snapshot_id,
        "system_gate_evaluation_id": evaluation.evaluation_id,
    }


def test_gate_evidence_reducer_rejects_system_gate_before_retrieval() -> None:
    _, _, _, system_gate = _events()
    reducer = build_gate_evidence_reducer()

    with pytest.raises(ReducerExecutionError) as raised:
        execute_reducer_step(
            reducer,
            reducer.initial_state(),
            ReducerEvent(
                system_gate,
                DEFAULT_EVENT_TYPE_REGISTRY.consume(system_gate),
            ),
        )
    assert raised.value.code == "TBM_GATE_EVIDENCE_REDUCER_EVENT_INVALID"


def test_retrieval_evidence_rejects_fabricated_causation() -> None:
    _, _, retrieval, _ = _events()
    malformed = _with_causation(retrieval, "evt_fabricated_parent")

    with pytest.raises(GateEvidenceEventV1Error):
        parse_gate_evidence_event(malformed)
    with pytest.raises(tbm.EventRegistryV1Error):
        DEFAULT_EVENT_TYPE_REGISTRY.consume(malformed)


def test_gate_evidence_projection_parity_rejects_head_and_artifact_drift() -> None:
    snapshot, evaluation, retrieval, system_gate = _events()
    reducer = build_gate_evidence_reducer()
    state = reducer.initial_state()
    for event in (retrieval, system_gate):
        state = execute_reducer_step(
            reducer,
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        ).state

    orphan_head = _plain(state)
    assert type(orphan_head) is dict
    orphan_head["heads"]["orphan"] = dict(orphan_head["heads"][snapshot.snapshot_id])
    with pytest.raises(ReducerExecutionError):
        verify_gate_evidence_projection_parity(
            orphan_head,
            (snapshot,),
            (evaluation,),
        )

    artifact_drift = _plain(state)
    assert type(artifact_drift) is dict
    artifact_drift["retrieval_snapshots"][snapshot.snapshot_id]["artifact_ref"][
        "size_bytes"
    ] = 1
    with pytest.raises(ReducerExecutionError):
        verify_gate_evidence_projection_parity(
            artifact_drift,
            (snapshot,),
            (evaluation,),
        )
