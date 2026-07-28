from __future__ import annotations

from dataclasses import replace

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.authorization_v3 import (
    AgentClientIdentity,
    AuthorizationDecision,
    AuthorizationPolicyBundle,
    PrincipalIdentity,
    RepositoryAlias,
    RepositoryTenantBinding,
    RoleBinding,
)
from trace_backed_memory.contracts_v3 import (
    AuthorizationScope,
    CanonicalRepository,
)
from trace_backed_memory.entity_registry_v3 import (
    EntityRegistrySnapshot,
    EnvironmentIdentity,
    OrganizationIdentity,
    TenantEntity,
)
from trace_backed_memory.service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizationDeniedError,
    AuthorizedRetrievalScope,
)
from trace_backed_memory.sqlite_authorization_v3 import (
    SQLiteAuthorizationV3Repository,
)


NOW = "2026-07-28T00:00:00Z"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _principal(
    *,
    principal_id: str = "principal_001",
    tenant_id: str | None = "tenant_001",
    status: str = "active",
    subject_hash: str = DIGEST_A,
) -> PrincipalIdentity:
    return PrincipalIdentity(
        principal_id=principal_id,
        issuer="https://identity.example.test",
        subject_hash=subject_hash,
        tenant_id=tenant_id,
        status=status,  # type: ignore[arg-type]
    )


def _client(
    *,
    agent_client_id: str = "client_001",
    tenant_id: str | None = "tenant_001",
    status: str = "active",
) -> AgentClientIdentity:
    return AgentClientIdentity(
        agent_client_id=agent_client_id,
        tenant_id=tenant_id,
        client_kind="service",
        status=status,  # type: ignore[arg-type]
    )


def _repository() -> CanonicalRepository:
    return CanonicalRepository(
        repository_id="repository_001",
        provider="local",
        provider_repository_id="provider_repository_001",
        canonical_locator_hash=DIGEST_A,
        display_name="Example repository",
    )


def _policy(
    *,
    principals: tuple[PrincipalIdentity, ...] | None = None,
    clients: tuple[AgentClientIdentity, ...] | None = None,
    bindings: tuple[RoleBinding, ...] | None = None,
) -> AuthorizationPolicyBundle:
    principal_records = principals if principals is not None else (_principal(),)
    client_records = clients if clients is not None else (_client(),)
    principal = principal_records[0]
    client = client_records[0]
    return AuthorizationPolicyBundle(
        policy_version="policy_001",
        principals=principal_records,
        agent_clients=client_records,
        repositories=(_repository(),),
        repository_tenants=(
            RepositoryTenantBinding(
                repository_id="repository_001",
                tenant_id="tenant_001",
            ),
        ),
        repository_aliases=(
            RepositoryAlias(
                alias="owner/repository",
                repository_id="repository_001",
                tenant_id="tenant_001",
                source="operator_registry",
            ),
        ),
        role_bindings=bindings
        if bindings is not None
        else (
            RoleBinding(
                binding_id="binding_001",
                principal_id=principal.principal_id,
                agent_client_id=client.agent_client_id,
                role_name="repository_reader",
                scope=AuthorizationScope(
                    kind="repository",
                    tenant_id="tenant_001",
                    repository_id="repository_001",
                ),
                permissions=("memory:retrieve",),
                status="active",
                valid_from=NOW,
            ),
        ),
    )


def _registry(
    *,
    policy: AuthorizationPolicyBundle | None = None,
    environment: EnvironmentIdentity | None = None,
) -> EntityRegistrySnapshot:
    return EntityRegistrySnapshot(
        registry_version="registry_001",
        organizations=(
            OrganizationIdentity(
                organization_id="organization_001",
                display_name="Example organization",
            ),
        ),
        tenants=(
            TenantEntity(
                tenant_id="tenant_001",
                organization_id="organization_001",
                display_name="Example tenant",
            ),
        ),
        environments=(
            environment
            or EnvironmentIdentity(
                environment_id="environment_001",
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_kind="production",
                display_name="Production",
            ),
        ),
        authorization_policy=policy or _policy(),
    )


def _context(
    registry: EntityRegistrySnapshot,
    *,
    repository_reference: str = "repository_001",
    environment_id: str = "environment_001",
) -> AuthenticatedServiceContext:
    policy = registry.authorization_policy
    return AuthenticatedServiceContext(
        principal=policy.principals[0],
        agent_client=policy.agent_clients[0],
        tenant_id="tenant_001",
        repository_reference=repository_reference,
        environment_id=environment_id,
    )


class _Writer:
    def __init__(self, events: list[str] | None = None) -> None:
        self.decisions: list[AuthorizationDecision] = []
        self.events = events

    def append_decision(
        self,
        policy: AuthorizationPolicyBundle,
        request: tbm.AuthorizationRequest,
        decision: AuthorizationDecision,
    ) -> _Receipt:
        tbm.verify_authorization_decision(policy, request, decision)
        self.decisions.append(decision)
        if self.events is not None:
            self.events.append("authorization")
        return _Receipt(
            policy_sha256=policy.policy_sha256,
            authorization_event_id=decision.authorization_event_id,
            decision_inserted=True,
        )

    def load_decision(
        self,
        authorization_event_id: str,
    ) -> AuthorizationDecision:
        return next(
            decision
            for decision in self.decisions
            if decision.authorization_event_id == authorization_event_id
        )


class _Receipt:
    def __init__(
        self,
        *,
        policy_sha256: str,
        authorization_event_id: str,
        decision_inserted: bool,
    ) -> None:
        self.policy_sha256 = policy_sha256
        self.authorization_event_id = authorization_event_id
        self.decision_inserted = decision_inserted


def _service(
    registry: EntityRegistrySnapshot,
    writer: _Writer,
) -> AuthenticatedRetrievalService:
    return AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=writer,
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )


def test_service_persists_allow_before_canonical_retrieval():
    events: list[str] = []
    registry = _registry()
    writer = _Writer(events)
    service = _service(registry, writer)

    def retrieve(scope: AuthorizedRetrievalScope) -> str:
        events.append("retrieval")
        assert scope.repository_id == "repository_001"
        assert scope.environment_id == "environment_001"
        assert scope.authorization_event_id == writer.decisions[0].authorization_event_id
        return "retrieved"

    result = service.authorize_retrieval(
        _context(registry, repository_reference="owner/repository"),
        retrieve,
    )

    assert events == ["authorization", "retrieval"]
    assert result.value == "retrieved"
    assert result.decision.allowed is True
    assert result.scope.authorization_event_id == result.decision.authorization_event_id


@pytest.mark.parametrize(
    ("policy", "expected_reason"),
    (
        (
            _policy(principals=(_principal(principal_id="principal_other"),)),
            "unknown_principal",
        ),
        (
            _policy(principals=(_principal(status="disabled"),)),
            "principal_disabled",
        ),
        (
            _policy(clients=(_client(agent_client_id="client_other"),)),
            "unknown_agent_client",
        ),
        (
            _policy(clients=(_client(status="disabled"),)),
            "agent_client_disabled",
        ),
        (_policy(bindings=()), "no_matching_binding"),
    ),
)
def test_service_persists_denial_and_never_calls_retrieval(
    policy: AuthorizationPolicyBundle,
    expected_reason: str,
):
    registry = _registry(policy=policy)
    writer = _Writer()
    service = _service(registry, writer)
    context = AuthenticatedServiceContext(
        principal=_principal(),
        agent_client=_client(),
        tenant_id="tenant_001",
        repository_reference="repository_001",
        environment_id="environment_001",
    )
    if expected_reason == "principal_disabled":
        context = replace(context, principal=policy.principals[0])
    if expected_reason == "agent_client_disabled":
        context = replace(context, agent_client=policy.agent_clients[0])

    with pytest.raises(AuthorizationDeniedError) as raised:
        service.authorize_retrieval(
            context,
            lambda _scope: pytest.fail("retrieval must not be called"),
        )

    assert raised.value.code == "TBM_SERVICE_AUTHORIZATION_DENIED"
    assert raised.value.decision.reason == expected_reason
    assert writer.decisions == [raised.value.decision]


def test_service_rejects_stale_authenticated_identity_before_authorization():
    registry = _registry()
    writer = _Writer()
    context = replace(
        _context(registry),
        principal=replace(registry.authorization_policy.principals[0], subject_hash=DIGEST_B),
    )

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        _service(registry, writer).authorize_retrieval(
            context,
            lambda _scope: pytest.fail("retrieval must not be called"),
        )

    assert raised.value.code == "TBM_SERVICE_AUTHENTICATION_CONTEXT_REJECTED"
    assert writer.decisions == []
    assert "principal_001" not in str(raised.value)


@pytest.mark.parametrize(
    "environment",
    (
        EnvironmentIdentity(
            environment_id="environment_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_kind="production",
            display_name="Production",
            status="disabled",
        ),
        EnvironmentIdentity(
            environment_id="environment_other",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_kind="production",
            display_name="Other",
        ),
    ),
)
def test_service_rejects_environment_before_retrieval(
    environment: EnvironmentIdentity,
):
    registry = _registry(environment=environment)
    writer = _Writer()

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        _service(registry, writer).authorize_retrieval(
            AuthenticatedServiceContext(
                principal=registry.authorization_policy.principals[0],
                agent_client=registry.authorization_policy.agent_clients[0],
                tenant_id="tenant_001",
                repository_reference="repository_001",
                environment_id="environment_001",
            ),
            lambda _scope: pytest.fail("retrieval must not be called"),
        )

    assert raised.value.code == "TBM_SERVICE_ENTITY_CONTEXT_REJECTED"
    assert writer.decisions[0].allowed is True


def test_service_fails_closed_when_decision_persistence_fails():
    class BrokenWriter:
        def append_decision(
            self,
            policy: AuthorizationPolicyBundle,
            request: tbm.AuthorizationRequest,
            decision: AuthorizationDecision,
        ) -> _Receipt:
            raise RuntimeError(
                f"secret database path for {policy.policy_sha256} {request.request_id}"
            )

    registry = _registry()
    service = AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=BrokenWriter(),
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        service.authorize_retrieval(
            _context(registry),
            lambda _scope: pytest.fail("retrieval must not be called"),
        )

    assert raised.value.code == "TBM_SERVICE_AUTHORIZATION_PERSIST_FAILED"
    assert "secret" not in str(raised.value)
    assert "authorization_request_001" not in str(raised.value)


def test_service_rejects_noop_persistence_receipt_before_retrieval():
    class NoopWriter:
        def append_decision(
            self,
            policy: AuthorizationPolicyBundle,
            request: tbm.AuthorizationRequest,
            decision: AuthorizationDecision,
        ) -> object:
            return None

    registry = _registry()
    service = AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=NoopWriter(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        service.authorize_retrieval(
            _context(registry),
            lambda _scope: pytest.fail("retrieval must not be called"),
        )

    assert raised.value.code == "TBM_SERVICE_AUTHORIZATION_RECEIPT_INVALID"


def test_service_accepts_exact_sqlite_insert_and_idempotent_replay():
    registry = _registry()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as authority:
        service = AuthenticatedRetrievalService(
            registry_provider=lambda: registry,
            decision_writer=authority,
            clock=lambda: NOW,
            request_id_factory=lambda: "authorization_request_001",
        )

        first = service.authorize_retrieval(
            _context(registry),
            lambda scope: scope.authorization_event_id,
        )
        replay = service.authorize_retrieval(
            _context(registry),
            lambda scope: scope.authorization_event_id,
        )

    assert first.decision == replay.decision
    assert first.value == replay.value == first.decision.authorization_event_id


def test_service_rechecks_registry_after_persisting_allow():
    original = _registry()
    disabled = _registry(
        environment=replace(original.environments[0], status="disabled")
    )
    current = [original]

    class RotatingWriter(_Writer):
        def append_decision(
            self,
            policy: AuthorizationPolicyBundle,
            request: tbm.AuthorizationRequest,
            decision: AuthorizationDecision,
        ) -> _Receipt:
            receipt = super().append_decision(policy, request, decision)
            current[0] = disabled
            return receipt

    service = AuthenticatedRetrievalService(
        registry_provider=lambda: current[0],
        decision_writer=RotatingWriter(),
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        service.authorize_retrieval(
            _context(original),
            lambda _scope: pytest.fail("retrieval must not be called"),
        )

    assert raised.value.code == "TBM_SERVICE_REGISTRY_CHANGED"


def test_service_rechecks_policy_rotation_after_persisting_allow():
    original = _registry()
    rotated = replace(
        original,
        registry_version="registry_002",
        authorization_policy=replace(
            original.authorization_policy,
            policy_version="policy_002",
        ),
    )
    current = [original]

    class RotatingWriter(_Writer):
        def append_decision(
            self,
            policy: AuthorizationPolicyBundle,
            request: tbm.AuthorizationRequest,
            decision: AuthorizationDecision,
        ) -> _Receipt:
            receipt = super().append_decision(policy, request, decision)
            current[0] = rotated
            return receipt

    service = AuthenticatedRetrievalService(
        registry_provider=lambda: current[0],
        decision_writer=RotatingWriter(),
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        service.authorize_retrieval(
            _context(original),
            lambda _scope: pytest.fail("retrieval must not be called"),
        )

    assert raised.value.code == "TBM_SERVICE_REGISTRY_CHANGED"


@pytest.mark.parametrize("failure_point", ("clock", "retrieval"))
def test_service_sanitizes_callback_failures(failure_point: str):
    registry = _registry()

    def secret_failure() -> str:
        raise RuntimeError("secret callback path")

    service = AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=_Writer(),
        clock=secret_failure if failure_point == "clock" else lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )

    def retrieve(_scope: AuthorizedRetrievalScope) -> None:
        if failure_point == "retrieval":
            raise RuntimeError("secret callback path")
        pytest.fail("retrieval must not be called")

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        service.authorize_retrieval(_context(registry), retrieve)

    expected = (
        "TBM_SERVICE_REQUEST_CONTEXT_INVALID"
        if failure_point == "clock"
        else "TBM_SERVICE_RETRIEVAL_FAILED"
    )
    assert raised.value.code == expected
    assert "secret" not in str(raised.value)


def test_service_sanitizes_callback_supplied_service_error():
    registry = _registry()
    service = _service(registry, _Writer())

    def retrieve(_scope: AuthorizedRetrievalScope) -> None:
        raise AuthenticatedServiceV3Error(
            "TBM_UNTRUSTED_CALLBACK",
            "secret callback token",
        )

    with pytest.raises(AuthenticatedServiceV3Error) as raised:
        service.authorize_retrieval(_context(registry), retrieve)

    assert raised.value.code == "TBM_SERVICE_RETRIEVAL_FAILED"
    assert "secret" not in str(raised.value)


def test_service_rejects_invalid_provider_and_request_factory_outputs():
    writer = _Writer()
    invalid_registry = AuthenticatedRetrievalService(
        registry_provider=lambda: object(),  # type: ignore[arg-type]
        decision_writer=writer,
        clock=lambda: NOW,
        request_id_factory=lambda: "authorization_request_001",
    )
    with pytest.raises(AuthenticatedServiceV3Error) as invalid:
        invalid_registry.authorize_retrieval(
            _context(_registry()),
            lambda _scope: None,
        )
    assert invalid.value.code == "TBM_SERVICE_REGISTRY_INVALID"

    registry = _registry()
    invalid_request = AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=writer,
        clock=lambda: NOW,
        request_id_factory=lambda: "",
    )
    with pytest.raises(AuthenticatedServiceV3Error) as malformed:
        invalid_request.authorize_retrieval(
            _context(registry),
            lambda _scope: None,
        )
    assert malformed.value.code == "TBM_SERVICE_REQUEST_CONTEXT_INVALID"
    assert writer.decisions == []


def test_service_v3_public_exports_are_available():
    assert tbm.AuthenticatedRetrievalService is AuthenticatedRetrievalService
    assert tbm.AuthenticatedServiceContext is AuthenticatedServiceContext
    assert tbm.AuthorizationDeniedError is AuthorizationDeniedError
    assert "AuthenticatedRetrievalService" in tbm.__all__
