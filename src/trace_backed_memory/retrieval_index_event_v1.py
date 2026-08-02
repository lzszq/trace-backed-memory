from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, Protocol, cast

from ._ingestion import parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .authorization_v3 import (
    AuthorizationDecision,
    AuthorizationPermission,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    parse_authorization_decision,
    parse_authorization_policy,
    verify_authorization_decision,
)
from .contracts_v3 import canonical_sha256
from .event_registry_v1 import EventPayloadRegistration, EventTypeRegistry
from .event_v1 import CanonicalEvent, build_canonical_event, verify_event_parent
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerConflictError,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerIdempotency,
    LedgerTenantPartition,
    verify_ledger_append_receipt,
)
from .managed_index_v3 import (
    ManagedIndexBundle,
    ManagedIndexPublication,
    ManagedIndexRepository,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerV1Error,
    execute_reducer_step,
    initial_reducer_state,
)
from .retrieval_v3 import IndexKind, IndexVersion


RETRIEVAL_INDEX_EVENT_PROTOCOL_VERSION = "tbm.retrieval-index-event.v1"
RETRIEVAL_INDEX_EVENT_STREAM_TYPE = "retrieval_index"
RETRIEVAL_INDEX_EVENT_PROJECTION = "retrieval_index_current_v1"
RETRIEVAL_INDEX_EVENT_REDUCER_ID = "retrieval-index-current"
RETRIEVAL_INDEX_EVENT_MAX_BATCH = 16
RETRIEVAL_INDEX_EVENT_MAX_STREAM_EVENTS = 2_048
RETRIEVAL_INDEX_JSON_MAX_BYTES = 512 * 1024
RETRIEVAL_INDEX_JSON_MAX_DEPTH = 32
RETRIEVAL_INDEX_JSON_MAX_NODES = 50_000
RETRIEVAL_INDEX_MANIFEST_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "retrieval_index_manifest_v1.schema.json"
)
RETRIEVAL_INDEX_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "retrieval_index_event_payload_registry_v1.schema.json"
)

INDEX_BUILD_REQUESTED = "tbm.index.build_requested"
INDEX_BUILD_COMPLETED = "tbm.index.build_completed"
INDEX_ACTIVATED = "tbm.index.activated"
INDEX_MARKED_STALE = "tbm.index.marked_stale"
RETRIEVAL_INDEX_EVENT_TYPES = tuple(
    sorted(
        (
            INDEX_BUILD_REQUESTED,
            INDEX_BUILD_COMPLETED,
            INDEX_ACTIVATED,
            INDEX_MARKED_STALE,
        )
    )
)
_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in RETRIEVAL_INDEX_EVENT_TYPES
}
_INDEX_KINDS: tuple[IndexKind, ...] = (
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "git_graph",
)
_ALL_CLASSIFICATIONS = (
    "public",
    "internal",
    "confidential",
    "restricted",
)
_STALE_REASONS = (
    "source_advanced",
    "policy_changed",
    "retention_purge",
    "provider_rotated",
    "manual",
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_ID_RE = re.compile(r"^managed_index_bundle_sha256_[0-9a-f]{64}$")
_MANIFEST_ID_RE = re.compile(
    r"^retrieval_index_manifest_sha256_[0-9a-f]{64}$"
)
_REQUEST_ID_RE = re.compile(r"^index_build_request_sha256_[0-9a-f]{64}$")
_COMPLETION_ID_RE = re.compile(
    r"^index_build_completion_sha256_[0-9a-f]{64}$"
)
_ACTIVATION_ID_RE = re.compile(r"^index_activation_sha256_[0-9a-f]{64}$")
_STALE_ID_RE = re.compile(r"^index_stale_sha256_[0-9a-f]{64}$")
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")


class RetrievalIndexEventV1Error(ReducerV1Error):
    """Stable retrieval-index event, replay, and head-selection failure."""


def _fail(code: str, message: str) -> NoReturn:
    raise RetrievalIndexEventV1Error(code, message)


def _record_invalid(message: str) -> NoReturn:
    _fail("TBM_RETRIEVAL_INDEX_RECORD_INVALID", message)


def _transition_invalid(message: str) -> NoReturn:
    _fail("TBM_RETRIEVAL_INDEX_TRANSITION_INVALID", message)


def _projection_invalid(message: str) -> NoReturn:
    _fail("TBM_RETRIEVAL_INDEX_PROJECTION_INVALID", message)


@dataclass(frozen=True)
class RetrievalIndexManifest:
    manifest_id: str
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    bundle_id: str
    retriever_id: str
    retriever_version: str
    index_versions: tuple[IndexVersion, ...]
    source_event_watermark: int
    source_event_sha256: str
    source_catalog_sha256: str
    memory_revision_ids: tuple[str, ...]
    embedding_provider_id: str
    embedding_model_id: str
    lexical_tokenizer_id: str
    lexical_tokenizer_version: str
    git_graph_version: str
    build_sha256: str
    stale_status: Literal["fresh"] = "fresh"
    contract_version: str = "tbm.retrieval-index-manifest.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.retrieval-index-manifest.v1":
            _record_invalid("retrieval index manifest version is unsupported")
        if (
            type(self.manifest_id) is not str
            or _MANIFEST_ID_RE.fullmatch(self.manifest_id) is None
        ):
            _record_invalid("manifest_id is invalid")
        _target_partition(self)
        if (
            type(self.bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.bundle_id) is None
        ):
            _record_invalid("bundle_id is invalid")
        for name in (
            "retriever_id",
            "retriever_version",
            "embedding_provider_id",
            "embedding_model_id",
            "lexical_tokenizer_id",
            "lexical_tokenizer_version",
            "git_graph_version",
        ):
            _identifier(getattr(self, name), name)
        _index_versions(self.index_versions)
        if (
            type(self.source_event_watermark) is not int
            or self.source_event_watermark < 1
        ):
            _record_invalid("source_event_watermark must be positive")
        for name in (
            "source_event_sha256",
            "source_catalog_sha256",
            "build_sha256",
        ):
            _digest(getattr(self, name), name)
        if (
            type(self.memory_revision_ids) is not tuple
            or len(self.memory_revision_ids) > 1_000
            or any(
                type(value) is not str
                or _REVISION_ID_RE.fullmatch(value) is None
                for value in self.memory_revision_ids
            )
            or self.memory_revision_ids
            != tuple(sorted(set(self.memory_revision_ids)))
        ):
            _record_invalid(
                "memory_revision_ids must be a bounded unique sorted tuple"
            )
        if self.stale_status != "fresh":
            _record_invalid("new retrieval index manifests must be fresh")
        if self.git_graph_version != self.index_version("git_graph").index_version:
            _record_invalid("git_graph_version does not match index versions")
        if self.manifest_id != _content_id(
            "retrieval_index_manifest_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_HASH_MISMATCH",
                "manifest_id does not match canonical content",
            )

    def index_version(self, kind: IndexKind) -> IndexVersion:
        for version in self.index_versions:
            if version.index_kind == kind:
                return version
        raise AssertionError("validated manifest is missing an index kind")

    def verify_bundle(self, bundle: ManagedIndexBundle) -> None:
        if type(bundle) is not ManagedIndexBundle:
            _fail(
                "TBM_RETRIEVAL_INDEX_BUNDLE_INVALID",
                "managed index bundle has an invalid type",
            )
        revision_ids = tuple(
            sorted(candidate.memory_revision_id for candidate in bundle.candidates)
        )
        if (
            bundle.bundle_id != self.bundle_id
            or bundle.tenant_id != self.tenant_id
            or bundle.repository_id != self.repository_id
            or bundle.environment_id != self.environment_id
            or bundle.retriever_id != self.retriever_id
            or bundle.retriever_version != self.retriever_version
            or bundle.index_versions != self.index_versions
            or bundle.source_catalog_sha256 != self.source_catalog_sha256
            or revision_ids != self.memory_revision_ids
            or bundle.semantic_provider_id != self.embedding_provider_id
            or bundle.semantic_provider_version != self.embedding_model_id
            or bundle.tokenizer_id != self.lexical_tokenizer_id
            or bundle.tokenizer_version != self.lexical_tokenizer_version
            or canonical_sha256(bundle.to_dict()) != self.build_sha256
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_BUNDLE_MISMATCH",
                "managed index bundle does not match its event manifest",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "bundle_id": self.bundle_id,
            "retriever_id": self.retriever_id,
            "retriever_version": self.retriever_version,
            "index_versions": [item.to_dict() for item in self.index_versions],
            "source_event_watermark": self.source_event_watermark,
            "source_event_sha256": self.source_event_sha256,
            "source_catalog_sha256": self.source_catalog_sha256,
            "memory_revision_ids": list(self.memory_revision_ids),
            "embedding_provider_id": self.embedding_provider_id,
            "embedding_model_id": self.embedding_model_id,
            "lexical_tokenizer_id": self.lexical_tokenizer_id,
            "lexical_tokenizer_version": self.lexical_tokenizer_version,
            "git_graph_version": self.git_graph_version,
            "build_sha256": self.build_sha256,
            "stale_status": self.stale_status,
        }

    def to_dict(self) -> dict[str, object]:
        return {"manifest_id": self.manifest_id, **self._unsigned_dict()}


def build_retrieval_index_manifest(
    *,
    partition: LedgerTenantPartition,
    bundle: ManagedIndexBundle,
    source_event_watermark: int,
    source_event_sha256: str,
) -> RetrievalIndexManifest:
    if type(partition) is not LedgerTenantPartition:
        _record_invalid("partition must be LedgerTenantPartition")
    if type(bundle) is not ManagedIndexBundle:
        _record_invalid("bundle must be ManagedIndexBundle")
    if (
        bundle.tenant_id != partition.tenant_id
        or bundle.repository_id != partition.repository_id
        or bundle.environment_id != partition.environment_id
    ):
        _record_invalid("bundle is outside the manifest partition")
    revision_ids = tuple(
        sorted(candidate.memory_revision_id for candidate in bundle.candidates)
    )
    git_graph_version = bundle.index_version("git_graph").index_version
    values: dict[str, object] = {
        "contract_version": "tbm.retrieval-index-manifest.v1",
        "organization_id": partition.organization_id,
        "tenant_id": partition.tenant_id,
        "repository_id": partition.repository_id,
        "environment_id": partition.environment_id,
        "bundle_id": bundle.bundle_id,
        "retriever_id": bundle.retriever_id,
        "retriever_version": bundle.retriever_version,
        "index_versions": [item.to_dict() for item in bundle.index_versions],
        "source_event_watermark": source_event_watermark,
        "source_event_sha256": source_event_sha256,
        "source_catalog_sha256": bundle.source_catalog_sha256,
        "memory_revision_ids": list(revision_ids),
        "embedding_provider_id": bundle.semantic_provider_id,
        "embedding_model_id": bundle.semantic_provider_version,
        "lexical_tokenizer_id": bundle.tokenizer_id,
        "lexical_tokenizer_version": bundle.tokenizer_version,
        "git_graph_version": git_graph_version,
        "build_sha256": canonical_sha256(bundle.to_dict()),
        "stale_status": "fresh",
    }
    return RetrievalIndexManifest(
        manifest_id=_content_id("retrieval_index_manifest_sha256_", values),
        organization_id=partition.organization_id,
        tenant_id=partition.tenant_id,
        repository_id=partition.repository_id,
        environment_id=partition.environment_id,
        bundle_id=bundle.bundle_id,
        retriever_id=bundle.retriever_id,
        retriever_version=bundle.retriever_version,
        index_versions=bundle.index_versions,
        source_event_watermark=source_event_watermark,
        source_event_sha256=source_event_sha256,
        source_catalog_sha256=bundle.source_catalog_sha256,
        memory_revision_ids=revision_ids,
        embedding_provider_id=bundle.semantic_provider_id,
        embedding_model_id=bundle.semantic_provider_version,
        lexical_tokenizer_id=bundle.tokenizer_id,
        lexical_tokenizer_version=bundle.tokenizer_version,
        git_graph_version=git_graph_version,
        build_sha256=cast(str, values["build_sha256"]),
    )


@dataclass(frozen=True)
class IndexBuildRequest:
    build_request_id: str
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    source_event_watermark: int
    source_event_sha256: str
    source_catalog_sha256: str
    retriever_id: str
    retriever_version: str
    requested_by: str
    requested_via_client_id: str
    authorization_event_id: str
    requested_at: str
    contract_version: str = "tbm.index-build-request.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.index-build-request.v1":
            _record_invalid("index build request version is unsupported")
        if (
            type(self.build_request_id) is not str
            or _REQUEST_ID_RE.fullmatch(self.build_request_id) is None
        ):
            _record_invalid("build_request_id is invalid")
        _target_partition(self)
        if (
            type(self.source_event_watermark) is not int
            or self.source_event_watermark < 1
        ):
            _record_invalid("source_event_watermark must be positive")
        for name in ("source_event_sha256", "source_catalog_sha256"):
            _digest(getattr(self, name), name)
        for name in (
            "retriever_id",
            "retriever_version",
            "requested_by",
            "requested_via_client_id",
            "authorization_event_id",
        ):
            _identifier(getattr(self, name), name)
        _timestamp(self.requested_at, "requested_at")
        if self.build_request_id != _content_id(
            "index_build_request_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_HASH_MISMATCH",
                "build_request_id does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "source_event_watermark": self.source_event_watermark,
            "source_event_sha256": self.source_event_sha256,
            "source_catalog_sha256": self.source_catalog_sha256,
            "retriever_id": self.retriever_id,
            "retriever_version": self.retriever_version,
            "requested_by": self.requested_by,
            "requested_via_client_id": self.requested_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "requested_at": self.requested_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"build_request_id": self.build_request_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class IndexBuildCompletion:
    completion_id: str
    build_request_id: str
    manifest: RetrievalIndexManifest
    completed_by: str
    completed_via_client_id: str
    authorization_event_id: str
    completed_at: str
    contract_version: str = "tbm.index-build-completion.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.index-build-completion.v1":
            _record_invalid("index build completion version is unsupported")
        if (
            type(self.completion_id) is not str
            or _COMPLETION_ID_RE.fullmatch(self.completion_id) is None
        ):
            _record_invalid("completion_id is invalid")
        if (
            type(self.build_request_id) is not str
            or _REQUEST_ID_RE.fullmatch(self.build_request_id) is None
        ):
            _record_invalid("build_request_id is invalid")
        if type(self.manifest) is not RetrievalIndexManifest:
            _record_invalid("manifest is invalid")
        for name in (
            "completed_by",
            "completed_via_client_id",
            "authorization_event_id",
        ):
            _identifier(getattr(self, name), name)
        _timestamp(self.completed_at, "completed_at")
        if self.completion_id != _content_id(
            "index_build_completion_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_HASH_MISMATCH",
                "completion_id does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "build_request_id": self.build_request_id,
            "manifest": self.manifest.to_dict(),
            "completed_by": self.completed_by,
            "completed_via_client_id": self.completed_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "completed_at": self.completed_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"completion_id": self.completion_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class IndexActivation:
    activation_id: str
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    manifest_id: str
    bundle_id: str
    completion_id: str
    previous_bundle_id: str | None
    activated_by: str
    activated_via_client_id: str
    authorization_event_id: str
    activated_at: str
    contract_version: str = "tbm.index-activation.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.index-activation.v1":
            _record_invalid("index activation version is unsupported")
        if (
            type(self.activation_id) is not str
            or _ACTIVATION_ID_RE.fullmatch(self.activation_id) is None
        ):
            _record_invalid("activation_id is invalid")
        _target_partition(self)
        for value, pattern, name in (
            (self.manifest_id, _MANIFEST_ID_RE, "manifest_id"),
            (self.bundle_id, _BUNDLE_ID_RE, "bundle_id"),
            (self.completion_id, _COMPLETION_ID_RE, "completion_id"),
        ):
            if type(value) is not str or pattern.fullmatch(value) is None:
                _record_invalid(f"{name} is invalid")
        if self.previous_bundle_id is not None and (
            type(self.previous_bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.previous_bundle_id) is None
        ):
            _record_invalid("previous_bundle_id is invalid")
        if self.previous_bundle_id == self.bundle_id:
            _record_invalid("activation cannot name itself as predecessor")
        for name in (
            "activated_by",
            "activated_via_client_id",
            "authorization_event_id",
        ):
            _identifier(getattr(self, name), name)
        _timestamp(self.activated_at, "activated_at")
        if self.activation_id != _content_id(
            "index_activation_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_HASH_MISMATCH",
                "activation_id does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "manifest_id": self.manifest_id,
            "bundle_id": self.bundle_id,
            "completion_id": self.completion_id,
            "previous_bundle_id": self.previous_bundle_id,
            "activated_by": self.activated_by,
            "activated_via_client_id": self.activated_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "activated_at": self.activated_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"activation_id": self.activation_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class IndexStaleMark:
    stale_mark_id: str
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    manifest_id: str
    bundle_id: str
    reason: Literal[
        "source_advanced",
        "policy_changed",
        "retention_purge",
        "provider_rotated",
        "manual",
    ]
    marked_by: str
    marked_via_client_id: str
    authorization_event_id: str
    marked_at: str
    contract_version: str = "tbm.index-stale-mark.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.index-stale-mark.v1":
            _record_invalid("index stale mark version is unsupported")
        if (
            type(self.stale_mark_id) is not str
            or _STALE_ID_RE.fullmatch(self.stale_mark_id) is None
        ):
            _record_invalid("stale_mark_id is invalid")
        _target_partition(self)
        for value, pattern, name in (
            (self.manifest_id, _MANIFEST_ID_RE, "manifest_id"),
            (self.bundle_id, _BUNDLE_ID_RE, "bundle_id"),
        ):
            if type(value) is not str or pattern.fullmatch(value) is None:
                _record_invalid(f"{name} is invalid")
        if self.reason not in _STALE_REASONS:
            _record_invalid("stale reason is unsupported")
        for name in (
            "marked_by",
            "marked_via_client_id",
            "authorization_event_id",
        ):
            _identifier(getattr(self, name), name)
        _timestamp(self.marked_at, "marked_at")
        if self.stale_mark_id != _content_id(
            "index_stale_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_HASH_MISMATCH",
                "stale_mark_id does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "manifest_id": self.manifest_id,
            "bundle_id": self.bundle_id,
            "reason": self.reason,
            "marked_by": self.marked_by,
            "marked_via_client_id": self.marked_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "marked_at": self.marked_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"stale_mark_id": self.stale_mark_id, **self._unsigned_dict()}


RetrievalIndexRecord = (
    IndexBuildRequest | IndexBuildCompletion | IndexActivation | IndexStaleMark
)


@dataclass(frozen=True)
class StoredRetrievalIndexRecord:
    record: RetrievalIndexRecord
    policy: AuthorizationPolicyBundle
    request: AuthorizationRequest
    decision: AuthorizationDecision
    attestation_verified_by: str

    def __post_init__(self) -> None:
        if type(self.record) not in {
            IndexBuildRequest,
            IndexBuildCompletion,
            IndexActivation,
            IndexStaleMark,
        }:
            _record_invalid("stored retrieval index record is invalid")
        if (
            type(self.policy) is not AuthorizationPolicyBundle
            or type(self.request) is not AuthorizationRequest
            or type(self.decision) is not AuthorizationDecision
        ):
            _record_invalid("stored authorization records are invalid")
        _identifier(self.attestation_verified_by, "attestation_verified_by")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": "tbm.stored-retrieval-index-record.v1",
            "record_type": _record_type(self.record),
            "record": self.record.to_dict(),
            "policy": self.policy.to_dict(),
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "attestation_verified_by": self.attestation_verified_by,
        }


@dataclass(frozen=True)
class ProjectedRetrievalIndexRecord:
    stored_record: StoredRetrievalIndexRecord
    source_event_sha256: str
    global_position: int

    def __post_init__(self) -> None:
        if type(self.stored_record) is not StoredRetrievalIndexRecord:
            _projection_invalid("projected stored record is invalid")
        _digest(self.source_event_sha256, "source_event_sha256")
        if type(self.global_position) is not int or self.global_position < 1:
            _projection_invalid("projected global position is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "stored_record": self.stored_record.to_dict(),
            "source_event_sha256": self.source_event_sha256,
            "global_position": self.global_position,
        }


def build_index_build_request(
    *,
    partition: LedgerTenantPartition,
    source_event_watermark: int,
    source_event_sha256: str,
    source_catalog_sha256: str,
    retriever_id: str,
    retriever_version: str,
    requested_by: str,
    requested_via_client_id: str,
    authorization_event_id: str,
    requested_at: str,
) -> IndexBuildRequest:
    if type(partition) is not LedgerTenantPartition:
        _record_invalid("partition must be LedgerTenantPartition")
    canonical_requested_at = _timestamp(requested_at, "requested_at")
    values: dict[str, object] = {
        "contract_version": "tbm.index-build-request.v1",
        "organization_id": partition.organization_id,
        "tenant_id": partition.tenant_id,
        "repository_id": partition.repository_id,
        "environment_id": partition.environment_id,
        "source_event_watermark": source_event_watermark,
        "source_event_sha256": source_event_sha256,
        "source_catalog_sha256": source_catalog_sha256,
        "retriever_id": retriever_id,
        "retriever_version": retriever_version,
        "requested_by": requested_by,
        "requested_via_client_id": requested_via_client_id,
        "authorization_event_id": authorization_event_id,
        "requested_at": canonical_requested_at,
    }
    return IndexBuildRequest(
        build_request_id=_content_id("index_build_request_sha256_", values),
        organization_id=partition.organization_id,
        tenant_id=partition.tenant_id,
        repository_id=partition.repository_id,
        environment_id=partition.environment_id,
        source_event_watermark=source_event_watermark,
        source_event_sha256=source_event_sha256,
        source_catalog_sha256=source_catalog_sha256,
        retriever_id=retriever_id,
        retriever_version=retriever_version,
        requested_by=requested_by,
        requested_via_client_id=requested_via_client_id,
        authorization_event_id=authorization_event_id,
        requested_at=canonical_requested_at,
    )


def build_index_build_completion(
    *,
    build_request: IndexBuildRequest,
    manifest: RetrievalIndexManifest,
    completed_by: str,
    completed_via_client_id: str,
    authorization_event_id: str,
    completed_at: str,
) -> IndexBuildCompletion:
    if type(build_request) is not IndexBuildRequest:
        _record_invalid("build_request must be IndexBuildRequest")
    if type(manifest) is not RetrievalIndexManifest:
        _record_invalid("manifest must be RetrievalIndexManifest")
    if (
        _target_partition(build_request) != _target_partition(manifest)
        or build_request.source_event_watermark
        != manifest.source_event_watermark
        or build_request.source_event_sha256 != manifest.source_event_sha256
        or build_request.source_catalog_sha256
        != manifest.source_catalog_sha256
        or build_request.retriever_id != manifest.retriever_id
        or build_request.retriever_version != manifest.retriever_version
    ):
        _record_invalid("manifest does not satisfy the build request")
    canonical_completed_at = _timestamp(completed_at, "completed_at")
    values: dict[str, object] = {
        "contract_version": "tbm.index-build-completion.v1",
        "build_request_id": build_request.build_request_id,
        "manifest": manifest.to_dict(),
        "completed_by": completed_by,
        "completed_via_client_id": completed_via_client_id,
        "authorization_event_id": authorization_event_id,
        "completed_at": canonical_completed_at,
    }
    return IndexBuildCompletion(
        completion_id=_content_id("index_build_completion_sha256_", values),
        build_request_id=build_request.build_request_id,
        manifest=manifest,
        completed_by=completed_by,
        completed_via_client_id=completed_via_client_id,
        authorization_event_id=authorization_event_id,
        completed_at=canonical_completed_at,
    )


def build_index_activation(
    *,
    completion: IndexBuildCompletion,
    previous_bundle_id: str | None,
    activated_by: str,
    activated_via_client_id: str,
    authorization_event_id: str,
    activated_at: str,
) -> IndexActivation:
    if type(completion) is not IndexBuildCompletion:
        _record_invalid("completion must be IndexBuildCompletion")
    manifest = completion.manifest
    canonical_activated_at = _timestamp(activated_at, "activated_at")
    values: dict[str, object] = {
        "contract_version": "tbm.index-activation.v1",
        "organization_id": manifest.organization_id,
        "tenant_id": manifest.tenant_id,
        "repository_id": manifest.repository_id,
        "environment_id": manifest.environment_id,
        "manifest_id": manifest.manifest_id,
        "bundle_id": manifest.bundle_id,
        "completion_id": completion.completion_id,
        "previous_bundle_id": previous_bundle_id,
        "activated_by": activated_by,
        "activated_via_client_id": activated_via_client_id,
        "authorization_event_id": authorization_event_id,
        "activated_at": canonical_activated_at,
    }
    return IndexActivation(
        activation_id=_content_id("index_activation_sha256_", values),
        organization_id=manifest.organization_id,
        tenant_id=manifest.tenant_id,
        repository_id=manifest.repository_id,
        environment_id=manifest.environment_id,
        manifest_id=manifest.manifest_id,
        bundle_id=manifest.bundle_id,
        completion_id=completion.completion_id,
        previous_bundle_id=previous_bundle_id,
        activated_by=activated_by,
        activated_via_client_id=activated_via_client_id,
        authorization_event_id=authorization_event_id,
        activated_at=canonical_activated_at,
    )


def build_index_stale_mark(
    *,
    activation: IndexActivation,
    reason: Literal[
        "source_advanced",
        "policy_changed",
        "retention_purge",
        "provider_rotated",
        "manual",
    ],
    marked_by: str,
    marked_via_client_id: str,
    authorization_event_id: str,
    marked_at: str,
) -> IndexStaleMark:
    if type(activation) is not IndexActivation:
        _record_invalid("activation must be IndexActivation")
    canonical_marked_at = _timestamp(marked_at, "marked_at")
    values: dict[str, object] = {
        "contract_version": "tbm.index-stale-mark.v1",
        "organization_id": activation.organization_id,
        "tenant_id": activation.tenant_id,
        "repository_id": activation.repository_id,
        "environment_id": activation.environment_id,
        "manifest_id": activation.manifest_id,
        "bundle_id": activation.bundle_id,
        "reason": reason,
        "marked_by": marked_by,
        "marked_via_client_id": marked_via_client_id,
        "authorization_event_id": authorization_event_id,
        "marked_at": canonical_marked_at,
    }
    return IndexStaleMark(
        stale_mark_id=_content_id("index_stale_sha256_", values),
        organization_id=activation.organization_id,
        tenant_id=activation.tenant_id,
        repository_id=activation.repository_id,
        environment_id=activation.environment_id,
        manifest_id=activation.manifest_id,
        bundle_id=activation.bundle_id,
        reason=reason,
        marked_by=marked_by,
        marked_via_client_id=marked_via_client_id,
        authorization_event_id=authorization_event_id,
        marked_at=canonical_marked_at,
    )


@dataclass(frozen=True)
class RetrievalIndexHead:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    bundle_id: str
    manifest_id: str
    completion_id: str
    activation_id: str
    previous_bundle_id: str | None
    index_versions: tuple[IndexVersion, ...]
    source_event_watermark: int
    source_event_sha256: str
    stale: bool
    stale_reason: str | None
    stale_mark_id: str | None
    completion_authorization_event_id: str
    activation_authorization_event_id: str
    completion_attestation_verified_by: str
    activation_attestation_verified_by: str
    activated_by: str
    activated_at: str
    status_event_sha256: str
    head_sha256: str
    contract_version: str = "tbm.retrieval-index-head.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.retrieval-index-head.v1":
            _projection_invalid("retrieval index head version is unsupported")
        _target_partition(self)
        for value, pattern, name in (
            (self.bundle_id, _BUNDLE_ID_RE, "bundle_id"),
            (self.manifest_id, _MANIFEST_ID_RE, "manifest_id"),
            (self.completion_id, _COMPLETION_ID_RE, "completion_id"),
            (self.activation_id, _ACTIVATION_ID_RE, "activation_id"),
        ):
            if type(value) is not str or pattern.fullmatch(value) is None:
                _projection_invalid(f"{name} is invalid")
        if self.previous_bundle_id is not None and (
            type(self.previous_bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.previous_bundle_id) is None
        ):
            _projection_invalid("previous_bundle_id is invalid")
        _index_versions(self.index_versions, projection=True)
        if (
            type(self.source_event_watermark) is not int
            or self.source_event_watermark < 1
        ):
            _projection_invalid("source_event_watermark is invalid")
        _digest(self.source_event_sha256, "source_event_sha256")
        if type(self.stale) is not bool:
            _projection_invalid("stale must be a boolean")
        if self.stale:
            if (
                self.stale_reason not in _STALE_REASONS
                or type(self.stale_mark_id) is not str
                or _STALE_ID_RE.fullmatch(self.stale_mark_id) is None
            ):
                _projection_invalid("stale head metadata is invalid")
        elif self.stale_reason is not None or self.stale_mark_id is not None:
            _projection_invalid("fresh head cannot retain stale metadata")
        for name in (
            "completion_authorization_event_id",
            "activation_authorization_event_id",
            "completion_attestation_verified_by",
            "activation_attestation_verified_by",
            "activated_by",
        ):
            _identifier(getattr(self, name), name)
        _timestamp(self.activated_at, "activated_at")
        _digest(self.status_event_sha256, "status_event_sha256")
        _digest(self.head_sha256, "head_sha256")
        if self.head_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("retrieval index head digest does not match")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "bundle_id": self.bundle_id,
            "manifest_id": self.manifest_id,
            "completion_id": self.completion_id,
            "activation_id": self.activation_id,
            "previous_bundle_id": self.previous_bundle_id,
            "index_versions": [item.to_dict() for item in self.index_versions],
            "source_event_watermark": self.source_event_watermark,
            "source_event_sha256": self.source_event_sha256,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "stale_mark_id": self.stale_mark_id,
            "completion_authorization_event_id": (
                self.completion_authorization_event_id
            ),
            "activation_authorization_event_id": (
                self.activation_authorization_event_id
            ),
            "completion_attestation_verified_by": (
                self.completion_attestation_verified_by
            ),
            "activation_attestation_verified_by": (
                self.activation_attestation_verified_by
            ),
            "activated_by": self.activated_by,
            "activated_at": self.activated_at,
            "status_event_sha256": self.status_event_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._unsigned_dict(), "head_sha256": self.head_sha256}


@dataclass(frozen=True)
class RetrievalIndexProjection:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    records: tuple[ProjectedRetrievalIndexRecord, ...]
    active_head: RetrievalIndexHead | None
    last_event_sha256: str
    last_global_position: int

    def __post_init__(self) -> None:
        partition = _target_partition(self)
        if (
            type(self.records) is not tuple
            or not self.records
            or any(type(item) is not ProjectedRetrievalIndexRecord for item in self.records)
        ):
            _projection_invalid("projection records are invalid")
        positions = tuple(item.global_position for item in self.records)
        if positions != tuple(sorted(set(positions))):
            _projection_invalid(
                "projection record positions must be strictly ordered"
            )
        if len({item.source_event_sha256 for item in self.records}) != len(
            self.records
        ):
            _projection_invalid("projection record events are duplicated")
        for item in self.records:
            if _record_partition(item.stored_record.record) != partition:
                _projection_invalid("projection record crossed its partition")
        if self.last_event_sha256 != self.records[-1].source_event_sha256:
            _projection_invalid("projection last event does not match records")
        if self.last_global_position != self.records[-1].global_position:
            _projection_invalid("projection last position does not match records")
        if self.active_head is not None:
            if type(self.active_head) is not RetrievalIndexHead:
                _projection_invalid("active head is invalid")
            if _target_partition(self.active_head) != partition:
                _projection_invalid("active head crossed its partition")
            _verify_projection_head(self.records, self.active_head)

    def load_active_head(self) -> RetrievalIndexHead:
        if self.active_head is None:
            _fail(
                "TBM_RETRIEVAL_INDEX_HEAD_MISSING",
                "retrieval index projection has no active head",
            )
        return self.active_head

    def verify_head(self, head: RetrievalIndexHead) -> None:
        if type(head) is not RetrievalIndexHead or self.active_head != head:
            _fail(
                "TBM_RETRIEVAL_INDEX_HEAD_SUPERSEDED",
                "retrieval index head changed during use",
            )

    def load_manifest(self, manifest_id: str) -> RetrievalIndexManifest:
        for projected in self.records:
            record = projected.stored_record.record
            if (
                type(record) is IndexBuildCompletion
                and record.manifest.manifest_id == manifest_id
            ):
                return record.manifest
        _fail(
            "TBM_RETRIEVAL_INDEX_MANIFEST_MISSING",
            "active retrieval index manifest is not retained",
        )


@dataclass(frozen=True)
class DurableRetrievalIndexSnapshot:
    projection: RetrievalIndexProjection
    partition_sha256: str
    reducer_descriptor_sha256: str
    reducer_configuration_sha256: str
    stream_version: int
    source_event_count: int
    snapshot_sha256: str
    contract_version: str = "tbm.durable-retrieval-index-snapshot.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.durable-retrieval-index-snapshot.v1":
            _projection_invalid("durable snapshot version is unsupported")
        if type(self.projection) is not RetrievalIndexProjection:
            _projection_invalid("durable snapshot projection is invalid")
        for name in (
            "partition_sha256",
            "reducer_descriptor_sha256",
            "reducer_configuration_sha256",
            "snapshot_sha256",
        ):
            _digest(getattr(self, name), name)
        if self.partition_sha256 != _target_partition(
            self.projection
        ).partition_sha256:
            _projection_invalid("durable snapshot partition is mismatched")
        if (
            type(self.stream_version) is not int
            or self.stream_version < 1
            or type(self.source_event_count) is not int
            or self.source_event_count != self.stream_version
            or len(self.projection.records) != self.stream_version
        ):
            _projection_invalid("durable snapshot event count is invalid")
        if self.snapshot_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("durable snapshot digest does not match")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "partition_sha256": self.partition_sha256,
            "reducer_descriptor_sha256": self.reducer_descriptor_sha256,
            "reducer_configuration_sha256": self.reducer_configuration_sha256,
            "stream_version": self.stream_version,
            "source_event_count": self.source_event_count,
            "projection": _projection_digest_value(self.projection),
        }

    def load_active_head(self) -> RetrievalIndexHead:
        return self.projection.load_active_head()

    def verify_head(self, head: RetrievalIndexHead) -> None:
        self.projection.verify_head(head)

    def load_manifest(self, manifest_id: str) -> RetrievalIndexManifest:
        return self.projection.load_manifest(manifest_id)


@dataclass(frozen=True)
class RetrievalIndexAppendResult:
    receipt: LedgerAppendReceipt
    projection: RetrievalIndexProjection

    def __post_init__(self) -> None:
        if (
            type(self.receipt) is not LedgerAppendReceipt
            or type(self.projection) is not RetrievalIndexProjection
        ):
            _projection_invalid("retrieval index append result is invalid")


class RetrievalIndexHeadReader(Protocol):
    def load_active_head(self) -> RetrievalIndexHead: ...

    def verify_head(self, head: RetrievalIndexHead) -> None: ...

    def load_manifest(self, manifest_id: str) -> RetrievalIndexManifest: ...


class EventManagedIndexRepository:
    """Read-only managed-index view selected by the event-derived active head."""

    def __init__(
        self,
        repository: ManagedIndexRepository,
        head_reader: RetrievalIndexHeadReader,
    ) -> None:
        if not all(
            callable(getattr(repository, name, None))
            for name in ("publish", "load", "load_current")
        ):
            _record_invalid("repository must implement ManagedIndexRepository")
        if not all(
            callable(getattr(head_reader, name, None))
            for name in ("load_active_head", "verify_head", "load_manifest")
        ):
            _record_invalid("head_reader is invalid")
        self._repository = repository
        self._head_reader = head_reader

    def publish(
        self,
        bundle: ManagedIndexBundle,
        *,
        expected_current_bundle_id: str | None,
    ) -> ManagedIndexPublication:
        del bundle, expected_current_bundle_id
        _fail(
            "TBM_RETRIEVAL_INDEX_EVENT_REQUIRED",
            "event-selected managed index view is read-only",
        )

    def load(self, bundle_id: str) -> ManagedIndexBundle:
        head = self._head_reader.load_active_head()
        if head.stale or bundle_id != head.bundle_id:
            _fail(
                "TBM_RETRIEVAL_INDEX_HEAD_STALE",
                "only the fresh event-selected index bundle may be loaded",
            )
        bundle = self._repository.load(bundle_id)
        self._head_reader.load_manifest(head.manifest_id).verify_bundle(bundle)
        self._head_reader.verify_head(head)
        return bundle

    def load_current(
        self,
        *,
        tenant_id: str,
        repository_id: str,
        environment_id: str,
    ) -> ManagedIndexBundle:
        head = self._head_reader.load_active_head()
        if (
            head.stale
            or head.tenant_id != tenant_id
            or head.repository_id != repository_id
            or head.environment_id != environment_id
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_HEAD_STALE",
                "fresh event-selected index head does not match requested scope",
            )
        bundle = self._repository.load(head.bundle_id)
        self._head_reader.load_manifest(head.manifest_id).verify_bundle(bundle)
        self._head_reader.verify_head(head)
        return bundle


def retrieval_index_stream_id(partition: LedgerTenantPartition) -> str:
    if type(partition) is not LedgerTenantPartition:
        _record_invalid("partition must be LedgerTenantPartition")
    return "retrieval_index_" + partition.partition_sha256.removeprefix(
        "sha256:"
    )


def build_retrieval_index_event_batch(
    access: LedgerAccessContext,
    records: tuple[StoredRetrievalIndexRecord, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if type(access) is not LedgerAccessContext:
        _fail("TBM_RETRIEVAL_INDEX_ACCESS_INVALID", "access is invalid")
    if (
        type(records) is not tuple
        or not 1 <= len(records) <= RETRIEVAL_INDEX_EVENT_MAX_BATCH
        or any(type(item) is not StoredRetrievalIndexRecord for item in records)
    ):
        _fail(
            "TBM_RETRIEVAL_INDEX_BATCH_INVALID",
            "records must be a bounded non-empty tuple",
        )
    if type(expected_stream_version) is not int or expected_stream_version < 0:
        _fail(
            "TBM_RETRIEVAL_INDEX_BATCH_INVALID",
            "expected_stream_version is invalid",
        )
    if type(next_global_position) is not int or next_global_position < 1:
        _fail(
            "TBM_RETRIEVAL_INDEX_BATCH_INVALID",
            "next_global_position is invalid",
        )
    canonical_recorded_at = _timestamp(recorded_at, "recorded_at")
    descriptors = tuple(_record_descriptor(record) for record in records)
    stream_id = retrieval_index_stream_id(access.partition)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_RETRIEVAL_INDEX_BATCH_INVALID",
                "nonzero stream version requires its parent",
            )
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail(
            "TBM_RETRIEVAL_INDEX_BATCH_INVALID",
            "previous event does not match the retrieval-index head",
        )
    command_value = {
        "protocol_version": RETRIEVAL_INDEX_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "records": [item[3] for item in descriptors],
    }
    command_sha256 = _domain_sha256(
        b"tbm.retrieval-index-event-command.v1\x00", command_value
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=_domain_sha256(
            b"tbm.retrieval-index-event-idempotency.v1\x00", command_value
        ),
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    parent = previous_event
    events: list[CanonicalEvent] = []
    for offset, descriptor in enumerate(descriptors):
        (
            event_type,
            subject_id,
            occurred_at,
            record_dict,
            actor_id,
            client_id,
            authorization_event_id,
            partition,
        ) = descriptor
        _verify_record_access(
            access,
            partition=partition,
            actor_id=actor_id,
            client_id=client_id,
            authorization_event_id=authorization_event_id,
        )
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        payload = {
            "subject_id": subject_id,
            "record_type": event_type,
            "record_sha256": canonical_sha256(record_dict),
            "record_json": _canonical_json(record_dict),
        }
        event = build_canonical_event(
            event_id="evt_ri_" + event_digest,
            event_type=event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=RETRIEVAL_INDEX_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_ri_" + event_digest[:32],
            idempotency_key_sha256=idempotency.idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_ri_" + stream_id[-32:],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=occurred_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_retrieval_index_runtime",
            producer_version="f4-v1",
            payload_schema=_PAYLOAD_SCHEMAS[event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification="internal",
            retention_policy_id="retention_retrieval_index_events",
            artifact_refs=(),
            payload=payload,
        )
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_retrieval_index_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    schemas = _payload_json_schemas()
    for event_type in RETRIEVAL_INDEX_EVENT_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="domain",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=schemas[event_type],
            )
        )
    return registry.seal()


def retrieval_index_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_retrieval_index_event_registry().dispatch_schema()
    schema["$id"] = RETRIEVAL_INDEX_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory retrieval-index event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed retrieval-index event registry; exact "
        "authorization records and manifests are reverified during replay."
    )
    return schema


def dumps_retrieval_index_event_payload_dispatch_schema() -> str:
    return json.dumps(
        retrieval_index_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_retrieval_index_reducer(
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
    trusted_embedding_provider_models: tuple[tuple[str, str], ...],
) -> FunctionalReducer:
    trusted_verifiers = _trusted_verifier_set(
        trusted_attestation_verifier_ids
    )
    trusted_embeddings = _trusted_embedding_set(
        trusted_embedding_provider_models
    )
    descriptor = ReducerDescriptor(
        reducer_id=RETRIEVAL_INDEX_EVENT_REDUCER_ID,
        reducer_version=1,
        input_event_types=RETRIEVAL_INDEX_EVENT_TYPES,
        output_projection=RETRIEVAL_INDEX_EVENT_PROJECTION,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "retrieval-index-current",
                "algorithm_version": 1,
                "event_types": list(RETRIEVAL_INDEX_EVENT_TYPES),
                "head_source": INDEX_ACTIVATED,
                "stale_source": INDEX_MARKED_STALE,
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {
                "configuration": "trusted-attestation-verifiers",
                "trusted_attestation_verifier_ids": sorted(trusted_verifiers),
                "trusted_embedding_provider_models": [
                    list(value) for value in sorted(trusted_embeddings)
                ],
                "version": 1,
            },
        ),
        target_event_versions={
            event_type: 1 for event_type in RETRIEVAL_INDEX_EVENT_TYPES
        },
    )

    def initial() -> Mapping[str, object]:
        return {
            "organization_id": None,
            "tenant_id": None,
            "repository_id": None,
            "environment_id": None,
            "records": [],
            "head": None,
            "last_event_sha256": None,
            "last_global_position": 0,
        }

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        payload = _typed_payload(reducer_event)
        event = reducer_event.source_event
        partition = _event_partition(event)
        for name in (
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
        ):
            if state.get(name) not in {None, getattr(partition, name)}:
                _transition_invalid("retrieval-index stream crossed a partition")
        records_state = _state_list(state, "records")
        head = _optional_state_mapping(state.get("head"), "head")
        stored = _load_stored_record(
            event.event_type, cast(str, payload["record_json"])
        )
        _verify_loaded_record(payload, stored, event)
        descriptor = _stored_record_fields(stored)
        _verify_stored_authorization(
            stored,
            permission=descriptor[4],
            actor_id=descriptor[1],
            client_id=descriptor[2],
            authorization_event_id=descriptor[3],
            occurred_at=descriptor[0],
            event=event,
        )
        if stored.attestation_verified_by not in trusted_verifiers:
            _transition_invalid("record attestation verifier is not trusted")
        if type(stored.record) is IndexBuildCompletion and (
            stored.record.manifest.embedding_provider_id,
            stored.record.manifest.embedding_model_id,
        ) not in trusted_embeddings:
            _transition_invalid("embedding provider/model is not trusted")
        projected_records = tuple(
            _parse_projected_record(cast(Mapping[str, object], item))
            for item in records_state
            if isinstance(item, Mapping)
        )
        if len(projected_records) != len(records_state):
            _projection_invalid("retrieval-index reducer records are invalid")
        _apply_transition_checks(projected_records, head, stored)
        projected = ProjectedRetrievalIndexRecord(
            stored_record=stored,
            source_event_sha256=event.event_sha256,
            global_position=event.global_position,
        )
        records_state.append(projected.to_dict())
        if event.event_type == INDEX_ACTIVATED:
            head = _head_state(
                projected_records, stored, event.event_sha256
            )
        elif event.event_type == INDEX_MARKED_STALE:
            if head is None:  # pragma: no cover - guarded above
                raise AssertionError("stale transition requires a head")
            stale = cast(IndexStaleMark, stored.record)
            values = {
                **head,
                "stale": True,
                "stale_reason": stale.reason,
                "stale_mark_id": stale.stale_mark_id,
                "status_event_sha256": event.event_sha256,
            }
            values.pop("head_sha256", None)
            head = {**values, "head_sha256": canonical_sha256(values)}
        return {
            "organization_id": partition.organization_id,
            "tenant_id": partition.tenant_id,
            "repository_id": partition.repository_id,
            "environment_id": partition.environment_id,
            "records": records_state,
            "head": head,
            "last_event_sha256": event.event_sha256,
            "last_global_position": event.global_position,
        }

    return FunctionalReducer(descriptor, initial, transition)


def reduce_retrieval_index_events(
    events: tuple[CanonicalEvent, ...],
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
    trusted_embedding_provider_models: tuple[tuple[str, str], ...],
    event_registry: EventTypeRegistry | None = None,
) -> RetrievalIndexProjection:
    if (
        type(events) is not tuple
        or not events
        or len(events) > RETRIEVAL_INDEX_EVENT_MAX_STREAM_EVENTS
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _fail(
            "TBM_RETRIEVAL_INDEX_EVENT_SEQUENCE_INVALID",
            "events must be a bounded non-empty CanonicalEvent tuple",
        )
    registry = (
        build_retrieval_index_event_registry()
        if event_registry is None
        else event_registry
    )
    if type(registry) is not EventTypeRegistry or not registry.sealed:
        _fail(
            "TBM_RETRIEVAL_INDEX_EVENT_REGISTRY_INVALID",
            "event registry must be sealed",
        )
    reducer = build_retrieval_index_reducer(
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
        trusted_embedding_provider_models=trusted_embedding_provider_models,
    )
    state = initial_reducer_state(reducer)
    parent: CanonicalEvent | None = None
    stream_id = events[0].stream_id
    for event in events:
        try:
            verify_event_parent(event, parent)
        except ValueError as error:
            raise RetrievalIndexEventV1Error(
                "TBM_RETRIEVAL_INDEX_EVENT_SEQUENCE_INVALID",
                "retrieval-index event chain is invalid",
            ) from error
        if (
            event.stream_type != RETRIEVAL_INDEX_EVENT_STREAM_TYPE
            or event.stream_id != stream_id
            or event.event_type not in RETRIEVAL_INDEX_EVENT_TYPES
            or event.classification != "internal"
            or event.producer != "tbm_retrieval_index_runtime"
            or event.producer_version != "f4-v1"
            or event.retention_policy_id != "retention_retrieval_index_events"
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_EVENT_SEQUENCE_INVALID",
                "retrieval-index event envelope is invalid",
            )
        if event.stream_id != retrieval_index_stream_id(_event_partition(event)):
            _fail(
                "TBM_RETRIEVAL_INDEX_EVENT_SEQUENCE_INVALID",
                "retrieval-index stream does not match its partition",
            )
        typed = registry.consume(event, target_version=1)
        state = execute_reducer_step(
            reducer,
            state.state,
            ReducerEvent(event, typed),
        )
        parent = event
    return _hydrate_projection(state.state)


def append_retrieval_index_records(
    ledger: EventLedgerPort,
    records: tuple[StoredRetrievalIndexRecord, ...],
    *,
    recorded_at: str,
    trusted_attestation_verifier_ids: tuple[str, ...],
    trusted_embedding_provider_models: tuple[tuple[str, str], ...],
) -> RetrievalIndexAppendResult:
    access = _require_ledger(ledger)
    if type(records) is not tuple or not records:
        _fail(
            "TBM_RETRIEVAL_INDEX_BATCH_INVALID",
            "records must be a non-empty tuple",
        )
    stream_id = retrieval_index_stream_id(access.partition)
    retained = _read_retrieval_index_stream(ledger, stream_id, allow_empty=True)
    if retained:
        _verify_retained_stream(ledger, stream_id, retained)
    expected_version = len(retained)
    parent = None if not retained else retained[-1]
    events: tuple[CanonicalEvent, ...] | None = None
    idempotency: LedgerIdempotency | None = None
    predicted: RetrievalIndexProjection | None = None
    for attempt in range(8):
        high_watermark = ledger.read_global(
            after_position=0, limit=1
        ).high_watermark_global_position
        events, idempotency = build_retrieval_index_event_batch(
            access,
            records,
            expected_stream_version=expected_version,
            next_global_position=high_watermark + 1,
            previous_event=parent,
            recorded_at=recorded_at,
        )
        predicted = reduce_retrieval_index_events(
            (*retained, *events),
            trusted_attestation_verifier_ids=(
                trusted_attestation_verifier_ids
            ),
            trusted_embedding_provider_models=(
                trusted_embedding_provider_models
            ),
        )
        try:
            receipt = ledger.append(
                stream_id,
                expected_version,
                events,
                idempotency,
            )
            break
        except EventLedgerConflictError as error:
            if (
                error.code != "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"
                or attempt == 7
            ):
                raise
    else:  # pragma: no cover
        raise AssertionError("retrieval-index append retry did not terminate")
    request = LedgerAppendRequest(
        access=access,
        stream_id=stream_id,
        expected_stream_version=expected_version,
        events=cast(tuple[CanonicalEvent, ...], events),
        idempotency=cast(LedgerIdempotency, idempotency),
    )
    verify_ledger_append_receipt(request, receipt)
    durable_events = _read_retrieval_index_stream(ledger, stream_id)
    _verify_retained_stream(ledger, stream_id, durable_events)
    rebuilt = reduce_retrieval_index_events(
        durable_events,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
        trusted_embedding_provider_models=trusted_embedding_provider_models,
    )
    if rebuilt != predicted:
        _fail(
            "TBM_RETRIEVAL_INDEX_PROJECTION_MISMATCH",
            "durable retrieval-index replay differs from predicted projection",
        )
    return RetrievalIndexAppendResult(receipt=receipt, projection=rebuilt)


def rebuild_retrieval_index_from_ledger(
    ledger: EventLedgerPort,
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
    trusted_embedding_provider_models: tuple[tuple[str, str], ...],
) -> DurableRetrievalIndexSnapshot:
    access = _require_ledger(ledger)
    stream_id = retrieval_index_stream_id(access.partition)
    events = _read_retrieval_index_stream(ledger, stream_id)
    _verify_retained_stream(ledger, stream_id, events)
    projection = reduce_retrieval_index_events(
        events,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
        trusted_embedding_provider_models=trusted_embedding_provider_models,
    )
    repeated = _read_retrieval_index_stream(ledger, stream_id)
    if repeated != events:
        _fail(
            "TBM_RETRIEVAL_INDEX_REBUILD_SUPERSEDED",
            "retrieval-index stream changed during rebuild",
        )
    _verify_retained_stream(ledger, stream_id, repeated)
    descriptor = build_retrieval_index_reducer(
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
        trusted_embedding_provider_models=trusted_embedding_provider_models,
    ).descriptor
    values = {
        "contract_version": "tbm.durable-retrieval-index-snapshot.v1",
        "partition_sha256": access.partition.partition_sha256,
        "reducer_descriptor_sha256": descriptor.descriptor_sha256,
        "reducer_configuration_sha256": descriptor.configuration_sha256,
        "stream_version": len(events),
        "source_event_count": len(events),
        "projection": _projection_digest_value(projection),
    }
    return DurableRetrievalIndexSnapshot(
        projection=projection,
        partition_sha256=access.partition.partition_sha256,
        reducer_descriptor_sha256=descriptor.descriptor_sha256,
        reducer_configuration_sha256=descriptor.configuration_sha256,
        stream_version=len(events),
        source_event_count=len(events),
        snapshot_sha256=canonical_sha256(values),
    )


def dumps_retrieval_index_manifest(manifest: RetrievalIndexManifest) -> str:
    if type(manifest) is not RetrievalIndexManifest:
        _record_invalid("manifest must be RetrievalIndexManifest")
    return _canonical_json(manifest.to_dict())


def loads_retrieval_index_manifest(
    document: str | bytes,
) -> RetrievalIndexManifest:
    return _parse_manifest(
        _loads_record(document, "retrieval index manifest")
    )


def dumps_stored_retrieval_index_record(
    stored: StoredRetrievalIndexRecord,
) -> str:
    if type(stored) is not StoredRetrievalIndexRecord:
        _record_invalid("stored record must be StoredRetrievalIndexRecord")
    return _canonical_json(stored.to_dict())


def loads_stored_retrieval_index_record(
    document: str | bytes,
) -> StoredRetrievalIndexRecord:
    return _parse_stored_record(
        _loads_record(document, "stored retrieval index record")
    )


def retrieval_index_manifest_schema() -> dict[str, object]:
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    index_version = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "index_kind",
            "index_id",
            "index_version",
            "content_sha256",
        ],
        "properties": {
            "index_kind": {"enum": list(_INDEX_KINDS)},
            "index_id": identifier,
            "index_version": identifier,
            "content_sha256": digest,
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RETRIEVAL_INDEX_MANIFEST_SCHEMA_ID,
        "title": "Trace-backed Memory retrieval index manifest v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "manifest_id",
            "contract_version",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "bundle_id",
            "retriever_id",
            "retriever_version",
            "index_versions",
            "source_event_watermark",
            "source_event_sha256",
            "source_catalog_sha256",
            "memory_revision_ids",
            "embedding_provider_id",
            "embedding_model_id",
            "lexical_tokenizer_id",
            "lexical_tokenizer_version",
            "git_graph_version",
            "build_sha256",
            "stale_status",
        ],
        "properties": {
            "manifest_id": {
                "type": "string",
                "pattern": (
                    r"^retrieval_index_manifest_sha256_[0-9a-f]{64}$"
                ),
            },
            "contract_version": {
                "const": "tbm.retrieval-index-manifest.v1"
            },
            "organization_id": identifier,
            "tenant_id": identifier,
            "repository_id": identifier,
            "environment_id": identifier,
            "bundle_id": {
                "type": "string",
                "pattern": r"^managed_index_bundle_sha256_[0-9a-f]{64}$",
            },
            "retriever_id": identifier,
            "retriever_version": identifier,
            "index_versions": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "uniqueItems": True,
                "items": index_version,
                "allOf": [
                    {
                        "contains": {
                            "properties": {"index_kind": {"const": kind}},
                            "required": ["index_kind"],
                        },
                        "minContains": 1,
                        "maxContains": 1,
                    }
                    for kind in _INDEX_KINDS
                ],
            },
            "source_event_watermark": {"type": "integer", "minimum": 1},
            "source_event_sha256": digest,
            "source_catalog_sha256": digest,
            "memory_revision_ids": {
                "type": "array",
                "maxItems": 1_000,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "pattern": r"^memory_revision_sha256_[0-9a-f]{64}$",
                },
            },
            "embedding_provider_id": identifier,
            "embedding_model_id": identifier,
            "lexical_tokenizer_id": identifier,
            "lexical_tokenizer_version": identifier,
            "git_graph_version": identifier,
            "build_sha256": digest,
            "stale_status": {"const": "fresh"},
        },
    }


def dumps_retrieval_index_manifest_schema() -> str:
    return json.dumps(
        retrieval_index_manifest_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _record_type(record: RetrievalIndexRecord) -> str:
    if type(record) is IndexBuildRequest:
        return INDEX_BUILD_REQUESTED
    if type(record) is IndexBuildCompletion:
        return INDEX_BUILD_COMPLETED
    if type(record) is IndexActivation:
        return INDEX_ACTIVATED
    if type(record) is IndexStaleMark:
        return INDEX_MARKED_STALE
    _record_invalid("retrieval index record type is unsupported")


def _record_partition(record: RetrievalIndexRecord) -> LedgerTenantPartition:
    if type(record) is IndexBuildCompletion:
        return _target_partition(record.manifest)
    return _target_partition(record)


def _record_subject_id(record: RetrievalIndexRecord) -> str:
    if type(record) is IndexBuildRequest:
        return record.build_request_id
    if type(record) is IndexBuildCompletion:
        return record.manifest.bundle_id
    if type(record) is IndexActivation:
        return record.bundle_id
    if type(record) is IndexStaleMark:
        return record.bundle_id
    _record_invalid("retrieval index record type is unsupported")


def _stored_record_fields(
    stored: StoredRetrievalIndexRecord,
) -> tuple[str, str, str, str, AuthorizationPermission]:
    record = stored.record
    if type(record) is IndexBuildRequest:
        return (
            record.requested_at,
            record.requested_by,
            record.requested_via_client_id,
            record.authorization_event_id,
            "memory:create",
        )
    if type(record) is IndexBuildCompletion:
        return (
            record.completed_at,
            record.completed_by,
            record.completed_via_client_id,
            record.authorization_event_id,
            "memory:create",
        )
    if type(record) is IndexActivation:
        return (
            record.activated_at,
            record.activated_by,
            record.activated_via_client_id,
            record.authorization_event_id,
            "memory:activate",
        )
    if type(record) is IndexStaleMark:
        return (
            record.marked_at,
            record.marked_by,
            record.marked_via_client_id,
            record.authorization_event_id,
            "memory:activate",
        )
    _record_invalid("retrieval index record type is unsupported")


def _record_descriptor(
    stored: StoredRetrievalIndexRecord,
) -> tuple[
    str,
    str,
    str,
    dict[str, object],
    str,
    str,
    str,
    LedgerTenantPartition,
]:
    if type(stored) is not StoredRetrievalIndexRecord:
        _record_invalid("stored retrieval index record is invalid")
    occurred_at, actor_id, client_id, authorization_event_id, _ = (
        _stored_record_fields(stored)
    )
    return (
        _record_type(stored.record),
        _record_subject_id(stored.record),
        occurred_at,
        stored.to_dict(),
        actor_id,
        client_id,
        authorization_event_id,
        _record_partition(stored.record),
    )


def _verify_record_access(
    access: LedgerAccessContext,
    *,
    partition: LedgerTenantPartition,
    actor_id: str,
    client_id: str,
    authorization_event_id: str,
) -> None:
    if access.partition != partition:
        _fail(
            "TBM_RETRIEVAL_INDEX_SCOPE_DENIED",
            "record target is outside the ledger partition",
        )
    if (
        access.actor_id != actor_id
        or access.actor_type != "principal"
        or access.principal_id != actor_id
        or access.agent_client_id != client_id
        or access.authorization_decision_id != authorization_event_id
    ):
        _fail(
            "TBM_RETRIEVAL_INDEX_ACTOR_MISMATCH",
            "record provenance does not match trusted ledger access",
        )
    if access.classification_filter.allowed != _ALL_CLASSIFICATIONS:
        _fail(
            "TBM_RETRIEVAL_INDEX_CLASSIFICATION_VIEW_INCOMPLETE",
            "retrieval-index operations require a complete classification view",
        )


def _typed_payload(reducer_event: ReducerEvent) -> dict[str, object]:
    typed = reducer_event.typed_event
    if typed is None:
        _fail(
            "TBM_RETRIEVAL_INDEX_TYPED_INPUT_REQUIRED",
            "retrieval-index reducer requires typed input",
        )
    payload = _thaw_json(typed.payload)
    if type(payload) is not dict:
        _transition_invalid("retrieval-index payload must be an object")
    if payload.get("record_type") != reducer_event.source_event.event_type:
        _transition_invalid("retrieval-index payload type does not match event")
    return cast(dict[str, object], payload)


def _load_stored_record(
    event_type: str, record_json: str
) -> StoredRetrievalIndexRecord:
    try:
        stored = loads_stored_retrieval_index_record(record_json)
    except ValueError as error:
        raise RetrievalIndexEventV1Error(
            "TBM_RETRIEVAL_INDEX_EVENT_RECORD_INVALID",
            "retrieval-index event contains an invalid exact record",
        ) from error
    if _record_type(stored.record) != event_type:
        _transition_invalid("stored retrieval-index record type is mismatched")
    return stored


def _verify_loaded_record(
    payload: Mapping[str, object],
    stored: StoredRetrievalIndexRecord,
    event: CanonicalEvent,
) -> None:
    descriptor = _record_descriptor(stored)
    if (
        payload.get("record_type") != descriptor[0]
        or payload.get("subject_id") != descriptor[1]
        or payload.get("record_sha256") != canonical_sha256(descriptor[3])
        or payload.get("record_json") != _canonical_json(descriptor[3])
        or event.occurred_at != descriptor[2]
        or event.actor_id != descriptor[4]
        or event.actor_type != "principal"
        or event.principal_id != descriptor[4]
        or event.agent_client_id != descriptor[5]
        or event.authorization_decision_id != descriptor[6]
        or _event_partition(event) != descriptor[7]
    ):
        _transition_invalid("retrieval-index record binding does not match event")


def _verify_stored_authorization(
    stored: StoredRetrievalIndexRecord,
    *,
    permission: AuthorizationPermission,
    actor_id: str,
    client_id: str,
    authorization_event_id: str,
    occurred_at: str,
    event: CanonicalEvent,
) -> None:
    try:
        verify_authorization_decision(
            stored.policy, stored.request, stored.decision
        )
    except ValueError as error:
        raise RetrievalIndexEventV1Error(
            "TBM_RETRIEVAL_INDEX_AUTHORIZATION_INVALID",
            "stored retrieval-index authorization is invalid",
        ) from error
    partition = _record_partition(stored.record)
    request = stored.request
    decision = stored.decision
    if (
        not decision.allowed
        or request.permission != permission
        or decision.permission != permission
        or request.tenant_id != partition.tenant_id
        or request.repository_reference is None
        or decision.tenant_id != partition.tenant_id
        or decision.repository_id != partition.repository_id
        or request.principal_id != actor_id
        or request.agent_client_id != client_id
        or decision.principal_id != actor_id
        or decision.agent_client_id != client_id
        or decision.authorization_event_id != authorization_event_id
        or event.authorization_decision_id != authorization_event_id
        or parse_rfc3339(decision.decided_at) > parse_rfc3339(occurred_at)
    ):
        _transition_invalid(
            "stored authorization does not match retrieval-index event"
        )


def _apply_transition_checks(
    projected_records: tuple[ProjectedRetrievalIndexRecord, ...],
    head_state: Mapping[str, object] | None,
    stored: StoredRetrievalIndexRecord,
) -> None:
    record = stored.record
    current_head = None if head_state is None else _parse_head(head_state)
    prior_records = tuple(item.stored_record.record for item in projected_records)
    if type(record) is IndexBuildRequest:
        if any(
            type(item) is IndexBuildRequest
            and item.build_request_id == record.build_request_id
            for item in prior_records
        ):
            _transition_invalid("index build request is duplicated")
        if (
            current_head is not None
            and record.source_event_watermark
            < current_head.source_event_watermark
        ):
            _transition_invalid("index build request rolls back source watermark")
        return
    if type(record) is IndexBuildCompletion:
        request = next(
            (
                item
                for item in prior_records
                if type(item) is IndexBuildRequest
                and item.build_request_id == record.build_request_id
            ),
            None,
        )
        if request is None:
            _transition_invalid("index build completion has no request")
        if any(
            type(item) is IndexBuildCompletion
            and (
                item.completion_id == record.completion_id
                or item.manifest.manifest_id == record.manifest.manifest_id
                or item.manifest.bundle_id == record.manifest.bundle_id
            )
            for item in prior_records
        ):
            _transition_invalid("index build completion is duplicated")
        request = cast(IndexBuildRequest, request)
        manifest = record.manifest
        if (
            _target_partition(request) != _target_partition(manifest)
            or request.source_event_watermark
            != manifest.source_event_watermark
            or request.source_event_sha256 != manifest.source_event_sha256
            or request.source_catalog_sha256
            != manifest.source_catalog_sha256
            or request.retriever_id != manifest.retriever_id
            or request.retriever_version != manifest.retriever_version
            or parse_rfc3339(record.completed_at)
            < parse_rfc3339(request.requested_at)
        ):
            _transition_invalid("index build completion mismatches its request")
        return
    if type(record) is IndexActivation:
        completion = next(
            (
                item
                for item in prior_records
                if type(item) is IndexBuildCompletion
                and item.completion_id == record.completion_id
            ),
            None,
        )
        if completion is None:
            _transition_invalid("activated index has no completion")
        completion = cast(IndexBuildCompletion, completion)
        if (
            record.manifest_id != completion.manifest.manifest_id
            or record.bundle_id != completion.manifest.bundle_id
            or record.activated_by == completion.completed_by
            or parse_rfc3339(record.activated_at)
            < parse_rfc3339(completion.completed_at)
        ):
            _transition_invalid("index activation mismatches its completion")
        current_bundle_id = (
            None if current_head is None else current_head.bundle_id
        )
        if record.previous_bundle_id != current_bundle_id:
            _transition_invalid("index activation predecessor is stale")
        if current_head is not None and (
            completion.manifest.source_event_watermark
            < current_head.source_event_watermark
            or parse_rfc3339(record.activated_at)
            <= parse_rfc3339(current_head.activated_at)
        ):
            _transition_invalid("index activation is not forward-only")
        if any(
            type(item) is IndexActivation
            and item.activation_id == record.activation_id
            for item in prior_records
        ):
            _transition_invalid("index activation is duplicated")
        return
    if type(record) is IndexStaleMark:
        if (
            current_head is None
            or current_head.stale
            or record.manifest_id != current_head.manifest_id
            or record.bundle_id != current_head.bundle_id
            or parse_rfc3339(record.marked_at)
            <= parse_rfc3339(current_head.activated_at)
        ):
            _transition_invalid("stale mark does not match the fresh active head")
        if any(
            type(item) is IndexStaleMark
            and item.stale_mark_id == record.stale_mark_id
            for item in prior_records
        ):
            _transition_invalid("index stale mark is duplicated")
        return
    _transition_invalid("retrieval index record type is unsupported")


def _head_state(
    projected_records: tuple[ProjectedRetrievalIndexRecord, ...],
    stored: StoredRetrievalIndexRecord,
    status_event_sha256: str,
) -> dict[str, object]:
    _digest(status_event_sha256, "status_event_sha256")
    activation = cast(IndexActivation, stored.record)
    completion_projected = next(
        (
            item
            for item in projected_records
            if type(item.stored_record.record) is IndexBuildCompletion
            and cast(
                IndexBuildCompletion, item.stored_record.record
            ).completion_id
            == activation.completion_id
        ),
        None,
    )
    if completion_projected is None:  # pragma: no cover - checked transition
        raise AssertionError("activation requires a retained completion")
    completion_stored = completion_projected.stored_record
    completion = cast(IndexBuildCompletion, completion_stored.record)
    manifest = completion.manifest
    values: dict[str, object] = {
        "contract_version": "tbm.retrieval-index-head.v1",
        "organization_id": activation.organization_id,
        "tenant_id": activation.tenant_id,
        "repository_id": activation.repository_id,
        "environment_id": activation.environment_id,
        "bundle_id": activation.bundle_id,
        "manifest_id": activation.manifest_id,
        "completion_id": activation.completion_id,
        "activation_id": activation.activation_id,
        "previous_bundle_id": activation.previous_bundle_id,
        "index_versions": [item.to_dict() for item in manifest.index_versions],
        "source_event_watermark": manifest.source_event_watermark,
        "source_event_sha256": manifest.source_event_sha256,
        "stale": False,
        "stale_reason": None,
        "stale_mark_id": None,
        "completion_authorization_event_id": completion.authorization_event_id,
        "activation_authorization_event_id": activation.authorization_event_id,
        "completion_attestation_verified_by": (
            completion_stored.attestation_verified_by
        ),
        "activation_attestation_verified_by": stored.attestation_verified_by,
        "activated_by": activation.activated_by,
        "activated_at": activation.activated_at,
        "status_event_sha256": status_event_sha256,
    }
    return {**values, "head_sha256": canonical_sha256(values)}


def _expected_head(
    records: tuple[ProjectedRetrievalIndexRecord, ...],
) -> RetrievalIndexHead | None:
    head: RetrievalIndexHead | None = None
    seen: list[ProjectedRetrievalIndexRecord] = []
    for projected in records:
        record = projected.stored_record.record
        if type(record) is IndexActivation:
            head = _parse_head(
                _head_state(
                    tuple(seen),
                    projected.stored_record,
                    projected.source_event_sha256,
                )
            )
        elif type(record) is IndexStaleMark:
            if head is None:  # pragma: no cover - invalid sequence elsewhere
                _projection_invalid("stale record precedes activation")
            values = {
                **head.to_dict(),
                "stale": True,
                "stale_reason": record.reason,
                "stale_mark_id": record.stale_mark_id,
                "status_event_sha256": projected.source_event_sha256,
            }
            values.pop("head_sha256")
            head = _parse_head(
                {**values, "head_sha256": canonical_sha256(values)}
            )
        seen.append(projected)
    return head


def _verify_projection_head(
    records: tuple[ProjectedRetrievalIndexRecord, ...],
    head: RetrievalIndexHead,
) -> None:
    if _expected_head(records) != head:
        _projection_invalid("active head does not match retained index events")


def _hydrate_projection(state: Mapping[str, object]) -> RetrievalIndexProjection:
    records_state = _state_list(state, "records")
    records = tuple(
        _parse_projected_record(cast(Mapping[str, object], item))
        for item in records_state
        if isinstance(item, Mapping)
    )
    if len(records) != len(records_state):
        _projection_invalid("retrieval-index projection records are invalid")
    head_state = _optional_state_mapping(state.get("head"), "head")
    return RetrievalIndexProjection(
        organization_id=cast(str, state.get("organization_id")),
        tenant_id=cast(str, state.get("tenant_id")),
        repository_id=cast(str, state.get("repository_id")),
        environment_id=cast(str, state.get("environment_id")),
        records=records,
        active_head=None if head_state is None else _parse_head(head_state),
        last_event_sha256=cast(str, state.get("last_event_sha256")),
        last_global_position=cast(int, state.get("last_global_position")),
    )


def _parse_stored_record(
    item: Mapping[str, object],
) -> StoredRetrievalIndexRecord:
    _require_fields(
        item,
        {
            "contract_version",
            "record_type",
            "record",
            "policy",
            "request",
            "decision",
            "attestation_verified_by",
        },
        "stored retrieval index record",
    )
    if _string(item, "contract_version") != "tbm.stored-retrieval-index-record.v1":
        _record_invalid("stored retrieval index record version is unsupported")
    record_type = _string(item, "record_type")
    record_mapping = _mapping(item, "record")
    if record_type == INDEX_BUILD_REQUESTED:
        record: RetrievalIndexRecord = _parse_build_request(record_mapping)
    elif record_type == INDEX_BUILD_COMPLETED:
        record = _parse_build_completion(record_mapping)
    elif record_type == INDEX_ACTIVATED:
        record = _parse_activation(record_mapping)
    elif record_type == INDEX_MARKED_STALE:
        record = _parse_stale_mark(record_mapping)
    else:
        _record_invalid("stored retrieval index record type is unsupported")
    stored = StoredRetrievalIndexRecord(
        record=record,
        policy=parse_authorization_policy(_mapping(item, "policy")),
        request=_parse_authorization_request(_mapping(item, "request")),
        decision=parse_authorization_decision(_mapping(item, "decision")),
        attestation_verified_by=_string(item, "attestation_verified_by"),
    )
    if _record_type(stored.record) != record_type:
        _record_invalid("stored retrieval index record type is mismatched")
    return stored


def _parse_build_request(item: Mapping[str, object]) -> IndexBuildRequest:
    _require_fields(
        item,
        {
            "contract_version",
            "build_request_id",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "source_event_watermark",
            "source_event_sha256",
            "source_catalog_sha256",
            "retriever_id",
            "retriever_version",
            "requested_by",
            "requested_via_client_id",
            "authorization_event_id",
            "requested_at",
        },
        "index build request",
    )
    return IndexBuildRequest(
        contract_version=_string(item, "contract_version"),
        build_request_id=_string(item, "build_request_id"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        source_event_watermark=_integer(item, "source_event_watermark"),
        source_event_sha256=_string(item, "source_event_sha256"),
        source_catalog_sha256=_string(item, "source_catalog_sha256"),
        retriever_id=_string(item, "retriever_id"),
        retriever_version=_string(item, "retriever_version"),
        requested_by=_string(item, "requested_by"),
        requested_via_client_id=_string(item, "requested_via_client_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        requested_at=_string(item, "requested_at"),
    )


def _parse_build_completion(
    item: Mapping[str, object],
) -> IndexBuildCompletion:
    _require_fields(
        item,
        {
            "contract_version",
            "completion_id",
            "build_request_id",
            "manifest",
            "completed_by",
            "completed_via_client_id",
            "authorization_event_id",
            "completed_at",
        },
        "index build completion",
    )
    return IndexBuildCompletion(
        contract_version=_string(item, "contract_version"),
        completion_id=_string(item, "completion_id"),
        build_request_id=_string(item, "build_request_id"),
        manifest=_parse_manifest(_mapping(item, "manifest")),
        completed_by=_string(item, "completed_by"),
        completed_via_client_id=_string(item, "completed_via_client_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        completed_at=_string(item, "completed_at"),
    )


def _parse_activation(item: Mapping[str, object]) -> IndexActivation:
    _require_fields(
        item,
        {
            "contract_version",
            "activation_id",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "manifest_id",
            "bundle_id",
            "completion_id",
            "previous_bundle_id",
            "activated_by",
            "activated_via_client_id",
            "authorization_event_id",
            "activated_at",
        },
        "index activation",
    )
    previous_bundle_id = item.get("previous_bundle_id")
    if previous_bundle_id is not None and type(previous_bundle_id) is not str:
        _record_invalid("previous_bundle_id is invalid")
    return IndexActivation(
        contract_version=_string(item, "contract_version"),
        activation_id=_string(item, "activation_id"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        manifest_id=_string(item, "manifest_id"),
        bundle_id=_string(item, "bundle_id"),
        completion_id=_string(item, "completion_id"),
        previous_bundle_id=cast(str | None, previous_bundle_id),
        activated_by=_string(item, "activated_by"),
        activated_via_client_id=_string(item, "activated_via_client_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        activated_at=_string(item, "activated_at"),
    )


def _parse_stale_mark(item: Mapping[str, object]) -> IndexStaleMark:
    _require_fields(
        item,
        {
            "contract_version",
            "stale_mark_id",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "manifest_id",
            "bundle_id",
            "reason",
            "marked_by",
            "marked_via_client_id",
            "authorization_event_id",
            "marked_at",
        },
        "index stale mark",
    )
    return IndexStaleMark(
        contract_version=_string(item, "contract_version"),
        stale_mark_id=_string(item, "stale_mark_id"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        manifest_id=_string(item, "manifest_id"),
        bundle_id=_string(item, "bundle_id"),
        reason=cast(
            Literal[
                "source_advanced",
                "policy_changed",
                "retention_purge",
                "provider_rotated",
                "manual",
            ],
            _string(item, "reason"),
        ),
        marked_by=_string(item, "marked_by"),
        marked_via_client_id=_string(item, "marked_via_client_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        marked_at=_string(item, "marked_at"),
    )


def _parse_manifest(item: Mapping[str, object]) -> RetrievalIndexManifest:
    _require_fields(
        item,
        {
            "manifest_id",
            "contract_version",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "bundle_id",
            "retriever_id",
            "retriever_version",
            "index_versions",
            "source_event_watermark",
            "source_event_sha256",
            "source_catalog_sha256",
            "memory_revision_ids",
            "embedding_provider_id",
            "embedding_model_id",
            "lexical_tokenizer_id",
            "lexical_tokenizer_version",
            "git_graph_version",
            "build_sha256",
            "stale_status",
        },
        "retrieval index manifest",
    )
    versions = item.get("index_versions")
    revision_ids = item.get("memory_revision_ids")
    if type(versions) is not list or any(type(value) is not dict for value in versions):
        _record_invalid("index_versions must be an object array")
    if type(revision_ids) is not list or any(
        type(value) is not str for value in revision_ids
    ):
        _record_invalid("memory_revision_ids must be a string array")
    return RetrievalIndexManifest(
        manifest_id=_string(item, "manifest_id"),
        contract_version=_string(item, "contract_version"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        bundle_id=_string(item, "bundle_id"),
        retriever_id=_string(item, "retriever_id"),
        retriever_version=_string(item, "retriever_version"),
        index_versions=tuple(
            _parse_index_version(cast(Mapping[str, object], value))
            for value in versions
        ),
        source_event_watermark=_integer(item, "source_event_watermark"),
        source_event_sha256=_string(item, "source_event_sha256"),
        source_catalog_sha256=_string(item, "source_catalog_sha256"),
        memory_revision_ids=tuple(cast(list[str], revision_ids)),
        embedding_provider_id=_string(item, "embedding_provider_id"),
        embedding_model_id=_string(item, "embedding_model_id"),
        lexical_tokenizer_id=_string(item, "lexical_tokenizer_id"),
        lexical_tokenizer_version=_string(
            item, "lexical_tokenizer_version"
        ),
        git_graph_version=_string(item, "git_graph_version"),
        build_sha256=_string(item, "build_sha256"),
        stale_status=cast(Literal["fresh"], _string(item, "stale_status")),
    )


def _parse_index_version(item: Mapping[str, object]) -> IndexVersion:
    _require_fields(
        item,
        {"index_kind", "index_id", "index_version", "content_sha256"},
        "index version",
    )
    return IndexVersion(
        index_kind=cast(IndexKind, _string(item, "index_kind")),
        index_id=_string(item, "index_id"),
        index_version=_string(item, "index_version"),
        content_sha256=_string(item, "content_sha256"),
    )


def _parse_authorization_request(
    item: Mapping[str, object],
) -> AuthorizationRequest:
    _require_fields(
        item,
        {
            "request_id",
            "principal_id",
            "agent_client_id",
            "tenant_id",
            "repository_reference",
            "permission",
            "requested_at",
        },
        "AuthorizationRequest",
    )
    tenant_id = item.get("tenant_id")
    repository_reference = item.get("repository_reference")
    if tenant_id is not None and type(tenant_id) is not str:
        _record_invalid("AuthorizationRequest tenant_id is invalid")
    if repository_reference is not None and type(repository_reference) is not str:
        _record_invalid("AuthorizationRequest repository_reference is invalid")
    return AuthorizationRequest(
        request_id=_string(item, "request_id"),
        principal_id=_string(item, "principal_id"),
        agent_client_id=_string(item, "agent_client_id"),
        tenant_id=cast(str | None, tenant_id),
        repository_reference=cast(str | None, repository_reference),
        permission=cast(AuthorizationPermission, _string(item, "permission")),
        requested_at=_string(item, "requested_at"),
    )


def _parse_projected_record(
    item: Mapping[str, object],
) -> ProjectedRetrievalIndexRecord:
    _require_fields(
        item,
        {"stored_record", "source_event_sha256", "global_position"},
        "projected retrieval index record",
    )
    return ProjectedRetrievalIndexRecord(
        stored_record=_parse_stored_record(_mapping(item, "stored_record")),
        source_event_sha256=_string(item, "source_event_sha256"),
        global_position=_integer(item, "global_position"),
    )


def _parse_head(item: Mapping[str, object]) -> RetrievalIndexHead:
    _require_fields(
        item,
        {
            "contract_version",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "bundle_id",
            "manifest_id",
            "completion_id",
            "activation_id",
            "previous_bundle_id",
            "index_versions",
            "source_event_watermark",
            "source_event_sha256",
            "stale",
            "stale_reason",
            "stale_mark_id",
            "completion_authorization_event_id",
            "activation_authorization_event_id",
            "completion_attestation_verified_by",
            "activation_attestation_verified_by",
            "activated_by",
            "activated_at",
            "status_event_sha256",
            "head_sha256",
        },
        "retrieval index head",
    )
    versions = item.get("index_versions")
    if type(versions) is not list or any(type(value) is not dict for value in versions):
        _projection_invalid("head index_versions are invalid")
    previous_bundle_id = item.get("previous_bundle_id")
    stale_reason = item.get("stale_reason")
    stale_mark_id = item.get("stale_mark_id")
    for value, name in (
        (previous_bundle_id, "previous_bundle_id"),
        (stale_reason, "stale_reason"),
        (stale_mark_id, "stale_mark_id"),
    ):
        if value is not None and type(value) is not str:
            _projection_invalid(f"{name} is invalid")
    return RetrievalIndexHead(
        contract_version=_string(item, "contract_version"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        bundle_id=_string(item, "bundle_id"),
        manifest_id=_string(item, "manifest_id"),
        completion_id=_string(item, "completion_id"),
        activation_id=_string(item, "activation_id"),
        previous_bundle_id=cast(str | None, previous_bundle_id),
        index_versions=tuple(
            _parse_index_version(cast(Mapping[str, object], value))
            for value in versions
        ),
        source_event_watermark=_integer(item, "source_event_watermark"),
        source_event_sha256=_string(item, "source_event_sha256"),
        stale=_boolean(item, "stale"),
        stale_reason=cast(str | None, stale_reason),
        stale_mark_id=cast(str | None, stale_mark_id),
        completion_authorization_event_id=_string(
            item, "completion_authorization_event_id"
        ),
        activation_authorization_event_id=_string(
            item, "activation_authorization_event_id"
        ),
        completion_attestation_verified_by=_string(
            item, "completion_attestation_verified_by"
        ),
        activation_attestation_verified_by=_string(
            item, "activation_attestation_verified_by"
        ),
        activated_by=_string(item, "activated_by"),
        activated_at=_string(item, "activated_at"),
        status_event_sha256=_string(item, "status_event_sha256"),
        head_sha256=_string(item, "head_sha256"),
    )


def _read_retrieval_index_stream(
    ledger: EventLedgerPort,
    stream_id: str,
    *,
    allow_empty: bool = False,
) -> tuple[CanonicalEvent, ...]:
    events: list[CanonicalEvent] = []
    from_version = 1
    while True:
        page = ledger.read_stream(
            stream_id,
            from_version=from_version,
            limit=EVENT_LEDGER_MAX_READ_PAGE,
        )
        if page.events and page.events[0].stream_version != from_version:
            _fail(
                "TBM_RETRIEVAL_INDEX_LEDGER_READ_FAILED",
                "retrieval-index page skipped or repeated its cursor",
            )
        events.extend(page.events)
        if len(events) > RETRIEVAL_INDEX_EVENT_MAX_STREAM_EVENTS:
            _fail(
                "TBM_RETRIEVAL_INDEX_EVENT_SEQUENCE_INVALID",
                "retrieval-index stream exceeds the event limit",
            )
        if not page.has_more:
            break
        if (
            page.next_stream_version is None
            or page.next_stream_version <= from_version
        ):
            _fail(
                "TBM_RETRIEVAL_INDEX_LEDGER_READ_FAILED",
                "retrieval-index page lacks a forward cursor",
            )
        from_version = page.next_stream_version
    if not events and not allow_empty:
        _fail(
            "TBM_RETRIEVAL_INDEX_HEAD_MISSING",
            "retrieval-index stream is empty",
        )
    return tuple(events)


def _verify_retained_stream(
    ledger: EventLedgerPort,
    stream_id: str,
    events: tuple[CanonicalEvent, ...],
) -> None:
    verification = ledger.verify_stream(stream_id)
    if (
        not verification.valid
        or verification.verified_stream_version != len(events)
        or verification.head_event_sha256 != events[-1].event_sha256
    ):
        _fail(
            "TBM_RETRIEVAL_INDEX_LEDGER_VERIFICATION_FAILED",
            "retained retrieval-index stream failed verification",
        )


def _require_ledger(ledger: EventLedgerPort) -> LedgerAccessContext:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global", "verify_stream")
    ):
        _fail(
            "TBM_RETRIEVAL_INDEX_LEDGER_INVALID",
            "operation requires an access-bound EventLedgerPort",
        )
    if access.classification_filter.allowed != _ALL_CLASSIFICATIONS:
        _fail(
            "TBM_RETRIEVAL_INDEX_CLASSIFICATION_VIEW_INCOMPLETE",
            "retrieval-index ledger access must include every classification",
        )
    return access


def _payload_json_schemas() -> dict[str, Mapping[str, object]]:
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    result: dict[str, Mapping[str, object]] = {}
    for event_type in RETRIEVAL_INDEX_EVENT_TYPES:
        properties = {
            "subject_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "record_type": {"const": event_type},
            "record_sha256": digest,
            "record_json": {
                "type": "string",
                "minLength": 2,
                "maxLength": RETRIEVAL_INDEX_JSON_MAX_BYTES,
            },
        }
        result[event_type] = {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }
    return result


def _loads_record(document: str | bytes, description: str) -> dict[str, object]:
    if type(document) is bytes:
        try:
            source = document.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise RetrievalIndexEventV1Error(
                "TBM_RETRIEVAL_INDEX_JSON_INVALID",
                f"{description} must be strict UTF-8 JSON",
            ) from error
    elif type(document) is str:
        source = document
    else:
        _record_invalid(f"{description} must be JSON text")
    try:
        size = len(source.encode("utf-8"))
    except UnicodeError as error:
        raise RetrievalIndexEventV1Error(
            "TBM_RETRIEVAL_INDEX_JSON_INVALID",
            f"{description} must be strict UTF-8 JSON",
        ) from error
    if size > RETRIEVAL_INDEX_JSON_MAX_BYTES:
        _record_invalid(f"{description} exceeds the byte limit")
    try:
        value = parse_bounded_json(
            source,
            max_depth=RETRIEVAL_INDEX_JSON_MAX_DEPTH,
            max_nodes=RETRIEVAL_INDEX_JSON_MAX_NODES,
            description=description,
        )
    except (TypeError, ValueError) as error:
        raise RetrievalIndexEventV1Error(
            "TBM_RETRIEVAL_INDEX_JSON_INVALID",
            f"{description} is invalid",
        ) from error
    if type(value) is not dict:
        _record_invalid(f"{description} must be an object")
    return cast(dict[str, object], value)


def _projection_digest_value(
    projection: RetrievalIndexProjection,
) -> dict[str, object]:
    return {
        "organization_id": projection.organization_id,
        "tenant_id": projection.tenant_id,
        "repository_id": projection.repository_id,
        "environment_id": projection.environment_id,
        "records": [item.to_dict() for item in projection.records],
        "active_head": (
            None
            if projection.active_head is None
            else projection.active_head.to_dict()
        ),
        "last_event_sha256": projection.last_event_sha256,
        "last_global_position": projection.last_global_position,
    }


def _index_versions(
    values: tuple[IndexVersion, ...], *, projection: bool = False
) -> None:
    invalid = _projection_invalid if projection else _record_invalid
    if (
        type(values) is not tuple
        or len(values) != len(_INDEX_KINDS)
        or any(type(value) is not IndexVersion for value in values)
        or tuple(value.index_kind for value in values) != _INDEX_KINDS
    ):
        invalid("index_versions must contain each canonical index kind once")


def _trusted_verifier_set(values: tuple[str, ...]) -> frozenset[str]:
    if type(values) is not tuple or not values or len(values) > 64:
        _record_invalid(
            "trusted_attestation_verifier_ids must be a bounded unique tuple"
        )
    if any(type(value) is not str for value in values):
        _record_invalid("trusted attestation verifier IDs must be strings")
    if len(set(values)) != len(values):
        _record_invalid("trusted attestation verifier IDs must be unique")
    for value in values:
        _identifier(value, "trusted_attestation_verifier_id")
    return frozenset(values)


def _trusted_embedding_set(
    values: tuple[tuple[str, str], ...],
) -> frozenset[tuple[str, str]]:
    if type(values) is not tuple or not values or len(values) > 64:
        _record_invalid(
            "trusted_embedding_provider_models must be a bounded unique tuple"
        )
    if any(
        type(value) is not tuple
        or len(value) != 2
        or any(type(part) is not str for part in value)
        for value in values
    ):
        _record_invalid(
            "trusted embedding provider/model entries must be string pairs"
        )
    if len(set(values)) != len(values):
        _record_invalid("trusted embedding provider/model entries must be unique")
    for provider_id, model_id in values:
        _identifier(provider_id, "trusted_embedding_provider_id")
        _identifier(model_id, "trusted_embedding_model_id")
    return frozenset(values)


def _target_partition(value: object) -> LedgerTenantPartition:
    try:
        return LedgerTenantPartition(
            organization_id=cast(str, getattr(value, "organization_id")),
            tenant_id=cast(str, getattr(value, "tenant_id")),
            repository_id=cast(str, getattr(value, "repository_id")),
            environment_id=cast(str, getattr(value, "environment_id")),
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RetrievalIndexEventV1Error(
            "TBM_RETRIEVAL_INDEX_RECORD_INVALID",
            "retrieval index target partition is invalid",
        ) from error


def _event_partition(event: CanonicalEvent) -> LedgerTenantPartition:
    return LedgerTenantPartition(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
    )


def _content_id(prefix: str, value: Mapping[str, object]) -> str:
    return prefix + canonical_sha256(value).removeprefix("sha256:")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(
        domain + _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise RetrievalIndexEventV1Error(
            "TBM_RETRIEVAL_INDEX_RECORD_INVALID",
            "retrieval index record is not canonical JSON",
        ) from error


def _timestamp(value: object, name: str) -> str:
    try:
        if type(value) is not str:
            raise ValueError("timestamp must be a string")
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise RetrievalIndexEventV1Error(
            "TBM_RETRIEVAL_INDEX_RECORD_INVALID",
            f"{name} must be canonical RFC3339",
        ) from error


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _record_invalid(f"{name} must be a bounded identifier")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _record_invalid(f"{name} must be sha256:<64 lowercase hex>")


def _require_fields(
    value: Mapping[str, object], expected: set[str], name: str
) -> None:
    if set(value) != expected:
        _record_invalid(f"{name} fields do not match the contract")


def _mapping(value: Mapping[str, object], name: str) -> dict[str, object]:
    item = value.get(name)
    if type(item) is not dict:
        _record_invalid(f"{name} must be an object")
    return cast(dict[str, object], item)


def _string(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if type(item) is not str:
        _record_invalid(f"{name} must be a string")
    return item


def _integer(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if type(item) is not int:
        _record_invalid(f"{name} must be an integer")
    return item


def _boolean(value: Mapping[str, object], name: str) -> bool:
    item = value.get(name)
    if type(item) is not bool:
        _record_invalid(f"{name} must be a boolean")
    return item


def _state_list(state: Mapping[str, object], name: str) -> list[object]:
    value = _thaw_json(state.get(name))
    if type(value) is not list:
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(list[object], value)


def _optional_state_mapping(
    value: object, name: str
) -> dict[str, object] | None:
    thawed = _thaw_json(value)
    if thawed is None:
        return None
    if type(thawed) is not dict:
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(dict[str, object], thawed)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "DurableRetrievalIndexSnapshot",
    "EventManagedIndexRepository",
    "INDEX_ACTIVATED",
    "INDEX_BUILD_COMPLETED",
    "INDEX_BUILD_REQUESTED",
    "INDEX_MARKED_STALE",
    "IndexActivation",
    "IndexBuildCompletion",
    "IndexBuildRequest",
    "IndexStaleMark",
    "ProjectedRetrievalIndexRecord",
    "RETRIEVAL_INDEX_EVENT_MAX_BATCH",
    "RETRIEVAL_INDEX_EVENT_MAX_STREAM_EVENTS",
    "RETRIEVAL_INDEX_EVENT_PAYLOAD_SCHEMA_ID",
    "RETRIEVAL_INDEX_EVENT_PROJECTION",
    "RETRIEVAL_INDEX_EVENT_PROTOCOL_VERSION",
    "RETRIEVAL_INDEX_EVENT_REDUCER_ID",
    "RETRIEVAL_INDEX_EVENT_STREAM_TYPE",
    "RETRIEVAL_INDEX_EVENT_TYPES",
    "RETRIEVAL_INDEX_JSON_MAX_BYTES",
    "RETRIEVAL_INDEX_MANIFEST_SCHEMA_ID",
    "RetrievalIndexAppendResult",
    "RetrievalIndexEventV1Error",
    "RetrievalIndexHead",
    "RetrievalIndexHeadReader",
    "RetrievalIndexManifest",
    "RetrievalIndexProjection",
    "StoredRetrievalIndexRecord",
    "append_retrieval_index_records",
    "build_index_activation",
    "build_index_build_completion",
    "build_index_build_request",
    "build_index_stale_mark",
    "build_retrieval_index_event_batch",
    "build_retrieval_index_event_registry",
    "build_retrieval_index_manifest",
    "build_retrieval_index_reducer",
    "dumps_retrieval_index_event_payload_dispatch_schema",
    "dumps_retrieval_index_manifest",
    "dumps_retrieval_index_manifest_schema",
    "dumps_stored_retrieval_index_record",
    "loads_retrieval_index_manifest",
    "loads_stored_retrieval_index_record",
    "rebuild_retrieval_index_from_ledger",
    "reduce_retrieval_index_events",
    "retrieval_index_event_payload_dispatch_schema",
    "retrieval_index_manifest_schema",
    "retrieval_index_stream_id",
]
