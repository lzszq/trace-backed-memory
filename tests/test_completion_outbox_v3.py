from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import trace_backed_memory as tbm
from trace_backed_memory.completion_outbox_v3 import (
    COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION,
    COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION,
    COMPLETION_OUTBOX_JSON_MAX_BYTES,
    COMPLETION_OUTBOX_MAX_ATTEMPTS,
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    CompletionOutboxContractError,
    acknowledge_completion_outbox_delivery,
    build_completion_outbox_event,
    build_initial_completion_outbox_delivery,
    claim_completion_outbox_delivery,
    completion_outbox_delivery_id,
    completion_outbox_event_id,
    dumps_completion_outbox_delivery,
    dumps_completion_outbox_event,
    fail_completion_outbox_delivery,
    loads_completion_outbox_delivery,
    loads_completion_outbox_event,
    parse_completion_outbox_delivery,
    parse_completion_outbox_event,
    verify_completion_outbox_delivery_transition,
    verify_completion_outbox_event,
)
from trace_backed_memory.gate_session_v3 import (
    create_gate_session,
    transition_gate_session,
)
from trace_backed_memory.outcome_v3 import build_run_outcome


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64
ROOT = Path(__file__).resolve().parents[1]


def _completed_pair():
    outcome = build_run_outcome(
        session_id="gate_session_001",
        trace_id="trace_001",
        run_id="run_001",
        usage_decision_id="usage_decision_001",
        result="pass",
        evaluator_id="evaluation_service",
        evaluator_version="1.2.0",
        output_sha256=DIGEST_A,
        evidence_artifact_sha256s=(DIGEST_B,),
        measured_at="2026-07-29T00:06:00Z",
    )
    created = create_gate_session(
        session_id="gate_session_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=DIGEST_A,
        idempotency_key="request-001",
        created_at="2026-07-29T00:00:00Z",
        expires_at="2026-07-29T01:00:00Z",
    )
    prepared = transition_gate_session(
        created,
        "prepared",
        expected_version=1,
        updated_at="2026-07-29T00:01:00Z",
        lease_expires_at="2026-07-29T00:20:00Z",
        retrieval_snapshot_id="retrieval_001",
        system_gate_evaluation_id="system_gate_001",
    )
    awaiting = transition_gate_session(
        prepared,
        "awaiting_decision",
        expected_version=2,
        updated_at="2026-07-29T00:02:00Z",
    )
    decided = transition_gate_session(
        awaiting,
        "decided",
        expected_version=3,
        updated_at="2026-07-29T00:03:00Z",
        semantic_gate_attempt_ids=("semantic_attempt_001",),
        decision_id="decision_001",
    )
    finalized = transition_gate_session(
        decided,
        "finalized",
        expected_version=4,
        updated_at="2026-07-29T00:04:00Z",
        final_memory_revision_ids=(REVISION_A,),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_decision_001",
    )
    executing = transition_gate_session(
        finalized,
        "executing",
        expected_version=5,
        updated_at="2026-07-29T00:05:00Z",
    )
    completed = transition_gate_session(
        executing,
        "completed",
        expected_version=6,
        updated_at="2026-07-29T00:06:00Z",
        run_outcome_id=outcome.run_outcome_id,
    )
    return outcome, completed


def _event():
    outcome, completed = _completed_pair()
    return build_completion_outbox_event(outcome, completed)


def _lease():
    return claim_completion_outbox_delivery(
        build_initial_completion_outbox_delivery(_event()),
        worker_id="dispatcher_001",
        claimed_at="2026-07-29T00:07:00Z",
        lease_seconds=60,
    )


def _delivery_from_payload(
    **overrides: object,
) -> CompletionOutboxDelivery:
    payload: dict[str, object] = {
        "contract_version": COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION,
        "event_id": _event().event_id,
        "version": 1,
        "status": "pending",
        "attempt_count": 0,
        "updated_at": "9999-12-31T23:59:59.999999Z",
        "available_at": "9999-12-31T23:59:59.999999Z",
        "worker_id": None,
        "lease_expires_at": None,
        "delivered_at": None,
        "last_error_code": None,
        "response_sha256": None,
    }
    payload.update(overrides)
    return CompletionOutboxDelivery(
        delivery_revision_id=completion_outbox_delivery_id(payload),
        **payload,  # type: ignore[arg-type]
    )


def test_completion_outbox_event_is_content_addressed_and_linked():
    outcome, completed = _completed_pair()
    event = build_completion_outbox_event(outcome, completed)

    assert event.contract_version == COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION
    assert event.event_id == completion_outbox_event_id(
        event.to_dict(include_id=False)
    )
    verify_completion_outbox_event(event, outcome, completed)
    assert loads_completion_outbox_event(
        dumps_completion_outbox_event(event)
    ) == event
    assert parse_completion_outbox_event(event.to_dict()) == event
    with pytest.raises(FrozenInstanceError):
        event.event_type = "changed"  # type: ignore[misc]


def test_completion_outbox_event_rejects_linkage_and_tampering():
    outcome, completed = _completed_pair()
    event = _event()
    other_outcome = build_run_outcome(
        session_id=outcome.session_id,
        trace_id="trace_other",
        run_id=outcome.run_id,
        usage_decision_id=outcome.usage_decision_id,
        result=outcome.result,
        evaluator_id=outcome.evaluator_id,
        evaluator_version=outcome.evaluator_version,
        output_sha256=outcome.output_sha256,
        evidence_artifact_sha256s=outcome.evidence_artifact_sha256s,
        measured_at=outcome.measured_at,
    )

    with pytest.raises(CompletionOutboxContractError, match="linkage"):
        build_completion_outbox_event(
            other_outcome,
            completed,
        )
    with pytest.raises(CompletionOutboxContractError, match="event_id"):
        replace(event, repository_id="repository_other")


def test_completion_outbox_delivery_claim_ack_roundtrip():
    pending = build_initial_completion_outbox_delivery(_event())
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
        response_sha256=DIGEST_A,
    )

    assert pending.status == "pending"
    assert leased.status == "leased"
    assert leased.attempt_count == 1
    assert delivered.status == "delivered"
    assert delivered.contract_version == (
        COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION
    )
    assert delivered.delivery_revision_id == completion_outbox_delivery_id(
        delivered.to_dict(include_id=False)
    )
    assert loads_completion_outbox_delivery(
        dumps_completion_outbox_delivery(delivered)
    ) == delivered
    assert parse_completion_outbox_delivery(
        delivered.to_dict()
    ) == delivered


def test_completion_outbox_retry_reclaim_and_dead_letter():
    first_lease = _lease()
    retry = fail_completion_outbox_delivery(
        first_lease,
        worker_id="dispatcher_001",
        failed_at="2026-07-29T00:07:30Z",
        error_code="DELIVERY_TIMEOUT",
        retry_delay_seconds=30,
        max_attempts=2,
    )
    second_lease = claim_completion_outbox_delivery(
        retry,
        worker_id="dispatcher_002",
        claimed_at="2026-07-29T00:08:00Z",
        lease_seconds=60,
    )
    dead = fail_completion_outbox_delivery(
        second_lease,
        worker_id="dispatcher_002",
        failed_at="2026-07-29T00:08:30Z",
        error_code="REMOTE_REJECTED",
        retry_delay_seconds=30,
        max_attempts=2,
    )

    assert retry.status == "retry_wait"
    assert second_lease.attempt_count == 2
    assert dead.status == "dead_letter"
    assert dead.last_error_code == "REMOTE_REJECTED"


def test_completion_outbox_expired_lease_can_be_reclaimed():
    first = _lease()
    second = claim_completion_outbox_delivery(
        first,
        worker_id="dispatcher_002",
        claimed_at="2026-07-29T00:08:00Z",
        lease_seconds=60,
    )

    assert second.status == "leased"
    assert second.version == first.version + 1
    assert second.attempt_count == 2
    assert second.worker_id == "dispatcher_002"


def test_completion_outbox_timestamp_arithmetic_overflow_is_stable():
    with pytest.raises(
        CompletionOutboxContractError,
        match="supported timestamp range",
    ):
        claim_completion_outbox_delivery(
            _delivery_from_payload(),
            worker_id="dispatcher_001",
            claimed_at="9999-12-31T23:59:59.999999Z",
            lease_seconds=1,
        )

    leased = _delivery_from_payload(
        version=2,
        status="leased",
        attempt_count=1,
        updated_at="9999-12-31T23:59:58Z",
        available_at=None,
        worker_id="dispatcher_001",
        lease_expires_at="9999-12-31T23:59:59.999999Z",
    )
    with pytest.raises(
        CompletionOutboxContractError,
        match="supported timestamp range",
    ):
        fail_completion_outbox_delivery(
            leased,
            worker_id="dispatcher_001",
            failed_at="9999-12-31T23:59:59Z",
            error_code="DELIVERY_TIMEOUT",
            retry_delay_seconds=2,
            max_attempts=2,
        )


@pytest.mark.parametrize(
    "action",
    (
        lambda lease: acknowledge_completion_outbox_delivery(
            lease,
            worker_id="dispatcher_other",
            acknowledged_at="2026-07-29T00:07:30Z",
        ),
        lambda lease: acknowledge_completion_outbox_delivery(
            lease,
            worker_id="dispatcher_001",
            acknowledged_at="2026-07-29T00:09:00Z",
        ),
        lambda lease: fail_completion_outbox_delivery(
            lease,
            worker_id="dispatcher_001",
            failed_at="2026-07-29T00:07:30Z",
            error_code="FAILED",
            retry_delay_seconds=0,
            max_attempts=2,
        ),
    ),
)
def test_completion_outbox_rejects_invalid_lease_operations(action):
    with pytest.raises(CompletionOutboxContractError):
        action(_lease())


def test_completion_outbox_terminal_delivery_cannot_transition():
    delivered = acknowledge_completion_outbox_delivery(
        _lease(),
        worker_id="dispatcher_001",
        acknowledged_at="2026-07-29T00:07:30Z",
    )
    with pytest.raises(CompletionOutboxContractError, match="terminal"):
        claim_completion_outbox_delivery(
            delivered,
            worker_id="dispatcher_002",
            claimed_at="2026-07-29T00:08:00Z",
            lease_seconds=60,
        )


def test_completion_outbox_json_is_strict_and_duplicate_rejecting():
    event = _event()
    value = event.to_dict()
    value["extra"] = True
    with pytest.raises(CompletionOutboxContractError, match="fields"):
        parse_completion_outbox_event(value)

    payload = dumps_completion_outbox_event(event)
    duplicate = payload.replace(
        '"event_type":"execution_completed"',
        '"event_type":"execution_completed",'
        '"event_type":"execution_completed"',
    )
    with pytest.raises(
        CompletionOutboxContractError,
        match="duplicate",
    ):
        loads_completion_outbox_event(duplicate)

    assert json.loads(payload) == event.to_dict()


@pytest.mark.parametrize(
    "changes",
    (
        {"contract_version": "wrong"},
        {"event_id": "bad"},
        {"event_type": "changed"},
        {"run_outcome_id": "bad"},
        {"outcome_descriptor_sha256": "bad"},
        {"occurred_at": 1},
        {"occurred_at": "not-a-time"},
        {"occurred_at": "2026-07-29T00:06:00.1Z"},
    ),
)
def test_completion_outbox_event_rejects_invalid_fields(changes):
    with pytest.raises(CompletionOutboxContractError):
        replace(_event(), **changes)


@pytest.mark.parametrize(
    "changes",
    (
        {"contract_version": "wrong"},
        {"delivery_revision_id": "bad"},
        {"event_id": "bad"},
        {"version": 0},
        {"status": "unknown"},
        {"attempt_count": -1},
        {"attempt_count": COMPLETION_OUTBOX_MAX_ATTEMPTS + 1},
        {"version": 2},
        {"attempt_count": 1},
        {"available_at": None},
        {"worker_id": "dispatcher_001"},
    ),
)
def test_pending_delivery_rejects_invalid_fields(changes):
    pending = build_initial_completion_outbox_delivery(_event())
    with pytest.raises(CompletionOutboxContractError):
        replace(pending, **changes)


@pytest.mark.parametrize(
    "changes",
    (
        {"attempt_count": 0},
        {"worker_id": None},
        {"lease_expires_at": "2026-07-29T00:07:00Z"},
        {"available_at": "2026-07-29T00:07:00Z"},
    ),
)
def test_leased_delivery_rejects_invalid_fields(changes):
    with pytest.raises(CompletionOutboxContractError):
        replace(_lease(), **changes)


def test_retry_delivered_and_dead_letter_invariants():
    lease = _lease()
    retry = fail_completion_outbox_delivery(
        lease,
        worker_id="dispatcher_001",
        failed_at="2026-07-29T00:07:30Z",
        error_code="DELIVERY_TIMEOUT",
        retry_delay_seconds=30,
        max_attempts=2,
    )
    delivered = acknowledge_completion_outbox_delivery(
        lease,
        worker_id="dispatcher_001",
        acknowledged_at="2026-07-29T00:07:30Z",
    )
    dead = fail_completion_outbox_delivery(
        lease,
        worker_id="dispatcher_001",
        failed_at="2026-07-29T00:07:30Z",
        error_code="DELIVERY_TIMEOUT",
        retry_delay_seconds=30,
        max_attempts=1,
    )
    for current, changes in (
        (retry, {"attempt_count": 0}),
        (retry, {"available_at": None}),
        (retry, {"available_at": retry.updated_at}),
        (retry, {"last_error_code": None}),
        (retry, {"worker_id": "dispatcher_001"}),
        (delivered, {"attempt_count": 0}),
        (delivered, {"delivered_at": None}),
        (delivered, {"delivered_at": "2026-07-29T00:07:31Z"}),
        (delivered, {"available_at": "2026-07-29T00:07:31Z"}),
        (dead, {"attempt_count": 0}),
        (dead, {"last_error_code": None}),
        (dead, {"worker_id": "dispatcher_001"}),
    ):
        with pytest.raises(CompletionOutboxContractError):
            replace(current, **changes)


def test_claim_rejects_unavailable_stale_and_exhausted_delivery():
    pending = build_initial_completion_outbox_delivery(_event())
    with pytest.raises(CompletionOutboxContractError, match="not available"):
        claim_completion_outbox_delivery(
            pending,
            worker_id="dispatcher_001",
            claimed_at="2026-07-29T00:05:59Z",
            lease_seconds=60,
        )
    with pytest.raises(CompletionOutboxContractError, match="not expired"):
        claim_completion_outbox_delivery(
            _lease(),
            worker_id="dispatcher_002",
            claimed_at="2026-07-29T00:07:30Z",
            lease_seconds=60,
        )
    stale_pending = _delivery_from_payload(
        updated_at="2026-07-29T00:07:00Z",
        available_at="2026-07-29T00:06:00Z",
    )
    with pytest.raises(CompletionOutboxContractError, match="precedes"):
        claim_completion_outbox_delivery(
            stale_pending,
            worker_id="dispatcher_001",
            claimed_at="2026-07-29T00:06:30Z",
            lease_seconds=60,
        )
    exhausted = _delivery_from_payload(
        version=2,
        status="leased",
        attempt_count=COMPLETION_OUTBOX_MAX_ATTEMPTS,
        updated_at="2026-07-29T00:05:00Z",
        available_at=None,
        worker_id="dispatcher_001",
        lease_expires_at="2026-07-29T00:06:00Z",
    )
    with pytest.raises(CompletionOutboxContractError, match="limit"):
        claim_completion_outbox_delivery(
            exhausted,
            worker_id="dispatcher_002",
            claimed_at="2026-07-29T00:07:00Z",
            lease_seconds=60,
        )


def test_delivery_operations_reject_invalid_current_and_limits():
    with pytest.raises(TypeError):
        acknowledge_completion_outbox_delivery(
            object(),  # type: ignore[arg-type]
            worker_id="dispatcher_001",
            acknowledged_at="2026-07-29T00:07:30Z",
        )
    with pytest.raises(CompletionOutboxContractError, match="not leased"):
        acknowledge_completion_outbox_delivery(
            build_initial_completion_outbox_delivery(_event()),
            worker_id="dispatcher_001",
            acknowledged_at="2026-07-29T00:07:30Z",
        )
    with pytest.raises(CompletionOutboxContractError, match="backwards"):
        acknowledge_completion_outbox_delivery(
            _lease(),
            worker_id="dispatcher_001",
            acknowledged_at="2026-07-29T00:06:59Z",
        )
    with pytest.raises(CompletionOutboxContractError, match="max_attempts"):
        fail_completion_outbox_delivery(
            _lease(),
            worker_id="dispatcher_001",
            failed_at="2026-07-29T00:07:30Z",
            error_code="DELIVERY_TIMEOUT",
            retry_delay_seconds=30,
            max_attempts=0,
        )


def test_delivery_transition_verifier_rejects_invalid_edges():
    pending = build_initial_completion_outbox_delivery(_event())
    valid_lease = _lease()
    other_event_id = "completion_outbox_event_sha256_" + "f" * 64
    invalid_transitions = (
        _delivery_from_payload(
            event_id=other_event_id,
            version=2,
            status="leased",
            attempt_count=1,
            updated_at="2026-07-29T00:07:00Z",
            available_at=None,
            worker_id="dispatcher_001",
            lease_expires_at="2026-07-29T00:08:00Z",
        ),
        _delivery_from_payload(
            version=3,
            status="leased",
            attempt_count=1,
            updated_at="2026-07-29T00:07:00Z",
            available_at=None,
            worker_id="dispatcher_001",
            lease_expires_at="2026-07-29T00:08:00Z",
        ),
        _delivery_from_payload(
            version=2,
            status="leased",
            attempt_count=1,
            updated_at="2026-07-29T00:05:00Z",
            available_at=None,
            worker_id="dispatcher_001",
            lease_expires_at="2026-07-29T00:08:00Z",
        ),
        _delivery_from_payload(
            version=2,
            status="leased",
            attempt_count=2,
            updated_at="2026-07-29T00:07:00Z",
            available_at=None,
            worker_id="dispatcher_001",
            lease_expires_at="2026-07-29T00:08:00Z",
        ),
    )
    for current in invalid_transitions:
        with pytest.raises(CompletionOutboxContractError):
            verify_completion_outbox_delivery_transition(pending, current)

    delivered = acknowledge_completion_outbox_delivery(
        valid_lease,
        worker_id="dispatcher_001",
        acknowledged_at="2026-07-29T00:07:30Z",
    )
    after_terminal = _delivery_from_payload(
        version=delivered.version + 1,
        status="delivered",
        attempt_count=delivered.attempt_count,
        updated_at="2026-07-29T00:08:00Z",
        available_at=None,
        delivered_at="2026-07-29T00:08:00Z",
    )
    with pytest.raises(CompletionOutboxContractError, match="not allowed"):
        verify_completion_outbox_delivery_transition(
            delivered,
            after_terminal,
        )


def test_completion_outbox_builders_and_verifier_reject_wrong_types():
    outcome, completed = _completed_pair()
    with pytest.raises(TypeError):
        build_completion_outbox_event(object(), completed)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_completion_outbox_event(outcome, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_initial_completion_outbox_delivery(object())  # type: ignore[arg-type]
    other_session = replace(completed, tenant_id="tenant_other")
    with pytest.raises(CompletionOutboxContractError, match="does not match"):
        verify_completion_outbox_event(_event(), outcome, other_session)


def test_completion_outbox_json_and_parser_rejection_matrix():
    event = _event()
    assert loads_completion_outbox_event(
        dumps_completion_outbox_event(event).encode("utf-8")
    ) == event
    with pytest.raises(CompletionOutboxContractError):
        loads_completion_outbox_event(
            "x" * (COMPLETION_OUTBOX_JSON_MAX_BYTES + 1)
        )
    with pytest.raises(CompletionOutboxContractError):
        loads_completion_outbox_event(1)  # type: ignore[arg-type]
    with pytest.raises(CompletionOutboxContractError):
        loads_completion_outbox_event("[]")
    with pytest.raises(CompletionOutboxContractError):
        parse_completion_outbox_event([])  # type: ignore[arg-type]

    class Key(str):
        pass

    subclass_key = {
        Key(key): value for key, value in event.to_dict().items()
    }
    with pytest.raises(CompletionOutboxContractError, match="keys"):
        parse_completion_outbox_event(subclass_key)

    for field, value in (
        ("event_id", 1),
        ("outcome_descriptor_sha256", 1),
    ):
        changed = {**event.to_dict(), field: value}
        with pytest.raises(CompletionOutboxContractError):
            parse_completion_outbox_event(changed)
    delivery = _lease().to_dict()
    for field, value in (
        ("worker_id", 1),
        ("version", True),
        ("last_error_code", " BAD "),
    ):
        changed = {**delivery, field: value}
        with pytest.raises(CompletionOutboxContractError):
            parse_completion_outbox_delivery(changed)


def test_completion_outbox_schema_examples_and_public_exports_match_runtime():
    event_value = json.loads(
        (
            ROOT / "examples" / "completion_outbox_event_v3.example.json"
        ).read_text(encoding="utf-8")
    )
    delivery_value = json.loads(
        (
            ROOT
            / "examples"
            / "completion_outbox_delivery_v3.example.json"
        ).read_text(encoding="utf-8")
    )
    event_schema = json.loads(
        (
            ROOT / "schemas" / "completion_outbox_event_v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    delivery_schema = json.loads(
        (
            ROOT
            / "schemas"
            / "completion_outbox_delivery_v3.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert parse_completion_outbox_event(event_value).to_dict() == event_value
    assert (
        parse_completion_outbox_delivery(delivery_value).to_dict()
        == delivery_value
    )
    assert set(event_schema["required"]) == set(event_value)
    assert set(delivery_schema["required"]) == set(delivery_value)
    assert event_schema["additionalProperties"] is False
    assert delivery_schema["additionalProperties"] is False
    event_validator = Draft202012Validator(
        event_schema,
        format_checker=FormatChecker(),
    )
    delivery_validator = Draft202012Validator(
        delivery_schema,
        format_checker=FormatChecker(),
    )
    event_validator.validate(event_value)
    delivery_validator.validate(delivery_value)
    for field, invalid in (
        ("tenant_id", " tenant_001"),
        ("tenant_id", "tenant_001\n"),
        ("event_id", f"{event_value['event_id']}\n"),
        (
            "outcome_descriptor_sha256",
            f"{event_value['outcome_descriptor_sha256']}\n",
        ),
        ("occurred_at", "2026-07-29T00:06:00.1Z"),
        ("occurred_at", "0000-01-01T00:00:00Z"),
        ("occurred_at", "2026-02-30T00:00:00Z"),
        ("occurred_at", "2026-13-01T00:00:00Z"),
    ):
        changed = {**event_value, field: invalid}
        assert tuple(event_validator.iter_errors(changed))
    event_validator.validate(
        {**event_value, "occurred_at": "2024-02-29T00:00:00Z"}
    )
    changed_delivery = {**delivery_value, "worker_id": " worker_001 "}
    assert tuple(delivery_validator.iter_errors(changed_delivery))
    identifier_validator = Draft202012Validator(
        delivery_schema["$defs"]["identifier"]
    )
    error_code_validator = Draft202012Validator(
        delivery_schema["properties"]["last_error_code"]["oneOf"][0]
    )
    assert tuple(identifier_validator.iter_errors("worker_001\n"))
    assert tuple(error_code_validator.iter_errors("DELIVERY_TIMEOUT\n"))
    assert tbm.CompletionOutboxEvent is CompletionOutboxEvent
    assert tbm.CompletionOutboxDelivery is CompletionOutboxDelivery
    for name in (
        "CompletionOutboxEvent",
        "CompletionOutboxDelivery",
        "build_completion_outbox_event",
        "claim_completion_outbox_delivery",
        "acknowledge_completion_outbox_delivery",
        "fail_completion_outbox_delivery",
    ):
        assert name in tbm.__all__
