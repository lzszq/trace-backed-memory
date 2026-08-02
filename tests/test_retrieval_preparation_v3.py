from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Callable

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.retrieval_preparation_v3 as retrieval_preparation_v3
from tests.test_artifact_service_v3 import _context, _registry
from tests.test_memory_publication_v3 import (
    ACTIVATED_AT,
    CONTENT,
    DIGEST,
    _approval,
    _authorization as _publication_authorization,
    _publication_inputs,
)


NOW = "2026-07-29T00:00:00Z"
AUTHORIZATION_ID = "authz_sha256_" + "a" * 64
QUERY = b"repair the cache"
SEMANTIC_QUERY = tbm.SemanticQueryVector(
    provider_id="reference_embeddings",
    provider_version="v1",
    vector=(0.6, 0.8),
)
QUERY_EVIDENCE_SHA256 = SEMANTIC_QUERY.evidence_sha256(
    "sha256:" + hashlib.sha256(QUERY).hexdigest()
)


def test_semantic_query_vector_rejects_overflowing_number():
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        tbm.SemanticQueryVector(
            "reference_embeddings",
            "v1",
            (10**400,),
        )


def _rules(
    *,
    repair: tuple[tbm.MemoryType, ...] = (
        "procedural",
        "semantic",
        "policy",
    ),
) -> tuple[tbm.ModeMemoryRule, ...]:
    return (
        tbm.ModeMemoryRule("planning", ("semantic", "policy")),
        tbm.ModeMemoryRule("repair", repair),
        tbm.ModeMemoryRule("debug", ("procedural", "episodic", "policy")),
        tbm.ModeMemoryRule("eval", ("procedural", "semantic")),
        tbm.ModeMemoryRule("production", ("procedural", "policy")),
    )


def _policy(
    *,
    allowed_classifications: tuple[tbm.DataClassification, ...] = ("internal",),
    minimum_fused_score: float = 0.5,
    payload_budget_bytes: int = 8_192,
    ancestry_mode: tbm.AncestryMode = "required",
    policy_version: str = "retrieval_policy_001",
) -> tbm.RetrievalPolicyBundle:
    return tbm.build_retrieval_policy(
        policy_version=policy_version,
        allowed_classifications=allowed_classifications,
        mode_memory_rules=_rules(),
        ancestry_mode=ancestry_mode,
        ancestry_bypass_reason=(
            None
            if ancestry_mode == "required"
            else "Git ancestry is unavailable in this test profile."
        ),
        stage_weights=(
            ("metadata", 0.1),
            ("lexical", 0.2),
            ("semantic", 0.4),
            ("evidence_graph", 0.3),
        ),
        minimum_fused_score=minimum_fused_score,
        payload_budget_bytes=payload_budget_bytes,
    )


def _candidate(
    memory_id: str,
    *,
    memory_type: tbm.MemoryType = "procedural",
    eval_leaking: bool = False,
) -> tbm.ActivatedRevisionCandidate:
    base, _fixes, _regressions = _publication_inputs()
    revision_values = {
        key: value
        for key, value in base.__dict__.items()
        if key not in {"revision_id", "contract_version"}
    }
    revision_values.update(
        memory_id=memory_id,
        memory_type=memory_type,
        eval_leaking=eval_leaking,
    )
    revision = tbm.build_memory_revision(**revision_values)
    (
        approval,
        _revision,
        fixes,
        regressions,
        publication_policy,
        approval_request,
        approval_decision,
    ) = _approval(revision=revision)
    activation_request, activation_decision = _publication_authorization(
        publication_policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )
    activation = tbm.activate_memory_revision(
        revision=revision,
        approval=approval,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=publication_policy,
        approval_request=approval_request,
        approval_decision=approval_decision,
        previous_activation=None,
        policy=publication_policy,
        request=activation_request,
        decision=activation_decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at=ACTIVATED_AT,
        activation_attestation_sha256=DIGEST,
    )
    return tbm.ActivatedRevisionCandidate(
        revision=revision,
        approval=approval,
        activation=activation,
        content=CONTENT,
        fix_evidence=next(iter(fixes.values())),
        regression_evidence=tuple(regressions.values()),
        candidate_sha256=tbm.activated_revision_candidate_sha256(
            revision,
            approval,
            activation,
            approval_attestation_verified_by="attestation_verifier",
            activation_attestation_verified_by="attestation_verifier",
        ),
        retrieval_authorization_event_id=AUTHORIZATION_ID,
        artifact_authorization_event_id=AUTHORIZATION_ID,
        approval_attestation_verified_by="attestation_verifier",
        activation_attestation_verified_by="attestation_verifier",
    )


class _Discovery:
    def __init__(self, result: tbm.CandidateDiscoveryResult) -> None:
        self.result = result
        self.calls = 0

    def discover(self, _context, _scope, _request, _policy):
        self.calls += 1
        return self.result


class _Source:
    def __init__(
        self,
        candidates: tuple[tbm.ActivatedRevisionCandidate, ...],
        *,
        stale: bool = False,
        substitute: tbm.ActivatedRevisionCandidate | None = None,
    ) -> None:
        self.candidates = {item.revision.memory_id: item for item in candidates}
        self.stale = stale
        self.substitute = substitute
        self.load_calls = 0
        self.verify_calls = 0

    def load_authorized(self, _context, scope, *, memory_id):
        self.load_calls += 1
        candidate = self.substitute or self.candidates[memory_id]
        return replace(
            candidate,
            retrieval_authorization_event_id=scope.authorization_event_id,
        )

    def verify_current(self, _scope, _candidate):
        self.verify_calls += 1
        if self.stale:
            raise RuntimeError("private stale-head detail")


class _PolicyProvider:
    def __init__(
        self,
        first: tbm.RetrievalPolicyBundle,
        second: tbm.RetrievalPolicyBundle | None = None,
    ) -> None:
        self.first = first
        self.second = second or first
        self.calls = 0

    def __call__(self) -> tbm.RetrievalPolicyBundle:
        self.calls += 1
        return self.first if self.calls == 1 else self.second


def _indexes(
    *kinds: tbm.retrieval_v3.IndexKind,
) -> tuple[tbm.IndexVersion, ...]:
    return tuple(
        tbm.IndexVersion(
            index_kind=kind,
            index_id=f"{kind}_index",
            index_version="v1",
            content_sha256="sha256:" + f"{index + 1:x}" * 64,
        )
        for index, kind in enumerate(kinds)
    )


def _record(
    candidate: tbm.ActivatedRevisionCandidate,
    *,
    lexical: float | None = 0.8,
    semantic: float | None = 0.8,
    evidence_graph: float | None = None,
) -> tbm.CandidateIndexRecord:
    return tbm.CandidateIndexRecord(
        memory_id=candidate.revision.memory_id,
        candidate_sha256=candidate.candidate_sha256,
        lexical_score=lexical,
        semantic_score=semantic,
        evidence_graph_score=evidence_graph,
    )


def _result(
    *,
    records: tuple[tbm.CandidateIndexRecord, ...],
    index_versions: tuple[tbm.IndexVersion, ...],
    ancestry_relations: tuple[tuple[str, bool], ...] = (("def456", True),),
) -> tbm.CandidateDiscoveryResult:
    has_semantic_index = any(item.index_kind == "semantic" for item in index_versions)
    return tbm.CandidateDiscoveryResult(
        records=records,
        index_versions=index_versions,
        ancestry_relations=ancestry_relations,
        query_evidence_sha256=(QUERY_EVIDENCE_SHA256 if has_semantic_index else None),
    )


def _request(
    *,
    context: tbm.RetrievalPreparationContext | None = None,
    top_k: int = 10,
    mode: tbm.retrieval_v3.RetrievalMode = "hybrid",
) -> tbm.RetrievalPreparationRequest:
    return tbm.RetrievalPreparationRequest(
        session_id="session_001",
        request_id="request_001",
        trace_id="trace_001",
        run_id="run_001",
        context=context
        or tbm.RetrievalPreparationContext(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
            task_mode="repair",
            commit_sha="fedcba",
            attributes=(("branch", "main"),),
        ),
        retrieval_mode=mode,
        retriever_id="reference_retriever",
        retriever_version="v1",
        top_k=top_k,
        query=None if mode == "metadata" else QUERY,
        semantic_query=(SEMANTIC_QUERY if mode in {"semantic", "hybrid"} else None),
    )


def _retrieval_authorization(
    registry: tbm.EntityRegistrySnapshot,
    *,
    check_same_thread: bool = True,
) -> tuple[
    tbm.AuthenticatedRetrievalService,
    tbm.SQLiteAuthorizationV3Repository,
]:
    repository = tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True,
        check_same_thread=check_same_thread,
    )
    request_number = iter(range(1, 100))
    service = tbm.AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=repository,
        clock=lambda: NOW,
        request_id_factory=lambda: f"retrieval_{next(request_number):03d}",
    )
    return service, repository


def _service(
    authorization: tbm.AuthenticatedRetrievalService,
    policy_provider: Callable[[], tbm.RetrievalPolicyBundle],
    discovery: _Discovery,
    source: _Source,
) -> tbm.AuthenticatedRetrievalPreparationService:
    return tbm.AuthenticatedRetrievalPreparationService(
        authorization_service=authorization,
        policy_provider=policy_provider,
        discovery=discovery,
        revision_source=source,
        clock=lambda: NOW,
        evaluator_id="system_gate",
        evaluator_version="v1",
    )


def test_retrieval_preparation_authorizes_ranks_gates_and_rechecks():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    allowed = _candidate("memory_allowed")
    blocked = _candidate("memory_blocked", memory_type="episodic")
    next_candidate = _candidate("memory_next")
    below_minimum = _candidate("memory_below_minimum")
    discovery = _Discovery(
        _result(
            records=(
                _record(allowed, lexical=0.9, semantic=0.9),
                _record(blocked, lexical=1.0, semantic=1.0),
                _record(next_candidate, lexical=0.6, semantic=0.6),
                _record(below_minimum, lexical=0.0, semantic=0.0),
            ),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    source = _Source((allowed, blocked, next_candidate, below_minimum))
    provider = _PolicyProvider(_policy())
    try:
        prepared = _service(
            authorization,
            provider,
            discovery,
            source,
        ).prepare(
            _context(registry),
            _request(top_k=2),
        )
        evidence = prepared.value

        assert evidence.snapshot.authorization_event_id == (
            prepared.scope.authorization_event_id
        )
        assert evidence.snapshot.context_sha256 == (
            discovery.result.prepared_context_sha256(_request().context)
        )
        assert tuple(hit.memory_id for hit in evidence.snapshot.hits) == (
            "memory_blocked",
            "memory_allowed",
        )
        assert tuple(
            decision.outcome for decision in evidence.system_gate_evaluation.decisions
        ) == ("blocked", "allowed")
        assert evidence.allowed_revision_ids == (allowed.revision.revision_id,)
        assert evidence.snapshot.truncation_reasons == (
            "minimum_score",
            "top_k",
        )
        assert evidence.snapshot.query_sha256 == _request().query_sha256
        assert b"repair the cache" not in (
            tbm.dumps_retrieval_snapshot(evidence.snapshot).encode()
        )
        assert source.load_calls == 4
        assert source.verify_calls == 2
        assert discovery.calls == 1
        assert provider.calls == 2
        assert (
            len(repository.list_decisions(registry.authorization_policy.policy_sha256))
            == 1
        )
        with tbm.SQLiteGateEvidenceV3Repository.connect(
            initialize=True
        ) as evidence_repository:
            stored = evidence_repository.store_bundle(
                evidence.snapshot,
                evidence.system_gate_evaluation,
            )
            assert stored.snapshot_inserted is True
            assert stored.evaluation_inserted is True
            assert (
                evidence_repository.load_snapshot(evidence.snapshot.snapshot_id)
                == evidence.snapshot
            )
    finally:
        repository.close()


@pytest.mark.parametrize(
    (
        "context",
        "policy",
        "candidate",
        "ancestry_relations",
        "reason",
    ),
    (
        (
            tbm.RetrievalPreparationContext(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
                task_mode="repair",
                commit_sha="fedcba",
                attributes=(("branch", "other"),),
            ),
            _policy(minimum_fused_score=0.0),
            _candidate("memory_applicability"),
            (("def456", True),),
            "applicability",
        ),
        (
            tbm.RetrievalPreparationContext(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
                task_mode="eval",
                commit_sha="fedcba",
                attributes=(("branch", "main"),),
                evaluation_suite="regression",
                evaluation_case_id="case_fixed",
            ),
            _policy(minimum_fused_score=0.0),
            _candidate("memory_eval_case"),
            (("def456", True),),
            "eval_leakage",
        ),
        (
            _request().context,
            _policy(minimum_fused_score=0.0),
            _candidate("memory_eval_flag", eval_leaking=True),
            (("def456", True),),
            "eval_leakage",
        ),
        (
            _request().context,
            _policy(minimum_fused_score=0.0),
            _candidate("memory_ancestry"),
            (),
            "git_ancestry",
        ),
        (
            _request().context,
            _policy(
                allowed_classifications=("public",),
                minimum_fused_score=0.0,
            ),
            _candidate("memory_classification"),
            (("def456", True),),
            "classification",
        ),
    ),
)
def test_retrieval_preparation_records_fail_closed_filter_reasons(
    context,
    policy,
    candidate,
    ancestry_relations,
    reason,
):
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
            ancestry_relations=ancestry_relations,
        )
    )
    source = _Source((candidate,))
    try:
        result = _service(
            authorization,
            _PolicyProvider(policy),
            discovery,
            source,
        ).prepare(_context(registry), _request(context=context))
        assert result.value.snapshot.hits == ()
        assert result.value.snapshot.truncation_reasons == (reason,)
        assert result.value.system_gate_evaluation.decisions == ()
        assert source.verify_calls == 0
    finally:
        repository.close()


def test_retrieval_preparation_records_payload_budget_truncation():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_payload")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    try:
        result = _service(
            authorization,
            _PolicyProvider(
                _policy(
                    minimum_fused_score=0.0,
                    payload_budget_bytes=len(CONTENT) - 1,
                )
            ),
            discovery,
            _Source((candidate,)),
        ).prepare(_context(registry), _request())
        assert result.value.snapshot.hits == ()
        assert result.value.snapshot.truncation_reasons == ("payload_budget",)
    finally:
        repository.close()


def test_retrieval_preparation_allows_audited_ancestry_bypass_policy():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_ancestry_bypass")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
            ),
            ancestry_relations=(),
        )
    )
    try:
        result = _service(
            authorization,
            _PolicyProvider(
                _policy(
                    minimum_fused_score=0.0,
                    ancestry_mode="disabled",
                )
            ),
            discovery,
            _Source((candidate,)),
        ).prepare(_context(registry), _request())
        assert len(result.value.snapshot.hits) == 1
        assert result.value.snapshot.truncation_reasons == ()
        assert all(
            index.index_kind != "git_graph"
            for index in result.value.snapshot.index_versions
        )
    finally:
        repository.close()


def test_retrieval_preparation_context_requires_exact_eval_identity():
    with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
        tbm.RetrievalPreparationContext(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
            task_mode="eval",
            commit_sha="fedcba",
        )
    assert caught.value.code == "TBM_RETRIEVAL_PREPARATION_INVALID"


def test_candidate_discovery_requires_one_exact_version_per_index_kind():
    candidate = _candidate("memory_duplicate_index")
    with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "metadata"),
        )
    assert caught.value.code == "TBM_RETRIEVAL_PREPARATION_INVALID"


def test_retrieval_preparation_denial_precedes_discovery_and_revision_reads():
    registry = _registry(permissions=("artifact:read",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_denied")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    source = _Source((candidate,))
    try:
        with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
            _service(
                authorization,
                _PolicyProvider(_policy()),
                discovery,
                source,
            ).prepare(_context(registry), _request())
        assert caught.value.code == "TBM_SERVICE_AUTHORIZATION_DENIED"
        assert discovery.calls == 0
        assert source.load_calls == 0
    finally:
        repository.close()


def test_retrieval_preparation_rejects_unversioned_score_before_reads():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_expected")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "git_graph"),
        )
    )
    try:
        source = _Source((candidate,))
        with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
            _service(
                authorization,
                _PolicyProvider(_policy()),
                discovery,
                source,
            ).prepare(_context(registry), _request())
        assert caught.value.code == "TBM_RETRIEVAL_PREPARATION_INVALID"
        assert source.load_calls == 0
    finally:
        repository.close()


def test_retrieval_preparation_rejects_candidate_substitution():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_expected")
    substitute = _candidate("memory_substitute")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    try:
        with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
            _service(
                authorization,
                _PolicyProvider(_policy()),
                discovery,
                _Source((candidate,), substitute=substitute),
            ).prepare(_context(registry), _request())
        assert caught.value.code == "TBM_RETRIEVAL_PREPARATION_CANDIDATE_MISMATCH"
    finally:
        repository.close()


def test_retrieval_preparation_uses_revision_id_as_stable_tie_breaker():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    first = _candidate("memory_tie_first")
    second = _candidate("memory_tie_second")
    discovery = _Discovery(
        _result(
            records=(_record(second), _record(first)),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    try:
        result = _service(
            authorization,
            _PolicyProvider(_policy()),
            discovery,
            _Source((first, second)),
        ).prepare(_context(registry), _request())
        assert tuple(
            hit.memory_revision_id for hit in result.value.snapshot.hits
        ) == tuple(
            sorted(
                (
                    first.revision.revision_id,
                    second.revision.revision_id,
                )
            )
        )
    finally:
        repository.close()


@pytest.mark.parametrize("failure", ("stale", "policy"))
def test_retrieval_preparation_rejects_changes_before_publication(failure):
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate(f"memory_{failure}")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "lexical",
                "semantic",
                "git_graph",
            ),
        )
    )
    provider = _PolicyProvider(
        _policy(),
        _policy(policy_version="retrieval_policy_002"),
    )
    source = _Source((candidate,), stale=failure == "stale")
    try:
        with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
            _service(
                authorization,
                provider,
                discovery,
                source,
            ).prepare(_context(registry), _request())
        assert caught.value.code == (
            "TBM_RETRIEVAL_PREPARATION_STALE"
            if failure == "stale"
            else "TBM_RETRIEVAL_PREPARATION_POLICY_CHANGED"
        )
        assert "private" not in str(caught.value)
    finally:
        repository.close()


def test_hybrid_semantic_index_requires_query_vector_evidence():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_semantic_unbound")
    discovery = _Discovery(
        tbm.CandidateDiscoveryResult(
            records=(_record(candidate),),
            index_versions=_indexes(
                "metadata",
                "semantic",
                "git_graph",
            ),
            ancestry_relations=(("def456", True),),
            query_evidence_sha256=None,
        )
    )
    source = _Source((candidate,))
    try:
        with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
            _service(
                authorization,
                _PolicyProvider(_policy()),
                discovery,
                source,
            ).prepare(
                _context(registry),
                replace(_request(), semantic_query=None),
            )
        assert caught.value.code == "TBM_RETRIEVAL_PREPARATION_INVALID"
        assert source.load_calls == 0
    finally:
        repository.close()


def test_retrieval_preparation_context_and_query_contract_matrix():
    context = _request().context
    for changes in (
        {"task_mode": "unsupported"},
        {"evaluation_suite": "suite"},
        {"evaluation_suite": "suite", "evaluation_case_id": "case"},
    ):
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            replace(context, **changes)

    for vector in (
        [],
        (),
        ("not-a-number",),
        (float("inf"),),
        (0.0, 0.0),
    ):
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            tbm.SemanticQueryVector("provider", "v1", vector)
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        SEMANTIC_QUERY.evidence_sha256("not-a-digest")

    request = _request()
    invalid_request_changes = (
        {"context": object()},
        {"retrieval_mode": "unsupported"},
        {"top_k": 0},
        {"query": None},
        {"query": b""},
        {"semantic_query": object()},
    )
    for changes in invalid_request_changes:
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            replace(request, **changes)
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        replace(_request(mode="metadata"), query=b"unexpected")
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        replace(_request(mode="semantic"), semantic_query=None)
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        replace(_request(mode="lexical"), semantic_query=SEMANTIC_QUERY)
    assert _request(mode="metadata").query_sha256 is None


def test_candidate_discovery_contract_matrix():
    first = _candidate("memory_contract_first")
    second = _candidate("memory_contract_second")
    first_record = _record(first)
    second_record = _record(second)
    indexes = _indexes("metadata", "lexical", "semantic", "git_graph")

    for changes in (
        {"candidate_sha256": "not-a-digest"},
        {"lexical_score": object()},
        {"semantic_score": float("inf")},
        {"evidence_graph_score": -1.0},
    ):
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            replace(first_record, **changes)

    invalid_result_changes = (
        {"records": [first_record]},
        {
            "records": (
                first_record,
                replace(second_record, memory_id=first_record.memory_id),
            )
        },
        {
            "records": (
                first_record,
                replace(second_record, candidate_sha256=first_record.candidate_sha256),
            )
        },
        {"index_versions": []},
        {"index_versions": ()},
        {"ancestry_relations": []},
        {"ancestry_relations": (("same", True), ("same", False))},
        {"ancestry_relations": (("anchor", 1),)},
        {"query_evidence_sha256": "not-a-digest"},
    )
    for changes in invalid_result_changes:
        values = {
            "records": (first_record, second_record),
            "index_versions": indexes,
            "ancestry_relations": (("def456", True),),
            "query_evidence_sha256": QUERY_EVIDENCE_SHA256,
            **changes,
        }
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            tbm.CandidateDiscoveryResult(**values)

    result = _result(records=(first_record,), index_versions=indexes)
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        result.prepared_context_sha256(object())


def _prepared_evidence() -> tbm.PreparedRetrievalEvidence:
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_prepared_contract")
    try:
        return (
            _service(
                authorization,
                _PolicyProvider(_policy()),
                _Discovery(
                    _result(
                        records=(_record(candidate),),
                        index_versions=_indexes(
                            "metadata",
                            "lexical",
                            "semantic",
                            "git_graph",
                        ),
                    )
                ),
                _Source((candidate,)),
            )
            .prepare(_context(registry), _request())
            .value
        )
    finally:
        repository.close()


def test_prepared_retrieval_evidence_rejects_invalid_linkage():
    evidence = _prepared_evidence()
    for changes in (
        {"snapshot": object()},
        {"system_gate_evaluation": object()},
        {"policy": object()},
        {"candidates": []},
        {"candidates": ()},
        {"policy": _policy(policy_version="retrieval_policy_other")},
    ):
        with pytest.raises((tbm.RetrievalPreparationV3Error, ValueError)):
            replace(evidence, **changes)


def test_retrieval_preparation_service_rejects_invalid_dependencies_and_inputs():
    registry = _registry(permissions=("memory:retrieve",))
    authorization, repository = _retrieval_authorization(registry)
    candidate = _candidate("memory_dependency")
    discovery = _Discovery(
        _result(
            records=(_record(candidate),),
            index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
        )
    )
    source = _Source((candidate,))
    valid = {
        "authorization_service": authorization,
        "policy_provider": _policy,
        "discovery": discovery,
        "revision_source": source,
        "clock": lambda: NOW,
        "evaluator_id": "system_gate",
        "evaluator_version": "v1",
    }
    try:
        for changes in (
            {"authorization_service": object()},
            {"policy_provider": object()},
            {"discovery": object()},
            {"revision_source": object()},
            {"clock": object()},
        ):
            with pytest.raises(TypeError):
                tbm.AuthenticatedRetrievalPreparationService(**{**valid, **changes})

        service = tbm.AuthenticatedRetrievalPreparationService(**valid)
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            service.prepare(object(), _request())
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            service.prepare(_context(registry), object())
    finally:
        repository.close()


class _FailingDiscovery:
    def __init__(self, value=None, error: Exception | None = None):
        self.value = value
        self.error = error

    def discover(self, *_args):
        if self.error is not None:
            raise self.error
        return self.value


class _FailingSource:
    def __init__(
        self,
        *,
        value=None,
        load_error: Exception | None = None,
        verify_error: Exception | None = None,
    ):
        self.value = value
        self.load_error = load_error
        self.verify_error = verify_error

    def load_authorized(self, _context, scope, **_kwargs):
        if self.load_error is not None:
            raise self.load_error
        if type(self.value) is tbm.ActivatedRevisionCandidate:
            return replace(
                self.value,
                retrieval_authorization_event_id=scope.authorization_event_id,
            )
        return self.value

    def verify_current(self, *_args):
        if self.verify_error is not None:
            raise self.verify_error


def test_retrieval_preparation_service_sanitizes_dependency_failures():
    candidate = _candidate("memory_dependency_failure")
    result = _result(
        records=(_record(candidate),),
        index_versions=_indexes("metadata", "lexical", "semantic", "git_graph"),
    )
    cases = (
        (
            lambda: (_ for _ in ()).throw(RuntimeError("private policy")),
            _FailingDiscovery(result),
            _FailingSource(value=candidate),
            lambda: NOW,
            "TBM_RETRIEVAL_PREPARATION_POLICY_UNAVAILABLE",
        ),
        (
            lambda: object(),
            _FailingDiscovery(result),
            _FailingSource(value=candidate),
            lambda: NOW,
            "TBM_RETRIEVAL_PREPARATION_POLICY_INVALID",
        ),
        (
            _policy,
            _FailingDiscovery(error=RuntimeError("private discovery")),
            _FailingSource(value=candidate),
            lambda: NOW,
            "TBM_RETRIEVAL_PREPARATION_DISCOVERY_FAILED",
        ),
        (
            _policy,
            _FailingDiscovery(object()),
            _FailingSource(value=candidate),
            lambda: NOW,
            "TBM_RETRIEVAL_PREPARATION_DISCOVERY_INVALID",
        ),
        (
            _policy,
            _FailingDiscovery(result),
            _FailingSource(load_error=RuntimeError("private revision")),
            lambda: NOW,
            "TBM_RETRIEVAL_PREPARATION_REVISION_FAILED",
        ),
        (
            _policy,
            _FailingDiscovery(result),
            _FailingSource(value=object()),
            lambda: NOW,
            "TBM_RETRIEVAL_PREPARATION_CANDIDATE_MISMATCH",
        ),
        (
            _policy,
            _FailingDiscovery(result),
            _FailingSource(value=candidate),
            lambda: (_ for _ in ()).throw(RuntimeError("private clock")),
            "TBM_RETRIEVAL_PREPARATION_CLOCK_FAILED",
        ),
        (
            _policy,
            _FailingDiscovery(result),
            _FailingSource(value=candidate),
            lambda: object(),
            "TBM_RETRIEVAL_PREPARATION_CLOCK_INVALID",
        ),
    )
    for policy_provider, discovery, source, clock, expected_code in cases:
        registry = _registry(permissions=("memory:retrieve",))
        authorization, repository = _retrieval_authorization(registry)
        try:
            service = tbm.AuthenticatedRetrievalPreparationService(
                authorization_service=authorization,
                policy_provider=policy_provider,
                discovery=discovery,
                revision_source=source,
                clock=clock,
                evaluator_id="system_gate",
                evaluator_version="v1",
            )
            with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
                service.prepare(_context(registry), _request())
            assert caught.value.code == expected_code
            assert "private" not in str(caught.value)
        finally:
            repository.close()


def test_retrieval_preparation_scope_stage_and_discovery_guards():
    context = _context(_registry(permissions=("memory:retrieve",)))
    scope = tbm.AuthorizedRetrievalScope(
        authorization_event_id=AUTHORIZATION_ID,
        organization_id="organization_001",
        principal_id=context.principal.principal_id,
        agent_client_id=context.agent_client.agent_client_id,
        tenant_id=context.tenant_id,
        repository_id="repository_001",
        environment_id=context.environment_id,
    )
    request = _request()
    policy = _policy()

    with pytest.raises(tbm.RetrievalPreparationV3Error):
        tbm.AuthenticatedRetrievalPreparationService._verify_scope(
            context, object(), request.context
        )
    with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
        tbm.AuthenticatedRetrievalPreparationService._verify_scope(
            context,
            replace(scope, repository_id="other_repository"),
            request.context,
        )
    assert caught.value.code == "TBM_RETRIEVAL_PREPARATION_SCOPE_MISMATCH"

    invalid_stage_cases = (
        (_record(_candidate("memory_metadata_score")), "metadata"),
        (
            tbm.CandidateIndexRecord(
                "memory_hybrid_empty",
                "sha256:" + "1" * 64,
            ),
            "hybrid",
        ),
        (
            tbm.CandidateIndexRecord(
                "memory_lexical_extra",
                "sha256:" + "2" * 64,
                lexical_score=0.5,
                semantic_score=0.5,
            ),
            "lexical",
        ),
    )
    for record, mode in invalid_stage_cases:
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            tbm.AuthenticatedRetrievalPreparationService._stage_scores(record, mode)

    candidate = _candidate("memory_discovery_guard")
    record = _record(candidate)
    discovery_cases = (
        _result(records=(record,), index_versions=_indexes("lexical", "git_graph")),
        _result(records=(record,), index_versions=_indexes("metadata", "git_graph")),
        _result(
            records=(record,),
            index_versions=_indexes("metadata", "lexical"),
            ancestry_relations=(),
        ),
        _result(
            records=(record,),
            index_versions=_indexes("metadata", "lexical"),
            ancestry_relations=(("def456", True),),
        ),
    )
    requests_and_policies = (
        (request, policy),
        (replace(request, retrieval_mode="semantic"), policy),
        (request, policy),
        (request, _policy(ancestry_mode="disabled")),
    )
    for discovery, (guard_request, guard_policy) in zip(
        discovery_cases,
        requests_and_policies,
        strict=True,
    ):
        with pytest.raises(tbm.RetrievalPreparationV3Error):
            tbm.AuthenticatedRetrievalPreparationService._verify_discovery(
                discovery,
                guard_request,
                guard_policy,
            )


def test_retrieval_preparation_unwrap_rejects_invalid_outcomes():
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        tbm.AuthenticatedRetrievalPreparationService._unwrap(object())
    with pytest.raises(tbm.RetrievalPreparationV3Error):
        tbm.AuthenticatedRetrievalPreparationService._unwrap(
            retrieval_preparation_v3._PreparationOutcome()
        )
    expected = tbm.RetrievalPreparationV3Error("expected", "expected")
    with pytest.raises(tbm.RetrievalPreparationV3Error) as caught:
        tbm.AuthenticatedRetrievalPreparationService._unwrap(
            retrieval_preparation_v3._PreparationOutcome(error=expected)
        )
    assert caught.value is expected
