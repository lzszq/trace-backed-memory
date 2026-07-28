from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import trace_backed_memory as tbm


ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "a" * 64


@dataclass(frozen=True)
class Claim:
    event: tbm.CompletionOutboxEvent
    delivery: tbm.CompletionOutboxDelivery


class ExplodingClaim:
    @property
    def event(self):
        raise RuntimeError("sensitive authority detail")


class FakeRepository:
    def __init__(self) -> None:
        self.event = tbm.loads_completion_outbox_event(
            (ROOT / "examples" / "completion_outbox_event_v3.example.json")
            .read_text(encoding="utf-8")
        )
        self.current = tbm.loads_completion_outbox_delivery(
            (
                ROOT
                / "examples"
                / "completion_outbox_delivery_v3.example.json"
            ).read_text(encoding="utf-8")
        )
        self.times = iter(
            (
                "2026-07-29T00:07:00Z",
                "2026-07-29T00:08:00Z",
                "2026-07-29T00:09:00Z",
            )
        )
        self.claim_error: Exception | None = None
        self.write_mode = "normal"
        self.read_error: Exception | None = None
        self.claim_value: object | None = None
        self.claim_calls: list[tuple[str, int, int]] = []

    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int = 100,
    ):
        self.claim_calls.append((worker_id, lease_seconds, limit))
        if self.claim_error is not None:
            raise self.claim_error
        if self.claim_value is not None:
            return self.claim_value
        self.current = tbm.claim_completion_outbox_delivery(
            self.current,
            worker_id=worker_id,
            claimed_at=next(self.times),
            lease_seconds=lease_seconds,
        )
        return (Claim(self.event, self.current),)

    def get_delivery(self, event_id: str):
        if self.read_error is not None:
            raise self.read_error
        assert event_id == self.event.event_id
        return self.current

    def acknowledge(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        response_sha256: str | None = None,
    ):
        assert event_id == self.event.event_id
        assert expected_version == self.current.version
        if self.write_mode == "fail_before":
            raise RuntimeError("write failed")
        if self.write_mode == "forged_receipt":
            payload: dict[str, object] = {
                "contract_version": (
                    tbm.COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION
                ),
                "event_id": self.current.event_id,
                "version": self.current.version + 1,
                "status": "delivered",
                "attempt_count": self.current.attempt_count,
                "updated_at": "2020-01-01T00:00:00Z",
                "available_at": None,
                "worker_id": None,
                "lease_expires_at": None,
                "delivered_at": "2020-01-01T00:00:00Z",
                "last_error_code": None,
                "response_sha256": response_sha256,
            }
            self.current = tbm.CompletionOutboxDelivery(
                delivery_revision_id=(
                    tbm.completion_outbox_delivery_id(payload)
                ),
                event_id=self.current.event_id,
                version=self.current.version + 1,
                status="delivered",
                attempt_count=self.current.attempt_count,
                updated_at="2020-01-01T00:00:00Z",
                delivered_at="2020-01-01T00:00:00Z",
                response_sha256=response_sha256,
            )
            return self.current
        updated = tbm.acknowledge_completion_outbox_delivery(
            self.current,
            worker_id=worker_id,
            acknowledged_at=next(self.times),
            response_sha256=response_sha256,
        )
        if self.write_mode == "invalid_receipt":
            return self.current
        self.current = updated
        if self.write_mode == "fail_after":
            raise RuntimeError("response lost")
        return updated

    def fail_delivery(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ):
        assert event_id == self.event.event_id
        assert expected_version == self.current.version
        if self.write_mode == "fail_before":
            raise RuntimeError("write failed")
        updated = tbm.fail_completion_outbox_delivery(
            self.current,
            worker_id=worker_id,
            failed_at=next(self.times),
            error_code=error_code,
            retry_delay_seconds=(
                1
                if self.write_mode == "forged_retry"
                else retry_delay_seconds
            ),
            max_attempts=max_attempts,
        )
        if self.write_mode == "invalid_receipt":
            return self.current
        self.current = updated
        if self.write_mode == "fail_after":
            raise RuntimeError("response lost")
        return updated


def _worker(repository: FakeRepository, consumer):
    return tbm.CompletionOutboxDeliveryWorker(repository, consumer)


def test_completion_outbox_worker_delivers_and_retains_receipt():
    repository = FakeRepository()
    seen: list[str] = []
    worker = _worker(
        repository,
        lambda event: (
            seen.append(event.event_id)
            or tbm.CompletionOutboxConsumerReceipt(DIGEST_A)
        ),
    )

    results = worker.run_once(worker_id="worker_001")

    assert seen == [repository.event.event_id]
    assert len(results) == 1
    assert results[0].outcome == "delivered"
    assert results[0].observed_version == 2
    assert results[0].attempt_count == 1
    assert results[0].current == repository.current
    assert repository.current.status == "delivered"
    assert repository.current.response_sha256 == DIGEST_A
    assert repository.claim_calls == [("worker_001", 60, 100)]


def test_completion_outbox_worker_forwards_bounds_and_accepts_empty_page():
    repository = FakeRepository()
    repository.claim_value = ()
    called = False

    def consumer(_event):
        nonlocal called
        called = True
        return tbm.CompletionOutboxConsumerReceipt()

    result = _worker(repository, consumer).run_once(
        worker_id="worker_002",
        lease_seconds=45,
        limit=7,
        retry_delay_seconds=30,
        max_attempts=3,
    )

    assert result == ()
    assert called is False
    assert repository.claim_calls == [("worker_002", 45, 7)]


@pytest.mark.parametrize(
    ("consumer", "error_code"),
    (
        (
            lambda _event: (_ for _ in ()).throw(
                tbm.CompletionOutboxConsumerError("REMOTE_TEMPORARY")
            ),
            "REMOTE_TEMPORARY",
        ),
        (
            lambda _event: (_ for _ in ()).throw(RuntimeError("secret")),
            "TBM_COMPLETION_OUTBOX_CONSUMER_FAILED",
        ),
        (
            lambda _event: object(),
            "TBM_COMPLETION_OUTBOX_CONSUMER_RECEIPT_INVALID",
        ),
    ),
)
def test_completion_outbox_worker_records_sanitized_retry(
    consumer,
    error_code: str,
):
    repository = FakeRepository()

    result = _worker(repository, consumer).run_once(
        worker_id="worker_001",
        retry_delay_seconds=30,
        max_attempts=2,
    )[0]

    assert result.outcome == "retry_wait"
    assert result.current.status == "retry_wait"
    assert result.current.last_error_code == error_code
    assert "secret" not in str(result.current.to_dict())


def test_completion_outbox_worker_dead_letters_at_attempt_limit():
    repository = FakeRepository()
    worker = _worker(
        repository,
        lambda _event: (_ for _ in ()).throw(
            tbm.CompletionOutboxConsumerError("PERMANENT")
        ),
    )

    result = worker.run_once(
        worker_id="worker_001",
        max_attempts=1,
    )[0]

    assert result.outcome == "dead_letter"
    assert result.current.status == "dead_letter"
    assert result.current.last_error_code == "PERMANENT"


@pytest.mark.parametrize(
    ("write_mode", "expected"),
    (
        ("fail_before", "recovery_required"),
        ("fail_after", "superseded"),
    ),
)
def test_completion_outbox_worker_classifies_ack_uncertainty(
    write_mode: str,
    expected: str,
):
    repository = FakeRepository()
    repository.write_mode = write_mode
    worker = _worker(
        repository,
        lambda _event: tbm.CompletionOutboxConsumerReceipt(),
    )

    result = worker.run_once(worker_id="worker_001")[0]

    assert result.outcome == expected
    assert result.current == repository.current


def test_completion_outbox_worker_classifies_failure_write_uncertainty():
    repository = FakeRepository()
    repository.write_mode = "fail_after"
    worker = _worker(
        repository,
        lambda _event: (_ for _ in ()).throw(
            tbm.CompletionOutboxConsumerError("TEMPORARY")
        ),
    )

    result = worker.run_once(worker_id="worker_001")[0]

    assert result.outcome == "superseded"
    assert result.current.status == "retry_wait"


def test_completion_outbox_worker_validates_whole_claim_page_before_callback():
    repository = FakeRepository()
    called = False

    def consumer(_event):
        nonlocal called
        called = True
        return tbm.CompletionOutboxConsumerReceipt()

    repository.claim_value = []
    with pytest.raises(
        tbm.CompletionOutboxWorkerError,
        match="invalid claims",
    ):
        _worker(repository, consumer).run_once(worker_id="worker_001")
    assert called is False

    repository.claim_value = (
        Claim(repository.event, repository.current),
        Claim(repository.event, repository.current),
    )
    with pytest.raises(
        tbm.CompletionOutboxWorkerError,
        match="invalid claims",
    ):
        _worker(repository, consumer).run_once(worker_id="worker_001")
    assert called is False


def test_completion_outbox_worker_sanitizes_exploding_claim_properties():
    repository = FakeRepository()
    repository.claim_value = (ExplodingClaim(),)
    worker = _worker(
        repository,
        lambda _event: tbm.CompletionOutboxConsumerReceipt(),
    )

    with pytest.raises(tbm.CompletionOutboxWorkerError) as captured:
        worker.run_once(worker_id="worker_001")

    assert (
        captured.value.code
        == "TBM_COMPLETION_OUTBOX_WORKER_CLAIMS_INVALID"
    )
    assert "sensitive authority detail" not in str(captured.value)


def test_completion_outbox_worker_maps_repository_failures():
    repository = FakeRepository()
    repository.claim_error = RuntimeError("database details")
    worker = _worker(
        repository,
        lambda _event: tbm.CompletionOutboxConsumerReceipt(),
    )

    with pytest.raises(tbm.CompletionOutboxWorkerError) as captured:
        worker.run_once(worker_id="worker_001")
    assert captured.value.code == "TBM_COMPLETION_OUTBOX_WORKER_CLAIM_FAILED"
    assert "database details" not in str(captured.value)

    repository = FakeRepository()
    repository.write_mode = "fail_before"
    repository.read_error = RuntimeError("read details")
    worker = _worker(
        repository,
        lambda _event: tbm.CompletionOutboxConsumerReceipt(),
    )
    with pytest.raises(tbm.CompletionOutboxWorkerError) as captured:
        worker.run_once(worker_id="worker_001")
    assert (
        captured.value.code
        == "TBM_COMPLETION_OUTBOX_WORKER_RECOVERY_READ_FAILED"
    )
    assert "read details" not in str(captured.value)


def test_completion_outbox_worker_rejects_invalid_receipts_and_configuration():
    assert "CompletionOutboxDeliveryWorker" in tbm.__all__
    assert tbm.COMPLETION_OUTBOX_WORKER_MAX_PAGE_SIZE == 1000
    with pytest.raises(tbm.CompletionOutboxWorkerError):
        tbm.CompletionOutboxConsumerReceipt("not-a-digest")
    with pytest.raises(tbm.CompletionOutboxWorkerError):
        tbm.CompletionOutboxConsumerError("bad\ncode")
    with pytest.raises(TypeError):
        tbm.CompletionOutboxDeliveryWorker(FakeRepository(), None)

    worker = _worker(
        FakeRepository(),
        lambda _event: tbm.CompletionOutboxConsumerReceipt(),
    )
    for values in (
        {"worker_id": ""},
        {"worker_id": "worker", "lease_seconds": 0},
        {"worker_id": "worker", "limit": 0},
        {"worker_id": "worker", "limit": 1001},
        {"worker_id": "worker", "retry_delay_seconds": 0},
        {"worker_id": "worker", "max_attempts": 0},
    ):
        with pytest.raises(tbm.CompletionOutboxWorkerError):
            worker.run_once(**values)


def test_completion_outbox_worker_rejects_invalid_authority_receipt():
    repository = FakeRepository()
    repository.write_mode = "invalid_receipt"
    worker = _worker(
        repository,
        lambda _event: tbm.CompletionOutboxConsumerReceipt(),
    )

    with pytest.raises(tbm.CompletionOutboxWorkerError) as captured:
        worker.run_once(worker_id="worker_001")
    assert (
        captured.value.code
        == "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_INVALID"
    )

    repository = FakeRepository()
    repository.write_mode = "forged_retry"
    worker = _worker(
        repository,
        lambda _event: (_ for _ in ()).throw(
            tbm.CompletionOutboxConsumerError("TEMPORARY")
        ),
    )
    with pytest.raises(tbm.CompletionOutboxWorkerError) as captured:
        worker.run_once(
            worker_id="worker_001",
            retry_delay_seconds=60,
        )
    assert (
        captured.value.code
        == "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_INVALID"
    )

    repository = FakeRepository()
    repository.write_mode = "forged_receipt"
    worker = _worker(
        repository,
        lambda _event: tbm.CompletionOutboxConsumerReceipt(),
    )
    with pytest.raises(tbm.CompletionOutboxWorkerError) as captured:
        worker.run_once(worker_id="worker_001")
    assert (
        captured.value.code
        == "TBM_COMPLETION_OUTBOX_WORKER_RECEIPT_INVALID"
    )
