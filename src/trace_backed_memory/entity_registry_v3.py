from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Literal, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from .authorization_v3 import (
    AUTHORIZATION_MAX_REGISTRY_ITEMS,
    AuthorizationContractError,
    AuthorizationPolicyBundle,
    parse_authorization_policy,
)
from .contracts_v3 import V3ContractError, canonical_sha256
from .policy import MEMORY_DECISION_REASON_MAX_CHARS, MEMORY_ID_MAX_CHARS, METADATA_VALUE_MAX_CHARS


ENTITY_REGISTRY_CONTRACT_VERSION = "tbm.entity-registry.v3"
ENTITY_REGISTRY_JSON_MAX_BYTES = 1024 * 1024
ENTITY_REGISTRY_JSON_MAX_DEPTH = 32
ENTITY_REGISTRY_JSON_MAX_NODES = 30_000

EntityStatus = Literal["active", "disabled"]
EnvironmentKind = Literal[
    "development",
    "test",
    "staging",
    "production",
    "ci",
    "other",
]

_ENTITY_STATUSES = {"active", "disabled"}
_ENVIRONMENT_KINDS = {
    "development",
    "test",
    "staging",
    "production",
    "ci",
    "other",
}
_REGISTRY_FIELDS = frozenset(
    {
        "contract_version",
        "registry_version",
        "organizations",
        "tenants",
        "environments",
        "authorization_policy",
    }
)
_ORGANIZATION_FIELDS = frozenset(
    {"organization_id", "display_name", "status"}
)
_TENANT_FIELDS = frozenset(
    {"tenant_id", "organization_id", "display_name", "status"}
)
_ENVIRONMENT_FIELDS = frozenset(
    {
        "environment_id",
        "tenant_id",
        "repository_id",
        "environment_kind",
        "display_name",
        "status",
        "attributes",
    }
)


class EntityRegistryContractError(V3ContractError):
    """Stable failure for malformed entity-registry v3 contracts."""


@dataclass(frozen=True)
class OrganizationIdentity:
    organization_id: str
    display_name: str
    status: EntityStatus = "active"

    def __post_init__(self) -> None:
        _identifier(self.organization_id, "organization_id")
        _metadata(self.display_name, "display_name")
        _status(self.status, "organization")

    def to_dict(self) -> dict[str, str]:
        return {
            "organization_id": self.organization_id,
            "display_name": self.display_name,
            "status": self.status,
        }


@dataclass(frozen=True)
class TenantEntity:
    tenant_id: str
    organization_id: str
    display_name: str
    status: EntityStatus = "active"

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "tenant_id")
        _identifier(self.organization_id, "organization_id")
        _metadata(self.display_name, "display_name")
        _status(self.status, "tenant")

    def to_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "display_name": self.display_name,
            "status": self.status,
        }


@dataclass(frozen=True)
class EnvironmentIdentity:
    environment_id: str
    tenant_id: str
    repository_id: str | None
    environment_kind: EnvironmentKind
    display_name: str
    status: EntityStatus = "active"
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.environment_id, "environment_id")
        _identifier(self.tenant_id, "tenant_id")
        if self.repository_id is not None:
            _identifier(self.repository_id, "repository_id")
        if (
            type(self.environment_kind) is not str
            or self.environment_kind not in _ENVIRONMENT_KINDS
        ):
            _invalid("environment_kind must be supported")
        _metadata(self.display_name, "display_name")
        _status(self.status, "environment")
        _attributes(self.attributes)

    def to_dict(self) -> dict[str, object]:
        return {
            "environment_id": self.environment_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_kind": self.environment_kind,
            "display_name": self.display_name,
            "status": self.status,
            "attributes": dict(sorted(self.attributes)),
        }


@dataclass(frozen=True)
class EntityRegistrySnapshot:
    registry_version: str
    organizations: tuple[OrganizationIdentity, ...]
    tenants: tuple[TenantEntity, ...]
    environments: tuple[EnvironmentIdentity, ...]
    authorization_policy: AuthorizationPolicyBundle
    contract_version: str = ENTITY_REGISTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != ENTITY_REGISTRY_CONTRACT_VERSION:
            _invalid(
                f"contract_version must be {ENTITY_REGISTRY_CONTRACT_VERSION}"
            )
        _metadata(self.registry_version, "registry_version")
        organizations = _records(
            self.organizations,
            OrganizationIdentity,
            "organizations",
            "organization_id",
        )
        tenants = _records(
            self.tenants,
            TenantEntity,
            "tenants",
            "tenant_id",
        )
        environments = _records(
            self.environments,
            EnvironmentIdentity,
            "environments",
            "environment_id",
        )
        if type(self.authorization_policy) is not AuthorizationPolicyBundle:
            _invalid(
                "authorization_policy must be exactly AuthorizationPolicyBundle"
            )
        for tenant in tenants.values():
            if tenant.organization_id not in organizations:
                _invalid("tenant references an unknown organization")
        policy = self.authorization_policy
        referenced_tenants: set[str] = {
            item.tenant_id for item in policy.repository_tenants
        }
        referenced_tenants.update(
            item.tenant_id
            for item in policy.repository_aliases
        )
        referenced_tenants.update(
            item.tenant_id
            for item in policy.principals
            if item.tenant_id is not None
        )
        referenced_tenants.update(
            item.tenant_id
            for item in policy.agent_clients
            if item.tenant_id is not None
        )
        referenced_tenants.update(
            item.scope.tenant_id
            for item in policy.role_bindings
            if item.scope.tenant_id is not None
        )
        if not referenced_tenants.issubset(tenants):
            _invalid("authorization policy references an unknown tenant")
        for tenant_id in referenced_tenants:
            tenant = cast(TenantEntity, tenants[tenant_id])
            organization = cast(
                OrganizationIdentity,
                organizations[tenant.organization_id],
            )
            if tenant.status != "active":
                _invalid("authorization policy references a disabled tenant")
            if organization.status != "active":
                _invalid(
                    "authorization policy references a tenant in a disabled "
                    "organization"
                )
        repository_tenants = {
            item.repository_id: item.tenant_id
            for item in policy.repository_tenants
        }
        for environment in environments.values():
            if environment.tenant_id not in tenants:
                _invalid("environment references an unknown tenant")
            if environment.repository_id is not None:
                repository_tenant = repository_tenants.get(
                    environment.repository_id
                )
                if repository_tenant is None:
                    _invalid("environment references an unknown repository")
                if repository_tenant != environment.tenant_id:
                    _invalid("environment repository is cross-tenant")

    @property
    def registry_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "registry_version": self.registry_version,
            "organizations": [
                item.to_dict()
                for item in sorted(
                    self.organizations,
                    key=lambda item: item.organization_id,
                )
            ],
            "tenants": [
                item.to_dict()
                for item in sorted(
                    self.tenants,
                    key=lambda item: item.tenant_id,
                )
            ],
            "environments": [
                item.to_dict()
                for item in sorted(
                    self.environments,
                    key=lambda item: item.environment_id,
                )
            ],
            "authorization_policy": self.authorization_policy.to_dict(),
        }


def parse_entity_registry(
    payload: Mapping[str, object],
) -> EntityRegistrySnapshot:
    data = _strict_object(payload, _REGISTRY_FIELDS, "entity registry")
    return EntityRegistrySnapshot(
        contract_version=_string(data, "contract_version"),
        registry_version=_string(data, "registry_version"),
        organizations=tuple(
            _parse_organization(item)
            for item in _object_list(data, "organizations")
        ),
        tenants=tuple(
            _parse_tenant(item) for item in _object_list(data, "tenants")
        ),
        environments=tuple(
            _parse_environment(item)
            for item in _object_list(data, "environments")
        ),
        authorization_policy=_parse_authorization_policy(
            _object(data, "authorization_policy")
        ),
    )


def loads_entity_registry(source: str | bytes) -> EntityRegistrySnapshot:
    try:
        if type(source) is str:
            source = source.encode("utf-8")
        elif type(source) is not bytes:
            raise TypeError
        decoded = decode_bounded_utf8(
            source,
            max_bytes=ENTITY_REGISTRY_JSON_MAX_BYTES,
            description="entity registry",
        )
        payload = parse_bounded_json(
            decoded,
            description="entity registry",
            max_depth=ENTITY_REGISTRY_JSON_MAX_DEPTH,
            max_nodes=ENTITY_REGISTRY_JSON_MAX_NODES,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise EntityRegistryContractError(
            "TBM_ENTITY_REGISTRY_INVALID",
            "entity registry must be bounded duplicate-free JSON",
        ) from error
    if type(payload) is not dict:
        _invalid("entity registry must be a JSON object")
    return parse_entity_registry(cast(dict[str, object], payload))


def dumps_entity_registry(registry: EntityRegistrySnapshot) -> str:
    if type(registry) is not EntityRegistrySnapshot:
        _invalid("registry must be exactly EntityRegistrySnapshot")
    try:
        encoded = json.dumps(
            registry.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise EntityRegistryContractError(
            "TBM_ENTITY_REGISTRY_INVALID",
            "entity registry cannot be encoded as canonical JSON",
        ) from error
    if len(encoded.encode("utf-8")) > ENTITY_REGISTRY_JSON_MAX_BYTES:
        _invalid("entity registry exceeds the maximum encoded size")
    return encoded


def _parse_authorization_policy(
    payload: Mapping[str, object],
) -> AuthorizationPolicyBundle:
    try:
        return parse_authorization_policy(payload)
    except AuthorizationContractError as error:
        raise EntityRegistryContractError(
            "TBM_ENTITY_REGISTRY_INVALID",
            "authorization_policy is not a valid authorization policy",
        ) from error


def _parse_organization(payload: Mapping[str, object]) -> OrganizationIdentity:
    data = _strict_object(payload, _ORGANIZATION_FIELDS, "organization")
    return OrganizationIdentity(
        organization_id=_string(data, "organization_id"),
        display_name=_string(data, "display_name"),
        status=cast(EntityStatus, _string(data, "status")),
    )


def _parse_tenant(payload: Mapping[str, object]) -> TenantEntity:
    data = _strict_object(payload, _TENANT_FIELDS, "tenant")
    return TenantEntity(
        tenant_id=_string(data, "tenant_id"),
        organization_id=_string(data, "organization_id"),
        display_name=_string(data, "display_name"),
        status=cast(EntityStatus, _string(data, "status")),
    )


def _parse_environment(payload: Mapping[str, object]) -> EnvironmentIdentity:
    data = _strict_object(payload, _ENVIRONMENT_FIELDS, "environment")
    attributes = _object(data, "attributes")
    if any(type(key) is not str or type(value) is not str for key, value in attributes.items()):
        _invalid("attributes must map strings to strings")
    return EnvironmentIdentity(
        environment_id=_string(data, "environment_id"),
        tenant_id=_string(data, "tenant_id"),
        repository_id=_optional_string(data, "repository_id"),
        environment_kind=cast(
            EnvironmentKind,
            _string(data, "environment_kind"),
        ),
        display_name=_string(data, "display_name"),
        status=cast(EntityStatus, _string(data, "status")),
        attributes=tuple(cast(dict[str, str], attributes).items()),
    )


def _strict_object(
    payload: Mapping[str, object],
    fields: frozenset[str],
    name: str,
) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != fields:
        _invalid(f"{name} fields must match the contract exactly")
    return cast(dict[str, object], payload)


def _object(
    data: dict[str, object],
    field_name: str,
) -> dict[str, object]:
    value = data[field_name]
    if type(value) is not dict:
        _invalid(f"{field_name} must be an object")
    return cast(dict[str, object], value)


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


def _records(
    values: object,
    expected_type: type,
    field_name: str,
    identifier_name: str,
) -> dict[str, object]:
    if (
        type(values) is not tuple
        or len(values) > AUTHORIZATION_MAX_REGISTRY_ITEMS
        or any(type(item) is not expected_type for item in values)
    ):
        _invalid(f"{field_name} must be a bounded tuple of {expected_type.__name__}")
    result: dict[str, object] = {}
    for item in cast(tuple[object, ...], values):
        identifier = cast(str, getattr(item, identifier_name))
        if identifier in result:
            _invalid(f"{identifier_name} values must be unique")
        result[identifier] = item
    return result


def _identifier(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_ID_MAX_CHARS
    ):
        _invalid(f"{field_name} must be a non-empty bounded identifier")
    _utf8(cast(str, value), field_name)


def _metadata(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > METADATA_VALUE_MAX_CHARS
    ):
        _invalid(f"{field_name} must be non-empty bounded metadata")
    _utf8(cast(str, value), field_name)


def _status(value: object, entity_name: str) -> None:
    if type(value) is not str or value not in _ENTITY_STATUSES:
        _invalid(f"{entity_name} status must be active or disabled")


def _attributes(values: object) -> None:
    if (
        type(values) is not tuple
        or len(values) > AUTHORIZATION_MAX_REGISTRY_ITEMS
    ):
        _invalid("attributes must be a bounded tuple")
    keys: set[str] = set()
    for item in cast(tuple[object, ...], values):
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            _invalid("attributes must contain string pairs")
        key, value = cast(tuple[str, str], item)
        _metadata(key, "attribute key")
        _metadata(value, "attribute value")
        if key in keys:
            _invalid("attribute keys must be unique")
        keys.add(key)


def _utf8(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise EntityRegistryContractError(
            "TBM_ENTITY_REGISTRY_INVALID",
            f"{field_name} must be valid UTF-8",
        ) from error


def _invalid(message: str) -> None:
    if len(message) > MEMORY_DECISION_REASON_MAX_CHARS:
        message = message[:MEMORY_DECISION_REASON_MAX_CHARS]
    raise EntityRegistryContractError("TBM_ENTITY_REGISTRY_INVALID", message)
