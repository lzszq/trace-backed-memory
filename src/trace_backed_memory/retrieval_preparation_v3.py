from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import math
import re
from typing import Generic, NoReturn, Protocol, TypeVar, cast

from .activated_revision_v3 import ActivatedRevisionCandidate
from .contracts_v3 import canonical_sha256
from .gate_evaluation_v3 import (
    SystemGateDecision,
    SystemGateEvaluation,
    build_system_gate_evaluation,
    verify_system_gate_evaluation,
)
from .retrieval_policy_v3 import (
    RETRIEVAL_RANKING_STAGES,
    RETRIEVAL_TASK_MODES,
    RankingStage,
    RetrievalPolicyBundle,
    TaskMode,
)
from .retrieval_v3 import (
    IndexVersion,
    RetrievalHit,
    RetrievalMode,
    RetrievalSnapshot,
    RetrievalStage,
    TruncationReason,
    build_retrieval_snapshot,
)
from .service_v3 import (
    AuthenticatedRetrievalService,
    AuthenticatedServiceContext,
    AuthenticatedServiceV3Error,
    AuthorizedRetrievalResult,
    AuthorizedRetrievalScope,
)


RETRIEVAL_PREPARATION_MAX_CANDIDATES = 1_000
RETRIEVAL_PREPARATION_MAX_QUERY_BYTES = 64 * 1024
RETRIEVAL_PREPARATION_MAX_ATTRIBUTES = 16
RETRIEVAL_PREPARATION_MAX_ANCESTRY_RELATIONS = 1_000

_IDENTIFIER_MAX_CHARS = 128
_CANDIDATE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AUTHORIZATION_ID_RE = re.compile(r"^authz_sha256_[0-9a-f]{64}$")
_SUPPORTED_ATTRIBUTES = frozenset(
    {
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
)
_STAGE_ORDER = {
    value: index for index, value in enumerate(RETRIEVAL_RANKING_STAGES)
}


class RetrievalPreparationV3Error(AuthenticatedServiceV3Error):
    """Stable, sanitized failure while preparing retrieval/gate evidence."""


@dataclass(frozen=True)
class RetrievalPreparationContext:
    tenant_id: str
    repository_id: str
    environment_id: str
    task_mode: TaskMode
    commit_sha: str
    attributes: tuple[tuple[str, str], ...] = ()
    evaluation_suite: str | None = None
    evaluation_case_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.repository_id, "repository_id"),
            (self.environment_id, "environment_id"),
        ):
            _identifier(value, name)
        if self.task_mode not in RETRIEVAL_TASK_MODES:
            _invalid("task_mode is not supported")
        _metadata(self.commit_sha, "commit_sha", maximum=512)
        _attributes(self.attributes)
        if (self.evaluation_suite is None) != (
            self.evaluation_case_id is None
        ):
            _invalid(
                "evaluation_suite and evaluation_case_id must be paired"
            )
        if self.task_mode == "eval" and self.evaluation_suite is None:
            _invalid("eval mode requires an evaluation identity")
        if self.evaluation_suite is not None:
            if self.task_mode != "eval":
                _invalid("evaluation identity is only valid in eval mode")
            _metadata(
                self.evaluation_suite,
                "evaluation_suite",
                maximum=256,
            )
            _metadata(
                self.evaluation_case_id,
                "evaluation_case_id",
                maximum=256,
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "task_mode": self.task_mode,
            "commit_sha": self.commit_sha,
            "attributes": dict(self.attributes),
            "evaluation_suite": self.evaluation_suite,
            "evaluation_case_id": self.evaluation_case_id,
        }


@dataclass(frozen=True)
class RetrievalPreparationRequest:
    session_id: str
    request_id: str
    trace_id: str
    run_id: str
    context: RetrievalPreparationContext
    retrieval_mode: RetrievalMode
    retriever_id: str
    retriever_version: str
    top_k: int
    query: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value, name in (
            (self.session_id, "session_id"),
            (self.request_id, "request_id"),
            (self.trace_id, "trace_id"),
            (self.run_id, "run_id"),
            (self.retriever_id, "retriever_id"),
            (self.retriever_version, "retriever_version"),
        ):
            _identifier(value, name)
        if type(self.context) is not RetrievalPreparationContext:
            _invalid("context must be exactly RetrievalPreparationContext")
        if self.retrieval_mode not in {
            "metadata",
            "lexical",
            "semantic",
            "evidence_graph",
            "hybrid",
        }:
            _invalid("retrieval_mode is not supported")
        if type(self.top_k) is not int or self.top_k < 1 or self.top_k > 100:
            _invalid("top_k must be between 1 and 100")
        if self.retrieval_mode == "metadata":
            if self.query is not None:
                _invalid("metadata retrieval does not accept a query")
        elif (
            type(self.query) is not bytes
            or not self.query
            or len(self.query) > RETRIEVAL_PREPARATION_MAX_QUERY_BYTES
        ):
            _invalid("query must be bounded non-empty bytes")

    @property
    def query_sha256(self) -> str | None:
        if self.query is None:
            return None
        return "sha256:" + hashlib.sha256(self.query).hexdigest()


@dataclass(frozen=True)
class CandidateIndexRecord:
    memory_id: str
    candidate_sha256: str
    lexical_score: float | None = None
    semantic_score: float | None = None
    evidence_graph_score: float | None = None

    def __post_init__(self) -> None:
        _identifier(self.memory_id, "memory_id")
        if (
            type(self.candidate_sha256) is not str
            or not _CANDIDATE_DIGEST_RE.fullmatch(
                self.candidate_sha256
            )
        ):
            _invalid("candidate_sha256 must be a SHA-256 digest")
        for name in (
            "lexical_score",
            "semantic_score",
            "evidence_graph_score",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _score(value, name),
                )


@dataclass(frozen=True)
class CandidateDiscoveryResult:
    records: tuple[CandidateIndexRecord, ...]
    index_versions: tuple[IndexVersion, ...]
    ancestry_relations: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.records) is not tuple
            or len(self.records) > RETRIEVAL_PREPARATION_MAX_CANDIDATES
            or any(
                type(item) is not CandidateIndexRecord
                for item in self.records
            )
        ):
            _invalid("records must be a bounded tuple of index records")
        if len({item.memory_id for item in self.records}) != len(
            self.records
        ):
            _invalid("candidate discovery memory IDs must be unique")
        if len(
            {item.candidate_sha256 for item in self.records}
        ) != len(self.records):
            _invalid("candidate discovery hashes must be unique")
        if (
            type(self.index_versions) is not tuple
            or not self.index_versions
            or any(
                type(item) is not IndexVersion
                for item in self.index_versions
            )
            or len({item.index_kind for item in self.index_versions})
            != len(self.index_versions)
        ):
            _invalid(
                "index_versions must contain one index per unique kind"
            )
        _ancestry_relations(self.ancestry_relations)

    def prepared_context_sha256(
        self,
        context: RetrievalPreparationContext,
    ) -> str:
        if type(context) is not RetrievalPreparationContext:
            _invalid("retrieval preparation context is invalid")
        return canonical_sha256(
            {
                "request_context": context.to_dict(),
                "git_ancestry_relations": [
                    {
                        "anchor_commit_sha": anchor,
                        "is_ancestor": is_ancestor,
                    }
                    for anchor, is_ancestor in self.ancestry_relations
                ],
            }
        )


class CandidateDiscovery(Protocol):
    def discover(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        request: RetrievalPreparationRequest,
    ) -> CandidateDiscoveryResult: ...


class ActivatedRevisionRetrievalSource(Protocol):
    def load_authorized(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        *,
        memory_id: str,
    ) -> ActivatedRevisionCandidate: ...

    def verify_current(
        self,
        scope: AuthorizedRetrievalScope,
        candidate: ActivatedRevisionCandidate,
    ) -> None: ...


@dataclass(frozen=True)
class PreparedRetrievalEvidence:
    snapshot: RetrievalSnapshot
    system_gate_evaluation: SystemGateEvaluation
    candidates: tuple[ActivatedRevisionCandidate, ...] = field(
        repr=False
    )
    policy: RetrievalPolicyBundle

    def __post_init__(self) -> None:
        if type(self.snapshot) is not RetrievalSnapshot:
            _invalid("snapshot must be exactly RetrievalSnapshot")
        if type(self.system_gate_evaluation) is not SystemGateEvaluation:
            _invalid(
                "system_gate_evaluation must be exactly SystemGateEvaluation"
            )
        if type(self.policy) is not RetrievalPolicyBundle:
            _invalid("policy must be exactly RetrievalPolicyBundle")
        if (
            type(self.candidates) is not tuple
            or any(
                type(item) is not ActivatedRevisionCandidate
                for item in self.candidates
            )
        ):
            _invalid("candidates must be a tuple of activated revisions")
        verify_system_gate_evaluation(
            self.system_gate_evaluation,
            self.snapshot,
        )
        expected = tuple(
            (
                candidate.revision.memory_id,
                candidate.revision.revision_id,
                candidate.candidate_sha256,
            )
            for candidate in self.candidates
        )
        actual = tuple(
            (
                hit.memory_id,
                hit.memory_revision_id,
                hit.candidate_sha256,
            )
            for hit in self.snapshot.hits
        )
        if expected != actual:
            _invalid("prepared candidates do not match retrieval hits")
        if (
            self.system_gate_evaluation.policy_bundle_sha256
            != self.policy.policy_sha256
        ):
            _invalid("System Gate policy digest does not match policy")

    @property
    def allowed_revision_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.memory_revision_id
            for decision in self.system_gate_evaluation.decisions
            if decision.outcome == "allowed"
        )


_T = TypeVar("_T")


@dataclass(frozen=True)
class _PreparationOutcome(Generic[_T]):
    value: _T | None = None
    error: RetrievalPreparationV3Error | None = None


@dataclass(frozen=True)
class _RankedCandidate:
    candidate: ActivatedRevisionCandidate
    metadata_score: float
    lexical_score: float | None
    semantic_score: float | None
    evidence_graph_score: float | None
    fused_score: float
    selected_stages: tuple[RetrievalStage, ...]


class AuthenticatedRetrievalPreparationService:
    """Authorize, retrieve, filter, rank, gate, and recheck v3 revisions."""

    def __init__(
        self,
        *,
        authorization_service: AuthenticatedRetrievalService,
        policy_provider: Callable[[], RetrievalPolicyBundle],
        discovery: CandidateDiscovery,
        revision_source: ActivatedRevisionRetrievalSource,
        clock: Callable[[], str],
        evaluator_id: str,
        evaluator_version: str,
    ) -> None:
        if type(authorization_service) is not AuthenticatedRetrievalService:
            raise TypeError(
                "authorization_service must be AuthenticatedRetrievalService"
            )
        if not callable(policy_provider):
            raise TypeError("policy_provider must be callable")
        if not callable(getattr(discovery, "discover", None)):
            raise TypeError("discovery must provide discover()")
        if (
            not callable(
                getattr(revision_source, "load_authorized", None)
            )
            or not callable(
                getattr(revision_source, "verify_current", None)
            )
        ):
            raise TypeError("revision_source is invalid")
        if not callable(clock):
            raise TypeError("clock must be callable")
        _identifier(evaluator_id, "evaluator_id")
        _identifier(evaluator_version, "evaluator_version")
        self._authorization_service = authorization_service
        self._policy_provider = policy_provider
        self._discovery = discovery
        self._revision_source = revision_source
        self._clock = clock
        self._evaluator_id = evaluator_id
        self._evaluator_version = evaluator_version

    def prepare(
        self,
        context: AuthenticatedServiceContext,
        request: RetrievalPreparationRequest,
    ) -> AuthorizedRetrievalResult[PreparedRetrievalEvidence]:
        if type(context) is not AuthenticatedServiceContext:
            _invalid("authenticated service context is invalid")
        if type(request) is not RetrievalPreparationRequest:
            _invalid(
                "request must be exactly RetrievalPreparationRequest"
            )

        def prepare_authorized(
            scope: AuthorizedRetrievalScope,
        ) -> _PreparationOutcome[PreparedRetrievalEvidence]:
            try:
                return _PreparationOutcome(
                    value=self._prepare_authorized(
                        context,
                        scope,
                        request,
                    )
                )
            except RetrievalPreparationV3Error as error:
                return _PreparationOutcome(error=error)
            except Exception:
                return _PreparationOutcome(
                    error=RetrievalPreparationV3Error(
                        "TBM_RETRIEVAL_PREPARATION_FAILED",
                        "retrieval preparation failed",
                    )
                )

        try:
            authorized = self._authorization_service.authorize_retrieval(
                context,
                prepare_authorized,
            )
        except AuthenticatedServiceV3Error as error:
            raise RetrievalPreparationV3Error(
                error.code,
                str(error),
            ) from None
        evidence = self._unwrap(authorized.value)
        return AuthorizedRetrievalResult(
            decision=authorized.decision,
            scope=authorized.scope,
            value=evidence,
        )

    def _prepare_authorized(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        request: RetrievalPreparationRequest,
    ) -> PreparedRetrievalEvidence:
        self._verify_scope(context, scope, request.context)
        policy = self._load_policy()
        discovery = self._load_discovery(context, scope, request)
        self._verify_discovery(
            discovery,
            request.retrieval_mode,
            policy,
        )
        reasons: set[TruncationReason] = set()
        ranked: list[_RankedCandidate] = []
        for record in sorted(
            discovery.records,
            key=lambda item: item.memory_id,
        ):
            candidate = self._load_candidate(
                context,
                scope,
                record,
            )
            reason = self._pre_gate_filter_reason(
                candidate,
                request.context,
                policy,
                discovery.ancestry_relations,
            )
            if reason is not None:
                reasons.add(reason)
                continue
            stage_scores = self._stage_scores(
                record,
                request.retrieval_mode,
            )
            fused_score = self._fused_score(stage_scores, policy)
            if fused_score < policy.minimum_fused_score:
                reasons.add("minimum_score")
                continue
            ranked.append(
                _RankedCandidate(
                    candidate=candidate,
                    metadata_score=stage_scores["metadata"],
                    lexical_score=stage_scores.get("lexical"),
                    semantic_score=stage_scores.get("semantic"),
                    evidence_graph_score=stage_scores.get(
                        "evidence_graph"
                    ),
                    fused_score=fused_score,
                    selected_stages=cast(
                        tuple[RetrievalStage, ...],
                        tuple(
                            sorted(
                                stage_scores,
                                key=_STAGE_ORDER.__getitem__,
                            )
                        )
                        + ("fusion",),
                    ),
                )
            )
        ranked.sort(
            key=lambda item: (
                -item.fused_score,
                item.candidate.revision.revision_id,
            )
        )
        selected: list[_RankedCandidate] = []
        payload_bytes = 0
        for item in ranked:
            if len(selected) >= request.top_k:
                reasons.add("top_k")
                continue
            next_size = payload_bytes + len(item.candidate.content)
            if next_size > policy.payload_budget_bytes:
                reasons.add("payload_budget")
                continue
            payload_bytes = next_size
            selected.append(item)
        hits = tuple(
            RetrievalHit(
                memory_id=item.candidate.revision.memory_id,
                memory_revision_id=item.candidate.revision.revision_id,
                candidate_sha256=item.candidate.candidate_sha256,
                rank=index,
                metadata_score=item.metadata_score,
                lexical_score=item.lexical_score,
                semantic_score=item.semantic_score,
                evidence_graph_score=item.evidence_graph_score,
                fused_score=item.fused_score,
                selected_stages=item.selected_stages,
            )
            for index, item in enumerate(selected, start=1)
        )
        recorded_at = self._trusted_time()
        snapshot = build_retrieval_snapshot(
            session_id=request.session_id,
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            authorization_event_id=scope.authorization_event_id,
            context_sha256=discovery.prepared_context_sha256(
                request.context
            ),
            query_sha256=request.query_sha256,
            retrieval_mode=request.retrieval_mode,
            retriever_id=request.retriever_id,
            retriever_version=request.retriever_version,
            index_versions=discovery.index_versions,
            hits=hits,
            total_candidates=len(discovery.records),
            top_k=request.top_k,
            truncated=bool(reasons),
            truncation_reasons=tuple(sorted(reasons)),
            created_at=recorded_at,
        )
        decisions = tuple(
            self._system_gate_decision(
                item.candidate,
                request.context.task_mode,
                policy,
            )
            for item in selected
        )
        evaluation = build_system_gate_evaluation(
            session_id=request.session_id,
            retrieval_snapshot_id=snapshot.snapshot_id,
            authorization_event_id=scope.authorization_event_id,
            policy_bundle_sha256=policy.policy_sha256,
            evaluator_id=self._evaluator_id,
            evaluator_version=self._evaluator_version,
            decisions=decisions,
            evaluated_at=recorded_at,
        )
        try:
            verify_system_gate_evaluation(evaluation, snapshot)
            for item in selected:
                self._revision_source.verify_current(
                    scope,
                    item.candidate,
                )
        except RetrievalPreparationV3Error:
            raise
        except Exception as error:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_STALE",
                "retrieval candidate changed before evidence publication",
            ) from error
        if self._load_policy() != policy:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_POLICY_CHANGED",
                "retrieval policy changed during preparation",
            )
        return PreparedRetrievalEvidence(
            snapshot=snapshot,
            system_gate_evaluation=evaluation,
            candidates=tuple(item.candidate for item in selected),
            policy=policy,
        )

    @staticmethod
    def _verify_scope(
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        preparation_context: RetrievalPreparationContext,
    ) -> None:
        if type(scope) is not AuthorizedRetrievalScope or not (
            type(scope.authorization_event_id) is str
            and _AUTHORIZATION_ID_RE.fullmatch(
                scope.authorization_event_id
            )
        ):
            _invalid("authorized retrieval scope is invalid")
        if (
            scope.principal_id != context.principal.principal_id
            or scope.agent_client_id
            != context.agent_client.agent_client_id
            or scope.tenant_id != context.tenant_id
            or scope.environment_id != context.environment_id
            or preparation_context.tenant_id != scope.tenant_id
            or preparation_context.repository_id != scope.repository_id
            or preparation_context.environment_id
            != scope.environment_id
        ):
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_SCOPE_MISMATCH",
                "retrieval preparation context is outside authorized scope",
            )

    def _load_policy(self) -> RetrievalPolicyBundle:
        try:
            policy = self._policy_provider()
        except Exception as error:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_POLICY_UNAVAILABLE",
                "retrieval policy could not be loaded",
            ) from error
        if type(policy) is not RetrievalPolicyBundle:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_POLICY_INVALID",
                "retrieval policy provider returned an invalid record",
            )
        return policy

    def _load_discovery(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        request: RetrievalPreparationRequest,
    ) -> CandidateDiscoveryResult:
        try:
            result = self._discovery.discover(
                context,
                scope,
                request,
            )
        except Exception as error:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_DISCOVERY_FAILED",
                "candidate discovery failed",
            ) from error
        if type(result) is not CandidateDiscoveryResult:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_DISCOVERY_INVALID",
                "candidate discovery returned an invalid record",
            )
        return result

    def _load_candidate(
        self,
        context: AuthenticatedServiceContext,
        scope: AuthorizedRetrievalScope,
        record: CandidateIndexRecord,
    ) -> ActivatedRevisionCandidate:
        try:
            candidate = self._revision_source.load_authorized(
                context,
                scope,
                memory_id=record.memory_id,
            )
        except Exception as error:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_REVISION_FAILED",
                "activated revision could not be loaded",
            ) from error
        if type(candidate) is not ActivatedRevisionCandidate:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_CANDIDATE_MISMATCH",
                "candidate does not match authorized discovery evidence",
            )
        revision = candidate.revision
        if (
            candidate.candidate_sha256 != record.candidate_sha256
            or candidate.retrieval_authorization_event_id
            != scope.authorization_event_id
            or revision.memory_id != record.memory_id
            or revision.scope.kind != "repository"
            or revision.scope.tenant_id != scope.tenant_id
            or revision.scope.repository_id != scope.repository_id
        ):
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_CANDIDATE_MISMATCH",
                "candidate does not match authorized discovery evidence",
            )
        if revision.memory_kind == "lesson" and (
            candidate.fix_evidence is None
            or not candidate.regression_evidence
        ):
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_EVIDENCE_MISSING",
                "lesson candidate is missing structured evidence",
            )
        return candidate

    @staticmethod
    def _pre_gate_filter_reason(
        candidate: ActivatedRevisionCandidate,
        context: RetrievalPreparationContext,
        policy: RetrievalPolicyBundle,
        ancestry_relations: tuple[tuple[str, bool], ...],
    ) -> TruncationReason | None:
        revision = candidate.revision
        if (
            revision.content_artifact.classification
            not in policy.allowed_classifications
        ):
            return "classification"
        context_attributes = dict(context.attributes)
        if any(
            context_attributes.get(key) != value
            for key, value in revision.scope.attributes
        ):
            return "applicability"
        if revision.eval_leaking:
            return "eval_leakage"
        if (
            context.evaluation_suite is not None
            and context.evaluation_case_id is not None
            and any(
                evidence.evaluation_suite
                == context.evaluation_suite
                and evidence.evaluation_case_id
                == context.evaluation_case_id
                for evidence in candidate.regression_evidence
            )
        ):
            return "eval_leakage"
        if policy.ancestry_mode == "required" and (
            revision.memory_kind == "lesson"
        ):
            fix_evidence = candidate.fix_evidence
            if fix_evidence is None:
                return "git_ancestry"
            relation_by_anchor = dict(ancestry_relations)
            if relation_by_anchor.get(fix_evidence.fix_commit_sha) is not True:
                return "git_ancestry"
        return None

    @staticmethod
    def _stage_scores(
        record: CandidateIndexRecord,
        mode: RetrievalMode,
    ) -> dict[RankingStage, float]:
        values = {
            "lexical": record.lexical_score,
            "semantic": record.semantic_score,
            "evidence_graph": record.evidence_graph_score,
        }
        if mode == "metadata":
            if any(value is not None for value in values.values()):
                _invalid(
                    "metadata discovery cannot include other stage scores"
                )
            return {"metadata": 1.0}
        if mode == "hybrid":
            selected = {
                cast(RankingStage, name): cast(float, value)
                for name, value in values.items()
                if value is not None
            }
            if not selected:
                _invalid(
                    "hybrid discovery requires a non-metadata stage score"
                )
            return {"metadata": 1.0, **selected}
        if values[mode] is None or any(
            value is not None
            for name, value in values.items()
            if name != mode
        ):
            _invalid(
                "non-hybrid discovery must contain exactly its mode score"
            )
        return {
            "metadata": 1.0,
            cast(RankingStage, mode): cast(float, values[mode]),
        }

    @staticmethod
    def _fused_score(
        stage_scores: Mapping[RankingStage, float],
        policy: RetrievalPolicyBundle,
    ) -> float:
        weighted = math.fsum(
            value * policy.weight(stage)
            for stage, value in stage_scores.items()
        )
        total_weight = math.fsum(
            policy.weight(stage) for stage in stage_scores
        )
        return weighted / total_weight

    @staticmethod
    def _system_gate_decision(
        candidate: ActivatedRevisionCandidate,
        task_mode: TaskMode,
        policy: RetrievalPolicyBundle,
    ) -> SystemGateDecision:
        revision = candidate.revision
        allowed = revision.memory_type in policy.allowed_types(task_mode)
        return SystemGateDecision(
            memory_revision_id=revision.revision_id,
            candidate_sha256=candidate.candidate_sha256,
            outcome="allowed" if allowed else "blocked",
            reason_code=(
                "allowed" if allowed else "memory_type_not_allowed"
            ),
            rule_id="tbm.retrieval-policy.v3.mode-memory-type",
        )

    def _trusted_time(self) -> str:
        try:
            value = self._clock()
        except Exception as error:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_CLOCK_FAILED",
                "trusted retrieval clock failed",
            ) from error
        if type(value) is not str:
            raise RetrievalPreparationV3Error(
                "TBM_RETRIEVAL_PREPARATION_CLOCK_INVALID",
                "trusted retrieval clock returned invalid data",
            )
        return value

    @staticmethod
    def _unwrap(
        outcome: _PreparationOutcome[PreparedRetrievalEvidence],
    ) -> PreparedRetrievalEvidence:
        if type(outcome) is not _PreparationOutcome:
            _invalid("authorized preparation returned an invalid outcome")
        if outcome.error is not None:
            raise outcome.error
        if type(outcome.value) is not PreparedRetrievalEvidence:
            _invalid("authorized preparation did not return evidence")
        return outcome.value

    @staticmethod
    def _verify_discovery(
        discovery: CandidateDiscoveryResult,
        mode: RetrievalMode,
        policy: RetrievalPolicyBundle,
    ) -> None:
        index_versions = discovery.index_versions
        kinds = {item.index_kind for item in index_versions}
        if "metadata" not in kinds:
            _invalid("retrieval preparation requires a metadata index")
        if mode != "hybrid" and mode not in kinds:
            _invalid("retrieval mode requires a matching index")
        if mode == "hybrid" and len(
            kinds.intersection(
                {"metadata", "lexical", "semantic", "evidence_graph"}
            )
        ) < 2:
            _invalid("hybrid retrieval requires at least two indexes")
        if policy.ancestry_mode == "required" and "git_graph" not in kinds:
            _invalid("required ancestry needs a git_graph index")
        if (
            policy.ancestry_mode == "disabled"
            and discovery.ancestry_relations
        ):
            _invalid(
                "disabled ancestry cannot include ancestry relations"
            )
        for record in discovery.records:
            stage_scores = (
                AuthenticatedRetrievalPreparationService._stage_scores(
                    record,
                    mode,
                )
            )
            if any(stage not in kinds for stage in stage_scores):
                _invalid(
                    "every discovery score requires a matching index version"
                )


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _IDENTIFIER_MAX_CHARS
    ):
        _invalid(f"{name} must be a bounded identifier")
    try:
        cast(str, value).encode("utf-8")
    except UnicodeError as error:
        raise RetrievalPreparationV3Error(
            "TBM_RETRIEVAL_PREPARATION_INVALID",
            f"{name} must be valid UTF-8",
        ) from error


def _metadata(value: object, name: str, *, maximum: int) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
    ):
        _invalid(f"{name} must be bounded non-empty text")
    try:
        cast(str, value).encode("utf-8")
    except UnicodeError as error:
        raise RetrievalPreparationV3Error(
            "TBM_RETRIEVAL_PREPARATION_INVALID",
            f"{name} must be valid UTF-8",
        ) from error


def _attributes(values: tuple[tuple[str, str], ...]) -> None:
    if (
        type(values) is not tuple
        or len(values) > RETRIEVAL_PREPARATION_MAX_ATTRIBUTES
    ):
        _invalid("attributes must be a bounded tuple")
    previous: str | None = None
    for value in values:
        if (
            type(value) is not tuple
            or len(value) != 2
            or type(value[0]) is not str
            or value[0] not in _SUPPORTED_ATTRIBUTES
        ):
            _invalid("attributes contains an unsupported key")
        _metadata(value[1], f"attributes.{value[0]}", maximum=512)
        if previous is not None and value[0] <= previous:
            _invalid("attributes must be unique and sorted")
        previous = value[0]


def _ancestry_relations(
    values: tuple[tuple[str, bool], ...],
) -> None:
    if (
        type(values) is not tuple
        or len(values) > RETRIEVAL_PREPARATION_MAX_ANCESTRY_RELATIONS
    ):
        _invalid("ancestry_relations must be a bounded tuple")
    previous: str | None = None
    for value in values:
        if (
            type(value) is not tuple
            or len(value) != 2
            or type(value[1]) is not bool
        ):
            _invalid("ancestry_relations contains an invalid relation")
        _metadata(value[0], "anchor_commit_sha", maximum=512)
        if previous is not None and value[0] <= previous:
            _invalid("ancestry_relations must be unique and sorted")
        previous = value[0]


def _score(value: object, name: str) -> float:
    if type(value) not in {int, float}:
        _invalid(f"{name} must be a finite score")
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        _invalid(f"{name} must be between zero and one")
    return result


def _invalid(message: str) -> NoReturn:
    raise RetrievalPreparationV3Error(
        "TBM_RETRIEVAL_PREPARATION_INVALID",
        message,
    )


__all__ = [
    "ActivatedRevisionRetrievalSource",
    "AuthenticatedRetrievalPreparationService",
    "CandidateDiscovery",
    "CandidateDiscoveryResult",
    "CandidateIndexRecord",
    "PreparedRetrievalEvidence",
    "RETRIEVAL_PREPARATION_MAX_ANCESTRY_RELATIONS",
    "RETRIEVAL_PREPARATION_MAX_ATTRIBUTES",
    "RETRIEVAL_PREPARATION_MAX_CANDIDATES",
    "RETRIEVAL_PREPARATION_MAX_QUERY_BYTES",
    "RetrievalPreparationContext",
    "RetrievalPreparationRequest",
    "RetrievalPreparationV3Error",
]
