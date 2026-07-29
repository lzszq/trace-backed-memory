from __future__ import annotations

from dataclasses import replace
import json

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
import trace_backed_memory.managed_index_v3 as managed_index_v3
from tests.test_artifact_service_v3 import _context, _registry
from tests.test_retrieval_preparation_v3 import (
    AUTHORIZATION_ID,
    NOW,
    QUERY,
    SEMANTIC_QUERY,
    _candidate,
    _policy,
    _retrieval_authorization,
)


def _build_input(
    *memory_ids: str,
    vector: tuple[float, ...] = (0.6, 0.8),
    retriever_version: str = "v1",
) -> tbm.ManagedIndexBuildInput:
    sources = tuple(
        tbm.ManagedIndexSource(
            _candidate(memory_id),
            index_text=f"repair cache {memory_id}",
            semantic_vector=vector,
        )
        for memory_id in memory_ids
    )
    evidence_edges = (
        (
            tbm.ManagedEvidenceEdge(
                "repair",
                sources[0].candidate.revision.fix_evidence_id,
                1.0,
            ),
        )
        if sources
        else ()
    )
    return tbm.ManagedIndexBuildInput(
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
        retriever_id="reference_retriever",
        retriever_version=retriever_version,
        sources=sources,
        semantic_provider_id="reference_embeddings",
        semantic_provider_version="v1",
        evidence_edges=evidence_edges,
        git_commits=("def456", "fedcba"),
        git_edges=(tbm.ManagedGitEdge("fedcba", "def456"),),
    )


def _bundle(
    *memory_ids: str,
    vector: tuple[float, ...] = (0.6, 0.8),
    retriever_version: str = "v1",
) -> tbm.ManagedIndexBundle:
    return tbm.build_managed_index_bundle(
        _build_input(
            *(memory_ids or ("memory_managed",)),
            vector=vector,
            retriever_version=retriever_version,
        )
    )


def _request(
    mode: tbm.retrieval_v3.RetrievalMode,
    *,
    semantic_query: tbm.SemanticQueryVector | None = None,
) -> tbm.RetrievalPreparationRequest:
    return tbm.RetrievalPreparationRequest(
        session_id="session_001",
        request_id="request_001",
        trace_id="trace_001",
        run_id="run_001",
        context=tbm.RetrievalPreparationContext(
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
        top_k=10,
        query=None if mode == "metadata" else QUERY,
        semantic_query=semantic_query,
    )


def _scope() -> tbm.AuthorizedRetrievalScope:
    return tbm.AuthorizedRetrievalScope(
        authorization_event_id=AUTHORIZATION_ID,
        principal_id="principal_001",
        agent_client_id="client_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
    )


def test_managed_index_bundle_is_deterministic_and_exactly_replayable():
    first = _bundle("memory_b", "memory_a")
    second = _bundle("memory_a", "memory_b")

    assert first == second
    assert tuple(item.memory_id for item in first.candidates) == (
        "memory_a",
        "memory_b",
    )
    assert tuple(item.index_kind for item in first.index_versions) == (
        "metadata",
        "lexical",
        "semantic",
        "evidence_graph",
        "git_graph",
    )
    payload = tbm.dumps_managed_index_bundle(first)
    assert tbm.loads_managed_index_bundle(payload) == first
    assert (
        tbm.managed_index_bundle_id(
            {key: value for key, value in first.to_dict().items() if key != "bundle_id"}
        )
        == first.bundle_id
    )


@pytest.mark.parametrize(
    ("mode", "expected_kinds"),
    (
        ("metadata", ("metadata", "git_graph")),
        ("lexical", ("metadata", "lexical", "git_graph")),
        (
            "semantic",
            ("metadata", "semantic", "git_graph"),
        ),
        (
            "evidence_graph",
            ("metadata", "evidence_graph", "git_graph"),
        ),
        (
            "hybrid",
            (
                "metadata",
                "lexical",
                "semantic",
                "evidence_graph",
                "git_graph",
            ),
        ),
    ),
)
def test_managed_discovery_uses_published_indexes(
    mode,
    expected_kinds,
):
    bundle = _bundle()
    request = _request(
        mode,
        semantic_query=(SEMANTIC_QUERY if mode in {"semantic", "hybrid"} else None),
    )
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(bundle, expected_current_bundle_id=None)
        result = tbm.ManagedIndexDiscovery(repository).discover(
            _context(_registry(permissions=("memory:retrieve",))),
            _scope(),
            request,
            _policy(),
        )

    assert tuple(item.index_kind for item in result.index_versions) == expected_kinds
    assert tuple(item.memory_id for item in result.records) == ("memory_managed",)
    assert result.ancestry_relations == (("def456", True),)
    if mode in {"semantic", "hybrid"}:
        assert request.query_sha256 is not None
        assert result.query_evidence_sha256 == (
            SEMANTIC_QUERY.evidence_sha256(request.query_sha256)
        )
        assert result.records[0].semantic_score == pytest.approx(1.0)
    else:
        assert result.query_evidence_sha256 is None


def test_managed_discovery_integrates_after_durable_authorization():
    bundle = _bundle()
    registry = _registry(permissions=("memory:retrieve",))
    authorization, decisions = _retrieval_authorization(registry)
    source_candidate = _candidate("memory_managed")
    source = type(
        "_Source",
        (),
        {
            "load_authorized": lambda _self, _context, scope, *, memory_id: replace(
                source_candidate,
                retrieval_authorization_event_id=(scope.authorization_event_id),
            ),
            "verify_current": lambda _self, _scope, _candidate: None,
        },
    )()
    try:
        with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
            repository.publish(bundle, expected_current_bundle_id=None)
            prepared = tbm.AuthenticatedRetrievalPreparationService(
                authorization_service=authorization,
                policy_provider=_policy,
                discovery=tbm.ManagedIndexDiscovery(repository),
                revision_source=source,
                clock=lambda: NOW,
                evaluator_id="system_gate",
                evaluator_version="v1",
            ).prepare(
                _context(registry),
                _request("hybrid", semantic_query=SEMANTIC_QUERY),
            )
        assert prepared.value.allowed_revision_ids == (
            source_candidate.revision.revision_id,
        )
        assert (
            len(decisions.list_decisions(registry.authorization_policy.policy_sha256))
            == 1
        )
    finally:
        decisions.close()


def test_managed_semantic_vectors_are_stable_at_numeric_extremes():
    tiny = _bundle(vector=(5e-324, 5e-324))
    huge = _bundle(vector=(1e308, 1e308))

    for bundle in (tiny, huge):
        vector = bundle.candidates[0].semantic_vector
        assert vector is not None
        assert sum(item * item for item in vector) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "semantic_query",
    (
        tbm.SemanticQueryVector(
            "other_provider",
            "v1",
            (0.6, 0.8),
        ),
        tbm.SemanticQueryVector(
            "reference_embeddings",
            "v2",
            (0.6, 0.8),
        ),
        tbm.SemanticQueryVector(
            "reference_embeddings",
            "v1",
            (1.0,),
        ),
    ),
)
def test_managed_discovery_rejects_semantic_evidence_mismatch(
    semantic_query,
):
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(_bundle(), expected_current_bundle_id=None)
        with pytest.raises(tbm.ManagedIndexV3Error) as caught:
            tbm.ManagedIndexDiscovery(repository).discover(
                _context(_registry(permissions=("memory:retrieve",))),
                _scope(),
                _request("semantic", semantic_query=semantic_query),
                _policy(),
            )
    assert caught.value.code == "TBM_MANAGED_INDEX_QUERY_UNAVAILABLE"


def test_managed_discovery_rejects_forged_authorized_scope():
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(_bundle(), expected_current_bundle_id=None)
        with pytest.raises(tbm.ManagedIndexV3Error) as caught:
            tbm.ManagedIndexDiscovery(repository).discover(
                _context(_registry(permissions=("memory:retrieve",))),
                replace(_scope(), agent_client_id="forged_client"),
                _request("metadata"),
                _policy(),
            )
    assert caught.value.code == "TBM_MANAGED_INDEX_SCOPE_MISMATCH"


def test_sensitive_candidates_cannot_enter_content_derived_indexes():
    candidate = _candidate("memory_sensitive")
    revision = candidate.revision
    assert revision.fix_evidence_id is not None
    assert candidate.fix_evidence is not None
    values = {
        "memory_id": revision.memory_id,
        "memory_revision_id": revision.revision_id,
        "candidate_sha256": candidate.candidate_sha256,
        "memory_kind": revision.memory_kind,
        "memory_type": revision.memory_type,
        "classification": "confidential",
        "scope_attributes": revision.scope.attributes,
        "eval_leaking": revision.eval_leaking,
        "evidence_ids": tuple(
            sorted(
                (
                    revision.fix_evidence_id,
                    *revision.regression_evidence_ids,
                )
            )
        ),
        "git_anchor_commit_sha": candidate.fix_evidence.fix_commit_sha,
    }

    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.ManagedIndexCandidate(
            **values,
            lexical_tokens=("secret",),
            semantic_vector=None,
        )
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.ManagedIndexCandidate(
            **values,
            lexical_tokens=(),
            semantic_vector=(1.0,),
        )
    sensitive = tbm.ManagedIndexCandidate(
        **values,
        lexical_tokens=(),
        semantic_vector=None,
    )
    public = _bundle().candidates[0]
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        replace(
            _bundle(),
            candidates=tuple(
                sorted((public, sensitive), key=lambda item: item.memory_id)
            ),
            evidence_edges=(
                tbm.ManagedEvidenceEdge(
                    "secret",
                    sensitive.evidence_ids[0],
                    1.0,
                ),
            ),
        )


def test_managed_candidates_enforce_per_candidate_collection_bounds():
    candidate = _bundle().candidates[0]

    with pytest.raises(tbm.ManagedIndexV3ContractError):
        replace(
            candidate,
            scope_attributes=tuple(
                (f"attribute_{index}", "value") for index in range(65)
            ),
        )
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        replace(
            candidate,
            evidence_ids=tuple(f"evidence_{index:04d}" for index in range(4_097)),
        )


def test_managed_index_rejects_invalid_graphs_and_evidence():
    build_input = _build_input("memory_graph")

    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(
            replace(
                build_input,
                git_edges=(
                    tbm.ManagedGitEdge("def456", "fedcba"),
                    tbm.ManagedGitEdge("fedcba", "def456"),
                ),
            )
        )
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(
            replace(
                build_input,
                evidence_edges=(
                    tbm.ManagedEvidenceEdge(
                        "repair",
                        "unknown_evidence",
                        1.0,
                    ),
                ),
            )
        )


def test_managed_index_json_rejects_tamper_duplicates_and_nonfinite():
    bundle = _bundle()
    payload = tbm.dumps_managed_index_bundle(bundle)
    tampered = json.loads(payload)
    tampered["retriever_version"] = "v2"

    with pytest.raises(tbm.ManagedIndexV3ContractError) as caught:
        tbm.loads_managed_index_bundle(json.dumps(tampered, separators=(",", ":")))
    assert caught.value.code == "TBM_MANAGED_INDEX_HASH_MISMATCH"
    with pytest.raises(ValueError):
        tbm.loads_managed_index_bundle('{"bundle_id":"x","bundle_id":"y"}')
    with pytest.raises(ValueError):
        tbm.loads_managed_index_bundle('{"value":NaN}')
    oversized_number = json.loads(payload)
    oversized_number["candidates"][0]["semantic_vector"] = [10**400]
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.loads_managed_index_bundle(
            json.dumps(oversized_number, separators=(",", ":"))
        )


def test_managed_index_enforces_serialized_byte_bound(monkeypatch):
    bundle = _bundle()
    monkeypatch.setattr(
        managed_index_v3,
        "MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES",
        1,
    )

    with pytest.raises(tbm.ManagedIndexV3ContractError) as caught:
        tbm.dumps_managed_index_bundle(bundle)
    assert caught.value.code == "TBM_MANAGED_INDEX_INVALID"
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        _bundle()


def test_managed_index_schema_and_example_match_the_runtime_contract():
    schema = json.loads(
        tbm.read_packaged_resource("schemas/managed_index_bundle_v3.schema.json")
    )
    example_bytes = tbm.read_packaged_resource(
        "examples/managed_index_bundle_v3.example.json"
    )
    example = json.loads(example_bytes)

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)
    sensitive = json.loads(json.dumps(example))
    sensitive["candidates"][0]["classification"] = "confidential"
    assert tuple(Draft202012Validator(schema).iter_errors(sensitive))
    for changes in (
        {
            "evidence_ids": [],
            "git_anchor_commit_sha": None,
        },
        {"memory_kind": "project_policy"},
        {"lexical_tokens": ["not canonical"]},
        {"lexical_tokens": ["!!!"]},
        {"lexical_tokens": ["1"]},
        {"lexical_tokens": ["É"]},
        {"lexical_tokens": ["foo-bar"]},
        {"semantic_vector": [0.0, 0.0]},
    ):
        invalid = json.loads(json.dumps(example))
        invalid["candidates"][0].update(changes)
        assert tuple(Draft202012Validator(schema).iter_errors(invalid))
    blank_identifier = json.loads(json.dumps(example))
    blank_identifier["retriever_id"] = "   "
    assert tuple(Draft202012Validator(schema).iter_errors(blank_identifier))
    blank_attribute = json.loads(json.dumps(example))
    blank_attribute["candidates"][0]["scope_attributes"] = {"   ": "value"}
    assert tuple(Draft202012Validator(schema).iter_errors(blank_attribute))
    assert tbm.loads_managed_index_bundle(example_bytes) == _bundle()


def test_managed_index_contracts_reject_invalid_component_shapes():
    candidate = _bundle().candidates[0]
    build_input = _build_input("memory_contract")

    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.ManagedIndexSource(object())
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.ManagedGitEdge("same", "same")

    invalid_candidate_changes = (
        {"memory_revision_id": "not-a-revision"},
        {"candidate_sha256": "not-a-digest"},
        {"memory_kind": "unsupported"},
        {"memory_type": "unsupported"},
        {"classification": "unsupported"},
        {"scope_attributes": []},
        {"eval_leaking": 1},
        {"lexical_tokens": ["repair"]},
        {"semantic_vector": ()},
        {"evidence_ids": list(candidate.evidence_ids)},
        {"git_anchor_commit_sha": ""},
        {"evidence_ids": ()},
        {"git_anchor_commit_sha": None},
        {"memory_kind": "project_policy"},
    )
    for changes in invalid_candidate_changes:
        with pytest.raises(tbm.ManagedIndexV3ContractError):
            replace(candidate, **changes)

    for changes in (
        {"sources": []},
        {"evidence_edges": []},
        {"git_edges": []},
        {"git_commits": []},
    ):
        with pytest.raises(tbm.ManagedIndexV3ContractError):
            replace(build_input, **changes)


def test_managed_index_bundle_rejects_invalid_collections_graphs_and_hashes():
    bundle = _bundle()
    candidate = bundle.candidates[0]
    evidence_id = candidate.evidence_ids[0]
    extra_edge = tbm.ManagedEvidenceEdge("cache", evidence_id, 0.5)

    invalid_bundle_changes = (
        {"contract_version": "unsupported"},
        {"bundle_id": "not-a-bundle-id"},
        {"semantic_metric": "dot"},
        {"semantic_dimension": -1},
        {"candidates": list(bundle.candidates)},
        {"semantic_dimension": bundle.semantic_dimension + 1},
        {"evidence_edges": list(bundle.evidence_edges)},
        {"evidence_edges": (extra_edge, bundle.evidence_edges[0])},
        {"evidence_edges": (bundle.evidence_edges[0], bundle.evidence_edges[0])},
        {"git_edges": list(bundle.git_edges)},
        {"git_edges": (bundle.git_edges[0], bundle.git_edges[0])},
        {"git_edges": (tbm.ManagedGitEdge("unknown", "def456"),)},
        {"candidates": (replace(candidate, git_anchor_commit_sha="unknown"),)},
        {"source_catalog_sha256": "sha256:" + "0" * 64},
        {"index_versions": bundle.index_versions[:-1]},
        {"bundle_id": "managed_index_bundle_sha256_" + "0" * 64},
    )
    for changes in invalid_bundle_changes:
        with pytest.raises(tbm.ManagedIndexV3ContractError):
            replace(bundle, **changes)

    with pytest.raises(AssertionError):
        bundle.index_version("unsupported")

    for changes in (
        {"bundle": object()},
        {"previous_bundle_id": "invalid"},
        {"head_version": 0},
        {"changed": 1},
    ):
        values = {
            "bundle": bundle,
            "previous_bundle_id": None,
            "head_version": 1,
            "changed": True,
            **changes,
        }
        with pytest.raises(tbm.ManagedIndexV3ContractError):
            tbm.ManagedIndexPublication(**values)


def test_managed_index_build_rejects_scope_dimensions_and_duplicate_edges(
    monkeypatch,
):
    first = tbm.ManagedIndexSource(
        _candidate("memory_dimension_first"),
        index_text="repair cache",
        semantic_vector=(1.0, 0.0),
    )
    second = tbm.ManagedIndexSource(
        _candidate("memory_dimension_second"),
        index_text="repair cache",
        semantic_vector=(1.0, 0.0, 0.0),
    )
    build_input = replace(
        _build_input("memory_dimension_first"),
        sources=(first, second),
        evidence_edges=(),
    )

    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(object())
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(
            replace(_build_input("memory_scope"), tenant_id="tenant_other")
        )
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(build_input)

    duplicate_evidence = _build_input("memory_duplicate_evidence")
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(
            replace(
                duplicate_evidence,
                evidence_edges=(
                    duplicate_evidence.evidence_edges[0],
                    duplicate_evidence.evidence_edges[0],
                ),
            )
        )
    duplicate_git = _build_input("memory_duplicate_git")
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(
            replace(
                duplicate_git,
                git_edges=(duplicate_git.git_edges[0], duplicate_git.git_edges[0]),
            )
        )

    monkeypatch.setattr(managed_index_v3, "MANAGED_INDEX_MAX_TOKENS_PER_CANDIDATE", 0)
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.build_managed_index_bundle(_build_input("memory_token_bound"))


class _ManagedRepositoryStub:
    def __init__(self, value=None, error: Exception | None = None):
        self.value = value
        self.error = error

    def publish(self, *_args, **_kwargs):
        raise AssertionError("not used")

    def load(self, *_args, **_kwargs):
        raise AssertionError("not used")

    def load_current(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.value


def test_managed_discovery_rejects_invalid_dependencies_inputs_and_loads():
    context = _context(_registry(permissions=("memory:retrieve",)))
    scope = _scope()
    request = _request("metadata")
    policy = _policy()

    with pytest.raises(TypeError):
        tbm.ManagedIndexDiscovery(object())

    discovery = tbm.ManagedIndexDiscovery(_ManagedRepositoryStub(_bundle()))
    for arguments in (
        (object(), scope, request, policy),
        (context, object(), request, policy),
        (context, scope, object(), policy),
        (context, scope, request, object()),
    ):
        with pytest.raises(tbm.ManagedIndexV3Error) as caught:
            discovery.discover(*arguments)
        assert caught.value.code == "TBM_MANAGED_INDEX_QUERY_INVALID"

    expected = tbm.ManagedIndexV3Error("expected", "expected")
    with pytest.raises(tbm.ManagedIndexV3Error) as caught:
        tbm.ManagedIndexDiscovery(_ManagedRepositoryStub(error=expected)).discover(
            context, scope, request, policy
        )
    assert caught.value is expected

    with pytest.raises(tbm.ManagedIndexV3Error) as caught:
        tbm.ManagedIndexDiscovery(
            _ManagedRepositoryStub(error=RuntimeError("private"))
        ).discover(context, scope, request, policy)
    assert caught.value.code == "TBM_MANAGED_INDEX_UNAVAILABLE"
    assert "private" not in str(caught.value)

    with pytest.raises(tbm.ManagedIndexV3Error) as caught:
        tbm.ManagedIndexDiscovery(_ManagedRepositoryStub(object())).discover(
            context, scope, request, policy
        )
    assert caught.value.code == "TBM_MANAGED_INDEX_INVALID"

    with pytest.raises(tbm.ManagedIndexV3Error) as caught:
        tbm.ManagedIndexDiscovery(
            _ManagedRepositoryStub(_bundle(retriever_version="v2"))
        ).discover(context, scope, request, policy)
    assert caught.value.code == "TBM_MANAGED_INDEX_SCOPE_MISMATCH"

    invalid_query = replace(_request("lexical"), query=b"\xff")
    with pytest.raises(tbm.ManagedIndexV3Error) as caught:
        discovery.discover(context, scope, invalid_query, policy)
    assert caught.value.code == "TBM_MANAGED_INDEX_QUERY_INVALID"


@pytest.mark.parametrize(
    ("mode", "query", "semantic_query"),
    (
        ("lexical", b"unmatched", None),
        ("evidence_graph", b"unmatched", None),
        ("hybrid", b"unmatched", None),
    ),
)
def test_managed_discovery_omits_candidates_without_requested_scores(
    mode,
    query,
    semantic_query,
):
    bundle = _bundle()
    request = replace(
        _request(mode, semantic_query=semantic_query),
        query=query,
    )
    result = tbm.ManagedIndexDiscovery(_ManagedRepositoryStub(bundle)).discover(
        _context(_registry(permissions=("memory:retrieve",))),
        _scope(),
        request,
        _policy(ancestry_mode="disabled"),
    )
    assert result.records == ()
    assert all(item.index_kind != "git_graph" for item in result.index_versions)


def test_managed_discovery_enforces_candidate_and_git_bounds(monkeypatch):
    discovery = tbm.ManagedIndexDiscovery(_ManagedRepositoryStub(_bundle()))
    context = _context(_registry(permissions=("memory:retrieve",)))

    monkeypatch.setattr(managed_index_v3, "MANAGED_INDEX_MAX_CANDIDATES", 0)
    with pytest.raises(tbm.ManagedIndexV3Error) as caught:
        discovery.discover(context, _scope(), _request("metadata"), _policy())
    assert caught.value.code == "TBM_MANAGED_INDEX_BOUNDS"

    monkeypatch.setattr(managed_index_v3, "MANAGED_INDEX_MAX_CANDIDATES", 1_000)
    unknown_commit = replace(
        _request("metadata"),
        context=replace(_request("metadata").context, commit_sha="unknown"),
    )
    with pytest.raises(tbm.ManagedIndexV3Error) as caught:
        discovery.discover(context, _scope(), unknown_commit, _policy())
    assert caught.value.code == "TBM_MANAGED_INDEX_QUERY_UNAVAILABLE"


def test_managed_index_json_rejects_invalid_external_shapes(monkeypatch):
    bundle = _bundle()
    payload = json.loads(tbm.dumps_managed_index_bundle(bundle))

    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.dumps_managed_index_bundle(object())
    for invalid in (1, b"\xff", "[]", "{}"):
        with pytest.raises(tbm.ManagedIndexV3ContractError):
            tbm.loads_managed_index_bundle(invalid)

    collections = json.loads(json.dumps(payload))
    collections["candidates"] = {}
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.loads_managed_index_bundle(json.dumps(collections))

    monkeypatch.setattr(managed_index_v3, "MANAGED_INDEX_MAX_CANDIDATES", 0)
    with pytest.raises(tbm.ManagedIndexV3ContractError):
        tbm.loads_managed_index_bundle(json.dumps(payload))
    monkeypatch.setattr(managed_index_v3, "MANAGED_INDEX_MAX_CANDIDATES", 1_000)

    mutations = (
        ("candidates", 0, {"unexpected": True}),
        ("candidates", 0, {"scope_attributes": []}),
        ("evidence_edges", 0, {"unexpected": True}),
        ("git_edges", 0, {"unexpected": True}),
        ("index_versions", 0, {"unexpected": True}),
    )
    for collection, index, replacement_value in mutations:
        invalid = json.loads(json.dumps(payload))
        if set(replacement_value) == {"unexpected"}:
            invalid[collection][index] = replacement_value
        else:
            invalid[collection][index].update(replacement_value)
        with pytest.raises(tbm.ManagedIndexV3ContractError):
            tbm.loads_managed_index_bundle(json.dumps(invalid))


def test_managed_index_scalar_helpers_reject_noncanonical_values():
    candidate = _bundle().candidates[0]
    for factory in (
        lambda: tbm.ManagedEvidenceEdge("!", candidate.evidence_ids[0], 1.0),
        lambda: tbm.ManagedEvidenceEdge("repair", candidate.evidence_ids[0], object()),
        lambda: tbm.ManagedEvidenceEdge("repair", candidate.evidence_ids[0], 2.0),
        lambda: replace(candidate, lexical_tokens=("Repair",)),
        lambda: replace(candidate, lexical_tokens=("repair", "repair")),
        lambda: replace(candidate, semantic_vector=(object(),)),
        lambda: replace(candidate, semantic_vector=(float("inf"),)),
        lambda: replace(candidate, semantic_vector=(0.0, 0.0)),
        lambda: replace(candidate, memory_id=" "),
        lambda: replace(candidate, memory_id="\ud800"),
    ):
        with pytest.raises(tbm.ManagedIndexV3ContractError):
            factory()
