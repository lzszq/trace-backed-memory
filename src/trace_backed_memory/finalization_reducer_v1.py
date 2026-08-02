from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import NoReturn, cast

from .event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from .event_v1 import CanonicalEvent
from .finalization_event_v1 import (
    INJECTION_RENDERED_EVENT,
    FinalizationEventRef,
    FinalizationEventV1Error,
    finalization_event_ref,
    parse_injection_rendered_event,
)
from .gate_session_event_v1 import (
    GATE_SESSION_EVENT_CONTRACT_VERSION,
    GATE_SESSION_EVENT_PRODUCER,
    GATE_SESSION_EVENT_PRODUCER_VERSION,
    GATE_SESSION_EVENT_RETENTION_POLICY_ID,
    GATE_SESSION_EVENT_STREAM_TYPE,
    GATE_SESSION_EVENT_VERSION,
    USAGE_DECISION_FINALIZED_EVENT,
    gate_session_event_id,
    gate_session_revision_sha256,
)
from .gate_session_v3 import GateSession, parse_gate_session
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerExecutionError,
)
from .replay_v3 import (
    DecisionReplayManifest,
    InjectionArtifact,
    StoredReplayArtifact,
    build_decision_replay_manifest,
)
from .usage_decision_v3 import UsageDecision, usage_decision_artifact_id


FINALIZATION_REDUCER_ID = "final-decision-injection"
FINALIZATION_PROJECTION_NAME = "final_decision_injection_v1"
FINALIZATION_PROJECTION_SCHEMA_VERSION = 1

_INPUT_EVENT_TYPES = tuple(
    sorted((USAGE_DECISION_FINALIZED_EVENT, INJECTION_RENDERED_EVENT))
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
class FinalizationProjectionAuthority:
    finalized_session: GateSession
    usage_decision: UsageDecision
    supporting_artifacts: tuple[StoredReplayArtifact, ...]
    injection: InjectionArtifact
    injection_content: bytes
    manifest: DecisionReplayManifest

    def __post_init__(self) -> None:
        if type(self.finalized_session) is not GateSession:
            _reject("finalized_session must be exactly GateSession")
        if type(self.usage_decision) is not UsageDecision:
            _reject("usage_decision must be exactly UsageDecision")
        if (
            type(self.supporting_artifacts) is not tuple
            or any(
                type(item) is not StoredReplayArtifact
                for item in self.supporting_artifacts
            )
        ):
            _reject("supporting_artifacts authority input is invalid")
        if type(self.injection) is not InjectionArtifact:
            _reject("injection must be exactly InjectionArtifact")
        if type(self.injection_content) is not bytes:
            _reject("injection_content must be bytes")
        if type(self.manifest) is not DecisionReplayManifest:
            _reject("manifest must be exactly DecisionReplayManifest")


def build_finalization_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=FINALIZATION_REDUCER_ID,
        reducer_version=1,
        input_event_types=_INPUT_EVENT_TYPES,
        output_projection=FINALIZATION_PROJECTION_NAME,
        output_schema_version=FINALIZATION_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "final-decision-injection",
                "algorithm_version": 1,
                "validation": [
                    "typed-payload",
                    "usage-decision-finalized-parent",
                    "trusted-event-scope",
                    "fresh-transition-authorization",
                    "complete-artifact-role-set",
                    "manifest-reconstruction",
                    "single-final-decision",
                    "single-injection",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={event_type: 1 for event_type in _INPUT_EVENT_TYPES},
    )

    def initial() -> Mapping[str, object]:
        return {
            "finalized_sessions": {},
            "usage_sessions": {},
            "injection_sessions": {},
            "final_decisions": {},
            "injections": {},
            "replay_manifests": {},
            "heads": {},
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        typed = reducer_event.typed_event
        source = reducer_event.source_event
        if typed is None:
            _reject("typed finalization projection input is required")
        if (
            typed.target_version != 1
            or typed.event_type != source.event_type
            or _plain_json(typed.payload) != _plain_json(source.payload)
        ):
            _reject("typed finalization projection input is invalid")
        finalized_sessions = _mapping_copy(
            state.get("finalized_sessions"),
            "finalized_sessions",
        )
        usage_sessions = _mapping_copy(
            state.get("usage_sessions"),
            "usage_sessions",
        )
        injection_sessions = _mapping_copy(
            state.get("injection_sessions"),
            "injection_sessions",
        )
        final_decisions = _mapping_copy(
            state.get("final_decisions"),
            "final_decisions",
        )
        injections = _mapping_copy(state.get("injections"), "injections")
        replay_manifests = _mapping_copy(
            state.get("replay_manifests"),
            "replay_manifests",
        )
        heads = _mapping_copy(state.get("heads"), "heads")

        if source.event_type == USAGE_DECISION_FINALIZED_EVENT:
            session = _parse_finalized_session_event(source)
            usage_decision_id_value = cast(str, session.usage_decision_id)
            injection_artifact_id = cast(str, session.injection_artifact_id)
            if (
                session.session_id in finalized_sessions
                or usage_decision_id_value in usage_sessions
                or injection_artifact_id in injection_sessions
            ):
                _reject("UsageDecisionFinalized appears more than once")
            finalized_sessions[session.session_id] = {
                "session": session.to_dict(),
                **_event_metadata(source),
            }
            usage_sessions[usage_decision_id_value] = session.session_id
            injection_sessions[injection_artifact_id] = session.session_id
        elif source.event_type == INJECTION_RENDERED_EVENT:
            try:
                record_ref = parse_injection_rendered_event(source)
            except FinalizationEventV1Error as error:
                raise ReducerExecutionError(
                    "TBM_FINALIZATION_REDUCER_EVENT_INVALID",
                    "InjectionRendered event cannot update the projection",
                ) from error
            usage_decision = record_ref.usage_decision
            injection = record_ref.injection
            session_id = usage_sessions.get(usage_decision.usage_decision_id)
            session_projection = _mapping_copy(
                finalized_sessions.get(session_id),
                "UsageDecisionFinalized parent",
            )
            session_payload = _mapping_copy(
                session_projection.get("session"),
                "UsageDecisionFinalized session",
            )
            session = _parse_session(session_payload)
            if (
                session_id != usage_decision.session_id
                or injection_sessions.get(injection.artifact.artifact_id)
                != session_id
                or session_projection.get("event_id")
                != record_ref.causation_event_id
                or type(session_projection.get("global_position")) is not int
                or cast(int, session_projection["global_position"])
                >= source.global_position
                or session_projection.get("authorization_decision_id")
                != source.authorization_decision_id
                or not _projection_scope_matches_event(
                    session_projection,
                    source,
                )
                or not _session_matches_record(session, record_ref)
            ):
                _reject("InjectionRendered has no exact finalized parent")
            if (
                usage_decision.usage_decision_id in final_decisions
                or injection.artifact.artifact_id in injections
                or record_ref.replay_manifest_sha256 in replay_manifests
                or source.stream_id in heads
            ):
                _reject("final decision or injection appears more than once")
            final_decisions[usage_decision.usage_decision_id] = (
                _final_decision_projection(record_ref, source)
            )
            injections[injection.artifact.artifact_id] = (
                _injection_projection(record_ref, source)
            )
            replay_manifests[record_ref.replay_manifest_sha256] = {
                "manifest": _manifest_from_ref(record_ref).to_dict(),
                **_event_metadata(source),
            }
            heads[source.stream_id] = {
                "event_id": source.event_id,
                "event_sha256": source.event_sha256,
                "global_position": source.global_position,
                "stream_version": source.stream_version,
                **_event_scope(source),
                "authorization_decision_id": source.authorization_decision_id,
            }
        else:
            _reject("finalization projection received an unrelated event")

        return {
            "finalized_sessions": finalized_sessions,
            "usage_sessions": usage_sessions,
            "injection_sessions": injection_sessions,
            "final_decisions": final_decisions,
            "injections": injections,
            "replay_manifests": replay_manifests,
            "heads": heads,
        }

    return FunctionalReducer(descriptor, initial, transition)


def verify_finalization_projection_parity(
    state: Mapping[str, object],
    authorities: tuple[FinalizationProjectionAuthority, ...],
    events: tuple[CanonicalEvent, ...],
) -> None:
    if (
        type(authorities) is not tuple
        or any(type(item) is not FinalizationProjectionAuthority for item in authorities)
    ):
        _reject("finalization parity authority input is invalid")
    session_ids = tuple(item.finalized_session.session_id for item in authorities)
    usage_ids = tuple(item.usage_decision.usage_decision_id for item in authorities)
    injection_ids = tuple(
        item.injection.artifact.artifact_id for item in authorities
    )
    manifest_ids = tuple(item.manifest.manifest_sha256 for item in authorities)
    for values in (session_ids, usage_ids, injection_ids, manifest_ids):
        if len(values) != len(set(values)):
            _reject("finalization parity authority input has duplicates")
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _reject("finalization parity event input is invalid")

    finalized_events: dict[str, tuple[GateSession, CanonicalEvent]] = {}
    injection_events: dict[str, tuple[FinalizationEventRef, CanonicalEvent]] = {}
    for event in events:
        if event.event_type == USAGE_DECISION_FINALIZED_EVENT:
            session = _parse_finalized_session_event(event)
            if session.session_id in finalized_events:
                _reject("finalization parity has duplicate finalized events")
            finalized_events[session.session_id] = (session, event)
        elif event.event_type == INJECTION_RENDERED_EVENT:
            try:
                record_ref = parse_injection_rendered_event(event)
            except FinalizationEventV1Error as error:
                raise ReducerExecutionError(
                    "TBM_FINALIZATION_REDUCER_EVENT_INVALID",
                    "finalization parity InjectionRendered event is invalid",
                ) from error
            usage_id = record_ref.usage_decision.usage_decision_id
            if usage_id in injection_events:
                _reject("finalization parity has duplicate injection events")
            injection_events[usage_id] = (record_ref, event)
        else:
            _reject("finalization parity contains an unrelated event")

    if set(finalized_events) != set(session_ids) or set(injection_events) != set(
        usage_ids
    ):
        _reject("finalization event identities differ from authority rows")

    expected_state = build_finalization_reducer().initial_state()
    ordered_events = sorted(events, key=lambda event: event.global_position)
    if len({event.global_position for event in ordered_events}) != len(ordered_events):
        _reject("finalization parity events have duplicate global positions")
    for event in ordered_events:
        expected_state = build_finalization_reducer().transition(
            expected_state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )

    for authority in authorities:
        finalized_session, finalized_event = finalized_events[
            authority.finalized_session.session_id
        ]
        if finalized_session != authority.finalized_session:
            _reject("finalized event differs from the exact GateSession row")
        event_ref, injection_event = injection_events[
            authority.usage_decision.usage_decision_id
        ]
        expected_ref = finalization_event_ref(
            authority.usage_decision,
            authority.supporting_artifacts,
            authority.injection,
            authority.injection_content,
            authority.manifest,
            causation_event_id=finalized_event.event_id,
        )
        if (
            event_ref != expected_ref
            or injection_event.causation_id != finalized_event.event_id
            or finalized_event.global_position >= injection_event.global_position
            or finalized_event.authorization_decision_id
            != injection_event.authorization_decision_id
            or not _same_event_scope(finalized_event, injection_event)
            or not _session_matches_record(finalized_session, event_ref)
        ):
            _reject("finalization event chain differs from authority rows")

    if _plain_json(state) != _plain_json(expected_state):
        _reject("finalization projection differs from events and authority rows")


def _parse_finalized_session_event(event: CanonicalEvent) -> GateSession:
    if (
        event.event_type != USAGE_DECISION_FINALIZED_EVENT
        or event.event_version != GATE_SESSION_EVENT_VERSION
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != GATE_SESSION_EVENT_STREAM_TYPE
        or event.payload_schema != f"{USAGE_DECISION_FINALIZED_EVENT}.v1"
        or event.classification != "internal"
        or event.retention_policy_id != GATE_SESSION_EVENT_RETENTION_POLICY_ID
        or event.artifact_refs
    ):
        _reject("UsageDecisionFinalized event envelope is invalid")
    payload = _mapping_copy(event.payload, "UsageDecisionFinalized payload")
    expected_fields = {
        "contract_version",
        "session",
        "session_sha256",
        "previous_session_sha256",
        "transition_authorization_event_id",
    }
    if (
        set(payload) != expected_fields
        or payload.get("contract_version")
        != GATE_SESSION_EVENT_CONTRACT_VERSION
    ):
        _reject("UsageDecisionFinalized payload is invalid")
    session = _parse_session(
        _mapping_copy(payload.get("session"), "UsageDecisionFinalized session")
    )
    if (
        session.status != "finalized"
        or session.usage_decision_id is None
        or session.injection_artifact_id is None
        or payload.get("session_sha256") != gate_session_revision_sha256(session)
        or payload.get("transition_authorization_event_id")
        != event.authorization_decision_id
        or event.event_id != gate_session_event_id(session)
        or event.stream_id != session.session_id
        or event.stream_version != session.version
        or event.causation_id is None
        or event.previous_stream_event_sha256 is None
        or event.occurred_at != session.updated_at
        or event.recorded_at != session.updated_at
        or event.producer != GATE_SESSION_EVENT_PRODUCER
        or event.producer_version != GATE_SESSION_EVENT_PRODUCER_VERSION
        or event.tenant_id != session.tenant_id
        or event.repository_id != session.repository_id
        or event.principal_id != session.principal_id
        or event.agent_client_id != session.agent_client_id
    ):
        _reject("UsageDecisionFinalized session linkage is invalid")
    return session


def _parse_session(payload: Mapping[str, object]) -> GateSession:
    try:
        return parse_gate_session(payload)
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_FINALIZATION_REDUCER_EVENT_INVALID",
            "UsageDecisionFinalized contains an invalid GateSession",
        ) from error


def _session_matches_record(
    session: GateSession,
    record_ref: FinalizationEventRef,
) -> bool:
    usage = record_ref.usage_decision
    injection = record_ref.injection
    return (
        session.session_id == usage.session_id
        and session.decision_id == usage.decision_id
        and session.trace_id == usage.trace_id
        and session.run_id == usage.run_id
        and session.retrieval_snapshot_id == usage.retrieval_snapshot_id
        and session.system_gate_evaluation_id == usage.system_gate_evaluation_id
        and bool(session.semantic_gate_attempt_ids)
        and session.semantic_gate_attempt_ids[-1]
        == usage.semantic_gate_attempt_id
        and session.final_memory_revision_ids == usage.final_memory_revision_ids
        and session.usage_decision_id == usage.usage_decision_id
        and session.injection_artifact_id == injection.artifact.artifact_id
    )


def _final_decision_projection(
    record_ref: FinalizationEventRef,
    event: CanonicalEvent,
) -> dict[str, object]:
    refs = {item.artifact_id: item for item in record_ref.artifact_refs}
    usage_artifact_ref = refs[
        usage_decision_artifact_id(
            record_ref.usage_decision.usage_decision_id
        )
    ]
    return {
        "usage_decision": record_ref.usage_decision.to_dict(),
        "usage_artifact_ref": usage_artifact_ref.to_dict(),
        "replay_manifest_sha256": record_ref.replay_manifest_sha256,
        **_event_metadata(event),
    }


def _injection_projection(
    record_ref: FinalizationEventRef,
    event: CanonicalEvent,
) -> dict[str, object]:
    injection_artifact_id = record_ref.injection.artifact.artifact_id
    refs = {item.artifact_id: item for item in record_ref.artifact_refs}
    return {
        "injection": record_ref.injection.to_dict(),
        "injection_artifact_ref": refs[injection_artifact_id].to_dict(),
        "replay_manifest_sha256": record_ref.replay_manifest_sha256,
        "artifact_roles": dict(record_ref.artifact_roles),
        "artifact_refs": [item.to_dict() for item in record_ref.artifact_refs],
        **_event_metadata(event),
    }


def _manifest_from_ref(
    record_ref: FinalizationEventRef,
) -> DecisionReplayManifest:
    usage = record_ref.usage_decision
    return build_decision_replay_manifest(
        session_id=usage.session_id,
        decision_id=usage.decision_id,
        usage_decision_id=usage.usage_decision_id,
        component_hashes=dict(usage.replay_components),
        injection_artifact_id=usage.injection_artifact_id,
        completeness="complete",
        created_at=usage.created_at,
    )


def _event_metadata(event: CanonicalEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_sha256": event.event_sha256,
        "global_position": event.global_position,
        "authorization_decision_id": event.authorization_decision_id,
        **_event_scope(event),
    }


def _event_scope(event: CanonicalEvent) -> dict[str, object]:
    return {name: getattr(event, name) for name in _EVENT_SCOPE_FIELDS}


def _projection_scope_matches_event(
    projection: Mapping[str, object],
    event: CanonicalEvent,
) -> bool:
    return all(
        projection.get(name) == getattr(event, name)
        for name in _EVENT_SCOPE_FIELDS
    )


def _same_event_scope(left: CanonicalEvent, right: CanonicalEvent) -> bool:
    return all(
        getattr(left, name) == getattr(right, name)
        for name in _EVENT_SCOPE_FIELDS
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
        "TBM_FINALIZATION_REDUCER_EVENT_INVALID",
        message,
    )


__all__ = [
    "FINALIZATION_PROJECTION_NAME",
    "FINALIZATION_PROJECTION_SCHEMA_VERSION",
    "FINALIZATION_REDUCER_ID",
    "FinalizationProjectionAuthority",
    "build_finalization_reducer",
    "verify_finalization_projection_parity",
]
