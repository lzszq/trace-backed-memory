from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.test_completion_outbox_v3 import _completed_pair
from trace_backed_memory.completion_outbox_v3 import (
    acknowledge_completion_outbox_delivery,
    build_completion_outbox_event,
    build_initial_completion_outbox_delivery,
    claim_completion_outbox_delivery,
    fail_completion_outbox_delivery,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.outcome_effect_event_v1 import (
    EFFECT_COMPENSATED,
    EFFECT_COMPENSATION_REQUESTED,
    EFFECT_DEAD_LETTERED,
    EFFECT_REQUESTED,
    EFFECT_STARTED,
    EFFECT_SUCCEEDED,
    OUTCOME_EFFECT_EVENT_TYPES,
    RUN_OUTCOME_RECORDED,
    OutcomeEffectEventV1Error,
    build_completion_outbox_effect_drafts,
    build_effect_compensation_draft,
    build_effect_compensation_reducer,
    build_effect_dead_letter_reducer,
    build_effect_delivery_history_reducer,
    build_effect_queue_reducer,
    build_effect_requested_draft,
    build_effect_transition_draft,
    build_outcome_attribution_draft,
    build_outcome_attribution_reducer,
    build_outcome_effect_event_batch,
    build_outcome_effect_event_registry,
    build_run_outcome_draft,
    build_run_outcome_reducer,
    dumps_outcome_effect_event_payload_dispatch_schema,
    hydrate_outcome_effect_views,
    outcome_effect_stream_id,
    reduce_outcome_effect_events,
)
from trace_backed_memory.outcome_v3 import build_outcome_attribution


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
ROOT = Path(__file__).resolve().parents[1]


def _access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_001",
        actor_type="service",
        actor_id="service_outcome_effect_adapter",
        authorization_decision_id="authorization_outcome_effect_append",
        classification_filter=LedgerClassificationFilter(("internal",)),
    )


def _events(drafts):
    events, idempotency = build_outcome_effect_event_batch(
        tuple(drafts),
        access=_access(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-07-29T01:00:00Z",
    )
    return events, idempotency


def _attribution(outcome, session):
    return build_outcome_attribution(
        run_outcome_id_value=outcome.run_outcome_id,
        usage_decision_id=outcome.usage_decision_id,
        memory_revision_ids=session.final_memory_revision_ids,
        claim_strength="association",
        effect="unknown",
        method="runtime_observation",
        evaluator_id="outcome_observer",
        evaluator_version="1.0.0",
        evidence_artifact_sha256s=(DIGEST_A,),
        confidence=0.5,
        reason="The memory revision was present in the completed run.",
        recorded_at="2026-07-29T00:10:00Z",
    )


def test_registry_and_six_reducer_contracts_are_sealed_and_deterministic():
    registry = build_outcome_effect_event_registry()
    assert registry.sealed
    assert tuple(
        item["event_type"] for item in registry.catalog()["event_types"]
    ) == OUTCOME_EFFECT_EVENT_TYPES
    schema_text = dumps_outcome_effect_event_payload_dispatch_schema()
    schema = json.loads(schema_text)
    Draft202012Validator.check_schema(schema)
    assert schema_text == dumps_outcome_effect_event_payload_dispatch_schema()

    reducers = (
        build_run_outcome_reducer(),
        build_outcome_attribution_reducer(),
        build_effect_queue_reducer(),
        build_effect_delivery_history_reducer(),
        build_effect_dead_letter_reducer(),
        build_effect_compensation_reducer(),
    )
    assert len({item.descriptor.output_projection for item in reducers}) == 6
    assert all(item.descriptor.deterministic for item in reducers)


def test_packaged_registry_and_dispatch_schema_match_runtime_exactly():
    schema_text = dumps_outcome_effect_event_payload_dispatch_schema()
    assert (
        ROOT
        / "schemas"
        / "outcome_effect_event_payload_registry_v1.schema.json"
    ).read_text(encoding="utf-8") == schema_text
    example = json.loads(
        (
            ROOT
            / "examples"
            / "outcome_effect_event_type_registry_v1.example.json"
        ).read_text(encoding="utf-8")
    )
    assert example == build_outcome_effect_event_registry().catalog()


def test_run_outcome_and_attribution_rebuild_exact_domain_records():
    outcome, session = _completed_pair()
    attribution = _attribution(outcome, session)
    drafts = (
        build_run_outcome_draft(outcome, session),
        build_outcome_attribution_draft(attribution, outcome, session),
    )
    events, first_idempotency = _events(drafts)
    repeated, second_idempotency = _events(drafts)

    assert repeated == events
    assert second_idempotency == first_idempotency
    assert events[0].stream_id == outcome_effect_stream_id(session.session_id)
    assert events[1].previous_stream_event_sha256 == events[0].event_sha256

    reduced = reduce_outcome_effect_events(events)
    views = hydrate_outcome_effect_views(reduced, session=session)
    assert views.run_outcome == outcome
    assert views.outcome_attributions == (attribution,)
    assert reduced.run_outcome is not None
    assert isinstance(reduced.run_outcome["record_json"], str)
    assert "0.5" in reduced.outcome_attributions[0]["record_json"]


def test_completion_outbox_rebuilds_queue_history_and_dead_letter_exactly():
    outcome, session = _completed_pair()
    outbox_event = build_completion_outbox_event(outcome, session)
    pending = build_initial_completion_outbox_delivery(outbox_event)
    first_lease = claim_completion_outbox_delivery(
        pending,
        worker_id="dispatcher_001",
        claimed_at="2026-07-29T00:07:00Z",
        lease_seconds=60,
    )
    retry = fail_completion_outbox_delivery(
        first_lease,
        worker_id="dispatcher_001",
        failed_at="2026-07-29T00:07:30Z",
        error_code="provider_timeout",
        retry_delay_seconds=60,
        max_attempts=2,
    )
    second_lease = claim_completion_outbox_delivery(
        retry,
        worker_id="dispatcher_002",
        claimed_at="2026-07-29T00:08:30Z",
        lease_seconds=60,
    )
    dead_letter = fail_completion_outbox_delivery(
        second_lease,
        worker_id="dispatcher_002",
        failed_at="2026-07-29T00:09:00Z",
        error_code="provider_timeout",
        retry_delay_seconds=60,
        max_attempts=2,
    )
    history = (pending, first_lease, retry, second_lease, dead_letter)
    drafts = (
        build_run_outcome_draft(outcome, session),
        *build_completion_outbox_effect_drafts(
            outbox_event,
            history,
            outcome,
            session,
        ),
    )
    events, _ = _events(drafts)
    reduced = reduce_outcome_effect_events(events)
    views = hydrate_outcome_effect_views(reduced, session=session)

    assert views.delivery_history[outbox_event.event_id] == history
    assert views.dead_letters == (dead_letter,)
    assert reduced.dead_letters[0]["error_code"] == "provider_timeout"
    queue = views.effect_queue[outbox_event.event_id]
    assert queue["queue_status"] == "dead_letter"
    assert queue["delivery_revision_id"] == dead_letter.delivery_revision_id
    assert queue["attempt_count"] == 2
    assert queue["compensation_supported"] is False


def test_successful_delivery_is_not_claimed_as_exactly_once_or_compensatable():
    outcome, session = _completed_pair()
    outbox_event = build_completion_outbox_event(outcome, session)
    pending = build_initial_completion_outbox_delivery(outbox_event)
    leased = claim_completion_outbox_delivery(
        pending,
        worker_id="dispatcher_001",
        claimed_at="2026-07-29T00:07:00Z",
        lease_seconds=60,
    )
    delivered = acknowledge_completion_outbox_delivery(
        leased,
        worker_id="dispatcher_001",
        acknowledged_at="2026-07-29T00:07:30Z",
        response_sha256=DIGEST_B,
    )
    drafts = (
        build_run_outcome_draft(outcome, session),
        *build_completion_outbox_effect_drafts(
            outbox_event,
            (pending, leased, delivered),
            outcome,
            session,
        ),
        build_effect_compensation_draft(
            event_type=EFFECT_COMPENSATION_REQUESTED,
            session_id=session.session_id,
            effect_id="effect_compensation_001",
            effect_type="completion.retract",
            compensates_effect_id=outbox_event.event_id,
            occurred_at="2026-07-29T00:08:00Z",
        ),
    )
    events, _ = _events(drafts)
    with pytest.raises(
        OutcomeEffectEventV1Error,
        match="compensation requires one uncompensated successful effect",
    ):
        reduce_outcome_effect_events(events)


def test_compensation_is_a_new_effect_and_never_rewrites_original_history():
    session_id = "gate_session_compensation_001"
    original_id = "effect_publish_001"
    compensation_id = "effect_unpublish_001"
    drafts = (
        build_effect_requested_draft(
            session_id=session_id,
            effect_id=original_id,
            effect_type="github.check.publish",
            occurred_at="2026-07-29T00:00:00Z",
            compensation_supported=True,
        ),
        build_effect_transition_draft(
            event_type=EFFECT_STARTED,
            session_id=session_id,
            effect_id=original_id,
            effect_type="github.check.publish",
            occurred_at="2026-07-29T00:00:01Z",
            compensation_supported=True,
        ),
        build_effect_transition_draft(
            event_type=EFFECT_SUCCEEDED,
            session_id=session_id,
            effect_id=original_id,
            effect_type="github.check.publish",
            occurred_at="2026-07-29T00:00:02Z",
            compensation_supported=True,
        ),
        build_effect_compensation_draft(
            event_type=EFFECT_COMPENSATION_REQUESTED,
            session_id=session_id,
            effect_id=compensation_id,
            effect_type="github.check.delete",
            compensates_effect_id=original_id,
            occurred_at="2026-07-29T00:00:03Z",
        ),
        build_effect_transition_draft(
            event_type=EFFECT_STARTED,
            session_id=session_id,
            effect_id=compensation_id,
            effect_type="github.check.delete",
            compensates_effect_id=original_id,
            occurred_at="2026-07-29T00:00:04Z",
            compensation_supported=False,
        ),
        build_effect_compensation_draft(
            event_type=EFFECT_COMPENSATED,
            session_id=session_id,
            effect_id=compensation_id,
            effect_type="github.check.delete",
            compensates_effect_id=original_id,
            occurred_at="2026-07-29T00:00:05Z",
        ),
    )
    events, _ = _events(drafts)
    reduced = reduce_outcome_effect_events(events)

    assert len(reduced.delivery_history) == 6
    assert [item["event_type"] for item in reduced.compensations] == [
        EFFECT_COMPENSATION_REQUESTED,
        EFFECT_COMPENSATED,
    ]
    assert reduced.effect_queue[original_id]["queue_status"] == "succeeded"
    assert (
        reduced.effect_queue[original_id]["compensation_status"]
        == "compensated"
    )
    assert reduced.effect_queue[compensation_id]["queue_status"] == "compensated"
    assert reduced.effect_queue[compensation_id]["compensates_effect_id"] == (
        original_id
    )


def test_reducer_fails_closed_on_reordered_or_tampered_lifecycle():
    drafts = (
        build_effect_requested_draft(
            session_id="gate_session_001",
            effect_id="effect_001",
            effect_type="github.check.publish",
            occurred_at="2026-07-29T00:00:00Z",
            compensation_supported=False,
        ),
        build_effect_transition_draft(
            event_type=EFFECT_STARTED,
            session_id="gate_session_001",
            effect_id="effect_001",
            effect_type="github.check.publish",
            occurred_at="2026-07-29T00:00:01Z",
            compensation_supported=False,
        ),
    )
    events, _ = _events(drafts)
    with pytest.raises(OutcomeEffectEventV1Error):
        reduce_outcome_effect_events((events[1], events[0]))

    unsigned = events[1].to_dict(include_event_sha256=False)
    unsigned["stream_id"] = outcome_effect_stream_id("another_session")
    with pytest.raises(ValueError):
        replace(events[1], stream_id=unsigned["stream_id"])

    terminal_drafts = (
        *drafts,
        build_effect_transition_draft(
            event_type=EFFECT_SUCCEEDED,
            session_id="gate_session_001",
            effect_id="effect_001",
            effect_type="github.check.publish",
            occurred_at="2026-07-29T00:00:02Z",
            compensation_supported=False,
        ),
        build_effect_transition_draft(
            event_type=EFFECT_DEAD_LETTERED,
            session_id="gate_session_001",
            effect_id="effect_001",
            effect_type="github.check.publish",
            occurred_at="2026-07-29T00:00:03Z",
            compensation_supported=False,
        ),
    )
    terminal_events, _ = _events(terminal_drafts)
    with pytest.raises(
        OutcomeEffectEventV1Error,
        match="effect queue transition is invalid",
    ):
        reduce_outcome_effect_events(terminal_events)


def test_outcome_effect_event_names_match_the_migration_taxonomy():
    assert RUN_OUTCOME_RECORDED == "tbm.execution.run_outcome_recorded"
    assert EFFECT_REQUESTED == "tbm.effect.requested"
    assert EFFECT_STARTED == "tbm.effect.started"
    assert EFFECT_SUCCEEDED == "tbm.effect.succeeded"
    assert EFFECT_DEAD_LETTERED == "tbm.effect.dead_lettered"
    assert EFFECT_COMPENSATION_REQUESTED == "tbm.effect.compensation_requested"
    assert EFFECT_COMPENSATED == "tbm.effect.compensated"
