from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_completion_outbox_v3 as outbox_module
from trace_backed_memory.gate_completion_v3 import GateCompletionRequest
from trace_backed_memory.resources import read_packaged_resource


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64
TIMES = tuple(
    f"2026-07-29T00:{minute:02d}:00Z" for minute in range(40)
)


class SequenceClock:
    def __init__(self, values: tuple[str, ...] = TIMES) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return next(self._values)


class FixedClock:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return self.value


def _request(
    *,
    suffix: str = "001",
    **overrides: object,
) -> GateCompletionRequest:
    values: dict[str, object] = {
        "session_id": f"gate_session_{suffix}",
        "expected_version": 6,
        "result": "pass",
        "evaluator_id": "evaluation_service",
        "evaluator_version": "1.2.0",
        "output_sha256": DIGEST_A,
        "evidence_artifact_sha256s": (DIGEST_B,),
        "latency_ms": 250,
        "cost_usd": 0.25,
    }
    values.update(overrides)
    return GateCompletionRequest(**values)  # type: ignore[arg-type]


def _executing(
    repository: tbm.SQLiteCompletionOutboxV3Repository,
    *,
    suffix: str = "001",
) -> None:
    sessions = repository.outcomes.gate_sessions
    created = sessions.create_or_get(
        session_id=f"gate_session_{suffix}",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id=f"trace_{suffix}",
        run_id=f"run_{suffix}",
        request_fingerprint=DIGEST_A,
        idempotency_key=f"request-{suffix}",
        expires_in_seconds=3600,
    ).session
    prepared = sessions.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=1200,
        retrieval_snapshot_id=f"retrieval_{suffix}",
        system_gate_evaluation_id=f"system_gate_{suffix}",
    )
    awaiting = sessions.transition(
        prepared.session_id,
        "awaiting_decision",
        expected_version=prepared.version,
    )
    decided = sessions.transition(
        awaiting.session_id,
        "decided",
        expected_version=awaiting.version,
        semantic_gate_attempt_ids=(f"semantic_attempt_{suffix}",),
        decision_id=f"decision_{suffix}",
    )
    finalized = sessions.transition(
        decided.session_id,
        "finalized",
        expected_version=decided.version,
        final_memory_revision_ids=(REVISION_A,),
        injection_artifact_id=f"injection_{suffix}",
        usage_decision_id=f"usage_{suffix}",
    )
    executing = sessions.transition(
        finalized.session_id,
        "executing",
        expected_version=finalized.version,
    )
    assert executing.status == "executing"


def _repository(
    database: str | Path = ":memory:",
    *,
    clock=None,
    **kwargs: object,
) -> tbm.SQLiteCompletionOutboxV3Repository:
    return tbm.SQLiteCompletionOutboxV3Repository.connect(
        database,
        initialize=True,
        clock=clock or SequenceClock(),
        **kwargs,
    )


def _completed(
    repository: tbm.SQLiteCompletionOutboxV3Repository,
):
    _executing(repository)
    return repository.complete_session(_request())


def test_sqlite_completion_outbox_is_atomic_and_exactly_replayable():
    clock = SequenceClock()
    with _repository(clock=clock) as repository:
        first = _completed(repository)
        calls_after_first = clock.calls

        assert first.completion.inserted is True
        assert first.event_inserted is True
        assert first.completion.session.status == "completed"
        assert first.event.run_outcome_id == (
            first.completion.outcome.run_outcome_id
        )
        assert first.delivery.status == "pending"
        assert repository.get_event(first.event.event_id) == first.event
        assert repository.get_delivery(first.event.event_id) == first.delivery

        replay = repository.complete_session(_request())

        assert replay.completion.inserted is False
        assert replay.event_inserted is False
        assert replay.event == first.event
        assert replay.delivery == first.delivery
        assert clock.calls == calls_after_first


def test_sqlite_completion_outbox_replay_returns_current_delivery_state():
    with _repository() as repository:
        completed = _completed(repository)
        claimed = repository.claim_due(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )[0]
        delivered = repository.acknowledge(
            completed.event.event_id,
            expected_version=claimed.delivery.version,
            worker_id="dispatcher_001",
            response_sha256=DIGEST_A,
        )

        replay = repository.complete_session(_request())

        assert replay.event_inserted is False
        assert replay.delivery == delivered
        assert replay.delivery.status == "delivered"


def test_sqlite_completion_outbox_worker_dispatches_real_claim():
    with _repository() as repository:
        completed = _completed(repository)
        seen: list[str] = []
        worker = tbm.CompletionOutboxDeliveryWorker(
            repository,
            lambda event: (
                seen.append(event.event_id)
                or tbm.CompletionOutboxConsumerReceipt(DIGEST_B)
            ),
        )

        result = worker.run_once(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )

        assert seen == [completed.event.event_id]
        assert len(result) == 1
        assert result[0].outcome == "delivered"
        assert result[0].current.response_sha256 == DIGEST_B
        assert repository.get_delivery(
            completed.event.event_id
        ) == result[0].current
        assert worker.run_once(worker_id="dispatcher_001") == ()


def test_sqlite_completion_outbox_worker_bounds_pages_and_dead_letters():
    with _repository() as repository:
        _executing(repository, suffix="001")
        first = repository.complete_session(_request(suffix="001"))
        _executing(repository, suffix="002")
        second = repository.complete_session(_request(suffix="002"))
        seen: list[str] = []
        worker = tbm.CompletionOutboxDeliveryWorker(
            repository,
            lambda event: (
                seen.append(event.event_id)
                or tbm.CompletionOutboxConsumerReceipt()
            ),
        )

        first_page = worker.run_once(
            worker_id="dispatcher_001",
            limit=1,
        )
        second_page = worker.run_once(
            worker_id="dispatcher_001",
            limit=1,
        )

        assert len(first_page) == len(second_page) == 1
        assert set(seen) == {first.event.event_id, second.event.event_id}
        assert all(
            result.outcome == "delivered"
            for result in first_page + second_page
        )

    with _repository() as repository:
        completed = _completed(repository)
        worker = tbm.CompletionOutboxDeliveryWorker(
            repository,
            lambda _event: (_ for _ in ()).throw(
                tbm.CompletionOutboxConsumerError("PERMANENT")
            ),
        )

        result = worker.run_once(
            worker_id="dispatcher_001",
            max_attempts=1,
        )[0]

        assert result.outcome == "dead_letter"
        assert result.current.last_error_code == "PERMANENT"
        assert repository.get_delivery(
            completed.event.event_id
        ) == result.current


def test_sqlite_completion_outbox_preserves_caller_transaction():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-session.sql",
        "schemas/sqlite-v3-outcome.sql",
        "schemas/sqlite-v3-completion-outbox.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    repository = tbm.SQLiteCompletionOutboxV3Repository(
        connection,
        clock=SequenceClock(),
    )
    _executing(repository)

    connection.execute("BEGIN IMMEDIATE")
    completed = repository.complete_session(_request())
    assert connection.in_transaction is True
    assert repository.get_event(completed.event.event_id) == completed.event
    connection.rollback()

    assert repository.outcomes.gate_sessions.get(
        "gate_session_001"
    ).status == "executing"
    with pytest.raises(tbm.SQLiteOutcomeV3NotFoundError):
        repository.outcomes.get_outcome(
            completed.completion.outcome.run_outcome_id
        )
    with pytest.raises(tbm.SQLiteCompletionOutboxV3NotFoundError):
        repository.get_event(completed.event.event_id)
    repository.close()


def test_sqlite_completion_outbox_shared_connection_mutation_scope():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-session.sql",
        "schemas/sqlite-v3-outcome.sql",
        "schemas/sqlite-v3-completion-outbox.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    first = tbm.SQLiteCompletionOutboxV3Repository(
        connection,
        clock=SequenceClock(),
    )
    second = tbm.SQLiteCompletionOutboxV3Repository(
        connection,
        clock=FixedClock("2026-07-29T00:07:00Z"),
    )
    assert first._lock is second._lock

    completed = _completed(first)
    claimed = second.claim_due(
        worker_id="dispatcher_001",
        lease_seconds=60,
    )

    assert claimed[0].event == completed.event
    assert claimed[0].delivery.status == "leased"
    first.close()
    second.close()
    connection.close()


def test_sqlite_completion_outbox_failure_rolls_back_completion(monkeypatch):
    with _repository() as repository:
        _executing(repository)
        original = repository._insert_bundle

        def fail_after_insert(cursor, event, delivery):
            original(cursor, event, delivery)
            raise sqlite3.IntegrityError("synthetic outbox failure")

        monkeypatch.setattr(repository, "_insert_bundle", fail_after_insert)
        with pytest.raises(tbm.SQLiteCompletionOutboxV3PersistenceError):
            repository.complete_session(_request())

        assert repository.outcomes.gate_sessions.get(
            "gate_session_001"
        ).status == "executing"


def test_sqlite_completion_outbox_does_not_repair_orphaned_outcome():
    with _repository() as repository:
        _executing(repository)
        completion = repository.outcomes.complete_session(_request())

        with pytest.raises(
            tbm.SQLiteCompletionOutboxV3PersistenceError,
            match="no outbox event",
        ):
            repository.complete_session(_request())

        with pytest.raises(tbm.SQLiteCompletionOutboxV3NotFoundError):
            repository.get_event(
                tbm.build_completion_outbox_event(
                    completion.outcome,
                    completion.session,
                ).event_id
            )


def test_sqlite_completion_outbox_claim_ack_and_history():
    with _repository() as repository:
        completed = _completed(repository)
        claims = repository.claim_due(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )

        assert len(claims) == 1
        claim = claims[0]
        assert claim.event == completed.event
        assert claim.delivery.status == "leased"
        delivered = repository.acknowledge(
            claim.event.event_id,
            expected_version=claim.delivery.version,
            worker_id="dispatcher_001",
            response_sha256=DIGEST_A,
        )
        assert delivered.status == "delivered"
        assert repository.claim_due(
            worker_id="dispatcher_002",
            lease_seconds=60,
        ) == ()
        assert tuple(
            value.status
            for value in repository.list_delivery_history(
                claim.event.event_id
            )
        ) == ("pending", "leased", "delivered")


@pytest.mark.parametrize(
    ("worker_id", "lease_seconds"),
    (
        ("", 60),
        ("dispatcher_001", 0),
        ("dispatcher_001", 86_401),
    ),
)
def test_sqlite_completion_outbox_rejects_invalid_empty_queue_claim(
    worker_id: str,
    lease_seconds: int,
):
    with _repository() as repository:
        with pytest.raises(tbm.CompletionOutboxContractError):
            repository.claim_due(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )


def test_sqlite_completion_outbox_retry_and_dead_letter():
    with _repository() as repository:
        completed = _completed(repository)
        first = repository.claim_due(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )[0].delivery
        retry = repository.fail_delivery(
            completed.event.event_id,
            expected_version=first.version,
            worker_id="dispatcher_001",
            error_code="DELIVERY_TIMEOUT",
            retry_delay_seconds=30,
            max_attempts=2,
        )
        assert retry.status == "retry_wait"

        second = repository.claim_due(
            worker_id="dispatcher_002",
            lease_seconds=60,
        )[0].delivery
        dead = repository.fail_delivery(
            completed.event.event_id,
            expected_version=second.version,
            worker_id="dispatcher_002",
            error_code="REMOTE_REJECTED",
            retry_delay_seconds=30,
            max_attempts=2,
        )
        assert dead.status == "dead_letter"
        assert repository.claim_due(
            worker_id="dispatcher_003",
            lease_seconds=60,
        ) == ()


def test_sqlite_completion_outbox_reclaims_expired_lease():
    clock = SequenceClock(
        TIMES[:7]
        + (
            "2026-07-29T00:07:00Z",
            "2026-07-29T00:09:00Z",
        )
    )
    with _repository(clock=clock) as repository:
        completed = _completed(repository)
        first = repository.claim_due(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )[0].delivery
        second = repository.claim_due(
            worker_id="dispatcher_002",
            lease_seconds=60,
        )[0].delivery

        assert second.version == first.version + 1
        assert second.attempt_count == 2
        assert second.worker_id == "dispatcher_002"
        assert repository.get_event(completed.event.event_id) == (
            completed.event
        )


def test_sqlite_completion_outbox_stale_or_wrong_worker_fails_closed():
    with _repository() as repository:
        completed = _completed(repository)
        claimed = repository.claim_due(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )[0].delivery

        with pytest.raises(tbm.SQLiteCompletionOutboxV3ConflictError):
            repository.acknowledge(
                completed.event.event_id,
                expected_version=claimed.version - 1,
                worker_id="dispatcher_001",
            )
        with pytest.raises(tbm.CompletionOutboxContractError, match="own"):
            repository.acknowledge(
                completed.event.event_id,
                expected_version=claimed.version,
                worker_id="dispatcher_other",
            )
        assert repository.get_delivery(
            completed.event.event_id
        ) == claimed


def test_sqlite_completion_outbox_concurrent_claim_has_one_winner(
    tmp_path: Path,
):
    database = tmp_path / "completion-outbox.db"
    with _repository(database) as repository:
        completed = _completed(repository)

    def claim(worker: str):
        with tbm.SQLiteCompletionOutboxV3Repository.connect(
            database,
            clock=FixedClock("2026-07-29T00:07:00Z"),
            timeout=5,
        ) as repository:
            return repository.claim_due(
                worker_id=worker,
                lease_seconds=60,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(claim, ("dispatcher_001", "dispatcher_002"))
        )

    assert sorted(len(result) for result in results) == [0, 1]
    with tbm.SQLiteCompletionOutboxV3Repository.connect(
        database
    ) as repository:
        delivery = repository.get_delivery(completed.event.event_id)
        assert delivery.status == "leased"
        assert delivery.attempt_count == 1


def test_sqlite_completion_outbox_direct_sql_guards_and_schema_drift():
    with _repository() as repository:
        completed = _completed(repository)
        connection = repository._connection
        for statement in (
            "UPDATE v3_completion_outbox_events SET run_id = 'changed'",
            "DELETE FROM v3_completion_outbox_events",
            "UPDATE v3_completion_outbox_delivery_revisions "
            "SET status = 'delivered'",
            "DELETE FROM v3_completion_outbox_delivery_heads",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement)
        connection.rollback()

        connection.execute(
            "CREATE INDEX unexpected_outbox_index "
            "ON v3_completion_outbox_events (event_type)"
        )
        connection.commit()
        with pytest.raises(tbm.SQLiteCompletionOutboxV3SchemaError):
            repository.get_event(completed.event.event_id)


def test_sqlite_completion_outbox_rejects_noncanonical_direct_insert():
    with _repository() as repository:
        completed = _completed(repository)
        row = list(outbox_module._event_row(completed.event))
        row[3] = "repository_other"
        with pytest.raises(sqlite3.IntegrityError, match="invalid"):
            repository._connection.execute(
                "INSERT INTO v3_completion_outbox_events ("
                "event_id, event_type, tenant_id, repository_id, "
                "session_id, trace_id, run_id, usage_decision_id, "
                "run_outcome_id, outcome_descriptor_sha256, occurred_at, "
                "occurred_at_us, descriptor"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )


def test_sqlite_completion_outbox_rejects_canonical_direct_transition():
    with _repository() as repository:
        completed = _completed(repository)
        claimed = repository.claim_due(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )[0].delivery
        delivered = tbm.acknowledge_completion_outbox_delivery(
            claimed,
            worker_id="dispatcher_001",
            acknowledged_at="2026-07-29T00:07:30Z",
        )

        with pytest.raises(sqlite3.IntegrityError, match="invalid"):
            repository._connection.execute(
                "INSERT INTO v3_completion_outbox_delivery_revisions ("
                "event_id, version, delivery_revision_id, status, "
                "attempt_count, updated_at, updated_at_us, available_at, "
                "available_at_us, worker_id, lease_expires_at, "
                "lease_expires_at_us, delivered_at, delivered_at_us, "
                "last_error_code, response_sha256, descriptor"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                outbox_module._delivery_row(delivered),
            )
        repository._connection.rollback()

        retained = repository.acknowledge(
            completed.event.event_id,
            expected_version=claimed.version,
            worker_id="dispatcher_001",
        )
        assert retained.status == "delivered"


def test_sqlite_completion_outbox_resources_exports_and_close():
    assert tbm.SQLITE_COMPLETION_OUTBOX_V3_SCHEMA_VERSION == 1
    assert tbm.COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION == (
        "tbm.completion-outbox-event.v3"
    )
    assert tbm.COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION == (
        "tbm.completion-outbox-delivery.v3"
    )
    assert read_packaged_resource(
        "schemas/sqlite-v3-completion-outbox.sql"
    ) == (
        Path("schemas/sqlite-v3-completion-outbox.sql").read_bytes()
    )
    repository = _repository()
    assert repository.outcomes is repository.outcomes
    repository.close()
    with pytest.raises(tbm.SQLiteCompletionOutboxV3PersistenceError):
        repository.get_event(
            "completion_outbox_event_sha256_" + "f" * 64
        )


@pytest.mark.parametrize(
    ("dependency_error", "expected_type", "expected_code"),
    (
        (
            tbm.SQLiteOutcomeV3SchemaError("DEPENDENCY", "schema"),
            tbm.SQLiteCompletionOutboxV3SchemaError,
            "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
        ),
        (
            tbm.SQLiteOutcomeV3NotFoundError("DEPENDENCY", "missing"),
            tbm.SQLiteCompletionOutboxV3NotFoundError,
            "TBM_SQLITE_COMPLETION_OUTBOX_SESSION_NOT_FOUND",
        ),
        (
            tbm.SQLiteOutcomeV3ConflictError("DEPENDENCY", "conflict"),
            tbm.SQLiteCompletionOutboxV3ConflictError,
            "TBM_SQLITE_COMPLETION_OUTBOX_COMPLETION_CONFLICT",
        ),
        (
            tbm.SQLiteOutcomeV3PersistenceError("DEPENDENCY", "storage"),
            tbm.SQLiteCompletionOutboxV3PersistenceError,
            "TBM_SQLITE_COMPLETION_OUTBOX_DEPENDENCY",
        ),
    ),
)
def test_sqlite_completion_outbox_maps_outcome_dependency_errors(
    monkeypatch,
    dependency_error,
    expected_type,
    expected_code,
):
    with _repository() as repository:
        def fail(_request):
            raise dependency_error

        monkeypatch.setattr(repository._outcomes, "complete_session", fail)
        with pytest.raises(expected_type) as raised:
            repository.complete_session(_request())
        assert raised.value.code == expected_code
        assert raised.value.__cause__ is dependency_error


def test_sqlite_completion_outbox_schema_failures_are_closed(monkeypatch):
    with _repository() as repository:
        repository._connection.execute(
            "DROP TRIGGER "
            "v3_completion_outbox_delivery_revisions_immutable_update"
        )
        with pytest.raises(tbm.SQLiteCompletionOutboxV3SchemaError):
            repository.get_event("completion_outbox_event_sha256_" + "f" * 64)

    with tbm.SQLiteCompletionOutboxV3Repository.connect(
        ":memory:",
        initialize=False,
    ) as repository:
        with pytest.raises(tbm.SQLiteCompletionOutboxV3SchemaError):
            repository.get_event("completion_outbox_event_sha256_" + "f" * 64)

    with pytest.raises(tbm.SQLiteCompletionOutboxV3SchemaError):
        outbox_module._normalized_schema_sql(None)

    outbox_module._canonical_schema_definitions.cache_clear()
    with monkeypatch.context() as scoped:
        def reject_resource(_name):
            raise OSError("synthetic resource failure")

        scoped.setattr(
            outbox_module,
            "read_packaged_resource",
            reject_resource,
        )
        with pytest.raises(tbm.SQLiteCompletionOutboxV3SchemaError):
            outbox_module._canonical_schema_definitions()
    outbox_module._canonical_schema_definitions.cache_clear()


def test_sqlite_completion_outbox_udfs_reject_malformed_values():
    assert outbox_module._event_is_canonical(None) == 0
    assert outbox_module._event_is_canonical(
        *((None,) * 12 + ("not-json",))
    ) == 0
    assert outbox_module._delivery_is_canonical(None) == 0
    assert outbox_module._delivery_is_canonical(
        *((None,) * 16 + ("not-json",))
    ) == 0
    assert outbox_module._transition_is_valid(None, "not-json") == 0
    assert outbox_module._transition_is_valid("not-json", "not-json") == 0


def test_sqlite_completion_outbox_inputs_clock_and_not_found_fail_closed():
    unknown = "completion_outbox_event_sha256_" + "f" * 64
    with _repository() as repository:
        with pytest.raises(TypeError):
            repository.complete_session(object())  # type: ignore[arg-type]
        for operation in (
            repository.get_event,
            repository.get_delivery,
            repository.list_delivery_history,
        ):
            with pytest.raises(ValueError):
                operation(1)  # type: ignore[arg-type]
        with pytest.raises(tbm.SQLiteCompletionOutboxV3NotFoundError):
            repository.get_delivery(unknown)
        with pytest.raises(tbm.SQLiteCompletionOutboxV3NotFoundError):
            repository.list_delivery_history(unknown)
        for limit in (0, 1001):
            with pytest.raises(ValueError):
                repository.claim_due(
                    worker_id="dispatcher_001",
                    lease_seconds=60,
                    limit=limit,
                )
        with pytest.raises(ValueError):
            repository.acknowledge(
                1,  # type: ignore[arg-type]
                expected_version=1,
                worker_id="dispatcher_001",
            )
        with pytest.raises(ValueError):
            repository.acknowledge(
                unknown,
                expected_version=0,
                worker_id="dispatcher_001",
            )

    with _repository(clock=FixedClock("not-a-time")) as repository:
        with pytest.raises(
            tbm.SQLiteCompletionOutboxV3PersistenceError,
            match="clock",
        ):
            repository.claim_due(
                worker_id="dispatcher_001",
                lease_seconds=60,
            )

    with _repository() as repository:
        completed = _completed(repository)
        claimed = repository.claim_due(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )[0].delivery
        repository._clock = FixedClock("2026-07-29T00:06:59Z")
        with pytest.raises(
            tbm.SQLiteCompletionOutboxV3PersistenceError,
            match="backwards",
        ):
            repository.acknowledge(
                completed.event.event_id,
                expected_version=claimed.version,
                worker_id="dispatcher_001",
            )


def test_sqlite_completion_outbox_nested_failure_preserves_outer_transaction(
    monkeypatch,
):
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-session.sql",
        "schemas/sqlite-v3-outcome.sql",
        "schemas/sqlite-v3-completion-outbox.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    repository = tbm.SQLiteCompletionOutboxV3Repository(
        connection,
        clock=SequenceClock(),
    )
    _executing(repository)
    original = repository._insert_bundle

    def fail_after_insert(cursor, event, delivery):
        original(cursor, event, delivery)
        raise sqlite3.IntegrityError("synthetic nested failure")

    monkeypatch.setattr(repository, "_insert_bundle", fail_after_insert)
    connection.execute("BEGIN")
    with pytest.raises(tbm.SQLiteCompletionOutboxV3PersistenceError):
        repository.complete_session(_request())
    assert connection.in_transaction is True
    assert repository.outcomes.gate_sessions.get(
        "gate_session_001"
    ).status == "executing"
    connection.rollback()
    repository.close()
    connection.close()


def test_sqlite_completion_outbox_constructor_and_cleanup_guards():
    with pytest.raises(ValueError):
        tbm.SQLiteCompletionOutboxV3Repository("not-a-connection")  # type: ignore[arg-type]
    connection = sqlite3.connect(":memory:")
    with pytest.raises(ValueError):
        tbm.SQLiteCompletionOutboxV3Repository(
            connection,
            clock=None,  # type: ignore[arg-type]
        )
    connection.close()

    closed = sqlite3.connect(":memory:")
    closed.close()
    with pytest.raises(tbm.SQLiteCompletionOutboxV3PersistenceError):
        tbm.SQLiteCompletionOutboxV3Repository(closed)

    foreign_keys_off = sqlite3.connect(":memory:")
    foreign_keys_off.execute("PRAGMA recursive_triggers = ON")
    foreign_keys_off.execute("BEGIN")
    with pytest.raises(
        tbm.SQLiteCompletionOutboxV3PersistenceError,
        match="foreign keys",
    ):
        tbm.SQLiteCompletionOutboxV3Repository(foreign_keys_off)
    foreign_keys_off.rollback()
    foreign_keys_off.close()

    recursive_off = sqlite3.connect(":memory:")
    recursive_off.execute("PRAGMA foreign_keys = ON")
    recursive_off.execute("PRAGMA recursive_triggers = OFF")
    recursive_off.execute("BEGIN")
    with pytest.raises(
        tbm.SQLiteCompletionOutboxV3PersistenceError,
        match="recursive triggers",
    ):
        tbm.SQLiteCompletionOutboxV3Repository(recursive_off)
    recursive_off.rollback()
    recursive_off.close()

    with pytest.raises(ValueError):
        tbm.SQLiteCompletionOutboxV3Repository.connect(
            ":memory:",
            initialize=1,  # type: ignore[arg-type]
        )

    repository = _repository()
    with repository._allow_mutation():
        with repository._allow_mutation():
            assert outbox_module._mutation_depths()[
                repository._connection_identity
            ] == 2
    repository.close()
    repository.close()


def test_sqlite_completion_outbox_database_error_mapping():
    schema_error = sqlite3.OperationalError("no such table: missing")
    with pytest.raises(tbm.SQLiteCompletionOutboxV3SchemaError):
        outbox_module.SQLiteCompletionOutboxV3Repository._raise_database_error(
            schema_error,
            "ignored",
        )
    persistence_error = sqlite3.OperationalError("disk I/O error")
    with pytest.raises(tbm.SQLiteCompletionOutboxV3PersistenceError):
        outbox_module.SQLiteCompletionOutboxV3Repository._raise_database_error(
            persistence_error,
            "failed operation",
        )
