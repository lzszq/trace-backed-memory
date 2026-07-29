from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import trace_backed_memory as tbm
from tests.test_memory_revision_v3 import (
    _evidence,
    _fix_evidence,
    _revision,
)
from trace_backed_memory.authorization_v3 import (
    AgentClientIdentity,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    PrincipalIdentity,
    RepositoryTenantBinding,
    RoleBinding,
    authorize,
)
from trace_backed_memory.contracts_v3 import (
    AuthorizationScope,
    CanonicalRepository,
)
from trace_backed_memory.memory_publication_v3 import (
    MEMORY_PUBLICATION_JSON_MAX_BYTES,
    MemoryPublicationContractError,
    MemoryRevisionActivation,
    MemoryRevisionApproval,
    activate_memory_revision,
    approve_memory_revision,
    dumps_memory_revision_activation,
    dumps_memory_revision_approval,
    loads_memory_revision_activation,
    loads_memory_revision_approval,
    verify_memory_revision_activation,
    verify_memory_revision_approval,
)
from trace_backed_memory.memory_revision_v3 import build_memory_revision
from trace_backed_memory.replay_v3 import create_content_addressed_artifact


ROOT = Path(__file__).resolve().parents[1]
APPROVED_AT = "2026-07-27T00:07:00Z"
ACTIVATED_AT = "2026-07-27T00:08:00Z"
DIGEST = "sha256:" + "f" * 64
CONTENT = b'{"memory_text":"Prefer the verified workflow."}'


def _principal(principal_id: str) -> PrincipalIdentity:
    return PrincipalIdentity(
        principal_id=principal_id,
        issuer="https://identity.example.test",
        subject_hash=DIGEST,
        tenant_id="tenant_001",
    )


def _policy() -> AuthorizationPolicyBundle:
    repository = CanonicalRepository(
        repository_id="repository_001",
        provider="local",
        provider_repository_id="provider_repository_001",
        canonical_locator_hash=DIGEST,
        display_name="Repository",
    )
    principals = (
        _principal("publication_approver"),
        _principal("publication_activator"),
    )
    client = AgentClientIdentity(
        agent_client_id="publication_service",
        tenant_id="tenant_001",
        client_kind="service",
    )
    return AuthorizationPolicyBundle(
        policy_version="publication_policy_001",
        principals=principals,
        agent_clients=(client,),
        repositories=(repository,),
        repository_tenants=(
            RepositoryTenantBinding(
                repository_id=repository.repository_id,
                tenant_id="tenant_001",
            ),
        ),
        repository_aliases=(),
        role_bindings=(
            RoleBinding(
                binding_id="binding_approver",
                principal_id=principals[0].principal_id,
                agent_client_id=client.agent_client_id,
                role_name="memory_approver",
                scope=AuthorizationScope(
                    kind="repository",
                    tenant_id="tenant_001",
                    repository_id="repository_001",
                ),
                permissions=("memory:review",),
                status="active",
                valid_from="2026-07-27T00:00:00Z",
            ),
            RoleBinding(
                binding_id="binding_activator",
                principal_id=principals[1].principal_id,
                agent_client_id=client.agent_client_id,
                role_name="memory_activator",
                scope=AuthorizationScope(
                    kind="repository",
                    tenant_id="tenant_001",
                    repository_id="repository_001",
                ),
                permissions=("memory:activate",),
                status="active",
                valid_from="2026-07-27T00:00:00Z",
            ),
        ),
    )


def _authorization(
    policy: AuthorizationPolicyBundle,
    *,
    actor_id: str,
    permission: str,
    decided_at: str,
):
    request = AuthorizationRequest(
        request_id=f"request_{actor_id}_{permission.replace(':', '_')}",
        principal_id=actor_id,
        agent_client_id="publication_service",
        tenant_id="tenant_001",
        repository_reference="repository_001",
        permission=permission,  # type: ignore[arg-type]
        requested_at=decided_at,
    )
    return request, authorize(policy, request, decided_at=decided_at)


def _publication_inputs():
    evidence = _evidence()
    fix = _fix_evidence()
    revision = _revision(evidence=evidence, fix_evidence=fix)
    return revision, {fix.evidence_id: fix}, {evidence.evidence_id: evidence}


def _approval(
    *,
    revision=None,
    policy: AuthorizationPolicyBundle | None = None,
):
    base_revision, fixes, regressions = _publication_inputs()
    revision = revision or base_revision
    policy = policy or _policy()
    request, decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    approval = approve_memory_revision(
        revision=revision,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        policy=policy,
        request=request,
        decision=decision,
        approved_by="publication_approver",
        approved_via_client_id="publication_service",
        approved_at=APPROVED_AT,
        approval_attestation_sha256=DIGEST,
    )
    return approval, revision, fixes, regressions, policy, request, decision


def test_approval_and_activation_are_exact_independent_events():
    (
        approval,
        revision,
        fixes,
        regressions,
        policy,
        approval_request,
        approval_decision,
    ) = _approval()
    activation_request, activation_decision = _authorization(
        policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )

    activation = activate_memory_revision(
        revision=revision,
        approval=approval,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=policy,
        approval_request=approval_request,
        approval_decision=approval_decision,
        previous_activation=None,
        policy=policy,
        request=activation_request,
        decision=activation_decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at=ACTIVATED_AT,
        activation_attestation_sha256=DIGEST,
    )

    assert approval.revision_id == revision.revision_id
    assert approval.evidence_bundle_sha256.startswith("sha256:")
    assert activation.approval_id == approval.approval_id
    assert activation.activation_sequence == 1
    assert activation.previous_activation_id is None
    assert loads_memory_revision_approval(
        dumps_memory_revision_approval(approval)
    ) == approval
    assert loads_memory_revision_activation(
        dumps_memory_revision_activation(activation)
    ) == activation
    verify_memory_revision_approval(
        approval,
        revision=revision,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        policy=policy,
        request=approval_request,
        decision=approval_decision,
    )
    verify_memory_revision_activation(
        activation,
        revision=revision,
        approval=approval,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=policy,
        approval_request=approval_request,
        approval_decision=approval_decision,
        previous_activation=None,
        policy=policy,
        request=activation_request,
        decision=activation_decision,
    )


def test_approval_rejects_wrong_bytes_denial_and_actor_overlap():
    revision, fixes, regressions = _publication_inputs()
    policy = _policy()
    request, decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    kwargs = {
        "revision": revision,
        "previous_revision": None,
        "content": CONTENT,
        "fix_evidence_by_id": fixes,
        "regression_evidence_by_id": regressions,
        "policy": policy,
        "request": request,
        "decision": decision,
        "approved_by": "publication_approver",
        "approved_via_client_id": "publication_service",
        "approved_at": APPROVED_AT,
        "approval_attestation_sha256": DIGEST,
    }

    with pytest.raises(
        MemoryPublicationContractError,
        match="content bytes",
    ):
        approve_memory_revision(**{**kwargs, "content": b"changed"})

    denied_policy = replace(policy, role_bindings=())
    denied = authorize(
        denied_policy,
        request,
        decided_at=APPROVED_AT,
    )
    with pytest.raises(
        MemoryPublicationContractError,
        match="was denied",
    ):
        approve_memory_revision(
            **{
                **kwargs,
                "policy": denied_policy,
                "decision": denied,
            }
        )

    overlapping = build_memory_revision(
        **{
            **{
                key: value
                for key, value in revision.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "proposed_by": "publication_approver",
        }
    )
    with pytest.raises(
        MemoryPublicationContractError,
        match="must not approve",
    ):
        approve_memory_revision(**{**kwargs, "revision": overlapping})

    with pytest.raises(
        MemoryPublicationContractError,
        match="fix_evidence_by_id must be a mapping",
    ):
        approve_memory_revision(
            **{
                **kwargs,
                "fix_evidence_by_id": None,
            }
        )


def test_approval_rejects_global_scope_and_non_current_authorization():
    revision, fixes, regressions = _publication_inputs()
    global_revision = build_memory_revision(
        **{
            **{
                key: value
                for key, value in revision.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "scope": AuthorizationScope(kind="global"),
        }
    )
    policy = _policy()
    request, decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )

    with pytest.raises(
        MemoryPublicationContractError,
        match="forbids global scope",
    ):
        approve_memory_revision(
            revision=global_revision,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            policy=policy,
            request=request,
            decision=decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256=DIGEST,
        )

    with pytest.raises(
        MemoryPublicationContractError,
        match="publication event",
    ):
        approve_memory_revision(
            revision=revision,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            policy=policy,
            request=request,
            decision=decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at="2026-07-27T00:07:01Z",
            approval_attestation_sha256=DIGEST,
        )


def test_activation_rejects_same_actor_and_mismatched_approval():
    (
        approval,
        revision,
        fixes,
        regressions,
        policy,
        approval_request,
        approval_decision,
    ) = (
        _approval()
    )
    request, decision = _authorization(
        policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )
    kwargs = {
        "revision": revision,
        "approval": approval,
        "previous_revision": None,
        "content": CONTENT,
        "fix_evidence_by_id": fixes,
        "regression_evidence_by_id": regressions,
        "approval_policy": policy,
        "approval_request": approval_request,
        "approval_decision": approval_decision,
        "previous_activation": None,
        "policy": policy,
        "request": request,
        "decision": decision,
        "activated_by": "publication_activator",
        "activated_via_client_id": "publication_service",
        "activated_at": ACTIVATED_AT,
        "activation_attestation_sha256": DIGEST,
    }

    with pytest.raises(
        MemoryPublicationContractError,
        match="independent",
    ):
        activate_memory_revision(
            **{
                **kwargs,
                "activated_by": approval.approved_by,
            }
        )

    alternate_revision = build_memory_revision(
        **{
            **{
                key: value
                for key, value in revision.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "memory_id": "memory_other",
        }
    )
    alternate_approval, *_rest = _approval(
        revision=alternate_revision,
        policy=policy,
    )
    with pytest.raises(
        MemoryPublicationContractError,
        match="approval does not match",
    ):
        activate_memory_revision(
            **{
                **kwargs,
                "approval": alternate_approval,
            }
        )


def test_second_activation_requires_exact_predecessor_linkage():
    (
        first_approval,
        first,
        fixes,
        regressions,
        policy,
        first_approval_request,
        first_approval_decision,
    ) = _approval()
    activation_request, activation_decision = _authorization(
        policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )
    first_activation = activate_memory_revision(
        revision=first,
        approval=first_approval,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=policy,
        approval_request=first_approval_request,
        approval_decision=first_approval_decision,
        previous_activation=None,
        policy=policy,
        request=activation_request,
        decision=activation_decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at=ACTIVATED_AT,
        activation_attestation_sha256=DIGEST,
    )
    second_content = b'{"memory_text":"Use the verified workflow twice."}'
    second_artifact = create_content_addressed_artifact(
        second_content,
        media_type=first.content_artifact.media_type,
        classification="internal",
        created_at="2026-07-27T00:09:00Z",
    )
    second = build_memory_revision(
        memory_id=first.memory_id,
        memory_kind=first.memory_kind,
        revision_number=2,
        previous_revision_id=first.revision_id,
        memory_type=first.memory_type,
        content_artifact=second_artifact,
        scope=first.scope,
        confidence=first.confidence,
        sensitive=first.sensitive,
        eval_leaking=first.eval_leaking,
        source_case_id=first.source_case_id,
        source_case_revision_id=first.source_case_revision_id,
        fix_evidence_id=first.fix_evidence_id,
        regression_evidence_ids=first.regression_evidence_ids,
        proposed_by=first.proposed_by,
        proposed_via_client_id=first.proposed_via_client_id,
        proposed_at="2026-07-27T00:10:00Z",
        proposal_attestation_sha256=first.proposal_attestation_sha256,
    )
    relocated = build_memory_revision(
        **{
            **{
                key: value
                for key, value in second.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "scope": AuthorizationScope(
                kind="repository",
                tenant_id="tenant_001",
                repository_id="repository_other",
            ),
        }
    )
    second_approval_request, second_approval_decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at="2026-07-27T00:11:00Z",
    )
    with pytest.raises(
        MemoryPublicationContractError,
        match="linear lineage",
    ):
        approve_memory_revision(
            revision=relocated,
            previous_revision=first,
            content=second_content,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            policy=policy,
            request=second_approval_request,
            decision=second_approval_decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at="2026-07-27T00:11:00Z",
            approval_attestation_sha256=DIGEST,
        )
    second_approval = approve_memory_revision(
        revision=second,
        previous_revision=first,
        content=second_content,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        policy=policy,
        request=second_approval_request,
        decision=second_approval_decision,
        approved_by="publication_approver",
        approved_via_client_id="publication_service",
        approved_at="2026-07-27T00:11:00Z",
        approval_attestation_sha256=DIGEST,
    )
    second_activation_request, second_activation_decision = _authorization(
        policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at="2026-07-27T00:12:00Z",
    )

    second_activation = activate_memory_revision(
        revision=second,
        approval=second_approval,
        previous_revision=first,
        content=second_content,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=policy,
        approval_request=second_approval_request,
        approval_decision=second_approval_decision,
        previous_activation=first_activation,
        policy=policy,
        request=second_activation_request,
        decision=second_activation_decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at="2026-07-27T00:12:00Z",
        activation_attestation_sha256=DIGEST,
    )

    assert second_activation.previous_activation_id == (
        first_activation.activation_id
    )
    assert second_activation.activation_sequence == 2
    with pytest.raises(
        MemoryPublicationContractError,
        match="previous_activation",
    ):
        activate_memory_revision(
            revision=second,
            approval=second_approval,
            previous_revision=first,
            content=second_content,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            approval_policy=policy,
            approval_request=second_approval_request,
            approval_decision=second_approval_decision,
            previous_activation=None,
            policy=policy,
            request=second_activation_request,
            decision=second_activation_decision,
            activated_by="publication_activator",
            activated_via_client_id="publication_service",
            activated_at="2026-07-27T00:12:00Z",
            activation_attestation_sha256=DIGEST,
        )


def test_publication_json_is_strict_bounded_and_content_derived():
    (
        approval,
        revision,
        fixes,
        regressions,
        activation_policy,
        approval_request,
        approval_decision,
    ) = _approval()
    request, decision = _authorization(
        activation_policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )
    activation = activate_memory_revision(
        revision=revision,
        approval=approval,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=activation_policy,
        approval_request=approval_request,
        approval_decision=approval_decision,
        previous_activation=None,
        policy=activation_policy,
        request=request,
        decision=decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at=ACTIVATED_AT,
        activation_attestation_sha256=DIGEST,
    )

    with pytest.raises(
        MemoryPublicationContractError,
        match="approval_id",
    ):
        replace(approval, evidence_bundle_sha256="sha256:" + "a" * 64)
    with pytest.raises(
        MemoryPublicationContractError,
        match="activation_id",
    ):
        replace(activation, authorization_policy_sha256=DIGEST)
    for loader in (
        loads_memory_revision_approval,
        loads_memory_revision_activation,
    ):
        with pytest.raises(MemoryPublicationContractError):
            loader('{"contract_version":"a","contract_version":"b"}')
        with pytest.raises(MemoryPublicationContractError):
            loader(b"\xff")
        with pytest.raises(MemoryPublicationContractError):
            loader(" " * (MEMORY_PUBLICATION_JSON_MAX_BYTES + 1))


def test_publication_schema_examples_and_exports():
    approval_example = json.loads(
        (
            ROOT / "examples" / "memory_revision_approval_v3.example.json"
        ).read_text(encoding="utf-8")
    )
    activation_example = json.loads(
        (
            ROOT / "examples" / "memory_revision_activation_v3.example.json"
        ).read_text(encoding="utf-8")
    )
    approval_schema = json.loads(
        (
            ROOT / "schemas" / "memory_revision_approval_v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    activation_schema = json.loads(
        (
            ROOT / "schemas" / "memory_revision_activation_v3.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert set(approval_schema["required"]) == set(approval_example)
    assert set(activation_schema["required"]) == set(activation_example)
    Draft202012Validator.check_schema(approval_schema)
    Draft202012Validator.check_schema(activation_schema)
    Draft202012Validator(
        approval_schema,
        format_checker=FormatChecker(),
    ).validate(approval_example)
    Draft202012Validator(
        activation_schema,
        format_checker=FormatChecker(),
    ).validate(activation_example)
    assert loads_memory_revision_approval(
        json.dumps(approval_example)
    ).to_dict() == approval_example
    assert loads_memory_revision_activation(
        json.dumps(activation_example)
    ).to_dict() == activation_example
    assert tbm.MemoryRevisionApproval is MemoryRevisionApproval
    assert tbm.MemoryRevisionActivation is MemoryRevisionActivation
    for name in (
        "MemoryRevisionApproval",
        "MemoryRevisionActivation",
        "approve_memory_revision",
        "activate_memory_revision",
    ):
        assert name in tbm.__all__
