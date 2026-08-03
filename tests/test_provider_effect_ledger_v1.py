from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory as tbm

from trace_backed_memory.effect_event_v1 import (
    EffectContract,
    EffectEventV1Error,
    EffectRequestedRef,
    ProviderEffectTransitionRef,
    build_effect_requested_event,
    effect_event_stream_id,
    provider_effect_attempt_id,
    provider_effect_invocation_id,
    provider_effect_receipt_id,
    provider_effect_reconciliation_id,
)
from trace_backed_memory.effect_reducer_v1 import (
    projected_effect_status,
)
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    build_canonical_event,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.provider_effect_ledger_v1 import (
    ProviderEffectLedgerService,
    ProviderEffectLedgerV1Error,
    TrustedProviderEffectRegistration,
)
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from tests.postgres_support import PostgresCluster


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_INSTALL = ROOT / "schemas" / "postgres-v3-event-ledger.sql"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
EFFECT_ID = "effect_provider_001"


def _provider() -> TrustedProviderEffectRegistration:
    return TrustedProviderEffectRegistration(
        provider_id="provider_001",
        model_id="model_001",
        model_version="v1",
        endpoint_id="endpoint_001",
    )


def _access(
    *,
    principal_id: str = "principal_001",
    authorization_decision_id: str = "authorization_decision_001",
) -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_local",
        ),
        principal_id=principal_id,
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="provider_effect_service",
        authorization_decision_id=authorization_decision_id,
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _parent(access: LedgerAccessContext) -> CanonicalEvent:
    return build_canonical_event(
        event_id="evt_provider_effect_parent_001",
        event_type="tbm.test.provider_effect_authorized",
        event_version=1,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id="provider_effect_parent_stream_001",
        stream_type="test_command",
        stream_version=1,
        global_position=1,
        trusted_context=access.event_trusted_context(),
        request_id="provider_effect_parent_request_001",
        idempotency_key_sha256=DIGEST_A,
        request_sha256=DIGEST_B,
        correlation_id="provider_effect_correlation_001",
        causation_id=None,
        occurred_at="2026-08-03T00:00:00Z",
        recorded_at="2026-08-03T00:00:00Z",
        producer="trace_backed_memory",
        producer_version="0.1.0",
        payload_schema="tbm.test.provider_effect_authorized.v1",
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id="retention_engineering_memory",
        artifact_refs=(),
        payload={"authorized": True},
    )


def _seed(ledger, access: LedgerAccessContext) -> None:
    parent = _parent(access)
    ledger.append(
        parent.stream_id,
        0,
        (parent,),
        LedgerIdempotency(
            parent.idempotency_key_sha256,
            parent.request_sha256,
        ),
    )
    effect = EffectContract(
        effect_id=EFFECT_ID,
        effect_type="semantic_provider_call",
        idempotency_key="semantic-provider-call-001",
        requested_by_event_id=parent.event_id,
        input_artifact_sha256=DIGEST_A,
        authorization_event_id=access.authorization_decision_id,
        compensation_supported=True,
    )
    requested = build_effect_requested_event(
        EffectRequestedRef(effect),
        requested_by_event=parent,
        global_position=2,
        trusted_context=access.event_trusted_context(),
    )
    ledger.append(
        requested.stream_id,
        0,
        (requested,),
        LedgerIdempotency(
            requested.idempotency_key_sha256,
            requested.request_sha256,
        ),
    )


def _transition(
    stage,
    *,
    attempt_sequence: int = 1,
    provider_request_id: str | None = None,
    response_sha256: str | None = None,
    error_code: str | None = None,
    reconciliation_sequence: int | None = None,
    reconciliation_result=None,
    retry_at: str | None = None,
) -> ProviderEffectTransitionRef:
    attempt_id = provider_effect_attempt_id(EFFECT_ID, attempt_sequence)
    invocation_id = provider_effect_invocation_id(
        effect_id=EFFECT_ID,
        attempt_id=attempt_id,
        provider_id="provider_001",
        model_id="model_001",
        model_version="v1",
        endpoint_id="endpoint_001",
        request_sha256=DIGEST_A,
    )
    receipt_id = (
        None
        if provider_request_id is None or response_sha256 is None
        else provider_effect_receipt_id(
            provider_invocation_id=invocation_id,
            provider_request_id=provider_request_id,
            response_sha256=response_sha256,
        )
    )
    reconciliation_id = (
        None
        if reconciliation_sequence is None or reconciliation_result is None
        else provider_effect_reconciliation_id(
            provider_invocation_id=invocation_id,
            reconciliation_sequence=reconciliation_sequence,
            reconciliation_result=reconciliation_result,
            provider_request_id=provider_request_id,
            response_sha256=response_sha256,
            provider_receipt_id=receipt_id,
        )
    )
    return ProviderEffectTransitionRef(
        effect_id=EFFECT_ID,
        attempt_id=attempt_id,
        attempt_sequence=attempt_sequence,
        provider_invocation_id=invocation_id,
        stage=stage,
        provider_id="provider_001",
        model_id="model_001",
        model_version="v1",
        endpoint_id="endpoint_001",
        request_sha256=DIGEST_A,
        provider_request_id=provider_request_id,
        response_sha256=response_sha256,
        provider_receipt_id=receipt_id,
        error_code=error_code,
        reconciliation_sequence=reconciliation_sequence,
        reconciliation_id=reconciliation_id,
        reconciliation_result=reconciliation_result,
        retry_at=retry_at,
    )


def _append(
    service: ProviderEffectLedgerService,
    reference: ProviderEffectTransitionRef,
    second: int,
):
    return service.append_transition(
        reference,
        occurred_at=f"2026-08-03T00:00:{second:02d}Z",
    )


def test_provider_effect_public_exports_are_intentional() -> None:
    assert tbm.ProviderEffectLedgerService is ProviderEffectLedgerService
    assert tbm.TrustedProviderEffectRegistration is (
        TrustedProviderEffectRegistration
    )
    assert tbm.EFFECT_PROVIDER_TRANSITION_EVENT == (
        "tbm.effect.provider_transition"
    )
    assert "ProviderEffectLedgerService" in tbm.__all__


def test_provider_effect_unknown_requires_reconciliation_before_retry() -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        _seed(ledger, _access())
        service = ProviderEffectLedgerService(ledger, _provider())
        assert service.recover(EFFECT_ID).next_action == "start_attempt"

        started = _transition("attempt_started")
        first = _append(service, started, 1)
        replay = _append(service, started, 1)
        legacy_result = tbm.ProviderEffectAppendResult(
            first.reference,
            first.receipt,
            first.recovery,
        )
        assert legacy_result.inserted is False
        assert first.inserted is True
        assert replay.inserted is False
        assert replay.receipt == first.receipt
        assert replay.recovery.provider_status == "in_flight"
        assert replay.recovery.next_action == "reconcile"

        submitted = _transition(
            "request_submitted",
            provider_request_id="provider_request_001",
        )
        _append(service, submitted, 2)
        unknown = _transition(
            "result_unknown",
            provider_request_id="provider_request_001",
            error_code="provider_timeout",
        )
        result = _append(service, unknown, 3)
        assert result.recovery.provider_status == "unknown"
        assert result.recovery.next_action == "reconcile"

        retry = _transition(
            "retry_scheduled",
            retry_at="2026-08-03T00:02:00Z",
        )
        with pytest.raises(ProviderEffectLedgerV1Error) as error:
            _append(service, retry, 4)
        assert error.value.code == "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED"

        still_unknown = _transition(
            "reconciled",
            provider_request_id="provider_request_001",
            reconciliation_sequence=1,
            reconciliation_result="still_unknown",
        )
        assert _append(service, still_unknown, 5).recovery.next_action == (
            "reconcile"
        )
        not_found = _transition(
            "reconciled",
            provider_request_id="provider_request_001",
            reconciliation_sequence=2,
            reconciliation_result="not_found",
        )
        assert _append(service, not_found, 6).recovery.next_action == (
            "schedule_retry"
        )
        assert _append(service, retry, 7).recovery.provider_status == "retry_wait"

        second_started = _transition("attempt_started", attempt_sequence=2)
        assert _append(service, second_started, 8).recovery.next_action == (
            "reconcile"
        )
        receipt = _transition(
            "receipt_recorded",
            attempt_sequence=2,
            provider_request_id="provider_request_002",
            response_sha256=DIGEST_B,
        )
        completed = _append(service, receipt, 9)
        assert completed.recovery.provider_status == "succeeded"
        assert completed.recovery.next_action == "complete"
        assert completed.recovery.provider_receipt_id == receipt.provider_receipt_id

        state = service._load_effect(EFFECT_ID)[1]
        assert projected_effect_status(state, EFFECT_ID) == "succeeded"
        assert ledger.verify_stream(effect_event_stream_id(EFFECT_ID)).valid


def test_provider_effect_confirmed_reconciliation_retains_exact_receipt() -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        _seed(ledger, _access())
        service = ProviderEffectLedgerService(ledger, _provider())
        _append(service, _transition("attempt_started"), 1)
        _append(
            service,
            _transition("result_unknown", error_code="response_lost"),
            2,
        )
        reconciled = _transition(
            "reconciled",
            provider_request_id="provider_request_001",
            response_sha256=DIGEST_B,
            reconciliation_sequence=1,
            reconciliation_result="confirmed",
        )
        result = _append(service, reconciled, 3)
        assert result.recovery.next_action == "complete"
        assert result.recovery.provider_request_id == "provider_request_001"
        assert result.recovery.provider_receipt_id == (
            reconciled.provider_receipt_id
        )


def test_provider_effect_post_commit_response_loss_replays_exact_event() -> None:
    class LoseOneAppendResponse:
        def __init__(self, ledger: SQLiteEventLedgerV1) -> None:
            self._ledger = ledger
            self.access_context = ledger.access_context
            self.armed = True

        @property
        def authority_identity(self):
            return self._ledger.authority_identity

        def append(self, *args, **kwargs):
            return self._ledger.append(*args, **kwargs)

        def append_once(self, *args, **kwargs):
            commit = self._ledger.append_once(*args, **kwargs)
            if self.armed:
                self.armed = False
                raise RuntimeError("simulated post-commit response loss")
            return commit

        def read_stream(self, *args, **kwargs):
            return self._ledger.read_stream(*args, **kwargs)

        def read_global(self, *args, **kwargs):
            return self._ledger.read_global(*args, **kwargs)

        def verify_stream(self, *args, **kwargs):
            return self._ledger.verify_stream(*args, **kwargs)

        def subscribe(self, *args, **kwargs):
            return self._ledger.subscribe(*args, **kwargs)

        def close(self):
            self._ledger.close()

    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        _seed(ledger, _access())
        started = _transition("attempt_started")
        lossy = ProviderEffectLedgerService(
            LoseOneAppendResponse(ledger),
            _provider(),
        )
        with pytest.raises(RuntimeError, match="response loss"):
            _append(lossy, started, 1)

        recovered = ProviderEffectLedgerService(ledger, _provider())
        assert recovered.recover(EFFECT_ID).next_action == "reconcile"
        first_replay = _append(recovered, started, 1)
        second_replay = _append(recovered, started, 1)
        assert first_replay.inserted is False
        assert second_replay.inserted is False
        assert first_replay.receipt == second_replay.receipt
        stream = ledger.read_stream(effect_event_stream_id(EFFECT_ID), limit=100)
        assert tuple(event.stream_version for event in stream.events) == (1, 2)


def test_provider_effect_rejects_receipt_and_request_identity_mismatch() -> None:
    receipt = _transition(
        "receipt_recorded",
        provider_request_id="provider_request_001",
        response_sha256=DIGEST_B,
    )
    with pytest.raises(EffectEventV1Error):
        replace(receipt, provider_receipt_id="provider_receipt_invalid")

    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        _seed(ledger, _access())
        service = ProviderEffectLedgerService(ledger, _provider())
        _append(service, _transition("attempt_started"), 1)
        _append(
            service,
            _transition(
                "request_submitted",
                provider_request_id="provider_request_001",
            ),
            2,
        )
        mismatch = _transition(
            "receipt_recorded",
            provider_request_id="provider_request_other",
            response_sha256=DIGEST_B,
        )
        with pytest.raises(ProviderEffectLedgerV1Error) as error:
            _append(service, mismatch, 3)
        assert error.value.code == "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED"


def test_provider_effect_reconciliation_retains_first_request_identity() -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        _seed(ledger, _access())
        service = ProviderEffectLedgerService(ledger, _provider())
        _append(service, _transition("attempt_started"), 1)
        _append(
            service,
            _transition("result_unknown", error_code="response_lost"),
            2,
        )
        first = _transition(
            "reconciled",
            provider_request_id="provider_request_001",
            reconciliation_sequence=1,
            reconciliation_result="still_unknown",
        )
        result = _append(service, first, 3)
        assert result.recovery.provider_request_id == "provider_request_001"

        changed = _transition(
            "reconciled",
            provider_request_id="provider_request_other",
            reconciliation_sequence=2,
            reconciliation_result="not_found",
        )
        with pytest.raises(ProviderEffectLedgerV1Error) as error:
            _append(service, changed, 4)
        assert error.value.code == "TBM_PROVIDER_EFFECT_TRANSITION_REJECTED"


def test_provider_effect_rejects_untrusted_provider_and_cross_scope_read(
    tmp_path: Path,
) -> None:
    database = tmp_path / "provider-effect.sqlite3"
    owner_access = _access()
    with SQLiteEventLedgerV1.connect(
        database,
        owner_access,
        initialize=True,
    ) as owner:
        _seed(owner, owner_access)
        mismatched_provider = TrustedProviderEffectRegistration(
            provider_id="provider_other",
            model_id="model_001",
            model_version="v1",
            endpoint_id="endpoint_001",
        )
        service = ProviderEffectLedgerService(owner, mismatched_provider)
        with pytest.raises(ProviderEffectLedgerV1Error) as error:
            _append(service, _transition("attempt_started"), 1)
        assert error.value.code == "TBM_PROVIDER_EFFECT_PROVIDER_MISMATCH"

    other_access = _access(
        principal_id="principal_other",
        authorization_decision_id="authorization_decision_other",
    )
    with SQLiteEventLedgerV1.connect(
        database,
        other_access,
    ) as other:
        service = ProviderEffectLedgerService(other, _provider())
        with pytest.raises(ProviderEffectLedgerV1Error) as error:
            service.recover(EFFECT_ID)
        assert error.value.code == "TBM_PROVIDER_EFFECT_SCOPE_DENIED"


def test_provider_effect_allows_explicit_same_scope_reauthorization(
    tmp_path: Path,
) -> None:
    database = tmp_path / "provider-effect-reauthorized.sqlite3"
    owner_access = _access()
    with SQLiteEventLedgerV1.connect(
        database,
        owner_access,
        initialize=True,
    ) as owner:
        _seed(owner, owner_access)

    reauthorized = _access(
        authorization_decision_id="authorization_decision_reconcile",
    )
    with SQLiteEventLedgerV1.connect(database, reauthorized) as ledger:
        service = ProviderEffectLedgerService(
            ledger,
            _provider(),
            authorized_origin_decision_id=(
                owner_access.authorization_decision_id
            ),
        )
        appended = _append(service, _transition("attempt_started"), 1)
        assert appended.recovery.provider_status == "in_flight"
        assert appended.receipt.events[0].authorization_decision_id == (
            reauthorized.authorization_decision_id
        )
        _append(
            service,
            _transition(
                "request_submitted",
                provider_request_id="provider_request_001",
            ),
            2,
        )
        _append(
            service,
            _transition(
                "result_unknown",
                provider_request_id="provider_request_001",
                error_code="provider_timeout",
            ),
            3,
        )
        reconciled = _append(
            service,
            _transition(
                "reconciled",
                provider_request_id="provider_request_001",
                reconciliation_sequence=1,
                reconciliation_result="not_found",
            ),
            4,
        )
        assert reconciled.recovery.provider_status == "not_found"
        assert reconciled.recovery.next_action == "schedule_retry"
        verification = ledger.verify_stream(effect_event_stream_id(EFFECT_ID))
        assert verification.valid is True
        page = ledger.read_stream(
            effect_event_stream_id(EFFECT_ID),
            limit=100,
        )
        assert all(
            tbm.DEFAULT_EVENT_TYPE_REGISTRY.consume(event)
            for event in page.events
        )

        replay_access = _access(
            authorization_decision_id="authorization_decision_replay",
        )
        with SQLiteEventLedgerV1(
            getattr(ledger, "_connection"),
            replay_access,
        ) as replay_ledger:
            replay_service = ProviderEffectLedgerService(
                replay_ledger,
                _provider(),
                authorized_origin_decision_id=(
                    owner_access.authorization_decision_id
                ),
            )
            with pytest.raises(ProviderEffectLedgerV1Error) as replay_error:
                _append(replay_service, _transition("attempt_started"), 1)
            assert replay_error.value.code == (
                "TBM_PROVIDER_EFFECT_REPLAY_AUTHORIZATION_MISMATCH"
            )

        denied = ProviderEffectLedgerService(
            ledger,
            _provider(),
            authorized_origin_decision_id="authorization_decision_wrong",
        )
        with pytest.raises(ProviderEffectLedgerV1Error) as error:
            denied.recover(EFFECT_ID)
        assert error.value.code == "TBM_PROVIDER_EFFECT_SCOPE_DENIED"


def test_provider_effect_invalid_enums_return_stable_contract_errors() -> None:
    started = _transition("attempt_started")
    with pytest.raises(EffectEventV1Error):
        replace(started, stage=[])  # type: ignore[arg-type]
    with pytest.raises(EffectEventV1Error):
        replace(
            started,
            stage="reconciled",
            reconciliation_sequence=1,
            reconciliation_id="reconciliation_invalid",
            reconciliation_result=[],  # type: ignore[arg-type]
        )


def test_postgres_provider_effect_recovery_matches_sqlite(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    installed = postgres_cluster.run_script(POSTGRES_INSTALL)
    assert installed.returncode == 0, installed.stderr
    with PostgresEventLedgerV1.connect(
        _access(),
        **postgres_cluster.connection_kwargs(),
    ) as ledger:
        _seed(ledger, _access())
        service = ProviderEffectLedgerService(ledger, _provider())
        _append(service, _transition("attempt_started"), 1)
        _append(
            service,
            _transition("result_unknown", error_code="provider_timeout"),
            2,
        )
        _append(
            service,
            _transition(
                "reconciled",
                reconciliation_sequence=1,
                reconciliation_result="not_found",
            ),
            3,
        )
        _append(
            service,
            _transition(
                "retry_scheduled",
                retry_at="2026-08-03T00:02:00Z",
            ),
            4,
        )
        _append(service, _transition("attempt_started", attempt_sequence=2), 5)
        receipt = _append(
            service,
            _transition(
                "receipt_recorded",
                attempt_sequence=2,
                provider_request_id="provider_request_002",
                response_sha256=DIGEST_B,
            ),
            6,
        )
        assert receipt.recovery.provider_status == "succeeded"
        assert receipt.recovery.next_action == "complete"
