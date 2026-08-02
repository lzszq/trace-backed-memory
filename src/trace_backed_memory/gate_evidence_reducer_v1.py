from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import NoReturn, cast

from .gate_evaluation_v3 import SystemGateEvaluation
from .gate_evidence_event_v1 import (
    GATE_EVIDENCE_EVENT_TYPES,
    RETRIEVAL_PREPARED_EVENT,
    SYSTEM_GATE_EVALUATED_EVENT,
    GateEvidenceEventV1Error,
    GateEvidenceRecordRef,
    gate_evidence_event_id,
    parse_gate_evidence_event,
    retrieval_snapshot_record_ref,
    system_gate_evaluation_record_ref,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerExecutionError,
)
from .retrieval_v3 import RetrievalSnapshot


GATE_EVIDENCE_REDUCER_ID = "gate-evidence-current"
GATE_EVIDENCE_PROJECTION_NAME = "gate_evidence_current_v1"
GATE_EVIDENCE_PROJECTION_SCHEMA_VERSION = 1

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def build_gate_evidence_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=GATE_EVIDENCE_REDUCER_ID,
        reducer_version=1,
        input_event_types=GATE_EVIDENCE_EVENT_TYPES,
        output_projection=GATE_EVIDENCE_PROJECTION_NAME,
        output_schema_version=GATE_EVIDENCE_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "gate-evidence-current",
                "algorithm_version": 1,
                "validation": [
                    "typed-payload",
                    "artifact-reference",
                    "authorization-linkage",
                    "retrieval-before-system-gate",
                    "session-current-linkage",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={event_type: 1 for event_type in GATE_EVIDENCE_EVENT_TYPES},
    )

    def initial() -> Mapping[str, object]:
        return {
            "retrieval_snapshots": {},
            "system_gate_evaluations": {},
            "sessions": {},
            "heads": {},
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        typed = reducer_event.typed_event
        if typed is None:
            _reject("typed Gate evidence event is required")
        if (
            typed.target_version != 1
            or typed.event_type != reducer_event.source_event.event_type
            or _plain_json(typed.payload)
            != _plain_json(reducer_event.source_event.payload)
        ):
            _reject("typed Gate evidence event is invalid")
        try:
            record_ref = parse_gate_evidence_event(reducer_event.source_event)
        except GateEvidenceEventV1Error as error:
            raise ReducerExecutionError(
                "TBM_GATE_EVIDENCE_REDUCER_EVENT_INVALID",
                "Gate evidence event cannot update the projection",
            ) from error
        retrieval = _mapping_copy(state.get("retrieval_snapshots"), "retrieval_snapshots")
        system_gate = _mapping_copy(
            state.get("system_gate_evaluations"),
            "system_gate_evaluations",
        )
        sessions = _mapping_copy(state.get("sessions"), "sessions")
        heads = _mapping_copy(state.get("heads"), "heads")
        source = reducer_event.source_event
        if source.stream_id in heads:
            _reject("Gate evidence stream appears more than once")
        raw_session = sessions.get(record_ref.session_id)
        session = (
            {
                "authorization_event_id": record_ref.authorization_event_id,
                "retrieval_snapshot_id": None,
                "system_gate_evaluation_id": None,
            }
            if raw_session is None
            else _mapping_copy(raw_session, "session current view")
        )
        if session.get("authorization_event_id") != record_ref.authorization_event_id:
            _reject("Gate evidence session authorization changed")

        if source.event_type == RETRIEVAL_PREPARED_EVENT:
            if (
                record_ref.evidence_kind != "retrieval_snapshot"
                or record_ref.record_id in retrieval
                or record_ref.causation_event_id is not None
                or session.get("retrieval_snapshot_id") is not None
                or session.get("system_gate_evaluation_id") is not None
            ):
                _reject("retrieval evidence conflicts with the current session view")
            retrieval[record_ref.record_id] = record_ref.to_projection_dict()
            session["retrieval_snapshot_id"] = record_ref.record_id
        elif source.event_type == SYSTEM_GATE_EVALUATED_EVENT:
            if (
                record_ref.evidence_kind != "system_gate_evaluation"
                or record_ref.record_id in system_gate
                or record_ref.retrieval_snapshot_id not in retrieval
                or session.get("retrieval_snapshot_id")
                != record_ref.retrieval_snapshot_id
                or session.get("system_gate_evaluation_id") is not None
            ):
                _reject("System Gate evidence conflicts with retrieval evidence")
            retrieval_ref = _mapping_copy(
                retrieval[cast(str, record_ref.retrieval_snapshot_id)],
                "retrieval evidence record",
            )
            retrieval_head = _mapping_copy(
                heads.get(cast(str, record_ref.retrieval_snapshot_id)),
                "retrieval evidence head",
            )
            if (
                retrieval_ref.get("session_id") != record_ref.session_id
                or retrieval_ref.get("authorization_event_id")
                != record_ref.authorization_event_id
                or retrieval_head.get("event_id") != record_ref.causation_event_id
            ):
                _reject("System Gate evidence causation is invalid")
            system_gate[record_ref.record_id] = record_ref.to_projection_dict()
            session["system_gate_evaluation_id"] = record_ref.record_id
        else:
            _reject("Gate evidence event type is invalid")

        sessions[record_ref.session_id] = session
        heads[source.stream_id] = {
            "event_id": source.event_id,
            "event_sha256": source.event_sha256,
            "global_position": source.global_position,
            "authorization_decision_id": source.authorization_decision_id,
        }
        return {
            "retrieval_snapshots": retrieval,
            "system_gate_evaluations": system_gate,
            "sessions": sessions,
            "heads": heads,
        }

    return FunctionalReducer(descriptor, initial, transition)


def verify_gate_evidence_projection_parity(
    state: Mapping[str, object],
    snapshots: tuple[RetrievalSnapshot, ...],
    evaluations: tuple[SystemGateEvaluation, ...],
) -> None:
    if type(snapshots) is not tuple or any(
        type(snapshot) is not RetrievalSnapshot for snapshot in snapshots
    ):
        _reject("retrieval snapshot parity input is invalid")
    if type(evaluations) is not tuple or any(
        type(evaluation) is not SystemGateEvaluation for evaluation in evaluations
    ):
        _reject("System Gate parity input is invalid")
    projected_retrieval = state.get("retrieval_snapshots")
    projected_system = state.get("system_gate_evaluations")
    heads = state.get("heads")
    sessions = state.get("sessions")
    if not all(
        isinstance(value, Mapping)
        for value in (projected_retrieval, projected_system, heads, sessions)
    ):
        _reject("Gate evidence projection state is invalid")
    expected_retrieval = {
        snapshot.snapshot_id: (
            retrieval_snapshot_record_ref(snapshot).to_projection_dict()
        )
        for snapshot in snapshots
    }
    if _plain_json(projected_retrieval) != expected_retrieval:
        _reject("retrieval evidence projection differs from authority rows")
    expected_system: dict[str, object] = {}
    for evaluation in evaluations:
        retrieval_head = heads.get(evaluation.retrieval_snapshot_id)  # type: ignore[union-attr]
        if not isinstance(retrieval_head, Mapping):
            _reject("retrieval evidence head is absent from projection")
        event_id = retrieval_head.get("event_id")
        if type(event_id) is not str:
            _reject("retrieval evidence head event ID is invalid")
        expected_system[evaluation.evaluation_id] = (
            system_gate_evaluation_record_ref(
                evaluation,
                causation_event_id=event_id,
            ).to_projection_dict()
        )
    if _plain_json(projected_system) != expected_system:
        _reject("System Gate projection differs from authority rows")
    expected_sessions: dict[str, object] = {}
    for snapshot in snapshots:
        expected_sessions[snapshot.session_id] = {
            "authorization_event_id": snapshot.authorization_event_id,
            "retrieval_snapshot_id": snapshot.snapshot_id,
            "system_gate_evaluation_id": None,
        }
    for evaluation in evaluations:
        current = expected_sessions.get(evaluation.session_id)
        if not isinstance(current, dict):
            _reject("System Gate authority row has no retrieval parent")
        if (
            current["authorization_event_id"] != evaluation.authorization_event_id
            or current["retrieval_snapshot_id"] != evaluation.retrieval_snapshot_id
        ):
            _reject("Gate evidence authority scope differs")
        current["system_gate_evaluation_id"] = evaluation.evaluation_id
    if _plain_json(sessions) != expected_sessions:
        _reject("Gate evidence session view differs from authority rows")
    expected_records: dict[str, GateEvidenceRecordRef] = {
        snapshot.snapshot_id: retrieval_snapshot_record_ref(snapshot)
        for snapshot in snapshots
    }
    for evaluation in evaluations:
        expected_records[evaluation.evaluation_id] = (
            system_gate_evaluation_record_ref(
                evaluation,
                causation_event_id=gate_evidence_event_id(
                    RETRIEVAL_PREPARED_EVENT,
                    evaluation.retrieval_snapshot_id,
                ),
            )
        )
    if set(heads) != set(expected_records):  # type: ignore[arg-type]
        _reject("Gate evidence projection head identities differ")
    positions: dict[str, int] = {}
    for record_id, record_ref in expected_records.items():
        head = heads.get(record_id)  # type: ignore[union-attr]
        if not isinstance(head, Mapping):
            _reject("Gate evidence projection head is invalid")
        expected_type = (
            RETRIEVAL_PREPARED_EVENT
            if record_ref.evidence_kind == "retrieval_snapshot"
            else SYSTEM_GATE_EVALUATED_EVENT
        )
        global_position = head.get("global_position")
        if (
            set(head)
            != {
                "event_id",
                "event_sha256",
                "global_position",
                "authorization_decision_id",
            }
            or head.get("event_id")
            != gate_evidence_event_id(expected_type, record_id)
            or type(head.get("event_sha256")) is not str
            or _DIGEST_RE.fullmatch(cast(str, head["event_sha256"])) is None
            or type(global_position) is not int
            or cast(int, global_position) <= 0
            or head.get("authorization_decision_id")
            != record_ref.authorization_event_id
        ):
            _reject("Gate evidence projection head differs from event contract")
        positions[record_id] = cast(int, global_position)
    for evaluation in evaluations:
        if (
            positions[evaluation.retrieval_snapshot_id]
            >= positions[evaluation.evaluation_id]
        ):
            _reject("System Gate projection does not follow retrieval evidence")


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
        "TBM_GATE_EVIDENCE_REDUCER_EVENT_INVALID",
        message,
    )


__all__ = [
    "GATE_EVIDENCE_PROJECTION_NAME",
    "GATE_EVIDENCE_PROJECTION_SCHEMA_VERSION",
    "GATE_EVIDENCE_REDUCER_ID",
    "build_gate_evidence_reducer",
    "verify_gate_evidence_projection_parity",
]
