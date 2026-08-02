from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Callable

import pytest

from tests.durable_crash_matrix_child import CRASH_EXIT_CODE, STAGES
from tests.durable_event_first_support import (
    event_first_report,
    open_event_first_runtime,
)
from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_semantic_gate_v3 import _context as _provider_context
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableStartRequest,
)
from trace_backed_memory.gate_session_v3 import GateSession


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class _Stage:
    name: str
    prior_status: str | None
    committed_status: str


MATRIX = (
    _Stage("auth", None, "prepared"),
    _Stage("created", None, "prepared"),
    _Stage("retrieval_evidence", None, "prepared"),
    _Stage("prepared", None, "prepared"),
    _Stage("provider_call", "prepared", "decided"),
    _Stage("decided", "prepared", "decided"),
    _Stage("replay_retention", "decided", "finalized"),
    _Stage("finalized", "decided", "finalized"),
    _Stage("executing", "finalized", "executing"),
    _Stage("outcome", "executing", "completed"),
    _Stage("outbox", "executing", "completed"),
)


def _advance_to(
    runtime: object,
    context: object,
    target: str | None,
) -> GateSession | None:
    if target is None:
        return None
    dispatcher = getattr(runtime, "dispatcher")
    sessions = getattr(runtime, "sessions")
    evidence = getattr(runtime, "evidence_repository")
    prepared_response = dispatcher.prepare(context, _prepare_request())
    session_id = prepared_response["result"]["session"]["session_id"]
    prepared = sessions.get(session_id)
    if target == "prepared":
        return prepared
    evaluation = evidence.load_evaluation(
        prepared.system_gate_evaluation_id
    )
    dispatcher.decide(
        context,
        _provider_context(),
        _decide_request(prepared, evaluation),
    )
    decided = sessions.get(session_id)
    if target == "decided":
        return decided
    dispatcher.finalize(
        context,
        DurableFinalizeRequest(
            session_id=session_id,
            expected_session_version=decided.version,
        ),
    )
    finalized = sessions.get(session_id)
    if target == "finalized":
        return finalized
    dispatcher.start(
        context,
        DurableStartRequest(
            session_id=session_id,
            expected_session_version=finalized.version,
        ),
    )
    executing = sessions.get(session_id)
    if target == "executing":
        return executing
    raise AssertionError(f"unsupported lifecycle target: {target}")


def _request_for(
    runtime: object,
    stage: _Stage,
    prior: GateSession | None,
) -> tuple[str, object]:
    if prior is None:
        return "prepare", _prepare_request()
    if stage.name in {"provider_call", "decided"}:
        evaluation = getattr(runtime, "evidence_repository").load_evaluation(
            prior.system_gate_evaluation_id
        )
        return "decide", _decide_request(prior, evaluation)
    if stage.name in {"replay_retention", "finalized"}:
        return (
            "finalize",
            DurableFinalizeRequest(
                session_id=prior.session_id,
                expected_session_version=prior.version,
            ),
        )
    if stage.name == "executing":
        return (
            "start",
            DurableStartRequest(
                session_id=prior.session_id,
                expected_session_version=prior.version,
            ),
        )
    if stage.name in {"outcome", "outbox"}:
        completion = _completion(prior)
        return (
            "complete",
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(
                    completion.evidence_artifact_sha256s
                ),
                output_sha256=completion.output_sha256,
                tool_outputs_sha256=completion.tool_outputs_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
                error_code=completion.error_code,
            ),
        )
    raise AssertionError(f"unsupported crash stage: {stage.name}")


def _execute(
    runtime: object,
    context: object,
    operation: str,
    request: object,
) -> dict[str, object]:
    dispatcher = getattr(runtime, "dispatcher")
    if operation == "decide":
        return dispatcher.decide(context, _provider_context(), request)
    if operation == "complete":
        return dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            request,
        )
    return getattr(dispatcher, operation)(context, request)


def _database_snapshot(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == (
            "ok",
        )
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        )
        return {
            table: tuple(
                sorted(
                    connection.execute(
                        f'SELECT * FROM "{table}"'
                    ).fetchall(),
                    key=repr,
                )
            )
            for table in tables
        }
    finally:
        connection.close()


def _single_session_id(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT session_id FROM gate_session_heads ORDER BY session_id"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    return rows[0][0]


def _run_child(
    database: Path,
    stage: _Stage,
    mode: str,
    session_id: str | None,
) -> None:
    command = [
        sys.executable,
        "-m",
        "tests.durable_crash_matrix_child",
        "--database",
        str(database),
        "--stage",
        stage.name,
        "--mode",
        mode,
    ]
    if session_id is not None:
        command.extend(("--session-id", session_id))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == CRASH_EXIT_CODE, (
        completed.stdout,
        completed.stderr,
    )


def _with_runtime(
    database: Path,
    prefix: str,
    callback: Callable[[object, object], None],
) -> None:
    runtime, context = open_event_first_runtime(
        database,
        initialize=False,
        identifier_prefix=prefix,
        clock_advance_seconds=20,
    )
    try:
        callback(runtime, context)
    finally:
        runtime.close()


@pytest.mark.parametrize("stage", MATRIX, ids=lambda item: item.name)
@pytest.mark.parametrize("mode", ("precommit", "response_lost"))
def test_event_first_crash_matrix_has_exact_known_recovery(
    tmp_path: Path,
    stage: _Stage,
    mode: str,
) -> None:
    assert tuple(item.name for item in MATRIX) == STAGES
    database = tmp_path / f"{mode}-{stage.name}.sqlite3"
    runtime, context = open_event_first_runtime(
        database,
        identifier_prefix=f"setup_{mode}_{stage.name}",
    )
    try:
        prior = _advance_to(runtime, context, stage.prior_status)
        operation, request = _request_for(runtime, stage, prior)
        session_id = None if prior is None else prior.session_id
    finally:
        runtime.close()

    before = _database_snapshot(database)
    _run_child(database, stage, mode, session_id)

    if mode == "precommit":
        # A kill before the outer commit has one known result: the exact
        # previously committed prefix, with no event/authority partial state.
        assert _database_snapshot(database) == before

        def recover(runtime: object, context: object) -> None:
            response = _execute(runtime, context, operation, request)
            recovered_id = response["result"]["session"]["session_id"]
            recovered = getattr(runtime, "sessions").get(recovered_id)
            assert recovered.status == stage.committed_status
            report = event_first_report(runtime, recovered_id)
            _execute(runtime, context, operation, request)
            assert event_first_report(runtime, recovered_id) == report

        _with_runtime(
            database,
            f"recover_{stage.name}",
            recover,
        )
        return

    # The command committed and the process died before its response arrived.
    # Reopen must expose the acknowledged event prefix, and replaying the
    # original exact request must not create a second logical transition.
    committed_id = session_id or _single_session_id(database)

    def replay(runtime: object, context: object) -> None:
        committed = getattr(runtime, "sessions").get(committed_id)
        assert committed.status == stage.committed_status
        report = event_first_report(runtime, committed_id)
        _execute(runtime, context, operation, request)
        assert getattr(runtime, "sessions").get(committed_id) == committed
        assert event_first_report(runtime, committed_id) == report

    _with_runtime(
        database,
        f"replay_{stage.name}",
        replay,
    )
