from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.completion_provider_effect_v1 import (
    CompletionProviderCall,
    CompletionProviderCallError,
    CompletionProviderEffectConsumer,
    CompletionProviderEffectRecoveryRequiredError,
    CompletionProviderReconciliationCall,
    CompletionProviderReconciliationResult,
    CompletionProviderResult,
)


ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "a" * 64


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 29, 0, 7, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self.current
        self.current += timedelta(seconds=1)
        return value.isoformat().replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass(frozen=True)
class _Claim:
    event: tbm.CompletionOutboxEvent
    delivery: tbm.CompletionOutboxDelivery


class _LedgerBackedRepository:
    def __init__(
        self,
        ledger: tbm.SQLiteEventLedgerV1,
        event: tbm.CompletionOutboxEvent,
        delivery: tbm.CompletionOutboxDelivery,
        clock: _Clock,
    ) -> None:
        self.ledger = ledger
        self.event = event
        self.current = delivery
        self.clock = clock
        self.ack_failures = 0

    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int = 100,
    ) -> tuple[_Claim, ...]:
        assert limit >= 1
        if self.current.status in {"delivered", "dead_letter"}:
            return ()
        previous = self.current
        current = tbm.claim_completion_outbox_delivery(
            previous,
            worker_id=worker_id,
            claimed_at=self.clock(),
            lease_seconds=lease_seconds,
        )
        self._append_delivery(previous, current)
        self.current = current
        return (_Claim(self.event, current),)

    def get_delivery(self, event_id: str) -> tbm.CompletionOutboxDelivery:
        assert event_id == self.event.event_id
        return self.current

    def acknowledge(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        response_sha256: str | None = None,
    ) -> tbm.CompletionOutboxDelivery:
        assert event_id == self.event.event_id
        assert expected_version == self.current.version
        if self.ack_failures:
            self.ack_failures -= 1
            raise RuntimeError("ack failed before commit")
        previous = self.current
        current = tbm.acknowledge_completion_outbox_delivery(
            previous,
            worker_id=worker_id,
            acknowledged_at=self.clock(),
            response_sha256=response_sha256,
        )
        self._append_delivery(previous, current)
        self.current = current
        return current

    def fail_delivery(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ) -> tbm.CompletionOutboxDelivery:
        assert event_id == self.event.event_id
        assert expected_version == self.current.version
        previous = self.current
        current = tbm.fail_completion_outbox_delivery(
            previous,
            worker_id=worker_id,
            failed_at=self.clock(),
            error_code=error_code,
            retry_delay_seconds=retry_delay_seconds,
            max_attempts=max_attempts,
        )
        self._append_delivery(previous, current)
        self.current = current
        return current

    def _append_delivery(
        self,
        previous: tbm.CompletionOutboxDelivery,
        current: tbm.CompletionOutboxDelivery,
    ) -> None:
        page = self.ledger.read_stream(
            tbm.effect_event_stream_id(self.event.event_id),
            limit=1000,
        )
        parent = page.events[-1]
        events = tbm.build_effect_delivery_event_batch(
            previous,
            current,
            parent_event=parent,
            first_global_position=page.high_watermark_global_position + 1,
            trusted_context=self.ledger.access_context.event_trusted_context(),
        )
        self.ledger.append(
            parent.stream_id,
            parent.stream_version,
            events,
            tbm.LedgerIdempotency(
                events[0].idempotency_key_sha256,
                events[0].request_sha256,
            ),
        )


def _access(
    *,
    actor_type: str,
    actor_id: str,
) -> tbm.LedgerAccessContext:
    return tbm.LedgerAccessContext(
        partition=tbm.LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_local",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type=actor_type,
        actor_id=actor_id,
        authorization_decision_id="authorization_decision_001",
        classification_filter=tbm.LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _provider() -> tbm.TrustedProviderEffectRegistration:
    return tbm.TrustedProviderEffectRegistration(
        provider_id="completion_provider_001",
        model_id="completion_adapter_001",
        model_version="v1",
        endpoint_id="local_endpoint_001",
    )


def _event() -> tbm.CompletionOutboxEvent:
    return tbm.loads_completion_outbox_event(
        (
            ROOT / "examples" / "completion_outbox_event_v3.example.json"
        ).read_text(encoding="utf-8")
    )


def _seed(
    request_ledger: tbm.SQLiteEventLedgerV1,
) -> tuple[tbm.CompletionOutboxEvent, tbm.CompletionOutboxDelivery]:
    event = _event()
    initial = tbm.build_initial_completion_outbox_delivery(event)
    trusted = request_ledger.access_context.event_trusted_context()
    parent = tbm.build_canonical_event(
        event_id="evt_completion_provider_parent_001",
        event_type="tbm.test.completion_provider_authorized",
        event_version=1,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id="completion_provider_parent_stream_001",
        stream_type="test_command",
        stream_version=1,
        global_position=1,
        trusted_context=trusted,
        request_id="completion_provider_parent_request_001",
        idempotency_key_sha256=DIGEST_A,
        request_sha256=event.outcome_descriptor_sha256,
        correlation_id="completion_provider_correlation_001",
        causation_id=None,
        occurred_at=event.occurred_at,
        recorded_at=event.occurred_at,
        producer="trace_backed_memory",
        producer_version="0.1.0",
        payload_schema="tbm.test.completion_provider_authorized.v1",
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id="retention_engineering_memory",
        artifact_refs=(),
        payload={"authorized": True},
    )
    request_ledger.append(
        parent.stream_id,
        0,
        (parent,),
        tbm.LedgerIdempotency(
            parent.idempotency_key_sha256,
            parent.request_sha256,
        ),
    )
    contract = tbm.EffectContract(
        effect_id=event.event_id,
        effect_type="completion_notification",
        idempotency_key=event.event_id,
        requested_by_event_id=parent.event_id,
        input_artifact_sha256=event.outcome_descriptor_sha256,
        authorization_event_id=trusted.authorization_decision_id,
        compensation_supported=False,
    )
    requested = tbm.build_effect_requested_event(
        tbm.EffectRequestedRef(contract, event, initial),
        requested_by_event=parent,
        global_position=2,
        trusted_context=trusted,
    )
    request_ledger.append(
        requested.stream_id,
        0,
        (requested,),
        tbm.LedgerIdempotency(
            requested.idempotency_key_sha256,
            requested.request_sha256,
        ),
    )
    return event, initial


def _setup(clock: _Clock):
    request_ledger = tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(actor_type="agent_client", actor_id="agent_client_001"),
        initialize=True,
    )
    event, initial = _seed(request_ledger)
    worker_ledger = tbm.SQLiteEventLedgerV1(
        getattr(request_ledger, "_connection"),
        _access(actor_type="worker", actor_id="worker_001"),
    )
    repository = _LedgerBackedRepository(
        worker_ledger,
        event,
        initial,
        clock,
    )
    return request_ledger, worker_ledger, repository


def _result(
    request_id: str = "provider_request_001",
) -> CompletionProviderResult:
    return CompletionProviderResult(request_id, DIGEST_A)


def _provider_stages(
    ledger: tbm.SQLiteEventLedgerV1,
    event_id: str,
) -> tuple[str, ...]:
    page = ledger.read_stream(tbm.effect_event_stream_id(event_id), limit=1000)
    return tuple(
        tbm.parse_provider_effect_transition_event(event).stage
        for event in page.events
        if event.event_type == tbm.EFFECT_PROVIDER_TRANSITION_EVENT
    )


def test_completion_provider_bridge_replays_receipt_after_ack_response_loss():
    clock = _Clock()
    request_ledger, worker_ledger, repository = _setup(clock)
    repository.ack_failures = 1
    calls: list[CompletionProviderCall] = []
    consumer = CompletionProviderEffectConsumer(
        ledger=worker_ledger,
        provider=_provider(),
        call_provider=lambda call: (calls.append(call), _result())[1],
        clock=clock,
    )
    worker = tbm.CompletionOutboxDeliveryWorker(repository, consumer)
    try:
        first = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
        )
        assert first[0].outcome == "recovery_required"
        assert repository.current.status == "leased"

        clock.advance(40)
        second = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
        )
        assert second[0].outcome == "delivered"
        assert repository.current.response_sha256 == DIGEST_A
        assert len(calls) == 1
        assert calls[0].idempotency_key == repository.event.event_id
        assert calls[0].event == repository.event
        assert _provider_stages(worker_ledger, repository.event.event_id) == (
            "attempt_started",
            "request_submitted",
            "receipt_recorded",
        )
    finally:
        worker_ledger.close()
        request_ledger.close()


def test_completion_provider_bridge_reconciles_unknown_without_recalling():
    clock = _Clock()
    request_ledger, worker_ledger, repository = _setup(clock)
    calls: list[CompletionProviderCall] = []
    reconciliations: list[CompletionProviderReconciliationCall] = []

    def call_provider(call: CompletionProviderCall) -> CompletionProviderResult:
        calls.append(call)
        raise CompletionProviderCallError(
            "provider_timeout",
            provider_request_id="provider_request_001",
        )

    def reconcile(
        call: CompletionProviderReconciliationCall,
    ) -> CompletionProviderReconciliationResult:
        reconciliations.append(call)
        return CompletionProviderReconciliationResult(
            "confirmed",
            _result(),
        )

    consumer = CompletionProviderEffectConsumer(
        ledger=worker_ledger,
        provider=_provider(),
        call_provider=call_provider,
        clock=clock,
        reconcile_provider=reconcile,
    )
    worker = tbm.CompletionOutboxDeliveryWorker(repository, consumer)
    try:
        first = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert first[0].outcome == "retry_wait"
        assert repository.current.last_error_code == (
            "TBM_COMPLETION_PROVIDER_RECOVERY_REQUIRED"
        )

        clock.advance(5)
        second = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert second[0].outcome == "delivered"
        assert repository.current.response_sha256 == DIGEST_A
        assert len(calls) == 1
        assert len(reconciliations) == 1
        assert reconciliations[0].provider_request_id == (
            "provider_request_001"
        )
        assert _provider_stages(worker_ledger, repository.event.event_id) == (
            "attempt_started",
            "request_submitted",
            "result_unknown",
            "reconciled",
        )
    finally:
        worker_ledger.close()
        request_ledger.close()


def test_completion_provider_bridge_retries_only_after_reconciled_not_found():
    clock = _Clock()
    request_ledger, worker_ledger, repository = _setup(clock)
    calls: list[CompletionProviderCall] = []
    reconciliation_calls: list[CompletionProviderReconciliationCall] = []

    def call_provider(call: CompletionProviderCall) -> CompletionProviderResult:
        calls.append(call)
        if len(calls) == 1:
            raise CompletionProviderCallError(
                "provider_timeout",
                provider_request_id="provider_request_001",
            )
        return _result("provider_request_002")

    def reconcile(
        call: CompletionProviderReconciliationCall,
    ) -> CompletionProviderReconciliationResult:
        reconciliation_calls.append(call)
        return CompletionProviderReconciliationResult("not_found")

    consumer = CompletionProviderEffectConsumer(
        ledger=worker_ledger,
        provider=_provider(),
        call_provider=call_provider,
        clock=clock,
        reconcile_provider=reconcile,
    )
    worker = tbm.CompletionOutboxDeliveryWorker(repository, consumer)
    try:
        first = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert first[0].outcome == "retry_wait"

        clock.advance(5)
        second = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert second[0].outcome == "retry_wait"
        assert repository.current.last_error_code == (
            "TBM_COMPLETION_PROVIDER_NOT_FOUND"
        )
        assert len(calls) == 1

        clock.advance(5)
        third = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert third[0].outcome == "delivered"
        assert len(calls) == 2
        assert len(reconciliation_calls) == 1
        assert _provider_stages(worker_ledger, repository.event.event_id) == (
            "attempt_started",
            "request_submitted",
            "result_unknown",
            "reconciled",
            "retry_scheduled",
            "attempt_started",
            "request_submitted",
            "receipt_recorded",
        )
    finally:
        worker_ledger.close()
        request_ledger.close()


def test_completion_provider_bridge_requires_active_worker_lease_and_sanitizes():
    clock = _Clock()
    request_ledger, worker_ledger, repository = _setup(clock)
    calls: list[CompletionProviderCall] = []
    consumer = CompletionProviderEffectConsumer(
        ledger=worker_ledger,
        provider=_provider(),
        call_provider=lambda call: (calls.append(call), _result())[1],
        clock=clock,
    )
    try:
        with pytest.raises(
            CompletionProviderEffectRecoveryRequiredError,
            match="TBM_COMPLETION_PROVIDER_RECOVERY_REQUIRED",
        ):
            consumer(repository.event)
        assert calls == []

        worker = tbm.CompletionOutboxDeliveryWorker(
            repository,
            CompletionProviderEffectConsumer(
                ledger=worker_ledger,
                provider=_provider(),
                call_provider=lambda _call: (_ for _ in ()).throw(
                    RuntimeError("secret provider body")
                ),
                clock=clock,
            ),
        )
        result = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert result[0].outcome == "retry_wait"
        assert repository.current.last_error_code == (
            "TBM_COMPLETION_PROVIDER_RECOVERY_REQUIRED"
        )
        page = worker_ledger.read_stream(
            tbm.effect_event_stream_id(repository.event.event_id),
            limit=1000,
        )
        assert "secret provider body" not in json.dumps(
            [event.to_dict() for event in page.events],
            sort_keys=True,
        )
    finally:
        worker_ledger.close()
        request_ledger.close()


def test_late_owner_cannot_record_receipt_after_delivery_reclaim():
    clock = _Clock()
    request_ledger, worker_ledger, repository = _setup(clock)
    calls: list[CompletionProviderCall] = []
    reconciliations: list[CompletionProviderReconciliationCall] = []

    def call_provider(call: CompletionProviderCall) -> CompletionProviderResult:
        calls.append(call)
        clock.advance(40)
        reclaimed = repository.claim_due(
            worker_id="worker_001",
            lease_seconds=30,
        )
        assert reclaimed[0].delivery.version == 3
        return _result()

    def reconcile(
        call: CompletionProviderReconciliationCall,
    ) -> CompletionProviderReconciliationResult:
        reconciliations.append(call)
        return CompletionProviderReconciliationResult(
            "confirmed",
            _result(),
        )

    consumer = CompletionProviderEffectConsumer(
        ledger=worker_ledger,
        provider=_provider(),
        call_provider=call_provider,
        clock=clock,
        reconcile_provider=reconcile,
    )
    worker = tbm.CompletionOutboxDeliveryWorker(repository, consumer)
    try:
        first = worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
        )
        assert first[0].outcome == "superseded"
        assert _provider_stages(worker_ledger, repository.event.event_id) == (
            "attempt_started",
        )

        receipt = consumer(repository.event)
        assert receipt.response_sha256 == DIGEST_A
        assert len(calls) == 1
        assert len(reconciliations) == 1
        assert _provider_stages(worker_ledger, repository.event.event_id) == (
            "attempt_started",
            "result_unknown",
            "reconciled",
        )
    finally:
        worker_ledger.close()
        request_ledger.close()


def test_completion_provider_contracts_reject_invalid_values():
    event = _event()
    call = CompletionProviderCall(
        provider_id="completion_provider_001",
        model_id="completion_adapter_001",
        model_version="v1",
        endpoint_id="local_endpoint_001",
        event=event,
    )
    assert call.idempotency_key is None
    assert CompletionProviderResult("provider_request_001", DIGEST_A) == (
        _result()
    )
    with pytest.raises(ValueError):
        CompletionProviderResult("bad request", DIGEST_A)
    with pytest.raises(ValueError):
        CompletionProviderResult("provider_request_001", "sha256:bad")
    with pytest.raises(ValueError):
        CompletionProviderReconciliationResult("confirmed")
    with pytest.raises(ValueError):
        CompletionProviderReconciliationResult(
            "still_unknown",
            _result(),
        )
    with pytest.raises(ValueError):
        CompletionProviderCallError("api_key_ABC123")
    with pytest.raises(ValueError):
        CompletionProviderReconciliationResult([])  # type: ignore[arg-type]


def test_completion_provider_reconciliation_call_is_cross_linked():
    clock = _Clock()
    request_ledger, worker_ledger, repository = _setup(clock)
    retained: list[CompletionProviderReconciliationCall] = []

    def call_provider(_call: CompletionProviderCall) -> CompletionProviderResult:
        raise CompletionProviderCallError("provider_timeout")

    def reconcile(
        call: CompletionProviderReconciliationCall,
    ) -> CompletionProviderReconciliationResult:
        retained.append(call)
        return CompletionProviderReconciliationResult("still_unknown")

    consumer = CompletionProviderEffectConsumer(
        ledger=worker_ledger,
        provider=_provider(),
        call_provider=call_provider,
        clock=clock,
        reconcile_provider=reconcile,
    )
    worker = tbm.CompletionOutboxDeliveryWorker(repository, consumer)
    try:
        worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        clock.advance(5)
        worker.run_once(
            worker_id="worker_001",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        call = retained[0]
        with pytest.raises(ValueError, match="linkage"):
            replace(call, effect_id="unrelated_effect")
        with pytest.raises(ValueError, match="linkage"):
            replace(call, request_sha256="sha256:" + "b" * 64)
        with pytest.raises(ValueError, match="linkage"):
            replace(
                call,
                provider_call=replace(call.provider_call, idempotency_key=None),
            )
    finally:
        worker_ledger.close()
        request_ledger.close()


def test_completion_provider_bridge_rejects_service_actor_for_worker_lease():
    clock = _Clock()
    request_ledger, worker_ledger, repository = _setup(clock)
    service_ledger = tbm.SQLiteEventLedgerV1(
        getattr(request_ledger, "_connection"),
        _access(actor_type="service", actor_id="completion_service_001"),
    )
    try:
        with pytest.raises(ValueError, match="delivery worker"):
            CompletionProviderEffectConsumer(
                ledger=service_ledger,
                provider=_provider(),
                call_provider=lambda _call: _result(),
                clock=clock,
            )
        assert repository.current.status == "pending"
    finally:
        service_ledger.close()
        worker_ledger.close()
        request_ledger.close()


def test_completion_provider_exports_are_intentional():
    assert tbm.CompletionProviderEffectConsumer is CompletionProviderEffectConsumer
    assert tbm.CompletionProviderResult is CompletionProviderResult
    assert "CompletionProviderEffectConsumer" in tbm.__all__
