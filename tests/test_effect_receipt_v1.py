from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from trace_backed_memory.effect_receipt_v1 import (
    EFFECT_AUTHORIZED,
    EFFECT_COMPENSATED,
    EFFECT_COMPENSATION_REQUESTED,
    EFFECT_PROVIDER_REQUEST_RECORDED,
    EFFECT_RECEIPT_EVENT_TYPES,
    EFFECT_RECEIPT_RECORDED,
    EFFECT_REQUESTED,
    EFFECT_STARTED,
    EFFECT_SUCCEEDED,
    EffectContract,
    EffectEventDraft,
    EffectReceiptV1Error,
    TrustedEffectProvider,
    append_effect_receipt_batch,
    build_effect_authorized_draft,
    build_effect_compensated_draft,
    build_effect_compensation_requested_draft,
    build_effect_dead_lettered_draft,
    build_effect_failed_draft,
    build_effect_provider_request_recorded_draft,
    build_effect_receipt_batch,
    build_effect_receipt_recorded_draft,
    build_effect_receipt_registry,
    build_effect_requested_draft,
    build_effect_result_unknown_draft,
    build_effect_retry_scheduled_draft,
    build_effect_started_draft,
    build_effect_succeeded_draft,
    dumps_effect_receipt_payload_dispatch_schema,
    effect_idempotency_key_sha256,
    effect_projection,
    effect_receipt_stream_id,
    reduce_effect_receipt_events,
    verify_effect_receipt_event,
)
from trace_backed_memory.event_v1 import EventArtifactRef, build_canonical_event
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import (
    SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE,
    SQLiteEventLedgerV1,
)


ROOT = Path(__file__).resolve().parents[1]
T0 = "2026-08-02T02:00:00Z"
T1 = "2026-08-02T02:00:01Z"
T2 = "2026-08-02T02:00:02Z"
T3 = "2026-08-02T02:00:03Z"
T4 = "2026-08-02T02:00:04Z"
T5 = "2026-08-02T02:00:05Z"
T6 = "2026-08-02T02:00:06Z"
T7 = "2026-08-02T02:00:07Z"


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _artifact(label: str, media_type: str) -> EventArtifactRef:
    data = label.encode()
    digest = hashlib.sha256(data).hexdigest()
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + digest,
        content_sha256="sha256:" + digest,
        media_type=media_type,
        size_bytes=len(data),
        classification="confidential",
        retention_policy_id="retention_effect_bytes",
        encryption_key_id="effect_key_001",
        availability="available",
    )


def _access(authorization: str = "authorization_effect_001") -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="effect_dispatcher_001",
        authorization_decision_id=authorization,
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _provider(provider_id: str = "provider_001") -> TrustedEffectProvider:
    return TrustedEffectProvider(
        provider_id=provider_id,
        registration_sha256=_digest(provider_id + "-registration"),
        adapter_id="effect_adapter_001",
        adapter_version="v1",
    )


def _contract(
    effect_id: str = "effect_001",
    *,
    authorization: str = "authorization_effect_001",
    compensation_supported: bool = True,
    max_attempts: int = 2,
    input_artifact: EventArtifactRef | None = None,
) -> EffectContract:
    artifact = input_artifact or _artifact(effect_id + "-input", "application/json")
    values = {
        "effect_id": effect_id,
        "effect_type": "completion.notification",
        "requested_by_event_id": "evt_gate_completion_001",
        "input_artifact_sha256": artifact.content_sha256,
        "authorization_event_id": authorization,
        "compensation_supported": compensation_supported,
        "max_attempts": max_attempts,
    }
    return EffectContract(
        **values,
        idempotency_key_sha256=effect_idempotency_key_sha256(**values),
    )


def _success_drafts(
    contract: EffectContract,
    input_artifact: EventArtifactRef,
    *,
    provider_request_id: str = "provider-request-001",
    compensation: bool = False,
    parent: object | None = None,
) -> tuple[EffectEventDraft, ...]:
    provider = _provider()
    receipt = _artifact(contract.effect_id + "-receipt", "application/provider-receipt+json")
    if compensation:
        assert parent is not None
        first = build_effect_compensation_requested_draft(
            contract,
            input_artifact=input_artifact,
            parent=parent,  # type: ignore[arg-type]
            occurred_at=T0,
        )
    else:
        first = build_effect_requested_draft(
            contract, input_artifact=input_artifact, occurred_at=T0
        )
    terminal = (
        build_effect_compensated_draft(
            contract,
            provider=provider,
            attempt_number=1,
            provider_request_id=provider_request_id,
            receipt_sha256=receipt.content_sha256,
            result_sha256=_digest(contract.effect_id + "-result"),
            parent_effect_id=parent.effect_id,  # type: ignore[union-attr]
            parent_success_event_id=parent.terminal_event_id,  # type: ignore[union-attr]
            parent_success_event_sha256=parent.terminal_event_sha256,  # type: ignore[union-attr]
            parent_receipt_sha256=parent.receipt_sha256,  # type: ignore[union-attr]
            occurred_at=T5,
        )
        if compensation
        else build_effect_succeeded_draft(
            contract,
            provider=provider,
            attempt_number=1,
            provider_request_id=provider_request_id,
            receipt_sha256=receipt.content_sha256,
            result_sha256=_digest(contract.effect_id + "-result"),
            occurred_at=T5,
        )
    )
    return (
        first,
        build_effect_authorized_draft(contract, occurred_at=T1),
        build_effect_started_draft(
            contract, provider=provider, attempt_number=1, occurred_at=T2
        ),
        build_effect_provider_request_recorded_draft(
            contract,
            provider=provider,
            attempt_number=1,
            provider_request_id=provider_request_id,
            occurred_at=T3,
        ),
        build_effect_receipt_recorded_draft(
            contract,
            provider=provider,
            attempt_number=1,
            provider_request_id=provider_request_id,
            receipt_artifact=receipt,
            result_sha256=_digest(contract.effect_id + "-result"),
            occurred_at=T4,
        ),
        terminal,
    )


def _events(
    drafts: tuple[EffectEventDraft, ...],
    *,
    start: int = 1,
    prior: tuple = (),
    related: tuple = (),
):
    events, _ = build_effect_receipt_batch(
        drafts,
        access=_access(drafts[0].contract.authorization_event_id),
        expected_stream_version=len(prior),
        next_global_position=start,
        prior_stream_events=prior,
        related_events=related,
        recorded_at=T7,
    )
    return events


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        ":memory:", isolation_level=None, check_same_thread=False
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource(SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE).decode("utf-8")
    )
    return connection


def test_registry_schema_and_root_exports_are_stable():
    registry = build_effect_receipt_registry()
    assert registry.sealed
    assert tuple(row["event_type"] for row in registry.catalog()["event_types"]) == (
        EFFECT_RECEIPT_EVENT_TYPES
    )
    schema_text = dumps_effect_receipt_payload_dispatch_schema()
    Draft202012Validator.check_schema(json.loads(schema_text))
    assert schema_text == dumps_effect_receipt_payload_dispatch_schema()
    assert (
        ROOT / "schemas" / "effect_receipt_payload_registry_v1.schema.json"
    ).read_text(encoding="utf-8") == schema_text
    assert json.loads(
        (
            ROOT / "examples" / "effect_receipt_type_registry_v1.example.json"
        ).read_text(encoding="utf-8")
    ) == registry.catalog()
    assert tbm.EffectContract is EffectContract
    assert tbm.EFFECT_RECEIPT_EVENT_TYPES == EFFECT_RECEIPT_EVENT_TYPES


def test_request_authorization_attempt_provider_request_receipt_and_success_reduce_exactly():
    input_artifact = _artifact("effect-input", "application/json")
    contract = _contract(input_artifact=input_artifact)
    events = _events(_success_drafts(contract, input_artifact))
    assert tuple(event.event_type for event in events) == (
        EFFECT_REQUESTED,
        EFFECT_AUTHORIZED,
        EFFECT_STARTED,
        EFFECT_PROVIDER_REQUEST_RECORDED,
        EFFECT_RECEIPT_RECORDED,
        EFFECT_SUCCEEDED,
    )
    projection = effect_projection(events, contract.effect_id)
    assert projection.status == "succeeded"
    assert projection.attempt_count == 1
    assert projection.provider_request_id == "provider-request-001"
    assert projection.receipt_sha256 == events[4].payload["receipt_sha256"]
    assert projection.terminal_event_id == events[-1].event_id
    assert len(projection.projection_sha256) == 71
    for event in events:
        verify_effect_receipt_event(event)


def test_authorization_must_match_trusted_access_before_append():
    artifact = _artifact("auth-input", "application/json")
    contract = _contract(input_artifact=artifact)
    draft = build_effect_requested_draft(
        contract, input_artifact=artifact, occurred_at=T0
    )
    with pytest.raises(EffectReceiptV1Error, match="trusted ledger access"):
        build_effect_receipt_batch(
            (draft,),
            access=_access("authorization_other"),
            expected_stream_version=0,
            next_global_position=1,
            prior_stream_events=(),
            recorded_at=T7,
        )


def test_timeout_is_unknown_and_cannot_be_blindly_retried_or_dead_lettered():
    artifact = _artifact("unknown-input", "application/json")
    contract = _contract(input_artifact=artifact)
    provider = _provider()
    prefix = (
        build_effect_requested_draft(contract, input_artifact=artifact, occurred_at=T0),
        build_effect_authorized_draft(contract, occurred_at=T1),
        build_effect_started_draft(
            contract, provider=provider, attempt_number=1, occurred_at=T2
        ),
        build_effect_result_unknown_draft(
            contract,
            provider=provider,
            attempt_number=1,
            provider_request_id=None,
            unknown_reason="timeout",
            occurred_at=T3,
        ),
    )
    unknown_events = _events(prefix)
    assert effect_projection(unknown_events, contract.effect_id).status == "unknown"
    retry = build_effect_retry_scheduled_draft(
        contract,
        provider=provider,
        attempt_number=1,
        provider_request_id=None,
        retry_at=T6,
        occurred_at=T4,
    )
    with pytest.raises(EffectReceiptV1Error, match="invalid from the current"):
        _events((retry,), start=5, prior=unknown_events)


def test_unknown_result_requires_reconciliation_before_retry_then_dead_letters_at_bound():
    artifact = _artifact("retry-input", "application/json")
    contract = _contract(input_artifact=artifact, max_attempts=2)
    provider = _provider()
    first = _events(
        (
            build_effect_requested_draft(contract, input_artifact=artifact, occurred_at=T0),
            build_effect_authorized_draft(contract, occurred_at=T1),
            build_effect_started_draft(
                contract, provider=provider, attempt_number=1, occurred_at=T2
            ),
            build_effect_result_unknown_draft(
                contract,
                provider=provider,
                attempt_number=1,
                provider_request_id=None,
                unknown_reason="response_lost",
                occurred_at=T3,
            ),
            build_effect_failed_draft(
                contract,
                provider=provider,
                attempt_number=1,
                provider_request_id=None,
                failure_phase="reconciled_absent",
                failure_code="provider_absent",
                retryable=True,
                occurred_at=T4,
                reconciliation_artifact=_artifact(
                    "provider-absence-proof", "application/provider-reconciliation+json"
                ),
            ),
            build_effect_retry_scheduled_draft(
                contract,
                provider=provider,
                attempt_number=1,
                provider_request_id=None,
                retry_at=T6,
                occurred_at=T5,
            ),
        )
    )
    second = _events(
        (
            build_effect_started_draft(
                contract, provider=provider, attempt_number=2, occurred_at=T6
            ),
            build_effect_failed_draft(
                contract,
                provider=provider,
                attempt_number=2,
                provider_request_id=None,
                failure_phase="pre_send",
                failure_code="network_unavailable",
                retryable=True,
                occurred_at=T6,
            ),
            build_effect_dead_lettered_draft(
                contract,
                provider=provider,
                attempt_number=2,
                provider_request_id=None,
                occurred_at=T7,
            ),
        ),
        start=7,
        prior=first,
    )
    projection = effect_projection((*first, *second), contract.effect_id)
    assert projection.status == "dead_lettered"
    assert projection.attempt_count == 2


def test_unknown_cannot_resolve_without_evidence_or_invent_provider_request_id():
    artifact = _artifact("reconcile-input", "application/json")
    contract = _contract(input_artifact=artifact)
    provider = _provider()
    prefix = _events(
        (
            build_effect_requested_draft(contract, input_artifact=artifact, occurred_at=T0),
            build_effect_authorized_draft(contract, occurred_at=T1),
            build_effect_started_draft(
                contract, provider=provider, attempt_number=1, occurred_at=T2
            ),
            build_effect_result_unknown_draft(
                contract,
                provider=provider,
                attempt_number=1,
                provider_request_id=None,
                unknown_reason="acknowledgement_uncertain",
                occurred_at=T3,
            ),
        )
    )
    with pytest.raises(EffectReceiptV1Error, match="reconciliation Artifact"):
        build_effect_failed_draft(
            contract,
            provider=provider,
            attempt_number=1,
            provider_request_id=None,
            failure_phase="reconciled_absent",
            failure_code="provider_absent",
            retryable=True,
            occurred_at=T4,
        )
    reconciled = build_effect_failed_draft(
        contract,
        provider=provider,
        attempt_number=1,
        provider_request_id="invented-provider-id",
        failure_phase="reconciled_absent",
        failure_code="provider_absent",
        retryable=True,
        occurred_at=T4,
        reconciliation_artifact=_artifact(
            "absence-proof", "application/provider-reconciliation+json"
        ),
    )
    with pytest.raises(EffectReceiptV1Error, match="cannot invent"):
        _events((reconciled,), start=5, prior=prefix)


def test_effect_idempotency_key_is_derived_from_exact_intent():
    artifact = _artifact("idempotency-input", "application/json")
    valid = _contract(input_artifact=artifact)
    with pytest.raises(EffectReceiptV1Error, match="exact immutable intent"):
        replace(valid, idempotency_key_sha256=_digest("caller-selected"))


def test_receipt_mismatch_and_provider_request_rebinding_fail_closed():
    artifact = _artifact("mismatch-input", "application/json")
    contract = _contract(input_artifact=artifact)
    drafts = list(_success_drafts(contract, artifact))
    terminal = drafts[-1]
    drafts[-1] = replace(terminal, receipt_sha256=_digest("forged-receipt"))
    with pytest.raises(EffectReceiptV1Error, match="exact recorded receipt"):
        _events(tuple(drafts))

    contract_b = _contract("effect_002", input_artifact=_artifact("b", "application/json"))
    first = _events(_success_drafts(contract, artifact, provider_request_id="duplicate-id"))
    with pytest.raises(EffectReceiptV1Error, match="conflict"):
        _events(
            _success_drafts(
                contract_b,
                _artifact("b", "application/json"),
                provider_request_id="duplicate-id",
            ),
            start=7,
            related=first,
        )


def test_compensation_is_a_distinct_authorized_child_and_never_mutates_parent():
    parent_artifact = _artifact("parent-input", "application/json")
    parent_contract = _contract(input_artifact=parent_artifact)
    parent_events = _events(_success_drafts(parent_contract, parent_artifact))
    parent = effect_projection(parent_events, parent_contract.effect_id)
    parent_bytes = tuple(event.event_sha256 for event in parent_events)

    child_artifact = _artifact("compensation-input", "application/json")
    child_contract = _contract(
        "effect_compensation_001",
        authorization="authorization_effect_compensation_001",
        input_artifact=child_artifact,
    )
    child_events = _events(
        _success_drafts(
            child_contract,
            child_artifact,
            provider_request_id="provider-compensation-001",
            compensation=True,
            parent=parent,
        ),
        start=7,
        related=parent_events,
    )
    projections = reduce_effect_receipt_events((*parent_events, *child_events))
    assert tuple(item.status for item in projections) == ("succeeded", "compensated")
    assert child_events[0].event_type == EFFECT_COMPENSATION_REQUESTED
    assert child_events[-1].event_type == EFFECT_COMPENSATED
    assert tuple(event.event_sha256 for event in parent_events) == parent_bytes


def test_compensation_rejects_unsupported_or_tampered_parent():
    artifact = _artifact("unsupported-input", "application/json")
    contract = _contract(input_artifact=artifact, compensation_supported=False)
    events = _events(_success_drafts(contract, artifact))
    parent = effect_projection(events, contract.effect_id)
    child_artifact = _artifact("child-input", "application/json")
    child = _contract("effect_child", input_artifact=child_artifact)
    with pytest.raises(EffectReceiptV1Error, match="not explicitly compensable"):
        build_effect_compensation_requested_draft(
            child, input_artifact=child_artifact, parent=parent, occurred_at=T7
        )

    supported_contract = _contract(
        "effect_supported", input_artifact=_artifact("supported", "application/json")
    )
    supported_events = _events(
        _success_drafts(
            supported_contract,
            _artifact("supported", "application/json"),
        )
    )
    supported_parent = effect_projection(
        supported_events, supported_contract.effect_id
    )
    with pytest.raises(EffectReceiptV1Error, match="own authorization"):
        build_effect_compensation_requested_draft(
            child,
            input_artifact=child_artifact,
            parent=supported_parent,
            occurred_at=T7,
        )


def test_event_payload_cannot_carry_raw_provider_bytes_or_forged_artifact_binding():
    artifact = _artifact("secret-input", "application/json")
    contract = _contract(input_artifact=artifact)
    event = _events(
        (build_effect_requested_draft(contract, input_artifact=artifact, occurred_at=T0),)
    )[0]
    forged_payload = dict(event.payload)
    forged_payload["raw_response"] = "sensitive"
    forged = build_canonical_event(
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
        trusted_context=_access().event_trusted_context(),
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=event.request_sha256,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        artifact_refs=event.artifact_refs,
        payload=forged_payload,
    )
    with pytest.raises(EffectReceiptV1Error, match="sealed event type"):
        verify_effect_receipt_event(forged)


def test_sqlite_append_is_atomic_exactly_idempotent_and_receipt_verified():
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    artifact = _artifact("atomic-input", "application/json")
    contract = _contract(input_artifact=artifact)
    drafts = _success_drafts(contract, artifact)

    connection.execute("BEGIN IMMEDIATE")
    rolled_back = append_effect_receipt_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        recorded_at=T7,
    )
    assert rolled_back.current_stream_version == 6
    connection.rollback()
    assert ledger.read_stream(effect_receipt_stream_id(contract.effect_id)).events == ()

    committed = append_effect_receipt_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        recorded_at=T7,
    )
    replayed = append_effect_receipt_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        recorded_at=T7,
    )
    assert replayed == committed
    assert len(ledger.read_stream(committed.stream_id).events) == 6
