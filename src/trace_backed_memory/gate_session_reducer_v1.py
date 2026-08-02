from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import NoReturn, cast

from .gate_session_event_v1 import (
    GATE_SESSION_EVENT_TYPES,
    GateSessionEventV1Error,
    parse_gate_session_event,
)
from .gate_session_v3 import GateSession, parse_gate_session
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerExecutionError,
)


GATE_SESSION_REDUCER_ID = "gate-session-current"
GATE_SESSION_PROJECTION_NAME = "gate_session_current_v1"
GATE_SESSION_PROJECTION_SCHEMA_VERSION = 1


def build_gate_session_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=GATE_SESSION_REDUCER_ID,
        reducer_version=1,
        input_event_types=GATE_SESSION_EVENT_TYPES,
        output_projection=GATE_SESSION_PROJECTION_NAME,
        output_schema_version=GATE_SESSION_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "gate-session-current",
                "algorithm_version": 1,
                "validation": [
                    "typed-payload",
                    "trusted-context",
                    "stream-version",
                    "revision-digest",
                    "exact-domain-transition",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={event_type: 1 for event_type in GATE_SESSION_EVENT_TYPES},
    )

    def initial() -> Mapping[str, object]:
        return {"sessions": {}, "heads": {}}

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        typed = reducer_event.typed_event
        if typed is None:
            _reject("typed GateSession event is required")
        if typed.target_version != 1 or typed.event_type != reducer_event.source_event.event_type:
            _reject("typed GateSession event version is invalid")
        if _thaw_json(typed.payload) != _thaw_json(reducer_event.source_event.payload):
            _reject("typed GateSession payload differs from its source event")
        sessions = _mapping_copy(state.get("sessions"), "sessions")
        heads = _mapping_copy(state.get("heads"), "heads")
        source = reducer_event.source_event
        raw_previous = sessions.get(source.stream_id)
        previous = None
        if raw_previous is not None:
            try:
                previous = parse_gate_session(
                    cast(dict[str, object], _thaw_json(raw_previous))
                )
            except Exception as error:
                raise ReducerExecutionError(
                    "TBM_GATE_SESSION_REDUCER_STATE_INVALID",
                    "retained GateSession projection state is invalid",
                ) from error
        try:
            session = parse_gate_session_event(
                source,
                previous_session=previous,
            )
        except GateSessionEventV1Error as error:
            raise ReducerExecutionError(
                "TBM_GATE_SESSION_REDUCER_EVENT_INVALID",
                "GateSession event cannot update the projection",
            ) from error
        if source.stream_id in heads:
            raw_head = heads[source.stream_id]
            if not isinstance(raw_head, Mapping):
                _reject("retained GateSession projection head is invalid")
            expected_head_fields = {
                "session_version",
                "event_id",
                "event_sha256",
                "global_position",
                "organization_id",
                "tenant_id",
                "repository_id",
                "environment_id",
                "authorization_decision_id",
            }
            if (
                set(raw_head) != expected_head_fields
                or raw_head.get("session_version") != source.stream_version - 1
                or raw_head.get("event_sha256")
                != source.previous_stream_event_sha256
                or raw_head.get("event_id") != source.causation_id
                or type(raw_head.get("global_position")) is not int
                or cast(int, raw_head["global_position"])
                >= source.global_position
                or raw_head.get("organization_id") != source.organization_id
                or raw_head.get("tenant_id") != source.tenant_id
                or raw_head.get("repository_id") != source.repository_id
                or raw_head.get("environment_id") != source.environment_id
            ):
                _reject("GateSession event does not extend the retained head")
        elif source.stream_version != 1:
            _reject("GateSession projection cannot start after revision 1")
        sessions[source.stream_id] = session.to_dict()
        heads[source.stream_id] = {
            "session_version": session.version,
            "event_id": source.event_id,
            "event_sha256": source.event_sha256,
            "global_position": source.global_position,
            "organization_id": source.organization_id,
            "tenant_id": source.tenant_id,
            "repository_id": source.repository_id,
            "environment_id": source.environment_id,
            "authorization_decision_id": source.authorization_decision_id,
        }
        return {"sessions": sessions, "heads": heads}

    return FunctionalReducer(descriptor, initial, transition)


def projected_gate_session(
    state: Mapping[str, object],
    session_id: str,
) -> GateSession:
    sessions = state.get("sessions")
    if not isinstance(sessions, Mapping) or session_id not in sessions:
        _reject("GateSession is absent from projection state")
    payload = _thaw_json(sessions[session_id])
    if type(payload) is not dict:
        _reject("projected GateSession payload is invalid")
    try:
        return parse_gate_session(cast(dict[str, object], payload))
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_GATE_SESSION_REDUCER_STATE_INVALID",
            "projected GateSession payload is invalid",
        ) from error


def verify_gate_session_projection_parity(
    state: Mapping[str, object],
    sessions: tuple[GateSession, ...],
) -> None:
    if type(sessions) is not tuple or any(
        type(session) is not GateSession for session in sessions
    ):
        _reject("GateSession parity input is invalid")
    expected = {session.session_id: session for session in sessions}
    projected = state.get("sessions")
    if not isinstance(projected, Mapping) or set(projected) != set(expected):
        _reject("GateSession projection identity set differs from authority rows")
    for session_id, session in expected.items():
        if projected_gate_session(state, session_id) != session:
            _reject("GateSession projection differs from authority row")


def _mapping_copy(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject(f"{name} projection state is invalid")
    copied = _thaw_json(value)
    if type(copied) is not dict:
        _reject(f"{name} projection state is invalid")
    return cast(dict[str, object], copied)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_thaw_json(item) for item in value]
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
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _reject(message: str) -> NoReturn:
    raise ReducerExecutionError(
        "TBM_GATE_SESSION_REDUCER_EVENT_INVALID",
        message,
    )


__all__ = [
    "GATE_SESSION_PROJECTION_NAME",
    "GATE_SESSION_PROJECTION_SCHEMA_VERSION",
    "GATE_SESSION_REDUCER_ID",
    "build_gate_session_reducer",
    "projected_gate_session",
    "verify_gate_session_projection_parity",
]
