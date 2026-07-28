from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.authenticated_agent_v3 import (
    AuthenticatedAgentPrepareContext,
    AuthenticatedLocalAgentMemory,
)
from trace_backed_memory.service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizationDeniedError,
)
from trace_backed_memory.sqlite_authorization_v3 import (
    SQLiteAuthorizationV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-28T04:00:00Z"


def _registry() -> tbm.EntityRegistrySnapshot:
    return tbm.loads_entity_registry(
        (ROOT / "examples" / "entity_registry_v3.example.json").read_bytes()
    )


def _service_context(
    registry: tbm.EntityRegistrySnapshot,
    *,
    repository_reference: str = "owner/repository",
) -> AuthenticatedServiceContext:
    policy = registry.authorization_policy
    return AuthenticatedServiceContext(
        principal=policy.principals[0],
        agent_client=policy.agent_clients[0],
        tenant_id="tenant_001",
        repository_reference=repository_reference,
        environment_id="environment_001",
    )


def _service(
    registry: tbm.EntityRegistrySnapshot,
    writer,
) -> AuthenticatedRetrievalService:
    return AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=writer,
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )


def _trace() -> tbm.Trace:
    return tbm.Trace(
        trace_id="trace_authenticated_001",
        run_id="run_authenticated_001",
        commit_sha="a" * 40,
        repo="caller_controlled_repo",
        tenant="caller_controlled_tenant",
    )


def _prepare_context() -> AuthenticatedAgentPrepareContext:
    return AuthenticatedAgentPrepareContext(
        mode="production",
        commit_sha="a" * 40,
    )


def test_authenticated_agent_authorizes_before_prepare_and_binds_scope() -> None:
    registry = _registry()
    runtime = tbm.LocalAgentMemory.in_memory()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as decisions:
        agent = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=_service(registry, decisions),
            service_context=_service_context(registry),
        )
        result = agent.prepare(
            _trace(),
            _prepare_context(),
            task="retrieve safe memory",
        )

        stored_trace = runtime.snapshot()["traces"][0]
        assert result.decision.allowed is True
        assert result.scope.repository_id == "repository_001"
        assert result.value.trace_id == "trace_authenticated_001"
        assert stored_trace["repo"] == "repository_001"
        assert stored_trace["tenant"] == "tenant_001"
        assert (
            decisions.load_decision(result.decision.authorization_event_id)
            == result.decision
        )
    runtime.close()


def test_authenticated_agent_denial_never_records_trace_or_prepares() -> None:
    registry = _registry()
    runtime = tbm.LocalAgentMemory.in_memory()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as decisions:
        agent = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=_service(registry, decisions),
            service_context=_service_context(
                registry,
                repository_reference="unknown/repository",
            ),
        )
        with pytest.raises(AuthorizationDeniedError):
            agent.prepare(
                _trace(),
                _prepare_context(),
                task="must not retrieve",
            )
        assert runtime.snapshot()["traces"] == []
    runtime.close()


def test_authenticated_agent_persistence_failure_never_prepares() -> None:
    registry = _registry()
    runtime = tbm.LocalAgentMemory.in_memory()

    class _FailingWriter:
        def append_decision(self, *_args):
            raise RuntimeError("secret persistence failure")

        def load_decision(self, _authorization_event_id):
            raise AssertionError("must not read after failed append")

    agent = AuthenticatedLocalAgentMemory(
        runtime=runtime,
        authorization_service=_service(registry, _FailingWriter()),
        service_context=_service_context(registry),
    )
    with pytest.raises(
        AuthenticatedServiceV3Error,
        match="authorization decision could not be persisted",
    ) as failure:
        agent.prepare(
            _trace(),
            _prepare_context(),
            task="must not retrieve",
        )
    assert "secret" not in str(failure.value)
    assert runtime.snapshot()["traces"] == []
    runtime.close()


def test_authenticated_prepare_context_has_no_identity_or_target_fields() -> None:
    names = {field.name for field in fields(AuthenticatedAgentPrepareContext)}
    assert "principal_id" not in names
    assert "agent_client_id" not in names
    assert "tenant" not in names
    assert "tenant_id" not in names
    assert "repo" not in names
    assert "repository_id" not in names
    assert "repository_reference" not in names
    assert "environment_id" not in names


def test_authenticated_agent_types_are_public_package_exports() -> None:
    assert tbm.AuthenticatedAgentPrepareContext is AuthenticatedAgentPrepareContext
    assert tbm.AuthenticatedLocalAgentMemory is AuthenticatedLocalAgentMemory


def test_authenticated_agent_delegates_finalize_complete_and_cancel() -> None:
    registry = _registry()
    runtime = tbm.LocalAgentMemory.in_memory()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as decisions:
        agent = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=_service(registry, decisions),
            service_context=_service_context(registry),
        )
        prepared = agent.prepare(
            _trace(),
            _prepare_context(),
            task="complete lifecycle",
        ).value
        finalized = agent.finalize(
            prepared.request_id,
            {
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "No applicable memory.",
                "risk": "none",
                "recommended_injection": "none",
            },
        )
        completed = agent.complete(
            finalized.decision_id,
            tbm.MemoryRunMeasurement(eval_result="pass"),
        )
        assert completed.trace_id == prepared.trace_id

        cancel_trace = tbm.Trace(
            trace_id="trace_authenticated_cancel",
            run_id="run_authenticated_cancel",
            commit_sha="a" * 40,
        )
        pending = agent.prepare(
            cancel_trace,
            _prepare_context(),
            task="cancel lifecycle",
        ).value
        agent.cancel(pending.request_id)
        with pytest.raises(
            AuthenticatedServiceV3Error,
            match="cancel failed",
        ):
            agent.cancel(pending.request_id)
    runtime.close()


def test_authenticated_agent_rejects_handles_owned_by_another_facade() -> None:
    registry = _registry()
    runtime = tbm.LocalAgentMemory.in_memory()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as decisions:
        owner = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=_service(registry, decisions),
            service_context=_service_context(registry),
        )
        other = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=_service(registry, decisions),
            service_context=_service_context(registry),
        )
        prepared = owner.prepare(
            _trace(),
            _prepare_context(),
            task="owned lifecycle",
        ).value
        with pytest.raises(
            AuthenticatedServiceV3Error,
            match="finalize failed",
        ):
            other.finalize(
                prepared.request_id,
                {
                    "use_memory": False,
                    "allowed_memory_ids": [],
                    "blocked_memory_ids": [],
                    "reason": "Must not cross facade ownership.",
                    "risk": "none",
                    "recommended_injection": "none",
                },
            )
        with pytest.raises(
            AuthenticatedServiceV3Error,
            match="cancel failed",
        ):
            other.cancel(prepared.request_id)

        finalized = owner.finalize(
            prepared.request_id,
            {
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "Owner finalizes.",
                "risk": "none",
                "recommended_injection": "none",
            },
        )
        with pytest.raises(
            AuthenticatedServiceV3Error,
            match="complete failed",
        ):
            other.complete(
                finalized.decision_id,
                tbm.MemoryRunMeasurement(eval_result="pass"),
            )
    runtime.close()


def test_authenticated_agent_sanitizes_all_lifecycle_failures() -> None:
    registry = _registry()
    runtime = tbm.LocalAgentMemory.in_memory()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as decisions:
        agent = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=_service(registry, decisions),
            service_context=_service_context(registry),
        )
        prepared = agent.prepare(
            _trace(),
            _prepare_context(),
            task="sanitize lifecycle errors",
        ).value

        with pytest.raises(AuthenticatedServiceV3Error) as malformed:
            agent.finalize(
                prepared.request_id,
                {"use_memory": "secret"},  # type: ignore[dict-item]
            )
        with pytest.raises(AuthenticatedServiceV3Error) as unknown:
            agent.finalize(
                "unknown_request",
                {"use_memory": "secret"},  # type: ignore[dict-item]
            )
        with pytest.raises(AuthenticatedServiceV3Error) as unhashable:
            agent.cancel([])  # type: ignore[arg-type]

        for failure in (malformed.value, unknown.value, unhashable.value):
            assert failure.code == "TBM_AUTHENTICATED_AGENT_OPERATION_FAILED"
            assert failure.__cause__ is None
            assert failure.__context__ is None
            assert "secret" not in str(failure)
    runtime.close()


def test_authenticated_agent_rejects_invalid_constructor_and_inputs() -> None:
    registry = _registry()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as decisions:
        service = _service(registry, decisions)
        context = _service_context(registry)
        runtime = tbm.LocalAgentMemory.in_memory()
        with pytest.raises(TypeError, match="runtime"):
            AuthenticatedLocalAgentMemory(  # type: ignore[arg-type]
                runtime=object(),
                authorization_service=service,
                service_context=context,
            )
        agent = AuthenticatedLocalAgentMemory(
            runtime=runtime,
            authorization_service=service,
            service_context=context,
        )
        with pytest.raises(
            AuthenticatedServiceV3Error,
            match="input is invalid",
        ):
            agent.prepare(  # type: ignore[arg-type]
                object(),
                _prepare_context(),
                task="invalid",
            )
        runtime.close()
