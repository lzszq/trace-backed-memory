from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.cli as cli
from trace_backed_memory._timestamps import RFC3339_PATTERN


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-27T00:00:00Z"
HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64


def _contract_fixture(
    *,
    include_usage: bool = False,
) -> tuple[tbm.TraceBackedMemoryStore, tbm.SnapshotV3MigrationMapping]:
    store = tbm.TraceBackedMemoryStore()
    source = store.record_trace(
        tbm.Trace(
            trace_id="trace_failure",
            run_id="run_failure",
            commit_sha="commit_failure",
            repo="repo",
            tenant="tenant",
            eval_result="fail",
        )
    )
    regression = store.record_trace(
        tbm.Trace(
            trace_id="trace_regression",
            run_id="run_regression",
            commit_sha="commit_fix",
            repo="repo",
            tenant="tenant",
            eval_suite="suite",
            eval_result="pass",
        )
    )
    case = store.add_failure_case(
        tbm.verify_failure_case(
            tbm.review_failure_case(
                tbm.draft_failure_case(
                    source,
                    case_id="case",
                    failure_type="tool_contract",
                    symptom="query was empty",
                ),
                reviewed_by="reviewer",
                root_cause="the caller omitted the required query",
                reviewed_at=NOW,
            ),
            fix="require a non-empty query",
            fix_commit_sha="commit_fix",
            regression_passed=True,
        )
    )
    store.add_lesson(
        tbm.lesson_from_failure_case(
            case,
            lesson_id="lesson",
            lesson_text="Always provide a non-empty query.",
            memory_type="procedural",
            scope={
                "repo": "repo",
                "tenant": "tenant",
                "tool": "search_docs",
            },
        )
    )
    store.add_project_policy(
        tbm.ProjectPolicy(
            policy_id="policy",
            policy_text="Use repository tools only for the current task.",
            scope={"tool": "search_docs"},
        )
    )

    bindings = [
        tbm.TraceIdentityBinding(
            trace_id=source.trace_id,
            repository_id="repository",
            tenant_id="tenant_id",
        ),
        tbm.TraceIdentityBinding(
            trace_id=regression.trace_id,
            repository_id="repository",
            tenant_id="tenant_id",
        ),
    ]
    if include_usage:
        current = store.record_trace(
            tbm.Trace(
                trace_id="trace_current",
                run_id="run_current",
                commit_sha="commit_fix",
                repo="repo",
                tenant="tenant",
                eval_result="unknown",
                tool_calls=[{"name": "search_docs"}],
            )
        )
        request = store.prepare_memory(
            tbm.MemoryContext(
                mode="repair",
                repo="repo",
                tenant="tenant",
                commit_sha="commit_fix",
                tool="search_docs",
            ),
            task="repair search",
            trace_id=current.trace_id,
        )
        store.finalize_memory(
            request,
            {
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "memory is not needed",
                "risk": "none",
                "recommended_injection": "none",
            },
            trace_id=current.trace_id,
        )
        bindings.append(
            tbm.TraceIdentityBinding(
                trace_id=current.trace_id,
                repository_id="repository",
                tenant_id="tenant_id",
            )
        )

    repository = tbm.CanonicalRepository(
        repository_id="repository",
        provider="github",
        provider_repository_id="123",
        canonical_locator_hash=HASH_1,
        display_name="repo",
        legacy_aliases=("repo",),
    )
    tenant = tbm.TenantIdentity(
        tenant_id="tenant_id",
        display_name="tenant",
        legacy_aliases=("tenant",),
    )
    source_to_fix = tbm.CommitRelationEvidence(
        from_commit_sha="commit_failure",
        to_commit_sha="commit_fix",
        relation="ancestor",
        verified_by="ci",
        verified_at=NOW,
    )
    fix_to_regression = tbm.CommitRelationEvidence(
        from_commit_sha="commit_fix",
        to_commit_sha="commit_fix",
        relation="ancestor",
        verified_by="ci",
        verified_at=NOW,
    )
    evidence = tbm.RegressionEvidence(
        evidence_id="evidence",
        case_id="case",
        regression_trace_id="trace_regression",
        regression_run_id="run_regression",
        evaluator_id="evaluator",
        evaluator_version="1",
        eval_suite="suite",
        eval_case_id="case",
        evaluated_commit_sha="commit_fix",
        result="pass",
        observed_at=NOW,
        source_to_fix=source_to_fix,
        fix_to_regression=fix_to_regression,
        artifact_hashes=(HASH_2,),
    )
    mapping = tbm.SnapshotV3MigrationMapping(
        repositories=(repository,),
        tenants=(tenant,),
        trace_bindings=tuple(bindings),
        memory_scopes=(
            tbm.MemoryScopeBinding(
                memory_kind="lesson",
                memory_id="lesson",
                scope=tbm.AuthorizationScope(
                    kind="repository",
                    tenant_id="tenant_id",
                    repository_id="repository",
                    attributes=(("tool", "search_docs"),),
                ),
            ),
            tbm.MemoryScopeBinding(
                memory_kind="project_policy",
                memory_id="policy",
                scope=tbm.AuthorizationScope(
                    kind="global",
                    attributes=(("tool", "search_docs"),),
                ),
            ),
        ),
        regression_evidence=(evidence,),
        global_policy_approvals=(
            tbm.GlobalPolicyApproval(
                policy_id="policy",
                principal_id="policy_admin",
                authorization_event_id="authorization_event",
                approved_at=NOW,
                reason="approved through the privileged workflow",
            ),
        ),
        ancestry_policy=tbm.AncestryPolicy(mode="required"),
    )
    return store, mapping


def _issue_codes(plan: tbm.SnapshotV3MigrationPlan) -> set[str]:
    return {issue.code for issue in plan.issues}


def _trusted_relation(
    repository_id: str,
    relation: tbm.CommitRelationEvidence,
) -> bool:
    return (
        repository_id == "repository"
        and relation.relation == "ancestor"
    )


def _plan(source, mapping) -> tbm.SnapshotV3MigrationPlan:
    return tbm.plan_snapshot_v3_migration(
        source,
        mapping,
        commit_relation_verifier=_trusted_relation,
    )


def _plan_counts(
    *,
    errors: int = 0,
    warnings: int = 0,
) -> tuple[tuple[str, int], ...]:
    return (
        ("errors", errors),
        ("failure_cases", 0),
        ("lessons", 0),
        ("mapped_scopes", 0),
        ("project_policies", 0),
        ("regression_evidence", 0),
        ("repositories", 0),
        ("tenants", 0),
        ("traces", 0),
        ("usage_logs", 0),
        ("warnings", warnings),
    )


def test_v3_mapping_round_trip_and_ready_plan_are_deterministic():
    store, mapping = _contract_fixture()

    parsed = tbm.parse_v3_migration_mapping(mapping.to_dict())
    first = _plan(store, parsed)
    second = _plan(
        store.to_snapshot(),
        mapping.to_dict(),
    )

    assert parsed == mapping
    assert first == second
    assert first.ready is True
    assert first.issues == ()
    assert first.plan_version == "tbm.snapshot.v2-to-v3.plan.v1"
    assert first.source_snapshot_version == 2
    assert first.target_snapshot_version == 3
    assert first.to_dict()["counts"] == {
        "errors": 0,
        "failure_cases": 1,
        "lessons": 1,
        "mapped_scopes": 2,
        "project_policies": 1,
        "regression_evidence": 1,
        "repositories": 1,
        "tenants": 1,
        "traces": 2,
        "usage_logs": 0,
        "warnings": 0,
    }
    assert first.source_snapshot_sha256.startswith("sha256:")
    assert first.mapping_sha256 == tbm.canonical_sha256(mapping.to_dict())


def test_v3_mapping_digest_is_stable_for_semantically_unordered_fields():
    store, mapping = _contract_fixture()
    repository = replace(
        mapping.repositories[0],
        legacy_aliases=("repo_alias", "repo"),
    )
    tenant = replace(
        mapping.tenants[0],
        legacy_aliases=("tenant_alias", "tenant"),
    )
    lesson_scope = replace(
        mapping.memory_scopes[0],
        scope=replace(
            mapping.memory_scopes[0].scope,
            attributes=(
                ("tool", "search_docs"),
                ("task_type", "repair"),
            ),
        ),
    )
    evidence = replace(
        mapping.regression_evidence[0],
        artifact_hashes=(HASH_2, HASH_1),
    )
    first_mapping = replace(
        mapping,
        repositories=(repository,),
        tenants=(tenant,),
        trace_bindings=tuple(reversed(mapping.trace_bindings)),
        memory_scopes=(
            mapping.memory_scopes[1],
            lesson_scope,
        ),
        regression_evidence=(evidence,),
    )
    second_mapping = replace(
        first_mapping,
        repositories=(
            replace(repository, legacy_aliases=tuple(reversed(repository.legacy_aliases))),
        ),
        tenants=(
            replace(tenant, legacy_aliases=tuple(reversed(tenant.legacy_aliases))),
        ),
        trace_bindings=tuple(reversed(first_mapping.trace_bindings)),
        memory_scopes=tuple(reversed(first_mapping.memory_scopes)),
        regression_evidence=(
            replace(
                evidence,
                artifact_hashes=tuple(reversed(evidence.artifact_hashes)),
            ),
        ),
    )
    second_scope = second_mapping.memory_scopes[0]
    second_mapping = replace(
        second_mapping,
        memory_scopes=(
            replace(
                second_scope,
                scope=replace(
                    second_scope.scope,
                    attributes=tuple(
                        reversed(second_scope.scope.attributes)
                    ),
                ),
            ),
            second_mapping.memory_scopes[1],
        ),
    )

    first = _plan(store, first_mapping)
    second = _plan(store, second_mapping)

    assert first.mapping_sha256 == second.mapping_sha256
    assert tbm.canonical_sha256(first_mapping.to_dict()) == (
        tbm.canonical_sha256(second_mapping.to_dict())
    )


def test_v3_usage_logs_require_trace_binding_and_remain_legacy_partial():
    store, mapping = _contract_fixture(include_usage=True)

    ready = _plan(store, mapping)
    assert ready.ready is True
    assert _issue_codes(ready) == {"TBM_V3_LEGACY_REPLAY_PARTIAL"}
    assert ready.to_dict()["counts"]["warnings"] == 1

    without_current_binding = replace(
        mapping,
        trace_bindings=mapping.trace_bindings[:-1],
    )
    blocked = _plan(
        store,
        without_current_binding,
    )
    assert blocked.ready is False
    assert {
        "TBM_V3_TRACE_IDENTITY_REQUIRED",
        "TBM_V3_USAGE_TRACE_BINDING_REQUIRED",
        "TBM_V3_LEGACY_REPLAY_PARTIAL",
    }.issubset(_issue_codes(blocked))


def test_v3_preflight_fails_closed_when_explicit_bindings_are_missing():
    store, mapping = _contract_fixture()
    empty = replace(
        mapping,
        trace_bindings=(),
        memory_scopes=(),
        regression_evidence=(),
        global_policy_approvals=(),
    )

    plan = _plan(store, empty)

    assert plan.ready is False
    assert _issue_codes(plan) == {
        "TBM_V3_EXPLICIT_SCOPE_REQUIRED",
        "TBM_V3_STRUCTURED_REGRESSION_REQUIRED",
        "TBM_V3_TRACE_IDENTITY_REQUIRED",
        "TBM_V3_UNUSED_GLOBAL_APPROVAL",
    } - {"TBM_V3_UNUSED_GLOBAL_APPROVAL"}
    assert plan.to_dict()["counts"]["errors"] == 5


def test_v3_identity_aliases_and_registry_references_are_verified():
    store, mapping = _contract_fixture()
    unknown = replace(
        mapping,
        repositories=(
            replace(mapping.repositories[0], legacy_aliases=("other",)),
        ),
        tenants=(
            replace(mapping.tenants[0], legacy_aliases=("other",)),
        ),
        trace_bindings=(
            replace(
                mapping.trace_bindings[0],
                repository_id="unknown_repository",
                tenant_id="unknown_tenant",
            ),
            *mapping.trace_bindings[1:],
            tbm.TraceIdentityBinding(
                trace_id="unknown_trace",
                repository_id="repository",
                tenant_id="tenant_id",
            ),
        ),
    )

    plan = _plan(store, unknown)

    assert {
        "TBM_V3_UNKNOWN_REPOSITORY",
        "TBM_V3_UNKNOWN_TENANT",
        "TBM_V3_REPOSITORY_ALIAS_MISMATCH",
        "TBM_V3_TENANT_ALIAS_MISMATCH",
        "TBM_V3_UNKNOWN_TRACE_BINDING",
    }.issubset(_issue_codes(plan))


def test_v3_explicit_binding_can_repair_missing_legacy_trace_names():
    store, mapping = _contract_fixture()
    snapshot = store.to_snapshot()
    snapshot["traces"] = [
        {
            **trace,
            "repo": None,
            "tenant": None,
        }
        for trace in snapshot["traces"]
    ]
    repaired_store = tbm.TraceBackedMemoryStore.from_snapshot(snapshot)

    plan = _plan(repaired_store, mapping)

    assert plan.ready is True
    assert _issue_codes(plan) == {
        "TBM_V3_LEGACY_REPO_MISSING",
        "TBM_V3_LEGACY_TENANT_MISSING",
    }


def test_v3_scope_preflight_blocks_broadening_global_lessons_and_unknowns():
    store, mapping = _contract_fixture()
    global_lesson = replace(
        mapping.memory_scopes[0],
        scope=tbm.AuthorizationScope(
            kind="global",
            attributes=(),
        ),
    )
    unknown_policy_scope = tbm.MemoryScopeBinding(
        memory_kind="project_policy",
        memory_id="unknown_policy",
        scope=tbm.AuthorizationScope(kind="global"),
    )
    modified = replace(
        mapping,
        memory_scopes=(
            global_lesson,
            replace(
                mapping.memory_scopes[1],
                scope=tbm.AuthorizationScope(
                    kind="repository",
                    tenant_id="unknown_tenant",
                    repository_id="unknown_repository",
                ),
            ),
            unknown_policy_scope,
        ),
        global_policy_approvals=(),
    )

    plan = _plan(store, modified)

    assert {
        "TBM_V3_APPLICABILITY_BROADENED",
        "TBM_V3_GLOBAL_LESSON_FORBIDDEN",
        "TBM_V3_LESSON_SOURCE_TENANT_MISMATCH",
        "TBM_V3_SCOPE_REPOSITORY_BROADENED",
        "TBM_V3_SCOPE_TENANT_BROADENED",
        "TBM_V3_UNKNOWN_REPOSITORY",
        "TBM_V3_UNKNOWN_SCOPE_BINDING",
        "TBM_V3_UNKNOWN_TENANT",
        "TBM_V3_UNUSED_GLOBAL_APPROVAL",
    } - {"TBM_V3_UNUSED_GLOBAL_APPROVAL"} <= _issue_codes(plan)


def test_v3_global_policy_requires_approval_and_unused_approval_is_reported():
    store, mapping = _contract_fixture()
    missing = replace(mapping, global_policy_approvals=())
    plan = _plan(store, missing)
    assert "TBM_V3_GLOBAL_POLICY_APPROVAL_REQUIRED" in _issue_codes(plan)

    tenant_policy = replace(
        mapping,
        memory_scopes=(
            mapping.memory_scopes[0],
            replace(
                mapping.memory_scopes[1],
                scope=tbm.AuthorizationScope(
                    kind="tenant",
                    tenant_id="tenant_id",
                    attributes=(("tool", "search_docs"),),
                ),
            ),
        ),
    )
    plan = _plan(store, tenant_policy)
    assert "TBM_V3_UNUSED_GLOBAL_APPROVAL" in _issue_codes(plan)
    assert plan.ready is True


def test_v3_lesson_scope_must_match_source_trace_identity():
    store, mapping = _contract_fixture()
    other_repository = tbm.CanonicalRepository(
        repository_id="repository_other",
        provider="github",
        provider_repository_id="456",
        canonical_locator_hash="sha256:" + "3" * 64,
        display_name="other",
        legacy_aliases=("other",),
    )
    other_tenant = tbm.TenantIdentity(
        tenant_id="tenant_other",
        display_name="other",
        legacy_aliases=("other",),
    )
    lesson_scope = replace(
        mapping.memory_scopes[0],
        scope=tbm.AuthorizationScope(
            kind="repository",
            tenant_id="tenant_other",
            repository_id="repository_other",
            attributes=(("tool", "search_docs"),),
        ),
    )
    modified = replace(
        mapping,
        repositories=(*mapping.repositories, other_repository),
        tenants=(*mapping.tenants, other_tenant),
        memory_scopes=(lesson_scope, mapping.memory_scopes[1]),
    )

    plan = _plan(store, modified)

    assert {
        "TBM_V3_SCOPE_REPOSITORY_BROADENED",
        "TBM_V3_SCOPE_TENANT_BROADENED",
        "TBM_V3_LESSON_SOURCE_TENANT_MISMATCH",
        "TBM_V3_LESSON_SOURCE_REPOSITORY_MISMATCH",
    }.issubset(_issue_codes(plan))


def test_v3_regression_evidence_binds_case_trace_commits_and_identity():
    store, mapping = _contract_fixture()
    evidence = mapping.regression_evidence[0]
    bad = replace(
        evidence,
        result="fail",
        regression_trace_id="missing_trace",
        source_to_fix=replace(
            evidence.source_to_fix,
            from_commit_sha="wrong_source",
        ),
            fix_to_regression=replace(
                evidence.fix_to_regression,
                to_commit_sha="wrong_evaluated",
                relation="ancestor",
            ),
    )
    modified = replace(mapping, regression_evidence=(bad,))

    plan = _plan(store, modified)

    assert {
        "TBM_V3_REGRESSION_NOT_PASSING",
        "TBM_V3_SOURCE_FIX_RELATION_MISMATCH",
        "TBM_V3_FIX_REGRESSION_RELATION_MISMATCH",
        "TBM_V3_REGRESSION_TRACE_REQUIRED",
    }.issubset(_issue_codes(plan))


def test_v3_regression_trace_fields_and_identity_must_match():
    store, mapping = _contract_fixture()
    evidence = replace(
        mapping.regression_evidence[0],
        regression_run_id="wrong_run",
        eval_suite="wrong_suite",
    )
    binding = replace(
        mapping.trace_bindings[1],
        tenant_id="tenant_other",
    )
    other_tenant = tbm.TenantIdentity(
        tenant_id="tenant_other",
        display_name="other",
        legacy_aliases=("other",),
    )
    modified = replace(
        mapping,
        tenants=(*mapping.tenants, other_tenant),
        trace_bindings=(mapping.trace_bindings[0], binding),
        regression_evidence=(evidence,),
    )

    plan = _plan(store, modified)

    assert {
        "TBM_V3_TENANT_ALIAS_MISMATCH",
        "TBM_V3_REGRESSION_TRACE_MISMATCH",
        "TBM_V3_REGRESSION_IDENTITY_MISMATCH",
    }.issubset(_issue_codes(plan))


def test_v3_unknown_and_unused_regression_evidence_are_reported():
    store, mapping = _contract_fixture()
    unused = replace(
        mapping.regression_evidence[0],
        evidence_id="unused",
        case_id="unknown_case",
    )
    modified = replace(
        mapping,
        regression_evidence=(*mapping.regression_evidence, unused),
    )

    plan = _plan(store, modified)
    assert "TBM_V3_UNKNOWN_REGRESSION_CASE" in _issue_codes(plan)

    draft_store = tbm.TraceBackedMemoryStore()
    trace = draft_store.record_trace(
        tbm.Trace(
            trace_id="trace_failure",
            run_id="run_failure",
            commit_sha="commit_failure",
            repo="repo",
            tenant="tenant",
            eval_result="fail",
        )
    )
    draft_store.add_failure_case(
        tbm.draft_failure_case(
            trace,
            case_id="case",
            failure_type="tool_contract",
            symptom="query was empty",
        )
    )
    draft_mapping = replace(
        mapping,
        trace_bindings=(mapping.trace_bindings[0],),
        memory_scopes=(),
        global_policy_approvals=(),
    )
    plan = _plan(draft_store, draft_mapping)
    assert "TBM_V3_UNUSED_REGRESSION_EVIDENCE" in _issue_codes(plan)


@pytest.mark.parametrize(
    ("constructor", "match"),
    [
        (
            lambda: tbm.CanonicalRepository(
                "repo",
                "unsupported",
                "1",
                HASH_1,
                "repo",
            ),
            "provider",
        ),
        (
            lambda: tbm.CanonicalRepository(
                "repo",
                "github",
                "1",
                "bad",
                "repo",
            ),
            "sha256",
        ),
        (
            lambda: tbm.TenantIdentity(
                "tenant",
                "tenant",
                ("same", "same"),
            ),
            "duplicates",
        ),
        (
            lambda: tbm.AuthorizationScope(
                "global",
                tenant_id="tenant",
            ),
            "global scope",
        ),
        (
            lambda: tbm.AuthorizationScope("tenant"),
            "tenant scope",
        ),
        (
            lambda: tbm.AuthorizationScope(
                "repository",
                tenant_id="tenant",
            ),
            "repository scope",
        ),
        (
            lambda: tbm.AuthorizationScope(
                "repository",
                tenant_id="tenant",
                repository_id="repo",
                attributes=(("repo", "legacy"),),
            ),
            "unsupported applicability",
        ),
        (
            lambda: tbm.CommitRelationEvidence(
                "a",
                "b",
                "unsupported",
                "ci",
                NOW,
            ),
            "commit relation",
        ),
        (
            lambda: tbm.CommitRelationEvidence(
                "a",
                "b",
                "ancestor",
                "ci",
                "not-a-time",
            ),
            "RFC 3339",
        ),
        (
            lambda: tbm.AncestryPolicy("required", "not allowed"),
            "cannot declare",
        ),
        (
            lambda: tbm.AncestryPolicy("disabled"),
            "bypass_reason",
        ),
    ],
)
def test_v3_value_objects_fail_closed(constructor, match):
    with pytest.raises(tbm.V3ContractError, match=match) as error:
        constructor()
    assert error.value.code == "TBM_V3_INVALID_CONTRACT"


def test_v3_regression_and_approval_value_objects_reject_malformed_fields():
    _, mapping = _contract_fixture()
    evidence = mapping.regression_evidence[0]

    with pytest.raises(tbm.V3ContractError, match="regression result"):
        replace(evidence, result="unknown")
    with pytest.raises(tbm.V3ContractError, match="duplicates"):
        replace(evidence, artifact_hashes=(HASH_1, HASH_1))
    with pytest.raises(tbm.V3ContractError, match="sha256"):
        replace(evidence, artifact_hashes=("bad",))
    with pytest.raises(tbm.V3ContractError, match="sha256"):
        replace(evidence, artifact_hashes=([],))
    with pytest.raises(tbm.V3ContractError, match="source_to_fix"):
        replace(evidence, source_to_fix=None)
    with pytest.raises(tbm.V3ContractError, match="reason"):
        replace(mapping.global_policy_approvals[0], reason=" ")


def test_v3_mapping_rejects_ambiguous_or_duplicate_operator_input():
    _, mapping = _contract_fixture()
    with pytest.raises(tbm.V3ContractError, match="mapping_version"):
        replace(mapping, mapping_version="wrong")
    with pytest.raises(tbm.V3ContractError, match="repository_id"):
        replace(
            mapping,
            repositories=(
                mapping.repositories[0],
                replace(
                    mapping.repositories[0],
                    provider_repository_id="other",
                    canonical_locator_hash="sha256:" + "3" * 64,
                ),
            ),
        )
    with pytest.raises(tbm.V3ContractError, match="legacy aliases"):
        replace(
            mapping,
            repositories=(
                mapping.repositories[0],
                tbm.CanonicalRepository(
                    "other",
                    "github",
                    "other",
                    "sha256:" + "3" * 64,
                    "other",
                    ("repo",),
                ),
            ),
        )
    with pytest.raises(tbm.V3ContractError, match="canonical identifiers"):
        replace(
            mapping,
            repositories=(
                mapping.repositories[0],
                tbm.CanonicalRepository(
                    "other",
                    "github",
                    "other",
                    "sha256:" + "3" * 64,
                    "other",
                    ("repository",),
                ),
            ),
        )
    with pytest.raises(tbm.V3ContractError, match="canonical identifiers"):
        replace(
            mapping,
            tenants=(
                mapping.tenants[0],
                tbm.TenantIdentity(
                    "other",
                    "other",
                    ("tenant_id",),
                ),
            ),
        )
    with pytest.raises(tbm.V3ContractError, match="trace_id"):
        replace(
            mapping,
            trace_bindings=(
                mapping.trace_bindings[0],
                mapping.trace_bindings[0],
            ),
        )
    with pytest.raises(tbm.V3ContractError, match="memory_kind/memory_id"):
        replace(
            mapping,
            memory_scopes=(
                mapping.memory_scopes[0],
                mapping.memory_scopes[0],
            ),
        )


def test_v3_parser_rejects_unknown_missing_and_wrong_json_shapes():
    _, mapping = _contract_fixture()
    payload = mapping.to_dict()

    with pytest.raises(tbm.V3ContractError, match="must be an object"):
        tbm.parse_v3_migration_mapping([])
    with pytest.raises(tbm.V3ContractError, match="missing required"):
        tbm.parse_v3_migration_mapping(
            {key: value for key, value in payload.items() if key != "tenants"}
        )
    with pytest.raises(tbm.V3ContractError, match="unknown field"):
        tbm.parse_v3_migration_mapping({**payload, "unknown": True})
    with pytest.raises(tbm.V3ContractError, match="must be an array"):
        tbm.parse_v3_migration_mapping({**payload, "repositories": {}})
    with pytest.raises(tbm.V3ContractError, match="must be a string"):
        tbm.parse_v3_migration_mapping({**payload, "mapping_version": 3})
    with pytest.raises(tbm.V3ContractError, match="field names"):
        tbm.parse_v3_migration_mapping({**payload, 3: "invalid"})

    repository = dict(payload["repositories"][0])
    repository["unknown"] = True
    with pytest.raises(tbm.V3ContractError, match="unknown field"):
        tbm.parse_v3_migration_mapping(
            {**payload, "repositories": [repository]}
        )


def test_v3_hashing_is_canonical_algorithm_tagged_and_finite():
    assert tbm.canonical_sha256({"b": 2, "a": 1}) == tbm.canonical_sha256(
        {"a": 1, "b": 2}
    )
    assert len(tbm.canonical_sha256({})) == len("sha256:") + 64

    with pytest.raises(tbm.V3ContractError) as error:
        tbm.canonical_sha256({"value": float("nan")})
    assert error.value.code == "TBM_V3_NON_CANONICAL_JSON"
    with pytest.raises(tbm.V3ContractError):
        tbm.canonical_sha256({"value": "\ud800"})


def test_v3_plan_record_validates_ready_counts_and_issue_consistency():
    issue = tbm.V3MigrationIssue(
        code="TBM_V3_TEST",
        severity="error",
        record_kind="trace",
        record_id="trace",
        message="test blocker",
    )
    with pytest.raises(tbm.V3ContractError, match="ready"):
        tbm.SnapshotV3MigrationPlan(
            source_snapshot_sha256=HASH_1,
            mapping_sha256=HASH_2,
            ready=True,
            counts=_plan_counts(errors=1),
            issues=(issue,),
        )
    with pytest.raises(tbm.V3ContractError, match="duplicate"):
        tbm.SnapshotV3MigrationPlan(
            source_snapshot_sha256=HASH_1,
            mapping_sha256=HASH_2,
            ready=False,
            counts=(*_plan_counts(errors=1), ("errors", 1)),
            issues=(issue,),
        )
    with pytest.raises(tbm.V3ContractError, match="complete"):
        tbm.SnapshotV3MigrationPlan(
            source_snapshot_sha256=HASH_1,
            mapping_sha256=HASH_2,
            ready=False,
            counts=(("errors", 1),),
            issues=(issue,),
        )
    with pytest.raises(tbm.V3ContractError, match="must match"):
        tbm.SnapshotV3MigrationPlan(
            source_snapshot_sha256=HASH_1,
            mapping_sha256=HASH_2,
            ready=False,
            counts=_plan_counts(errors=2),
            issues=(issue,),
        )
    with pytest.raises(tbm.V3ContractError, match="severity"):
        replace(issue, severity=[])


def test_v3_planner_rejects_invalid_source_and_mapping_types():
    store, _ = _contract_fixture()
    with pytest.raises(tbm.V3ContractError) as error:
        tbm.plan_snapshot_v3_migration([], {})
    assert error.value.code == "TBM_V3_INVALID_SOURCE"

    with pytest.raises(tbm.V3ContractError) as error:
        tbm.plan_snapshot_v3_migration(store, [])
    assert error.value.code == "TBM_V3_INVALID_MAPPING"


def test_v3_planner_requires_explicit_v2_and_wraps_invalid_snapshots():
    store, mapping = _contract_fixture()
    legacy = dict(store.to_snapshot())
    del legacy["snapshot_version"]

    with pytest.raises(tbm.V3ContractError) as error:
        _plan(legacy, mapping)
    assert error.value.code == "TBM_V3_SOURCE_VERSION_MISMATCH"

    with pytest.raises(tbm.V3ContractError) as error:
        _plan({"snapshot_version": 2}, mapping)
    assert error.value.code == "TBM_V3_INVALID_SOURCE_SNAPSHOT"
    assert str(error.value) == "source snapshot failed strict version-2 validation"


def test_v3_planner_freezes_a_store_into_one_source_snapshot(monkeypatch):
    store, mapping = _contract_fixture()
    original = store.to_snapshot
    calls = 0

    def capture_once():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(store, "to_snapshot", capture_once)

    plan = _plan(store, mapping)

    assert plan.ready is True
    assert calls == 1


def test_v3_planner_hashes_the_frozen_mapping_source_not_later_mutation():
    store, mapping = _contract_fixture()
    initial = store.to_snapshot()
    expected_hash = tbm.canonical_sha256(initial)

    class MutatingSnapshot(dict):
        def items(self):
            for item in dict.items(self):
                yield item
            self["traces"].reverse()

    source = MutatingSnapshot(
        json.loads(json.dumps(initial))
    )

    plan = _plan(source, mapping)

    assert source["traces"] != initial["traces"]
    assert plan.source_snapshot_sha256 == expected_hash


def test_v3_required_ancestry_uses_a_trusted_verifier_and_fails_closed():
    store, mapping = _contract_fixture()

    without_verifier = tbm.plan_snapshot_v3_migration(store, mapping)
    assert without_verifier.ready is False
    assert "TBM_V3_ANCESTRY_VERIFIER_REQUIRED" in _issue_codes(
        without_verifier
    )

    rejected = tbm.plan_snapshot_v3_migration(
        store,
        mapping,
        commit_relation_verifier=lambda _repository_id, _relation: False,
    )
    assert rejected.ready is False
    assert "TBM_V3_COMMIT_RELATION_UNVERIFIED" in _issue_codes(rejected)

    failed = tbm.plan_snapshot_v3_migration(
        store,
        mapping,
        commit_relation_verifier=lambda _repository_id, _relation: 1 / 0,
    )
    assert failed.ready is False
    assert "TBM_V3_ANCESTRY_VERIFICATION_FAILED" in _issue_codes(failed)

    invalid = tbm.plan_snapshot_v3_migration(
        store,
        mapping,
        commit_relation_verifier=lambda _repository_id, _relation: 1,
    )
    assert invalid.ready is False
    assert "TBM_V3_ANCESTRY_VERIFICATION_FAILED" in _issue_codes(invalid)


def test_v3_disabled_ancestry_requires_and_reports_audited_bypass():
    store, mapping = _contract_fixture()
    disabled = replace(
        mapping,
        ancestry_policy=tbm.AncestryPolicy(
            mode="disabled",
            bypass_reason="trusted Git objects are unavailable during preflight",
        ),
    )

    plan = tbm.plan_snapshot_v3_migration(store, disabled)

    assert plan.ready is True
    assert _issue_codes(plan) == {"TBM_V3_ANCESTRY_DISABLED"}


def test_v3_cli_preflight_is_read_only_and_deterministic(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, mapping = _contract_fixture()
    snapshot_path = tmp_path / "snapshot.json"
    mapping_path = tmp_path / "mapping.json"
    store.save_json(snapshot_path)
    before = snapshot_path.read_bytes()
    mapping_before = json.dumps(
        mapping.to_dict(),
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    mapping_path.write_bytes(mapping_before)
    ancestry_calls = []

    def capture(current, anchors, repo_path):
        ancestry_calls.append((current, tuple(anchors), repo_path))
        return tbm.CommitAncestryEvidence(
            current_commit_sha=current,
            commit_relations=tuple((anchor, True) for anchor in anchors),
        )

    monkeypatch.setattr(cli, "capture_commit_ancestry", capture)
    command = [
        "migration",
        "plan-v3",
        str(snapshot_path),
        str(mapping_path),
        "--repository-root",
        f"repository={tmp_path}",
    ]

    assert cli.main(command) == 0
    first_output = capsys.readouterr().out
    assert cli.main(command) == 0
    second_output = capsys.readouterr().out

    payload = json.loads(first_output)
    assert payload["ready"] is True
    assert payload["target_snapshot_version"] == 3
    assert first_output == second_output
    assert snapshot_path.read_bytes() == before
    assert mapping_path.read_bytes() == mapping_before
    assert not cli._snapshot_lock_path(snapshot_path).exists()
    assert len(ancestry_calls) == 4


def test_v3_cli_rejects_invalid_unicode_as_input(tmp_path, capsys):
    store, mapping = _contract_fixture()
    snapshot_path = tmp_path / "snapshot.json"
    mapping_path = tmp_path / "mapping.json"
    store.save_json(snapshot_path)
    payload = mapping.to_dict()
    payload["repositories"][0]["display_name"] = "\ud800"
    mapping_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "migration",
                "plan-v3",
                str(snapshot_path),
                str(mapping_path),
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["kind"] == "input"
    assert "invalid Unicode string" in error["message"]


def test_v3_cli_rejects_duplicate_and_unknown_mapping_fields(
    tmp_path,
    capsys,
):
    store, mapping = _contract_fixture()
    snapshot_path = tmp_path / "snapshot.json"
    mapping_path = tmp_path / "mapping.json"
    store.save_json(snapshot_path)
    canonical = json.dumps(mapping.to_dict(), sort_keys=True)
    mapping_path.write_text(
        '{"mapping_version":"duplicate","mapping_version":'
        + canonical.split('"mapping_version":', 1)[1],
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "migration",
                "plan-v3",
                str(snapshot_path),
                str(mapping_path),
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["kind"] == "input"
    assert "duplicate object key" in error["message"]

    payload = mapping.to_dict()
    payload["unknown"] = True
    mapping_path.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        cli.main(
            [
                "migration",
                "plan-v3",
                str(snapshot_path),
                str(mapping_path),
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "TBM_V3_INVALID_CONTRACT"
    assert "unknown field" in error["message"]


def test_v3_schema_and_examples_publish_the_same_strict_contract():
    mapping_schema = json.loads(
        (
            ROOT / "schemas" / "snapshot_v3_migration_mapping.schema.json"
        ).read_text(encoding="utf-8")
    )
    plan_schema = json.loads(
        (
            ROOT / "schemas" / "snapshot_v3_migration_plan.schema.json"
        ).read_text(encoding="utf-8")
    )
    mapping_example = json.loads(
        (
            ROOT / "examples" / "snapshot_v3_migration_mapping.example.json"
        ).read_text(encoding="utf-8")
    )
    plan_example = json.loads(
        (
            ROOT / "examples" / "snapshot_v3_migration_plan.example.json"
        ).read_text(encoding="utf-8")
    )

    assert mapping_schema["additionalProperties"] is False
    assert plan_schema["additionalProperties"] is False
    assert (
        mapping_schema["$defs"]["timestamp"]["pattern"]
        == f"^{RFC3339_PATTERN}$"
    )
    assert (
        mapping_schema["$defs"]["commitRelation"]["properties"]["relation"]
        == {"const": "ancestor"}
    )
    assert len(plan_schema["allOf"]) == 2
    assert (
        mapping_schema["properties"]["mapping_version"]["const"]
        == tbm.V3_MIGRATION_MAPPING_VERSION
    )
    assert (
        plan_schema["properties"]["plan_version"]["const"]
        == tbm.V3_MIGRATION_PLAN_VERSION
    )
    assert tbm.parse_v3_migration_mapping(mapping_example).to_dict() == (
        mapping_example
    )
    assert plan_example["source_snapshot_version"] == 2
    assert plan_example["target_snapshot_version"] == 3
    assert plan_example["ready"] is True


def test_v3_public_contract_exports_are_intentional():
    assert tbm.V3_CONTRACT_VERSION == "tbm.trust.v3"
    assert tbm.V3_SOURCE_SNAPSHOT_VERSION == 2
    assert tbm.V3_TARGET_SNAPSHOT_VERSION == 3
    for name in (
        "AncestryPolicy",
        "AuthorizationScope",
        "CanonicalRepository",
        "CommitRelationEvidence",
        "CommitRelationVerifier",
        "GlobalPolicyApproval",
        "MemoryScopeBinding",
        "RegressionEvidence",
        "SnapshotV3MigrationMapping",
        "SnapshotV3MigrationPlan",
        "TenantIdentity",
        "TraceIdentityBinding",
        "V3ContractError",
        "V3MigrationIssue",
        "canonical_sha256",
        "parse_v3_migration_mapping",
        "parse_v3_migration_plan",
        "plan_snapshot_v3_migration",
    ):
        assert name in tbm.__all__
