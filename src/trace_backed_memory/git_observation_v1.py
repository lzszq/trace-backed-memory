from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from threading import Lock
from typing import Literal, NoReturn, cast

from ._timestamps import RFC3339_PATTERN, canonical_rfc3339
from .capture import GitObservationRecorder
from .contracts_v3 import V3ContractError
from .event_v1 import (
    EVENT_MAX_VERSION,
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    EventSource,
    EventTrustedContext,
    EventV1ContractError,
    build_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerAtomicAppendPort,
    LedgerAccessContext,
    LedgerAppendCommit,
    LedgerIdempotency,
)
from .models import CommitAncestryEvidence, TraceMetadata


GIT_OBSERVATION_CONTRACT_VERSION = "tbm.git-observation.v1"
GIT_OBSERVATION_VERSION = 1
GIT_CHECKOUT_OBSERVED = "tbm.git.checkout_observed"
GIT_COMMIT_OBSERVED = "tbm.git.commit_observed"
GIT_REF_OBSERVED = "tbm.git.ref_observed"
GIT_WORKTREE_STATUS_OBSERVED = "tbm.git.worktree_status_observed"
GIT_DIFF_CAPTURED = "tbm.git.diff_captured"
GIT_COMMIT_RELATION_OBSERVED = "tbm.git.commit_relation_observed"
GIT_OBJECT_AVAILABILITY_OBSERVED = "tbm.git.object_availability_observed"
GIT_SHALLOW_STATE_OBSERVED = "tbm.git.shallow_state_observed"
GIT_OBSERVATION_TYPES = (
    GIT_CHECKOUT_OBSERVED,
    GIT_COMMIT_OBSERVED,
    GIT_REF_OBSERVED,
    GIT_WORKTREE_STATUS_OBSERVED,
    GIT_DIFF_CAPTURED,
    GIT_COMMIT_RELATION_OBSERVED,
    GIT_OBJECT_AVAILABILITY_OBSERVED,
    GIT_SHALLOW_STATE_OBSERVED,
)
GIT_OBSERVATION_STREAM_TYPE = "git_observation"
GIT_OBSERVATION_PRODUCER = "trace_backed_memory"
GIT_OBSERVATION_PRODUCER_VERSION = "0.1.0"
GIT_OBSERVATION_MAX_SEQUENCE = 1_000_000_000
GIT_OBSERVATION_MAX_BOUNDARY_COMMITS = 100

GitObservationKind = Literal[
    "checkout",
    "commit",
    "ref",
    "worktree_status",
    "diff",
    "ancestry",
    "object_availability",
    "shallow_state",
]
GitAncestryRelation = Literal["ancestor", "not_ancestor", "unknown"]
GitObjectType = Literal["commit", "tree", "blob", "tag", "unknown"]
GitObjectAvailability = Literal["available", "unavailable", "unknown"]
GitObjectUnavailableReason = Literal[
    "missing",
    "shallow_boundary",
    "not_fetched",
    "capture_failed",
    "unknown",
]

_EVENT_TYPE_BY_KIND: dict[GitObservationKind, str] = {
    "checkout": GIT_CHECKOUT_OBSERVED,
    "commit": GIT_COMMIT_OBSERVED,
    "ref": GIT_REF_OBSERVED,
    "worktree_status": GIT_WORKTREE_STATUS_OBSERVED,
    "diff": GIT_DIFF_CAPTURED,
    "ancestry": GIT_COMMIT_RELATION_OBSERVED,
    "object_availability": GIT_OBJECT_AVAILABILITY_OBSERVED,
    "shallow_state": GIT_SHALLOW_STATE_OBSERVED,
}
_KIND_BY_EVENT_TYPE = {
    event_type: kind for kind, event_type in _EVENT_TYPE_BY_KIND.items()
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$")
_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_GIT_REF_NAME_PATTERN = (
    r"^(?![-/.])(?!@$)(?!.*//)(?!.*\.\.)(?!.*@\{)"
    r"(?!.*(?:^|/)\.)(?!.*(?:^|/)[^/]*\.lock(?:/|$))"
    r"(?!.*[./]$)[A-Za-z0-9_@+./-]{1,512}$"
)
_GIT_REF_NAME_RE = re.compile(_GIT_REF_NAME_PATTERN)
_EVIDENCE_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$"
_EVIDENCE_VERSION_RE = re.compile(_EVIDENCE_VERSION_PATTERN)
_ANCESTRY_RELATIONS = frozenset({"ancestor", "not_ancestor", "unknown"})
_OBJECT_TYPES = frozenset({"commit", "tree", "blob", "tag", "unknown"})
_OBJECT_AVAILABILITY = frozenset({"available", "unavailable", "unknown"})
_UNAVAILABLE_REASONS = frozenset(
    {"missing", "shallow_boundary", "not_fetched", "capture_failed", "unknown"}
)


class GitObservationV1Error(V3ContractError):
    """Stable failure for typed, artifact-linked Git observations."""


@dataclass(frozen=True)
class GitObservationDetails:
    observation_kind: GitObservationKind
    commit_sha: str | None = None
    base_commit_sha: str | None = None
    ref_name: str | None = None
    detached: bool | None = None
    dirty: bool | None = None
    remote_sha256: str | None = None
    diff_artifact_id: str | None = None
    ancestor_sha: str | None = None
    descendant_sha: str | None = None
    relation: GitAncestryRelation | None = None
    object_sha: str | None = None
    object_type: GitObjectType | None = None
    availability: GitObjectAvailability | None = None
    unavailable_reason: GitObjectUnavailableReason | None = None
    shallow: bool | None = None
    boundary_commit_shas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.observation_kind) is not str
            or self.observation_kind not in _EVENT_TYPE_BY_KIND
        ):
            _invalid("observation_kind is invalid")
        for name in (
            "commit_sha",
            "base_commit_sha",
            "ancestor_sha",
            "descendant_sha",
            "object_sha",
        ):
            value = getattr(self, name)
            if value is not None:
                _commit_sha(value, name)
        if self.ref_name is not None:
            _git_ref_name(self.ref_name)
        if self.remote_sha256 is not None:
            _digest(self.remote_sha256, "remote_sha256")
        if self.diff_artifact_id is not None:
            _artifact_id(self.diff_artifact_id, "diff_artifact_id")
        for name in ("detached", "dirty", "shallow"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                _invalid(f"{name} must be a boolean or null")
        if self.relation is not None and (
            type(self.relation) is not str or self.relation not in _ANCESTRY_RELATIONS
        ):
            _invalid("relation is invalid")
        if self.object_type is not None and (
            type(self.object_type) is not str or self.object_type not in _OBJECT_TYPES
        ):
            _invalid("object_type is invalid")
        if self.availability is not None and (
            type(self.availability) is not str
            or self.availability not in _OBJECT_AVAILABILITY
        ):
            _invalid("availability is invalid")
        if self.unavailable_reason is not None and (
            type(self.unavailable_reason) is not str
            or self.unavailable_reason not in _UNAVAILABLE_REASONS
        ):
            _invalid("unavailable_reason is invalid")
        if (
            type(self.boundary_commit_shas) is not tuple
            or len(self.boundary_commit_shas) > GIT_OBSERVATION_MAX_BOUNDARY_COMMITS
        ):
            _invalid("boundary_commit_shas must be a bounded tuple")
        for value in self.boundary_commit_shas:
            _commit_sha(value, "boundary_commit_shas item")
        canonical_boundaries = tuple(sorted(self.boundary_commit_shas))
        if len(set(canonical_boundaries)) != len(canonical_boundaries):
            _invalid("boundary_commit_shas must be unique")
        object.__setattr__(self, "boundary_commit_shas", canonical_boundaries)
        self._verify_shape()

    @property
    def event_type(self) -> str:
        return _EVENT_TYPE_BY_KIND[self.observation_kind]

    def _verify_shape(self) -> None:
        kind = self.observation_kind
        if kind == "checkout":
            _require(self.commit_sha, "checkout requires commit_sha")
            _require_bool(self.detached, "checkout requires detached")
            _ref_matches_detached(self.ref_name, cast(bool, self.detached))
            _forbid_detail_fields(
                self,
                {
                    "commit_sha",
                    "ref_name",
                    "detached",
                    "remote_sha256",
                },
            )
        elif kind == "commit":
            _require(self.commit_sha, "commit observation requires commit_sha")
            _forbid_detail_fields(self, {"commit_sha"})
        elif kind == "ref":
            _require(self.commit_sha, "ref observation requires commit_sha")
            _require_bool(self.detached, "ref observation requires detached")
            _ref_matches_detached(self.ref_name, cast(bool, self.detached))
            _forbid_detail_fields(self, {"commit_sha", "ref_name", "detached"})
        elif kind == "worktree_status":
            _require(self.commit_sha, "worktree status requires commit_sha")
            _require_bool(self.detached, "worktree status requires detached")
            _require_bool(self.dirty, "worktree status requires dirty")
            _ref_matches_detached(self.ref_name, cast(bool, self.detached))
            _forbid_detail_fields(
                self,
                {
                    "commit_sha",
                    "ref_name",
                    "detached",
                    "dirty",
                    "diff_artifact_id",
                },
            )
        elif kind == "diff":
            _require(self.commit_sha, "diff observation requires commit_sha")
            _require(self.diff_artifact_id, "diff observation requires artifact")
            _forbid_detail_fields(
                self,
                {"commit_sha", "base_commit_sha", "diff_artifact_id"},
            )
        elif kind == "ancestry":
            _require(self.ancestor_sha, "ancestry requires ancestor_sha")
            _require(self.descendant_sha, "ancestry requires descendant_sha")
            _require(self.relation, "ancestry requires relation")
            if self.ancestor_sha == self.descendant_sha and self.relation != "ancestor":
                _invalid("a commit must be its own ancestor")
            _forbid_detail_fields(
                self,
                {"ancestor_sha", "descendant_sha", "relation"},
            )
        elif kind == "object_availability":
            _require(self.object_sha, "object availability requires object_sha")
            _require(self.object_type, "object availability requires object_type")
            _require(self.availability, "object availability requires availability")
            if self.availability == "available" and self.unavailable_reason is not None:
                _invalid("available object cannot have unavailable_reason")
            if self.availability != "available" and self.unavailable_reason is None:
                _invalid("unavailable or unknown object requires a reason")
            _forbid_detail_fields(
                self,
                {
                    "object_sha",
                    "object_type",
                    "availability",
                    "unavailable_reason",
                },
            )
        else:
            _require_bool(self.shallow, "shallow state requires shallow")
            if self.shallow is False and self.boundary_commit_shas:
                _invalid("non-shallow repository cannot name shallow boundaries")
            _forbid_detail_fields(self, {"shallow", "boundary_commit_shas"})

    def to_dict(self) -> dict[str, object]:
        kind = self.observation_kind
        if kind == "checkout":
            return {
                "commit_sha": self.commit_sha,
                "ref_name": self.ref_name,
                "detached": self.detached,
                "remote_sha256": self.remote_sha256,
            }
        if kind == "commit":
            return {"commit_sha": self.commit_sha}
        if kind == "ref":
            return {
                "commit_sha": self.commit_sha,
                "ref_name": self.ref_name,
                "detached": self.detached,
            }
        if kind == "worktree_status":
            return {
                "commit_sha": self.commit_sha,
                "ref_name": self.ref_name,
                "detached": self.detached,
                "dirty": self.dirty,
                "diff_artifact_id": self.diff_artifact_id,
            }
        if kind == "diff":
            return {
                "commit_sha": self.commit_sha,
                "base_commit_sha": self.base_commit_sha,
                "diff_artifact_id": self.diff_artifact_id,
            }
        if kind == "ancestry":
            return {
                "ancestor_sha": self.ancestor_sha,
                "descendant_sha": self.descendant_sha,
                "relation": self.relation,
            }
        if kind == "object_availability":
            return {
                "object_sha": self.object_sha,
                "object_type": self.object_type,
                "availability": self.availability,
                "unavailable_reason": self.unavailable_reason,
            }
        return {
            "shallow": self.shallow,
            "boundary_commit_shas": list(self.boundary_commit_shas),
        }


@dataclass(frozen=True)
class GitObservationRecordRef:
    trace_id: str
    run_id: str
    repository_id: str
    checkout_alias: str
    sequence: int
    occurred_at: str
    authorization_event_id: str
    source: EventSource
    details: GitObservationDetails
    artifact_refs: tuple[EventArtifactRef, ...]
    classification: EventClassification
    retention_policy_id: str
    git_version: str
    runner_name: str
    runner_version: str
    algorithm_name: str
    algorithm_version: str
    causation_event_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "trace_id",
            "run_id",
            "repository_id",
            "checkout_alias",
            "authorization_event_id",
            "retention_policy_id",
            "runner_name",
            "algorithm_name",
        ):
            _identifier(getattr(self, name), name)
        for name in (
            "git_version",
            "runner_version",
            "algorithm_version",
        ):
            _evidence_version(getattr(self, name), name)
        if (
            type(self.sequence) is not int
            or not 1 <= self.sequence <= GIT_OBSERVATION_MAX_SEQUENCE
        ):
            _invalid("sequence must be a bounded positive integer")
        if type(self.source) is not EventSource:
            _invalid("source must be exactly EventSource")
        if type(self.details) is not GitObservationDetails:
            _invalid("details must be exactly GitObservationDetails")
        if self.causation_event_id is not None and (
            type(self.causation_event_id) is not str
            or _EVENT_ID_RE.fullmatch(self.causation_event_id) is None
        ):
            _invalid("causation_event_id is invalid")
        canonical_occurred_at = _canonical_timestamp(self.occurred_at, "occurred_at")
        canonical_observed_at = _canonical_timestamp(
            self.source.observed_at,
            "source.observed_at",
        )
        if self.occurred_at != canonical_occurred_at:
            _invalid("occurred_at must use canonical UTC RFC 3339")
        if self.source.observed_at != canonical_observed_at:
            _invalid("source observed_at must use canonical UTC RFC 3339")
        if type(self.artifact_refs) is not tuple or any(
            type(item) is not EventArtifactRef for item in self.artifact_refs
        ):
            _invalid("artifact_refs must be a tuple of EventArtifactRef")
        artifact_refs = tuple(
            sorted(self.artifact_refs, key=lambda item: item.artifact_id)
        )
        if len({item.artifact_id for item in artifact_refs}) != len(artifact_refs):
            _invalid("artifact_refs must be unique")
        expected_artifact_ids = (
            set()
            if self.details.diff_artifact_id is None
            else {self.details.diff_artifact_id}
        )
        if {item.artifact_id for item in artifact_refs} != expected_artifact_ids:
            _invalid("artifact_refs must exactly match the Git diff artifact")
        object.__setattr__(self, "artifact_refs", artifact_refs)
        if type(self.classification) is not str or self.classification not in {
            "public",
            "internal",
            "confidential",
            "restricted",
        }:
            _invalid("classification is invalid")

    @property
    def event_type(self) -> str:
        return self.details.event_type

    @property
    def observation_kind(self) -> GitObservationKind:
        return self.details.observation_kind

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": GIT_OBSERVATION_CONTRACT_VERSION,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "repository_id": self.repository_id,
            "checkout_alias": self.checkout_alias,
            "sequence": self.sequence,
            "observation_kind": self.observation_kind,
            "occurred_at": self.occurred_at,
            "authorization_event_id": self.authorization_event_id,
            "git_version": self.git_version,
            "runner_name": self.runner_name,
            "runner_version": self.runner_version,
            "algorithm_name": self.algorithm_name,
            "algorithm_version": self.algorithm_version,
            "artifact_ids": [item.artifact_id for item in self.artifact_refs],
            "details": self.details.to_dict(),
            "causation_event_id": self.causation_event_id,
        }

    def to_projection_dict(self) -> dict[str, object]:
        return {
            **self.to_dict(),
            "source": self.source.to_dict(),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
        }


@dataclass(frozen=True)
class _PendingGitObservationBatch:
    events: tuple[CanonicalEvent, ...]
    parent_event: CanonicalEvent | None


class GitObservationEventRecorder:
    def __init__(
        self,
        ledger: EventLedgerAtomicAppendPort,
        *,
        trace_id: str,
        run_id: str,
        checkout_alias: str,
        source: EventSource,
        trusted_context: EventTrustedContext,
        occurred_at: str,
        recorded_at: str,
        git_version: str,
        runner_name: str,
        runner_version: str,
        algorithm_name: str,
        algorithm_version: str,
        classification: EventClassification,
        retention_policy_id: str,
        first_sequence: int,
        first_global_position: int,
        parent_event: CanonicalEvent | None,
        remote_sha256: str | None = None,
        shallow: bool = False,
        boundary_commit_shas: tuple[str, ...] = (),
        diff_artifact_ref: EventArtifactRef | None = None,
        diff_base_commit_sha: str | None = None,
    ) -> None:
        if type(trusted_context) is not EventTrustedContext:
            _invalid("trusted_context must be exactly EventTrustedContext")
        if type(ledger.access_context) is not LedgerAccessContext:
            _invalid("ledger must expose exactly LedgerAccessContext")
        if ledger.access_context.event_trusted_context() != trusted_context:
            _invalid("ledger trusted context does not match recorder context")
        self._ledger = ledger
        self._trace_id = trace_id
        self._run_id = run_id
        self._checkout_alias = checkout_alias
        self._source = source
        self._trusted_context = trusted_context
        self._occurred_at = occurred_at
        self._recorded_at = recorded_at
        self._git_version = git_version
        self._runner_name = runner_name
        self._runner_version = runner_version
        self._algorithm_name = algorithm_name
        self._algorithm_version = algorithm_version
        self._classification = classification
        self._retention_policy_id = retention_policy_id
        self._next_sequence = first_sequence
        self._next_global_position = first_global_position
        self._parent_event = parent_event
        self._remote_sha256 = remote_sha256
        self._shallow = shallow
        self._boundary_commit_shas = boundary_commit_shas
        self._diff_artifact_ref = diff_artifact_ref
        self._diff_base_commit_sha = diff_base_commit_sha
        self._commits: list[LedgerAppendCommit] = []
        self._pending_batches: tuple[_PendingGitObservationBatch, ...] = ()
        self._pending_batch_index = 0
        self._lock = Lock()
        self._preflight()

    @property
    def commits(self) -> tuple[LedgerAppendCommit, ...]:
        with self._lock:
            return tuple(self._commits)

    @property
    def parent_event(self) -> CanonicalEvent | None:
        with self._lock:
            return self._parent_event

    @property
    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence

    @property
    def next_global_position(self) -> int:
        with self._lock:
            return self._next_global_position

    @property
    def pending_batch_count(self) -> int:
        with self._lock:
            return len(self._pending_batches) - self._pending_batch_index

    def resume_pending(self) -> None:
        with self._lock:
            self._drain_pending_batches()

    def record_metadata(self, metadata: TraceMetadata) -> None:
        if type(metadata) is not TraceMetadata:
            _invalid("metadata must be exactly TraceMetadata")
        detached = metadata.branch is None
        diff_artifact_id = (
            None
            if self._diff_artifact_ref is None
            else self._diff_artifact_ref.artifact_id
        )
        details = [
            GitObservationDetails(
                "checkout",
                commit_sha=metadata.commit_sha,
                ref_name=metadata.branch,
                detached=detached,
                remote_sha256=self._remote_sha256,
            ),
            GitObservationDetails("commit", commit_sha=metadata.commit_sha),
            GitObservationDetails(
                "ref",
                commit_sha=metadata.commit_sha,
                ref_name=metadata.branch,
                detached=detached,
            ),
            GitObservationDetails(
                "worktree_status",
                commit_sha=metadata.commit_sha,
                ref_name=metadata.branch,
                detached=detached,
                dirty=metadata.dirty,
                diff_artifact_id=diff_artifact_id,
            ),
        ]
        if diff_artifact_id is not None:
            details.append(
                GitObservationDetails(
                    "diff",
                    commit_sha=metadata.commit_sha,
                    base_commit_sha=self._diff_base_commit_sha,
                    diff_artifact_id=diff_artifact_id,
                )
            )
        details.append(
            GitObservationDetails(
                "shallow_state",
                shallow=self._shallow,
                boundary_commit_shas=self._boundary_commit_shas,
            )
        )
        self._append_details(tuple(details))

    def record_ancestry(self, evidence: CommitAncestryEvidence) -> None:
        if type(evidence) is not CommitAncestryEvidence:
            _invalid("evidence must be exactly CommitAncestryEvidence")
        object_shas = {evidence.current_commit_sha}
        object_shas.update(anchor for anchor, _result in evidence.commit_relations)
        details = [
            GitObservationDetails(
                "object_availability",
                object_sha=object_sha,
                object_type="commit",
                availability="available",
            )
            for object_sha in sorted(object_shas)
        ]
        details.extend(
            GitObservationDetails(
                "ancestry",
                ancestor_sha=anchor,
                descendant_sha=evidence.current_commit_sha,
                relation="ancestor" if result else "not_ancestor",
            )
            for anchor, result in evidence.commit_relations
        )
        self._append_details(tuple(details))

    def record_ancestry_failure(
        self,
        ancestor_sha: str,
        descendant_sha: str,
        reason: GitObjectUnavailableReason,
    ) -> None:
        details = tuple(
            GitObservationDetails(
                "object_availability",
                object_sha=object_sha,
                object_type="commit",
                availability="unknown",
                unavailable_reason=reason,
            )
            for object_sha in sorted({ancestor_sha, descendant_sha})
        )
        self._append_details(details)

    def _preflight(self) -> None:
        if (
            type(self._next_sequence) is not int
            or not 1 <= self._next_sequence <= GIT_OBSERVATION_MAX_SEQUENCE
        ):
            _invalid("first_sequence is invalid")
        if (
            type(self._next_global_position) is not int
            or not 1 <= self._next_global_position <= EVENT_MAX_VERSION
        ):
            _invalid("first_global_position is invalid")
        if self._parent_event is None:
            if self._next_sequence != 1:
                _invalid("new Git observation stream must begin at sequence one")
        else:
            parent = parse_git_observation(self._parent_event)
            if self._next_sequence != parent.sequence + 1:
                _invalid("first_sequence does not continue parent event")
            if self._next_global_position <= self._parent_event.global_position:
                _invalid("first_global_position must follow parent event")
        probe = GitObservationRecordRef(
            trace_id=self._trace_id,
            run_id=self._run_id,
            repository_id=self._trusted_context.repository_id,
            checkout_alias=self._checkout_alias,
            sequence=self._next_sequence,
            occurred_at=self._occurred_at,
            authorization_event_id=self._trusted_context.authorization_decision_id,
            source=self._source,
            details=GitObservationDetails(
                "shallow_state",
                shallow=self._shallow,
                boundary_commit_shas=self._boundary_commit_shas,
            ),
            artifact_refs=(),
            classification=self._classification,
            retention_policy_id=self._retention_policy_id,
            git_version=self._git_version,
            runner_name=self._runner_name,
            runner_version=self._runner_version,
            algorithm_name=self._algorithm_name,
            algorithm_version=self._algorithm_version,
        )
        _canonical_timestamp(self._recorded_at, "recorded_at")
        if self._remote_sha256 is not None:
            _digest(self._remote_sha256, "remote_sha256")
        if self._diff_artifact_ref is not None and (
            type(self._diff_artifact_ref) is not EventArtifactRef
        ):
            _invalid("diff_artifact_ref must be exactly EventArtifactRef")
        if self._diff_base_commit_sha is not None:
            _commit_sha(self._diff_base_commit_sha, "diff_base_commit_sha")
        del probe

    def _append_details(self, details: tuple[GitObservationDetails, ...]) -> None:
        if not details:
            return
        with self._lock:
            if self._pending_batches:
                _invalid("pending Git observation batches must be resumed first")
            if (
                self._next_sequence + len(details) - 1
                > GIT_OBSERVATION_MAX_SEQUENCE
                or self._next_global_position + len(details) - 1
                > EVENT_MAX_VERSION
            ):
                _invalid("Git observation recording exceeds the sequence boundary")
            records = tuple(
                self._record(detail, self._next_sequence + offset)
                for offset, detail in enumerate(details)
            )
            pending_batches: list[_PendingGitObservationBatch] = []
            parent_event = self._parent_event
            first_global_position = self._next_global_position
            for start in range(0, len(records), EVENT_LEDGER_MAX_APPEND_BATCH):
                chunk = records[start : start + EVENT_LEDGER_MAX_APPEND_BATCH]
                events = build_git_observation_batch(
                    chunk,
                    parent_event=parent_event,
                    first_global_position=first_global_position,
                    trusted_context=self._trusted_context,
                    recorded_at=self._recorded_at,
                )
                pending_batches.append(
                    _PendingGitObservationBatch(events, parent_event)
                )
                parent_event = events[-1]
                first_global_position += len(events)
            self._pending_batches = tuple(pending_batches)
            self._pending_batch_index = 0
            self._drain_pending_batches()

    def _drain_pending_batches(self) -> None:
        while self._pending_batch_index < len(self._pending_batches):
            pending = self._pending_batches[self._pending_batch_index]
            commit = append_git_observation_batch(
                self._ledger,
                pending.events,
                parent_event=pending.parent_event,
            )
            self._commits.append(commit)
            self._parent_event = pending.events[-1]
            self._next_sequence += len(pending.events)
            self._next_global_position += len(pending.events)
            self._pending_batch_index += 1
        self._pending_batches = ()
        self._pending_batch_index = 0

    def _record(
        self,
        details: GitObservationDetails,
        sequence: int,
    ) -> GitObservationRecordRef:
        artifact_refs = (
            ()
            if details.diff_artifact_id is None
            else cast(tuple[EventArtifactRef, ...], (self._diff_artifact_ref,))
        )
        return GitObservationRecordRef(
            trace_id=self._trace_id,
            run_id=self._run_id,
            repository_id=self._trusted_context.repository_id,
            checkout_alias=self._checkout_alias,
            sequence=sequence,
            occurred_at=self._occurred_at,
            authorization_event_id=self._trusted_context.authorization_decision_id,
            source=self._source,
            details=details,
            artifact_refs=artifact_refs,
            classification=self._classification,
            retention_policy_id=self._retention_policy_id,
            git_version=self._git_version,
            runner_name=self._runner_name,
            runner_version=self._runner_version,
            algorithm_name=self._algorithm_name,
            algorithm_version=self._algorithm_version,
        )


def git_observation_payload_schema(event_type: str) -> dict[str, object]:
    kind = _KIND_BY_EVENT_TYPE.get(event_type)
    if kind is None:
        _invalid("event_type is not a Git observation")
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    }
    code = {
        "type": "string",
        "minLength": 1,
        "maxLength": 256,
        "pattern": _EVIDENCE_VERSION_PATTERN,
    }
    timestamp = {
        "type": "string",
        "minLength": 20,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }
    optional_event_id = {
        "oneOf": [
            {
                "type": "string",
                "minLength": 5,
                "maxLength": 128,
                "pattern": r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$",
            },
            {"type": "null"},
        ]
    }
    properties: dict[str, object] = {
        "contract_version": {"const": GIT_OBSERVATION_CONTRACT_VERSION},
        "trace_id": identifier,
        "run_id": identifier,
        "repository_id": identifier,
        "checkout_alias": identifier,
        "sequence": {
            "type": "integer",
            "minimum": 1,
            "maximum": GIT_OBSERVATION_MAX_SEQUENCE,
        },
        "observation_kind": {"const": kind},
        "occurred_at": timestamp,
        "authorization_event_id": identifier,
        "git_version": code,
        "runner_name": identifier,
        "runner_version": code,
        "algorithm_name": identifier,
        "algorithm_version": code,
        "artifact_ids": {
            "type": "array",
            "maxItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "pattern": r"^artifact_sha256_[0-9a-f]{64}$",
            },
        },
        "details": _details_schema(kind),
        "causation_event_id": optional_event_id,
        "batch_first_sequence": {
            "type": "integer",
            "minimum": 1,
            "maximum": GIT_OBSERVATION_MAX_SEQUENCE,
        },
        "batch_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": EVENT_LEDGER_MAX_APPEND_BATCH,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def git_observation_stream_id(
    repository_id: str,
    trace_id: str,
    run_id: str,
    checkout_alias: str,
    trusted_context: EventTrustedContext,
) -> str:
    for name, value in (
        ("repository_id", repository_id),
        ("trace_id", trace_id),
        ("run_id", run_id),
        ("checkout_alias", checkout_alias),
    ):
        _identifier(value, name)
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if repository_id != trusted_context.repository_id:
        _invalid("repository_id must match trusted_context")
    digest = _domain_sha256(
        b"tbm.git-observation-stream.v1\x00",
        {
            **_partition_dict(trusted_context),
            "trace_id": trace_id,
            "run_id": run_id,
            "checkout_alias": checkout_alias,
        },
    )
    return "git_observation_" + digest.removeprefix("sha256:")[:48]


def git_observation_event_id(
    stream_id: str,
    sequence: int,
    trusted_context: EventTrustedContext,
) -> str:
    _identifier(stream_id, "stream_id")
    if type(sequence) is not int or not 1 <= sequence <= GIT_OBSERVATION_MAX_SEQUENCE:
        _invalid("sequence must be a bounded positive integer")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    digest = _domain_sha256(
        b"tbm.git-observation-event-identity.v1\x00",
        {
            **_partition_dict(trusted_context),
            "stream_id": stream_id,
            "sequence": sequence,
        },
    )
    return "evt_git_" + digest.removeprefix("sha256:")


def build_git_observation(
    reference: GitObservationRecordRef,
    *,
    parent_event: CanonicalEvent | None,
    global_position: int,
    trusted_context: EventTrustedContext,
    recorded_at: str,
) -> CanonicalEvent:
    return build_git_observation_batch(
        (reference,),
        parent_event=parent_event,
        first_global_position=global_position,
        trusted_context=trusted_context,
        recorded_at=recorded_at,
    )[0]


def build_git_observation_batch(
    references: tuple[GitObservationRecordRef, ...],
    *,
    parent_event: CanonicalEvent | None,
    first_global_position: int,
    trusted_context: EventTrustedContext,
    recorded_at: str,
) -> tuple[CanonicalEvent, ...]:
    if (
        type(references) is not tuple
        or not 1 <= len(references) <= EVENT_LEDGER_MAX_APPEND_BATCH
        or any(type(item) is not GitObservationRecordRef for item in references)
    ):
        _invalid(
            "references must be a bounded non-empty tuple of GitObservationRecordRef"
        )
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if (
        type(first_global_position) is not int
        or not 1 <= first_global_position <= EVENT_MAX_VERSION
        or first_global_position + len(references) - 1 > EVENT_MAX_VERSION
    ):
        _invalid("first_global_position does not fit the Git observation batch")
    canonical_recorded_at = _canonical_timestamp(recorded_at, "recorded_at")
    if recorded_at != canonical_recorded_at:
        _invalid("recorded_at must use canonical UTC RFC 3339")
    first_reference = references[0]
    if any(
        reference.trace_id != first_reference.trace_id
        or reference.run_id != first_reference.run_id
        or reference.repository_id != first_reference.repository_id
        or reference.checkout_alias != first_reference.checkout_alias
        or reference.sequence != first_reference.sequence + offset
        or reference.authorization_event_id != trusted_context.authorization_decision_id
        or reference.repository_id != trusted_context.repository_id
        for offset, reference in enumerate(references)
    ):
        _invalid("Git observation batch identity or sequence is invalid")
    stream_id = git_observation_stream_id(
        first_reference.repository_id,
        first_reference.trace_id,
        first_reference.run_id,
        first_reference.checkout_alias,
        trusted_context,
    )
    batch_identity_sha256 = _batch_identity_sha256(
        stream_id,
        first_reference.sequence,
        len(references),
        trusted_context,
    )
    command_sha256 = _batch_command_sha256(
        references,
        recorded_at=canonical_recorded_at,
        trusted_context=trusted_context,
    )
    events: list[CanonicalEvent] = []
    current_parent = parent_event
    for offset, reference in enumerate(references):
        event = _build_git_observation(
            reference,
            stream_id=stream_id,
            parent_event=current_parent,
            global_position=first_global_position + offset,
            trusted_context=trusted_context,
            recorded_at=canonical_recorded_at,
            batch_first_sequence=first_reference.sequence,
            batch_size=len(references),
            batch_identity_sha256=batch_identity_sha256,
            command_sha256=command_sha256,
        )
        events.append(event)
        current_parent = event
    result = tuple(events)
    verify_git_observation_batch(result, parent_event=parent_event)
    return result


def parse_git_observation(event: CanonicalEvent) -> GitObservationRecordRef:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    kind = _KIND_BY_EVENT_TYPE.get(event.event_type)
    if (
        kind is None
        or event.event_version != GIT_OBSERVATION_VERSION
        or event.event_kind != "observation"
        or event.origin != "native"
        or event.source is None
        or event.stream_type != GIT_OBSERVATION_STREAM_TYPE
        or event.payload_schema != f"{event.event_type}.v1"
        or event.occurred_at is None
    ):
        _invalid("Git observation envelope is invalid")
    payload = _plain_mapping(event.payload)
    expected_fields = {
        "contract_version",
        "trace_id",
        "run_id",
        "repository_id",
        "checkout_alias",
        "sequence",
        "observation_kind",
        "occurred_at",
        "authorization_event_id",
        "git_version",
        "runner_name",
        "runner_version",
        "algorithm_name",
        "algorithm_version",
        "artifact_ids",
        "details",
        "causation_event_id",
        "batch_first_sequence",
        "batch_size",
    }
    if set(payload) != expected_fields:
        _invalid("Git observation payload fields are invalid")
    if (
        payload["contract_version"] != GIT_OBSERVATION_CONTRACT_VERSION
        or payload["observation_kind"] != kind
        or payload["sequence"] != event.stream_version
        or payload["occurred_at"] != event.occurred_at
        or payload["repository_id"] != event.repository_id
        or payload["authorization_event_id"] != event.authorization_decision_id
        or payload["causation_event_id"] != event.causation_id
        or payload["artifact_ids"] != [item.artifact_id for item in event.artifact_refs]
    ):
        _invalid("Git observation linkage is invalid")
    details = _parse_details(kind, payload["details"])
    reference = GitObservationRecordRef(
        trace_id=cast(str, payload["trace_id"]),
        run_id=cast(str, payload["run_id"]),
        repository_id=cast(str, payload["repository_id"]),
        checkout_alias=cast(str, payload["checkout_alias"]),
        sequence=cast(int, payload["sequence"]),
        occurred_at=cast(str, payload["occurred_at"]),
        authorization_event_id=cast(str, payload["authorization_event_id"]),
        source=event.source,
        details=details,
        artifact_refs=event.artifact_refs,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        git_version=cast(str, payload["git_version"]),
        runner_name=cast(str, payload["runner_name"]),
        runner_version=cast(str, payload["runner_version"]),
        algorithm_name=cast(str, payload["algorithm_name"]),
        algorithm_version=cast(str, payload["algorithm_version"]),
        causation_event_id=cast(str | None, payload["causation_event_id"]),
    )
    _verify_deterministic_envelope(event, reference)
    _first_sequence, batch_size = _batch_descriptor(payload, reference.sequence)
    if batch_size == 1 and event.request_sha256 != _batch_command_sha256(
        (reference,),
        recorded_at=event.recorded_at,
        trusted_context=_trusted_context_from_event(event),
    ):
        _invalid("Git observation singleton command digest is invalid")
    return reference


def verify_git_observation_parent(
    event: CanonicalEvent,
    parent_event: CanonicalEvent | None,
) -> None:
    reference = parse_git_observation(event)
    if reference.sequence == 1:
        if parent_event is not None:
            _invalid("the first Git observation cannot name a parent")
        try:
            verify_event_parent(event, None)
        except EventV1ContractError as error:
            raise GitObservationV1Error(
                "TBM_GIT_OBSERVATION_INVALID",
                "Git observation parent envelope is invalid",
            ) from error
        return
    if type(parent_event) is not CanonicalEvent:
        _invalid("non-first Git observation requires its parent")
    parent_reference = parse_git_observation(parent_event)
    if (
        reference.trace_id != parent_reference.trace_id
        or reference.run_id != parent_reference.run_id
        or reference.repository_id != parent_reference.repository_id
        or reference.checkout_alias != parent_reference.checkout_alias
        or reference.sequence != parent_reference.sequence + 1
    ):
        _invalid("Git observation parent identity is invalid")
    try:
        verify_event_parent(event, parent_event)
    except EventV1ContractError as error:
        raise GitObservationV1Error(
            "TBM_GIT_OBSERVATION_INVALID",
            "Git observation parent envelope is invalid",
        ) from error


def verify_git_observation_batch(
    events: tuple[CanonicalEvent, ...],
    *,
    parent_event: CanonicalEvent | None,
) -> None:
    if (
        type(events) is not tuple
        or not 1 <= len(events) <= EVENT_LEDGER_MAX_APPEND_BATCH
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _invalid("events must be a bounded non-empty tuple of CanonicalEvent")
    references = tuple(parse_git_observation(event) for event in events)
    first_sequence = references[0].sequence
    descriptors = tuple(
        _batch_descriptor(_plain_mapping(event.payload), reference.sequence)
        for event, reference in zip(events, references, strict=True)
    )
    expected_descriptor = (first_sequence, len(events))
    if any(descriptor != expected_descriptor for descriptor in descriptors):
        _invalid("Git observation batch descriptor is inconsistent")
    first_event = events[0]
    trusted_context = _trusted_context_from_event(first_event)
    expected_identity = _batch_identity_sha256(
        first_event.stream_id,
        first_sequence,
        len(events),
        trusted_context,
    )
    expected_command = _batch_command_sha256(
        references,
        recorded_at=first_event.recorded_at,
        trusted_context=trusted_context,
    )
    if any(
        reference.trace_id != references[0].trace_id
        or reference.run_id != references[0].run_id
        or reference.repository_id != references[0].repository_id
        or reference.checkout_alias != references[0].checkout_alias
        or reference.sequence != first_sequence + offset
        or event.global_position != first_event.global_position + offset
        or event.recorded_at != first_event.recorded_at
        or event.idempotency_key_sha256 != expected_identity
        or event.request_sha256 != expected_command
        or event.request_id
        != "git_observation_request_" + expected_identity.removeprefix("sha256:")[:40]
        or _trusted_context_from_event(event) != trusted_context
        for offset, (event, reference) in enumerate(
            zip(events, references, strict=True)
        )
    ):
        _invalid("Git observation batch command is invalid")
    current_parent = parent_event
    for event in events:
        verify_git_observation_parent(event, current_parent)
        current_parent = event


def append_git_observation_batch(
    ledger: EventLedgerAtomicAppendPort,
    events: tuple[CanonicalEvent, ...],
    *,
    parent_event: CanonicalEvent | None,
) -> LedgerAppendCommit:
    try:
        access_context = ledger.access_context
        append_once = ledger.append_once
    except Exception as error:
        raise GitObservationV1Error(
            "TBM_GIT_OBSERVATION_INVALID",
            "ledger must support trusted atomic append",
        ) from error
    if type(access_context) is not LedgerAccessContext or not callable(append_once):
        _invalid("ledger must support trusted atomic append")
    verify_git_observation_batch(events, parent_event=parent_event)
    if access_context.event_trusted_context() != _trusted_context_from_event(events[0]):
        _invalid("ledger trusted context does not match the Git observation batch")
    expected_version = 0 if parent_event is None else parent_event.stream_version
    return append_once(
        events[0].stream_id,
        expected_version,
        events,
        LedgerIdempotency(
            events[0].idempotency_key_sha256,
            events[0].request_sha256,
        ),
    )


def _build_git_observation(
    reference: GitObservationRecordRef,
    *,
    stream_id: str,
    parent_event: CanonicalEvent | None,
    global_position: int,
    trusted_context: EventTrustedContext,
    recorded_at: str,
    batch_first_sequence: int,
    batch_size: int,
    batch_identity_sha256: str,
    command_sha256: str,
) -> CanonicalEvent:
    if parent_event is None:
        if reference.sequence != 1:
            _invalid("the first Git observation sequence must be one")
        previous_sha256 = None
    else:
        parent_reference = parse_git_observation(parent_event)
        if (
            reference.trace_id != parent_reference.trace_id
            or reference.run_id != parent_reference.run_id
            or reference.repository_id != parent_reference.repository_id
            or reference.checkout_alias != parent_reference.checkout_alias
            or reference.sequence != parent_reference.sequence + 1
        ):
            _invalid("Git observation does not continue its parent stream")
        previous_sha256 = parent_event.event_sha256
    payload = {
        **reference.to_dict(),
        "batch_first_sequence": batch_first_sequence,
        "batch_size": batch_size,
    }
    try:
        return build_canonical_event(
            event_id=git_observation_event_id(
                stream_id,
                reference.sequence,
                trusted_context,
            ),
            event_type=reference.event_type,
            event_version=GIT_OBSERVATION_VERSION,
            event_kind="observation",
            origin="native",
            source=reference.source,
            stream_id=stream_id,
            stream_type=GIT_OBSERVATION_STREAM_TYPE,
            stream_version=reference.sequence,
            global_position=global_position,
            trusted_context=trusted_context,
            request_id=(
                "git_observation_request_"
                + batch_identity_sha256.removeprefix("sha256:")[:40]
            ),
            idempotency_key_sha256=batch_identity_sha256,
            request_sha256=command_sha256,
            correlation_id=_correlation_id(reference.trace_id, reference.run_id),
            causation_id=reference.causation_event_id,
            occurred_at=reference.occurred_at,
            recorded_at=recorded_at,
            producer=GIT_OBSERVATION_PRODUCER,
            producer_version=GIT_OBSERVATION_PRODUCER_VERSION,
            payload_schema=f"{reference.event_type}.v1",
            previous_stream_event_sha256=previous_sha256,
            classification=reference.classification,
            retention_policy_id=reference.retention_policy_id,
            artifact_refs=reference.artifact_refs,
            payload=payload,
        )
    except EventV1ContractError as error:
        raise GitObservationV1Error(
            "TBM_GIT_OBSERVATION_INVALID",
            "Git observation envelope is invalid",
        ) from error


def _verify_deterministic_envelope(
    event: CanonicalEvent,
    reference: GitObservationRecordRef,
) -> None:
    payload = _plain_mapping(event.payload)
    batch_first_sequence, batch_size = _batch_descriptor(
        payload,
        reference.sequence,
    )
    trusted_context = _trusted_context_from_event(event)
    expected_stream_id = git_observation_stream_id(
        reference.repository_id,
        reference.trace_id,
        reference.run_id,
        reference.checkout_alias,
        trusted_context,
    )
    batch_identity_sha256 = _batch_identity_sha256(
        expected_stream_id,
        batch_first_sequence,
        batch_size,
        trusted_context,
    )
    if (
        event.stream_id != expected_stream_id
        or event.event_id
        != git_observation_event_id(
            expected_stream_id,
            reference.sequence,
            trusted_context,
        )
        or event.idempotency_key_sha256 != batch_identity_sha256
        or event.request_id
        != "git_observation_request_"
        + batch_identity_sha256.removeprefix("sha256:")[:40]
        or event.correlation_id != _correlation_id(reference.trace_id, reference.run_id)
        or event.producer != GIT_OBSERVATION_PRODUCER
        or event.producer_version != GIT_OBSERVATION_PRODUCER_VERSION
    ):
        _invalid("Git observation deterministic envelope is invalid")


def _details_schema(kind: GitObservationKind) -> dict[str, object]:
    commit_sha = {
        "type": "string",
        "pattern": r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    }
    optional_commit_sha = {"oneOf": [commit_sha, {"type": "null"}]}
    optional_ref = {
        "oneOf": [
            {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": _GIT_REF_NAME_PATTERN,
            },
            {"type": "null"},
        ]
    }
    optional_digest = {
        "oneOf": [
            {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
            {"type": "null"},
        ]
    }
    optional_artifact_id = {
        "oneOf": [
            {
                "type": "string",
                "pattern": r"^artifact_sha256_[0-9a-f]{64}$",
            },
            {"type": "null"},
        ]
    }
    properties_by_kind: dict[GitObservationKind, dict[str, object]] = {
        "checkout": {
            "commit_sha": commit_sha,
            "ref_name": optional_ref,
            "detached": {"type": "boolean"},
            "remote_sha256": optional_digest,
        },
        "commit": {"commit_sha": commit_sha},
        "ref": {
            "commit_sha": commit_sha,
            "ref_name": optional_ref,
            "detached": {"type": "boolean"},
        },
        "worktree_status": {
            "commit_sha": commit_sha,
            "ref_name": optional_ref,
            "detached": {"type": "boolean"},
            "dirty": {"type": "boolean"},
            "diff_artifact_id": optional_artifact_id,
        },
        "diff": {
            "commit_sha": commit_sha,
            "base_commit_sha": optional_commit_sha,
            "diff_artifact_id": {
                "type": "string",
                "pattern": r"^artifact_sha256_[0-9a-f]{64}$",
            },
        },
        "ancestry": {
            "ancestor_sha": commit_sha,
            "descendant_sha": commit_sha,
            "relation": {"enum": sorted(_ANCESTRY_RELATIONS)},
        },
        "object_availability": {
            "object_sha": commit_sha,
            "object_type": {"enum": sorted(_OBJECT_TYPES)},
            "availability": {"enum": sorted(_OBJECT_AVAILABILITY)},
            "unavailable_reason": {
                "oneOf": [
                    {"enum": sorted(_UNAVAILABLE_REASONS)},
                    {"type": "null"},
                ]
            },
        },
        "shallow_state": {
            "shallow": {"type": "boolean"},
            "boundary_commit_shas": {
                "type": "array",
                "maxItems": GIT_OBSERVATION_MAX_BOUNDARY_COMMITS,
                "uniqueItems": True,
                "items": commit_sha,
            },
        },
    }
    properties = properties_by_kind[kind]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _parse_details(kind: GitObservationKind, value: object) -> GitObservationDetails:
    obj = _plain_mapping(value)
    expected_fields = set(cast(dict[str, object], _details_schema(kind)["properties"]))
    if set(obj) != expected_fields:
        _invalid("Git observation details fields are invalid")
    return GitObservationDetails(
        kind,
        commit_sha=cast(str | None, obj.get("commit_sha")),
        base_commit_sha=cast(str | None, obj.get("base_commit_sha")),
        ref_name=cast(str | None, obj.get("ref_name")),
        detached=cast(bool | None, obj.get("detached")),
        dirty=cast(bool | None, obj.get("dirty")),
        remote_sha256=cast(str | None, obj.get("remote_sha256")),
        diff_artifact_id=cast(str | None, obj.get("diff_artifact_id")),
        ancestor_sha=cast(str | None, obj.get("ancestor_sha")),
        descendant_sha=cast(str | None, obj.get("descendant_sha")),
        relation=cast(GitAncestryRelation | None, obj.get("relation")),
        object_sha=cast(str | None, obj.get("object_sha")),
        object_type=cast(GitObjectType | None, obj.get("object_type")),
        availability=cast(GitObjectAvailability | None, obj.get("availability")),
        unavailable_reason=cast(
            GitObjectUnavailableReason | None,
            obj.get("unavailable_reason"),
        ),
        shallow=cast(bool | None, obj.get("shallow")),
        boundary_commit_shas=tuple(
            cast(list[str], obj.get("boundary_commit_shas", []))
        ),
    )


def _detail_field_values(details: GitObservationDetails) -> dict[str, object]:
    return {
        "commit_sha": details.commit_sha,
        "base_commit_sha": details.base_commit_sha,
        "ref_name": details.ref_name,
        "detached": details.detached,
        "dirty": details.dirty,
        "remote_sha256": details.remote_sha256,
        "diff_artifact_id": details.diff_artifact_id,
        "ancestor_sha": details.ancestor_sha,
        "descendant_sha": details.descendant_sha,
        "relation": details.relation,
        "object_sha": details.object_sha,
        "object_type": details.object_type,
        "availability": details.availability,
        "unavailable_reason": details.unavailable_reason,
        "shallow": details.shallow,
        "boundary_commit_shas": details.boundary_commit_shas,
    }


def _forbid_detail_fields(
    details: GitObservationDetails,
    allowed: set[str],
) -> None:
    for name, value in _detail_field_values(details).items():
        if name in allowed:
            continue
        if value is not None and value != ():
            _invalid(f"{name} is not valid for {details.observation_kind}")


def _ref_matches_detached(ref_name: str | None, detached: bool) -> None:
    if detached and ref_name is not None:
        _invalid("detached observation cannot name ref_name")
    if not detached and ref_name is None:
        _invalid("attached observation requires ref_name")


def _require(value: object, message: str) -> None:
    if value is None:
        _invalid(message)


def _require_bool(value: object, message: str) -> None:
    if type(value) is not bool:
        _invalid(message)


def _batch_identity_sha256(
    stream_id: str,
    first_sequence: int,
    batch_size: int,
    trusted_context: EventTrustedContext,
) -> str:
    return _domain_sha256(
        b"tbm.git-observation-batch-identity.v1\x00",
        {
            **_partition_dict(trusted_context),
            "stream_id": stream_id,
            "first_sequence": first_sequence,
            "batch_size": batch_size,
        },
    )


def _batch_command_sha256(
    references: tuple[GitObservationRecordRef, ...],
    *,
    recorded_at: str,
    trusted_context: EventTrustedContext,
) -> str:
    return _domain_sha256(
        b"tbm.git-observation-batch-command.v1\x00",
        {
            "records": [reference.to_projection_dict() for reference in references],
            "recorded_at": recorded_at,
            "trusted_context": {
                "organization_id": trusted_context.organization_id,
                "tenant_id": trusted_context.tenant_id,
                "repository_id": trusted_context.repository_id,
                "environment_id": trusted_context.environment_id,
                "principal_id": trusted_context.principal_id,
                "agent_client_id": trusted_context.agent_client_id,
                "actor_type": trusted_context.actor_type,
                "actor_id": trusted_context.actor_id,
                "authorization_decision_id": (
                    trusted_context.authorization_decision_id
                ),
            },
        },
    )


def _batch_descriptor(
    payload: Mapping[str, object],
    sequence: int,
) -> tuple[int, int]:
    first_sequence = payload.get("batch_first_sequence")
    batch_size = payload.get("batch_size")
    if (
        type(first_sequence) is not int
        or type(batch_size) is not int
        or not 1 <= first_sequence <= GIT_OBSERVATION_MAX_SEQUENCE
        or not 1 <= batch_size <= EVENT_LEDGER_MAX_APPEND_BATCH
        or not first_sequence <= sequence < first_sequence + batch_size
        or first_sequence + batch_size - 1 > GIT_OBSERVATION_MAX_SEQUENCE
    ):
        _invalid("Git observation batch descriptor is invalid")
    return first_sequence, batch_size


def _trusted_context_from_event(event: CanonicalEvent) -> EventTrustedContext:
    return EventTrustedContext(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
        principal_id=event.principal_id,
        agent_client_id=event.agent_client_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        authorization_decision_id=event.authorization_decision_id,
    )


def _partition_dict(trusted_context: EventTrustedContext) -> dict[str, object]:
    return {
        "organization_id": trusted_context.organization_id,
        "tenant_id": trusted_context.tenant_id,
        "repository_id": trusted_context.repository_id,
        "environment_id": trusted_context.environment_id,
    }


def _correlation_id(trace_id: str, run_id: str) -> str:
    digest = _domain_sha256(
        b"tbm.git-observation-correlation.v1\x00",
        {"trace_id": trace_id, "run_id": run_id},
    )
    return "git_observation_correlation_" + digest.removeprefix("sha256:")[:40]


def _plain_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid("Git observation payload is invalid")
    return {str(key): _plain_json(item) for key, item in value.items()}


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    return value


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _git_ref_name(value: object) -> None:
    if type(value) is not str or _GIT_REF_NAME_RE.fullmatch(value) is None:
        _invalid("ref_name is invalid")


def _evidence_version(value: object, name: str) -> None:
    if type(value) is not str or _EVIDENCE_VERSION_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _commit_sha(value: object, name: str) -> None:
    if type(value) is not str or _COMMIT_SHA_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _artifact_id(value: object, name: str) -> None:
    if type(value) is not str or _ARTIFACT_ID_RE.fullmatch(value) is None:
        _invalid(f"{name} is invalid")


def _canonical_timestamp(value: object, name: str) -> str:
    try:
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise GitObservationV1Error(
            "TBM_GIT_OBSERVATION_INVALID",
            f"{name} is invalid",
        ) from error


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


def _invalid(message: str) -> NoReturn:
    raise GitObservationV1Error(
        "TBM_GIT_OBSERVATION_INVALID",
        message,
    )


__all__ = [
    "GIT_CHECKOUT_OBSERVED",
    "GIT_COMMIT_OBSERVED",
    "GIT_COMMIT_RELATION_OBSERVED",
    "GIT_DIFF_CAPTURED",
    "GIT_OBJECT_AVAILABILITY_OBSERVED",
    "GIT_OBSERVATION_CONTRACT_VERSION",
    "GIT_OBSERVATION_MAX_BOUNDARY_COMMITS",
    "GIT_OBSERVATION_MAX_SEQUENCE",
    "GIT_OBSERVATION_PRODUCER",
    "GIT_OBSERVATION_PRODUCER_VERSION",
    "GIT_OBSERVATION_STREAM_TYPE",
    "GIT_OBSERVATION_TYPES",
    "GIT_OBSERVATION_VERSION",
    "GIT_REF_OBSERVED",
    "GIT_SHALLOW_STATE_OBSERVED",
    "GIT_WORKTREE_STATUS_OBSERVED",
    "GitAncestryRelation",
    "GitObjectAvailability",
    "GitObjectType",
    "GitObjectUnavailableReason",
    "GitObservationDetails",
    "GitObservationEventRecorder",
    "GitObservationKind",
    "GitObservationRecordRef",
    "GitObservationRecorder",
    "GitObservationV1Error",
    "append_git_observation_batch",
    "build_git_observation",
    "build_git_observation_batch",
    "git_observation_event_id",
    "git_observation_payload_schema",
    "git_observation_stream_id",
    "parse_git_observation",
    "verify_git_observation_batch",
    "verify_git_observation_parent",
]
