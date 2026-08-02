from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import NoReturn, cast

from ._timestamps import RFC3339_PATTERN, canonical_rfc3339
from .completion_outbox_v3 import (
    COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION,
    COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION,
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    build_initial_completion_outbox_delivery,
    parse_completion_outbox_delivery,
    parse_completion_outbox_event,
    verify_completion_outbox_delivery_transition,
)
from .contracts_v3 import V3ContractError
from .event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
    verify_event_parent,
)
from .gate_session_event_v1 import (
    GATE_SESSION_COMPLETED_EVENT,
    GATE_SESSION_EVENT_RETENTION_POLICY_ID,
)


EFFECT_EVENT_CONTRACT_VERSION = "tbm.effect-event.v1"
EFFECT_EVENT_VERSION = 1
EFFECT_EVENT_STREAM_TYPE = "effect"
EFFECT_EVENT_PRODUCER = "trace_backed_memory"
EFFECT_EVENT_PRODUCER_VERSION = "0.1.0"
EFFECT_EVENT_RETENTION_POLICY_ID = GATE_SESSION_EVENT_RETENTION_POLICY_ID

EFFECT_REQUESTED_EVENT = "tbm.effect.requested"
EFFECT_STARTED_EVENT = "tbm.effect.started"
EFFECT_SUCCEEDED_EVENT = "tbm.effect.succeeded"
EFFECT_FAILED_EVENT = "tbm.effect.failed"
EFFECT_RETRY_SCHEDULED_EVENT = "tbm.effect.retry_scheduled"
EFFECT_DEAD_LETTERED_EVENT = "tbm.effect.dead_lettered"
EFFECT_COMPENSATION_REQUESTED_EVENT = "tbm.effect.compensation_requested"
EFFECT_COMPENSATED_EVENT = "tbm.effect.compensated"
EFFECT_EVENT_TYPES = tuple(
    sorted(
        (
            EFFECT_REQUESTED_EVENT,
            EFFECT_STARTED_EVENT,
            EFFECT_SUCCEEDED_EVENT,
            EFFECT_FAILED_EVENT,
            EFFECT_RETRY_SCHEDULED_EVENT,
            EFFECT_DEAD_LETTERED_EVENT,
            EFFECT_COMPENSATION_REQUESTED_EVENT,
            EFFECT_COMPENSATED_EVENT,
        )
    )
)

_DELIVERY_EVENT_TYPES = frozenset(
    {
        EFFECT_STARTED_EVENT,
        EFFECT_SUCCEEDED_EVENT,
        EFFECT_FAILED_EVENT,
        EFFECT_RETRY_SCHEDULED_EVENT,
        EFFECT_DEAD_LETTERED_EVENT,
    }
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class EffectEventV1Error(V3ContractError):
    """Stable failure for canonical local effect lifecycle events."""


@dataclass(frozen=True)
class EffectContract:
    effect_id: str
    effect_type: str
    idempotency_key: str
    requested_by_event_id: str
    input_artifact_sha256: str
    authorization_event_id: str
    compensation_supported: bool

    def __post_init__(self) -> None:
        for name in (
            "effect_id",
            "effect_type",
            "requested_by_event_id",
            "authorization_event_id",
        ):
            _identifier(getattr(self, name), name)
        if (
            type(self.idempotency_key) is not str
            or not self.idempotency_key
            or len(self.idempotency_key) > 512
            or self.idempotency_key.strip() != self.idempotency_key
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.idempotency_key
            )
        ):
            _invalid("idempotency_key must be bounded non-empty text")
        _digest(self.input_artifact_sha256, "input_artifact_sha256")
        if type(self.compensation_supported) is not bool:
            _invalid("compensation_supported must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "idempotency_key": self.idempotency_key,
            "requested_by_event_id": self.requested_by_event_id,
            "input_artifact_sha256": self.input_artifact_sha256,
            "authorization_event_id": self.authorization_event_id,
            "compensation_supported": self.compensation_supported,
        }


@dataclass(frozen=True)
class EffectRequestedRef:
    effect: EffectContract
    outbox_event: CompletionOutboxEvent | None = None
    initial_delivery: CompletionOutboxDelivery | None = None

    def __post_init__(self) -> None:
        if type(self.effect) is not EffectContract:
            _invalid("effect must be exactly EffectContract")
        if (self.outbox_event is None) != (self.initial_delivery is None):
            _invalid("outbox event and initial delivery must be paired")
        if self.outbox_event is None:
            return
        if type(self.outbox_event) is not CompletionOutboxEvent:
            _invalid("outbox_event must be exactly CompletionOutboxEvent")
        if type(self.initial_delivery) is not CompletionOutboxDelivery:
            _invalid("initial_delivery must be exactly CompletionOutboxDelivery")
        expected = build_initial_completion_outbox_delivery(self.outbox_event)
        if (
            self.initial_delivery != expected
            or self.effect.effect_id != self.outbox_event.event_id
            or self.effect.effect_type != "completion_notification"
            or self.effect.idempotency_key != self.outbox_event.event_id
            or self.effect.input_artifact_sha256
            != self.outbox_event.outcome_descriptor_sha256
        ):
            _invalid("completion effect does not match the outbox authority")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": EFFECT_EVENT_CONTRACT_VERSION,
            "effect": self.effect.to_dict(),
            "outbox_event": (
                None if self.outbox_event is None else self.outbox_event.to_dict()
            ),
            "initial_delivery": (
                None
                if self.initial_delivery is None
                else self.initial_delivery.to_dict()
            ),
        }


@dataclass(frozen=True)
class EffectDeliveryTransitionRef:
    effect_id: str
    outbox_event_id: str
    previous_delivery: CompletionOutboxDelivery
    delivery: CompletionOutboxDelivery

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        _identifier(self.outbox_event_id, "outbox_event_id")
        if (
            type(self.previous_delivery) is not CompletionOutboxDelivery
            or type(self.delivery) is not CompletionOutboxDelivery
        ):
            _invalid("delivery revisions must be exact CompletionOutboxDelivery")
        try:
            verify_completion_outbox_delivery_transition(
                self.previous_delivery,
                self.delivery,
            )
        except Exception as error:
            raise EffectEventV1Error(
                "TBM_EFFECT_EVENT_INVALID",
                "effect delivery transition is invalid",
            ) from error
        if (
            self.effect_id != self.outbox_event_id
            or self.previous_delivery.event_id != self.outbox_event_id
            or self.delivery.event_id != self.outbox_event_id
        ):
            _invalid("effect delivery transition identity is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": EFFECT_EVENT_CONTRACT_VERSION,
            "effect_id": self.effect_id,
            "outbox_event_id": self.outbox_event_id,
            "previous_delivery": self.previous_delivery.to_dict(),
            "delivery": self.delivery.to_dict(),
        }


@dataclass(frozen=True)
class EffectCompensationRequestedRef:
    original_effect_id: str
    original_terminal_event_id: str
    compensation_effect: EffectContract

    def __post_init__(self) -> None:
        _identifier(self.original_effect_id, "original_effect_id")
        _identifier(
            self.original_terminal_event_id,
            "original_terminal_event_id",
        )
        if type(self.compensation_effect) is not EffectContract:
            _invalid("compensation_effect must be exactly EffectContract")
        if (
            self.compensation_effect.effect_id == self.original_effect_id
            or self.compensation_effect.requested_by_event_id
            != self.original_terminal_event_id
        ):
            _invalid("compensation must be a distinct causally linked effect")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": EFFECT_EVENT_CONTRACT_VERSION,
            "original_effect_id": self.original_effect_id,
            "original_terminal_event_id": self.original_terminal_event_id,
            "compensation_effect": self.compensation_effect.to_dict(),
        }


@dataclass(frozen=True)
class EffectCompensatedRef:
    original_effect_id: str
    compensation_effect_id: str
    compensation_request_event_id: str

    def __post_init__(self) -> None:
        for name in (
            "original_effect_id",
            "compensation_effect_id",
            "compensation_request_event_id",
        ):
            _identifier(getattr(self, name), name)
        if self.original_effect_id == self.compensation_effect_id:
            _invalid("compensation effect must differ from the original effect")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": EFFECT_EVENT_CONTRACT_VERSION,
            "original_effect_id": self.original_effect_id,
            "compensation_effect_id": self.compensation_effect_id,
            "compensation_request_event_id": (
                self.compensation_request_event_id
            ),
        }


def completion_effect_contract(
    outbox_event: CompletionOutboxEvent,
    completed_event: CanonicalEvent,
) -> EffectContract:
    if type(outbox_event) is not CompletionOutboxEvent:
        _invalid("outbox_event must be exactly CompletionOutboxEvent")
    _verify_completion_parent(outbox_event, completed_event)
    return EffectContract(
        effect_id=outbox_event.event_id,
        effect_type="completion_notification",
        idempotency_key=outbox_event.event_id,
        requested_by_event_id=completed_event.event_id,
        input_artifact_sha256=outbox_event.outcome_descriptor_sha256,
        authorization_event_id=completed_event.authorization_decision_id,
        compensation_supported=False,
    )


def build_completion_effect_requested_event(
    outbox_event: CompletionOutboxEvent,
    initial_delivery: CompletionOutboxDelivery,
    *,
    completed_event: CanonicalEvent,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    return build_effect_requested_event(
        EffectRequestedRef(
            effect=completion_effect_contract(outbox_event, completed_event),
            outbox_event=outbox_event,
            initial_delivery=initial_delivery,
        ),
        requested_by_event=completed_event,
        global_position=global_position,
        trusted_context=trusted_context,
    )


def build_effect_requested_event(
    reference: EffectRequestedRef,
    *,
    requested_by_event: CanonicalEvent,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    if type(reference) is not EffectRequestedRef:
        _invalid("reference must be exactly EffectRequestedRef")
    if type(requested_by_event) is not CanonicalEvent:
        _invalid("requested_by_event must be exactly CanonicalEvent")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if (
        reference.effect.requested_by_event_id != requested_by_event.event_id
        or reference.effect.authorization_event_id
        != trusted_context.authorization_decision_id
        or global_position <= requested_by_event.global_position
    ):
        _invalid("EffectRequested causation or authorization is invalid")
    _verify_scope(
        requested_by_event,
        trusted_context,
        require_authorization=True,
    )
    payload = reference.to_dict()
    effect = reference.effect
    event = build_canonical_event(
        event_id=_effect_event_id(EFFECT_REQUESTED_EVENT, effect.effect_id),
        event_type=EFFECT_REQUESTED_EVENT,
        event_version=EFFECT_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=effect_event_stream_id(effect.effect_id),
        stream_type=EFFECT_EVENT_STREAM_TYPE,
        stream_version=1,
        global_position=global_position,
        trusted_context=trusted_context,
        request_id=_effect_request_id(effect.effect_id),
        idempotency_key_sha256=_domain_sha256(
            b"tbm.effect-command-identity.v1\x00",
            {
                "effect_id": effect.effect_id,
                "idempotency_key": effect.idempotency_key,
            },
        ),
        request_sha256=_domain_sha256(
            b"tbm.effect-request-command.v1\x00",
            payload,
        ),
        correlation_id=_effect_correlation_id(effect.effect_id),
        causation_id=requested_by_event.event_id,
        occurred_at=(
            requested_by_event.occurred_at
            if reference.outbox_event is None
            else reference.outbox_event.occurred_at
        ),
        recorded_at=(
            requested_by_event.recorded_at
            if reference.outbox_event is None
            else reference.outbox_event.occurred_at
        ),
        producer=EFFECT_EVENT_PRODUCER,
        producer_version=EFFECT_EVENT_PRODUCER_VERSION,
        payload_schema=f"{EFFECT_REQUESTED_EVENT}.v1",
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id=EFFECT_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=payload,
    )
    parsed = parse_effect_requested_event(event)
    if parsed != reference:
        raise AssertionError("EffectRequested did not round-trip")
    return event


def build_effect_delivery_event_batch(
    previous_delivery: CompletionOutboxDelivery,
    delivery: CompletionOutboxDelivery,
    *,
    parent_event: CanonicalEvent,
    first_global_position: int,
    trusted_context: EventTrustedContext,
) -> tuple[CanonicalEvent, ...]:
    reference = EffectDeliveryTransitionRef(
        effect_id=delivery.event_id,
        outbox_event_id=delivery.event_id,
        previous_delivery=previous_delivery,
        delivery=delivery,
    )
    if type(parent_event) is not CanonicalEvent:
        _invalid("parent_event must be exactly CanonicalEvent")
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    if (
        parent_event.stream_id != effect_event_stream_id(delivery.event_id)
        or parent_event.stream_type != EFFECT_EVENT_STREAM_TYPE
        or parent_event.event_type not in EFFECT_EVENT_TYPES
        or first_global_position <= parent_event.global_position
    ):
        _invalid("effect delivery event parent is invalid")
    _verify_delivery_scope(parent_event, trusted_context)
    event_types = _delivery_transition_event_types(delivery)
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.effect-transition-identity.v1\x00",
        {
            "effect_id": delivery.event_id,
            "delivery_revision_id": delivery.delivery_revision_id,
        },
    )
    request_sha256 = _domain_sha256(
        b"tbm.effect-transition-command.v1\x00",
        reference.to_dict(),
    )
    events: list[CanonicalEvent] = []
    previous = parent_event
    for offset, event_type in enumerate(event_types):
        event = _build_stream_event(
            event_type=event_type,
            effect_id=delivery.event_id,
            payload=reference.to_dict(),
            parent_event=previous,
            global_position=first_global_position + offset,
            occurred_at=delivery.updated_at,
            trusted_context=trusted_context,
            identity_suffix=delivery.delivery_revision_id,
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=request_sha256,
        )
        parse_effect_delivery_event(event, parent_event=previous)
        events.append(event)
        previous = event
    return tuple(events)


def build_effect_compensation_requested_event(
    reference: EffectCompensationRequestedRef,
    *,
    original_requested_event: CanonicalEvent,
    original_terminal_event: CanonicalEvent,
    global_position: int,
    occurred_at: str,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    if type(reference) is not EffectCompensationRequestedRef:
        _invalid("reference must be exactly EffectCompensationRequestedRef")
    original = parse_effect_requested_event(original_requested_event)
    if (
        original.effect.effect_id != reference.original_effect_id
        or not original.effect.compensation_supported
        or original_terminal_event.event_type != EFFECT_SUCCEEDED_EVENT
        or original_terminal_event.stream_id
        != original_requested_event.stream_id
        or reference.original_terminal_event_id
        != original_terminal_event.event_id
        or reference.compensation_effect.authorization_event_id
        != trusted_context.authorization_decision_id
        or global_position <= original_terminal_event.global_position
    ):
        _invalid("compensation request does not match a compensable effect")
    _timestamp(occurred_at, "occurred_at")
    _verify_scope(
        original_terminal_event,
        trusted_context,
        require_authorization=True,
    )
    payload = reference.to_dict()
    event = build_canonical_event(
        event_id=_effect_event_id(
            EFFECT_COMPENSATION_REQUESTED_EVENT,
            reference.compensation_effect.effect_id,
        ),
        event_type=EFFECT_COMPENSATION_REQUESTED_EVENT,
        event_version=EFFECT_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=effect_event_stream_id(
            reference.compensation_effect.effect_id
        ),
        stream_type=EFFECT_EVENT_STREAM_TYPE,
        stream_version=1,
        global_position=global_position,
        trusted_context=trusted_context,
        request_id=_effect_request_id(
            reference.compensation_effect.effect_id
        ),
        idempotency_key_sha256=_domain_sha256(
            b"tbm.effect-compensation-command-identity.v1\x00",
            {
                "original_effect_id": reference.original_effect_id,
                "compensation_effect_id": (
                    reference.compensation_effect.effect_id
                ),
            },
        ),
        request_sha256=_domain_sha256(
            b"tbm.effect-compensation-request.v1\x00",
            payload,
        ),
        correlation_id=_effect_correlation_id(
            reference.compensation_effect.effect_id
        ),
        causation_id=original_terminal_event.event_id,
        occurred_at=canonical_rfc3339(occurred_at),
        recorded_at=canonical_rfc3339(occurred_at),
        producer=EFFECT_EVENT_PRODUCER,
        producer_version=EFFECT_EVENT_PRODUCER_VERSION,
        payload_schema=f"{EFFECT_COMPENSATION_REQUESTED_EVENT}.v1",
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id=EFFECT_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=payload,
    )
    parsed = parse_effect_compensation_requested_event(event)
    if parsed != reference:
        raise AssertionError("EffectCompensationRequested did not round-trip")
    return event


def build_effect_compensated_event(
    compensation_request_event: CanonicalEvent,
    *,
    global_position: int,
    occurred_at: str,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    request = parse_effect_compensation_requested_event(
        compensation_request_event
    )
    if global_position <= compensation_request_event.global_position:
        _invalid("EffectCompensated position must follow its request")
    _timestamp(occurred_at, "occurred_at")
    _verify_scope(
        compensation_request_event,
        trusted_context,
        require_authorization=True,
    )
    reference = EffectCompensatedRef(
        original_effect_id=request.original_effect_id,
        compensation_effect_id=request.compensation_effect.effect_id,
        compensation_request_event_id=compensation_request_event.event_id,
    )
    event = _build_stream_event(
        event_type=EFFECT_COMPENSATED_EVENT,
        effect_id=request.compensation_effect.effect_id,
        payload=reference.to_dict(),
        parent_event=compensation_request_event,
        global_position=global_position,
        occurred_at=occurred_at,
        trusted_context=trusted_context,
        identity_suffix=compensation_request_event.event_id,
    )
    parsed = parse_effect_compensated_event(
        event,
        compensation_request_event=compensation_request_event,
    )
    if parsed != reference:
        raise AssertionError("EffectCompensated did not round-trip")
    return event


def parse_effect_requested_event(event: CanonicalEvent) -> EffectRequestedRef:
    _verify_event_shape(event, EFFECT_REQUESTED_EVENT)
    if event.stream_version != 1 or event.previous_stream_event_sha256 is not None:
        _invalid("EffectRequested must begin an effect stream")
    payload = _payload(event)
    if set(payload) != {
        "contract_version",
        "effect",
        "outbox_event",
        "initial_delivery",
    } or payload.get("contract_version") != EFFECT_EVENT_CONTRACT_VERSION:
        _invalid("EffectRequested payload is invalid")
    effect = _parse_effect_contract(payload.get("effect"))
    outbox_payload = payload.get("outbox_event")
    delivery_payload = payload.get("initial_delivery")
    if outbox_payload is None:
        outbox_event = None
    elif type(outbox_payload) is dict:
        try:
            outbox_event = parse_completion_outbox_event(outbox_payload)
        except Exception as error:
            raise EffectEventV1Error(
                "TBM_EFFECT_EVENT_INVALID",
                "EffectRequested outbox event is invalid",
            ) from error
    else:
        _invalid("EffectRequested outbox event is invalid")
    if delivery_payload is None:
        delivery = None
    elif type(delivery_payload) is dict:
        try:
            delivery = parse_completion_outbox_delivery(delivery_payload)
        except Exception as error:
            raise EffectEventV1Error(
                "TBM_EFFECT_EVENT_INVALID",
                "EffectRequested initial delivery is invalid",
            ) from error
    else:
        _invalid("EffectRequested initial delivery is invalid")
    reference = EffectRequestedRef(effect, outbox_event, delivery)
    if (
        event.stream_id != effect_event_stream_id(effect.effect_id)
        or effect.requested_by_event_id != event.causation_id
        or effect.authorization_event_id != event.authorization_decision_id
        or event.event_id
        != _effect_event_id(EFFECT_REQUESTED_EVENT, effect.effect_id)
    ):
        _invalid("EffectRequested envelope linkage is invalid")
    return reference


def parse_effect_delivery_event(
    event: CanonicalEvent,
    *,
    parent_event: CanonicalEvent | None = None,
) -> EffectDeliveryTransitionRef:
    if type(event) is not CanonicalEvent or event.event_type not in _DELIVERY_EVENT_TYPES:
        _invalid("event is not an effect delivery event")
    _verify_event_shape(event, event.event_type)
    payload = _payload(event)
    if set(payload) != {
        "contract_version",
        "effect_id",
        "outbox_event_id",
        "previous_delivery",
        "delivery",
    } or payload.get("contract_version") != EFFECT_EVENT_CONTRACT_VERSION:
        _invalid("effect delivery payload is invalid")
    previous_payload = payload.get("previous_delivery")
    delivery_payload = payload.get("delivery")
    if type(previous_payload) is not dict or type(delivery_payload) is not dict:
        _invalid("effect delivery revisions are invalid")
    try:
        previous = parse_completion_outbox_delivery(previous_payload)
        delivery = parse_completion_outbox_delivery(delivery_payload)
    except Exception as error:
        raise EffectEventV1Error(
            "TBM_EFFECT_EVENT_INVALID",
            "effect delivery revision is invalid",
        ) from error
    reference = EffectDeliveryTransitionRef(
        effect_id=cast(str, payload["effect_id"]),
        outbox_event_id=cast(str, payload["outbox_event_id"]),
        previous_delivery=previous,
        delivery=delivery,
    )
    if (
        event.stream_id != effect_event_stream_id(reference.effect_id)
        or event.event_id
        != _effect_event_id(
            event.event_type,
            reference.effect_id,
            reference.delivery.delivery_revision_id,
        )
        or event.occurred_at != reference.delivery.updated_at
    ):
        _invalid("effect delivery envelope linkage is invalid")
    _verify_delivery_event_status(event.event_type, reference.delivery.status)
    if parent_event is not None:
        try:
            verify_event_parent(event, parent_event)
        except Exception as error:
            raise EffectEventV1Error(
                "TBM_EFFECT_EVENT_INVALID",
                "effect delivery stream parent is invalid",
            ) from error
        if not _same_delivery_scope(parent_event, event):
            _invalid("effect delivery scope changed")
        if event.event_type in {
            EFFECT_RETRY_SCHEDULED_EVENT,
            EFFECT_DEAD_LETTERED_EVENT,
        }:
            if parent_event.event_type != EFFECT_FAILED_EVENT:
                _invalid("effect retry or dead-letter requires EffectFailed")
            if parse_effect_delivery_event(parent_event) != reference:
                _invalid("effect failure and disposition differ")
    return reference


def parse_effect_compensation_requested_event(
    event: CanonicalEvent,
) -> EffectCompensationRequestedRef:
    _verify_event_shape(event, EFFECT_COMPENSATION_REQUESTED_EVENT)
    if event.stream_version != 1 or event.previous_stream_event_sha256 is not None:
        _invalid("EffectCompensationRequested must begin a new effect stream")
    payload = _payload(event)
    if set(payload) != {
        "contract_version",
        "original_effect_id",
        "original_terminal_event_id",
        "compensation_effect",
    } or payload.get("contract_version") != EFFECT_EVENT_CONTRACT_VERSION:
        _invalid("EffectCompensationRequested payload is invalid")
    reference = EffectCompensationRequestedRef(
        original_effect_id=cast(str, payload["original_effect_id"]),
        original_terminal_event_id=cast(
            str,
            payload["original_terminal_event_id"],
        ),
        compensation_effect=_parse_effect_contract(
            payload.get("compensation_effect")
        ),
    )
    if (
        reference.original_terminal_event_id != event.causation_id
        or reference.compensation_effect.authorization_event_id
        != event.authorization_decision_id
        or event.stream_id
        != effect_event_stream_id(reference.compensation_effect.effect_id)
        or event.event_id
        != _effect_event_id(
            EFFECT_COMPENSATION_REQUESTED_EVENT,
            reference.compensation_effect.effect_id,
        )
    ):
        _invalid("EffectCompensationRequested envelope linkage is invalid")
    return reference


def parse_effect_compensated_event(
    event: CanonicalEvent,
    *,
    compensation_request_event: CanonicalEvent | None = None,
) -> EffectCompensatedRef:
    _verify_event_shape(event, EFFECT_COMPENSATED_EVENT)
    payload = _payload(event)
    if set(payload) != {
        "contract_version",
        "original_effect_id",
        "compensation_effect_id",
        "compensation_request_event_id",
    } or payload.get("contract_version") != EFFECT_EVENT_CONTRACT_VERSION:
        _invalid("EffectCompensated payload is invalid")
    reference = EffectCompensatedRef(
        original_effect_id=cast(str, payload["original_effect_id"]),
        compensation_effect_id=cast(str, payload["compensation_effect_id"]),
        compensation_request_event_id=cast(
            str,
            payload["compensation_request_event_id"],
        ),
    )
    if (
        event.stream_id
        != effect_event_stream_id(reference.compensation_effect_id)
        or reference.compensation_request_event_id != event.causation_id
        or event.event_id
        != _effect_event_id(
            EFFECT_COMPENSATED_EVENT,
            reference.compensation_effect_id,
            reference.compensation_request_event_id,
        )
    ):
        _invalid("EffectCompensated envelope linkage is invalid")
    if compensation_request_event is not None:
        request = parse_effect_compensation_requested_event(
            compensation_request_event
        )
        try:
            verify_event_parent(event, compensation_request_event)
        except Exception as error:
            raise EffectEventV1Error(
                "TBM_EFFECT_EVENT_INVALID",
                "EffectCompensated parent is invalid",
            ) from error
        if (
            request.original_effect_id != reference.original_effect_id
            or request.compensation_effect.effect_id
            != reference.compensation_effect_id
            or not _same_scope(compensation_request_event, event)
        ):
            _invalid("EffectCompensated request is inconsistent")
    return reference


def effect_event_stream_id(effect_id: str) -> str:
    _identifier(effect_id, "effect_id")
    return "effect_stream_sha256_" + _domain_sha256(
        b"tbm.effect-stream.v1\x00",
        {"effect_id": effect_id},
    ).removeprefix("sha256:")


def effect_event_payload_schema(event_type: str) -> dict[str, object]:
    if event_type not in EFFECT_EVENT_TYPES:
        _invalid("event_type is not an effect event")
    identifier: dict[str, object] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": _IDENTIFIER_RE.pattern,
    }
    bounded_identifier: dict[str, object] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
    }
    digest: dict[str, object] = {
        "type": "string",
        "pattern": r"^sha256:[0-9a-f]{64}$",
    }
    timestamp: dict[str, object] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }
    effect = _effect_contract_schema(identifier, digest)
    if event_type == EFFECT_REQUESTED_EVENT:
        properties: dict[str, object] = {
            "contract_version": {"const": EFFECT_EVENT_CONTRACT_VERSION},
            "effect": effect,
            "outbox_event": {
                "oneOf": [
                    {"type": "null"},
                    _outbox_event_schema(
                        bounded_identifier,
                        digest,
                        timestamp,
                    ),
                ]
            },
            "initial_delivery": {
                "oneOf": [
                    {"type": "null"},
                    _delivery_schema(
                        bounded_identifier,
                        digest,
                        timestamp,
                    ),
                ]
            },
        }
    elif event_type in _DELIVERY_EVENT_TYPES:
        properties = {
            "contract_version": {"const": EFFECT_EVENT_CONTRACT_VERSION},
            "effect_id": identifier,
            "outbox_event_id": identifier,
            "previous_delivery": _delivery_schema(
                bounded_identifier,
                digest,
                timestamp,
            ),
            "delivery": _delivery_schema(
                bounded_identifier,
                digest,
                timestamp,
            ),
        }
    elif event_type == EFFECT_COMPENSATION_REQUESTED_EVENT:
        properties = {
            "contract_version": {"const": EFFECT_EVENT_CONTRACT_VERSION},
            "original_effect_id": identifier,
            "original_terminal_event_id": identifier,
            "compensation_effect": effect,
        }
    else:
        properties = {
            "contract_version": {"const": EFFECT_EVENT_CONTRACT_VERSION},
            "original_effect_id": identifier,
            "compensation_effect_id": identifier,
            "compensation_request_event_id": identifier,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _build_stream_event(
    *,
    event_type: str,
    effect_id: str,
    payload: Mapping[str, object],
    parent_event: CanonicalEvent,
    global_position: int,
    occurred_at: str,
    trusted_context: EventTrustedContext,
    identity_suffix: str,
    idempotency_key_sha256: str | None = None,
    request_sha256: str | None = None,
) -> CanonicalEvent:
    event = build_canonical_event(
        event_id=_effect_event_id(event_type, effect_id, identity_suffix),
        event_type=event_type,
        event_version=EFFECT_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=effect_event_stream_id(effect_id),
        stream_type=EFFECT_EVENT_STREAM_TYPE,
        stream_version=parent_event.stream_version + 1,
        global_position=global_position,
        trusted_context=trusted_context,
        request_id=_effect_request_id(effect_id),
        idempotency_key_sha256=(
            idempotency_key_sha256
            if idempotency_key_sha256 is not None
            else _domain_sha256(
                b"tbm.effect-transition-identity.v1\x00",
                {
                    "event_type": event_type,
                    "effect_id": effect_id,
                    "identity_suffix": identity_suffix,
                },
            )
        ),
        request_sha256=(
            request_sha256
            if request_sha256 is not None
            else _domain_sha256(
                b"tbm.effect-transition-command.v1\x00",
                {"event_type": event_type, "payload": payload},
            )
        ),
        correlation_id=_effect_correlation_id(effect_id),
        causation_id=parent_event.event_id,
        occurred_at=canonical_rfc3339(occurred_at),
        recorded_at=canonical_rfc3339(occurred_at),
        producer=EFFECT_EVENT_PRODUCER,
        producer_version=EFFECT_EVENT_PRODUCER_VERSION,
        payload_schema=f"{event_type}.v1",
        previous_stream_event_sha256=parent_event.event_sha256,
        classification="internal",
        retention_policy_id=EFFECT_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=payload,
    )
    try:
        verify_event_parent(event, parent_event)
    except Exception as error:
        raise EffectEventV1Error(
            "TBM_EFFECT_EVENT_INVALID",
            "effect stream parent is invalid",
        ) from error
    return event


def _delivery_transition_event_types(
    delivery: CompletionOutboxDelivery,
) -> tuple[str, ...]:
    if delivery.status == "leased":
        return (EFFECT_STARTED_EVENT,)
    if delivery.status == "delivered":
        return (EFFECT_SUCCEEDED_EVENT,)
    if delivery.status == "retry_wait":
        return (EFFECT_FAILED_EVENT, EFFECT_RETRY_SCHEDULED_EVENT)
    if delivery.status == "dead_letter":
        return (EFFECT_FAILED_EVENT, EFFECT_DEAD_LETTERED_EVENT)
    _invalid("initial pending delivery is represented by EffectRequested")


def _verify_delivery_event_status(event_type: str, status: str) -> None:
    expected = {
        EFFECT_STARTED_EVENT: frozenset({"leased"}),
        EFFECT_SUCCEEDED_EVENT: frozenset({"delivered"}),
        EFFECT_FAILED_EVENT: frozenset({"retry_wait", "dead_letter"}),
        EFFECT_RETRY_SCHEDULED_EVENT: frozenset({"retry_wait"}),
        EFFECT_DEAD_LETTERED_EVENT: frozenset({"dead_letter"}),
    }
    if status not in expected[event_type]:
        _invalid("effect event type does not match delivery status")


def _verify_completion_parent(
    outbox_event: CompletionOutboxEvent,
    completed_event: CanonicalEvent,
) -> None:
    if (
        type(completed_event) is not CanonicalEvent
        or completed_event.event_type != GATE_SESSION_COMPLETED_EVENT
        or completed_event.tenant_id != outbox_event.tenant_id
        or completed_event.repository_id != outbox_event.repository_id
    ):
        _invalid("completion effect parent is invalid")
    payload = _payload(completed_event)
    session = payload.get("session")
    if not isinstance(session, Mapping) or (
        session.get("session_id") != outbox_event.session_id
        or session.get("trace_id") != outbox_event.trace_id
        or session.get("run_id") != outbox_event.run_id
        or session.get("usage_decision_id") != outbox_event.usage_decision_id
        or session.get("run_outcome_id") != outbox_event.run_outcome_id
        or session.get("status") != "completed"
    ):
        _invalid("completion effect parent does not match the outbox event")


def _parse_effect_contract(value: object) -> EffectContract:
    if type(value) is not dict or set(value) != {
        "effect_id",
        "effect_type",
        "idempotency_key",
        "requested_by_event_id",
        "input_artifact_sha256",
        "authorization_event_id",
        "compensation_supported",
    }:
        _invalid("effect contract payload is invalid")
    return EffectContract(
        effect_id=cast(str, value["effect_id"]),
        effect_type=cast(str, value["effect_type"]),
        idempotency_key=cast(str, value["idempotency_key"]),
        requested_by_event_id=cast(str, value["requested_by_event_id"]),
        input_artifact_sha256=cast(str, value["input_artifact_sha256"]),
        authorization_event_id=cast(str, value["authorization_event_id"]),
        compensation_supported=cast(bool, value["compensation_supported"]),
    )


def _effect_contract_schema(
    identifier: Mapping[str, object],
    digest: Mapping[str, object],
) -> dict[str, object]:
    properties: dict[str, object] = {
        "effect_id": identifier,
        "effect_type": identifier,
        "idempotency_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
        },
        "requested_by_event_id": identifier,
        "input_artifact_sha256": digest,
        "authorization_event_id": identifier,
        "compensation_supported": {"type": "boolean"},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _outbox_event_schema(
    identifier: Mapping[str, object],
    digest: Mapping[str, object],
    timestamp: Mapping[str, object],
) -> dict[str, object]:
    properties: dict[str, object] = {
        "contract_version": {"const": COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION},
        "event_id": identifier,
        "event_type": {"const": "execution_completed"},
        "tenant_id": identifier,
        "repository_id": identifier,
        "session_id": identifier,
        "trace_id": identifier,
        "run_id": identifier,
        "usage_decision_id": identifier,
        "run_outcome_id": identifier,
        "outcome_descriptor_sha256": digest,
        "occurred_at": timestamp,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _delivery_schema(
    identifier: Mapping[str, object],
    digest: Mapping[str, object],
    timestamp: Mapping[str, object],
) -> dict[str, object]:
    optional_timestamp = {"oneOf": [{"type": "null"}, timestamp]}
    optional_identifier = {"oneOf": [{"type": "null"}, identifier]}
    optional_digest = {"oneOf": [{"type": "null"}, digest]}
    properties: dict[str, object] = {
        "contract_version": {
            "const": COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION
        },
        "delivery_revision_id": identifier,
        "event_id": identifier,
        "version": {"type": "integer", "minimum": 1},
        "status": {
            "enum": [
                "pending",
                "leased",
                "retry_wait",
                "delivered",
                "dead_letter",
            ]
        },
        "attempt_count": {"type": "integer", "minimum": 0, "maximum": 1000},
        "updated_at": timestamp,
        "available_at": optional_timestamp,
        "worker_id": optional_identifier,
        "lease_expires_at": optional_timestamp,
        "delivered_at": optional_timestamp,
        "last_error_code": {
            "oneOf": [
                {"type": "null"},
                {"type": "string", "minLength": 1, "maxLength": 256},
            ]
        },
        "response_sha256": optional_digest,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _verify_event_shape(event: CanonicalEvent, event_type: str) -> None:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if (
        event.event_type != event_type
        or event.event_version != EFFECT_EVENT_VERSION
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != EFFECT_EVENT_STREAM_TYPE
        or event.payload_schema != f"{event_type}.v1"
        or event.classification != "internal"
        or event.retention_policy_id != EFFECT_EVENT_RETENTION_POLICY_ID
        or event.artifact_refs
    ):
        _invalid("effect event envelope is invalid")


def _verify_scope(
    event: CanonicalEvent,
    trusted_context: EventTrustedContext,
    *,
    require_authorization: bool,
) -> None:
    fields = (
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "principal_id",
        "agent_client_id",
        "actor_type",
        "actor_id",
    )
    if any(
        getattr(event, name) != getattr(trusted_context, name)
        for name in fields
    ) or (
        require_authorization
        and event.authorization_decision_id
        != trusted_context.authorization_decision_id
    ):
        _invalid("event is outside the trusted effect scope")


def _same_scope(left: CanonicalEvent, right: CanonicalEvent) -> bool:
    fields = (
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "principal_id",
        "agent_client_id",
        "actor_type",
        "actor_id",
        "authorization_decision_id",
    )
    return all(getattr(left, name) == getattr(right, name) for name in fields)


def _verify_delivery_scope(
    event: CanonicalEvent,
    trusted_context: EventTrustedContext,
) -> None:
    fields = (
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "principal_id",
        "agent_client_id",
        "authorization_decision_id",
    )
    if any(
        getattr(event, name) != getattr(trusted_context, name)
        for name in fields
    ) or trusted_context.actor_type != "worker":
        _invalid("delivery event is outside the trusted effect authority")


def _same_delivery_scope(
    left: CanonicalEvent,
    right: CanonicalEvent,
) -> bool:
    fields = (
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "principal_id",
        "agent_client_id",
        "authorization_decision_id",
    )
    return (
        all(getattr(left, name) == getattr(right, name) for name in fields)
        and right.actor_type == "worker"
    )


def _payload(event: CanonicalEvent) -> dict[str, object]:
    try:
        payload = event.to_dict()["payload"]
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise EffectEventV1Error(
            "TBM_EFFECT_EVENT_INVALID",
            "effect event payload is not canonical JSON",
        ) from error
    if type(payload) is not dict:
        _invalid("effect event payload must be an object")
    return cast(dict[str, object], payload)


def _effect_event_id(
    event_type: str,
    effect_id: str,
    identity_suffix: str | None = None,
) -> str:
    return "evt_effect_" + _domain_sha256(
        b"tbm.effect-event-identity.v1\x00",
        {
            "event_type": event_type,
            "effect_id": effect_id,
            "identity_suffix": identity_suffix,
        },
    ).removeprefix("sha256:")


def _effect_request_id(effect_id: str) -> str:
    return "effect_request_" + _domain_sha256(
        b"tbm.effect-request.v1\x00",
        {"effect_id": effect_id},
    ).removeprefix("sha256:")[:48]


def _effect_correlation_id(effect_id: str) -> str:
    return "effect_correlation_" + _domain_sha256(
        b"tbm.effect-correlation.v1\x00",
        {"effect_id": effect_id},
    ).removeprefix("sha256:")[:44]


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a bounded identifier")
    return value


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a SHA-256 digest")
    return value


def _timestamp(value: object, name: str) -> str:
    if type(value) is not str or canonical_rfc3339(value) != value:
        _invalid(f"{name} must be a canonical RFC 3339 timestamp")
    return value


def _domain_sha256(domain: bytes, value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise EffectEventV1Error(
            "TBM_EFFECT_EVENT_INVALID",
            "effect event identity input is not canonical JSON",
        ) from error
    return "sha256:" + hashlib.sha256(domain + payload).hexdigest()


def _invalid(message: str) -> NoReturn:
    raise EffectEventV1Error("TBM_EFFECT_EVENT_INVALID", message)


__all__ = [
    "EFFECT_COMPENSATED_EVENT",
    "EFFECT_COMPENSATION_REQUESTED_EVENT",
    "EFFECT_DEAD_LETTERED_EVENT",
    "EFFECT_EVENT_CONTRACT_VERSION",
    "EFFECT_EVENT_TYPES",
    "EFFECT_EVENT_VERSION",
    "EFFECT_FAILED_EVENT",
    "EFFECT_REQUESTED_EVENT",
    "EFFECT_RETRY_SCHEDULED_EVENT",
    "EFFECT_STARTED_EVENT",
    "EFFECT_SUCCEEDED_EVENT",
    "EffectCompensatedRef",
    "EffectCompensationRequestedRef",
    "EffectContract",
    "EffectDeliveryTransitionRef",
    "EffectEventV1Error",
    "EffectRequestedRef",
    "build_completion_effect_requested_event",
    "build_effect_compensated_event",
    "build_effect_compensation_requested_event",
    "build_effect_delivery_event_batch",
    "build_effect_requested_event",
    "completion_effect_contract",
    "effect_event_payload_schema",
    "effect_event_stream_id",
    "parse_effect_compensated_event",
    "parse_effect_compensation_requested_event",
    "parse_effect_delivery_event",
    "parse_effect_requested_event",
]
