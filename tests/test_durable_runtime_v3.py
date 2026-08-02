from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_artifact_service_v3 import (
    _context as _service_context,
    _registry,
)
from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import (
    EVALUATOR,
    EVALUATOR_CONTEXT,
    _authenticate_evaluator,
)
from tests.test_durable_retrieval_preparation_v3 import _durable_request
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
)
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _Source,
    _candidate,
    _indexes,
    _policy,
    _record,
    _result,
)
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableGetSessionRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeDependencies,
    DurableRuntimeFactory,
    DurableRuntimeV3Error,
)
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import loads_canonical_event
from trace_backed_memory.reducer import ReducerEvent, execute_reducer_step


ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(milliseconds=125)
        return value.isoformat().replace("+00:00", "Z")

    def advance(self, *, seconds: int) -> None:
        self._next += timedelta(seconds=seconds)


def _dependencies(
    clock: _Clock,
    *,
    completion_consumer=None,
) -> tuple[
    DurableRuntimeDependencies,
    tbm.AuthenticatedServiceContext,
]:
    registry = _registry(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    context = _service_context(registry)
    policy = _policy()
    candidate = _candidate("memory_durable_runtime")
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
    repository_id = _durable_request().context.repository_id
    request_numbers = iter(range(1, 10_000))
    session_numbers = iter(range(1, 10_000))
    dependencies = DurableRuntimeDependencies(
        registry_provider=lambda: registry,
        policy_provider=lambda: policy,
        discovery=discovery,
        revision_source=source,
        semantic_provider=tbm.TrustedSemanticProvider(
            provider_id="provider_openai",
            authenticator_id="authenticator_oidc",
            credential_id="credential_prod_01",
            model_id="model_gate",
            model_version="2026-07-01",
            endpoint_id="endpoint_primary",
        ),
        semantic_configuration=tbm.SemanticGateServiceConfiguration(
            prompt_template_id="semantic_gate_default",
            prompt_template_version="v1",
            generation_config_sha256="sha256:" + "3" * 64,
            response_media_type="application/json",
        ),
        evaluator_authenticator=_authenticate_evaluator,
        repository_id_resolver=lambda _context: repository_id,
        clock=clock,
        authorization_request_id_factory=lambda: (
            f"authorization_runtime_{next(request_numbers):04d}"
        ),
        session_id_factory=lambda: (
            f"gate_session_runtime_{next(session_numbers):04d}"
        ),
        completion_consumer=completion_consumer,
    )
    return dependencies, context


def _run_prepare_crash_probe(database: str, checkpoint: str) -> None:
    targets = {
        "authorization": "INSERT INTO V3_AUTHORIZATION_DECISIONS",
        "created": "INSERT INTO GATE_SESSION_HEADS",
        "evidence": "INSERT INTO V3_SYSTEM_GATE_EVALUATIONS",
    }
    target = targets[checkpoint]
    dependencies, context = _dependencies(_Clock())
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    target_seen = False
    commit_seen = False

    def trace(statement: str) -> None:
        nonlocal target_seen, commit_seen
        normalized = " ".join(statement.upper().split())
        if commit_seen:
            os.kill(os.getpid(), signal.SIGKILL)
        if target in normalized:
            target_seen = True
        elif target_seen and normalized == "COMMIT":
            commit_seen = True

    runtime._connection.set_trace_callback(trace)
    runtime.dispatcher.prepare(context, _prepare_request())
    raise RuntimeError("prepare crash checkpoint was not reached")


def _run_finalization_crash_probe(
    database: str,
    session_id: str,
    expected_session_version: str,
) -> None:
    clock = _Clock()
    clock.advance(seconds=60)
    dependencies, context = _dependencies(clock)
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            "authorization_runtime_finalization_crash"
        ),
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    manifest_inserted = False

    def trace(statement: str) -> None:
        nonlocal manifest_inserted
        if manifest_inserted:
            os.kill(os.getpid(), signal.SIGKILL)
        normalized = " ".join(statement.upper().split())
        if "INSERT INTO V3_REPLAY_MANIFESTS" in normalized:
            manifest_inserted = True

    runtime._connection.set_trace_callback(trace)
    runtime.dispatcher.finalize(
        context,
        DurableFinalizeRequest(
            session_id=session_id,
            expected_session_version=int(expected_session_version),
        ),
    )
    raise RuntimeError("finalization crash checkpoint was not reached")


def _run_completion_crash_probe(
    database: str,
    session_id: str,
) -> None:
    clock = _Clock()
    clock.advance(seconds=120)
    dependencies, context = _dependencies(clock)
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            "authorization_runtime_completion_crash"
        ),
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    executing = runtime.sessions.get(session_id)
    completion = _completion(executing)
    outbox_inserted = False

    def trace(statement: str) -> None:
        nonlocal outbox_inserted
        if outbox_inserted:
            os.kill(os.getpid(), signal.SIGKILL)
        normalized = " ".join(statement.upper().split())
        if "INSERT INTO V3_COMPLETION_OUTBOX_EVENTS" in normalized:
            outbox_inserted = True

    runtime._connection.set_trace_callback(trace)
    runtime.dispatcher.complete(
        context,
        EVALUATOR_CONTEXT,
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
    raise RuntimeError("completion crash checkpoint was not reached")


def _run_outbox_ack_crash_probe(
    database: str,
    delivery_file: str,
) -> None:
    clock = _Clock()
    clock.advance(seconds=300)
    consumer_returned = False

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        nonlocal consumer_returned
        Path(delivery_file).write_text(event.event_id, encoding="utf-8")
        consumer_returned = True
        return tbm.CompletionOutboxConsumerReceipt(
            response_sha256="sha256:" + "a" * 64
        )

    dependencies, _context = _dependencies(
        clock,
        completion_consumer=consume,
    )
    runtime = DurableRuntimeFactory(dependencies).open_sqlite(database)
    acknowledgement_inserted = False

    def trace(statement: str) -> None:
        nonlocal acknowledgement_inserted
        if acknowledgement_inserted:
            os.kill(os.getpid(), signal.SIGKILL)
        normalized = " ".join(statement.upper().split())
        if (
            consumer_returned
            and "INSERT INTO V3_COMPLETION_OUTBOX_DELIVERY_REVISIONS"
            in normalized
        ):
            acknowledgement_inserted = True

    runtime._connection.set_trace_callback(trace)
    runtime.deliver_outbox(
        worker_id="worker_completion_ack_crash",
        lease_seconds=1,
        limit=1,
    )
    raise RuntimeError("outbox acknowledgement crash checkpoint was not reached")


def test_durable_sqlite_runtime_builds_one_restart_safe_authority_graph(
    tmp_path: Path,
) -> None:
    delivered: list[tbm.CompletionOutboxEvent] = []

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivered.append(event)
        return tbm.CompletionOutboxConsumerReceipt(
            response_sha256="sha256:" + "f" * 64
        )

    database = tmp_path / "durable-runtime.sqlite3"
    clock = _Clock()
    dependencies, context = _dependencies(
        clock,
        completion_consumer=consume,
    )
    factory = DurableRuntimeFactory(dependencies)
    runtime = factory.open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        assert runtime.sessions is runtime.outbox_repository.gate_sessions
        assert runtime.agent.service_bundle is runtime.service_bundle
        assert runtime.service_bundle.authority_graph is runtime.authority_graph
        assert runtime.authority_graph.authorization_service is (
            runtime.authorization_service
        )
        assert runtime.dispatcher.capabilities()["durable_sessions"] is True

        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = runtime.sessions.get(decided.session_id)
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        executing = runtime.sessions.get(finalized.session_id)
        completion = _completion(executing)
        completed_response = runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(
                    completion.evidence_artifact_sha256s
                ),
                output_sha256=completion.output_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
            ),
        )
        completed = runtime.sessions.get(executing.session_id)
        assert completed_response["result"]["session"]["status"] == "completed"

        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (completed.session_id,),
        ).fetchall()
        events = tuple(loads_canonical_event(row[0]) for row in event_rows)
        assert tuple(event.event_type for event in events) == (
            tbm.GATE_SESSION_CREATED_EVENT,
            tbm.GATE_SESSION_PREPARED_EVENT,
            tbm.SEMANTIC_GATE_REQUESTED_EVENT,
            tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
            tbm.SEMANTIC_GATE_DECIDED_EVENT,
            tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
            tbm.USAGE_DECISION_FINALIZED_EVENT,
            tbm.EXECUTION_STARTED_EVENT,
            tbm.GATE_SESSION_COMPLETED_EVENT,
        )
        authorization_ids = tuple(
            event.authorization_decision_id for event in events
        )
        assert authorization_ids[0] == authorization_ids[1]
        assert authorization_ids[2] == authorization_ids[3] == authorization_ids[4]
        assert authorization_ids[5] == authorization_ids[6]
        assert len(
            {
                authorization_ids[0],
                authorization_ids[2],
                authorization_ids[5],
                authorization_ids[7],
                authorization_ids[8],
            }
        ) == 5
        reducer = tbm.build_gate_session_reducer()
        state = reducer.initial_state()
        for event in events:
            state = execute_reducer_step(
                reducer,
                state,
                ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
            ).state
        tbm.verify_gate_session_projection_parity(state, (completed,))

        snapshot = runtime.evidence_repository.load_snapshot(
            completed.retrieval_snapshot_id
        )
        evidence_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id IN (?, ?) ORDER BY global_position",
            (
                snapshot.snapshot_id,
                evaluation.evaluation_id,
            ),
        ).fetchall()
        evidence_events = tuple(
            loads_canonical_event(row[0]) for row in evidence_rows
        )
        assert tuple(event.event_type for event in evidence_events) == (
            tbm.RETRIEVAL_PREPARED_EVENT,
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
        )
        evidence_reducer = tbm.build_gate_evidence_reducer()
        evidence_state = evidence_reducer.initial_state()
        for event in evidence_events:
            evidence_state = execute_reducer_step(
                evidence_reducer,
                evidence_state,
                ReducerEvent(
                    event,
                    DEFAULT_EVENT_TYPE_REGISTRY.consume(event),
                ),
            ).state
        tbm.verify_gate_evidence_projection_parity(
            evidence_state,
            (snapshot,),
            (evaluation,),
        )

        semantic_bundles = tuple(
            runtime.semantic_repository.load_attempt_with_artifacts(
                attempt_id
            )
            for attempt_id in completed.semantic_gate_attempt_ids
        )
        semantic_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (
                tbm.semantic_gate_attempt_stream_id(
                    evaluation.evaluation_id
                ),
            ),
        ).fetchall()
        semantic_events = tuple(
            loads_canonical_event(row[0]) for row in semantic_rows
        )
        assert tuple(event.event_type for event in semantic_events) == (
            tbm.SEMANTIC_GATE_ATTEMPT_SUCCEEDED_EVENT,
        )
        assert semantic_events[0].causation_id == tbm.gate_evidence_event_id(
            tbm.SYSTEM_GATE_EVALUATED_EVENT,
            evaluation.evaluation_id,
        )
        assert semantic_events[0].authorization_decision_id == (
            authorization_ids[2]
        )
        semantic_reducer = tbm.build_semantic_gate_attempt_reducer()
        semantic_state = semantic_reducer.initial_state()
        for event in (evidence_events[1], *semantic_events):
            semantic_state = execute_reducer_step(
                semantic_reducer,
                semantic_state,
                ReducerEvent(
                    event,
                    DEFAULT_EVENT_TYPE_REGISTRY.consume(event),
                ),
            ).state
        tbm.verify_semantic_gate_attempt_projection_parity(
            semantic_state,
            semantic_bundles,
            (evidence_events[1], *semantic_events),
        )

        deliveries = runtime.deliver_outbox(worker_id="worker_runtime_01")
        assert len(deliveries) == 1
        assert deliveries[0].outcome == "delivered"
        assert [event.event_id for event in delivered] == [
            completed_response["result"]["outbox_event"]["event_id"]
        ]
        session_id = completed.session_id
        session_version = completed.version
    finally:
        runtime.close()

    with pytest.raises(DurableRuntimeV3Error) as raised:
        runtime.dispatcher.capabilities()
    assert raised.value.code == "TBM_DURABLE_RUNTIME_CLOSED"

    reopened = factory.open_sqlite(
        database,
        initialize=False,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        loaded = reopened.dispatcher.get_session(
            context,
            DurableGetSessionRequest(session_id=session_id),
        )
        assert loaded["result"]["session"]["version"] == session_version
        assert loaded["result"]["session"]["status"] == "completed"
        assert reopened.deliver_outbox(worker_id="worker_runtime_02") == ()
    finally:
        reopened.close()


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
@pytest.mark.parametrize(
    ("checkpoint", "expected_session_count", "expected_evidence_count"),
    [
        ("authorization", 0, 0),
        ("created", 1, 0),
        ("evidence", 1, 1),
    ],
)
def test_durable_sqlite_prepare_recovers_after_committed_crash_boundaries(
    tmp_path: Path,
    checkpoint: str,
    expected_session_count: int,
    expected_evidence_count: int,
) -> None:
    database = tmp_path / f"prepare-crash-{checkpoint}.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
    ):
        pass

    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_prepare_crash_probe; "
        "_run_prepare_crash_probe(sys.argv[1], sys.argv[2])"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(database), checkpoint],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    recovery_clock = _Clock()
    recovery_clock.advance(seconds=30)
    dependencies, context = _dependencies(recovery_clock)
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            f"authorization_runtime_prepare_recovery_{checkpoint}"
        ),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(database) as runtime:
        connection = runtime._connection
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_authorization_decisions"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM gate_session_heads"
        ).fetchone() == (expected_session_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_retrieval_snapshots"
        ).fetchone() == (expected_evidence_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_system_gate_evaluations"
        ).fetchone() == (expected_evidence_count,)

        response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        session = runtime.sessions.get(
            response["result"]["session"]["session_id"]
        )
        assert session.status == "prepared"
        assert [
            revision.status
            for revision in runtime.sessions.history(session.session_id)
        ] == ["created", "prepared"]
        assert connection.execute(
            "SELECT COUNT(*) FROM gate_session_heads"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_retrieval_snapshots"
        ).fetchone() == ((2 if checkpoint == "evidence" else 1),)
        assert connection.execute(
            "SELECT COUNT(*) FROM v3_system_gate_evaluations"
        ).fetchone() == ((2 if checkpoint == "evidence" else 1),)
        authorization_rows = connection.execute(
            "SELECT authorization_event_id FROM v3_authorization_decisions "
            "ORDER BY decided_at, authorization_event_id"
        ).fetchall()
        assert len(authorization_rows) == 2
        snapshot_rows = connection.execute(
            "SELECT snapshot_id, authorization_event_id "
            "FROM v3_retrieval_snapshots ORDER BY snapshot_id"
        ).fetchall()
        snapshot_authorizations = dict(snapshot_rows)
        assert session.retrieval_snapshot_id in snapshot_authorizations
        if checkpoint == "evidence":
            assert len(frozenset(snapshot_authorizations.values())) == 2
        assert snapshot_authorizations[session.retrieval_snapshot_id] in {
            row[0] for row in authorization_rows
        }

        event_rows = connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "WHERE stream_id = ? ORDER BY stream_version",
            (session.session_id,),
        ).fetchall()
        events = tuple(
            loads_canonical_event(row[0]) for row in event_rows
        )
        assert tuple(event.event_type for event in events) == (
            tbm.GATE_SESSION_CREATED_EVENT,
            tbm.GATE_SESSION_PREPARED_EVENT,
        )


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
def test_durable_sqlite_finalization_recovers_after_replay_transaction_crash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "finalization-replay-crash.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        finalize_request = DurableFinalizeRequest(
            session_id=decided.session_id,
            expected_session_version=decided.version,
        )

    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_finalization_crash_probe; "
        "_run_finalization_crash_probe(sys.argv[1], sys.argv[2], sys.argv[3])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            decided.session_id,
            str(decided.version),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    clock = _Clock()
    clock.advance(seconds=120)
    dependencies, context = _dependencies(clock)
    recovery_request_ids = iter(
        (
            "authorization_runtime_finalization_recovery_001",
            "authorization_runtime_finalization_recovery_002",
        )
    )
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            next(recovery_request_ids)
        ),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        current = runtime.sessions.get(decided.session_id)
        assert current.status == "decided"
        assert current.version == decided.version + 1
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_artifacts"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_injections"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_manifests"
        ).fetchone() == (0,)

        recovered = runtime.dispatcher.finalize(
            context,
            finalize_request,
        )
        assert recovered["result"]["session"]["status"] == "finalized"
        assert recovered["result"]["replayed"] is False
        replayed = runtime.dispatcher.finalize(
            context,
            finalize_request,
        )
        assert replayed["result"]["replayed"] is True
        assert replayed["result"]["manifest"] == recovered["result"][
            "manifest"
        ]
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_injections"
        ).fetchone() == (1,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_replay_manifests"
        ).fetchone() == (1,)

        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "ORDER BY global_position"
        ).fetchall()
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        assert event_types.count(tbm.USAGE_DECISION_FINALIZED_EVENT) == 1
        assert event_types.count(tbm.INJECTION_RENDERED_EVENT) == 1


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="hard-crash transaction probes require SIGKILL",
)
def test_durable_sqlite_completion_rolls_back_partial_outbox_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion-outbox-crash.sqlite3"
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = runtime.sessions.get(decided.session_id)
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        executing = runtime.sessions.get(finalized.session_id)
        completion = _completion(executing)
        complete_request = DurableCompleteRequest(
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
        )

    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_completion_crash_probe; "
        "_run_completion_crash_probe(sys.argv[1], sys.argv[2])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            executing.session_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr

    clock = _Clock()
    clock.advance(seconds=180)
    dependencies, context = _dependencies(clock)
    recovery_request_ids = iter(
        (
            "authorization_runtime_completion_recovery_001",
            "authorization_runtime_completion_recovery_002",
        )
    )
    dependencies = replace(
        dependencies,
        authorization_request_id_factory=lambda: (
            next(recovery_request_ids)
        ),
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(database) as runtime:
        current = runtime.sessions.get(executing.session_id)
        assert current == executing
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_run_outcomes"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_completion_outbox_events"
        ).fetchone() == (0,)

        completed = runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            complete_request,
        )
        assert completed["result"]["session"]["status"] == "completed"
        assert completed["result"]["replayed"] is False
        replayed = runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            complete_request,
        )
        assert replayed["result"]["replayed"] is True
        assert replayed["result"]["outcome"] == completed["result"][
            "outcome"
        ]
        assert replayed["result"]["outbox_event"] == completed["result"][
            "outbox_event"
        ]
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_run_outcomes"
        ).fetchone() == (1,)
        assert runtime._connection.execute(
            "SELECT COUNT(*) FROM v3_completion_outbox_events"
        ).fetchone() == (1,)

        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "ORDER BY global_position"
        ).fetchall()
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        assert event_types.count(tbm.EVALUATION_AUTHENTICATED_EVENT) == 1
        assert event_types.count(tbm.RUN_OUTCOME_RECORDED_EVENT) == 1
        assert event_types.count(tbm.GATE_SESSION_COMPLETED_EVENT) == 1
        assert event_types.count(tbm.EFFECT_REQUESTED_EVENT) == 1
        event_id = completed["result"]["outbox_event"]["event_id"]

    delivery_file = tmp_path / "completion-consumer-before-ack.txt"
    code = (
        "import sys; "
        "from tests.test_durable_runtime_v3 import "
        "_run_outbox_ack_crash_probe; "
        "_run_outbox_ack_crash_probe(sys.argv[1], sys.argv[2])"
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(database),
            str(delivery_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    assert delivery_file.read_text(encoding="utf-8") == event_id

    redelivered: list[str] = []

    def consume_reclaimed(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        redelivered.append(event.event_id)
        return tbm.CompletionOutboxConsumerReceipt(
            response_sha256="sha256:" + "b" * 64
        )

    clock = _Clock()
    clock.advance(seconds=360)
    dependencies, _context = _dependencies(
        clock,
        completion_consumer=consume_reclaimed,
    )
    with DurableRuntimeFactory(dependencies).open_sqlite(database) as runtime:
        leased = runtime.outbox_repository.get_delivery(event_id)
        assert leased.status == "leased"
        assert leased.attempt_count == 1
        results = runtime.deliver_outbox(
            worker_id="worker_completion_ack_recovery",
            lease_seconds=1,
            limit=1,
        )
        assert len(results) == 1
        assert results[0].outcome == "delivered"
        assert redelivered == [event_id]
        history = runtime.outbox_repository.list_delivery_history(event_id)
        assert tuple(item.status for item in history) == (
            "pending",
            "leased",
            "leased",
            "delivered",
        )
        event_rows = runtime._connection.execute(
            "SELECT canonical_event FROM v3_event_ledger_events "
            "ORDER BY global_position"
        ).fetchall()
        event_types = tuple(
            loads_canonical_event(row[0]).event_type for row in event_rows
        )
        assert event_types.count(tbm.EFFECT_REQUESTED_EVENT) == 1
        assert event_types.count(tbm.EFFECT_STARTED_EVENT) == 2
        assert event_types.count(tbm.EFFECT_SUCCEEDED_EVENT) == 1


def test_durable_sqlite_runtime_recovery_worker_expires_due_preparation() -> None:
    clock = _Clock()
    dependencies, context = _dependencies(clock)
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        payload = _prepare_request().model_dump()
        payload["expires_in_seconds"] = 300
        prepared_response = runtime.dispatcher.prepare(
            context,
            tbm.durable_agent_wire_v1.DurablePrepareRequest.model_validate(
                payload
            ),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        assert prepared.status == "prepared"

        clock.advance(seconds=600)
        recovered = runtime.recover_due(limit=10)

        assert len(recovered) == 1
        assert recovered[0].session_id == prepared.session_id
        assert recovered[0].outcome == "expired"
        assert recovered[0].current.status == "expired"


def test_durable_sqlite_runtime_fails_closed_on_missing_schema(
    tmp_path: Path,
) -> None:
    dependencies, _context = _dependencies(_Clock())
    with pytest.raises(DurableRuntimeV3Error) as raised:
        DurableRuntimeFactory(dependencies).open_sqlite(
            tmp_path / "missing-schema.sqlite3",
            initialize=False,
        )
    assert raised.value.code == (
        "TBM_DURABLE_RUNTIME_SQLITE_SCHEMA_INVALID"
    )


def test_durable_sqlite_runtime_requires_explicit_outbox_consumer() -> None:
    dependencies, _context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        with pytest.raises(DurableRuntimeV3Error) as raised:
            runtime.deliver_outbox(worker_id="worker_missing_consumer")
        assert raised.value.code == (
            "TBM_DURABLE_RUNTIME_OUTBOX_CONSUMER_MISSING"
        )


def test_durable_sqlite_runtime_dependency_guards() -> None:
    dependencies, _context = _dependencies(_Clock())
    with pytest.raises(TypeError):
        DurableRuntimeDependencies(
            **{
                **dependencies.__dict__,
                "repository_id_resolver": None,
            }
        )
    with pytest.raises(TypeError):
        DurableRuntimeFactory(object())  # type: ignore[arg-type]

    assert EVALUATOR.status == "active"


def test_durable_postgres_runtime_parity_and_catalog_verification(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    postgres_cluster.load_schema()
    for script in (
        "postgres-v3-authorization.sql",
        "postgres-v3-gate-session.sql",
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
        "postgres-v3-replay.sql",
        "postgres-v3-outcome.sql",
        "postgres-v3-completion-outbox.sql",
        "postgres-v3-event-ledger.sql",
    ):
        installed = postgres_cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr

    delivered: list[tbm.CompletionOutboxEvent] = []

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivered.append(event)
        return tbm.CompletionOutboxConsumerReceipt()

    dependencies, context = _dependencies(
        _Clock(),
        completion_consumer=consume,
    )
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        runtime = DurableRuntimeFactory(dependencies).bind_postgres(
            connection,
            expose_injection_content=True,
        )
        try:
            assert connection.info.transaction_status.name == "IDLE"
            assert runtime.sessions is runtime.outbox_repository.gate_sessions
            assert runtime.dispatcher.capabilities()["storage_mode"] == (
                "postgres"
            )

            prepared_response = runtime.dispatcher.prepare(
                context,
                _prepare_request(),
            )
            prepared = runtime.sessions.get(
                prepared_response["result"]["session"]["session_id"]
            )
            evaluation = runtime.evidence_repository.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            runtime.dispatcher.decide(
                context,
                _provider_context(),
                _decide_request(prepared, evaluation),
            )
            decided = runtime.sessions.get(prepared.session_id)
            runtime.dispatcher.finalize(
                context,
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                ),
            )
            finalized = runtime.sessions.get(decided.session_id)
            runtime.dispatcher.start(
                context,
                DurableStartRequest(
                    session_id=finalized.session_id,
                    expected_session_version=finalized.version,
                ),
            )
            executing = runtime.sessions.get(finalized.session_id)
            completion = _completion(executing)
            completed = runtime.dispatcher.complete(
                context,
                EVALUATOR_CONTEXT,
                DurableCompleteRequest(
                    session_id=completion.session_id,
                    expected_session_version=completion.expected_version,
                    result=completion.result,
                    evidence_artifact_sha256s=list(
                        completion.evidence_artifact_sha256s
                    ),
                    output_sha256=completion.output_sha256,
                    latency_ms=completion.latency_ms,
                    cost_usd=completion.cost_usd,
                ),
            )
            assert completed["result"]["session"]["status"] == "completed"

            results = runtime.deliver_outbox(
                worker_id="worker_postgres_runtime"
            )
            assert len(results) == 1
            assert results[0].outcome == "delivered"
            assert [event.event_id for event in delivered] == [
                completed["result"]["outbox_event"]["event_id"]
            ]
        finally:
            runtime.close()

    dependencies, _context = _dependencies(_Clock())
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DROP SCHEMA trace_backed_memory_v3_replay CASCADE"
            )
        connection.commit()
        with pytest.raises(DurableRuntimeV3Error) as raised:
            DurableRuntimeFactory(dependencies).bind_postgres(connection)
        assert raised.value.code == (
            "TBM_DURABLE_RUNTIME_POSTGRES_SCHEMA_INVALID"
        )
