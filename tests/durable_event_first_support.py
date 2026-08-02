from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
import json
from pathlib import Path
from typing import Mapping, cast

from tests.test_durable_runtime_v3 import _Clock, _dependencies
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _Source,
    _candidate,
    _indexes,
    _record,
    _result,
)
from trace_backed_memory.contracts_v3 import canonical_sha256
from trace_backed_memory.artifact_service_v3 import AuthenticatedServiceContext
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeDependencies,
    DurableRuntimeFactory,
    DurableSQLiteRuntime,
)
from trace_backed_memory.gate_session_event_v1 import (
    gate_session_projection_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_FIXTURE = cast(
    dict[str, dict[str, object]],
    json.loads(
        (ROOT / "tests/fixtures/durable_client_lifecycle.json").read_text(
            encoding="utf-8"
        )
    ),
)
CONFORMANCE_REPORT_VERSION = "tbm.durable-event-first-conformance.v1"
EXPECTED_SESSION_ID = "gate_session_runtime_0001"


def event_first_dependencies(
    *,
    identifier_prefix: str | None = None,
    clock_advance_seconds: int = 0,
) -> tuple[
    DurableRuntimeDependencies,
    AuthenticatedServiceContext,
]:
    """Return fresh deterministic dependencies matching the shared fixture."""

    if type(clock_advance_seconds) is not int or clock_advance_seconds < 0:
        raise ValueError("clock_advance_seconds must be a non-negative integer")
    clock = _Clock()
    if clock_advance_seconds:
        clock.advance(seconds=clock_advance_seconds)
    dependencies, context = _dependencies(clock)
    candidate = _candidate("memory_durable_agent")
    source = _Source((candidate,))
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    dependencies = replace(
        dependencies,
        discovery=discovery,
        revision_source=source,
    )
    if identifier_prefix is not None:
        if (
            not identifier_prefix
            or not identifier_prefix.replace("_", "a").isalnum()
        ):
            raise ValueError("identifier_prefix must be a safe identifier")
        request_numbers = iter(range(1, 10_000))
        session_numbers = iter(range(1, 10_000))
        dependencies = replace(
            dependencies,
            authorization_request_id_factory=lambda: (
                f"authorization_{identifier_prefix}_"
                f"{next(request_numbers):04d}"
            ),
            session_id_factory=lambda: (
                f"gate_session_{identifier_prefix}_"
                f"{next(session_numbers):04d}"
            ),
        )
    return dependencies, context


def open_event_first_runtime(
    database: Path,
    *,
    initialize: bool = True,
    identifier_prefix: str | None = None,
    clock_advance_seconds: int = 0,
) -> tuple[DurableSQLiteRuntime, AuthenticatedServiceContext]:
    dependencies, context = event_first_dependencies(
        identifier_prefix=identifier_prefix,
        clock_advance_seconds=clock_advance_seconds,
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=initialize,
        expose_injection_content=True,
        expose_replay_content=True,
        event_first_commands=True,
        check_same_thread=False,
    )
    return runtime, context


def event_first_report(
    runtime: DurableSQLiteRuntime,
    session_id: str = EXPECTED_SESSION_ID,
) -> dict[str, object]:
    """Return adapter-neutral event order and hydrated projection evidence."""

    session = runtime.sessions.get(session_id)
    rebuilt_session = runtime.gate_session_event_projector.rebuild_current(
        session
    )
    if rebuilt_session != session:
        raise AssertionError("GateSession event projection drifted")
    evidence = runtime.gate_evidence_event_projector.rebuild_current(session)
    outcome_projector = runtime.outcome_effect_event_projector
    if outcome_projector is None:
        raise AssertionError("event-first Outcome/Effect projector is missing")
    outcome_effect = outcome_projector.rebuild_current(session)

    projection = {
        "gate_session": _json_value(rebuilt_session),
        "gate_evidence": _json_value(evidence),
        "outcome_effect": _json_value(outcome_effect),
    }
    return {
        "report_version": CONFORMANCE_REPORT_VERSION,
        "session_id": session.session_id,
        "gate_session_projection_sha256": gate_session_projection_sha256(
            session
        ),
        "projection_sha256": canonical_sha256(projection),
        "streams": {
            "gate_session": _event_sequence(
                runtime.gate_session_event_projector.read_events(session)
            ),
            "gate_evidence": _event_sequence(
                runtime.gate_evidence_event_projector.read_events(session)
            ),
            "outcome_effect": _event_sequence(
                outcome_projector.read_events(session)
            ),
        },
    }


def _event_sequence(events: tuple[object, ...]) -> list[dict[str, object]]:
    return [
        {
            "global_position": getattr(event, "global_position"),
            "stream_version": getattr(event, "stream_version"),
            "event_type": getattr(event, "event_type"),
            "event_sha256": getattr(event, "event_sha256"),
        }
        for event in events
    ]


def _json_value(value: object) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
        }
    raise TypeError(f"unsupported projection value: {type(value).__name__}")


__all__ = [
    "CONFORMANCE_REPORT_VERSION",
    "EXPECTED_SESSION_ID",
    "LIFECYCLE_FIXTURE",
    "event_first_dependencies",
    "event_first_report",
    "open_event_first_runtime",
]
