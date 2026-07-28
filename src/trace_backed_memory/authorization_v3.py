from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Literal, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import (
    AuthorizationScope,
    CanonicalRepository,
    RepositoryProvider,
    ScopeKind,
    V3ContractError,
    canonical_sha256,
)
from .policy import (
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)


AUTHORIZATION_POLICY_VERSION = "tbm.authorization-policy.v3"
AUTHORIZATION_DECISION_VERSION = "tbm.authorization-decision.v3"
AUTHORIZATION_JSON_MAX_BYTES = 1024 * 1024
AUTHORIZATION_JSON_MAX_DEPTH = 32
AUTHORIZATION_JSON_MAX_NODES = 25_000
AUTHORIZATION_MAX_REGISTRY_ITEMS = 10_000
AUTHORIZATION_MAX_BINDING_PERMISSIONS = 32

IdentityStatus = Literal["active", "disabled"]
BindingStatus = Literal["active", "revoked"]
AgentClientKind = Literal["local_agent", "service", "sdk", "mcp", "worker"]
AuthorizationPermission = Literal[
    "memory:retrieve",
    "memory:inject",
    "memory:create",
    "memory:review",
    "memory:verify",
    "memory:activate",
    "gate_session:create",
    "gate_session:transition",
    "artifact:read",
    "artifact:write",
    "tenant:audit_read",
    "policy:create_global",
    "policy:approve_global",
    "platform:audit_read",
    "platform:admin",
]
AuthorizationReason = Literal[
    "allowed",
    "unknown_principal",
    "principal_disabled",
    "principal_tenant_mismatch",
    "unknown_agent_client",
    "agent_client_disabled",
    "agent_client_tenant_mismatch",
    "unknown_repository",
    "repository_tenant_mismatch",
    "no_matching_binding",
]

_IDENTITY_STATUSES = {"active", "disabled"}
_BINDING_STATUSES = {"active", "revoked"}
_AGENT_CLIENT_KINDS = {"local_agent", "service", "sdk", "mcp", "worker"}
_REPOSITORY_PERMISSIONS = {
    "memory:retrieve",
    "memory:inject",
    "memory:create",
    "memory:verify",
    "gate_session:create",
    "gate_session:transition",
    "artifact:read",
    "artifact:write",
}
_TENANT_OR_REPOSITORY_PERMISSIONS = {
    "memory:review",
    "memory:activate",
}
_TENANT_PERMISSIONS = {"tenant:audit_read"}
_GLOBAL_PERMISSIONS = {
    "policy:create_global",
    "policy:approve_global",
    "platform:audit_read",
}
_ADMIN_PERMISSION = "platform:admin"
AUTHORIZATION_PERMISSIONS = tuple(
    sorted(
        _REPOSITORY_PERMISSIONS
        | _TENANT_OR_REPOSITORY_PERMISSIONS
        | _TENANT_PERMISSIONS
        | _GLOBAL_PERMISSIONS
        | {_ADMIN_PERMISSION}
    )
)
_PERMISSIONS = frozenset(AUTHORIZATION_PERMISSIONS)
_REASONS = {
    "allowed",
    "unknown_principal",
    "principal_disabled",
    "principal_tenant_mismatch",
    "unknown_agent_client",
    "agent_client_disabled",
    "agent_client_tenant_mismatch",
    "unknown_repository",
    "repository_tenant_mismatch",
    "no_matching_binding",
}
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])"
)
_POLICY_FIELDS = frozenset(
    {
        "contract_version",
        "policy_version",
        "principals",
        "agent_clients",
        "repositories",
        "repository_tenants",
        "repository_aliases",
        "role_bindings",
    }
)
_PRINCIPAL_FIELDS = frozenset(
    {
        "principal_id",
        "issuer",
        "subject_hash",
        "tenant_id",
        "status",
    }
)
_CLIENT_FIELDS = frozenset(
    {
        "agent_client_id",
        "tenant_id",
        "client_kind",
        "status",
    }
)
_REPOSITORY_FIELDS = frozenset(
    {
        "repository_id",
        "provider",
        "provider_repository_id",
        "canonical_locator_hash",
        "display_name",
        "legacy_aliases",
    }
)
_REPOSITORY_TENANT_FIELDS = frozenset({"repository_id", "tenant_id"})
_ALIAS_FIELDS = frozenset(
    {"alias", "repository_id", "tenant_id", "source"}
)
_BINDING_FIELDS = frozenset(
    {
        "binding_id",
        "principal_id",
        "agent_client_id",
        "role_name",
        "scope",
        "permissions",
        "status",
        "valid_from",
        "expires_at",
    }
)
_SCOPE_FIELDS = frozenset(
    {"kind", "tenant_id", "repository_id", "attributes"}
)
_DECISION_FIELDS = frozenset(
    {
        "contract_version",
        "authorization_event_id",
        "request_id",
        "request_sha256",
        "policy_sha256",
        "principal_id",
        "agent_client_id",
        "tenant_id",
        "repository_id",
        "permission",
        "allowed",
        "reason",
        "matched_binding_ids",
        "decided_at",
    }
)


class AuthorizationContractError(V3ContractError):
    """Stable failure for malformed authorization v3 contracts."""


@dataclass(frozen=True)
class PrincipalIdentity:
    principal_id: str
    issuer: str
    subject_hash: str
    tenant_id: str | None
    status: IdentityStatus = "active"

    def __post_init__(self) -> None:
        _identifier(self.principal_id, "principal_id")
        _metadata(self.issuer, "issuer")
        _digest(self.subject_hash, "subject_hash")
        _optional_identifier(self.tenant_id, "tenant_id")
        if (
            type(self.status) is not str
            or self.status not in _IDENTITY_STATUSES
        ):
            _invalid("principal status must be active or disabled")

    def to_dict(self) -> dict[str, object]:
        return {
            "principal_id": self.principal_id,
            "issuer": self.issuer,
            "subject_hash": self.subject_hash,
            "tenant_id": self.tenant_id,
            "status": self.status,
        }


@dataclass(frozen=True)
class AgentClientIdentity:
    agent_client_id: str
    tenant_id: str | None
    client_kind: AgentClientKind
    status: IdentityStatus = "active"

    def __post_init__(self) -> None:
        _identifier(self.agent_client_id, "agent_client_id")
        _optional_identifier(self.tenant_id, "tenant_id")
        if (
            type(self.client_kind) is not str
            or self.client_kind not in _AGENT_CLIENT_KINDS
        ):
            _invalid("client_kind must be a supported agent client kind")
        if (
            type(self.status) is not str
            or self.status not in _IDENTITY_STATUSES
        ):
            _invalid("agent client status must be active or disabled")

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_client_id": self.agent_client_id,
            "tenant_id": self.tenant_id,
            "client_kind": self.client_kind,
            "status": self.status,
        }


@dataclass(frozen=True)
class RepositoryTenantBinding:
    repository_id: str
    tenant_id: str

    def __post_init__(self) -> None:
        _identifier(self.repository_id, "repository_id")
        _identifier(self.tenant_id, "tenant_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "repository_id": self.repository_id,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True)
class RepositoryAlias:
    alias: str
    repository_id: str
    tenant_id: str
    source: str

    def __post_init__(self) -> None:
        _metadata(self.alias, "alias")
        _identifier(self.repository_id, "repository_id")
        _identifier(self.tenant_id, "tenant_id")
        _metadata(self.source, "source")

    def to_dict(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "repository_id": self.repository_id,
            "tenant_id": self.tenant_id,
            "source": self.source,
        }


@dataclass(frozen=True)
class RoleBinding:
    binding_id: str
    principal_id: str
    agent_client_id: str
    role_name: str
    scope: AuthorizationScope
    permissions: tuple[AuthorizationPermission, ...]
    status: BindingStatus
    valid_from: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.binding_id, "binding_id")
        _identifier(self.principal_id, "principal_id")
        _identifier(self.agent_client_id, "agent_client_id")
        _metadata(self.role_name, "role_name")
        if type(self.scope) is not AuthorizationScope:
            _invalid("scope must be exactly AuthorizationScope")
        if (
            type(self.permissions) is not tuple
            or not self.permissions
            or len(self.permissions) > AUTHORIZATION_MAX_BINDING_PERMISSIONS
            or any(
                type(permission) is not str
                or permission not in _PERMISSIONS
                for permission in self.permissions
            )
            or len(set(self.permissions)) != len(self.permissions)
        ):
            _invalid("permissions must be a unique supported tuple")
        if (
            type(self.status) is not str
            or self.status not in _BINDING_STATUSES
        ):
            _invalid("binding status must be active or revoked")
        valid_from = _timestamp(self.valid_from, "valid_from")
        if self.expires_at is not None:
            expires_at = _timestamp(self.expires_at, "expires_at")
            if expires_at <= valid_from:
                _invalid("expires_at must be later than valid_from")
        if self.scope.kind == "repository":
            invalid = set(self.permissions) - (
                _REPOSITORY_PERMISSIONS
                | _TENANT_OR_REPOSITORY_PERMISSIONS
                | {_ADMIN_PERMISSION}
            )
            if invalid:
                _invalid("repository binding contains a broader permission")
        if self.scope.kind == "tenant":
            invalid = set(self.permissions) & _GLOBAL_PERMISSIONS
            if invalid:
                _invalid("tenant binding contains a global permission")
        if _ADMIN_PERMISSION in self.permissions and self.scope.kind != "global":
            _invalid("platform:admin requires global scope")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "principal_id": self.principal_id,
            "agent_client_id": self.agent_client_id,
            "role_name": self.role_name,
            "scope": self.scope.to_dict(),
            "permissions": sorted(self.permissions),
            "status": self.status,
            "valid_from": canonical_rfc3339(self.valid_from),
            "expires_at": (
                canonical_rfc3339(self.expires_at)
                if self.expires_at is not None
                else None
            ),
        }


@dataclass(frozen=True)
class AuthorizationPolicyBundle:
    policy_version: str
    principals: tuple[PrincipalIdentity, ...]
    agent_clients: tuple[AgentClientIdentity, ...]
    repositories: tuple[CanonicalRepository, ...]
    repository_tenants: tuple[RepositoryTenantBinding, ...]
    repository_aliases: tuple[RepositoryAlias, ...]
    role_bindings: tuple[RoleBinding, ...]
    contract_version: str = AUTHORIZATION_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != AUTHORIZATION_POLICY_VERSION:
            _invalid(
                f"contract_version must be {AUTHORIZATION_POLICY_VERSION}"
            )
        _metadata(self.policy_version, "policy_version")
        for name, values, expected_type in (
            ("principals", self.principals, PrincipalIdentity),
            ("agent_clients", self.agent_clients, AgentClientIdentity),
            ("repositories", self.repositories, CanonicalRepository),
            (
                "repository_tenants",
                self.repository_tenants,
                RepositoryTenantBinding,
            ),
            ("repository_aliases", self.repository_aliases, RepositoryAlias),
            ("role_bindings", self.role_bindings, RoleBinding),
        ):
            _record_tuple(values, expected_type, name)
        principals = _unique_by(
            self.principals,
            "principal_id",
            "principal_id",
        )
        clients = _unique_by(
            self.agent_clients,
            "agent_client_id",
            "agent_client_id",
        )
        repositories = _unique_by(
            self.repositories,
            "repository_id",
            "repository_id",
        )
        if any(
            len(repository.legacy_aliases)
            > AUTHORIZATION_MAX_REGISTRY_ITEMS
            for repository in self.repositories
        ):
            _invalid("repository legacy_aliases exceed authorization bound")
        targets = _unique_by(
            self.repository_tenants,
            "repository_id",
            "repository tenant repository_id",
        )
        _unique_by(
            self.role_bindings,
            "binding_id",
            "binding_id",
        )
        if set(repositories) != set(targets):
            _invalid("every canonical repository requires one tenant binding")
        aliases: set[tuple[str, str]] = set()
        canonical_refs = {
            (target.tenant_id, target.repository_id)
            for target in self.repository_tenants
        }
        for alias in self.repository_aliases:
            key = (alias.tenant_id, alias.alias)
            if key in aliases or key in canonical_refs:
                _invalid("repository aliases must be unambiguous")
            aliases.add(key)
            target = targets.get(alias.repository_id)
            if target is None or target.tenant_id != alias.tenant_id:
                _invalid("repository alias target is unknown or cross-tenant")
        for binding in self.role_bindings:
            if binding.principal_id not in principals:
                _invalid("role binding references an unknown principal")
            if binding.agent_client_id not in clients:
                _invalid("role binding references an unknown agent client")
            _validate_binding_scope(binding, targets)

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "policy_version": self.policy_version,
            "principals": [
                item.to_dict()
                for item in sorted(
                    self.principals,
                    key=lambda item: item.principal_id,
                )
            ],
            "agent_clients": [
                item.to_dict()
                for item in sorted(
                    self.agent_clients,
                    key=lambda item: item.agent_client_id,
                )
            ],
            "repositories": [
                item.to_dict()
                for item in sorted(
                    self.repositories,
                    key=lambda item: item.repository_id,
                )
            ],
            "repository_tenants": [
                item.to_dict()
                for item in sorted(
                    self.repository_tenants,
                    key=lambda item: item.repository_id,
                )
            ],
            "repository_aliases": [
                item.to_dict()
                for item in sorted(
                    self.repository_aliases,
                    key=lambda item: (item.tenant_id, item.alias),
                )
            ],
            "role_bindings": [
                item.to_dict()
                for item in sorted(
                    self.role_bindings,
                    key=lambda item: item.binding_id,
                )
            ],
        }


@dataclass(frozen=True)
class AuthorizationRequest:
    request_id: str
    principal_id: str
    agent_client_id: str
    tenant_id: str | None
    repository_reference: str | None
    permission: AuthorizationPermission
    requested_at: str

    def __post_init__(self) -> None:
        _identifier(self.request_id, "request_id")
        _identifier(self.principal_id, "principal_id")
        _identifier(self.agent_client_id, "agent_client_id")
        _optional_identifier(self.tenant_id, "tenant_id")
        _optional_metadata(
            self.repository_reference,
            "repository_reference",
        )
        if (
            type(self.permission) is not str
            or self.permission not in _PERMISSIONS
        ):
            _invalid("permission must be supported")
        _timestamp(self.requested_at, "requested_at")
        if self.permission in _REPOSITORY_PERMISSIONS:
            if self.tenant_id is None or self.repository_reference is None:
                _invalid(
                    "repository permission requires tenant and repository"
                )
        elif self.permission in _TENANT_OR_REPOSITORY_PERMISSIONS:
            if self.tenant_id is None:
                _invalid(
                    "tenant or repository permission requires tenant_id"
                )
        elif self.permission in _TENANT_PERMISSIONS:
            if self.tenant_id is None or self.repository_reference is not None:
                _invalid("tenant permission requires only tenant_id")
        elif (
            self.tenant_id is not None
            or self.repository_reference is not None
        ):
            _invalid("global permission forbids tenant and repository")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "principal_id": self.principal_id,
            "agent_client_id": self.agent_client_id,
            "tenant_id": self.tenant_id,
            "repository_reference": self.repository_reference,
            "permission": self.permission,
            "requested_at": canonical_rfc3339(self.requested_at),
        }


@dataclass(frozen=True)
class AuthorizationDecision:
    authorization_event_id: str
    request_id: str
    request_sha256: str
    policy_sha256: str
    principal_id: str
    agent_client_id: str
    tenant_id: str | None
    repository_id: str | None
    permission: AuthorizationPermission
    allowed: bool
    reason: AuthorizationReason
    matched_binding_ids: tuple[str, ...]
    decided_at: str
    contract_version: str = AUTHORIZATION_DECISION_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != AUTHORIZATION_DECISION_VERSION:
            _invalid(
                f"contract_version must be {AUTHORIZATION_DECISION_VERSION}"
            )
        if (
            type(self.authorization_event_id) is not str
            or re.fullmatch(
                r"authz_sha256_[0-9a-f]{64}",
                self.authorization_event_id,
            )
            is None
        ):
            _invalid("authorization_event_id must be content-derived")
        _identifier(self.request_id, "request_id")
        _digest(self.request_sha256, "request_sha256")
        _digest(self.policy_sha256, "policy_sha256")
        _identifier(self.principal_id, "principal_id")
        _identifier(self.agent_client_id, "agent_client_id")
        _optional_identifier(self.tenant_id, "tenant_id")
        _optional_identifier(self.repository_id, "repository_id")
        if (
            type(self.permission) is not str
            or self.permission not in _PERMISSIONS
        ):
            _invalid("permission must be supported")
        if type(self.allowed) is not bool:
            _invalid("allowed must be a boolean")
        if type(self.reason) is not str or self.reason not in _REASONS:
            _invalid("authorization reason must be supported")
        _identifier_tuple(
            self.matched_binding_ids,
            "matched_binding_ids",
        )
        if self.matched_binding_ids != tuple(sorted(self.matched_binding_ids)):
            _invalid("matched_binding_ids must be sorted")
        if self.allowed != (self.reason == "allowed"):
            _invalid("allowed and reason disagree")
        if self.allowed != bool(self.matched_binding_ids):
            _invalid("allowed decision requires matched bindings")
        _timestamp(self.decided_at, "decided_at")
        if self.authorization_event_id != _authorization_event_id(
            self._content()
        ):
            _invalid("authorization_event_id does not match decision content")

    def _content(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "policy_sha256": self.policy_sha256,
            "principal_id": self.principal_id,
            "agent_client_id": self.agent_client_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "permission": self.permission,
            "allowed": self.allowed,
            "reason": self.reason,
            "matched_binding_ids": sorted(self.matched_binding_ids),
            "decided_at": canonical_rfc3339(self.decided_at),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._content(),
            "authorization_event_id": self.authorization_event_id,
        }


def authorize(
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    *,
    decided_at: str,
) -> AuthorizationDecision:
    """Evaluate identity and scope authorization without applicability logic."""

    if type(policy) is not AuthorizationPolicyBundle:
        _invalid("policy must be exactly AuthorizationPolicyBundle")
    if type(request) is not AuthorizationRequest:
        _invalid("request must be exactly AuthorizationRequest")
    decided = _timestamp(decided_at, "decided_at")
    if decided < _timestamp(request.requested_at, "requested_at"):
        _invalid("decided_at must not precede requested_at")

    principals = {item.principal_id: item for item in policy.principals}
    clients = {item.agent_client_id: item for item in policy.agent_clients}
    principal = principals.get(request.principal_id)
    client = clients.get(request.agent_client_id)
    repository_id: str | None = None

    if principal is None:
        return _decision(policy, request, decided_at, None, "unknown_principal")
    if principal.status != "active":
        return _decision(policy, request, decided_at, None, "principal_disabled")
    if client is None:
        return _decision(
            policy,
            request,
            decided_at,
            None,
            "unknown_agent_client",
        )
    if client.status != "active":
        return _decision(
            policy,
            request,
            decided_at,
            None,
            "agent_client_disabled",
        )
    if request.tenant_id is None:
        if principal.tenant_id is not None:
            return _decision(
                policy,
                request,
                decided_at,
                None,
                "principal_tenant_mismatch",
            )
        if client.tenant_id is not None:
            return _decision(
                policy,
                request,
                decided_at,
                None,
                "agent_client_tenant_mismatch",
            )
    else:
        if (
            principal.tenant_id is not None
            and principal.tenant_id != request.tenant_id
        ):
            return _decision(
                policy,
                request,
                decided_at,
                None,
                "principal_tenant_mismatch",
            )
        if (
            client.tenant_id is not None
            and client.tenant_id != request.tenant_id
        ):
            return _decision(
                policy,
                request,
                decided_at,
                None,
                "agent_client_tenant_mismatch",
            )
    if request.repository_reference is not None:
        repository_id = _resolve_repository(policy, request)
        if repository_id is None:
            return _decision(
                policy,
                request,
                decided_at,
                None,
                "unknown_repository",
            )
        targets = {
            item.repository_id: item.tenant_id
            for item in policy.repository_tenants
        }
        if targets[repository_id] != request.tenant_id:
            return _decision(
                policy,
                request,
                decided_at,
                repository_id,
                "repository_tenant_mismatch",
            )

    matched = tuple(
        sorted(
            binding.binding_id
            for binding in policy.role_bindings
            if _binding_authorizes(
                binding,
                request,
                repository_id,
                decided,
            )
        )
    )
    return _decision(
        policy,
        request,
        decided_at,
        repository_id,
        "allowed" if matched else "no_matching_binding",
        matched,
    )


def verify_authorization_decision(
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
) -> None:
    """Verify one decision against the exact policy and request it names."""

    if type(decision) is not AuthorizationDecision:
        _invalid("decision must be exactly AuthorizationDecision")
    expected = authorize(
        policy,
        request,
        decided_at=decision.decided_at,
    )
    if expected != decision:
        raise AuthorizationContractError(
            "TBM_AUTHORIZATION_DECISION_MISMATCH",
            "authorization decision does not match policy and request",
        )


def parse_authorization_policy(
    payload: Mapping[str, object],
) -> AuthorizationPolicyBundle:
    data = _strict_object(payload, _POLICY_FIELDS, "authorization policy")
    return AuthorizationPolicyBundle(
        contract_version=_string(data, "contract_version"),
        policy_version=_string(data, "policy_version"),
        principals=tuple(
            _parse_principal(item)
            for item in _object_list(data, "principals")
        ),
        agent_clients=tuple(
            _parse_client(item)
            for item in _object_list(data, "agent_clients")
        ),
        repositories=tuple(
            _parse_repository(item)
            for item in _object_list(data, "repositories")
        ),
        repository_tenants=tuple(
            _parse_repository_tenant(item)
            for item in _object_list(data, "repository_tenants")
        ),
        repository_aliases=tuple(
            _parse_alias(item)
            for item in _object_list(data, "repository_aliases")
        ),
        role_bindings=tuple(
            _parse_binding(item)
            for item in _object_list(data, "role_bindings")
        ),
    )


def parse_authorization_decision(
    payload: Mapping[str, object],
) -> AuthorizationDecision:
    data = _strict_object(payload, _DECISION_FIELDS, "authorization decision")
    return AuthorizationDecision(
        contract_version=_string(data, "contract_version"),
        authorization_event_id=_string(data, "authorization_event_id"),
        request_id=_string(data, "request_id"),
        request_sha256=_string(data, "request_sha256"),
        policy_sha256=_string(data, "policy_sha256"),
        principal_id=_string(data, "principal_id"),
        agent_client_id=_string(data, "agent_client_id"),
        tenant_id=_optional_string(data, "tenant_id"),
        repository_id=_optional_string(data, "repository_id"),
        permission=cast(
            AuthorizationPermission,
            _string(data, "permission"),
        ),
        allowed=_boolean(data, "allowed"),
        reason=cast(AuthorizationReason, _string(data, "reason")),
        matched_binding_ids=_string_tuple(data, "matched_binding_ids"),
        decided_at=_string(data, "decided_at"),
    )


def loads_authorization_policy(
    source: str | bytes,
) -> AuthorizationPolicyBundle:
    return parse_authorization_policy(
        _loads_json(source, "authorization policy")
    )


def loads_authorization_decision(
    source: str | bytes,
) -> AuthorizationDecision:
    return parse_authorization_decision(
        _loads_json(source, "authorization decision")
    )


def dumps_authorization_policy(policy: AuthorizationPolicyBundle) -> str:
    if type(policy) is not AuthorizationPolicyBundle:
        _invalid("policy must be exactly AuthorizationPolicyBundle")
    return _canonical_json(policy.to_dict())


def dumps_authorization_decision(decision: AuthorizationDecision) -> str:
    if type(decision) is not AuthorizationDecision:
        _invalid("decision must be exactly AuthorizationDecision")
    return _canonical_json(decision.to_dict())


def _decision(
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decided_at: str,
    repository_id: str | None,
    reason: AuthorizationReason,
    matched: tuple[str, ...] = (),
) -> AuthorizationDecision:
    content = {
        "contract_version": AUTHORIZATION_DECISION_VERSION,
        "request_id": request.request_id,
        "request_sha256": canonical_sha256(request.to_dict()),
        "policy_sha256": policy.policy_sha256,
        "principal_id": request.principal_id,
        "agent_client_id": request.agent_client_id,
        "tenant_id": request.tenant_id,
        "repository_id": repository_id,
        "permission": request.permission,
        "allowed": reason == "allowed",
        "reason": reason,
        "matched_binding_ids": sorted(matched),
        "decided_at": canonical_rfc3339(decided_at),
    }
    return AuthorizationDecision(
        authorization_event_id=_authorization_event_id(content),
        request_id=request.request_id,
        request_sha256=cast(str, content["request_sha256"]),
        policy_sha256=cast(str, content["policy_sha256"]),
        principal_id=request.principal_id,
        agent_client_id=request.agent_client_id,
        tenant_id=request.tenant_id,
        repository_id=repository_id,
        permission=request.permission,
        allowed=reason == "allowed",
        reason=reason,
        matched_binding_ids=matched,
        decided_at=decided_at,
    )


def _authorization_event_id(content: Mapping[str, object]) -> str:
    return "authz_sha256_" + canonical_sha256(content).removeprefix("sha256:")


def _resolve_repository(
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
) -> str | None:
    reference = request.repository_reference
    tenant_id = request.tenant_id
    for target in policy.repository_tenants:
        if (
            target.tenant_id == tenant_id
            and target.repository_id == reference
        ):
            return target.repository_id
    for alias in policy.repository_aliases:
        if alias.tenant_id == tenant_id and alias.alias == reference:
            return alias.repository_id
    return None


def _binding_authorizes(
    binding: RoleBinding,
    request: AuthorizationRequest,
    repository_id: str | None,
    decided_at: datetime,
) -> bool:
    if (
        binding.status != "active"
        or binding.principal_id != request.principal_id
        or binding.agent_client_id != request.agent_client_id
    ):
        return False
    if decided_at < parse_rfc3339(binding.valid_from):
        return False
    if (
        binding.expires_at is not None
        and decided_at >= parse_rfc3339(binding.expires_at)
    ):
        return False
    if (
        request.permission not in binding.permissions
        and _ADMIN_PERMISSION not in binding.permissions
    ):
        return False
    if binding.scope.kind == "global":
        return True
    if binding.scope.tenant_id != request.tenant_id:
        return False
    if binding.scope.kind == "tenant":
        return True
    return binding.scope.repository_id == repository_id


def _validate_binding_scope(
    binding: RoleBinding,
    targets: dict[object, object],
) -> None:
    if binding.scope.kind != "repository":
        return
    target = targets.get(binding.scope.repository_id)
    if target is None or target.tenant_id != binding.scope.tenant_id:
        _invalid("repository role binding has an unknown target")


def _record_tuple(values: object, expected: type[object], name: str) -> None:
    if (
        type(values) is not tuple
        or len(values) > AUTHORIZATION_MAX_REGISTRY_ITEMS
        or any(type(item) is not expected for item in values)
    ):
        _invalid(f"{name} must be a bounded exact-record tuple")


def _unique_by(
    values: tuple[object, ...],
    field_name: str,
    label: str,
) -> dict[object, object]:
    result: dict[object, object] = {}
    for item in values:
        key = getattr(item, field_name)
        if key in result:
            _invalid(f"{label} values must be unique")
        result[key] = item
    return result


def _parse_principal(payload: Mapping[str, object]) -> PrincipalIdentity:
    data = _strict_object(payload, _PRINCIPAL_FIELDS, "principal")
    return PrincipalIdentity(
        principal_id=_string(data, "principal_id"),
        issuer=_string(data, "issuer"),
        subject_hash=_string(data, "subject_hash"),
        tenant_id=_optional_string(data, "tenant_id"),
        status=cast(IdentityStatus, _string(data, "status")),
    )


def _parse_client(payload: Mapping[str, object]) -> AgentClientIdentity:
    data = _strict_object(payload, _CLIENT_FIELDS, "agent client")
    return AgentClientIdentity(
        agent_client_id=_string(data, "agent_client_id"),
        tenant_id=_optional_string(data, "tenant_id"),
        client_kind=cast(AgentClientKind, _string(data, "client_kind")),
        status=cast(IdentityStatus, _string(data, "status")),
    )


def _parse_repository(payload: Mapping[str, object]) -> CanonicalRepository:
    data = _strict_object(payload, _REPOSITORY_FIELDS, "repository")
    return CanonicalRepository(
        repository_id=_string(data, "repository_id"),
        provider=cast(RepositoryProvider, _string(data, "provider")),
        provider_repository_id=_string(data, "provider_repository_id"),
        canonical_locator_hash=_string(data, "canonical_locator_hash"),
        display_name=_string(data, "display_name"),
        legacy_aliases=_string_tuple(data, "legacy_aliases"),
    )


def _parse_repository_tenant(
    payload: Mapping[str, object],
) -> RepositoryTenantBinding:
    data = _strict_object(
        payload,
        _REPOSITORY_TENANT_FIELDS,
        "repository tenant",
    )
    return RepositoryTenantBinding(
        repository_id=_string(data, "repository_id"),
        tenant_id=_string(data, "tenant_id"),
    )


def _parse_alias(payload: Mapping[str, object]) -> RepositoryAlias:
    data = _strict_object(payload, _ALIAS_FIELDS, "repository alias")
    return RepositoryAlias(
        alias=_string(data, "alias"),
        repository_id=_string(data, "repository_id"),
        tenant_id=_string(data, "tenant_id"),
        source=_string(data, "source"),
    )


def _parse_binding(payload: Mapping[str, object]) -> RoleBinding:
    data = _strict_object(payload, _BINDING_FIELDS, "role binding")
    scope_data = _strict_object(data["scope"], _SCOPE_FIELDS, "scope")
    attributes = scope_data["attributes"]
    if type(attributes) is not dict or any(
        type(key) is not str or type(value) is not str
        for key, value in attributes.items()
    ):
        _invalid("scope attributes must be a string mapping")
    scope = AuthorizationScope(
        kind=cast(ScopeKind, _string(scope_data, "kind")),
        tenant_id=_optional_string(scope_data, "tenant_id"),
        repository_id=_optional_string(scope_data, "repository_id"),
        attributes=tuple(sorted(attributes.items())),
    )
    return RoleBinding(
        binding_id=_string(data, "binding_id"),
        principal_id=_string(data, "principal_id"),
        agent_client_id=_string(data, "agent_client_id"),
        role_name=_string(data, "role_name"),
        scope=scope,
        permissions=cast(
            tuple[AuthorizationPermission, ...],
            _string_tuple(data, "permissions"),
        ),
        status=cast(BindingStatus, _string(data, "status")),
        valid_from=_string(data, "valid_from"),
        expires_at=_optional_string(data, "expires_at"),
    )


def _loads_json(source: str | bytes, label: str) -> dict[str, object]:
    try:
        if type(source) is bytes:
            raw = decode_bounded_utf8(
                source,
                max_bytes=AUTHORIZATION_JSON_MAX_BYTES,
                description=label,
            )
        elif type(source) is str:
            raw = decode_bounded_utf8(
                source.encode("utf-8"),
                max_bytes=AUTHORIZATION_JSON_MAX_BYTES,
                description=label,
            )
        else:
            raise TypeError
        parsed = parse_bounded_json(
            raw,
            description=label,
            max_nodes=AUTHORIZATION_JSON_MAX_NODES,
            max_depth=AUTHORIZATION_JSON_MAX_DEPTH,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise AuthorizationContractError(
            "TBM_AUTHORIZATION_INVALID_JSON",
            f"{label} must be bounded strict JSON",
        ) from error
    if type(parsed) is not dict:
        _invalid(f"{label} must be a JSON object")
    return cast(dict[str, object], parsed)


def _strict_object(
    payload: object,
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(payload) is not dict or any(
        type(key) is not str for key in payload
    ):
        _invalid(f"{label} must be a JSON object")
    data = cast(dict[str, object], payload)
    unknown = sorted(set(data) - fields)
    missing = sorted(fields - set(data))
    if unknown:
        _invalid(f"{label} has unknown field: {unknown[0]}")
    if missing:
        _invalid(f"{label} is missing field: {missing[0]}")
    return data


def _object_list(
    data: dict[str, object],
    field_name: str,
) -> list[dict[str, object]]:
    value = data[field_name]
    if (
        type(value) is not list
        or len(value) > AUTHORIZATION_MAX_REGISTRY_ITEMS
        or any(type(item) is not dict for item in value)
    ):
        _invalid(f"{field_name} must be a bounded object array")
    return cast(list[dict[str, object]], value)


def _string(data: dict[str, object], field_name: str) -> str:
    value = data[field_name]
    if type(value) is not str:
        _invalid(f"{field_name} must be a string")
    return cast(str, value)


def _optional_string(
    data: dict[str, object],
    field_name: str,
) -> str | None:
    value = data[field_name]
    if value is not None and type(value) is not str:
        _invalid(f"{field_name} must be a string or null")
    return cast(str | None, value)


def _boolean(data: dict[str, object], field_name: str) -> bool:
    value = data[field_name]
    if type(value) is not bool:
        _invalid(f"{field_name} must be a boolean")
    return cast(bool, value)


def _string_tuple(
    data: dict[str, object],
    field_name: str,
) -> tuple[str, ...]:
    value = data[field_name]
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid(f"{field_name} must be a string array")
    return tuple(cast(list[str], value))


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise AuthorizationContractError(
            "TBM_AUTHORIZATION_INVALID",
            "authorization contract cannot be encoded as canonical JSON",
        ) from error


def _identifier(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_ID_MAX_CHARS
    ):
        _invalid(f"{field_name} must be a non-empty bounded identifier")
    _utf8(value, field_name)


def _optional_identifier(value: object, field_name: str) -> None:
    if value is not None:
        _identifier(value, field_name)


def _metadata(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > METADATA_VALUE_MAX_CHARS
    ):
        _invalid(f"{field_name} must be non-empty bounded metadata")
    _utf8(value, field_name)


def _optional_metadata(value: object, field_name: str) -> None:
    if value is not None:
        _metadata(value, field_name)


def _digest(value: object, field_name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{field_name} must be a sha256 digest")


def _identifier_tuple(values: object, field_name: str) -> None:
    if (
        type(values) is not tuple
        or len(values) > AUTHORIZATION_MAX_REGISTRY_ITEMS
        or len(set(values)) != len(values)
    ):
        _invalid(f"{field_name} must be a unique bounded tuple")
    for value in values:
        _identifier(value, field_name)


def _timestamp(value: object, field_name: str) -> datetime:
    try:
        if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
            raise ValueError
        return parse_rfc3339(cast(str, value))
    except (TypeError, ValueError) as error:
        raise AuthorizationContractError(
            "TBM_AUTHORIZATION_INVALID",
            f"{field_name} must be a timezone-aware RFC 3339 date-time",
        ) from error


def _utf8(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise AuthorizationContractError(
            "TBM_AUTHORIZATION_INVALID",
            f"{field_name} must be valid UTF-8",
        ) from error


def _invalid(message: str) -> None:
    if len(message) > MEMORY_DECISION_REASON_MAX_CHARS:
        message = message[:MEMORY_DECISION_REASON_MAX_CHARS]
    raise AuthorizationContractError("TBM_AUTHORIZATION_INVALID", message)
