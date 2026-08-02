from dataclasses import replace
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from tests.test_managed_index_v3 import _bundle
from tests.test_sqlite_event_ledger_v1 import _connection as _ledger_connection
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
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.retrieval_index_event_v1 import (
    INDEX_ACTIVATED,
    INDEX_BUILD_COMPLETED,
    INDEX_BUILD_REQUESTED,
    INDEX_MARKED_STALE,
    EventManagedIndexRepository,
    RetrievalIndexEventV1Error,
    StoredRetrievalIndexRecord,
    append_retrieval_index_records,
    build_index_activation,
    build_index_build_completion,
    build_index_build_request,
    build_index_stale_mark,
    build_retrieval_index_event_batch,
    build_retrieval_index_event_registry,
    build_retrieval_index_manifest,
    dumps_retrieval_index_manifest,
    loads_retrieval_index_manifest,
    rebuild_retrieval_index_from_ledger,
    reduce_retrieval_index_events,
    retrieval_index_event_payload_dispatch_schema,
    retrieval_index_manifest_schema,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1


DIGEST = "sha256:" + "a" * 64
TRUSTED_VERIFIERS = ("attestation_verifier",)
TRUSTED_EMBEDDINGS = (("reference_embeddings", "v1"),)
ALL_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
ROOT = Path(__file__).resolve().parents[1]


def _partition() -> LedgerTenantPartition:
    return LedgerTenantPartition(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
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
        tenant_id="tenant_001",
        status="active",
    )
    client = AgentClientIdentity(
        agent_client_id=client_id,
        tenant_id="tenant_001",
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
        role_name="index_operator",
        scope=AuthorizationScope(
            kind="repository",
            tenant_id="tenant_001",
            repository_id="repository_001",
        ),
        permissions=(permission,),  # type: ignore[arg-type]
        status="active",
        valid_from="2026-07-27T00:00:00Z",
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
        tenant_id="tenant_001",
        repository_reference="repository_001",
        permission=permission,  # type: ignore[arg-type]
        requested_at=requested_at,
    )
    decision = authorize(policy, request, decided_at=decided_at)
    assert decision.allowed
    return policy, request, decision


def _access(
    actor_id: str,
    client_id: str,
    authorization_event_id: str,
    *,
    classifications=ALL_CLASSIFICATIONS,
) -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=_partition(),
        principal_id=actor_id,
        agent_client_id=client_id,
        actor_type="principal",
        actor_id=actor_id,
        authorization_decision_id=authorization_event_id,
        classification_filter=LedgerClassificationFilter(classifications),
    )


def _stored(record, *, actor, client, permission, requested_at, decided_at):
    policy, request, decision = _authorization(
        actor_id=actor,
        client_id=client,
        permission=permission,
        requested_at=requested_at,
        decided_at=decided_at,
    )
    return StoredRetrievalIndexRecord(
        record=record,
        policy=policy,
        request=request,
        decision=decision,
        attestation_verified_by=TRUSTED_VERIFIERS[0],
    )


def _records():
    bundle = _bundle("memory_managed")
    manifest = build_retrieval_index_manifest(
        partition=_partition(),
        bundle=bundle,
        source_event_watermark=42,
        source_event_sha256=DIGEST,
    )
    request_auth = _authorization(
        actor_id="index_requester",
        client_id="index_request_service",
        permission="memory:create",
        requested_at="2026-07-27T00:00:30Z",
        decided_at="2026-07-27T00:01:00Z",
    )
    request = build_index_build_request(
        partition=_partition(),
        source_event_watermark=manifest.source_event_watermark,
        source_event_sha256=manifest.source_event_sha256,
        source_catalog_sha256=manifest.source_catalog_sha256,
        retriever_id=manifest.retriever_id,
        retriever_version=manifest.retriever_version,
        requested_by="index_requester",
        requested_via_client_id="index_request_service",
        authorization_event_id=request_auth[2].authorization_event_id,
        requested_at="2026-07-27T00:02:00Z",
    )
    stored_request = StoredRetrievalIndexRecord(
        request,
        *request_auth,
        attestation_verified_by=TRUSTED_VERIFIERS[0],
    )

    completion_auth = _authorization(
        actor_id="index_builder",
        client_id="index_build_service",
        permission="memory:create",
        requested_at="2026-07-27T00:02:30Z",
        decided_at="2026-07-27T00:03:00Z",
    )
    completion = build_index_build_completion(
        build_request=request,
        manifest=manifest,
        completed_by="index_builder",
        completed_via_client_id="index_build_service",
        authorization_event_id=completion_auth[2].authorization_event_id,
        completed_at="2026-07-27T00:04:00Z",
    )
    stored_completion = StoredRetrievalIndexRecord(
        completion,
        *completion_auth,
        attestation_verified_by=TRUSTED_VERIFIERS[0],
    )

    activation_auth = _authorization(
        actor_id="index_activator",
        client_id="index_activation_service",
        permission="memory:activate",
        requested_at="2026-07-27T00:04:30Z",
        decided_at="2026-07-27T00:05:00Z",
    )
    activation = build_index_activation(
        completion=completion,
        previous_bundle_id=None,
        activated_by="index_activator",
        activated_via_client_id="index_activation_service",
        authorization_event_id=activation_auth[2].authorization_event_id,
        activated_at="2026-07-27T00:06:00Z",
    )
    stored_activation = StoredRetrievalIndexRecord(
        activation,
        *activation_auth,
        attestation_verified_by=TRUSTED_VERIFIERS[0],
    )

    stale_auth = _authorization(
        actor_id="index_stale_operator",
        client_id="index_stale_service",
        permission="memory:activate",
        requested_at="2026-07-27T00:06:30Z",
        decided_at="2026-07-27T00:07:00Z",
    )
    stale = build_index_stale_mark(
        activation=activation,
        reason="source_advanced",
        marked_by="index_stale_operator",
        marked_via_client_id="index_stale_service",
        authorization_event_id=stale_auth[2].authorization_event_id,
        marked_at="2026-07-27T00:08:00Z",
    )
    stored_stale = StoredRetrievalIndexRecord(
        stale,
        *stale_auth,
        attestation_verified_by=TRUSTED_VERIFIERS[0],
    )
    return bundle, (
        stored_request,
        stored_completion,
        stored_activation,
        stored_stale,
    )


def _record_access(stored):
    record = stored.record
    if hasattr(record, "requested_by"):
        actor, client = record.requested_by, record.requested_via_client_id
    elif hasattr(record, "completed_by"):
        actor, client = record.completed_by, record.completed_via_client_id
    elif hasattr(record, "activated_by"):
        actor, client = record.activated_by, record.activated_via_client_id
    else:
        actor, client = record.marked_by, record.marked_via_client_id
    return _access(actor, client, record.authorization_event_id)


def _events(records):
    events = []
    parent = None
    for index, stored in enumerate(records, 1):
        batch, _ = build_retrieval_index_event_batch(
            _record_access(stored),
            (stored,),
            expected_stream_version=index - 1,
            next_global_position=index,
            previous_event=parent,
            recorded_at="2026-07-27T00:10:00Z",
        )
        events.extend(batch)
        parent = batch[-1]
    return tuple(events)


def test_manifest_binds_exact_five_indexes_watermark_and_bundle():
    bundle, records = _records()
    manifest = records[1].record.manifest
    assert [item.index_kind for item in manifest.index_versions] == [
        "metadata",
        "lexical",
        "semantic",
        "evidence_graph",
        "git_graph",
    ]
    assert manifest.source_event_watermark == 42
    assert loads_retrieval_index_manifest(
        dumps_retrieval_index_manifest(manifest)
    ) == manifest
    assert not list(
        Draft202012Validator(retrieval_index_manifest_schema()).iter_errors(
            manifest.to_dict()
        )
    )
    manifest.verify_bundle(bundle)
    with pytest.raises(RetrievalIndexEventV1Error) as mismatch:
        manifest.verify_bundle(_bundle("memory_other"))
    assert mismatch.value.code == "TBM_RETRIEVAL_INDEX_BUNDLE_MISMATCH"

    duplicate = manifest.to_dict()
    duplicate["index_versions"][1] = duplicate["index_versions"][0]
    assert list(
        Draft202012Validator(retrieval_index_manifest_schema()).iter_errors(
            duplicate
        )
    )


def test_reducer_rebuilds_activation_and_stale_status_deterministically():
    _, records = _records()
    events = _events(records)
    assert tuple(event.event_type for event in events) == (
        INDEX_BUILD_REQUESTED,
        INDEX_BUILD_COMPLETED,
        INDEX_ACTIVATED,
        INDEX_MARKED_STALE,
    )
    projection = reduce_retrieval_index_events(
        events,
        trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
    )
    assert projection == reduce_retrieval_index_events(
        events,
        trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
        event_registry=build_retrieval_index_event_registry(),
    )
    head = projection.load_active_head()
    assert head.bundle_id == records[1].record.manifest.bundle_id
    assert head.source_event_watermark == 42
    assert head.stale
    assert head.stale_reason == "source_advanced"
    assert head.status_event_sha256 == events[-1].event_sha256

    duplicate_position = replace(
        projection.records[1],
        global_position=projection.records[0].global_position,
    )
    with pytest.raises(RetrievalIndexEventV1Error) as duplicate:
        replace(
            projection,
            records=(
                projection.records[0],
                duplicate_position,
                *projection.records[2:],
            ),
        )
    assert duplicate.value.code == "TBM_RETRIEVAL_INDEX_PROJECTION_INVALID"


def test_reducer_rejects_untrusted_verifier_stale_predecessor_and_self_activation():
    _, records = _records()
    with pytest.raises(RetrievalIndexEventV1Error) as verifier:
        reduce_retrieval_index_events(
            _events(records[:3]),
            trusted_attestation_verifier_ids=("other_verifier",),
            trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
        )
    assert verifier.value.code == "TBM_RETRIEVAL_INDEX_TRANSITION_INVALID"

    with pytest.raises(RetrievalIndexEventV1Error) as provider:
        reduce_retrieval_index_events(
            _events(records[:3]),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            trusted_embedding_provider_models=(("unknown_provider", "v1"),),
        )
    assert provider.value.code == "TBM_RETRIEVAL_INDEX_TRANSITION_INVALID"

    activation = records[2].record
    stale_predecessor = build_index_activation(
        completion=records[1].record,
        previous_bundle_id="managed_index_bundle_sha256_" + "f" * 64,
        activated_by=activation.activated_by,
        activated_via_client_id=activation.activated_via_client_id,
        authorization_event_id=activation.authorization_event_id,
        activated_at=activation.activated_at,
    )
    with pytest.raises(RetrievalIndexEventV1Error) as predecessor:
        reduce_retrieval_index_events(
            _events((*records[:2], replace(records[2], record=stale_predecessor))),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
        )
    assert predecessor.value.code == "TBM_RETRIEVAL_INDEX_TRANSITION_INVALID"

    completion = records[1].record
    auth = _authorization(
        actor_id=completion.completed_by,
        client_id=completion.completed_via_client_id,
        permission="memory:activate",
        requested_at="2026-07-27T00:04:30Z",
        decided_at="2026-07-27T00:05:00Z",
    )
    self_activation = build_index_activation(
        completion=completion,
        previous_bundle_id=None,
        activated_by=completion.completed_by,
        activated_via_client_id=completion.completed_via_client_id,
        authorization_event_id=auth[2].authorization_event_id,
        activated_at="2026-07-27T00:06:00Z",
    )
    stored_self = StoredRetrievalIndexRecord(
        self_activation,
        *auth,
        attestation_verified_by=TRUSTED_VERIFIERS[0],
    )
    with pytest.raises(RetrievalIndexEventV1Error) as independent:
        reduce_retrieval_index_events(
            _events((*records[:2], stored_self)),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
        )
    assert independent.value.code == "TBM_RETRIEVAL_INDEX_TRANSITION_INVALID"


class _BundleRepository:
    def __init__(self, bundle):
        self.bundle = bundle

    def publish(self, bundle, *, expected_current_bundle_id):
        raise AssertionError((bundle, expected_current_bundle_id))

    def load(self, bundle_id):
        assert bundle_id == self.bundle.bundle_id
        return self.bundle

    def load_current(self, **scope):
        assert scope
        return self.bundle


def test_sqlite_append_rebuild_and_event_selected_repository_fail_closed():
    bundle, records = _records()
    connection = _ledger_connection()
    ledger = None
    try:
        result = None
        for stored in records[:3]:
            ledger = SQLiteEventLedgerV1(connection, _record_access(stored))
            result = append_retrieval_index_records(
                ledger,
                (stored,),
                recorded_at="2026-07-27T00:10:00Z",
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
                trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
            )
        assert result is not None
        snapshot = rebuild_retrieval_index_from_ledger(
            ledger,
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
        )
        assert snapshot.projection == result.projection
        assert snapshot.source_event_count == 3
        forged_values = snapshot._unsigned_dict()
        forged_values["stream_version"] = 2
        forged_values["source_event_count"] = 2
        with pytest.raises(RetrievalIndexEventV1Error) as forged_count:
            replace(
                snapshot,
                stream_version=2,
                source_event_count=2,
                snapshot_sha256=canonical_sha256(forged_values),
            )
        assert (
            forged_count.value.code
            == "TBM_RETRIEVAL_INDEX_PROJECTION_INVALID"
        )
        selected = EventManagedIndexRepository(_BundleRepository(bundle), snapshot)
        assert selected.load_current(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ) == bundle
        with pytest.raises(RetrievalIndexEventV1Error) as direct_publish:
            selected.publish(bundle, expected_current_bundle_id=None)
        assert direct_publish.value.code == "TBM_RETRIEVAL_INDEX_EVENT_REQUIRED"

        narrowed = SQLiteEventLedgerV1(
            connection,
            replace(
                ledger.access_context,
                classification_filter=LedgerClassificationFilter(("internal",)),
            ),
        )
        with pytest.raises(RetrievalIndexEventV1Error) as incomplete:
            rebuild_retrieval_index_from_ledger(
                narrowed,
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
                trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
            )
        assert (
            incomplete.value.code
            == "TBM_RETRIEVAL_INDEX_CLASSIFICATION_VIEW_INCOMPLETE"
        )

        stale_ledger = SQLiteEventLedgerV1(connection, _record_access(records[3]))
        stale_result = append_retrieval_index_records(
            stale_ledger,
            (records[3],),
            recorded_at="2026-07-27T00:10:00Z",
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
        )
        stale_selected = EventManagedIndexRepository(
            _BundleRepository(bundle), stale_result.projection
        )
        with pytest.raises(RetrievalIndexEventV1Error) as stale:
            stale_selected.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )
        assert stale.value.code == "TBM_RETRIEVAL_INDEX_HEAD_STALE"
    finally:
        connection.close()


def test_manifest_json_is_duplicate_key_and_unicode_safe():
    _, records = _records()
    manifest = records[1].record.manifest
    document = dumps_retrieval_index_manifest(manifest)
    with pytest.raises(RetrievalIndexEventV1Error):
        loads_retrieval_index_manifest(
            document.replace(
                '"stale_status":"fresh"',
                '"stale_status":"fresh","stale_status":"fresh"',
            )
        )
    with pytest.raises(RetrievalIndexEventV1Error):
        loads_retrieval_index_manifest(document.replace("42", "NaN", 1))
    with pytest.raises(RetrievalIndexEventV1Error):
        loads_retrieval_index_manifest(json.dumps({"bad": "\ud800"}))


def test_retrieval_index_resources_and_root_exports_are_exact():
    _, records = _records()
    expected = {
        "schemas/retrieval_index_manifest_v1.schema.json": (
            retrieval_index_manifest_schema()
        ),
        "schemas/retrieval_index_event_payload_registry_v1.schema.json": (
            retrieval_index_event_payload_dispatch_schema()
        ),
        "examples/retrieval_index_event_type_registry_v1.example.json": (
            build_retrieval_index_event_registry().catalog()
        ),
    }
    for relative, value in expected.items():
        path = ROOT / relative
        assert json.loads(path.read_text(encoding="utf-8")) == value
        assert read_packaged_resource(relative) == path.read_bytes()

    example = ROOT / "examples/retrieval_index_manifest_v1.example.json"
    loaded_example = loads_retrieval_index_manifest(example.read_text("utf-8"))
    assert [item.index_kind for item in loaded_example.index_versions] == [
        "metadata",
        "lexical",
        "semantic",
        "evidence_graph",
        "git_graph",
    ]
    assert read_packaged_resource(
        "examples/retrieval_index_manifest_v1.example.json"
    ) == example.read_bytes()

    assert records[1].record.manifest.source_event_watermark == 42
    for name in (
        "RetrievalIndexManifest",
        "RetrievalIndexProjection",
        "EventManagedIndexRepository",
        "append_retrieval_index_records",
        "rebuild_retrieval_index_from_ledger",
    ):
        assert name in tbm.__all__
