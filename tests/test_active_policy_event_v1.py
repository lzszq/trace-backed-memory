from dataclasses import replace
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from tests.test_sqlite_event_ledger_v1 import _connection as _ledger_connection
from trace_backed_memory.active_policy_event_v1 import (
    ACTIVE_POLICY_EVENT_TYPES,
    ActivePolicyEventV1Error,
    StoredActivePolicyActivation,
    StoredActivePolicyRegistration,
    active_policy_bundle_schema,
    active_policy_event_payload_dispatch_schema,
    append_active_policy_records,
    build_active_policy_activation,
    build_active_policy_bundle,
    build_active_policy_event_batch,
    build_active_policy_event_registry,
    build_active_policy_reducer,
    build_active_policy_registration,
    dumps_active_policy_bundle,
    loads_active_policy_bundle,
    rebuild_active_policy_from_ledger,
    reduce_active_policy_events,
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
    canonical_sha256,
)
from trace_backed_memory.event_v1 import EventTrustedContext, build_canonical_event
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.retrieval_policy_v3 import (
    ModeMemoryRule,
    build_retrieval_policy,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1


DIGEST = "sha256:" + "a" * 64
TRUSTED_VERIFIERS = ("attestation_verifier",)
REGISTERED_AT = "2026-07-27T00:02:00Z"
ACTIVATED_AT = "2026-07-27T00:04:00Z"
ROOT = Path(__file__).resolve().parents[1]


def _partition() -> LedgerTenantPartition:
    return LedgerTenantPartition(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
    )


def _retrieval_policy(*, version: str = "retrieval_policy_001"):
    rules = (
        ModeMemoryRule("planning", ("semantic", "policy")),
        ModeMemoryRule("repair", ("procedural", "semantic", "policy")),
        ModeMemoryRule("debug", ("procedural", "episodic", "policy")),
        ModeMemoryRule("eval", ("procedural", "semantic")),
        ModeMemoryRule("production", ("procedural", "policy")),
    )
    return build_retrieval_policy(
        policy_version=version,
        allowed_classifications=("public", "internal"),
        mode_memory_rules=rules,
        ancestry_mode="required",
        ancestry_bypass_reason=None,
        stage_weights=(
            ("metadata", 0.1),
            ("lexical", 0.2),
            ("semantic", 0.4),
            ("evidence_graph", 0.3),
        ),
        minimum_fused_score=0.25,
        payload_budget_bytes=8_192,
    )


def _bundle(*, version: str = "retrieval_policy_001"):
    return build_active_policy_bundle(
        retrieval_policy=_retrieval_policy(version=version),
        minimum_trust_tier="regression_verified",
    )


def _authorization(
    *,
    actor_id: str,
    client_id: str,
    permission: str,
    requested_at: str,
    decided_at: str,
):
    principal = PrincipalIdentity(
        principal_id=actor_id,
        issuer="https://identity.example.test",
        subject_hash=DIGEST,
        tenant_id=None,
        status="active",
    )
    client = AgentClientIdentity(
        agent_client_id=client_id,
        tenant_id=None,
        client_kind="service",
        status="active",
    )
    repository = CanonicalRepository(
        repository_id="repository_001",
        provider="local",
        provider_repository_id="provider_repository_001",
        canonical_locator_hash=DIGEST,
        display_name="Repository",
        legacy_aliases=(),
    )
    binding = RoleBinding(
        binding_id="binding_" + actor_id,
        principal_id=actor_id,
        agent_client_id=client_id,
        role_name="global_policy_operator",
        scope=AuthorizationScope(kind="global"),
        permissions=(permission,),
        status="active",
        valid_from="2026-07-27T00:00:00Z",
        expires_at=None,
    )
    policy = AuthorizationPolicyBundle(
        policy_version="authorization_policy_001",
        principals=(principal,),
        agent_clients=(client,),
        repositories=(repository,),
        repository_tenants=(
            RepositoryTenantBinding(
                repository_id="repository_001",
                tenant_id="tenant_001",
            ),
        ),
        repository_aliases=(),
        role_bindings=(binding,),
    )
    request = AuthorizationRequest(
        request_id="request_" + actor_id,
        principal_id=actor_id,
        agent_client_id=client_id,
        tenant_id=None,
        repository_reference=None,
        permission=permission,  # type: ignore[arg-type]
        requested_at=requested_at,
    )
    decision = authorize(policy, request, decided_at=decided_at)
    assert decision.allowed
    return policy, request, decision


def _access(actor_id: str, client_id: str, authorization_event_id: str):
    return LedgerAccessContext(
        partition=_partition(),
        principal_id=actor_id,
        agent_client_id=client_id,
        actor_type="principal",
        actor_id=actor_id,
        authorization_decision_id=authorization_event_id,
        classification_filter=LedgerClassificationFilter(("internal",)),
    )


def _records(
    *,
    bundle=None,
    previous_policy_bundle_id=None,
    registered_at: str = REGISTERED_AT,
    activated_at: str = ACTIVATED_AT,
):
    selected = _bundle() if bundle is None else bundle
    register_policy, register_request, register_decision = _authorization(
        actor_id="policy_registrar",
        client_id="policy_registration_service",
        permission="policy:create_global",
        requested_at="2026-07-27T00:00:30Z",
        decided_at="2026-07-27T00:01:00Z",
    )
    registration = build_active_policy_registration(
        partition=_partition(),
        policy_bundle=selected,
        registered_by="policy_registrar",
        registered_via_client_id="policy_registration_service",
        authorization_event_id=register_decision.authorization_event_id,
        registered_at=registered_at,
    )
    stored_registration = StoredActivePolicyRegistration(
        registration=registration,
        policy=register_policy,
        request=register_request,
        decision=register_decision,
        attestation_verified_by="attestation_verifier",
    )
    activate_policy, activate_request, activate_decision = _authorization(
        actor_id="policy_activator",
        client_id="policy_activation_service",
        permission="policy:approve_global",
        requested_at="2026-07-27T00:02:30Z",
        decided_at="2026-07-27T00:03:00Z",
    )
    activation = build_active_policy_activation(
        registration=registration,
        previous_policy_bundle_id=previous_policy_bundle_id,
        activated_by="policy_activator",
        activated_via_client_id="policy_activation_service",
        authorization_event_id=activate_decision.authorization_event_id,
        activated_at=activated_at,
    )
    stored_activation = StoredActivePolicyActivation(
        activation=activation,
        policy=activate_policy,
        request=activate_request,
        decision=activate_decision,
        attestation_verified_by="attestation_verifier",
    )
    return stored_registration, stored_activation


def _events(records):
    events = []
    parent = None
    for record in records:
        if isinstance(record, StoredActivePolicyRegistration):
            actor = record.registration.registered_by
            client = record.registration.registered_via_client_id
            authorization_id = record.registration.authorization_event_id
        else:
            actor = record.activation.activated_by
            client = record.activation.activated_via_client_id
            authorization_id = record.activation.authorization_event_id
        batch, _ = build_active_policy_event_batch(
            _access(actor, client, authorization_id),
            (record,),
            expected_stream_version=len(events),
            next_global_position=len(events) + 1,
            previous_event=parent,
            recorded_at="2026-07-27T00:10:00Z",
        )
        events.extend(batch)
        parent = batch[-1]
    return tuple(events)


def _rehash_event(
    event, *, actor_id=None, principal_id=None, occurred_at=None
):
    context = EventTrustedContext(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
        principal_id=(
            event.principal_id if principal_id is None else principal_id
        ),
        agent_client_id=event.agent_client_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id if actor_id is None else actor_id,
        authorization_decision_id=event.authorization_decision_id,
    )
    return build_canonical_event(
        event_id=event.event_id,
        event_type=event.event_type,
        event_version=event.event_version,
        event_kind=event.event_kind,
        origin=event.origin,
        source=event.source,
        stream_id=event.stream_id,
        stream_type=event.stream_type,
        stream_version=event.stream_version,
        global_position=event.global_position,
        trusted_context=context,
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=event.request_sha256,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        occurred_at=event.occurred_at if occurred_at is None else occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        artifact_refs=event.artifact_refs,
        payload=event.payload,
    )


def test_active_bundle_covers_all_policy_dimensions_and_round_trips():
    bundle = _bundle()
    document = dumps_active_policy_bundle(bundle)

    assert loads_active_policy_bundle(document) == bundle
    assert bundle.trust_tier_policy.minimum_trust_tier == "regression_verified"
    assert bundle.trust_tier_policy.allow_legacy_unstructured is False
    assert bundle.task_modes == (
        "planning",
        "repair",
        "debug",
        "eval",
        "production",
    )
    assert bundle.ancestry_mode == "required"
    assert bundle.allowed_classifications == ("public", "internal")
    assert bundle.block_eval_leaking is True
    assert bundle.candidate_budget.semantic_gate_max_candidates == 50
    assert bundle.renderer_policy.max_memories == 20
    assert bundle.semantic_gate_required is True
    Draft202012Validator(active_policy_bundle_schema()).validate(
        json.loads(document)
    )


def test_event_reducer_rebuilds_exact_active_policy_head():
    registration, activation = _records()
    events = _events((registration, activation))

    projection = reduce_active_policy_events(
        events,
        trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
    )

    assert projection.load_active_policy() == registration.registration.policy_bundle
    assert projection() == registration.registration.policy_bundle.retrieval_policy
    assert projection.active_head is not None
    assert projection.active_head.source_event_sha256 == events[-1].event_sha256
    assert (
        projection.active_head.registration_authorization_event_id
        == registration.registration.authorization_event_id
    )
    assert (
        projection.active_head.activation_authorization_event_id
        == activation.activation.authorization_event_id
    )
    projection.verify_head(projection.active_head)


def test_reducer_rejects_actor_time_verifier_and_semantic_bypass():
    registration, activation = _records()
    first = _events((registration,))[0]
    for forged in (
        _rehash_event(first, actor_id="attacker"),
        _rehash_event(first, principal_id="attacker"),
        _rehash_event(first, occurred_at="2026-07-27T00:02:01Z"),
    ):
        with pytest.raises(ActivePolicyEventV1Error):
            reduce_active_policy_events(
                (forged,),
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            )

    untrusted = replace(
        activation,
        attestation_verified_by="attacker_verifier",
    )
    with pytest.raises(ActivePolicyEventV1Error):
        reduce_active_policy_events(
            _events((registration, untrusted)),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )

    with pytest.raises(ActivePolicyEventV1Error):
        replace(_bundle(), semantic_gate_required=False)


def test_policy_limits_fail_closed_on_budget_renderer_and_trust_drift():
    bundle = _bundle()
    with pytest.raises(ActivePolicyEventV1Error):
        replace(
            bundle.candidate_budget,
            injection_max_memories=21,
        )
    with pytest.raises(ActivePolicyEventV1Error):
        replace(
            bundle.renderer_policy,
            snippet_max_chars=12_001,
        )
    with pytest.raises(ActivePolicyEventV1Error):
        replace(
            bundle.trust_tier_policy,
            allow_legacy_unstructured=True,
        )
    narrowed_payload = replace(
        bundle.candidate_budget,
        payload_budget_bytes=4_096,
    )
    with pytest.raises(ActivePolicyEventV1Error):
        replace(bundle, candidate_budget=narrowed_payload)


def test_activation_requires_independent_actor_and_exact_predecessor():
    registration, activation = _records()
    wrong_predecessor = build_active_policy_activation(
        registration=registration.registration,
        previous_policy_bundle_id="active_policy_sha256_" + "f" * 64,
        activated_by=activation.activation.activated_by,
        activated_via_client_id=activation.activation.activated_via_client_id,
        authorization_event_id=activation.activation.authorization_event_id,
        activated_at=activation.activation.activated_at,
    )
    wrong_stored = replace(activation, activation=wrong_predecessor)
    with pytest.raises(ActivePolicyEventV1Error):
        reduce_active_policy_events(
            _events((registration, wrong_stored)),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )

    same_actor_activation = build_active_policy_activation(
        registration=registration.registration,
        previous_policy_bundle_id=None,
        activated_by=registration.registration.registered_by,
        activated_via_client_id=activation.activation.activated_via_client_id,
        authorization_event_id=activation.activation.authorization_event_id,
        activated_at=activation.activation.activated_at,
    )
    same_actor_stored = replace(activation, activation=same_actor_activation)
    with pytest.raises(ActivePolicyEventV1Error):
        reduce_active_policy_events(
            _events((registration, same_actor_stored)),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )


def test_successor_activation_moves_head_without_mutating_old_bundle():
    first_registration, first_activation = _records()
    second_bundle = _bundle(version="retrieval_policy_002")
    second_registration, second_activation = _records(
        bundle=second_bundle,
        previous_policy_bundle_id=(
            first_registration.registration.policy_bundle.policy_bundle_id
        ),
        registered_at="2026-07-27T00:05:00Z",
        activated_at="2026-07-27T00:06:00Z",
    )
    projection = reduce_active_policy_events(
        _events(
            (
                first_registration,
                first_activation,
                second_registration,
                second_activation,
            )
        ),
        trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
    )

    assert len(projection.registrations) == 2
    assert len(projection.activations) == 2
    assert projection.load_active_policy() == second_bundle
    assert projection.active_head is not None
    assert (
        projection.active_head.previous_policy_bundle_id
        == first_registration.registration.policy_bundle.policy_bundle_id
    )


def test_sqlite_append_and_rebuild_preserve_active_policy_snapshot():
    registration, activation = _records()
    connection = _ledger_connection()
    try:
        registration_ledger = SQLiteEventLedgerV1(
            connection,
            _access(
                registration.registration.registered_by,
                registration.registration.registered_via_client_id,
                registration.registration.authorization_event_id,
            ),
        )
        first = append_active_policy_records(
            registration_ledger,
            (registration,),
            recorded_at="2026-07-27T00:10:00Z",
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        assert first.receipt.current_stream_version == 1

        activation_ledger = SQLiteEventLedgerV1(
            connection,
            _access(
                activation.activation.activated_by,
                activation.activation.activated_via_client_id,
                activation.activation.authorization_event_id,
            ),
        )
        second = append_active_policy_records(
            activation_ledger,
            (activation,),
            recorded_at="2026-07-27T00:10:00Z",
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        snapshot = rebuild_active_policy_from_ledger(
            activation_ledger,
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        assert snapshot.source_event_count == 2
        assert snapshot.stream_version == 2
        assert snapshot.projection == second.projection
        assert snapshot() == registration.registration.policy_bundle.retrieval_policy

        forged_partition_sha256 = "sha256:" + "f" * 64
        forged_values = snapshot._unsigned_dict()
        forged_values["partition_sha256"] = forged_partition_sha256
        with pytest.raises(ActivePolicyEventV1Error) as forged_partition:
            replace(
                snapshot,
                partition_sha256=forged_partition_sha256,
                snapshot_sha256=canonical_sha256(forged_values),
            )
        assert (
            forged_partition.value.code
            == "TBM_ACTIVE_POLICY_PROJECTION_INVALID"
        )

        public_only = SQLiteEventLedgerV1(
            connection,
            replace(
                activation_ledger.access_context,
                classification_filter=LedgerClassificationFilter(("public",)),
            ),
        )
        with pytest.raises(ActivePolicyEventV1Error) as denied:
            rebuild_active_policy_from_ledger(
                public_only,
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            )
        assert denied.value.code == "TBM_ACTIVE_POLICY_CLASSIFICATION_DENIED"
    finally:
        connection.close()


def test_bundle_json_and_registry_are_bounded_and_sealed():
    bundle = _bundle()
    with pytest.raises(ActivePolicyEventV1Error):
        loads_active_policy_bundle(
            dumps_active_policy_bundle(bundle).replace(
                '"semantic_gate_required":true',
                '"semantic_gate_required":true,"semantic_gate_required":true',
            )
        )
    with pytest.raises(ActivePolicyEventV1Error):
        loads_active_policy_bundle("\ud800")

    registry = build_active_policy_event_registry()
    assert registry.sealed
    assert tuple(sorted(registry.catalog()))
    schema = active_policy_event_payload_dispatch_schema()
    assert len(schema["oneOf"]) == len(ACTIVE_POLICY_EVENT_TYPES)

    duplicate_modes = json.loads(dumps_active_policy_bundle(bundle))
    rules = duplicate_modes["retrieval_policy"]["mode_memory_rules"]
    rules[1] = rules[0]
    assert list(
        Draft202012Validator(active_policy_bundle_schema()).iter_errors(
            duplicate_modes
        )
    )

    with pytest.raises(ActivePolicyEventV1Error):
        build_active_policy_reducer(
            trusted_attestation_verifier_ids=([],),  # type: ignore[arg-type]
        )


def test_projection_rejects_cross_field_forged_active_head():
    registration, activation = _records()
    projection = reduce_active_policy_events(
        _events((registration, activation)),
        trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
    )
    assert projection.active_head is not None
    values = projection.active_head.to_dict()
    values.pop("head_sha256")
    values["retrieval_policy_id"] = "retrieval_policy_sha256_" + "f" * 64
    forged = replace(
        projection.active_head,
        retrieval_policy_id=values["retrieval_policy_id"],
        head_sha256=canonical_sha256(values),
    )
    with pytest.raises(ActivePolicyEventV1Error) as caught:
        replace(projection, active_head=forged)
    assert caught.value.code == "TBM_ACTIVE_POLICY_PROJECTION_INVALID"


def test_active_policy_resources_and_root_exports_are_exact():
    expected = {
        "schemas/active_policy_bundle_v1.schema.json": (
            active_policy_bundle_schema()
        ),
        "schemas/active_policy_event_payload_registry_v1.schema.json": (
            active_policy_event_payload_dispatch_schema()
        ),
        "examples/active_policy_bundle_v1.example.json": _bundle().to_dict(),
        "examples/active_policy_event_type_registry_v1.example.json": (
            build_active_policy_event_registry().catalog()
        ),
    }
    for relative, value in expected.items():
        path = ROOT / relative
        assert json.loads(path.read_text(encoding="utf-8")) == value
        assert read_packaged_resource(relative) == path.read_bytes()

    for name in (
        "ActivePolicyBundle",
        "ActivePolicyHead",
        "DurableActivePolicySnapshot",
        "append_active_policy_records",
        "rebuild_active_policy_from_ledger",
    ):
        assert name in tbm.__all__
        assert getattr(tbm, name) is not None
