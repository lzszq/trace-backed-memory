from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.authorization_v3 import (
    AUTHORIZATION_DECISION_VERSION,
    AUTHORIZATION_POLICY_VERSION,
    AgentClientIdentity,
    AuthorizationContractError,
    AuthorizationDecision,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    PrincipalIdentity,
    RepositoryAlias,
    RepositoryTenantBinding,
    RoleBinding,
    authorize,
    dumps_authorization_decision,
    dumps_authorization_policy,
    loads_authorization_decision,
    loads_authorization_policy,
    verify_authorization_decision,
)
from trace_backed_memory.contracts_v3 import (
    AuthorizationScope,
    CanonicalRepository,
)


NOW = "2026-07-27T00:00:00Z"
LATER = "2026-07-27T00:01:00Z"
DIGEST_A = "sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def _principal(
    *,
    principal_id: str = "principal_tenant_001",
    tenant_id: str | None = "tenant_001",
    status: str = "active",
) -> PrincipalIdentity:
    return PrincipalIdentity(
        principal_id=principal_id,
        issuer="https://identity.example.test",
        subject_hash=DIGEST_A,
        tenant_id=tenant_id,
        status=status,  # type: ignore[arg-type]
    )


def _client(
    *,
    agent_client_id: str = "agent_client_001",
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
        legacy_aliases=("legacy/repository",),
    )


def _binding(
    *,
    binding_id: str = "binding_001",
    principal_id: str = "principal_tenant_001",
    agent_client_id: str = "agent_client_001",
    scope: AuthorizationScope | None = None,
    permissions: tuple[str, ...] = ("memory:retrieve",),
    status: str = "active",
    valid_from: str = NOW,
    expires_at: str | None = None,
) -> RoleBinding:
    return RoleBinding(
        binding_id=binding_id,
        principal_id=principal_id,
        agent_client_id=agent_client_id,
        role_name="repository_reader",
        scope=scope
        or AuthorizationScope(
            kind="repository",
            tenant_id="tenant_001",
            repository_id="repository_001",
            attributes=(("branch", "main"),),
        ),
        permissions=permissions,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        valid_from=valid_from,
        expires_at=expires_at,
    )


def _policy(
    *,
    principals: tuple[PrincipalIdentity, ...] | None = None,
    clients: tuple[AgentClientIdentity, ...] | None = None,
    aliases: tuple[RepositoryAlias, ...] | None = None,
    bindings: tuple[RoleBinding, ...] | None = None,
) -> AuthorizationPolicyBundle:
    return AuthorizationPolicyBundle(
        policy_version="policy_001",
        principals=principals if principals is not None else (_principal(),),
        agent_clients=clients if clients is not None else (_client(),),
        repositories=(_repository(),),
        repository_tenants=(
            RepositoryTenantBinding(
                repository_id="repository_001",
                tenant_id="tenant_001",
            ),
        ),
        repository_aliases=aliases
        if aliases is not None
        else (
            RepositoryAlias(
                alias="owner/repository",
                repository_id="repository_001",
                tenant_id="tenant_001",
                source="operator_registry",
            ),
        ),
        role_bindings=bindings if bindings is not None else (_binding(),),
    )


def _request(
    *,
    request_id: str = "request_001",
    principal_id: str = "principal_tenant_001",
    agent_client_id: str = "agent_client_001",
    tenant_id: str | None = "tenant_001",
    repository_reference: str | None = "repository_001",
    permission: str = "memory:retrieve",
) -> AuthorizationRequest:
    return AuthorizationRequest(
        request_id=request_id,
        principal_id=principal_id,
        agent_client_id=agent_client_id,
        tenant_id=tenant_id,
        repository_reference=repository_reference,
        permission=permission,  # type: ignore[arg-type]
        requested_at=NOW,
    )


@pytest.mark.parametrize(
    ("reference", "expected_repository"),
    (
        ("repository_001", "repository_001"),
        ("owner/repository", "repository_001"),
    ),
)
def test_authorize_resolves_only_exact_registered_references(
    reference: str,
    expected_repository: str,
):
    request = _request(repository_reference=reference)

    decision = authorize(_policy(), request, decided_at=LATER)

    assert decision.allowed is True
    assert decision.repository_id == expected_repository
    assert decision.matched_binding_ids == ("binding_001",)
    assert decision.authorization_event_id.startswith("authz_sha256_")


@pytest.mark.parametrize(
    "reference",
    ("Owner/Repository", "owner/repository/", "legacy/repository"),
)
def test_authorize_does_not_fuzz_or_trust_migration_legacy_aliases(
    reference: str,
):
    decision = authorize(
        _policy(),
        _request(repository_reference=reference),
        decided_at=LATER,
    )

    assert decision.allowed is False
    assert decision.reason == "unknown_repository"


@pytest.mark.parametrize(
    ("identity_case", "reason"),
    (
        ("unknown_principal", "unknown_principal"),
        ("disabled_principal", "principal_disabled"),
        ("unknown_client", "unknown_agent_client"),
        ("disabled_client", "agent_client_disabled"),
    ),
)
def test_authorize_rejects_unknown_or_disabled_identity(
    identity_case: str,
    reason: str,
):
    policy = _policy()
    request = _request()
    if identity_case == "unknown_principal":
        request = replace(request, principal_id="principal_missing")
    elif identity_case == "disabled_principal":
        policy = _policy(principals=(_principal(status="disabled"),))
    elif identity_case == "unknown_client":
        request = replace(request, agent_client_id="client_missing")
    else:
        policy = _policy(clients=(_client(status="disabled"),))

    decision = authorize(policy, request, decided_at=LATER)

    assert decision.allowed is False
    assert decision.reason == reason


def test_authorize_rejects_cross_tenant_identity_before_binding_match():
    policy = _policy(
        principals=(_principal(tenant_id="tenant_other"),),
        clients=(_client(tenant_id="tenant_other"),),
    )

    decision = authorize(policy, _request(), decided_at=LATER)

    assert decision.allowed is False
    assert decision.reason == "principal_tenant_mismatch"


def test_authorize_denies_when_no_binding_matches():
    decision = authorize(
        _policy(bindings=()),
        _request(),
        decided_at=LATER,
    )

    assert decision.allowed is False
    assert decision.reason == "no_matching_binding"


@pytest.mark.parametrize(
    "binding",
    (
        _binding(status="revoked"),
        _binding(valid_from="2026-07-27T00:02:00Z"),
        _binding(expires_at=LATER),
    ),
)
def test_authorize_denies_inactive_binding_windows(binding: RoleBinding):
    decision = authorize(
        _policy(bindings=(binding,)),
        _request(),
        decided_at=LATER,
    )

    assert decision.allowed is False
    assert decision.reason == "no_matching_binding"


def test_scope_attributes_are_applicability_metadata_not_authorization_inputs():
    binding = _binding(
        scope=AuthorizationScope(
            kind="repository",
            tenant_id="tenant_001",
            repository_id="repository_001",
            attributes=(("branch", "release"), ("task_type", "refactor")),
        )
    )

    decision = authorize(
        _policy(bindings=(binding,)),
        _request(),
        decided_at=LATER,
    )

    assert decision.allowed is True


def test_global_admin_is_an_explicit_superuser_binding():
    principal = _principal(
        principal_id="principal_global_admin",
        tenant_id=None,
    )
    client = _client(agent_client_id="client_global_admin", tenant_id=None)
    binding = _binding(
        principal_id=principal.principal_id,
        agent_client_id=client.agent_client_id,
        scope=AuthorizationScope(kind="global"),
        permissions=("platform:admin",),
    )
    request = _request(
        principal_id=principal.principal_id,
        agent_client_id=client.agent_client_id,
    )

    decision = authorize(
        _policy(
            principals=(principal,),
            clients=(client,),
            bindings=(binding,),
        ),
        request,
        decided_at=LATER,
    )

    assert decision.allowed is True


def test_global_permission_forbids_tenant_identity_and_target():
    with pytest.raises(
        AuthorizationContractError,
        match="global permission forbids",
    ):
        _request(
            repository_reference=None,
            permission="policy:approve_global",
        )


def test_policy_rejects_duplicate_and_dangling_registry_entries():
    with pytest.raises(
        AuthorizationContractError,
        match="binding_id values must be unique",
    ):
        _policy(
            bindings=(
                _binding(),
                _binding(permissions=("memory:inject",)),
            )
        )
    with pytest.raises(
        AuthorizationContractError,
        match="unknown principal",
    ):
        _policy(bindings=(_binding(principal_id="missing"),))


def test_policy_rejects_ambiguous_or_cross_tenant_alias():
    alias = RepositoryAlias(
        alias="repository_001",
        repository_id="repository_001",
        tenant_id="tenant_001",
        source="operator_registry",
    )
    with pytest.raises(
        AuthorizationContractError,
        match="aliases must be unambiguous",
    ):
        _policy(aliases=(alias,))

    cross_tenant = replace(alias, alias="other", tenant_id="tenant_other")
    with pytest.raises(
        AuthorizationContractError,
        match="cross-tenant",
    ):
        _policy(aliases=(cross_tenant,))


def test_policy_applies_authorization_bound_to_migration_legacy_aliases():
    repository = replace(
        _repository(),
        legacy_aliases=tuple(
            f"legacy_{index}"
            for index in range(tbm.AUTHORIZATION_MAX_REGISTRY_ITEMS + 1)
        ),
    )
    policy = _policy()

    with pytest.raises(
        AuthorizationContractError,
        match="legacy_aliases exceed",
    ):
        replace(policy, repositories=(repository,))


def test_decision_verification_binds_exact_policy_request_and_decision():
    policy = _policy()
    request = _request()
    decision = authorize(policy, request, decided_at=LATER)

    verify_authorization_decision(policy, request, decision)
    with pytest.raises(
        AuthorizationContractError,
        match="does not match policy and request",
    ):
        verify_authorization_decision(
            policy,
            replace(request, request_id="request_other"),
            decision,
        )
    with pytest.raises(
        AuthorizationContractError,
        match="does not match policy and request",
    ):
        verify_authorization_decision(
            replace(policy, policy_version="policy_other"),
            request,
            decision,
        )


def test_decision_is_content_derived_but_not_a_signature():
    decision = authorize(_policy(), _request(), decided_at=LATER)

    with pytest.raises(
        AuthorizationContractError,
        match="does not match decision content",
    ):
        replace(decision, policy_sha256="sha256:" + "b" * 64)


def test_policy_and_decision_strict_json_round_trip():
    policy = _policy()
    decision = authorize(policy, _request(), decided_at=LATER)

    assert loads_authorization_policy(
        dumps_authorization_policy(policy)
    ) == policy
    assert loads_authorization_decision(
        dumps_authorization_decision(decision)
    ) == decision
    assert policy.contract_version == AUTHORIZATION_POLICY_VERSION
    assert decision.contract_version == AUTHORIZATION_DECISION_VERSION


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-07-27T00:00:00.1234567Z",
        "2026-07-27T00:00:00+15:00",
    ),
)
def test_authorization_timestamps_match_schema_boundaries(timestamp: str):
    with pytest.raises(
        AuthorizationContractError,
        match="RFC 3339",
    ):
        replace(_request(), requested_at=timestamp)


@pytest.mark.parametrize(
    "source",
    (
        b"\xff",
        '{"contract_version":NaN}',
        '{"contract_version":"a","contract_version":"b"}',
        "[]",
    ),
)
def test_policy_json_rejects_invalid_utf8_nonfinite_duplicates_and_nonobject(
    source: str | bytes,
):
    with pytest.raises(AuthorizationContractError):
        loads_authorization_policy(source)


def test_policy_json_rejects_unknown_and_missing_fields():
    payload = _policy().to_dict()
    payload["unexpected"] = True
    with pytest.raises(AuthorizationContractError, match="unknown field"):
        loads_authorization_policy(json.dumps(payload))

    del payload["unexpected"]
    del payload["role_bindings"]
    with pytest.raises(AuthorizationContractError, match="missing field"):
        loads_authorization_policy(json.dumps(payload))


def test_policy_json_rejects_oversized_string_and_bytes_before_parsing():
    oversized = " " * (tbm.AUTHORIZATION_JSON_MAX_BYTES + 1) + "{}"
    with pytest.raises(
        AuthorizationContractError,
        match="bounded strict JSON",
    ):
        loads_authorization_policy(oversized)
    with pytest.raises(
        AuthorizationContractError,
        match="bounded strict JSON",
    ):
        loads_authorization_policy(oversized.encode())


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    (
        (_principal, "status", []),
        (_client, "status", []),
        (_request, "permission", []),
    ),
)
def test_direct_api_rejects_unhashable_enum_values(
    factory,
    field: str,
    value: object,
):
    with pytest.raises(AuthorizationContractError):
        factory(**{field: value})


def test_decision_requires_sorted_binding_ids():
    decision = authorize(_policy(), _request(), decided_at=LATER)

    with pytest.raises(
        AuthorizationContractError,
        match="must be sorted",
    ):
        AuthorizationDecision(
            **{
                **decision.to_dict(),
                "matched_binding_ids": ("binding_z", "binding_a"),
            }
        )


def test_authorization_schemas_examples_and_public_exports():
    policy_schema = json.loads(
        (
            ROOT / "schemas" / "authorization_policy_v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    decision_schema = json.loads(
        (
            ROOT / "schemas" / "authorization_decision_v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    policy_example = json.loads(
        (
            ROOT / "examples" / "authorization_policy_v3.example.json"
        ).read_text(encoding="utf-8")
    )
    decision_example = json.loads(
        (
            ROOT / "examples" / "authorization_decision_v3.example.json"
        ).read_text(encoding="utf-8")
    )

    assert policy_schema["additionalProperties"] is False
    assert decision_schema["additionalProperties"] is False
    assert set(policy_schema["required"]) == set(policy_example)
    assert set(decision_schema["required"]) == set(decision_example)
    assert (
        policy_schema["properties"]["contract_version"]["const"]
        == AUTHORIZATION_POLICY_VERSION
    )
    assert (
        decision_schema["properties"]["contract_version"]["const"]
        == AUTHORIZATION_DECISION_VERSION
    )
    assert (
        tuple(
            sorted(
                policy_schema["$defs"]["permission"]["enum"]
            )
        )
        == tbm.AUTHORIZATION_PERMISSIONS
    )
    for schema in (policy_schema, decision_schema):
        timestamp_pattern = schema["$defs"]["timestamp"]["pattern"]
        assert (
            re.fullmatch(
                timestamp_pattern.removeprefix("^").removesuffix("$"),
                "2026-07-27T00:00:00.1234567Z",
            )
            is None
        )
        assert (
            re.fullmatch(
                timestamp_pattern.removeprefix("^").removesuffix("$"),
                "2026-07-27T00:00:00+15:00",
            )
            is None
        )
    policy = loads_authorization_policy(json.dumps(policy_example))
    decision = loads_authorization_decision(json.dumps(decision_example))
    assert decision.policy_sha256 == policy.policy_sha256
    verify_authorization_decision(
        policy,
        _request(repository_reference="owner/repository"),
        decision,
    )
    for name in (
        "AUTHORIZATION_DECISION_VERSION",
        "AUTHORIZATION_PERMISSIONS",
        "AUTHORIZATION_POLICY_VERSION",
        "AgentClientIdentity",
        "AuthorizationContractError",
        "AuthorizationDecision",
        "AuthorizationPolicyBundle",
        "AuthorizationRequest",
        "PrincipalIdentity",
        "RepositoryAlias",
        "RepositoryTenantBinding",
        "RoleBinding",
        "authorize",
        "dumps_authorization_decision",
        "dumps_authorization_policy",
        "loads_authorization_decision",
        "loads_authorization_policy",
        "parse_authorization_decision",
        "parse_authorization_policy",
        "verify_authorization_decision",
    ):
        assert name in tbm.__all__
