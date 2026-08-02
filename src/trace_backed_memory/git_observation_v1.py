from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, TypeAlias, cast

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError
from .event_registry_v1 import (
    EventPayloadRegistration,
    EventRegistryV1Error,
    EventTypeRegistry,
)
from .event_v1 import (
    EVENT_MAX_ARTIFACT_REFS,
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    EventSource,
    build_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerIdempotency,
    verify_ledger_append_receipt,
)


GIT_OBSERVATION_PROTOCOL_VERSION = "tbm.git-observation.v1"
GIT_OBSERVATION_STREAM_TYPE = "git_observation"
GIT_OBSERVATION_MAX_BATCH = EVENT_LEDGER_MAX_APPEND_BATCH
GIT_OBSERVATION_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.invalid/schemas/"
    "git_observation_payload_registry_v1.schema.json"
)
GIT_CHECKOUT_OBSERVED = "tbm.git.checkout_observed"
GIT_REF_OBSERVED = "tbm.git.ref_observed"
GIT_COMMIT_OBSERVED = "tbm.git.commit_observed"
GIT_DIFF_OBSERVED = "tbm.git.diff_observed"
GIT_ANCESTRY_OBSERVED = "tbm.git.ancestry_observed"
GIT_OBJECT_AVAILABILITY_OBSERVED = "tbm.git.object_availability_observed"
GIT_SHALLOW_STATE_OBSERVED = "tbm.git.shallow_state_observed"
GIT_OBSERVATION_TYPES = tuple(
    sorted(
        {
            GIT_CHECKOUT_OBSERVED,
            GIT_REF_OBSERVED,
            GIT_COMMIT_OBSERVED,
            GIT_DIFF_OBSERVED,
            GIT_ANCESTRY_OBSERVED,
            GIT_OBJECT_AVAILABILITY_OBSERVED,
            GIT_SHALLOW_STATE_OBSERVED,
        }
    )
)
GIT_OBSERVATION_DEFAULT_RUNNER_ID = "tbm_git_capture"
GIT_OBSERVATION_DEFAULT_RUNNER_VERSION = "f3-v1"
GIT_OBSERVATION_DEFAULT_ALGORITHM_ID = "git_observation"
GIT_OBSERVATION_DEFAULT_ALGORITHM_VERSION = "v1"

GitObjectFormat = Literal["sha1", "sha256"]
GitAncestryStatus = Literal["ancestor", "not_ancestor", "unknown"]
GitObjectAvailabilityStatus = Literal["present", "missing", "unknown"]
GitShallowState = Literal["full", "shallow", "unknown"]

_MAX_SEQUENCE = 9_223_372_036_854_775_807
_MAX_PARENTS = 128
_MAX_RELATIONS = 1_000
_MAX_OBJECTS = 1_001
_MAX_REF_CHARS = 1_024
_MAX_GIT_VERSION_CHARS = 256
_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_OID_PATTERNS = {
    "sha1": re.compile(r"^[0-9a-f]{40}$"),
    "sha256": re.compile(r"^[0-9a-f]{64}$"),
}
_PAYLOAD_SCHEMAS = {
    GIT_CHECKOUT_OBSERVED: "tbm.git.checkout-observed.v1",
    GIT_REF_OBSERVED: "tbm.git.ref-observed.v1",
    GIT_COMMIT_OBSERVED: "tbm.git.commit-observed.v1",
    GIT_DIFF_OBSERVED: "tbm.git.diff-observed.v1",
    GIT_ANCESTRY_OBSERVED: "tbm.git.ancestry-observed.v1",
    GIT_OBJECT_AVAILABILITY_OBSERVED: (
        "tbm.git.object-availability-observed.v1"
    ),
    GIT_SHALLOW_STATE_OBSERVED: "tbm.git.shallow-state-observed.v1",
}


class GitObservationV1Error(V3ContractError):
    """Stable failure for Git observation version-1 contracts."""


@dataclass(frozen=True)
class GitObservationProvenance:
    runner_id: str
    runner_version: str
    algorithm_id: str
    algorithm_version: str
    git_version: str

    def __post_init__(self) -> None:
        _identifier(self.runner_id, "runner_id")
        _code(self.runner_version, "runner_version")
        _identifier(self.algorithm_id, "algorithm_id")
        _code(self.algorithm_version, "algorithm_version")
        _bounded_text(self.git_version, "git_version", _MAX_GIT_VERSION_CHARS)

    def to_dict(self) -> dict[str, object]:
        return {
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "git_version": self.git_version,
        }


@dataclass(frozen=True)
class GitCheckoutObservation:
    root_sha256: str
    repository_name: str | None
    object_format: GitObjectFormat
    head_oid: str
    dirty: bool

    def __post_init__(self) -> None:
        _digest(self.root_sha256, "root_sha256")
        if self.repository_name is not None:
            _bounded_text(self.repository_name, "repository_name", 256)
        _object_format(self.object_format)
        _oid(self.head_oid, self.object_format, "head_oid")
        _boolean(self.dirty, "dirty")

    def to_dict(self) -> dict[str, object]:
        return {
            "root_sha256": self.root_sha256,
            "repository_name": self.repository_name,
            "object_format": self.object_format,
            "head_oid": self.head_oid,
            "dirty": self.dirty,
        }


@dataclass(frozen=True)
class GitRefObservation:
    object_format: GitObjectFormat
    target_oid: str
    ref_name: str | None
    detached: bool

    def __post_init__(self) -> None:
        _object_format(self.object_format)
        _oid(self.target_oid, self.object_format, "target_oid")
        _boolean(self.detached, "detached")
        if self.ref_name is not None:
            _bounded_text(self.ref_name, "ref_name", _MAX_REF_CHARS)
        if self.detached != (self.ref_name is None):
            _fail(
                "TBM_GIT_OBSERVATION_REF_INVALID",
                "detached state must exactly match the absence of ref_name",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "object_format": self.object_format,
            "target_oid": self.target_oid,
            "ref_name": self.ref_name,
            "detached": self.detached,
        }


@dataclass(frozen=True)
class GitCommitObservation:
    object_format: GitObjectFormat
    commit_oid: str
    tree_oid: str
    parent_oids: tuple[str, ...]

    def __post_init__(self) -> None:
        _object_format(self.object_format)
        _oid(self.commit_oid, self.object_format, "commit_oid")
        _oid(self.tree_oid, self.object_format, "tree_oid")
        if (
            type(self.parent_oids) is not tuple
            or len(self.parent_oids) > _MAX_PARENTS
            or len(self.parent_oids) != len(set(self.parent_oids))
        ):
            _fail(
                "TBM_GIT_OBSERVATION_COMMIT_INVALID",
                "parent_oids must be a bounded unique tuple",
            )
        for parent_oid in self.parent_oids:
            _oid(parent_oid, self.object_format, "parent_oid")

    def to_dict(self) -> dict[str, object]:
        return {
            "object_format": self.object_format,
            "commit_oid": self.commit_oid,
            "tree_oid": self.tree_oid,
            "parent_oids": list(self.parent_oids),
        }


@dataclass(frozen=True)
class GitDiffObservation:
    object_format: GitObjectFormat
    base_oid: str
    target: Literal["index_and_worktree"]
    content_sha256: str
    size_bytes: int
    artifact_id: str

    def __post_init__(self) -> None:
        _object_format(self.object_format)
        _oid(self.base_oid, self.object_format, "base_oid")
        if self.target != "index_and_worktree":
            _fail(
                "TBM_GIT_OBSERVATION_DIFF_INVALID",
                "diff target must be index_and_worktree",
            )
        _digest(self.content_sha256, "content_sha256")
        if type(self.size_bytes) is not int or not 0 <= self.size_bytes <= 64 * 1024 * 1024:
            _fail(
                "TBM_GIT_OBSERVATION_DIFF_INVALID",
                "diff size_bytes must be a bounded non-negative integer",
            )
        expected_artifact_id = "artifact_sha256_" + self.content_sha256[7:]
        if (
            type(self.artifact_id) is not str
            or _ARTIFACT_ID_RE.fullmatch(self.artifact_id) is None
            or self.artifact_id != expected_artifact_id
        ):
            _fail(
                "TBM_GIT_OBSERVATION_DIFF_INVALID",
                "diff artifact_id must be derived from content_sha256",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "object_format": self.object_format,
            "base_oid": self.base_oid,
            "target": self.target,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True)
class GitAncestryRelation:
    anchor_oid: str
    status: GitAncestryStatus

    def __post_init__(self) -> None:
        _generic_oid(self.anchor_oid, "anchor_oid")
        if self.status not in {"ancestor", "not_ancestor", "unknown"}:
            _fail(
                "TBM_GIT_OBSERVATION_ANCESTRY_INVALID",
                "ancestry status is invalid",
            )

    def to_dict(self) -> dict[str, object]:
        return {"anchor_oid": self.anchor_oid, "status": self.status}


@dataclass(frozen=True)
class GitAncestryObservation:
    object_format: GitObjectFormat
    current_oid: str
    relations: tuple[GitAncestryRelation, ...]

    def __post_init__(self) -> None:
        _object_format(self.object_format)
        _oid(self.current_oid, self.object_format, "current_oid")
        if (
            type(self.relations) is not tuple
            or len(self.relations) > _MAX_RELATIONS
            or any(type(item) is not GitAncestryRelation for item in self.relations)
        ):
            _fail(
                "TBM_GIT_OBSERVATION_ANCESTRY_INVALID",
                "relations must be a bounded tuple of GitAncestryRelation",
            )
        anchors = tuple(item.anchor_oid for item in self.relations)
        if anchors != tuple(sorted(set(anchors))):
            _fail(
                "TBM_GIT_OBSERVATION_ANCESTRY_INVALID",
                "ancestry relations must be sorted and unique by anchor_oid",
            )
        for relation in self.relations:
            _oid(relation.anchor_oid, self.object_format, "anchor_oid")

    def to_dict(self) -> dict[str, object]:
        return {
            "object_format": self.object_format,
            "current_oid": self.current_oid,
            "relations": [item.to_dict() for item in self.relations],
        }


@dataclass(frozen=True)
class GitObjectAvailability:
    object_oid: str
    status: GitObjectAvailabilityStatus

    def __post_init__(self) -> None:
        _generic_oid(self.object_oid, "object_oid")
        if self.status not in {"present", "missing", "unknown"}:
            _fail(
                "TBM_GIT_OBSERVATION_OBJECT_AVAILABILITY_INVALID",
                "object availability status is invalid",
            )

    def to_dict(self) -> dict[str, object]:
        return {"object_oid": self.object_oid, "status": self.status}


@dataclass(frozen=True)
class GitObjectAvailabilityObservation:
    object_format: GitObjectFormat
    objects: tuple[GitObjectAvailability, ...]

    def __post_init__(self) -> None:
        _object_format(self.object_format)
        if (
            type(self.objects) is not tuple
            or not 1 <= len(self.objects) <= _MAX_OBJECTS
            or any(type(item) is not GitObjectAvailability for item in self.objects)
        ):
            _fail(
                "TBM_GIT_OBSERVATION_OBJECT_AVAILABILITY_INVALID",
                "objects must be a bounded non-empty tuple",
            )
        object_oids = tuple(item.object_oid for item in self.objects)
        if object_oids != tuple(sorted(set(object_oids))):
            _fail(
                "TBM_GIT_OBSERVATION_OBJECT_AVAILABILITY_INVALID",
                "object availability entries must be sorted and unique",
            )
        for item in self.objects:
            _oid(item.object_oid, self.object_format, "object_oid")

    def to_dict(self) -> dict[str, object]:
        return {
            "object_format": self.object_format,
            "objects": [item.to_dict() for item in self.objects],
        }


@dataclass(frozen=True)
class GitShallowStateObservation:
    state: GitShallowState

    def __post_init__(self) -> None:
        if self.state not in {"full", "shallow", "unknown"}:
            _fail(
                "TBM_GIT_OBSERVATION_SHALLOW_STATE_INVALID",
                "shallow state is invalid",
            )

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state}


GitObservationValue: TypeAlias = (
    GitCheckoutObservation
    | GitRefObservation
    | GitCommitObservation
    | GitDiffObservation
    | GitAncestryObservation
    | GitObjectAvailabilityObservation
    | GitShallowStateObservation
)

_OBSERVATION_EVENT_TYPES: dict[type[object], str] = {
    GitCheckoutObservation: GIT_CHECKOUT_OBSERVED,
    GitRefObservation: GIT_REF_OBSERVED,
    GitCommitObservation: GIT_COMMIT_OBSERVED,
    GitDiffObservation: GIT_DIFF_OBSERVED,
    GitAncestryObservation: GIT_ANCESTRY_OBSERVED,
    GitObjectAvailabilityObservation: GIT_OBJECT_AVAILABILITY_OBSERVED,
    GitShallowStateObservation: GIT_SHALLOW_STATE_OBSERVED,
}


@dataclass(frozen=True)
class GitObservationDraft:
    checkout_id: str
    sequence: int
    observed_at: str
    provenance: GitObservationProvenance
    observation: GitObservationValue
    classification: EventClassification = "confidential"
    retention_policy_id: str = "retention_git_observation"
    artifact_refs: tuple[EventArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.checkout_id, "checkout_id")
        _sequence(self.sequence)
        _canonical_timestamp(self.observed_at, "observed_at")
        if type(self.provenance) is not GitObservationProvenance:
            _fail(
                "TBM_GIT_OBSERVATION_PROVENANCE_INVALID",
                "provenance must be exactly GitObservationProvenance",
            )
        if type(self.observation) not in _OBSERVATION_EVENT_TYPES:
            _fail(
                "TBM_GIT_OBSERVATION_VALUE_INVALID",
                "observation type is not registered",
            )
        if self.classification not in _CLASSIFICATION_RANK:
            _fail(
                "TBM_GIT_OBSERVATION_CLASSIFICATION_INVALID",
                "classification is invalid",
            )
        _identifier(self.retention_policy_id, "retention_policy_id")
        if (
            type(self.artifact_refs) is not tuple
            or len(self.artifact_refs) > EVENT_MAX_ARTIFACT_REFS
            or any(type(item) is not EventArtifactRef for item in self.artifact_refs)
        ):
            _fail(
                "TBM_GIT_OBSERVATION_ARTIFACT_REFS_INVALID",
                "artifact_refs must be a bounded tuple of EventArtifactRef",
            )
        artifact_ids = tuple(item.artifact_id for item in self.artifact_refs)
        if artifact_ids != tuple(sorted(set(artifact_ids))):
            _fail(
                "TBM_GIT_OBSERVATION_ARTIFACT_REFS_INVALID",
                "artifact_refs must be sorted and unique",
            )
        for item in self.artifact_refs:
            if _CLASSIFICATION_RANK[item.classification] > _CLASSIFICATION_RANK[
                self.classification
            ]:
                _fail(
                    "TBM_GIT_OBSERVATION_CLASSIFICATION_INVALID",
                    "event classification cannot be lower than an artifact",
                )
        if type(self.observation) is GitDiffObservation:
            if len(self.artifact_refs) != 1:
                _fail(
                    "TBM_GIT_OBSERVATION_DIFF_INVALID",
                    "diff observation requires exactly one artifact descriptor",
                )
            artifact = self.artifact_refs[0]
            diff = cast(GitDiffObservation, self.observation)
            if (
                artifact.artifact_id != diff.artifact_id
                or artifact.content_sha256 != diff.content_sha256
                or artifact.size_bytes != diff.size_bytes
                or artifact.media_type != "application/vnd.git.diff"
                or artifact.availability != "available"
                or artifact.classification not in {"confidential", "restricted"}
                or artifact.encryption_key_id is None
            ):
                _fail(
                    "TBM_GIT_OBSERVATION_DIFF_INVALID",
                    "diff artifact descriptor must exactly bind protected available bytes",
                )
        elif self.artifact_refs:
            _fail(
                "TBM_GIT_OBSERVATION_ARTIFACT_REFS_INVALID",
                "only diff observations carry artifact descriptors",
            )

    @property
    def event_type(self) -> str:
        return _OBSERVATION_EVENT_TYPES[type(self.observation)]

    def payload(self) -> dict[str, object]:
        return {
            "protocol_version": GIT_OBSERVATION_PROTOCOL_VERSION,
            "checkout_id": self.checkout_id,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "provenance": self.provenance.to_dict(),
            "point": self.event_type.removeprefix("tbm.git.").removesuffix(
                "_observed"
            ),
            "artifact_ids": [item.artifact_id for item in self.artifact_refs],
            "observation": self.observation.to_dict(),
        }

    def command_value(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "payload": self.payload(),
        }


def git_observation_stream_id(checkout_id: str) -> str:
    _identifier(checkout_id, "checkout_id")
    return "git_observation_" + hashlib.sha256(
        checkout_id.encode("utf-8")
    ).hexdigest()


def build_git_observation_batch(
    drafts: tuple[GitObservationDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= GIT_OBSERVATION_MAX_BATCH
        or any(type(item) is not GitObservationDraft for item in drafts)
    ):
        _fail(
            "TBM_GIT_OBSERVATION_BATCH_INVALID",
            "drafts must be a bounded non-empty tuple of GitObservationDraft",
        )
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_GIT_OBSERVATION_ACCESS_INVALID",
            "access must be exactly LedgerAccessContext",
        )
    if type(expected_stream_version) is not int or not 0 <= expected_stream_version <= _MAX_SEQUENCE:
        _fail(
            "TBM_GIT_OBSERVATION_BATCH_INVALID",
            "expected stream version is invalid",
        )
    if type(next_global_position) is not int or not 1 <= next_global_position <= _MAX_SEQUENCE:
        _fail(
            "TBM_GIT_OBSERVATION_BATCH_INVALID",
            "next global position is invalid",
        )
    canonical_recorded_at = _canonical_timestamp(recorded_at, "recorded_at")
    checkout_id = drafts[0].checkout_id
    provenance = drafts[0].provenance
    if any(
        item.checkout_id != checkout_id or item.provenance != provenance
        for item in drafts
    ):
        _fail(
            "TBM_GIT_OBSERVATION_BATCH_INVALID",
            "one batch must share checkout identity and capture provenance",
        )
    for offset, draft in enumerate(drafts, start=1):
        if draft.sequence != expected_stream_version + offset:
            _fail(
                "TBM_GIT_OBSERVATION_SEQUENCE_INVALID",
                "observation sequence must be contiguous from the expected head",
            )
    stream_id = git_observation_stream_id(checkout_id)
    parent = previous_event
    previous_observed_at: str | None = None
    if parent is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_GIT_OBSERVATION_BATCH_INVALID",
                "nonzero stream version requires its parent event",
            )
    else:
        if (
            type(parent) is not CanonicalEvent
            or parent.stream_id != stream_id
            or parent.stream_version != expected_stream_version
        ):
            _fail(
                "TBM_GIT_OBSERVATION_BATCH_INVALID",
                "previous event does not match the Git observation stream head",
            )
        verify_git_observation_event(parent)
        previous_payload = cast(Mapping[str, object], parent.payload)
        if previous_payload["checkout_id"] != checkout_id:
            _fail(
                "TBM_GIT_OBSERVATION_BATCH_INVALID",
                "previous event belongs to another checkout",
            )
        previous_observed_at = cast(str, previous_payload["observed_at"])
    for draft in drafts:
        if (
            previous_observed_at is not None
            and parse_rfc3339(draft.observed_at)
            < parse_rfc3339(previous_observed_at)
        ):
            _fail(
                "TBM_GIT_OBSERVATION_TIMESTAMP_INVALID",
                "observation timestamps cannot move backwards",
            )
        previous_observed_at = draft.observed_at
    command_value = {
        "protocol_version": GIT_OBSERVATION_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "next_global_position": next_global_position,
        "recorded_at": canonical_recorded_at,
        "drafts": [item.command_value() for item in drafts],
    }
    command_sha256 = _domain_sha256(
        b"tbm.git-observation-command.v1\x00", command_value
    )
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.git-observation-idempotency.v1\x00", command_value
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    correlation_digest = hashlib.sha256(
        (access.partition.partition_sha256 + "\x00" + checkout_id).encode(
            "utf-8"
        )
    ).hexdigest()
    parent = previous_event
    events: list[CanonicalEvent] = []
    for offset, draft in enumerate(drafts):
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        source = EventSource(
            source_system=draft.provenance.runner_id,
            source_record_id="git_observation_" + event_digest[:48],
            evidence_quality="observed",
            observed_at=draft.observed_at,
        )
        event = build_canonical_event(
            event_id="evt_git_" + event_digest,
            event_type=draft.event_type,
            event_version=1,
            event_kind="observation",
            origin="native",
            source=source,
            stream_id=stream_id,
            stream_type=GIT_OBSERVATION_STREAM_TYPE,
            stream_version=draft.sequence,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_git_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_git_" + correlation_digest[:32],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=draft.observed_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_git_observation_adapter",
            producer_version=GIT_OBSERVATION_DEFAULT_RUNNER_VERSION,
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification=draft.classification,
            retention_policy_id=draft.retention_policy_id,
            artifact_refs=draft.artifact_refs,
            payload=draft.payload(),
        )
        verify_git_observation_event(event)
        if parent is not None:
            verify_event_parent(event, parent)
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_git_observation_append_request(
    drafts: tuple[GitObservationDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> LedgerAppendRequest:
    events, idempotency = build_git_observation_batch(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )
    return LedgerAppendRequest(
        access=access,
        stream_id=events[0].stream_id,
        expected_stream_version=expected_stream_version,
        events=events,
        idempotency=idempotency,
    )


def append_git_observation_batch(
    ledger: EventLedgerPort,
    drafts: tuple[GitObservationDraft, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> LedgerAppendReceipt:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not callable(
        getattr(ledger, "append", None)
    ):
        _fail(
            "TBM_GIT_OBSERVATION_LEDGER_INVALID",
            "append requires an access-bound EventLedgerPort",
        )
    request = build_git_observation_append_request(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )
    receipt = ledger.append(
        request.stream_id,
        request.expected_stream_version,
        request.events,
        request.idempotency,
    )
    verify_ledger_append_receipt(request, receipt)
    return receipt


def verify_git_observation_event(event: CanonicalEvent) -> None:
    if type(event) is not CanonicalEvent:
        _fail(
            "TBM_GIT_OBSERVATION_EVENT_INVALID",
            "event must be exactly CanonicalEvent",
        )
    if (
        event.event_type not in GIT_OBSERVATION_TYPES
        or event.event_version != 1
        or event.event_kind != "observation"
        or event.origin != "native"
        or type(event.source) is not EventSource
        or event.stream_type != GIT_OBSERVATION_STREAM_TYPE
        or event.occurred_at is None
    ):
        _fail(
            "TBM_GIT_OBSERVATION_EVENT_INVALID",
            "canonical event is not a native Git observation v1",
        )
    try:
        payload = build_git_observation_registry().consume(event).payload
    except EventRegistryV1Error as error:
        raise GitObservationV1Error(
            "TBM_GIT_OBSERVATION_PAYLOAD_INVALID",
            "Git observation payload does not match its sealed type",
        ) from error
    checkout_id = cast(str, payload["checkout_id"])
    if event.stream_id != git_observation_stream_id(checkout_id):
        _fail(
            "TBM_GIT_OBSERVATION_EVENT_INVALID",
            "stream does not match checkout identity",
        )
    if payload["sequence"] != event.stream_version:
        _fail(
            "TBM_GIT_OBSERVATION_SEQUENCE_INVALID",
            "payload sequence does not match stream version",
        )
    if payload["observed_at"] != event.occurred_at:
        _fail(
            "TBM_GIT_OBSERVATION_TIMESTAMP_INVALID",
            "payload observed_at does not match event occurrence",
        )
    if event.source.observed_at != event.occurred_at:
        _fail(
            "TBM_GIT_OBSERVATION_SOURCE_INVALID",
            "source timestamp does not match event occurrence",
        )
    provenance = cast(Mapping[str, object], payload["provenance"])
    if event.source.source_system != provenance["runner_id"]:
        _fail(
            "TBM_GIT_OBSERVATION_SOURCE_INVALID",
            "source system does not match the persisted runner identity",
        )
    artifact_ids = tuple(cast(tuple[str, ...], payload["artifact_ids"]))
    if artifact_ids != tuple(item.artifact_id for item in event.artifact_refs):
        _fail(
            "TBM_GIT_OBSERVATION_ARTIFACT_REFS_INVALID",
            "payload artifact IDs do not match event descriptors",
        )
    if event.event_type == GIT_DIFF_OBSERVED:
        observation = cast(Mapping[str, object], payload["observation"])
        if len(event.artifact_refs) != 1:
            _fail(
                "TBM_GIT_OBSERVATION_DIFF_INVALID",
                "diff event requires exactly one artifact descriptor",
            )
        artifact = event.artifact_refs[0]
        if (
            artifact.artifact_id != observation["artifact_id"]
            or artifact.content_sha256 != observation["content_sha256"]
            or artifact.size_bytes != observation["size_bytes"]
            or artifact.media_type != "application/vnd.git.diff"
            or artifact.availability != "available"
            or artifact.classification not in {"confidential", "restricted"}
            or artifact.encryption_key_id is None
        ):
            _fail(
                "TBM_GIT_OBSERVATION_DIFF_INVALID",
                "diff event descriptor does not exactly bind protected available bytes",
            )
    expected_point = event.event_type.removeprefix("tbm.git.").removesuffix(
        "_observed"
    )
    if payload["point"] != expected_point:
        _fail(
            "TBM_GIT_OBSERVATION_PAYLOAD_INVALID",
            "payload point does not match event type",
        )
    _validate_observation_semantics(event.event_type, payload)


def build_git_observation_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    for event_type in GIT_OBSERVATION_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="observation",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=_payload_json_schema(event_type),
            )
        )
    return registry.seal()


def git_observation_payload_dispatch_schema() -> dict[str, object]:
    schema = build_git_observation_registry().dispatch_schema()
    schema["$id"] = GIT_OBSERVATION_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory Git observation payloads v1"
    schema["$comment"] = (
        "Generated from the sealed Git observation event registry. Runtime "
        "semantic verification and exact artifact binding remain authoritative."
    )
    return schema


def dumps_git_observation_payload_dispatch_schema() -> str:
    return json.dumps(
        git_observation_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _payload_json_schema(event_type: str) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "protocol_version",
            "checkout_id",
            "sequence",
            "observed_at",
            "provenance",
            "point",
            "artifact_ids",
            "observation",
        ],
        "properties": {
            "protocol_version": {"const": GIT_OBSERVATION_PROTOCOL_VERSION},
            "checkout_id": _identifier_schema(),
            "sequence": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_SEQUENCE,
            },
            "observed_at": {"type": "string", "pattern": _TIMESTAMP_PATTERN},
            "provenance": _provenance_schema(),
            "point": {
                "const": event_type.removeprefix("tbm.git.").removesuffix(
                    "_observed"
                )
            },
            "artifact_ids": {
                "type": "array",
                "items": {"type": "string", "pattern": _ARTIFACT_ID_RE.pattern},
                "minItems": 1 if event_type == GIT_DIFF_OBSERVED else 0,
                "maxItems": 1 if event_type == GIT_DIFF_OBSERVED else 0,
                "uniqueItems": True,
            },
            "observation": _observation_json_schema(event_type),
        },
    }


def _provenance_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "runner_id",
            "runner_version",
            "algorithm_id",
            "algorithm_version",
            "git_version",
        ],
        "properties": {
            "runner_id": _identifier_schema(),
            "runner_version": _code_schema(),
            "algorithm_id": _identifier_schema(),
            "algorithm_version": _code_schema(),
            "git_version": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_GIT_VERSION_CHARS,
            },
        },
    }


def _observation_json_schema(event_type: str) -> dict[str, object]:
    oid_schema = {
        "type": "string",
        "pattern": r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    }
    object_format = {"enum": ["sha1", "sha256"]}
    if event_type == GIT_CHECKOUT_OBSERVED:
        return _object_schema(
            ["root_sha256", "repository_name", "object_format", "head_oid", "dirty"],
            {
                "root_sha256": {"type": "string", "pattern": _DIGEST_RE.pattern},
                "repository_name": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "string", "minLength": 1, "maxLength": 256},
                    ]
                },
                "object_format": object_format,
                "head_oid": oid_schema,
                "dirty": {"type": "boolean"},
            },
        )
    if event_type == GIT_REF_OBSERVED:
        return _object_schema(
            ["object_format", "target_oid", "ref_name", "detached"],
            {
                "object_format": object_format,
                "target_oid": oid_schema,
                "ref_name": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "string", "minLength": 1, "maxLength": _MAX_REF_CHARS},
                    ]
                },
                "detached": {"type": "boolean"},
            },
        )
    if event_type == GIT_COMMIT_OBSERVED:
        return _object_schema(
            ["object_format", "commit_oid", "tree_oid", "parent_oids"],
            {
                "object_format": object_format,
                "commit_oid": oid_schema,
                "tree_oid": oid_schema,
                "parent_oids": {
                    "type": "array",
                    "items": oid_schema,
                    "minItems": 0,
                    "maxItems": _MAX_PARENTS,
                    "uniqueItems": True,
                },
            },
        )
    if event_type == GIT_DIFF_OBSERVED:
        return _object_schema(
            ["object_format", "base_oid", "target", "content_sha256", "size_bytes", "artifact_id"],
            {
                "object_format": object_format,
                "base_oid": oid_schema,
                "target": {"const": "index_and_worktree"},
                "content_sha256": {"type": "string", "pattern": _DIGEST_RE.pattern},
                "size_bytes": {"type": "integer", "minimum": 0, "maximum": 64 * 1024 * 1024},
                "artifact_id": {"type": "string", "pattern": _ARTIFACT_ID_RE.pattern},
            },
        )
    if event_type == GIT_ANCESTRY_OBSERVED:
        relation = _object_schema(
            ["anchor_oid", "status"],
            {
                "anchor_oid": oid_schema,
                "status": {"enum": ["ancestor", "not_ancestor", "unknown"]},
            },
        )
        return _object_schema(
            ["object_format", "current_oid", "relations"],
            {
                "object_format": object_format,
                "current_oid": oid_schema,
                "relations": {
                    "type": "array",
                    "items": relation,
                    "minItems": 0,
                    "maxItems": _MAX_RELATIONS,
                    "uniqueItems": True,
                },
            },
        )
    if event_type == GIT_OBJECT_AVAILABILITY_OBSERVED:
        availability = _object_schema(
            ["object_oid", "status"],
            {
                "object_oid": oid_schema,
                "status": {"enum": ["present", "missing", "unknown"]},
            },
        )
        return _object_schema(
            ["object_format", "objects"],
            {
                "object_format": object_format,
                "objects": {
                    "type": "array",
                    "items": availability,
                    "minItems": 1,
                    "maxItems": _MAX_OBJECTS,
                    "uniqueItems": True,
                },
            },
        )
    return _object_schema(
        ["state"],
        {"state": {"enum": ["full", "shallow", "unknown"]}},
    )


def _object_schema(
    required: list[str], properties: dict[str, object]
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _identifier_schema() -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": 128}


def _code_schema() -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": 256}


def _validate_observation_semantics(
    event_type: str, payload: Mapping[str, object]
) -> None:
    provenance_raw = payload["provenance"]
    observation_raw = payload["observation"]
    if not isinstance(provenance_raw, Mapping) or not isinstance(
        observation_raw, Mapping
    ):
        _fail(
            "TBM_GIT_OBSERVATION_PAYLOAD_INVALID",
            "provenance and observation must be objects",
        )
    GitObservationProvenance(
        runner_id=cast(str, provenance_raw["runner_id"]),
        runner_version=cast(str, provenance_raw["runner_version"]),
        algorithm_id=cast(str, provenance_raw["algorithm_id"]),
        algorithm_version=cast(str, provenance_raw["algorithm_version"]),
        git_version=cast(str, provenance_raw["git_version"]),
    )
    object_format = cast(GitObjectFormat, observation_raw.get("object_format"))
    if event_type == GIT_CHECKOUT_OBSERVED:
        GitCheckoutObservation(
            root_sha256=cast(str, observation_raw["root_sha256"]),
            repository_name=cast(str | None, observation_raw["repository_name"]),
            object_format=object_format,
            head_oid=cast(str, observation_raw["head_oid"]),
            dirty=cast(bool, observation_raw["dirty"]),
        )
    elif event_type == GIT_REF_OBSERVED:
        GitRefObservation(
            object_format=object_format,
            target_oid=cast(str, observation_raw["target_oid"]),
            ref_name=cast(str | None, observation_raw["ref_name"]),
            detached=cast(bool, observation_raw["detached"]),
        )
    elif event_type == GIT_COMMIT_OBSERVED:
        GitCommitObservation(
            object_format=object_format,
            commit_oid=cast(str, observation_raw["commit_oid"]),
            tree_oid=cast(str, observation_raw["tree_oid"]),
            parent_oids=tuple(cast(list[str], observation_raw["parent_oids"])),
        )
    elif event_type == GIT_DIFF_OBSERVED:
        GitDiffObservation(
            object_format=object_format,
            base_oid=cast(str, observation_raw["base_oid"]),
            target=cast(Literal["index_and_worktree"], observation_raw["target"]),
            content_sha256=cast(str, observation_raw["content_sha256"]),
            size_bytes=cast(int, observation_raw["size_bytes"]),
            artifact_id=cast(str, observation_raw["artifact_id"]),
        )
    elif event_type == GIT_ANCESTRY_OBSERVED:
        relations_raw = cast(list[Mapping[str, object]], observation_raw["relations"])
        GitAncestryObservation(
            object_format=object_format,
            current_oid=cast(str, observation_raw["current_oid"]),
            relations=tuple(
                GitAncestryRelation(
                    anchor_oid=cast(str, item["anchor_oid"]),
                    status=cast(GitAncestryStatus, item["status"]),
                )
                for item in relations_raw
            ),
        )
    elif event_type == GIT_OBJECT_AVAILABILITY_OBSERVED:
        objects_raw = cast(list[Mapping[str, object]], observation_raw["objects"])
        GitObjectAvailabilityObservation(
            object_format=object_format,
            objects=tuple(
                GitObjectAvailability(
                    object_oid=cast(str, item["object_oid"]),
                    status=cast(GitObjectAvailabilityStatus, item["status"]),
                )
                for item in objects_raw
            ),
        )
    else:
        GitShallowStateObservation(
            state=cast(GitShallowState, observation_raw["state"])
        )


def _object_format(value: object) -> None:
    if value not in _OID_PATTERNS:
        _fail(
            "TBM_GIT_OBSERVATION_OBJECT_FORMAT_INVALID",
            "object_format must be sha1 or sha256",
        )


def _oid(value: object, object_format: GitObjectFormat, name: str) -> None:
    if (
        type(value) is not str
        or object_format not in _OID_PATTERNS
        or _OID_PATTERNS[object_format].fullmatch(value) is None
    ):
        _fail(
            "TBM_GIT_OBSERVATION_OID_INVALID",
            f"{name} does not match object_format",
        )


def _generic_oid(value: object, name: str) -> None:
    if type(value) is not str or not any(
        pattern.fullmatch(value) is not None for pattern in _OID_PATTERNS.values()
    ):
        _fail(
            "TBM_GIT_OBSERVATION_OID_INVALID",
            f"{name} must be a complete Git object ID",
        )


def _sequence(value: object) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SEQUENCE:
        _fail(
            "TBM_GIT_OBSERVATION_SEQUENCE_INVALID",
            "sequence must be a bounded positive integer",
        )


def _boolean(value: object, name: str) -> None:
    if type(value) is not bool:
        _fail(
            "TBM_GIT_OBSERVATION_VALUE_INVALID", f"{name} must be a boolean"
        )


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or not value[0].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in value)
    ):
        _fail(
            "TBM_GIT_OBSERVATION_IDENTIFIER_INVALID",
            f"{name} must be a bounded identifier",
        )


def _code(value: object, name: str) -> None:
    _bounded_text(value, name, 256)


def _bounded_text(value: object, name: str, max_chars: int) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= max_chars
        or any(ord(character) < 32 for character in value)
    ):
        _fail(
            "TBM_GIT_OBSERVATION_TEXT_INVALID",
            f"{name} must be bounded printable text",
        )


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail(
            "TBM_GIT_OBSERVATION_DIGEST_INVALID",
            f"{name} must be a sha256 digest",
        )


def _canonical_timestamp(value: object, name: str) -> str:
    if type(value) is not str:
        _fail(
            "TBM_GIT_OBSERVATION_TIMESTAMP_INVALID",
            f"{name} must be a canonical timestamp",
        )
    try:
        canonical = canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise GitObservationV1Error(
            "TBM_GIT_OBSERVATION_TIMESTAMP_INVALID",
            f"{name} must be a canonical timestamp",
        ) from error
    if canonical != value:
        _fail(
            "TBM_GIT_OBSERVATION_TIMESTAMP_INVALID",
            f"{name} must use canonical UTC spelling",
        )
    return canonical


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _fail(code: str, message: str) -> NoReturn:
    raise GitObservationV1Error(code, message)


__all__ = [
    "GIT_ANCESTRY_OBSERVED",
    "GIT_CHECKOUT_OBSERVED",
    "GIT_COMMIT_OBSERVED",
    "GIT_DIFF_OBSERVED",
    "GIT_OBJECT_AVAILABILITY_OBSERVED",
    "GIT_OBSERVATION_DEFAULT_ALGORITHM_ID",
    "GIT_OBSERVATION_DEFAULT_ALGORITHM_VERSION",
    "GIT_OBSERVATION_DEFAULT_RUNNER_ID",
    "GIT_OBSERVATION_DEFAULT_RUNNER_VERSION",
    "GIT_OBSERVATION_MAX_BATCH",
    "GIT_OBSERVATION_PAYLOAD_SCHEMA_ID",
    "GIT_OBSERVATION_PROTOCOL_VERSION",
    "GIT_OBSERVATION_STREAM_TYPE",
    "GIT_OBSERVATION_TYPES",
    "GIT_REF_OBSERVED",
    "GIT_SHALLOW_STATE_OBSERVED",
    "GitAncestryObservation",
    "GitAncestryRelation",
    "GitAncestryStatus",
    "GitCheckoutObservation",
    "GitCommitObservation",
    "GitDiffObservation",
    "GitObjectAvailability",
    "GitObjectAvailabilityObservation",
    "GitObjectAvailabilityStatus",
    "GitObjectFormat",
    "GitObservationDraft",
    "GitObservationProvenance",
    "GitObservationV1Error",
    "GitObservationValue",
    "GitRefObservation",
    "GitShallowState",
    "GitShallowStateObservation",
    "append_git_observation_batch",
    "build_git_observation_append_request",
    "build_git_observation_batch",
    "build_git_observation_registry",
    "dumps_git_observation_payload_dispatch_schema",
    "git_observation_payload_dispatch_schema",
    "git_observation_stream_id",
    "verify_git_observation_event",
]
