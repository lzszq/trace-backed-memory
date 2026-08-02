from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm

from tests.test_artifact_service_v3 import _context
from tests.test_activated_revision_v3 import _published_source
from tests.test_memory_publication_v3 import (
    ACTIVATED_AT,
    CONTENT,
    DIGEST,
    _approval,
    _authorization,
)
from tests.test_memory_revision_v3 import _evidence
from tests.test_sqlite_event_ledger_v1 import _connection as _ledger_connection
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.activated_revision_v3 import ActivatedRevisionV3Error
from trace_backed_memory.contracts_v3 import canonical_sha256
from trace_backed_memory.event_v1 import EventTrustedContext, build_canonical_event
from trace_backed_memory.memory_catalog_event_v1 import (
    MEMORY_REVISION_APPROVED,
    ActivatedMemoryHead,
    EventActivatedMemoryHeadSource,
    LegacyLessonCompatibilityProjection,
    MemoryCatalogEventV1Error,
    MemoryCatalogEvidenceRecord,
    build_memory_catalog_event_batch,
    build_memory_catalog_event_registry,
    append_memory_catalog_records,
    build_memory_catalog_reducer,
    build_memory_revision_counterexample,
    build_memory_revision_relationship,
    build_memory_revision_review,
    build_memory_revision_state_change,
    loads_memory_revision_review,
    memory_catalog_event_payload_dispatch_schema,
    project_legacy_lesson,
    rebuild_memory_catalog,
    rebuild_memory_catalog_from_ledger,
    reduce_memory_catalog_events,
)
from trace_backed_memory.memory_publication_v3 import (
    MemoryRevisionApproval,
    StoredMemoryRevisionActivationPublication,
    StoredMemoryRevisionApprovalPublication,
    activate_memory_revision,
    approve_memory_revision,
    memory_revision_approval_id,
)
from trace_backed_memory.memory_revision_v3 import build_memory_revision
from trace_backed_memory.models import Lesson
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.resources import read_packaged_resource


TRUSTED_VERIFIERS = ("attestation_verifier",)
ROOT = Path(__file__).resolve().parents[1]


def _reduce(events):
    return reduce_memory_catalog_events(
        events, trusted_attestation_verifier_ids=TRUSTED_VERIFIERS
    )


def _rebuild(streams):
    return rebuild_memory_catalog(
        streams, trusted_attestation_verifier_ids=TRUSTED_VERIFIERS
    )


def _access(actor_id: str, authorization_id: str = "authorization_catalog"):
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id=actor_id,
        agent_client_id="catalog_service",
        actor_type="principal",
        actor_id=actor_id,
        authorization_decision_id=authorization_id,
        classification_filter=LedgerClassificationFilter(("internal",)),
    )


def _records_and_publication():
    (
        approval,
        revision,
        fixes,
        regressions,
        policy,
        approval_request,
        approval_decision,
    ) = _approval()
    activation_request, activation_decision = _authorization(
        policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )
    activation = activate_memory_revision(
        revision=revision,
        approval=approval,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=policy,
        approval_request=approval_request,
        approval_decision=approval_decision,
        previous_activation=None,
        policy=policy,
        request=activation_request,
        decision=activation_decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at=ACTIVATED_AT,
        activation_attestation_sha256=DIGEST,
    )
    review = build_memory_revision_review(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=revision.memory_id,
        revision_id=revision.revision_id,
        decision="accepted",
        reviewed_by="catalog_reviewer",
        reviewed_at="2026-07-27T00:06:30Z",
        rationale_sha256=DIGEST,
        review_attestation_sha256=DIGEST,
    )
    approval_record = StoredMemoryRevisionApprovalPublication(
        approval=approval,
        policy=policy,
        request=approval_request,
        decision=approval_decision,
        attestation_verified_by="attestation_verifier",
    )
    activation_record = StoredMemoryRevisionActivationPublication(
        activation=activation,
        policy=policy,
        request=activation_request,
        decision=activation_decision,
        attestation_verified_by="attestation_verifier",
    )
    records = (
        revision,
        MemoryCatalogEvidenceRecord(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=revision.memory_id,
            revision_id=revision.revision_id,
            evidence=next(iter(fixes.values())),
        ),
        MemoryCatalogEvidenceRecord(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=revision.memory_id,
            revision_id=revision.revision_id,
            evidence=next(iter(regressions.values())),
        ),
        review,
        approval_record,
        activation_record,
    )
    authorization_ids = (
        "authorization_proposal",
        "authorization_fix_evidence",
        "authorization_regression_evidence",
        "authorization_review",
        approval.authorization_event_id,
        activation.authorization_event_id,
    )
    return records, authorization_ids


def _event_stream(records, authorization_ids):
    events = []
    parent = None
    for record, authorization_id in zip(records, authorization_ids, strict=True):
        actor_id = (
            record.proposed_by
            if hasattr(record, "proposed_by")
            else record.actor_id
            if isinstance(record, MemoryCatalogEvidenceRecord)
            else record.reviewed_by
            if hasattr(record, "reviewed_by")
            else record.approval.approved_by
            if isinstance(record, StoredMemoryRevisionApprovalPublication)
            else record.activation.activated_by
            if isinstance(record, StoredMemoryRevisionActivationPublication)
            else record.approved_by
            if hasattr(record, "approved_by")
            else record.activated_by
            if hasattr(record, "activated_by")
            else record.changed_by
            if hasattr(record, "changed_by")
            else record.recorded_by
        )
        batch, _ = build_memory_catalog_event_batch(
            _access(actor_id, authorization_id),
            (record,),
            expected_stream_version=len(events),
            next_global_position=len(events) + 1,
            previous_event=parent,
            recorded_at="2026-07-27T00:20:00Z",
        )
        events.extend(batch)
        parent = batch[-1]
    return tuple(events)


def _rebuild_event_envelope(event, *, actor_id=None, occurred_at=None):
    trusted_context = EventTrustedContext(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
        principal_id=event.principal_id,
        agent_client_id=event.agent_client_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id if actor_id is None else actor_id,
        authorization_decision_id=event.authorization_decision_id,
    )
    return build_canonical_event(
        event_id=event.event_id,
        event_type=event.event_type,
        event_version=event.event_version,
        event_kind=event.event_kind,
        origin=event.origin,
        source=event.source,
        stream_id=event.stream_id,
        stream_type=event.stream_type,
        stream_version=event.stream_version,
        global_position=event.global_position,
        trusted_context=trusted_context,
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=event.request_sha256,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        occurred_at=event.occurred_at if occurred_at is None else occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        artifact_refs=event.artifact_refs,
        payload=event.payload,
    )


def test_catalog_rebuilds_proposal_review_evidence_approval_and_head():
    records, authorization_ids = _records_and_publication()
    events = _event_stream(records, authorization_ids)

    projection = _reduce(events)
    catalog = _rebuild((events,))

    assert len(projection.revisions) == 1
    revision = projection.revisions[0]
    assert revision.status == "active"
    assert revision.fix_evidence == records[1].evidence
    assert revision.regression_evidence == (records[2].evidence,)
    assert revision.approval == records[4].approval
    assert revision.activation == records[5].activation
    assert revision.eligible_for_retrieval is True
    assert projection.activated_head is not None
    assert catalog.load_head(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=records[0].memory_id,
    ) == projection.activated_head


def test_sqlite_event_ledger_append_rebuilds_the_same_catalog_head():
    records, authorization_ids = _records_and_publication()
    connection = _ledger_connection()
    try:
        result = None
        for record, authorization_id in zip(
            records, authorization_ids, strict=True
        ):
            actor = (
                record.proposed_by
                if hasattr(record, "proposed_by")
                else record.actor_id
                if isinstance(record, MemoryCatalogEvidenceRecord)
                else record.reviewed_by
                if hasattr(record, "reviewed_by")
                else record.approval.approved_by
                if isinstance(record, StoredMemoryRevisionApprovalPublication)
                else record.activation.activated_by
            )
            ledger = SQLiteEventLedgerV1(
                connection, _access(actor, authorization_id)
            )
            result = append_memory_catalog_records(
                ledger,
                (record,),
                recorded_at="2026-07-27T00:20:00Z",
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            )
        assert result is not None
        assert result.projection.activated_head is not None
        assert (
            result.projection.activated_head.current_revision_id
            == records[0].revision_id
        )
        assert result.receipt.current_stream_version == len(records)
        snapshot = rebuild_memory_catalog_from_ledger(
            ledger,
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        descriptor = build_memory_catalog_reducer(
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS
        ).descriptor
        assert snapshot.source_event_count == len(records)
        assert snapshot.event_high_watermark == len(records)
        assert (
            snapshot.reducer_descriptor_sha256
            == descriptor.descriptor_sha256
        )
        assert (
            snapshot.reducer_configuration_sha256
            == descriptor.configuration_sha256
        )
        assert snapshot.load_head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=records[0].memory_id,
        ) == result.projection.activated_head
    finally:
        connection.close()


def test_counterexample_suspension_and_obsolescence_are_forward_only():
    records, authorization_ids = _records_and_publication()
    events = list(_event_stream(records, authorization_ids))
    revision = records[0]
    activation = records[-1].activation
    counterexample = build_memory_revision_counterexample(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=revision.memory_id,
        revision_id=revision.revision_id,
        evidence=_evidence(result="fail", evaluation_case_id="counterexample"),
        recorded_by="counterexample_reviewer",
        recorded_at="2026-07-27T00:09:00Z",
        counterexample_attestation_sha256=DIGEST,
    )
    suspension = build_memory_revision_state_change(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=revision.memory_id,
        revision_id=revision.revision_id,
        activation_id=activation.activation_id,
        change="suspended",
        replacement_revision_id=None,
        changed_by="catalog_operator",
        changed_at="2026-07-27T00:10:00Z",
        reason_sha256=DIGEST,
        change_attestation_sha256=DIGEST,
    )
    obsolescence = build_memory_revision_state_change(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=revision.memory_id,
        revision_id=revision.revision_id,
        activation_id=activation.activation_id,
        change="obsoleted",
        replacement_revision_id=None,
        changed_by="catalog_operator",
        changed_at="2026-07-27T00:11:00Z",
        reason_sha256=DIGEST,
        change_attestation_sha256=DIGEST,
    )
    tail = _event_stream(
        (counterexample, suspension, obsolescence),
        (
            "authorization_counterexample",
            "authorization_suspend",
            "authorization_obsolete",
        ),
    )
    # Rebuild the tail against the actual prior head, preserving its hashes.
    parent = events[-1]
    for record, authorization_id in zip(
        (counterexample, suspension, obsolescence),
        (
            "authorization_counterexample",
            "authorization_suspend",
            "authorization_obsolete",
        ),
        strict=True,
    ):
        actor = record.recorded_by if hasattr(record, "recorded_by") else record.changed_by
        batch, _ = build_memory_catalog_event_batch(
            _access(actor, authorization_id),
            (record,),
            expected_stream_version=len(events),
            next_global_position=len(events) + 1,
            previous_event=parent,
            recorded_at="2026-07-27T00:20:00Z",
        )
        events.extend(batch)
        parent = batch[-1]
    assert tail  # exercises independent first-event construction too

    projection = _reduce(tuple(events))
    view = projection.revisions[0]
    assert view.status == "obsoleted"
    assert len(view.counterexamples) == 1
    assert [item.change for item in view.state_changes] == [
        "suspended",
        "obsoleted",
    ]
    assert projection.activated_head is None

    with pytest.raises(MemoryCatalogEventV1Error):
        _reduce(tuple(events + [events[-1]]))


def test_reducer_rejects_approval_before_review_and_actor_substitution():
    records, authorization_ids = _records_and_publication()
    proposal, fix, regression, _review, approval, _activation = records
    events = _event_stream(
        (proposal, fix, regression, approval),
        (
            authorization_ids[0],
            authorization_ids[1],
            authorization_ids[2],
            authorization_ids[4],
        ),
    )
    with pytest.raises(MemoryCatalogEventV1Error) as caught:
        _reduce(events)
    assert caught.value.code == "TBM_MEMORY_CATALOG_TRANSITION_INVALID"

    with pytest.raises(MemoryCatalogEventV1Error) as actor_error:
        build_memory_catalog_event_batch(
            _access("other_actor"),
            (proposal,),
            expected_stream_version=0,
            next_global_position=1,
            previous_event=None,
            recorded_at="2026-07-27T00:20:00Z",
        )
    assert actor_error.value.code == "TBM_MEMORY_CATALOG_EVENT_ACTOR_MISMATCH"


def test_replay_rejects_record_scope_hidden_under_another_event_partition():
    records, authorization_ids = _records_and_publication()
    proposal_event = _event_stream((records[0],), (authorization_ids[0],))[0]
    evil_context = EventTrustedContext(
        organization_id=proposal_event.organization_id,
        tenant_id="tenant_evil",
        repository_id=proposal_event.repository_id,
        environment_id=proposal_event.environment_id,
        principal_id=proposal_event.principal_id,
        agent_client_id=proposal_event.agent_client_id,
        actor_type=proposal_event.actor_type,
        actor_id=proposal_event.actor_id,
        authorization_decision_id=proposal_event.authorization_decision_id,
    )
    evil_event = build_canonical_event(
        event_id="evt_mc_" + "e" * 64,
        event_type=proposal_event.event_type,
        event_version=proposal_event.event_version,
        event_kind=proposal_event.event_kind,
        origin=proposal_event.origin,
        source=proposal_event.source,
        stream_id=proposal_event.stream_id,
        stream_type=proposal_event.stream_type,
        stream_version=proposal_event.stream_version,
        global_position=proposal_event.global_position,
        trusted_context=evil_context,
        request_id=proposal_event.request_id,
        idempotency_key_sha256=proposal_event.idempotency_key_sha256,
        request_sha256=proposal_event.request_sha256,
        correlation_id=proposal_event.correlation_id,
        causation_id=proposal_event.causation_id,
        occurred_at=proposal_event.occurred_at,
        recorded_at=proposal_event.recorded_at,
        producer=proposal_event.producer,
        producer_version=proposal_event.producer_version,
        payload_schema=proposal_event.payload_schema,
        previous_stream_event_sha256=None,
        classification=proposal_event.classification,
        retention_policy_id=proposal_event.retention_policy_id,
        artifact_refs=proposal_event.artifact_refs,
        payload=proposal_event.payload,
    )
    with pytest.raises(MemoryCatalogEventV1Error) as caught:
        _reduce((evil_event,))
    assert caught.value.code == "TBM_MEMORY_CATALOG_TRANSITION_INVALID"


def test_replay_binds_record_actor_and_occurrence_time_to_event_envelope():
    records, authorization_ids = _records_and_publication()
    proposal_event = _event_stream((records[0],), (authorization_ids[0],))[0]

    for forged_event in (
        _rebuild_event_envelope(
            proposal_event,
            actor_id="attacker_actor",
        ),
        _rebuild_event_envelope(
            proposal_event,
            occurred_at="2026-07-27T00:06:01Z",
        ),
    ):
        with pytest.raises(MemoryCatalogEventV1Error) as caught:
            _reduce((forged_event,))
        assert caught.value.code == "TBM_MEMORY_CATALOG_TRANSITION_INVALID"


def test_replay_rejects_forged_approval_evidence_digest_and_time_travel():
    records, authorization_ids = _records_and_publication()
    stored = records[4]
    approval = stored.approval
    unsigned = approval.to_dict()
    unsigned.pop("approval_id")
    unsigned["evidence_bundle_sha256"] = "sha256:" + "0" * 64
    forged_approval = MemoryRevisionApproval(
        approval_id=memory_revision_approval_id(unsigned),
        **unsigned,
    )
    forged_stored = StoredMemoryRevisionApprovalPublication(
        approval=forged_approval,
        policy=stored.policy,
        request=stored.request,
        decision=stored.decision,
        attestation_verified_by=stored.attestation_verified_by,
    )
    forged_events = _event_stream(
        (*records[:4], forged_stored), authorization_ids[:5]
    )
    with pytest.raises(MemoryCatalogEventV1Error) as digest_error:
        _reduce(forged_events)
    assert digest_error.value.code == "TBM_MEMORY_CATALOG_TRANSITION_INVALID"

    late_review = build_memory_revision_review(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=records[0].memory_id,
        revision_id=records[0].revision_id,
        decision="accepted",
        reviewed_by="catalog_reviewer",
        reviewed_at="2026-07-27T00:07:30Z",
        rationale_sha256=DIGEST,
        review_attestation_sha256=DIGEST,
    )
    time_events = _event_stream(
        (*records[:3], late_review, stored), authorization_ids[:5]
    )
    with pytest.raises(MemoryCatalogEventV1Error) as time_error:
        _reduce(time_events)
    assert time_error.value.code == "TBM_MEMORY_CATALOG_TRANSITION_INVALID"

    untrusted_stored = replace(
        stored, attestation_verified_by="attacker_verifier"
    )
    untrusted_events = _event_stream(
        (*records[:4], untrusted_stored), authorization_ids[:5]
    )
    with pytest.raises(MemoryCatalogEventV1Error) as verifier_error:
        _reduce(untrusted_events)
    assert verifier_error.value.code == "TBM_MEMORY_CATALOG_TRANSITION_INVALID"


def test_relationship_supersedes_old_head_before_next_activation():
    records, authorization_ids = _records_and_publication()
    events = list(_event_stream(records, authorization_ids))
    revision_1 = records[0]
    fix = records[1].evidence
    regression = records[2].evidence
    activation_1 = records[5].activation
    (
        _base_approval,
        _base_revision,
        fixes,
        regressions,
        policy,
        _approval_request_1,
        _approval_decision_1,
    ) = _approval()
    revision_2 = build_memory_revision(
        memory_id=revision_1.memory_id,
        memory_kind=revision_1.memory_kind,
        revision_number=2,
        previous_revision_id=revision_1.revision_id,
        memory_type=revision_1.memory_type,
        content_artifact=revision_1.content_artifact,
        scope=revision_1.scope,
        confidence=revision_1.confidence,
        sensitive=revision_1.sensitive,
        eval_leaking=revision_1.eval_leaking,
        source_case_id=revision_1.source_case_id,
        source_case_revision_id=revision_1.source_case_revision_id,
        fix_evidence_id=revision_1.fix_evidence_id,
        regression_evidence_ids=revision_1.regression_evidence_ids,
        proposed_by=revision_1.proposed_by,
        proposed_via_client_id=revision_1.proposed_via_client_id,
        proposed_at="2026-07-27T00:09:00Z",
        proposal_attestation_sha256=DIGEST,
    )
    review_2 = build_memory_revision_review(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=revision_2.memory_id,
        revision_id=revision_2.revision_id,
        decision="accepted",
        reviewed_by="catalog_reviewer",
        reviewed_at="2026-07-27T00:10:00Z",
        rationale_sha256=DIGEST,
        review_attestation_sha256=DIGEST,
    )
    approval_request_2, approval_decision_2 = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at="2026-07-27T00:11:00Z",
    )
    approval_2 = approve_memory_revision(
        revision=revision_2,
        previous_revision=revision_1,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        policy=policy,
        request=approval_request_2,
        decision=approval_decision_2,
        approved_by="publication_approver",
        approved_via_client_id="publication_service",
        approved_at="2026-07-27T00:11:00Z",
        approval_attestation_sha256=DIGEST,
    )
    relationship = build_memory_revision_relationship(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=revision_1.memory_id,
        from_revision_id=revision_2.revision_id,
        to_revision_id=revision_1.revision_id,
        relationship="supersedes",
        recorded_by="catalog_operator",
        recorded_at="2026-07-27T00:12:00Z",
        evidence_sha256=DIGEST,
        relationship_attestation_sha256=DIGEST,
    )
    superseded = build_memory_revision_state_change(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=revision_1.memory_id,
        revision_id=revision_1.revision_id,
        activation_id=activation_1.activation_id,
        change="superseded",
        replacement_revision_id=revision_2.revision_id,
        changed_by="catalog_operator",
        changed_at="2026-07-27T00:13:00Z",
        reason_sha256=DIGEST,
        change_attestation_sha256=DIGEST,
    )
    activation_request_2, activation_decision_2 = _authorization(
        policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at="2026-07-27T00:14:00Z",
    )
    activation_2 = activate_memory_revision(
        revision=revision_2,
        approval=approval_2,
        previous_revision=revision_1,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval_policy=policy,
        approval_request=approval_request_2,
        approval_decision=approval_decision_2,
        previous_activation=activation_1,
        policy=policy,
        request=activation_request_2,
        decision=activation_decision_2,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at="2026-07-27T00:14:00Z",
        activation_attestation_sha256=DIGEST,
    )
    approval_record_2 = StoredMemoryRevisionApprovalPublication(
        approval=approval_2,
        policy=policy,
        request=approval_request_2,
        decision=approval_decision_2,
        attestation_verified_by="attestation_verifier",
    )
    activation_record_2 = StoredMemoryRevisionActivationPublication(
        activation=activation_2,
        policy=policy,
        request=activation_request_2,
        decision=activation_decision_2,
        attestation_verified_by="attestation_verifier",
    )
    tail_records = (
        revision_2,
        MemoryCatalogEvidenceRecord(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=revision_2.memory_id,
            revision_id=revision_2.revision_id,
            evidence=fix,
        ),
        MemoryCatalogEvidenceRecord(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=revision_2.memory_id,
            revision_id=revision_2.revision_id,
            evidence=regression,
        ),
        review_2,
        approval_record_2,
        relationship,
        superseded,
        activation_record_2,
    )
    tail_auth = (
        "authorization_proposal_2",
        "authorization_fix_2",
        "authorization_regression_2",
        "authorization_review_2",
        approval_2.authorization_event_id,
        "authorization_relationship",
        "authorization_supersede",
        activation_2.authorization_event_id,
    )
    parent = events[-1]
    for record, authorization_id in zip(tail_records, tail_auth, strict=True):
        actor = (
            record.proposed_by
            if hasattr(record, "proposed_by")
            else record.actor_id
            if isinstance(record, MemoryCatalogEvidenceRecord)
            else record.reviewed_by
            if hasattr(record, "reviewed_by")
            else record.approval.approved_by
            if isinstance(record, StoredMemoryRevisionApprovalPublication)
            else record.activation.activated_by
            if isinstance(record, StoredMemoryRevisionActivationPublication)
            else record.approved_by
            if hasattr(record, "approved_by")
            else record.activated_by
            if hasattr(record, "activated_by")
            else record.changed_by
            if hasattr(record, "changed_by")
            else record.recorded_by
        )
        batch, _ = build_memory_catalog_event_batch(
            _access(actor, authorization_id),
            (record,),
            expected_stream_version=len(events),
            next_global_position=len(events) + 1,
            previous_event=parent,
            recorded_at="2026-07-27T00:20:00Z",
        )
        events.extend(batch)
        parent = batch[-1]

    projection = _reduce(tuple(events))
    assert [item.status for item in projection.revisions] == [
        "superseded",
        "active",
    ]
    assert projection.activated_head is not None
    assert projection.activated_head.current_revision_id == revision_2.revision_id
    assert projection.revisions[0].relationships[0] == relationship


def test_event_head_wraps_exact_source_and_rechecks_event_projection():
    environment = _published_source()
    try:
        revision = environment.revision
        review = build_memory_revision_review(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=revision.memory_id,
            revision_id=revision.revision_id,
            decision="accepted",
            reviewed_by="catalog_reviewer",
            reviewed_at="2026-07-27T00:06:30Z",
            rationale_sha256=DIGEST,
            review_attestation_sha256=DIGEST,
        )
        approval_request, approval_decision = _authorization(
            environment.publication_policy,
            actor_id="publication_approver",
            permission="memory:review",
            decided_at="2026-07-27T00:07:00Z",
        )
        activation_request, activation_decision = _authorization(
            environment.publication_policy,
            actor_id="publication_activator",
            permission="memory:activate",
            decided_at="2026-07-27T00:08:00Z",
        )
        approval_record = StoredMemoryRevisionApprovalPublication(
            approval=environment.approval,
            policy=environment.publication_policy,
            request=approval_request,
            decision=approval_decision,
            attestation_verified_by="attestation_verifier",
        )
        activation_record = StoredMemoryRevisionActivationPublication(
            activation=environment.activation,
            policy=environment.publication_policy,
            request=activation_request,
            decision=activation_decision,
            attestation_verified_by="attestation_verifier",
        )
        records = (
            revision,
            MemoryCatalogEvidenceRecord(
                tenant_id="tenant_001",
                repository_id="repository_001",
                memory_id=revision.memory_id,
                revision_id=revision.revision_id,
                evidence=next(iter(environment.fixes.values())),
            ),
            MemoryCatalogEvidenceRecord(
                tenant_id="tenant_001",
                repository_id="repository_001",
                memory_id=revision.memory_id,
                revision_id=revision.revision_id,
                evidence=next(iter(environment.regressions.values())),
            ),
            review,
            approval_record,
            activation_record,
        )
        events = _event_stream(
            records,
            (
                "authorization_proposal",
                "authorization_fix",
                "authorization_regression",
                "authorization_review",
                environment.approval.authorization_event_id,
                environment.activation.authorization_event_id,
            ),
        )
        catalog = _rebuild((events,))
        source = EventActivatedMemoryHeadSource(
            head_reader=catalog,
            verified_source=environment.source,
        )
        context = _context(environment.registry)
        authorized = environment.authorization.authorize_retrieval(
            context, lambda scope: scope
        )
        candidate = source.load_authorized(
            context, authorized.scope, memory_id=revision.memory_id
        )
        source.verify_current(authorized.scope, candidate)
        assert candidate.revision == revision

        head = catalog.load_head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=revision.memory_id,
        )
        forged_values = head.to_dict()
        forged_values.pop("head_sha256")
        forged_values["activated_at"] = "2026-07-28T00:08:00Z"
        forged_head = replace(
            head,
            activated_at="2026-07-28T00:08:00Z",
            head_sha256=canonical_sha256(forged_values),
        )

        class _ForgedHeadReader:
            def load_head(self, **_kwargs):
                return forged_head

            def verify_head(self, candidate_head):
                catalog.verify_head(candidate_head)

        forged_source = EventActivatedMemoryHeadSource(
            head_reader=_ForgedHeadReader(),
            verified_source=environment.source,
        )
        with pytest.raises(ActivatedRevisionV3Error) as provenance_error:
            forged_source.load_authorized(
                context, authorized.scope, memory_id=revision.memory_id
            )
        assert (
            provenance_error.value.code
            == "TBM_MEMORY_CATALOG_SOURCE_HEAD_INVALID"
        )

        source_forged_values = head.to_dict()
        source_forged_values.pop("head_sha256")
        source_forged_values["source_event_sha256"] = "sha256:" + "f" * 64
        forged_source_event_head = replace(
            head,
            source_event_sha256="sha256:" + "f" * 64,
            head_sha256=canonical_sha256(source_forged_values),
        )

        class _ForgedSourceEventHeadReader:
            def load_head(self, **_kwargs):
                return forged_source_event_head

            def verify_head(self, candidate_head):
                catalog.verify_head(candidate_head)

        source_event_forgery = EventActivatedMemoryHeadSource(
            head_reader=_ForgedSourceEventHeadReader(),
            verified_source=environment.source,
        )
        with pytest.raises(ActivatedRevisionV3Error) as source_event_error:
            source_event_forgery.load_authorized(
                context, authorized.scope, memory_id=revision.memory_id
            )
        assert (
            source_event_error.value.code
            == "TBM_MEMORY_CATALOG_SOURCE_HEAD_INVALID"
        )
        with pytest.raises(MemoryCatalogEventV1Error):
            replace(head, head_sha256="sha256:" + "0" * 64)
    finally:
        environment.close()


def test_legacy_lesson_requires_explicit_non_authoritative_projection():
    lesson = Lesson(
        lesson_id="legacy_lesson_001",
        source_case_id="legacy_case_001",
        lesson_text="Legacy guidance.",
        memory_type="procedural",
        scope={"repository_id": "repository_001"},
    )
    projection = project_legacy_lesson(
        lesson,
        tenant_id="tenant_001",
        repository_id="repository_001",
    )

    assert isinstance(projection, LegacyLessonCompatibilityProjection)
    assert projection.eligible_for_activated_head is False
    assert not isinstance(projection, ActivatedMemoryHead)


def test_payload_record_type_is_not_substitutable_after_build():
    records, authorization_ids = _records_and_publication()
    events = list(_event_stream(records, authorization_ids))
    approval_event = events[-2]
    assert approval_event.event_type == MEMORY_REVISION_APPROVED

    with pytest.raises(ValueError):
        replace(
            approval_event,
            payload={
                **dict(approval_event.payload),
                "record_type": "tbm.memory.revision_activated",
            },
        )


def test_custom_record_loader_sanitizes_invalid_unicode():
    with pytest.raises(MemoryCatalogEventV1Error) as caught:
        loads_memory_revision_review("\ud800")
    assert caught.value.code == "TBM_MEMORY_CATALOG_RECORD_INVALID_JSON"


def test_reducer_descriptor_binds_trusted_verifier_configuration():
    trusted = build_memory_catalog_reducer(
        trusted_attestation_verifier_ids=("attestation_verifier",)
    )
    other = build_memory_catalog_reducer(
        trusted_attestation_verifier_ids=("other_verifier",)
    )
    assert (
        trusted.descriptor.configuration_sha256
        != other.descriptor.configuration_sha256
    )


def test_registry_resources_and_root_exports_are_exact():
    schema_path = (
        ROOT
        / "schemas"
        / "memory_catalog_event_payload_registry_v1.schema.json"
    )
    catalog_path = (
        ROOT
        / "examples"
        / "memory_catalog_event_type_registry_v1.example.json"
    )
    assert json.loads(schema_path.read_text(encoding="utf-8")) == (
        memory_catalog_event_payload_dispatch_schema()
    )
    assert json.loads(catalog_path.read_text(encoding="utf-8")) == (
        build_memory_catalog_event_registry().catalog()
    )
    for relative in (
        "schemas/memory_catalog_event_payload_registry_v1.schema.json",
        "examples/memory_catalog_event_type_registry_v1.example.json",
    ):
        assert read_packaged_resource(relative) == (ROOT / relative).read_bytes()
    for name in (
        "MemoryCatalog",
        "DurableMemoryCatalogSnapshot",
        "ActivatedMemoryHead",
        "EventActivatedMemoryHeadSource",
        "append_memory_catalog_records",
        "rebuild_memory_catalog_from_ledger",
    ):
        assert name in tbm.__all__
        assert getattr(tbm, name) is not None
