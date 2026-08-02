from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from tests.test_sqlite_semantic_gate_artifact_v3 import (
    PROMPT,
    RESPONSE,
    _artifacts,
    _attempt,
    _evidence,
)
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.reducer import (
    ReducerEvent,
    ReducerExecutionError,
    execute_reducer_step,
)
from trace_backed_memory.semantic_gate_attempt_event_v1 import (
    SEMANTIC_GATE_ATTEMPT_FAILED_EVENT,
    SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
    build_semantic_gate_attempt_event,
    parse_semantic_gate_attempt_event,
    semantic_gate_attempt_event_payload_schema,
)
from trace_backed_memory.semantic_gate_attempt_reducer_v1 import (
    build_semantic_gate_attempt_reducer,
    verify_semantic_gate_attempt_projection_parity,
)


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


def _attempts() -> tuple[
    tbm.StoredSemanticGateAttemptArtifacts,
    tbm.StoredSemanticGateAttemptArtifacts,
]:
    failed = _attempt(succeeded=False)
    failed_prompt, failed_response = _artifacts(failed)
    assert failed_response is None
    succeeded_template = _attempt()
    values = {
        key: value
        for key, value in succeeded_template.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(
        sequence=2,
        previous_attempt_id=failed.attempt_id,
        started_at="2026-07-27T08:03:00Z",
        finished_at="2026-07-27T08:03:01Z",
    )
    succeeded = tbm.build_semantic_gate_attempt(**values)
    succeeded_prompt, succeeded_response = _artifacts(succeeded)
    assert succeeded_response is not None
    return (
        tbm.StoredSemanticGateAttemptArtifacts(
            failed,
            failed_prompt,
            None,
        ),
        tbm.StoredSemanticGateAttemptArtifacts(
            succeeded,
            succeeded_prompt,
            succeeded_response,
        ),
    )


def _events():
    snapshot, evaluation = _evidence()
    evidence_trusted = _trusted(evaluation.authorization_event_id)
    retrieval_event = tbm.build_retrieval_prepared_event(
        snapshot,
        global_position=1,
        trusted_context=evidence_trusted,
    )
    system_gate_event = tbm.build_system_gate_evaluated_event(
        evaluation,
        retrieval_event=retrieval_event,
        global_position=2,
        trusted_context=evidence_trusted,
    )
    trusted = _trusted("authorization_transition_001")
    failed, succeeded = _attempts()
    failed_event = build_semantic_gate_attempt_event(
        failed.attempt,
        failed.prompt,
        failed.response,
        system_gate_event=system_gate_event,
        previous_event=None,
        global_position=3,
        trusted_context=trusted,
    )
    succeeded_event = build_semantic_gate_attempt_event(
        succeeded.attempt,
        succeeded.prompt,
        succeeded.response,
        system_gate_event=None,
        previous_event=failed_event,
        global_position=4,
        trusted_context=_trusted("authorization_transition_002"),
    )
    return failed, succeeded, system_gate_event, failed_event, succeeded_event


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain(item) for item in value]
    return value


def _with_payload(
    event: CanonicalEvent,
    payload: Mapping[str, object],
    *,
    trusted_context: EventTrustedContext | None = None,
    request_sha256: str | None = None,
) -> CanonicalEvent:
    trusted = trusted_context or EventTrustedContext(
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
        request_sha256=request_sha256 or "sha256:" + "f" * 64,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
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


def test_semantic_gate_attempt_events_bind_exact_artifact_descriptors() -> None:
    failed, succeeded, system_gate_event, failed_event, succeeded_event = _events()

    assert failed_event.event_type == SEMANTIC_GATE_ATTEMPT_FAILED_EVENT
    assert succeeded_event.event_type == SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT
    assert tuple(event.stream_version for event in (failed_event, succeeded_event)) == (
        1,
        2,
    )
    assert succeeded_event.stream_id == failed_event.stream_id
    assert failed_event.causation_id == system_gate_event.event_id
    assert failed_event.authorization_decision_id != (
        system_gate_event.authorization_decision_id
    )
    assert succeeded_event.causation_id == failed_event.event_id
    assert succeeded_event.authorization_decision_id != (
        failed_event.authorization_decision_id
    )
    assert (
        succeeded_event.previous_stream_event_sha256
        == failed_event.event_sha256
    )
    assert tuple(ref.artifact_id for ref in succeeded_event.artifact_refs) == tuple(
        sorted(ref.artifact_id for ref in succeeded_event.artifact_refs)
    )
    assert PROMPT.decode("utf-8") not in str(succeeded_event.payload)
    assert RESPONSE.decode("utf-8") not in str(succeeded_event.payload)
    assert succeeded.attempt.reason not in str(succeeded_event.payload)
    parsed = parse_semantic_gate_attempt_event(succeeded_event)
    assert parsed.attempt_id == succeeded.attempt.attempt_id
    assert (
        parsed.prompt_artifact_ref.artifact_id
        == succeeded.prompt.binding.artifact.artifact_id
    )
    assert (
        parsed.prompt_artifact_ref.content_sha256
        == succeeded.prompt.binding.artifact.content_sha256
    )
    assert DEFAULT_EVENT_TYPE_REGISTRY.consume(failed_event).payload == (
        failed_event.payload
    )
    assert DEFAULT_EVENT_TYPE_REGISTRY.consume(succeeded_event).payload == (
        succeeded_event.payload
    )


def test_semantic_gate_attempt_reducer_rebuilds_exact_chain() -> None:
    failed, succeeded, system_gate_event, failed_event, succeeded_event = _events()
    reducer = build_semantic_gate_attempt_reducer()
    state = reducer.initial_state()
    for event in (system_gate_event, failed_event, succeeded_event):
        state = execute_reducer_step(
            reducer,
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        ).state

    verify_semantic_gate_attempt_projection_parity(
        state,
        (failed, succeeded),
        (system_gate_event, failed_event, succeeded_event),
    )
    stream = state["streams"][failed_event.stream_id]  # type: ignore[index]
    assert stream["current_sequence"] == 2
    assert stream["current_attempt_id"] == succeeded.attempt.attempt_id


def test_semantic_gate_attempt_reducer_rejects_out_of_order_and_drift() -> None:
    failed, succeeded, system_gate_event, failed_event, succeeded_event = _events()
    reducer = build_semantic_gate_attempt_reducer()
    with pytest.raises(ReducerExecutionError):
        execute_reducer_step(
            reducer,
            reducer.initial_state(),
            ReducerEvent(
                succeeded_event,
                DEFAULT_EVENT_TYPE_REGISTRY.consume(succeeded_event),
            ),
        )
    with pytest.raises(ReducerExecutionError):
        execute_reducer_step(
            reducer,
            reducer.initial_state(),
            ReducerEvent(
                failed_event,
                DEFAULT_EVENT_TYPE_REGISTRY.consume(failed_event),
            ),
        )

    parent_state = execute_reducer_step(
        reducer,
        reducer.initial_state(),
        ReducerEvent(
            system_gate_event,
            DEFAULT_EVENT_TYPE_REGISTRY.consume(system_gate_event),
        ),
    ).state
    state = execute_reducer_step(
        reducer,
        parent_state,
        ReducerEvent(
            failed_event,
            DEFAULT_EVENT_TYPE_REGISTRY.consume(failed_event),
        ),
    ).state
    drift = _plain(state)
    assert type(drift) is dict
    drift["attempts"][failed.attempt.attempt_id]["prompt_artifact_ref"][
        "size_bytes"
    ] = 1
    with pytest.raises(ReducerExecutionError):
        verify_semantic_gate_attempt_projection_parity(
            drift,
            (failed,),
            (system_gate_event, failed_event),
        )
    with pytest.raises(ReducerExecutionError):
        verify_semantic_gate_attempt_projection_parity(
            state,
            (failed, succeeded),
            (system_gate_event, failed_event),
        )

    digest_drift = _plain(state)
    assert type(digest_drift) is dict
    digest_drift["attempts"][failed.attempt.attempt_id]["event_sha256"] = (
        "sha256:" + "f" * 64
    )
    digest_drift["heads"][failed_event.stream_id]["event_sha256"] = (
        "sha256:" + "f" * 64
    )
    with pytest.raises(ReducerExecutionError):
        verify_semantic_gate_attempt_projection_parity(
            digest_drift,
            (failed,),
            (system_gate_event, failed_event),
        )

    with pytest.raises(ReducerExecutionError):
        verify_semantic_gate_attempt_projection_parity(
            state,
            (failed, failed),
            (system_gate_event, failed_event),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("contract_version", "tbm.semantic-gate-attempt-event.v999"),
        ("retrieval_snapshot_id", "snapshot_generic"),
        ("final_allowed_revision_ids", ["revision_generic"]),
    ),
)
def test_semantic_gate_attempt_parser_and_registry_reject_contract_and_id_drift(
    field: str,
    value: object,
) -> None:
    _failed, _succeeded, _system, _failed_event, succeeded_event = _events()
    payload = dict(succeeded_event.payload)
    payload[field] = value
    malformed = _with_payload(succeeded_event, payload)

    with pytest.raises(tbm.SemanticGateAttemptEventV1Error):
        parse_semantic_gate_attempt_event(malformed)
    with pytest.raises(tbm.EventRegistryV1Error):
        DEFAULT_EVENT_TYPE_REGISTRY.consume(malformed)


@pytest.mark.parametrize(
    ("event_name", "field", "value"),
    (
        ("succeeded", "provider_id", "provider id"),
        ("succeeded", "provider_id", "提供方"),
        (
            "succeeded",
            "prompt_artifact_sha256",
            "xsha256:" + "a" * 64,
        ),
        (
            "failed",
            "final_allowed_revision_ids",
            ["memory_revision_sha256_" + "a" * 64],
        ),
        (
            "failed",
            "previous_attempt_id",
            "semantic_attempt_sha256_" + "a" * 64,
        ),
    ),
)
def test_semantic_gate_attempt_payload_schema_rejects_python_contract_drift(
    event_name: str,
    field: str,
    value: object,
) -> None:
    _failed, _succeeded, _system, failed_event, succeeded_event = _events()
    event = failed_event if event_name == "failed" else succeeded_event
    payload = dict(event.payload)
    payload[field] = value
    schema = semantic_gate_attempt_event_payload_schema(event.event_type)

    assert not Draft202012Validator(schema).is_valid(payload)


def test_semantic_gate_attempt_payload_schema_accepts_exact_events() -> None:
    _failed, _succeeded, _system, failed_event, succeeded_event = _events()

    for event in (failed_event, succeeded_event):
        schema = semantic_gate_attempt_event_payload_schema(event.event_type)
        Draft202012Validator(schema).validate(_plain(event.payload))


def test_first_semantic_attempt_requires_exact_same_scope_system_parent() -> None:
    failed, _succeeded, system_gate_event, _failed_event, _success = _events()
    trusted = _trusted("authorization_transition_001")
    with pytest.raises(tbm.SemanticGateAttemptEventV1Error):
        build_semantic_gate_attempt_event(
            failed.attempt,
            failed.prompt,
            failed.response,
            system_gate_event=None,
            previous_event=None,
            global_position=3,
            trusted_context=trusted,
        )

    other_scope = EventTrustedContext(
        organization_id=trusted.organization_id,
        tenant_id="tenant_other",
        repository_id=trusted.repository_id,
        environment_id=trusted.environment_id,
        principal_id=trusted.principal_id,
        agent_client_id=trusted.agent_client_id,
        actor_type=trusted.actor_type,
        actor_id=trusted.actor_id,
        authorization_decision_id=system_gate_event.authorization_decision_id,
    )
    moved_parent = _with_payload(
        system_gate_event,
        system_gate_event.payload,
        trusted_context=other_scope,
        request_sha256=system_gate_event.request_sha256,
    )
    with pytest.raises(tbm.SemanticGateAttemptEventV1Error):
        build_semantic_gate_attempt_event(
            failed.attempt,
            failed.prompt,
            failed.response,
            system_gate_event=moved_parent,
            previous_event=None,
            global_position=3,
            trusted_context=trusted,
        )


def test_semantic_attempt_builder_rejects_non_monotonic_parent_position() -> None:
    failed, succeeded, system_gate_event, failed_event, _success = _events()

    with pytest.raises(tbm.SemanticGateAttemptEventV1Error):
        build_semantic_gate_attempt_event(
            failed.attempt,
            failed.prompt,
            failed.response,
            system_gate_event=system_gate_event,
            previous_event=None,
            global_position=system_gate_event.global_position,
            trusted_context=_trusted("authorization_transition_001"),
        )

    with pytest.raises(tbm.SemanticGateAttemptEventV1Error):
        build_semantic_gate_attempt_event(
            succeeded.attempt,
            succeeded.prompt,
            succeeded.response,
            system_gate_event=None,
            previous_event=failed_event,
            global_position=failed_event.global_position,
            trusted_context=_trusted("authorization_transition_002"),
        )
