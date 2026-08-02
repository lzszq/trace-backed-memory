from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from tests.test_completion_outbox_v3 import _completed_pair
from tests.test_sqlite_event_ledger_v1 import _connection as _ledger_connection
from trace_backed_memory.authorization_v3 import (
    AgentClientIdentity,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    PrincipalIdentity,
    RepositoryTenantBinding,
    RoleBinding,
    authorize,
)
from trace_backed_memory.contracts_v3 import (
    AuthorizationScope,
    CanonicalRepository,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.outcome_effect_event_v1 import (
    build_outcome_attribution_draft,
    build_outcome_effect_event_batch,
    build_run_outcome_draft,
)
from trace_backed_memory.outcome_harm_event_v1 import (
    OutcomeHarmEventV1Error,
    StoredOutcomeEvaluationContext,
    append_outcome_evaluation_contexts,
    build_outcome_evaluation_context,
    build_outcome_harm_event_batch,
    build_outcome_harm_event_registry,
    build_outcome_harm_policy,
    dumps_outcome_evaluation_context,
    dumps_outcome_harm_event_payload_dispatch_schema,
    loads_outcome_evaluation_context,
    outcome_evaluation_context_schema,
    rebuild_outcome_harm_from_ledger,
    reduce_outcome_harm_events,
)
from trace_backed_memory.outcome_v3 import build_outcome_attribution
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
ALL_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
TRUSTED_VERIFIERS = ("outcome_context_attestation_service",)
ROOT = Path(__file__).resolve().parents[1]


def _partition() -> LedgerTenantPartition:
    return LedgerTenantPartition(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
    )


def _source_access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=_partition(),
        principal_id="principal_001",
        agent_client_id="agent_001",
        actor_type="service",
        actor_id="service_outcome_effect_adapter",
        authorization_decision_id="authorization_outcome_effect_append",
        classification_filter=LedgerClassificationFilter(("internal",)),
    )


def _authorization():
    principal = PrincipalIdentity(
        principal_id="outcome_reviewer",
        issuer="https://identity.example.test",
        subject_hash=DIGEST_A,
        tenant_id="tenant_001",
        status="active",
    )
    client = AgentClientIdentity(
        agent_client_id="outcome_review_service",
        tenant_id="tenant_001",
        client_kind="service",
        status="active",
    )
    repository = CanonicalRepository(
        repository_id="repository_001",
        provider="local",
        provider_repository_id="provider_repository_001",
        canonical_locator_hash=DIGEST_A,
        display_name="Repository",
        legacy_aliases=(),
    )
    binding = RoleBinding(
        binding_id="binding_outcome_reviewer",
        principal_id=principal.principal_id,
        agent_client_id=client.agent_client_id,
        role_name="outcome_reviewer",
        scope=AuthorizationScope(
            kind="repository",
            tenant_id="tenant_001",
            repository_id="repository_001",
        ),
        permissions=("memory:verify",),
        status="active",
        valid_from="2026-07-27T00:00:00Z",
    )
    policy = AuthorizationPolicyBundle(
        policy_version="authorization_policy_outcome_001",
        principals=(principal,),
        agent_clients=(client,),
        repositories=(repository,),
        repository_tenants=(
            RepositoryTenantBinding(
                repository_id="repository_001", tenant_id="tenant_001"
            ),
        ),
        repository_aliases=(),
        role_bindings=(binding,),
    )
    request = AuthorizationRequest(
        request_id="request_outcome_context",
        principal_id=principal.principal_id,
        agent_client_id=client.agent_client_id,
        tenant_id="tenant_001",
        repository_reference="repository_001",
        permission="memory:verify",
        requested_at="2026-07-29T00:10:30Z",
    )
    decision = authorize(
        policy, request, decided_at="2026-07-29T00:10:31Z"
    )
    assert decision.allowed
    return policy, request, decision


def _context_access(decision, *, classifications=ALL_CLASSIFICATIONS):
    return LedgerAccessContext(
        partition=_partition(),
        principal_id="outcome_reviewer",
        agent_client_id="outcome_review_service",
        actor_type="principal",
        actor_id="outcome_reviewer",
        authorization_decision_id=decision.authorization_event_id,
        classification_filter=LedgerClassificationFilter(classifications),
    )


def _records():
    outcome, session = _completed_pair()
    association = build_outcome_attribution(
        run_outcome_id_value=outcome.run_outcome_id,
        usage_decision_id=outcome.usage_decision_id,
        memory_revision_ids=session.final_memory_revision_ids,
        claim_strength="association",
        effect="harmed",
        method="runtime_observation",
        evaluator_id="runtime_observer",
        evaluator_version="1.0.0",
        evidence_artifact_sha256s=(DIGEST_A,),
        confidence=0.95,
        reason="The revision was present when the regression was observed.",
        recorded_at="2026-07-29T00:10:00Z",
    )
    causal = build_outcome_attribution(
        run_outcome_id_value=outcome.run_outcome_id,
        usage_decision_id=outcome.usage_decision_id,
        memory_revision_ids=session.final_memory_revision_ids,
        claim_strength="causal",
        effect="harmed",
        method="controlled_experiment",
        evaluator_id="experiment_evaluator",
        evaluator_version="1.0.0",
        evidence_artifact_sha256s=(DIGEST_B,),
        confidence=0.95,
        reason="A controlled cohort reproduced the regression.",
        recorded_at="2026-07-29T00:10:01Z",
        verifier_id="independent_experiment_verifier",
    )
    policy, request, decision = _authorization()
    context = build_outcome_evaluation_context(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
        run_outcome_id=outcome.run_outcome_id,
        session_id=outcome.session_id,
        trace_id=outcome.trace_id,
        run_id=outcome.run_id,
        usage_decision_id=outcome.usage_decision_id,
        usage_decision_sha256=DIGEST_A,
        replay_manifest_sha256=DIGEST_B,
        retrieval_snapshot_sha256=DIGEST_C,
        injection_artifact_id="injection_artifact_001",
        memory_revision_ids=session.final_memory_revision_ids,
        evaluation_suite="regression_suite_001",
        evaluation_case="regression_case_001",
        experiment_id="experiment_001",
        cohort_id="cohort_with_memory_001",
        cohort_arm="with_memory",
        assignment_method="randomized",
        assignment_evidence_sha256=DIGEST_C,
        bound_by="outcome_reviewer",
        bound_via_client_id="outcome_review_service",
        authorization_event_id=decision.authorization_event_id,
        bound_at="2026-07-29T00:10:32Z",
    )
    stored = StoredOutcomeEvaluationContext(
        context=context,
        policy=policy,
        request=request,
        decision=decision,
        attestation_verified_by=TRUSTED_VERIFIERS[0],
    )
    return outcome, session, association, causal, stored


def _source_events(outcome, session, *attributions):
    drafts = [build_run_outcome_draft(outcome, session)]
    drafts.extend(
        build_outcome_attribution_draft(item, outcome, session)
        for item in attributions
    )
    return build_outcome_effect_event_batch(
        tuple(drafts),
        access=_source_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-07-29T01:00:00Z",
    )


def _write_sources(connection, outcome, session, *attributions):
    ledger = SQLiteEventLedgerV1(connection, _source_access())
    events, idempotency = _source_events(
        outcome, session, *attributions
    )
    ledger.append(events[0].stream_id, 0, events, idempotency)
    return events


def test_contract_registry_and_schema_are_deterministic():
    _, _, _, _, stored = _records()
    document = dumps_outcome_evaluation_context(stored.context)
    assert loads_outcome_evaluation_context(document) == stored.context
    assert build_outcome_harm_event_registry().sealed
    schema = outcome_evaluation_context_schema()
    Draft202012Validator.check_schema(schema)
    dispatch = json.loads(dumps_outcome_harm_event_payload_dispatch_schema())
    Draft202012Validator.check_schema(dispatch)


def test_packaged_outcome_harm_resources_match_canonical_bytes():
    expected = {
        "schemas/outcome_evaluation_context_v1.schema.json": json.dumps(
            outcome_evaluation_context_schema(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        "schemas/outcome_harm_event_payload_registry_v1.schema.json": (
            dumps_outcome_harm_event_payload_dispatch_schema()
        ),
    }
    for resource_name, document in expected.items():
        assert (ROOT / resource_name).read_text(encoding="utf-8") == document
        assert read_packaged_resource(resource_name) == document.encode("utf-8")
    example = json.loads(
        (ROOT / "examples/outcome_evaluation_context_v1.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert not list(
        Draft202012Validator(outcome_evaluation_context_schema()).iter_errors(
            example
        )
    )
    registry_example = json.loads(
        (
            ROOT
            / "examples/outcome_harm_event_type_registry_v1.example.json"
        ).read_text(encoding="utf-8")
    )
    assert registry_example == build_outcome_harm_event_registry().catalog()
    assert tbm.OutcomeHarmProjection.__module__.endswith("outcome_harm_event_v1")


def test_sqlite_rebuild_derives_all_five_outcome_harm_views():
    outcome, session, association, causal, stored = _records()
    connection = _ledger_connection()
    try:
        _write_sources(connection, outcome, session, association, causal)
        ledger = SQLiteEventLedgerV1(
            connection, _context_access(stored.decision)
        )
        result = append_outcome_evaluation_contexts(
            ledger,
            (stored,),
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            recorded_at="2026-07-29T01:00:01Z",
        )
        projection = result.snapshot.projection
        assert projection.evaluated_run_outcome_ids == (outcome.run_outcome_id,)
        assert projection.unevaluated_run_outcome_ids == ()
        assert projection.observed_associations[0].harmed_count == 1
        assert projection.experiment_cohorts[0].cohort_arm == "with_memory"
        assert projection.verified_causal_claims[0].effect == "harmed"
        assert len(projection.harmful_memory_signals) == 1
        assert projection.suspension_recommendations[0].action == "suspend"
        assert result.snapshot == rebuild_outcome_harm_from_ledger(
            ledger,
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
    finally:
        connection.close()


def test_unbound_attributions_are_retained_but_never_promoted():
    outcome, session, association, causal, _ = _records()
    events, _ = _source_events(outcome, session, association, causal)
    projection = reduce_outcome_harm_events(
        events,
        policy=build_outcome_harm_policy(),
        trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
    )
    assert len(projection.attributions) == 2
    assert projection.unevaluated_run_outcome_ids == (outcome.run_outcome_id,)
    assert projection.observed_associations == ()
    assert projection.verified_causal_claims == ()
    assert projection.harmful_memory_signals == ()


def test_without_memory_cohort_rejects_memory_attribution():
    outcome, session, association, _, stored = _records()
    context_values = stored.context._unsigned_dict()
    context_values.pop("contract_version")
    without = build_outcome_evaluation_context(
        **{
            **context_values,
            "memory_revision_ids": (),
            "cohort_id": "cohort_without_memory_001",
            "cohort_arm": "without_memory",
        }
    )
    forged = replace(stored, context=without)
    source_events, _ = _source_events(outcome, session, association)
    context_events, _ = build_outcome_harm_event_batch(
        _context_access(stored.decision),
        (forged,),
        expected_stream_version=0,
        next_global_position=len(source_events) + 1,
        previous_event=None,
        recorded_at="2026-07-29T01:00:01Z",
    )
    with pytest.raises(OutcomeHarmEventV1Error, match="without-memory"):
        reduce_outcome_harm_events(
            (*source_events, *context_events),
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )


def test_untrusted_context_attestation_fails_closed():
    outcome, session, _, _, stored = _records()
    source_events, _ = _source_events(outcome, session)
    context_events, _ = build_outcome_harm_event_batch(
        _context_access(stored.decision),
        (replace(stored, attestation_verified_by="untrusted_verifier"),),
        expected_stream_version=0,
        next_global_position=2,
        previous_event=None,
        recorded_at="2026-07-29T01:00:01Z",
    )
    with pytest.raises(OutcomeHarmEventV1Error, match="not trusted"):
        reduce_outcome_harm_events(
            (*source_events, *context_events),
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )


def test_rebuild_rejects_incomplete_classification_view():
    _, _, _, _, stored = _records()
    connection = _ledger_connection()
    try:
        narrowed = SQLiteEventLedgerV1(
            connection,
            _context_access(stored.decision, classifications=("internal",)),
        )
        with pytest.raises(OutcomeHarmEventV1Error, match="classification"):
            rebuild_outcome_harm_from_ledger(
                narrowed,
                policy=build_outcome_harm_policy(),
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            )
    finally:
        connection.close()


class _OneEventPageLedger:
    def __init__(self, delegate):
        self.delegate = delegate
        self.access_context = delegate.access_context

    def append(self, *args, **kwargs):
        return self.delegate.append(*args, **kwargs)

    def read_stream(self, *args, **kwargs):
        return self.delegate.read_stream(*args, **kwargs)

    def read_global(self, *, after_position=0, limit=100):
        return self.delegate.read_global(
            after_position=after_position, limit=min(limit, 1)
        )

    def verify_stream(self, *args, **kwargs):
        return self.delegate.verify_stream(*args, **kwargs)


def test_cross_page_rebuild_uses_exact_forward_cursor_without_duplicates():
    outcome, session, association, causal, stored = _records()
    connection = _ledger_connection()
    try:
        _write_sources(connection, outcome, session, association, causal)
        ledger = SQLiteEventLedgerV1(
            connection, _context_access(stored.decision)
        )
        append_outcome_evaluation_contexts(
            ledger,
            (stored,),
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            recorded_at="2026-07-29T01:00:01Z",
        )
        snapshot = rebuild_outcome_harm_from_ledger(
            _OneEventPageLedger(ledger),
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        positions = tuple(
            item.global_position for item in snapshot.projection.source_events
        )
        assert positions == tuple(sorted(set(positions)))
        assert len(positions) == 4
    finally:
        connection.close()
