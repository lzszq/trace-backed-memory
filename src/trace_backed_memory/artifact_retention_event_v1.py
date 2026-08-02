from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Literal, NoReturn, Protocol, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError, canonical_sha256
from .event_registry_v1 import (
    EventPayloadRegistration,
    EventRegistryV1Error,
    EventTypeRegistry,
)
from .event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    build_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerIdempotency,
    verify_ledger_append_receipt,
)
from .managed_index_v3 import (
    ManagedIndexBundle,
    ManagedIndexPublication,
    ManagedIndexRepository,
    purge_managed_index_revisions,
)
from .replay_v3 import REPLAY_COMPONENT_NAMES, ReplayComponentName


ARTIFACT_RETENTION_EVENT_PROTOCOL_VERSION = "tbm.artifact-retention-event.v1"
ARTIFACT_RETENTION_STREAM_TYPE = "artifact_retention"
ARTIFACT_RETENTION_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.local/schemas/"
    "artifact_retention_event_payload_registry_v1.schema.json"
)
ARTIFACT_RETENTION_MAX_TARGETS = 60
ARTIFACT_RETENTION_MAX_EVENTS = 64
ARTIFACT_RETENTION_MANIFEST_MAX_BYTES = 2 * 1024 * 1024

ARTIFACT_RETENTION_APPLIED = "tbm.artifact.retention_applied"
ARTIFACT_REDACTION_MANIFEST_RECORDED = (
    "tbm.artifact.redaction_manifest_recorded"
)
ARTIFACT_CRYPTO_ERASURE_REQUESTED = "tbm.artifact.crypto_erasure_requested"
ARTIFACT_CRYPTO_ERASURE_AUTHORIZED = "tbm.artifact.crypto_erasure_authorized"
ARTIFACT_CRYPTO_ERASURE_BLOCKED = "tbm.artifact.crypto_erasure_blocked"
ARTIFACT_CRYPTO_ERASURE_UNKNOWN = "tbm.artifact.crypto_erasure_unknown"
ARTIFACT_CRYPTO_ERASURE_REJECTED = "tbm.artifact.crypto_erasure_rejected"
ARTIFACT_INDEX_PURGED = "tbm.artifact.index_purged"
ARTIFACT_REPLAY_PARTIAL_MARKED = "tbm.artifact.replay_partial_marked"
ARTIFACT_CRYPTOGRAPHICALLY_ERASED = (
    "tbm.artifact.cryptographically_erased"
)
ARTIFACT_TOMBSTONED = "tbm.artifact.tombstoned"

ARTIFACT_RETENTION_EVENT_TYPES = (
    ARTIFACT_RETENTION_APPLIED,
    ARTIFACT_REDACTION_MANIFEST_RECORDED,
    ARTIFACT_CRYPTO_ERASURE_REQUESTED,
    ARTIFACT_CRYPTO_ERASURE_AUTHORIZED,
    ARTIFACT_CRYPTO_ERASURE_BLOCKED,
    ARTIFACT_CRYPTO_ERASURE_UNKNOWN,
    ARTIFACT_CRYPTO_ERASURE_REJECTED,
    ARTIFACT_INDEX_PURGED,
    ARTIFACT_REPLAY_PARTIAL_MARKED,
    ARTIFACT_CRYPTOGRAPHICALLY_ERASED,
    ARTIFACT_TOMBSTONED,
)

RetentionOperationStatus = Literal[
    "prepared",
    "blocked",
    "index_head_stale",
    "crypto_erasure_unknown",
    "crypto_erasure_rejected",
    "cryptographically_erased",
    "tombstoned",
]
KeyDestructionStatus = Literal[
    "destroyed",
    "already_destroyed",
    "unknown",
    "rejected",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-=]{0,255}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_ID_RE = re.compile(r"^managed_index_bundle_sha256_[0-9a-f]{64}$")
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_OPERATION_ID_RE = re.compile(r"^retention_operation_sha256_[0-9a-f]{64}$")
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_CLASSIFICATION_ORDER = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_MANIFEST_MEDIA_TYPE = "application/vnd.tbm.redaction-manifest+json"
_MANIFEST_RETENTION_POLICY = "retention_artifact_governance_v1"
_RECEIPT_MEDIA_TYPE = "application/provider-receipt+json"
_RETENTION_EVENT_PRODUCER_CAPABILITY = object()


class ArtifactRetentionEventV1Error(V3ContractError):
    """Stable storage-neutral retention/erasure contract failure."""


@dataclass(frozen=True)
class ReplayImpact:
    replay_manifest_sha256: str
    missing_components: tuple[ReplayComponentName, ...]
    source_completeness: Literal["complete", "legacy_partial"] = "complete"
    source_missing_components: tuple[ReplayComponentName, ...] = ()

    def __post_init__(self) -> None:
        _digest(self.replay_manifest_sha256, "replay_manifest_sha256")
        _replay_components(self.missing_components)
        if self.source_completeness != "complete" or self.source_missing_components:
            _fail(
                "TBM_RETENTION_REPLAY_SOURCE_PARTIAL",
                "runtime erasure markers require a previously complete replay manifest",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "replay_manifest_sha256": self.replay_manifest_sha256,
            "missing_components": list(self.missing_components),
            "source_completeness": self.source_completeness,
            "source_missing_components": list(self.source_missing_components),
        }


@dataclass(frozen=True)
class ReplayPartialMarker:
    marker_sha256: str
    replay_manifest_sha256: str
    missing_components: tuple[ReplayComponentName, ...]
    erased_artifact_ids: tuple[str, ...]
    reason_code: str
    marked_at: str

    def __post_init__(self) -> None:
        _digest(self.marker_sha256, "marker_sha256")
        _digest(self.replay_manifest_sha256, "replay_manifest_sha256")
        _replay_components(self.missing_components)
        _artifact_ids(self.erased_artifact_ids, "erased_artifact_ids")
        _identifier(self.reason_code, "reason_code")
        canonical_marked = _timestamp(self.marked_at, "marked_at")
        object.__setattr__(self, "marked_at", canonical_marked)
        if self.marker_sha256 != canonical_sha256(self._unsigned_dict()):
            _fail(
                "TBM_RETENTION_HASH_MISMATCH",
                "replay partial marker digest does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "replay_manifest_sha256": self.replay_manifest_sha256,
            "missing_components": list(self.missing_components),
            "erased_artifact_ids": list(self.erased_artifact_ids),
            "reason_code": self.reason_code,
            "marked_at": self.marked_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"marker_sha256": self.marker_sha256, **self._unsigned_dict()}


@dataclass(frozen=True)
class RedactionTarget:
    artifact: EventArtifactRef
    memory_revision_ids: tuple[str, ...]
    replay_impacts: tuple[ReplayImpact, ...]

    def __post_init__(self) -> None:
        if type(self.artifact) is not EventArtifactRef:
            _invalid("artifact must be exactly EventArtifactRef")
        if (
            self.artifact.availability != "available"
            or self.artifact.encryption_key_id is None
        ):
            _fail(
                "TBM_RETENTION_TARGET_INVALID",
                "crypto-erasure targets must be available encrypted artifacts",
            )
        _revision_ids(self.memory_revision_ids)
        if (
            type(self.replay_impacts) is not tuple
            or len(self.replay_impacts) > ARTIFACT_RETENTION_MAX_TARGETS
            or any(type(item) is not ReplayImpact for item in self.replay_impacts)
            or self.replay_impacts
            != tuple(
                sorted(
                    self.replay_impacts,
                    key=lambda item: item.replay_manifest_sha256,
                )
            )
            or len({item.replay_manifest_sha256 for item in self.replay_impacts})
            != len(self.replay_impacts)
        ):
            _invalid("replay_impacts must be a unique sorted tuple")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_dict(),
            "memory_revision_ids": list(self.memory_revision_ids),
            "replay_impacts": [item.to_dict() for item in self.replay_impacts],
        }


@dataclass(frozen=True)
class KeyReferenceSet:
    encryption_key_id: str
    artifact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.encryption_key_id, "encryption_key_id")
        _artifact_ids(self.artifact_ids, "artifact_ids")

    def to_dict(self) -> dict[str, object]:
        return {
            "encryption_key_id": self.encryption_key_id,
            "artifact_ids": list(self.artifact_ids),
        }


@dataclass(frozen=True)
class RetentionResolution:
    targets: tuple[RedactionTarget, ...]
    key_references: tuple[KeyReferenceSet, ...]

    def __post_init__(self) -> None:
        if (
            type(self.targets) is not tuple
            or not 1 <= len(self.targets) <= ARTIFACT_RETENTION_MAX_TARGETS
            or any(type(item) is not RedactionTarget for item in self.targets)
            or self.targets
            != tuple(sorted(self.targets, key=lambda item: item.artifact.artifact_id))
            or len({item.artifact.artifact_id for item in self.targets})
            != len(self.targets)
        ):
            _invalid("targets must be a unique sorted bounded tuple")
        if (
            type(self.key_references) is not tuple
            or len(self.key_references) > ARTIFACT_RETENTION_MAX_TARGETS
            or any(type(item) is not KeyReferenceSet for item in self.key_references)
            or self.key_references
            != tuple(
                sorted(self.key_references, key=lambda item: item.encryption_key_id)
            )
            or len({item.encryption_key_id for item in self.key_references})
            != len(self.key_references)
        ):
            _invalid("key_references must be a unique sorted bounded tuple")

    @property
    def resolution_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "targets": [item.to_dict() for item in self.targets],
            "key_references": [item.to_dict() for item in self.key_references],
        }


@dataclass(frozen=True)
class ArtifactRetentionDecision:
    artifact_id: str
    retention_policy_id: str
    retain_until: str | None
    legal_hold: bool
    hold_epoch: int

    def __post_init__(self) -> None:
        _artifact_id(self.artifact_id, "artifact_id")
        _identifier(self.retention_policy_id, "retention_policy_id")
        if self.retain_until is not None:
            object.__setattr__(
                self,
                "retain_until",
                _timestamp(self.retain_until, "retain_until"),
            )
        if type(self.legal_hold) is not bool:
            _invalid("legal_hold must be a boolean")
        if type(self.hold_epoch) is not int or not 0 <= self.hold_epoch <= _MAX_SEQUENCE:
            _invalid("hold_epoch must be a bounded non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "retention_policy_id": self.retention_policy_id,
            "retain_until": self.retain_until,
            "legal_hold": self.legal_hold,
            "hold_epoch": self.hold_epoch,
        }


@dataclass(frozen=True)
class RetentionPolicySnapshot:
    snapshot_sha256: str
    policy_state_sha256: str
    decisions: tuple[ArtifactRetentionDecision, ...]
    evaluated_at: str

    def __post_init__(self) -> None:
        _digest(self.snapshot_sha256, "snapshot_sha256")
        _digest(self.policy_state_sha256, "policy_state_sha256")
        if (
            type(self.decisions) is not tuple
            or not 1 <= len(self.decisions) <= ARTIFACT_RETENTION_MAX_TARGETS
            or any(type(item) is not ArtifactRetentionDecision for item in self.decisions)
            or self.decisions
            != tuple(sorted(self.decisions, key=lambda item: item.artifact_id))
            or len({item.artifact_id for item in self.decisions}) != len(self.decisions)
        ):
            _invalid("decisions must be a unique sorted bounded tuple")
        canonical_evaluated = _timestamp(self.evaluated_at, "evaluated_at")
        object.__setattr__(self, "evaluated_at", canonical_evaluated)
        if self.policy_state_sha256 != canonical_sha256(
            [item.to_dict() for item in self.decisions]
        ):
            _fail(
                "TBM_RETENTION_HASH_MISMATCH",
                "policy state digest does not match decisions",
            )
        if self.snapshot_sha256 != canonical_sha256(self._unsigned_dict()):
            _fail(
                "TBM_RETENTION_HASH_MISMATCH",
                "retention snapshot digest does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "policy_state_sha256": self.policy_state_sha256,
            "decisions": [item.to_dict() for item in self.decisions],
            "evaluated_at": self.evaluated_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"snapshot_sha256": self.snapshot_sha256, **self._unsigned_dict()}


@dataclass(frozen=True)
class RetentionRequest:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    authorization_event_id: str
    artifact_ids: tuple[str, ...]
    deletion_policy_id: str
    reason_code: str
    idempotency_key_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "authorization_event_id",
            "deletion_policy_id",
            "reason_code",
        ):
            _identifier(getattr(self, name), name)
        _artifact_ids(self.artifact_ids, "artifact_ids")
        _digest(self.idempotency_key_sha256, "idempotency_key_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "authorization_event_id": self.authorization_event_id,
            "artifact_ids": list(self.artifact_ids),
            "deletion_policy_id": self.deletion_policy_id,
            "reason_code": self.reason_code,
            "idempotency_key_sha256": self.idempotency_key_sha256,
        }


@dataclass(frozen=True)
class RedactionManifest:
    manifest_sha256: str
    request: RetentionRequest
    resolution: RetentionResolution
    retention_snapshot: RetentionPolicySnapshot
    expected_index_bundle_id: str
    successor_index_bundle_id: str
    replay_partial_markers: tuple[ReplayPartialMarker, ...]
    planned_at: str
    contract_version: str = ARTIFACT_RETENTION_EVENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != ARTIFACT_RETENTION_EVENT_PROTOCOL_VERSION:
            _invalid(
                "contract_version must be "
                f"{ARTIFACT_RETENTION_EVENT_PROTOCOL_VERSION}"
            )
        _digest(self.manifest_sha256, "manifest_sha256")
        if type(self.request) is not RetentionRequest:
            _invalid("request must be exactly RetentionRequest")
        if type(self.resolution) is not RetentionResolution:
            _invalid("resolution must be exactly RetentionResolution")
        if type(self.retention_snapshot) is not RetentionPolicySnapshot:
            _invalid("retention_snapshot must be exactly RetentionPolicySnapshot")
        for value, name in (
            (self.expected_index_bundle_id, "expected_index_bundle_id"),
            (self.successor_index_bundle_id, "successor_index_bundle_id"),
        ):
            if type(value) is not str or _BUNDLE_ID_RE.fullmatch(value) is None:
                _invalid(f"{name} must be a managed index bundle ID")
        if (
            type(self.replay_partial_markers) is not tuple
            or len(self.replay_partial_markers) > ARTIFACT_RETENTION_MAX_TARGETS
            or any(
                type(item) is not ReplayPartialMarker
                for item in self.replay_partial_markers
            )
            or self.replay_partial_markers
            != tuple(
                sorted(
                    self.replay_partial_markers,
                    key=lambda item: item.replay_manifest_sha256,
                )
            )
            or len(
                {item.replay_manifest_sha256 for item in self.replay_partial_markers}
            )
            != len(self.replay_partial_markers)
        ):
            _invalid("replay_partial_markers must be a unique sorted tuple")
        canonical_planned = _timestamp(self.planned_at, "planned_at")
        object.__setattr__(self, "planned_at", canonical_planned)
        if tuple(item.artifact.artifact_id for item in self.resolution.targets) != (
            self.request.artifact_ids
        ):
            _fail(
                "TBM_RETENTION_TARGET_MISMATCH",
                "resolved targets do not exactly match the requested artifacts",
            )
        if tuple(item.artifact_id for item in self.retention_snapshot.decisions) != (
            self.request.artifact_ids
        ):
            _fail(
                "TBM_RETENTION_POLICY_MISMATCH",
                "retention snapshot does not exactly cover requested artifacts",
            )
        if self.replay_partial_markers != _build_replay_partial_markers(
            self.resolution,
            reason_code=self.request.reason_code,
            marked_at=canonical_planned,
        ):
            _fail(
                "TBM_RETENTION_REPLAY_MARKER_MISMATCH",
                "replay partial markers do not match resolved replay impacts",
            )
        if self.manifest_sha256 != canonical_sha256(self._unsigned_dict()):
            _fail(
                "TBM_RETENTION_HASH_MISMATCH",
                "redaction manifest digest does not match canonical content",
            )

    @property
    def operation_id(self) -> str:
        identity = canonical_sha256(
            {
                "idempotency_key_sha256": self.request.idempotency_key_sha256,
                "scope": {
                    "organization_id": self.request.organization_id,
                    "tenant_id": self.request.tenant_id,
                    "repository_id": self.request.repository_id,
                    "environment_id": self.request.environment_id,
                },
                "artifact_ids": list(self.request.artifact_ids),
                "deletion_policy_id": self.request.deletion_policy_id,
                "reason_code": self.request.reason_code,
                "resolution_sha256": self.resolution.resolution_sha256,
                "policy_state_sha256": (
                    self.retention_snapshot.policy_state_sha256
                ),
            }
        )
        return "retention_operation_sha256_" + identity.removeprefix("sha256:")

    @property
    def memory_revision_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    revision_id
                    for target in self.resolution.targets
                    for revision_id in target.memory_revision_ids
                }
            )
        )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "request": self.request.to_dict(),
            "resolution": self.resolution.to_dict(),
            "retention_snapshot": self.retention_snapshot.to_dict(),
            "expected_index_bundle_id": self.expected_index_bundle_id,
            "successor_index_bundle_id": self.successor_index_bundle_id,
            "replay_partial_markers": [
                item.to_dict() for item in self.replay_partial_markers
            ],
            "planned_at": self.planned_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"manifest_sha256": self.manifest_sha256, **self._unsigned_dict()}


class RetentionTargetResolver(Protocol):
    def resolve(self, request: RetentionRequest) -> RetentionResolution: ...


class RetentionPolicyGuard(Protocol):
    def evaluate(
        self,
        resolution: RetentionResolution,
        *,
        evaluated_at: str,
    ) -> RetentionPolicySnapshot: ...

    def authorize_destruction(
        self,
        *,
        operation_id: str,
        resolution: RetentionResolution,
        expected_policy_state_sha256: str,
        authorized_at: str,
    ) -> RetentionDestructionAuthorization: ...


@dataclass(frozen=True)
class RetentionDestructionAuthorization:
    authorization_sha256: str
    operation_id: str
    policy_state_sha256: str
    hold_epoch_sha256: str
    authorized_at: str
    expires_at: str

    def __post_init__(self) -> None:
        _digest(self.authorization_sha256, "authorization_sha256")
        _operation_id(self.operation_id)
        _digest(self.policy_state_sha256, "policy_state_sha256")
        _digest(self.hold_epoch_sha256, "hold_epoch_sha256")
        object.__setattr__(
            self,
            "authorized_at",
            _timestamp(self.authorized_at, "authorized_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            _timestamp(self.expires_at, "expires_at"),
        )
        if parse_rfc3339(self.expires_at) <= parse_rfc3339(self.authorized_at):
            _invalid("destruction authorization must have a future expiry")
        if self.authorization_sha256 != canonical_sha256(self._unsigned_dict()):
            _fail(
                "TBM_RETENTION_HASH_MISMATCH",
                "destruction authorization digest does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "policy_state_sha256": self.policy_state_sha256,
            "hold_epoch_sha256": self.hold_epoch_sha256,
            "authorized_at": self.authorized_at,
            "expires_at": self.expires_at,
        }


class RedactionManifestStore(Protocol):
    def put(
        self,
        manifest: RedactionManifest,
        payload: bytes,
    ) -> EventArtifactRef: ...

    def load(self, descriptor: EventArtifactRef) -> bytes: ...


@dataclass(frozen=True)
class TrustedKeyDestructionProvider:
    provider_id: str
    provider_version: str
    registration_sha256: str
    attestation_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.provider_id, "provider_id")
        _identifier(self.provider_version, "provider_version")
        _digest(self.registration_sha256, "registration_sha256")
        _digest(self.attestation_sha256, "attestation_sha256")


@dataclass(frozen=True)
class KeyDestructionRequest:
    operation_id: str
    encryption_key_id: str
    provider_request_id: str
    provider_id: str
    provider_version: str
    provider_registration_sha256: str
    provider_attestation_sha256: str
    destruction_authorization_sha256: str
    request_sha256: str
    authorization_event_id: str
    requested_at: str

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _identifier(self.encryption_key_id, "encryption_key_id")
        _identifier(self.provider_request_id, "provider_request_id")
        _identifier(self.provider_id, "provider_id")
        _identifier(self.provider_version, "provider_version")
        _digest(self.provider_registration_sha256, "provider_registration_sha256")
        _digest(self.provider_attestation_sha256, "provider_attestation_sha256")
        _digest(
            self.destruction_authorization_sha256,
            "destruction_authorization_sha256",
        )
        _digest(self.request_sha256, "request_sha256")
        _identifier(self.authorization_event_id, "authorization_event_id")
        object.__setattr__(
            self,
            "requested_at",
            _timestamp(self.requested_at, "requested_at"),
        )
        if self.request_sha256 != canonical_sha256(self._unsigned_dict()):
            _fail(
                "TBM_RETENTION_HASH_MISMATCH",
                "key-destruction request digest does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "encryption_key_id": self.encryption_key_id,
            "provider_request_id": self.provider_request_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "provider_registration_sha256": self.provider_registration_sha256,
            "provider_attestation_sha256": self.provider_attestation_sha256,
            "destruction_authorization_sha256": (
                self.destruction_authorization_sha256
            ),
            "authorization_event_id": self.authorization_event_id,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True)
class KeyDestructionReceipt:
    provider_request_id: str
    request_sha256: str
    status: KeyDestructionStatus
    receipt_sha256: str | None
    receipt_artifact: EventArtifactRef | None
    completed_at: str | None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.provider_request_id, "provider_request_id")
        _digest(self.request_sha256, "request_sha256")
        if self.status not in {
            "destroyed",
            "already_destroyed",
            "unknown",
            "rejected",
        }:
            _invalid("key destruction status is not supported")
        terminal_success = self.status in {"destroyed", "already_destroyed"}
        if terminal_success:
            if (
                self.receipt_sha256 is None
                or self.receipt_artifact is None
                or self.completed_at is None
                or self.failure_code is not None
            ):
                _invalid("confirmed key destruction requires an exact receipt")
            _digest(self.receipt_sha256, "receipt_sha256")
            if (
                type(self.receipt_artifact) is not EventArtifactRef
                or self.receipt_artifact.content_sha256 != self.receipt_sha256
                or self.receipt_artifact.availability != "available"
                or self.receipt_artifact.classification
                not in {"confidential", "restricted"}
            ):
                _invalid("key destruction receipt Artifact is invalid")
            object.__setattr__(
                self,
                "completed_at",
                _timestamp(cast(str, self.completed_at), "completed_at"),
            )
        elif any(
            item is not None
            for item in (self.receipt_sha256, self.receipt_artifact, self.completed_at)
        ):
            _invalid("unconfirmed key destruction cannot carry a success receipt")
        if self.status == "rejected":
            _identifier(self.failure_code, "failure_code")
        elif self.failure_code is not None:
            _invalid("failure_code is only valid for rejected key destruction")


class KeyDestructionProvider(Protocol):
    """Trusted KMS adapter with a non-mutating reconciliation operation.

    ``destroy`` is the only operation allowed to request key destruction.
    ``reconcile`` must only query the exact provider request identity and may
    never create, retry, or otherwise initiate a destructive side effect.
    """

    @property
    def trusted_provider(self) -> TrustedKeyDestructionProvider: ...

    def destroy(self, request: KeyDestructionRequest) -> KeyDestructionReceipt: ...

    def reconcile(self, request: KeyDestructionRequest) -> KeyDestructionReceipt:
        """Read the exact request status without initiating destruction."""
        ...


class KeyDestructionReceiptVerifier(Protocol):
    def verify(
        self,
        request: KeyDestructionRequest,
        receipt: KeyDestructionReceipt,
    ) -> None: ...


@dataclass(frozen=True)
class RetentionEventDraft:
    event_type: str
    manifest: RedactionManifest
    manifest_artifact: EventArtifactRef
    occurred_at: str
    index_previous_bundle_id: str | None = None
    index_successor_bundle_id: str | None = None
    provider_request_ids: tuple[str, ...] = ()
    provider_request_sha256s: tuple[str, ...] = ()
    provider_id: str | None = None
    provider_version: str | None = None
    provider_registration_sha256: str | None = None
    provider_attestation_sha256: str | None = None
    destruction_authorization_sha256: str | None = None
    provider_receipt_request_ids: tuple[str, ...] = ()
    provider_receipt_request_sha256s: tuple[str, ...] = ()
    provider_receipt_sha256s: tuple[str, ...] = ()
    receipt_artifacts: tuple[EventArtifactRef, ...] = ()
    replay_marker_sha256s: tuple[str, ...] = ()
    unknown_provider_request_ids: tuple[str, ...] = ()
    failure_code: str | None = None
    producer_capability: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.event_type not in ARTIFACT_RETENTION_EVENT_TYPES:
            _invalid("event_type is not an artifact retention event")
        if self.producer_capability is not _RETENTION_EVENT_PRODUCER_CAPABILITY:
            _fail(
                "TBM_RETENTION_PRODUCER_UNTRUSTED",
                "retention event drafts are coordinator-internal",
            )
        if type(self.manifest) is not RedactionManifest:
            _invalid("manifest must be exactly RedactionManifest")
        _manifest_artifact(self.manifest_artifact, self.manifest)
        object.__setattr__(
            self,
            "occurred_at",
            _timestamp(self.occurred_at, "occurred_at"),
        )
        for value, name in (
            (self.index_previous_bundle_id, "index_previous_bundle_id"),
            (self.index_successor_bundle_id, "index_successor_bundle_id"),
        ):
            if value is not None and (
                type(value) is not str or _BUNDLE_ID_RE.fullmatch(value) is None
            ):
                _invalid(f"{name} is invalid")
        _identifiers(self.provider_request_ids, "provider_request_ids")
        _digests(self.provider_request_sha256s, "provider_request_sha256s")
        if len(self.provider_request_ids) != len(self.provider_request_sha256s):
            _invalid("provider request IDs and digests must have equal length")
        for value, name in (
            (self.provider_id, "provider_id"),
            (self.provider_version, "provider_version"),
        ):
            if value is not None:
                _identifier(value, name)
        for value, name in (
            (self.provider_registration_sha256, "provider_registration_sha256"),
            (self.provider_attestation_sha256, "provider_attestation_sha256"),
            (
                self.destruction_authorization_sha256,
                "destruction_authorization_sha256",
            ),
        ):
            if value is not None:
                _digest(value, name)
        provider_metadata = (
            self.provider_id,
            self.provider_version,
            self.provider_registration_sha256,
            self.provider_attestation_sha256,
            self.destruction_authorization_sha256,
        )
        if any(item is not None for item in provider_metadata) != all(
            item is not None for item in provider_metadata
        ):
            _invalid("provider authorization metadata must be all present or all absent")
        _identifiers(
            self.provider_receipt_request_ids,
            "provider_receipt_request_ids",
        )
        _digests(
            self.provider_receipt_request_sha256s,
            "provider_receipt_request_sha256s",
        )
        _digests(self.provider_receipt_sha256s, "provider_receipt_sha256s")
        if not (
            len(self.provider_receipt_request_ids)
            == len(self.provider_receipt_request_sha256s)
            == len(self.provider_receipt_sha256s)
        ):
            _invalid("provider receipt bindings must have equal length")
        if (
            type(self.receipt_artifacts) is not tuple
            or any(type(item) is not EventArtifactRef for item in self.receipt_artifacts)
            or any(
                item.availability != "available"
                or item.classification not in {"confidential", "restricted"}
                for item in self.receipt_artifacts
            )
            or tuple(item.content_sha256 for item in self.receipt_artifacts)
            != self.provider_receipt_sha256s
        ):
            _invalid("receipt_artifacts do not match provider receipt digests")
        _digests(self.replay_marker_sha256s, "replay_marker_sha256s")
        _identifiers(
            self.unknown_provider_request_ids,
            "unknown_provider_request_ids",
        )
        if self.failure_code is not None:
            _identifier(self.failure_code, "failure_code")

    def payload(self, *, sequence: int) -> dict[str, object]:
        return {
            "operation_id": self.manifest.operation_id,
            "sequence": sequence,
            "manifest_sha256": self.manifest.manifest_sha256,
            "manifest_artifact_id": self.manifest_artifact.artifact_id,
            "point": self.event_type.removeprefix("tbm.artifact."),
            "occurred_at": self.occurred_at,
            "retention_snapshot_sha256": (
                self.manifest.retention_snapshot.snapshot_sha256
            ),
            "index_previous_bundle_id": self.index_previous_bundle_id,
            "index_successor_bundle_id": self.index_successor_bundle_id,
            "provider_request_ids": list(self.provider_request_ids),
            "provider_request_sha256s": list(self.provider_request_sha256s),
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "provider_registration_sha256": self.provider_registration_sha256,
            "provider_attestation_sha256": self.provider_attestation_sha256,
            "destruction_authorization_sha256": (
                self.destruction_authorization_sha256
            ),
            "provider_receipt_request_ids": list(
                self.provider_receipt_request_ids
            ),
            "provider_receipt_request_sha256s": list(
                self.provider_receipt_request_sha256s
            ),
            "provider_receipt_sha256s": list(self.provider_receipt_sha256s),
            "replay_marker_sha256s": list(self.replay_marker_sha256s),
            "unknown_provider_request_ids": list(
                self.unknown_provider_request_ids
            ),
            "failure_code": self.failure_code,
            "artifact_ids": list(self.manifest.request.artifact_ids),
        }


def _retention_draft(
    event_type: str,
    manifest: RedactionManifest,
    manifest_artifact: EventArtifactRef,
    occurred_at: str,
    index_previous_bundle_id: str | None = None,
    index_successor_bundle_id: str | None = None,
    provider_request_ids: tuple[str, ...] = (),
    provider_request_sha256s: tuple[str, ...] = (),
    provider_id: str | None = None,
    provider_version: str | None = None,
    provider_registration_sha256: str | None = None,
    provider_attestation_sha256: str | None = None,
    destruction_authorization_sha256: str | None = None,
    provider_receipt_request_ids: tuple[str, ...] = (),
    provider_receipt_request_sha256s: tuple[str, ...] = (),
    provider_receipt_sha256s: tuple[str, ...] = (),
    receipt_artifacts: tuple[EventArtifactRef, ...] = (),
    replay_marker_sha256s: tuple[str, ...] = (),
    unknown_provider_request_ids: tuple[str, ...] = (),
    failure_code: str | None = None,
) -> RetentionEventDraft:
    return RetentionEventDraft(
        event_type=event_type,
        manifest=manifest,
        manifest_artifact=manifest_artifact,
        occurred_at=occurred_at,
        index_previous_bundle_id=index_previous_bundle_id,
        index_successor_bundle_id=index_successor_bundle_id,
        provider_request_ids=provider_request_ids,
        provider_request_sha256s=provider_request_sha256s,
        provider_id=provider_id,
        provider_version=provider_version,
        provider_registration_sha256=provider_registration_sha256,
        provider_attestation_sha256=provider_attestation_sha256,
        destruction_authorization_sha256=destruction_authorization_sha256,
        provider_receipt_request_ids=provider_receipt_request_ids,
        provider_receipt_request_sha256s=provider_receipt_request_sha256s,
        provider_receipt_sha256s=provider_receipt_sha256s,
        receipt_artifacts=receipt_artifacts,
        replay_marker_sha256s=replay_marker_sha256s,
        unknown_provider_request_ids=unknown_provider_request_ids,
        failure_code=failure_code,
        producer_capability=_RETENTION_EVENT_PRODUCER_CAPABILITY,
    )


@dataclass(frozen=True)
class RetentionProjection:
    operation_id: str
    manifest_sha256: str
    manifest_artifact_id: str
    current_sequence: int
    status: RetentionOperationStatus
    index_previous_bundle_id: str | None
    index_successor_bundle_id: str | None
    provider_request_ids: tuple[str, ...]
    provider_request_sha256s: tuple[str, ...]
    provider_id: str | None
    provider_version: str | None
    provider_registration_sha256: str | None
    provider_attestation_sha256: str | None
    destruction_authorization_sha256: str | None
    provider_receipt_sha256s: tuple[str, ...]
    replay_marker_sha256s: tuple[str, ...]
    terminal_event_sha256: str | None


@dataclass(frozen=True)
class RetentionErasureResult:
    operation_id: str
    status: RetentionOperationStatus
    projection: RetentionProjection
    index_publication: ManagedIndexPublication | None
    replayed: bool


def build_retention_policy_snapshot(
    decisions: tuple[ArtifactRetentionDecision, ...],
    *,
    evaluated_at: str,
) -> RetentionPolicySnapshot:
    canonical_decisions = tuple(sorted(decisions, key=lambda item: item.artifact_id))
    policy_state_sha256 = canonical_sha256(
        [item.to_dict() for item in canonical_decisions]
    )
    unsigned = {
        "policy_state_sha256": policy_state_sha256,
        "decisions": [item.to_dict() for item in canonical_decisions],
        "evaluated_at": _timestamp(evaluated_at, "evaluated_at"),
    }
    return RetentionPolicySnapshot(
        snapshot_sha256=canonical_sha256(unsigned),
        policy_state_sha256=policy_state_sha256,
        decisions=canonical_decisions,
        evaluated_at=cast(str, unsigned["evaluated_at"]),
    )


def build_retention_destruction_authorization(
    *,
    operation_id: str,
    resolution: RetentionResolution,
    policy_state_sha256: str,
    authorized_at: str,
    expires_at: str,
) -> RetentionDestructionAuthorization:
    _operation_id(operation_id)
    if type(resolution) is not RetentionResolution:
        _invalid("resolution must be exactly RetentionResolution")
    _digest(policy_state_sha256, "policy_state_sha256")
    canonical_authorized = _timestamp(authorized_at, "authorized_at")
    canonical_expires = _timestamp(expires_at, "expires_at")
    hold_epoch_sha256 = _hold_epoch_sha256(resolution, policy_state_sha256)
    unsigned = {
        "operation_id": operation_id,
        "policy_state_sha256": policy_state_sha256,
        "hold_epoch_sha256": hold_epoch_sha256,
        "authorized_at": canonical_authorized,
        "expires_at": canonical_expires,
    }
    return RetentionDestructionAuthorization(
        authorization_sha256=canonical_sha256(unsigned),
        operation_id=operation_id,
        policy_state_sha256=policy_state_sha256,
        hold_epoch_sha256=hold_epoch_sha256,
        authorized_at=canonical_authorized,
        expires_at=canonical_expires,
    )


def build_replay_partial_marker(
    *,
    replay_manifest_sha256: str,
    missing_components: tuple[ReplayComponentName, ...],
    erased_artifact_ids: tuple[str, ...],
    reason_code: str,
    marked_at: str,
) -> ReplayPartialMarker:
    unsigned = {
        "replay_manifest_sha256": replay_manifest_sha256,
        "missing_components": list(missing_components),
        "erased_artifact_ids": list(erased_artifact_ids),
        "reason_code": reason_code,
        "marked_at": _timestamp(marked_at, "marked_at"),
    }
    return ReplayPartialMarker(
        marker_sha256=canonical_sha256(unsigned),
        replay_manifest_sha256=replay_manifest_sha256,
        missing_components=missing_components,
        erased_artifact_ids=erased_artifact_ids,
        reason_code=reason_code,
        marked_at=cast(str, unsigned["marked_at"]),
    )


def _hold_epoch_sha256(
    resolution: RetentionResolution,
    policy_state_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "policy_state_sha256": policy_state_sha256,
            "artifact_ids": [
                item.artifact.artifact_id for item in resolution.targets
            ],
        }
    )


def _build_replay_partial_markers(
    resolution: RetentionResolution,
    *,
    reason_code: str,
    marked_at: str,
) -> tuple[ReplayPartialMarker, ...]:
    impacts: dict[str, tuple[set[ReplayComponentName], set[str]]] = {}
    for target in resolution.targets:
        for impact in target.replay_impacts:
            components, artifact_ids = impacts.setdefault(
                impact.replay_manifest_sha256,
                (set(), set()),
            )
            components.update(impact.missing_components)
            artifact_ids.add(target.artifact.artifact_id)
    return tuple(
        build_replay_partial_marker(
            replay_manifest_sha256=manifest_sha256,
            missing_components=tuple(
                name for name in REPLAY_COMPONENT_NAMES if name in components
            ),
            erased_artifact_ids=tuple(sorted(artifact_ids)),
            reason_code=reason_code,
            marked_at=marked_at,
        )
        for manifest_sha256, (components, artifact_ids) in sorted(impacts.items())
    )


def build_redaction_manifest(
    request: RetentionRequest,
    *,
    resolution: RetentionResolution,
    retention_snapshot: RetentionPolicySnapshot,
    current_index: ManagedIndexBundle,
    planned_at: str,
) -> RedactionManifest:
    if type(request) is not RetentionRequest:
        _invalid("request must be exactly RetentionRequest")
    if type(resolution) is not RetentionResolution:
        _invalid("resolution must be exactly RetentionResolution")
    if type(retention_snapshot) is not RetentionPolicySnapshot:
        _invalid("retention_snapshot must be exactly RetentionPolicySnapshot")
    if type(current_index) is not ManagedIndexBundle:
        _invalid("current_index must be exactly ManagedIndexBundle")
    if (
        current_index.tenant_id != request.tenant_id
        or current_index.repository_id != request.repository_id
        or current_index.environment_id != request.environment_id
    ):
        _fail(
            "TBM_RETENTION_INDEX_SCOPE_MISMATCH",
            "current managed index is outside the retention request scope",
        )
    revision_ids = tuple(
        sorted(
            {
                revision_id
                for target in resolution.targets
                for revision_id in target.memory_revision_ids
            }
        )
    )
    successor = (
        current_index
        if not revision_ids
        else purge_managed_index_revisions(
            current_index,
            memory_revision_ids=revision_ids,
        )
    )
    canonical_planned_at = _timestamp(planned_at, "planned_at")
    markers = _build_replay_partial_markers(
        resolution,
        reason_code=request.reason_code,
        marked_at=canonical_planned_at,
    )
    unsigned = {
        "contract_version": ARTIFACT_RETENTION_EVENT_PROTOCOL_VERSION,
        "request": request.to_dict(),
        "resolution": resolution.to_dict(),
        "retention_snapshot": retention_snapshot.to_dict(),
        "expected_index_bundle_id": current_index.bundle_id,
        "successor_index_bundle_id": successor.bundle_id,
        "replay_partial_markers": [item.to_dict() for item in markers],
        "planned_at": canonical_planned_at,
    }
    return RedactionManifest(
        manifest_sha256=canonical_sha256(unsigned),
        request=request,
        resolution=resolution,
        retention_snapshot=retention_snapshot,
        expected_index_bundle_id=current_index.bundle_id,
        successor_index_bundle_id=successor.bundle_id,
        replay_partial_markers=markers,
        planned_at=canonical_planned_at,
    )


def dumps_redaction_manifest(manifest: RedactionManifest) -> bytes:
    if type(manifest) is not RedactionManifest:
        _invalid("manifest must be exactly RedactionManifest")
    payload = json.dumps(
        manifest.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > ARTIFACT_RETENTION_MANIFEST_MAX_BYTES:
        _fail(
            "TBM_RETENTION_MANIFEST_TOO_LARGE",
            "redaction manifest exceeds the supported byte bound",
        )
    return payload


def loads_redaction_manifest(source: str | bytes | bytearray) -> RedactionManifest:
    try:
        encoded = source.encode("utf-8") if type(source) is str else bytes(source)
    except (TypeError, ValueError, UnicodeError) as error:
        raise ArtifactRetentionEventV1Error(
            "TBM_RETENTION_MANIFEST_INVALID",
            "redaction manifest JSON is invalid",
        ) from error
    if len(encoded) > ARTIFACT_RETENTION_MANIFEST_MAX_BYTES:
        _fail(
            "TBM_RETENTION_MANIFEST_TOO_LARGE",
            "redaction manifest exceeds the supported byte bound",
        )
    try:
        text = decode_bounded_utf8(
            encoded,
            max_bytes=ARTIFACT_RETENTION_MANIFEST_MAX_BYTES,
            description="redaction manifest",
        )
        value = parse_bounded_json(
            text,
            description="redaction manifest",
            max_nodes=20_000,
            max_depth=24,
        )
    except ArtifactRetentionEventV1Error:
        raise
    except (TypeError, ValueError, UnicodeError) as error:
        raise ArtifactRetentionEventV1Error(
            "TBM_RETENTION_MANIFEST_INVALID",
            "redaction manifest JSON is invalid",
        ) from error
    if type(value) is not dict:
        _invalid("redaction manifest must be a JSON object")
    return _manifest_from_dict(cast(dict[str, object], value))


def build_key_destruction_requests(
    manifest: RedactionManifest,
    provider: TrustedKeyDestructionProvider,
    authorization: RetentionDestructionAuthorization,
) -> tuple[KeyDestructionRequest, ...]:
    if type(manifest) is not RedactionManifest:
        _invalid("manifest must be exactly RedactionManifest")
    if type(provider) is not TrustedKeyDestructionProvider:
        _invalid("provider must be exactly TrustedKeyDestructionProvider")
    if (
        type(authorization) is not RetentionDestructionAuthorization
        or authorization.operation_id != manifest.operation_id
        or authorization.policy_state_sha256
        != manifest.retention_snapshot.policy_state_sha256
    ):
        _fail(
            "TBM_RETENTION_DESTRUCTION_AUTHORIZATION_INVALID",
            "destruction authorization does not match the manifest",
        )
    return _build_key_destruction_requests(
        manifest,
        provider,
        authorization.authorization_sha256,
    )


def _build_key_destruction_requests(
    manifest: RedactionManifest,
    provider: TrustedKeyDestructionProvider,
    destruction_authorization_sha256: str,
) -> tuple[KeyDestructionRequest, ...]:
    _digest(
        destruction_authorization_sha256,
        "destruction_authorization_sha256",
    )
    requests: list[KeyDestructionRequest] = []
    for key_reference in manifest.resolution.key_references:
        request_identity = canonical_sha256(
            {
                "operation_id": manifest.operation_id,
                "encryption_key_id": key_reference.encryption_key_id,
                "provider_id": provider.provider_id,
                "provider_version": provider.provider_version,
                "provider_registration_sha256": provider.registration_sha256,
                "provider_attestation_sha256": provider.attestation_sha256,
                "destruction_authorization_sha256": (
                    destruction_authorization_sha256
                ),
            }
        ).removeprefix("sha256:")
        provider_request_id = "key_destroy_" + request_identity
        unsigned = {
            "operation_id": manifest.operation_id,
            "encryption_key_id": key_reference.encryption_key_id,
            "provider_request_id": provider_request_id,
            "provider_id": provider.provider_id,
            "provider_version": provider.provider_version,
            "provider_registration_sha256": provider.registration_sha256,
            "provider_attestation_sha256": provider.attestation_sha256,
            "destruction_authorization_sha256": destruction_authorization_sha256,
            "authorization_event_id": manifest.request.authorization_event_id,
            "requested_at": manifest.planned_at,
        }
        requests.append(
            KeyDestructionRequest(
                operation_id=manifest.operation_id,
                encryption_key_id=key_reference.encryption_key_id,
                provider_request_id=provider_request_id,
                provider_id=provider.provider_id,
                provider_version=provider.provider_version,
                provider_registration_sha256=provider.registration_sha256,
                provider_attestation_sha256=provider.attestation_sha256,
                destruction_authorization_sha256=destruction_authorization_sha256,
                request_sha256=canonical_sha256(unsigned),
                authorization_event_id=manifest.request.authorization_event_id,
                requested_at=manifest.planned_at,
            )
        )
    return tuple(requests)


def artifact_retention_stream_id(operation_id: str) -> str:
    _operation_id(operation_id)
    return "artifact_retention_" + operation_id.removeprefix(
        "retention_operation_"
    )


def build_artifact_retention_event_batch(
    drafts: tuple[RetentionEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    prior_stream_events: tuple[CanonicalEvent, ...],
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= EVENT_LEDGER_MAX_APPEND_BATCH
        or any(type(item) is not RetentionEventDraft for item in drafts)
    ):
        _invalid("drafts must be a bounded non-empty RetentionEventDraft tuple")
    if type(access) is not LedgerAccessContext:
        _invalid("access must be exactly LedgerAccessContext")
    if (
        type(expected_stream_version) is not int
        or not 0 <= expected_stream_version < ARTIFACT_RETENTION_MAX_EVENTS
        or expected_stream_version + len(drafts) > ARTIFACT_RETENTION_MAX_EVENTS
    ):
        _invalid("expected stream version is outside the retention event bound")
    if type(next_global_position) is not int or not (
        1 <= next_global_position <= _MAX_SEQUENCE
    ):
        _invalid("next global position is invalid")
    _event_tuple(prior_stream_events, "prior_stream_events")
    if len(prior_stream_events) != expected_stream_version:
        _fail(
            "TBM_RETENTION_HISTORY_INCOMPLETE",
            "retention history must be complete through the expected head",
        )
    manifest = drafts[0].manifest
    if any(item.manifest != manifest for item in drafts):
        _fail(
            "TBM_RETENTION_MANIFEST_DRIFT",
            "one append batch must preserve the exact redaction manifest",
        )
    if any(item.manifest_artifact != drafts[0].manifest_artifact for item in drafts):
        _fail(
            "TBM_RETENTION_MANIFEST_DRIFT",
            "one append batch must preserve the manifest Artifact descriptor",
        )
    request = manifest.request
    partition = access.partition
    if (
        partition.organization_id != request.organization_id
        or partition.tenant_id != request.tenant_id
        or partition.repository_id != request.repository_id
        or partition.environment_id != request.environment_id
        or access.authorization_decision_id != request.authorization_event_id
    ):
        _fail(
            "TBM_RETENTION_ACCESS_DENIED",
            "ledger access is outside the manifest scope or authorization",
        )
    stream_id = artifact_retention_stream_id(manifest.operation_id)
    previous = None
    for offset, event in enumerate(prior_stream_events, start=1):
        verify_artifact_retention_event(event)
        if event.stream_id != stream_id or event.stream_version != offset:
            _fail(
                "TBM_RETENTION_HISTORY_INCOMPLETE",
                "prior events are not the complete retention stream",
            )
        if previous is not None:
            _verify_parent(event, previous)
        previous = event
    canonical_recorded_at = _timestamp(recorded_at, "recorded_at")
    payloads = [
        draft.payload(sequence=expected_stream_version + offset + 1)
        for offset, draft in enumerate(drafts)
    ]
    command_sha256 = canonical_sha256(
        {
            "operation_id": manifest.operation_id,
            "expected_stream_version": expected_stream_version,
            "payloads": payloads,
        }
    )
    idempotency_key_sha256 = canonical_sha256(
        {
            "manifest_idempotency_key_sha256": (
                request.idempotency_key_sha256
            ),
            "expected_stream_version": expected_stream_version,
            "command_sha256": command_sha256,
        }
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    events: list[CanonicalEvent] = []
    parent = previous
    for offset, (draft, payload) in enumerate(zip(drafts, payloads, strict=True)):
        sequence = expected_stream_version + offset + 1
        identity_sha256 = canonical_sha256(
            {
                "operation_id": manifest.operation_id,
                "sequence": sequence,
                "event_type": draft.event_type,
                "command_sha256": command_sha256,
            }
        ).removeprefix("sha256:")
        artifact_refs = _draft_artifact_refs(draft)
        classification = cast(
            EventClassification,
            max(
                (item.classification for item in artifact_refs),
                key=lambda item: _CLASSIFICATION_ORDER[item],
            ),
        )
        event = build_canonical_event(
            event_id="evt_retention_" + identity_sha256,
            event_type=draft.event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=ARTIFACT_RETENTION_STREAM_TYPE,
            stream_version=sequence,
            global_position=next_global_position + offset,
            trusted_context=access.event_trusted_context(),
            request_id="retention_request_" + command_sha256.removeprefix("sha256:"),
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id=manifest.operation_id,
            causation_id=None if parent is None else parent.event_id,
            occurred_at=draft.occurred_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_artifact_retention_adapter",
            producer_version="f3-v1",
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification=classification,
            retention_policy_id=request.deletion_policy_id,
            artifact_refs=artifact_refs,
            payload=payload,
        )
        verify_artifact_retention_event(event)
        if parent is not None:
            _verify_parent(event, parent)
        events.append(event)
        parent = event
    reduce_artifact_retention_events(
        (*prior_stream_events, *events),
        manifest=manifest,
    )
    return tuple(events), idempotency


def append_artifact_retention_event_batch(
    ledger: EventLedgerPort,
    drafts: tuple[RetentionEventDraft, ...],
    *,
    recorded_at: str,
) -> LedgerAppendReceipt:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global")
    ):
        _fail(
            "TBM_RETENTION_LEDGER_INVALID",
            "append requires an access-bound readable EventLedgerPort",
        )
    if not drafts or type(drafts[0]) is not RetentionEventDraft:
        _invalid("drafts are invalid")
    stream_id = artifact_retention_stream_id(drafts[0].manifest.operation_id)
    page = ledger.read_stream(stream_id, 1, ARTIFACT_RETENTION_MAX_EVENTS)
    if page.has_more:
        _fail(
            "TBM_RETENTION_HISTORY_INCOMPLETE",
            "retention history exceeds the supported event bound",
        )
    global_page = ledger.read_global(0, 1)
    next_global_position = global_page.high_watermark_global_position + 1
    events, idempotency = build_artifact_retention_event_batch(
        drafts,
        access=access,
        expected_stream_version=len(page.events),
        next_global_position=next_global_position,
        prior_stream_events=page.events,
        recorded_at=recorded_at,
    )
    receipt = ledger.append(stream_id, len(page.events), events, idempotency)
    from .ledger_port_v1 import LedgerAppendRequest

    request = LedgerAppendRequest(
        access=access,
        stream_id=stream_id,
        expected_stream_version=len(page.events),
        events=events,
        idempotency=idempotency,
    )
    verify_ledger_append_receipt(request, receipt)
    return receipt


def verify_artifact_retention_event(event: CanonicalEvent) -> None:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if (
        event.event_type not in ARTIFACT_RETENTION_EVENT_TYPES
        or event.event_version != 1
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != ARTIFACT_RETENTION_STREAM_TYPE
        or event.occurred_at is None
        or event.producer != "tbm_artifact_retention_adapter"
        or event.producer_version != "f3-v1"
        or event.payload_schema != _PAYLOAD_SCHEMAS.get(event.event_type)
    ):
        _fail(
            "TBM_RETENTION_EVENT_INVALID",
            "event is not a native artifact retention event v1",
        )
    try:
        payload = build_artifact_retention_event_registry().consume(event).payload
    except EventRegistryV1Error as error:
        raise ArtifactRetentionEventV1Error(
            "TBM_RETENTION_PAYLOAD_INVALID",
            "retention payload does not match its sealed event type",
        ) from error
    operation_id = cast(str, payload["operation_id"])
    manifest_artifact_id = cast(str, payload["manifest_artifact_id"])
    artifact_ids = tuple(cast(tuple[str, ...], payload["artifact_ids"]))
    if (
        event.stream_id != artifact_retention_stream_id(operation_id)
        or payload["sequence"] != event.stream_version
        or payload["occurred_at"] != event.occurred_at
        or payload["point"] != event.event_type.removeprefix("tbm.artifact.")
        or event.correlation_id != operation_id
        or event.authorization_decision_id is None
    ):
        _fail(
            "TBM_RETENTION_EVENT_INVALID",
            "retention envelope, stream, and payload are not exactly bound",
        )
    refs = {item.artifact_id: item for item in event.artifact_refs}
    manifest_ref = refs.get(manifest_artifact_id)
    if manifest_ref is None or manifest_ref.availability != "available":
        _fail(
            "TBM_RETENTION_ARTIFACT_MISMATCH",
            "retention event lacks its available protected manifest descriptor",
        )
    target_refs = tuple(refs.get(item) for item in artifact_ids)
    if event.event_type in {
        ARTIFACT_RETENTION_APPLIED,
        ARTIFACT_REDACTION_MANIFEST_RECORDED,
    } and any(item is None or item.availability != "available" for item in target_refs):
        _fail(
            "TBM_RETENTION_ARTIFACT_MISMATCH",
            "pre-erasure events must retain available target descriptors",
        )
    if event.event_type in {
        ARTIFACT_REPLAY_PARTIAL_MARKED,
        ARTIFACT_CRYPTOGRAPHICALLY_ERASED,
        ARTIFACT_TOMBSTONED,
    } and any(item is None or item.availability != "erased" for item in target_refs):
        _fail(
            "TBM_RETENTION_ARTIFACT_MISMATCH",
            "post-erasure events must retain erased target descriptors",
        )


def reduce_artifact_retention_events(
    events: tuple[CanonicalEvent, ...],
    *,
    manifest: RedactionManifest | None = None,
) -> RetentionProjection:
    _event_tuple(events, "events")
    if not events:
        _fail("TBM_RETENTION_NOT_FOUND", "retention projection is absent")
    if len(events) > ARTIFACT_RETENTION_MAX_EVENTS:
        _fail("TBM_RETENTION_REDUCER_BOUNDS", "retention history is too large")
    previous = None
    payloads: list[Mapping[str, object]] = []
    for sequence, event in enumerate(events, start=1):
        verify_artifact_retention_event(event)
        if event.stream_version != sequence:
            _fail(
                "TBM_RETENTION_TRANSITION_INVALID",
                "retention stream versions are not contiguous",
            )
        if previous is not None:
            _verify_parent(event, previous)
            if event.recorded_at < previous.recorded_at:
                _fail(
                    "TBM_RETENTION_TRANSITION_INVALID",
                    "retention recorded time is not monotonic",
                )
        payloads.append(cast(Mapping[str, object], event.payload))
        previous = event
    operation_id = cast(str, payloads[0]["operation_id"])
    manifest_sha256 = cast(str, payloads[0]["manifest_sha256"])
    manifest_artifact_id = cast(str, payloads[0]["manifest_artifact_id"])
    for event, payload in zip(events, payloads, strict=True):
        if (
            payload["operation_id"] != operation_id
            or payload["manifest_sha256"] != manifest_sha256
            or payload["manifest_artifact_id"] != manifest_artifact_id
            or event.stream_id != events[0].stream_id
        ):
            _fail(
                "TBM_RETENTION_MANIFEST_DRIFT",
                "retention stream changed operation or manifest identity",
            )
    if manifest is not None:
        if type(manifest) is not RedactionManifest:
            _invalid("manifest must be exactly RedactionManifest or null")
        expected_manifest_artifact_id = "artifact_sha256_" + hashlib.sha256(
            dumps_redaction_manifest(manifest)
        ).hexdigest()
        manifest_descriptors = tuple(
            next(
                (
                    descriptor
                    for descriptor in event.artifact_refs
                    if descriptor.artifact_id == manifest_artifact_id
                ),
                None,
            )
            for event in events
        )
        if any(item is None for item in manifest_descriptors):
            _fail(
                "TBM_RETENTION_MANIFEST_DRIFT",
                "retention stream lacks its exact manifest descriptor",
            )
        exact_manifest_descriptor = cast(EventArtifactRef, manifest_descriptors[0])
        _manifest_artifact(exact_manifest_descriptor, manifest)
        for event, payload in zip(events, payloads, strict=True):
            if (
                operation_id != manifest.operation_id
                or manifest_sha256 != manifest.manifest_sha256
                or manifest_artifact_id != expected_manifest_artifact_id
                or tuple(cast(tuple[str, ...], payload["artifact_ids"]))
                != manifest.request.artifact_ids
                or payload["retention_snapshot_sha256"]
                != manifest.retention_snapshot.snapshot_sha256
                or event.authorization_decision_id
                != manifest.request.authorization_event_id
                or event.retention_policy_id != manifest.request.deletion_policy_id
                or next(
                    descriptor
                    for descriptor in event.artifact_refs
                    if descriptor.artifact_id == manifest_artifact_id
                )
                != exact_manifest_descriptor
            ):
                _fail(
                    "TBM_RETENTION_MANIFEST_DRIFT",
                    "retention event does not exactly bind the stored manifest",
                )
            _verify_manifest_bound_artifact_refs(
                event,
                payload,
                manifest,
                exact_manifest_descriptor,
            )
    required_prefix = (
        ARTIFACT_RETENTION_APPLIED,
        ARTIFACT_REDACTION_MANIFEST_RECORDED,
        ARTIFACT_CRYPTO_ERASURE_REQUESTED,
    )
    if tuple(item.event_type for item in events[:3]) != required_prefix:
        _fail(
            "TBM_RETENTION_TRANSITION_INVALID",
            "retention stream must begin with applied, manifest, and erasure intent",
        )
    if any(
        payloads[2][name]
        for name in (
            "provider_request_ids",
            "provider_request_sha256s",
            "provider_receipt_request_ids",
            "provider_receipt_request_sha256s",
            "provider_receipt_sha256s",
        )
    ):
        _fail(
            "TBM_RETENTION_TRANSITION_INVALID",
            "crypto-erasure intent must precede provider authorization",
        )
    request_ids: tuple[str, ...] = ()
    request_sha256s: tuple[str, ...] = ()
    provider_id = None
    provider_version = None
    provider_registration_sha256 = None
    provider_attestation_sha256 = None
    destruction_authorization_sha256 = None
    status: RetentionOperationStatus = "prepared"
    previous_bundle_id = None
    successor_bundle_id = None
    receipt_sha256s: tuple[str, ...] = ()
    marker_sha256s: tuple[str, ...] = ()
    seen_index = False
    seen_authorized = False
    seen_unknown = False
    seen_replay = False
    seen_erased = False
    terminal_sha256 = None
    for event, payload in zip(events[3:], payloads[3:], strict=True):
        event_type = event.event_type
        if status in {"blocked", "crypto_erasure_rejected", "tombstoned"}:
            _fail(
                "TBM_RETENTION_TERMINAL_IMMUTABLE",
                "terminal retention state cannot accept later events",
            )
        if event_type == ARTIFACT_INDEX_PURGED:
            if seen_index or seen_authorized:
                _transition_invalid("index purge can be recorded only once")
            previous_bundle_id = cast(str, payload["index_previous_bundle_id"])
            successor_bundle_id = cast(str, payload["index_successor_bundle_id"])
            if previous_bundle_id is None or successor_bundle_id is None:
                _transition_invalid("index purge must bind both index heads")
            if manifest is not None and (
                previous_bundle_id != manifest.expected_index_bundle_id
                or successor_bundle_id != manifest.successor_index_bundle_id
            ):
                _transition_invalid("index purge does not match the manifest plan")
            seen_index = True
        elif event_type == ARTIFACT_CRYPTO_ERASURE_AUTHORIZED:
            if not seen_index or seen_authorized:
                _transition_invalid("provider authorization transition is invalid")
            request_ids = tuple(
                cast(tuple[str, ...], payload["provider_request_ids"])
            )
            request_sha256s = tuple(
                cast(tuple[str, ...], payload["provider_request_sha256s"])
            )
            provider_id = cast(str | None, payload["provider_id"])
            provider_version = cast(str | None, payload["provider_version"])
            provider_registration_sha256 = cast(
                str | None, payload["provider_registration_sha256"]
            )
            provider_attestation_sha256 = cast(
                str | None, payload["provider_attestation_sha256"]
            )
            destruction_authorization_sha256 = cast(
                str | None, payload["destruction_authorization_sha256"]
            )
            if (
                not request_ids
                or len(request_ids) != len(request_sha256s)
                or any(
                    item is None
                    for item in (
                        provider_id,
                        provider_version,
                        provider_registration_sha256,
                        provider_attestation_sha256,
                        destruction_authorization_sha256,
                    )
                )
            ):
                _transition_invalid("provider authorization is incomplete")
            seen_authorized = True
        elif event_type == ARTIFACT_CRYPTO_ERASURE_BLOCKED:
            if not seen_index or seen_authorized or payload["failure_code"] is None:
                _transition_invalid("blocked erasure requires index purge and a code")
            status = "blocked"
            terminal_sha256 = event.event_sha256
        elif event_type == ARTIFACT_CRYPTO_ERASURE_REJECTED:
            if not seen_authorized or payload["failure_code"] is None:
                _transition_invalid("rejected erasure requires index purge and a code")
            status = "crypto_erasure_rejected"
            terminal_sha256 = event.event_sha256
        elif event_type == ARTIFACT_CRYPTO_ERASURE_UNKNOWN:
            unknown = tuple(
                cast(tuple[str, ...], payload["unknown_provider_request_ids"])
            )
            receipt_request_ids = tuple(
                cast(tuple[str, ...], payload["provider_receipt_request_ids"])
            )
            receipt_request_sha256s = tuple(
                cast(
                    tuple[str, ...],
                    payload["provider_receipt_request_sha256s"],
                )
            )
            if not seen_authorized or seen_unknown or not unknown:
                _transition_invalid("unknown erasure transition is invalid")
            if (
                not set(unknown).issubset(request_ids)
                or set(unknown) & set(receipt_request_ids)
                or set(unknown) | set(receipt_request_ids) != set(request_ids)
                or tuple(
                    request_sha256s[request_ids.index(item)]
                    for item in receipt_request_ids
                )
                != receipt_request_sha256s
            ):
                _transition_invalid("unknown provider requests were not authorized")
            seen_unknown = True
            status = "crypto_erasure_unknown"
        elif event_type == ARTIFACT_REPLAY_PARTIAL_MARKED:
            markers = tuple(cast(tuple[str, ...], payload["replay_marker_sha256s"]))
            if not seen_authorized or seen_replay or not markers:
                _transition_invalid("replay partial marker transition is invalid")
            if manifest is not None and markers != tuple(
                item.marker_sha256 for item in manifest.replay_partial_markers
            ):
                _transition_invalid("replay partial markers do not match the manifest")
            seen_replay = True
            marker_sha256s = markers
        elif event_type == ARTIFACT_CRYPTOGRAPHICALLY_ERASED:
            receipts = tuple(
                cast(tuple[str, ...], payload["provider_receipt_sha256s"])
            )
            receipt_request_ids = tuple(
                cast(tuple[str, ...], payload["provider_receipt_request_ids"])
            )
            receipt_request_sha256s = tuple(
                cast(
                    tuple[str, ...],
                    payload["provider_receipt_request_sha256s"],
                )
            )
            if (
                not seen_authorized
                or seen_erased
                or receipt_request_ids != request_ids
                or receipt_request_sha256s != request_sha256s
                or len(receipts) != len(request_ids)
            ):
                _transition_invalid("cryptographic erasure receipts are incomplete")
            if manifest is not None and bool(manifest.replay_partial_markers) != seen_replay:
                _transition_invalid("replay partial evidence is incomplete")
            seen_erased = True
            receipt_sha256s = receipts
            status = "cryptographically_erased"
        elif event_type == ARTIFACT_TOMBSTONED:
            if not seen_erased:
                _transition_invalid("tombstone requires confirmed cryptographic erasure")
            if tuple(
                cast(tuple[str, ...], payload["provider_receipt_request_ids"])
            ) != request_ids or tuple(
                cast(tuple[str, ...], payload["provider_receipt_request_sha256s"])
            ) != request_sha256s:
                _transition_invalid("tombstone receipt bindings are incomplete")
            if tuple(
                cast(tuple[str, ...], payload["provider_receipt_sha256s"])
            ) != receipt_sha256s:
                _transition_invalid("tombstone receipts changed after erasure")
            if manifest is not None and tuple(
                cast(tuple[str, ...], payload["replay_marker_sha256s"])
            ) != tuple(
                item.marker_sha256 for item in manifest.replay_partial_markers
            ):
                _transition_invalid("tombstone replay markers are incomplete")
            status = "tombstoned"
            terminal_sha256 = event.event_sha256
        else:
            _transition_invalid("retention event is out of lifecycle order")
    return RetentionProjection(
        operation_id=operation_id,
        manifest_sha256=manifest_sha256,
        manifest_artifact_id=manifest_artifact_id,
        current_sequence=len(events),
        status=status,
        index_previous_bundle_id=previous_bundle_id,
        index_successor_bundle_id=successor_bundle_id,
        provider_request_ids=request_ids,
        provider_request_sha256s=request_sha256s,
        provider_id=provider_id,
        provider_version=provider_version,
        provider_registration_sha256=provider_registration_sha256,
        provider_attestation_sha256=provider_attestation_sha256,
        destruction_authorization_sha256=destruction_authorization_sha256,
        provider_receipt_sha256s=receipt_sha256s,
        replay_marker_sha256s=marker_sha256s,
        terminal_event_sha256=terminal_sha256,
    )


def build_artifact_retention_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    for event_type in ARTIFACT_RETENTION_EVENT_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="domain",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=_retention_payload_schema(event_type),
            )
        )
    return registry.seal()


def artifact_retention_payload_dispatch_schema() -> dict[str, object]:
    schema = build_artifact_retention_event_registry().dispatch_schema()
    schema["$id"] = ARTIFACT_RETENTION_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory artifact retention event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed artifact-retention registry. Lifecycle, "
        "legal-hold, index-CAS, provider-receipt, and Artifact checks remain "
        "authoritative in runtime code."
    )
    return schema


def dumps_artifact_retention_payload_dispatch_schema() -> str:
    return json.dumps(
        artifact_retention_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def replay_partial_marker_for_manifest(
    markers: tuple[ReplayPartialMarker, ...],
    replay_manifest_sha256: str,
) -> ReplayPartialMarker | None:
    _digest(replay_manifest_sha256, "replay_manifest_sha256")
    if type(markers) is not tuple or any(
        type(item) is not ReplayPartialMarker for item in markers
    ):
        _invalid("markers must contain ReplayPartialMarker values")
    for marker in markers:
        if marker.replay_manifest_sha256 == replay_manifest_sha256:
            return marker
    return None


def require_replay_not_erased(
    markers: tuple[ReplayPartialMarker, ...],
    replay_manifest_sha256: str,
) -> None:
    marker = replay_partial_marker_for_manifest(markers, replay_manifest_sha256)
    if marker is not None:
        _fail(
            "TBM_RETENTION_REPLAY_PARTIAL",
            "replay bytes were erased; exact replay is unavailable",
        )


class RetentionErasureCoordinator:
    """Event-first retention, index-purge, and crypto-erasure coordinator.

    Local authorities remain immutable.  The coordinator records intent before
    index/KMS effects, selects a successor managed-index head with CAS, and
    models external key destruction as confirmed, rejected, or unknown.  An
    unknown provider result is never retried blindly; ``recover`` only calls
    the provider's reconciliation operation.
    """

    def __init__(
        self,
        *,
        ledger: EventLedgerPort,
        managed_index: ManagedIndexRepository,
        manifest_store: RedactionManifestStore,
        target_resolver: RetentionTargetResolver,
        policy_guard: RetentionPolicyGuard,
        key_destruction_provider: KeyDestructionProvider,
        receipt_verifier: KeyDestructionReceiptVerifier,
        clock: Callable[[], str],
    ) -> None:
        access = getattr(ledger, "access_context", None)
        if type(access) is not LedgerAccessContext or not all(
            callable(getattr(ledger, name, None))
            for name in ("append", "read_stream", "read_global")
        ):
            _invalid("ledger must be an access-bound EventLedgerPort")
        if not all(
            callable(getattr(managed_index, name, None))
            for name in ("publish", "load", "load_current")
        ):
            _invalid("managed_index must implement ManagedIndexRepository")
        if not all(
            callable(getattr(manifest_store, name, None))
            for name in ("put", "load")
        ):
            _invalid("manifest_store must implement RedactionManifestStore")
        if not callable(getattr(target_resolver, "resolve", None)):
            _invalid("target_resolver must implement RetentionTargetResolver")
        if not all(
            callable(getattr(policy_guard, name, None))
            for name in ("evaluate", "authorize_destruction")
        ):
            _invalid("policy_guard must implement RetentionPolicyGuard")
        if not all(
            callable(getattr(key_destruction_provider, name, None))
            for name in ("destroy", "reconcile")
        ) or type(
            getattr(key_destruction_provider, "trusted_provider", None)
        ) is not TrustedKeyDestructionProvider:
            _invalid("key_destruction_provider is invalid")
        if not callable(getattr(receipt_verifier, "verify", None)):
            _invalid("receipt_verifier must implement KeyDestructionReceiptVerifier")
        if not callable(clock):
            _invalid("clock must be callable")
        self._ledger = ledger
        self._managed_index = managed_index
        self._manifest_store = manifest_store
        self._target_resolver = target_resolver
        self._policy_guard = policy_guard
        self._key_destruction_provider = key_destruction_provider
        self._receipt_verifier = receipt_verifier
        self._clock = clock

    def plan(self, request: RetentionRequest) -> RedactionManifest:
        if type(request) is not RetentionRequest:
            _invalid("request must be exactly RetentionRequest")
        self._verify_request_access(request)
        now = self._trusted_now()
        resolution, snapshot = self._fresh_state(request, now=now)
        _verify_key_reference_closure(resolution)
        _verify_erasure_eligible(snapshot, now=now)
        try:
            current_index = self._managed_index.load_current(
                tenant_id=request.tenant_id,
                repository_id=request.repository_id,
                environment_id=request.environment_id,
            )
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_INDEX_UNAVAILABLE",
                "current managed index is unavailable",
            ) from error
        return build_redaction_manifest(
            request,
            resolution=resolution,
            retention_snapshot=snapshot,
            current_index=current_index,
            planned_at=now,
        )

    def submit(self, manifest: RedactionManifest) -> RetentionErasureResult:
        if type(manifest) is not RedactionManifest:
            _invalid("manifest must be exactly RedactionManifest")
        self._verify_request_access(manifest.request)
        now = self._trusted_now()
        resolution, snapshot = self._fresh_state(manifest.request, now=now)
        self._verify_fresh_manifest_state(manifest, resolution, snapshot, now=now)
        stream_events = self._read_stream(manifest.operation_id)
        replayed = bool(stream_events)
        if stream_events:
            projection = reduce_artifact_retention_events(
                stream_events,
                manifest=manifest,
            )
            if projection.manifest_sha256 != manifest.manifest_sha256:
                _fail(
                    "TBM_RETENTION_MANIFEST_DRIFT",
                    "operation is already bound to another redaction manifest",
                )
            manifest_artifact = self._manifest_descriptor(stream_events)
            self._verify_stored_manifest(manifest, manifest_artifact)
        else:
            manifest_artifact = self._store_manifest(manifest)
            first_drafts = (
                _retention_draft(
                    ARTIFACT_RETENTION_APPLIED,
                    manifest,
                    manifest_artifact,
                    now,
                ),
                _retention_draft(
                    ARTIFACT_REDACTION_MANIFEST_RECORDED,
                    manifest,
                    manifest_artifact,
                    now,
                ),
                _retention_draft(
                    ARTIFACT_CRYPTO_ERASURE_REQUESTED,
                    manifest,
                    manifest_artifact,
                    now,
                ),
            )
            self._append_event_batch(
                first_drafts,
                recorded_at=now,
            )
            projection = self._projection(manifest.operation_id, manifest)
        return self._continue(
            manifest,
            manifest_artifact,
            projection,
            replayed=replayed,
            reconcile_only=(
                projection.destruction_authorization_sha256 is not None
            ),
        )

    def recover(self, operation_id: str) -> RetentionErasureResult:
        _operation_id(operation_id)
        stream_events = self._read_stream(operation_id)
        if not stream_events:
            _fail("TBM_RETENTION_NOT_FOUND", "retention operation is absent")
        projection = reduce_artifact_retention_events(stream_events)
        manifest_artifact = self._manifest_descriptor(stream_events)
        manifest = loads_redaction_manifest(
            self._load_manifest_bytes(manifest_artifact)
        )
        if manifest.operation_id != operation_id:
            _fail(
                "TBM_RETENTION_MANIFEST_DRIFT",
                "stored manifest does not match the retention operation",
            )
        self._verify_request_access(manifest.request)
        self._verify_stored_manifest(manifest, manifest_artifact)
        projection = reduce_artifact_retention_events(
            stream_events,
            manifest=manifest,
        )
        return self._continue(
            manifest,
            manifest_artifact,
            projection,
            replayed=True,
            reconcile_only=(
                projection.destruction_authorization_sha256 is not None
            ),
        )

    def _continue(
        self,
        manifest: RedactionManifest,
        manifest_artifact: EventArtifactRef,
        projection: RetentionProjection,
        *,
        replayed: bool,
        reconcile_only: bool,
    ) -> RetentionErasureResult:
        if projection.status in {
            "blocked",
            "crypto_erasure_rejected",
            "tombstoned",
        }:
            return RetentionErasureResult(
                manifest.operation_id,
                projection.status,
                projection,
                None,
                True,
            )
        publication: ManagedIndexPublication | None = None
        if projection.index_successor_bundle_id is None:
            publication, stale = self._publish_index_successor(manifest)
            if stale:
                return RetentionErasureResult(
                    manifest.operation_id,
                    "index_head_stale",
                    projection,
                    None,
                    replayed,
                )
            now = self._trusted_now()
            self._append_index_outcome(
                (
                    _retention_draft(
                        ARTIFACT_INDEX_PURGED,
                        manifest,
                        manifest_artifact,
                        now,
                        index_previous_bundle_id=(
                            manifest.expected_index_bundle_id
                        ),
                        index_successor_bundle_id=(
                            manifest.successor_index_bundle_id
                        ),
                    ),
                ),
                recorded_at=now,
            )
            projection = self._projection(manifest.operation_id, manifest)

        if not self._index_head_is_successor(manifest):
            repair_publication, stale = self._publish_index_successor(manifest)
            if repair_publication is not None:
                publication = repair_publication
            if stale or not self._index_head_is_successor(manifest):
                return RetentionErasureResult(
                    manifest.operation_id,
                    "index_head_stale",
                    projection,
                    publication,
                    replayed,
                )

        now = self._trusted_now()
        if projection.destruction_authorization_sha256 is None:
            try:
                resolution, snapshot = self._fresh_state(manifest.request, now=now)
                self._verify_fresh_manifest_state(
                    manifest,
                    resolution,
                    snapshot,
                    now=now,
                )
                authorization = self._authorize_destruction(
                    manifest,
                    resolution,
                    now=now,
                )
            except ArtifactRetentionEventV1Error as error:
                self._append_event_batch(
                    (
                        _retention_draft(
                            ARTIFACT_CRYPTO_ERASURE_BLOCKED,
                            manifest,
                            manifest_artifact,
                            now,
                            failure_code=error.code,
                        ),
                    ),
                    recorded_at=now,
                )
                projection = self._projection(manifest.operation_id, manifest)
                return RetentionErasureResult(
                    manifest.operation_id,
                    "blocked",
                    projection,
                    publication,
                    replayed,
                )
            requests = build_key_destruction_requests(
                manifest,
                self._key_destruction_provider.trusted_provider,
                authorization,
            )
            provider = self._key_destruction_provider.trusted_provider
            self._append_event_batch(
                (
                    _retention_draft(
                        ARTIFACT_CRYPTO_ERASURE_AUTHORIZED,
                        manifest,
                        manifest_artifact,
                        now,
                        provider_request_ids=tuple(
                            item.provider_request_id for item in requests
                        ),
                        provider_request_sha256s=tuple(
                            item.request_sha256 for item in requests
                        ),
                        provider_id=provider.provider_id,
                        provider_version=provider.provider_version,
                        provider_registration_sha256=provider.registration_sha256,
                        provider_attestation_sha256=provider.attestation_sha256,
                        destruction_authorization_sha256=(
                            authorization.authorization_sha256
                        ),
                    ),
                ),
                recorded_at=now,
            )
            projection = self._projection(manifest.operation_id, manifest)
            reconcile_only = False
        else:
            requests = _build_key_destruction_requests(
                manifest,
                self._key_destruction_provider.trusted_provider,
                projection.destruction_authorization_sha256,
            )
        provider = self._key_destruction_provider.trusted_provider
        if (
            tuple(item.provider_request_id for item in requests)
            != projection.provider_request_ids
            or tuple(item.request_sha256 for item in requests)
            != projection.provider_request_sha256s
            or projection.provider_id != provider.provider_id
            or projection.provider_version != provider.provider_version
            or projection.provider_registration_sha256
            != provider.registration_sha256
            or projection.provider_attestation_sha256
            != provider.attestation_sha256
        ):
            _fail(
                "TBM_RETENTION_PROVIDER_DRIFT",
                "trusted key-destruction provider changed after authorization",
            )
        receipts = tuple(
            self._invoke_provider(
                request,
                manifest=manifest,
                reconcile_only=reconcile_only,
            )
            for request in requests
        )
        rejected = tuple(item for item in receipts if item.status == "rejected")
        confirmed = tuple(
            item
            for item in receipts
            if item.status in {"destroyed", "already_destroyed"}
        )
        if (
            rejected
            and len(rejected) == len(receipts)
            and projection.status != "crypto_erasure_unknown"
        ):
            rejected_at = self._trusted_now()
            self._append_provider_outcome(
                (
                    _retention_draft(
                        ARTIFACT_CRYPTO_ERASURE_REJECTED,
                        manifest,
                        manifest_artifact,
                        rejected_at,
                        failure_code="TBM_RETENTION_PROVIDER_REJECTED",
                    ),
                ),
                recorded_at=rejected_at,
            )
            projection = self._projection(manifest.operation_id, manifest)
            return RetentionErasureResult(
                manifest.operation_id,
                "crypto_erasure_rejected",
                projection,
                publication,
                replayed,
            )
        unresolved = tuple(
            item.provider_request_id
            for item in receipts
            if item.status in {"unknown", "rejected"}
        )
        if unresolved:
            if projection.status != "crypto_erasure_unknown":
                confirmed_pairs = tuple(
                    (request, receipt)
                    for request, receipt in zip(requests, receipts, strict=True)
                    if receipt.status in {"destroyed", "already_destroyed"}
                )
                unknown_at = self._trusted_now()
                self._append_provider_outcome(
                    (
                        _retention_draft(
                            ARTIFACT_CRYPTO_ERASURE_UNKNOWN,
                            manifest,
                            manifest_artifact,
                            unknown_at,
                            provider_receipt_request_ids=tuple(
                                request.provider_request_id
                                for request, _ in confirmed_pairs
                            ),
                            provider_receipt_request_sha256s=tuple(
                                request.request_sha256
                                for request, _ in confirmed_pairs
                            ),
                            provider_receipt_sha256s=tuple(
                                cast(str, receipt.receipt_sha256)
                                for _, receipt in confirmed_pairs
                            ),
                            receipt_artifacts=tuple(
                                cast(EventArtifactRef, receipt.receipt_artifact)
                                for _, receipt in confirmed_pairs
                            ),
                            unknown_provider_request_ids=unresolved,
                        ),
                    ),
                    recorded_at=unknown_at,
                )
            projection = self._projection(manifest.operation_id, manifest)
            return RetentionErasureResult(
                manifest.operation_id,
                "crypto_erasure_unknown",
                projection,
                publication,
                replayed,
            )

        confirmed_receipts = confirmed
        if len(confirmed_receipts) != len(requests):
            _fail(
                "TBM_RETENTION_PROVIDER_INVALID",
                "provider did not return one terminal receipt per request",
            )
        finalized_at = self._trusted_now()
        late_failure_code = self._late_hold_evidence(manifest, now=finalized_at)
        final_drafts: list[RetentionEventDraft] = []
        marker_sha256s = tuple(
            item.marker_sha256 for item in manifest.replay_partial_markers
        )
        if marker_sha256s:
            final_drafts.append(
                _retention_draft(
                    ARTIFACT_REPLAY_PARTIAL_MARKED,
                    manifest,
                    manifest_artifact,
                    finalized_at,
                    replay_marker_sha256s=marker_sha256s,
                )
            )
        receipt_sha256s = tuple(
            cast(str, item.receipt_sha256) for item in confirmed_receipts
        )
        receipt_artifacts = tuple(
            cast(EventArtifactRef, item.receipt_artifact)
            for item in confirmed_receipts
        )
        final_drafts.extend(
            (
                _retention_draft(
                    ARTIFACT_CRYPTOGRAPHICALLY_ERASED,
                    manifest,
                    manifest_artifact,
                    finalized_at,
                    provider_receipt_request_ids=tuple(
                        item.provider_request_id for item in requests
                    ),
                    provider_receipt_request_sha256s=tuple(
                        item.request_sha256 for item in requests
                    ),
                    provider_receipt_sha256s=receipt_sha256s,
                    receipt_artifacts=receipt_artifacts,
                    failure_code=late_failure_code,
                ),
                _retention_draft(
                    ARTIFACT_TOMBSTONED,
                    manifest,
                    manifest_artifact,
                    finalized_at,
                    provider_receipt_request_ids=tuple(
                        item.provider_request_id for item in requests
                    ),
                    provider_receipt_request_sha256s=tuple(
                        item.request_sha256 for item in requests
                    ),
                    provider_receipt_sha256s=receipt_sha256s,
                    receipt_artifacts=receipt_artifacts,
                    replay_marker_sha256s=marker_sha256s,
                    failure_code=late_failure_code,
                ),
            )
        )
        if not self._index_head_is_successor(manifest):
            repair_publication, stale = self._publish_index_successor(manifest)
            if repair_publication is not None:
                publication = repair_publication
            if stale or not self._index_head_is_successor(manifest):
                raise ArtifactRetentionEventV1Error(
                    "TBM_RETENTION_RECOVERY_REQUIRED",
                    "managed-index head changed after external key destruction",
                )
        self._append_provider_outcome(
            tuple(final_drafts),
            recorded_at=finalized_at,
        )
        projection = self._projection(manifest.operation_id, manifest)
        return RetentionErasureResult(
            manifest.operation_id,
            "tombstoned",
            projection,
            publication,
            replayed,
        )

    def _verify_request_access(self, request: RetentionRequest) -> None:
        access = cast(LedgerAccessContext, self._ledger.access_context)
        partition = access.partition
        if (
            partition.organization_id != request.organization_id
            or partition.tenant_id != request.tenant_id
            or partition.repository_id != request.repository_id
            or partition.environment_id != request.environment_id
            or access.authorization_decision_id != request.authorization_event_id
        ):
            _fail(
                "TBM_RETENTION_ACCESS_DENIED",
                "trusted adapter scope or authorization does not cover the request",
            )

    def _append_provider_outcome(
        self,
        drafts: tuple[RetentionEventDraft, ...],
        *,
        recorded_at: str,
    ) -> LedgerAppendReceipt:
        try:
            return append_artifact_retention_event_batch(
                self._ledger,
                drafts,
                recorded_at=recorded_at,
            )
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_RECOVERY_REQUIRED",
                "external key-destruction outcome requires durable recovery",
            ) from error

    def _append_index_outcome(
        self,
        drafts: tuple[RetentionEventDraft, ...],
        *,
        recorded_at: str,
    ) -> LedgerAppendReceipt:
        try:
            return append_artifact_retention_event_batch(
                self._ledger,
                drafts,
                recorded_at=recorded_at,
            )
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_RECOVERY_REQUIRED",
                "managed-index publication outcome requires durable recovery",
            ) from error

    def _append_event_batch(
        self,
        drafts: tuple[RetentionEventDraft, ...],
        *,
        recorded_at: str,
    ) -> LedgerAppendReceipt:
        try:
            return append_artifact_retention_event_batch(
                self._ledger,
                drafts,
                recorded_at=recorded_at,
            )
        except ArtifactRetentionEventV1Error:
            raise
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_EVENT_PERSIST_FAILED",
                "artifact retention event batch could not be persisted",
            ) from error

    def _fresh_state(
        self,
        request: RetentionRequest,
        *,
        now: str,
    ) -> tuple[RetentionResolution, RetentionPolicySnapshot]:
        try:
            resolution = self._target_resolver.resolve(request)
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_TARGET_RESOLUTION_FAILED",
                "retention targets could not be resolved",
            ) from error
        if type(resolution) is not RetentionResolution:
            _fail(
                "TBM_RETENTION_TARGET_RESOLUTION_FAILED",
                "target resolver returned an invalid resolution",
            )
        if tuple(item.artifact.artifact_id for item in resolution.targets) != (
            request.artifact_ids
        ):
            _fail(
                "TBM_RETENTION_TARGET_MISMATCH",
                "target resolver did not return the exact requested artifacts",
            )
        _verify_key_reference_closure(resolution)
        try:
            snapshot = self._policy_guard.evaluate(resolution, evaluated_at=now)
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_POLICY_EVALUATION_FAILED",
                "retention policy could not be evaluated",
            ) from error
        if type(snapshot) is not RetentionPolicySnapshot:
            _fail(
                "TBM_RETENTION_POLICY_EVALUATION_FAILED",
                "policy guard returned an invalid snapshot",
            )
        target_policies = {
            item.artifact.artifact_id: item.artifact.retention_policy_id
            for item in resolution.targets
        }
        if any(
            target_policies.get(decision.artifact_id)
            != decision.retention_policy_id
            for decision in snapshot.decisions
        ):
            _fail(
                "TBM_RETENTION_POLICY_MISMATCH",
                "retention decisions do not match target Artifact policies",
            )
        return resolution, snapshot

    def _verify_fresh_manifest_state(
        self,
        manifest: RedactionManifest,
        resolution: RetentionResolution,
        snapshot: RetentionPolicySnapshot,
        *,
        now: str,
    ) -> None:
        if resolution != manifest.resolution:
            _fail(
                "TBM_RETENTION_TARGET_DRIFT",
                "artifact, Memory, replay, or key-reference closure changed",
            )
        if (
            snapshot.policy_state_sha256
            != manifest.retention_snapshot.policy_state_sha256
        ):
            _fail(
                "TBM_RETENTION_POLICY_DRIFT",
                "retention policy or legal-hold epoch changed after planning",
            )
        _verify_erasure_eligible(snapshot, now=now)

    def _authorize_destruction(
        self,
        manifest: RedactionManifest,
        resolution: RetentionResolution,
        *,
        now: str,
    ) -> RetentionDestructionAuthorization:
        try:
            authorization = self._policy_guard.authorize_destruction(
                operation_id=manifest.operation_id,
                resolution=resolution,
                expected_policy_state_sha256=(
                    manifest.retention_snapshot.policy_state_sha256
                ),
                authorized_at=now,
            )
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_DESTRUCTION_AUTHORIZATION_FAILED",
                "legal-hold guard did not authorize key destruction",
            ) from error
        if (
            type(authorization) is not RetentionDestructionAuthorization
            or authorization.operation_id != manifest.operation_id
            or authorization.policy_state_sha256
            != manifest.retention_snapshot.policy_state_sha256
            or authorization.hold_epoch_sha256
            != _hold_epoch_sha256(
                resolution,
                manifest.retention_snapshot.policy_state_sha256,
            )
            or parse_rfc3339(authorization.authorized_at) > parse_rfc3339(now)
            or parse_rfc3339(authorization.expires_at) <= parse_rfc3339(now)
        ):
            _fail(
                "TBM_RETENTION_DESTRUCTION_AUTHORIZATION_INVALID",
                "legal-hold guard returned an invalid or expired authorization",
            )
        return authorization

    def _publish_index_successor(
        self,
        manifest: RedactionManifest,
    ) -> tuple[ManagedIndexPublication | None, bool]:
        try:
            current = self._managed_index.load_current(
                tenant_id=manifest.request.tenant_id,
                repository_id=manifest.request.repository_id,
                environment_id=manifest.request.environment_id,
            )
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_INDEX_UNAVAILABLE",
                "current managed index is unavailable",
            ) from error
        if current.bundle_id == manifest.successor_index_bundle_id:
            return None, False
        if current.bundle_id != manifest.expected_index_bundle_id:
            return None, True
        successor = (
            current
            if not manifest.memory_revision_ids
            else purge_managed_index_revisions(
                current,
                memory_revision_ids=manifest.memory_revision_ids,
            )
        )
        if successor.bundle_id != manifest.successor_index_bundle_id:
            _fail(
                "TBM_RETENTION_INDEX_PLAN_MISMATCH",
                "managed index successor no longer matches the redaction manifest",
            )
        try:
            publication = self._managed_index.publish(
                successor,
                expected_current_bundle_id=manifest.expected_index_bundle_id,
            )
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_INDEX_PERSIST_FAILED",
                "managed index successor could not be published",
            ) from error
        if publication.bundle != successor:
            _fail(
                "TBM_RETENTION_INDEX_PERSIST_FAILED",
                "managed index publication read-back is inconsistent",
            )
        return publication, False

    def _index_head_is_successor(self, manifest: RedactionManifest) -> bool:
        try:
            current = self._managed_index.load_current(
                tenant_id=manifest.request.tenant_id,
                repository_id=manifest.request.repository_id,
                environment_id=manifest.request.environment_id,
            )
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_INDEX_UNAVAILABLE",
                "current managed index is unavailable",
            ) from error
        return current.bundle_id == manifest.successor_index_bundle_id

    def _invoke_provider(
        self,
        request: KeyDestructionRequest,
        *,
        manifest: RedactionManifest,
        reconcile_only: bool,
    ) -> KeyDestructionReceipt:
        try:
            receipt = (
                self._key_destruction_provider.reconcile(request)
                if reconcile_only
                else self._key_destruction_provider.destroy(request)
            )
        except Exception:
            return KeyDestructionReceipt(
                provider_request_id=request.provider_request_id,
                request_sha256=request.request_sha256,
                status="unknown",
                receipt_sha256=None,
                receipt_artifact=None,
                completed_at=None,
            )
        if type(receipt) is not KeyDestructionReceipt:
            _fail(
                "TBM_RETENTION_PROVIDER_INVALID",
                "key-destruction provider returned an invalid receipt",
            )
        if (
            receipt.provider_request_id != request.provider_request_id
            or receipt.request_sha256 != request.request_sha256
        ):
            _fail(
                "TBM_RETENTION_PROVIDER_MISMATCH",
                "key-destruction receipt does not match the exact request",
            )
        if receipt.status in {"destroyed", "already_destroyed"}:
            assert receipt.receipt_artifact is not None
            observed_at = self._trusted_now()
            target_key_ids = {
                item.encryption_key_id
                for item in manifest.resolution.key_references
            }
            if (
                receipt.receipt_artifact.media_type != _RECEIPT_MEDIA_TYPE
                or receipt.receipt_artifact.retention_policy_id
                != _MANIFEST_RETENTION_POLICY
                or receipt.receipt_artifact.encryption_key_id in target_key_ids
                or parse_rfc3339(cast(str, receipt.completed_at))
                < parse_rfc3339(request.requested_at)
                or parse_rfc3339(cast(str, receipt.completed_at))
                > parse_rfc3339(observed_at)
            ):
                return KeyDestructionReceipt(
                    provider_request_id=request.provider_request_id,
                    request_sha256=request.request_sha256,
                    status="unknown",
                    receipt_sha256=None,
                    receipt_artifact=None,
                    completed_at=None,
                )
            try:
                self._receipt_verifier.verify(request, receipt)
            except Exception:
                return KeyDestructionReceipt(
                    provider_request_id=request.provider_request_id,
                    request_sha256=request.request_sha256,
                    status="unknown",
                    receipt_sha256=None,
                    receipt_artifact=None,
                    completed_at=None,
                )
        return receipt

    def _late_hold_evidence(
        self,
        manifest: RedactionManifest,
        *,
        now: str,
    ) -> str | None:
        try:
            resolution, snapshot = self._fresh_state(manifest.request, now=now)
            if resolution != manifest.resolution:
                return "TBM_RETENTION_TARGET_DRIFT_AFTER_ERASURE"
            if any(item.legal_hold for item in snapshot.decisions):
                return "TBM_RETENTION_LEGAL_HOLD_LATE"
            if (
                snapshot.policy_state_sha256
                != manifest.retention_snapshot.policy_state_sha256
            ):
                return "TBM_RETENTION_POLICY_DRIFT_AFTER_ERASURE"
        except ArtifactRetentionEventV1Error:
            return "TBM_RETENTION_GUARD_FAILED_AFTER_ERASURE"
        return None

    def _store_manifest(self, manifest: RedactionManifest) -> EventArtifactRef:
        payload = dumps_redaction_manifest(manifest)
        try:
            descriptor = self._manifest_store.put(manifest, payload)
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_MANIFEST_PERSIST_FAILED",
                "redaction manifest could not be persisted",
            ) from error
        _manifest_artifact(descriptor, manifest)
        return descriptor

    def _verify_stored_manifest(
        self,
        manifest: RedactionManifest,
        descriptor: EventArtifactRef,
    ) -> None:
        _manifest_artifact(descriptor, manifest)
        loaded = loads_redaction_manifest(self._load_manifest_bytes(descriptor))
        if loaded != manifest:
            _fail(
                "TBM_RETENTION_MANIFEST_DRIFT",
                "stored redaction manifest does not match the operation",
            )

    def _load_manifest_bytes(self, descriptor: EventArtifactRef) -> bytes:
        try:
            payload = self._manifest_store.load(descriptor)
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_MANIFEST_UNAVAILABLE",
                "redaction manifest Artifact is unavailable",
            ) from error
        if type(payload) is not bytes:
            _fail(
                "TBM_RETENTION_MANIFEST_UNAVAILABLE",
                "manifest store returned invalid bytes",
            )
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if digest != descriptor.content_sha256 or len(payload) != descriptor.size_bytes:
            _fail(
                "TBM_RETENTION_MANIFEST_INTEGRITY_FAILED",
                "redaction manifest bytes do not match their descriptor",
            )
        return payload

    def _manifest_descriptor(
        self,
        events: tuple[CanonicalEvent, ...],
    ) -> EventArtifactRef:
        artifact_id = cast(str, events[0].payload["manifest_artifact_id"])
        for event in events:
            for descriptor in event.artifact_refs:
                if descriptor.artifact_id == artifact_id:
                    return descriptor
        _fail(
            "TBM_RETENTION_MANIFEST_UNAVAILABLE",
            "retention history lacks its manifest Artifact descriptor",
        )

    def _read_stream(self, operation_id: str) -> tuple[CanonicalEvent, ...]:
        page = self._ledger.read_stream(
            artifact_retention_stream_id(operation_id),
            1,
            ARTIFACT_RETENTION_MAX_EVENTS,
        )
        if page.has_more:
            _fail(
                "TBM_RETENTION_HISTORY_INCOMPLETE",
                "retention history exceeds the supported event bound",
            )
        return page.events

    def _projection(
        self,
        operation_id: str,
        manifest: RedactionManifest,
    ) -> RetentionProjection:
        return reduce_artifact_retention_events(
            self._read_stream(operation_id),
            manifest=manifest,
        )

    def _trusted_now(self) -> str:
        try:
            value = self._clock()
        except Exception as error:
            raise ArtifactRetentionEventV1Error(
                "TBM_RETENTION_CLOCK_FAILED",
                "trusted clock failed",
            ) from error
        return _timestamp(value, "trusted time")


def _retention_payload_schema(event_type: str) -> dict[str, object]:
    properties: dict[str, object] = {
        "operation_id": _string_schema(pattern=_OPERATION_ID_RE.pattern),
        "sequence": {"type": "integer", "minimum": 1, "maximum": _MAX_SEQUENCE},
        "manifest_sha256": _string_schema(pattern=_DIGEST_RE.pattern),
        "manifest_artifact_id": _string_schema(pattern=_ARTIFACT_ID_RE.pattern),
        "point": _string_schema(
            const=event_type.removeprefix("tbm.artifact.")
        ),
        "occurred_at": _string_schema(minimum=20, maximum=64),
        "retention_snapshot_sha256": _string_schema(pattern=_DIGEST_RE.pattern),
        "index_previous_bundle_id": _nullable_string_schema(_BUNDLE_ID_RE.pattern),
        "index_successor_bundle_id": _nullable_string_schema(_BUNDLE_ID_RE.pattern),
        "provider_request_ids": _string_array_schema(0, ARTIFACT_RETENTION_MAX_TARGETS),
        "provider_request_sha256s": _digest_array_schema(),
        "provider_id": _nullable_string_schema(_IDENTIFIER_RE.pattern),
        "provider_version": _nullable_string_schema(_IDENTIFIER_RE.pattern),
        "provider_registration_sha256": _nullable_string_schema(_DIGEST_RE.pattern),
        "provider_attestation_sha256": _nullable_string_schema(_DIGEST_RE.pattern),
        "destruction_authorization_sha256": _nullable_string_schema(
            _DIGEST_RE.pattern
        ),
        "provider_receipt_request_ids": _string_array_schema(
            0, ARTIFACT_RETENTION_MAX_TARGETS
        ),
        "provider_receipt_request_sha256s": _digest_array_schema(),
        "provider_receipt_sha256s": _digest_array_schema(),
        "replay_marker_sha256s": _digest_array_schema(),
        "unknown_provider_request_ids": _string_array_schema(
            0, ARTIFACT_RETENTION_MAX_TARGETS
        ),
        "failure_code": _nullable_string_schema(_IDENTIFIER_RE.pattern),
        "artifact_ids": {
            "type": "array",
            "items": _string_schema(pattern=_ARTIFACT_ID_RE.pattern),
            "minItems": 1,
            "maxItems": ARTIFACT_RETENTION_MAX_TARGETS,
            "uniqueItems": True,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _string_schema(
    *,
    pattern: str | None = None,
    const: str | None = None,
    minimum: int = 1,
    maximum: int = 512,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "string",
        "minLength": minimum,
        "maxLength": maximum,
    }
    if pattern is not None:
        schema["pattern"] = pattern
    if const is not None:
        schema["const"] = const
    return schema


def _nullable_string_schema(pattern: str) -> dict[str, object]:
    return {
        "oneOf": [
            _string_schema(pattern=pattern),
            {"type": "null"},
        ]
    }


def _string_array_schema(minimum: int, maximum: int) -> dict[str, object]:
    return {
        "type": "array",
        "items": _string_schema(pattern=_IDENTIFIER_RE.pattern),
        "minItems": minimum,
        "maxItems": maximum,
        "uniqueItems": True,
    }


def _digest_array_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": _string_schema(pattern=_DIGEST_RE.pattern),
        "minItems": 0,
        "maxItems": ARTIFACT_RETENTION_MAX_TARGETS,
        "uniqueItems": True,
    }


_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in ARTIFACT_RETENTION_EVENT_TYPES
}


def _draft_artifact_refs(draft: RetentionEventDraft) -> tuple[EventArtifactRef, ...]:
    refs: list[EventArtifactRef] = [draft.manifest_artifact]
    if draft.event_type in {
        ARTIFACT_RETENTION_APPLIED,
        ARTIFACT_REDACTION_MANIFEST_RECORDED,
    }:
        refs.extend(item.artifact for item in draft.manifest.resolution.targets)
    if draft.event_type in {
        ARTIFACT_REPLAY_PARTIAL_MARKED,
        ARTIFACT_CRYPTOGRAPHICALLY_ERASED,
        ARTIFACT_TOMBSTONED,
    }:
        refs.extend(
            replace(item.artifact, availability="erased")
            for item in draft.manifest.resolution.targets
        )
    refs.extend(draft.receipt_artifacts)
    if len({item.artifact_id for item in refs}) != len(refs):
        _fail(
            "TBM_RETENTION_ARTIFACT_MISMATCH",
            "retention event Artifact descriptors must be unique",
        )
    return tuple(sorted(refs, key=lambda item: item.artifact_id))


def _verify_key_reference_closure(resolution: RetentionResolution) -> None:
    target_by_key: dict[str, set[str]] = {}
    for target in resolution.targets:
        assert target.artifact.encryption_key_id is not None
        target_by_key.setdefault(target.artifact.encryption_key_id, set()).add(
            target.artifact.artifact_id
        )
    reference_by_key = {
        item.encryption_key_id: set(item.artifact_ids)
        for item in resolution.key_references
    }
    if set(target_by_key) != set(reference_by_key):
        _fail(
            "TBM_RETENTION_KEY_STILL_REFERENCED",
            "key-reference closure is missing or contains an unrelated key",
        )
    for key_id, target_ids in target_by_key.items():
        if reference_by_key[key_id] != target_ids:
            _fail(
                "TBM_RETENTION_KEY_STILL_REFERENCED",
                "an encryption key still protects a non-target Artifact",
            )


def _verify_erasure_eligible(
    snapshot: RetentionPolicySnapshot,
    *,
    now: str,
) -> None:
    canonical_now = _timestamp(now, "trusted time")
    now_value = parse_rfc3339(canonical_now)
    for decision in snapshot.decisions:
        if decision.legal_hold:
            _fail(
                "TBM_RETENTION_LEGAL_HOLD",
                "legal hold blocks cryptographic erasure",
            )
        if decision.retain_until is None or parse_rfc3339(decision.retain_until) > now_value:
            _fail(
                "TBM_RETENTION_NOT_EXPIRED",
                "retention policy does not yet permit erasure",
            )


def _manifest_artifact(
    descriptor: EventArtifactRef,
    manifest: RedactionManifest,
) -> None:
    if type(descriptor) is not EventArtifactRef:
        _invalid("manifest descriptor must be exactly EventArtifactRef")
    payload = dumps_redaction_manifest(manifest)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if (
        descriptor.content_sha256 != digest
        or descriptor.artifact_id != "artifact_sha256_" + digest.removeprefix("sha256:")
        or descriptor.media_type != _MANIFEST_MEDIA_TYPE
        or descriptor.size_bytes != len(payload)
        or descriptor.classification not in {"confidential", "restricted"}
        or descriptor.retention_policy_id != _MANIFEST_RETENTION_POLICY
        or descriptor.encryption_key_id is None
        or descriptor.encryption_key_id
        in {
            item.encryption_key_id
            for item in manifest.resolution.key_references
        }
        or descriptor.availability != "available"
    ):
        _fail(
            "TBM_RETENTION_MANIFEST_ARTIFACT_INVALID",
            "manifest Artifact descriptor does not bind exact protected bytes",
        )


def _verify_manifest_bound_artifact_refs(
    event: CanonicalEvent,
    payload: Mapping[str, object],
    manifest: RedactionManifest,
    manifest_descriptor: EventArtifactRef,
) -> None:
    refs = {item.artifact_id: item for item in event.artifact_refs}
    expected: dict[str, EventArtifactRef] = {
        manifest_descriptor.artifact_id: manifest_descriptor
    }
    if event.event_type in {
        ARTIFACT_RETENTION_APPLIED,
        ARTIFACT_REDACTION_MANIFEST_RECORDED,
    }:
        expected.update(
            (item.artifact.artifact_id, item.artifact)
            for item in manifest.resolution.targets
        )
    if event.event_type in {
        ARTIFACT_REPLAY_PARTIAL_MARKED,
        ARTIFACT_CRYPTOGRAPHICALLY_ERASED,
        ARTIFACT_TOMBSTONED,
    }:
        expected.update(
            (
                item.artifact.artifact_id,
                replace(item.artifact, availability="erased"),
            )
            for item in manifest.resolution.targets
        )
    receipt_sha256s = tuple(
        cast(tuple[str, ...], payload["provider_receipt_sha256s"])
    )
    target_key_ids = {
        item.encryption_key_id for item in manifest.resolution.key_references
    }
    for digest in receipt_sha256s:
        candidates = tuple(
            descriptor
            for descriptor in event.artifact_refs
            if descriptor.content_sha256 == digest
            and descriptor.artifact_id not in expected
        )
        if len(candidates) != 1:
            _fail(
                "TBM_RETENTION_RECEIPT_MISMATCH",
                "retention receipt digest lacks one exact Artifact descriptor",
            )
        descriptor = candidates[0]
        if (
            descriptor.artifact_id
            != "artifact_sha256_" + digest.removeprefix("sha256:")
            or descriptor.media_type != _RECEIPT_MEDIA_TYPE
            or descriptor.classification not in {"confidential", "restricted"}
            or descriptor.retention_policy_id != _MANIFEST_RETENTION_POLICY
            or descriptor.encryption_key_id is None
            or descriptor.encryption_key_id in target_key_ids
            or descriptor.availability != "available"
        ):
            _fail(
                "TBM_RETENTION_RECEIPT_MISMATCH",
                "retention receipt Artifact descriptor is not governance-safe",
            )
        expected[descriptor.artifact_id] = descriptor
    if refs != expected:
        _fail(
            "TBM_RETENTION_ARTIFACT_MISMATCH",
            "retention event Artifact descriptors do not match the manifest",
        )


def _manifest_from_dict(value: dict[str, object]) -> RedactionManifest:
    expected = {
        "contract_version",
        "manifest_sha256",
        "request",
        "resolution",
        "retention_snapshot",
        "expected_index_bundle_id",
        "successor_index_bundle_id",
        "replay_partial_markers",
        "planned_at",
    }
    _fields(value, expected, "RedactionManifest")
    return RedactionManifest(
        manifest_sha256=_string(value["manifest_sha256"], "manifest_sha256"),
        request=_request_from_dict(_dict(value["request"], "request")),
        resolution=_resolution_from_dict(
            _dict(value["resolution"], "resolution")
        ),
        retention_snapshot=_snapshot_from_dict(
            _dict(value["retention_snapshot"], "retention_snapshot")
        ),
        expected_index_bundle_id=_string(
            value["expected_index_bundle_id"], "expected_index_bundle_id"
        ),
        successor_index_bundle_id=_string(
            value["successor_index_bundle_id"], "successor_index_bundle_id"
        ),
        replay_partial_markers=tuple(
            _marker_from_dict(_dict(item, "replay_partial_marker"))
            for item in _list(
                value["replay_partial_markers"], "replay_partial_markers"
            )
        ),
        planned_at=_string(value["planned_at"], "planned_at"),
        contract_version=_string(value["contract_version"], "contract_version"),
    )


def _request_from_dict(value: dict[str, object]) -> RetentionRequest:
    expected = {
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "authorization_event_id",
        "artifact_ids",
        "deletion_policy_id",
        "reason_code",
        "idempotency_key_sha256",
    }
    _fields(value, expected, "RetentionRequest")
    return RetentionRequest(
        organization_id=_string(value["organization_id"], "organization_id"),
        tenant_id=_string(value["tenant_id"], "tenant_id"),
        repository_id=_string(value["repository_id"], "repository_id"),
        environment_id=_string(value["environment_id"], "environment_id"),
        authorization_event_id=_string(
            value["authorization_event_id"], "authorization_event_id"
        ),
        artifact_ids=tuple(
            _string(item, "artifact_id")
            for item in _list(value["artifact_ids"], "artifact_ids")
        ),
        deletion_policy_id=_string(
            value["deletion_policy_id"], "deletion_policy_id"
        ),
        reason_code=_string(value["reason_code"], "reason_code"),
        idempotency_key_sha256=_string(
            value["idempotency_key_sha256"], "idempotency_key_sha256"
        ),
    )


def _resolution_from_dict(value: dict[str, object]) -> RetentionResolution:
    _fields(value, {"targets", "key_references"}, "RetentionResolution")
    return RetentionResolution(
        targets=tuple(
            _target_from_dict(_dict(item, "target"))
            for item in _list(value["targets"], "targets")
        ),
        key_references=tuple(
            _key_reference_from_dict(_dict(item, "key_reference"))
            for item in _list(value["key_references"], "key_references")
        ),
    )


def _target_from_dict(value: dict[str, object]) -> RedactionTarget:
    _fields(
        value,
        {"artifact", "memory_revision_ids", "replay_impacts"},
        "RedactionTarget",
    )
    return RedactionTarget(
        artifact=_artifact_from_dict(_dict(value["artifact"], "artifact")),
        memory_revision_ids=tuple(
            _string(item, "memory_revision_id")
            for item in _list(value["memory_revision_ids"], "memory_revision_ids")
        ),
        replay_impacts=tuple(
            _impact_from_dict(_dict(item, "replay_impact"))
            for item in _list(value["replay_impacts"], "replay_impacts")
        ),
    )


def _artifact_from_dict(value: dict[str, object]) -> EventArtifactRef:
    expected = {
        "artifact_id",
        "content_sha256",
        "media_type",
        "size_bytes",
        "classification",
        "retention_policy_id",
        "encryption_key_id",
        "availability",
    }
    _fields(value, expected, "EventArtifactRef")
    return EventArtifactRef(
        artifact_id=_string(value["artifact_id"], "artifact_id"),
        content_sha256=_string(value["content_sha256"], "content_sha256"),
        media_type=_string(value["media_type"], "media_type"),
        size_bytes=_integer(value["size_bytes"], "size_bytes"),
        classification=cast(
            EventClassification,
            _string(value["classification"], "classification"),
        ),
        retention_policy_id=_string(
            value["retention_policy_id"], "retention_policy_id"
        ),
        encryption_key_id=(
            None
            if value["encryption_key_id"] is None
            else _string(value["encryption_key_id"], "encryption_key_id")
        ),
        availability=cast(
            Literal["available", "pending", "unavailable", "erased"],
            _string(value["availability"], "availability"),
        ),
    )


def _impact_from_dict(value: dict[str, object]) -> ReplayImpact:
    _fields(
        value,
        {
            "replay_manifest_sha256",
            "missing_components",
            "source_completeness",
            "source_missing_components",
        },
        "ReplayImpact",
    )
    return ReplayImpact(
        replay_manifest_sha256=_string(
            value["replay_manifest_sha256"], "replay_manifest_sha256"
        ),
        missing_components=tuple(
            cast(ReplayComponentName, _string(item, "missing_component"))
            for item in _list(value["missing_components"], "missing_components")
        ),
        source_completeness=cast(
            Literal["complete", "legacy_partial"],
            _string(value["source_completeness"], "source_completeness"),
        ),
        source_missing_components=tuple(
            cast(ReplayComponentName, _string(item, "source_missing_component"))
            for item in _list(
                value["source_missing_components"],
                "source_missing_components",
            )
        ),
    )


def _key_reference_from_dict(value: dict[str, object]) -> KeyReferenceSet:
    _fields(value, {"encryption_key_id", "artifact_ids"}, "KeyReferenceSet")
    return KeyReferenceSet(
        encryption_key_id=_string(value["encryption_key_id"], "encryption_key_id"),
        artifact_ids=tuple(
            _string(item, "artifact_id")
            for item in _list(value["artifact_ids"], "artifact_ids")
        ),
    )


def _snapshot_from_dict(value: dict[str, object]) -> RetentionPolicySnapshot:
    _fields(
        value,
        {"snapshot_sha256", "policy_state_sha256", "decisions", "evaluated_at"},
        "RetentionPolicySnapshot",
    )
    return RetentionPolicySnapshot(
        snapshot_sha256=_string(value["snapshot_sha256"], "snapshot_sha256"),
        policy_state_sha256=_string(
            value["policy_state_sha256"], "policy_state_sha256"
        ),
        decisions=tuple(
            _decision_from_dict(_dict(item, "retention_decision"))
            for item in _list(value["decisions"], "decisions")
        ),
        evaluated_at=_string(value["evaluated_at"], "evaluated_at"),
    )


def _decision_from_dict(value: dict[str, object]) -> ArtifactRetentionDecision:
    _fields(
        value,
        {"artifact_id", "retention_policy_id", "retain_until", "legal_hold", "hold_epoch"},
        "ArtifactRetentionDecision",
    )
    if type(value["legal_hold"]) is not bool:
        _invalid("legal_hold must be a boolean")
    return ArtifactRetentionDecision(
        artifact_id=_string(value["artifact_id"], "artifact_id"),
        retention_policy_id=_string(
            value["retention_policy_id"], "retention_policy_id"
        ),
        retain_until=(
            None
            if value["retain_until"] is None
            else _string(value["retain_until"], "retain_until")
        ),
        legal_hold=cast(bool, value["legal_hold"]),
        hold_epoch=_integer(value["hold_epoch"], "hold_epoch"),
    )


def _marker_from_dict(value: dict[str, object]) -> ReplayPartialMarker:
    _fields(
        value,
        {
            "marker_sha256",
            "replay_manifest_sha256",
            "missing_components",
            "erased_artifact_ids",
            "reason_code",
            "marked_at",
        },
        "ReplayPartialMarker",
    )
    return ReplayPartialMarker(
        marker_sha256=_string(value["marker_sha256"], "marker_sha256"),
        replay_manifest_sha256=_string(
            value["replay_manifest_sha256"], "replay_manifest_sha256"
        ),
        missing_components=tuple(
            cast(ReplayComponentName, _string(item, "missing_component"))
            for item in _list(value["missing_components"], "missing_components")
        ),
        erased_artifact_ids=tuple(
            _string(item, "artifact_id")
            for item in _list(value["erased_artifact_ids"], "erased_artifact_ids")
        ),
        reason_code=_string(value["reason_code"], "reason_code"),
        marked_at=_string(value["marked_at"], "marked_at"),
    )


def _verify_parent(event: CanonicalEvent, parent: CanonicalEvent) -> None:
    try:
        verify_event_parent(event, parent)
    except Exception as error:
        raise ArtifactRetentionEventV1Error(
            "TBM_RETENTION_TRANSITION_INVALID",
            "retention event parent chain is invalid",
        ) from error


def _event_tuple(value: object, name: str) -> None:
    if type(value) is not tuple or any(
        type(item) is not CanonicalEvent for item in cast(tuple[object, ...], value)
    ):
        _invalid(f"{name} must contain CanonicalEvent values")


def _fields(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _invalid(f"{name} has invalid fields")


def _dict(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        _invalid(f"{name} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, name: str) -> list[object]:
    if type(value) is not list:
        _invalid(f"{name} must be an array")
    return cast(list[object], value)


def _string(value: object, name: str) -> str:
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return cast(str, value)


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    return cast(int, value)


def _timestamp(value: object, name: str) -> str:
    if type(value) is not str:
        _invalid(f"{name} must be an RFC3339 timestamp")
    try:
        return canonical_rfc3339(cast(str, value))
    except (TypeError, ValueError) as error:
        raise ArtifactRetentionEventV1Error(
            "TBM_RETENTION_INVALID",
            f"{name} must be an RFC3339 timestamp",
        ) from error


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a bounded identifier")


def _operation_id(value: object) -> None:
    if type(value) is not str or _OPERATION_ID_RE.fullmatch(value) is None:
        _invalid("operation_id must be a retention operation ID")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a canonical sha256 digest")


def _artifact_id(value: object, name: str) -> None:
    if type(value) is not str or _ARTIFACT_ID_RE.fullmatch(value) is None:
        _invalid(f"{name} must be an Artifact ID")


def _artifact_ids(values: object, name: str) -> None:
    if (
        type(values) is not tuple
        or not 1 <= len(cast(tuple[object, ...], values)) <= ARTIFACT_RETENTION_MAX_TARGETS
        or any(
            type(item) is not str or _ARTIFACT_ID_RE.fullmatch(item) is None
            for item in cast(tuple[object, ...], values)
        )
        or values != tuple(sorted(set(cast(tuple[str, ...], values))))
    ):
        _invalid(f"{name} must be a unique sorted bounded Artifact ID tuple")


def _revision_ids(values: object) -> None:
    if (
        type(values) is not tuple
        or len(cast(tuple[object, ...], values)) > ARTIFACT_RETENTION_MAX_TARGETS
        or any(
            type(item) is not str or _REVISION_ID_RE.fullmatch(item) is None
            for item in cast(tuple[object, ...], values)
        )
        or values != tuple(sorted(set(cast(tuple[str, ...], values))))
    ):
        _invalid("memory_revision_ids must be a unique sorted bounded tuple")


def _identifiers(values: object, name: str) -> None:
    if (
        type(values) is not tuple
        or len(cast(tuple[object, ...], values)) > ARTIFACT_RETENTION_MAX_TARGETS
        or any(
            type(item) is not str or _IDENTIFIER_RE.fullmatch(item) is None
            for item in cast(tuple[object, ...], values)
        )
        or len(set(cast(tuple[str, ...], values))) != len(cast(tuple[str, ...], values))
    ):
        _invalid(f"{name} must be a unique bounded identifier tuple")


def _digests(values: object, name: str) -> None:
    if (
        type(values) is not tuple
        or len(cast(tuple[object, ...], values)) > ARTIFACT_RETENTION_MAX_TARGETS
        or any(
            type(item) is not str or _DIGEST_RE.fullmatch(item) is None
            for item in cast(tuple[object, ...], values)
        )
        or len(set(cast(tuple[str, ...], values))) != len(cast(tuple[str, ...], values))
    ):
        _invalid(f"{name} must be a unique bounded digest tuple")


def _replay_components(values: object) -> None:
    if (
        type(values) is not tuple
        or not values
        or any(item not in REPLAY_COMPONENT_NAMES for item in cast(tuple[object, ...], values))
        or values
        != tuple(
            name for name in REPLAY_COMPONENT_NAMES if name in cast(tuple[object, ...], values)
        )
    ):
        _invalid("missing_components must be a canonical non-empty component tuple")


def _transition_invalid(message: str) -> NoReturn:
    _fail("TBM_RETENTION_TRANSITION_INVALID", message)


def _invalid(message: str) -> NoReturn:
    _fail("TBM_RETENTION_INVALID", message)


def _fail(code: str, message: str) -> NoReturn:
    raise ArtifactRetentionEventV1Error(code, message)


__all__ = [
    "ARTIFACT_CRYPTOGRAPHICALLY_ERASED",
    "ARTIFACT_CRYPTO_ERASURE_AUTHORIZED",
    "ARTIFACT_CRYPTO_ERASURE_BLOCKED",
    "ARTIFACT_CRYPTO_ERASURE_REJECTED",
    "ARTIFACT_CRYPTO_ERASURE_REQUESTED",
    "ARTIFACT_CRYPTO_ERASURE_UNKNOWN",
    "ARTIFACT_INDEX_PURGED",
    "ARTIFACT_REDACTION_MANIFEST_RECORDED",
    "ARTIFACT_REPLAY_PARTIAL_MARKED",
    "ARTIFACT_RETENTION_APPLIED",
    "ARTIFACT_RETENTION_EVENT_PROTOCOL_VERSION",
    "ARTIFACT_RETENTION_EVENT_TYPES",
    "ARTIFACT_RETENTION_MAX_EVENTS",
    "ARTIFACT_RETENTION_MAX_TARGETS",
    "ARTIFACT_RETENTION_MANIFEST_MAX_BYTES",
    "ARTIFACT_RETENTION_PAYLOAD_SCHEMA_ID",
    "ARTIFACT_RETENTION_STREAM_TYPE",
    "ARTIFACT_TOMBSTONED",
    "ArtifactRetentionDecision",
    "ArtifactRetentionEventV1Error",
    "KeyDestructionProvider",
    "KeyDestructionReceiptVerifier",
    "KeyDestructionReceipt",
    "KeyDestructionRequest",
    "KeyDestructionStatus",
    "KeyReferenceSet",
    "RedactionManifest",
    "RedactionManifestStore",
    "RedactionTarget",
    "ReplayImpact",
    "ReplayPartialMarker",
    "RetentionErasureCoordinator",
    "RetentionErasureResult",
    "RetentionDestructionAuthorization",
    "RetentionOperationStatus",
    "RetentionPolicyGuard",
    "RetentionPolicySnapshot",
    "RetentionProjection",
    "RetentionRequest",
    "RetentionResolution",
    "RetentionTargetResolver",
    "TrustedKeyDestructionProvider",
    "artifact_retention_payload_dispatch_schema",
    "artifact_retention_stream_id",
    "build_artifact_retention_event_registry",
    "build_key_destruction_requests",
    "build_redaction_manifest",
    "build_replay_partial_marker",
    "build_retention_destruction_authorization",
    "build_retention_policy_snapshot",
    "dumps_artifact_retention_payload_dispatch_schema",
    "dumps_redaction_manifest",
    "loads_redaction_manifest",
    "reduce_artifact_retention_events",
    "replay_partial_marker_for_manifest",
    "require_replay_not_erased",
    "verify_artifact_retention_event",
]
