from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import NoReturn, cast

from .event_v1 import CanonicalEvent
from .gate_evidence_event_v1 import (
    SYSTEM_GATE_EVALUATED_EVENT,
    GateEvidenceEventV1Error,
    GateEvidenceRecordRef,
    parse_gate_evidence_event,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerExecutionError,
)
from .semantic_gate_artifact_v3 import StoredSemanticGateAttemptArtifacts
from .semantic_gate_attempt_event_v1 import (
    SEMANTIC_GATE_ATTEMPT_EVENT_TYPES,
    SemanticGateAttemptEventRef,
    SemanticGateAttemptEventV1Error,
    parse_semantic_gate_attempt_event,
    semantic_gate_attempt_event_ref,
)


SEMANTIC_GATE_ATTEMPT_REDUCER_ID = "semantic-gate-attempt-chain"
SEMANTIC_GATE_ATTEMPT_PROJECTION_NAME = "semantic_gate_attempt_chain_v1"
SEMANTIC_GATE_ATTEMPT_PROJECTION_SCHEMA_VERSION = 1

_INPUT_EVENT_TYPES = tuple(
    sorted((*SEMANTIC_GATE_ATTEMPT_EVENT_TYPES, SYSTEM_GATE_EVALUATED_EVENT))
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


def build_semantic_gate_attempt_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=SEMANTIC_GATE_ATTEMPT_REDUCER_ID,
        reducer_version=1,
        input_event_types=_INPUT_EVENT_TYPES,
        output_projection=SEMANTIC_GATE_ATTEMPT_PROJECTION_NAME,
        output_schema_version=SEMANTIC_GATE_ATTEMPT_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "semantic-gate-attempt-chain",
                "algorithm_version": 2,
                "validation": [
                    "typed-payload",
                    "retained-system-gate-parent",
                    "trusted-event-scope",
                    "artifact-reference",
                    "attempt-sequence",
                    "parent-causation",
                    "previous-event-hash",
                    "single-current-head",
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
            "system_gate_parents": {},
            "attempts": {},
            "streams": {},
            "heads": {},
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        typed = reducer_event.typed_event
        if typed is None:
            _reject("typed Semantic Gate projection input is required")
        if (
            typed.target_version != 1
            or typed.event_type != reducer_event.source_event.event_type
            or _plain_json(typed.payload)
            != _plain_json(reducer_event.source_event.payload)
        ):
            _reject("typed Semantic Gate projection input is invalid")
        system_parents = _mapping_copy(
            state.get("system_gate_parents"),
            "system_gate_parents",
        )
        attempts = _mapping_copy(state.get("attempts"), "attempts")
        streams = _mapping_copy(state.get("streams"), "streams")
        heads = _mapping_copy(state.get("heads"), "heads")
        source = reducer_event.source_event

        if source.event_type == SYSTEM_GATE_EVALUATED_EVENT:
            try:
                system_ref = parse_gate_evidence_event(source)
            except GateEvidenceEventV1Error as error:
                raise ReducerExecutionError(
                    "TBM_SEMANTIC_GATE_ATTEMPT_REDUCER_EVENT_INVALID",
                    "System Gate parent event cannot update the projection",
                ) from error
            if (
                system_ref.evidence_kind != "system_gate_evaluation"
                or system_ref.record_id in system_parents
            ):
                _reject("System Gate parent appears more than once")
            system_parents[system_ref.record_id] = _system_parent_projection(
                system_ref,
                source,
            )
            return {
                "system_gate_parents": system_parents,
                "attempts": attempts,
                "streams": streams,
                "heads": heads,
            }

        try:
            record_ref = parse_semantic_gate_attempt_event(source)
        except SemanticGateAttemptEventV1Error as error:
            raise ReducerExecutionError(
                "TBM_SEMANTIC_GATE_ATTEMPT_REDUCER_EVENT_INVALID",
                "Semantic Gate attempt event cannot update the projection",
            ) from error
        if record_ref.attempt_id in attempts:
            _reject("Semantic Gate attempt appears more than once")
        raw_stream = streams.get(record_ref.stream_id)
        if record_ref.sequence == 1:
            parent = _mapping_copy(
                system_parents.get(record_ref.system_gate_evaluation_id),
                "System Gate parent",
            )
            if (
                raw_stream is not None
                or record_ref.stream_id in heads
                or parent.get("session_id") != record_ref.session_id
                or parent.get("retrieval_snapshot_id")
                != record_ref.retrieval_snapshot_id
                or parent.get("event_id") != record_ref.causation_event_id
                or type(parent.get("global_position")) is not int
                or cast(int, parent["global_position"]) >= source.global_position
                or not _projection_scope_matches_event(parent, source)
            ):
                _reject("first Semantic Gate attempt has no exact System Gate parent")
        else:
            stream = _mapping_copy(raw_stream, "Semantic Gate attempt stream")
            head = _mapping_copy(
                heads.get(record_ref.stream_id),
                "Semantic Gate attempt head",
            )
            if (
                stream.get("session_id") != record_ref.session_id
                or stream.get("retrieval_snapshot_id")
                != record_ref.retrieval_snapshot_id
                or stream.get("system_gate_evaluation_id")
                != record_ref.system_gate_evaluation_id
                or stream.get("current_sequence") != record_ref.sequence - 1
                or stream.get("current_attempt_id")
                != record_ref.previous_attempt_id
                or head.get("event_id") != record_ref.causation_event_id
                or head.get("event_sha256")
                != source.previous_stream_event_sha256
                or type(head.get("global_position")) is not int
                or cast(int, head["global_position"]) >= source.global_position
                or not _projection_scope_matches_event(stream, source)
            ):
                _reject("Semantic Gate retry does not extend the current head")

        attempts[record_ref.attempt_id] = {
            **record_ref.to_projection_dict(),
            **_event_metadata(source),
        }
        streams[record_ref.stream_id] = {
            "session_id": record_ref.session_id,
            "retrieval_snapshot_id": record_ref.retrieval_snapshot_id,
            "system_gate_evaluation_id": record_ref.system_gate_evaluation_id,
            "current_sequence": record_ref.sequence,
            "current_attempt_id": record_ref.attempt_id,
            **_event_scope(source),
            "authorization_decision_id": source.authorization_decision_id,
        }
        heads[record_ref.stream_id] = {
            "event_id": source.event_id,
            "event_sha256": source.event_sha256,
            "global_position": source.global_position,
            "stream_version": source.stream_version,
            **_event_scope(source),
            "authorization_decision_id": source.authorization_decision_id,
        }
        return {
            "system_gate_parents": system_parents,
            "attempts": attempts,
            "streams": streams,
            "heads": heads,
        }

    return FunctionalReducer(descriptor, initial, transition)


def verify_semantic_gate_attempt_projection_parity(
    state: Mapping[str, object],
    bundles: tuple[StoredSemanticGateAttemptArtifacts, ...],
    events: tuple[CanonicalEvent, ...],
) -> None:
    if type(bundles) is not tuple or any(
        type(bundle) is not StoredSemanticGateAttemptArtifacts
        for bundle in bundles
    ):
        _reject("Semantic Gate attempt parity authority input is invalid")
    attempt_ids = tuple(bundle.attempt.attempt_id for bundle in bundles)
    if len(attempt_ids) != len(set(attempt_ids)):
        _reject("Semantic Gate attempt parity authority input has duplicates")
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _reject("Semantic Gate attempt parity event input is invalid")
    projected_parents = state.get("system_gate_parents")
    projected_attempts = state.get("attempts")
    projected_streams = state.get("streams")
    projected_heads = state.get("heads")
    if not all(
        isinstance(value, Mapping)
        for value in (
            projected_parents,
            projected_attempts,
            projected_streams,
            projected_heads,
        )
    ):
        _reject("Semantic Gate attempt projection state is invalid")

    system_events: dict[str, tuple[GateEvidenceRecordRef, CanonicalEvent]] = {}
    attempt_events: dict[
        str,
        tuple[SemanticGateAttemptEventRef, CanonicalEvent],
    ] = {}
    for event in events:
        if event.event_type == SYSTEM_GATE_EVALUATED_EVENT:
            try:
                system_ref = parse_gate_evidence_event(event)
            except GateEvidenceEventV1Error as error:
                raise ReducerExecutionError(
                    "TBM_SEMANTIC_GATE_ATTEMPT_REDUCER_EVENT_INVALID",
                    "Semantic Gate parity System Gate event is invalid",
                ) from error
            if system_ref.record_id in system_events:
                _reject("Semantic Gate parity has duplicate System Gate events")
            system_events[system_ref.record_id] = (system_ref, event)
        elif event.event_type in SEMANTIC_GATE_ATTEMPT_EVENT_TYPES:
            try:
                attempt_ref = parse_semantic_gate_attempt_event(event)
            except SemanticGateAttemptEventV1Error as error:
                raise ReducerExecutionError(
                    "TBM_SEMANTIC_GATE_ATTEMPT_REDUCER_EVENT_INVALID",
                    "Semantic Gate parity attempt event is invalid",
                ) from error
            if attempt_ref.attempt_id in attempt_events:
                _reject("Semantic Gate parity has duplicate attempt events")
            attempt_events[attempt_ref.attempt_id] = (attempt_ref, event)
        else:
            _reject("Semantic Gate parity event input contains an unrelated event")

    expected_refs = {
        bundle.attempt.attempt_id: semantic_gate_attempt_event_ref(
            bundle.attempt,
            bundle.prompt,
            bundle.response,
        )
        for bundle in bundles
    }
    if set(attempt_events) != set(expected_refs):
        _reject("Semantic Gate event identities differ from authority rows")
    expected_parent_projection = {
        record_id: _system_parent_projection(system_ref, event)
        for record_id, (system_ref, event) in system_events.items()
    }
    if _plain_json(projected_parents) != expected_parent_projection:
        _reject("System Gate parent projection differs from canonical events")

    expected_attempt_projection: dict[str, object] = {}
    grouped: dict[
        str,
        list[tuple[SemanticGateAttemptEventRef, CanonicalEvent]],
    ] = {}
    for attempt_id, expected_ref in expected_refs.items():
        event_ref, event = attempt_events[attempt_id]
        if event_ref != expected_ref:
            _reject("Semantic Gate event differs from exact authority rows")
        if expected_ref.system_gate_evaluation_id not in system_events:
            _reject("Semantic Gate event has no retained System Gate parent")
        expected_attempt_projection[attempt_id] = {
            **expected_ref.to_projection_dict(),
            **_event_metadata(event),
        }
        grouped.setdefault(expected_ref.stream_id, []).append(
            (expected_ref, event)
        )
    if _plain_json(projected_attempts) != expected_attempt_projection:
        _reject("Semantic Gate attempt projection differs from events and rows")

    expected_streams: dict[str, object] = {}
    expected_heads: dict[str, object] = {}
    for stream_id, chain in grouped.items():
        chain.sort(key=lambda value: value[0].sequence)
        if [value[0].sequence for value in chain] != list(
            range(1, len(chain) + 1)
        ):
            _reject("Semantic Gate authority rows do not form a complete chain")
        parent_ref, parent_event = system_events[
            chain[0][0].system_gate_evaluation_id
        ]
        previous_event: CanonicalEvent | None = None
        for attempt_ref, event in chain:
            if attempt_ref.sequence == 1:
                if (
                    event.causation_id != parent_event.event_id
                    or parent_ref.session_id != attempt_ref.session_id
                    or parent_ref.retrieval_snapshot_id
                    != attempt_ref.retrieval_snapshot_id
                    or parent_event.global_position >= event.global_position
                    or not _same_event_scope(parent_event, event)
                ):
                    _reject("Semantic Gate first event parent linkage is invalid")
            elif (
                previous_event is None
                or event.causation_id != previous_event.event_id
                or event.previous_stream_event_sha256
                != previous_event.event_sha256
                or previous_event.global_position >= event.global_position
                or not _same_event_scope(previous_event, event)
            ):
                _reject("Semantic Gate retry event linkage is invalid")
            previous_event = event
        last_ref, last_event = chain[-1]
        expected_streams[stream_id] = {
            "session_id": last_ref.session_id,
            "retrieval_snapshot_id": last_ref.retrieval_snapshot_id,
            "system_gate_evaluation_id": last_ref.system_gate_evaluation_id,
            "current_sequence": last_ref.sequence,
            "current_attempt_id": last_ref.attempt_id,
            **_event_scope(last_event),
            "authorization_decision_id": last_event.authorization_decision_id,
        }
        expected_heads[stream_id] = {
            "event_id": last_event.event_id,
            "event_sha256": last_event.event_sha256,
            "global_position": last_event.global_position,
            "stream_version": last_event.stream_version,
            **_event_scope(last_event),
            "authorization_decision_id": last_event.authorization_decision_id,
        }
    if _plain_json(projected_streams) != expected_streams:
        _reject("Semantic Gate current streams differ from canonical events")
    if _plain_json(projected_heads) != expected_heads:
        _reject("Semantic Gate current heads differ from canonical events")


def _system_parent_projection(
    record_ref: GateEvidenceRecordRef,
    event: CanonicalEvent,
) -> dict[str, object]:
    return {
        **record_ref.to_projection_dict(),
        "event_id": event.event_id,
        "event_sha256": event.event_sha256,
        "global_position": event.global_position,
        **_event_scope(event),
        "authorization_decision_id": event.authorization_decision_id,
    }


def _event_metadata(event: CanonicalEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_sha256": event.event_sha256,
        "global_position": event.global_position,
        "authorization_decision_id": event.authorization_decision_id,
        "previous_stream_event_sha256": event.previous_stream_event_sha256,
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
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _reject(message: str) -> NoReturn:
    raise ReducerExecutionError(
        "TBM_SEMANTIC_GATE_ATTEMPT_REDUCER_EVENT_INVALID",
        message,
    )


__all__ = [
    "SEMANTIC_GATE_ATTEMPT_PROJECTION_NAME",
    "SEMANTIC_GATE_ATTEMPT_PROJECTION_SCHEMA_VERSION",
    "SEMANTIC_GATE_ATTEMPT_REDUCER_ID",
    "build_semantic_gate_attempt_reducer",
    "verify_semantic_gate_attempt_projection_parity",
]
