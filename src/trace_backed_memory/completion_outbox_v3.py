from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import re
from typing import Literal, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import (
    aware_datetime_to_rfc3339,
    canonical_rfc3339,
    parse_rfc3339,
)
from .contracts_v3 import canonical_sha256
from .gate_session_v3 import GateSession
from .outcome_v3 import RunOutcome, verify_run_outcome


COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION = (
    "tbm.completion-outbox-event.v3"
)
COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION = (
    "tbm.completion-outbox-delivery.v3"
)
COMPLETION_OUTBOX_JSON_MAX_BYTES = 1024 * 1024
COMPLETION_OUTBOX_JSON_MAX_DEPTH = 24
COMPLETION_OUTBOX_JSON_MAX_NODES = 4096
COMPLETION_OUTBOX_MAX_ATTEMPTS = 1000
COMPLETION_OUTBOX_MAX_LEASE_SECONDS = 86_400
COMPLETION_OUTBOX_MAX_RETRY_SECONDS = 604_800

_IDENTIFIER_MAX_CHARS = 128
_ERROR_CODE_MAX_CHARS = 256
_EVENT_ID_RE = re.compile(
    r"^completion_outbox_event_sha256_[0-9a-f]{64}$"
)
_DELIVERY_ID_RE = re.compile(
    r"^completion_outbox_delivery_sha256_[0-9a-f]{64}$"
)
_RUN_OUTCOME_ID_RE = re.compile(r"^run_outcome_sha256_[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_TYPES = frozenset({"execution_completed"})
_DELIVERY_STATUSES = frozenset(
    {"pending", "leased", "retry_wait", "delivered", "dead_letter"}
)
_EVENT_FIELDS = frozenset(
    {
        "contract_version",
        "event_id",
        "event_type",
        "tenant_id",
        "repository_id",
        "session_id",
        "trace_id",
        "run_id",
        "usage_decision_id",
        "run_outcome_id",
        "outcome_descriptor_sha256",
        "occurred_at",
    }
)
_DELIVERY_FIELDS = frozenset(
    {
        "contract_version",
        "delivery_revision_id",
        "event_id",
        "version",
        "status",
        "attempt_count",
        "updated_at",
        "available_at",
        "worker_id",
        "lease_expires_at",
        "delivered_at",
        "last_error_code",
        "response_sha256",
    }
)

CompletionOutboxEventType = Literal["execution_completed"]
CompletionOutboxDeliveryStatus = Literal[
    "pending",
    "leased",
    "retry_wait",
    "delivered",
    "dead_letter",
]


class CompletionOutboxContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise CompletionOutboxContractError(
        "TBM_COMPLETION_OUTBOX_INVALID",
        message,
    )


@dataclass(frozen=True)
class CompletionOutboxEvent:
    """Immutable completion notification keyed by canonical content."""

    event_id: str
    event_type: CompletionOutboxEventType
    tenant_id: str
    repository_id: str
    session_id: str
    trace_id: str
    run_id: str
    usage_decision_id: str
    run_outcome_id: str
    outcome_descriptor_sha256: str
    occurred_at: str
    contract_version: str = COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION:
            _invalid(
                "contract_version must be "
                f"{COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION}"
            )
        if (
            type(self.event_id) is not str
            or _EVENT_ID_RE.fullmatch(self.event_id) is None
        ):
            _invalid("event_id must be a canonical content identifier")
        if (
            type(self.event_type) is not str
            or self.event_type not in _EVENT_TYPES
        ):
            _invalid("event_type is not supported")
        for name in (
            "tenant_id",
            "repository_id",
            "session_id",
            "trace_id",
            "run_id",
            "usage_decision_id",
        ):
            _identifier(getattr(self, name), name)
        if (
            type(self.run_outcome_id) is not str
            or _RUN_OUTCOME_ID_RE.fullmatch(self.run_outcome_id) is None
        ):
            _invalid("run_outcome_id must be a canonical content identifier")
        _digest(
            self.outcome_descriptor_sha256,
            "outcome_descriptor_sha256",
        )
        _timestamp(self.occurred_at, "occurred_at")
        if self.event_id != completion_outbox_event_id(
            self.to_dict(include_id=False)
        ):
            _invalid("event_id does not match the canonical payload")

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "contract_version": self.contract_version,
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "usage_decision_id": self.usage_decision_id,
            "run_outcome_id": self.run_outcome_id,
            "outcome_descriptor_sha256": self.outcome_descriptor_sha256,
            "occurred_at": self.occurred_at,
        }
        if include_id:
            value["event_id"] = self.event_id
        return value


@dataclass(frozen=True)
class CompletionOutboxDelivery:
    """One immutable revision of durable delivery state."""

    delivery_revision_id: str
    event_id: str
    version: int
    status: CompletionOutboxDeliveryStatus
    attempt_count: int
    updated_at: str
    available_at: str | None = None
    worker_id: str | None = None
    lease_expires_at: str | None = None
    delivered_at: str | None = None
    last_error_code: str | None = None
    response_sha256: str | None = None
    contract_version: str = COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION
        ):
            _invalid(
                "contract_version must be "
                f"{COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION}"
            )
        if (
            type(self.delivery_revision_id) is not str
            or _DELIVERY_ID_RE.fullmatch(self.delivery_revision_id) is None
        ):
            _invalid(
                "delivery_revision_id must be a canonical content identifier"
            )
        if (
            type(self.event_id) is not str
            or _EVENT_ID_RE.fullmatch(self.event_id) is None
        ):
            _invalid("event_id must be a canonical content identifier")
        if type(self.version) is not int or self.version < 1:
            _invalid("version must be a positive integer")
        if (
            type(self.status) is not str
            or self.status not in _DELIVERY_STATUSES
        ):
            _invalid("status is not supported")
        if (
            type(self.attempt_count) is not int
            or not 0 <= self.attempt_count <= COMPLETION_OUTBOX_MAX_ATTEMPTS
        ):
            _invalid("attempt_count is outside the supported range")
        updated = _timestamp(self.updated_at, "updated_at")
        available = _optional_timestamp(self.available_at, "available_at")
        lease = _optional_timestamp(
            self.lease_expires_at,
            "lease_expires_at",
        )
        delivered = _optional_timestamp(self.delivered_at, "delivered_at")
        if self.worker_id is not None:
            _identifier(self.worker_id, "worker_id")
        _optional_error_code(self.last_error_code)
        _optional_digest(self.response_sha256, "response_sha256")

        if self.status == "pending":
            if self.version != 1 or self.attempt_count != 0:
                _invalid("pending is only valid for the initial revision")
            if available is None:
                _invalid("pending delivery requires available_at")
            self._require_absent_delivery_fields()
        elif self.status == "leased":
            if self.attempt_count < 1:
                _invalid("leased delivery requires an attempt")
            if self.worker_id is None or lease is None:
                _invalid("leased delivery requires worker_id and lease")
            if lease <= updated:
                _invalid("lease_expires_at must follow updated_at")
            if any(
                value is not None
                for value in (
                    self.available_at,
                    self.delivered_at,
                    self.last_error_code,
                    self.response_sha256,
                )
            ):
                _invalid("leased delivery has invalid terminal fields")
        elif self.status == "retry_wait":
            if self.attempt_count < 1:
                _invalid("retry_wait delivery requires an attempt")
            if available is None or available <= updated:
                _invalid("retry_wait requires future available_at")
            if self.last_error_code is None:
                _invalid("retry_wait requires last_error_code")
            if any(
                value is not None
                for value in (
                    self.worker_id,
                    self.lease_expires_at,
                    self.delivered_at,
                    self.response_sha256,
                )
            ):
                _invalid("retry_wait delivery has invalid lease fields")
        elif self.status == "delivered":
            if self.attempt_count < 1 or delivered is None:
                _invalid("delivered state requires an attempt and timestamp")
            if delivered != updated:
                _invalid("delivered_at must equal updated_at")
            if any(
                value is not None
                for value in (
                    self.available_at,
                    self.worker_id,
                    self.lease_expires_at,
                    self.last_error_code,
                )
            ):
                _invalid("delivered state has invalid retry fields")
        else:
            if self.attempt_count < 1 or self.last_error_code is None:
                _invalid("dead_letter requires an attempt and error code")
            if any(
                value is not None
                for value in (
                    self.available_at,
                    self.worker_id,
                    self.lease_expires_at,
                    self.delivered_at,
                    self.response_sha256,
                )
            ):
                _invalid("dead_letter state has invalid delivery fields")

        if self.delivery_revision_id != completion_outbox_delivery_id(
            self.to_dict(include_id=False)
        ):
            _invalid(
                "delivery_revision_id does not match the canonical payload"
            )

    def _require_absent_delivery_fields(self) -> None:
        if any(
            value is not None
            for value in (
                self.worker_id,
                self.lease_expires_at,
                self.delivered_at,
                self.last_error_code,
                self.response_sha256,
            )
        ):
            _invalid(f"{self.status} state has invalid delivery fields")

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "version": self.version,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "updated_at": self.updated_at,
            "available_at": self.available_at,
            "worker_id": self.worker_id,
            "lease_expires_at": self.lease_expires_at,
            "delivered_at": self.delivered_at,
            "last_error_code": self.last_error_code,
            "response_sha256": self.response_sha256,
        }
        if include_id:
            value["delivery_revision_id"] = self.delivery_revision_id
        return value


def completion_outbox_event_id(payload: Mapping[str, object]) -> str:
    return "completion_outbox_event_sha256_" + canonical_sha256(
        payload
    ).removeprefix("sha256:")


def completion_outbox_delivery_id(payload: Mapping[str, object]) -> str:
    return "completion_outbox_delivery_sha256_" + canonical_sha256(
        payload
    ).removeprefix("sha256:")


def build_completion_outbox_event(
    outcome: RunOutcome,
    session: GateSession,
) -> CompletionOutboxEvent:
    if type(outcome) is not RunOutcome:
        raise TypeError("outcome must be exactly RunOutcome")
    if type(session) is not GateSession:
        raise TypeError("session must be exactly GateSession")
    try:
        verify_run_outcome(outcome, session)
    except ValueError as error:
        raise CompletionOutboxContractError(
            "TBM_COMPLETION_OUTBOX_LINKAGE",
            "completion outbox event linkage is invalid",
        ) from error
    payload: dict[str, object] = {
        "contract_version": COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION,
        "event_type": "execution_completed",
        "tenant_id": session.tenant_id,
        "repository_id": session.repository_id,
        "session_id": session.session_id,
        "trace_id": outcome.trace_id,
        "run_id": outcome.run_id,
        "usage_decision_id": outcome.usage_decision_id,
        "run_outcome_id": outcome.run_outcome_id,
        "outcome_descriptor_sha256": canonical_sha256(outcome.to_dict()),
        "occurred_at": outcome.measured_at,
    }
    return CompletionOutboxEvent(
        event_id=completion_outbox_event_id(payload),
        event_type="execution_completed",
        tenant_id=session.tenant_id,
        repository_id=session.repository_id,
        session_id=session.session_id,
        trace_id=outcome.trace_id,
        run_id=outcome.run_id,
        usage_decision_id=outcome.usage_decision_id,
        run_outcome_id=outcome.run_outcome_id,
        outcome_descriptor_sha256=cast(
            str,
            payload["outcome_descriptor_sha256"],
        ),
        occurred_at=outcome.measured_at,
    )


def verify_completion_outbox_event(
    event: CompletionOutboxEvent,
    outcome: RunOutcome,
    session: GateSession,
) -> None:
    expected = build_completion_outbox_event(outcome, session)
    if event != expected:
        raise CompletionOutboxContractError(
            "TBM_COMPLETION_OUTBOX_LINKAGE",
            "completion outbox event does not match outcome and session",
        )


def build_initial_completion_outbox_delivery(
    event: CompletionOutboxEvent,
) -> CompletionOutboxDelivery:
    if type(event) is not CompletionOutboxEvent:
        raise TypeError("event must be exactly CompletionOutboxEvent")
    return _build_delivery(
        event_id=event.event_id,
        version=1,
        status="pending",
        attempt_count=0,
        updated_at=event.occurred_at,
        available_at=event.occurred_at,
    )


def claim_completion_outbox_delivery(
    current: CompletionOutboxDelivery,
    *,
    worker_id: str,
    claimed_at: str,
    lease_seconds: int,
) -> CompletionOutboxDelivery:
    if type(current) is not CompletionOutboxDelivery:
        raise TypeError("current must be exactly CompletionOutboxDelivery")
    _validate_completion_outbox_claim(worker_id, lease_seconds)
    claimed = _timestamp(claimed_at, "claimed_at")
    if current.status in {"pending", "retry_wait"}:
        if current.available_at is None or parse_rfc3339(
            current.available_at
        ) > claimed:
            _invalid("delivery is not available for claim")
    elif current.status == "leased":
        if current.lease_expires_at is None or parse_rfc3339(
            current.lease_expires_at
        ) > claimed:
            _invalid("delivery lease has not expired")
    else:
        _invalid("terminal delivery cannot be claimed")
    if claimed < parse_rfc3339(current.updated_at):
        _invalid("claimed_at precedes current delivery state")
    if current.attempt_count >= COMPLETION_OUTBOX_MAX_ATTEMPTS:
        _invalid("delivery attempt limit has been reached")
    lease_expires_at = _timestamp_plus_seconds(
        claimed,
        lease_seconds,
        "lease_expires_at",
    )
    result = _build_delivery(
        event_id=current.event_id,
        version=current.version + 1,
        status="leased",
        attempt_count=current.attempt_count + 1,
        updated_at=canonical_rfc3339(claimed_at),
        worker_id=worker_id,
        lease_expires_at=lease_expires_at,
    )
    verify_completion_outbox_delivery_transition(current, result)
    return result


def acknowledge_completion_outbox_delivery(
    current: CompletionOutboxDelivery,
    *,
    worker_id: str,
    acknowledged_at: str,
    response_sha256: str | None = None,
) -> CompletionOutboxDelivery:
    _require_active_lease(
        current,
        worker_id=worker_id,
        occurred_at=acknowledged_at,
    )
    _optional_digest(response_sha256, "response_sha256")
    result = _build_delivery(
        event_id=current.event_id,
        version=current.version + 1,
        status="delivered",
        attempt_count=current.attempt_count,
        updated_at=canonical_rfc3339(acknowledged_at),
        delivered_at=canonical_rfc3339(acknowledged_at),
        response_sha256=response_sha256,
    )
    verify_completion_outbox_delivery_transition(current, result)
    return result


def fail_completion_outbox_delivery(
    current: CompletionOutboxDelivery,
    *,
    worker_id: str,
    failed_at: str,
    error_code: str,
    retry_delay_seconds: int,
    max_attempts: int,
) -> CompletionOutboxDelivery:
    failed = _require_active_lease(
        current,
        worker_id=worker_id,
        occurred_at=failed_at,
    )
    _error_code(error_code)
    if (
        type(retry_delay_seconds) is not int
        or not 1 <= retry_delay_seconds <= COMPLETION_OUTBOX_MAX_RETRY_SECONDS
    ):
        _invalid("retry_delay_seconds is outside the supported range")
    if (
        type(max_attempts) is not int
        or not 1 <= max_attempts <= COMPLETION_OUTBOX_MAX_ATTEMPTS
    ):
        _invalid("max_attempts is outside the supported range")
    if current.attempt_count >= max_attempts:
        result = _build_delivery(
            event_id=current.event_id,
            version=current.version + 1,
            status="dead_letter",
            attempt_count=current.attempt_count,
            updated_at=canonical_rfc3339(failed_at),
            last_error_code=error_code,
        )
    else:
        result = _build_delivery(
            event_id=current.event_id,
            version=current.version + 1,
            status="retry_wait",
            attempt_count=current.attempt_count,
            updated_at=canonical_rfc3339(failed_at),
            available_at=_timestamp_plus_seconds(
                failed,
                retry_delay_seconds,
                "available_at",
            ),
            last_error_code=error_code,
        )
    verify_completion_outbox_delivery_transition(current, result)
    return result


def verify_completion_outbox_delivery_transition(
    previous: CompletionOutboxDelivery,
    current: CompletionOutboxDelivery,
) -> None:
    if previous.event_id != current.event_id:
        _invalid("delivery transition cannot change event_id")
    if current.version != previous.version + 1:
        _invalid("delivery transition must advance exactly one version")
    if parse_rfc3339(current.updated_at) < parse_rfc3339(
        previous.updated_at
    ):
        _invalid("delivery transition timestamp moved backwards")
    allowed = {
        "pending": frozenset({"leased"}),
        "retry_wait": frozenset({"leased"}),
        "leased": frozenset(
            {"leased", "retry_wait", "delivered", "dead_letter"}
        ),
        "delivered": frozenset(),
        "dead_letter": frozenset(),
    }
    if current.status not in allowed[previous.status]:
        _invalid("delivery transition is not allowed")
    expected_attempts = previous.attempt_count + (
        1 if current.status == "leased" else 0
    )
    if current.attempt_count != expected_attempts:
        _invalid("delivery attempt_count does not match the transition")


def dumps_completion_outbox_event(event: CompletionOutboxEvent) -> str:
    return _dumps(event.to_dict())


def dumps_completion_outbox_delivery(
    delivery: CompletionOutboxDelivery,
) -> str:
    return _dumps(delivery.to_dict())


def loads_completion_outbox_event(
    data: str | bytes | bytearray,
) -> CompletionOutboxEvent:
    return parse_completion_outbox_event(_loads_object(data))


def loads_completion_outbox_delivery(
    data: str | bytes | bytearray,
) -> CompletionOutboxDelivery:
    return parse_completion_outbox_delivery(_loads_object(data))


def parse_completion_outbox_event(
    value: Mapping[str, object],
) -> CompletionOutboxEvent:
    obj = _strict_object(value, _EVENT_FIELDS, "CompletionOutboxEvent")
    return CompletionOutboxEvent(
        event_id=_as_str(obj["event_id"], "event_id"),
        event_type=cast(CompletionOutboxEventType, obj["event_type"]),
        tenant_id=_as_str(obj["tenant_id"], "tenant_id"),
        repository_id=_as_str(obj["repository_id"], "repository_id"),
        session_id=_as_str(obj["session_id"], "session_id"),
        trace_id=_as_str(obj["trace_id"], "trace_id"),
        run_id=_as_str(obj["run_id"], "run_id"),
        usage_decision_id=_as_str(
            obj["usage_decision_id"],
            "usage_decision_id",
        ),
        run_outcome_id=_as_str(obj["run_outcome_id"], "run_outcome_id"),
        outcome_descriptor_sha256=_as_str(
            obj["outcome_descriptor_sha256"],
            "outcome_descriptor_sha256",
        ),
        occurred_at=_as_str(obj["occurred_at"], "occurred_at"),
        contract_version=_as_str(
            obj["contract_version"],
            "contract_version",
        ),
    )


def parse_completion_outbox_delivery(
    value: Mapping[str, object],
) -> CompletionOutboxDelivery:
    obj = _strict_object(
        value,
        _DELIVERY_FIELDS,
        "CompletionOutboxDelivery",
    )
    return CompletionOutboxDelivery(
        delivery_revision_id=_as_str(
            obj["delivery_revision_id"],
            "delivery_revision_id",
        ),
        event_id=_as_str(obj["event_id"], "event_id"),
        version=_as_int(obj["version"], "version"),
        status=cast(CompletionOutboxDeliveryStatus, obj["status"]),
        attempt_count=_as_int(obj["attempt_count"], "attempt_count"),
        updated_at=_as_str(obj["updated_at"], "updated_at"),
        available_at=_as_optional_str(
            obj["available_at"],
            "available_at",
        ),
        worker_id=_as_optional_str(obj["worker_id"], "worker_id"),
        lease_expires_at=_as_optional_str(
            obj["lease_expires_at"],
            "lease_expires_at",
        ),
        delivered_at=_as_optional_str(
            obj["delivered_at"],
            "delivered_at",
        ),
        last_error_code=_as_optional_str(
            obj["last_error_code"],
            "last_error_code",
        ),
        response_sha256=_as_optional_str(
            obj["response_sha256"],
            "response_sha256",
        ),
        contract_version=_as_str(
            obj["contract_version"],
            "contract_version",
        ),
    )


def _build_delivery(
    *,
    event_id: str,
    version: int,
    status: CompletionOutboxDeliveryStatus,
    attempt_count: int,
    updated_at: str,
    available_at: str | None = None,
    worker_id: str | None = None,
    lease_expires_at: str | None = None,
    delivered_at: str | None = None,
    last_error_code: str | None = None,
    response_sha256: str | None = None,
) -> CompletionOutboxDelivery:
    payload: dict[str, object] = {
        "contract_version": COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION,
        "event_id": event_id,
        "version": version,
        "status": status,
        "attempt_count": attempt_count,
        "updated_at": canonical_rfc3339(updated_at),
        "available_at": (
            None if available_at is None else canonical_rfc3339(available_at)
        ),
        "worker_id": worker_id,
        "lease_expires_at": (
            None
            if lease_expires_at is None
            else canonical_rfc3339(lease_expires_at)
        ),
        "delivered_at": (
            None
            if delivered_at is None
            else canonical_rfc3339(delivered_at)
        ),
        "last_error_code": last_error_code,
        "response_sha256": response_sha256,
    }
    return CompletionOutboxDelivery(
        delivery_revision_id=completion_outbox_delivery_id(payload),
        event_id=event_id,
        version=version,
        status=status,
        attempt_count=attempt_count,
        updated_at=cast(str, payload["updated_at"]),
        available_at=cast(str | None, payload["available_at"]),
        worker_id=worker_id,
        lease_expires_at=cast(str | None, payload["lease_expires_at"]),
        delivered_at=cast(str | None, payload["delivered_at"]),
        last_error_code=last_error_code,
        response_sha256=response_sha256,
    )


def _require_active_lease(
    current: CompletionOutboxDelivery,
    *,
    worker_id: str,
    occurred_at: str,
):
    if type(current) is not CompletionOutboxDelivery:
        raise TypeError("current must be exactly CompletionOutboxDelivery")
    _identifier(worker_id, "worker_id")
    occurred = _timestamp(occurred_at, "occurred_at")
    if current.status != "leased":
        _invalid("delivery is not leased")
    if current.worker_id != worker_id:
        _invalid("worker_id does not own the delivery lease")
    if (
        current.lease_expires_at is None
        or occurred > parse_rfc3339(current.lease_expires_at)
    ):
        _invalid("delivery lease has expired")
    if occurred < parse_rfc3339(current.updated_at):
        _invalid("delivery timestamp moved backwards")
    return occurred


def _dumps(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads_object(data: str | bytes | bytearray) -> Mapping[str, object]:
    try:
        if isinstance(data, (bytes, bytearray)):
            text = decode_bounded_utf8(
                bytes(data),
                max_bytes=COMPLETION_OUTBOX_JSON_MAX_BYTES,
                description="completion outbox JSON",
            )
        elif type(data) is str:
            text = data
            if (
                len(text.encode("utf-8"))
                > COMPLETION_OUTBOX_JSON_MAX_BYTES
            ):
                raise ValueError("completion outbox JSON exceeds byte limit")
        else:
            raise TypeError(
                "completion outbox JSON must be str, bytes, or bytearray"
            )
        value = parse_bounded_json(
            text,
            description="completion outbox",
            max_depth=COMPLETION_OUTBOX_JSON_MAX_DEPTH,
            max_nodes=COMPLETION_OUTBOX_JSON_MAX_NODES,
        )
    except (TypeError, ValueError) as error:
        raise CompletionOutboxContractError(
            "TBM_COMPLETION_OUTBOX_INVALID_JSON",
            str(error),
        ) from error
    if not isinstance(value, Mapping):
        _invalid("completion outbox JSON must be an object")
    return cast(Mapping[str, object], value)


def _strict_object(
    value: Mapping[str, object],
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{label} must be an object")
    obj = dict(value)
    if set(obj) != fields:
        _invalid(f"{label} fields do not match the contract")
    if any(type(key) is not str for key in obj):
        _invalid(f"{label} keys must be strings")
    return obj


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _IDENTIFIER_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _invalid(f"{name} must be a bounded identifier")


def _error_code(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _ERROR_CODE_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _invalid("error_code must be a bounded identifier")


def _optional_error_code(value: object) -> None:
    if value is not None:
        _error_code(value)


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{name} must be a canonical sha256 digest")


def _optional_digest(value: object, name: str) -> None:
    if value is not None:
        _digest(value, name)


def _timestamp(value: object, name: str):
    if type(value) is not str:
        _invalid(f"{name} must be an RFC3339 timestamp")
    try:
        parsed = parse_rfc3339(value)
    except (TypeError, ValueError) as error:
        _invalid(f"{name} must be an RFC3339 timestamp: {error}")
    if canonical_rfc3339(value) != value:
        _invalid(f"{name} must be canonical RFC3339")
    return parsed


def _optional_timestamp(value: object, name: str):
    if value is None:
        return None
    return _timestamp(value, name)


def _timestamp_plus_seconds(
    value: datetime,
    seconds: int,
    name: str,
) -> str:
    try:
        shifted = value + timedelta(seconds=seconds)
    except OverflowError:
        _invalid(f"{name} exceeds the supported timestamp range")
    return aware_datetime_to_rfc3339(shifted)


def _validate_completion_outbox_claim(
    worker_id: object,
    lease_seconds: object,
) -> None:
    _identifier(worker_id, "worker_id")
    if (
        type(lease_seconds) is not int
        or not 1 <= lease_seconds <= COMPLETION_OUTBOX_MAX_LEASE_SECONDS
    ):
        _invalid("lease_seconds is outside the supported range")


def _as_str(value: object, name: str) -> str:
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return value


def _as_optional_str(value: object, name: str) -> str | None:
    if value is not None and type(value) is not str:
        _invalid(f"{name} must be a string or null")
    return cast(str | None, value)


def _as_int(value: object, name: str) -> int:
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    return value


__all__ = [
    "COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION",
    "COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION",
    "COMPLETION_OUTBOX_JSON_MAX_BYTES",
    "COMPLETION_OUTBOX_MAX_ATTEMPTS",
    "COMPLETION_OUTBOX_MAX_LEASE_SECONDS",
    "COMPLETION_OUTBOX_MAX_RETRY_SECONDS",
    "CompletionOutboxContractError",
    "CompletionOutboxDelivery",
    "CompletionOutboxDeliveryStatus",
    "CompletionOutboxEvent",
    "CompletionOutboxEventType",
    "acknowledge_completion_outbox_delivery",
    "build_completion_outbox_event",
    "build_initial_completion_outbox_delivery",
    "claim_completion_outbox_delivery",
    "completion_outbox_delivery_id",
    "completion_outbox_event_id",
    "dumps_completion_outbox_delivery",
    "dumps_completion_outbox_event",
    "fail_completion_outbox_delivery",
    "loads_completion_outbox_delivery",
    "loads_completion_outbox_event",
    "parse_completion_outbox_delivery",
    "parse_completion_outbox_event",
    "verify_completion_outbox_delivery_transition",
    "verify_completion_outbox_event",
]
