from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Literal, cast

from ._timestamps import parse_rfc3339
from .models import FailureCase, Lesson, ProjectPolicy, Trace
from .policy import MEMORY_DECISION_REASON_MAX_CHARS, MEMORY_ID_MAX_CHARS, METADATA_VALUE_MAX_CHARS
from .store import TraceBackedMemoryStore


V3_CONTRACT_VERSION = "tbm.trust.v3"
V3_MIGRATION_MAPPING_VERSION = "tbm.snapshot.v2-to-v3.mapping.v1"
V3_MIGRATION_PLAN_VERSION = "tbm.snapshot.v2-to-v3.plan.v1"
V3_SOURCE_SNAPSHOT_VERSION = 2
V3_TARGET_SNAPSHOT_VERSION = 3
V3_MAX_REGISTRY_ITEMS = 100_000
V3_MAX_ARTIFACT_HASHES = 1_000
V3_MAX_MIGRATION_COUNT = 250_000
_V3_SOURCE_MAX_DEPTH = 100

RepositoryProvider = Literal[
    "local",
    "github",
    "gitlab",
    "bitbucket",
    "azure_devops",
    "other",
]
ScopeKind = Literal["global", "tenant", "repository"]
MemoryScopeKind = Literal["lesson", "project_policy"]
CommitRelation = Literal["ancestor"]
AncestryMode = Literal["required", "disabled"]
MigrationIssueSeverity = Literal["error", "warning"]
CommitRelationVerifier = Callable[[str, "CommitRelationEvidence"], bool]
_IssueSink = Callable[
    [str, MigrationIssueSeverity, str, str, str],
    None,
]

_REPOSITORY_PROVIDERS = {
    "local",
    "github",
    "gitlab",
    "bitbucket",
    "azure_devops",
    "other",
}
_SCOPE_KINDS = {"global", "tenant", "repository"}
_MEMORY_SCOPE_KINDS = {"lesson", "project_policy"}
_COMMIT_RELATIONS = {"ancestor"}
_ANCESTRY_MODES = {"required", "disabled"}
_MEASURED_RESULTS = {"pass", "fail", "error"}
_APPLICABILITY_FIELDS = {
    "branch",
    "prompt_version",
    "prompt_family",
    "tool",
    "tool_schema_version",
    "model",
    "model_family",
    "eval_suite",
    "task_type",
    "failure_type",
}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
V3_MIGRATION_PLAN_COUNT_NAMES = (
    "errors",
    "failure_cases",
    "lessons",
    "mapped_scopes",
    "project_policies",
    "regression_evidence",
    "repositories",
    "tenants",
    "traces",
    "usage_logs",
    "warnings",
)


class V3ContractError(ValueError):
    """Stable failure raised for malformed v3 contract input."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def canonical_sha256(value: object) -> str:
    """Return an algorithm-tagged digest of canonical JSON bytes."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise V3ContractError(
            "TBM_V3_NON_CANONICAL_JSON",
            "value cannot be encoded as finite canonical JSON",
        ) from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _freeze_source_snapshot(
    source: Mapping[str, object],
) -> dict[str, object]:
    active: set[int] = set()

    def freeze(value: object, depth: int) -> object:
        if depth > _V3_SOURCE_MAX_DEPTH:
            raise ValueError("source snapshot exceeds maximum depth")
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in active:
                raise ValueError("source snapshot contains a cycle")
            active.add(identity)
            try:
                frozen: dict[str, object] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise ValueError(
                            "source snapshot object keys must be strings"
                        )
                    key.encode("utf-8")
                    frozen[key] = freeze(item, depth + 1)
                return frozen
            finally:
                active.remove(identity)
        if type(value) is list:
            identity = id(value)
            if identity in active:
                raise ValueError("source snapshot contains a cycle")
            active.add(identity)
            try:
                return [freeze(item, depth + 1) for item in value]
            finally:
                active.remove(identity)
        if type(value) is str:
            value.encode("utf-8")
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError(
                    "source snapshot numbers must be finite"
                )
            return value
        if value is None or type(value) in {bool, int}:
            return value
        raise TypeError(
            "source snapshot values must use JSON-compatible types"
        )

    try:
        frozen_source = freeze(source, 0)
    except (
        RuntimeError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ) as error:
        raise V3ContractError(
            "TBM_V3_INVALID_SOURCE_SNAPSHOT",
            "source snapshot failed strict version-2 validation",
        ) from error
    if type(frozen_source) is not dict:
        raise V3ContractError(
            "TBM_V3_INVALID_SOURCE_SNAPSHOT",
            "source snapshot failed strict version-2 validation",
        )
    return cast(dict[str, object], frozen_source)


@dataclass(frozen=True)
class CanonicalRepository:
    repository_id: str
    provider: RepositoryProvider
    provider_repository_id: str
    canonical_locator_hash: str
    display_name: str
    legacy_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier(self.repository_id, "repository_id")
        if type(self.provider) is not str or self.provider not in _REPOSITORY_PROVIDERS:
            _invalid("provider must be a supported repository provider")
        _required_metadata(self.provider_repository_id, "provider_repository_id")
        _digest(self.canonical_locator_hash, "canonical_locator_hash")
        _required_metadata(self.display_name, "display_name")
        _string_tuple(
            self.legacy_aliases,
            "legacy_aliases",
            max_items=V3_MAX_REGISTRY_ITEMS,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "provider": self.provider,
            "provider_repository_id": self.provider_repository_id,
            "canonical_locator_hash": self.canonical_locator_hash,
            "display_name": self.display_name,
            "legacy_aliases": sorted(self.legacy_aliases),
        }


@dataclass(frozen=True)
class TenantIdentity:
    tenant_id: str
    display_name: str
    legacy_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_identifier(self.tenant_id, "tenant_id")
        _required_metadata(self.display_name, "display_name")
        _string_tuple(
            self.legacy_aliases,
            "legacy_aliases",
            max_items=V3_MAX_REGISTRY_ITEMS,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "legacy_aliases": sorted(self.legacy_aliases),
        }


@dataclass(frozen=True)
class TraceIdentityBinding:
    trace_id: str
    repository_id: str
    tenant_id: str

    def __post_init__(self) -> None:
        _required_identifier(self.trace_id, "trace_id")
        _required_identifier(self.repository_id, "repository_id")
        _required_identifier(self.tenant_id, "tenant_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "repository_id": self.repository_id,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True)
class AuthorizationScope:
    kind: ScopeKind
    tenant_id: str | None = None
    repository_id: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _SCOPE_KINDS:
            _invalid("scope kind must be global, tenant, or repository")
        _optional_identifier(self.tenant_id, "tenant_id")
        _optional_identifier(self.repository_id, "repository_id")
        if self.kind == "global" and (
            self.tenant_id is not None or self.repository_id is not None
        ):
            _invalid("global scope cannot declare tenant_id or repository_id")
        if self.kind == "tenant" and (
            self.tenant_id is None or self.repository_id is not None
        ):
            _invalid("tenant scope requires tenant_id and forbids repository_id")
        if self.kind == "repository" and (
            self.tenant_id is None or self.repository_id is None
        ):
            _invalid("repository scope requires tenant_id and repository_id")
        _attributes(self.attributes)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "attributes": dict(sorted(self.attributes)),
        }


@dataclass(frozen=True)
class MemoryScopeBinding:
    memory_kind: MemoryScopeKind
    memory_id: str
    scope: AuthorizationScope

    def __post_init__(self) -> None:
        if (
            type(self.memory_kind) is not str
            or self.memory_kind not in _MEMORY_SCOPE_KINDS
        ):
            _invalid("memory_kind must be lesson or project_policy")
        _required_identifier(self.memory_id, "memory_id")
        if type(self.scope) is not AuthorizationScope:
            _invalid("scope must be exactly an AuthorizationScope")

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_kind": self.memory_kind,
            "memory_id": self.memory_id,
            "scope": self.scope.to_dict(),
        }


@dataclass(frozen=True)
class CommitRelationEvidence:
    from_commit_sha: str
    to_commit_sha: str
    relation: CommitRelation
    verified_by: str
    verified_at: str

    def __post_init__(self) -> None:
        _required_metadata(self.from_commit_sha, "from_commit_sha")
        _required_metadata(self.to_commit_sha, "to_commit_sha")
        if type(self.relation) is not str or self.relation not in _COMMIT_RELATIONS:
            _invalid("commit relation must be ancestor")
        _required_identifier(self.verified_by, "verified_by")
        _timestamp(self.verified_at, "verified_at")

    def to_dict(self) -> dict[str, str]:
        return {
            "from_commit_sha": self.from_commit_sha,
            "to_commit_sha": self.to_commit_sha,
            "relation": self.relation,
            "verified_by": self.verified_by,
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True)
class RegressionEvidence:
    evidence_id: str
    case_id: str
    regression_trace_id: str
    regression_run_id: str
    evaluator_id: str
    evaluator_version: str
    eval_suite: str
    eval_case_id: str
    evaluated_commit_sha: str
    result: Literal["pass", "fail", "error"]
    observed_at: str
    source_to_fix: CommitRelationEvidence
    fix_to_regression: CommitRelationEvidence
    artifact_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_id",
            "case_id",
            "regression_trace_id",
            "regression_run_id",
            "evaluator_id",
        ):
            _required_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "evaluator_version",
            "eval_suite",
            "eval_case_id",
            "evaluated_commit_sha",
        ):
            _required_metadata(getattr(self, field_name), field_name)
        if type(self.result) is not str or self.result not in _MEASURED_RESULTS:
            _invalid("regression result must be pass, fail, or error")
        _timestamp(self.observed_at, "observed_at")
        if type(self.source_to_fix) is not CommitRelationEvidence:
            _invalid("source_to_fix must be exactly CommitRelationEvidence")
        if type(self.fix_to_regression) is not CommitRelationEvidence:
            _invalid("fix_to_regression must be exactly CommitRelationEvidence")
        if (
            type(self.artifact_hashes) is not tuple
            or len(self.artifact_hashes) > V3_MAX_ARTIFACT_HASHES
        ):
            _invalid(
                f"artifact_hashes must be a tuple with at most "
                f"{V3_MAX_ARTIFACT_HASHES} entries"
            )
        for value in self.artifact_hashes:
            _digest(value, "artifact_hash")
        if len(set(self.artifact_hashes)) != len(self.artifact_hashes):
            _invalid("artifact_hashes must not contain duplicates")

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "case_id": self.case_id,
            "regression_trace_id": self.regression_trace_id,
            "regression_run_id": self.regression_run_id,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "eval_suite": self.eval_suite,
            "eval_case_id": self.eval_case_id,
            "evaluated_commit_sha": self.evaluated_commit_sha,
            "result": self.result,
            "observed_at": self.observed_at,
            "source_to_fix": self.source_to_fix.to_dict(),
            "fix_to_regression": self.fix_to_regression.to_dict(),
            "artifact_hashes": sorted(self.artifact_hashes),
        }


@dataclass(frozen=True)
class GlobalPolicyApproval:
    policy_id: str
    principal_id: str
    authorization_event_id: str
    approved_at: str
    reason: str

    def __post_init__(self) -> None:
        _required_identifier(self.policy_id, "policy_id")
        _required_identifier(self.principal_id, "principal_id")
        _required_identifier(self.authorization_event_id, "authorization_event_id")
        _timestamp(self.approved_at, "approved_at")
        _required_reason(self.reason, "reason")

    def to_dict(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "principal_id": self.principal_id,
            "authorization_event_id": self.authorization_event_id,
            "approved_at": self.approved_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AncestryPolicy:
    mode: AncestryMode
    bypass_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in _ANCESTRY_MODES:
            _invalid("ancestry mode must be required or disabled")
        if self.mode == "required" and self.bypass_reason is not None:
            _invalid("required ancestry policy cannot declare bypass_reason")
        if self.mode == "disabled":
            _required_reason(self.bypass_reason, "bypass_reason")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "bypass_reason": self.bypass_reason,
        }


@dataclass(frozen=True)
class SnapshotV3MigrationMapping:
    repositories: tuple[CanonicalRepository, ...]
    tenants: tuple[TenantIdentity, ...]
    trace_bindings: tuple[TraceIdentityBinding, ...]
    memory_scopes: tuple[MemoryScopeBinding, ...]
    regression_evidence: tuple[RegressionEvidence, ...]
    global_policy_approvals: tuple[GlobalPolicyApproval, ...]
    ancestry_policy: AncestryPolicy
    mapping_version: str = V3_MIGRATION_MAPPING_VERSION

    def __post_init__(self) -> None:
        if self.mapping_version != V3_MIGRATION_MAPPING_VERSION:
            _invalid(
                "mapping_version must be "
                f"{V3_MIGRATION_MAPPING_VERSION}"
            )
        _typed_tuple(
            self.repositories,
            CanonicalRepository,
            "repositories",
        )
        _typed_tuple(self.tenants, TenantIdentity, "tenants")
        _typed_tuple(
            self.trace_bindings,
            TraceIdentityBinding,
            "trace_bindings",
        )
        _typed_tuple(
            self.memory_scopes,
            MemoryScopeBinding,
            "memory_scopes",
        )
        _typed_tuple(
            self.regression_evidence,
            RegressionEvidence,
            "regression_evidence",
        )
        _typed_tuple(
            self.global_policy_approvals,
            GlobalPolicyApproval,
            "global_policy_approvals",
        )
        if type(self.ancestry_policy) is not AncestryPolicy:
            _invalid("ancestry_policy must be exactly AncestryPolicy")
        _unique(self.repositories, "repository_id", "repositories")
        _unique_pairs(
            self.repositories,
            ("provider", "provider_repository_id"),
            "repositories",
        )
        _unique_aliases(
            self.repositories,
            "repository_id",
            "legacy_aliases",
            "repository legacy aliases",
        )
        _unique(self.tenants, "tenant_id", "tenants")
        _unique_aliases(
            self.tenants,
            "tenant_id",
            "legacy_aliases",
            "tenant legacy aliases",
        )
        _unique(self.trace_bindings, "trace_id", "trace_bindings")
        _unique_pairs(
            self.memory_scopes,
            ("memory_kind", "memory_id"),
            "memory_scopes",
        )
        _unique(
            self.regression_evidence,
            "case_id",
            "regression_evidence",
        )
        _unique(
            self.regression_evidence,
            "evidence_id",
            "regression_evidence",
        )
        _unique(
            self.global_policy_approvals,
            "policy_id",
            "global_policy_approvals",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mapping_version": self.mapping_version,
            "repositories": [
                item.to_dict()
                for item in sorted(
                    self.repositories,
                    key=lambda item: item.repository_id,
                )
            ],
            "tenants": [
                item.to_dict()
                for item in sorted(
                    self.tenants,
                    key=lambda item: item.tenant_id,
                )
            ],
            "trace_bindings": [
                item.to_dict()
                for item in sorted(
                    self.trace_bindings,
                    key=lambda item: item.trace_id,
                )
            ],
            "memory_scopes": [
                item.to_dict()
                for item in sorted(
                    self.memory_scopes,
                    key=lambda item: (item.memory_kind, item.memory_id),
                )
            ],
            "regression_evidence": [
                item.to_dict()
                for item in sorted(
                    self.regression_evidence,
                    key=lambda item: (item.case_id, item.evidence_id),
                )
            ],
            "global_policy_approvals": [
                item.to_dict()
                for item in sorted(
                    self.global_policy_approvals,
                    key=lambda item: item.policy_id,
                )
            ],
            "ancestry_policy": self.ancestry_policy.to_dict(),
        }


@dataclass(frozen=True)
class V3MigrationIssue:
    code: str
    severity: MigrationIssueSeverity
    record_kind: str
    record_id: str
    message: str

    def __post_init__(self) -> None:
        _required_identifier(self.code, "code")
        if (
            type(self.severity) is not str
            or self.severity not in {"error", "warning"}
        ):
            _invalid("migration issue severity must be error or warning")
        _required_identifier(self.record_kind, "record_kind")
        _required_identifier(self.record_id, "record_id")
        _required_reason(self.message, "message")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "record_kind": self.record_kind,
            "record_id": self.record_id,
            "message": self.message,
        }


@dataclass(frozen=True)
class SnapshotV3MigrationPlan:
    source_snapshot_sha256: str
    mapping_sha256: str
    ready: bool
    counts: tuple[tuple[str, int], ...]
    issues: tuple[V3MigrationIssue, ...]
    plan_version: str = V3_MIGRATION_PLAN_VERSION
    source_snapshot_version: int = V3_SOURCE_SNAPSHOT_VERSION
    target_snapshot_version: int = V3_TARGET_SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        if self.plan_version != V3_MIGRATION_PLAN_VERSION:
            _invalid(
                f"plan_version must be {V3_MIGRATION_PLAN_VERSION}"
            )
        if self.source_snapshot_version != V3_SOURCE_SNAPSHOT_VERSION:
            _invalid(
                "source_snapshot_version must be "
                f"{V3_SOURCE_SNAPSHOT_VERSION}"
            )
        if self.target_snapshot_version != V3_TARGET_SNAPSHOT_VERSION:
            _invalid(
                f"target_snapshot_version must be {V3_TARGET_SNAPSHOT_VERSION}"
            )
        _digest(self.source_snapshot_sha256, "source_snapshot_sha256")
        _digest(self.mapping_sha256, "mapping_sha256")
        if type(self.ready) is not bool:
            _invalid("ready must be a boolean")
        if type(self.counts) is not tuple:
            _invalid("counts must be a tuple")
        seen_counts: set[str] = set()
        count_values: dict[str, int] = {}
        for item in self.counts:
            if type(item) is not tuple or len(item) != 2:
                _invalid("count entries must be two-item tuples")
            name, value = item
            _required_identifier(name, "count name")
            if name in seen_counts:
                _invalid("counts must not contain duplicate names")
            seen_counts.add(name)
            if (
                type(value) is not int
                or value < 0
                or value > V3_MAX_MIGRATION_COUNT
            ):
                _invalid(
                    "count values must be integers from 0 through "
                    f"{V3_MAX_MIGRATION_COUNT}"
                )
            count_values[name] = value
        if seen_counts != set(V3_MIGRATION_PLAN_COUNT_NAMES):
            _invalid("counts must contain the complete v3 migration count set")
        _typed_tuple(self.issues, V3MigrationIssue, "issues")
        error_count = sum(issue.severity == "error" for issue in self.issues)
        warning_count = len(self.issues) - error_count
        if (
            count_values["errors"] != error_count
            or count_values["warnings"] != warning_count
        ):
            _invalid("error and warning counts must match migration issues")
        has_error = error_count > 0
        if self.ready == has_error:
            _invalid("ready must be true exactly when the plan has no errors")

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_version": self.plan_version,
            "source_snapshot_version": self.source_snapshot_version,
            "target_snapshot_version": self.target_snapshot_version,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "mapping_sha256": self.mapping_sha256,
            "ready": self.ready,
            "counts": {
                name: dict(self.counts)[name]
                for name in V3_MIGRATION_PLAN_COUNT_NAMES
            },
            "issues": [issue.to_dict() for issue in self.issues],
        }


def parse_v3_migration_mapping(
    payload: Mapping[str, object],
) -> SnapshotV3MigrationMapping:
    root = _object(
        payload,
        "v3 migration mapping",
        {
            "mapping_version",
            "repositories",
            "tenants",
            "trace_bindings",
            "memory_scopes",
            "regression_evidence",
            "global_policy_approvals",
            "ancestry_policy",
        },
    )
    return SnapshotV3MigrationMapping(
        mapping_version=_string(root["mapping_version"], "mapping_version"),
        repositories=tuple(
            _parse_repository(item)
            for item in _array(root["repositories"], "repositories")
        ),
        tenants=tuple(
            _parse_tenant(item)
            for item in _array(root["tenants"], "tenants")
        ),
        trace_bindings=tuple(
            _parse_trace_binding(item)
            for item in _array(root["trace_bindings"], "trace_bindings")
        ),
        memory_scopes=tuple(
            _parse_memory_scope(item)
            for item in _array(root["memory_scopes"], "memory_scopes")
        ),
        regression_evidence=tuple(
            _parse_regression_evidence(item)
            for item in _array(
                root["regression_evidence"],
                "regression_evidence",
            )
        ),
        global_policy_approvals=tuple(
            _parse_global_policy_approval(item)
            for item in _array(
                root["global_policy_approvals"],
                "global_policy_approvals",
            )
        ),
        ancestry_policy=_parse_ancestry_policy(root["ancestry_policy"]),
    )


def parse_v3_migration_plan(
    payload: Mapping[str, object],
) -> SnapshotV3MigrationPlan:
    root = _object(
        payload,
        "v3 migration plan",
        {
            "plan_version",
            "source_snapshot_version",
            "target_snapshot_version",
            "source_snapshot_sha256",
            "mapping_sha256",
            "ready",
            "counts",
            "issues",
        },
    )
    counts_data = root["counts"]
    if type(counts_data) is not dict:
        _invalid("v3 migration plan counts must be an object")
    if any(type(key) is not str for key in counts_data):
        _invalid("v3 migration plan count names must be strings")
    count_keys = set(counts_data)
    expected_count_keys = set(V3_MIGRATION_PLAN_COUNT_NAMES)
    if count_keys != expected_count_keys:
        missing = sorted(expected_count_keys - count_keys)
        unknown = sorted(count_keys - expected_count_keys)
        if missing:
            _invalid(
                f"v3 migration plan counts missing required field: {missing[0]}"
            )
        _invalid(
            f"v3 migration plan counts has unknown field: {unknown[0]}"
        )
    issues = tuple(
        _parse_migration_issue(item)
        for item in _array(root["issues"], "issues")
    )
    return SnapshotV3MigrationPlan(
        plan_version=_string(root["plan_version"], "plan_version"),
        source_snapshot_version=_integer(
            root["source_snapshot_version"],
            "source_snapshot_version",
        ),
        target_snapshot_version=_integer(
            root["target_snapshot_version"],
            "target_snapshot_version",
        ),
        source_snapshot_sha256=_string(
            root["source_snapshot_sha256"],
            "source_snapshot_sha256",
        ),
        mapping_sha256=_string(
            root["mapping_sha256"],
            "mapping_sha256",
        ),
        ready=_boolean(root["ready"], "ready"),
        counts=tuple(
            (
                name,
                _integer(counts_data[name], f"count {name}"),
            )
            for name in V3_MIGRATION_PLAN_COUNT_NAMES
        ),
        issues=issues,
    )


def plan_snapshot_v3_migration(
    source: TraceBackedMemoryStore | Mapping[str, object],
    mapping: SnapshotV3MigrationMapping | Mapping[str, object],
    *,
    commit_relation_verifier: CommitRelationVerifier | None = None,
) -> SnapshotV3MigrationPlan:
    """Validate explicit v3 bindings without mutating the v2 snapshot."""

    if type(source) is TraceBackedMemoryStore:
        source_snapshot = _freeze_source_snapshot(source.to_snapshot())
    elif isinstance(source, Mapping):
        source_snapshot = _freeze_source_snapshot(source)
    else:
        raise V3ContractError(
            "TBM_V3_INVALID_SOURCE",
            "source must be a TraceBackedMemoryStore or snapshot object",
        )
    source_version = source_snapshot.get("snapshot_version")
    if (
        type(source_version) is not int
        or source_version != V3_SOURCE_SNAPSHOT_VERSION
    ):
        raise V3ContractError(
            "TBM_V3_SOURCE_VERSION_MISMATCH",
            "v3 migration preflight requires an explicit snapshot version "
            f"{V3_SOURCE_SNAPSHOT_VERSION}",
        )
    try:
        store = TraceBackedMemoryStore.from_snapshot(source_snapshot)
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as error:
        raise V3ContractError(
            "TBM_V3_INVALID_SOURCE_SNAPSHOT",
            "source snapshot failed strict version-2 validation",
        ) from error
    if not isinstance(mapping, SnapshotV3MigrationMapping):
        if not isinstance(mapping, Mapping):
            raise V3ContractError(
                "TBM_V3_INVALID_MAPPING",
                "mapping must be a SnapshotV3MigrationMapping or object",
            )
        mapping = parse_v3_migration_mapping(mapping)

    snapshot = store.to_snapshot()
    if snapshot.get("snapshot_version") != V3_SOURCE_SNAPSHOT_VERSION:
        raise V3ContractError(
            "TBM_V3_SOURCE_VERSION_MISMATCH",
            "v3 migration preflight requires snapshot version "
            f"{V3_SOURCE_SNAPSHOT_VERSION}",
        )
    repositories = {
        repository.repository_id: repository
        for repository in mapping.repositories
    }
    tenants = {tenant.tenant_id: tenant for tenant in mapping.tenants}
    trace_bindings = {
        binding.trace_id: binding for binding in mapping.trace_bindings
    }
    scope_bindings = {
        (binding.memory_kind, binding.memory_id): binding
        for binding in mapping.memory_scopes
    }
    evidence_by_case = {
        evidence.case_id: evidence
        for evidence in mapping.regression_evidence
    }
    approvals = {
        approval.policy_id: approval
        for approval in mapping.global_policy_approvals
    }
    traces = store.traces
    cases = store.failure_cases
    lessons = store.lessons
    policies = store.project_policies
    issues: list[V3MigrationIssue] = []

    def issue(
        code: str,
        severity: MigrationIssueSeverity,
        record_kind: str,
        record_id: str,
        message: str,
    ) -> None:
        issues.append(
            V3MigrationIssue(
                code=code,
                severity=severity,
                record_kind=record_kind,
                record_id=record_id,
                message=message,
            )
        )

    if mapping.ancestry_policy.mode == "required":
        if commit_relation_verifier is None:
            issue(
                "TBM_V3_ANCESTRY_VERIFIER_REQUIRED",
                "error",
                "ancestry_policy",
                "*",
                (
                    "required ancestry policy needs a trusted commit-relation "
                    "verifier"
                ),
            )
    else:
        issue(
            "TBM_V3_ANCESTRY_DISABLED",
            "warning",
            "ancestry_policy",
            "*",
            (
                "commit ancestry verification is disabled under an explicit "
                "operator bypass"
            ),
        )

    for trace in traces.values():
        _plan_trace_identity(
            trace,
            trace_bindings.get(trace.trace_id),
            repositories,
            tenants,
            issue,
        )
    for trace_id in sorted(set(trace_bindings) - set(traces)):
        issue(
            "TBM_V3_UNKNOWN_TRACE_BINDING",
            "error",
            "trace",
            trace_id,
            "trace binding does not reference a source snapshot Trace",
        )

    memory_records: tuple[tuple[str, str, Lesson | ProjectPolicy], ...] = (
        *(
            ("lesson", lesson.lesson_id, lesson)
            for lesson in lessons.values()
        ),
        *(
            ("project_policy", policy.policy_id, policy)
            for policy in policies.values()
        ),
    )
    for memory_kind, memory_id, memory in memory_records:
        _plan_memory_scope(
            memory_kind,
            memory_id,
            memory,
            scope_bindings.get((memory_kind, memory_id)),
            repositories,
            tenants,
            cases,
            trace_bindings,
            approvals,
            issue,
        )
    known_memory_keys = {
        (memory_kind, memory_id)
        for memory_kind, memory_id, _memory in memory_records
    }
    for memory_kind, memory_id in sorted(
        set(scope_bindings) - known_memory_keys
    ):
        issue(
            "TBM_V3_UNKNOWN_SCOPE_BINDING",
            "error",
            memory_kind,
            memory_id,
            "scope binding does not reference a source snapshot memory",
        )

    for case in cases.values():
        _plan_regression_evidence(
            case,
            evidence_by_case.get(case.case_id),
            traces,
            trace_bindings,
            (
                commit_relation_verifier
                if mapping.ancestry_policy.mode == "required"
                else None
            ),
            issue,
        )
    for case_id in sorted(set(evidence_by_case) - set(cases)):
        issue(
            "TBM_V3_UNKNOWN_REGRESSION_CASE",
            "error",
            "failure_case",
            case_id,
            "regression evidence does not reference a source FailureCase",
        )

    global_policy_ids = {
        policy_id
        for (memory_kind, policy_id), binding in scope_bindings.items()
        if memory_kind == "project_policy" and binding.scope.kind == "global"
    }
    for policy_id in sorted(set(approvals) - global_policy_ids):
        issue(
            "TBM_V3_UNUSED_GLOBAL_APPROVAL",
            "warning",
            "project_policy",
            policy_id,
            "global-policy approval is not used by a global scope binding",
        )

    usage_logs = tuple(store.usage_logs)
    unbound_usage = [
        log.decision_id
        for log in usage_logs
        if log.trace_id is None or log.trace_id not in trace_bindings
    ]
    for decision_id in unbound_usage:
        issue(
            "TBM_V3_USAGE_TRACE_BINDING_REQUIRED",
            "error",
            "usage_log",
            decision_id,
            "usage log requires a Trace with an explicit identity binding",
        )
    if usage_logs:
        issue(
            "TBM_V3_LEGACY_REPLAY_PARTIAL",
            "warning",
            "usage_log",
            "*",
            (
                f"{len(usage_logs)} v2 usage logs lack complete replay "
                "metadata and must remain marked legacy_partial"
            ),
        )

    issues.sort(
        key=lambda item: (
            0 if item.severity == "error" else 1,
            item.code,
            item.record_kind,
            item.record_id,
            item.message,
        )
    )
    error_count = sum(issue_item.severity == "error" for issue_item in issues)
    warning_count = len(issues) - error_count
    counts = (
        ("errors", error_count),
        ("failure_cases", len(cases)),
        ("lessons", len(lessons)),
        ("mapped_scopes", len(mapping.memory_scopes)),
        ("project_policies", len(policies)),
        ("regression_evidence", len(mapping.regression_evidence)),
        ("repositories", len(mapping.repositories)),
        ("tenants", len(mapping.tenants)),
        ("traces", len(traces)),
        ("usage_logs", len(usage_logs)),
        ("warnings", warning_count),
    )
    return SnapshotV3MigrationPlan(
        source_snapshot_sha256=canonical_sha256(source_snapshot),
        mapping_sha256=canonical_sha256(mapping.to_dict()),
        ready=error_count == 0,
        counts=counts,
        issues=tuple(issues),
    )


def _plan_trace_identity(
    trace: Trace,
    binding: TraceIdentityBinding | None,
    repositories: Mapping[str, CanonicalRepository],
    tenants: Mapping[str, TenantIdentity],
    issue: _IssueSink,
) -> None:
    if binding is None:
        issue(
            "TBM_V3_TRACE_IDENTITY_REQUIRED",
            "error",
            "trace",
            trace.trace_id,
            "Trace requires an explicit canonical repository and tenant binding",
        )
        return
    repository = repositories.get(binding.repository_id)
    tenant = tenants.get(binding.tenant_id)
    if repository is None:
        issue(
            "TBM_V3_UNKNOWN_REPOSITORY",
            "error",
            "trace",
            trace.trace_id,
            f"unknown repository_id: {binding.repository_id}",
        )
    elif trace.repo is None:
        issue(
            "TBM_V3_LEGACY_REPO_MISSING",
            "warning",
            "trace",
            trace.trace_id,
            "legacy Trace has no repo; explicit canonical binding is retained",
        )
    elif trace.repo not in repository.legacy_aliases:
        issue(
            "TBM_V3_REPOSITORY_ALIAS_MISMATCH",
            "error",
            "trace",
            trace.trace_id,
            "legacy Trace repo is not an alias of the bound repository",
        )
    if tenant is None:
        issue(
            "TBM_V3_UNKNOWN_TENANT",
            "error",
            "trace",
            trace.trace_id,
            f"unknown tenant_id: {binding.tenant_id}",
        )
    elif trace.tenant is None:
        issue(
            "TBM_V3_LEGACY_TENANT_MISSING",
            "warning",
            "trace",
            trace.trace_id,
            "legacy Trace has no tenant; explicit canonical binding is retained",
        )
    elif trace.tenant not in tenant.legacy_aliases:
        issue(
            "TBM_V3_TENANT_ALIAS_MISMATCH",
            "error",
            "trace",
            trace.trace_id,
            "legacy Trace tenant is not an alias of the bound tenant",
        )


def _plan_memory_scope(
    memory_kind: str,
    memory_id: str,
    memory: Lesson | ProjectPolicy,
    binding: MemoryScopeBinding | None,
    repositories: Mapping[str, CanonicalRepository],
    tenants: Mapping[str, TenantIdentity],
    cases: Mapping[str, FailureCase],
    trace_bindings: Mapping[str, TraceIdentityBinding],
    approvals: Mapping[str, GlobalPolicyApproval],
    issue: _IssueSink,
) -> None:
    if binding is None:
        issue(
            "TBM_V3_EXPLICIT_SCOPE_REQUIRED",
            "error",
            memory_kind,
            memory_id,
            "memory requires an explicit global, tenant, or repository scope",
        )
        return
    scope = binding.scope
    repository = (
        repositories.get(scope.repository_id)
        if scope.repository_id is not None
        else None
    )
    tenant = (
        tenants.get(scope.tenant_id)
        if scope.tenant_id is not None
        else None
    )
    if scope.repository_id is not None and repository is None:
        issue(
            "TBM_V3_UNKNOWN_REPOSITORY",
            "error",
            memory_kind,
            memory_id,
            f"unknown repository_id: {scope.repository_id}",
        )
    if scope.tenant_id is not None and tenant is None:
        issue(
            "TBM_V3_UNKNOWN_TENANT",
            "error",
            memory_kind,
            memory_id,
            f"unknown tenant_id: {scope.tenant_id}",
        )
    if memory_kind == "lesson" and scope.kind == "global":
        issue(
            "TBM_V3_GLOBAL_LESSON_FORBIDDEN",
            "error",
            memory_kind,
            memory_id,
            "only project policies may use global authorization scope",
        )
    if memory_kind == "project_policy" and scope.kind == "global":
        if memory_id not in approvals:
            issue(
                "TBM_V3_GLOBAL_POLICY_APPROVAL_REQUIRED",
                "error",
                memory_kind,
                memory_id,
                "global policy requires a privileged approval record",
            )

    attributes = dict(scope.attributes)
    for key, value in memory.scope.items():
        if key == "repo":
            if (
                scope.kind != "repository"
                or repository is None
                or value not in repository.legacy_aliases
            ):
                issue(
                    "TBM_V3_SCOPE_REPOSITORY_BROADENED",
                    "error",
                    memory_kind,
                    memory_id,
                    "legacy repo scope must map to the same repository scope",
                )
        elif key == "tenant":
            if tenant is None or value not in tenant.legacy_aliases:
                issue(
                    "TBM_V3_SCOPE_TENANT_BROADENED",
                    "error",
                    memory_kind,
                    memory_id,
                    "legacy tenant scope must map to the same tenant",
                )
        elif attributes.get(key) != value:
            issue(
                "TBM_V3_APPLICABILITY_BROADENED",
                "error",
                memory_kind,
                memory_id,
                f"legacy applicability field is not preserved: {key}",
            )

    if memory_kind != "lesson" or not isinstance(memory, Lesson):
        return
    case = cases.get(memory.source_case_id)
    source_binding = (
        trace_bindings.get(case.source_trace_id)
        if case is not None
        else None
    )
    if source_binding is None:
        return
    if scope.tenant_id != source_binding.tenant_id:
        issue(
            "TBM_V3_LESSON_SOURCE_TENANT_MISMATCH",
            "error",
            memory_kind,
            memory_id,
            "lesson scope tenant does not match its source Trace",
        )
    if (
        scope.kind == "repository"
        and scope.repository_id != source_binding.repository_id
    ):
        issue(
            "TBM_V3_LESSON_SOURCE_REPOSITORY_MISMATCH",
            "error",
            memory_kind,
            memory_id,
            "lesson scope repository does not match its source Trace",
        )


def _plan_regression_evidence(
    case: FailureCase,
    evidence: RegressionEvidence | None,
    traces: Mapping[str, Trace],
    trace_bindings: Mapping[str, TraceIdentityBinding],
    commit_relation_verifier: CommitRelationVerifier | None,
    issue: _IssueSink,
) -> None:
    required = case.status == "verified" or case.regression_passed
    if evidence is None:
        if required:
            issue(
                "TBM_V3_STRUCTURED_REGRESSION_REQUIRED",
                "error",
                "failure_case",
                case.case_id,
                "verified legacy case requires structured regression evidence",
            )
        return
    if not required:
        issue(
            "TBM_V3_UNUSED_REGRESSION_EVIDENCE",
            "warning",
            "failure_case",
            case.case_id,
            "structured evidence is not used by this unverified legacy case",
        )
    if evidence.result != "pass":
        issue(
            "TBM_V3_REGRESSION_NOT_PASSING",
            "error",
            "failure_case",
            case.case_id,
            "verified case requires a passing regression result",
        )
    if case.fix_commit_sha is None:
        issue(
            "TBM_V3_FIX_COMMIT_REQUIRED",
            "error",
            "failure_case",
            case.case_id,
            "structured regression evidence requires fix_commit_sha",
        )
    else:
        if (
            evidence.source_to_fix.from_commit_sha != case.commit_sha
            or evidence.source_to_fix.to_commit_sha != case.fix_commit_sha
        ):
            issue(
                "TBM_V3_SOURCE_FIX_RELATION_MISMATCH",
                "error",
                "failure_case",
                case.case_id,
                "source-to-fix relation does not bind the case commits",
            )
        if (
            evidence.fix_to_regression.from_commit_sha
            != case.fix_commit_sha
            or evidence.fix_to_regression.to_commit_sha
            != evidence.evaluated_commit_sha
        ):
            issue(
                "TBM_V3_FIX_REGRESSION_RELATION_MISMATCH",
                "error",
                "failure_case",
                case.case_id,
                "fix-to-regression relation does not bind evaluated commit",
            )

    regression_trace = traces.get(evidence.regression_trace_id)
    if regression_trace is None:
        issue(
            "TBM_V3_REGRESSION_TRACE_REQUIRED",
            "error",
            "failure_case",
            case.case_id,
            "regression evidence must reference a stored Trace",
        )
        return
    if (
        regression_trace.run_id != evidence.regression_run_id
        or regression_trace.commit_sha != evidence.evaluated_commit_sha
        or regression_trace.eval_result != evidence.result
        or regression_trace.eval_suite != evidence.eval_suite
    ):
        issue(
            "TBM_V3_REGRESSION_TRACE_MISMATCH",
            "error",
            "failure_case",
            case.case_id,
            "regression Trace does not match run, commit, suite, and result evidence",
        )
    source_trace = traces.get(case.source_trace_id)
    source_binding = (
        trace_bindings.get(source_trace.trace_id)
        if source_trace is not None
        else None
    )
    regression_binding = trace_bindings.get(regression_trace.trace_id)
    if (
        source_binding is None
        or regression_binding is None
        or source_binding.repository_id != regression_binding.repository_id
        or source_binding.tenant_id != regression_binding.tenant_id
    ):
        issue(
            "TBM_V3_REGRESSION_IDENTITY_MISMATCH",
            "error",
            "failure_case",
            case.case_id,
            "source and regression Traces require the same repository and tenant",
        )
        return
    if commit_relation_verifier is None:
        return
    for relation_name, relation in (
        ("source_to_fix", evidence.source_to_fix),
        ("fix_to_regression", evidence.fix_to_regression),
    ):
        try:
            verified = commit_relation_verifier(
                source_binding.repository_id,
                relation,
            )
        except Exception:
            issue(
                "TBM_V3_ANCESTRY_VERIFICATION_FAILED",
                "error",
                "failure_case",
                case.case_id,
                (
                    f"trusted verifier failed while checking "
                    f"{relation_name}"
                ),
            )
            continue
        if type(verified) is not bool:
            issue(
                "TBM_V3_ANCESTRY_VERIFICATION_FAILED",
                "error",
                "failure_case",
                case.case_id,
                (
                    f"trusted verifier returned an invalid result for "
                    f"{relation_name}"
                ),
            )
        elif not verified:
            issue(
                "TBM_V3_COMMIT_RELATION_UNVERIFIED",
                "error",
                "failure_case",
                case.case_id,
                f"trusted verifier rejected {relation_name}",
            )


def _parse_repository(value: object) -> CanonicalRepository:
    item = _object(
        value,
        "repository",
        {
            "repository_id",
            "provider",
            "provider_repository_id",
            "canonical_locator_hash",
            "display_name",
            "legacy_aliases",
        },
    )
    return CanonicalRepository(
        repository_id=_string(item["repository_id"], "repository_id"),
        provider=_string(item["provider"], "provider"),
        provider_repository_id=_string(
            item["provider_repository_id"],
            "provider_repository_id",
        ),
        canonical_locator_hash=_string(
            item["canonical_locator_hash"],
            "canonical_locator_hash",
        ),
        display_name=_string(item["display_name"], "display_name"),
        legacy_aliases=_parse_string_array(
            item["legacy_aliases"],
            "legacy_aliases",
        ),
    )


def _parse_tenant(value: object) -> TenantIdentity:
    item = _object(
        value,
        "tenant",
        {"tenant_id", "display_name", "legacy_aliases"},
    )
    return TenantIdentity(
        tenant_id=_string(item["tenant_id"], "tenant_id"),
        display_name=_string(item["display_name"], "display_name"),
        legacy_aliases=_parse_string_array(
            item["legacy_aliases"],
            "legacy_aliases",
        ),
    )


def _parse_trace_binding(value: object) -> TraceIdentityBinding:
    item = _object(
        value,
        "trace binding",
        {"trace_id", "repository_id", "tenant_id"},
    )
    return TraceIdentityBinding(
        trace_id=_string(item["trace_id"], "trace_id"),
        repository_id=_string(item["repository_id"], "repository_id"),
        tenant_id=_string(item["tenant_id"], "tenant_id"),
    )


def _parse_memory_scope(value: object) -> MemoryScopeBinding:
    item = _object(
        value,
        "memory scope binding",
        {"memory_kind", "memory_id", "scope"},
    )
    scope_data = _object(
        item["scope"],
        "authorization scope",
        {"kind", "tenant_id", "repository_id", "attributes"},
    )
    attributes_data = scope_data["attributes"]
    if type(attributes_data) is not dict:
        _invalid("authorization scope attributes must be an object")
    if any(type(key) is not str for key in attributes_data):
        _invalid("authorization scope attribute names must be strings")
    attributes = tuple(
        (
            _string(key, "attribute name"),
            _string(value, f"attribute {key}"),
        )
        for key, value in sorted(attributes_data.items())
    )
    return MemoryScopeBinding(
        memory_kind=_string(item["memory_kind"], "memory_kind"),
        memory_id=_string(item["memory_id"], "memory_id"),
        scope=AuthorizationScope(
            kind=_string(scope_data["kind"], "scope kind"),
            tenant_id=_nullable_string(
                scope_data["tenant_id"],
                "tenant_id",
            ),
            repository_id=_nullable_string(
                scope_data["repository_id"],
                "repository_id",
            ),
            attributes=attributes,
        ),
    )


def _parse_commit_relation(value: object, label: str) -> CommitRelationEvidence:
    item = _object(
        value,
        label,
        {
            "from_commit_sha",
            "to_commit_sha",
            "relation",
            "verified_by",
            "verified_at",
        },
    )
    return CommitRelationEvidence(
        from_commit_sha=_string(item["from_commit_sha"], "from_commit_sha"),
        to_commit_sha=_string(item["to_commit_sha"], "to_commit_sha"),
        relation=_string(item["relation"], "relation"),
        verified_by=_string(item["verified_by"], "verified_by"),
        verified_at=_string(item["verified_at"], "verified_at"),
    )


def _parse_regression_evidence(value: object) -> RegressionEvidence:
    fields = {
        "evidence_id",
        "case_id",
        "regression_trace_id",
        "regression_run_id",
        "evaluator_id",
        "evaluator_version",
        "eval_suite",
        "eval_case_id",
        "evaluated_commit_sha",
        "result",
        "observed_at",
        "source_to_fix",
        "fix_to_regression",
        "artifact_hashes",
    }
    item = _object(value, "regression evidence", fields)
    return RegressionEvidence(
        evidence_id=_string(item["evidence_id"], "evidence_id"),
        case_id=_string(item["case_id"], "case_id"),
        regression_trace_id=_string(
            item["regression_trace_id"],
            "regression_trace_id",
        ),
        regression_run_id=_string(
            item["regression_run_id"],
            "regression_run_id",
        ),
        evaluator_id=_string(item["evaluator_id"], "evaluator_id"),
        evaluator_version=_string(
            item["evaluator_version"],
            "evaluator_version",
        ),
        eval_suite=_string(item["eval_suite"], "eval_suite"),
        eval_case_id=_string(item["eval_case_id"], "eval_case_id"),
        evaluated_commit_sha=_string(
            item["evaluated_commit_sha"],
            "evaluated_commit_sha",
        ),
        result=_string(item["result"], "result"),
        observed_at=_string(item["observed_at"], "observed_at"),
        source_to_fix=_parse_commit_relation(
            item["source_to_fix"],
            "source_to_fix",
        ),
        fix_to_regression=_parse_commit_relation(
            item["fix_to_regression"],
            "fix_to_regression",
        ),
        artifact_hashes=_parse_string_array(
            item["artifact_hashes"],
            "artifact_hashes",
        ),
    )


def _parse_global_policy_approval(value: object) -> GlobalPolicyApproval:
    item = _object(
        value,
        "global policy approval",
        {
            "policy_id",
            "principal_id",
            "authorization_event_id",
            "approved_at",
            "reason",
        },
    )
    return GlobalPolicyApproval(
        policy_id=_string(item["policy_id"], "policy_id"),
        principal_id=_string(item["principal_id"], "principal_id"),
        authorization_event_id=_string(
            item["authorization_event_id"],
            "authorization_event_id",
        ),
        approved_at=_string(item["approved_at"], "approved_at"),
        reason=_string(item["reason"], "reason"),
    )


def _parse_ancestry_policy(value: object) -> AncestryPolicy:
    item = _object(
        value,
        "ancestry policy",
        {"mode", "bypass_reason"},
    )
    return AncestryPolicy(
        mode=_string(item["mode"], "mode"),
        bypass_reason=_nullable_string(
            item["bypass_reason"],
            "bypass_reason",
        ),
    )


def _parse_migration_issue(value: object) -> V3MigrationIssue:
    item = _object(
        value,
        "v3 migration issue",
        {
            "code",
            "severity",
            "record_kind",
            "record_id",
            "message",
        },
    )
    return V3MigrationIssue(
        code=_string(item["code"], "code"),
        severity=_string(item["severity"], "severity"),
        record_kind=_string(item["record_kind"], "record_kind"),
        record_id=_string(item["record_id"], "record_id"),
        message=_string(item["message"], "message"),
    )


def _object(
    value: object,
    label: str,
    required_fields: set[str],
) -> dict[str, object]:
    if type(value) is not dict:
        _invalid(f"{label} must be an object")
    if any(type(key) is not str for key in value):
        _invalid(f"{label} field names must be strings")
    for key in value:
        _unicode(key, f"{label} field name")
    keys = set(value)
    if keys != required_fields:
        missing = sorted(required_fields - keys)
        unknown = sorted(keys - required_fields)
        if missing:
            _invalid(f"{label} missing required field: {missing[0]}")
        _invalid(f"{label} has unknown field: {unknown[0]}")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        _invalid(f"{label} must be an array")
    if len(value) > V3_MAX_REGISTRY_ITEMS:
        _invalid(
            f"{label} contains more than {V3_MAX_REGISTRY_ITEMS} items"
        )
    return cast(list[object], value)


def _parse_string_array(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _string(item, label)
        for item in _array(value, label)
    )


def _string(value: object, label: str) -> str:
    if type(value) is not str:
        _invalid(f"{label} must be a string")
    string_value = cast(str, value)
    _unicode(string_value, label)
    return string_value


def _nullable_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        _invalid(f"{label} must be an integer")
    return cast(int, value)


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        _invalid(f"{label} must be a boolean")
    return cast(bool, value)


def _required_identifier(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_ID_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{MEMORY_ID_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _optional_identifier(value: object, field_name: str) -> None:
    if value is not None:
        _required_identifier(value, field_name)


def _required_metadata(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > METADATA_VALUE_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{METADATA_VALUE_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _required_reason(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_DECISION_REASON_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{MEMORY_DECISION_REASON_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _unicode(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise V3ContractError(
            "TBM_V3_INVALID_CONTRACT",
            f"{field_name} must contain valid Unicode",
        ) from error


def _timestamp(value: object, field_name: str) -> None:
    try:
        parse_rfc3339(value)
    except ValueError as error:
        raise V3ContractError(
            "TBM_V3_INVALID_CONTRACT",
            f"{field_name} must be a timezone-aware RFC 3339 date-time",
        ) from error


def _digest(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _invalid(f"{field_name} must use sha256:<64 lowercase hex>")


def _string_tuple(
    value: object,
    field_name: str,
    *,
    max_items: int,
) -> None:
    if type(value) is not tuple or len(value) > max_items:
        _invalid(
            f"{field_name} must be a tuple with at most {max_items} entries"
        )
    for item in value:
        _required_metadata(item, field_name)
    if len(set(value)) != len(value):
        _invalid(f"{field_name} must not contain duplicates")


def _attributes(value: object) -> None:
    if type(value) is not tuple:
        _invalid("attributes must be a tuple")
    seen: set[str] = set()
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            _invalid("attribute entries must be two-item tuples")
        key, attribute_value = item
        if type(key) is not str:
            _invalid("attribute names must be strings")
        _required_metadata(attribute_value, f"attribute {key}")
        if key in seen:
            _invalid("attributes must not contain duplicate fields")
        seen.add(key)
        if key not in _APPLICABILITY_FIELDS:
            _invalid(f"unsupported applicability attribute: {key}")


def _typed_tuple(
    value: object,
    record_type: type[object],
    field_name: str,
) -> None:
    if type(value) is not tuple or len(value) > V3_MAX_REGISTRY_ITEMS:
        _invalid(
            f"{field_name} must be a tuple with at most "
            f"{V3_MAX_REGISTRY_ITEMS} entries"
        )
    if any(type(item) is not record_type for item in value):
        _invalid(f"{field_name} entries must be exactly {record_type.__name__}")


def _unique(
    records: tuple[object, ...],
    field_name: str,
    label: str,
) -> None:
    values = [getattr(record, field_name) for record in records]
    if len(set(values)) != len(values):
        _invalid(f"{label} must have unique {field_name} values")


def _unique_pairs(
    records: tuple[object, ...],
    field_names: tuple[str, str],
    label: str,
) -> None:
    values = [
        tuple(getattr(record, field_name) for field_name in field_names)
        for record in records
    ]
    if len(set(values)) != len(values):
        _invalid(
            f"{label} must have unique "
            + "/".join(field_names)
            + " values"
        )


def _unique_aliases(
    records: tuple[object, ...],
    canonical_field_name: str,
    field_name: str,
    label: str,
) -> None:
    aliases = [
        alias
        for record in records
        for alias in getattr(record, field_name)
    ]
    if len(set(aliases)) != len(aliases):
        _invalid(f"{label} must be globally unique")
    canonical_ids = {
        getattr(record, canonical_field_name)
        for record in records
    }
    if canonical_ids.intersection(aliases):
        _invalid(f"{label} must not overlap canonical identifiers")


def _invalid(message: str) -> None:
    raise V3ContractError("TBM_V3_INVALID_CONTRACT", message)
