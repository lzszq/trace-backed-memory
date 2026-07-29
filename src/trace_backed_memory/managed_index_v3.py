from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re
from typing import Literal, NoReturn, Protocol, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from .activated_revision_v3 import ActivatedRevisionCandidate
from .contracts_v3 import canonical_sha256
from .retrieval_policy_v3 import RetrievalPolicyBundle
from .retrieval_preparation_v3 import (
    CandidateDiscoveryResult,
    CandidateIndexRecord,
    RETRIEVAL_PREPARATION_MAX_CANDIDATES,
    RetrievalPreparationRequest,
    SemanticQueryVector,
)
from .retrieval_v3 import IndexKind, IndexVersion
from .service_v3 import (
    AuthenticatedServiceContext,
    AuthorizedRetrievalScope,
)


MANAGED_INDEX_BUNDLE_CONTRACT_VERSION = "tbm.managed-index-bundle.v3"
MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES = 64 * 1024 * 1024
MANAGED_INDEX_BUNDLE_JSON_MAX_DEPTH = 32
MANAGED_INDEX_BUNDLE_JSON_MAX_NODES = 500_000
MANAGED_INDEX_MAX_CANDIDATES = RETRIEVAL_PREPARATION_MAX_CANDIDATES
MANAGED_INDEX_MAX_TEXT_BYTES = 64 * 1024
MANAGED_INDEX_MAX_TOKENS_PER_CANDIDATE = 4_096
MANAGED_INDEX_MAX_SCOPE_ATTRIBUTES = 64
MANAGED_INDEX_MAX_EVIDENCE_IDS = 4_096
MANAGED_INDEX_MAX_EVIDENCE_EDGES = 50_000
MANAGED_INDEX_MAX_GIT_COMMITS = 20_000
MANAGED_INDEX_MAX_GIT_EDGES = 50_000
MANAGED_INDEX_MAX_SEMANTIC_DIMENSIONS = 4_096

_IDENTIFIER_MAX_CHARS = 128
_METADATA_MAX_CHARS = 512
_INDEX_KINDS: tuple[IndexKind, ...] = (
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "git_graph",
)
_INDEX_KIND_ORDER = {value: index for index, value in enumerate(_INDEX_KINDS)}
_BUNDLE_ID_RE = re.compile(r"^managed_index_bundle_sha256_[0-9a-f]{64}$")
_AUTHORIZATION_ID_RE = re.compile(r"^authz_sha256_[0-9a-f]{64}$")
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CLASSIFICATIONS = frozenset({"public", "internal", "confidential", "restricted"})
_MEMORY_KINDS = frozenset({"lesson", "project_policy"})
_MEMORY_TYPES = frozenset({"procedural", "semantic", "episodic", "policy"})
_BUNDLE_FIELDS = frozenset(
    {
        "contract_version",
        "bundle_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "retriever_id",
        "retriever_version",
        "tokenizer_id",
        "tokenizer_version",
        "semantic_provider_id",
        "semantic_provider_version",
        "semantic_metric",
        "semantic_dimension",
        "source_catalog_sha256",
        "index_versions",
        "candidates",
        "evidence_edges",
        "git_commits",
        "git_edges",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "memory_id",
        "memory_revision_id",
        "candidate_sha256",
        "memory_kind",
        "memory_type",
        "classification",
        "scope_attributes",
        "eval_leaking",
        "lexical_tokens",
        "semantic_vector",
        "evidence_ids",
        "git_anchor_commit_sha",
    }
)
_EVIDENCE_EDGE_FIELDS = frozenset({"query_token", "evidence_id", "weight"})
_GIT_EDGE_FIELDS = frozenset({"child_commit_sha", "parent_commit_sha"})
_INDEX_VERSION_FIELDS = frozenset(
    {"index_kind", "index_id", "index_version", "content_sha256"}
)


class ManagedIndexV3ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ManagedIndexV3Error(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ManagedIndexSource:
    candidate: ActivatedRevisionCandidate = field(repr=False)
    index_text: str | None = field(default=None, repr=False)
    semantic_vector: tuple[float, ...] | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if type(self.candidate) is not ActivatedRevisionCandidate:
            _invalid("candidate must be exactly ActivatedRevisionCandidate")
        if self.index_text is not None:
            _bounded_text(
                self.index_text,
                "index_text",
                maximum_bytes=MANAGED_INDEX_MAX_TEXT_BYTES,
            )
        if self.semantic_vector is not None:
            _vector(
                self.semantic_vector,
                "semantic_vector",
                allow_empty=False,
            )
        classification = self.candidate.revision.content_artifact.classification
        if classification in {"confidential", "restricted"} and (
            self.index_text is not None or self.semantic_vector is not None
        ):
            _invalid(
                "sensitive candidates cannot place content-derived data in managed indexes"
            )


@dataclass(frozen=True)
class ManagedEvidenceEdge:
    query_token: str
    evidence_id: str
    weight: float

    def __post_init__(self) -> None:
        normalized = _single_token(self.query_token)
        object.__setattr__(self, "query_token", normalized)
        _identifier(self.evidence_id, "evidence_id")
        object.__setattr__(
            self,
            "weight",
            _unit_score(self.weight, "weight"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "query_token": self.query_token,
            "evidence_id": self.evidence_id,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class ManagedGitEdge:
    child_commit_sha: str
    parent_commit_sha: str

    def __post_init__(self) -> None:
        _metadata(self.child_commit_sha, "child_commit_sha")
        _metadata(self.parent_commit_sha, "parent_commit_sha")
        if self.child_commit_sha == self.parent_commit_sha:
            _invalid("git edge cannot reference the same commit twice")

    def to_dict(self) -> dict[str, str]:
        return {
            "child_commit_sha": self.child_commit_sha,
            "parent_commit_sha": self.parent_commit_sha,
        }


@dataclass(frozen=True)
class ManagedIndexCandidate:
    memory_id: str
    memory_revision_id: str
    candidate_sha256: str
    memory_kind: Literal["lesson", "project_policy"]
    memory_type: Literal["procedural", "semantic", "episodic", "policy"]
    classification: Literal[
        "public",
        "internal",
        "confidential",
        "restricted",
    ]
    scope_attributes: tuple[tuple[str, str], ...]
    eval_leaking: bool
    lexical_tokens: tuple[str, ...]
    semantic_vector: tuple[float, ...] | None
    evidence_ids: tuple[str, ...]
    git_anchor_commit_sha: str | None

    def __post_init__(self) -> None:
        _identifier(self.memory_id, "memory_id")
        if (
            type(self.memory_revision_id) is not str
            or _REVISION_ID_RE.fullmatch(self.memory_revision_id) is None
        ):
            _invalid("memory_revision_id must reference a memory revision")
        _digest(self.candidate_sha256, "candidate_sha256")
        if self.memory_kind not in _MEMORY_KINDS:
            _invalid("memory_kind is not supported")
        if self.memory_type not in _MEMORY_TYPES:
            _invalid("memory_type is not supported")
        if self.classification not in _CLASSIFICATIONS:
            _invalid("classification is not supported")
        _attributes(self.scope_attributes)
        if len(self.scope_attributes) > MANAGED_INDEX_MAX_SCOPE_ATTRIBUTES:
            _invalid("scope_attributes exceeds the supported bound")
        if type(self.eval_leaking) is not bool:
            _invalid("eval_leaking must be a boolean")
        _tokens(self.lexical_tokens)
        if self.semantic_vector is not None:
            _vector(
                self.semantic_vector,
                "semantic_vector",
                allow_empty=False,
            )
        _identifiers(self.evidence_ids, "evidence_ids")
        if len(self.evidence_ids) > MANAGED_INDEX_MAX_EVIDENCE_IDS:
            _invalid("evidence_ids exceeds the supported bound")
        if self.git_anchor_commit_sha is not None:
            _metadata(
                self.git_anchor_commit_sha,
                "git_anchor_commit_sha",
            )
        if self.memory_kind == "lesson":
            if not self.evidence_ids or self.git_anchor_commit_sha is None:
                _invalid("lesson index candidates require evidence and a Git anchor")
        elif self.git_anchor_commit_sha is not None:
            _invalid("project policy index candidates cannot have a Git anchor")
        if self.classification in {"confidential", "restricted"} and (
            self.lexical_tokens or self.semantic_vector is not None
        ):
            _invalid(
                "sensitive candidates cannot contain lexical or semantic index data"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "memory_revision_id": self.memory_revision_id,
            "candidate_sha256": self.candidate_sha256,
            "memory_kind": self.memory_kind,
            "memory_type": self.memory_type,
            "classification": self.classification,
            "scope_attributes": dict(self.scope_attributes),
            "eval_leaking": self.eval_leaking,
            "lexical_tokens": list(self.lexical_tokens),
            "semantic_vector": (
                None if self.semantic_vector is None else list(self.semantic_vector)
            ),
            "evidence_ids": list(self.evidence_ids),
            "git_anchor_commit_sha": self.git_anchor_commit_sha,
        }


@dataclass(frozen=True)
class ManagedIndexBuildInput:
    tenant_id: str
    repository_id: str
    environment_id: str
    retriever_id: str
    retriever_version: str
    sources: tuple[ManagedIndexSource, ...] = field(repr=False)
    semantic_provider_id: str
    semantic_provider_version: str
    evidence_edges: tuple[ManagedEvidenceEdge, ...] = ()
    git_commits: tuple[str, ...] = ()
    git_edges: tuple[ManagedGitEdge, ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.environment_id, "environment_id"),
            (self.retriever_id, "retriever_id"),
            (self.retriever_version, "retriever_version"),
            (self.semantic_provider_id, "semantic_provider_id"),
            (
                self.semantic_provider_version,
                "semantic_provider_version",
            ),
        ):
            _identifier(value, name)
        if (
            type(self.sources) is not tuple
            or len(self.sources) > MANAGED_INDEX_MAX_CANDIDATES
            or any(type(item) is not ManagedIndexSource for item in self.sources)
        ):
            _invalid("sources must be a bounded tuple of managed index sources")
        if (
            type(self.evidence_edges) is not tuple
            or len(self.evidence_edges) > MANAGED_INDEX_MAX_EVIDENCE_EDGES
            or any(
                type(item) is not ManagedEvidenceEdge for item in self.evidence_edges
            )
        ):
            _invalid("evidence_edges must be a bounded tuple")
        if (
            type(self.git_edges) is not tuple
            or len(self.git_edges) > MANAGED_INDEX_MAX_GIT_EDGES
            or any(type(item) is not ManagedGitEdge for item in self.git_edges)
        ):
            _invalid("git_edges must be a bounded tuple")
        _git_commits(self.git_commits)


@dataclass(frozen=True)
class ManagedIndexBundle:
    bundle_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    retriever_id: str
    retriever_version: str
    tokenizer_id: str
    tokenizer_version: str
    semantic_provider_id: str
    semantic_provider_version: str
    semantic_metric: Literal["cosine"]
    semantic_dimension: int
    source_catalog_sha256: str
    index_versions: tuple[IndexVersion, ...]
    candidates: tuple[ManagedIndexCandidate, ...]
    evidence_edges: tuple[ManagedEvidenceEdge, ...]
    git_commits: tuple[str, ...]
    git_edges: tuple[ManagedGitEdge, ...]
    contract_version: str = MANAGED_INDEX_BUNDLE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != MANAGED_INDEX_BUNDLE_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if (
            type(self.bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.bundle_id) is None
        ):
            _invalid("bundle_id must be a managed index bundle ID")
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.environment_id, "environment_id"),
            (self.retriever_id, "retriever_id"),
            (self.retriever_version, "retriever_version"),
            (self.tokenizer_id, "tokenizer_id"),
            (self.tokenizer_version, "tokenizer_version"),
            (self.semantic_provider_id, "semantic_provider_id"),
            (
                self.semantic_provider_version,
                "semantic_provider_version",
            ),
        ):
            _identifier(value, name)
        if self.semantic_metric != "cosine":
            _invalid("semantic_metric must be cosine")
        if (
            type(self.semantic_dimension) is not int
            or self.semantic_dimension < 0
            or self.semantic_dimension > MANAGED_INDEX_MAX_SEMANTIC_DIMENSIONS
        ):
            _invalid("semantic_dimension is outside the supported bound")
        _digest(self.source_catalog_sha256, "source_catalog_sha256")
        if (
            type(self.candidates) is not tuple
            or len(self.candidates) > MANAGED_INDEX_MAX_CANDIDATES
            or any(type(item) is not ManagedIndexCandidate for item in self.candidates)
        ):
            _invalid("candidates must be a bounded tuple")
        _canonical_candidates(self.candidates)
        dimensions = {
            len(cast(tuple[float, ...], item.semantic_vector))
            for item in self.candidates
            if item.semantic_vector is not None
        }
        expected_dimension = next(iter(dimensions), 0)
        if len(dimensions) > 1 or expected_dimension != self.semantic_dimension:
            _invalid("semantic vectors must use one declared dimension")
        if (
            type(self.evidence_edges) is not tuple
            or len(self.evidence_edges) > MANAGED_INDEX_MAX_EVIDENCE_EDGES
            or any(
                type(item) is not ManagedEvidenceEdge for item in self.evidence_edges
            )
        ):
            _invalid("evidence_edges must be a bounded tuple")
        if self.evidence_edges != tuple(
            sorted(
                self.evidence_edges,
                key=lambda item: (item.query_token, item.evidence_id),
            )
        ) or len(
            {(item.query_token, item.evidence_id) for item in self.evidence_edges}
        ) != len(self.evidence_edges):
            _invalid("evidence_edges must be unique and sorted")
        known_evidence = {
            evidence_id
            for candidate in self.candidates
            for evidence_id in candidate.evidence_ids
        }
        if any(edge.evidence_id not in known_evidence for edge in self.evidence_edges):
            _invalid("evidence edge references unknown evidence")
        sensitive_evidence = {
            evidence_id
            for candidate in self.candidates
            if candidate.classification in {"confidential", "restricted"}
            for evidence_id in candidate.evidence_ids
        }
        if any(edge.evidence_id in sensitive_evidence for edge in self.evidence_edges):
            _invalid(
                "sensitive candidates cannot enter content-derived evidence indexes"
            )
        _git_commits(self.git_commits)
        if (
            type(self.git_edges) is not tuple
            or len(self.git_edges) > MANAGED_INDEX_MAX_GIT_EDGES
            or any(type(item) is not ManagedGitEdge for item in self.git_edges)
        ):
            _invalid("git_edges must be a bounded tuple")
        if self.git_edges != tuple(
            sorted(
                self.git_edges,
                key=lambda item: (
                    item.child_commit_sha,
                    item.parent_commit_sha,
                ),
            )
        ) or len(
            {(item.child_commit_sha, item.parent_commit_sha) for item in self.git_edges}
        ) != len(self.git_edges):
            _invalid("git_edges must be unique and sorted")
        commit_set = set(self.git_commits)
        if any(
            edge.child_commit_sha not in commit_set
            or edge.parent_commit_sha not in commit_set
            for edge in self.git_edges
        ):
            _invalid("git edge references an unknown commit")
        if any(
            candidate.git_anchor_commit_sha is not None
            and candidate.git_anchor_commit_sha not in commit_set
            for candidate in self.candidates
        ):
            _invalid("candidate Git anchor is absent from the managed graph")
        _validate_acyclic_git_graph(self.git_commits, self.git_edges)
        expected_catalog = _source_catalog_sha256(self.candidates)
        if self.source_catalog_sha256 != expected_catalog:
            raise ManagedIndexV3ContractError(
                "TBM_MANAGED_INDEX_HASH_MISMATCH",
                "source catalog digest does not match candidates",
            )
        expected_versions = _index_versions(self)
        if self.index_versions != expected_versions:
            raise ManagedIndexV3ContractError(
                "TBM_MANAGED_INDEX_HASH_MISMATCH",
                "index versions do not match managed index content",
            )
        expected_id = managed_index_bundle_id(self._unsigned_dict())
        if self.bundle_id != expected_id:
            raise ManagedIndexV3ContractError(
                "TBM_MANAGED_INDEX_HASH_MISMATCH",
                "bundle_id does not match managed index content",
            )
        dumps_managed_index_bundle(self)

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "retriever_id": self.retriever_id,
            "retriever_version": self.retriever_version,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_version": self.tokenizer_version,
            "semantic_provider_id": self.semantic_provider_id,
            "semantic_provider_version": self.semantic_provider_version,
            "semantic_metric": self.semantic_metric,
            "semantic_dimension": self.semantic_dimension,
            "source_catalog_sha256": self.source_catalog_sha256,
            "index_versions": [item.to_dict() for item in self.index_versions],
            "candidates": [item.to_dict() for item in self.candidates],
            "evidence_edges": [item.to_dict() for item in self.evidence_edges],
            "git_commits": list(self.git_commits),
            "git_edges": [item.to_dict() for item in self.git_edges],
        }

    def to_dict(self) -> dict[str, object]:
        return {"bundle_id": self.bundle_id, **self._unsigned_dict()}

    def index_version(self, kind: IndexKind) -> IndexVersion:
        for version in self.index_versions:
            if version.index_kind == kind:
                return version
        raise AssertionError("validated bundle is missing an index kind")


@dataclass(frozen=True)
class ManagedIndexPublication:
    bundle: ManagedIndexBundle
    previous_bundle_id: str | None
    head_version: int
    changed: bool

    def __post_init__(self) -> None:
        if type(self.bundle) is not ManagedIndexBundle:
            _invalid("publication bundle is invalid")
        if self.previous_bundle_id is not None and (
            type(self.previous_bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.previous_bundle_id) is None
        ):
            _invalid("previous_bundle_id is invalid")
        if type(self.head_version) is not int or self.head_version < 1:
            _invalid("head_version must be a positive integer")
        if type(self.changed) is not bool:
            _invalid("changed must be a boolean")


class ManagedIndexRepository(Protocol):
    def publish(
        self,
        bundle: ManagedIndexBundle,
        *,
        expected_current_bundle_id: str | None,
    ) -> ManagedIndexPublication: ...

    def load(self, bundle_id: str) -> ManagedIndexBundle: ...

    def load_current(
        self,
        *,
        tenant_id: str,
        repository_id: str,
        environment_id: str,
    ) -> ManagedIndexBundle: ...


class ManagedIndexDiscovery:
    """Managed, immutable five-index adapter for CandidateDiscovery."""

    def __init__(self, repository: ManagedIndexRepository) -> None:
        if not all(
            callable(getattr(repository, name, None))
            for name in ("publish", "load", "load_current")
        ):
            raise TypeError("repository must satisfy ManagedIndexRepository")
        self._repository = repository

    def discover(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        request: RetrievalPreparationRequest,
        policy: RetrievalPolicyBundle,
    ) -> CandidateDiscoveryResult:
        if type(context) is not AuthenticatedServiceContext:
            _query_invalid("authenticated context is invalid")
        if type(scope) is not AuthorizedRetrievalScope:
            _query_invalid("authorized scope is invalid")
        if type(request) is not RetrievalPreparationRequest:
            _query_invalid("retrieval request is invalid")
        if type(policy) is not RetrievalPolicyBundle:
            _query_invalid("retrieval policy is invalid")
        if (
            type(scope.authorization_event_id) is not str
            or not _AUTHORIZATION_ID_RE.fullmatch(scope.authorization_event_id)
            or scope.principal_id != context.principal.principal_id
            or scope.agent_client_id != context.agent_client.agent_client_id
            or context.tenant_id != scope.tenant_id
            or context.environment_id != scope.environment_id
            or request.context.tenant_id != scope.tenant_id
            or request.context.repository_id != scope.repository_id
            or request.context.environment_id != scope.environment_id
        ):
            _query_reject(
                "TBM_MANAGED_INDEX_SCOPE_MISMATCH",
                "managed index query is outside the authorized scope",
            )
        try:
            bundle = self._repository.load_current(
                tenant_id=scope.tenant_id,
                repository_id=scope.repository_id,
                environment_id=scope.environment_id,
            )
        except ManagedIndexV3Error:
            raise
        except Exception as error:
            raise ManagedIndexV3Error(
                "TBM_MANAGED_INDEX_UNAVAILABLE",
                "managed index bundle could not be loaded",
            ) from error
        if type(bundle) is not ManagedIndexBundle:
            _query_reject(
                "TBM_MANAGED_INDEX_INVALID",
                "managed index repository returned invalid data",
            )
        if (
            bundle.tenant_id != scope.tenant_id
            or bundle.repository_id != scope.repository_id
            or bundle.environment_id != scope.environment_id
            or bundle.retriever_id != request.retriever_id
            or bundle.retriever_version != request.retriever_version
        ):
            _query_reject(
                "TBM_MANAGED_INDEX_SCOPE_MISMATCH",
                "managed index bundle does not match the authorized request",
            )
        context_attributes = dict(request.context.attributes)
        eligible = tuple(
            candidate
            for candidate in bundle.candidates
            if all(
                context_attributes.get(key) == value
                for key, value in candidate.scope_attributes
            )
        )
        query_tokens: tuple[str, ...] = ()
        if request.query is not None:
            try:
                query_text = decode_bounded_utf8(
                    request.query,
                    max_bytes=MANAGED_INDEX_MAX_TEXT_BYTES,
                    description="managed index query",
                )
            except (UnicodeError, ValueError) as error:
                raise ManagedIndexV3Error(
                    "TBM_MANAGED_INDEX_QUERY_INVALID",
                    "managed index query must be bounded UTF-8",
                ) from error
            query_tokens = _tokenize(query_text)
        semantic_query = request.semantic_query
        semantic_enabled = semantic_query is not None and request.retrieval_mode in {
            "semantic",
            "hybrid",
        }
        if semantic_enabled:
            self._verify_semantic_query(
                bundle, cast(SemanticQueryVector, semantic_query)
            )
        evidence_weights = {
            (edge.query_token, edge.evidence_id): edge.weight
            for edge in bundle.evidence_edges
        }
        records: list[CandidateIndexRecord] = []
        for candidate in eligible:
            lexical_score = self._lexical_score(
                query_tokens,
                candidate.lexical_tokens,
            )
            evidence_score = self._evidence_score(
                query_tokens,
                candidate.evidence_ids,
                evidence_weights,
            )
            semantic_score = (
                self._semantic_score(
                    bundle,
                    cast(SemanticQueryVector, semantic_query),
                    candidate.semantic_vector,
                )
                if semantic_enabled and candidate.semantic_vector is not None
                else None
            )
            mode = request.retrieval_mode
            if mode == "metadata":
                record = CandidateIndexRecord(
                    memory_id=candidate.memory_id,
                    candidate_sha256=candidate.candidate_sha256,
                )
            elif mode == "lexical":
                if lexical_score is None:
                    continue
                record = CandidateIndexRecord(
                    memory_id=candidate.memory_id,
                    candidate_sha256=candidate.candidate_sha256,
                    lexical_score=lexical_score,
                )
            elif mode == "semantic":
                if semantic_score is None:
                    continue
                record = CandidateIndexRecord(
                    memory_id=candidate.memory_id,
                    candidate_sha256=candidate.candidate_sha256,
                    semantic_score=semantic_score,
                )
            elif mode == "evidence_graph":
                if evidence_score is None:
                    continue
                record = CandidateIndexRecord(
                    memory_id=candidate.memory_id,
                    candidate_sha256=candidate.candidate_sha256,
                    evidence_graph_score=evidence_score,
                )
            else:
                if (
                    lexical_score is None
                    and semantic_score is None
                    and evidence_score is None
                ):
                    continue
                record = CandidateIndexRecord(
                    memory_id=candidate.memory_id,
                    candidate_sha256=candidate.candidate_sha256,
                    lexical_score=lexical_score,
                    semantic_score=semantic_score,
                    evidence_graph_score=evidence_score,
                )
            records.append(record)
        if len(records) > MANAGED_INDEX_MAX_CANDIDATES:
            _query_reject(
                "TBM_MANAGED_INDEX_BOUNDS",
                "managed index query exceeds the complete candidate bound",
            )
        kinds: list[IndexKind] = ["metadata"]
        if request.retrieval_mode == "hybrid":
            kinds.extend(["lexical", "evidence_graph"])
            if semantic_enabled:
                kinds.append("semantic")
        elif request.retrieval_mode != "metadata":
            kinds.append(cast(IndexKind, request.retrieval_mode))
        ancestry_relations: tuple[tuple[str, bool], ...] = ()
        if policy.ancestry_mode == "required":
            kinds.append("git_graph")
            anchors = tuple(
                sorted(
                    {
                        cast(str, candidate.git_anchor_commit_sha)
                        for candidate in eligible
                        if candidate.git_anchor_commit_sha is not None
                    }
                )
            )
            ancestors = self._ancestors(
                bundle,
                request.context.commit_sha,
            )
            ancestry_relations = tuple(
                (anchor, anchor in ancestors) for anchor in anchors
            )
        index_versions = tuple(
            bundle.index_version(kind)
            for kind in sorted(set(kinds), key=_INDEX_KIND_ORDER.__getitem__)
        )
        query_evidence_sha256 = (
            cast(SemanticQueryVector, semantic_query).evidence_sha256(
                cast(str, request.query_sha256)
            )
            if semantic_enabled and "semantic" in kinds
            else None
        )
        return CandidateDiscoveryResult(
            records=tuple(sorted(records, key=lambda item: item.memory_id)),
            index_versions=index_versions,
            ancestry_relations=ancestry_relations,
            query_evidence_sha256=query_evidence_sha256,
        )

    @staticmethod
    def _verify_semantic_query(
        bundle: ManagedIndexBundle,
        query: SemanticQueryVector,
    ) -> None:
        if (
            query.provider_id != bundle.semantic_provider_id
            or query.provider_version != bundle.semantic_provider_version
            or len(query.vector) != bundle.semantic_dimension
            or bundle.semantic_dimension == 0
        ):
            _query_reject(
                "TBM_MANAGED_INDEX_QUERY_UNAVAILABLE",
                "semantic query evidence does not match the managed index",
            )

    @staticmethod
    def _lexical_score(
        query_tokens: tuple[str, ...],
        candidate_tokens: tuple[str, ...],
    ) -> float | None:
        if not query_tokens or not candidate_tokens:
            return None
        overlap = len(set(query_tokens).intersection(candidate_tokens))
        if overlap == 0:
            return None
        return overlap / len(query_tokens)

    @staticmethod
    def _evidence_score(
        query_tokens: tuple[str, ...],
        evidence_ids: tuple[str, ...],
        weights: Mapping[tuple[str, str], float],
    ) -> float | None:
        if not query_tokens or not evidence_ids:
            return None
        per_token = [
            max(
                (
                    weights.get((token, evidence_id), 0.0)
                    for evidence_id in evidence_ids
                ),
                default=0.0,
            )
            for token in query_tokens
        ]
        result = math.fsum(per_token) / len(query_tokens)
        return result if result > 0.0 else None

    @staticmethod
    def _semantic_score(
        bundle: ManagedIndexBundle,
        query: SemanticQueryVector,
        candidate_vector: tuple[float, ...],
    ) -> float:
        del bundle
        normalized_query = _normalized_finite_vector(query.vector)
        normalized_candidate = _normalized_finite_vector(candidate_vector)
        cosine = math.fsum(
            left * right
            for left, right in zip(
                normalized_query,
                normalized_candidate,
                strict=True,
            )
        )
        return min(1.0, max(0.0, (cosine + 1.0) / 2.0))

    @staticmethod
    def _ancestors(
        bundle: ManagedIndexBundle,
        current_commit_sha: str,
    ) -> frozenset[str]:
        if current_commit_sha not in set(bundle.git_commits):
            _query_reject(
                "TBM_MANAGED_INDEX_QUERY_UNAVAILABLE",
                "current commit is absent from the managed Git graph",
            )
        parents: dict[str, list[str]] = {}
        for edge in bundle.git_edges:
            parents.setdefault(edge.child_commit_sha, []).append(edge.parent_commit_sha)
        found: set[str] = set()
        pending = [current_commit_sha]
        while pending:
            commit = pending.pop()
            if commit in found:
                continue
            found.add(commit)
            pending.extend(parents.get(commit, ()))
        return frozenset(found)


def build_managed_index_bundle(
    build_input: ManagedIndexBuildInput,
) -> ManagedIndexBundle:
    if type(build_input) is not ManagedIndexBuildInput:
        _invalid("build_input must be exactly ManagedIndexBuildInput")
    candidates: list[ManagedIndexCandidate] = []
    dimensions: set[int] = set()
    for source in sorted(
        build_input.sources,
        key=lambda item: item.candidate.revision.memory_id,
    ):
        candidate = source.candidate
        revision = candidate.revision
        if (
            revision.scope.kind != "repository"
            or revision.scope.tenant_id != build_input.tenant_id
            or revision.scope.repository_id != build_input.repository_id
        ):
            _invalid("managed index source is outside the bundle scope")
        if revision.memory_kind == "lesson" and (
            candidate.fix_evidence is None or not candidate.regression_evidence
        ):
            _invalid("lesson managed index source lacks structured evidence")
        lexical_tokens = (
            () if source.index_text is None else _tokenize(source.index_text)
        )
        if len(lexical_tokens) > MANAGED_INDEX_MAX_TOKENS_PER_CANDIDATE:
            _invalid("managed index source contains too many lexical tokens")
        semantic_vector = (
            None
            if source.semantic_vector is None
            else _normalize_vector(source.semantic_vector)
        )
        if semantic_vector is not None:
            dimensions.add(len(semantic_vector))
        evidence_ids = tuple(
            sorted(
                {
                    *revision.regression_evidence_ids,
                    *(
                        (revision.fix_evidence_id,)
                        if revision.fix_evidence_id is not None
                        else ()
                    ),
                }
            )
        )
        candidates.append(
            ManagedIndexCandidate(
                memory_id=revision.memory_id,
                memory_revision_id=revision.revision_id,
                candidate_sha256=candidate.candidate_sha256,
                memory_kind=revision.memory_kind,
                memory_type=revision.memory_type,
                classification=revision.content_artifact.classification,
                scope_attributes=tuple(sorted(revision.scope.attributes)),
                eval_leaking=revision.eval_leaking,
                lexical_tokens=lexical_tokens,
                semantic_vector=semantic_vector,
                evidence_ids=evidence_ids,
                git_anchor_commit_sha=(
                    candidate.fix_evidence.fix_commit_sha
                    if candidate.fix_evidence is not None
                    else None
                ),
            )
        )
    if len(dimensions) > 1:
        _invalid("managed semantic vectors must use one dimension")
    canonical_candidates = tuple(candidates)
    _canonical_candidates(canonical_candidates)
    evidence_edges = tuple(
        sorted(
            build_input.evidence_edges,
            key=lambda item: (item.query_token, item.evidence_id),
        )
    )
    if len({(item.query_token, item.evidence_id) for item in evidence_edges}) != len(
        evidence_edges
    ):
        _invalid("managed evidence edges must be unique")
    git_commits = tuple(sorted(build_input.git_commits))
    git_edges = tuple(
        sorted(
            build_input.git_edges,
            key=lambda item: (
                item.child_commit_sha,
                item.parent_commit_sha,
            ),
        )
    )
    if len(
        {(item.child_commit_sha, item.parent_commit_sha) for item in git_edges}
    ) != len(git_edges):
        _invalid("managed Git edges must be unique")
    unsigned_without_versions: dict[str, object] = {
        "contract_version": MANAGED_INDEX_BUNDLE_CONTRACT_VERSION,
        "tenant_id": build_input.tenant_id,
        "repository_id": build_input.repository_id,
        "environment_id": build_input.environment_id,
        "retriever_id": build_input.retriever_id,
        "retriever_version": build_input.retriever_version,
        "tokenizer_id": "tbm.unicode-token-bigram",
        "tokenizer_version": "v1",
        "semantic_provider_id": build_input.semantic_provider_id,
        "semantic_provider_version": build_input.semantic_provider_version,
        "semantic_metric": "cosine",
        "semantic_dimension": next(iter(dimensions), 0),
        "source_catalog_sha256": _source_catalog_sha256(canonical_candidates),
        "candidates": [item.to_dict() for item in canonical_candidates],
        "evidence_edges": [item.to_dict() for item in evidence_edges],
        "git_commits": list(git_commits),
        "git_edges": [item.to_dict() for item in git_edges],
    }
    provisional = ManagedIndexBundle.__new__(ManagedIndexBundle)
    for key, value in unsigned_without_versions.items():
        object.__setattr__(provisional, key, value)
    object.__setattr__(provisional, "candidates", canonical_candidates)
    object.__setattr__(provisional, "evidence_edges", evidence_edges)
    object.__setattr__(provisional, "git_commits", git_commits)
    object.__setattr__(provisional, "git_edges", git_edges)
    object.__setattr__(
        provisional,
        "index_versions",
        (),
    )
    versions = _index_versions(cast(ManagedIndexBundle, provisional))
    unsigned = {
        **unsigned_without_versions,
        "index_versions": [item.to_dict() for item in versions],
    }
    return ManagedIndexBundle(
        bundle_id=managed_index_bundle_id(unsigned),
        tenant_id=build_input.tenant_id,
        repository_id=build_input.repository_id,
        environment_id=build_input.environment_id,
        retriever_id=build_input.retriever_id,
        retriever_version=build_input.retriever_version,
        tokenizer_id="tbm.unicode-token-bigram",
        tokenizer_version="v1",
        semantic_provider_id=build_input.semantic_provider_id,
        semantic_provider_version=build_input.semantic_provider_version,
        semantic_metric="cosine",
        semantic_dimension=next(iter(dimensions), 0),
        source_catalog_sha256=cast(
            str,
            unsigned_without_versions["source_catalog_sha256"],
        ),
        index_versions=versions,
        candidates=canonical_candidates,
        evidence_edges=evidence_edges,
        git_commits=git_commits,
        git_edges=git_edges,
    )


def managed_index_bundle_id(payload: Mapping[str, object]) -> str:
    return "managed_index_bundle_sha256_" + canonical_sha256(payload).removeprefix(
        "sha256:"
    )


def dumps_managed_index_bundle(bundle: ManagedIndexBundle) -> str:
    if type(bundle) is not ManagedIndexBundle:
        _invalid("bundle must be exactly ManagedIndexBundle")
    payload = json.dumps(
        bundle.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("utf-8")) > MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES:
        _invalid("managed index bundle exceeds maximum bytes")
    return payload


def loads_managed_index_bundle(
    source: str | bytes | bytearray,
) -> ManagedIndexBundle:
    try:
        if type(source) is str:
            encoded = source.encode("utf-8")
        elif type(source) in {bytes, bytearray}:
            encoded = bytes(source)
        else:
            _invalid("managed index source must be text or bytes")
        text = decode_bounded_utf8(
            encoded,
            max_bytes=MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES,
            description="managed index bundle",
        )
        item = parse_bounded_json(
            text,
            description="managed index bundle",
            max_nodes=MANAGED_INDEX_BUNDLE_JSON_MAX_NODES,
            max_depth=MANAGED_INDEX_BUNDLE_JSON_MAX_DEPTH,
        )
    except ManagedIndexV3ContractError:
        raise
    except (UnicodeError, ValueError) as error:
        raise ManagedIndexV3ContractError(
            "TBM_MANAGED_INDEX_INVALID",
            "managed index bundle JSON is invalid",
        ) from error
    if type(item) is not dict or frozenset(item) != _BUNDLE_FIELDS:
        _invalid("managed index bundle has invalid fields")
    candidates_value = item["candidates"]
    evidence_edges_value = item["evidence_edges"]
    git_commits_value = item["git_commits"]
    git_edges_value = item["git_edges"]
    index_versions_value = item["index_versions"]
    if not all(
        type(value) is list
        for value in (
            candidates_value,
            evidence_edges_value,
            git_commits_value,
            git_edges_value,
            index_versions_value,
        )
    ):
        _invalid("managed index bundle collections must be arrays")
    if len(cast(list[object], candidates_value)) > MANAGED_INDEX_MAX_CANDIDATES:
        _invalid("managed index bundle contains too many candidates")
    candidates = tuple(
        _candidate_from_dict(value) for value in cast(list[object], candidates_value)
    )
    evidence_edges = tuple(
        _evidence_edge_from_dict(value)
        for value in cast(list[object], evidence_edges_value)
    )
    git_edges = tuple(
        _git_edge_from_dict(value) for value in cast(list[object], git_edges_value)
    )
    index_versions = tuple(
        _index_version_from_dict(value)
        for value in cast(list[object], index_versions_value)
    )
    return ManagedIndexBundle(
        bundle_id=cast(str, item["bundle_id"]),
        tenant_id=cast(str, item["tenant_id"]),
        repository_id=cast(str, item["repository_id"]),
        environment_id=cast(str, item["environment_id"]),
        retriever_id=cast(str, item["retriever_id"]),
        retriever_version=cast(str, item["retriever_version"]),
        tokenizer_id=cast(str, item["tokenizer_id"]),
        tokenizer_version=cast(str, item["tokenizer_version"]),
        semantic_provider_id=cast(str, item["semantic_provider_id"]),
        semantic_provider_version=cast(
            str,
            item["semantic_provider_version"],
        ),
        semantic_metric=cast(Literal["cosine"], item["semantic_metric"]),
        semantic_dimension=cast(int, item["semantic_dimension"]),
        source_catalog_sha256=cast(str, item["source_catalog_sha256"]),
        index_versions=index_versions,
        candidates=candidates,
        evidence_edges=evidence_edges,
        git_commits=tuple(cast(list[str], git_commits_value)),
        git_edges=git_edges,
        contract_version=cast(str, item["contract_version"]),
    )


def _candidate_from_dict(value: object) -> ManagedIndexCandidate:
    if type(value) is not dict or frozenset(value) != _CANDIDATE_FIELDS:
        _invalid("managed index candidate has invalid fields")
    item = cast(dict[str, object], value)
    attributes = item["scope_attributes"]
    lexical_tokens = item["lexical_tokens"]
    semantic_vector = item["semantic_vector"]
    evidence_ids = item["evidence_ids"]
    if (
        type(attributes) is not dict
        or type(lexical_tokens) is not list
        or type(evidence_ids) is not list
        or (semantic_vector is not None and type(semantic_vector) is not list)
    ):
        _invalid("managed index candidate collections are invalid")
    return ManagedIndexCandidate(
        memory_id=cast(str, item["memory_id"]),
        memory_revision_id=cast(str, item["memory_revision_id"]),
        candidate_sha256=cast(str, item["candidate_sha256"]),
        memory_kind=cast(
            Literal["lesson", "project_policy"],
            item["memory_kind"],
        ),
        memory_type=cast(
            Literal["procedural", "semantic", "episodic", "policy"],
            item["memory_type"],
        ),
        classification=cast(
            Literal["public", "internal", "confidential", "restricted"],
            item["classification"],
        ),
        scope_attributes=tuple(
            (cast(str, key), cast(str, attribute_value))
            for key, attribute_value in cast(
                dict[object, object],
                attributes,
            ).items()
        ),
        eval_leaking=cast(bool, item["eval_leaking"]),
        lexical_tokens=tuple(cast(list[str], lexical_tokens)),
        semantic_vector=(
            None
            if semantic_vector is None
            else tuple(cast(list[float], semantic_vector))
        ),
        evidence_ids=tuple(cast(list[str], evidence_ids)),
        git_anchor_commit_sha=cast(
            str | None,
            item["git_anchor_commit_sha"],
        ),
    )


def _evidence_edge_from_dict(value: object) -> ManagedEvidenceEdge:
    if type(value) is not dict or frozenset(value) != _EVIDENCE_EDGE_FIELDS:
        _invalid("managed evidence edge has invalid fields")
    item = cast(dict[str, object], value)
    return ManagedEvidenceEdge(
        query_token=cast(str, item["query_token"]),
        evidence_id=cast(str, item["evidence_id"]),
        weight=cast(float, item["weight"]),
    )


def _git_edge_from_dict(value: object) -> ManagedGitEdge:
    if type(value) is not dict or frozenset(value) != _GIT_EDGE_FIELDS:
        _invalid("managed Git edge has invalid fields")
    item = cast(dict[str, object], value)
    return ManagedGitEdge(
        child_commit_sha=cast(str, item["child_commit_sha"]),
        parent_commit_sha=cast(str, item["parent_commit_sha"]),
    )


def _index_version_from_dict(value: object) -> IndexVersion:
    if type(value) is not dict or frozenset(value) != _INDEX_VERSION_FIELDS:
        _invalid("managed index version has invalid fields")
    item = cast(dict[str, object], value)
    return IndexVersion(
        index_kind=cast(IndexKind, item["index_kind"]),
        index_id=cast(str, item["index_id"]),
        index_version=cast(str, item["index_version"]),
        content_sha256=cast(str, item["content_sha256"]),
    )


def _index_versions(bundle: ManagedIndexBundle) -> tuple[IndexVersion, ...]:
    scope_sha = canonical_sha256(
        {
            "tenant_id": bundle.tenant_id,
            "repository_id": bundle.repository_id,
            "environment_id": bundle.environment_id,
        }
    ).removeprefix("sha256:")
    common = {
        "contract_version": bundle.contract_version,
        "tenant_id": bundle.tenant_id,
        "repository_id": bundle.repository_id,
        "environment_id": bundle.environment_id,
        "retriever_id": bundle.retriever_id,
        "retriever_version": bundle.retriever_version,
        "source_catalog_sha256": bundle.source_catalog_sha256,
    }
    candidates = bundle.candidates
    content_by_kind: dict[IndexKind, object] = {
        "metadata": {
            **common,
            "candidates": [
                {
                    "memory_id": item.memory_id,
                    "memory_revision_id": item.memory_revision_id,
                    "candidate_sha256": item.candidate_sha256,
                    "memory_kind": item.memory_kind,
                    "memory_type": item.memory_type,
                    "classification": item.classification,
                    "scope_attributes": dict(item.scope_attributes),
                    "eval_leaking": item.eval_leaking,
                }
                for item in candidates
            ],
        },
        "lexical": {
            **common,
            "tokenizer_id": bundle.tokenizer_id,
            "tokenizer_version": bundle.tokenizer_version,
            "candidates": [
                {
                    "candidate_sha256": item.candidate_sha256,
                    "lexical_tokens": list(item.lexical_tokens),
                }
                for item in candidates
            ],
        },
        "semantic": {
            **common,
            "provider_id": bundle.semantic_provider_id,
            "provider_version": bundle.semantic_provider_version,
            "metric": bundle.semantic_metric,
            "dimension": bundle.semantic_dimension,
            "candidates": [
                {
                    "candidate_sha256": item.candidate_sha256,
                    "semantic_vector": (
                        None
                        if item.semantic_vector is None
                        else list(item.semantic_vector)
                    ),
                }
                for item in candidates
            ],
        },
        "evidence_graph": {
            **common,
            "candidate_evidence": [
                {
                    "candidate_sha256": item.candidate_sha256,
                    "evidence_ids": list(item.evidence_ids),
                }
                for item in candidates
            ],
            "edges": [item.to_dict() for item in bundle.evidence_edges],
        },
        "git_graph": {
            **common,
            "candidate_anchors": [
                {
                    "candidate_sha256": item.candidate_sha256,
                    "git_anchor_commit_sha": item.git_anchor_commit_sha,
                }
                for item in candidates
            ],
            "commits": list(bundle.git_commits),
            "edges": [item.to_dict() for item in bundle.git_edges],
        },
    }
    versions: list[IndexVersion] = []
    for kind in _INDEX_KINDS:
        digest = canonical_sha256(content_by_kind[kind])
        versions.append(
            IndexVersion(
                index_kind=kind,
                index_id=f"managed_{kind}_{scope_sha[:24]}",
                index_version=f"v3_{digest.removeprefix('sha256:')}",
                content_sha256=digest,
            )
        )
    return tuple(versions)


def _source_catalog_sha256(
    candidates: tuple[ManagedIndexCandidate, ...],
) -> str:
    return canonical_sha256(
        [
            {
                "memory_id": item.memory_id,
                "memory_revision_id": item.memory_revision_id,
                "candidate_sha256": item.candidate_sha256,
            }
            for item in candidates
        ]
    )


def _canonical_candidates(
    candidates: tuple[ManagedIndexCandidate, ...],
) -> None:
    if candidates != tuple(sorted(candidates, key=lambda item: item.memory_id)):
        _invalid("managed index candidates must be sorted by memory_id")
    if len({item.memory_id for item in candidates}) != len(candidates):
        _invalid("managed index candidate memory IDs must be unique")
    if len({item.memory_revision_id for item in candidates}) != len(candidates):
        _invalid("managed index revision IDs must be unique")
    if len({item.candidate_sha256 for item in candidates}) != len(candidates):
        _invalid("managed index candidate hashes must be unique")


def _validate_acyclic_git_graph(
    commits: tuple[str, ...],
    edges: tuple[ManagedGitEdge, ...],
) -> None:
    parents: dict[str, list[str]] = {}
    for edge in edges:
        parents.setdefault(edge.child_commit_sha, []).append(edge.parent_commit_sha)
    colors: dict[str, int] = {}
    for start in commits:
        if colors.get(start) == 2:
            continue
        pending: list[tuple[str, bool]] = [(start, False)]
        while pending:
            node, exiting = pending.pop()
            if exiting:
                colors[node] = 2
                continue
            color = colors.get(node, 0)
            if color == 1:
                _invalid("managed Git graph must be acyclic")
            if color == 2:
                continue
            colors[node] = 1
            pending.append((node, True))
            for parent in parents.get(node, ()):
                parent_color = colors.get(parent, 0)
                if parent_color == 1:
                    _invalid("managed Git graph must be acyclic")
                if parent_color != 2:
                    pending.append((parent, False))


def _normalize_vector(values: tuple[float, ...]) -> tuple[float, ...]:
    vector = _vector(values, "semantic_vector", allow_empty=False)
    return _normalized_finite_vector(vector)


def _normalized_finite_vector(
    values: tuple[float, ...],
) -> tuple[float, ...]:
    scale = max(abs(value) for value in values)
    scaled = tuple(value / scale for value in values)
    magnitude = math.hypot(*scaled)
    return tuple(value / magnitude for value in scaled)


def _vector(
    values: object,
    name: str,
    *,
    allow_empty: bool,
) -> tuple[float, ...]:
    if (
        type(values) is not tuple
        or (not allow_empty and not values)
        or len(cast(tuple[object, ...], values)) > MANAGED_INDEX_MAX_SEMANTIC_DIMENSIONS
    ):
        _invalid(f"{name} must be a bounded tuple")
    result: list[float] = []
    has_nonzero = False
    for value in cast(tuple[object, ...], values):
        if type(value) not in {int, float}:
            _invalid(f"{name} must contain finite numbers")
        try:
            item = float(value)
        except (OverflowError, ValueError):
            _invalid(f"{name} must contain finite numbers")
        if not math.isfinite(item):
            _invalid(f"{name} must contain finite numbers")
        result.append(item)
        has_nonzero = has_nonzero or item != 0.0
    if result and not has_nonzero:
        _invalid(f"{name} must contain a non-zero value")
    return tuple(result)


def _tokenize(value: str) -> tuple[str, ...]:
    tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(value.casefold()):
        token = match.group(0)
        if len(token) >= 2:
            tokens.add(token)
        if any(ord(character) > 127 for character in token):
            tokens.update(
                token[index : index + 2] for index in range(max(0, len(token) - 1))
            )
    return tuple(sorted(tokens))


def _single_token(value: object) -> str:
    _bounded_text(
        value,
        "query_token",
        maximum_bytes=_METADATA_MAX_CHARS,
    )
    tokens = _tokenize(cast(str, value))
    normalized = cast(str, value).casefold()
    if normalized not in tokens:
        _invalid("query_token must be one canonical retrieval token")
    return normalized


def _tokens(values: tuple[str, ...]) -> None:
    if (
        type(values) is not tuple
        or len(values) > MANAGED_INDEX_MAX_TOKENS_PER_CANDIDATE
        or any(type(item) is not str for item in values)
        or values != tuple(sorted(set(values)))
    ):
        _invalid("lexical_tokens must be a unique sorted bounded tuple")
    for value in values:
        if _single_token(value) != value:
            _invalid("lexical_tokens must contain canonical tokens")


def _git_commits(values: tuple[str, ...]) -> None:
    if (
        type(values) is not tuple
        or len(values) > MANAGED_INDEX_MAX_GIT_COMMITS
        or any(type(item) is not str for item in values)
        or values != tuple(sorted(set(values)))
    ):
        _invalid("git_commits must be a unique sorted bounded tuple")
    for value in values:
        _metadata(value, "git_commit_sha")


def _attributes(values: tuple[tuple[str, str], ...]) -> None:
    if type(values) is not tuple:
        _invalid("scope_attributes must be a tuple")
    previous: str | None = None
    for item in values:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            _invalid("scope_attributes contains an invalid item")
        _identifier(item[0], "scope attribute key")
        _metadata(item[1], "scope attribute value")
        if previous is not None and item[0] <= previous:
            _invalid("scope_attributes must be unique and sorted")
        previous = item[0]


def _identifiers(values: tuple[str, ...], name: str) -> None:
    if (
        type(values) is not tuple
        or values != tuple(sorted(set(values)))
        or any(type(item) is not str for item in values)
    ):
        _invalid(f"{name} must be a unique sorted tuple")
    for value in values:
        _identifier(value, name)


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _IDENTIFIER_MAX_CHARS
    ):
        _invalid(f"{name} must be a bounded identifier")
    _utf8(cast(str, value), name)


def _metadata(value: object, name: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > _METADATA_MAX_CHARS:
        _invalid(f"{name} must be bounded non-empty text")
    _utf8(cast(str, value), name)


def _bounded_text(
    value: object,
    name: str,
    *,
    maximum_bytes: int,
) -> None:
    if type(value) is not str:
        _invalid(f"{name} must be text")
    try:
        encoded = cast(str, value).encode("utf-8")
    except UnicodeError as error:
        raise ManagedIndexV3ContractError(
            "TBM_MANAGED_INDEX_INVALID",
            f"{name} must be valid UTF-8",
        ) from error
    if not encoded or len(encoded) > maximum_bytes:
        _invalid(f"{name} must be bounded non-empty text")


def _utf8(value: str, name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ManagedIndexV3ContractError(
            "TBM_MANAGED_INDEX_INVALID",
            f"{name} must be valid UTF-8",
        ) from error


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a SHA-256 digest")


def _unit_score(value: object, name: str) -> float:
    if type(value) not in {int, float}:
        _invalid(f"{name} must be a finite score")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        _invalid(f"{name} must be a finite score")
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        _invalid(f"{name} must be between zero and one")
    return result


def _invalid(message: str) -> NoReturn:
    raise ManagedIndexV3ContractError(
        "TBM_MANAGED_INDEX_INVALID",
        message,
    )


def _query_invalid(message: str) -> NoReturn:
    raise ManagedIndexV3Error(
        "TBM_MANAGED_INDEX_QUERY_INVALID",
        message,
    )


def _query_reject(code: str, message: str) -> NoReturn:
    raise ManagedIndexV3Error(code, message)


__all__ = [
    "MANAGED_INDEX_BUNDLE_CONTRACT_VERSION",
    "MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES",
    "MANAGED_INDEX_BUNDLE_JSON_MAX_DEPTH",
    "MANAGED_INDEX_BUNDLE_JSON_MAX_NODES",
    "MANAGED_INDEX_MAX_CANDIDATES",
    "MANAGED_INDEX_MAX_EVIDENCE_EDGES",
    "MANAGED_INDEX_MAX_GIT_COMMITS",
    "MANAGED_INDEX_MAX_GIT_EDGES",
    "MANAGED_INDEX_MAX_SEMANTIC_DIMENSIONS",
    "MANAGED_INDEX_MAX_TEXT_BYTES",
    "MANAGED_INDEX_MAX_TOKENS_PER_CANDIDATE",
    "ManagedEvidenceEdge",
    "ManagedGitEdge",
    "ManagedIndexBuildInput",
    "ManagedIndexBundle",
    "ManagedIndexCandidate",
    "ManagedIndexDiscovery",
    "ManagedIndexPublication",
    "ManagedIndexRepository",
    "ManagedIndexSource",
    "ManagedIndexV3ContractError",
    "ManagedIndexV3Error",
    "build_managed_index_bundle",
    "dumps_managed_index_bundle",
    "loads_managed_index_bundle",
    "managed_index_bundle_id",
]
