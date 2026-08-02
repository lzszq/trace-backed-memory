from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

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
