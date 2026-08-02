from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, cast

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError
from .event_registry_v1 import (
    EventPayloadRegistration,
    EventRegistryV1Error,
    EventTypeRegistry,
)
from .event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    build_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerIdempotency,
    verify_ledger_append_receipt,
)


EFFECT_RECEIPT_PROTOCOL_VERSION = "tbm.effect-receipt.v1"
EFFECT_RECEIPT_STREAM_TYPE = "effect_receipt"
EFFECT_RECEIPT_MAX_ATTEMPTS = 16
EFFECT_RECEIPT_MAX_STREAM_EVENTS = 96
EFFECT_RECEIPT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.invalid/schemas/"
    "effect_receipt_payload_registry_v1.schema.json"
)

EFFECT_REQUESTED = "tbm.effect.requested"
EFFECT_AUTHORIZED = "tbm.effect.authorized"
EFFECT_STARTED = "tbm.effect.started"
EFFECT_PROVIDER_REQUEST_RECORDED = "tbm.effect.provider_request_recorded"
EFFECT_RECEIPT_RECORDED = "tbm.effect.receipt_recorded"
EFFECT_SUCCEEDED = "tbm.effect.succeeded"
EFFECT_RESULT_UNKNOWN = "tbm.effect.result_unknown"
EFFECT_FAILED = "tbm.effect.failed"
EFFECT_RETRY_SCHEDULED = "tbm.effect.retry_scheduled"
EFFECT_DEAD_LETTERED = "tbm.effect.dead_lettered"
EFFECT_COMPENSATION_REQUESTED = "tbm.effect.compensation_requested"
EFFECT_COMPENSATED = "tbm.effect.compensated"

EFFECT_RECEIPT_EVENT_TYPES = tuple(
    sorted(
        {
            EFFECT_REQUESTED,
            EFFECT_AUTHORIZED,
            EFFECT_STARTED,
            EFFECT_PROVIDER_REQUEST_RECORDED,
            EFFECT_RECEIPT_RECORDED,
            EFFECT_SUCCEEDED,
            EFFECT_RESULT_UNKNOWN,
            EFFECT_FAILED,
            EFFECT_RETRY_SCHEDULED,
            EFFECT_DEAD_LETTERED,
            EFFECT_COMPENSATION_REQUESTED,
            EFFECT_COMPENSATED,
        }
    )
)

EffectLifecycleStatus = Literal[
    "requested",
    "authorized",
    "executing",
    "provider_confirmed",
    "unknown",
    "receipt_recorded",
    "failed",
    "retry_wait",
    "succeeded",
    "dead_lettered",
    "compensated",
]
EffectUnknownReason = Literal[
    "timeout",
    "response_lost",
    "process_interrupted",
    "acknowledgement_uncertain",
]
EffectFailurePhase = Literal[
    "pre_send",
    "provider_rejected",
    "reconciled_absent",
]

_UNKNOWN_REASONS = frozenset(
    {"timeout", "response_lost", "process_interrupted", "acknowledgement_uncertain"}
)
_FAILURE_PHASES = frozenset(
    {"pre_send", "provider_rejected", "reconciled_absent"}
)
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_PROVIDER_REQUEST_ID_RE = re.compile(r"^[\x21-\x7e]{1,256}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_PAYLOAD_SCHEMAS = {
    event_type: "tbm.effect." + event_type.removeprefix("tbm.effect.").replace("_", "-") + ".v1"
    for event_type in EFFECT_RECEIPT_EVENT_TYPES
}


class EffectReceiptV1Error(V3ContractError):
    """Stable failure for the external-effect receipt protocol."""


@dataclass(frozen=True)
class TrustedEffectProvider:
    """Provider identity supplied by trusted service composition, never request JSON."""

    provider_id: str
    registration_sha256: str
    adapter_id: str
    adapter_version: str

    def __post_init__(self) -> None:
        _identifier(self.provider_id, "provider_id")
        _digest(self.registration_sha256, "registration_sha256")
        _identifier(self.adapter_id, "adapter_id")
        _code(self.adapter_version, "adapter_version")

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "registration_sha256": self.registration_sha256,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
        }


@dataclass(frozen=True)
class EffectContract:
    effect_id: str
    effect_type: str
    idempotency_key_sha256: str
    requested_by_event_id: str
    input_artifact_sha256: str
    authorization_event_id: str
    compensation_supported: bool
    max_attempts: int

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        _code(self.effect_type, "effect_type")
        _digest(self.idempotency_key_sha256, "idempotency_key_sha256")
        _event_id(self.requested_by_event_id, "requested_by_event_id")
        _digest(self.input_artifact_sha256, "input_artifact_sha256")
        _identifier(self.authorization_event_id, "authorization_event_id")
        if type(self.compensation_supported) is not bool:
            _fail(
                "TBM_EFFECT_CONTRACT_INVALID",
                "compensation_supported must be a boolean",
            )
        if (
            type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= EFFECT_RECEIPT_MAX_ATTEMPTS
        ):
            _fail(
                "TBM_EFFECT_CONTRACT_INVALID",
                "max_attempts is outside the bounded protocol range",
            )
        expected_idempotency = effect_idempotency_key_sha256(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            requested_by_event_id=self.requested_by_event_id,
            input_artifact_sha256=self.input_artifact_sha256,
            authorization_event_id=self.authorization_event_id,
            compensation_supported=self.compensation_supported,
            max_attempts=self.max_attempts,
        )
        if self.idempotency_key_sha256 != expected_idempotency:
            _fail(
                "TBM_EFFECT_IDEMPOTENCY_CONFLICT",
                "effect idempotency key must be derived from the exact immutable intent",
            )

    @property
    def input_artifact_id(self) -> str:
        return "artifact_sha256_" + self.input_artifact_sha256.removeprefix("sha256:")

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "requested_by_event_id": self.requested_by_event_id,
            "input_artifact_id": self.input_artifact_id,
            "input_artifact_sha256": self.input_artifact_sha256,
            "authorization_event_id": self.authorization_event_id,
            "compensation_supported": self.compensation_supported,
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True)
class EffectEventDraft:
    event_type: str
    contract: EffectContract
    occurred_at: str
    input_artifact: EventArtifactRef | None = None
    attempt_number: int | None = None
    attempt_id: str | None = None
    provider: TrustedEffectProvider | None = None
    provider_request_id: str | None = None
    canonical_request_sha256: str | None = None
    receipt_artifact: EventArtifactRef | None = None
    receipt_sha256: str | None = None
    result_sha256: str | None = None
    unknown_reason: EffectUnknownReason | None = None
    failure_phase: EffectFailurePhase | None = None
    failure_code: str | None = None
    retryable: bool | None = None
    retry_at: str | None = None
    reconciliation_artifact: EventArtifactRef | None = None
    reconciliation_sha256: str | None = None
    parent_effect_id: str | None = None
    parent_success_event_id: str | None = None
    parent_success_event_sha256: str | None = None
    parent_receipt_sha256: str | None = None
    classification: EventClassification = "confidential"
    retention_policy_id: str = "retention_effect_receipt"

    def __post_init__(self) -> None:
        if self.event_type not in EFFECT_RECEIPT_EVENT_TYPES:
            _fail("TBM_EFFECT_EVENT_TYPE_INVALID", "event_type is not registered")
        if type(self.contract) is not EffectContract:
            _fail("TBM_EFFECT_CONTRACT_INVALID", "contract must be exactly EffectContract")
        _canonical_timestamp(self.occurred_at, "occurred_at")
        if self.classification not in _CLASSIFICATION_RANK:
            _fail("TBM_EFFECT_CLASSIFICATION_INVALID", "classification is invalid")
        _identifier(self.retention_policy_id, "retention_policy_id")
        _validate_optional_fields(self)
        _validate_draft_shape(self)

    @property
    def artifact_refs(self) -> tuple[EventArtifactRef, ...]:
        values = tuple(
            item
            for item in (
                self.input_artifact,
                self.receipt_artifact,
                self.reconciliation_artifact,
            )
            if item is not None
        )
        return tuple(sorted(values, key=lambda item: item.artifact_id))

    def payload(self, *, sequence: int) -> dict[str, object]:
        return {
            "protocol_version": EFFECT_RECEIPT_PROTOCOL_VERSION,
            "point": self.event_type.removeprefix("tbm.effect."),
            "effect_id": self.contract.effect_id,
            "sequence": sequence,
            "occurred_at": self.occurred_at,
            "contract": self.contract.to_dict(),
            "attempt_number": self.attempt_number,
            "attempt_id": self.attempt_id,
            "provider": None if self.provider is None else self.provider.to_dict(),
            "provider_request_id": self.provider_request_id,
            "canonical_request_sha256": self.canonical_request_sha256,
            "receipt_artifact_id": (
                None if self.receipt_artifact is None else self.receipt_artifact.artifact_id
            ),
            "receipt_sha256": self.receipt_sha256,
            "result_sha256": self.result_sha256,
            "unknown_reason": self.unknown_reason,
            "failure_phase": self.failure_phase,
            "failure_code": self.failure_code,
            "retryable": self.retryable,
            "retry_at": self.retry_at,
            "reconciliation_artifact_id": (
                None
                if self.reconciliation_artifact is None
                else self.reconciliation_artifact.artifact_id
            ),
            "reconciliation_sha256": self.reconciliation_sha256,
            "parent_effect_id": self.parent_effect_id,
            "parent_success_event_id": self.parent_success_event_id,
            "parent_success_event_sha256": self.parent_success_event_sha256,
            "parent_receipt_sha256": self.parent_receipt_sha256,
        }

    def command_value(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "classification": self.classification,
            "retention_policy_id": self.retention_policy_id,
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "draft": self.payload(sequence=0),
        }


@dataclass(frozen=True)
class EffectLifecycleProjection:
    effect_id: str
    effect_type: str
    status: EffectLifecycleStatus
    contract: EffectContract
    sequence: int
    request_event_id: str
    request_event_sha256: str
    attempt_count: int
    current_attempt_id: str | None
    provider: TrustedEffectProvider | None
    provider_request_id: str | None
    canonical_request_sha256: str | None
    receipt_sha256: str | None
    result_sha256: str | None
    unknown_reason: EffectUnknownReason | None
    failure_phase: EffectFailurePhase | None
    failure_code: str | None
    retryable: bool | None
    retry_at: str | None
    reconciliation_sha256: str | None
    parent_effect_id: str | None
    parent_success_event_id: str | None
    parent_success_event_sha256: str | None
    parent_receipt_sha256: str | None
    terminal_event_id: str | None
    terminal_event_sha256: str | None
    last_event_id: str
    last_event_sha256: str
    last_global_position: int
    projection_sha256: str

    def to_dict(self, *, include_projection_sha256: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol_version": EFFECT_RECEIPT_PROTOCOL_VERSION,
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "status": self.status,
            "contract": self.contract.to_dict(),
            "sequence": self.sequence,
            "request_event_id": self.request_event_id,
            "request_event_sha256": self.request_event_sha256,
            "attempt_count": self.attempt_count,
            "current_attempt_id": self.current_attempt_id,
            "provider": None if self.provider is None else self.provider.to_dict(),
            "provider_request_id": self.provider_request_id,
            "canonical_request_sha256": self.canonical_request_sha256,
            "receipt_sha256": self.receipt_sha256,
            "result_sha256": self.result_sha256,
            "unknown_reason": self.unknown_reason,
            "failure_phase": self.failure_phase,
            "failure_code": self.failure_code,
            "retryable": self.retryable,
            "retry_at": self.retry_at,
            "reconciliation_sha256": self.reconciliation_sha256,
            "parent_effect_id": self.parent_effect_id,
            "parent_success_event_id": self.parent_success_event_id,
            "parent_success_event_sha256": self.parent_success_event_sha256,
            "parent_receipt_sha256": self.parent_receipt_sha256,
            "terminal_event_id": self.terminal_event_id,
            "terminal_event_sha256": self.terminal_event_sha256,
            "last_event_id": self.last_event_id,
            "last_event_sha256": self.last_event_sha256,
            "last_global_position": self.last_global_position,
        }
        if include_projection_sha256:
            value["projection_sha256"] = self.projection_sha256
        return value


@dataclass
class _State:
    contract: EffectContract
    status: EffectLifecycleStatus
    sequence: int
    request_event_id: str
    request_event_sha256: str
    attempt_count: int = 0
    current_attempt_id: str | None = None
    provider: TrustedEffectProvider | None = None
    provider_request_id: str | None = None
    canonical_request_sha256: str | None = None
    receipt_sha256: str | None = None
    result_sha256: str | None = None
    unknown_reason: EffectUnknownReason | None = None
    failure_phase: EffectFailurePhase | None = None
    failure_code: str | None = None
    retryable: bool | None = None
    retry_at: str | None = None
    reconciliation_sha256: str | None = None
    parent_effect_id: str | None = None
    parent_success_event_id: str | None = None
    parent_success_event_sha256: str | None = None
    parent_receipt_sha256: str | None = None
    terminal_event_id: str | None = None
    terminal_event_sha256: str | None = None
    last_event_id: str = ""
    last_event_sha256: str = ""
    last_global_position: int = 0


def effect_receipt_stream_id(effect_id: str) -> str:
    _identifier(effect_id, "effect_id")
    return "effect_receipt_" + hashlib.sha256(effect_id.encode("utf-8")).hexdigest()


def effect_idempotency_key_sha256(
    *,
    effect_id: str,
    effect_type: str,
    requested_by_event_id: str,
    input_artifact_sha256: str,
    authorization_event_id: str,
    compensation_supported: bool,
    max_attempts: int,
) -> str:
    _identifier(effect_id, "effect_id")
    _code(effect_type, "effect_type")
    _event_id(requested_by_event_id, "requested_by_event_id")
    _digest(input_artifact_sha256, "input_artifact_sha256")
    _identifier(authorization_event_id, "authorization_event_id")
    if type(compensation_supported) is not bool:
        _fail(
            "TBM_EFFECT_CONTRACT_INVALID",
            "compensation_supported must be a boolean",
        )
    if (
        type(max_attempts) is not int
        or not 1 <= max_attempts <= EFFECT_RECEIPT_MAX_ATTEMPTS
    ):
        _fail("TBM_EFFECT_CONTRACT_INVALID", "max_attempts is invalid")
    return _domain_sha256(
        b"tbm.effect-idempotency.v1\x00",
        {
            "effect_id": effect_id,
            "effect_type": effect_type,
            "requested_by_event_id": requested_by_event_id,
            "input_artifact_sha256": input_artifact_sha256,
            "authorization_event_id": authorization_event_id,
            "compensation_supported": compensation_supported,
            "max_attempts": max_attempts,
        },
    )


def effect_attempt_id(effect_id: str, attempt_number: int) -> str:
    _identifier(effect_id, "effect_id")
    _attempt_number(attempt_number)
    digest = hashlib.sha256(
        (EFFECT_RECEIPT_PROTOCOL_VERSION + "\x00" + effect_id + f"\x00{attempt_number}").encode(
            "utf-8"
        )
    ).hexdigest()
    return "effect_attempt_" + digest


def effect_provider_request_sha256(
    contract: EffectContract,
    provider: TrustedEffectProvider,
    attempt_number: int,
) -> str:
    if type(contract) is not EffectContract:
        _fail("TBM_EFFECT_CONTRACT_INVALID", "contract must be exactly EffectContract")
    if type(provider) is not TrustedEffectProvider:
        _fail("TBM_EFFECT_PROVIDER_INVALID", "provider must be trusted provider context")
    _attempt_number(attempt_number)
    return _domain_sha256(
        b"tbm.effect-provider-request.v1\x00",
        {
            "contract": contract.to_dict(),
            "attempt_number": attempt_number,
            "attempt_id": effect_attempt_id(contract.effect_id, attempt_number),
            "provider": provider.to_dict(),
        },
    )


def build_effect_requested_draft(
    contract: EffectContract,
    *,
    input_artifact: EventArtifactRef,
    occurred_at: str,
    classification: EventClassification = "confidential",
    retention_policy_id: str = "retention_effect_receipt",
) -> EffectEventDraft:
    return EffectEventDraft(
        event_type=EFFECT_REQUESTED,
        contract=contract,
        occurred_at=occurred_at,
        input_artifact=input_artifact,
        classification=classification,
        retention_policy_id=retention_policy_id,
    )


def build_effect_authorized_draft(
    contract: EffectContract, *, occurred_at: str
) -> EffectEventDraft:
    return EffectEventDraft(
        event_type=EFFECT_AUTHORIZED, contract=contract, occurred_at=occurred_at
    )


def build_effect_started_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_STARTED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        occurred_at=occurred_at,
    )


def build_effect_provider_request_recorded_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_PROVIDER_REQUEST_RECORDED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        occurred_at=occurred_at,
    )


def build_effect_result_unknown_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str | None,
    unknown_reason: EffectUnknownReason,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_RESULT_UNKNOWN,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        unknown_reason=unknown_reason,
        occurred_at=occurred_at,
    )


def build_effect_receipt_recorded_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str,
    receipt_artifact: EventArtifactRef,
    result_sha256: str,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_RECEIPT_RECORDED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        receipt_artifact=receipt_artifact,
        receipt_sha256=receipt_artifact.content_sha256,
        result_sha256=result_sha256,
        occurred_at=occurred_at,
    )


def build_effect_succeeded_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str,
    receipt_sha256: str,
    result_sha256: str,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_SUCCEEDED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        receipt_sha256=receipt_sha256,
        result_sha256=result_sha256,
        occurred_at=occurred_at,
    )


def build_effect_failed_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str | None,
    failure_phase: EffectFailurePhase,
    failure_code: str,
    retryable: bool,
    occurred_at: str,
    reconciliation_artifact: EventArtifactRef | None = None,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_FAILED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        failure_phase=failure_phase,
        failure_code=failure_code,
        retryable=retryable,
        reconciliation_artifact=reconciliation_artifact,
        reconciliation_sha256=(
            None
            if reconciliation_artifact is None
            else reconciliation_artifact.content_sha256
        ),
        occurred_at=occurred_at,
    )


def build_effect_retry_scheduled_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str | None,
    retry_at: str,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_RETRY_SCHEDULED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        retry_at=retry_at,
        occurred_at=occurred_at,
    )


def build_effect_dead_lettered_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str | None,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_DEAD_LETTERED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        occurred_at=occurred_at,
    )


def build_effect_compensation_requested_draft(
    contract: EffectContract,
    *,
    input_artifact: EventArtifactRef,
    parent: EffectLifecycleProjection,
    occurred_at: str,
    classification: EventClassification = "confidential",
    retention_policy_id: str = "retention_effect_receipt",
) -> EffectEventDraft:
    if type(parent) is not EffectLifecycleProjection or parent.status != "succeeded":
        _fail(
            "TBM_EFFECT_COMPENSATION_INVALID",
            "compensation requires an exact succeeded parent projection",
        )
    if not parent.contract.compensation_supported or parent.receipt_sha256 is None:
        _fail(
            "TBM_EFFECT_COMPENSATION_INVALID",
            "parent effect is not explicitly compensable with a durable receipt",
        )
    if contract.effect_id == parent.effect_id:
        _fail(
            "TBM_EFFECT_COMPENSATION_INVALID",
            "compensation must use a distinct child effect",
        )
    if contract.authorization_event_id == parent.contract.authorization_event_id:
        _fail(
            "TBM_EFFECT_COMPENSATION_INVALID",
            "compensation child requires its own authorization decision",
        )
    return EffectEventDraft(
        event_type=EFFECT_COMPENSATION_REQUESTED,
        contract=contract,
        occurred_at=occurred_at,
        input_artifact=input_artifact,
        parent_effect_id=parent.effect_id,
        parent_success_event_id=parent.terminal_event_id,
        parent_success_event_sha256=parent.terminal_event_sha256,
        parent_receipt_sha256=parent.receipt_sha256,
        classification=classification,
        retention_policy_id=retention_policy_id,
    )


def build_effect_compensated_draft(
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    provider_request_id: str,
    receipt_sha256: str,
    result_sha256: str,
    parent_effect_id: str,
    parent_success_event_id: str,
    parent_success_event_sha256: str,
    parent_receipt_sha256: str,
    occurred_at: str,
) -> EffectEventDraft:
    return _provider_draft(
        EFFECT_COMPENSATED,
        contract,
        provider=provider,
        attempt_number=attempt_number,
        provider_request_id=provider_request_id,
        receipt_sha256=receipt_sha256,
        result_sha256=result_sha256,
        parent_effect_id=parent_effect_id,
        parent_success_event_id=parent_success_event_id,
        parent_success_event_sha256=parent_success_event_sha256,
        parent_receipt_sha256=parent_receipt_sha256,
        occurred_at=occurred_at,
    )


def build_effect_receipt_batch(
    drafts: tuple[EffectEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    prior_stream_events: tuple[CanonicalEvent, ...],
    related_events: tuple[CanonicalEvent, ...] = (),
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= EVENT_LEDGER_MAX_APPEND_BATCH
        or any(type(item) is not EffectEventDraft for item in drafts)
    ):
        _fail(
            "TBM_EFFECT_BATCH_INVALID",
            "drafts must be a bounded non-empty tuple of EffectEventDraft",
        )
    if type(access) is not LedgerAccessContext:
        _fail("TBM_EFFECT_ACCESS_INVALID", "access must be exactly LedgerAccessContext")
    if (
        type(expected_stream_version) is not int
        or not 0 <= expected_stream_version < EFFECT_RECEIPT_MAX_STREAM_EVENTS
    ):
        _fail("TBM_EFFECT_BATCH_INVALID", "expected stream version is invalid")
    if expected_stream_version + len(drafts) > EFFECT_RECEIPT_MAX_STREAM_EVENTS:
        _fail("TBM_EFFECT_BATCH_INVALID", "effect stream exceeds its event bound")
    if (
        type(next_global_position) is not int
        or not 1 <= next_global_position <= _MAX_SEQUENCE
    ):
        _fail("TBM_EFFECT_BATCH_INVALID", "next global position is invalid")
    _event_tuple(prior_stream_events, "prior_stream_events")
    _event_tuple(related_events, "related_events")
    if len(prior_stream_events) != expected_stream_version:
        _fail(
            "TBM_EFFECT_BATCH_INVALID",
            "prior stream history must be complete through the expected version",
        )
    effect_id = drafts[0].contract.effect_id
    contract = drafts[0].contract
    stream_id = effect_receipt_stream_id(effect_id)
    if any(item.contract != contract for item in drafts):
        _fail(
            "TBM_EFFECT_CONTRACT_DRIFT",
            "one append batch must preserve the exact effect contract",
        )
    previous: CanonicalEvent | None = None
    for offset, event in enumerate(prior_stream_events, start=1):
        verify_effect_receipt_event(event)
        if (
            event.stream_id != stream_id
            or event.stream_version != offset
            or cast(Mapping[str, object], event.payload)["effect_id"] != effect_id
        ):
            _fail(
                "TBM_EFFECT_BATCH_INVALID",
                "prior stream history is not the complete effect stream",
            )
        if previous is not None:
            _verify_parent(event, previous)
        previous = event
    canonical_recorded_at = _canonical_timestamp(recorded_at, "recorded_at")
    previous_occurred_at = None if previous is None else previous.occurred_at
    for draft in drafts:
        if draft.contract.authorization_event_id != access.authorization_decision_id:
            _fail(
                "TBM_EFFECT_AUTHORIZATION_MISMATCH",
                "effect authorization must match the trusted ledger access context",
            )
        if (
            previous_occurred_at is not None
            and parse_rfc3339(draft.occurred_at) < parse_rfc3339(previous_occurred_at)
        ):
            _fail(
                "TBM_EFFECT_TIMESTAMP_INVALID",
                "effect occurrence time cannot move backwards",
            )
        if parse_rfc3339(draft.occurred_at) > parse_rfc3339(canonical_recorded_at):
            _fail(
                "TBM_EFFECT_TIMESTAMP_INVALID",
                "effect occurrence time cannot follow ledger recording time",
            )
        previous_occurred_at = draft.occurred_at
    command_value = {
        "protocol_version": EFFECT_RECEIPT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "prior_head_sha256": None if previous is None else previous.event_sha256,
        "next_global_position": next_global_position,
        "recorded_at": canonical_recorded_at,
        "drafts": [item.command_value() for item in drafts],
    }
    command_sha256 = _domain_sha256(
        b"tbm.effect-receipt-command.v1\x00", command_value
    )
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.effect-receipt-command-idempotency.v1\x00", command_value
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    correlation = hashlib.sha256(
        (access.partition.partition_sha256 + "\x00" + effect_id).encode("utf-8")
    ).hexdigest()
    events: list[CanonicalEvent] = []
    parent = previous
    for offset, draft in enumerate(drafts):
        sequence = expected_stream_version + offset + 1
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        event = build_canonical_event(
            event_id="evt_effect_" + event_digest,
            event_type=draft.event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=EFFECT_RECEIPT_STREAM_TYPE,
            stream_version=sequence,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_effect_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_effect_" + correlation[:32],
            causation_id=(
                draft.contract.requested_by_event_id
                if parent is None
                else parent.event_id
            ),
            occurred_at=draft.occurred_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_effect_receipt_adapter",
            producer_version="f3-v1",
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification=draft.classification,
            retention_policy_id=draft.retention_policy_id,
            artifact_refs=draft.artifact_refs,
            payload=draft.payload(sequence=sequence),
        )
        verify_effect_receipt_event(event)
        if parent is not None:
            _verify_parent(event, parent)
        events.append(event)
        parent = event
    combined = tuple(
        sorted(
            (*related_events, *prior_stream_events, *events),
            key=lambda item: item.global_position,
        )
    )
    reduce_effect_receipt_events(combined)
    return tuple(events), idempotency


def build_effect_receipt_append_request(
    drafts: tuple[EffectEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    prior_stream_events: tuple[CanonicalEvent, ...],
    related_events: tuple[CanonicalEvent, ...] = (),
    recorded_at: str,
) -> LedgerAppendRequest:
    events, idempotency = build_effect_receipt_batch(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        prior_stream_events=prior_stream_events,
        related_events=related_events,
        recorded_at=recorded_at,
    )
    return LedgerAppendRequest(
        access=access,
        stream_id=events[0].stream_id,
        expected_stream_version=expected_stream_version,
        events=events,
        idempotency=idempotency,
    )


def append_effect_receipt_batch(
    ledger: EventLedgerPort,
    drafts: tuple[EffectEventDraft, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    recorded_at: str,
) -> LedgerAppendReceipt:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not all(
        callable(getattr(ledger, name, None)) for name in ("append", "read_stream")
    ):
        _fail(
            "TBM_EFFECT_LEDGER_INVALID",
            "append requires an access-bound readable EventLedgerPort",
        )
    if not drafts or type(drafts[0]) is not EffectEventDraft:
        _fail("TBM_EFFECT_BATCH_INVALID", "drafts are invalid")
    stream_id = effect_receipt_stream_id(drafts[0].contract.effect_id)
    page = ledger.read_stream(stream_id, 1, EFFECT_RECEIPT_MAX_STREAM_EVENTS)
    if page.has_more or len(page.events) < expected_stream_version:
        _fail(
            "TBM_EFFECT_HISTORY_INCOMPLETE",
            "effect history cannot be loaded completely through the expected head",
        )
    prior = page.events[:expected_stream_version]
    related: tuple[CanonicalEvent, ...] = ()
    first = drafts[0]
    if first.event_type == EFFECT_COMPENSATION_REQUESTED:
        assert first.parent_effect_id is not None
        parent_page = ledger.read_stream(
            effect_receipt_stream_id(first.parent_effect_id),
            1,
            EFFECT_RECEIPT_MAX_STREAM_EVENTS,
        )
        if parent_page.has_more or not parent_page.events:
            _fail(
                "TBM_EFFECT_COMPENSATION_INVALID",
                "parent effect history is unavailable or incomplete",
            )
        related = parent_page.events
    request = build_effect_receipt_append_request(
        drafts,
        access=access,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        prior_stream_events=prior,
        related_events=related,
        recorded_at=recorded_at,
    )
    receipt = ledger.append(
        request.stream_id,
        request.expected_stream_version,
        request.events,
        request.idempotency,
    )
    verify_ledger_append_receipt(request, receipt)
    return receipt


def verify_effect_receipt_event(event: CanonicalEvent) -> None:
    if type(event) is not CanonicalEvent:
        _fail("TBM_EFFECT_EVENT_INVALID", "event must be exactly CanonicalEvent")
    if (
        event.event_type not in EFFECT_RECEIPT_EVENT_TYPES
        or event.event_version != 1
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != EFFECT_RECEIPT_STREAM_TYPE
        or event.occurred_at is None
    ):
        _fail(
            "TBM_EFFECT_EVENT_INVALID",
            "canonical event is not a native effect receipt v1 event",
        )
    try:
        payload = build_effect_receipt_registry().consume(event).payload
    except EventRegistryV1Error as error:
        raise EffectReceiptV1Error(
            "TBM_EFFECT_PAYLOAD_INVALID",
            "effect payload does not match its sealed event type",
        ) from error
    contract = _contract_from_payload(cast(Mapping[str, object], payload["contract"]))
    if (
        payload["effect_id"] != contract.effect_id
        or event.stream_id != effect_receipt_stream_id(contract.effect_id)
        or payload["sequence"] != event.stream_version
        or payload["occurred_at"] != event.occurred_at
        or payload["point"] != event.event_type.removeprefix("tbm.effect.")
        or contract.authorization_event_id != event.authorization_decision_id
    ):
        _fail(
            "TBM_EFFECT_EVENT_INVALID",
            "effect envelope, stream, authorization, and payload are not exactly bound",
        )
    draft = _draft_from_event(event, contract, payload)
    if draft.artifact_refs != event.artifact_refs:
        _fail(
            "TBM_EFFECT_ARTIFACT_MISMATCH",
            "effect artifact descriptors do not exactly match the typed payload",
        )


def reduce_effect_receipt_events(
    events: tuple[CanonicalEvent, ...],
) -> tuple[EffectLifecycleProjection, ...]:
    _event_tuple(events, "events")
    if len(events) > 10_000:
        _fail("TBM_EFFECT_REDUCER_BOUNDS", "effect reducer input exceeds its bound")
    positions = tuple(event.global_position for event in events)
    if positions != tuple(sorted(set(positions))):
        _fail(
            "TBM_EFFECT_REDUCER_ORDER_INVALID",
            "events must use strictly increasing unique global positions",
        )
    states: dict[str, _State] = {}
    idempotency_owners: dict[str, str] = {}
    provider_requests: dict[tuple[str, str], tuple[str, str, str]] = {}
    partition: tuple[str, str, str, str] | None = None
    stream_heads: dict[str, CanonicalEvent] = {}
    for event in events:
        verify_effect_receipt_event(event)
        current_partition = (
            event.organization_id,
            event.tenant_id,
            event.repository_id,
            event.environment_id,
        )
        if partition is None:
            partition = current_partition
        elif partition != current_partition:
            _fail(
                "TBM_EFFECT_REDUCER_SCOPE_INVALID",
                "one reduction cannot mix tenant partitions",
            )
        previous = stream_heads.get(event.stream_id)
        if previous is not None:
            _verify_parent(event, previous)
        elif event.stream_version != 1:
            _fail(
                "TBM_EFFECT_REDUCER_HISTORY_INCOMPLETE",
                "effect stream must begin at version one",
            )
        stream_heads[event.stream_id] = event
        payload = cast(Mapping[str, object], event.payload)
        contract = _contract_from_payload(cast(Mapping[str, object], payload["contract"]))
        draft = _draft_from_event(event, contract, payload)
        state = states.get(contract.effect_id)
        if state is None:
            _apply_first_event(
                states,
                idempotency_owners,
                event,
                draft,
            )
        else:
            _apply_transition(state, event, draft, provider_requests)
        state = states[contract.effect_id]
        state.last_event_id = event.event_id
        state.last_event_sha256 = event.event_sha256
        state.last_global_position = event.global_position
        state.sequence = event.stream_version
        if event.event_type in {EFFECT_SUCCEEDED, EFFECT_DEAD_LETTERED, EFFECT_COMPENSATED}:
            state.terminal_event_id = event.event_id
            state.terminal_event_sha256 = event.event_sha256
        if event.event_type == EFFECT_COMPENSATION_REQUESTED:
            _verify_compensation_parent(state, states)
        if draft.provider is not None and draft.provider_request_id is not None:
            assert draft.provider is not None
            assert draft.provider_request_id is not None
            assert draft.attempt_id is not None
            assert draft.canonical_request_sha256 is not None
            key = (draft.provider.provider_id, draft.provider_request_id)
            binding = (
                contract.effect_id,
                draft.attempt_id,
                draft.canonical_request_sha256,
            )
            prior_binding = provider_requests.get(key)
            if prior_binding is not None and prior_binding != binding:
                _fail(
                    "TBM_EFFECT_PROVIDER_REQUEST_CONFLICT",
                    "provider request ID is already bound to another exact request",
                )
            provider_requests[key] = binding
    return tuple(_freeze_projection(states[key]) for key in sorted(states))


def effect_projection(
    events: tuple[CanonicalEvent, ...], effect_id: str
) -> EffectLifecycleProjection:
    _identifier(effect_id, "effect_id")
    for projection in reduce_effect_receipt_events(events):
        if projection.effect_id == effect_id:
            return projection
    _fail("TBM_EFFECT_NOT_FOUND", "effect projection is absent")


def build_effect_receipt_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    for event_type in EFFECT_RECEIPT_EVENT_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="domain",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=_payload_json_schema(event_type),
            )
        )
    return registry.seal()


def effect_receipt_payload_dispatch_schema() -> dict[str, object]:
    schema = build_effect_receipt_registry().dispatch_schema()
    schema["$id"] = EFFECT_RECEIPT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory external effect receipt payloads v1"
    schema["$comment"] = (
        "Generated from the sealed effect receipt registry. Runtime transition, "
        "authorization, provider-request, receipt, and artifact checks remain authoritative."
    )
    return schema


def dumps_effect_receipt_payload_dispatch_schema() -> str:
    return json.dumps(
        effect_receipt_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _provider_draft(
    event_type: str,
    contract: EffectContract,
    *,
    provider: TrustedEffectProvider,
    attempt_number: int,
    occurred_at: str,
    provider_request_id: str | None = None,
    receipt_artifact: EventArtifactRef | None = None,
    receipt_sha256: str | None = None,
    result_sha256: str | None = None,
    unknown_reason: EffectUnknownReason | None = None,
    failure_phase: EffectFailurePhase | None = None,
    failure_code: str | None = None,
    retryable: bool | None = None,
    retry_at: str | None = None,
    reconciliation_artifact: EventArtifactRef | None = None,
    reconciliation_sha256: str | None = None,
    parent_effect_id: str | None = None,
    parent_success_event_id: str | None = None,
    parent_success_event_sha256: str | None = None,
    parent_receipt_sha256: str | None = None,
) -> EffectEventDraft:
    return EffectEventDraft(
        event_type=event_type,
        contract=contract,
        occurred_at=occurred_at,
        attempt_number=attempt_number,
        attempt_id=effect_attempt_id(contract.effect_id, attempt_number),
        provider=provider,
        provider_request_id=provider_request_id,
        canonical_request_sha256=effect_provider_request_sha256(
            contract, provider, attempt_number
        ),
        receipt_artifact=receipt_artifact,
        receipt_sha256=receipt_sha256,
        result_sha256=result_sha256,
        unknown_reason=unknown_reason,
        failure_phase=failure_phase,
        failure_code=failure_code,
        retryable=retryable,
        retry_at=retry_at,
        reconciliation_artifact=reconciliation_artifact,
        reconciliation_sha256=reconciliation_sha256,
        parent_effect_id=parent_effect_id,
        parent_success_event_id=parent_success_event_id,
        parent_success_event_sha256=parent_success_event_sha256,
        parent_receipt_sha256=parent_receipt_sha256,
    )


def _validate_optional_fields(draft: EffectEventDraft) -> None:
    if draft.input_artifact is not None:
        _artifact(draft.input_artifact, "input_artifact")
    if draft.receipt_artifact is not None:
        _artifact(draft.receipt_artifact, "receipt_artifact")
    if draft.reconciliation_artifact is not None:
        _artifact(draft.reconciliation_artifact, "reconciliation_artifact")
    if draft.attempt_number is not None:
        _attempt_number(draft.attempt_number)
    if draft.attempt_id is not None:
        _identifier(draft.attempt_id, "attempt_id")
    if draft.provider is not None and type(draft.provider) is not TrustedEffectProvider:
        _fail(
            "TBM_EFFECT_PROVIDER_INVALID",
            "provider must be exactly TrustedEffectProvider",
        )
    if draft.provider_request_id is not None:
        _provider_request_id(draft.provider_request_id)
    for name in (
        "canonical_request_sha256",
        "receipt_sha256",
        "result_sha256",
        "parent_success_event_sha256",
        "parent_receipt_sha256",
        "reconciliation_sha256",
    ):
        value = getattr(draft, name)
        if value is not None:
            _digest(value, name)
    if draft.unknown_reason is not None and draft.unknown_reason not in _UNKNOWN_REASONS:
        _fail("TBM_EFFECT_UNKNOWN_INVALID", "unknown_reason is not supported")
    if draft.failure_phase is not None and draft.failure_phase not in _FAILURE_PHASES:
        _fail("TBM_EFFECT_FAILURE_INVALID", "failure_phase is not supported")
    if draft.failure_code is not None:
        _code(draft.failure_code, "failure_code")
    if draft.retryable is not None and type(draft.retryable) is not bool:
        _fail("TBM_EFFECT_FAILURE_INVALID", "retryable must be a boolean or null")
    if draft.retry_at is not None:
        _canonical_timestamp(draft.retry_at, "retry_at")
    if draft.parent_effect_id is not None:
        _identifier(draft.parent_effect_id, "parent_effect_id")
    if draft.parent_success_event_id is not None:
        _event_id(draft.parent_success_event_id, "parent_success_event_id")


def _validate_draft_shape(draft: EffectEventDraft) -> None:
    provider_types = {
        EFFECT_STARTED,
        EFFECT_PROVIDER_REQUEST_RECORDED,
        EFFECT_RECEIPT_RECORDED,
        EFFECT_SUCCEEDED,
        EFFECT_RESULT_UNKNOWN,
        EFFECT_FAILED,
        EFFECT_RETRY_SCHEDULED,
        EFFECT_DEAD_LETTERED,
        EFFECT_COMPENSATED,
    }
    if draft.event_type in provider_types:
        if (
            draft.provider is None
            or draft.attempt_number is None
            or draft.attempt_id
            != effect_attempt_id(draft.contract.effect_id, draft.attempt_number)
            or draft.canonical_request_sha256
            != effect_provider_request_sha256(
                draft.contract, draft.provider, draft.attempt_number
            )
        ):
            _fail(
                "TBM_EFFECT_ATTEMPT_INVALID",
                "provider event must bind the deterministic attempt and request",
            )
    elif any(
        value is not None
        for value in (
            draft.attempt_number,
            draft.attempt_id,
            draft.provider,
            draft.provider_request_id,
            draft.canonical_request_sha256,
        )
    ):
        _fail(
            "TBM_EFFECT_ATTEMPT_INVALID",
            "non-attempt event cannot contain provider attempt fields",
        )
    request_types = {EFFECT_REQUESTED, EFFECT_COMPENSATION_REQUESTED}
    if draft.event_type in request_types:
        if (
            draft.input_artifact is None
            or draft.input_artifact.artifact_id != draft.contract.input_artifact_id
            or draft.input_artifact.content_sha256
            != draft.contract.input_artifact_sha256
            or draft.input_artifact.availability != "available"
        ):
            _fail(
                "TBM_EFFECT_INPUT_ARTIFACT_INVALID",
                "effect request must exactly bind an available input artifact descriptor",
            )
    elif draft.input_artifact is not None:
        _fail(
            "TBM_EFFECT_INPUT_ARTIFACT_INVALID",
            "only request events carry the input artifact descriptor",
        )
    if draft.event_type == EFFECT_RECEIPT_RECORDED:
        if (
            draft.provider_request_id is None
            or draft.receipt_artifact is None
            or draft.receipt_sha256 != draft.receipt_artifact.content_sha256
            or draft.receipt_artifact.availability != "available"
            or draft.result_sha256 is None
        ):
            _fail(
                "TBM_EFFECT_RECEIPT_INVALID",
                "receipt event must bind provider request, result, and available receipt artifact",
            )
    elif draft.receipt_artifact is not None:
        _fail(
            "TBM_EFFECT_RECEIPT_INVALID",
            "only receipt-recorded events carry receipt artifact descriptors",
        )
    if draft.event_type in {EFFECT_SUCCEEDED, EFFECT_COMPENSATED}:
        if (
            draft.provider_request_id is None
            or draft.receipt_sha256 is None
            or draft.result_sha256 is None
        ):
            _fail(
                "TBM_EFFECT_RECEIPT_INVALID",
                "terminal success must bind the exact provider receipt and result",
            )
    elif draft.event_type != EFFECT_RECEIPT_RECORDED and (
        draft.receipt_sha256 is not None or draft.result_sha256 is not None
    ):
        _fail(
            "TBM_EFFECT_RECEIPT_INVALID",
            "non-receipt event cannot claim receipt or result digests",
        )
    required_provider_id = {
        EFFECT_PROVIDER_REQUEST_RECORDED,
        EFFECT_RECEIPT_RECORDED,
        EFFECT_SUCCEEDED,
        EFFECT_COMPENSATED,
    }
    if draft.event_type in required_provider_id and draft.provider_request_id is None:
        _fail(
            "TBM_EFFECT_PROVIDER_REQUEST_INVALID",
            "event requires a provider request ID",
        )
    if draft.event_type == EFFECT_RESULT_UNKNOWN:
        if draft.unknown_reason is None:
            _fail("TBM_EFFECT_UNKNOWN_INVALID", "unknown result requires a reason")
    elif draft.unknown_reason is not None:
        _fail("TBM_EFFECT_UNKNOWN_INVALID", "only unknown result carries a reason")
    if draft.event_type == EFFECT_FAILED:
        if (
            draft.failure_phase is None
            or draft.failure_code is None
            or draft.retryable is None
        ):
            _fail(
                "TBM_EFFECT_FAILURE_INVALID",
                "known failure requires phase, code, and retryability",
            )
        if draft.failure_phase == "pre_send" and draft.provider_request_id is not None:
            _fail(
                "TBM_EFFECT_FAILURE_INVALID",
                "pre-send failure cannot claim a provider request ID",
            )
        if draft.failure_phase == "provider_rejected" and draft.provider_request_id is None:
            _fail(
                "TBM_EFFECT_FAILURE_INVALID",
                "provider rejection requires a provider request ID",
            )
        if draft.failure_phase == "reconciled_absent":
            if (
                draft.reconciliation_artifact is None
                or draft.reconciliation_sha256
                != draft.reconciliation_artifact.content_sha256
                or draft.reconciliation_artifact.availability != "available"
            ):
                _fail(
                    "TBM_EFFECT_RECONCILIATION_INVALID",
                    "reconciled absence requires an exact available reconciliation Artifact",
                )
        elif (
            draft.reconciliation_artifact is not None
            or draft.reconciliation_sha256 is not None
        ):
            _fail(
                "TBM_EFFECT_RECONCILIATION_INVALID",
                "only reconciled absence may carry reconciliation evidence",
            )
    elif any(
        value is not None
        for value in (draft.failure_phase, draft.failure_code, draft.retryable)
    ):
        _fail("TBM_EFFECT_FAILURE_INVALID", "only failure events carry failure fields")
    elif (
        draft.reconciliation_artifact is not None
        or draft.reconciliation_sha256 is not None
    ):
        _fail(
            "TBM_EFFECT_RECONCILIATION_INVALID",
            "only reconciled failure may carry reconciliation evidence",
        )
    if draft.event_type == EFFECT_RETRY_SCHEDULED:
        if draft.retry_at is None or parse_rfc3339(draft.retry_at) < parse_rfc3339(
            draft.occurred_at
        ):
            _fail(
                "TBM_EFFECT_RETRY_INVALID",
                "retry_at must be present and not precede the scheduling event",
            )
    elif draft.retry_at is not None:
        _fail("TBM_EFFECT_RETRY_INVALID", "only retry events carry retry_at")
    parent_fields = (
        draft.parent_effect_id,
        draft.parent_success_event_id,
        draft.parent_success_event_sha256,
        draft.parent_receipt_sha256,
    )
    if draft.event_type in {EFFECT_COMPENSATION_REQUESTED, EFFECT_COMPENSATED}:
        if any(value is None for value in parent_fields):
            _fail(
                "TBM_EFFECT_COMPENSATION_INVALID",
                "compensation must bind the exact parent success and receipt",
            )
        if draft.parent_effect_id == draft.contract.effect_id:
            _fail(
                "TBM_EFFECT_COMPENSATION_INVALID",
                "compensation must be a distinct child effect",
            )
    elif any(value is not None for value in parent_fields):
        _fail(
            "TBM_EFFECT_COMPENSATION_INVALID",
            "non-compensation event cannot carry parent effect fields",
        )
    for artifact in draft.artifact_refs:
        if _CLASSIFICATION_RANK[artifact.classification] > _CLASSIFICATION_RANK[
            draft.classification
        ]:
            _fail(
                "TBM_EFFECT_CLASSIFICATION_INVALID",
                "event classification cannot be lower than an artifact descriptor",
            )


def _apply_first_event(
    states: dict[str, _State],
    idempotency_owners: dict[str, str],
    event: CanonicalEvent,
    draft: EffectEventDraft,
) -> None:
    if event.stream_version != 1 or draft.event_type not in {
        EFFECT_REQUESTED,
        EFFECT_COMPENSATION_REQUESTED,
    }:
        _fail(
            "TBM_EFFECT_TRANSITION_INVALID",
            "effect stream must begin with a request",
        )
    owner = idempotency_owners.get(draft.contract.idempotency_key_sha256)
    if owner is not None and owner != draft.contract.effect_id:
        _fail(
            "TBM_EFFECT_IDEMPOTENCY_CONFLICT",
            "effect idempotency key is already bound to another effect",
        )
    idempotency_owners[draft.contract.idempotency_key_sha256] = draft.contract.effect_id
    states[draft.contract.effect_id] = _State(
        contract=draft.contract,
        status="requested",
        sequence=event.stream_version,
        request_event_id=event.event_id,
        request_event_sha256=event.event_sha256,
        parent_effect_id=draft.parent_effect_id,
        parent_success_event_id=draft.parent_success_event_id,
        parent_success_event_sha256=draft.parent_success_event_sha256,
        parent_receipt_sha256=draft.parent_receipt_sha256,
    )


def _apply_transition(
    state: _State,
    event: CanonicalEvent,
    draft: EffectEventDraft,
    provider_requests: dict[tuple[str, str], tuple[str, str, str]],
) -> None:
    if event.stream_version != state.sequence + 1 or draft.contract != state.contract:
        _fail(
            "TBM_EFFECT_CONTRACT_DRIFT",
            "effect transitions must be contiguous and preserve the exact contract",
        )
    if state.status in {"succeeded", "dead_lettered", "compensated"}:
        _fail(
            "TBM_EFFECT_TRANSITION_INVALID",
            "terminal effect cannot accept another transition",
        )
    event_type = draft.event_type
    if event_type == EFFECT_AUTHORIZED:
        _require_status(state, {"requested"}, event_type)
        state.status = "authorized"
        return
    if event_type == EFFECT_STARTED:
        _require_status(state, {"authorized", "retry_wait"}, event_type)
        assert draft.attempt_number is not None
        if (
            draft.attempt_number != state.attempt_count + 1
            or draft.attempt_number > state.contract.max_attempts
        ):
            _fail(
                "TBM_EFFECT_ATTEMPT_INVALID",
                "attempt number must be monotonic and within the retry budget",
            )
        state.attempt_count = draft.attempt_number
        _bind_attempt(state, draft)
        state.provider_request_id = None
        state.receipt_sha256 = None
        state.result_sha256 = None
        state.unknown_reason = None
        state.failure_phase = None
        state.failure_code = None
        state.retryable = None
        state.retry_at = None
        state.reconciliation_sha256 = None
        state.status = "executing"
        return
    if event_type == EFFECT_PROVIDER_REQUEST_RECORDED:
        _require_status(state, {"executing"}, event_type)
        _verify_attempt_binding(state, draft)
        assert draft.provider is not None
        assert draft.provider_request_id is not None
        assert draft.attempt_id is not None
        assert draft.canonical_request_sha256 is not None
        binding = (
            state.contract.effect_id,
            draft.attempt_id,
            draft.canonical_request_sha256,
        )
        prior = provider_requests.get((draft.provider.provider_id, draft.provider_request_id))
        if prior is not None and prior != binding:
            _fail(
                "TBM_EFFECT_PROVIDER_REQUEST_CONFLICT",
                "provider request ID conflicts with a prior exact request",
            )
        state.provider_request_id = draft.provider_request_id
        state.status = "provider_confirmed"
        return
    if event_type == EFFECT_RESULT_UNKNOWN:
        _require_status(state, {"executing", "provider_confirmed"}, event_type)
        _verify_attempt_binding(state, draft)
        if state.provider_request_id is not None and (
            draft.provider_request_id != state.provider_request_id
        ):
            _fail(
                "TBM_EFFECT_PROVIDER_REQUEST_CONFLICT",
                "unknown result changed the provider request ID",
            )
        state.provider_request_id = draft.provider_request_id
        state.unknown_reason = draft.unknown_reason
        state.status = "unknown"
        return
    if event_type == EFFECT_RECEIPT_RECORDED:
        _require_status(state, {"provider_confirmed", "unknown"}, event_type)
        _verify_attempt_binding(state, draft)
        if state.provider_request_id is not None and (
            draft.provider_request_id != state.provider_request_id
        ):
            _fail(
                "TBM_EFFECT_RECEIPT_MISMATCH",
                "receipt changed the provider request ID",
            )
        state.provider_request_id = draft.provider_request_id
        state.receipt_sha256 = draft.receipt_sha256
        state.result_sha256 = draft.result_sha256
        state.unknown_reason = None
        state.status = "receipt_recorded"
        return
    if event_type in {EFFECT_SUCCEEDED, EFFECT_COMPENSATED}:
        _require_status(state, {"receipt_recorded"}, event_type)
        _verify_attempt_binding(state, draft)
        if (
            draft.provider_request_id != state.provider_request_id
            or draft.receipt_sha256 != state.receipt_sha256
            or draft.result_sha256 != state.result_sha256
        ):
            _fail(
                "TBM_EFFECT_RECEIPT_MISMATCH",
                "terminal success does not bind the exact recorded receipt",
            )
        is_compensation = state.parent_effect_id is not None
        if (event_type == EFFECT_COMPENSATED) != is_compensation:
            _fail(
                "TBM_EFFECT_COMPENSATION_INVALID",
                "terminal event does not match normal versus compensation effect kind",
            )
        if is_compensation and (
            draft.parent_effect_id != state.parent_effect_id
            or draft.parent_success_event_id != state.parent_success_event_id
            or draft.parent_success_event_sha256 != state.parent_success_event_sha256
            or draft.parent_receipt_sha256 != state.parent_receipt_sha256
        ):
            _fail(
                "TBM_EFFECT_COMPENSATION_INVALID",
                "compensation completion changed its exact parent binding",
            )
        state.status = "compensated" if is_compensation else "succeeded"
        return
    if event_type == EFFECT_FAILED:
        _require_status(state, {"executing", "provider_confirmed", "unknown"}, event_type)
        _verify_attempt_binding(state, draft)
        if state.status == "executing" and draft.failure_phase != "pre_send":
            _fail(
                "TBM_EFFECT_FAILURE_INVALID",
                "failure before provider confirmation must be explicitly pre-send",
            )
        if state.status == "provider_confirmed" and (
            draft.failure_phase != "provider_rejected"
            or draft.provider_request_id != state.provider_request_id
        ):
            _fail(
                "TBM_EFFECT_FAILURE_INVALID",
                "known provider failure must bind its rejection request",
            )
        if state.status == "unknown":
            if draft.failure_phase != "reconciled_absent":
                _fail(
                    "TBM_EFFECT_UNKNOWN_INVALID",
                    "unknown result may resolve to failure only through reconciliation",
                )
            if draft.provider_request_id != state.provider_request_id:
                _fail(
                    "TBM_EFFECT_PROVIDER_REQUEST_CONFLICT",
                    "reconciliation cannot invent or change a provider request ID",
                )
        if state.provider_request_id is not None and (
            draft.provider_request_id != state.provider_request_id
        ):
            _fail(
                "TBM_EFFECT_PROVIDER_REQUEST_CONFLICT",
                "failure changed the provider request ID",
            )
        state.failure_phase = draft.failure_phase
        state.failure_code = draft.failure_code
        state.retryable = draft.retryable
        state.reconciliation_sha256 = draft.reconciliation_sha256
        state.unknown_reason = None
        state.status = "failed"
        return
    if event_type == EFFECT_RETRY_SCHEDULED:
        _require_status(state, {"failed"}, event_type)
        _verify_attempt_binding(state, draft)
        if not state.retryable or state.attempt_count >= state.contract.max_attempts:
            _fail(
                "TBM_EFFECT_RETRY_INVALID",
                "retry requires retryable known failure and remaining budget",
            )
        if draft.provider_request_id != state.provider_request_id:
            _fail(
                "TBM_EFFECT_PROVIDER_REQUEST_CONFLICT",
                "retry scheduling changed the prior provider request ID",
            )
        state.retry_at = draft.retry_at
        state.status = "retry_wait"
        return
    if event_type == EFFECT_DEAD_LETTERED:
        _require_status(state, {"failed"}, event_type)
        _verify_attempt_binding(state, draft)
        if draft.provider_request_id != state.provider_request_id:
            _fail(
                "TBM_EFFECT_PROVIDER_REQUEST_CONFLICT",
                "dead-letter transition changed the provider request ID",
            )
        state.status = "dead_lettered"
        return
    _fail(
        "TBM_EFFECT_TRANSITION_INVALID",
        "event type is not valid after the effect request",
    )


def _verify_compensation_parent(
    state: _State, states: Mapping[str, _State]
) -> None:
    assert state.parent_effect_id is not None
    parent = states.get(state.parent_effect_id)
    if (
        parent is None
        or parent.status != "succeeded"
        or not parent.contract.compensation_supported
        or state.parent_success_event_id != parent.terminal_event_id
        or state.parent_success_event_sha256 != parent.terminal_event_sha256
        or state.parent_receipt_sha256 != parent.receipt_sha256
    ):
        _fail(
            "TBM_EFFECT_COMPENSATION_INVALID",
            "compensation request does not bind an exact compensable success",
        )


def _bind_attempt(state: _State, draft: EffectEventDraft) -> None:
    state.current_attempt_id = draft.attempt_id
    state.provider = draft.provider
    state.canonical_request_sha256 = draft.canonical_request_sha256


def _verify_attempt_binding(state: _State, draft: EffectEventDraft) -> None:
    if (
        draft.attempt_number != state.attempt_count
        or draft.attempt_id != state.current_attempt_id
        or draft.provider != state.provider
        or draft.canonical_request_sha256 != state.canonical_request_sha256
    ):
        _fail(
            "TBM_EFFECT_ATTEMPT_INVALID",
            "transition changed the exact attempt or trusted provider binding",
        )


def _require_status(
    state: _State, allowed: set[str], event_type: str
) -> None:
    if state.status not in allowed:
        _fail(
            "TBM_EFFECT_TRANSITION_INVALID",
            f"{event_type} is invalid from the current effect status",
        )


def _freeze_projection(state: _State) -> EffectLifecycleProjection:
    unsigned = {
        "protocol_version": EFFECT_RECEIPT_PROTOCOL_VERSION,
        "effect_id": state.contract.effect_id,
        "effect_type": state.contract.effect_type,
        "status": state.status,
        "contract": state.contract.to_dict(),
        "sequence": state.sequence,
        "request_event_id": state.request_event_id,
        "request_event_sha256": state.request_event_sha256,
        "attempt_count": state.attempt_count,
        "current_attempt_id": state.current_attempt_id,
        "provider": None if state.provider is None else state.provider.to_dict(),
        "provider_request_id": state.provider_request_id,
        "canonical_request_sha256": state.canonical_request_sha256,
        "receipt_sha256": state.receipt_sha256,
        "result_sha256": state.result_sha256,
        "unknown_reason": state.unknown_reason,
        "failure_phase": state.failure_phase,
        "failure_code": state.failure_code,
        "retryable": state.retryable,
        "retry_at": state.retry_at,
        "reconciliation_sha256": state.reconciliation_sha256,
        "parent_effect_id": state.parent_effect_id,
        "parent_success_event_id": state.parent_success_event_id,
        "parent_success_event_sha256": state.parent_success_event_sha256,
        "parent_receipt_sha256": state.parent_receipt_sha256,
        "terminal_event_id": state.terminal_event_id,
        "terminal_event_sha256": state.terminal_event_sha256,
        "last_event_id": state.last_event_id,
        "last_event_sha256": state.last_event_sha256,
        "last_global_position": state.last_global_position,
    }
    return EffectLifecycleProjection(
        effect_id=state.contract.effect_id,
        effect_type=state.contract.effect_type,
        status=state.status,
        contract=state.contract,
        sequence=state.sequence,
        request_event_id=state.request_event_id,
        request_event_sha256=state.request_event_sha256,
        attempt_count=state.attempt_count,
        current_attempt_id=state.current_attempt_id,
        provider=state.provider,
        provider_request_id=state.provider_request_id,
        canonical_request_sha256=state.canonical_request_sha256,
        receipt_sha256=state.receipt_sha256,
        result_sha256=state.result_sha256,
        unknown_reason=state.unknown_reason,
        failure_phase=state.failure_phase,
        failure_code=state.failure_code,
        retryable=state.retryable,
        retry_at=state.retry_at,
        reconciliation_sha256=state.reconciliation_sha256,
        parent_effect_id=state.parent_effect_id,
        parent_success_event_id=state.parent_success_event_id,
        parent_success_event_sha256=state.parent_success_event_sha256,
        parent_receipt_sha256=state.parent_receipt_sha256,
        terminal_event_id=state.terminal_event_id,
        terminal_event_sha256=state.terminal_event_sha256,
        last_event_id=state.last_event_id,
        last_event_sha256=state.last_event_sha256,
        last_global_position=state.last_global_position,
        projection_sha256=_domain_sha256(
            b"tbm.effect-lifecycle-projection.v1\x00", unsigned
        ),
    )


def _draft_from_event(
    event: CanonicalEvent,
    contract: EffectContract,
    payload: Mapping[str, object],
) -> EffectEventDraft:
    artifact_by_id = {item.artifact_id: item for item in event.artifact_refs}
    input_artifact = (
        artifact_by_id.get(contract.input_artifact_id)
        if event.event_type in {EFFECT_REQUESTED, EFFECT_COMPENSATION_REQUESTED}
        else None
    )
    receipt_artifact_id = cast(str | None, payload["receipt_artifact_id"])
    receipt_artifact = (
        None
        if receipt_artifact_id is None
        else artifact_by_id.get(receipt_artifact_id)
    )
    reconciliation_artifact_id = cast(
        str | None, payload["reconciliation_artifact_id"]
    )
    reconciliation_artifact = (
        None
        if reconciliation_artifact_id is None
        else artifact_by_id.get(reconciliation_artifact_id)
    )
    provider_value = payload["provider"]
    provider = (
        None
        if provider_value is None
        else _provider_from_payload(cast(Mapping[str, object], provider_value))
    )
    return EffectEventDraft(
        event_type=event.event_type,
        contract=contract,
        occurred_at=cast(str, payload["occurred_at"]),
        input_artifact=input_artifact,
        attempt_number=cast(int | None, payload["attempt_number"]),
        attempt_id=cast(str | None, payload["attempt_id"]),
        provider=provider,
        provider_request_id=cast(str | None, payload["provider_request_id"]),
        canonical_request_sha256=cast(
            str | None, payload["canonical_request_sha256"]
        ),
        receipt_artifact=receipt_artifact,
        receipt_sha256=cast(str | None, payload["receipt_sha256"]),
        result_sha256=cast(str | None, payload["result_sha256"]),
        unknown_reason=cast(EffectUnknownReason | None, payload["unknown_reason"]),
        failure_phase=cast(EffectFailurePhase | None, payload["failure_phase"]),
        failure_code=cast(str | None, payload["failure_code"]),
        retryable=cast(bool | None, payload["retryable"]),
        retry_at=cast(str | None, payload["retry_at"]),
        reconciliation_artifact=reconciliation_artifact,
        reconciliation_sha256=cast(
            str | None, payload["reconciliation_sha256"]
        ),
        parent_effect_id=cast(str | None, payload["parent_effect_id"]),
        parent_success_event_id=cast(
            str | None, payload["parent_success_event_id"]
        ),
        parent_success_event_sha256=cast(
            str | None, payload["parent_success_event_sha256"]
        ),
        parent_receipt_sha256=cast(
            str | None, payload["parent_receipt_sha256"]
        ),
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
    )


def _contract_from_payload(value: Mapping[str, object]) -> EffectContract:
    contract = EffectContract(
        effect_id=cast(str, value["effect_id"]),
        effect_type=cast(str, value["effect_type"]),
        idempotency_key_sha256=cast(str, value["idempotency_key_sha256"]),
        requested_by_event_id=cast(str, value["requested_by_event_id"]),
        input_artifact_sha256=cast(str, value["input_artifact_sha256"]),
        authorization_event_id=cast(str, value["authorization_event_id"]),
        compensation_supported=cast(bool, value["compensation_supported"]),
        max_attempts=cast(int, value["max_attempts"]),
    )
    if value["input_artifact_id"] != contract.input_artifact_id:
        _fail(
            "TBM_EFFECT_INPUT_ARTIFACT_INVALID",
            "input artifact ID does not match its content digest",
        )
    return contract


def _provider_from_payload(value: Mapping[str, object]) -> TrustedEffectProvider:
    return TrustedEffectProvider(
        provider_id=cast(str, value["provider_id"]),
        registration_sha256=cast(str, value["registration_sha256"]),
        adapter_id=cast(str, value["adapter_id"]),
        adapter_version=cast(str, value["adapter_version"]),
    )


def _payload_json_schema(event_type: str) -> dict[str, object]:
    fields = [
        "protocol_version",
        "point",
        "effect_id",
        "sequence",
        "occurred_at",
        "contract",
        "attempt_number",
        "attempt_id",
        "provider",
        "provider_request_id",
        "canonical_request_sha256",
        "receipt_artifact_id",
        "receipt_sha256",
        "result_sha256",
        "unknown_reason",
        "failure_phase",
        "failure_code",
        "retryable",
        "retry_at",
        "reconciliation_artifact_id",
        "reconciliation_sha256",
        "parent_effect_id",
        "parent_success_event_id",
        "parent_success_event_sha256",
        "parent_receipt_sha256",
    ]
    digest_or_null = {
        "oneOf": [
            {"type": "string", "pattern": _DIGEST_RE.pattern},
            {"type": "null"},
        ]
    }
    identifier_or_null = {
        "oneOf": [
            {"type": "string", "pattern": _IDENTIFIER_RE.pattern},
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": fields,
        "properties": {
            "protocol_version": {"const": EFFECT_RECEIPT_PROTOCOL_VERSION},
            "point": {"const": event_type.removeprefix("tbm.effect.")},
            "effect_id": {"type": "string", "pattern": _IDENTIFIER_RE.pattern},
            "sequence": {
                "type": "integer",
                "minimum": 1,
                "maximum": EFFECT_RECEIPT_MAX_STREAM_EVENTS,
            },
            "occurred_at": {"type": "string", "pattern": _TIMESTAMP_PATTERN},
            "contract": _contract_json_schema(),
            "attempt_number": {
                "oneOf": [
                    {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": EFFECT_RECEIPT_MAX_ATTEMPTS,
                    },
                    {"type": "null"},
                ]
            },
            "attempt_id": identifier_or_null,
            "provider": {
                "oneOf": [_provider_json_schema(), {"type": "null"}]
            },
            "provider_request_id": {
                "oneOf": [
                    {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                        "pattern": _PROVIDER_REQUEST_ID_RE.pattern,
                    },
                    {"type": "null"},
                ]
            },
            "canonical_request_sha256": digest_or_null,
            "receipt_artifact_id": {
                "oneOf": [
                    {"type": "string", "pattern": _ARTIFACT_ID_RE.pattern},
                    {"type": "null"},
                ]
            },
            "receipt_sha256": digest_or_null,
            "result_sha256": digest_or_null,
            "unknown_reason": {
                "oneOf": [
                    {"enum": sorted(_UNKNOWN_REASONS)},
                    {"type": "null"},
                ]
            },
            "failure_phase": {
                "oneOf": [
                    {"enum": sorted(_FAILURE_PHASES)},
                    {"type": "null"},
                ]
            },
            "failure_code": {
                "oneOf": [
                    {"type": "string", "pattern": _CODE_RE.pattern},
                    {"type": "null"},
                ]
            },
            "retryable": {
                "oneOf": [{"type": "boolean"}, {"type": "null"}]
            },
            "retry_at": {
                "oneOf": [
                    {"type": "string", "pattern": _TIMESTAMP_PATTERN},
                    {"type": "null"},
                ]
            },
            "reconciliation_artifact_id": {
                "oneOf": [
                    {"type": "string", "pattern": _ARTIFACT_ID_RE.pattern},
                    {"type": "null"},
                ]
            },
            "reconciliation_sha256": digest_or_null,
            "parent_effect_id": identifier_or_null,
            "parent_success_event_id": {
                "oneOf": [
                    {"type": "string", "pattern": r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$"},
                    {"type": "null"},
                ]
            },
            "parent_success_event_sha256": digest_or_null,
            "parent_receipt_sha256": digest_or_null,
        },
    }


def _contract_json_schema() -> dict[str, object]:
    fields = [
        "effect_id",
        "effect_type",
        "idempotency_key_sha256",
        "requested_by_event_id",
        "input_artifact_id",
        "input_artifact_sha256",
        "authorization_event_id",
        "compensation_supported",
        "max_attempts",
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": fields,
        "properties": {
            "effect_id": {"type": "string", "pattern": _IDENTIFIER_RE.pattern},
            "effect_type": {"type": "string", "pattern": _CODE_RE.pattern},
            "idempotency_key_sha256": {"type": "string", "pattern": _DIGEST_RE.pattern},
            "requested_by_event_id": {
                "type": "string",
                "pattern": r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$",
            },
            "input_artifact_id": {"type": "string", "pattern": _ARTIFACT_ID_RE.pattern},
            "input_artifact_sha256": {"type": "string", "pattern": _DIGEST_RE.pattern},
            "authorization_event_id": {
                "type": "string",
                "pattern": _IDENTIFIER_RE.pattern,
            },
            "compensation_supported": {"type": "boolean"},
            "max_attempts": {
                "type": "integer",
                "minimum": 1,
                "maximum": EFFECT_RECEIPT_MAX_ATTEMPTS,
            },
        },
    }


def _provider_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "provider_id",
            "registration_sha256",
            "adapter_id",
            "adapter_version",
        ],
        "properties": {
            "provider_id": {"type": "string", "pattern": _IDENTIFIER_RE.pattern},
            "registration_sha256": {"type": "string", "pattern": _DIGEST_RE.pattern},
            "adapter_id": {"type": "string", "pattern": _IDENTIFIER_RE.pattern},
            "adapter_version": {"type": "string", "pattern": _CODE_RE.pattern},
        },
    }


def _event_tuple(value: object, name: str) -> None:
    if (
        type(value) is not tuple
        or len(value) > 10_000
        or any(type(item) is not CanonicalEvent for item in value)
    ):
        _fail("TBM_EFFECT_REDUCER_BOUNDS", f"{name} must be a bounded event tuple")


def _verify_parent(event: CanonicalEvent, previous: CanonicalEvent) -> None:
    try:
        verify_event_parent(event, previous)
    except V3ContractError as error:
        raise EffectReceiptV1Error(
            "TBM_EFFECT_PARENT_INVALID",
            "effect stream parent chain is invalid",
        ) from error


def _artifact(value: object, name: str) -> None:
    if type(value) is not EventArtifactRef:
        _fail("TBM_EFFECT_ARTIFACT_INVALID", f"{name} must be EventArtifactRef")


def _attempt_number(value: object) -> None:
    if type(value) is not int or not 1 <= value <= EFFECT_RECEIPT_MAX_ATTEMPTS:
        _fail("TBM_EFFECT_ATTEMPT_INVALID", "attempt number is outside its bound")


def _provider_request_id(value: object) -> None:
    if type(value) is not str or _PROVIDER_REQUEST_ID_RE.fullmatch(value) is None:
        _fail(
            "TBM_EFFECT_PROVIDER_REQUEST_INVALID",
            "provider_request_id must be bounded printable ASCII",
        )


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("TBM_EFFECT_IDENTIFIER_INVALID", f"{name} must be a bounded identifier")


def _event_id(value: object, name: str) -> None:
    if (
        type(value) is not str
        or re.fullmatch(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$", value) is None
    ):
        _fail("TBM_EFFECT_IDENTIFIER_INVALID", f"{name} must be a canonical event ID")


def _code(value: object, name: str) -> None:
    if type(value) is not str or _CODE_RE.fullmatch(value) is None:
        _fail("TBM_EFFECT_CODE_INVALID", f"{name} must be a bounded code")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail("TBM_EFFECT_DIGEST_INVALID", f"{name} must be a canonical sha256 digest")


def _canonical_timestamp(value: object, name: str) -> str:
    if type(value) is not str:
        _fail("TBM_EFFECT_TIMESTAMP_INVALID", f"{name} must be a canonical timestamp")
    try:
        canonical = canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise EffectReceiptV1Error(
            "TBM_EFFECT_TIMESTAMP_INVALID",
            f"{name} must be a canonical timestamp",
        ) from error
    if canonical != value:
        _fail("TBM_EFFECT_TIMESTAMP_INVALID", f"{name} must use canonical UTC spelling")
    return canonical


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
        raise EffectReceiptV1Error(
            "TBM_EFFECT_NON_CANONICAL_JSON",
            "effect descriptor is not canonical JSON",
        ) from error
    return "sha256:" + hashlib.sha256(domain + encoded).hexdigest()


def _fail(code: str, message: str) -> NoReturn:
    raise EffectReceiptV1Error(code, message)


__all__ = [
    "EFFECT_AUTHORIZED",
    "EFFECT_COMPENSATED",
    "EFFECT_COMPENSATION_REQUESTED",
    "EFFECT_DEAD_LETTERED",
    "EFFECT_FAILED",
    "EFFECT_PROVIDER_REQUEST_RECORDED",
    "EFFECT_RECEIPT_EVENT_TYPES",
    "EFFECT_RECEIPT_MAX_ATTEMPTS",
    "EFFECT_RECEIPT_MAX_STREAM_EVENTS",
    "EFFECT_RECEIPT_PAYLOAD_SCHEMA_ID",
    "EFFECT_RECEIPT_PROTOCOL_VERSION",
    "EFFECT_RECEIPT_RECORDED",
    "EFFECT_RECEIPT_STREAM_TYPE",
    "EFFECT_REQUESTED",
    "EFFECT_RESULT_UNKNOWN",
    "EFFECT_RETRY_SCHEDULED",
    "EFFECT_STARTED",
    "EFFECT_SUCCEEDED",
    "EffectContract",
    "EffectEventDraft",
    "EffectFailurePhase",
    "EffectLifecycleProjection",
    "EffectLifecycleStatus",
    "EffectReceiptV1Error",
    "EffectUnknownReason",
    "TrustedEffectProvider",
    "append_effect_receipt_batch",
    "build_effect_authorized_draft",
    "build_effect_compensated_draft",
    "build_effect_compensation_requested_draft",
    "build_effect_dead_lettered_draft",
    "build_effect_failed_draft",
    "build_effect_provider_request_recorded_draft",
    "build_effect_receipt_append_request",
    "build_effect_receipt_batch",
    "build_effect_receipt_recorded_draft",
    "build_effect_receipt_registry",
    "build_effect_requested_draft",
    "build_effect_result_unknown_draft",
    "build_effect_retry_scheduled_draft",
    "build_effect_started_draft",
    "build_effect_succeeded_draft",
    "dumps_effect_receipt_payload_dispatch_schema",
    "effect_attempt_id",
    "effect_idempotency_key_sha256",
    "effect_projection",
    "effect_provider_request_sha256",
    "effect_receipt_payload_dispatch_schema",
    "effect_receipt_stream_id",
    "reduce_effect_receipt_events",
    "verify_effect_receipt_event",
]
