from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster
from tests.test_artifact_service_v3 import (
    _context as _service_context,
    _registry,
)
from tests.test_durable_retrieval_preparation_v3 import _durable_request
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
    _provider_result,
    _request as _semantic_request,
    _semantic_service,
)
from tests.test_retrieval_preparation_v3 import (
    _Discovery,
    _PolicyProvider,
    _Source,
    _candidate,
    _indexes,
    _policy,
    _record,
    _result,
    _retrieval_authorization,
    _service,
)
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for script in (
        "postgres-v3-event-ledger.sql",
        "postgres-v3-gate-session.sql",
        "postgres-v3-gate-evidence.sql",
        "postgres-v3-semantic-gate.sql",
        "postgres-v3-semantic-gate-artifacts.sql",
        "postgres-v3-replay.sql",
    ):
        installed = cluster.run_script(ROOT / "schemas" / script)
        assert installed.returncode == 0, installed.stderr


def _decided_stack(
    connection,
    *,
    session_id: str,
    permissions: tuple[str, ...] = ("memory:retrieve",),
    sessions: tbm.PostgresGateSessionRepository | None = None,
    event_first: bool = False,
):
    registry = _registry(permissions=permissions)
    context = _service_context(registry)
    authorization, decisions = _retrieval_authorization(registry)
    if sessions is None:
        sessions = tbm.PostgresGateSessionRepository(
            connection,
            allow_direct_completion=False,
        )
    evidence = PostgresGateEvidenceV3Repository(connection)
    semantic = tbm.PostgresSemanticGateArtifactV3Repository(connection)
    replay = tbm.PostgresReplayV3Repository(connection)
    policy = _policy()
    candidate = _candidate("memory_postgres_finalization")
    source = _Source((candidate,))
    retrieval = _service(
        authorization,
        _PolicyProvider(policy),
        _Discovery(
            _result(
                records=(_record(candidate),),
                index_versions=_indexes(
                    "metadata",
                    "lexical",
                    "semantic",
                    "git_graph",
                ),
            )
        ),
        source,
    )
    gate = tbm.AuthenticatedGateSessionService(
        authorization_service=authorization,
        session_writer=sessions,
        session_id_factory=lambda: session_id,
        evidence_verifier=tbm.DurablePreparedGateEvidenceVerifier(evidence),
    )
    prepared = tbm.DurableRetrievalPreparationService(
        gate_session_service=gate,
        retrieval_service=retrieval,
        evidence_authority=evidence,
    ).prepare(
        context,
        replace(
            _durable_request(
                idempotency_key=f"{session_id}_retrieval",
            ),
            expires_in_seconds=3_600,
        ),
    )
    semantic_service = _semantic_service(
        semantic,
        evidence,
        iter(("2026-07-30T02:03:00Z", "2026-07-30T02:03:00.125Z")),
    )
    decided = tbm.AuthenticatedSemanticGateSessionService(
        semantic_gate_service=semantic_service,
        session_writer=sessions,
    ).decide(
        _provider_context(),
        _semantic_request(prepared.session),
        lambda _call: _provider_result(
            prepared.value.system_gate_evaluation
        ),
    ).session
    if event_first:
        _backfill_gate_session_events(
            sessions,
            decided,
            prepared.scope,
            connection,
        )
        sessions.enable_event_first()
        replay.enable_event_first()
    finalizer = tbm.DurableFinalizationService(
        authorization_service=authorization,
        session_writer=sessions,
        evidence_reader=evidence,
        semantic_authority=semantic,
        revision_source=source,
        policy_loader=lambda: policy,
        replay_authority=replay,
        clock=lambda: "2026-07-30T02:05:00Z",
    )
    return (
        decisions,
        context,
        prepared.scope,
        sessions,
        replay,
        decided,
        finalizer,
    )


def _event_context(
    scope: tbm.AuthorizedRetrievalScope,
) -> tbm.EventTrustedContext:
    return tbm.EventTrustedContext(
        organization_id=scope.organization_id,
        tenant_id=scope.tenant_id,
        repository_id=scope.repository_id,
        environment_id=scope.environment_id,
        principal_id=scope.principal_id,
        agent_client_id=scope.agent_client_id,
        actor_type="agent_client",
        actor_id=scope.agent_client_id,
        authorization_decision_id=scope.authorization_event_id,
    )


def _event_access(
    scope: tbm.AuthorizedRetrievalScope,
) -> tbm.LedgerAccessContext:
    trusted = _event_context(scope)
    return tbm.LedgerAccessContext(
        partition=tbm.LedgerTenantPartition(
            trusted.organization_id,
            trusted.tenant_id,
            trusted.repository_id,
            trusted.environment_id,
        ),
        principal_id=trusted.principal_id,
        agent_client_id=trusted.agent_client_id,
        actor_type=trusted.actor_type,
        actor_id=trusted.actor_id,
        authorization_decision_id=trusted.authorization_decision_id,
        classification_filter=tbm.LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _backfill_gate_session_events(
    sessions: tbm.PostgresGateSessionRepository,
    decided: tbm.GateSession,
    scope: tbm.AuthorizedRetrievalScope,
    connection,
) -> None:
    ledger = tbm.PostgresEventLedgerV1(connection, _event_access(scope))
    previous_session = None
    previous_event = None
    try:
        for session in sessions.history(decided.session_id):
            event = tbm.build_gate_session_event(
                session,
                previous_session=previous_session,
                parent_event=previous_event,
                global_position=session.version,
                trusted_context=_event_context(scope),
            )
            ledger.append(
                session.session_id,
                session.version - 1,
                (event,),
                tbm.LedgerIdempotency(
                    event.idempotency_key_sha256,
                    event.request_sha256,
                ),
            )
            previous_session = session
            previous_event = event
    finally:
        ledger.close()


def test_postgres_durable_finalization_parity(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            scope,
            sessions,
            replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_finalization_001",
        )
        request = tbm.DurableFinalizationRequest(
            decided.session_id,
            decided.version,
        )
        try:
            result = finalizer.finalize(context, scope, request)
            assert result.session.status == "finalized"
            assert sessions.get(decided.session_id) == result.session
            assert replay.load_injection(
                result.injection.artifact.artifact_id
            ) == (result.injection, result.snippet.encode())

            replayed = finalizer.finalize(context, scope, request)
            assert replayed.replayed is True
            assert replayed.session == result.session
            assert replayed.usage_decision == result.usage_decision
            assert replayed.manifest == result.manifest
        finally:
            decisions.close()


def test_postgres_durable_finalization_respects_outer_rollback(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            scope,
            sessions,
            replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_finalization_rollback",
        )
        request = tbm.DurableFinalizationRequest(
            decided.session_id,
            decided.version,
        )
        result = None
        try:
            with pytest.raises(RuntimeError, match="rollback caller"):
                with connection.transaction():
                    result = finalizer.finalize(context, scope, request)
                    raise RuntimeError("rollback caller")

            assert result is not None
            assert sessions.get(decided.session_id) == decided
            with pytest.raises(KeyError):
                replay.load_injection(
                    result.injection.artifact.artifact_id
                )
            with pytest.raises(KeyError):
                replay.load_artifact(
                    tbm.usage_decision_artifact_id(
                        result.usage_decision.usage_decision_id
                    )
                )
        finally:
            decisions.close()


def test_postgres_event_first_finalization_is_atomic_and_replayable(
    postgres_cluster: PostgresCluster,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            scope,
            sessions,
            replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_event_first_finalization",
            event_first=True,
        )
        trusted = _event_context(scope)
        try:
            with sessions.bind_event_context(
                trusted
            ), replay.bind_event_context(trusted):
                result = finalizer.finalize(
                    context,
                    scope,
                    tbm.DurableFinalizationRequest(
                        decided.session_id,
                        decided.version,
                    ),
                )

            ledger = tbm.PostgresEventLedgerV1(
                connection,
                _event_access(scope),
            )
            try:
                events = ledger.read_global().events
            finally:
                ledger.close()
            assert tuple(event.event_type for event in events[-3:]) == (
                tbm.GATE_SESSION_LEASE_RENEWED_EVENT,
                tbm.USAGE_DECISION_FINALIZED_EVENT,
                tbm.INJECTION_RENDERED_EVENT,
            )
            rendered = tbm.parse_injection_rendered_event(events[-1])
            assert rendered.usage_decision == result.usage_decision
            assert rendered.injection == result.injection
            assert replay.load_manifest(
                result.manifest.manifest_sha256
            ) == result.manifest
            parity_ledger = tbm.PostgresEventLedgerV1(
                connection,
                _event_access(scope),
            )
            try:
                ledger_reader = tbm.LedgerReplayExportReaderV1(
                    parity_ledger,
                    replay,
                )
                assert tbm.verify_ledger_replay_export_parity(
                    ledger_reader,
                    replay,
                    result.manifest.manifest_sha256,
                    allowed_classifications=frozenset({"internal"}),
                ).export_sha256 == tbm.export_replay_bundle(
                    replay,
                    result.manifest.manifest_sha256,
                    allowed_classifications=frozenset({"internal"}),
                ).export_sha256
            finally:
                parity_ledger.close()
        finally:
            decisions.close()


def test_postgres_event_first_finalization_rolls_back_after_projection_failure(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        (
            decisions,
            context,
            scope,
            sessions,
            replay,
            decided,
            finalizer,
        ) = _decided_stack(
            connection,
            session_id="gate_session_postgres_event_first_rollback",
            event_first=True,
        )
        original = replay._put_artifact

        def fail_projection(*args: object) -> bool:
            original(*args)
            raise RuntimeError("synthetic replay projection failure")

        monkeypatch.setattr(replay, "_put_artifact", fail_projection)
        trusted = _event_context(scope)
        try:
            with sessions.bind_event_context(
                trusted
            ), replay.bind_event_context(trusted):
                with pytest.raises(tbm.DurableFinalizationV3Error) as captured:
                    finalizer.finalize(
                        context,
                        scope,
                        tbm.DurableFinalizationRequest(
                            decided.session_id,
                            decided.version,
                        ),
                    )
            assert captured.value.code == (
                "TBM_DURABLE_FINALIZATION_EVENT_FIRST_FAILED"
            )
            current = sessions.get(decided.session_id)
            assert current.status == "decided"
            assert current.version == decided.version + 1
            ledger = tbm.PostgresEventLedgerV1(
                connection,
                _event_access(scope),
            )
            try:
                event_types = tuple(
                    event.event_type
                    for event in ledger.read_global().events
                )
            finally:
                ledger.close()
            assert event_types[-1] == tbm.GATE_SESSION_LEASE_RENEWED_EVENT
            assert tbm.USAGE_DECISION_FINALIZED_EVENT not in event_types
            assert tbm.INJECTION_RENDERED_EVENT not in event_types
            with pytest.raises(KeyError):
                replay.load_manifest_for_session(
                    decided.session_id,
                    decided.decision_id or "",
                    "usage_decision_sha256_" + "0" * 64,
                    "artifact_sha256_" + "0" * 64,
                )
        finally:
            decisions.close()
