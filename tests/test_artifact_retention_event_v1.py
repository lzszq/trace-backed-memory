from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from jsonschema import Draft202012Validator

import trace_backed_memory as tbm
from tests.test_managed_index_v3 import _bundle
from trace_backed_memory.artifact_retention_event_v1 import (
    ARTIFACT_CRYPTOGRAPHICALLY_ERASED,
    ARTIFACT_CRYPTO_ERASURE_AUTHORIZED,
    ARTIFACT_CRYPTO_ERASURE_BLOCKED,
    ARTIFACT_CRYPTO_ERASURE_REQUESTED,
    ARTIFACT_CRYPTO_ERASURE_UNKNOWN,
    ARTIFACT_INDEX_PURGED,
    ARTIFACT_REDACTION_MANIFEST_RECORDED,
    ARTIFACT_REPLAY_PARTIAL_MARKED,
    ARTIFACT_RETENTION_APPLIED,
    ARTIFACT_TOMBSTONED,
    ARTIFACT_RETENTION_MANIFEST_MAX_BYTES,
    ArtifactRetentionDecision,
    ArtifactRetentionEventV1Error,
    KeyDestructionReceipt,
    KeyReferenceSet,
    RedactionTarget,
    ReplayImpact,
    RetentionErasureCoordinator,
    RetentionDestructionAuthorization,
    RetentionRequest,
    RetentionResolution,
    TrustedKeyDestructionProvider,
    artifact_retention_stream_id,
    build_artifact_retention_event_registry,
    build_replay_partial_marker,
    build_retention_destruction_authorization,
    build_retention_policy_snapshot,
    dumps_redaction_manifest,
    dumps_artifact_retention_payload_dispatch_schema,
    loads_redaction_manifest,
    reduce_artifact_retention_events,
    require_replay_not_erased,
    verify_artifact_retention_event,
)
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import (
    SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE,
    SQLiteEventLedgerV1,
)


NOW = "2026-08-02T08:00:00Z"
EXPIRED = "2026-08-01T08:00:00Z"
AUTHORIZATION = "authorization_retention_001"
ROOT = Path(__file__).resolve().parents[1]


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _rebuild_event(
    event: CanonicalEvent,
    *,
    producer: str | None = None,
    retention_policy_id: str | None = None,
    artifact_refs: tuple[EventArtifactRef, ...] | None = None,
    payload: dict[str, object] | None = None,
) -> CanonicalEvent:
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
        trusted_context=EventTrustedContext(
            organization_id=event.organization_id,
            tenant_id=event.tenant_id,
            repository_id=event.repository_id,
            environment_id=event.environment_id,
            principal_id=event.principal_id,
            agent_client_id=event.agent_client_id,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            authorization_decision_id=event.authorization_decision_id,
        ),
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=event.request_sha256,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer if producer is None else producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=(
            event.retention_policy_id
            if retention_policy_id is None
            else retention_policy_id
        ),
        artifact_refs=event.artifact_refs if artifact_refs is None else artifact_refs,
        payload=dict(event.payload) if payload is None else payload,
    )


def _artifact(
    label: str,
    *,
    key_id: str = "source_key_001",
    availability: tbm.EventArtifactAvailability = "available",
) -> EventArtifactRef:
    payload = label.encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + digest.removeprefix("sha256:"),
        content_sha256=digest,
        media_type="application/octet-stream",
        size_bytes=len(payload),
        classification="restricted",
        retention_policy_id="retention_raw_execution_v1",
        encryption_key_id=key_id,
        availability=availability,
    )


def _access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="retention_worker_001",
        authorization_decision_id=AUTHORIZATION,
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _ledger() -> SQLiteEventLedgerV1:
    connection = sqlite3.connect(
        ":memory:", isolation_level=None, check_same_thread=False
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource(SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE).decode(
            "utf-8"
        )
    )
    return SQLiteEventLedgerV1(connection, _access())


class _ManifestStore:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.put_calls = 0

    def put(self, manifest: tbm.RedactionManifest, payload: bytes) -> EventArtifactRef:
        self.put_calls += 1
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        descriptor = EventArtifactRef(
            artifact_id="artifact_sha256_" + digest.removeprefix("sha256:"),
            content_sha256=digest,
            media_type="application/vnd.tbm.redaction-manifest+json",
            size_bytes=len(payload),
            classification="confidential",
            retention_policy_id="retention_artifact_governance_v1",
            encryption_key_id="manifest_key_001",
            availability="available",
        )
        prior = self.payloads.setdefault(descriptor.artifact_id, payload)
        assert prior == payload
        return descriptor

    def load(self, descriptor: EventArtifactRef) -> bytes:
        return self.payloads[descriptor.artifact_id]


class _TargetKeyManifestStore(_ManifestStore):
    def put(self, manifest: tbm.RedactionManifest, payload: bytes) -> EventArtifactRef:
        descriptor = super().put(manifest, payload)
        return replace(descriptor, encryption_key_id="source_key_001")


class _Resolver:
    def __init__(self, resolution: RetentionResolution) -> None:
        self.resolution = resolution
        self.calls = 0

    def resolve(self, request: RetentionRequest) -> RetentionResolution:
        self.calls += 1
        return self.resolution


class _Guard:
    def __init__(
        self,
        resolution: RetentionResolution,
        *,
        legal_hold: bool = False,
        hold_on_call: int | None = None,
    ) -> None:
        self.resolution = resolution
        self.legal_hold = legal_hold
        self.hold_on_call = hold_on_call
        self.calls = 0

    def evaluate(
        self,
        resolution: RetentionResolution,
        *,
        evaluated_at: str,
    ) -> tbm.RetentionPolicySnapshot:
        assert resolution == self.resolution
        self.calls += 1
        legal_hold = self.legal_hold or self.calls == self.hold_on_call
        decisions = tuple(
            ArtifactRetentionDecision(
                artifact_id=target.artifact.artifact_id,
                retention_policy_id=target.artifact.retention_policy_id,
                retain_until=EXPIRED,
                legal_hold=legal_hold,
                hold_epoch=1 if legal_hold else 0,
            )
            for target in resolution.targets
        )
        return build_retention_policy_snapshot(
            decisions,
            evaluated_at=evaluated_at,
        )

    def authorize_destruction(
        self,
        *,
        operation_id: str,
        resolution: RetentionResolution,
        expected_policy_state_sha256: str,
        authorized_at: str,
    ) -> tbm.RetentionDestructionAuthorization:
        if self.legal_hold:
            raise RuntimeError("held")
        return build_retention_destruction_authorization(
            operation_id=operation_id,
            resolution=resolution,
            policy_state_sha256=expected_policy_state_sha256,
            authorized_at=authorized_at,
            expires_at="2026-08-02T08:05:00Z",
        )


class _WrongHoldGuard(_Guard):
    def authorize_destruction(
        self,
        *,
        operation_id: str,
        resolution: RetentionResolution,
        expected_policy_state_sha256: str,
        authorized_at: str,
    ) -> RetentionDestructionAuthorization:
        del resolution
        wrong_hold = _digest("wrong-hold-epoch")
        unsigned = {
            "operation_id": operation_id,
            "policy_state_sha256": expected_policy_state_sha256,
            "hold_epoch_sha256": wrong_hold,
            "authorized_at": authorized_at,
            "expires_at": "2026-08-02T08:05:00Z",
        }
        return RetentionDestructionAuthorization(
            authorization_sha256=tbm.canonical_sha256(unsigned),
            operation_id=operation_id,
            policy_state_sha256=expected_policy_state_sha256,
            hold_epoch_sha256=wrong_hold,
            authorized_at=authorized_at,
            expires_at="2026-08-02T08:05:00Z",
        )


class _WrongPolicyGuard(_Guard):
    def evaluate(
        self,
        resolution: RetentionResolution,
        *,
        evaluated_at: str,
    ) -> tbm.RetentionPolicySnapshot:
        snapshot = super().evaluate(resolution, evaluated_at=evaluated_at)
        return build_retention_policy_snapshot(
            tuple(
                replace(decision, retention_policy_id="retention_other_policy_v1")
                for decision in snapshot.decisions
            ),
            evaluated_at=evaluated_at,
        )


class _ReceiptVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(
        self,
        request: tbm.KeyDestructionRequest,
        receipt: KeyDestructionReceipt,
    ) -> None:
        self.calls += 1
        assert receipt.receipt_artifact is not None
        expected = (request.provider_request_id + "-receipt").encode()
        assert receipt.receipt_artifact.content_sha256 == (
            "sha256:" + hashlib.sha256(expected).hexdigest()
        )


class _RejectingReceiptVerifier(_ReceiptVerifier):
    def verify(
        self,
        request: tbm.KeyDestructionRequest,
        receipt: KeyDestructionReceipt,
    ) -> None:
        super().verify(request, receipt)
        raise RuntimeError("receipt signature rejected")


class _KeyProvider:
    trusted_provider = TrustedKeyDestructionProvider(
        provider_id="kms_provider_001",
        provider_version="v1",
        registration_sha256=_digest("kms-registration"),
        attestation_sha256=_digest("kms-attestation"),
    )

    def __init__(self, *, unknown_first: bool = False) -> None:
        self.unknown_first = unknown_first
        self.destroy_calls = 0
        self.reconcile_calls = 0

    def destroy(self, request: tbm.KeyDestructionRequest) -> KeyDestructionReceipt:
        self.destroy_calls += 1
        if self.unknown_first:
            return KeyDestructionReceipt(
                provider_request_id=request.provider_request_id,
                request_sha256=request.request_sha256,
                status="unknown",
                receipt_sha256=None,
                receipt_artifact=None,
                completed_at=None,
            )
        return self._success(request)

    def reconcile(
        self, request: tbm.KeyDestructionRequest
    ) -> KeyDestructionReceipt:
        self.reconcile_calls += 1
        return self._success(request, status="already_destroyed")

    def _success(
        self,
        request: tbm.KeyDestructionRequest,
        *,
        status: tbm.KeyDestructionStatus = "destroyed",
    ) -> KeyDestructionReceipt:
        payload = (request.provider_request_id + "-receipt").encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        artifact = EventArtifactRef(
            artifact_id="artifact_sha256_" + digest.removeprefix("sha256:"),
            content_sha256=digest,
            media_type="application/provider-receipt+json",
            size_bytes=len(payload),
            classification="confidential",
            retention_policy_id="retention_artifact_governance_v1",
            encryption_key_id="receipt_key_001",
            availability="available",
        )
        return KeyDestructionReceipt(
            provider_request_id=request.provider_request_id,
            request_sha256=request.request_sha256,
            status=status,
            receipt_sha256=digest,
            receipt_artifact=artifact,
            completed_at=NOW,
        )


class _FutureReceiptProvider(_KeyProvider):
    def _success(
        self,
        request: tbm.KeyDestructionRequest,
        *,
        status: tbm.KeyDestructionStatus = "destroyed",
    ) -> KeyDestructionReceipt:
        return replace(
            super()._success(request, status=status),
            completed_at="2099-01-01T00:00:00Z",
        )


class _RejectingKeyProvider(_KeyProvider):
    def destroy(self, request: tbm.KeyDestructionRequest) -> KeyDestructionReceipt:
        self.destroy_calls += 1
        return KeyDestructionReceipt(
            provider_request_id=request.provider_request_id,
            request_sha256=request.request_sha256,
            status="rejected",
            receipt_sha256=None,
            receipt_artifact=None,
            completed_at=None,
            failure_code="kms_policy_rejected",
        )


class _IndexReintroducingProvider(_KeyProvider):
    def __init__(self, repository, original: tbm.ManagedIndexBundle) -> None:
        super().__init__()
        self.repository = repository
        self.original = original
        self.expected_successor: str | None = None

    def destroy(self, request: tbm.KeyDestructionRequest) -> KeyDestructionReceipt:
        assert self.expected_successor is not None
        self.repository.publish(
            self.original,
            expected_current_bundle_id=self.expected_successor,
        )
        return super().destroy(request)


class _LedgerProxy:
    def __init__(
        self,
        inner: SQLiteEventLedgerV1,
        *,
        access: LedgerAccessContext | None = None,
        fail_final_once: bool = False,
        fail_event_type_once: str | None = None,
    ) -> None:
        self.inner = inner
        self._access = access or inner.access_context
        self.fail_final_once = fail_final_once
        self.fail_event_type_once = fail_event_type_once

    @property
    def access_context(self) -> LedgerAccessContext:
        return self._access

    def append(self, stream_id, expected_version, events, idempotency):
        if self.fail_final_once and any(
            item.event_type == ARTIFACT_CRYPTOGRAPHICALLY_ERASED
            for item in events
        ):
            self.fail_final_once = False
            raise RuntimeError("simulated outcome persistence failure")
        if self.fail_event_type_once is not None and any(
            item.event_type == self.fail_event_type_once for item in events
        ):
            self.fail_event_type_once = None
            raise RuntimeError("simulated event persistence failure")
        return self.inner.append(stream_id, expected_version, events, idempotency)

    def read_stream(self, stream_id, from_version=1, limit=100):
        return self.inner.read_stream(stream_id, from_version, limit)

    def read_global(self, after_global_position=0, limit=100):
        return self.inner.read_global(after_global_position, limit)


def _resolution(bundle: tbm.ManagedIndexBundle) -> RetentionResolution:
    artifact = _artifact("raw-tool-output")
    target = RedactionTarget(
        artifact=artifact,
        memory_revision_ids=(bundle.candidates[0].memory_revision_id,),
        replay_impacts=(
            ReplayImpact(
                replay_manifest_sha256=_digest("complete-replay-manifest"),
                missing_components=("injection_artifact",),
            ),
        ),
    )
    return RetentionResolution(
        targets=(target,),
        key_references=(
            KeyReferenceSet(
                encryption_key_id="source_key_001",
                artifact_ids=(artifact.artifact_id,),
            ),
        ),
    )


def _request(resolution: RetentionResolution) -> RetentionRequest:
    return RetentionRequest(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
        authorization_event_id=AUTHORIZATION,
        artifact_ids=tuple(
            item.artifact.artifact_id for item in resolution.targets
        ),
        deletion_policy_id="delete_expired_execution_evidence_v1",
        reason_code="retention_expired",
        idempotency_key_sha256=_digest("retention-command"),
    )


def _coordinator(
    repository: tbm.SQLiteManagedIndexV3Repository,
    resolution: RetentionResolution,
    *,
    guard: _Guard | None = None,
    provider: _KeyProvider | None = None,
    store: _ManifestStore | None = None,
    verifier: _ReceiptVerifier | None = None,
    clock= lambda: NOW,
) -> tuple[
    RetentionErasureCoordinator,
    SQLiteEventLedgerV1,
    _ManifestStore,
    _Guard,
    _KeyProvider,
]:
    ledger = _ledger()
    selected_store = store or _ManifestStore()
    selected_guard = guard or _Guard(resolution)
    selected_provider = provider or _KeyProvider()
    coordinator = RetentionErasureCoordinator(
        ledger=ledger,
        managed_index=repository,
        manifest_store=selected_store,
        target_resolver=_Resolver(resolution),
        policy_guard=selected_guard,
        key_destruction_provider=selected_provider,
        receipt_verifier=verifier or _ReceiptVerifier(),
        clock=clock,
    )
    return coordinator, ledger, selected_store, selected_guard, selected_provider


def test_happy_path_persists_manifest_purges_index_marks_replay_and_tombstones():
    original = _bundle("memory_retained", "memory_survives")
    resolution = _resolution(original)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, store, _, provider = _coordinator(
            repository, resolution
        )

        manifest = coordinator.plan(_request(resolution))
        assert loads_redaction_manifest(dumps_redaction_manifest(manifest)) == manifest
        result = coordinator.submit(manifest)

        assert result.status == "tombstoned"
        assert result.projection.status == "tombstoned"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 0
        assert store.put_calls == 1
        current = repository.load_current(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        )
        assert current.bundle_id == manifest.successor_index_bundle_id
        assert resolution.targets[0].memory_revision_ids[0] not in {
            item.memory_revision_id for item in current.candidates
        }
        assert repository.load(original.bundle_id) == original

        events = ledger.read_stream(
            artifact_retention_stream_id(manifest.operation_id), 1, 64
        ).events
        assert tuple(item.event_type for item in events) == (
            ARTIFACT_RETENTION_APPLIED,
            ARTIFACT_REDACTION_MANIFEST_RECORDED,
            ARTIFACT_CRYPTO_ERASURE_REQUESTED,
            ARTIFACT_INDEX_PURGED,
            ARTIFACT_CRYPTO_ERASURE_AUTHORIZED,
            ARTIFACT_REPLAY_PARTIAL_MARKED,
            ARTIFACT_CRYPTOGRAPHICALLY_ERASED,
            ARTIFACT_TOMBSTONED,
        )
        assert all("raw-tool-output" not in repr(item.payload) for item in events)
        final_refs = {item.artifact_id: item for item in events[-1].artifact_refs}
        assert (
            final_refs[resolution.targets[0].artifact.artifact_id].availability
            == "erased"
        )
        marker = manifest.replay_partial_markers[0]
        with pytest.raises(
            ArtifactRetentionEventV1Error,
            match="exact replay is unavailable",
        ):
            require_replay_not_erased(
                manifest.replay_partial_markers,
                marker.replay_manifest_sha256,
            )

        replay = coordinator.recover(manifest.operation_id)
        assert replay.replayed is True
        assert replay.status == "tombstoned"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 0


def test_unknown_kms_result_requires_reconciliation_and_never_blind_retries():
    original = _bundle("memory_unknown", "memory_other")
    resolution = _resolution(original)
    provider = _KeyProvider(unknown_first=True)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, _, _, _ = _coordinator(
            repository,
            resolution,
            provider=provider,
        )
        manifest = coordinator.plan(_request(resolution))

        pending = coordinator.submit(manifest)
        assert pending.status == "crypto_erasure_unknown"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 0
        event_types = tuple(
            item.event_type
            for item in ledger.read_stream(
                artifact_retention_stream_id(manifest.operation_id), 1, 64
            ).events
        )
        assert event_types[-1] == ARTIFACT_CRYPTO_ERASURE_UNKNOWN
        assert ARTIFACT_TOMBSTONED not in event_types

        recovered = coordinator.recover(manifest.operation_id)
        assert recovered.status == "tombstoned"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 1


def test_legal_hold_wins_before_external_call_even_after_intent_and_index_purge():
    original = _bundle("memory_held", "memory_other")
    resolution = _resolution(original)
    guard = _Guard(resolution, hold_on_call=3)
    provider = _KeyProvider()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, store, _, _ = _coordinator(
            repository,
            resolution,
            guard=guard,
            provider=provider,
        )
        manifest = coordinator.plan(_request(resolution))
        blocked = coordinator.submit(manifest)

        assert blocked.status == "blocked"
        assert provider.destroy_calls == 0
        assert provider.reconcile_calls == 0
        assert store.put_calls == 1
        event_types = tuple(
            item.event_type
            for item in ledger.read_stream(
                artifact_retention_stream_id(manifest.operation_id), 1, 64
            ).events
        )
        assert event_types[-2:] == (
            ARTIFACT_INDEX_PURGED,
            ARTIFACT_CRYPTO_ERASURE_BLOCKED,
        )


def test_plan_fails_closed_for_current_hold_or_shared_key_without_writes():
    original = _bundle("memory_closed", "memory_other")
    resolution = _resolution(original)
    held_guard = _Guard(resolution, legal_hold=True)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, store, _, provider = _coordinator(
            repository,
            resolution,
            guard=held_guard,
        )
        with pytest.raises(ArtifactRetentionEventV1Error) as caught:
            coordinator.plan(_request(resolution))
        assert caught.value.code == "TBM_RETENTION_LEGAL_HOLD"
        assert ledger.read_global(0, 1).events == ()
        assert store.put_calls == 0
        assert provider.destroy_calls == 0

        extra = _artifact("other-live-artifact", key_id="source_key_001")
        unsafe = replace(
            resolution,
            key_references=(
                KeyReferenceSet(
                    "source_key_001",
                    tuple(
                        sorted(
                            (
                                resolution.targets[0].artifact.artifact_id,
                                extra.artifact_id,
                            )
                        )
                    ),
                ),
            ),
        )
        unsafe_coordinator, unsafe_ledger, unsafe_store, _, unsafe_provider = (
            _coordinator(repository, unsafe)
        )
        with pytest.raises(ArtifactRetentionEventV1Error) as shared:
            unsafe_coordinator.plan(_request(unsafe))
        assert shared.value.code == "TBM_RETENTION_KEY_STILL_REFERENCED"
        assert unsafe_ledger.read_global(0, 1).events == ()
        assert unsafe_store.put_calls == 0
        assert unsafe_provider.destroy_calls == 0


def test_registry_root_exports_and_successor_helper_are_stable():
    registry = build_artifact_retention_event_registry()
    assert registry.sealed
    assert tuple(
        row["event_type"] for row in registry.catalog()["event_types"]
    ) == tuple(sorted(tbm.ARTIFACT_RETENTION_EVENT_TYPES))
    schema_text = dumps_artifact_retention_payload_dispatch_schema()
    Draft202012Validator.check_schema(json.loads(schema_text))
    assert schema_text == dumps_artifact_retention_payload_dispatch_schema()
    assert (
        ROOT / "schemas" / "artifact_retention_event_payload_registry_v1.schema.json"
    ).read_text(encoding="utf-8") == schema_text
    assert json.loads(
        (
            ROOT
            / "examples"
            / "artifact_retention_event_type_registry_v1.example.json"
        ).read_text(encoding="utf-8")
    ) == registry.catalog()
    assert tbm.RetentionErasureCoordinator is RetentionErasureCoordinator
    assert tbm.RedactionManifest is tbm.artifact_retention_event_v1.RedactionManifest

    original = _bundle("memory_remove", "memory_keep")
    successor = tbm.purge_managed_index_revisions(
        original,
        memory_revision_ids=(original.candidates[0].memory_revision_id,),
    )
    assert successor.bundle_id != original.bundle_id
    assert len(successor.candidates) == 1
    assert original.candidates[0] not in successor.candidates
    assert len(original.candidates) == 2


def test_final_append_failure_recovers_by_reconciliation_without_destroy_retry():
    original = _bundle("memory_crash", "memory_other")
    resolution = _resolution(original)
    inner = _ledger()
    ledger = _LedgerProxy(inner, fail_final_once=True)
    store = _ManifestStore()
    guard = _Guard(resolution)
    provider = _KeyProvider()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator = RetentionErasureCoordinator(
            ledger=ledger,
            managed_index=repository,
            manifest_store=store,
            target_resolver=_Resolver(resolution),
            policy_guard=guard,
            key_destruction_provider=provider,
            receipt_verifier=_ReceiptVerifier(),
            clock=lambda: NOW,
        )
        manifest = coordinator.plan(_request(resolution))

        with pytest.raises(ArtifactRetentionEventV1Error) as caught:
            coordinator.submit(manifest)
        assert caught.value.code == "TBM_RETENTION_RECOVERY_REQUIRED"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 0
        before_recovery = inner.read_stream(
            artifact_retention_stream_id(manifest.operation_id), 1, 64
        ).events
        assert before_recovery[-1].event_type == ARTIFACT_CRYPTO_ERASURE_AUTHORIZED

        recovered = coordinator.recover(manifest.operation_id)
        assert recovered.status == "tombstoned"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 1


def test_recovery_repurges_reintroduced_original_head_before_reconcile():
    original = _bundle("memory_reintroduced", "memory_other")
    resolution = _resolution(original)
    provider = _KeyProvider(unknown_first=True)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, _, _, _, _ = _coordinator(
            repository,
            resolution,
            provider=provider,
        )
        manifest = coordinator.plan(_request(resolution))
        assert coordinator.submit(manifest).status == "crypto_erasure_unknown"
        repository.publish(
            original,
            expected_current_bundle_id=manifest.successor_index_bundle_id,
        )

        recovered = coordinator.recover(manifest.operation_id)
        assert recovered.status == "tombstoned"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 1
        current = repository.load_current(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        )
        assert current.bundle_id == manifest.successor_index_bundle_id


def test_recovery_rechecks_trusted_authorization_before_external_call():
    original = _bundle("memory_access", "memory_other")
    resolution = _resolution(original)
    provider = _KeyProvider(unknown_first=True)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, store, guard, _ = _coordinator(
            repository,
            resolution,
            provider=provider,
        )
        manifest = coordinator.plan(_request(resolution))
        assert coordinator.submit(manifest).status == "crypto_erasure_unknown"
        wrong_access = replace(
            _access(),
            authorization_decision_id="authorization_retention_other_001",
        )
        untrusted = RetentionErasureCoordinator(
            ledger=_LedgerProxy(ledger, access=wrong_access),
            managed_index=repository,
            manifest_store=store,
            target_resolver=_Resolver(resolution),
            policy_guard=guard,
            key_destruction_provider=provider,
            receipt_verifier=_ReceiptVerifier(),
            clock=lambda: NOW,
        )

        with pytest.raises(ArtifactRetentionEventV1Error) as caught:
            untrusted.recover(manifest.operation_id)
        assert caught.value.code == "TBM_RETENTION_ACCESS_DENIED"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 0


def test_manifest_and_receipt_governance_keys_cannot_be_erasure_targets():
    original = _bundle("memory_key_isolation", "memory_other")
    resolution = _resolution(original)
    bad_store = _TargetKeyManifestStore()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, _, _, provider = _coordinator(
            repository,
            resolution,
            store=bad_store,
        )
        manifest = coordinator.plan(_request(resolution))

        with pytest.raises(ArtifactRetentionEventV1Error) as caught:
            coordinator.submit(manifest)
        assert caught.value.code == "TBM_RETENTION_MANIFEST_ARTIFACT_INVALID"
        assert ledger.read_global(0, 1).events == ()
        assert provider.destroy_calls == 0

        receipt_provider = _KeyProvider()
        receipt_verifier = _RejectingReceiptVerifier()
        receipt_coordinator, receipt_ledger, _, _, _ = _coordinator(
            repository,
            resolution,
            provider=receipt_provider,
            verifier=receipt_verifier,
        )
        receipt_manifest = receipt_coordinator.plan(_request(resolution))
        pending = receipt_coordinator.submit(receipt_manifest)
        assert pending.status == "crypto_erasure_unknown"
        assert receipt_provider.destroy_calls == 1
        assert ARTIFACT_TOMBSTONED not in {
            item.event_type for item in receipt_ledger.read_global(0, 64).events
        }


def test_operation_identity_is_stable_across_plan_time_and_manifest_is_not():
    original = _bundle("memory_idempotent", "memory_other")
    resolution = _resolution(original)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        first, _, _, _, _ = _coordinator(
            repository,
            resolution,
            clock=lambda: "2026-08-02T08:00:00Z",
        )
        second, _, _, _, _ = _coordinator(
            repository,
            resolution,
            clock=lambda: "2026-08-02T08:01:00Z",
        )

        first_manifest = first.plan(_request(resolution))
        second_manifest = second.plan(_request(resolution))
        assert first_manifest.operation_id == second_manifest.operation_id
        assert first_manifest.manifest_sha256 != second_manifest.manifest_sha256


def test_manifest_parser_bounds_runtime_partial_boundary_and_private_drafts():
    with pytest.raises(ArtifactRetentionEventV1Error):
        loads_redaction_manifest(b'{"a":1,"a":2}')
    with pytest.raises(ArtifactRetentionEventV1Error) as oversized:
        loads_redaction_manifest(b" " * (ARTIFACT_RETENTION_MANIFEST_MAX_BYTES + 1))
    assert oversized.value.code == "TBM_RETENTION_MANIFEST_TOO_LARGE"
    with pytest.raises(ArtifactRetentionEventV1Error) as legacy:
        ReplayImpact(
            replay_manifest_sha256=_digest("legacy-partial-replay"),
            missing_components=("injection_artifact",),
            source_completeness="legacy_partial",
            source_missing_components=("usage_decision",),
        )
    assert legacy.value.code == "TBM_RETENTION_REPLAY_SOURCE_PARTIAL"
    assert not hasattr(tbm, "RetentionEventDraft")
    assert "RetentionEventDraft" not in tbm.artifact_retention_event_v1.__all__


def test_retention_event_reducer_rejects_forged_envelopes_receipts_and_refs():
    original = _bundle("memory_forge", "memory_other")
    resolution = _resolution(original)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, _, _, _ = _coordinator(repository, resolution)
        manifest = coordinator.plan(_request(resolution))
        assert coordinator.submit(manifest).status == "tombstoned"
        events = ledger.read_stream(
            artifact_retention_stream_id(manifest.operation_id), 1, 64
        ).events
        final = events[-1]

        with pytest.raises(ArtifactRetentionEventV1Error):
            verify_artifact_retention_event(
                _rebuild_event(final, producer="untrusted_retention_writer")
            )

        wrong_policy = _rebuild_event(
            final,
            retention_policy_id="unrelated_deletion_policy_v1",
        )
        with pytest.raises(ArtifactRetentionEventV1Error):
            reduce_artifact_retention_events(
                (*events[:-1], wrong_policy),
                manifest=manifest,
            )

        wrong_payload = dict(final.payload)
        wrong_payload["provider_receipt_sha256s"] = (_digest("forged-receipt"),)
        with pytest.raises(ArtifactRetentionEventV1Error):
            reduce_artifact_retention_events(
                (*events[:-1], _rebuild_event(final, payload=wrong_payload)),
                manifest=manifest,
            )

        target_id = resolution.targets[0].artifact.artifact_id
        wrong_refs = tuple(
            replace(item, encryption_key_id="attacker_key_001")
            if item.artifact_id == target_id
            else item
            for item in final.artifact_refs
        )
        with pytest.raises(ArtifactRetentionEventV1Error):
            reduce_artifact_retention_events(
                (*events[:-1], _rebuild_event(final, artifact_refs=wrong_refs)),
                manifest=manifest,
            )


def test_provider_future_receipt_rejection_and_hold_epoch_fail_closed():
    original = _bundle("memory_provider_negative", "memory_other")
    resolution = _resolution(original)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        future_provider = _FutureReceiptProvider()
        future, future_ledger, _, _, _ = _coordinator(
            repository,
            resolution,
            provider=future_provider,
        )
        future_manifest = future.plan(_request(resolution))
        assert future.submit(future_manifest).status == "crypto_erasure_unknown"
        assert ARTIFACT_TOMBSTONED not in {
            item.event_type for item in future_ledger.read_global(0, 64).events
        }

        wrong_guard = _WrongHoldGuard(resolution)
        blocked_provider = _KeyProvider()
        blocked, _, _, _, _ = _coordinator(
            repository,
            resolution,
            guard=wrong_guard,
            provider=blocked_provider,
        )
        blocked_manifest = blocked.plan(_request(resolution))
        assert blocked.submit(blocked_manifest).status == "blocked"
        assert blocked_provider.destroy_calls == 0


def test_provider_rejection_is_terminal_and_never_tombstones():
    original = _bundle("memory_rejected", "memory_other")
    resolution = _resolution(original)
    provider = _RejectingKeyProvider()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, _, _, _ = _coordinator(
            repository,
            resolution,
            provider=provider,
        )
        manifest = coordinator.plan(_request(resolution))
        rejected = coordinator.submit(manifest)
        assert rejected.status == "crypto_erasure_rejected"
        assert provider.destroy_calls == 1
        assert ARTIFACT_TOMBSTONED not in {
            item.event_type for item in ledger.read_global(0, 64).events
        }
        assert coordinator.recover(manifest.operation_id).status == (
            "crypto_erasure_rejected"
        )
        assert provider.destroy_calls == 1


def test_index_publication_append_crash_recovers_from_successor_head():
    original = _bundle("memory_index_crash", "memory_other")
    resolution = _resolution(original)
    inner = _ledger()
    ledger = _LedgerProxy(inner, fail_event_type_once=ARTIFACT_INDEX_PURGED)
    store = _ManifestStore()
    provider = _KeyProvider()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator = RetentionErasureCoordinator(
            ledger=ledger,
            managed_index=repository,
            manifest_store=store,
            target_resolver=_Resolver(resolution),
            policy_guard=_Guard(resolution),
            key_destruction_provider=provider,
            receipt_verifier=_ReceiptVerifier(),
            clock=lambda: NOW,
        )
        manifest = coordinator.plan(_request(resolution))
        with pytest.raises(ArtifactRetentionEventV1Error) as caught:
            coordinator.submit(manifest)
        assert caught.value.code == "TBM_RETENTION_RECOVERY_REQUIRED"
        current = repository.load_current(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        )
        assert current.bundle_id == manifest.successor_index_bundle_id
        assert provider.destroy_calls == 0

        recovered = coordinator.recover(manifest.operation_id)
        assert recovered.status == "tombstoned"
        assert provider.destroy_calls == 1


def test_manifest_rejects_forged_replay_markers_and_unbounded_collections():
    original = _bundle("memory_marker_forge", "memory_other")
    resolution = _resolution(original)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, _, _, _, _ = _coordinator(repository, resolution)
        manifest = coordinator.plan(_request(resolution))
        forged_marker = build_replay_partial_marker(
            replay_manifest_sha256=manifest.replay_partial_markers[
                0
            ].replay_manifest_sha256,
            missing_components=("injection_artifact",),
            erased_artifact_ids=("artifact_sha256_" + "a" * 64,),
            reason_code=manifest.request.reason_code,
            marked_at=manifest.planned_at,
        )
        unsigned = manifest.to_dict()
        unsigned.pop("manifest_sha256")
        unsigned["replay_partial_markers"] = [forged_marker.to_dict()]
        with pytest.raises(ArtifactRetentionEventV1Error) as marker_error:
            tbm.RedactionManifest(
                manifest_sha256=tbm.canonical_sha256(unsigned),
                request=manifest.request,
                resolution=manifest.resolution,
                retention_snapshot=manifest.retention_snapshot,
                expected_index_bundle_id=manifest.expected_index_bundle_id,
                successor_index_bundle_id=manifest.successor_index_bundle_id,
                replay_partial_markers=(forged_marker,),
                planned_at=manifest.planned_at,
            )
        assert marker_error.value.code == "TBM_RETENTION_REPLAY_MARKER_MISMATCH"

    target = resolution.targets[0]
    with pytest.raises(ArtifactRetentionEventV1Error):
        RetentionResolution(
            targets=(target,),
            key_references=tuple(
                KeyReferenceSet(
                    encryption_key_id=f"source_key_{index:03d}",
                    artifact_ids=(target.artifact.artifact_id,),
                )
                for index in range(tbm.ARTIFACT_RETENTION_MAX_TARGETS + 1)
            ),
        )
    with pytest.raises(ArtifactRetentionEventV1Error):
        build_retention_policy_snapshot(
            tuple(
                ArtifactRetentionDecision(
                    artifact_id="artifact_sha256_" + f"{index:064x}",
                    retention_policy_id="retention_raw_execution_v1",
                    retain_until=EXPIRED,
                    legal_hold=False,
                    hold_epoch=0,
                )
                for index in range(tbm.ARTIFACT_RETENTION_MAX_TARGETS + 1)
            ),
            evaluated_at=NOW,
        )


def test_policy_decision_must_match_target_artifact_retention_policy():
    original = _bundle("memory_policy_confusion", "memory_other")
    resolution = _resolution(original)
    guard = _WrongPolicyGuard(resolution)
    provider = _KeyProvider()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        coordinator, ledger, store, _, _ = _coordinator(
            repository,
            resolution,
            guard=guard,
            provider=provider,
        )

        with pytest.raises(ArtifactRetentionEventV1Error) as caught:
            coordinator.plan(_request(resolution))
        assert caught.value.code == "TBM_RETENTION_POLICY_MISMATCH"
        assert ledger.read_global(0, 1).events == ()
        assert store.put_calls == 0
        assert provider.destroy_calls == 0


def test_index_reintroduced_during_provider_call_is_repurged_before_tombstone():
    original = _bundle("memory_index_provider_race", "memory_other")
    resolution = _resolution(original)
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(original, expected_current_bundle_id=None)
        provider = _IndexReintroducingProvider(repository, original)
        coordinator, _, _, _, _ = _coordinator(
            repository,
            resolution,
            provider=provider,
        )
        manifest = coordinator.plan(_request(resolution))
        provider.expected_successor = manifest.successor_index_bundle_id

        submitted = coordinator.submit(manifest)

        assert submitted.status == "tombstoned"
        assert provider.destroy_calls == 1
        current = repository.load_current(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        )
        assert current.bundle_id == manifest.successor_index_bundle_id
        assert coordinator.recover(manifest.operation_id).status == "tombstoned"
        assert provider.destroy_calls == 1
        assert provider.reconcile_calls == 0
