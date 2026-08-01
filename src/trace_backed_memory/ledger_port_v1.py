from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, Protocol

from .contracts_v3 import V3ContractError
from .event_v1 import (
    CanonicalEvent,
    EventActorType,
    EventClassification,
    EventTrustedContext,
    EventV1ContractError,
    verify_event_parent,
    verify_event_trusted_context,
)


EVENT_LEDGER_PORT_VERSION = "tbm.event-ledger-port.v1"
EVENT_LEDGER_MAX_APPEND_BATCH = 100
EVENT_LEDGER_MAX_READ_PAGE = 1000
EVENT_LEDGER_MAX_SUBSCRIPTION_POLL_SECONDS = 60
EVENT_LEDGER_MAX_VERIFICATION_ISSUES = 1000

LedgerAppendOutcome = Literal["committed"]
LedgerReadKind = Literal["stream", "global"]
LedgerVerificationIssueCode = Literal[
    "EVENT_HASH_MISMATCH",
    "GLOBAL_POSITION_INVALID",
    "HASH_CHAIN_MISMATCH",
    "HEAD_MISMATCH",
    "PARTITION_MISMATCH",
    "STREAM_ID_MISMATCH",
    "STREAM_VERSION_GAP",
    "TRUSTED_CONTEXT_MISMATCH",
]

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CLASSIFICATION_ORDER: tuple[EventClassification, ...] = (
    "public",
    "internal",
    "confidential",
    "restricted",
)
_VERIFICATION_ISSUE_CODES: tuple[LedgerVerificationIssueCode, ...] = (
    "EVENT_HASH_MISMATCH",
    "GLOBAL_POSITION_INVALID",
    "HASH_CHAIN_MISMATCH",
    "HEAD_MISMATCH",
    "PARTITION_MISMATCH",
    "STREAM_ID_MISMATCH",
    "STREAM_VERSION_GAP",
    "TRUSTED_CONTEXT_MISMATCH",
)
_ERROR_CODE_RE = re.compile(r"^TBM_EVENT_LEDGER_[A-Z0-9_]{1,96}$")


class EventLedgerPortError(V3ContractError):
    """Stable storage-neutral event-ledger port failure."""

    def __init__(self, code: str, message: str) -> None:
        if (
            type(code) is not str
            or _ERROR_CODE_RE.fullmatch(code) is None
            or type(message) is not str
            or not 1 <= len(message) <= 512
            or any(ord(character) < 32 for character in message)
        ):
            code = "TBM_EVENT_LEDGER_INTERNAL"
            message = "event ledger operation failed"
        super().__init__(code, message)


class EventLedgerInvalidRequestError(EventLedgerPortError):
    """A caller supplied an invalid or unbounded port request."""


class EventLedgerConflictError(EventLedgerPortError):
    """The expected stream head no longer matches durable state."""


class EventLedgerIdempotencyConflictError(EventLedgerPortError):
    """An idempotency key was already bound to a different command."""


class EventLedgerScopeDeniedError(EventLedgerPortError):
    """The authenticated tenant partition does not cover an event."""


class EventLedgerClassificationDeniedError(EventLedgerPortError):
    """The authenticated classification filter does not cover an event."""


class EventLedgerNotFoundError(EventLedgerPortError):
    """A record is absent or intentionally hidden by the port boundary."""


class EventLedgerUnsupportedError(EventLedgerPortError):
    """The selected port cannot provide the requested bounded operation."""


@dataclass(frozen=True)
class LedgerTenantPartition:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
        ):
            _identifier(getattr(self, name), name)

    @property
    def partition_sha256(self) -> str:
        return _domain_sha256(
            b"tbm.event-ledger-partition.v1\x00",
            {
                "organization_id": self.organization_id,
                "tenant_id": self.tenant_id,
                "repository_id": self.repository_id,
                "environment_id": self.environment_id,
            },
        )


@dataclass(frozen=True)
class LedgerClassificationFilter:
    allowed: tuple[EventClassification, ...]

    def __post_init__(self) -> None:
        if type(self.allowed) is not tuple or not self.allowed:
            _invalid("classification filter must be a non-empty tuple")
        if any(item not in _CLASSIFICATION_ORDER for item in self.allowed):
            _invalid("classification filter contains an unsupported value")
        canonical = tuple(
            item for item in _CLASSIFICATION_ORDER if item in self.allowed
        )
        if self.allowed != canonical or len(self.allowed) != len(set(self.allowed)):
            _invalid("classification filter must be canonical and unique")

    @property
    def filter_sha256(self) -> str:
        return _domain_sha256(
            b"tbm.event-ledger-classification-filter.v1\x00",
            {"allowed": list(self.allowed)},
        )

    def allows(self, classification: EventClassification) -> bool:
        return classification in self.allowed


@dataclass(frozen=True)
class LedgerAccessContext:
    partition: LedgerTenantPartition
    principal_id: str
    agent_client_id: str
    actor_type: EventActorType
    actor_id: str
    authorization_decision_id: str
    classification_filter: LedgerClassificationFilter

    def __post_init__(self) -> None:
        if type(self.partition) is not LedgerTenantPartition:
            _invalid("partition must be exactly LedgerTenantPartition")
        for name in (
            "principal_id",
            "agent_client_id",
            "actor_id",
            "authorization_decision_id",
        ):
            _identifier(getattr(self, name), name)
        if type(self.actor_type) is not str or self.actor_type not in {
            "principal",
            "agent_client",
            "service",
            "worker",
        }:
            _invalid("actor_type is not supported")
        if type(self.classification_filter) is not LedgerClassificationFilter:
            _invalid(
                "classification_filter must be exactly LedgerClassificationFilter"
            )

    def event_trusted_context(self) -> EventTrustedContext:
        return EventTrustedContext(
            organization_id=self.partition.organization_id,
            tenant_id=self.partition.tenant_id,
            repository_id=self.partition.repository_id,
            environment_id=self.partition.environment_id,
            principal_id=self.principal_id,
            agent_client_id=self.agent_client_id,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            authorization_decision_id=self.authorization_decision_id,
        )


@dataclass(frozen=True)
class LedgerIdempotency:
    idempotency_key_sha256: str
    command_sha256: str

    def __post_init__(self) -> None:
        _digest(self.idempotency_key_sha256, "idempotency_key_sha256")
        _digest(self.command_sha256, "command_sha256")


@dataclass(frozen=True)
class LedgerAppendRequest:
    access: LedgerAccessContext
    stream_id: str
    expected_stream_version: int
    events: tuple[CanonicalEvent, ...]
    idempotency: LedgerIdempotency
    contract_version: str = EVENT_LEDGER_PORT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != EVENT_LEDGER_PORT_VERSION:
            _invalid(f"contract_version must be {EVENT_LEDGER_PORT_VERSION}")
        if type(self.access) is not LedgerAccessContext:
            _invalid("access must be exactly LedgerAccessContext")
        _identifier(self.stream_id, "stream_id")
        if (
            type(self.expected_stream_version) is not int
            or not 0 <= self.expected_stream_version <= 9_223_372_036_854_775_807
        ):
            _invalid("expected_stream_version must be a bounded integer")
        if (
            type(self.events) is not tuple
            or not 1 <= len(self.events) <= EVENT_LEDGER_MAX_APPEND_BATCH
        ):
            _invalid("events must be a bounded non-empty tuple")
        if any(type(event) is not CanonicalEvent for event in self.events):
            _invalid("events must contain CanonicalEvent values")
        if type(self.idempotency) is not LedgerIdempotency:
            _invalid("idempotency must be exactly LedgerIdempotency")
        trusted_context = self.access.event_trusted_context()
        previous: CanonicalEvent | None = None
        for offset, event in enumerate(self.events, start=1):
            if event.stream_id != self.stream_id:
                _invalid("event stream_id does not match append request")
            expected_version = self.expected_stream_version + offset
            if event.stream_version != expected_version:
                _invalid("event stream_version does not follow expected head")
            try:
                verify_event_trusted_context(event, trusted_context)
            except EventV1ContractError:
                _invalid("event trusted context does not match append access")
            if (
                event.idempotency_key_sha256
                != self.idempotency.idempotency_key_sha256
            ):
                _invalid("event idempotency key does not match append request")
            if event.request_sha256 != self.idempotency.command_sha256:
                _invalid("event request digest does not match canonical command")
            if not self.access.classification_filter.allows(
                event.classification
            ):
                raise EventLedgerClassificationDeniedError(
                    "TBM_EVENT_LEDGER_CLASSIFICATION_DENIED",
                    "event classification is not allowed for append",
                )
            if previous is not None:
                _verify_event_parent_or_invalid(event, previous)
            previous = event
        first = self.events[0]
        if self.expected_stream_version == 0:
            if first.previous_stream_event_sha256 is not None:
                _invalid("new stream append cannot name an existing parent")
        elif first.previous_stream_event_sha256 is None:
            _invalid("existing stream append must name the expected parent hash")

    @property
    def request_sha256(self) -> str:
        return _domain_sha256(
            b"tbm.event-ledger-append-request.v1\x00",
            {
                "contract_version": self.contract_version,
                "partition_sha256": self.access.partition.partition_sha256,
                "classification_filter_sha256": (
                    self.access.classification_filter.filter_sha256
                ),
                "principal_id": self.access.principal_id,
                "agent_client_id": self.access.agent_client_id,
                "actor_type": self.access.actor_type,
                "actor_id": self.access.actor_id,
                "authorization_decision_id": (
                    self.access.authorization_decision_id
                ),
                "stream_id": self.stream_id,
                "expected_stream_version": self.expected_stream_version,
                "event_sha256s": [event.event_sha256 for event in self.events],
                "idempotency_key_sha256": (
                    self.idempotency.idempotency_key_sha256
                ),
                "command_sha256": self.idempotency.command_sha256,
            },
        )


@dataclass(frozen=True)
class LedgerAppendReceipt:
    request_sha256: str
    idempotency_key_sha256: str
    command_sha256: str
    stream_id: str
    previous_stream_version: int
    current_stream_version: int
    first_global_position: int
    last_global_position: int
    events: tuple[CanonicalEvent, ...]
    outcome: LedgerAppendOutcome
    receipt_sha256: str
    contract_version: str = EVENT_LEDGER_PORT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != EVENT_LEDGER_PORT_VERSION:
            _invalid(f"contract_version must be {EVENT_LEDGER_PORT_VERSION}")
        for name in (
            "request_sha256",
            "idempotency_key_sha256",
            "command_sha256",
            "receipt_sha256",
        ):
            _digest(getattr(self, name), name)
        _identifier(self.stream_id, "stream_id")
        if (
            type(self.previous_stream_version) is not int
            or self.previous_stream_version < 0
            or type(self.current_stream_version) is not int
            or self.current_stream_version < 1
            or self.current_stream_version < self.previous_stream_version
        ):
            _invalid("receipt stream versions are invalid")
        if (
            type(self.first_global_position) is not int
            or type(self.last_global_position) is not int
            or self.first_global_position < 1
            or self.last_global_position < self.first_global_position
        ):
            _invalid("receipt global positions are invalid")
        if (
            type(self.events) is not tuple
            or not 1 <= len(self.events) <= EVENT_LEDGER_MAX_APPEND_BATCH
        ):
            _invalid("receipt events must be a bounded non-empty tuple")
        if any(type(event) is not CanonicalEvent for event in self.events):
            _invalid("receipt events must contain CanonicalEvent values")
        if self.current_stream_version != self.previous_stream_version + len(
            self.events
        ):
            _invalid("receipt stream versions do not match event count")
        previous: CanonicalEvent | None = None
        for offset, event in enumerate(self.events, start=1):
            if event.stream_id != self.stream_id:
                _invalid("receipt event stream_id is inconsistent")
            if event.stream_version != self.previous_stream_version + offset:
                _invalid("receipt event stream versions are not contiguous")
            if previous is not None:
                _verify_event_parent_or_invalid(event, previous)
            previous = event
        first = self.events[0]
        last = self.events[-1]
        if (
            self.first_global_position != first.global_position
            or self.last_global_position != last.global_position
        ):
            _invalid("receipt global positions do not match events")
        if self.previous_stream_version == 0:
            if first.previous_stream_event_sha256 is not None:
                _invalid("new stream receipt cannot name an existing parent")
        elif first.previous_stream_event_sha256 is None:
            _invalid("existing stream receipt must name its prior head")
        if self.outcome != "committed":
            _invalid("receipt outcome is not supported")
        expected_sha256 = ledger_append_receipt_sha256(
            request_sha256=self.request_sha256,
            idempotency_key_sha256=self.idempotency_key_sha256,
            command_sha256=self.command_sha256,
            stream_id=self.stream_id,
            previous_stream_version=self.previous_stream_version,
            current_stream_version=self.current_stream_version,
            first_global_position=self.first_global_position,
            last_global_position=self.last_global_position,
            events=self.events,
            outcome=self.outcome,
        )
        if self.receipt_sha256 != expected_sha256:
            _invalid("receipt_sha256 does not match append receipt")


@dataclass(frozen=True)
class LedgerStreamReadRequest:
    access: LedgerAccessContext
    stream_id: str
    from_version: int = 1
    limit: int = 100

    def __post_init__(self) -> None:
        if type(self.access) is not LedgerAccessContext:
            _invalid("access must be exactly LedgerAccessContext")
        _identifier(self.stream_id, "stream_id")
        _positive_cursor(self.from_version, "from_version", allow_zero=False)
        _read_limit(self.limit)


@dataclass(frozen=True)
class LedgerGlobalReadRequest:
    access: LedgerAccessContext
    after_position: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        if type(self.access) is not LedgerAccessContext:
            _invalid("access must be exactly LedgerAccessContext")
        _positive_cursor(
            self.after_position,
            "after_position",
            allow_zero=True,
        )
        _read_limit(self.limit)


@dataclass(frozen=True)
class LedgerPage:
    read_kind: LedgerReadKind
    events: tuple[CanonicalEvent, ...]
    high_watermark_global_position: int
    next_stream_version: int | None
    next_global_position: int | None
    has_more: bool
    page_sha256: str
    contract_version: str = EVENT_LEDGER_PORT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != EVENT_LEDGER_PORT_VERSION:
            _invalid(f"contract_version must be {EVENT_LEDGER_PORT_VERSION}")
        if self.read_kind not in {"stream", "global"}:
            _invalid("read_kind is not supported")
        if (
            type(self.events) is not tuple
            or len(self.events) > EVENT_LEDGER_MAX_READ_PAGE
            or any(type(event) is not CanonicalEvent for event in self.events)
        ):
            _invalid("page events are invalid or unbounded")
        _positive_cursor(
            self.high_watermark_global_position,
            "high_watermark_global_position",
            allow_zero=True,
        )
        if self.next_stream_version is not None:
            _positive_cursor(
                self.next_stream_version,
                "next_stream_version",
                allow_zero=False,
            )
        if self.next_global_position is not None:
            _positive_cursor(
                self.next_global_position,
                "next_global_position",
                allow_zero=True,
            )
        if type(self.has_more) is not bool:
            _invalid("has_more must be a boolean")
        if self.has_more and not self.events:
            _invalid("a page with more results must advance its cursor")
        if self.events and self.high_watermark_global_position < max(
            event.global_position for event in self.events
        ):
            _invalid("page high watermark precedes a returned event")
        if self.has_more and self.high_watermark_global_position <= max(
            event.global_position for event in self.events
        ):
            _invalid("page with more results must retain a later high watermark")
        _digest(self.page_sha256, "page_sha256")
        if self.page_sha256 != ledger_page_sha256(
            read_kind=self.read_kind,
            events=self.events,
            high_watermark_global_position=self.high_watermark_global_position,
            next_stream_version=self.next_stream_version,
            next_global_position=self.next_global_position,
            has_more=self.has_more,
        ):
            _invalid("page_sha256 does not match ledger page")


@dataclass(frozen=True)
class LedgerStreamVerification:
    stream_id: str
    partition_sha256: str
    verified_stream_version: int
    verified_event_count: int
    head_event_sha256: str | None
    valid: bool
    issue_codes: tuple[LedgerVerificationIssueCode, ...]

    def __post_init__(self) -> None:
        _identifier(self.stream_id, "stream_id")
        _digest(self.partition_sha256, "partition_sha256")
        _positive_cursor(
            self.verified_stream_version,
            "verified_stream_version",
            allow_zero=True,
        )
        _positive_cursor(
            self.verified_event_count,
            "verified_event_count",
            allow_zero=True,
        )
        if self.head_event_sha256 is not None:
            _digest(self.head_event_sha256, "head_event_sha256")
        if type(self.valid) is not bool:
            _invalid("valid must be a boolean")
        if (
            type(self.issue_codes) is not tuple
            or len(self.issue_codes) > EVENT_LEDGER_MAX_VERIFICATION_ISSUES
            or any(code not in _VERIFICATION_ISSUE_CODES for code in self.issue_codes)
            or len(self.issue_codes) != len(set(self.issue_codes))
            or self.issue_codes
            != tuple(code for code in _VERIFICATION_ISSUE_CODES if code in self.issue_codes)
        ):
            _invalid("verification issue codes are invalid")
        if self.valid == bool(self.issue_codes):
            _invalid("verification validity and issue codes disagree")
        if self.verified_stream_version == 0:
            if self.verified_event_count != 0 or self.head_event_sha256 is not None:
                _invalid("empty stream verification is inconsistent")
        elif self.verified_event_count != self.verified_stream_version or (
            self.head_event_sha256 is None
        ):
            _invalid("non-empty stream verification is inconsistent")


@dataclass(frozen=True)
class LedgerSubscriptionRequest:
    access: LedgerAccessContext
    after_position: int = 0
    limit: int = 100
    poll_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if type(self.access) is not LedgerAccessContext:
            _invalid("access must be exactly LedgerAccessContext")
        _positive_cursor(
            self.after_position,
            "after_position",
            allow_zero=True,
        )
        _read_limit(self.limit)
        if (
            type(self.poll_timeout_seconds) is not int
            or not 0
            <= self.poll_timeout_seconds
            <= EVENT_LEDGER_MAX_SUBSCRIPTION_POLL_SECONDS
        ):
            _invalid("poll_timeout_seconds is out of bounds")


@dataclass(frozen=True)
class LedgerSubscriptionPage:
    subscription_id: str
    delivery_id: str
    page: LedgerPage
    heartbeat: bool

    def __post_init__(self) -> None:
        _identifier(self.subscription_id, "subscription_id")
        _identifier(self.delivery_id, "delivery_id")
        if type(self.page) is not LedgerPage or self.page.read_kind != "global":
            _invalid("subscription page must contain one global LedgerPage")
        if type(self.heartbeat) is not bool:
            _invalid("heartbeat must be a boolean")
        if self.heartbeat and self.page.events:
            _invalid("heartbeat subscription page cannot contain events")


class EventLedgerSubscription(Protocol):
    """Bounded at-least-once subscription; consumers deduplicate event hashes."""

    def poll(self) -> LedgerSubscriptionPage: ...

    def acknowledge(
        self,
        delivery_id: str,
        *,
        expected_next_global_position: int | None,
    ) -> None: ...

    def close(self) -> None: ...


class EventLedgerPort(Protocol):
    """Trusted-context-bound canonical ledger port frozen by F0-04."""

    @property
    def access_context(self) -> LedgerAccessContext:
        """Return the adapter-authenticated context bound to this port."""
        ...

    def append(
        self,
        stream_id: str,
        expected_version: int,
        events: tuple[CanonicalEvent, ...],
        idempotency: LedgerIdempotency,
    ) -> LedgerAppendReceipt:
        """Atomically insert the full batch or replay its exact prior receipt."""
        ...

    def read_stream(
        self,
        stream_id: str,
        from_version: int = 1,
        limit: int = 100,
    ) -> LedgerPage:
        """Return one bounded, tenant/classification-filtered stream page."""
        ...

    def read_global(
        self,
        after_position: int = 0,
        limit: int = 100,
    ) -> LedgerPage:
        """Return one bounded, tenant/classification-filtered global page."""
        ...

    def verify_stream(
        self,
        stream_id: str,
    ) -> LedgerStreamVerification:
        """Verify exact versions, hashes, tenant partition, and stream head."""
        ...

    def subscribe(
        self,
        after_position: int = 0,
        limit: int = 100,
        poll_timeout_seconds: int = 10,
    ) -> EventLedgerSubscription:
        """Create a bounded page subscription; never expose raw backend state."""
        ...


def ledger_append_receipt_sha256(
    *,
    request_sha256: str,
    idempotency_key_sha256: str,
    command_sha256: str,
    stream_id: str,
    previous_stream_version: int,
    current_stream_version: int,
    first_global_position: int,
    last_global_position: int,
    events: tuple[CanonicalEvent, ...],
    outcome: LedgerAppendOutcome,
) -> str:
    return _domain_sha256(
        b"tbm.event-ledger-append-receipt.v1\x00",
        {
            "request_sha256": request_sha256,
            "idempotency_key_sha256": idempotency_key_sha256,
            "command_sha256": command_sha256,
            "stream_id": stream_id,
            "previous_stream_version": previous_stream_version,
            "current_stream_version": current_stream_version,
            "first_global_position": first_global_position,
            "last_global_position": last_global_position,
            "event_sha256s": [event.event_sha256 for event in events],
            "outcome": outcome,
        },
    )


def build_ledger_append_receipt(
    request: LedgerAppendRequest,
) -> LedgerAppendReceipt:
    if type(request) is not LedgerAppendRequest:
        _invalid("request must be exactly LedgerAppendRequest")
    first = request.events[0]
    last = request.events[-1]
    values: dict[str, object] = {
        "request_sha256": request.request_sha256,
        "idempotency_key_sha256": (
            request.idempotency.idempotency_key_sha256
        ),
        "command_sha256": request.idempotency.command_sha256,
        "stream_id": request.stream_id,
        "previous_stream_version": request.expected_stream_version,
        "current_stream_version": last.stream_version,
        "first_global_position": first.global_position,
        "last_global_position": last.global_position,
        "events": request.events,
        "outcome": "committed",
    }
    receipt_sha256 = ledger_append_receipt_sha256(
        request_sha256=request.request_sha256,
        idempotency_key_sha256=request.idempotency.idempotency_key_sha256,
        command_sha256=request.idempotency.command_sha256,
        stream_id=request.stream_id,
        previous_stream_version=request.expected_stream_version,
        current_stream_version=last.stream_version,
        first_global_position=first.global_position,
        last_global_position=last.global_position,
        events=request.events,
        outcome="committed",
    )
    return LedgerAppendReceipt(
        request_sha256=str(values["request_sha256"]),
        idempotency_key_sha256=str(values["idempotency_key_sha256"]),
        command_sha256=str(values["command_sha256"]),
        stream_id=str(values["stream_id"]),
        previous_stream_version=request.expected_stream_version,
        current_stream_version=last.stream_version,
        first_global_position=first.global_position,
        last_global_position=last.global_position,
        events=request.events,
        outcome="committed",
        receipt_sha256=receipt_sha256,
    )


def verify_ledger_append_receipt(
    request: LedgerAppendRequest,
    receipt: LedgerAppendReceipt,
) -> None:
    if type(request) is not LedgerAppendRequest:
        _invalid("request must be exactly LedgerAppendRequest")
    if type(receipt) is not LedgerAppendReceipt:
        _invalid("receipt must be exactly LedgerAppendReceipt")
    if (
        receipt.request_sha256 != request.request_sha256
        or receipt.idempotency_key_sha256
        != request.idempotency.idempotency_key_sha256
        or receipt.command_sha256 != request.idempotency.command_sha256
        or receipt.stream_id != request.stream_id
        or receipt.previous_stream_version != request.expected_stream_version
        or receipt.current_stream_version
        != request.expected_stream_version + len(request.events)
        or receipt.events != request.events
        or receipt.first_global_position != request.events[0].global_position
        or receipt.last_global_position != request.events[-1].global_position
    ):
        raise EventLedgerConflictError(
            "TBM_EVENT_LEDGER_RECEIPT_MISMATCH",
            "ledger did not return the exact append result",
        )


def verify_ledger_append_precondition(
    request: LedgerAppendRequest,
    *,
    current_head: CanonicalEvent | None,
    next_global_position: int,
) -> None:
    """Verify the stream head and globally consecutive positions before commit."""

    if type(request) is not LedgerAppendRequest:
        _invalid("request must be exactly LedgerAppendRequest")
    _positive_cursor(
        next_global_position,
        "next_global_position",
        allow_zero=False,
    )
    if request.expected_stream_version == 0:
        if current_head is not None:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_STALE_STREAM_VERSION",
                "expected stream version is stale",
            )
    else:
        if current_head is None:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_STALE_STREAM_VERSION",
                "expected stream version is stale",
            )
        if type(current_head) is not CanonicalEvent:
            _invalid("current_head must be exactly CanonicalEvent or null")
        if (
            current_head.stream_id != request.stream_id
            or current_head.stream_version != request.expected_stream_version
        ):
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_STALE_STREAM_VERSION",
                "expected stream version is stale",
            )
        try:
            verify_event_parent(request.events[0], current_head)
        except EventV1ContractError as error:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_HEAD_MISMATCH",
                "append event does not extend the exact stream head",
            ) from error
    for offset, event in enumerate(request.events):
        if event.global_position != next_global_position + offset:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT",
                "append event global positions are not currently available",
            )


def ledger_page_sha256(
    *,
    read_kind: LedgerReadKind,
    events: tuple[CanonicalEvent, ...],
    high_watermark_global_position: int,
    next_stream_version: int | None,
    next_global_position: int | None,
    has_more: bool,
) -> str:
    return _domain_sha256(
        b"tbm.event-ledger-page.v1\x00",
        {
            "read_kind": read_kind,
            "event_sha256s": [event.event_sha256 for event in events],
            "high_watermark_global_position": high_watermark_global_position,
            "next_stream_version": next_stream_version,
            "next_global_position": next_global_position,
            "has_more": has_more,
        },
    )


def build_ledger_page(
    *,
    read_kind: LedgerReadKind,
    events: tuple[CanonicalEvent, ...],
    high_watermark_global_position: int,
    next_stream_version: int | None,
    next_global_position: int | None,
    has_more: bool,
) -> LedgerPage:
    page_sha256 = ledger_page_sha256(
        read_kind=read_kind,
        events=events,
        high_watermark_global_position=high_watermark_global_position,
        next_stream_version=next_stream_version,
        next_global_position=next_global_position,
        has_more=has_more,
    )
    return LedgerPage(
        read_kind=read_kind,
        events=events,
        high_watermark_global_position=high_watermark_global_position,
        next_stream_version=next_stream_version,
        next_global_position=next_global_position,
        has_more=has_more,
        page_sha256=page_sha256,
    )


def verify_ledger_stream_page(
    request: LedgerStreamReadRequest,
    page: LedgerPage,
) -> None:
    if type(request) is not LedgerStreamReadRequest or type(page) is not LedgerPage:
        _invalid("stream page verification inputs are invalid")
    if page.read_kind != "stream" or len(page.events) > request.limit:
        _invalid("stream page does not match read request")
    expected_version = request.from_version
    previous: CanonicalEvent | None = None
    for event in page.events:
        _verify_read_access(request.access, event)
        if event.stream_id != request.stream_id:
            _invalid("stream page contains another stream")
        if event.stream_version != expected_version:
            _invalid("stream page versions are not contiguous")
        if previous is not None:
            _verify_event_parent_or_invalid(event, previous)
        previous = event
        expected_version += 1
    expected_next = expected_version if page.has_more else None
    if page.next_stream_version != expected_next or page.next_global_position is not None:
        _invalid("stream page cursor is inconsistent")


def verify_ledger_global_page(
    request: LedgerGlobalReadRequest,
    page: LedgerPage,
) -> None:
    if type(request) is not LedgerGlobalReadRequest or type(page) is not LedgerPage:
        _invalid("global page verification inputs are invalid")
    if page.read_kind != "global" or len(page.events) > request.limit:
        _invalid("global page does not match read request")
    if page.high_watermark_global_position < request.after_position:
        _invalid("global page high watermark precedes the read cursor")
    previous_position = request.after_position
    for event in page.events:
        _verify_read_access(request.access, event)
        if event.global_position <= previous_position:
            _invalid("global page positions are not strictly increasing")
        previous_position = event.global_position
    expected_next = previous_position if page.has_more else None
    if page.next_global_position != expected_next or page.next_stream_version is not None:
        _invalid("global page cursor is inconsistent")


def verify_ledger_stream_verification(
    access: LedgerAccessContext,
    stream_id: str,
    verification: LedgerStreamVerification,
) -> None:
    if type(access) is not LedgerAccessContext:
        _invalid("access must be exactly LedgerAccessContext")
    _identifier(stream_id, "stream_id")
    if type(verification) is not LedgerStreamVerification:
        _invalid("verification must be exactly LedgerStreamVerification")
    if (
        verification.stream_id != stream_id
        or verification.partition_sha256 != access.partition.partition_sha256
    ):
        raise EventLedgerConflictError(
            "TBM_EVENT_LEDGER_VERIFICATION_MISMATCH",
            "stream verification does not match the requested partition",
        )


def _verify_read_access(
    access: LedgerAccessContext,
    event: CanonicalEvent,
) -> None:
    partition = access.partition
    if (
        event.organization_id != partition.organization_id
        or event.tenant_id != partition.tenant_id
        or event.repository_id != partition.repository_id
        or event.environment_id != partition.environment_id
    ):
        raise EventLedgerScopeDeniedError(
            "TBM_EVENT_LEDGER_SCOPE_DENIED",
            "event is outside the authenticated tenant partition",
        )
    if not access.classification_filter.allows(event.classification):
        raise EventLedgerClassificationDeniedError(
            "TBM_EVENT_LEDGER_CLASSIFICATION_DENIED",
            "event classification is not allowed for read",
        )


def _verify_event_parent_or_invalid(
    event: CanonicalEvent,
    previous: CanonicalEvent,
) -> None:
    try:
        verify_event_parent(event, previous)
    except EventV1ContractError:
        _invalid("event parent chain is invalid")


def _read_limit(value: object) -> None:
    if type(value) is not int or not 1 <= value <= EVENT_LEDGER_MAX_READ_PAGE:
        _invalid("read limit is out of bounds")


def _positive_cursor(value: object, name: str, *, allow_zero: bool) -> None:
    minimum = 0 if allow_zero else 1
    if (
        type(value) is not int
        or not minimum <= value <= 9_223_372_036_854_775_807
    ):
        _invalid(f"{name} is out of bounds")


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a bounded identifier")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a canonical sha256 digest")


def _domain_sha256(domain: bytes, value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise EventLedgerInvalidRequestError(
            "TBM_EVENT_LEDGER_NON_CANONICAL_JSON",
            "ledger descriptor is not canonical JSON",
        ) from error
    return "sha256:" + hashlib.sha256(domain + encoded).hexdigest()


def _invalid(message: str) -> NoReturn:
    raise EventLedgerInvalidRequestError(
        "TBM_EVENT_LEDGER_REQUEST_INVALID",
        message,
    )


__all__ = [
    "EVENT_LEDGER_MAX_APPEND_BATCH",
    "EVENT_LEDGER_MAX_READ_PAGE",
    "EVENT_LEDGER_MAX_SUBSCRIPTION_POLL_SECONDS",
    "EVENT_LEDGER_MAX_VERIFICATION_ISSUES",
    "EVENT_LEDGER_PORT_VERSION",
    "EventLedgerClassificationDeniedError",
    "EventLedgerConflictError",
    "EventLedgerIdempotencyConflictError",
    "EventLedgerInvalidRequestError",
    "EventLedgerNotFoundError",
    "EventLedgerPort",
    "EventLedgerPortError",
    "EventLedgerScopeDeniedError",
    "EventLedgerSubscription",
    "EventLedgerUnsupportedError",
    "LedgerAccessContext",
    "LedgerAppendOutcome",
    "LedgerAppendReceipt",
    "LedgerAppendRequest",
    "LedgerClassificationFilter",
    "LedgerGlobalReadRequest",
    "LedgerIdempotency",
    "LedgerPage",
    "LedgerReadKind",
    "LedgerStreamReadRequest",
    "LedgerStreamVerification",
    "LedgerSubscriptionPage",
    "LedgerSubscriptionRequest",
    "LedgerTenantPartition",
    "LedgerVerificationIssueCode",
    "build_ledger_append_receipt",
    "build_ledger_page",
    "ledger_append_receipt_sha256",
    "ledger_page_sha256",
    "verify_ledger_append_receipt",
    "verify_ledger_append_precondition",
    "verify_ledger_global_page",
    "verify_ledger_stream_page",
    "verify_ledger_stream_verification",
]
