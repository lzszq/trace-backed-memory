from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from threading import Barrier, Event

import pytest

import trace_backed_memory as tbm


DIGEST_A = "sha256:" + "a" * 64
EVALUATION_ID = "system_gate_sha256_" + "b" * 64
PROMPT = b"Evaluate the prepared memory candidates."


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 8, 3, 0, 3, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(milliseconds=100)
        return value.isoformat().replace("+00:00", "Z")


def _access(
    *,
    actor_type: str,
    actor_id: str,
) -> tbm.LedgerAccessContext:
    return tbm.LedgerAccessContext(
        partition=tbm.LedgerTenantPartition(
            "organization_001",
            "tenant_001",
            "repository_001",
            "environment_local",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type=actor_type,
        actor_id=actor_id,
        authorization_decision_id="authorization_transition_001",
        classification_filter=tbm.LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _provider() -> tbm.TrustedProviderEffectRegistration:
    return tbm.TrustedProviderEffectRegistration(
        provider_id="provider_001",
        model_id="model_001",
        model_version="v1",
        endpoint_id="endpoint_001",
    )


def _call() -> tbm.SemanticProviderCall:
    return tbm.SemanticProviderCall(
        provider_id="provider_001",
        model_id="model_001",
        model_version="v1",
        endpoint_id="endpoint_001",
        prompt=PROMPT,
    )


def _result() -> tbm.SemanticProviderResult:
    return tbm.SemanticProviderResult(
        response=b'{"decision":"allow"}',
        provider_request_id="provider_request_001",
        decision_id="decision_001",
        final_allowed_revision_ids=(),
        final_blocked_revision_ids=(),
        reason="No candidate was reopened.",
        risk="low",
        recommended_injection="none",
    )


def _seed_awaiting_session(
    ledger: tbm.SQLiteEventLedgerV1,
) -> tbm.GateSession:
    created = tbm.create_gate_session(
        session_id="gate_session_provider_effect_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=DIGEST_A,
        idempotency_key="provider-effect-session-001",
        created_at="2026-08-03T00:00:00Z",
        expires_at="2026-08-03T01:00:00Z",
    )
    prepared = tbm.transition_gate_session(
        created,
        "prepared",
        expected_version=created.version,
        updated_at="2026-08-03T00:01:00Z",
        lease_expires_at="2026-08-03T00:20:00Z",
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id=EVALUATION_ID,
    )
    awaiting = tbm.transition_gate_session(
        prepared,
        "awaiting_decision",
        expected_version=prepared.version,
        updated_at="2026-08-03T00:02:00Z",
    )
    trusted = ledger.access_context.event_trusted_context()
    previous_session = None
    parent_event = None
    for global_position, session in enumerate(
        (created, prepared, awaiting),
        start=1,
    ):
        event = tbm.build_gate_session_event(
            session,
            previous_session=previous_session,
            parent_event=parent_event,
            global_position=global_position,
            trusted_context=trusted,
        )
        ledger.append(
            event.stream_id,
            event.stream_version - 1,
            (event,),
            tbm.LedgerIdempotency(
                event.idempotency_key_sha256,
                event.request_sha256,
            ),
        )
        previous_session = session
        parent_event = event
    return awaiting


def _service(
    request_ledger: tbm.SQLiteEventLedgerV1,
    *,
    reconcile_provider=None,
) -> tbm.SemanticProviderEffectService:
    provider_ledger = tbm.SQLiteEventLedgerV1(
        getattr(request_ledger, "_connection"),
        _access(
            actor_type="service",
            actor_id="semantic_provider_effect_service",
        ),
    )
    return tbm.SemanticProviderEffectService(
        request_ledger=request_ledger,
        provider_ledger=provider_ledger,
        provider=_provider(),
        clock=_Clock(),
        reconcile_provider=reconcile_provider,
    )


def _effect_id(session: tbm.GateSession) -> str:
    return tbm.semantic_provider_effect_id(
        session_id=session.session_id,
        system_gate_evaluation_id=EVALUATION_ID,
        expected_previous_attempt_id=None,
    )


def test_semantic_provider_effect_records_receipt_and_blocks_duplicate_call() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        service = _service(ledger)
        calls: list[tbm.SemanticProviderCall] = []

        returned = service.invoke(
            session_id=session.session_id,
            expected_previous_attempt_id=None,
            call=_call(),
            call_provider=lambda call: (calls.append(call), _result())[1],
        )
        assert returned == _result()
        assert calls == [_call()]

        page = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        assert tuple(event.event_type for event in page.events) == (
            tbm.EFFECT_REQUESTED_EVENT,
            tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
            tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
            tbm.EFFECT_PROVIDER_TRANSITION_EVENT,
        )
        assert tuple(event.actor_type for event in page.events) == (
            "agent_client",
            "service",
            "service",
            "service",
        )
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in page.events[1:]
        ) == (
            "attempt_started",
            "request_submitted",
            "receipt_recorded",
        )

        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ) as raised:
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "provider must not be called twice"
                ),
            )
        assert raised.value.effect_id == _effect_id(session)

        changed_call = tbm.SemanticProviderCall(
            provider_id="provider_001",
            model_id="model_001",
            model_version="v1",
            endpoint_id="endpoint_001",
            prompt=b"A changed prompt must not create another effect.",
        )
        reconciliations: list[tbm.SemanticProviderReconciliationCall] = []
        recoverable_service = _service(
            ledger,
            reconcile_provider=lambda call: (
                reconciliations.append(call),
                tbm.SemanticProviderReconciliationResult(
                    "confirmed",
                    _result(),
                ),
            )[1],
        )
        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ) as changed:
            recoverable_service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=changed_call,
                call_provider=lambda _call: pytest.fail(
                    "changed prompt must not call the provider"
                ),
            )
        assert changed.value.effect_id == _effect_id(session)
        assert reconciliations == []


def test_semantic_provider_effect_concurrent_request_invokes_provider_once() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        barrier = Barrier(2)

        class _SynchronizedEmptyRead:
            def __init__(self) -> None:
                self.access_context = ledger.access_context

            @property
            def authority_identity(self):
                return ledger.authority_identity

            def read_stream(self, stream_id, from_version=1, limit=100):
                page = ledger.read_stream(stream_id, from_version, limit)
                if stream_id.startswith("effect_") and not page.events:
                    barrier.wait(timeout=5)
                return page

            def append(self, stream_id, expected_version, events, idempotency):
                return ledger.append(
                    stream_id,
                    expected_version,
                    events,
                    idempotency,
                )

            def append_once(
                self,
                stream_id,
                expected_version,
                events,
                idempotency,
            ):
                return ledger.append_once(
                    stream_id,
                    expected_version,
                    events,
                    idempotency,
                )

            def close(self):
                return None

        services = tuple(
            tbm.SemanticProviderEffectService(
                request_ledger=_SynchronizedEmptyRead(),
                provider_ledger=tbm.SQLiteEventLedgerV1(
                    getattr(ledger, "_connection"),
                    _access(
                        actor_type="service",
                        actor_id="semantic_provider_effect_service",
                    ),
                ),
                provider=_provider(),
                clock=_Clock(),
                owns_ledgers=True,
            )
            for _ in range(2)
        )
        provider_calls: list[tbm.SemanticProviderCall] = []

        def invoke(service):
            try:
                return service.invoke(
                    session_id=session.session_id,
                    expected_previous_attempt_id=None,
                    call=_call(),
                    call_provider=lambda call: (
                        provider_calls.append(call),
                        _result(),
                    )[1],
                )
            except tbm.SemanticProviderEffectRecoveryRequiredError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(invoke, services))
        for service in services:
            service.close()

        assert sum(
            type(outcome) is tbm.SemanticProviderResult for outcome in outcomes
        ) == 1
        assert sum(
            isinstance(
                outcome,
                tbm.SemanticProviderEffectRecoveryRequiredError,
            )
            for outcome in outcomes
        ) == 1
        assert provider_calls == [_call()]
        page = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in page.events[1:]
        ) == (
            "attempt_started",
            "request_submitted",
            "receipt_recorded",
        )


def test_semantic_provider_effect_does_not_recover_active_attempt() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        owner_service = _service(ledger)
        recovery_service = _service(ledger)
        provider_entered = Event()
        release_provider = Event()

        def invoke_provider(
            _call: tbm.SemanticProviderCall,
        ) -> tbm.SemanticProviderResult:
            provider_entered.set()
            if not release_provider.wait(timeout=5):
                raise RuntimeError("provider release timed out")
            return _result()

        with ThreadPoolExecutor(max_workers=1) as executor:
            owner = executor.submit(
                owner_service.invoke,
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=invoke_provider,
            )
            assert provider_entered.wait(timeout=5)
            try:
                with pytest.raises(
                    tbm.SemanticProviderEffectRecoveryRequiredError
                ):
                    recovery_service.invoke(
                        session_id=session.session_id,
                        expected_previous_attempt_id=None,
                        call=_call(),
                        call_provider=lambda _call: pytest.fail(
                            "active recovery must not call the provider"
                        ),
                    )
                active = ledger.read_stream(
                    tbm.effect_event_stream_id(_effect_id(session)),
                    limit=100,
                )
                assert tuple(
                    tbm.parse_provider_effect_transition_event(event).stage
                    for event in active.events[1:]
                ) == ("attempt_started",)
            finally:
                release_provider.set()
            assert owner.result(timeout=5) == _result()

        completed = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in completed.events[1:]
        ) == (
            "attempt_started",
            "request_submitted",
            "receipt_recorded",
        )


def test_semantic_provider_effect_rejects_unretained_attempt_parent() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        service = _service(ledger)
        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ) as raised:
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=(
                    "semantic_attempt_sha256_" + "f" * 64
                ),
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "unretained parent must prevent provider invocation"
                ),
            )
        assert raised.value.effect_id == "effect_parent_mismatch"


def test_semantic_provider_effect_rejects_mixed_authorities() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as request_ledger, tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="service",
            actor_id="semantic_provider_effect_service",
        ),
        initialize=True,
    ) as provider_ledger:
        with pytest.raises(ValueError, match="incompatible access"):
            tbm.SemanticProviderEffectService(
                request_ledger=request_ledger,
                provider_ledger=provider_ledger,
                provider=_provider(),
                clock=_Clock(),
            )


def test_semantic_provider_effect_records_unknown_before_recovery() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        service = _service(ledger)

        def timeout(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
            raise tbm.SemanticProviderCallError(
                "provider_timeout",
                provider_request_id="provider_request_001",
            )

        with pytest.raises(tbm.SemanticProviderEffectRecoveryRequiredError):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=timeout,
            )

        page = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in page.events[1:]
        ) == (
            "attempt_started",
            "request_submitted",
            "result_unknown",
        )


def test_semantic_provider_effect_recovers_retained_receipt() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        reconciliations: list[tbm.SemanticProviderReconciliationCall] = []

        def reconcile(
            call: tbm.SemanticProviderReconciliationCall,
        ) -> tbm.SemanticProviderReconciliationResult:
            reconciliations.append(call)
            return tbm.SemanticProviderReconciliationResult(
                "confirmed",
                _result(),
            )

        service = _service(ledger, reconcile_provider=reconcile)
        assert service.invoke(
            session_id=session.session_id,
            expected_previous_attempt_id=None,
            call=_call(),
            call_provider=lambda _call: _result(),
        ) == _result()
        before = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )

        recovered = service.invoke(
            session_id=session.session_id,
            expected_previous_attempt_id=None,
            call=_call(),
            call_provider=lambda _call: pytest.fail(
                "retained receipt must prevent another provider invocation"
            ),
        )
        assert recovered == _result()
        assert len(reconciliations) == 1
        assert reconciliations[0].provider_status == "succeeded"
        assert reconciliations[0].provider_request_id == (
            _result().provider_request_id
        )
        assert reconciliations[0].provider_receipt_id is not None
        after = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        assert after.events == before.events


def test_semantic_provider_effect_rejects_changed_reconciliation_receipt() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        changed_result = replace(
            _result(),
            provider_request_id="provider_request_changed",
        )
        service = _service(
            ledger,
            reconcile_provider=lambda _call: (
                tbm.SemanticProviderReconciliationResult(
                    "confirmed",
                    changed_result,
                )
            ),
        )
        assert service.invoke(
            session_id=session.session_id,
            expected_previous_attempt_id=None,
            call=_call(),
            call_provider=lambda _call: _result(),
        ) == _result()

        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "receipt mismatch must not invoke the provider"
                ),
            )

        structured_changed = replace(
            _result(),
            decision_id="decision_changed",
            risk="high",
            recommended_injection="full",
        )
        assert structured_changed.response == _result().response
        assert tbm.semantic_provider_result_sha256(
            structured_changed
        ) != tbm.semantic_provider_result_sha256(_result())
        structured_service = _service(
            ledger,
            reconcile_provider=lambda _call: (
                tbm.SemanticProviderReconciliationResult(
                    "confirmed",
                    structured_changed,
                )
            ),
        )
        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ):
            structured_service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "structured mismatch must not invoke the provider"
                ),
            )


def test_semantic_provider_effect_reconciles_unknown_as_confirmed() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        reconciliations: list[tbm.SemanticProviderReconciliationCall] = []

        def reconcile(
            call: tbm.SemanticProviderReconciliationCall,
        ) -> tbm.SemanticProviderReconciliationResult:
            reconciliations.append(call)
            return tbm.SemanticProviderReconciliationResult(
                "confirmed",
                _result(),
            )

        service = _service(ledger, reconcile_provider=reconcile)

        def timeout(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
            raise tbm.SemanticProviderCallError(
                "provider_timeout",
                provider_request_id=_result().provider_request_id,
            )

        with pytest.raises(tbm.SemanticProviderEffectRecoveryRequiredError):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=timeout,
            )

        recovered = service.invoke(
            session_id=session.session_id,
            expected_previous_attempt_id=None,
            call=_call(),
            call_provider=lambda _call: pytest.fail(
                "reconciliation must not invoke the provider again"
            ),
        )
        assert recovered == _result()
        assert len(reconciliations) == 1
        assert reconciliations[0].provider_status == "unknown"
        page = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        assert tuple(
            tbm.parse_provider_effect_transition_event(event).stage
            for event in page.events[1:]
        ) == (
            "attempt_started",
            "request_submitted",
            "result_unknown",
            "reconciled",
        )
        reconciled = tbm.parse_provider_effect_transition_event(
            page.events[-1]
        )
        assert reconciled.reconciliation_result == "confirmed"
        assert reconciled.provider_receipt_id is not None


@pytest.mark.parametrize("outcome", ["still_unknown", "not_found"])
def test_semantic_provider_effect_reconciliation_remains_conservative(
    outcome: str,
) -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        service = _service(
            ledger,
            reconcile_provider=lambda _call: (
                tbm.SemanticProviderReconciliationResult(outcome)
            ),
        )

        def timeout(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
            raise tbm.SemanticProviderCallError("provider_timeout")

        with pytest.raises(tbm.SemanticProviderEffectRecoveryRequiredError):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=timeout,
            )
        with pytest.raises(tbm.SemanticProviderEffectRecoveryRequiredError):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "reconciliation must not call the provider"
                ),
            )

        page = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        reconciled = tbm.parse_provider_effect_transition_event(
            page.events[-1]
        )
        assert reconciled.stage == "reconciled"
        assert reconciled.reconciliation_result == outcome


def test_semantic_provider_effect_confirms_after_still_unknown() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        outcomes = iter(("still_unknown", "confirmed"))

        def reconcile(
            _call: tbm.SemanticProviderReconciliationCall,
        ) -> tbm.SemanticProviderReconciliationResult:
            outcome = next(outcomes)
            if outcome == "confirmed":
                return tbm.SemanticProviderReconciliationResult(
                    "confirmed",
                    _result(),
                )
            return tbm.SemanticProviderReconciliationResult(
                "still_unknown"
            )

        service = _service(ledger, reconcile_provider=reconcile)

        def timeout(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
            raise tbm.SemanticProviderCallError(
                "provider_timeout",
                provider_request_id=_result().provider_request_id,
            )

        with pytest.raises(tbm.SemanticProviderEffectRecoveryRequiredError):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=timeout,
            )
        with pytest.raises(tbm.SemanticProviderEffectRecoveryRequiredError):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "reconciliation must not call the provider"
                ),
            )
        assert service.invoke(
            session_id=session.session_id,
            expected_previous_attempt_id=None,
            call=_call(),
            call_provider=lambda _call: pytest.fail(
                "reconciliation must not call the provider"
            ),
        ) == _result()

        page = ledger.read_stream(
            tbm.effect_event_stream_id(_effect_id(session)),
            limit=100,
        )
        reconciliations = tuple(
            tbm.parse_provider_effect_transition_event(event)
            for event in page.events[1:]
            if tbm.parse_provider_effect_transition_event(event).stage
            == "reconciled"
        )
        assert tuple(
            item.reconciliation_sequence for item in reconciliations
        ) == (1, 2)
        assert tuple(
            item.reconciliation_result for item in reconciliations
        ) == ("still_unknown", "confirmed")


def test_semantic_provider_effect_read_failure_requires_recovery() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)

        class _EffectReadFailure:
            @property
            def access_context(self):
                return ledger.access_context

            @property
            def authority_identity(self):
                return ledger.authority_identity

            def read_stream(self, stream_id, from_version=1, limit=100):
                if stream_id.startswith("effect_"):
                    raise RuntimeError("simulated ledger read failure")
                return ledger.read_stream(stream_id, from_version, limit)

            def append(self, stream_id, expected_version, events, idempotency):
                return ledger.append(
                    stream_id,
                    expected_version,
                    events,
                    idempotency,
                )

            def append_once(
                self,
                stream_id,
                expected_version,
                events,
                idempotency,
            ):
                return ledger.append_once(
                    stream_id,
                    expected_version,
                    events,
                    idempotency,
                )

            def close(self):
                return None

        provider_ledger = tbm.SQLiteEventLedgerV1(
            getattr(ledger, "_connection"),
            _access(
                actor_type="service",
                actor_id="semantic_provider_effect_service",
            ),
        )
        service = tbm.SemanticProviderEffectService(
            request_ledger=_EffectReadFailure(),
            provider_ledger=provider_ledger,
            provider=_provider(),
            clock=_Clock(),
        )
        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ) as raised:
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "ledger failure must prevent provider invocation"
                ),
            )
        assert raised.value.effect_id == _effect_id(session)


def test_semantic_provider_effect_request_only_state_blocks_provider() -> None:
    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        parent = ledger.read_stream(session.session_id, limit=100).events[-1]
        effect_id = _effect_id(session)
        requested = tbm.build_effect_requested_event(
            tbm.EffectRequestedRef(
                tbm.EffectContract(
                    effect_id=effect_id,
                    effect_type="semantic_provider_call",
                    idempotency_key=effect_id,
                    requested_by_event_id=parent.event_id,
                    input_artifact_sha256=(
                        "sha256:" + hashlib.sha256(PROMPT).hexdigest()
                    ),
                    authorization_event_id=(
                        ledger.access_context.authorization_decision_id
                    ),
                    compensation_supported=False,
                )
            ),
            requested_by_event=parent,
            global_position=parent.global_position + 1,
            trusted_context=ledger.access_context.event_trusted_context(),
        )
        ledger.append(
            requested.stream_id,
            0,
            (requested,),
            tbm.LedgerIdempotency(
                requested.idempotency_key_sha256,
                requested.request_sha256,
            ),
        )
        reconciliations: list[tbm.SemanticProviderReconciliationCall] = []
        service = _service(
            ledger,
            reconcile_provider=lambda call: (
                reconciliations.append(call),
                tbm.SemanticProviderReconciliationResult("not_found"),
            )[1],
        )

        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "request-only state must not invoke provider twice"
                ),
            )
        assert reconciliations == []


def test_semantic_provider_effect_rejects_invalid_callbacks() -> None:
    with pytest.raises(ValueError):
        tbm.SemanticProviderReconciliationResult("confirmed")
    with pytest.raises(ValueError):
        tbm.SemanticProviderReconciliationResult(
            "still_unknown",
            _result(),
        )

    with tbm.SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(
            actor_type="agent_client",
            actor_id="agent_client_001",
        ),
        initialize=True,
    ) as ledger:
        session = _seed_awaiting_session(ledger)
        provider_ledger = tbm.SQLiteEventLedgerV1(
            getattr(ledger, "_connection"),
            _access(
                actor_type="service",
                actor_id="semantic_provider_effect_service",
            ),
        )
        with pytest.raises(TypeError):
            tbm.SemanticProviderEffectService(
                request_ledger=ledger,
                provider_ledger=provider_ledger,
                provider=_provider(),
                clock=_Clock(),
                reconcile_provider=object(),
            )

        service = _service(
            ledger,
            reconcile_provider=lambda _call: (_ for _ in ()).throw(
                RuntimeError("simulated reconciliation failure")
            ),
        )
        assert service.invoke(
            session_id=session.session_id,
            expected_previous_attempt_id=None,
            call=_call(),
            call_provider=lambda _call: _result(),
        ) == _result()
        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ):
            service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "failed reconciliation must not invoke the provider"
                ),
            )

        invalid_service = _service(
            ledger,
            reconcile_provider=lambda _call: object(),
        )
        with pytest.raises(
            tbm.SemanticProviderEffectRecoveryRequiredError
        ):
            invalid_service.invoke(
                session_id=session.session_id,
                expected_previous_attempt_id=None,
                call=_call(),
                call_provider=lambda _call: pytest.fail(
                    "invalid reconciliation must not invoke the provider"
                ),
            )
