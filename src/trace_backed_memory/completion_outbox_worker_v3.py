from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import re
from typing import Literal, NoReturn, Protocol

from .completion_outbox_v3 import (
    COMPLETION_OUTBOX_MAX_ATTEMPTS,
    COMPLETION_OUTBOX_MAX_LEASE_SECONDS,
    COMPLETION_OUTBOX_MAX_RETRY_SECONDS,
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    verify_completion_outbox_delivery_transition,
)
from .contracts_v3 import V3ContractError
from ._timestamps import parse_rfc3339


COMPLETION_OUTBOX_WORKER_MAX_PAGE_SIZE = 1000

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_CODE_MAX_CHARS = 256

CompletionOutboxWorkerOutcome = Literal[
    "delivered",
    "retry_wait",
    "dead_letter",
    "superseded",
    "recovery_required",
]


class CompletionOutboxWorkerError(V3ContractError):
    """Stable failure while dispatching durable completion events."""


class CompletionOutboxConsumerError(RuntimeError):
    """Sanitized consumer failure that is safe to persist in delivery state."""

    def __init__(self, error_code: str) -> None:
        _validate_error_code(error_code)
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class CompletionOutboxConsumerReceipt:
    """Bounded consumer response retained with a successful acknowledgement."""

    response_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            self.response_sha256 is not None
            and (
                type(self.response_sha256) is not str
                or _DIGEST_RE.fullmatch(self.response_sha256) is None
            )
        ):
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_INVALID",
                "completion outbox consumer receipt is invalid",
            )


class CompletionOutboxClaimRecord(Protocol):
    event: CompletionOutboxEvent
    delivery: CompletionOutboxDelivery


class CompletionOutboxDeliveryRepository(Protocol):
    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int = 100,
    ) -> tuple[CompletionOutboxClaimRecord, ...]: ...

    def get_delivery(self, event_id: str) -> CompletionOutboxDelivery: ...

    def acknowledge(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        response_sha256: str | None = None,
    ) -> CompletionOutboxDelivery: ...

    def fail_delivery(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ) -> CompletionOutboxDelivery: ...


@dataclass(frozen=True)
class CompletionOutboxWorkerResult:
    event_id: str
    observed_version: int
    attempt_count: int
    outcome: CompletionOutboxWorkerOutcome
    current: CompletionOutboxDelivery


class CompletionOutboxDeliveryWorker:
    """Run one bounded at-least-once delivery pass over durable claims."""

    def __init__(
        self,
        repository: CompletionOutboxDeliveryRepository,
        consumer: Callable[
            [CompletionOutboxEvent],
            CompletionOutboxConsumerReceipt,
        ],
    ) -> None:
        if not callable(consumer):
            raise TypeError("consumer must be callable")
        self._repository = repository
        self._consumer = consumer

    def run_once(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        limit: int = 100,
        retry_delay_seconds: int = 60,
        max_attempts: int = 5,
    ) -> tuple[CompletionOutboxWorkerResult, ...]:
        _validate_run_configuration(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            limit=limit,
            retry_delay_seconds=retry_delay_seconds,
            max_attempts=max_attempts,
        )
        try:
            claims = self._repository.claim_due(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                limit=limit,
            )
        except Exception as error:
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_CLAIM_FAILED",
                "completion outbox due claims could not be acquired",
            ) from error
        validated = self._validate_claims(
            claims,
            worker_id=worker_id,
            limit=limit,
        )
        return tuple(
            self._dispatch(
                event,
                delivery,
                worker_id=worker_id,
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            )
            for event, delivery in validated
        )

    @staticmethod
    def _validate_claims(
        claims: object,
        *,
        worker_id: str,
        limit: int,
    ) -> tuple[
        tuple[CompletionOutboxEvent, CompletionOutboxDelivery],
        ...,
    ]:
        if type(claims) is not tuple or len(claims) > limit:
            _invalid_claims()
        validated: list[
            tuple[CompletionOutboxEvent, CompletionOutboxDelivery]
        ] = []
        seen: set[str] = set()
        for claim in claims:
            try:
                event = getattr(claim, "event", None)
                delivery = getattr(claim, "delivery", None)
            except Exception as error:
                raise CompletionOutboxWorkerError(
                    "TBM_COMPLETION_OUTBOX_WORKER_CLAIMS_INVALID",
                    "completion outbox authority returned invalid claims",
                ) from error
            if (
                type(event) is not CompletionOutboxEvent
                or type(delivery) is not CompletionOutboxDelivery
                or event.event_id in seen
                or delivery.event_id != event.event_id
                or delivery.status != "leased"
                or delivery.worker_id != worker_id
                or delivery.lease_expires_at is None
                or delivery.attempt_count < 1
            ):
                _invalid_claims()
            seen.add(event.event_id)
            validated.append((event, delivery))
        return tuple(validated)

    def _dispatch(
        self,
        event: CompletionOutboxEvent,
        delivery: CompletionOutboxDelivery,
        *,
        worker_id: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ) -> CompletionOutboxWorkerResult:
        try:
            receipt = self._consumer(event)
            if type(receipt) is not CompletionOutboxConsumerReceipt:
                raise CompletionOutboxConsumerError(
                    "TBM_COMPLETION_OUTBOX_CONSUMER_RECEIPT_INVALID"
                )
        except CompletionOutboxConsumerError as error:
            return self._record_failure(
                delivery,
                worker_id=worker_id,
                error_code=error.error_code,
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            )
        except Exception:
            return self._record_failure(
                delivery,
                worker_id=worker_id,
                error_code="TBM_COMPLETION_OUTBOX_CONSUMER_FAILED",
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            )

        try:
            acknowledged = self._repository.acknowledge(
                event.event_id,
                expected_version=delivery.version,
                worker_id=worker_id,
                response_sha256=receipt.response_sha256,
            )
        except Exception:
            return self._classify_after_write_failure(delivery)
        self._verify_terminal_receipt(
            delivery,
            acknowledged,
            expected_status="delivered",
            response_sha256=receipt.response_sha256,
        )
        self._verify_retained(acknowledged)
        return self._result(delivery, "delivered", acknowledged)

    def _record_failure(
        self,
        delivery: CompletionOutboxDelivery,
        *,
        worker_id: str,
        error_code: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ) -> CompletionOutboxWorkerResult:
        try:
            failed = self._repository.fail_delivery(
                delivery.event_id,
                expected_version=delivery.version,
                worker_id=worker_id,
                error_code=error_code,
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            )
        except Exception:
            return self._classify_after_write_failure(delivery)
        expected_status: Literal["retry_wait", "dead_letter"] = (
            "dead_letter"
            if delivery.attempt_count >= max_attempts
            else "retry_wait"
        )
        self._verify_terminal_receipt(
            delivery,
            failed,
            expected_status=expected_status,
            error_code=error_code,
            retry_delay_seconds=retry_delay_seconds,
        )
        self._verify_retained(failed)
        return self._result(delivery, expected_status, failed)

    def _classify_after_write_failure(
        self,
        delivery: CompletionOutboxDelivery,
    ) -> CompletionOutboxWorkerResult:
        try:
            current = self._repository.get_delivery(delivery.event_id)
        except Exception as error:
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_RECOVERY_READ_FAILED",
                "completion outbox state could not be read after write failure",
            ) from error
        self._validate_current(delivery, current)
        outcome: CompletionOutboxWorkerOutcome = (
            "recovery_required"
            if current == delivery
            else "superseded"
        )
        return self._result(delivery, outcome, current)

    def _verify_retained(self, expected: CompletionOutboxDelivery) -> None:
        try:
            retained = self._repository.get_delivery(expected.event_id)
        except Exception as error:
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_READ_FAILED",
                "completion outbox delivery receipt could not be read",
            ) from error
        if retained != expected:
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_INVALID",
                "completion outbox delivery receipt was not durably retained",
            )

    @staticmethod
    def _verify_terminal_receipt(
        previous: CompletionOutboxDelivery,
        current: CompletionOutboxDelivery,
        *,
        expected_status: Literal[
            "delivered",
            "retry_wait",
            "dead_letter",
        ],
        response_sha256: str | None = None,
        error_code: str | None = None,
        retry_delay_seconds: int | None = None,
    ) -> None:
        if (
            type(current) is not CompletionOutboxDelivery
            or current.event_id != previous.event_id
            or current.version != previous.version + 1
            or current.attempt_count != previous.attempt_count
            or current.status != expected_status
            or (
                expected_status == "delivered"
                and current.response_sha256 != response_sha256
            )
            or (
                expected_status != "delivered"
                and current.last_error_code != error_code
            )
            or (
                expected_status == "retry_wait"
                and (
                    current.available_at is None
                    or retry_delay_seconds is None
                    or parse_rfc3339(current.available_at)
                    != parse_rfc3339(current.updated_at)
                    + timedelta(seconds=retry_delay_seconds)
                )
            )
        ):
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_INVALID",
                "completion outbox authority returned an invalid receipt",
            )
        try:
            verify_completion_outbox_delivery_transition(previous, current)
        except ValueError as error:
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_INVALID",
                "completion outbox authority returned an invalid receipt",
            ) from error

    @staticmethod
    def _validate_current(
        previous: CompletionOutboxDelivery,
        current: CompletionOutboxDelivery,
    ) -> None:
        if (
            type(current) is not CompletionOutboxDelivery
            or current.event_id != previous.event_id
            or current.contract_version != previous.contract_version
            or current.version < previous.version
            or (
                current.version == previous.version
                and current != previous
            )
        ):
            raise CompletionOutboxWorkerError(
                "TBM_COMPLETION_OUTBOX_WORKER_CURRENT_INVALID",
                "completion outbox authority returned invalid current state",
            )

    @staticmethod
    def _result(
        previous: CompletionOutboxDelivery,
        outcome: CompletionOutboxWorkerOutcome,
        current: CompletionOutboxDelivery,
    ) -> CompletionOutboxWorkerResult:
        return CompletionOutboxWorkerResult(
            event_id=previous.event_id,
            observed_version=previous.version,
            attempt_count=previous.attempt_count,
            outcome=outcome,
            current=current,
        )


def _validate_run_configuration(
    *,
    worker_id: object,
    lease_seconds: object,
    limit: object,
    retry_delay_seconds: object,
    max_attempts: object,
) -> None:
    if (
        type(worker_id) is not str
        or not worker_id
        or len(worker_id) > 128
        or worker_id.strip() != worker_id
        or any(ord(char) < 32 or ord(char) == 127 for char in worker_id)
    ):
        _invalid_configuration()
    if (
        type(lease_seconds) is not int
        or not 1 <= lease_seconds <= COMPLETION_OUTBOX_MAX_LEASE_SECONDS
        or type(limit) is not int
        or not 1 <= limit <= COMPLETION_OUTBOX_WORKER_MAX_PAGE_SIZE
        or type(retry_delay_seconds) is not int
        or not 1
        <= retry_delay_seconds
        <= COMPLETION_OUTBOX_MAX_RETRY_SECONDS
        or type(max_attempts) is not int
        or not 1 <= max_attempts <= COMPLETION_OUTBOX_MAX_ATTEMPTS
    ):
        _invalid_configuration()


def _validate_error_code(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _ERROR_CODE_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise CompletionOutboxWorkerError(
            "TBM_COMPLETION_OUTBOX_WORKER_ERROR_CODE_INVALID",
            "completion outbox consumer error code is invalid",
        )


def _invalid_configuration() -> NoReturn:
    raise CompletionOutboxWorkerError(
        "TBM_COMPLETION_OUTBOX_WORKER_CONFIGURATION_INVALID",
        "completion outbox worker configuration is invalid",
    )


def _invalid_claims() -> NoReturn:
    raise CompletionOutboxWorkerError(
        "TBM_COMPLETION_OUTBOX_WORKER_CLAIMS_INVALID",
        "completion outbox authority returned invalid claims",
    )


__all__ = [
    "COMPLETION_OUTBOX_WORKER_MAX_PAGE_SIZE",
    "CompletionOutboxClaimRecord",
    "CompletionOutboxConsumerError",
    "CompletionOutboxConsumerReceipt",
    "CompletionOutboxDeliveryRepository",
    "CompletionOutboxDeliveryWorker",
    "CompletionOutboxWorkerError",
    "CompletionOutboxWorkerOutcome",
    "CompletionOutboxWorkerResult",
]
