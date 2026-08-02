from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from typing import Literal, NoReturn, cast

from ._timestamps import parse_rfc3339
from .event_v1 import CanonicalEvent, verify_event_parent
from .evidence_v3 import StructuredRegressionEvidence
from .fix_evidence_v3 import FixEvidence
from .git_observation_v1 import (
    GIT_ANCESTRY_OBSERVED,
    GIT_CHECKOUT_OBSERVED,
    GIT_COMMIT_OBSERVED,
    GIT_DIFF_OBSERVED,
    GIT_OBJECT_AVAILABILITY_OBSERVED,
    GIT_OBSERVATION_PROTOCOL_VERSION,
    GIT_OBSERVATION_STREAM_TYPE,
    GIT_OBSERVATION_TYPES,
    GIT_REF_OBSERVED,
    GIT_SHALLOW_STATE_OBSERVED,
    GitAncestryStatus,
    GitObjectAvailabilityStatus,
    GitObjectFormat,
    GitShallowState,
    build_git_observation_registry,
    git_observation_stream_id,
    verify_git_observation_event,
)
from .ledger_port_v1 import LedgerAccessContext
from .models import CommitAncestryEvidence, PRCaseProvenance
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerV1Error,
    execute_reducer_step,
    initial_reducer_state,
)


GIT_GRAPH_PROTOCOL_VERSION = "tbm.git-graph.v1"
GIT_GRAPH_REDUCER_ID = "git-graph-current"
GIT_GRAPH_PROJECTION = "git_graph_current_v1"
GIT_GRAPH_MAX_EVENTS = 10_000
GIT_GRAPH_MAX_COMMITS = 20_000
GIT_GRAPH_MAX_EDGES = 50_000
GIT_GRAPH_MAX_EVIDENCE = 1_000
GIT_GRAPH_MAX_PR_CASES = 1_000

GitRelationConfidence = Literal[
    "independently_verified",
    "locally_observed",
    "degraded",
    "indeterminate",
]
GitEvidenceRelationKind = Literal["source_to_fix", "fix_to_verification"]
GitPRAnchorEndpoint = Literal["old", "new", "both", "legacy"]

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OID_PATTERNS = {
    "sha1": re.compile(r"^[0-9a-f]{40}$"),
    "sha256": re.compile(r"^[0-9a-f]{64}$"),
}
_CONFIDENCE_VALUES = {
    "independently_verified",
    "locally_observed",
    "degraded",
    "indeterminate",
}


class GitGraphV1Error(ReducerV1Error):
    """Stable failure for the storage-neutral Git graph projection."""


@dataclass(frozen=True)
class GitGraphObservation:
    event_sha256: str
    stream_version: int
    global_position: int
    observed_at: str
    source_system: str
    source_record_id: str
    runner_id: str
    runner_version: str
    algorithm_id: str
    algorithm_version: str
    git_version: str

    def __post_init__(self) -> None:
        _digest(self.event_sha256, "event_sha256")
        _positive_int(self.stream_version, "stream_version")
        _positive_int(self.global_position, "global_position")
        _timestamp(self.observed_at, "observed_at")
        for name in (
            "source_system",
            "source_record_id",
            "runner_id",
            "runner_version",
            "algorithm_id",
            "algorithm_version",
            "git_version",
        ):
            _bounded_string(getattr(self, name), name)

    def to_dict(self) -> dict[str, object]:
        return {
            "event_sha256": self.event_sha256,
            "stream_version": self.stream_version,
            "global_position": self.global_position,
            "observed_at": self.observed_at,
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "git_version": self.git_version,
        }


@dataclass(frozen=True)
class GitGraphRepository:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    checkout_id: str
    root_sha256: str | None
    observed_repository_name: str | None

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "checkout_id",
        ):
            _bounded_string(getattr(self, name), name)
        if self.root_sha256 is not None:
            _digest(self.root_sha256, "root_sha256")
        if self.observed_repository_name is not None:
            _bounded_string(
                self.observed_repository_name,
                "observed_repository_name",
                max_chars=256,
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "checkout_id": self.checkout_id,
            "root_sha256": self.root_sha256,
            "observed_repository_name": self.observed_repository_name,
        }


@dataclass(frozen=True)
class GitGraphCommitNode:
    commit_oid: str
    tree_oid: str | None
    parent_oids: tuple[str, ...]
    availability: GitObjectAvailabilityStatus | None
    observed: bool
    last_observation: GitGraphObservation | None

    def __post_init__(self) -> None:
        _generic_oid(self.commit_oid, "commit_oid")
        if self.tree_oid is not None:
            _matching_oid(self.tree_oid, self.commit_oid, "tree_oid")
        if (
            type(self.parent_oids) is not tuple
            or len(self.parent_oids) > 128
            or len(self.parent_oids) != len(set(self.parent_oids))
        ):
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "parent_oids must be a bounded unique tuple",
            )
        for parent_oid in self.parent_oids:
            _matching_oid(parent_oid, self.commit_oid, "parent_oid")
        if self.availability not in {None, "present", "missing", "unknown"}:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "availability is invalid")
        if type(self.observed) is not bool:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "observed is invalid")
        if self.observed != (self.last_observation is not None and self.tree_oid is not None):
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "observed commit nodes require their exact observation and tree",
            )
        if self.last_observation is not None and type(self.last_observation) is not GitGraphObservation:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "commit observation is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "commit_oid": self.commit_oid,
            "tree_oid": self.tree_oid,
            "parent_oids": list(self.parent_oids),
            "availability": self.availability,
            "observed": self.observed,
            "last_observation": (
                None if self.last_observation is None else self.last_observation.to_dict()
            ),
        }


@dataclass(frozen=True)
class GitGraphParentRelation:
    parent_oid: str
    child_oid: str
    confidence: GitRelationConfidence
    last_observation: GitGraphObservation
    last_validated_at: str | None

    def __post_init__(self) -> None:
        _generic_oid(self.parent_oid, "parent_oid")
        _matching_oid(self.child_oid, self.parent_oid, "child_oid")
        _confidence(self.confidence)
        if type(self.last_observation) is not GitGraphObservation:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "parent observation is invalid")
        if self.last_validated_at is not None:
            _timestamp(self.last_validated_at, "last_validated_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_oid": self.parent_oid,
            "child_oid": self.child_oid,
            "confidence": self.confidence,
            "last_observation": self.last_observation.to_dict(),
            "last_validated_at": self.last_validated_at,
        }


@dataclass(frozen=True)
class GitGraphAncestryRelation:
    anchor_oid: str
    current_oid: str
    reported_status: GitAncestryStatus
    status: GitAncestryStatus
    confidence: GitRelationConfidence
    last_observation: GitGraphObservation
    last_validated_at: str | None

    def __post_init__(self) -> None:
        _generic_oid(self.anchor_oid, "anchor_oid")
        _matching_oid(self.current_oid, self.anchor_oid, "current_oid")
        if self.reported_status not in {"ancestor", "not_ancestor", "unknown"}:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "reported status is invalid")
        if self.status not in {"ancestor", "not_ancestor", "unknown"}:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "ancestry status is invalid")
        _confidence(self.confidence)
        if self.status == "unknown":
            if self.confidence != "indeterminate" or self.last_validated_at is not None:
                _fail(
                    "TBM_GIT_GRAPH_PROJECTION_INVALID",
                    "unknown ancestry must remain indeterminate and unvalidated",
                )
        elif self.confidence != "locally_observed" or self.last_validated_at is None:
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "known ancestry requires local validation",
            )
        if type(self.last_observation) is not GitGraphObservation:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "ancestry observation is invalid")
        if self.last_validated_at is not None:
            _timestamp(self.last_validated_at, "last_validated_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "anchor_oid": self.anchor_oid,
            "current_oid": self.current_oid,
            "reported_status": self.reported_status,
            "status": self.status,
            "confidence": self.confidence,
            "last_observation": self.last_observation.to_dict(),
            "last_validated_at": self.last_validated_at,
        }


@dataclass(frozen=True)
class GitGraphMissingObject:
    object_oid: str
    status: Literal["missing", "unknown"]
    reason: Literal["object_missing", "availability_unknown"]
    last_observation: GitGraphObservation

    def __post_init__(self) -> None:
        _generic_oid(self.object_oid, "object_oid")
        expected_reason = (
            "object_missing" if self.status == "missing" else "availability_unknown"
        )
        if self.status not in {"missing", "unknown"} or self.reason != expected_reason:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "missing object is invalid")
        if type(self.last_observation) is not GitGraphObservation:
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "missing object observation is invalid",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "object_oid": self.object_oid,
            "status": self.status,
            "reason": self.reason,
            "last_observation": self.last_observation.to_dict(),
        }


@dataclass(frozen=True)
class GitGraphEvidenceRelation:
    relation_kind: GitEvidenceRelationKind
    from_commit_oid: str
    to_commit_oid: str
    evidence_id: str
    case_id: str
    source_trace_id: str
    verification_trace_id: str | None
    result: Literal["pass", "fail", "error"] | None
    verified_by: str
    verified_at: str
    confidence: GitRelationConfidence

    def __post_init__(self) -> None:
        if self.relation_kind not in {"source_to_fix", "fix_to_verification"}:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "evidence relation kind is invalid")
        _generic_oid(self.from_commit_oid, "from_commit_oid")
        _matching_oid(self.to_commit_oid, self.from_commit_oid, "to_commit_oid")
        for name in (
            "evidence_id",
            "case_id",
            "source_trace_id",
            "verified_by",
        ):
            _bounded_string(getattr(self, name), name)
        if self.verification_trace_id is not None:
            _bounded_string(self.verification_trace_id, "verification_trace_id")
        if self.result not in {None, "pass", "fail", "error"}:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "evidence result is invalid")
        _timestamp(self.verified_at, "verified_at")
        if self.confidence not in {"independently_verified", "degraded"}:
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "evidence confidence is invalid",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "relation_kind": self.relation_kind,
            "from_commit_oid": self.from_commit_oid,
            "to_commit_oid": self.to_commit_oid,
            "evidence_id": self.evidence_id,
            "case_id": self.case_id,
            "source_trace_id": self.source_trace_id,
            "verification_trace_id": self.verification_trace_id,
            "result": self.result,
            "verified_by": self.verified_by,
            "verified_at": self.verified_at,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class GitGraphPRAnchor:
    anchor_oid: str
    current_oid: str
    case_ids: tuple[str, ...]
    fix_commit_oids: tuple[str, ...]
    matched_endpoints: tuple[GitPRAnchorEndpoint, ...]
    status: GitAncestryStatus
    confidence: GitRelationConfidence
    last_observation: GitGraphObservation | None

    def __post_init__(self) -> None:
        _generic_oid(self.anchor_oid, "anchor_oid")
        _matching_oid(self.current_oid, self.anchor_oid, "current_oid")
        if (
            type(self.case_ids) is not tuple
            or not self.case_ids
            or self.case_ids != tuple(sorted(set(self.case_ids)))
        ):
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "PR case IDs are invalid")
        if (
            type(self.fix_commit_oids) is not tuple
            or self.fix_commit_oids != tuple(sorted(set(self.fix_commit_oids)))
        ):
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "PR fix commits are invalid")
        for fix_commit_oid in self.fix_commit_oids:
            _matching_oid(fix_commit_oid, self.anchor_oid, "fix_commit_oid")
        if (
            type(self.matched_endpoints) is not tuple
            or not self.matched_endpoints
            or self.matched_endpoints != tuple(sorted(set(self.matched_endpoints)))
            or any(
                item not in {"old", "new", "both", "legacy"}
                for item in self.matched_endpoints
            )
        ):
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "PR endpoints are invalid")
        if self.status not in {"ancestor", "not_ancestor", "unknown"}:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "PR anchor status is invalid")
        _confidence(self.confidence)
        if self.status == "unknown" and self.confidence != "indeterminate":
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "unknown PR anchors must remain indeterminate",
            )
        if self.status != "unknown" and self.confidence != "locally_observed":
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "known PR anchors require local validation",
            )
        if self.status != "unknown" and self.last_observation is None:
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "known PR anchors require exact observation provenance",
            )
        if self.last_observation is not None and type(self.last_observation) is not GitGraphObservation:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "PR observation is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "anchor_oid": self.anchor_oid,
            "current_oid": self.current_oid,
            "case_ids": list(self.case_ids),
            "fix_commit_oids": list(self.fix_commit_oids),
            "matched_endpoints": list(self.matched_endpoints),
            "status": self.status,
            "confidence": self.confidence,
            "last_observation": (
                None if self.last_observation is None else self.last_observation.to_dict()
            ),
        }


@dataclass(frozen=True)
class GitGraphProjection:
    repository: GitGraphRepository
    reducer_descriptor_sha256: str
    object_format: GitObjectFormat | None
    head_oid: str | None
    ref_name: str | None
    detached: bool | None
    dirty: bool | None
    shallow_state: GitShallowState
    commits: tuple[GitGraphCommitNode, ...]
    parent_relations: tuple[GitGraphParentRelation, ...]
    ancestry_relations: tuple[GitGraphAncestryRelation, ...]
    missing_objects: tuple[GitGraphMissingObject, ...]
    evidence_relations: tuple[GitGraphEvidenceRelation, ...]
    pr_anchors: tuple[GitGraphPRAnchor, ...]
    last_observation: GitGraphObservation
    last_validated_at: str | None
    last_event_sha256: str
    last_global_position: int
    projection_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.repository) is not GitGraphRepository:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "repository is invalid")
        _digest(self.reducer_descriptor_sha256, "reducer_descriptor_sha256")
        if self.object_format is not None and self.object_format not in _OID_PATTERNS:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "object format is invalid")
        if self.head_oid is not None:
            _projection_oid(self.head_oid, self.object_format, "head_oid")
        if self.ref_name is not None:
            _bounded_string(self.ref_name, "ref_name", max_chars=1_024)
        if self.detached is not None and type(self.detached) is not bool:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "detached is invalid")
        if self.dirty is not None and type(self.dirty) is not bool:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "dirty is invalid")
        if self.shallow_state not in {"full", "shallow", "unknown"}:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "shallow state is invalid")
        _canonical_projection_tuples(self)
        for node in self.commits:
            _projection_oid(node.commit_oid, self.object_format, "commit_oid")
            if node.tree_oid is not None:
                _projection_oid(node.tree_oid, self.object_format, "tree_oid")
            for parent_oid in node.parent_oids:
                _projection_oid(parent_oid, self.object_format, "parent_oid")
        for relation in self.parent_relations:
            _projection_oid(relation.parent_oid, self.object_format, "parent_oid")
            _projection_oid(relation.child_oid, self.object_format, "child_oid")
        for relation in self.ancestry_relations:
            _projection_oid(relation.anchor_oid, self.object_format, "anchor_oid")
            _projection_oid(relation.current_oid, self.object_format, "current_oid")
        for item in self.missing_objects:
            _projection_oid(item.object_oid, self.object_format, "missing object")
        for relation in self.evidence_relations:
            _projection_oid(
                relation.from_commit_oid,
                self.object_format,
                "evidence from commit",
            )
            _projection_oid(
                relation.to_commit_oid,
                self.object_format,
                "evidence to commit",
            )
        for anchor in self.pr_anchors:
            _projection_oid(anchor.anchor_oid, self.object_format, "PR anchor")
            _projection_oid(anchor.current_oid, self.object_format, "PR current commit")
            if self.head_oid is None or anchor.current_oid != self.head_oid:
                _fail(
                    "TBM_GIT_GRAPH_PROJECTION_INVALID",
                    "PR anchors must bind the projection current commit",
                )
            matching_relation = next(
                (
                    relation
                    for relation in self.ancestry_relations
                    if relation.current_oid == anchor.current_oid
                    and relation.anchor_oid == anchor.anchor_oid
                ),
                None,
            )
            if matching_relation is None:
                if (
                    anchor.status != "unknown"
                    or anchor.confidence != "indeterminate"
                    or anchor.last_observation is not None
                ):
                    _fail(
                        "TBM_GIT_GRAPH_PROJECTION_INVALID",
                        "PR anchor without ancestry must remain indeterminate",
                    )
            elif (
                anchor.status != matching_relation.status
                or anchor.confidence != matching_relation.confidence
                or anchor.last_observation != matching_relation.last_observation
            ):
                _fail(
                    "TBM_GIT_GRAPH_PROJECTION_INVALID",
                    "PR anchor does not match projection ancestry evidence",
                )
        if type(self.last_observation) is not GitGraphObservation:
            _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "last observation is invalid")
        if self.last_validated_at is not None:
            _timestamp(self.last_validated_at, "last_validated_at")
        _digest(self.last_event_sha256, "last_event_sha256")
        _positive_int(self.last_global_position, "last_global_position")
        unsigned = self.to_dict(include_digest=False)
        object.__setattr__(
            self,
            "projection_sha256",
            _domain_sha256(b"tbm.git-graph-projection.v1\x00", unsigned),
        )

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol_version": GIT_GRAPH_PROTOCOL_VERSION,
            "repository": self.repository.to_dict(),
            "reducer_descriptor_sha256": self.reducer_descriptor_sha256,
            "object_format": self.object_format,
            "head_oid": self.head_oid,
            "ref_name": self.ref_name,
            "detached": self.detached,
            "dirty": self.dirty,
            "shallow_state": self.shallow_state,
            "commits": [item.to_dict() for item in self.commits],
            "parent_relations": [item.to_dict() for item in self.parent_relations],
            "ancestry_relations": [item.to_dict() for item in self.ancestry_relations],
            "missing_objects": [item.to_dict() for item in self.missing_objects],
            "evidence_relations": [item.to_dict() for item in self.evidence_relations],
            "pr_anchors": [item.to_dict() for item in self.pr_anchors],
            "last_observation": self.last_observation.to_dict(),
            "last_validated_at": self.last_validated_at,
            "last_event_sha256": self.last_event_sha256,
            "last_global_position": self.last_global_position,
        }
        if include_digest:
            value["projection_sha256"] = self.projection_sha256
        return value


def build_git_graph_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=GIT_GRAPH_REDUCER_ID,
        reducer_version=1,
        input_event_types=GIT_OBSERVATION_TYPES,
        output_projection=GIT_GRAPH_PROJECTION,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "git-graph-current",
                "algorithm_version": 1,
                "input_event_types": list(GIT_OBSERVATION_TYPES),
                "confidence": sorted(_CONFIDENCE_VALUES),
                "unknown_is_not_false": True,
                "local_validation_requires": [
                    "full_repository",
                    "current_object_present",
                    "anchor_object_present",
                    "same_capture_request",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {
                "max_commits": GIT_GRAPH_MAX_COMMITS,
                "max_edges": GIT_GRAPH_MAX_EDGES,
                "version": 1,
            },
        ),
        target_event_versions={event_type: 1 for event_type in GIT_OBSERVATION_TYPES},
    )

    def initial() -> Mapping[str, object]:
        return {
            "checkout_id": None,
            "scope": None,
            "repository": None,
            "object_format": None,
            "head_oid": None,
            "ref": None,
            "dirty": None,
            "diff": None,
            "shallow": None,
            "commits": {},
            "availability": {},
            "ancestry": {},
            "active_capture": None,
            "capture_requests": [],
            "last_observation": None,
            "last_event_sha256": None,
            "last_global_position": 0,
            "last_stream_version": 0,
            "last_observed_at": None,
        }

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        typed = reducer_event.typed_event
        if typed is None:
            _fail(
                "TBM_GIT_GRAPH_TYPED_INPUT_REQUIRED",
                "Git graph reducer requires typed Git observation input",
            )
        event = reducer_event.source_event
        verify_git_observation_event(event)
        payload = _thaw_json(typed.payload)
        if type(payload) is not dict:
            _fail("TBM_GIT_GRAPH_EVENT_INVALID", "Git observation payload is invalid")
        next_state = _thaw_json(state)
        if type(next_state) is not dict:
            _fail("TBM_GIT_GRAPH_STATE_INVALID", "Git graph state is invalid")
        _apply_git_observation(next_state, event, payload)
        return next_state

    return FunctionalReducer(descriptor, initial, transition)


def reduce_git_graph_events(
    events: tuple[CanonicalEvent, ...],
    *,
    access: LedgerAccessContext,
    fix_evidence: tuple[FixEvidence, ...] = (),
    regression_evidence: tuple[StructuredRegressionEvidence, ...] = (),
    pr_case_provenance: tuple[PRCaseProvenance, ...] = (),
) -> GitGraphProjection | None:
    """Replay one access-bound Git observation stream into an immutable view."""

    if (
        type(events) is not tuple
        or len(events) > GIT_GRAPH_MAX_EVENTS
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _fail(
            "TBM_GIT_GRAPH_SEQUENCE_INVALID",
            "events must be a bounded tuple of CanonicalEvent values",
        )
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_GIT_GRAPH_ACCESS_INVALID",
            "access must be exactly LedgerAccessContext",
        )
    _validate_supplement_inputs(
        fix_evidence,
        regression_evidence,
        pr_case_provenance,
    )
    if not events:
        if fix_evidence or regression_evidence or pr_case_provenance:
            _fail(
                "TBM_GIT_GRAPH_SEQUENCE_INVALID",
                "supplemental relationships require a Git observation stream",
            )
        return None
    for event in events:
        _verify_replay_access(event, access)
        verify_git_observation_event(event)
    if events[0].stream_version != 1 or events[0].previous_stream_event_sha256 is not None:
        _fail(
            "TBM_GIT_GRAPH_SEQUENCE_INVALID",
            "Git observation stream must begin at version one",
        )
    _verify_capture_command_groups(events, access)
    registry = build_git_observation_registry()
    selected_reducer = build_git_graph_reducer()

    step = initial_reducer_state(selected_reducer)
    parent: CanonicalEvent | None = None
    previous_observed_at: datetime | None = None
    expected_stream_id: str | None = None
    for event in events:
        _verify_replay_access(event, access)
        verify_git_observation_event(event)
        if parent is None:
            if event.stream_version != 1 or event.previous_stream_event_sha256 is not None:
                _fail(
                    "TBM_GIT_GRAPH_SEQUENCE_INVALID",
                    "Git observation stream must begin at version one",
                )
            expected_stream_id = event.stream_id
        else:
            verify_event_parent(event, parent)
        if event.stream_type != GIT_OBSERVATION_STREAM_TYPE or event.stream_id != expected_stream_id:
            _fail(
                "TBM_GIT_GRAPH_SEQUENCE_INVALID",
                "events must belong to one Git observation stream",
            )
        if event.occurred_at is None:
            _fail(
                "TBM_GIT_GRAPH_SEQUENCE_INVALID",
                "Git observations require occurrence timestamps",
            )
        observed_at = parse_rfc3339(event.occurred_at)
        if previous_observed_at is not None and observed_at < previous_observed_at:
            _fail(
                "TBM_GIT_GRAPH_SEQUENCE_INVALID",
                "Git observation timestamps cannot move backwards",
            )
        typed = registry.consume(event, target_version=1)
        step = execute_reducer_step(
            selected_reducer,
            step.state,
            ReducerEvent(event, typed),
        )
        parent = event
        previous_observed_at = observed_at

    raw_state = _thaw_json(step.state)
    if type(raw_state) is not dict:
        _fail("TBM_GIT_GRAPH_STATE_INVALID", "Git graph state is invalid")
    return _projection_from_state(
        raw_state,
        descriptor=selected_reducer.descriptor,
        fix_evidence=fix_evidence,
        regression_evidence=regression_evidence,
        pr_case_provenance=pr_case_provenance,
    )


def pr_anchor_commit_ancestry_evidence(
    projection: GitGraphProjection,
) -> CommitAncestryEvidence:
    """Convert only complete, locally validated PR anchors to compatibility bools."""

    if type(projection) is not GitGraphProjection or projection.head_oid is None:
        _fail(
            "TBM_GIT_GRAPH_PR_ANCHOR_UNVERIFIED",
            "Git graph projection does not have a current commit",
        )
    relations: list[tuple[str, bool]] = []
    for anchor in projection.pr_anchors:
        if (
            anchor.current_oid != projection.head_oid
            or anchor.last_observation is None
            or anchor.status == "unknown"
            or anchor.confidence != "locally_observed"
        ):
            _fail(
                "TBM_GIT_GRAPH_PR_ANCHOR_UNVERIFIED",
                "PR anchors do not have complete locally validated ancestry",
            )
        relations.append((anchor.anchor_oid, anchor.status == "ancestor"))
    return CommitAncestryEvidence(
        current_commit_sha=projection.head_oid,
        commit_relations=tuple(relations),
    )


def _apply_git_observation(
    state: dict[str, object],
    event: CanonicalEvent,
    payload: dict[str, object],
) -> None:
    checkout_id = cast(str, payload["checkout_id"])
    if state["checkout_id"] is None:
        state["checkout_id"] = checkout_id
        state["scope"] = {
            "organization_id": event.organization_id,
            "tenant_id": event.tenant_id,
            "repository_id": event.repository_id,
            "environment_id": event.environment_id,
        }
        state["repository"] = {
            "root_sha256": None,
            "observed_repository_name": None,
        }
    elif state["checkout_id"] != checkout_id or event.stream_id != git_observation_stream_id(
        checkout_id
    ):
        _fail(
            "TBM_GIT_GRAPH_CHECKOUT_MISMATCH",
            "Git observation checkout identity changed during replay",
        )
    expected_scope = cast(dict[str, object], state["scope"])
    actual_scope = {
        "organization_id": event.organization_id,
        "tenant_id": event.tenant_id,
        "repository_id": event.repository_id,
        "environment_id": event.environment_id,
    }
    if expected_scope != actual_scope:
        _fail(
            "TBM_GIT_GRAPH_SCOPE_MISMATCH",
            "Git observation trusted scope changed during replay",
        )
    if event.stream_version != cast(int, state["last_stream_version"]) + 1:
        _fail(
            "TBM_GIT_GRAPH_SEQUENCE_INVALID",
            "Git graph reducer stream version is not contiguous",
        )
    previous_observed_at = cast(str | None, state["last_observed_at"])
    observed_at = cast(str, payload["observed_at"])
    if previous_observed_at is not None and parse_rfc3339(observed_at) < parse_rfc3339(
        previous_observed_at
    ):
        _fail(
            "TBM_GIT_GRAPH_SEQUENCE_INVALID",
            "Git graph reducer observation time moved backwards",
        )

    capture = _active_capture(state, event.request_sha256)
    observation = cast(dict[str, object], payload["observation"])
    object_format = cast(GitObjectFormat | None, observation.get("object_format"))
    if object_format is not None:
        _set_object_format(state, object_format)
    cursor = _cursor(event, payload)
    event_type = event.event_type
    if event_type == GIT_CHECKOUT_OBSERVED:
        _mark_capture_point(capture, "checkout")
        head_oid = cast(str, observation["head_oid"])
        _capture_head(capture, head_oid)
        repository = cast(dict[str, object], state["repository"])
        root_sha256 = cast(str, observation["root_sha256"])
        repository_name = cast(str | None, observation["repository_name"])
        if repository["root_sha256"] not in {None, root_sha256}:
            _fail(
                "TBM_GIT_GRAPH_REPOSITORY_MISMATCH",
                "checkout root changed for one Git observation stream",
            )
        if repository["observed_repository_name"] not in {None, repository_name}:
            _fail(
                "TBM_GIT_GRAPH_REPOSITORY_MISMATCH",
                "observed repository name changed for one checkout",
            )
        repository["root_sha256"] = root_sha256
        repository["observed_repository_name"] = repository_name
        state["head_oid"] = head_oid
        state["dirty"] = cast(bool, observation["dirty"])
    elif event_type == GIT_REF_OBSERVED:
        _mark_capture_point(capture, "ref")
        target_oid = cast(str, observation["target_oid"])
        _capture_head(capture, target_oid)
        state["head_oid"] = target_oid
        state["ref"] = {
            "target_oid": target_oid,
            "ref_name": observation["ref_name"],
            "detached": observation["detached"],
            "observation": cursor,
        }
    elif event_type == GIT_COMMIT_OBSERVED:
        commit_oid = cast(str, observation["commit_oid"])
        _mark_capture_point(capture, "commit:" + commit_oid)
        _capture_head(capture, commit_oid)
        state["head_oid"] = commit_oid
        commits = cast(dict[str, object], state["commits"])
        next_commit = {
            "tree_oid": observation["tree_oid"],
            "parent_oids": list(cast(list[str], observation["parent_oids"])),
            "observation": cursor,
        }
        current_commit = commits.get(commit_oid)
        if current_commit is not None:
            current_value = cast(dict[str, object], current_commit)
            if (
                current_value["tree_oid"] != next_commit["tree_oid"]
                or current_value["parent_oids"] != next_commit["parent_oids"]
            ):
                _fail(
                    "TBM_GIT_GRAPH_COMMIT_CONFLICT",
                    "one commit object was observed with conflicting contents",
                )
        commits[commit_oid] = next_commit
        _validate_graph_bounds_and_cycles(commits)
    elif event_type == GIT_DIFF_OBSERVED:
        _mark_capture_point(capture, "diff")
        base_oid = cast(str, observation["base_oid"])
        _capture_head(capture, base_oid)
        state["head_oid"] = base_oid
        state["diff"] = {
            "base_oid": base_oid,
            "artifact_id": observation["artifact_id"],
            "content_sha256": observation["content_sha256"],
            "observation": cursor,
        }
    elif event_type == GIT_OBJECT_AVAILABILITY_OBSERVED:
        _mark_capture_point(capture, "object_availability")
        active_availability = cast(dict[str, object], capture["availability"])
        availability = cast(dict[str, object], state["availability"])
        for raw in cast(list[dict[str, object]], observation["objects"]):
            object_oid = cast(str, raw["object_oid"])
            status = cast(GitObjectAvailabilityStatus, raw["status"])
            active_availability[object_oid] = status
            availability[object_oid] = {
                "status": status,
                "request_sha256": event.request_sha256,
                "observation": cursor,
            }
    elif event_type == GIT_ANCESTRY_OBSERVED:
        current_oid = cast(str, observation["current_oid"])
        _mark_capture_point(capture, "ancestry:" + current_oid)
        _capture_head(capture, current_oid)
        state["head_oid"] = current_oid
        ancestry = cast(dict[str, object], state["ancestry"])
        for raw in cast(list[dict[str, object]], observation["relations"]):
            anchor_oid = cast(str, raw["anchor_oid"])
            status = cast(GitAncestryStatus, raw["status"])
            key = current_oid + ":" + anchor_oid
            previous = ancestry.get(key)
            known_status: GitAncestryStatus | None = None
            if previous is not None:
                previous_record = cast(dict[str, object], previous)
                known_status = cast(
                    GitAncestryStatus | None, previous_record.get("known_status")
                )
                if (
                    known_status in {"ancestor", "not_ancestor"}
                    and status in {"ancestor", "not_ancestor"}
                    and known_status != status
                ):
                    _fail(
                        "TBM_GIT_GRAPH_ANCESTRY_CONFLICT",
                        "immutable commit ancestry was observed with conflicting results",
                    )
            if status in {"ancestor", "not_ancestor"}:
                known_status = status
            ancestry[key] = {
                "anchor_oid": anchor_oid,
                "current_oid": current_oid,
                "reported_status": status,
                "known_status": known_status,
                "status": "unknown",
                "confidence": "indeterminate",
                "last_validated_at": None,
                "request_sha256": event.request_sha256,
                "observation": cursor,
            }
    elif event_type == GIT_SHALLOW_STATE_OBSERVED:
        _mark_capture_point(capture, "shallow")
        shallow_state = cast(GitShallowState, observation["state"])
        capture["shallow_state"] = shallow_state
        state["shallow"] = {
            "state": shallow_state,
            "request_sha256": event.request_sha256,
            "observation": cursor,
        }
    else:
        _fail("TBM_GIT_GRAPH_EVENT_INVALID", "unsupported Git observation event")

    _refresh_capture_ancestry(state, event.request_sha256)
    state["last_observation"] = cursor
    state["last_event_sha256"] = event.event_sha256
    state["last_global_position"] = event.global_position
    state["last_stream_version"] = event.stream_version
    state["last_observed_at"] = observed_at


def _projection_from_state(
    state: dict[str, object],
    *,
    descriptor: ReducerDescriptor,
    fix_evidence: tuple[FixEvidence, ...],
    regression_evidence: tuple[StructuredRegressionEvidence, ...],
    pr_case_provenance: tuple[PRCaseProvenance, ...],
) -> GitGraphProjection:
    checkout_id = cast(str | None, state["checkout_id"])
    scope = cast(dict[str, object] | None, state["scope"])
    repository_state = cast(dict[str, object] | None, state["repository"])
    last_observation_raw = cast(dict[str, object] | None, state["last_observation"])
    if (
        checkout_id is None
        or scope is None
        or repository_state is None
        or last_observation_raw is None
    ):
        _fail(
            "TBM_GIT_GRAPH_STATE_INVALID",
            "Git graph reducer did not produce stream identity",
        )
    object_format = cast(GitObjectFormat | None, state["object_format"])
    head_oid = cast(str | None, state["head_oid"])
    commits_state = cast(dict[str, object], state["commits"])
    availability_state = cast(dict[str, object], state["availability"])
    ancestry_state = cast(dict[str, object], state["ancestry"])
    shallow_record = cast(dict[str, object] | None, state["shallow"])
    shallow_state = cast(
        GitShallowState,
        "unknown" if shallow_record is None else shallow_record["state"],
    )

    evidence_relations = _build_evidence_relations(
        object_format,
        shallow_state,
        availability_state,
        fix_evidence,
        regression_evidence,
    )
    ancestry_relations = _build_ancestry_relations(
        shallow_state,
        availability_state,
        ancestry_state,
    )
    ancestry_by_anchor = {
        (item.current_oid, item.anchor_oid): item for item in ancestry_relations
    }
    pr_anchors = _build_pr_anchors(
        object_format,
        head_oid,
        pr_case_provenance,
        ancestry_by_anchor,
    )

    referenced_oids: set[str] = set(commits_state)
    for raw in commits_state.values():
        referenced_oids.update(cast(list[str], cast(dict[str, object], raw)["parent_oids"]))
    referenced_oids.update(availability_state)
    for relation in ancestry_relations:
        referenced_oids.add(relation.current_oid)
        referenced_oids.add(relation.anchor_oid)
    for relation in evidence_relations:
        referenced_oids.add(relation.from_commit_oid)
        referenced_oids.add(relation.to_commit_oid)
    for anchor in pr_anchors:
        referenced_oids.add(anchor.anchor_oid)
        referenced_oids.add(anchor.current_oid)
        referenced_oids.update(anchor.fix_commit_oids)
    if len(referenced_oids) > GIT_GRAPH_MAX_COMMITS:
        _fail(
            "TBM_GIT_GRAPH_LIMIT_EXCEEDED",
            "Git graph projection contains too many commit nodes",
        )

    commit_nodes: list[GitGraphCommitNode] = []
    for commit_oid in sorted(referenced_oids):
        _projection_oid(commit_oid, object_format, "commit_oid")
        commit_record = cast(dict[str, object] | None, commits_state.get(commit_oid))
        availability_record = cast(
            dict[str, object] | None, availability_state.get(commit_oid)
        )
        commit_nodes.append(
            GitGraphCommitNode(
                commit_oid=commit_oid,
                tree_oid=(
                    None if commit_record is None else cast(str, commit_record["tree_oid"])
                ),
                parent_oids=(
                    ()
                    if commit_record is None
                    else tuple(cast(list[str], commit_record["parent_oids"]))
                ),
                availability=(
                    None
                    if availability_record is None
                    else cast(GitObjectAvailabilityStatus, availability_record["status"])
                ),
                observed=commit_record is not None,
                last_observation=(
                    None
                    if commit_record is None
                    else _observation_from_state(
                        cast(dict[str, object], commit_record["observation"])
                    )
                ),
            )
        )

    parent_relations = _build_parent_relations(
        shallow_state,
        commits_state,
        availability_state,
    )
    missing_objects = _build_missing_objects(availability_state)
    validated_times: list[str] = []
    validated_times.extend(
        item.last_validated_at
        for item in parent_relations
        if item.last_validated_at is not None
    )
    validated_times.extend(
        item.last_validated_at
        for item in ancestry_relations
        if item.last_validated_at is not None
    )
    validated_times.extend(item.verified_at for item in evidence_relations)
    ref_state = cast(dict[str, object] | None, state["ref"])
    repository = GitGraphRepository(
        organization_id=cast(str, scope["organization_id"]),
        tenant_id=cast(str, scope["tenant_id"]),
        repository_id=cast(str, scope["repository_id"]),
        environment_id=cast(str, scope["environment_id"]),
        checkout_id=checkout_id,
        root_sha256=cast(str | None, repository_state["root_sha256"]),
        observed_repository_name=cast(
            str | None, repository_state["observed_repository_name"]
        ),
    )
    last_observation = _observation_from_state(last_observation_raw)
    last_event_sha256 = cast(str | None, state["last_event_sha256"])
    if last_event_sha256 is None:
        _fail("TBM_GIT_GRAPH_STATE_INVALID", "last event digest is missing")
    return GitGraphProjection(
        repository=repository,
        reducer_descriptor_sha256=descriptor.descriptor_sha256,
        object_format=object_format,
        head_oid=head_oid,
        ref_name=(None if ref_state is None else cast(str | None, ref_state["ref_name"])),
        detached=(None if ref_state is None else cast(bool, ref_state["detached"])),
        dirty=cast(bool | None, state["dirty"]),
        shallow_state=shallow_state,
        commits=tuple(commit_nodes),
        parent_relations=parent_relations,
        ancestry_relations=ancestry_relations,
        missing_objects=missing_objects,
        evidence_relations=evidence_relations,
        pr_anchors=pr_anchors,
        last_observation=last_observation,
        last_validated_at=_latest_timestamp(validated_times),
        last_event_sha256=last_event_sha256,
        last_global_position=cast(int, state["last_global_position"]),
    )


def _build_parent_relations(
    shallow_state: GitShallowState,
    commits: dict[str, object],
    availability: dict[str, object],
) -> tuple[GitGraphParentRelation, ...]:
    result: list[GitGraphParentRelation] = []
    for child_oid, raw in commits.items():
        record = cast(dict[str, object], raw)
        observation = _observation_from_state(
            cast(dict[str, object], record["observation"])
        )
        for parent_oid in cast(list[str], record["parent_oids"]):
            degraded = shallow_state != "full" or not _endpoints_explicitly_present(
                availability, child_oid, parent_oid
            )
            confidence: GitRelationConfidence = (
                "degraded" if degraded else "locally_observed"
            )
            result.append(
                GitGraphParentRelation(
                    parent_oid=parent_oid,
                    child_oid=child_oid,
                    confidence=confidence,
                    last_observation=observation,
                    last_validated_at=(
                        None if degraded else observation.observed_at
                    ),
                )
            )
    return tuple(sorted(result, key=lambda item: (item.parent_oid, item.child_oid)))


def _build_ancestry_relations(
    shallow_state: GitShallowState,
    availability: dict[str, object],
    ancestry: dict[str, object],
) -> tuple[GitGraphAncestryRelation, ...]:
    result: list[GitGraphAncestryRelation] = []
    for raw in ancestry.values():
        record = cast(dict[str, object], raw)
        anchor_oid = cast(str, record["anchor_oid"])
        current_oid = cast(str, record["current_oid"])
        status = cast(GitAncestryStatus, record["status"])
        confidence = cast(GitRelationConfidence, record["confidence"])
        last_validated_at = cast(str | None, record["last_validated_at"])
        if shallow_state != "full" or _endpoint_unavailable(
            availability, current_oid, anchor_oid
        ):
            status = "unknown"
            confidence = "indeterminate"
            last_validated_at = None
        result.append(
            GitGraphAncestryRelation(
                anchor_oid=anchor_oid,
                current_oid=current_oid,
                reported_status=cast(GitAncestryStatus, record["reported_status"]),
                status=status,
                confidence=confidence,
                last_observation=_observation_from_state(
                    cast(dict[str, object], record["observation"])
                ),
                last_validated_at=last_validated_at,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.current_oid, item.anchor_oid)))


def _build_missing_objects(
    availability: dict[str, object],
) -> tuple[GitGraphMissingObject, ...]:
    result: list[GitGraphMissingObject] = []
    for object_oid, raw in availability.items():
        record = cast(dict[str, object], raw)
        status = cast(GitObjectAvailabilityStatus, record["status"])
        if status == "present":
            continue
        result.append(
            GitGraphMissingObject(
                object_oid=object_oid,
                status=cast(Literal["missing", "unknown"], status),
                reason=(
                    "object_missing" if status == "missing" else "availability_unknown"
                ),
                last_observation=_observation_from_state(
                    cast(dict[str, object], record["observation"])
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.object_oid))


def _build_evidence_relations(
    object_format: GitObjectFormat | None,
    shallow_state: GitShallowState,
    availability: dict[str, object],
    fix_evidence: tuple[FixEvidence, ...],
    regression_evidence: tuple[StructuredRegressionEvidence, ...],
) -> tuple[GitGraphEvidenceRelation, ...]:
    fix_keys = {
        (
            item.case_id,
            item.source_trace_id,
            item.source_commit_sha,
            item.fix_commit_sha,
        )
        for item in fix_evidence
    }
    result: list[GitGraphEvidenceRelation] = []
    for item in sorted(fix_evidence, key=lambda evidence: evidence.evidence_id):
        _projection_oid(item.source_commit_sha, object_format, "source_commit_sha")
        _projection_oid(item.fix_commit_sha, object_format, "fix_commit_sha")
        result.append(
            _evidence_relation(
                object_format=object_format,
                shallow_state=shallow_state,
                availability=availability,
                relation_kind="source_to_fix",
                from_commit_oid=item.source_commit_sha,
                to_commit_oid=item.fix_commit_sha,
                evidence_id=item.evidence_id,
                case_id=item.case_id,
                source_trace_id=item.source_trace_id,
                verification_trace_id=None,
                result=None,
                verified_by=item.source_to_fix.verified_by,
                verified_at=item.source_to_fix.verified_at,
            )
        )
    for item in sorted(
        regression_evidence, key=lambda evidence: evidence.evidence_id
    ):
        key = (
            item.case_id,
            item.source_trace_id,
            item.source_commit_sha,
            item.fix_commit_sha,
        )
        if key not in fix_keys:
            _fail(
                "TBM_GIT_GRAPH_EVIDENCE_MISMATCH",
                "regression evidence is not linked to exact fix evidence",
            )
        _projection_oid(item.source_commit_sha, object_format, "source_commit_sha")
        _projection_oid(item.fix_commit_sha, object_format, "fix_commit_sha")
        _projection_oid(
            item.verification_commit_sha,
            object_format,
            "verification_commit_sha",
        )
        result.append(
            _evidence_relation(
                object_format=object_format,
                shallow_state=shallow_state,
                availability=availability,
                relation_kind="source_to_fix",
                from_commit_oid=item.source_commit_sha,
                to_commit_oid=item.fix_commit_sha,
                evidence_id=item.evidence_id,
                case_id=item.case_id,
                source_trace_id=item.source_trace_id,
                verification_trace_id=item.verification_trace_id,
                result=item.result,
                verified_by=item.source_to_fix.verified_by,
                verified_at=item.source_to_fix.verified_at,
            )
        )
        result.append(
            _evidence_relation(
                object_format=object_format,
                shallow_state=shallow_state,
                availability=availability,
                relation_kind="fix_to_verification",
                from_commit_oid=item.fix_commit_sha,
                to_commit_oid=item.verification_commit_sha,
                evidence_id=item.evidence_id,
                case_id=item.case_id,
                source_trace_id=item.source_trace_id,
                verification_trace_id=item.verification_trace_id,
                result=item.result,
                verified_by=item.fix_to_verification.verified_by,
                verified_at=item.fix_to_verification.verified_at,
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.relation_kind,
                item.from_commit_oid,
                item.to_commit_oid,
                item.evidence_id,
            ),
        )
    )


def _evidence_relation(
    *,
    object_format: GitObjectFormat | None,
    shallow_state: GitShallowState,
    availability: dict[str, object],
    relation_kind: GitEvidenceRelationKind,
    from_commit_oid: str,
    to_commit_oid: str,
    evidence_id: str,
    case_id: str,
    source_trace_id: str,
    verification_trace_id: str | None,
    result: Literal["pass", "fail", "error"] | None,
    verified_by: str,
    verified_at: str,
) -> GitGraphEvidenceRelation:
    _projection_oid(from_commit_oid, object_format, "from_commit_oid")
    _projection_oid(to_commit_oid, object_format, "to_commit_oid")
    confidence: GitRelationConfidence = "independently_verified"
    if shallow_state != "full" or not _endpoints_explicitly_present(
        availability, from_commit_oid, to_commit_oid
    ):
        confidence = "degraded"
    return GitGraphEvidenceRelation(
        relation_kind=relation_kind,
        from_commit_oid=from_commit_oid,
        to_commit_oid=to_commit_oid,
        evidence_id=evidence_id,
        case_id=case_id,
        source_trace_id=source_trace_id,
        verification_trace_id=verification_trace_id,
        result=result,
        verified_by=verified_by,
        verified_at=verified_at,
        confidence=confidence,
    )


def _build_pr_anchors(
    object_format: GitObjectFormat | None,
    head_oid: str | None,
    provenance: tuple[PRCaseProvenance, ...],
    ancestry: Mapping[tuple[str, str], GitGraphAncestryRelation],
) -> tuple[GitGraphPRAnchor, ...]:
    if not provenance:
        return ()
    if head_oid is None:
        _fail(
            "TBM_GIT_GRAPH_PR_ANCHOR_INVALID",
            "PR anchors require a current Git commit",
        )
    grouped: dict[str, list[PRCaseProvenance]] = {}
    for item in provenance:
        _projection_oid(item.commit_sha, object_format, "PR source commit")
        if item.fix_commit_sha is not None:
            _projection_oid(item.fix_commit_sha, object_format, "PR fix commit")
        grouped.setdefault(item.commit_sha, []).append(item)
    result: list[GitGraphPRAnchor] = []
    for anchor_oid in sorted(grouped):
        relation = ancestry.get((head_oid, anchor_oid))
        items = grouped[anchor_oid]
        endpoint_values: set[GitPRAnchorEndpoint] = set()
        for item in items:
            endpoint_values.add(
                "legacy"
                if item.matched_change_endpoint is None
                else cast(GitPRAnchorEndpoint, item.matched_change_endpoint)
            )
        result.append(
            GitGraphPRAnchor(
                anchor_oid=anchor_oid,
                current_oid=head_oid,
                case_ids=tuple(sorted(item.case_id for item in items)),
                fix_commit_oids=tuple(
                    sorted(
                        {
                            item.fix_commit_sha
                            for item in items
                            if item.fix_commit_sha is not None
                        }
                    )
                ),
                matched_endpoints=tuple(sorted(endpoint_values)),
                status="unknown" if relation is None else relation.status,
                confidence=(
                    "indeterminate" if relation is None else relation.confidence
                ),
                last_observation=(
                    None if relation is None else relation.last_observation
                ),
            )
        )
    return tuple(result)


def _validate_supplement_inputs(
    fix_evidence: object,
    regression_evidence: object,
    pr_case_provenance: object,
) -> None:
    if (
        type(fix_evidence) is not tuple
        or len(fix_evidence) > GIT_GRAPH_MAX_EVIDENCE
        or any(type(item) is not FixEvidence for item in fix_evidence)
    ):
        _fail(
            "TBM_GIT_GRAPH_EVIDENCE_INVALID",
            "fix_evidence must be a bounded tuple of FixEvidence",
        )
    if (
        type(regression_evidence) is not tuple
        or len(regression_evidence) > GIT_GRAPH_MAX_EVIDENCE
        or any(type(item) is not StructuredRegressionEvidence for item in regression_evidence)
    ):
        _fail(
            "TBM_GIT_GRAPH_EVIDENCE_INVALID",
            "regression_evidence must be a bounded tuple of StructuredRegressionEvidence",
        )
    if (
        type(pr_case_provenance) is not tuple
        or len(pr_case_provenance) > GIT_GRAPH_MAX_PR_CASES
        or any(type(item) is not PRCaseProvenance for item in pr_case_provenance)
    ):
        _fail(
            "TBM_GIT_GRAPH_PR_ANCHOR_INVALID",
            "pr_case_provenance must be a bounded tuple of PRCaseProvenance",
        )
    fix_ids = [cast(FixEvidence, item).evidence_id for item in cast(tuple[object, ...], fix_evidence)]
    regression_ids = [
        cast(StructuredRegressionEvidence, item).evidence_id
        for item in cast(tuple[object, ...], regression_evidence)
    ]
    case_ids = [
        cast(PRCaseProvenance, item).case_id
        for item in cast(tuple[object, ...], pr_case_provenance)
    ]
    if len(fix_ids) != len(set(fix_ids)) or len(regression_ids) != len(
        set(regression_ids)
    ):
        _fail(
            "TBM_GIT_GRAPH_EVIDENCE_INVALID",
            "supplemental evidence IDs must be unique",
        )
    if len(case_ids) != len(set(case_ids)):
        _fail(
            "TBM_GIT_GRAPH_PR_ANCHOR_INVALID",
            "PR case provenance case IDs must be unique",
        )


def _verify_capture_command_groups(
    events: tuple[CanonicalEvent, ...], access: LedgerAccessContext
) -> None:
    start = 0
    while start < len(events):
        request_sha256 = events[start].request_sha256
        end = start + 1
        while end < len(events) and events[end].request_sha256 == request_sha256:
            end += 1
        group = events[start:end]
        first = group[0]
        command_value = {
            "protocol_version": GIT_OBSERVATION_PROTOCOL_VERSION,
            "partition_sha256": access.partition.partition_sha256,
            "stream_id": first.stream_id,
            "expected_stream_version": first.stream_version - 1,
            "next_global_position": first.global_position,
            "recorded_at": first.recorded_at,
            "drafts": [
                {
                    "event_type": event.event_type,
                    "classification": event.classification,
                    "retention_policy_id": event.retention_policy_id,
                    "artifact_refs": [
                        reference.to_dict() for reference in event.artifact_refs
                    ],
                    "payload": _thaw_json(event.payload),
                }
                for event in group
            ],
        }
        expected_command = _domain_sha256(
            b"tbm.git-observation-command.v1\x00",
            command_value,
        )
        expected_idempotency = _domain_sha256(
            b"tbm.git-observation-idempotency.v1\x00",
            command_value,
        )
        for offset, event in enumerate(group):
            event_digest = hashlib.sha256(
                (expected_command + f"\x00{offset}").encode("utf-8")
            ).hexdigest()
            source = event.source
            if (
                event.request_sha256 != expected_command
                or event.idempotency_key_sha256 != expected_idempotency
                or event.recorded_at != first.recorded_at
                or event.stream_version != first.stream_version + offset
                or event.global_position != first.global_position + offset
                or event.event_id != "evt_git_" + event_digest
                or event.request_id != "request_git_" + event_digest[:32]
                or source is None
                or source.source_record_id
                != "git_observation_" + event_digest[:48]
            ):
                _fail(
                    "TBM_GIT_GRAPH_CAPTURE_COMMAND_INVALID",
                    "Git observation capture command binding is invalid",
                )
        start = end


def _verify_replay_access(event: CanonicalEvent, access: LedgerAccessContext) -> None:
    partition = access.partition
    if (
        event.organization_id != partition.organization_id
        or event.tenant_id != partition.tenant_id
        or event.repository_id != partition.repository_id
        or event.environment_id != partition.environment_id
    ):
        _fail(
            "TBM_GIT_GRAPH_SCOPE_MISMATCH",
            "Git observation is outside the trusted replay partition",
        )
    if not access.classification_filter.allows(event.classification):
        _fail(
            "TBM_GIT_GRAPH_CLASSIFICATION_DENIED",
            "Git observation classification is not visible to replay access",
        )


def _active_capture(
    state: dict[str, object], request_sha256: str
) -> dict[str, object]:
    active = cast(dict[str, object] | None, state["active_capture"])
    if active is None or active["request_sha256"] != request_sha256:
        capture_requests = cast(list[str], state["capture_requests"])
        if request_sha256 in capture_requests:
            _fail(
                "TBM_GIT_GRAPH_CAPTURE_CONFLICT",
                "one capture request must form one contiguous event segment",
            )
        capture_requests.append(request_sha256)
        active = {
            "request_sha256": request_sha256,
            "head_oid": None,
            "seen": [],
            "availability": {},
            "shallow_state": None,
        }
        state["active_capture"] = active
    return active


def _mark_capture_point(capture: dict[str, object], point_key: str) -> None:
    seen = cast(list[str], capture["seen"])
    if point_key in seen:
        _fail(
            "TBM_GIT_GRAPH_CAPTURE_CONFLICT",
            "one capture request contains a duplicate Git observation point",
        )
    seen.append(point_key)
    seen.sort()


def _capture_head(capture: dict[str, object], head_oid: str) -> None:
    current = cast(str | None, capture["head_oid"])
    if current is not None and current != head_oid:
        _fail(
            "TBM_GIT_GRAPH_HEAD_CONFLICT",
            "one capture request observed conflicting current commits",
        )
    capture["head_oid"] = head_oid


def _set_object_format(state: dict[str, object], object_format: GitObjectFormat) -> None:
    current = cast(GitObjectFormat | None, state["object_format"])
    if current is not None and current != object_format:
        _fail(
            "TBM_GIT_GRAPH_OBJECT_FORMAT_CONFLICT",
            "Git object format changed during one checkout stream",
        )
    state["object_format"] = object_format


def _refresh_capture_ancestry(
    state: dict[str, object], request_sha256: str
) -> None:
    active = cast(dict[str, object] | None, state["active_capture"])
    if active is None or active["request_sha256"] != request_sha256:
        return
    shallow_state = active["shallow_state"]
    availability = cast(dict[str, object], active["availability"])
    ancestry = cast(dict[str, object], state["ancestry"])
    for raw in ancestry.values():
        record = cast(dict[str, object], raw)
        if record["request_sha256"] != request_sha256:
            continue
        reported = cast(GitAncestryStatus, record["reported_status"])
        current_oid = cast(str, record["current_oid"])
        anchor_oid = cast(str, record["anchor_oid"])
        complete = (
            shallow_state == "full"
            and availability.get(current_oid) == "present"
            and availability.get(anchor_oid) == "present"
        )
        observation = cast(dict[str, object], record["observation"])
        if reported != "unknown" and complete:
            record["status"] = reported
            record["confidence"] = "locally_observed"
            record["last_validated_at"] = observation["observed_at"]
        else:
            record["status"] = "unknown"
            record["confidence"] = "indeterminate"
            record["last_validated_at"] = None


def _validate_graph_bounds_and_cycles(commits: dict[str, object]) -> None:
    if len(commits) > GIT_GRAPH_MAX_COMMITS:
        _fail(
            "TBM_GIT_GRAPH_LIMIT_EXCEEDED",
            "Git graph contains too many observed commits",
        )
    edge_count = sum(
        len(cast(list[str], cast(dict[str, object], value)["parent_oids"]))
        for value in commits.values()
    )
    if edge_count > GIT_GRAPH_MAX_EDGES:
        _fail(
            "TBM_GIT_GRAPH_LIMIT_EXCEEDED",
            "Git graph contains too many parent edges",
        )
    parents_by_child = {
        child: tuple(cast(list[str], cast(dict[str, object], raw)["parent_oids"]))
        for child, raw in commits.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(commit_oid: str) -> None:
        if commit_oid in visiting:
            _fail(
                "TBM_GIT_GRAPH_CYCLE",
                "observed Git commit graph must be acyclic",
            )
        if commit_oid in visited:
            return
        visiting.add(commit_oid)
        for parent_oid in parents_by_child.get(commit_oid, ()):
            if parent_oid in parents_by_child:
                visit(parent_oid)
        visiting.remove(commit_oid)
        visited.add(commit_oid)

    for commit_oid in sorted(parents_by_child):
        visit(commit_oid)


def _cursor(event: CanonicalEvent, payload: dict[str, object]) -> dict[str, object]:
    source = event.source
    if source is None:
        _fail("TBM_GIT_GRAPH_EVENT_INVALID", "Git observation source is missing")
    provenance = cast(dict[str, object], payload["provenance"])
    return {
        "event_sha256": event.event_sha256,
        "stream_version": event.stream_version,
        "global_position": event.global_position,
        "observed_at": payload["observed_at"],
        "source_system": source.source_system,
        "source_record_id": source.source_record_id,
        "runner_id": provenance["runner_id"],
        "runner_version": provenance["runner_version"],
        "algorithm_id": provenance["algorithm_id"],
        "algorithm_version": provenance["algorithm_version"],
        "git_version": provenance["git_version"],
    }


def _observation_from_state(value: dict[str, object]) -> GitGraphObservation:
    return GitGraphObservation(
        event_sha256=cast(str, value["event_sha256"]),
        stream_version=cast(int, value["stream_version"]),
        global_position=cast(int, value["global_position"]),
        observed_at=cast(str, value["observed_at"]),
        source_system=cast(str, value["source_system"]),
        source_record_id=cast(str, value["source_record_id"]),
        runner_id=cast(str, value["runner_id"]),
        runner_version=cast(str, value["runner_version"]),
        algorithm_id=cast(str, value["algorithm_id"]),
        algorithm_version=cast(str, value["algorithm_version"]),
        git_version=cast(str, value["git_version"]),
    )


def _endpoint_unavailable(
    availability: dict[str, object], *object_oids: str
) -> bool:
    for object_oid in object_oids:
        raw = cast(dict[str, object] | None, availability.get(object_oid))
        if raw is not None and raw["status"] != "present":
            return True
    return False


def _endpoints_explicitly_present(
    availability: dict[str, object], *object_oids: str
) -> bool:
    return all(
        cast(dict[str, object] | None, availability.get(object_oid)) is not None
        and cast(dict[str, object], availability[object_oid])["status"] == "present"
        for object_oid in object_oids
    )


def _canonical_projection_tuples(projection: GitGraphProjection) -> None:
    checks: tuple[tuple[object, type[object], tuple[str, ...]], ...] = (
        (
            projection.commits,
            GitGraphCommitNode,
            tuple(item.commit_oid for item in projection.commits),
        ),
        (
            projection.parent_relations,
            GitGraphParentRelation,
            tuple(
                item.parent_oid + "\x00" + item.child_oid
                for item in projection.parent_relations
            ),
        ),
        (
            projection.ancestry_relations,
            GitGraphAncestryRelation,
            tuple(
                item.current_oid + "\x00" + item.anchor_oid
                for item in projection.ancestry_relations
            ),
        ),
        (
            projection.missing_objects,
            GitGraphMissingObject,
            tuple(item.object_oid for item in projection.missing_objects),
        ),
        (
            projection.evidence_relations,
            GitGraphEvidenceRelation,
            tuple(
                "\x00".join(
                    (
                        item.relation_kind,
                        item.from_commit_oid,
                        item.to_commit_oid,
                        item.evidence_id,
                    )
                )
                for item in projection.evidence_relations
            ),
        ),
        (
            projection.pr_anchors,
            GitGraphPRAnchor,
            tuple(item.anchor_oid for item in projection.pr_anchors),
        ),
    )
    for values, expected_type, keys in checks:
        if (
            type(values) is not tuple
            or any(type(item) is not expected_type for item in cast(tuple[object, ...], values))
            or keys != tuple(sorted(set(keys)))
        ):
            _fail(
                "TBM_GIT_GRAPH_PROJECTION_INVALID",
                "projection collections must be typed, sorted, and unique",
            )


def _latest_timestamp(values: list[str]) -> str | None:
    if not values:
        return None
    return max(values, key=parse_rfc3339)


def _projection_oid(
    value: object, object_format: GitObjectFormat | None, name: str
) -> None:
    if object_format is None:
        _fail(
            "TBM_GIT_GRAPH_OBJECT_FORMAT_REQUIRED",
            f"{name} requires an observed Git object format",
        )
    if type(value) is not str or _OID_PATTERNS[object_format].fullmatch(value) is None:
        _fail(
            "TBM_GIT_GRAPH_OID_INVALID",
            f"{name} does not match the observed Git object format",
        )


def _generic_oid(value: object, name: str) -> None:
    if type(value) is not str or not any(
        pattern.fullmatch(value) is not None for pattern in _OID_PATTERNS.values()
    ):
        _fail(
            "TBM_GIT_GRAPH_PROJECTION_INVALID",
            f"{name} must be a complete Git object ID",
        )


def _matching_oid(value: object, reference: str, name: str) -> None:
    _generic_oid(value, name)
    if len(cast(str, value)) != len(reference):
        _fail(
            "TBM_GIT_GRAPH_PROJECTION_INVALID",
            f"{name} does not match the relation object format",
        )


def _confidence(value: object) -> None:
    if value not in _CONFIDENCE_VALUES:
        _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", "confidence is invalid")


def _positive_int(value: object, name: str) -> None:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", f"{name} is invalid")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", f"{name} is invalid")


def _timestamp(value: object, name: str) -> None:
    if type(value) is not str:
        _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", f"{name} is invalid")
    try:
        parse_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise GitGraphV1Error(
            "TBM_GIT_GRAPH_PROJECTION_INVALID",
            f"{name} is invalid",
        ) from error


def _bounded_string(value: object, name: str, *, max_chars: int = 512) -> None:
    if type(value) is not str or not 1 <= len(value) <= max_chars:
        _fail("TBM_GIT_GRAPH_PROJECTION_INVALID", f"{name} is invalid")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if type(value) in {tuple, list}:
        return [_thaw_json(item) for item in cast(tuple[object, ...] | list[object], value)]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise GitGraphV1Error(
            "TBM_GIT_GRAPH_PROJECTION_INVALID",
            "Git graph value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _fail(code: str, message: str) -> NoReturn:
    raise GitGraphV1Error(code, message)


__all__ = [
    "GIT_GRAPH_MAX_COMMITS",
    "GIT_GRAPH_MAX_EDGES",
    "GIT_GRAPH_MAX_EVENTS",
    "GIT_GRAPH_MAX_EVIDENCE",
    "GIT_GRAPH_MAX_PR_CASES",
    "GIT_GRAPH_PROJECTION",
    "GIT_GRAPH_PROTOCOL_VERSION",
    "GIT_GRAPH_REDUCER_ID",
    "GitEvidenceRelationKind",
    "GitGraphAncestryRelation",
    "GitGraphCommitNode",
    "GitGraphEvidenceRelation",
    "GitGraphMissingObject",
    "GitGraphObservation",
    "GitGraphPRAnchor",
    "GitGraphParentRelation",
    "GitGraphProjection",
    "GitGraphRepository",
    "GitGraphV1Error",
    "GitPRAnchorEndpoint",
    "GitRelationConfidence",
    "build_git_graph_reducer",
    "pr_anchor_commit_ancestry_evidence",
    "reduce_git_graph_events",
]
