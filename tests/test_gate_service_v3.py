from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.authorization_v3 import PrincipalIdentity
from trace_backed_memory.gate_service_v3 import (
    AuthenticatedGateServiceV3Error,
    AuthenticatedGateSessionService,
    GatePreparationFailedError,
    GatePreparationRequest,
    GateSessionReplayError,
    PreparedGateEvidence,
)
from trace_backed_memory.service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthorizationDeniedError,
    AuthorizedRetrievalScope,
)
from trace_backed_memory.sqlite_authorization_v3 import (
    SQLiteAuthorizationV3Repository,
)
from trace_backed_memory.sqlite_gate_session_v3 import (
    SQLiteGateSessionRepository,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-28T00:00:00Z"
RETRIEVAL_ID = "retrieval_sha256_" + "a" * 64
SYSTEM_GATE_ID = "system_gate_sha256_" + "b" * 64


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 28, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(seconds=1)
        return value.isoformat().replace("+00:00", "Z")


def _registry() -> tbm.EntityRegistrySnapshot:
    return tbm.loads_entity_registry(
        (ROOT / "examples" / "entity_registry_v3.example.json").read_bytes()
    )


def _context(
    registry: tbm.EntityRegistrySnapshot,
    *,
    principal: PrincipalIdentity | None = None,
) -> AuthenticatedServiceContext:
    policy = registry.authorization_policy
    return AuthenticatedServiceContext(
        principal=principal or policy.principals[0],
        agent_client=policy.agent_clients[0],
        tenant_id="tenant_001",
        repository_reference="owner/repository",
        environment_id="environment_001",
    )


def _request(
    *,
    idempotency_key: str = "idempotency_001",
) -> GatePreparationRequest:
    return GatePreparationRequest(
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint="sha256:" + "c" * 64,
        idempotency_key=idempotency_key,
        expires_in_seconds=300,
        lease_seconds=60,
    )


def _services(
    registry: tbm.EntityRegistrySnapshot,
    authorization: SQLiteAuthorizationV3Repository,
    sessions: SQLiteGateSessionRepository,
    events: list[str],
) -> AuthenticatedGateSessionService:
    auth_service = AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=authorization,
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )

    def verify(
        scope: AuthorizedRetrievalScope,
        session: tbm.GateSession,
        evidence: PreparedGateEvidence[object],
    ) -> None:
        events.append("verify_evidence")
        assert scope.authorization_event_id.startswith("authz_sha256_")
        assert session.status == "created"
        assert sessions.get(session.session_id) == session
        assert evidence.retrieval_snapshot_id == RETRIEVAL_ID
        assert evidence.system_gate_evaluation_id == SYSTEM_GATE_ID

    return AuthenticatedGateSessionService(
        authorization_service=auth_service,
        session_writer=sessions,
        session_id_factory=lambda: "gate_session_001",
        evidence_verifier=verify,
    )


def test_gate_service_creates_before_retrieval_and_prepares_after_verification():
    registry = _registry()
    events: list[str] = []
    with (
        SQLiteAuthorizationV3Repository.connect(
            initialize=True,
        ) as authorization,
        SQLiteGateSessionRepository.connect(
            initialize=True,
            clock=_Clock(),
        ) as sessions,
    ):
        service = _services(registry, authorization, sessions, events)

        def prepare(
            scope: AuthorizedRetrievalScope,
            session: tbm.GateSession,
        ) -> PreparedGateEvidence[str]:
            events.append("prepare")
            assert scope.repository_id == "repository_001"
            assert sessions.get(session.session_id).status == "created"
            return PreparedGateEvidence(
                retrieval_snapshot_id=RETRIEVAL_ID,
                system_gate_evaluation_id=SYSTEM_GATE_ID,
                value="prepared value",
            )

        result = service.prepare(_context(registry), _request(), prepare)

        assert events == ["prepare", "verify_evidence"]
        assert result.value == "prepared value"
        assert result.authorization.allowed is True
        assert result.session.status == "prepared"
        assert result.session.version == 2
        assert result.session.retrieval_snapshot_id == RETRIEVAL_ID
        assert result.session.system_gate_evaluation_id == SYSTEM_GATE_ID
        assert [item.status for item in sessions.history(result.session.session_id)] == [
            "created",
            "prepared",
        ]
        created = sessions.history(result.session.session_id)[0]
        tampered = replace(result.session, tenant_id="tenant_other")
        with pytest.raises(AuthenticatedGateServiceV3Error):
            AuthenticatedGateSessionService._verify_prepared(
                tampered,
                created,
                PreparedGateEvidence(
                    RETRIEVAL_ID,
                    SYSTEM_GATE_ID,
                    "unused",
                ),
            )


def test_gate_service_exact_replay_does_not_repeat_retrieval():
    registry = _registry()
    events: list[str] = []
    with (
        SQLiteAuthorizationV3Repository.connect(
            initialize=True,
        ) as authorization,
        SQLiteGateSessionRepository.connect(
            initialize=True,
            clock=_Clock(),
        ) as sessions,
    ):
        service = _services(registry, authorization, sessions, events)
        first = service.prepare(
            _context(registry),
            _request(),
            lambda _scope, _session: PreparedGateEvidence(
                RETRIEVAL_ID,
                SYSTEM_GATE_ID,
                "first",
            ),
        )

        with pytest.raises(GateSessionReplayError) as replay:
            service.prepare(
                _context(registry),
                _request(),
                lambda _scope, _session: pytest.fail(
                    "idempotent replay must not repeat retrieval"
                ),
            )

        assert replay.value.session == first.session
        assert replay.value.session.status == "prepared"


def test_gate_service_denial_creates_no_session_and_calls_no_prepare():
    registry = _registry()
    unknown = replace(
        registry.authorization_policy.principals[0],
        principal_id="principal_unknown",
    )
    events: list[str] = []
    with (
        SQLiteAuthorizationV3Repository.connect(
            initialize=True,
        ) as authorization,
        SQLiteGateSessionRepository.connect(
            initialize=True,
            clock=_Clock(),
        ) as sessions,
    ):
        service = _services(registry, authorization, sessions, events)

        with pytest.raises(AuthorizationDeniedError):
            service.prepare(
                _context(registry, principal=unknown),
                _request(),
                lambda _scope, _session: pytest.fail(
                    "authorization denial must stop before preparation"
                ),
            )

        with pytest.raises(tbm.SQLiteGateSessionNotFoundError):
            sessions.get("gate_session_001")
        assert events == []


@pytest.mark.parametrize("failure_point", ("prepare", "verify"))
def test_gate_service_cancels_new_session_when_preparation_fails(
    failure_point: str,
):
    registry = _registry()
    with (
        SQLiteAuthorizationV3Repository.connect(
            initialize=True,
        ) as authorization,
        SQLiteGateSessionRepository.connect(
            initialize=True,
            clock=_Clock(),
        ) as sessions,
    ):
        auth_service = AuthenticatedRetrievalService(
            registry_provider=lambda: registry,
            decision_writer=authorization,
            clock=lambda: NOW,
            request_id_factory=lambda: "authorization_request_001",
        )

        def verify(
            _scope: AuthorizedRetrievalScope,
            _session: tbm.GateSession,
            _evidence: PreparedGateEvidence[object],
        ) -> None:
            if failure_point == "verify":
                raise RuntimeError("secret verifier failure")

        service = AuthenticatedGateSessionService(
            authorization_service=auth_service,
            session_writer=sessions,
            session_id_factory=lambda: "gate_session_001",
            evidence_verifier=verify,
        )

        def prepare(
            _scope: AuthorizedRetrievalScope,
            _session: tbm.GateSession,
        ) -> PreparedGateEvidence[str]:
            if failure_point == "prepare":
                raise RuntimeError("secret prepare failure")
            return PreparedGateEvidence(
                RETRIEVAL_ID,
                SYSTEM_GATE_ID,
                "unused",
            )

        with pytest.raises(GatePreparationFailedError) as failed:
            service.prepare(_context(registry), _request(), prepare)

        assert failed.value.session.status == "canceled"
        assert failed.value.session.terminal_reason == "prepare_failed"
        assert "secret" not in str(failed.value)
        assert [item.status for item in sessions.history("gate_session_001")] == [
            "created",
            "canceled",
        ]


def test_gate_service_public_exports_are_intentional():
    assert tbm.AuthenticatedGateSessionService is AuthenticatedGateSessionService
    assert tbm.GatePreparationRequest is GatePreparationRequest
    assert "AuthenticatedGateSessionService" in tbm.__all__
