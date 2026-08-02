from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import RLock

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
    _provider_result,
    _request as _semantic_request,
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
    DurableAgentWireError,
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableGetSessionRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeDependencies,
    DurableRuntimeFactory,
    DurableRuntimeV3Error,
    _SQLiteEventFirstCommandGuard,
)
ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUNTIME_GATE_SESSION_EVENTS = (
    tbm.GATE_SESSION_CREATED,
    tbm.GATE_SESSION_PREPARED,
    tbm.GATE_SESSION_AWAITING_DECISION,
    tbm.GATE_SESSION_LEASE_RENEWED,
    tbm.SEMANTIC_GATE_DECIDED,
    tbm.GATE_SESSION_LEASE_RENEWED,
    tbm.USAGE_DECISION_FINALIZED,
    tbm.EXECUTION_STARTED,
    tbm.GATE_SESSION_COMPLETED,
)
EXPECTED_RUNTIME_GATE_EVIDENCE_EVENTS = (
    tbm.RETRIEVAL_SNAPSHOT_RECORDED,
    tbm.SYSTEM_GATE_EVALUATED,
    tbm.SEMANTIC_GATE_ATTEMPT_RECORDED,
    tbm.USAGE_DECISION_RECORDED,
    tbm.INJECTION_ARTIFACT_RECORDED,
)
EXPECTED_RUNTIME_GATE_SESSION_EVENTS_AFTER_ROLLED_BACK_ATTEMPT = (
    tbm.GATE_SESSION_CREATED,
    tbm.GATE_SESSION_PREPARED,
    tbm.GATE_SESSION_AWAITING_DECISION,
    tbm.GATE_SESSION_LEASE_RENEWED,
    tbm.GATE_SESSION_LEASE_RENEWED,
    tbm.SEMANTIC_GATE_DECIDED,
    tbm.GATE_SESSION_LEASE_RENEWED,
    tbm.USAGE_DECISION_FINALIZED,
    tbm.EXECUTION_STARTED,
    tbm.GATE_SESSION_COMPLETED,
)


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(milliseconds=125)
        return value.isoformat().replace("+00:00", "Z")

    def advance(self, *, seconds: int) -> None:
        self._next += timedelta(seconds=seconds)


class _CommitRollbackFailureConnection(sqlite3.Connection):
    def commit(self) -> None:
        raise sqlite3.OperationalError("injected commit failure")

    def rollback(self) -> None:
        raise sqlite3.OperationalError("injected rollback failure")


class _BeginInterruptConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql == "BEGIN IMMEDIATE":
            raise KeyboardInterrupt("injected begin interruption")
        return super().execute(sql, parameters)


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
        session_id_factory=lambda: f"gate_session_runtime_{next(session_numbers):04d}",
        completion_consumer=completion_consumer,
    )
    return dependencies, context


def _start_runtime_session(runtime, context) -> tbm.GateSession:
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
    return runtime.sessions.get(finalized.session_id)


def _complete_runtime_session(runtime, context, executing: tbm.GateSession):
    completion = _completion(executing)
    return runtime.dispatcher.complete(
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


def test_event_first_command_guard_invalidates_unrecoverable_connection() -> None:
    connection = sqlite3.connect(
        ":memory:",
        factory=_CommitRollbackFailureConnection,
    )
    lock = RLock()
    invalidated: list[bool] = []

    def invalidate() -> None:
        invalidated.append(True)
        connection.close()

    guard = _SQLiteEventFirstCommandGuard(
        connection,
        lock,
        lambda: None,
        invalidate,
    )
    with pytest.raises(DurableRuntimeV3Error) as raised:
        with guard:
            connection.execute("CREATE TABLE injected (value INTEGER)")
    assert raised.value.code == (
        "TBM_DURABLE_RUNTIME_SQLITE_COMMAND_COMMIT_FAILED"
    )
    assert invalidated == [True]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert lock.acquire(blocking=False) is True
    lock.release()

    rollback_connection = sqlite3.connect(
        ":memory:",
        factory=_CommitRollbackFailureConnection,
    )
    rollback_invalidated: list[bool] = []

    def invalidate_rollback() -> None:
        rollback_invalidated.append(True)
        rollback_connection.close()

    rollback_guard = _SQLiteEventFirstCommandGuard(
        rollback_connection,
        RLock(),
        lambda: None,
        invalidate_rollback,
    )
    with pytest.raises(RuntimeError, match="primary command failure"):
        with rollback_guard:
            raise RuntimeError("primary command failure")
    assert rollback_invalidated == [True]


def test_event_first_command_guard_releases_lock_on_base_exception() -> None:
    connection = sqlite3.connect(
        ":memory:",
        factory=_BeginInterruptConnection,
    )
    lock = RLock()
    guard = _SQLiteEventFirstCommandGuard(
        connection,
        lock,
        lambda: None,
        connection.close,
    )
    with pytest.raises(KeyboardInterrupt, match="begin interruption"):
        with guard:
            pass
    assert lock.acquire(blocking=False) is True
    lock.release()
    connection.close()


def test_event_first_sqlite_runtime_commits_rebuilt_views_before_response(
    tmp_path: Path,
) -> None:
    delivered: list[tbm.CompletionOutboxEvent] = []

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivered.append(event)
        return tbm.CompletionOutboxConsumerReceipt(
            response_sha256="sha256:" + "e" * 64
        )

    database = tmp_path / "event-first-runtime.sqlite3"
    dependencies, context = _dependencies(
        _Clock(),
        completion_consumer=consume,
    )
    factory = DurableRuntimeFactory(dependencies)
    with factory.open_sqlite(
        database,
        initialize=True,
        event_first_commands=True,
    ) as runtime:
        assert runtime.event_first_commands is True
        projector = runtime.outcome_effect_event_projector
        assert type(projector) is tbm.OutcomeEffectEventLedgerProjector
        with pytest.raises(DurableAgentWireError) as invalid:
            runtime.dispatcher.prepare(context, object())  # type: ignore[arg-type]
        assert invalid.value.category == "input"
        assert runtime._connection.in_transaction is False
        assert runtime._connection.execute(
            "SELECT count(*) FROM v3_event_ledger_events"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT count(*) FROM v3_authorization_decisions"
        ).fetchone() == (0,)

        executing = _start_runtime_session(runtime, context)
        assert runtime._connection.in_transaction is False
        response = _complete_runtime_session(runtime, context, executing)
        assert runtime._connection.in_transaction is False
        completed = runtime.sessions.get(executing.session_id)
        outcome = runtime.outbox_repository.outcomes.get_outcome(
            completed.run_outcome_id
        )
        event_id = response["result"]["outbox_event"]["event_id"]
        initial = runtime.outbox_repository.get_delivery(event_id)

        completion_events = projector.read_events(completed)
        assert tuple(item.event_type for item in completion_events) == (
            tbm.RUN_OUTCOME_RECORDED,
            tbm.EFFECT_REQUESTED,
        )
        completion_views = projector.rebuild_current(completed)
        assert completion_views.run_outcome == outcome
        assert completion_views.delivery_history[event_id] == (initial,)
        assert completion_views.effect_queue[event_id]["queue_status"] == (
            "ready"
        )

        results = runtime.deliver_outbox(worker_id="worker_event_first")
        assert len(results) == 1
        assert results[0].outcome == "delivered"
        projected_events = projector.read_events(completed)
        assert tuple(item.event_type for item in projected_events) == (
            tbm.RUN_OUTCOME_RECORDED,
            tbm.EFFECT_REQUESTED,
            tbm.EFFECT_STARTED,
            tbm.EFFECT_SUCCEEDED,
        )
        projected = projector.rebuild_current(completed)
        history = runtime.outbox_repository.list_delivery_history(event_id)
        assert projected.delivery_history[event_id] == history
        assert projected.effect_queue[event_id]["queue_status"] == (
            "succeeded"
        )
        assert [item.event_id for item in delivered] == [event_id]

    with factory.open_sqlite(
        database,
        initialize=False,
        event_first_commands=True,
    ) as reopened:
        session = reopened.sessions.get(completed.session_id)
        projector = reopened.outcome_effect_event_projector
        assert type(projector) is tbm.OutcomeEffectEventLedgerProjector
        replayed = projector.rebuild_current(session)
        assert replayed.run_outcome == outcome
        assert replayed.delivery_history[event_id] == history
        assert len(projector.read_events(session)) == 4


def test_event_first_command_rolls_back_authorization_and_events_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
        event_first_commands=True,
    ) as runtime:
        executing = _start_runtime_session(runtime, context)
        projector = runtime.outcome_effect_event_projector
        assert type(projector) is tbm.OutcomeEffectEventLedgerProjector
        authorization_count = runtime._connection.execute(
            "SELECT count(*) FROM v3_authorization_decisions"
        ).fetchone()
        original = projector.append_completion

        def reject_after_append(
            outcome: tbm.RunOutcome,
            session: tbm.GateSession,
        ) -> tbm.OutcomeEffectViews:
            original(outcome, session)
            raise RuntimeError("reject after Outcome/Effect append")

        monkeypatch.setattr(
            projector,
            "append_completion",
            reject_after_append,
        )
        with pytest.raises(DurableAgentWireError) as raised:
            _complete_runtime_session(runtime, context, executing)
        assert raised.value.code == "TBM_DURABLE_EXECUTION_COMPLETION_FAILED"
        assert runtime._connection.in_transaction is False
        assert runtime.sessions.get(executing.session_id) == executing
        assert projector.read_events(executing) == ()
        assert runtime._connection.execute(
            "SELECT count(*) FROM v3_run_outcomes"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT count(*) FROM v3_completion_outbox_events"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT count(*) FROM v3_authorization_decisions"
        ).fetchone() == authorization_count


def test_event_first_delivery_projection_failure_rolls_back_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies, context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
        event_first_commands=True,
    ) as runtime:
        executing = _start_runtime_session(runtime, context)
        response = _complete_runtime_session(runtime, context, executing)
        completed = runtime.sessions.get(executing.session_id)
        event_id = response["result"]["outbox_event"]["event_id"]
        pending = runtime.outbox_repository.get_delivery(event_id)
        projector = runtime.outcome_effect_event_projector
        assert type(projector) is tbm.OutcomeEffectEventLedgerProjector
        original = projector.append_delivery

        def reject_after_append(
            event: tbm.CompletionOutboxEvent,
            previous: tbm.CompletionOutboxDelivery,
            current: tbm.CompletionOutboxDelivery,
        ) -> tbm.OutcomeEffectViews:
            original(event, previous, current)
            raise RuntimeError("reject after EffectStarted append")

        monkeypatch.setattr(
            projector,
            "append_delivery",
            reject_after_append,
        )
        with pytest.raises(RuntimeError, match="EffectStarted"):
            runtime.outbox_repository.claim_due(
                worker_id="worker_rollback",
                lease_seconds=60,
            )
        assert runtime.outbox_repository.get_delivery(event_id) == pending
        assert tuple(
            item.event_type for item in projector.read_events(completed)
        ) == (
            tbm.RUN_OUTCOME_RECORDED,
            tbm.EFFECT_REQUESTED,
        )


def test_durable_sqlite_runtime_builds_one_restart_safe_authority_graph(
    tmp_path: Path,
) -> None:
    delivered: list[tbm.CompletionOutboxEvent] = []

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivered.append(event)
        return tbm.CompletionOutboxConsumerReceipt(response_sha256="sha256:" + "f" * 64)

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
                evidence_artifact_sha256s=list(completion.evidence_artifact_sha256s),
                output_sha256=completion.output_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
            ),
        )
        completed = runtime.sessions.get(executing.session_id)
        assert completed_response["result"]["session"]["status"] == "completed"
        retained_events = runtime.gate_session_event_projector.read_events(completed)
        assert tuple(event.event_type for event in retained_events) == (
            EXPECTED_RUNTIME_GATE_SESSION_EVENTS
        )
        assert (
            runtime.gate_session_event_projector.rebuild_current(completed) == completed
        )
        evidence_events = runtime.gate_evidence_event_projector.read_events(
            completed
        )
        assert tuple(event.event_type for event in evidence_events) == (
            EXPECTED_RUNTIME_GATE_EVIDENCE_EVENTS
        )
        evidence_views = runtime.gate_evidence_event_projector.rebuild_current(
            completed
        )
        assert evidence_views.retrieval.snapshot_id == (
            completed.retrieval_snapshot_id
        )
        assert evidence_views.system_gate.evaluation_id == (
            completed.system_gate_evaluation_id
        )
        assert tuple(
            attempt.attempt_id for attempt in evidence_views.semantic_attempts
        ) == completed.semantic_gate_attempt_ids
        assert evidence_views.final_decision.usage_decision_id == (
            completed.usage_decision_id
        )
        assert evidence_views.injection.artifact.artifact_id == (
            completed.injection_artifact_id
        )
        assert evidence_views.replay_manifest.usage_decision_id == (
            completed.usage_decision_id
        )
        ledger_export = (
            runtime.gate_evidence_event_projector.export_replay_bundle(
                completed,
                allowed_classifications=frozenset({"internal"}),
            )
        )
        authority_export = tbm.export_replay_bundle(
            runtime.replay_repository,
            evidence_views.replay_manifest.manifest_sha256,
            allowed_classifications=frozenset({"internal"}),
        )
        assert ledger_export.export_sha256 == authority_export.export_sha256
        assert tbm.dumps_replay_bundle_export(ledger_export) == (
            tbm.dumps_replay_bundle_export(authority_export)
        )
        with pytest.raises(tbm.GateEvidenceEventV1Error) as forbidden:
            runtime.gate_evidence_event_projector.export_replay_bundle(
                completed,
                allowed_classifications=frozenset({"public"}),
            )
        assert forbidden.value.code == (
            "TBM_GATE_EVIDENCE_REPLAY_EXPORT_FORBIDDEN"
        )
        payload_text = "".join(
            tbm.dumps_canonical_event(event) for event in evidence_events
        )
        assert "memory_durable_runtime" not in payload_text
        assert '"prompt"' not in payload_text
        assert '"response"' not in payload_text

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
        reopened_session = reopened.sessions.get(session_id)
        assert (
            reopened.gate_evidence_event_projector.rebuild_current(
                reopened_session
            ).final_decision.usage_decision_id
            == reopened_session.usage_decision_id
        )
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
            tbm.durable_agent_wire_v1.DurablePrepareRequest.model_validate(payload),
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


def test_durable_sqlite_runtime_records_failed_attempt_immediately() -> None:
    clock = _Clock()
    dependencies, context = _dependencies(clock)
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        with pytest.raises(
            tbm.DurableSemanticGateProviderFailedError
        ) as failed:
            runtime.service_bundle.semantic_service.decide(
                _provider_context(),
                _semantic_request(prepared),
                lambda _call: (_ for _ in ()).throw(
                    tbm.SemanticProviderCallError(
                        "provider_timeout",
                        provider_request_id="provider_request_timeout",
                    )
                ),
            )

        awaiting = failed.value.session
        failed_events = runtime.gate_evidence_event_projector.read_events(
            awaiting
        )
        assert tuple(event.event_type for event in failed_events) == (
            tbm.RETRIEVAL_SNAPSHOT_RECORDED,
            tbm.SYSTEM_GATE_EVALUATED,
            tbm.SEMANTIC_GATE_ATTEMPT_RECORDED,
        )
        assert failed_events[-1].payload["status"] == "failed"
        assert failed_events[-1].payload["response_artifact_id"] is None
        assert len(failed_events[-1].artifact_refs) == 2
        failed_views = runtime.gate_evidence_event_projector.rebuild_current(
            awaiting
        )
        assert tuple(
            attempt.status for attempt in failed_views.semantic_attempts
        ) == ("failed",)


def test_durable_sqlite_runtime_rolls_back_attempt_when_event_sink_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    dependencies, context = _dependencies(clock)
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
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
        original = (
            runtime.gate_evidence_event_projector.append_semantic_attempt
        )

        def reject_after_append(
            bundle: tbm.StoredSemanticGateAttemptArtifacts,
        ) -> None:
            original(bundle)
            raise RuntimeError("reject after Gate evidence append")

        monkeypatch.setattr(
            runtime.gate_evidence_event_projector,
            "append_semantic_attempt",
            reject_after_append,
        )
        with pytest.raises(tbm.SemanticGateServiceV3Error) as raised:
            runtime.service_bundle.semantic_service.decide(
                _provider_context(),
                _semantic_request(prepared),
                lambda _call: _provider_result(evaluation),
            )
        assert raised.value.code == "TBM_SEMANTIC_SERVICE_PERSISTENCE_FAILED"
        awaiting = runtime.sessions.get(prepared.session_id)
        assert awaiting.status == "awaiting_decision"
        assert runtime.semantic_repository.load_attempt_chain(
            evaluation.evaluation_id
        ) == ()
        retained_events = runtime.gate_evidence_event_projector.read_events(
            awaiting
        )
        assert tuple(event.event_type for event in retained_events) == (
            tbm.RETRIEVAL_SNAPSHOT_RECORDED,
            tbm.SYSTEM_GATE_EVALUATED,
        )
        assert runtime._connection.execute(
            "SELECT count(*) FROM v3_semantic_gate_attempts"
        ).fetchone() == (0,)
        assert runtime._connection.execute(
            "SELECT count(*) FROM v3_semantic_gate_artifacts"
        ).fetchone() == (0,)


def test_durable_sqlite_runtime_fails_closed_on_missing_schema(
    tmp_path: Path,
) -> None:
    dependencies, _context = _dependencies(_Clock())
    with pytest.raises(DurableRuntimeV3Error) as raised:
        DurableRuntimeFactory(dependencies).open_sqlite(
            tmp_path / "missing-schema.sqlite3",
            initialize=False,
        )
    assert raised.value.code == ("TBM_DURABLE_RUNTIME_SQLITE_SCHEMA_INVALID")


def test_durable_sqlite_runtime_requires_explicit_outbox_consumer() -> None:
    dependencies, _context = _dependencies(_Clock())
    with DurableRuntimeFactory(dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        with pytest.raises(DurableRuntimeV3Error) as raised:
            runtime.deliver_outbox(worker_id="worker_missing_consumer")
        assert raised.value.code == ("TBM_DURABLE_RUNTIME_OUTBOX_CONSUMER_MISSING")


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
    monkeypatch: pytest.MonkeyPatch,
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
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        runtime = DurableRuntimeFactory(dependencies).bind_postgres(
            connection,
            expose_injection_content=True,
        )
        try:
            assert connection.info.transaction_status.name == "IDLE"
            assert runtime.sessions is runtime.outbox_repository.gate_sessions
            assert runtime.dispatcher.capabilities()["storage_mode"] == ("postgres")

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
            original = (
                runtime.gate_evidence_event_projector.append_semantic_attempt
            )

            def reject_after_append(
                bundle: tbm.StoredSemanticGateAttemptArtifacts,
            ) -> None:
                original(bundle)
                raise RuntimeError("reject after Gate evidence append")

            monkeypatch.setattr(
                runtime.gate_evidence_event_projector,
                "append_semantic_attempt",
                reject_after_append,
            )
            with pytest.raises(tbm.SemanticGateServiceV3Error) as raised:
                runtime.service_bundle.semantic_service.decide(
                    _provider_context(),
                    _semantic_request(prepared),
                    lambda _call: _provider_result(evaluation),
                )
            assert raised.value.code == (
                "TBM_SEMANTIC_SERVICE_PERSISTENCE_FAILED"
            )
            rollback_session = runtime.sessions.get(prepared.session_id)
            assert runtime.semantic_repository.load_attempt_chain(
                evaluation.evaluation_id
            ) == ()
            rollback_events = (
                runtime.gate_evidence_event_projector.read_events(
                    rollback_session
                )
            )
            assert tuple(event.event_type for event in rollback_events) == (
                tbm.RETRIEVAL_SNAPSHOT_RECORDED,
                tbm.SYSTEM_GATE_EVALUATED,
            )
            monkeypatch.setattr(
                runtime.gate_evidence_event_projector,
                "append_semantic_attempt",
                original,
            )
            semantic_result = runtime.service_bundle.semantic_service.decide(
                _provider_context(),
                _semantic_request(
                    prepared,
                    expected_session_version=rollback_session.version,
                ),
                lambda _call: _provider_result(evaluation),
            )
            decided = semantic_result.session
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
            retained = runtime.sessions.get(executing.session_id)
            retained_events = runtime.gate_session_event_projector.read_events(retained)
            assert tuple(event.event_type for event in retained_events) == (
                EXPECTED_RUNTIME_GATE_SESSION_EVENTS_AFTER_ROLLED_BACK_ATTEMPT
            )
            assert (
                runtime.gate_session_event_projector.rebuild_current(retained)
                == retained
            )
            evidence_events = runtime.gate_evidence_event_projector.read_events(
                retained
            )
            assert tuple(event.event_type for event in evidence_events) == (
                EXPECTED_RUNTIME_GATE_EVIDENCE_EVENTS
            )
            evidence_views = (
                runtime.gate_evidence_event_projector.rebuild_current(retained)
            )
            assert evidence_views.retrieval.snapshot_id == (
                retained.retrieval_snapshot_id
            )
            assert evidence_views.system_gate.evaluation_id == (
                retained.system_gate_evaluation_id
            )
            assert tuple(
                attempt.attempt_id
                for attempt in evidence_views.semantic_attempts
            ) == retained.semantic_gate_attempt_ids
            assert evidence_views.final_decision.usage_decision_id == (
                retained.usage_decision_id
            )
            assert evidence_views.injection.artifact.artifact_id == (
                retained.injection_artifact_id
            )
            ledger_export = (
                runtime.gate_evidence_event_projector.export_replay_bundle(
                    retained,
                    allowed_classifications=frozenset({"internal"}),
                )
            )
            authority_export = tbm.export_replay_bundle(
                runtime.replay_repository,
                evidence_views.replay_manifest.manifest_sha256,
                allowed_classifications=frozenset({"internal"}),
            )
            assert ledger_export.export_sha256 == (
                authority_export.export_sha256
            )
            assert tbm.dumps_replay_bundle_export(ledger_export) == (
                tbm.dumps_replay_bundle_export(authority_export)
            )

            results = runtime.deliver_outbox(worker_id="worker_postgres_runtime")
            assert len(results) == 1
            assert results[0].outcome == "delivered"
            assert [event.event_id for event in delivered] == [
                completed["result"]["outbox_event"]["event_id"]
            ]

        finally:
            runtime.close()

    dependencies, _context = _dependencies(_Clock())
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA trace_backed_memory_v3_replay CASCADE")
        connection.commit()
        with pytest.raises(DurableRuntimeV3Error) as raised:
            DurableRuntimeFactory(dependencies).bind_postgres(connection)
        assert raised.value.code == ("TBM_DURABLE_RUNTIME_POSTGRES_SCHEMA_INVALID")
