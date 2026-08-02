from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from tests.test_durable_finalization_v3 import _request, _stack
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
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
        authorization_decision_id="authorization_transition_001",
    )


def _gate_events(
    connection,
    session_id: str,
) -> tuple[
    tbm.GateSession,
    tbm.GateSession,
    CanonicalEvent,
    CanonicalEvent,
]:
    rows = connection.execute(
        "SELECT payload FROM gate_session_revisions "
        "WHERE session_id = ? ORDER BY version",
        (session_id,),
    ).fetchall()
    sessions = tuple(tbm.loads_gate_session(row[0]) for row in rows)
    events: list[CanonicalEvent] = []
    previous_session: tbm.GateSession | None = None
    parent_event: CanonicalEvent | None = None
    for position, session in enumerate(sessions, start=1):
        event = tbm.build_gate_session_event(
            session,
            previous_session=previous_session,
            parent_event=parent_event,
            global_position=position,
            trusted_context=_trusted(session),
        )
        events.append(event)
        previous_session = session
        parent_event = event
    return sessions[-2], sessions[-1], events[-2], events[-1]


def _supporting_artifacts(
    replay: tbm.SQLiteReplayV3Repository,
    usage_decision: tbm.UsageDecision,
) -> tuple[tbm.StoredReplayArtifact, ...]:
    artifact_ids = {
        tbm.usage_decision_artifact_id(usage_decision.usage_decision_id)
    }
    artifact_ids.update(
        tbm.artifact_id_from_sha256(digest)
        for name, digest in usage_decision.replay_components
        if name != "injection_artifact"
    )
    return tuple(replay.load_artifact(item) for item in sorted(artifact_ids))


def _clone_with_payload(
    event: CanonicalEvent,
    payload: Mapping[str, object],
) -> CanonicalEvent:
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
        trusted_context=EventTrustedContext(
            organization_id=event.organization_id,
            tenant_id=event.tenant_id,
            repository_id=event.repository_id,
            environment_id=event.environment_id,
            principal_id=event.principal_id,
            agent_client_id=event.agent_client_id,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            authorization_decision_id=event.authorization_decision_id,
        ),
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256="sha256:" + "f" * 64,
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


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain(item) for item in value]
    return value


def _built_event():
    stack = _stack()
    result = stack.finalizer().finalize(
        stack.context,
        stack.scope,
        _request(stack),
    )
    decided, finalized, decided_event, finalized_event = _gate_events(
        stack.connection,
        result.session.session_id,
    )
    supporting = _supporting_artifacts(stack.replay, result.usage_decision)
    event = tbm.build_injection_rendered_event(
        result.usage_decision,
        supporting,
        result.injection,
        result.snippet.encode("utf-8"),
        result.manifest,
        decided_session=decided,
        finalized_session=finalized,
        decided_event=decided_event,
        finalized_event=finalized_event,
        global_position=finalized_event.global_position + 1,
        trusted_context=_trusted(finalized),
    )
    return stack, result, supporting, finalized_event, event


def test_injection_rendered_event_binds_final_decision_and_complete_bundle() -> None:
    stack, result, _supporting, finalized_event, event = _built_event()
    try:
        parsed = tbm.parse_injection_rendered_event(event)

        assert event.event_type == tbm.INJECTION_RENDERED_EVENT
        assert event.causation_id == finalized_event.event_id
        assert parsed.usage_decision == result.usage_decision
        assert parsed.injection == result.injection
        assert parsed.replay_manifest_sha256 == result.manifest.manifest_sha256
        assert len(parsed.artifact_roles) == 9
        assert len(parsed.artifact_refs) == 9
        assert result.snippet not in str(event.payload)
        assert tbm.DEFAULT_EVENT_TYPE_REGISTRY.consume(event).payload == event.payload
        Draft202012Validator(
            tbm.finalization_event_payload_schema(event.event_type)
        ).validate(event.to_dict()["payload"])
    finally:
        stack.close()


def test_finalization_reducer_rebuilds_exact_decision_and_injection_views() -> None:
    stack, result, supporting, finalized_event, event = _built_event()
    try:
        reducer = tbm.build_finalization_reducer()
        state = reducer.initial_state()
        for source in (finalized_event, event):
            state = tbm.execute_reducer_step(
                reducer,
                state,
                tbm.ReducerEvent(
                    source,
                    tbm.DEFAULT_EVENT_TYPE_REGISTRY.consume(source),
                ),
            ).state

        authority = tbm.FinalizationProjectionAuthority(
            finalized_session=result.session,
            usage_decision=result.usage_decision,
            supporting_artifacts=supporting,
            injection=result.injection,
            injection_content=result.snippet.encode("utf-8"),
            manifest=result.manifest,
        )
        tbm.verify_finalization_projection_parity(
            state,
            (authority,),
            (finalized_event, event),
        )
        assert result.usage_decision.usage_decision_id in state["final_decisions"]
        assert result.injection.artifact.artifact_id in state["injections"]
        assert result.manifest.manifest_sha256 in state["replay_manifests"]
    finally:
        stack.close()


def test_finalization_reducer_rejects_missing_parent_and_projection_drift() -> None:
    stack, result, supporting, finalized_event, event = _built_event()
    try:
        reducer = tbm.build_finalization_reducer()
        with pytest.raises(tbm.ReducerExecutionError):
            tbm.execute_reducer_step(
                reducer,
                reducer.initial_state(),
                tbm.ReducerEvent(
                    event,
                    tbm.DEFAULT_EVENT_TYPE_REGISTRY.consume(event),
                ),
            )

        state = reducer.initial_state()
        for source in (finalized_event, event):
            state = tbm.execute_reducer_step(
                reducer,
                state,
                tbm.ReducerEvent(
                    source,
                    tbm.DEFAULT_EVENT_TYPE_REGISTRY.consume(source),
                ),
            ).state
        drift = _plain(state)
        assert type(drift) is dict
        drift["injections"][result.injection.artifact.artifact_id][
            "replay_manifest_sha256"
        ] = "sha256:" + "f" * 64
        authority = tbm.FinalizationProjectionAuthority(
            finalized_session=result.session,
            usage_decision=result.usage_decision,
            supporting_artifacts=supporting,
            injection=result.injection,
            injection_content=result.snippet.encode("utf-8"),
            manifest=result.manifest,
        )
        with pytest.raises(tbm.ReducerExecutionError):
            tbm.verify_finalization_projection_parity(
                drift,
                (authority,),
                (finalized_event, event),
            )
        with pytest.raises(tbm.ReducerExecutionError):
            tbm.verify_finalization_projection_parity(
                state,
                (authority, authority),
                (finalized_event, event),
            )
    finally:
        stack.close()


def test_injection_rendered_event_rejects_missing_artifact_and_parent_order() -> None:
    stack, result, supporting, finalized_event, _event = _built_event()
    try:
        decided, finalized, decided_event, retained_finalized_event = _gate_events(
            stack.connection,
            result.session.session_id,
        )
        with pytest.raises(tbm.FinalizationEventV1Error):
            tbm.build_injection_rendered_event(
                result.usage_decision,
                supporting[1:],
                result.injection,
                result.snippet.encode("utf-8"),
                result.manifest,
                decided_session=decided,
                finalized_session=finalized,
                decided_event=decided_event,
                finalized_event=retained_finalized_event,
                global_position=retained_finalized_event.global_position + 1,
                trusted_context=_trusted(finalized),
            )
        with pytest.raises(tbm.FinalizationEventV1Error):
            tbm.build_injection_rendered_event(
                result.usage_decision,
                supporting,
                result.injection,
                result.snippet.encode("utf-8"),
                result.manifest,
                decided_session=decided,
                finalized_session=finalized,
                decided_event=decided_event,
                finalized_event=retained_finalized_event,
                global_position=finalized_event.global_position,
                trusted_context=_trusted(finalized),
            )
    finally:
        stack.close()


def test_injection_rendered_parser_rejects_manifest_and_role_drift() -> None:
    stack, _result, _supporting, _finalized_event, event = _built_event()
    try:
        payload = event.to_dict()["payload"]
        assert type(payload) is dict
        manifest_drift = dict(payload)
        manifest_drift["replay_manifest_sha256"] = "sha256:" + "f" * 64
        with pytest.raises(tbm.FinalizationEventV1Error):
            tbm.parse_injection_rendered_event(
                _clone_with_payload(event, manifest_drift)
            )

        role_drift = dict(payload)
        roles = dict(role_drift["artifact_roles"])
        roles["injection_artifact"] = roles["usage_decision"]
        role_drift["artifact_roles"] = roles
        malformed = _clone_with_payload(event, role_drift)
        with pytest.raises(tbm.FinalizationEventV1Error):
            tbm.parse_injection_rendered_event(malformed)

        invalid_role = dict(payload)
        invalid_roles = dict(invalid_role["artifact_roles"])
        invalid_roles["injection_artifact"] = "artifact_generic"
        invalid_role["artifact_roles"] = invalid_roles
        with pytest.raises(tbm.EventRegistryV1Error):
            tbm.DEFAULT_EVENT_TYPE_REGISTRY.consume(
                _clone_with_payload(event, invalid_role)
            )
    finally:
        stack.close()
