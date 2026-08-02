from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .authorization_v3 import (
    AgentClientIdentity,
    AuthorizationDecision,
    AuthorizationPermission,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    PrincipalIdentity,
    authorize,
)
from .contracts_v3 import V3ContractError
from .entity_registry_v3 import EntityRegistrySnapshot


_RetrievalValue = TypeVar("_RetrievalValue")


class AuthenticatedServiceV3Error(V3ContractError):
    """Stable, sanitized failure at the authenticated service boundary."""


class AuthorizationAppendReceipt(Protocol):
    policy_sha256: str
    authorization_event_id: str
    decision_inserted: bool


class AuthorizationDecisionWriter(Protocol):
    """Persistence authority required before retrieval may start."""

    def append_decision(
        self,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
    ) -> AuthorizationAppendReceipt: ...

    def load_decision(
        self,
        authorization_event_id: str,
    ) -> AuthorizationDecision: ...


@dataclass(frozen=True)
class AuthenticatedServiceContext:
    """Identity and target facts produced by a trusted service authenticator."""

    principal: PrincipalIdentity
    agent_client: AgentClientIdentity
    tenant_id: str
    repository_reference: str
    environment_id: str

    def __post_init__(self) -> None:
        if type(self.principal) is not PrincipalIdentity:
            _reject_context()
        if type(self.agent_client) is not AgentClientIdentity:
            _reject_context()
        for value in (
            self.tenant_id,
            self.repository_reference,
            self.environment_id,
        ):
            if type(value) is not str or not value:
                _reject_context()


@dataclass(frozen=True)
class AuthorizedRetrievalScope:
    authorization_event_id: str
    organization_id: str
    principal_id: str
    agent_client_id: str
    tenant_id: str
    repository_id: str
    environment_id: str


@dataclass(frozen=True)
class AuthorizedRetrievalResult(Generic[_RetrievalValue]):
    decision: AuthorizationDecision
    scope: AuthorizedRetrievalScope
    value: _RetrievalValue


class AuthorizationDeniedError(AuthenticatedServiceV3Error):
    """Durable authorization denial; retrieval was not called."""

    def __init__(self, decision: AuthorizationDecision) -> None:
        self.decision = decision
        super().__init__(
            "TBM_SERVICE_AUTHORIZATION_DENIED",
            "authorization denied before retrieval",
        )


class AuthenticatedRetrievalService:
    """Enforce authenticated context and durable authorization before retrieval."""

    def __init__(
        self,
        *,
        registry_provider: Callable[[], EntityRegistrySnapshot],
        decision_writer: AuthorizationDecisionWriter,
        clock: Callable[[], str],
        request_id_factory: Callable[[], str],
    ) -> None:
        if not callable(registry_provider):
            raise TypeError("registry_provider must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(request_id_factory):
            raise TypeError("request_id_factory must be callable")
        self._registry_provider = registry_provider
        self._decision_writer = decision_writer
        self._clock = clock
        self._request_id_factory = request_id_factory

    def authorize_retrieval(
        self,
        context: AuthenticatedServiceContext,
        retrieve: Callable[[AuthorizedRetrievalScope], _RetrievalValue],
    ) -> AuthorizedRetrievalResult[_RetrievalValue]:
        return self.authorize_permission(
            context,
            permission="memory:retrieve",
            operation=retrieve,
        )

    def authorize_permission(
        self,
        context: AuthenticatedServiceContext,
        *,
        permission: AuthorizationPermission,
        operation: Callable[[AuthorizedRetrievalScope], _RetrievalValue],
    ) -> AuthorizedRetrievalResult[_RetrievalValue]:
        """Persist and read back one exact authorization before an operation."""
        if type(context) is not AuthenticatedServiceContext:
            _reject_context()
        if not callable(operation):
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_RETRIEVAL_CALLBACK_INVALID",
                "authorized operation callback is invalid",
            )

        registry = self._load_registry()
        policy = registry.authorization_policy
        self._verify_authenticated_records(policy, context)
        try:
            requested_at = self._clock()
            request = self._authorization_request(
                context,
                requested_at,
                permission,
            )
            decision = authorize(policy, request, decided_at=requested_at)
        except AuthenticatedServiceV3Error:
            raise
        except Exception as error:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_REQUEST_CONTEXT_INVALID",
                "server-owned authorization request context is invalid",
            ) from error
        try:
            receipt = self._decision_writer.append_decision(
                policy,
                request,
                decision,
            )
            self._verify_receipt(receipt, policy, decision)
            if (
                self._decision_writer.load_decision(decision.authorization_event_id)
                != decision
            ):
                raise AuthenticatedServiceV3Error(
                    "TBM_SERVICE_AUTHORIZATION_RECEIPT_INVALID",
                    "authorization authority did not retain the exact decision",
                )
        except AuthenticatedServiceV3Error:
            raise
        except Exception as error:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_AUTHORIZATION_PERSIST_FAILED",
                "authorization decision could not be persisted",
            ) from error
        if not decision.allowed:
            raise AuthorizationDeniedError(decision)

        current_registry = self._load_registry()
        if current_registry.registry_sha256 != registry.registry_sha256:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_REGISTRY_CHANGED",
                "entity registry changed during authorization",
            )
        scope = self._authorized_scope(current_registry, context, decision)
        try:
            value = operation(scope)
        except Exception as error:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_RETRIEVAL_FAILED",
                "authorized retrieval failed",
            ) from error
        return AuthorizedRetrievalResult(
            decision=decision,
            scope=scope,
            value=value,
        )

    def verify_authorized_scope(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        *,
        permission: AuthorizationPermission,
    ) -> AuthorizationDecision:
        """Reload and verify an existing allowed decision without appending one."""
        if (
            type(context) is not AuthenticatedServiceContext
            or type(scope) is not AuthorizedRetrievalScope
        ):
            _reject_context()
        decision, recovered = self._recover_authorized_scope_and_decision(
            context,
            scope.authorization_event_id,
            permission=permission,
        )
        if recovered != scope:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_AUTHORIZATION_SCOPE_INVALID",
                "authorized operation scope could not be verified",
            )
        return decision

    def recover_authorized_scope(
        self,
        context: AuthenticatedServiceContext,
        authorization_event_id: str,
        *,
        permission: AuthorizationPermission,
    ) -> AuthorizedRetrievalScope:
        """Reconstruct a current server-owned scope from one retained decision."""
        if (
            type(context) is not AuthenticatedServiceContext
            or type(authorization_event_id) is not str
            or not authorization_event_id
        ):
            _reject_context()
        _, scope = self._recover_authorized_scope_and_decision(
            context,
            authorization_event_id,
            permission=permission,
        )
        return scope

    def _recover_authorized_scope_and_decision(
        self,
        context: AuthenticatedServiceContext,
        authorization_event_id: str,
        *,
        permission: AuthorizationPermission,
    ) -> tuple[AuthorizationDecision, AuthorizedRetrievalScope]:
        registry = self._load_registry()
        policy = registry.authorization_policy
        self._verify_authenticated_records(policy, context)
        decision = self._load_allowed_decision(
            policy,
            authorization_event_id,
            permission,
        )
        try:
            scope = self._authorized_scope(
                registry,
                context,
                decision,
            )
        except AuthenticatedServiceV3Error:
            raise
        except Exception as error:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_AUTHORIZATION_SCOPE_INVALID",
                "authorized operation scope could not be verified",
            ) from error
        current_registry = self._load_registry()
        if current_registry.registry_sha256 != registry.registry_sha256:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_REGISTRY_CHANGED",
                "entity registry changed during authorization recovery",
            )
        return decision, scope

    def _load_allowed_decision(
        self,
        policy: AuthorizationPolicyBundle,
        authorization_event_id: str,
        permission: AuthorizationPermission,
    ) -> AuthorizationDecision:
        try:
            decision = self._decision_writer.load_decision(
                authorization_event_id
            )
        except Exception as error:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_AUTHORIZATION_SCOPE_INVALID",
                "authorized operation scope could not be verified",
            ) from error
        if (
            type(decision) is not AuthorizationDecision
            or decision.authorization_event_id != authorization_event_id
            or not decision.allowed
            or decision.permission != permission
            or decision.policy_sha256 != policy.policy_sha256
        ):
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_AUTHORIZATION_SCOPE_INVALID",
                "authorized operation scope could not be verified",
            )
        return decision

    def _load_registry(self) -> EntityRegistrySnapshot:
        try:
            registry = self._registry_provider()
        except Exception as error:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_REGISTRY_UNAVAILABLE",
                "entity registry could not be loaded",
            ) from error
        if type(registry) is not EntityRegistrySnapshot:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_REGISTRY_INVALID",
                "entity registry provider returned an invalid record",
            )
        return registry

    @staticmethod
    def _verify_authenticated_records(
        policy: AuthorizationPolicyBundle,
        context: AuthenticatedServiceContext,
    ) -> None:
        principal = next(
            (
                item
                for item in policy.principals
                if item.principal_id == context.principal.principal_id
            ),
            None,
        )
        if principal is not None and principal != context.principal:
            _reject_context()
        client = next(
            (
                item
                for item in policy.agent_clients
                if item.agent_client_id == context.agent_client.agent_client_id
            ),
            None,
        )
        if client is not None and client != context.agent_client:
            _reject_context()

    def _authorization_request(
        self,
        context: AuthenticatedServiceContext,
        requested_at: str,
        permission: AuthorizationPermission,
    ) -> AuthorizationRequest:
        try:
            return AuthorizationRequest(
                request_id=self._request_id_factory(),
                principal_id=context.principal.principal_id,
                agent_client_id=context.agent_client.agent_client_id,
                tenant_id=context.tenant_id,
                repository_reference=context.repository_reference,
                permission=permission,
                requested_at=requested_at,
            )
        except (TypeError, ValueError) as error:
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_REQUEST_CONTEXT_INVALID",
                "server-owned authorization request context is invalid",
            ) from error

    @staticmethod
    def _verify_receipt(
        receipt: AuthorizationAppendReceipt,
        policy: AuthorizationPolicyBundle,
        decision: AuthorizationDecision,
    ) -> None:
        if (
            getattr(receipt, "policy_sha256", None) != policy.policy_sha256
            or getattr(receipt, "authorization_event_id", None)
            != decision.authorization_event_id
            or type(getattr(receipt, "decision_inserted", None)) is not bool
        ):
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_AUTHORIZATION_RECEIPT_INVALID",
                "authorization authority returned an invalid persistence receipt",
            )

    @staticmethod
    def _authorized_scope(
        registry: EntityRegistrySnapshot,
        context: AuthenticatedServiceContext,
        decision: AuthorizationDecision,
    ) -> AuthorizedRetrievalScope:
        environment = next(
            (
                item
                for item in registry.environments
                if item.environment_id == context.environment_id
            ),
            None,
        )
        tenant = next(
            (
                item
                for item in registry.tenants
                if item.tenant_id == context.tenant_id
            ),
            None,
        )
        if (
            environment is None
            or tenant is None
            or tenant.status != "active"
            or environment.status != "active"
            or environment.tenant_id != context.tenant_id
            or environment.repository_id is None
            or environment.repository_id != decision.repository_id
            or not AuthenticatedRetrievalService._repository_reference_matches(
                registry.authorization_policy,
                context,
                decision.repository_id,
            )
            or decision.tenant_id != context.tenant_id
            or decision.repository_id is None
            or decision.principal_id != context.principal.principal_id
            or decision.agent_client_id != context.agent_client.agent_client_id
        ):
            raise AuthenticatedServiceV3Error(
                "TBM_SERVICE_ENTITY_CONTEXT_REJECTED",
                "service entity context was rejected before retrieval",
            )
        return AuthorizedRetrievalScope(
            authorization_event_id=decision.authorization_event_id,
            organization_id=tenant.organization_id,
            principal_id=decision.principal_id,
            agent_client_id=decision.agent_client_id,
            tenant_id=context.tenant_id,
            repository_id=decision.repository_id,
            environment_id=environment.environment_id,
        )

    @staticmethod
    def _repository_reference_matches(
        policy: AuthorizationPolicyBundle,
        context: AuthenticatedServiceContext,
        repository_id: str | None,
    ) -> bool:
        if repository_id is None:
            return False
        if any(
            target.tenant_id == context.tenant_id
            and target.repository_id == repository_id
            and context.repository_reference == repository_id
            for target in policy.repository_tenants
        ):
            return True
        return any(
            alias.tenant_id == context.tenant_id
            and alias.repository_id == repository_id
            and alias.alias == context.repository_reference
            for alias in policy.repository_aliases
        )


def _reject_context() -> None:
    raise AuthenticatedServiceV3Error(
        "TBM_SERVICE_AUTHENTICATION_CONTEXT_REJECTED",
        "authenticated service context does not match the active registry",
    )
