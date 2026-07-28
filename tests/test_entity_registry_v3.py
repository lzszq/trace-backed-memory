from __future__ import annotations

import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm


ROOT = Path(__file__).resolve().parents[1]


def _policy() -> tbm.AuthorizationPolicyBundle:
    return tbm.loads_authorization_policy(
        (ROOT / "examples" / "authorization_policy_v3.example.json").read_bytes()
    )


def _registry(
    *,
    organizations: tuple[tbm.OrganizationIdentity, ...] | None = None,
    tenants: tuple[tbm.TenantEntity, ...] | None = None,
    environments: tuple[tbm.EnvironmentIdentity, ...] | None = None,
) -> tbm.EntityRegistrySnapshot:
    return tbm.EntityRegistrySnapshot(
        registry_version="registry_001",
        organizations=organizations
        if organizations is not None
        else (
            tbm.OrganizationIdentity(
                organization_id="organization_001",
                display_name="Example organization",
            ),
        ),
        tenants=tenants
        if tenants is not None
        else (
            tbm.TenantEntity(
                tenant_id="tenant_001",
                organization_id="organization_001",
                display_name="Example tenant",
            ),
        ),
        environments=environments
        if environments is not None
        else (
            tbm.EnvironmentIdentity(
                environment_id="environment_001",
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_kind="production",
                display_name="Production",
                attributes=(("region", "ap-southeast-1"),),
            ),
        ),
        authorization_policy=_policy(),
    )


def test_entity_registry_round_trip_is_canonical_and_content_addressed() -> None:
    registry = _registry()

    encoded = tbm.dumps_entity_registry(registry)
    decoded = tbm.loads_entity_registry(encoded)

    assert decoded == registry
    assert encoded == json.dumps(
        registry.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert registry.registry_sha256 == tbm.canonical_sha256(registry.to_dict())


def test_packaged_entity_registry_contract_matches_canonical_resources() -> None:
    example = ROOT / "examples" / "entity_registry_v3.example.json"
    schema = ROOT / "schemas" / "entity_registry_v3.schema.json"

    assert tbm.loads_entity_registry(example.read_bytes()) == _registry()
    assert tbm.read_packaged_resource(
        "examples/entity_registry_v3.example.json"
    ) == example.read_bytes()
    assert tbm.read_packaged_resource(
        "schemas/entity_registry_v3.schema.json"
    ) == schema.read_bytes()
    schema_payload = json.loads(schema.read_text(encoding="utf-8"))
    assert schema_payload["properties"]["authorization_policy"] == {
        "$ref": "authorization_policy_v3.schema.json"
    }


def test_entity_registry_rejects_unknown_organization() -> None:
    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="unknown organization",
    ):
        _registry(
            tenants=(
                tbm.TenantEntity(
                    tenant_id="tenant_001",
                    organization_id="organization_missing",
                    display_name="Example tenant",
                ),
            )
        )


def test_entity_registry_requires_policy_tenant_closure() -> None:
    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="policy references an unknown tenant",
    ):
        _registry(tenants=())


def test_entity_registry_rejects_policy_references_to_disabled_tenant() -> None:
    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="disabled tenant",
    ):
        _registry(
            tenants=(
                tbm.TenantEntity(
                    tenant_id="tenant_001",
                    organization_id="organization_001",
                    display_name="Example tenant",
                    status="disabled",
                ),
            )
        )


def test_entity_registry_rejects_policy_tenant_in_disabled_organization() -> None:
    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="disabled organization",
    ):
        _registry(
            organizations=(
                tbm.OrganizationIdentity(
                    organization_id="organization_001",
                    display_name="Example organization",
                    status="disabled",
                ),
            )
        )


def test_entity_registry_rejects_unknown_environment_repository() -> None:
    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="unknown repository",
    ):
        _registry(
            environments=(
                tbm.EnvironmentIdentity(
                    environment_id="environment_001",
                    tenant_id="tenant_001",
                    repository_id="repository_missing",
                    environment_kind="ci",
                    display_name="CI",
                ),
            )
        )


def test_entity_registry_rejects_cross_tenant_environment_repository() -> None:
    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="cross-tenant",
    ):
        _registry(
            tenants=(
                tbm.TenantEntity(
                    tenant_id="tenant_001",
                    organization_id="organization_001",
                    display_name="Example tenant",
                ),
                tbm.TenantEntity(
                    tenant_id="tenant_002",
                    organization_id="organization_001",
                    display_name="Other tenant",
                ),
            ),
            environments=(
                tbm.EnvironmentIdentity(
                    environment_id="environment_001",
                    tenant_id="tenant_002",
                    repository_id="repository_001",
                    environment_kind="test",
                    display_name="Test",
                ),
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["organizations"][0].update({"unexpected": True}),
        lambda value: value["environments"][0]["attributes"].update(
            {"region": 1}
        ),
    ],
)
def test_entity_registry_rejects_non_contract_shapes(mutation) -> None:
    payload = _registry().to_dict()
    mutation(payload)

    with pytest.raises(tbm.EntityRegistryContractError):
        tbm.parse_entity_registry(payload)


def test_entity_registry_rejects_duplicate_json_keys() -> None:
    source = tbm.dumps_entity_registry(_registry())
    source = source.replace(
        '"registry_version":"registry_001"',
        '"registry_version":"registry_001","registry_version":"registry_002"',
    )

    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="duplicate-free JSON",
    ):
        tbm.loads_entity_registry(source)


def test_entity_registry_rejects_duplicate_entity_ids() -> None:
    organization = tbm.OrganizationIdentity(
        organization_id="organization_001",
        display_name="Example organization",
    )
    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="organization_id values must be unique",
    ):
        _registry(organizations=(organization, organization))


def test_entity_registry_wraps_nested_authorization_errors() -> None:
    payload = _registry().to_dict()
    policy = payload["authorization_policy"]
    assert isinstance(policy, dict)
    policy["policy_version"] = 1

    with pytest.raises(tbm.EntityRegistryContractError) as captured:
        tbm.parse_entity_registry(payload)

    assert type(captured.value) is tbm.EntityRegistryContractError


def test_entity_registry_dump_enforces_its_load_byte_limit() -> None:
    large_attributes = tuple(
        (f"key_{index}", "x" * 512)
        for index in range(3_000)
    )
    registry = _registry(
        environments=(
            tbm.EnvironmentIdentity(
                environment_id="environment_001",
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_kind="production",
                display_name="Production",
                attributes=large_attributes,
            ),
        )
    )

    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="maximum encoded size",
    ):
        tbm.dumps_entity_registry(registry)


def test_entity_registry_requires_exact_record_types() -> None:
    class DerivedOrganization(tbm.OrganizationIdentity):
        pass

    with pytest.raises(
        tbm.EntityRegistryContractError,
        match="bounded tuple",
    ):
        _registry(
            organizations=(
                DerivedOrganization(
                    organization_id="organization_001",
                    display_name="Example organization",
                ),
            )
        )
