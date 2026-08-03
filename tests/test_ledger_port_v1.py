from __future__ import annotations

from dataclasses import replace
import inspect

import pytest
import trace_backed_memory as tbm
import trace_backed_memory.ledger_port_v1 as ledger_port_v1

from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EVENT_LEDGER_MAX_READ_PAGE,
    EVENT_LEDGER_PORT_VERSION,
    EventLedgerAtomicAppendPort,
    EventLedgerClassificationDeniedError,
    EventLedgerConflictError,
    EventLedgerIdempotencyConflictError,
    EventLedgerInvalidRequestError,
    EventLedgerPort,
    EventLedgerScopeDeniedError,
    LedgerAccessContext,
    LedgerAppendCommit,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerClassificationFilter,
    LedgerGlobalReadRequest,
    LedgerIdempotency,
    LedgerStreamReadRequest,
    LedgerStreamVerification,
    LedgerSubscriptionPage,
    LedgerSubscriptionRequest,
    LedgerTenantPartition,
    build_ledger_append_receipt,
    build_ledger_page,
    ledger_page_sha256,
    verify_ledger_append_receipt,
    verify_ledger_append_precondition,
    verify_ledger_global_page,
    verify_ledger_stream_page,
    verify_ledger_stream_verification,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _access(
    *,
    tenant_id: str = "tenant_001",
    allowed: tuple[str, ...] = ("public", "internal"),
) -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id=tenant_id,
            repository_id="repository_001",
            environment_id="environment_local",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="principal",
        actor_id="principal_001",
        authorization_decision_id="authorization_decision_001",
        classification_filter=LedgerClassificationFilter(allowed),  # type: ignore[arg-type]
    )


def _event(
    access: LedgerAccessContext,
    *,
    stream_version: int,
    global_position: int,
    previous_sha256: str | None,
    event_id: str,
    classification: str = "internal",
    idempotency_key_sha256: str | None = None,
    request_sha256: str | None = None,
    stream_id: str = "stream_001",
) -> CanonicalEvent:
    trusted = access.event_trusted_context()
    assert type(trusted) is EventTrustedContext
    return build_canonical_event(
        event_id=event_id,
        event_type="tbm.test.committed",
        event_version=1,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=stream_id,
        stream_type="test_stream",
        stream_version=stream_version,
        global_position=global_position,
        trusted_context=trusted,
        request_id="request_001",
        idempotency_key_sha256=(
            _digest("a") if idempotency_key_sha256 is None else idempotency_key_sha256
        ),
        request_sha256=(_digest("b") if request_sha256 is None else request_sha256),
        correlation_id="correlation_001",
        causation_id=None,
        occurred_at="2026-08-01T00:00:00Z",
        recorded_at=f"2026-08-01T00:00:{stream_version:02d}Z",
        producer="trace_backed_memory",
        producer_version="0.1.0",
        payload_schema="tbm.test.committed.v1",
        previous_stream_event_sha256=previous_sha256,
        classification=classification,  # type: ignore[arg-type]
        retention_policy_id="retention_default",
        artifact_refs=(),
        payload={"event_id": event_id},
    )


def _request(
    *,
    access: LedgerAccessContext | None = None,
    idempotency_character: str = "a",
    command_character: str = "b",
    first_global_position: int = 1,
    event_id_prefix: str = "evt_ledger",
) -> LedgerAppendRequest:
    current_access = _access() if access is None else access
    idempotency_key_sha256 = _digest(idempotency_character)
    command_sha256 = _digest(command_character)
    first = _event(
        current_access,
        stream_version=1,
        global_position=first_global_position,
        previous_sha256=None,
        event_id=f"{event_id_prefix}_first",
        idempotency_key_sha256=idempotency_key_sha256,
        request_sha256=command_sha256,
    )
    second = _event(
        current_access,
        stream_version=2,
        global_position=first_global_position + 1,
        previous_sha256=first.event_sha256,
        event_id=f"{event_id_prefix}_second",
        idempotency_key_sha256=idempotency_key_sha256,
        request_sha256=command_sha256,
    )
    return LedgerAppendRequest(
        access=current_access,
        stream_id="stream_001",
        expected_stream_version=0,
        events=(first, second),
        idempotency=LedgerIdempotency(
            idempotency_key_sha256,
            command_sha256,
        ),
    )


def _continuation_request(
    head: CanonicalEvent,
    *,
    previous_sha256: str | None = None,
    first_global_position: int = 3,
) -> LedgerAppendRequest:
    access = _access()
    idempotency_key_sha256 = _digest("c")
    command_sha256 = _digest("d")
    first = _event(
        access,
        stream_version=3,
        global_position=first_global_position,
        previous_sha256=(
            head.event_sha256 if previous_sha256 is None else previous_sha256
        ),
        event_id="evt_ledger_third",
        idempotency_key_sha256=idempotency_key_sha256,
        request_sha256=command_sha256,
    )
    second = _event(
        access,
        stream_version=4,
        global_position=first_global_position + 1,
        previous_sha256=first.event_sha256,
        event_id="evt_ledger_fourth",
        idempotency_key_sha256=idempotency_key_sha256,
        request_sha256=command_sha256,
    )
    return LedgerAppendRequest(
        access=access,
        stream_id=head.stream_id,
        expected_stream_version=2,
        events=(first, second),
        idempotency=LedgerIdempotency(
            idempotency_key_sha256,
            command_sha256,
        ),
    )


class _ContractLedger:
    """Small test model for the frozen port's atomic/idempotent semantics."""

    def __init__(self) -> None:
        self.streams: dict[str, tuple[CanonicalEvent, ...]] = {}
        self.idempotency: dict[str, tuple[str, str, LedgerAppendReceipt]] = {}
        self.next_global_position = 1

    def append(self, request: LedgerAppendRequest) -> LedgerAppendReceipt:
        key = request.idempotency.idempotency_key_sha256
        prior = self.idempotency.get(key)
        if prior is not None:
            command_sha256, request_sha256, receipt = prior
            if (
                command_sha256 != request.idempotency.command_sha256
                or request_sha256 != request.request_sha256
            ):
                raise EventLedgerIdempotencyConflictError(
                    "TBM_EVENT_LEDGER_IDEMPOTENCY_CONFLICT",
                    "idempotency key is bound to another canonical command",
                )
            return receipt
        current = self.streams.get(request.stream_id, ())
        verify_ledger_append_precondition(
            request,
            current_head=None if not current else current[-1],
            next_global_position=self.next_global_position,
        )
        receipt = build_ledger_append_receipt(request)
        pending = current + request.events
        self.streams[request.stream_id] = pending
        self.idempotency[key] = (
            request.idempotency.command_sha256,
            request.request_sha256,
            receipt,
        )
        self.next_global_position += len(request.events)
        return receipt


def test_batch_append_request_and_exact_replay_receipt_are_content_bound() -> None:
    request = _request()
    ledger = _ContractLedger()

    first = ledger.append(request)
    replay = ledger.append(request)

    assert request.contract_version == EVENT_LEDGER_PORT_VERSION
    assert first is replay
    assert first.outcome == "committed"
    assert ledger.streams["stream_001"] == request.events
    verify_ledger_append_receipt(request, first)

    continuation = _continuation_request(request.events[-1])
    continuation_receipt = ledger.append(continuation)
    assert ledger.append(continuation) is continuation_receipt
    assert ledger.streams["stream_001"] == request.events + continuation.events
    assert ledger.next_global_position == 5


def test_event_ledger_port_contract_is_intentionally_exported() -> None:
    assert tbm.EVENT_LEDGER_PORT_VERSION == "tbm.event-ledger-port.v1"
    assert {
        "EventLedgerPort",
        "EventLedgerAtomicAppendPort",
        "LedgerAppendRequest",
        "LedgerAppendCommit",
        "LedgerAppendReceipt",
        "LedgerStreamReadRequest",
        "LedgerGlobalReadRequest",
        "LedgerSubscriptionRequest",
    } <= set(tbm.__all__)
    assert {
        "EventLedgerAtomicAppendPort",
        "LedgerAppendCommit",
    } <= set(ledger_port_v1.__all__)
    assert tuple(inspect.signature(EventLedgerPort.append).parameters) == (
        "self",
        "stream_id",
        "expected_version",
        "events",
        "idempotency",
    )
    assert tuple(
        inspect.signature(EventLedgerAtomicAppendPort.append_once).parameters
    ) == (
        "self",
        "stream_id",
        "expected_version",
        "events",
        "idempotency",
    )
    assert LedgerAppendCommit.__dataclass_params__.frozen is True
    assert tuple(inspect.signature(EventLedgerPort.read_stream).parameters) == (
        "self",
        "stream_id",
        "from_version",
        "limit",
    )
    assert tuple(inspect.signature(EventLedgerPort.read_global).parameters) == (
        "self",
        "after_position",
        "limit",
    )
    assert tuple(inspect.signature(EventLedgerPort.verify_stream).parameters) == (
        "self",
        "stream_id",
    )
    assert tuple(inspect.signature(EventLedgerPort.subscribe).parameters) == (
        "self",
        "after_position",
        "limit",
        "poll_timeout_seconds",
    )


def test_optimistic_concurrency_and_idempotency_conflicts_do_not_mutate() -> None:
    request = _request()
    ledger = _ContractLedger()
    ledger.append(request)
    before = dict(ledger.streams)

    stale = _request(
        idempotency_character="c",
        first_global_position=20,
        event_id_prefix="evt_ledger_stale",
    )
    with pytest.raises(EventLedgerConflictError) as stale_error:
        ledger.append(stale)
    assert stale_error.value.code == "TBM_EVENT_LEDGER_STALE_STREAM_VERSION"
    assert ledger.streams == before

    with pytest.raises(EventLedgerInvalidRequestError):
        replace(
            request,
            idempotency=LedgerIdempotency(_digest("a"), _digest("d")),
        )

    altered_first = _event(
        request.access,
        stream_version=1,
        global_position=12,
        previous_sha256=None,
        event_id="evt_ledger_altered",
    )
    altered = replace(request, events=(altered_first,))
    with pytest.raises(EventLedgerIdempotencyConflictError):
        ledger.append(altered)
    assert ledger.streams == before


def test_append_precondition_binds_exact_head_and_global_sequence() -> None:
    initial = _request()
    verify_ledger_append_precondition(
        initial,
        current_head=None,
        next_global_position=1,
    )
    head = initial.events[-1]
    continuation = _continuation_request(head)
    verify_ledger_append_precondition(
        continuation,
        current_head=head,
        next_global_position=3,
    )

    wrong_parent = _continuation_request(
        head,
        previous_sha256=_digest("f"),
    )
    with pytest.raises(EventLedgerConflictError) as parent_error:
        verify_ledger_append_precondition(
            wrong_parent,
            current_head=head,
            next_global_position=3,
        )
    assert parent_error.value.code == "TBM_EVENT_LEDGER_HEAD_MISMATCH"

    wrong_global = _continuation_request(head, first_global_position=4)
    with pytest.raises(EventLedgerConflictError) as global_error:
        verify_ledger_append_precondition(
            wrong_global,
            current_head=head,
            next_global_position=3,
        )
    assert global_error.value.code == "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"


def test_invalid_batch_fails_before_any_port_mutation() -> None:
    ledger = _ContractLedger()
    request = _request()
    first, second = request.events
    invalid_second = _event(
        request.access,
        stream_version=3,
        global_position=2,
        previous_sha256=first.event_sha256,
        event_id="evt_ledger_invalid_second",
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="stream_version"):
        LedgerAppendRequest(
            access=request.access,
            stream_id=request.stream_id,
            expected_stream_version=0,
            events=(first, invalid_second),
            idempotency=request.idempotency,
        )
    assert ledger.streams == {}
    assert second.stream_version == 2


@pytest.mark.parametrize(
    "events",
    [(), tuple([None] * (EVENT_LEDGER_MAX_APPEND_BATCH + 1)), [None]],
)
def test_append_batch_is_bounded_and_exactly_typed(events: object) -> None:
    request = _request()
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerAppendRequest(
            access=request.access,
            stream_id=request.stream_id,
            expected_stream_version=0,
            events=events,  # type: ignore[arg-type]
            idempotency=request.idempotency,
        )


def test_append_rejects_scope_digest_stream_and_classification_mismatch() -> None:
    request = _request()
    wrong_access = _access(tenant_id="tenant_002")
    with pytest.raises(EventLedgerInvalidRequestError):
        replace(request, access=wrong_access)
    with pytest.raises(EventLedgerInvalidRequestError, match="stream_id"):
        replace(request, stream_id="stream_002")

    restricted_access = _access(allowed=("public",))
    event = _event(
        restricted_access,
        stream_version=1,
        global_position=1,
        previous_sha256=None,
        event_id="evt_ledger_restricted",
        classification="internal",
    )
    with pytest.raises(EventLedgerClassificationDeniedError):
        LedgerAppendRequest(
            access=restricted_access,
            stream_id=event.stream_id,
            expected_stream_version=0,
            events=(event,),
            idempotency=LedgerIdempotency(_digest("a"), _digest("b")),
        )


def test_invalid_actor_and_backend_error_text_fail_with_stable_surface() -> None:
    access = _access()
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerAccessContext(
            partition=access.partition,
            principal_id=access.principal_id,
            agent_client_id=access.agent_client_id,
            actor_type=[],  # type: ignore[arg-type]
            actor_id=access.actor_id,
            authorization_decision_id=access.authorization_decision_id,
            classification_filter=access.classification_filter,
        )

    sanitized = EventLedgerConflictError(
        "not-a-ledger-code",
        "secret\nbackend detail",
    )
    assert sanitized.code == "TBM_EVENT_LEDGER_INTERNAL"
    assert str(sanitized) == "event ledger operation failed"


def test_stream_and_global_pages_are_bounded_ordered_and_exact() -> None:
    request = _request()
    stream_request = LedgerStreamReadRequest(
        request.access, "stream_001", from_version=1, limit=2
    )
    stream_page = build_ledger_page(
        read_kind="stream",
        events=request.events,
        high_watermark_global_position=2,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    verify_ledger_stream_page(stream_request, stream_page)

    global_request = LedgerGlobalReadRequest(request.access, after_position=0, limit=2)
    global_page = build_ledger_page(
        read_kind="global",
        events=request.events,
        high_watermark_global_position=3,
        next_stream_version=None,
        next_global_position=2,
        has_more=True,
    )
    verify_ledger_global_page(global_request, global_page)
    assert stream_page.page_sha256 != global_page.page_sha256


def test_read_pages_fail_closed_on_tenant_classification_and_order() -> None:
    request = _request()
    cross_tenant = LedgerGlobalReadRequest(
        _access(tenant_id="tenant_002"), after_position=0, limit=10
    )
    page = build_ledger_page(
        read_kind="global",
        events=request.events,
        high_watermark_global_position=2,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerScopeDeniedError):
        verify_ledger_global_page(cross_tenant, page)

    public_only = LedgerGlobalReadRequest(
        _access(allowed=("public",)), after_position=0, limit=10
    )
    with pytest.raises(EventLedgerClassificationDeniedError):
        verify_ledger_global_page(public_only, page)

    reversed_page = build_ledger_page(
        read_kind="global",
        events=tuple(reversed(request.events)),
        high_watermark_global_position=2,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="positions"):
        verify_ledger_global_page(
            LedgerGlobalReadRequest(request.access, 0, 10), reversed_page
        )


def test_read_pages_require_progress_and_trustworthy_high_watermark() -> None:
    request = _request()
    with pytest.raises(EventLedgerInvalidRequestError, match="advance"):
        build_ledger_page(
            read_kind="global",
            events=(),
            high_watermark_global_position=0,
            next_stream_version=None,
            next_global_position=0,
            has_more=True,
        )
    with pytest.raises(EventLedgerInvalidRequestError, match="watermark"):
        build_ledger_page(
            read_kind="stream",
            events=request.events,
            high_watermark_global_position=1,
            next_stream_version=None,
            next_global_position=None,
            has_more=False,
        )

    stale_watermark = build_ledger_page(
        read_kind="global",
        events=(),
        high_watermark_global_position=4,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="read cursor"):
        verify_ledger_global_page(
            LedgerGlobalReadRequest(request.access, after_position=5),
            stale_watermark,
        )


@pytest.mark.parametrize("limit", [0, EVENT_LEDGER_MAX_READ_PAGE + 1, True])
def test_read_limits_are_bounded(limit: int) -> None:
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerGlobalReadRequest(_access(), after_position=0, limit=limit)


def test_subscription_is_bounded_and_heartbeat_contains_no_events() -> None:
    request = LedgerSubscriptionRequest(
        _access(), after_position=0, limit=10, poll_timeout_seconds=60
    )
    empty = build_ledger_page(
        read_kind="global",
        events=(),
        high_watermark_global_position=0,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    page = LedgerSubscriptionPage(
        subscription_id="subscription_001",
        delivery_id="delivery_001",
        page=empty,
        heartbeat=True,
    )
    assert request.limit == 10
    assert page.heartbeat is True

    nonempty = build_ledger_page(
        read_kind="global",
        events=(_request().events[0],),
        high_watermark_global_position=1,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="heartbeat"):
        replace(page, page=nonempty)
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerSubscriptionRequest(_access(), poll_timeout_seconds=61)


def test_stream_verification_contract_binds_partition_head_and_issues() -> None:
    access = _access()
    partition = access.partition
    valid = LedgerStreamVerification(
        stream_id="stream_001",
        partition_sha256=partition.partition_sha256,
        verified_stream_version=2,
        verified_event_count=2,
        head_event_sha256=_request().events[-1].event_sha256,
        valid=True,
        issue_codes=(),
    )
    invalid = LedgerStreamVerification(
        stream_id="stream_001",
        partition_sha256=partition.partition_sha256,
        verified_stream_version=1,
        verified_event_count=1,
        head_event_sha256=_request().events[0].event_sha256,
        valid=False,
        issue_codes=("HASH_CHAIN_MISMATCH",),
    )
    assert valid.valid is True
    assert invalid.valid is False
    verify_ledger_stream_verification(access, "stream_001", valid)
    with pytest.raises(EventLedgerInvalidRequestError, match="disagree"):
        replace(invalid, valid=True)
    with pytest.raises(EventLedgerInvalidRequestError, match="inconsistent"):
        replace(valid, verified_event_count=3)
    with pytest.raises(EventLedgerInvalidRequestError, match="issue codes"):
        replace(
            invalid,
            issue_codes=("UNKNOWN_ISSUE",),  # type: ignore[arg-type]
        )
    with pytest.raises(EventLedgerConflictError) as mismatch:
        verify_ledger_stream_verification(
            _access(tenant_id="tenant_002"),
            "stream_001",
            valid,
        )
    assert mismatch.value.code == "TBM_EVENT_LEDGER_VERIFICATION_MISMATCH"


def test_receipt_tampering_is_rejected() -> None:
    request = _request()
    receipt = build_ledger_append_receipt(request)
    with pytest.raises(EventLedgerInvalidRequestError, match="receipt_sha256"):
        replace(receipt, receipt_sha256=_digest("f"))
    with pytest.raises(EventLedgerInvalidRequestError, match="bounded"):
        replace(
            receipt,
            events=tuple([request.events[0]] * (EVENT_LEDGER_MAX_APPEND_BATCH + 1)),
        )
    with pytest.raises(EventLedgerInvalidRequestError, match="event count"):
        replace(receipt, current_stream_version=3)
    other_request = _request(
        idempotency_character="c",
        first_global_position=20,
        event_id_prefix="evt_ledger_other",
    )
    with pytest.raises(EventLedgerConflictError):
        verify_ledger_append_receipt(other_request, receipt)


def test_classification_and_access_context_reject_noncanonical_boundaries() -> None:
    for allowed in (
        [],
        ("unsupported",),
        ("internal", "public"),
        ("public", "public"),
    ):
        with pytest.raises(EventLedgerInvalidRequestError) as error:
            LedgerClassificationFilter(allowed)  # type: ignore[arg-type]
        assert error.value.code == "TBM_EVENT_LEDGER_REQUEST_INVALID"

    access = _access()
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerAccessContext(
            partition=None,  # type: ignore[arg-type]
            principal_id=access.principal_id,
            agent_client_id=access.agent_client_id,
            actor_type=access.actor_type,
            actor_id=access.actor_id,
            authorization_decision_id=access.authorization_decision_id,
            classification_filter=access.classification_filter,
        )
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerAccessContext(
            partition=access.partition,
            principal_id=access.principal_id,
            agent_client_id=access.agent_client_id,
            actor_type=access.actor_type,
            actor_id=access.actor_id,
            authorization_decision_id=access.authorization_decision_id,
            classification_filter=None,  # type: ignore[arg-type]
        )


def test_append_request_rejects_invalid_envelope_boundaries() -> None:
    request = _request()
    invalid_mutations = (
        {"contract_version": "tbm.event-ledger-port.v0"},
        {"access": None},
        {"expected_stream_version": True},
        {"events": (None,)},
        {"idempotency": None},
        {
            "events": (
                _event(
                    request.access,
                    stream_version=1,
                    global_position=1,
                    previous_sha256=None,
                    event_id="evt_ledger_wrong_idempotency",
                    idempotency_key_sha256=_digest("f"),
                    request_sha256=_digest("b"),
                ),
            )
        },
    )
    for changes in invalid_mutations:
        with pytest.raises(EventLedgerInvalidRequestError) as error:
            replace(request, **changes)  # type: ignore[arg-type]
        assert error.value.code == "TBM_EVENT_LEDGER_REQUEST_INVALID"


def test_append_receipt_rejects_each_inconsistent_boundary() -> None:
    request = _request()
    receipt = build_ledger_append_receipt(request)
    invalid_mutations = (
        {"contract_version": "tbm.event-ledger-port.v0"},
        {"previous_stream_version": -1},
        {"first_global_position": 0},
        {"events": (None,)},
        {
            "events": (
                _event(
                    request.access,
                    stream_version=1,
                    global_position=1,
                    previous_sha256=None,
                    event_id="evt_ledger_wrong_stream",
                    stream_id="stream_002",
                ),
                request.events[1],
            )
        },
        {
            "events": (
                request.events[0],
                _event(
                    request.access,
                    stream_version=4,
                    global_position=2,
                    previous_sha256=request.events[0].event_sha256,
                    event_id="evt_ledger_noncontiguous",
                ),
            )
        },
        {"first_global_position": 2},
        {"outcome": "replayed"},
    )
    for changes in invalid_mutations:
        with pytest.raises(EventLedgerInvalidRequestError) as error:
            replace(receipt, **changes)  # type: ignore[arg-type]
        assert error.value.code == "TBM_EVENT_LEDGER_REQUEST_INVALID"


def test_read_request_page_and_subscription_constructors_fail_closed() -> None:
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerStreamReadRequest(None, "stream_001")  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerGlobalReadRequest(None)  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerSubscriptionRequest(None)  # type: ignore[arg-type]

    request = _request()
    page = build_ledger_page(
        read_kind="stream",
        events=request.events,
        high_watermark_global_position=2,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    invalid_mutations = (
        {"contract_version": "tbm.event-ledger-port.v0"},
        {"read_kind": "unsupported"},
        {"events": (None,)},
        {"next_stream_version": 0},
        {"has_more": 1},
        {"page_sha256": _digest("f")},
    )
    for changes in invalid_mutations:
        with pytest.raises(EventLedgerInvalidRequestError):
            replace(page, **changes)  # type: ignore[arg-type]

    with pytest.raises(EventLedgerInvalidRequestError, match="watermark"):
        build_ledger_page(
            read_kind="stream",
            events=request.events,
            high_watermark_global_position=2,
            next_stream_version=3,
            next_global_position=None,
            has_more=True,
        )

    empty_global = build_ledger_page(
        read_kind="global",
        events=(),
        high_watermark_global_position=0,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    subscription = LedgerSubscriptionPage(
        "subscription_001", "delivery_001", empty_global, True
    )
    with pytest.raises(EventLedgerInvalidRequestError):
        replace(subscription, page=None)  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError):
        replace(subscription, heartbeat=1)  # type: ignore[arg-type]


def test_stream_verification_rejects_invalid_hash_boolean_and_empty_head() -> None:
    request = _request()
    valid = LedgerStreamVerification(
        stream_id=request.stream_id,
        partition_sha256=request.access.partition.partition_sha256,
        verified_stream_version=2,
        verified_event_count=2,
        head_event_sha256=request.events[-1].event_sha256,
        valid=True,
        issue_codes=(),
    )
    with pytest.raises(EventLedgerInvalidRequestError):
        replace(valid, head_event_sha256="invalid")
    with pytest.raises(EventLedgerInvalidRequestError):
        replace(valid, valid=1)  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError, match="empty stream"):
        LedgerStreamVerification(
            stream_id=request.stream_id,
            partition_sha256=request.access.partition.partition_sha256,
            verified_stream_version=0,
            verified_event_count=1,
            head_event_sha256=None,
            valid=False,
            issue_codes=("HASH_CHAIN_MISMATCH",),
        )


def test_public_ledger_helpers_reject_wrong_exact_types() -> None:
    request = _request()
    receipt = build_ledger_append_receipt(request)
    with pytest.raises(EventLedgerInvalidRequestError):
        build_ledger_append_receipt(None)  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_append_receipt(None, receipt)  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_append_receipt(request, None)  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_append_precondition(
            None,  # type: ignore[arg-type]
            current_head=None,
            next_global_position=1,
        )


def test_append_precondition_rejects_missing_wrong_and_stale_heads() -> None:
    request = _request()
    continuation = _continuation_request(request.events[-1])
    with pytest.raises(EventLedgerConflictError) as missing:
        verify_ledger_append_precondition(
            continuation,
            current_head=None,
            next_global_position=3,
        )
    assert missing.value.code == "TBM_EVENT_LEDGER_STALE_STREAM_VERSION"

    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_append_precondition(
            continuation,
            current_head=object(),  # type: ignore[arg-type]
            next_global_position=3,
        )

    wrong_stream = _event(
        request.access,
        stream_version=2,
        global_position=2,
        previous_sha256=request.events[0].event_sha256,
        event_id="evt_ledger_stale_head",
        stream_id="stream_002",
    )
    with pytest.raises(EventLedgerConflictError) as stale:
        verify_ledger_append_precondition(
            continuation,
            current_head=wrong_stream,
            next_global_position=3,
        )
    assert stale.value.code == "TBM_EVENT_LEDGER_STALE_STREAM_VERSION"


def test_page_verifiers_reject_wrong_inputs_streams_versions_parents_and_cursors() -> (
    None
):
    request = _request()
    stream_request = LedgerStreamReadRequest(
        request.access, request.stream_id, from_version=1, limit=2
    )
    stream_page = build_ledger_page(
        read_kind="stream",
        events=request.events,
        high_watermark_global_position=2,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_stream_page(None, stream_page)  # type: ignore[arg-type]

    global_page = build_ledger_page(
        read_kind="global",
        events=request.events,
        high_watermark_global_position=2,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_stream_page(stream_request, global_page)

    other_stream = _event(
        request.access,
        stream_version=1,
        global_position=1,
        previous_sha256=None,
        event_id="evt_ledger_page_other_stream",
        stream_id="stream_002",
    )
    other_stream_page = build_ledger_page(
        read_kind="stream",
        events=(other_stream,),
        high_watermark_global_position=1,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="another stream"):
        verify_ledger_stream_page(stream_request, other_stream_page)

    with pytest.raises(EventLedgerInvalidRequestError, match="contiguous"):
        verify_ledger_stream_page(
            LedgerStreamReadRequest(request.access, request.stream_id, from_version=2),
            stream_page,
        )

    wrong_parent = _event(
        request.access,
        stream_version=2,
        global_position=2,
        previous_sha256=_digest("f"),
        event_id="evt_ledger_wrong_parent",
    )
    wrong_parent_page = build_ledger_page(
        read_kind="stream",
        events=(request.events[0], wrong_parent),
        high_watermark_global_position=2,
        next_stream_version=None,
        next_global_position=None,
        has_more=False,
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="parent chain"):
        verify_ledger_stream_page(stream_request, wrong_parent_page)

    stream_cursor_page = build_ledger_page(
        read_kind="stream",
        events=request.events,
        high_watermark_global_position=3,
        next_stream_version=99,
        next_global_position=None,
        has_more=True,
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="cursor"):
        verify_ledger_stream_page(stream_request, stream_cursor_page)

    global_request = LedgerGlobalReadRequest(request.access, after_position=0, limit=2)
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_global_page(None, global_page)  # type: ignore[arg-type]
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_global_page(global_request, stream_page)
    global_cursor_page = build_ledger_page(
        read_kind="global",
        events=request.events,
        high_watermark_global_position=3,
        next_stream_version=None,
        next_global_position=99,
        has_more=True,
    )
    with pytest.raises(EventLedgerInvalidRequestError, match="cursor"):
        verify_ledger_global_page(global_request, global_cursor_page)


def test_verification_and_scalar_helpers_reject_invalid_boundaries() -> None:
    request = _request()
    verification = LedgerStreamVerification(
        stream_id=request.stream_id,
        partition_sha256=request.access.partition.partition_sha256,
        verified_stream_version=2,
        verified_event_count=2,
        head_event_sha256=request.events[-1].event_sha256,
        valid=True,
        issue_codes=(),
    )
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_stream_verification(
            None,  # type: ignore[arg-type]
            request.stream_id,
            verification,
        )
    with pytest.raises(EventLedgerInvalidRequestError):
        verify_ledger_stream_verification(
            request.access,
            request.stream_id,
            None,  # type: ignore[arg-type]
        )
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerGlobalReadRequest(request.access, after_position=-1)
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerTenantPartition("", "tenant", "repository", "environment")
    with pytest.raises(EventLedgerInvalidRequestError):
        LedgerIdempotency("invalid", _digest("a"))
    with pytest.raises(EventLedgerInvalidRequestError) as noncanonical:
        ledger_page_sha256(
            read_kind="global",
            events=(),
            high_watermark_global_position=float("nan"),  # type: ignore[arg-type]
            next_stream_version=None,
            next_global_position=None,
            has_more=False,
        )
    assert noncanonical.value.code == "TBM_EVENT_LEDGER_NON_CANONICAL_JSON"
